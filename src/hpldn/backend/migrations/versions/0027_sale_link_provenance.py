"""
Record which Price Paid sale each ``ppd_table`` row is, and how it was linked.

Before this revision ``ppd_table`` had no ``transactionid`` and no record of the evidence
that placed each sale on its UPRN, so "held" was inferred from (postcode,
date, price) and no link could be audited or reverted by source. This
revision adds both, to ``staging`` and ``production``:

- ``transactionid`` (unique where set): back-filled from the evidence that made
  the row, strongest first -- UCL's ``staging.ukhpucl`` (same UPRN, date and
  price), then the tiered matcher's linkage tables (0006 fusion, 0017
  back-fill, 0025 history; same address, date and price), then a sale that is
  the only eligible London Price Paid record with the row's (postcode, date,
  price) and the row the only one with it. A row with none of those keeps a
  NULL, and a sale two rows claim (the same sale held at two addresses) goes
  to the first only; both are counted.
- ``link_method``: ``ucl``, ``epc_t1``..``epc_t4`` (the matcher tier),
  ``epc_matcher`` (0025's history rows, whose linkage table a later run
  overwrote, so the tier is unknown), or ``unrecorded``.

It creates the empty ledger ``staging.ppd_uprn_link`` that 0028's linking
tiers write, and ``staging.ppd_link_precision`` for their precision gate
(`hpldn.backend.scripts.link_precision`), and loads the two sources both
revisions share: HMLR's PPD->UPRN lookups (``staging.hmlr_uprn_lookup``) and
ONS NSUL's London UPRNs (``staging.nsul_london``).

Finally it audits the existing links against HMLR's lookup, per method, and
counts the matcher's tier-4 links ("the postcode has one EPC UPRN") whose
postcode NSUL says holds several UPRNs, which that tier could not see. Those
are flagged ``epc_t4_unsafe`` rather than deleted: moving a held sale between
addresses would invalidate its per-sale features, and every one is still in
the sale's own postcode.

``production.london_houses`` is not touched (it reads neither column).

Revision ID: 0027
Revises: 0026
"""

from pathlib import Path

from sqlalchemy import text

from hpldn.backend.migrations import sale_linking as sl
from hpldn.backend.migrations.chain import no_downgrade, vacuum_analyze
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
    require_data_path,
)
from hpldn.backend.common.tuning import apply_bulk_session_tuning

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

# production.ppd_table's max id before 0025 inserted the 1995-2023 history.
PRE_0025_PPD_MAX_ID = 8_113_758

# Measured on hpdbldn at 0026 (see the log lines beside each check): 2.96M
# rows, of which ~2.69M carry UCL evidence and ~0.15M the matcher's linkage.
MIN_NAMED_SHARE = 0.97
# HMLR agreement of the links held today (7,079 / 7,139 = 99.2 % when measured).
MIN_HMLR_AGREEMENT = 0.985

_LINKAGE_TABLES = (
    steps._PPD_UPRN_LINKAGE_TABLE,
    steps.PPD_UPRN_LINKAGE_BACKFILL_TABLE,
    steps.PPD_UPRN_LINKAGE_HISTORY_TABLE,
)

_ROWS_TABLE = "staging._ppd_provenance_rows"
_CLAIMS_TABLE = "staging._ppd_provenance_claims"

_ROWS_SQL = f"""
CREATE TABLE {_ROWS_TABLE} AS
SELECT p.id, p.address_id, p.dateoftransfer, p.price,
       staging.hpldn_pcnorm(a.postcode) AS pc, uk."UPRN" AS uprn
FROM staging.ppd_table p
JOIN staging.address_table a ON a.id = p.address_id
JOIN staging.ukhcs_table uk ON uk.id = a.uid
"""

# Each source's claims: (row id, transactionid, method, priority).
_UCL_CLAIMS_SQL = f"""
INSERT INTO {_CLAIMS_TABLE} (id, transactionid, method, priority)
SELECT r.id, u.transactionid, 'ucl', 1
FROM {_ROWS_TABLE} r
JOIN staging.ukhpucl u
  ON u."UPRN" = r.uprn AND u.price = r.price AND u.dateoftransfer::date = r.dateoftransfer
"""

_LINKAGE_CLAIMS_SQL = f"""
INSERT INTO {_CLAIMS_TABLE} (id, transactionid, method, priority)
SELECT r.id, l.transactionid, 'epc_t' || l.tier, 2
FROM {_ROWS_TABLE} r
JOIN {{linkage}} l
  ON l.address_id = r.address_id AND l.dateoftransfer = r.dateoftransfer AND l.price = r.price
"""

# A sale that is the only eligible record with the row's key, for a row that
# is the only one with it.
_KEY_CLAIMS_SQL = f"""
INSERT INTO {_CLAIMS_TABLE} (id, transactionid, method, priority)
SELECT r.id, s.transactionid,
       CASE WHEN r.id > {PRE_0025_PPD_MAX_ID} THEN 'epc_matcher' ELSE 'unrecorded' END, 3
FROM (
    SELECT pc, dateoftransfer, price, min(id) AS id
    FROM {_ROWS_TABLE} GROUP BY 1, 2, 3 HAVING count(*) = 1
) r
JOIN (
    SELECT staging.hpldn_pcnorm(p.postcode) AS pc, p.dateoftransfer::date AS dateoftransfer,
           p.price, min(p.transactionid) AS transactionid
    FROM staging.ppd_domestic p
    WHERE {sl.ELIGIBLE_SALE_PREDICATE}
    GROUP BY 1, 2, 3 HAVING count(*) = 1
) s ON s.pc = r.pc AND s.dateoftransfer = r.dateoftransfer AND s.price = r.price
"""

# One transactionid per row and one row per transactionid, best claim first.
_ASSIGN_SQL = f"""
WITH per_row AS (
    SELECT DISTINCT ON (id) id, transactionid, method, priority
    FROM {_CLAIMS_TABLE}
    ORDER BY id, priority, transactionid
), per_sale AS (
    SELECT DISTINCT ON (transactionid) id, transactionid, method
    FROM per_row
    ORDER BY transactionid, priority, id
)
UPDATE staging.ppd_table p
SET transactionid = s.transactionid, link_method = s.method
FROM per_sale s
WHERE p.id = s.id
"""

_SYNC_PRODUCTION_SQL = """
UPDATE production.ppd_table p
SET transactionid = s.transactionid, link_method = s.link_method
FROM staging.ppd_table s
WHERE s.id = p.id
  AND (p.transactionid IS DISTINCT FROM s.transactionid
       OR p.link_method IS DISTINCT FROM s.link_method)
"""

_UNSAFE_T4_SQL = """
UPDATE staging.ppd_table p
SET link_method = 'epc_t4_unsafe'
FROM staging.address_table a
WHERE a.id = p.address_id
  AND p.link_method = 'epc_t4'
  AND (SELECT count(*) FROM staging.nsul_london n
       WHERE n.postcode_norm = staging.hpldn_pcnorm(a.postcode)) > 1
"""

_HMLR_AUDIT_SQL = """
SELECT p.link_method, count(*), count(*) FILTER (WHERE uk."UPRN" = h.uprn)
FROM staging.ppd_table p
JOIN staging.hmlr_uprn_lookup h ON h.transactionid = p.transactionid
JOIN staging.address_table a ON a.id = p.address_id
JOIN staging.ukhcs_table uk ON uk.id = a.uid
GROUP BY 1 ORDER BY 2 DESC
"""


def _add_columns(engine, log) -> None:
    """Add ``transactionid`` and ``link_method`` to both ``ppd_table``s."""
    with engine.begin() as connection:
        connection.execute(text("SET lock_timeout = '60s'"))
        for schema in ("staging", "production"):
            connection.execute(
                text(
                    f"ALTER TABLE {schema}.ppd_table "
                    "ADD COLUMN IF NOT EXISTS transactionid varchar, "
                    "ADD COLUMN IF NOT EXISTS link_method varchar"
                )
            )
    log.info("0027: staging/production.ppd_table gained transactionid and link_method")


def _backfill(engine, cfg, log) -> None:
    """
    Back-fill ``transactionid`` and ``link_method`` on staging, then copy them to production by id.

    Raises
    ------
    RuntimeError
        If fewer than `MIN_NAMED_SHARE` of the rows get a transactionid.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(sl.LINK_FUNCTIONS_SQL))
        for table in (_ROWS_TABLE, _CLAIMS_TABLE):
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        connection.execute(text(_ROWS_SQL))
        for columns in ("uprn, price", "address_id, dateoftransfer, price", "pc, dateoftransfer, price"):
            connection.execute(text(f"CREATE INDEX ON {_ROWS_TABLE} ({columns})"))
        connection.execute(text(f"ANALYZE {_ROWS_TABLE}"))
        connection.execute(
            text(
                f"CREATE TABLE {_CLAIMS_TABLE} "
                "(id bigint, transactionid varchar, method varchar, priority int)"
            )
        )
        claims = {"ucl": connection.execute(text(_UCL_CLAIMS_SQL)).rowcount}
        for linkage in _LINKAGE_TABLES:
            claims[linkage] = connection.execute(
                text(_LINKAGE_CLAIMS_SQL.format(linkage=linkage))
            ).rowcount
        claims["key"] = connection.execute(text(_KEY_CLAIMS_SQL)).rowcount
        connection.execute(text(f"CREATE INDEX ON {_CLAIMS_TABLE} (id)"))
        connection.execute(text(f"ANALYZE {_CLAIMS_TABLE}"))
        log.info("0027: provenance claims by source: %s", claims)

        connection.execute(text("UPDATE staging.ppd_table SET transactionid = NULL, link_method = NULL "
                 "WHERE transactionid IS NOT NULL OR link_method IS NOT NULL"))
        named = connection.execute(text(_ASSIGN_SQL)).rowcount
        total, contested = connection.execute(
            text(
                f"""
                SELECT (SELECT count(*) FROM staging.ppd_table),
                       (SELECT count(*) FROM (
                            SELECT transactionid FROM (
                                SELECT DISTINCT id, transactionid FROM {_CLAIMS_TABLE}
                            ) c GROUP BY 1 HAVING count(*) > 1
                       ) x)
                """
            )
        ).one()
        by_method = connection.execute(
            text("SELECT coalesce(link_method, '(none)'), count(*) FROM staging.ppd_table GROUP BY 1 ORDER BY 2 DESC")
        ).all()
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_staging_ppd_table_transactionid "
                "ON staging.ppd_table (transactionid) WHERE transactionid IS NOT NULL"
            )
        )
        for table in (_ROWS_TABLE, _CLAIMS_TABLE):
            connection.execute(text(f"DROP TABLE {table}"))
    share = named / total if total else 0.0
    log.info(
        "0027: named %d of %d staging.ppd_table row(s) (%.2f%%); %d sale(s) were claimed by "
        "more than one row (the same sale held twice; the first keeps it). By method: %s",
        named, total, 100 * share, contested, dict(by_method),
    )
    if share < MIN_NAMED_SHARE:
        raise RuntimeError(
            f"0027 named only {share:.1%} of staging.ppd_table (want >= {MIN_NAMED_SHARE:.0%})"
        )


def _sync_production(engine, cfg, log) -> None:
    """Copy the two columns onto ``production.ppd_table`` by id and index them."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        synced = connection.execute(text(_SYNC_PRODUCTION_SQL)).rowcount
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_production_ppd_table_transactionid "
                "ON production.ppd_table (transactionid) WHERE transactionid IS NOT NULL"
            )
        )
    log.info("0027: copied transactionid/link_method onto %d production.ppd_table row(s)", synced)


def _load_shared_sources(engine, cfg, log) -> None:
    """Load HMLR's PPD->UPRN lookups and NSUL's London UPRNs (0028 reads both too)."""
    lookup = require_data_path(
        cfg, "hmlr_uprn_lookup_dir", "data",
        description="migration 0027 (HMLR pp-uprn-lookup-*.csv)", must_be_dir=True,
    )
    csv_fps = sorted(Path(lookup).glob("pp-uprn-lookup-*.csv"), key=lambda p: p.stat().st_mtime)
    if not csv_fps:
        raise FileNotFoundError(f"no pp-uprn-lookup-*.csv in {lookup}")
    sl.load_hmlr_lookups(engine, csv_fps, cfg, log)
    nsul_zip = require_data_path(
        cfg, "nsul_zip_fp", "data/nsul/NSUL_E127_JUN_2026.zip",
        description="migration 0027 (ONS National Statistics UPRN Lookup)",
    )
    sl.load_nsul(engine, Path(nsul_zip), cfg, log)


def _audit(engine, log) -> None:
    """
    Flag unsafe tier-4 links and audit every method against HMLR's lookup.

    Raises
    ------
    RuntimeError
        If the held links agree with HMLR on fewer than `MIN_HMLR_AGREEMENT`.
    """
    with engine.begin() as connection:
        connection.execute(text(sl.PRECISION_DDL))
        unsafe = connection.execute(text(_UNSAFE_T4_SQL)).rowcount
        connection.execute(
            text(
                "UPDATE production.ppd_table p SET link_method = s.link_method "
                "FROM staging.ppd_table s WHERE s.id = p.id AND s.link_method = 'epc_t4_unsafe' "
                "AND p.link_method IS DISTINCT FROM s.link_method"
            )
        )
        rows = connection.execute(text(_HMLR_AUDIT_SQL)).all()
        for method, n, agree in rows:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {sl.PRECISION_TABLE} (method, ground_truth, n, agree, precision, passed)
                    VALUES (:method, 'hmlr:held-audit', :n, :agree, :precision, true)
                    """
                ),
                {"method": method or "(none)", "n": n, "agree": agree, "precision": agree / n},
            )
    total = sum(n for _, n, _ in rows)
    agreeing = sum(a for _, _, a in rows)
    log.info(
        "0027: %d tier-4 link(s) are in postcodes NSUL gives several UPRNs; flagged epc_t4_unsafe",
        unsafe,
    )
    for method, n, agree in rows:
        log.info("0027: HMLR agrees with %d of %d held %s link(s) (%.2f%%)", agree, n, method, 100 * agree / n)
    share = agreeing / total if total else 0.0
    log.info("0027: HMLR agrees with %d of %d held links overall (%.2f%%)", agreeing, total, 100 * share)
    if share < MIN_HMLR_AGREEMENT:
        raise RuntimeError(
            f"0027: held links agree with HMLR's lookup on {share:.2%} (want >= {MIN_HMLR_AGREEMENT:.1%})"
        )


def upgrade() -> None:
    """Add and back-fill the provenance columns, create the ledger, load shared sources, audit."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    _add_columns(engine, log)
    _backfill(engine, cfg, log)
    _sync_production(engine, cfg, log)
    with engine.begin() as connection:
        connection.execute(text(sl.LEDGER_DDL))
        connection.execute(text(sl.PRECISION_DDL))
    _load_shared_sources(engine, cfg, log)
    _audit(engine, log)
    vacuum_analyze(engine, ("staging.ppd_table", "production.ppd_table"), log)
    log.info(
        "0027: complete. Next: migration 0028 links the sales ppd_table still lacks. "
        "Run src/hpldn/backend/db/sql/db_grants_post_migration.sql afterwards."
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
