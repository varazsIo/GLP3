"""
Enrich the promoted EPC and PPD tables.

In order: ``floor_level``; the fusion-gap and London-history EPC backfills;
``floor_level`` again for the backfilled rows; domain bounds and
``construction_age_band`` normalisation on both EPC tables; and the ``oldnew``
new-build flag on both PPD tables. Every statement is guarded on the value it
writes, so a re-run changes nothing.

Revision ID: 0008
Revises: 0007
"""

from datetime import date

from sqlalchemy import text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Domain bounds: generous, to remove impossible values only.
MIN_PLAUSIBLE_FLOOR_AREA_SQM = 15.0
MAX_PLAUSIBLE_FLOOR_AREA_SQM = 2_000.0
MIN_HABITABLE_ROOMS = 1
MAX_HABITABLE_ROOMS = 30

_EPC_SERVING_TABLES = ("staging.epc_table", "production.epc_table")
_PPD_SERVING_TABLES = ("staging.ppd_table", "production.ppd_table")

_BOUND_FLOOR_AREA_SQL = """
UPDATE {table}
SET total_floor_area = NULL
WHERE total_floor_area IS NOT NULL
  AND (total_floor_area < :min_area OR total_floor_area > :max_area)
"""

_BOUND_HABITABLE_ROOMS_SQL = """
UPDATE {table}
SET number_habitable_rooms = NULL
WHERE number_habitable_rooms IS NOT NULL
  AND (number_habitable_rooms < :min_rooms OR number_habitable_rooms > :max_rooms)
"""

_NORMALISE_AGE_BAND_SQL = r"""
UPDATE {table}
SET construction_age_band = substring(construction_age_band from '\d{4}(?:-\d{4})?')
WHERE construction_age_band IS NOT NULL
  AND construction_age_band
      IS DISTINCT FROM substring(construction_age_band from '\d{4}(?:-\d{4})?')
"""

_MALFORMED_BANDS_SQL = """
SELECT count(*) FROM {table}
WHERE construction_age_band IS NOT NULL
  AND construction_age_band !~ '^[0-9]{{4}}(-[0-9]{{4}})?$'
"""

# oldnew: ukhpucl is authoritative to SOURCE_CUTOVER_DATE, ppd_domestic after;
# coverage below MIN_OLDNEW_COVERAGE means the staging sources have moved.
SOURCE_CUTOVER_DATE = date(2024, 10, 31)

MIN_OLDNEW_COVERAGE = 0.99

_OLDNEW_LOOKUP_SQL = f"""
DROP TABLE IF EXISTS staging._ppd_oldnew_key;
DROP TABLE IF EXISTS staging._ppd_oldnew_lu;

CREATE UNLOGGED TABLE staging._ppd_oldnew_key AS
SELECT p.id AS ppd_id, '{{schema}}'::text AS src_schema, a.postcode,
       p.price, p.dateoftransfer
FROM {{schema}}.ppd_table p
JOIN {{schema}}.address_table a ON a.id = p.address_id;

CREATE INDEX ON staging._ppd_oldnew_key (postcode, price, dateoftransfer);
ANALYZE staging._ppd_oldnew_key;

CREATE UNLOGGED TABLE staging._ppd_oldnew_lu AS
SELECT s.postcode,
       s.price,
       s.dateoftransfer,
       -- A key whose source rows disagree resolves to NULL, never to a
       -- guess. staging.ukhpucl is EPC-fused, so one transaction appears
       -- once per matching certificate; those agree and collapse here.
       CASE WHEN count(DISTINCT s.oldnew) = 1 THEN min(s.oldnew) END AS oldnew
FROM (
    SELECT upper(btrim(u.postcode)) AS postcode,
           u.price,
           u.dateoftransfer::date AS dateoftransfer,
           u.oldnew
    FROM staging.ukhpucl u
    WHERE u.postcode IS NOT NULL
      AND u.oldnew IS NOT NULL
      AND u.dateoftransfer::date <= DATE '{SOURCE_CUTOVER_DATE}'
    UNION ALL
    SELECT upper(btrim(d.postcode)),
           d.price,
           d.dateoftransfer::date,
           d.oldnew
    FROM staging.ppd_domestic d
    WHERE d.postcode IS NOT NULL
      AND d.oldnew IS NOT NULL
      AND d.dateoftransfer::date > DATE '{SOURCE_CUTOVER_DATE}'
) s
WHERE EXISTS (
    SELECT 1 FROM staging._ppd_oldnew_key k
    WHERE k.postcode = s.postcode
      AND k.price = s.price
      AND k.dateoftransfer = s.dateoftransfer
)
GROUP BY 1, 2, 3;

CREATE INDEX ON staging._ppd_oldnew_lu (postcode, price, dateoftransfer);
ANALYZE staging._ppd_oldnew_lu;
"""

_OLDNEW_BACKFILL_SQL = """
UPDATE {schema}.ppd_table p
SET oldnew = lu.oldnew
FROM staging._ppd_oldnew_key k
JOIN staging._ppd_oldnew_lu lu
  ON lu.postcode = k.postcode
 AND lu.price = k.price
 AND lu.dateoftransfer = k.dateoftransfer
WHERE k.ppd_id = p.id
  AND lu.oldnew IS NOT NULL
"""

_OLDNEW_GUARD_SQL = """
SELECT count(*),
       count(oldnew),
       count(*) FILTER (WHERE oldnew = 'Y'),
       count(*) FILTER (WHERE oldnew IS NOT NULL AND oldnew NOT IN ('Y', 'N'))
FROM {schema}.ppd_table
"""

_DROP_OLDNEW_BUILD_SQL = """
DROP TABLE IF EXISTS staging._ppd_oldnew_key;
DROP TABLE IF EXISTS staging._ppd_oldnew_lu;
"""


def _for(sql: str, table: str) -> str:
    """Substitute `table` for the ``{table}`` placeholder in `sql`."""
    return sql.replace("{table}", table)


def _bound_and_normalise_epc(engine, cfg, log) -> None:
    """
    Bound and normalise both EPC tables.

    ``total_floor_area`` outside [15, 2000] m² and ``number_habitable_rooms``
    outside [1, 30] become NULL, and ``construction_age_band`` is normalised to
    ``YYYY`` or ``YYYY-YYYY``. Applied to staging too, so a re-promote keeps it.

    Raises
    ------
    RuntimeError
        If any band is still malformed afterwards.
    """
    totals = {"floor_area": 0, "habitable_rooms": 0, "age_band": 0}
    leftovers = {}
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        for table in _EPC_SERVING_TABLES:
            area = connection.execute(
                text(_for(_BOUND_FLOOR_AREA_SQL, table)),
                {
                    "min_area": MIN_PLAUSIBLE_FLOOR_AREA_SQM,
                    "max_area": MAX_PLAUSIBLE_FLOOR_AREA_SQM,
                },
            ).rowcount
            rooms = connection.execute(
                text(_for(_BOUND_HABITABLE_ROOMS_SQL, table)),
                {"min_rooms": MIN_HABITABLE_ROOMS, "max_rooms": MAX_HABITABLE_ROOMS},
            ).rowcount
            bands = connection.execute(
                text(_for(_NORMALISE_AGE_BAND_SQL, table))
            ).rowcount
            totals["floor_area"] += area
            totals["habitable_rooms"] += rooms
            totals["age_band"] += bands
            log.info(
                "0008: %s -- nulled total_floor_area on %d row(s) outside "
                "[%s, %s], number_habitable_rooms on %d row(s) outside "
                "[%d, %d], normalised construction_age_band on %d row(s)",
                table, area,
                MIN_PLAUSIBLE_FLOOR_AREA_SQM, MAX_PLAUSIBLE_FLOOR_AREA_SQM,
                rooms, MIN_HABITABLE_ROOMS, MAX_HABITABLE_ROOMS, bands,
            )
        connection.execute(text("ANALYZE " + ", ".join(_EPC_SERVING_TABLES)))
        for table in _EPC_SERVING_TABLES:
            leftovers[table] = connection.execute(
                text(_MALFORMED_BANDS_SQL.format(table=table))
            ).scalar_one()

    if any(leftovers.values()):
        raise RuntimeError(
            "construction_age_band still holds values outside "
            f"'YYYY'/'YYYY-YYYY' after normalisation: {leftovers}. "
            "hpp_rf._construction_age_band_midpoint parses that shape and "
            "would silently produce NULL midpoints for the rest."
        )
    log.info(
        "0008: bounded %d total_floor_area and %d number_habitable_rooms "
        "value(s) and normalised %d construction_age_band value(s) across %s",
        totals["floor_area"], totals["habitable_rooms"], totals["age_band"],
        " and ".join(_EPC_SERVING_TABLES),
    )


def _backfill_oldnew(engine, cfg, log) -> None:
    """
    Backfill ``oldnew`` on both PPD tables.

    Resolved on (postcode, price, dateoftransfer); a key whose source rows
    disagree stays NULL.

    Raises
    ------
    RuntimeError
        If a value is neither 'Y' nor 'N', coverage falls below
        `MIN_OLDNEW_COVERAGE`, or no new build is found.
    """
    for schema in ("staging", "production"):
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            connection.execute(text(_OLDNEW_LOOKUP_SQL.format(schema=schema)))
            keys, ambiguous = connection.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE oldnew IS NULL) "
                    "FROM staging._ppd_oldnew_lu"
                )
            ).one()
            log.info(
                "0008: %s -- (postcode, price, dateoftransfer) -> oldnew "
                "lookup holds %d key(s), %d ambiguous and left NULL",
                schema, keys, ambiguous,
            )
            updated = connection.execute(
                text(_OLDNEW_BACKFILL_SQL.format(schema=schema))
            ).rowcount
            connection.execute(text(_DROP_OLDNEW_BUILD_SQL))
            connection.execute(text(f"ANALYZE {schema}.ppd_table"))

        with engine.connect() as connection:
            rows, resolved, new_builds, bad = connection.execute(
                text(_OLDNEW_GUARD_SQL.format(schema=schema))
            ).one()

        coverage = resolved / rows if rows else 0.0
        log.info(
            "0008: %s.ppd_table -- backfilled %d row(s); %d of %d resolved "
            "(%.2f%%), %d new builds ('Y')",
            schema, updated, resolved, rows, coverage * 100.0, new_builds,
        )
        if bad:
            raise RuntimeError(
                f"{bad} {schema}.ppd_table row(s) carry an oldnew value that "
                "is neither 'Y' nor 'N' -- the source column's domain moved"
            )
        if coverage < MIN_OLDNEW_COVERAGE:
            raise RuntimeError(
                f"only {coverage:.2%} of {schema}.ppd_table rows resolved to "
                f"an oldnew value (want >= {MIN_OLDNEW_COVERAGE:.2%}). The "
                "(postcode, price, dateoftransfer) join validated at ~99.9%; "
                "at this rate the staging sources have moved."
            )
        if not new_builds:
            raise RuntimeError(
                f"no {schema}.ppd_table row resolved to oldnew = 'Y' -- a "
                "constant feature carries no signal, and this join is meant "
                "to find tens of thousands of new builds"
            )


def upgrade() -> None:
    """Run the enrichment steps in dependency order."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0008: deriving production.epc_table.floor_level")
    steps.add_floor_level_to_epc_table(engine, cfg, log)

    log.info("0008: backfilling certificates for the fusion gap")
    steps.backfill_epc_table_for_fusion_gap(engine, cfg, log)

    log.info(
        "0008: backfilling certificates from the %d-%d London history",
        steps.EPC_DOMESTIC_HISTORY_YEAR_START,
        steps.EPC_DOMESTIC_HISTORY_YEAR_END,
    )
    steps.backfill_epc_table_from_london_history(engine, cfg, log)

    log.info("0008: floor_level for the backfilled certificates")
    steps.add_floor_level_to_epc_table(engine, cfg, log)

    log.info("0008: bounding domain outliers and normalising age bands")
    _bound_and_normalise_epc(engine, cfg, log)

    log.info("0008: backfilling the oldnew new-build flag")
    _backfill_oldnew(engine, cfg, log)


def downgrade() -> None:
    """Refuse; bounds and normalisation overwrite values in place with no snapshot."""
    no_downgrade()
