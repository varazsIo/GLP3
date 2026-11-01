"""
Give every address a unit, and an address row to every certified London home without one.

Two gaps kept homes out of ``GET /addresses/search``:

- **No unit.** 0005 wrote a flat's address as its building's (postnumber =
  PAON, poststreet = street), so "Flat 3, 12 X Road" and "Flat 4, 12 X Road"
  carried the same text and search folded them into one entry. The register's
  SAON is now kept in a nullable ``unit`` column on both ``address_table``s:
  for a sold home, its newest sale's SAON (by ``ppd_table.transactionid``,
  which 0027 named for 99.94 % of rows); for a home only certificates know,
  the unit `address_parse_sql.PARSE_EPC_ADDRESS_SQL` reads from its newest
  certificate. `sale_linking.MINT_FROM_LEDGER_SQL` writes it for later links.
- **No row.** 983,395 London UPRNs with a certificate had no address row
  (0018 minted only the 2024-2026 register's), so they could never be
  suggested. Each gets one from its newest certificate, split by the parser
  into unit, postnumber and street, as 0018's ``_MINT_EPC_ADDRESSES_SQL`` did;
  so do the UPRNs 0029 resolved UPRN-less certificates to.

The parser must agree with the register on at least
`address_parse_sql.MIN_PARSER_AGREEMENT` of the rows that carry a held
sale's own text (0006's parser: 76.8 %). The new rows reach production by
append (no re-promote, which would drop the certificates 0008 back-filled
straight into production), then ``uprn_lookup`` and ``address_coordinates``
by insert, and their certificates by 0019's back-fills. No sale changes, so
``production.london_houses`` and the model are untouched; 0031 rebuilds
``production.address_search`` with the units.

Revision ID: 0030
Revises: 0029
"""

from sqlalchemy import text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations import address_parse_sql as parse
from hpldn.backend.migrations import sale_linking as sl
from hpldn.backend.migrations.chain import load_revision, no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

# London UPRNs with a certificate (and an OS Open UPRN record) still without
# an address row afterwards.
MAX_CERTIFIED_WITHOUT_ROW = 5_000

_ADDRESS_TABLES = ("staging.address_table", "production.address_table")

# The newest certificate of every London UPRN (resolved UPRN-less ones
# included) that has no address row, parsed.
_MINT_BUILD = "staging._epc_mint_addr"
_RESOLVED_HISTORY = "staging._epc_resolved_history"

# UPRN-less certificates with the UPRN 0029 resolved them to, shaped like
# the history, for the mint and the certificate back-fill.
_RESOLVED_HISTORY_SQL = f"""
CREATE TABLE {_RESOLVED_HISTORY} AS
SELECT h.*
FROM staging.epc_domestic_history h
WHERE false
"""

_RESOLVED_HISTORY_FILL_SQL = f"""
INSERT INTO {_RESOLVED_HISTORY}
SELECT h.*
FROM staging.epc_domestic_history h
JOIN {sl.RESOLVED_TABLE} r ON r.certificate_number = h.certificate_number
WHERE h.region = 'E12000007'
"""

_MINT_BUILD_SQL = f"""
CREATE TABLE {_MINT_BUILD} AS
WITH newest AS (
    SELECT DISTINCT ON (c.uprn) c.*
    FROM (
        SELECT h.uprn, h.address1, h.address2, h.address3, h.posttown, h.local_authority_label,
               h.postcode, h.lodgement_datetime, h.certificate_number
        FROM staging.epc_domestic_history h
        JOIN staging.nsul_london n ON n.uprn = h.uprn
        WHERE h.region = 'E12000007'
          AND NOT EXISTS (SELECT 1 FROM {sl.RESOLVED_TABLE} r WHERE r.certificate_number = h.certificate_number)
        UNION ALL
        SELECT h.uprn, h.address1, h.address2, h.address3, h.posttown, h.local_authority_label,
               h.postcode, h.lodgement_datetime, h.certificate_number
        FROM {_RESOLVED_HISTORY} h
    ) c
    JOIN staging.ukhcs_table uk ON uk."UPRN" = c.uprn
    WHERE NOT EXISTS (SELECT 1 FROM staging.address_table a WHERE a.uid = uk.id)
    ORDER BY c.uprn, c.lodgement_datetime DESC NULLS LAST, c.certificate_number DESC
)
SELECT uk.id AS uid, x.unit, x.postnumber, x.poststreet,
       upper(btrim(coalesce(NULLIF(btrim(e.posttown), ''), e.local_authority_label, 'LONDON'))) AS posttown,
       coalesce(n.postcode, NULLIF(btrim(e.postcode), '')) AS postcode
FROM newest e
JOIN staging.ukhcs_table uk ON uk."UPRN" = e.uprn
LEFT JOIN staging.nsul_london n ON n.uprn = e.uprn
CROSS JOIN LATERAL staging.hpldn_parse_epc_address(e.address1, e.address2, e.address3) x
"""

# postnumber and postcode are NOT NULL: a certificate naming only a flat
# ("FLAT 3" / "X ROAD") takes the flat as its number, as 0005 did.
_MINT_SQL = f"""
INSERT INTO staging.address_table (uid, postnumber, poststreet, posttown, postcode, postcounty, unit)
SELECT uid, coalesce(postnumber, unit), poststreet, left(posttown, 255), postcode, 'GREATER LONDON',
       CASE WHEN postnumber IS NOT NULL THEN unit END
FROM {_MINT_BUILD}
WHERE coalesce(postnumber, unit) IS NOT NULL AND postcode IS NOT NULL AND length(postcode) <= 8
ON CONFLICT (uid) DO NOTHING
"""

# A sold home's unit: the SAON of its newest sale.
_SOLD_UNIT_SQL = """
UPDATE staging.address_table a
SET unit = s.unit
FROM (
    SELECT DISTINCT ON (p.address_id) p.address_id, NULLIF(upper(btrim(d.saon)), '') AS unit
    FROM staging.ppd_table p
    JOIN staging.ppd_domestic d ON d.transactionid = p.transactionid
    ORDER BY p.address_id, p.dateoftransfer DESC, p.transactionid DESC
) s
WHERE a.id = s.address_id AND s.unit IS NOT NULL AND a.unit IS DISTINCT FROM s.unit
"""

# An unsold home's unit: its newest certificate's, as the parser reads it.
_CERTIFIED_UNIT_SQL = """
UPDATE staging.address_table a
SET unit = x.unit
FROM (
    SELECT DISTINCT ON (h.uprn) h.uprn, h.address1, h.address2, h.address3
    FROM staging.epc_domestic_history h
    WHERE h.region = 'E12000007' AND h.uprn IS NOT NULL
    ORDER BY h.uprn, h.lodgement_datetime DESC NULLS LAST, h.certificate_number DESC
) e
JOIN staging.ukhcs_table uk ON uk."UPRN" = e.uprn
CROSS JOIN LATERAL staging.hpldn_parse_epc_address(e.address1, e.address2, e.address3) x
WHERE a.uid = uk.id AND a.unit IS NULL AND x.unit IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.address_id = a.id)
"""

_SYNC_UNIT_SQL = """
UPDATE production.address_table p
SET unit = s.unit
FROM staging.address_table s
WHERE s.id = p.id AND p.unit IS DISTINCT FROM s.unit
"""

_UPRN_LOOKUP_INSERT_SQL = """
INSERT INTO production.uprn_lookup (uprn, address_id)
SELECT u."UPRN", a.id
FROM staging.address_table a
JOIN staging.ukhcs_table u ON u.id = a.uid
WHERE NOT EXISTS (SELECT 1 FROM production.uprn_lookup l WHERE l.uprn = u."UPRN")
"""

_COORDINATES_INSERT_SQL = """
INSERT INTO production.address_coordinates (address_id, latitude, longitude)
SELECT a.id, u."LATITUDE", u."LONGITUDE"
FROM production.address_table a
LEFT JOIN staging.ukhcs_table u ON u.id = a.uid
WHERE NOT EXISTS (SELECT 1 FROM production.address_coordinates ac WHERE ac.address_id = a.id)
"""

# London UPRNs (NSUL) with a certificate, in OS Open UPRN, and no address row.
_CERTIFIED_WITHOUT_ROW_SQL = f"""
SELECT count(DISTINCT c.uprn)
FROM (
    SELECT h.uprn FROM staging.epc_domestic_history h
    JOIN staging.nsul_london n ON n.uprn = h.uprn
    WHERE h.region = 'E12000007'
    UNION SELECT uprn FROM {sl.RESOLVED_TABLE}
) c
JOIN staging.ukhcs_table uk ON uk."UPRN" = c.uprn
WHERE NOT EXISTS (SELECT 1 FROM staging.address_table a WHERE a.uid = uk.id)
"""


def _install(engine, log) -> None:
    """Add ``unit`` to both address tables and install the parser and the unit key."""
    with engine.begin() as connection:
        for table in _ADDRESS_TABLES:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS unit VARCHAR"))
        connection.execute(text(sl.LINK_FUNCTIONS_SQL))
        connection.execute(text(sl.RESOLVED_DDL))
        connection.execute(text(parse.PARSE_EPC_ADDRESS_SQL))
        connection.execute(text(parse.UNIT_KEY_SQL))
    log.info("0030: unit columns, staging.hpldn_parse_epc_address and %s installed", parse.UNIT_KEY_FUNCTION_NAME)


def _check_parser(engine, cfg, log) -> float:
    """
    Check the certificate parser against the register.

    Raises
    ------
    RuntimeError
        If it agrees with fewer than `address_parse_sql.MIN_PARSER_AGREEMENT`
        of the register rows it is compared with.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        compared, agree = connection.execute(text(parse.PARSER_AGREEMENT_SQL)).one()
    share = agree / compared if compared else 0.0
    log.info(
        "0030: the certificate parser agrees with the register's number and street on %d of %d "
        "UPRN(s) (%.2f%%; 0006's parser 76.8%%)", agree, compared, 100 * share,
    )
    if share < parse.MIN_PARSER_AGREEMENT:
        raise RuntimeError(
            f"0030: the parser agrees with the register on {share:.2%} "
            f"(want >= {parse.MIN_PARSER_AGREEMENT:.0%})"
        )
    return share


def _mint_certified_homes(engine, cfg, log) -> int:
    """Mint an address row, unit included, for every certified London UPRN without one."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        for table in (_RESOLVED_HISTORY, _MINT_BUILD):
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        connection.execute(text(_RESOLVED_HISTORY_SQL))
        resolved = connection.execute(text(_RESOLVED_HISTORY_FILL_SQL)).rowcount
        connection.execute(
            text(
                f"UPDATE {_RESOLVED_HISTORY} h SET uprn = r.uprn "
                f"FROM {sl.RESOLVED_TABLE} r WHERE r.certificate_number = h.certificate_number"
            )
        )
        connection.execute(text(f"ANALYZE {_RESOLVED_HISTORY}"))
        connection.execute(text(_MINT_BUILD_SQL))
        candidates = connection.execute(text(f"SELECT count(*) FROM {_MINT_BUILD}")).scalar_one()
        minted = connection.execute(text(_MINT_SQL)).rowcount
        connection.execute(text("ANALYZE staging.address_table"))
    log.info(
        "0030: minted %d staging.address_table row(s) for %d certified London UPRN(s) without one "
        "(%d resolved UPRN-less certificate(s) among the sources)", minted, candidates, resolved,
    )
    return minted


def _backfill_units(engine, cfg, log) -> None:
    """Fill ``unit`` on existing rows: sold homes from their newest sale, the rest from their certificate."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        sold = connection.execute(text(_SOLD_UNIT_SQL)).rowcount
        certified = connection.execute(text(_CERTIFIED_UNIT_SQL)).rowcount
        connection.execute(text("ANALYZE staging.address_table"))
    log.info("0030: unit set on %d sold address(es) from their newest sale's SAON, %d unsold from a certificate",
             sold, certified)


def _propagate(engine, cfg, log) -> None:
    """Append the new rows to production, copy units across, then extend ``uprn_lookup`` and coordinates."""
    appended = steps.append_new_staging_rows_to_production(engine, cfg, log, tables=("address_table",))
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        synced = connection.execute(text(_SYNC_UNIT_SQL)).rowcount
        lookups = connection.execute(text(_UPRN_LOOKUP_INSERT_SQL)).rowcount
        coordinates = connection.execute(text(_COORDINATES_INSERT_SQL)).rowcount
        for table in ("production.address_table", "production.uprn_lookup", "production.address_coordinates"):
            connection.execute(text(f"ANALYZE {table}"))
    log.info(
        "0030: appended %s; unit synced onto %d production row(s); %d uprn_lookup and %d "
        "address_coordinates row(s) added", appended, synced, lookups, coordinates,
    )


def _backfill_certificates(engine, cfg, log) -> None:
    """Give the new rows their certificates: resolved UPRN-less ones, then the history (0019), then types."""
    repairs = load_revision("0019")
    steps.backfill_epc_table_from_london_history(engine, cfg, log, history_table=_RESOLVED_HISTORY)
    repairs._backfill_certificates(engine, cfg, log)
    repairs._backfill_property_type(engine, cfg, log)


def _check(engine, log) -> None:
    """
    Check the mint and the propagation.

    Raises
    ------
    RuntimeError
        If more than `MAX_CERTIFIED_WITHOUT_ROW` certified London UPRNs still
        have no address row, a ``uid`` is held twice, or staging and
        production disagree on the address rows or their units.
    """
    with engine.connect() as connection:
        without = connection.execute(text(_CERTIFIED_WITHOUT_ROW_SQL)).scalar_one()
        dup_staging, dup_production = (
            connection.execute(text(f"SELECT count(*) - count(DISTINCT uid) FROM {t}")).scalar_one()
            for t in _ADDRESS_TABLES
        )
        s_rows, p_rows, units, mismatched = connection.execute(
            text(
                """
                SELECT (SELECT count(*) FROM staging.address_table),
                       (SELECT count(*) FROM production.address_table),
                       (SELECT count(*) FROM production.address_table WHERE unit IS NOT NULL),
                       (SELECT count(*) FROM production.address_table p
                        JOIN staging.address_table s ON s.id = p.id
                        WHERE p.unit IS DISTINCT FROM s.unit)
                """
            )
        ).one()
    log.info(
        "0030: %d certified London UPRN(s) without an address row; %d/%d duplicate uid(s) "
        "(staging/production); address rows %d/%d, %d with a unit, %d unit mismatch(es)",
        without, dup_staging, dup_production, s_rows, p_rows, units, mismatched,
    )
    failing = []
    if without > MAX_CERTIFIED_WITHOUT_ROW:
        failing.append(f"{without} certified London UPRN(s) without a row (want <= {MAX_CERTIFIED_WITHOUT_ROW})")
    if dup_staging or dup_production:
        failing.append(f"duplicate uid(s): {dup_staging} staging, {dup_production} production")
    if s_rows != p_rows or mismatched:
        failing.append(f"address rows {s_rows} staging vs {p_rows} production, {mismatched} unit mismatch(es)")
    if failing:
        raise RuntimeError("0030: " + "; ".join(failing))


def upgrade() -> None:
    """Install the parser, check it, mint, fill units, propagate, back-fill certificates, check."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    _install(engine, log)
    _check_parser(engine, cfg, log)
    # The certificate back-fill bounds on staging.epc_table_condensed (UNLOGGED).
    steps.restore_crash_reset_london_staging(engine, cfg, log)
    _mint_certified_homes(engine, cfg, log)
    _backfill_units(engine, cfg, log)
    _propagate(engine, cfg, log)
    _backfill_certificates(engine, cfg, log)
    _check(engine, log)
    with engine.begin() as connection:
        for table in (_MINT_BUILD, _RESOLVED_HISTORY):
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
    log.info("0030: complete; 0031 rebuilds production.address_search with the units")


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
