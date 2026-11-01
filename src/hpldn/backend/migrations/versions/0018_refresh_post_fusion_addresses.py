"""
Refresh addresses, streets and parcels after the EPC/PPD fusion.

0005 runs the street and parcel enrichment before 0006 mints addresses and
inserts fused sales, so without this revision:

- 45 % of 2025 sales had no ``poststreet`` (minted addresses take it from an
  often-empty EPC ``address2``), which empties the API's comparables pool;
- 60 % had no ``lot_area_sqm``, because no parcel was ever computed for a
  minted UPRN;
- ``address_table`` would miss 363,900 rows: every London EPC UPRN
  (2024-2026 register) with no sale, which
  ``backfill_epc_table_from_london_history`` then gives certificates.

In order, on staging (0019 propagates to production):

1. mint an address for every London EPC UPRN that has none, from
   ``staging._epc_london_addr`` (0006's parsed EPC address), and derive
   their certificates with 0006's EPC pick;
2. re-run ``add_poststreet_to_address_table`` (ukhpucl streets);
3. fill a missing ``poststreet`` from the Price Paid ``street`` of the
   address's newest linked sale, then from any EPC certificate's
   ``address2``;
4. compute INSPIRE lot areas for every sold UPRN with no parcel row
   (``insert_inspire_polygon_data`` only covers ``london_houses_condensed``).

Every step only fills gaps, so a re-run writes nothing new.

Revision ID: 0018
Revises: 0017
"""

from sqlalchemy import text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations.chain import load_revision, no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# Addresses for London EPC UPRNs with none. postnumber and postcode are NOT
# NULL on address_table, so rows the parser could not fill are skipped.
_MINT_EPC_ADDRESSES_SQL = """
INSERT INTO staging.address_table
    (uid, postnumber, poststreet, posttown, postcode, postcounty)
SELECT u.id, la.postnumber, la.poststreet, la.posttown, la.postcode, la.postcounty
FROM staging._epc_london_addr la
JOIN staging.ukhcs_table u ON u."UPRN" = la.uprn
WHERE NULLIF(la.postnumber, '') IS NOT NULL
  AND NULLIF(la.postcode, '') IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM staging.address_table a WHERE a.uid = u.id)
ON CONFLICT (uid) DO NOTHING
"""

_POSTSTREET_FROM_PPD_BUILD = "staging._poststreet_from_ppd_build"

# Newest linked PPD sale per address, across both linkage tables.
_POSTSTREET_FROM_PPD_BUILD_SQL = f"""
CREATE UNLOGGED TABLE {_POSTSTREET_FROM_PPD_BUILD} AS
SELECT DISTINCT ON (l.address_id)
       l.address_id,
       upper(btrim(d.street)) AS poststreet
FROM (
    SELECT transactionid, address_id, dateoftransfer FROM staging.ppd_uprn_linkage
    UNION ALL
    SELECT transactionid, address_id, dateoftransfer FROM {steps.PPD_UPRN_LINKAGE_BACKFILL_TABLE}
) l
JOIN staging.ppd_domestic d ON d.transactionid = l.transactionid
JOIN staging.address_table a ON a.id = l.address_id
WHERE l.address_id IS NOT NULL
  AND a.poststreet IS NULL
  AND NULLIF(btrim(d.street), '') IS NOT NULL
ORDER BY l.address_id, l.dateoftransfer DESC, l.transactionid DESC
"""

_POSTSTREET_FROM_EPC_BUILD = "staging._poststreet_from_epc_build"

# Old 0077: any certificate's second address line, newest lodgement first.
_POSTSTREET_FROM_EPC_BUILD_SQL = f"""
CREATE UNLOGGED TABLE {_POSTSTREET_FROM_EPC_BUILD} AS
SELECT DISTINCT ON (t.address_id)
       t.address_id,
       upper(NULLIF(btrim(e.address2), '')) AS poststreet
FROM (
    SELECT a.id AS address_id, u."UPRN" AS uprn
    FROM staging.address_table a
    JOIN staging.ukhcs_table u ON u.id = a.uid
    WHERE a.poststreet IS NULL
      AND EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.address_id = a.id)
) t
JOIN staging.epc_domestic_history e ON e.uprn = t.uprn
WHERE NULLIF(btrim(e.address2), '') IS NOT NULL
ORDER BY t.address_id, e.lodgement_datetime DESC NULLS LAST, e.certificate_number DESC
"""

_POSTSTREET_FILL_SQL = """
UPDATE staging.address_table a
SET poststreet = b.poststreet
FROM {build} b
WHERE a.id = b.address_id
  AND a.poststreet IS NULL
"""

_PARCEL_BUILD = "staging._london_parcels_candidates_build"

# Old 0077: every UPRN with a sale and no parcel row yet.
_PARCEL_BUILD_SQL = f"""
CREATE UNLOGGED TABLE {_PARCEL_BUILD} AS
SELECT DISTINCT u."UPRN" AS uprn, u."X_COORDINATE" AS x, u."Y_COORDINATE" AS y
FROM staging.ppd_table p
JOIN staging.address_table a ON a.id = p.address_id
JOIN staging.ukhcs_table u ON u.id = a.uid
WHERE u."X_COORDINATE" IS NOT NULL
  AND u."Y_COORDINATE" IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM staging.london_parcels lp WHERE lp.uprn = u."UPRN")
"""

_PARCEL_INSERT_SQL = f"""
INSERT INTO staging.london_parcels (uprn, lot_area_sqm)
SELECT g.uprn, SUM(ST_Area(i.wkb_geometry)) AS lot_area_sqm
FROM {_PARCEL_BUILD} g
JOIN staging.inspire_polygons i
  ON ST_Contains(i.wkb_geometry, ST_SetSRID(ST_MakePoint(g.x, g.y), 27700))
GROUP BY g.uprn
"""

# Sold addresses still without a street or parcel, for the log and the guard.
# A LEFT JOIN, not NOT EXISTS inside the FILTER: PostgreSQL cannot turn a
# sublink in an aggregate FILTER into an anti-join, and staging.london_parcels
# has no uprn index, so that form seq-scanned 1M parcels per sold address
# (hours). One parcel row per uprn, so the join adds no rows.
_REMAINING_GAPS_SQL = """
SELECT count(*) AS sold_addresses,
       count(*) FILTER (WHERE a.poststreet IS NULL) AS no_street,
       count(*) FILTER (WHERE lp.uprn IS NULL) AS no_parcel
FROM staging.address_table a
JOIN staging.ukhcs_table u ON u.id = a.uid
LEFT JOIN (SELECT DISTINCT uprn FROM staging.london_parcels) lp ON lp.uprn = u."UPRN"
WHERE EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.address_id = a.id)
"""

# Sold addresses without a street after the refresh (~9 % without it).
MAX_SOLD_ADDRESSES_WITHOUT_STREET = 0.05


def _mint_epc_addresses(engine, cfg, log) -> int:
    """Mint addresses for London EPC UPRNs with none, then derive their certificates."""
    fusion = load_revision("0006")
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        minted = connection.execute(text(_MINT_EPC_ADDRESSES_SQL)).rowcount
        connection.execute(text("ANALYZE staging.address_table"))
    log.info("0018: minted %d staging.address_table row(s) for London EPC UPRNs", minted)
    if minted:
        fusion._rebuild_london_epc_pick(engine, log)
        fusion._insert_certificates(engine, log)
    return minted


def _fill_poststreet(engine, cfg, log, build: str, build_sql: str, source: str) -> int:
    """Fill ``poststreet`` where NULL from one build table; returns rows updated."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {build}"))
        connection.execute(text(build_sql))
        connection.execute(text(f"CREATE UNIQUE INDEX ON {build} (address_id)"))
        connection.execute(text(f"ANALYZE {build}"))
        updated = connection.execute(
            text(_POSTSTREET_FILL_SQL.format(build=build))
        ).rowcount
        connection.execute(text(f"DROP TABLE IF EXISTS {build}"))
    log.info("0018: filled poststreet on %d address(es) from %s", updated, source)
    return updated


def _insert_missing_parcels(engine, cfg, log) -> int:
    """Compute lot areas for sold UPRNs with no ``staging.london_parcels`` row."""
    with engine.connect() as connection:
        missing = [
            t
            for t in ("staging.inspire_polygons", "staging.london_parcels")
            if connection.execute(text(f"SELECT to_regclass('{t}')")).scalar() is None
        ]
    if missing:
        raise RuntimeError(
            f"0018 needs {', '.join(missing)}; 0005 builds them with "
            "insert_inspire_polygon_data(full_rebuild=True)"
        )
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {_PARCEL_BUILD}"))
        connection.execute(text(_PARCEL_BUILD_SQL))
        connection.execute(text(f"ANALYZE {_PARCEL_BUILD}"))
        candidates = connection.execute(
            text(f"SELECT count(*) FROM {_PARCEL_BUILD}")
        ).scalar_one()
        inserted = connection.execute(text(_PARCEL_INSERT_SQL)).rowcount
        connection.execute(text(f"DROP TABLE IF EXISTS {_PARCEL_BUILD}"))
        connection.execute(text("ANALYZE staging.london_parcels"))
        min_area = connection.execute(
            text("SELECT min(lot_area_sqm) FROM staging.london_parcels")
        ).scalar_one()
    log.info(
        "0018: %d sold UPRN(s) had no parcel row; %d fall inside an INSPIRE "
        "polygon and were inserted (INSPIRE covers freehold titles only)",
        candidates, inserted,
    )
    if min_area is not None and min_area <= 0:
        raise RuntimeError(
            f"min(staging.london_parcels.lot_area_sqm) is {min_area}; lot_area_sqm "
            "is a denominator downstream and must be > 0"
        )
    return inserted


def upgrade() -> None:
    """Mint, fill streets, add parcels, then check the sold-address street gap."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    _mint_epc_addresses(engine, cfg, log)

    log.info("0018: add_poststreet_to_address_table (ukhpucl streets)")
    # Its inputs are UNLOGGED: after a crash recovery the UPDATE would match nothing.
    steps.restore_crash_reset_london_staging(engine, cfg, log)
    steps.add_poststreet_to_address_table(engine, cfg, log)

    _fill_poststreet(
        engine, cfg, log, _POSTSTREET_FROM_PPD_BUILD, _POSTSTREET_FROM_PPD_BUILD_SQL,
        "the newest linked Price Paid sale's street",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_epc_domestic_history_uprn "
                "ON staging.epc_domestic_history (uprn)"
            )
        )
    _fill_poststreet(
        engine, cfg, log, _POSTSTREET_FROM_EPC_BUILD, _POSTSTREET_FROM_EPC_BUILD_SQL,
        "EPC certificates' second address line",
    )

    _insert_missing_parcels(engine, cfg, log)

    with engine.connect() as connection:
        sold, no_street, no_parcel = connection.execute(text(_REMAINING_GAPS_SQL)).one()
    street_gap = no_street / sold if sold else 0.0
    log.info(
        "0018: %d sold address(es); %d (%.1f%%) without poststreet, %d (%.1f%%) "
        "without a parcel", sold, no_street, 100 * street_gap,
        no_parcel, 100 * no_parcel / sold if sold else 0.0,
    )
    if street_gap > MAX_SOLD_ADDRESSES_WITHOUT_STREET:
        raise RuntimeError(
            f"{street_gap:.1%} of sold addresses still have no poststreet (want <= "
            f"{MAX_SOLD_ADDRESSES_WITHOUT_STREET:.0%}). The API's comparables "
            "require a street, so this gap removes recent sales from the evidence."
        )


def downgrade() -> None:
    """Refuse; the refresh only fills gaps and keeps no record of the empty state."""
    no_downgrade()
