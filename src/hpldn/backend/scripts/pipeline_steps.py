"""
HPLDN staging pipeline steps, called by the alembic revisions.

Every public step takes ``(engine, cfg, log)`` so it can also be run by name
with ``python -m hpldn.backend.scripts.run_pipeline_step <step_name>``. Heavy
statements run on tuned connections, and rebuild steps drop and recreate
their tables so a re-run is safe. See ``scripts/README.md``.
"""

import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import pandas as pd
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from hpldn.backend.common.constants import (
    API_ROLE_WORK_MEM,
    BUILD_FORM_MAPPING,
    ENERGY_TARIFF_MAPPING,
    FLOOR_LEVEL_MAPPING,
    GLAZED_TYPE_MAPPING,
    LONDON_DISTRICTS_SQL,
    MIN_HABITABLE_ROOMS,
    MIN_PLAUSIBLE_ANNUAL_COST,
    MIN_PLAUSIBLE_FLOOR_AREA_SQM,
    MIN_PLAUSIBLE_PRICE,
    RATING_CATEGORY_MAPPING,
    TENURE_MAPPING,
    TRANSACTION_TYPE_MAPPING,
)
from hpldn.backend.common.db import (
    drop_db,
    get_db_url,
    get_formatted_column_names,
    get_psql_env,
    get_psql_url,
)
from hpldn.backend.common.tuning import (
    HostResources,
    apply_bulk_session_tuning,
    detect_host_resources,
    get_bulk_copy_env,
    get_index_jobs,
    get_max_parallel_maintenance_workers,
)
from hpldn.backend.migrations import inputs
from hpldn.backend.models.staging import (
    AppBase,
    BuildFormCategory,
    EPC,
    EnergyTariffCategory,
    GlazedTypeCategory,
    RatingCategory,
    TransactionTypeCategory,
    UKHPUCL,
    app_base_meta,
)




def _emit(log: Optional[logging.Logger], msg: str, level: int = logging.INFO) -> None:
    """Log `msg`, or print it with a timestamp when `log` is None (as alembic calls pass)."""
    if log is None:
        print(
            f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] {msg}",
            flush=True,
        )
    else:
        log.log(level, msg)


@contextmanager
def _timed(log: Optional[logging.Logger], what: str):
    """Wrap a long step in START and FINISH log lines with elapsed seconds."""
    started = datetime.now()
    clock = time.monotonic()
    _emit(log, f"[{started.isoformat(sep=' ', timespec='seconds')}] START  {what}")
    try:
        yield
    except Exception:
        _emit(
            log,
            f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] FAILED  "
            f"{what} after {time.monotonic() - clock:.1f}s",
            logging.ERROR,
        )
        raise
    _emit(
        log,
        f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] FINISH  "
        f"{what} in {time.monotonic() - clock:.1f}s",
    )


def _index_name(index_sql: str) -> str:
    """Return the index name from a CREATE [UNIQUE] INDEX statement."""
    tokens = index_sql.split()
    return tokens[tokens.index("INDEX") + 1]


def _analyze(
    connection, tables: Iterable[str], log: Optional[logging.Logger] = None
) -> None:
    """
    ANALYZE freshly built tables.

    Tables built by CREATE TABLE AS have no statistics, so the planner would
    otherwise misjudge million-row joins.
    """
    for table in tables:
        with _timed(log, f"ANALYZE {table}"):
            connection.execute(text(f"ANALYZE {table}"))


def drop_all_tables(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """Drop every table in the `AppBase` schema. Destructive."""
    drop_db(engine, log)


def _create_index(
    engine: Engine,
    idx_name: str,
    column: str,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
) -> str:
    """Build one index on ``staging.ukhpucl`` on its own connection and return its name."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {idx_name} "
                f"ON {app_base_meta.schema}.ukhpucl ({column});"
            )
        )
    if log is not None:
        log.debug(f"Successfully created {idx_name} in {app_base_meta.schema}.ukhpucl")
    return idx_name


def create_ukhpucl_indexes(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create one btree index per ``staging.ukhpucl`` column, concurrently.

    Concurrency comes from `get_index_jobs`, and each build has its own
    connection, so one failure does not roll back completed indexes. Slow on
    the full dataset; run once.
    """
    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {app_base_meta.schema}.ukhpucl SET UNLOGGED")
        )
        if log is not None:
            log.debug(f"SET {app_base_meta.schema}.ukhpucl to UNLOGGED")

    columns = get_formatted_column_names(UKHPUCL)
    pending = []
    history = set()
    for c in columns:
        c2 = c.replace('"', "").lower()
        idx_str = f"idx_uk_{c2}"
        if idx_str in history:
            if log is not None:
                log.warning(f"Duplicate index {c2} found for column {c}")
            continue
        history.add(idx_str)
        pending.append((idx_str, c))

    resources = detect_host_resources(log)
    jobs = get_index_jobs(resources, log)
    if log is not None:
        log.info(
            "Building %d indexes on %s.ukhpucl, %d at a time "
            "(%d vCPU, %d parallel maintenance workers each)",
            len(pending),
            app_base_meta.schema,
            jobs,
            resources.vcpus,
            get_max_parallel_maintenance_workers(resources),
        )

    if jobs == 1:
        for idx_str, column in pending:
            _create_index(engine, idx_str, column, cfg, log)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [
                pool.submit(_create_index, engine, idx_str, column, cfg, log)
                for idx_str, column in pending
            ]
            for future in futures:
                future.result()

    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {app_base_meta.schema}.ukhpucl SET LOGGED")
        )
        if log is not None:
            log.debug(f"SET {app_base_meta.schema}.ukhpucl to LOGGED")


def create_london_houses_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """Create ``staging.london_houses``, the London-borough subset of ``staging.ukhpucl``."""
    london_houses_sql = text(f"""
    CREATE UNLOGGED TABLE {app_base_meta.schema}.london_houses AS
    SELECT *
    FROM {app_base_meta.schema}.ukhpucl
    WHERE district IN ({LONDON_DISTRICTS_SQL})
    ORDER BY dateoftransfer, postcode;
    """)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        if log is not None:
            log.debug("Creating london_houses table...")
        connection.execute(london_houses_sql)
        if log is not None:
            log.debug("Created london_houses table!")


def populate_ukhcs_table(
    engine: Engine, cfg: Dict, log: Optional[logging.Logger] = None
) -> None:
    r"""
    Bulk-load ``staging.ukhcs_table`` from the OS Open UPRN CSV with psql ``\copy``.

    Phase 1 of the two-phase load; `refresh_ukhcs_table_from_extract` appends
    the newer extract insert-only. The psql process targets `engine` (password
    in ``PGPASSWORD``) with ``ON_ERROR_STOP=1``, the path resolves through
    ``require_data_path``, and the schema is the literal ``staging``.
    """
    from hpldn.backend.scripts.migration_helpers import require_data_path

    file_path = require_data_path(
        cfg,
        "osopenuprn_csv_fp",
        description="OS Open UPRN extract (populate_ukhcs_table)",
    )
    db_url = get_psql_url(engine)
    selected_columns = '("UPRN","X_COORDINATE","Y_COORDINATE","LATITUDE","LONGITUDE")'
    cmd = [
        "psql",
        db_url,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f"\\copy staging.ukhcs_table {selected_columns} FROM "
        f"'{file_path}' WITH (FORMAT CSV, HEADER TRUE)",
    ]
    with _timed(log, "populate_ukhcs_table"):
        subprocess.run(
            cmd, check=True, env=get_psql_env(engine, get_bulk_copy_env(log))
        )
    with engine.begin() as connection:
        _analyze(connection, ("staging.ukhcs_table",), log)
        rows = connection.execute(
            text("SELECT count(*) FROM staging.ukhcs_table")
        ).scalar_one()
    _emit(log, f"staging.ukhcs_table loaded with {rows} rows")


_LOGGED_REFERENCING_TABLES_SQL = text("""
SELECT ns.nspname AS schema_name, cl.relname AS table_name
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = cl.relnamespace
WHERE con.contype = 'f'
  AND con.confrelid = to_regclass(:target)
  AND cl.relpersistence = 'p'
GROUP BY ns.nspname, cl.relname
ORDER BY ns.nspname, cl.relname;
""")


def _get_logged_referencing_tables(connection, target: str) -> list:
    """Return permanent tables with a foreign key onto `target` (``schema.table``)."""
    rows = connection.execute(_LOGGED_REFERENCING_TABLES_SQL, {"target": target}).all()
    return [(row.schema_name, row.table_name) for row in rows]


def _set_persistence(connection, tables: list, persistence: str) -> None:
    """Set every ``(schema, table)`` pair LOGGED or UNLOGGED."""
    for schema_name, table_name in tables:
        connection.execute(
            text(f'ALTER TABLE "{schema_name}"."{table_name}" SET {persistence}')
        )


def create_address_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Populate ``staging.address_table`` from ``staging.ukhpucl`` and ``staging.ukhcs_table``.

    Requires a freshly created, empty address table; slow on the full dataset.
    """
    insert_sql = text("""
    INSERT INTO staging.address_table (uid, postnumber, posttown, postcode, postcounty)
    SELECT DISTINCT ON (ukhcs.id)
        ukhcs.id     AS uid,
        u.paon       AS postnumber,
        u.towncity   AS posttown,
        u.postcode,
        u.county     AS postcounty
    FROM staging.ukhpucl u
    INNER JOIN staging.ukhcs_table ukhcs
        ON ukhcs."UPRN" = u."UPRN"
    WHERE u.paon IS NOT NULL
    ORDER BY ukhcs.id, u.dateoftransfer DESC NULLS LAST;
    """)

    if log is not None:
        log.info(
            "Starting to insert data from ukhpucl and ukhcs_table into address_table..."
        )
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        dependents = _get_logged_referencing_tables(connection, "staging.address_table")
        if log is not None and dependents:
            log.debug(
                "Temporarily setting %s UNLOGGED so address_table can be too",
                ", ".join(f"{s}.{t}" for s, t in dependents),
            )
        _set_persistence(connection, dependents, "UNLOGGED")
        connection.execute(text("ALTER TABLE staging.address_table SET UNLOGGED"))

        connection.execute(insert_sql)

        connection.execute(text("ALTER TABLE staging.address_table SET LOGGED"))
        _set_persistence(connection, dependents, "LOGGED")
    if log is not None:
        log.info(
            "Finished inserting data from ukhpucl and ukhcs_table into address_table!"
        )


def add_poststreet_to_address_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Add and populate ``staging.address_table.poststreet``.

    Idempotent and cheap to repeat: the ``IS DISTINCT FROM`` guard only
    touches rows that changed or are new.
    """
    if log is not None:
        log.debug("Adding poststreet column to staging.address_table...")
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE staging.address_table ADD COLUMN IF NOT EXISTS poststreet VARCHAR"
            )
        )

    if log is not None:
        log.info(
            "Populating poststreet from staging.london_houses.street "
            "(restricted to UPRNs also present in staging.london_houses_condensed)..."
        )
    update_sql = text(r"""
    CREATE INDEX IF NOT EXISTS idx_lhc_uprn ON staging.london_houses_condensed(uprn);
    CREATE INDEX IF NOT EXISTS idx_lh_uprn ON staging.london_houses("UPRN");
    CREATE INDEX IF NOT EXISTS idx_ukhcs_uprn ON staging.ukhcs_table("UPRN");

    UPDATE staging.address_table a
    SET poststreet = cleaned.poststreet
    FROM staging.ukhcs_table ukhcs
    INNER JOIN staging.london_houses lh
        ON lh."UPRN" = ukhcs."UPRN"
    INNER JOIN staging.london_houses_condensed lhc
        ON lhc.uprn = lh."UPRN"
    CROSS JOIN LATERAL (
        SELECT NULLIF(TRIM(regexp_replace(lh.street, '^\s*\d+[A-Za-z]?\s*,?\s*', '')), '') AS poststreet
    ) cleaned
    WHERE a.uid = ukhcs.id
      AND a.poststreet IS DISTINCT FROM cleaned.poststreet;
    """)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        updated = connection.execute(update_sql).rowcount
    if log is not None:
        log.info(f"Updated {updated} row(s) of poststreet in staging.address_table!")


def create_london_houses_condensed_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``staging.london_houses_condensed``, the London sales with an EPC.

    One row per (transaction, certificate) pair, carrying both the sale date
    and the certificate's inspection date for the downstream as-of join. The
    WHERE clause applies the plausibility floors from
    `hpldn.backend.common.constants`; no inspection-date window is needed.
    """
    age_band_regex = r"""substring("CONSTRUCTION_AGE_BAND" from '\d{4}(?:-\d{4})?')"""

    london_houses_sql = text(f"""
    CREATE UNLOGGED TABLE staging.london_houses_condensed AS
    SELECT "UPRN" as uprn, price, postcode, "TOTAL_FLOOR_AREA" as total_floor_area, propertytype, duration, "CURRENT_ENERGY_RATING" as current_energy_rating, "POTENTIAL_ENERGY_RATING" AS potential_energy_rating,
    {age_band_regex} AS construction_age_band, DATE(dateoftransfer), "INSPECTION_DATE"::date AS inspection_date, "FLAT_STOREY_COUNT" as flat_storey_count, "LIGHTING_COST_CURRENT" as lighting_cost_current, "LIGHTING_COST_POTENTIAL" as lighting_cost_potential, "HEATING_COST_CURRENT" as heating_cost_current,
    "HEATING_COST_POTENTIAL" as heating_cost_potential, "NUMBER_HABITABLE_ROOMS" as number_habitable_rooms,  "HOT_WATER_COST_CURRENT" as hot_water_cost_current, "HOT_WATER_COST_POTENTIAL" as hot_water_cost_potential, "EXTENSION_COUNT" as extension_count, "TENURE" as tenure
    FROM staging.ukhpucl lh
    WHERE
    district IN ({LONDON_DISTRICTS_SQL}) AND
    lh.paon IS NOT NULL AND
    lh.street IS NOT NULL AND
    lh."POSTTOWN" IS NOT NULL AND
    lh.postcode IS NOT NULL AND
    lh."UPRN" IS NOT NULL AND
    lh."BUILT_FORM" IS NOT NULL AND
    lh.dateoftransfer IS NOT NULL AND
    lh."INSPECTION_DATE" IS NOT NULL AND
    lh."CONSTRUCTION_AGE_BAND" IS NOT NULL AND
    lh."EXTENSION_COUNT" IS NOT NULL AND
    lh."TENURE" IS NOT NULL AND
    lh.paon ~ '^[0-9]+$' AND
    EXTRACT(YEAR FROM lh."INSPECTION_DATE"::date) > 2007 AND
    lh.price >= {MIN_PLAUSIBLE_PRICE} AND
    lh."LIGHTING_COST_CURRENT" > {MIN_PLAUSIBLE_ANNUAL_COST} AND
    lh."LIGHTING_COST_POTENTIAL" > {MIN_PLAUSIBLE_ANNUAL_COST} AND
    lh."HEATING_COST_CURRENT" > {MIN_PLAUSIBLE_ANNUAL_COST} AND
    lh."HEATING_COST_POTENTIAL" > {MIN_PLAUSIBLE_ANNUAL_COST} AND
    lh."HOT_WATER_COST_CURRENT" > {MIN_PLAUSIBLE_ANNUAL_COST} AND
    lh."HOT_WATER_COST_POTENTIAL" > {MIN_PLAUSIBLE_ANNUAL_COST} AND
    lh."NUMBER_HABITABLE_ROOMS" > {MIN_HABITABLE_ROOMS} AND
    lh."CURRENT_ENERGY_RATING" IN ('A','B','C','D','E','F','G') AND
    lh."POTENTIAL_ENERGY_RATING" IN ('A','B','C','D','E','F','G') AND
    lh."TOTAL_FLOOR_AREA" >= {MIN_PLAUSIBLE_FLOOR_AREA_SQM}
    ORDER BY dateoftransfer;
    """)
    step = "create_london_houses_condensed_table"
    with _timed(log, step), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        with _timed(log, "CREATE staging.london_houses_condensed"):
            connection.execute(london_houses_sql)
        with _timed(log, "index staging.london_houses_condensed (uprn)"):
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_lhc_uprn "
                    "ON staging.london_houses_condensed (uprn)"
                )
            )
        _analyze(connection, ("staging.london_houses_condensed",), log)
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.london_houses_condensed")
        ).scalar()
    _emit(
        log,
        f"staging.london_houses_condensed holds {row_count} "
        f"(sale, certificate) pairings",
    )


def load_ppd_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Populate ``staging.ppd_table`` from ``staging.london_houses_condensed``.

    One address per UPRN. Orphaned UPRNs and ``duration = 'U'`` rows are
    logged to ``staging.ppd_table_errors`` instead of failing the load.
    """
    if log is not None:
        log.debug("Inserting data into staging.ppd_table...")

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        ppd_sql = text("""
        CREATE TABLE IF NOT EXISTS staging.ppd_table_errors (
            address_id BIGINT,
            price BIGINT,
            dateoftransfer DATE,
            propertytype VARCHAR,
            duration CHAR(1),
            error_reason TEXT,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        ALTER TABLE staging.ppd_table DROP CONSTRAINT IF EXISTS ppd_table_address_id_fkey;
        CREATE INDEX IF NOT EXISTS idx_lhc_uprn ON staging.london_houses_condensed(uprn);
        CREATE INDEX IF NOT EXISTS idx_ukhcs_uprn ON staging.ukhcs_table("UPRN");

        WITH deduplicated_addresses AS (
            -- One address_id per UPRN: the lowest, among duplicates.
            SELECT
                u."UPRN",
                MIN(a.id) AS address_id
            FROM staging.ukhcs_table u
            INNER JOIN staging.address_table a ON u.id = a.uid
            GROUP BY u."UPRN"
        )
        INSERT INTO staging.ppd_table (address_id, price, dateoftransfer, propertytype, duration)
        SELECT
            da.address_id,
            lhc.price,
            lhc.date AS dateoftransfer,
            lhc.propertytype,
            lhc.duration
        FROM staging.london_houses_condensed lhc
        INNER JOIN deduplicated_addresses da
            ON lhc.uprn = da."UPRN"
        WHERE lhc.duration IN ('F', 'L');

        INSERT INTO staging.ppd_table_errors (address_id, price, dateoftransfer, propertytype, duration, error_reason)
        SELECT
            u.id AS address_id,
            lhc.price,
            lhc.date AS dateoftransfer,
            lhc.propertytype,
            lhc.duration,
            'Missing address_id in staging.address_table'
        FROM staging.london_houses_condensed lhc
        INNER JOIN staging.ukhcs_table u ON lhc.uprn = u."UPRN"
        LEFT JOIN staging.address_table a ON u.id = a.uid
        WHERE a.id IS NULL;

        WITH deduplicated_addresses AS (
            SELECT
                u."UPRN",
                MIN(a.id) AS address_id
            FROM staging.ukhcs_table u
            INNER JOIN staging.address_table a ON u.id = a.uid
            GROUP BY u."UPRN"
        )
        INSERT INTO staging.ppd_table_errors (address_id, price, dateoftransfer, propertytype, duration, error_reason)
        SELECT
            da.address_id,
            lhc.price,
            lhc.date AS dateoftransfer,
            lhc.propertytype,
            lhc.duration,
            'duration outside ck_ppd_duration domain (F, L)'
        FROM staging.london_houses_condensed lhc
        INNER JOIN deduplicated_addresses da
            ON lhc.uprn = da."UPRN"
        WHERE lhc.duration NOT IN ('F', 'L');

        ALTER TABLE staging.ppd_table
        ADD CONSTRAINT ppd_table_address_id_fkey
        FOREIGN KEY (address_id) REFERENCES staging.address_table(id);

        ALTER TABLE staging.ppd_table SET LOGGED;
        """)
        connection.execute(ppd_sql)

        error_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.ppd_table_errors;")
        ).scalar()

        if log is not None:
            if error_count > 0:
                log.warning(
                    f"Data insertion complete, but found {error_count} orphaned "
                    "records. View 'staging.ppd_table_errors' for details."
                )
            else:
                log.debug("Data insertion complete with 0 orphaned records.")

    if log is not None:
        log.debug("Committed ppd_table data to database.")


def load_epc_table_condensed(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Rebuild ``staging.epc_table_condensed``, the EPC certificate history.

    One row per (UPRN, inspection date), newest lodgement winning, covering
    each property's whole history so the view can pick the certificate that
    predates each sale. Drops and recreates the table so a re-run reflects
    the current ``staging.london_houses``.
    """
    epc_sql = text("""
    CREATE UNLOGGED TABLE staging.epc_table_condensed AS
    WITH deduplicated_london_houses AS (
        -- One record per (UPRN, INSPECTION_DATE), re-lodgements collapsed
        -- by taking the newest LODGEMENT_DATETIME.
        SELECT DISTINCT ON ("UPRN", "INSPECTION_DATE")
            "UPRN",
            "CURRENT_ENERGY_EFFICIENCY",
            "POTENTIAL_ENERGY_EFFICIENCY",
            "PROPERTY_TYPE",
            "TRANSACTION_TYPE",
            "ENVIRONMENT_IMPACT_CURRENT",
            "ENVIRONMENT_IMPACT_POTENTIAL",
            "ENERGY_CONSUMPTION_CURRENT",
            "ENERGY_CONSUMPTION_POTENTIAL",
            "CO2_EMISSIONS_CURRENT",
            "CO2_EMISS_CURR_PER_FLOOR_AREA",
            "CO2_EMISSIONS_POTENTIAL",
            "ENERGY_TARIFF",
            "MAINS_GAS_FLAG",
            "FLOOR_LEVEL",
            "EXTENSION_COUNT",
            "MULTI_GLAZE_PROPORTION",
            "NUMBER_HABITABLE_ROOMS",
            "NUMBER_HEATED_ROOMS",
            "LOW_ENERGY_LIGHTING",
            "NUMBER_OPEN_FIREPLACES",
            "HOT_WATER_ENERGY_EFF",
            "HOT_WATER_ENV_EFF",
            "FLOOR_ENERGY_EFF",
            "FLOOR_ENV_EFF",
            "WINDOWS_ENERGY_EFF",
            "WINDOWS_ENV_EFF",
            "WALLS_ENERGY_EFF",
            "WALLS_ENV_EFF",
            "ROOF_ENERGY_EFF",
            "ROOF_ENV_EFF",
            "MAINHEAT_ENERGY_EFF",
            "MAINHEAT_ENV_EFF",
            "MAINHEATC_ENERGY_EFF",
            "MAINHEATC_ENV_EFF",
            "LIGHTING_ENERGY_EFF",
            "LIGHTING_ENV_EFF",
            "WIND_TURBINE_COUNT",
            "PHOTO_SUPPLY",
            "SOLAR_WATER_HEATING_FLAG",
            "CONSTRUCTION_AGE_BAND",
            "LODGEMENT_DATETIME",
            "FIXED_LIGHTING_OUTLETS_COUNT",
            "LOW_ENERGY_FIXED_LIGHT_COUNT",
            "GLAZED_TYPE",
            "INSPECTION_DATE",
            "BUILT_FORM"
        FROM staging.london_houses
        WHERE "INSPECTION_DATE" IS NOT NULL
        ORDER BY "UPRN", "INSPECTION_DATE", "LODGEMENT_DATETIME" DESC
    )
    -- Restrict to properties london_houses_condensed kept. EXISTS rather
    -- than a join to london_houses_condensed itself, which has one row per
    -- *sale* and would fan each certificate out across every transaction.
    SELECT
    "UPRN" as uprn,
    lh."CURRENT_ENERGY_EFFICIENCY" as current_energy_efficiency,
    lh."POTENTIAL_ENERGY_EFFICIENCY" as potential_energy_efficiency,
    lh."TRANSACTION_TYPE" as transaction_type,
    lh."ENVIRONMENT_IMPACT_CURRENT" as environment_impact_current,
    lh."ENVIRONMENT_IMPACT_POTENTIAL" as environment_impact_potential,
    lh."ENERGY_CONSUMPTION_CURRENT" as energy_consumption_current,
    lh."ENERGY_CONSUMPTION_POTENTIAL" as energy_consumption_potential,
    lh."CO2_EMISSIONS_CURRENT" as co2_emissions_current,
    lh."CO2_EMISS_CURR_PER_FLOOR_AREA" as co2_emiss_curr_per_floor_area,
    lh."CO2_EMISSIONS_POTENTIAL" as co2_emissions_potential,
    lh."ENERGY_TARIFF" as energy_tariff,
    lh."MAINS_GAS_FLAG" as mains_gas_flag,
    lh."MULTI_GLAZE_PROPORTION" as multi_glaze_proportion,
    lh."EXTENSION_COUNT" as extension_count,
    lh."NUMBER_HABITABLE_ROOMS" as number_habitable_rooms,
    lh."NUMBER_HEATED_ROOMS" as number_heated_rooms,
    lh."LOW_ENERGY_LIGHTING" as low_energy_lighting,
    lh."HOT_WATER_ENERGY_EFF" as hot_water_energy_eff,
    lh."HOT_WATER_ENV_EFF" as hot_water_env_eff,
    lh."WINDOWS_ENERGY_EFF" as windows_energy_eff,
    lh."WINDOWS_ENV_EFF" as windows_env_eff,
    lh."WALLS_ENERGY_EFF" as walls_energy_eff,
    lh."WALLS_ENV_EFF" as walls_env_eff,
    lh."ROOF_ENERGY_EFF" as roof_energy_eff,
    lh."ROOF_ENV_EFF" as roof_env_eff,
    lh."MAINHEAT_ENERGY_EFF" as mainheat_energy_eff,
    lh."MAINHEAT_ENV_EFF" as mainheat_env_eff,
    lh."MAINHEATC_ENERGY_EFF" as mainheatc_energy_eff,
    lh."MAINHEATC_ENV_EFF" as mainheatc_env_eff,
    lh."LIGHTING_ENERGY_EFF" as lighting_energy_eff,
    lh."LIGHTING_ENV_EFF" as lighting_env_eff,
    lh."LODGEMENT_DATETIME" as lodgement_datetime,
    lh."GLAZED_TYPE" as glazed_type,
    lh."INSPECTION_DATE"::date as inspection_date,
    lh."BUILT_FORM" as build_form
    FROM deduplicated_london_houses lh
    WHERE EXISTS (
        SELECT 1 FROM staging.london_houses_condensed lhc
        WHERE lhc.uprn = lh."UPRN"
    );
    """)
    with _timed(log, "load_epc_table_condensed"), engine.begin() as connection:
        connection.execute(
            text("DROP TABLE IF EXISTS staging.epc_table_condensed CASCADE")
        )
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_lhc_uprn "
                "ON staging.london_houses_condensed (uprn)"
            )
        )
        _analyze(connection, ("staging.london_houses_condensed",), log)
        with _timed(log, "CREATE staging.epc_table_condensed"):
            connection.execute(epc_sql)
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.epc_table_condensed")
        ).scalar()
    _emit(
        log,
        f"staging.epc_table_condensed holds {row_count} certificates "
        f"(one row per uprn + inspection_date)",
    )


_CRASH_RESET_LONDON_STAGING_TABLES = (
    "staging.london_houses",
    "staging.london_houses_condensed",
    "staging.epc_table_condensed",
)


def restore_crash_reset_london_staging(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> bool:
    """
    Rebuild the UNLOGGED London staging tables if a crash recovery emptied them.

    PostgreSQL truncates every UNLOGGED table after a crash recovery (for
    example when a backend is killed with SIGKILL). Later revisions still read
    ``london_houses``/``london_houses_condensed`` (0018's ukhpucl street pass)
    and ``epc_table_condensed`` (the 3-sigma EPC bounds of the history
    backfill). They are rebuilt as 0005 built them, then 0006's certificate
    pick is re-inserted: that insert only adds (uprn, inspection_date) pairs
    the load lacks, and ``staging._epc_london_pick`` is logged and holds
    every pick 0006-0018 made, so the result matches the pre-crash table.
    ``epc_fused_staging_table`` is not rebuilt; nothing after 0005 reads it.

    Returns
    -------
    bool
        True if the tables were empty and have been rebuilt.
    """
    from hpldn.backend.migrations.chain import run_concurrently
    from hpldn.backend.migrations.epc_fusion_sql import EPC_CONDENSED_INSERT_SQL

    with engine.connect() as connection:
        empty = [
            table
            for table in _CRASH_RESET_LONDON_STAGING_TABLES
            if connection.execute(text(f"SELECT to_regclass('{table}')")).scalar() is None
            or not connection.execute(
                text(f"SELECT EXISTS (SELECT 1 FROM {table})")
            ).scalar()
        ]
    if not empty:
        return False
    _emit(
        log,
        f"{', '.join(empty)} empty (UNLOGGED, reset by a crash recovery); "
        "rebuilding the London staging slice from staging.ukhpucl",
        logging.WARNING,
    )

    def london_houses():
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS staging.london_houses"))
        create_london_houses_table(engine, cfg, log)

    def london_houses_condensed():
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS staging.london_houses_condensed"))
        create_london_houses_condensed_table(engine, cfg, log)

    # Both read only ukhpucl.
    run_concurrently(
        {
            "staging.london_houses": london_houses,
            "staging.london_houses_condensed": london_houses_condensed,
        },
        log,
    )
    load_epc_table_condensed(engine, cfg, log)
    with engine.begin() as connection:
        inserted = connection.execute(text(EPC_CONDENSED_INSERT_SQL)).rowcount
        _analyze(connection, _CRASH_RESET_LONDON_STAGING_TABLES, log)
    _emit(
        log,
        f"re-inserted {inserted} picked London certificate(s) into "
        "staging.epc_table_condensed",
    )
    return True


def _generate_case_sql(column: str, mapping: Dict, table_alias: str = "") -> str:
    """Return a SQL CASE block mapping raw values to codes from `mapping`."""
    prefix = f"{table_alias}." if table_alias else ""
    lines = [f"CASE {prefix}{column}"]
    for k, v in mapping.items():
        safe_k = k.replace("'", "''")
        val = "NULL" if v is None else (f"'{v}'" if isinstance(v, str) else str(v))
        lines.append(f"    WHEN '{safe_k}' THEN {val}")
    lines.append(f"    ELSE NULL END AS {column}")
    return "\n            ".join(lines)


def floor_level_case_sql(column: str = 'BTRIM("FLOOR_LEVEL")') -> str:
    """
    Return a SQL CASE normalising an EPC FLOOR_LEVEL to its canonical label.

    Built from `FLOOR_LEVEL_MAPPING`, so Python and SQL agree; unrecognised
    values become NULL.
    """
    lines = [f"CASE {column}"]
    for raw, canonical in FLOOR_LEVEL_MAPPING.items():
        if canonical is None:
            continue
        lines.append(f"WHEN '{raw.replace(chr(39), chr(39) * 2)}' THEN '{canonical}'")
    lines.append("ELSE NULL END")
    return "\n            ".join(lines)


def known_floor_levels_sql() -> str:
    """Return the FLOOR_LEVEL spellings that name a real storey, as a SQL IN list."""
    return ", ".join(
        f"'{raw.replace(chr(39), chr(39) * 2)}'"
        for raw, canonical in FLOOR_LEVEL_MAPPING.items()
        if canonical is not None
    )


_EPC_EFF_RATING_COLUMNS = (
    "hot_water_energy_eff",
    "hot_water_env_eff",
    "windows_energy_eff",
    "windows_env_eff",
    "walls_energy_eff",
    "walls_env_eff",
    "roof_energy_eff",
    "roof_env_eff",
    "mainheat_energy_eff",
    "mainheat_env_eff",
    "mainheatc_energy_eff",
    "mainheatc_env_eff",
    "lighting_energy_eff",
    "lighting_env_eff",
)


def _eff_rating_case_sql(table_alias: str = "") -> str:
    """
    Return SQL CASE expressions mapping each EPC efficiency rating to its id.

    Unmapped values become -1 rather than NULL, so they stay distinguishable
    until the insert's ``NULLIF(col, -1)``. Shared so the mapping cannot drift
    between call sites.
    """
    prefix = f"{table_alias}." if table_alias else ""
    when_clauses = " ".join(
        f"WHEN '{raw}' THEN {code}" for raw, code in RATING_CATEGORY_MAPPING.items()
    )
    return ",\n            ".join(
        f"CASE {prefix}{col} {when_clauses} ELSE -1 END AS {col}"
        for col in _EPC_EFF_RATING_COLUMNS
    )


_EPC_BOUNDED_COLUMNS = (
    "current_energy_efficiency",
    "potential_energy_efficiency",
    "environment_impact_current",
    "environment_impact_potential",
    "energy_consumption_current",
    "energy_consumption_potential",
    "co2_emissions_current",
    "co2_emiss_curr_per_floor_area",
    "co2_emissions_potential",
    "multi_glaze_proportion",
    "extension_count",
    "number_habitable_rooms",
    "number_heated_rooms",
    "low_energy_lighting",
)

_SQL_NUMERIC_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "decimal",
        "real",
        "double precision",
    }
)
_SQL_INTEGER_TYPES = frozenset({"smallint", "integer", "bigint"})


def _get_epc_numeric_bounds(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
) -> Dict[str, Tuple[float, float]]:
    """
    Return the 3-sigma ``(lower, upper)`` bounds for `_EPC_BOUNDED_COLUMNS`.

    The same arithmetic as `hpldn.backend.common.utils.get_bounds_based_on_std`
    in one aggregate query; integer columns round and negative lower bounds
    clamp to zero.

    Raises
    ------
    RuntimeError
        If a bounded column is not numeric.
    """
    with engine.connect() as connection:
        column_types = dict(
            connection.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'staging' "
                    "AND table_name = 'epc_table_condensed'"
                )
            ).all()
        )
    missing = [c for c in _EPC_BOUNDED_COLUMNS if c not in column_types]
    if missing:
        raise RuntimeError(
            f"staging.epc_table_condensed is missing the columns "
            f"{missing}, which create_epc_fused_staging_table bounds; it was "
            f"probably built by an older load_epc_table_condensed"
        )
    non_numeric = [
        f"{c} ({column_types[c]})"
        for c in _EPC_BOUNDED_COLUMNS
        if column_types[c] not in _SQL_NUMERIC_TYPES
    ]
    if non_numeric:
        raise RuntimeError(
            f"staging.epc_table_condensed columns {non_numeric} are not "
            f"numeric, so no 3-sigma bound can be computed for them."
        )

    aggregates = ",\n        ".join(
        f"avg({c})::float8 AS {c}_avg, "
        f"stddev_pop({c})::float8 AS {c}_std, "
        f"count({c}) AS {c}_n"
        for c in _EPC_BOUNDED_COLUMNS
    )
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        with _timed(log, "3-sigma bounds over staging.epc_table_condensed"):
            stats = (
                connection.execute(
                    text(
                        f"SELECT\n        {aggregates}\n"
                        f"    FROM staging.epc_table_condensed"
                    )
                )
                .mappings()
                .one()
            )

    bounds: Dict[str, Tuple[float, float]] = {}
    for column in _EPC_BOUNDED_COLUMNS:
        count = stats[f"{column}_n"]
        mean = stats[f"{column}_avg"]
        std = stats[f"{column}_std"]
        if count == 0 or mean is None or std is None:
            raise RuntimeError(
                f"staging.epc_table_condensed.{column} has {count} non-null "
                f"values, so it has no mean/standard deviation to bound on. "
                f"Check load_epc_table_condensed actually populated it."
            )
        lower = mean - (3 * std)
        upper = mean + (3 * std)
        if column_types[column] in _SQL_INTEGER_TYPES:
            lower = int(round(lower, 0))
            upper = int(round(upper, 0))
        if lower < 0.0:
            lower = 0.0
        bounds[column] = (lower, upper)
        _emit(
            log,
            f"  bound {column}: [{lower}, {upper}] from {count} values "
            f"(mean {mean:.4f}, sigma {std:.4f})",
            logging.DEBUG,
        )
    return bounds


def _domain_violation_predicate(column: str, domain) -> str:
    """
    Return a SQL predicate true for a row whose `column` lies outside `domain`.

    NULL is a violation unless None is in `domain`.
    """
    literals = []
    for value in domain:
        if value is None:
            continue
        if isinstance(value, str):
            escaped = value.replace("'", "''")
            literals.append(f"'{escaped}'")
        else:
            literals.append(str(value))
    if not literals:
        return f"{column} IS NOT NULL"
    in_list = ", ".join(sorted(literals))
    if None in domain:
        return f"({column} IS NOT NULL AND {column} NOT IN ({in_list}))"
    return f"({column} IS NULL OR {column} NOT IN ({in_list}))"


def _check_fused_category_domains(
    engine: Engine, log: Optional[logging.Logger] = None
) -> None:
    """
    Check every categorical column of the fused table is inside its value set.

    One aggregate query; raised rather than asserted so ``python -O`` cannot
    strip it.

    Raises
    ------
    ValueError
        Listing out-of-domain counts per column.
    """
    category_domains = [
        ("propertytype", ("D", "F", "S", "T")),
        ("duration", ("F", "L", "U")),
        ("potential_energy_rating", ("A", "B", "C", "D", "E", "F", "G")),
        ("current_energy_rating", ("A", "B", "C", "D", "E", "F", "G")),
        ("tenure", set(TENURE_MAPPING.values())),
        ("build_form", set(BUILD_FORM_MAPPING.values())),
        ("transaction_type", set(TRANSACTION_TYPE_MAPPING.values())),
        ("energy_tariff", set(ENERGY_TARIFF_MAPPING.values())),
        ("glazed_type", set(GLAZED_TYPE_MAPPING.values())),
    ]
    projections = []
    for column, domain in category_domains:
        predicate = _domain_violation_predicate(column, domain)
        projections.append(f"count(*) FILTER (WHERE {predicate}) AS {column}_bad")
        projections.append(
            f"min({column}::text) FILTER (WHERE {predicate}) AS {column}_example"
        )
    check_sql = text(
        "SELECT\n    "
        + ",\n    ".join(projections)
        + "\nFROM staging.epc_fused_staging_table"
    )
    with engine.connect() as connection:
        with _timed(log, "category-domain check on staging.epc_fused_staging_table"):
            result = connection.execute(check_sql).mappings().one()

    failures = []
    for column, domain in category_domains:
        bad = result[f"{column}_bad"]
        if bad:
            failures.append(
                f"staging.epc_fused_staging_table.{column} holds "
                f"{result[f'{column}_example']!r} on {bad} row(s), which is "
                f"not in its documented value set "
                f"{sorted((v for v in domain), key=repr)}"
            )
    if failures:
        raise ValueError("; ".join(failures))
    _emit(log, "Category-domain check passed on staging.epc_fused_staging_table")


def create_epc_fused_staging_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    validate_categories: bool = True,
) -> None:
    """
    Build ``staging.epc_fused_staging_table`` from the condensed EPC history and PPD.

    One row per (uprn, inspection_date) with numeric columns bounded at 3
    sigma (`_get_epc_numeric_bounds`). Inputs are ANALYZEd first, the table is
    dropped and recreated, and categories are checked in one scan.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Target database.
    cfg : dict, optional
        ``staging.yaml``, for tuning overrides.
    log : logging.Logger, optional
        Logger, or None to print progress.
    validate_categories : bool, optional
        Run `_check_fused_category_domains` on the result.
    """
    with _timed(log, "create_epc_fused_staging_table"):
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            with _timed(
                log, "index staging.epc_table_condensed (uprn, inspection_date)"
            ):
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_epc_condensed_uprn_inspection "
                        "ON staging.epc_table_condensed (uprn, inspection_date)"
                    )
                )
            with _timed(
                log, "index staging.london_houses_condensed (uprn, inspection_date)"
            ):
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_lhc_uprn_inspection "
                        "ON staging.london_houses_condensed (uprn, inspection_date)"
                    )
                )
            _analyze(
                connection,
                ("staging.epc_table_condensed", "staging.london_houses_condensed"),
                log,
            )

        bounds = _get_epc_numeric_bounds(engine, cfg, log)

        eff_cases_sql = _eff_rating_case_sql()

        tenure_cases_sql = _generate_case_sql("tenure", TENURE_MAPPING, "lhc")
        build_form_cases_sql = _generate_case_sql("build_form", BUILD_FORM_MAPPING)
        transaction_type_cases_sql = _generate_case_sql(
            "transaction_type", TRANSACTION_TYPE_MAPPING
        )
        energy_tariff_cases_sql = _generate_case_sql(
            "energy_tariff", ENERGY_TARIFF_MAPPING
        )
        glazed_type_cases_sql = _generate_case_sql("glazed_type", GLAZED_TYPE_MAPPING)

        bounded_predicates = "\n            AND ".join(
            f"{column} BETWEEN {low} AND {high}"
            for column, (low, high) in bounds.items()
        )

        fused_epc_table_sql = text(f"""
        CREATE UNLOGGED TABLE staging.epc_fused_staging_table AS
        -- One row per (uprn, inspection_date) — a property's certificate
        -- history. london_houses_condensed carries a (sale, certificate)
        -- pairing per row, so it is collapsed onto the certificate key
        -- first, to avoid fanning each certificate out across every
        -- transaction of the property.
        WITH deduplicated_lhc AS (
            SELECT DISTINCT ON (uprn, inspection_date)
                uprn,
                inspection_date,
                price,
                postcode,
                total_floor_area,
                propertytype,
                duration,
                potential_energy_rating,
                current_energy_rating,
                construction_age_band,
                flat_storey_count,
                lighting_cost_current,
                lighting_cost_potential,
                heating_cost_current,
                heating_cost_potential,
                hot_water_cost_current,
                hot_water_cost_potential,
                number_habitable_rooms,
                extension_count,
                tenure
            FROM staging.london_houses_condensed
            WHERE inspection_date IS NOT NULL
            -- The sale closest to the inspection describes the property as the
            -- certificate found it; ties fall back to the earliest.
            ORDER BY uprn, inspection_date, abs(date - inspection_date), date
        ),
        deduplicated_epc AS (
            SELECT DISTINCT ON (uprn, inspection_date)
                uprn,
                inspection_date,
                {build_form_cases_sql},
                current_energy_efficiency,
                potential_energy_efficiency,
                {transaction_type_cases_sql},
                environment_impact_current,
                environment_impact_potential,
                energy_consumption_current,
                energy_consumption_potential,
                co2_emissions_current,
                co2_emiss_curr_per_floor_area,
                co2_emissions_potential,
                {energy_tariff_cases_sql},
                mains_gas_flag,
                multi_glaze_proportion,
                {glazed_type_cases_sql},
                number_heated_rooms,
                low_energy_lighting,
                lodgement_datetime,
                {eff_cases_sql}
            FROM staging.epc_table_condensed
            WHERE
                inspection_date IS NOT NULL
                AND {bounded_predicates}
            ORDER BY uprn, inspection_date, lodgement_datetime DESC
        ),
        deduplicated_ppd AS (
            -- uprn -> address_id, for the UPRNs that actually have a
            -- transaction. load_ppd_table already collapses this to one
            -- address_id per UPRN, so MIN() over a hash aggregate gets the
            -- same value without a sort. Driven from ppd_table (hundreds of
            -- thousands of rows) rather than ukhcs_table (tens of millions),
            -- so the big table is only ever probed by its primary key.
            SELECT
                ukhcs."UPRN" AS uprn,
                MIN(p.address_id) AS address_id
            FROM staging.ppd_table p
            INNER JOIN staging.address_table a ON a.id = p.address_id
            INNER JOIN staging.ukhcs_table ukhcs ON ukhcs.id = a.uid
            GROUP BY ukhcs."UPRN"
        )
        SELECT
            ppd.address_id,
            lhc.uprn,
            lhc.price,
            lhc.postcode,
            lhc.total_floor_area,
            lhc.propertytype,
            lhc.duration,
            lhc.potential_energy_rating,
            lhc.current_energy_rating,
            lhc.construction_age_band,
            lhc.flat_storey_count,
            lhc.lighting_cost_current,
            lhc.lighting_cost_potential,
            lhc.heating_cost_current,
            lhc.heating_cost_potential,
            lhc.hot_water_cost_current,
            lhc.hot_water_cost_potential,
            {tenure_cases_sql},
            lhc.number_habitable_rooms AS number_habitable_rooms,
            lhc.extension_count AS extension_count,
            -- The certificate's own inspection date, not the sale date --
            -- an as-of join is only possible against a column that actually
            -- carries the certificate's date.
            epc.inspection_date,
            epc.build_form,
            epc.current_energy_efficiency,
            epc.potential_energy_efficiency,
            epc.transaction_type,
            epc.environment_impact_current,
            epc.environment_impact_potential,
            epc.energy_consumption_current,
            epc.energy_consumption_potential,
            epc.co2_emissions_current,
            epc.co2_emiss_curr_per_floor_area,
            epc.co2_emissions_potential,
            epc.energy_tariff,
            epc.mains_gas_flag,
            epc.multi_glaze_proportion,
            epc.glazed_type,
            epc.number_heated_rooms,
            epc.low_energy_lighting,
            epc.hot_water_energy_eff,
            epc.hot_water_env_eff,
            epc.windows_energy_eff,
            epc.windows_env_eff,
            epc.walls_energy_eff,
            epc.walls_env_eff,
            epc.roof_energy_eff,
            epc.roof_env_eff,
            epc.mainheat_energy_eff,
            epc.mainheat_env_eff,
            epc.mainheatc_energy_eff,
            epc.mainheatc_env_eff,
            epc.lighting_energy_eff,
            epc.lighting_env_eff,
            epc.lodgement_datetime
        FROM deduplicated_lhc lhc
        JOIN deduplicated_epc epc
            ON lhc.uprn = epc.uprn AND lhc.inspection_date = epc.inspection_date
        LEFT JOIN deduplicated_ppd ppd
            ON lhc.uprn = ppd.uprn;
        """)
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            connection.execute(
                text("DROP TABLE IF EXISTS staging.epc_fused_staging_table CASCADE")
            )
            with _timed(log, "CREATE staging.epc_fused_staging_table"):
                connection.execute(fused_epc_table_sql)
            with _timed(log, "index staging.epc_fused_staging_table"):
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_fused_uprn "
                        "ON staging.epc_fused_staging_table (uprn)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_fused_address "
                        "ON staging.epc_fused_staging_table (address_id)"
                    )
                )
            _analyze(connection, ("staging.epc_fused_staging_table",), log)
            row_count = connection.execute(
                text("SELECT COUNT(*) FROM staging.epc_fused_staging_table")
            ).scalar()
        _emit(
            log,
            f"staging.epc_fused_staging_table holds {row_count} certificate rows",
        )

        if validate_categories:
            _check_fused_category_domains(engine, log)


def get_category_labels(
    mapping: Dict[str, Optional[int]],
    table,
    column_name: str,
    log: Optional[logging.Logger] = None,
) -> Dict[int, str]:
    """
    Return ``{id: label}`` rows for a category table from a raw-value mapping.

    Where several raw spellings share an id, the shortest becomes the label;
    labels longer than the column width are truncated with a warning rather
    than failing the insert.
    """
    labels: Dict[int, str] = {}
    for raw_value, category_id in mapping.items():
        if category_id is None:
            continue
        current = labels.get(category_id)
        if current is None or len(raw_value) < len(current):
            labels[category_id] = raw_value

    limit = getattr(table.columns[column_name].type, "length", None)
    if limit is None:
        return labels
    for category_id, label in labels.items():
        if len(label) > limit:
            if log is not None:
                log.warning(
                    f"{table.name}.{column_name} value for id={category_id} is "
                    f"{len(label)} characters, exceeding the column limit of "
                    f"{limit}; truncating '{label}' to '{label[:limit]}'"
                )
            labels[category_id] = label[:limit]
    return labels


def insert_epc_data_into_db(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """Rebuild ``staging.epc_table`` and its category tables from the fused staging table."""
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging.epc_table"))

    AppBase.metadata.create_all(
        engine,
        tables=[
            RatingCategory.__table__,
            BuildFormCategory.__table__,
            TransactionTypeCategory.__table__,
            EnergyTariffCategory.__table__,
            GlazedTypeCategory.__table__,
            EPC.__table__,
        ],
        checkfirst=True,
    )
    if log is not None:
        log.debug("Created/verified epc_table and its reverse mapping tables!")

    category_tables = [
        (RatingCategory.__table__, "rating_category", RATING_CATEGORY_MAPPING),
        (BuildFormCategory.__table__, "build_form", BUILD_FORM_MAPPING),
        (
            TransactionTypeCategory.__table__,
            "transaction_type",
            TRANSACTION_TYPE_MAPPING,
        ),
        (EnergyTariffCategory.__table__, "energy_tariff", ENERGY_TARIFF_MAPPING),
        (GlazedTypeCategory.__table__, "glazed_type", GLAZED_TYPE_MAPPING),
    ]
    with engine.begin() as connection:
        for table, column_name, mapping in category_tables:
            values = get_category_labels(mapping, table, column_name, log)
            insert_sql = text(f"""
            INSERT INTO staging.{table.name} (id, {column_name})
            VALUES (:id, :val)
            ON CONFLICT (id) DO UPDATE SET {column_name} = EXCLUDED.{column_name}
            """)
            connection.execute(
                insert_sql, [{"id": k, "val": v} for k, v in values.items()]
            )
    if log is not None:
        log.debug("Populated reverse mapping/category tables!")

    mains_gas_flag_sql = "BTRIM(UPPER(mains_gas_flag)) IN ('Y', 'N')"
    with engine.begin() as connection:
        unusable_flags = connection.execute(
            text(f"""
            SELECT quote_literal(mains_gas_flag) AS value, COUNT(*) AS n
            FROM staging.epc_fused_staging_table
            WHERE address_id IS NOT NULL
              AND mains_gas_flag IS NOT NULL
              AND NOT ({mains_gas_flag_sql})
            GROUP BY 1
            ORDER BY n DESC
            """)
        ).all()
    if unusable_flags and log is not None:
        total = sum(row.n for row in unusable_flags)
        breakdown = ", ".join(f"{row.value} x{row.n}" for row in unusable_flags[:10])
        log.warning(
            f"{total} epc_fused_staging_table rows have a mains_gas_flag that "
            f"is neither Y nor N ({breakdown}); inserting NULL for those rows"
        )

    insert_epc_sql = text(f"""
    INSERT INTO staging.epc_table (
        address_id, current_energy_rating, potential_energy_rating,
        current_energy_efficiency, potential_energy_efficiency, build_form,
        inspection_date, transaction_type, environment_impact_current,
        environment_impact_potential, energy_consumption_current,
        energy_consumption_potential, co2_emissions_current,
        co2_emiss_curr_per_floor_area, co2_emissions_potential,
        lighting_cost_current, lighting_cost_potential, heating_cost_current,
        heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
        total_floor_area, energy_tariff, mains_gas_flag, flat_storey_count,
        multi_glaze_proportion, glazed_type, extension_count,
        number_habitable_rooms, number_heated_rooms, low_energy_lighting,
        hot_water_energy_eff, hot_water_env_eff, windows_energy_eff,
        windows_env_eff, walls_energy_eff, walls_env_eff, roof_energy_eff,
        roof_env_eff, mainheat_energy_eff, mainheat_env_eff,
        mainheatc_energy_eff, mainheatc_env_eff, lighting_energy_eff,
        lighting_env_eff, construction_age_band, lodgement_datetime, tenure
    )
    SELECT
        address_id, current_energy_rating, potential_energy_rating,
        current_energy_efficiency, potential_energy_efficiency, build_form,
        inspection_date, transaction_type, environment_impact_current,
        environment_impact_potential, energy_consumption_current,
        energy_consumption_potential, co2_emissions_current,
        co2_emiss_curr_per_floor_area, co2_emissions_potential,
        lighting_cost_current, lighting_cost_potential, heating_cost_current,
        heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
        total_floor_area, energy_tariff,
        CASE WHEN {mains_gas_flag_sql} THEN BTRIM(UPPER(mains_gas_flag)) END,
        flat_storey_count,
        multi_glaze_proportion, glazed_type, extension_count,
        number_habitable_rooms, number_heated_rooms, low_energy_lighting,
        NULLIF(hot_water_energy_eff, -1), NULLIF(hot_water_env_eff, -1),
        NULLIF(windows_energy_eff, -1), NULLIF(windows_env_eff, -1),
        NULLIF(walls_energy_eff, -1), NULLIF(walls_env_eff, -1),
        NULLIF(roof_energy_eff, -1), NULLIF(roof_env_eff, -1),
        NULLIF(mainheat_energy_eff, -1), NULLIF(mainheat_env_eff, -1),
        NULLIF(mainheatc_energy_eff, -1), NULLIF(mainheatc_env_eff, -1),
        NULLIF(lighting_energy_eff, -1), NULLIF(lighting_env_eff, -1),
        construction_age_band, lodgement_datetime, tenure
    FROM staging.epc_fused_staging_table
    WHERE address_id IS NOT NULL;
    """)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        with _timed(log, "INSERT staging.epc_fused_staging_table -> staging.epc_table"):
            connection.execute(insert_epc_sql)
        _analyze(connection, ("staging.epc_table",), log)

    with engine.begin() as connection:
        expected_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.epc_fused_staging_table WHERE address_id IS NOT NULL"
            )
        ).scalar()
        actual_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.epc_table")
        ).scalar()

    if actual_count != expected_count:
        raise RuntimeError(
            f"epc_table row count ({actual_count}) does not match the "
            f"number of epc_fused_staging_table rows with a non-null "
            f"address_id ({expected_count})"
        )
    if log is not None:
        log.debug(f"staging.epc_table now contains {actual_count} rows")


def insert_inspire_polygon_data(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    full_rebuild: bool = True,
) -> None:
    """
    Load INSPIRE parcel polygons and compute per-house lot areas.

    With `full_rebuild`, loads every borough GML into
    ``staging.inspire_polygons`` with ogr2ogr, indexes it, and builds
    ``staging.london_parcels`` from the parcel containing each house.
    Otherwise both tables must exist and only UPRNs without a parcel row are
    added. INSPIRE covers freehold only, so unmatched UPRNs are counted and
    logged.
    """
    if full_rebuild:
        from hpldn.backend.scripts.migration_helpers import require_data_path

        inspire_dir = require_data_path(
            cfg,
            "inspire_polygons_dir",
            description="INSPIRE cadastral parcels (insert_inspire_polygon_data)",
            must_be_dir=True,
        )
        borough_dirs = sorted(d for d in inspire_dir.iterdir() if d.is_dir())
        if len(borough_dirs) <= 0:
            raise RuntimeError(f"No borough directories found under {inspire_dir}")

        with engine.begin() as connection:
            connection.execute(text("SELECT deploy_spatial_extension();"))
            connection.execute(text("DROP TABLE IF EXISTS staging.london_parcels;"))
            connection.execute(text("DROP TABLE IF EXISTS staging.inspire_polygons;"))

        db_name = engine.url.database
        db_user = engine.url.username
        pg_conn_str = f"PG:dbname={db_name} user={db_user}"
        if engine.url.host:
            pg_conn_str += f" host={engine.url.host}"
        if engine.url.port:
            pg_conn_str += f" port={engine.url.port}"
        if engine.url.password:
            pg_conn_str += f" password={engine.url.password}"
        for borough_dir in borough_dirs:
            gml_fp = borough_dir.joinpath("Land_Registry_Cadastral_Parcels.gml")
            if not gml_fp.is_file():
                raise FileNotFoundError(f"GML file not found: {gml_fp}")
            if log is not None:
                log.debug(f"Loading INSPIRE polygons for {borough_dir.name}...")
            result = subprocess.run(
                [
                    "ogr2ogr",
                    "-f",
                    "PostgreSQL",
                    pg_conn_str,
                    str(gml_fp),
                    "-nln",
                    "staging.inspire_polygons",
                    "-lco",
                    "GEOMETRY_NAME=wkb_geometry",
                    "-lco",
                    "SPATIAL_INDEX=NONE",
                    "-t_srs",
                    "EPSG:27700",
                    "-nlt",
                    "PROMOTE_TO_MULTI",
                    "-append",
                    "--config",
                    "PG_USE_COPY",
                    "YES",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                if log is not None:
                    log.error(f"ogr2ogr failed for {borough_dir.name}: {result.stderr}")
                raise RuntimeError(
                    f"ogr2ogr failed for {borough_dir.name} "
                    f"(exit {result.returncode}): {result.stderr}"
                )

        with engine.begin() as connection:
            loaded_count = connection.execute(
                text("SELECT COUNT(*) FROM staging.inspire_polygons")
            ).scalar_one()
        if log is not None:
            log.debug(
                f"Loaded {loaded_count} INSPIRE polygons from {len(borough_dirs)} boroughs"
            )
        if loaded_count <= 0:
            raise RuntimeError("No INSPIRE polygons were loaded")

        dedupe_sql = text("""
        DELETE FROM staging.inspire_polygons a
        USING staging.inspire_polygons b
        WHERE a.inspireid = b.inspireid AND a.ogc_fid > b.ogc_fid;
        """)
        with engine.begin() as connection:
            deleted = connection.execute(dedupe_sql).rowcount
        if log is not None:
            log.debug(f"Removed {deleted} duplicate cross-borough INSPIRE polygons")

        if log is not None:
            log.debug("Creating GIST index on staging.inspire_polygons.wkb_geometry...")
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            connection.execute(
                text(
                    "CREATE INDEX idx_inspire_polygons_geom "
                    "ON staging.inspire_polygons USING gist(wkb_geometry);"
                )
            )
            connection.execute(text("ANALYZE staging.inspire_polygons;"))
        if log is not None:
            log.debug("Created GIST index on staging.inspire_polygons!")
    else:
        with engine.connect() as connection:
            missing_tables = [
                t
                for t in ("staging.inspire_polygons", "staging.london_parcels")
                if connection.execute(text(f"SELECT to_regclass('{t}')")).scalar() is None
            ]
        if missing_tables:
            raise RuntimeError(
                f"full_rebuild=False requires {', '.join(missing_tables)} to "
                "already exist; run with full_rebuild=True first"
            )
        if log is not None:
            log.debug(
                "Incremental insert_inspire_polygon_data: staging.inspire_polygons "
                "and staging.london_parcels already exist, skipping the ogr2ogr reload"
            )

    uprn_unique_sql = text("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_ukhcs_table_uprn'
              AND conrelid = 'staging.ukhcs_table'::regclass
        ) THEN
            ALTER TABLE staging.ukhcs_table
                ADD CONSTRAINT uq_ukhcs_table_uprn UNIQUE ("UPRN");
        END IF;
    END $$;
    """)
    if log is not None:
        log.debug("Ensuring unique constraint on staging.ukhcs_table.UPRN...")
    with engine.begin() as connection:
        connection.execute(uprn_unique_sql)

    if full_rebuild:
        create_london_parcels_sql = text("""
        CREATE TABLE staging.london_parcels (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            uprn BIGINT NOT NULL REFERENCES staging.ukhcs_table ("UPRN"),
            lot_area_sqm DOUBLE PRECISION NOT NULL
        );
        """)
        with engine.begin() as connection:
            connection.execute(create_london_parcels_sql)
        if log is not None:
            log.debug("Created staging.london_parcels table!")

    if full_rebuild:
        insert_london_parcels_sql = text("""
        INSERT INTO staging.london_parcels
        (uprn, lot_area_sqm)
        SELECT
            g."UPRN",
            SUM(ST_Area(i.wkb_geometry)) AS lot_area_sqm
        FROM (
            SELECT DISTINCT u."UPRN", u."X_COORDINATE", u."Y_COORDINATE"
            FROM staging.ukhcs_table u
            INNER JOIN staging.london_houses_condensed lhc ON lhc.uprn = u."UPRN"
        ) g
        INNER JOIN staging.inspire_polygons i
            ON ST_Contains(
                i.wkb_geometry,
                ST_SetSRID(ST_MakePoint(g."X_COORDINATE", g."Y_COORDINATE"), 27700)
            )
        GROUP BY g."UPRN";
        """)
    else:
        insert_london_parcels_sql = text("""
        INSERT INTO staging.london_parcels
        (uprn, lot_area_sqm)
        SELECT
            g."UPRN",
            SUM(ST_Area(i.wkb_geometry)) AS lot_area_sqm
        FROM (
            SELECT DISTINCT u."UPRN", u."X_COORDINATE", u."Y_COORDINATE"
            FROM staging.ukhcs_table u
            INNER JOIN staging.london_houses_condensed lhc ON lhc.uprn = u."UPRN"
            LEFT JOIN staging.london_parcels lp ON lp.uprn = u."UPRN"
            WHERE lp.uprn IS NULL
        ) g
        INNER JOIN staging.inspire_polygons i
            ON ST_Contains(
                i.wkb_geometry,
                ST_SetSRID(ST_MakePoint(g."X_COORDINATE", g."Y_COORDINATE"), 27700)
            )
        GROUP BY g."UPRN";
        """)
    if log is not None:
        log.debug("Inserting lot areas into staging.london_parcels...")
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        inserted = connection.execute(insert_london_parcels_sql).rowcount
    if log is not None:
        log.info(f"Inserted {inserted} new staging.london_parcels row(s)")

    with engine.begin() as connection:
        parcel_count, min_lot_area = connection.execute(
            text("SELECT COUNT(*), MIN(lot_area_sqm) FROM staging.london_parcels")
        ).one()
        lhc_uprn_count = connection.execute(
            text("SELECT COUNT(DISTINCT uprn) FROM staging.london_houses_condensed")
        ).scalar_one()
        missing_uprns = (
            connection.execute(
                text("""
                SELECT DISTINCT lhc.uprn
                FROM staging.london_houses_condensed lhc
                LEFT JOIN staging.london_parcels lp ON lp.uprn = lhc.uprn
                WHERE lp.uprn IS NULL
                ORDER BY lhc.uprn;
                """)
            )
            .scalars()
            .all()
        )

    if log is not None:
        log.debug(f"staging.london_parcels now contains {parcel_count} rows")
        log.info(
            f"Could not compute lot area for {len(missing_uprns)} of "
            f"{lhc_uprn_count} houses in staging.london_houses_condensed "
            "(no matching INSPIRE polygon, expected for leasehold/private "
            "properties, which INSPIRE does not cover)"
        )
        log.debug("UPRNs without a matching INSPIRE polygon:")
        for i in range(0, len(missing_uprns), 100):
            log.debug(", ".join(str(uprn) for uprn in missing_uprns[i : i + 100]))

    if parcel_count <= 0:
        raise RuntimeError("No parcel areas were computed")
    if parcel_count + len(missing_uprns) != lhc_uprn_count:
        raise RuntimeError(
            f"parcel_count ({parcel_count}) + missing_uprns "
            f"({len(missing_uprns)}) != lhc_uprn_count ({lhc_uprn_count})"
        )
    if min_lot_area <= 0:
        raise RuntimeError(f"min_lot_area was {min_lot_area}, expected > 0")


def _add_constraint_if_absent(connection, table: str, name: str, ddl: str) -> None:
    """Add a constraint unless ``pg_constraint`` already has it (PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS)."""
    exists = connection.execute(
        text(
            "SELECT 1 FROM pg_constraint WHERE conname = :name "
            "AND conrelid = to_regclass(:table)"
        ),
        {"name": name, "table": table},
    ).first()
    if exists is None:
        connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} {ddl}"))


def apply_data_quality_fixes(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Deduplicate the serving tables and add the constraints that prevent duplicates.

    Applied to staging in one idempotent transaction, and carried to
    production by the next promote:

    1. ``epc_table``: one row per (address_id, inspection_date), newest
       lodgement kept, ``UNIQUE NULLS NOT DISTINCT``.
    2. ``ppd_table``: lowest id kept per (address_id, dateoftransfer, price),
       with that UNIQUE and ``CHECK (price > 0)``.
    3. ``ppd_table.propertytype`` aligned to CHAR(1).
    4. ``address_table.uid`` made UNIQUE.
    5. Category labels made UNIQUE.
    """
    dedupe_epc_sql = text("""
    DELETE FROM staging.epc_table e
    USING (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY address_id, inspection_date
            ORDER BY lodgement_datetime DESC NULLS LAST,
                     id DESC
        ) AS rn
        FROM staging.epc_table
    ) ranked
    WHERE e.id = ranked.id AND ranked.rn > 1;
    """)
    dedupe_ppd_sql = text("""
    DELETE FROM staging.ppd_table p
    USING (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY address_id, price, dateoftransfer
            ORDER BY id
        ) AS rn
        FROM staging.ppd_table
    ) ranked
    WHERE p.id = ranked.id AND ranked.rn > 1;
    """)

    with _timed(log, "apply_data_quality_fixes"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        _analyze(connection, ("staging.epc_table", "staging.ppd_table"), log)

        with _timed(log, "deduplicate staging.epc_table"):
            epc_deleted = connection.execute(dedupe_epc_sql).rowcount
        with _timed(log, "deduplicate staging.ppd_table"):
            ppd_deleted = connection.execute(dedupe_ppd_sql).rowcount
        if log is not None:
            log.info(
                "Removed %d duplicate epc_table rows and %d duplicate ppd_table rows",
                epc_deleted,
                ppd_deleted,
            )

        connection.execute(
            text("ALTER TABLE staging.ppd_table ALTER COLUMN propertytype TYPE CHAR(1)")
        )

        connection.execute(
            text(
                "ALTER TABLE staging.epc_table "
                "DROP CONSTRAINT IF EXISTS uq_epc_table_address_id"
            )
        )
        with _timed(log, "uq_epc_table_address_inspection"):
            _add_constraint_if_absent(
                connection,
                "staging.epc_table",
                "uq_epc_table_address_inspection",
                "UNIQUE NULLS NOT DISTINCT (address_id, inspection_date)",
            )
        connection.execute(
            text(
                "ALTER TABLE staging.ppd_table "
                "DROP CONSTRAINT IF EXISTS uq_ppd_table_transaction"
            )
        )
        with _timed(log, "uq_ppd_table_transaction"):
            _add_constraint_if_absent(
                connection,
                "staging.ppd_table",
                "uq_ppd_table_transaction",
                "UNIQUE (address_id, dateoftransfer, price)",
            )
        _add_constraint_if_absent(
            connection,
            "staging.ppd_table",
            "ck_ppd_price_positive",
            "CHECK (price > 0)",
        )
        _add_constraint_if_absent(
            connection,
            "staging.address_table",
            "uq_address_table_uid",
            "UNIQUE (uid)",
        )
        for table, column in [
            ("rating_category_table", "rating_category"),
            ("build_form_category_table", "build_form"),
            ("transaction_type_category_table", "transaction_type"),
            ("energy_tariff_category_table", "energy_tariff"),
            ("glazed_type_category_table", "glazed_type"),
        ]:
            _add_constraint_if_absent(
                connection,
                f"staging.{table}",
                f"uq_{table}_{column}",
                f"UNIQUE ({column})",
            )
    if log is not None:
        log.info("Applied data quality constraints to the serving tables")


_SERVING_TABLES = [
    "rating_category_table",
    "build_form_category_table",
    "transaction_type_category_table",
    "energy_tariff_category_table",
    "glazed_type_category_table",
    "address_table",
    "ppd_table",
    "epc_table",
]


def _ensure_production_schema(engine: Engine) -> None:
    """
    Create the production schema despite a missing CREATE privilege.

    Tries, in order: an existing schema, ``CREATE SCHEMA``, then the
    ``public.deploy_production_schema()`` SECURITY DEFINER helper.

    Raises
    ------
    RuntimeError
        Naming the bootstrap command the superuser must run.
    """
    with engine.begin() as connection:
        exists = connection.execute(
            text(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'production'"
            )
        ).first()
        if exists is not None:
            return
        can_create = connection.execute(
            text(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
            )
        ).scalar()
        if can_create:
            connection.execute(text("CREATE SCHEMA production"))
            return
        helper = connection.execute(
            text(
                "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'deploy_production_schema'"
            )
        ).first()
        if helper is not None:
            connection.execute(text("SELECT public.deploy_production_schema()"))
            return
    raise RuntimeError(
        "The production schema does not exist and the current role can neither "
        "create it nor call public.deploy_production_schema(). Have the "
        "PostgreSQL superuser run: "
        "sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres "
        "-f src/hpldn/backend/db/sql/db_init.sql"
    )


def promote_staging_to_production(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    rebuild_view: bool = False,
) -> None:
    """
    Materialise the cleaned serving tables into the production schema.

    Rebuilds each table with ``LIKE staging.<t> INCLUDING ALL`` in one
    transaction, so readers see old or new data, never neither. Also builds
    ``production.uprn_lookup`` and the postcode and posttown indexes the API
    filters on.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Target database.
    cfg : dict, optional
        ``staging.yaml``, for tuning overrides.
    log : logging.Logger, optional
        Logger, or None to print progress.
    rebuild_view : bool, optional
        Rebuild ``production.london_houses`` if it existed. False by default,
        because the chain builds the view once, in revision 0016.
    """
    _ensure_production_schema(engine)

    with engine.connect() as connection:
        view_exists = connection.execute(
            text("SELECT to_regclass('production.london_houses') IS NOT NULL")
        ).scalar()

    statements = ["DROP MATERIALIZED VIEW IF EXISTS production.london_houses"]
    statements += [
        f"DROP TABLE IF EXISTS production.{t}" for t in reversed(_SERVING_TABLES)
    ]
    statements += ["DROP TABLE IF EXISTS production.uprn_lookup"]
    for t in _SERVING_TABLES:
        statements += [
            f"CREATE TABLE production.{t} (LIKE staging.{t} INCLUDING ALL)",
            f"INSERT INTO production.{t} SELECT * FROM staging.{t}",
        ]
    statements += [
        """
        CREATE TABLE production.uprn_lookup AS
        SELECT u."UPRN" AS uprn, a.id AS address_id
        FROM staging.ukhcs_table u
        INNER JOIN staging.address_table a ON a.uid = u.id
        """,
        "CREATE UNIQUE INDEX idx_prod_uprn_lookup ON production.uprn_lookup (uprn)",
        "CREATE INDEX idx_prod_address_postcode ON production.address_table (postcode)",
        "CREATE INDEX idx_prod_address_posttown ON production.address_table (posttown)",
    ]

    with _timed(log, "promote_staging_to_production"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        for statement in statements:
            with _timed(log, " ".join(statement.split())[:90]):
                connection.execute(text(statement))
        _analyze(
            connection,
            [f"production.{t}" for t in _SERVING_TABLES] + ["production.uprn_lookup"],
            log,
        )
        if view_exists and rebuild_view:
            _emit(log, "Rebuilding production.london_houses around the promotion...")
            from hpldn.backend.migrations.london_houses_view import (
                CREATE_INDEXES_SQL,
                CREATE_VIEW_SQL,
                EPC_ASOF_INDEX_SQL,
                STREET_KEY_FUNCTION_SQL,
            )

            with _timed(log, "idx_prod_epc_address_inspection"):
                connection.execute(text(EPC_ASOF_INDEX_SQL))
            connection.execute(text(STREET_KEY_FUNCTION_SQL))
            with _timed(log, "CREATE MATERIALIZED VIEW production.london_houses"):
                connection.execute(text(CREATE_VIEW_SQL))
            connection.execute(text(CREATE_INDEXES_SQL))
        elif view_exists:
            _emit(
                log,
                "production.london_houses was dropped by the promotion and "
                "NOT rebuilt here (rebuild_view=False); the caller is "
                "expected to rebuild it",
            )
    _emit(
        log,
        "Promoted %s (+ uprn_lookup) from staging to production"
        % ", ".join(_SERVING_TABLES),
    )


_ADDITIONAL_SERVING_TABLES = ["london_parcels"]


def promote_additional_serving_tables_to_production(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Promote serving tables added later than the core set (``london_parcels``).

    Same drop-and-rebuild as `promote_staging_to_production`, plus an index on
    ``uprn``, the API's join column.
    """
    _ensure_production_schema(engine)

    statements = [
        f"DROP TABLE IF EXISTS production.{t}"
        for t in reversed(_ADDITIONAL_SERVING_TABLES)
    ]
    for t in _ADDITIONAL_SERVING_TABLES:
        statements += [
            f"CREATE TABLE production.{t} (LIKE staging.{t} INCLUDING ALL)",
            f"INSERT INTO production.{t} SELECT * FROM staging.{t}",
        ]
    statements += [
        "CREATE INDEX idx_prod_london_parcels_uprn ON production.london_parcels (uprn)",
    ]

    step = "promote_additional_serving_tables_to_production"
    with _timed(log, step), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        for statement in statements:
            connection.execute(text(statement))
        _analyze(
            connection, [f"production.{t}" for t in _ADDITIONAL_SERVING_TABLES], log
        )
    _emit(
        log,
        "Promoted %s from staging to production"
        % ", ".join(_ADDITIONAL_SERVING_TABLES),
    )


def create_address_coordinates_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``production.address_coordinates``: latitude and longitude per served address.

    Takes only served addresses from ``staging.ukhcs_table`` rather than
    promoting its 41M rows, with a defensive LEFT JOIN.
    """
    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text("DROP TABLE IF EXISTS production.address_coordinates CASCADE")
        )
        connection.execute(
            text(
                """
                CREATE TABLE production.address_coordinates AS
                SELECT a.id AS address_id, u."LATITUDE" AS latitude, u."LONGITUDE" AS longitude
                FROM production.address_table a
                LEFT JOIN staging.ukhcs_table u ON u.id = a.uid
                """
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_address_coordinates_address_id "
                "ON production.address_coordinates (address_id)"
            )
        )
        row_count = connection.execute(
            text(
                "SELECT COUNT(*) FILTER (WHERE latitude IS NOT NULL) "
                "FROM production.address_coordinates"
            )
        ).scalar()
    if log is not None:
        log.info(
            f"Created production.address_coordinates with coordinates for "
            f"{row_count} addresses"
        )


# The view's certificate laterals (london_houses_view.CREATE_VIEW_SQL), so
# comps and neighbourhood builds see the same floor area and rating as the
# view: the rating as of the sale, the floor area as of the sale or, when no
# earlier certificate carries one, from the nearest later certificate (floor
# area barely changes; before 2009 almost no sale has an earlier one). LEFT
# JOINs: sales without a certificate stay in the pool. Exposes `e`.
PPD_EPC_ASOF_LATERAL = """
                    LEFT JOIN LATERAL (
                        SELECT ec.total_floor_area, ec.current_energy_rating
                        FROM production.epc_table ec
                        WHERE ec.address_id = p.address_id
                          AND ec.inspection_date IS NOT NULL
                          AND ec.inspection_date <= p.dateoftransfer
                        ORDER BY ec.inspection_date DESC,
                                 ec.lodgement_datetime DESC NULLS LAST,
                                 ec.id DESC
                        LIMIT 1
                    ) e_asof ON true
                    LEFT JOIN LATERAL (
                        SELECT ec.total_floor_area
                        FROM production.epc_table ec
                        WHERE e_asof.total_floor_area IS NULL
                          AND ec.address_id = p.address_id
                          AND ec.inspection_date > p.dateoftransfer
                          AND ec.total_floor_area IS NOT NULL
                        ORDER BY ec.inspection_date,
                                 ec.lodgement_datetime DESC NULLS LAST,
                                 ec.id DESC
                        LIMIT 1
                    ) e_later ON true
                    CROSS JOIN LATERAL (
                        SELECT COALESCE(e_asof.total_floor_area, e_later.total_floor_area)
                                   AS total_floor_area,
                               e_asof.current_energy_rating
                    ) e
"""


COMPS_K = 5
COMPS_WINDOW_MONTHS = 12

COMPS_RADIUS_M = 2000

COMPS_MATCH_PROPERTYTYPE = True


def create_comps_features_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    k: int = COMPS_K,
    window_months: int = COMPS_WINDOW_MONTHS,
    radius_m: int = COMPS_RADIUS_M,
    match_propertytype: bool = COMPS_MATCH_PROPERTYTYPE,
    use_btree_gist: Optional[bool] = None,
) -> None:
    """
    Create ``production.comps_features`` over every ``production.ppd_table`` sale.

    For each sale, the median price and price per m² of the `k` nearest other
    sales of the same type within `radius_m` in the prior `window_months`,
    and their count. Leakage-safe by construction. Coordinates are projected
    to EPSG:27700 in an UNLOGGED build table so the KNN operator uses metres.
    Floor areas come from `PPD_EPC_ASOF_LATERAL`, so price per m² uses only
    comparables with an area. With `use_btree_gist` (detected by default) the
    date window becomes an index condition.

    Notes
    -----
    Under ``hpp_rf``'s spatial holdout split, a held-out district can still
    draw comparables from its own sales, because this search ignores splits.
    """
    _ensure_production_schema(engine)
    build_table = "staging._comps_geo_build"
    propertytypes = ("D", "F", "S", "T")
    with _timed(log, "create_comps_features_table"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text(f"DROP TABLE IF EXISTS {build_table}"))
        with _timed(log, f"build {build_table}"):
            connection.execute(
                text(
                    f"""
                    CREATE UNLOGGED TABLE {build_table} AS
                    SELECT p.id AS ppd_id, p.dateoftransfer, p.price,
                           e.total_floor_area, p.propertytype,
                           ST_Transform(
                               ST_SetSRID(
                                   ST_MakePoint(ac.longitude, ac.latitude), 4326
                               ),
                               27700
                           ) AS geom
                    FROM production.ppd_table p
                    JOIN production.address_coordinates ac
                      ON ac.address_id = p.address_id
                    {PPD_EPC_ASOF_LATERAL}
                    WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                      AND p.price >= {MIN_PLAUSIBLE_PRICE}
                    """
                )
            )
        connection.execute(
            text("DROP TABLE IF EXISTS production.comps_features CASCADE")
        )
        if use_btree_gist is None:
            use_btree_gist = bool(
                connection.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
                ).first()
            )
        geom_index_cols = "geom, dateoftransfer" if use_btree_gist else "geom"
        _emit(
            log,
            f"comps geometry index: USING gist({geom_index_cols})"
            + ("" if use_btree_gist else " -- btree_gist not installed, so the "
               "12-month window stays a heap-fetch filter; see "
               "src/hpldn/backend/db/sql/db_init.sql"),
        )
        if match_propertytype:
            for propertytype in propertytypes:
                with _timed(log, f"gist index on {build_table} ({propertytype})"):
                    connection.execute(
                        text(
                            f"CREATE INDEX idx_comps_geo_{propertytype} "
                            f"ON {build_table} USING gist({geom_index_cols}) "
                            f"WHERE propertytype = '{propertytype}'"
                        )
                    )
        else:
            with _timed(log, f"gist index on {build_table}"):
                connection.execute(
                    text(f"CREATE INDEX ON {build_table} USING gist({geom_index_cols})")
                )
        connection.execute(
            text(f"CREATE INDEX ON {build_table} (propertytype, dateoftransfer)")
        )
        _analyze(connection, (build_table,), log)

        connection.execute(
            text(
                """
                CREATE UNLOGGED TABLE production.comps_features (
                    ppd_id BIGINT,
                    comp_median_price DOUBLE PRECISION,
                    comp_median_price_per_sqm DOUBLE PRECISION,
                    comp_txn_count BIGINT
                )
                """
            )
        )

        def comps_insert_sql(propertytype: Optional[str]):
            """Return the KNN insert, optionally restricted to one property type."""
            if propertytype is None:
                driver_predicate = ""
                neighbour_predicate = ""
            else:
                driver_predicate = f"WHERE lh1.propertytype = '{propertytype}'"
                neighbour_predicate = f"AND lh2.propertytype = '{propertytype}'"
            return text(
                f"""
                INSERT INTO production.comps_features
                SELECT lh1.ppd_id,
                       comp.comp_median_price,
                       comp.comp_median_price_per_sqm,
                       comp.comp_txn_count
                FROM {build_table} lh1
                CROSS JOIN LATERAL (
                    SELECT
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY nn.price)
                            AS comp_median_price,
                        percentile_cont(0.5) WITHIN GROUP (
                            ORDER BY nn.price / NULLIF(nn.total_floor_area, 0)
                        ) FILTER (
                            WHERE nn.total_floor_area IS NOT NULL
                        ) AS comp_median_price_per_sqm,
                        count(*) AS comp_txn_count
                    FROM (
                        SELECT lh2.price, lh2.total_floor_area
                        FROM {build_table} lh2
                        WHERE lh2.dateoftransfer < lh1.dateoftransfer
                          AND lh2.dateoftransfer >= lh1.dateoftransfer
                              - (:window_interval)::interval
                          AND lh2.ppd_id <> lh1.ppd_id
                          -- Bounded search: without this LIMIT :k always
                          -- fills, so comp_txn_count was the constant 5 and
                          -- a sparse-area property drew comps from
                          -- kilometres away unflagged. ST_DWithin is
                          -- index-backed on the GIST index above, and the
                          -- `<->` ordering still returns the nearest first.
                          AND ST_DWithin(lh2.geom, lh1.geom, :radius_m)
                          {neighbour_predicate}
                        ORDER BY lh2.geom <-> lh1.geom
                        LIMIT :k
                    ) nn
                ) comp
                {driver_predicate}
                """
            )

        params = {
            "k": k,
            "window_interval": f"{window_months} months",
            "radius_m": radius_m,
        }
        batches = propertytypes if match_propertytype else (None,)
        for propertytype in batches:
            label = propertytype or "all property types"
            with _timed(log, f"comps KNN search for {label}"):
                inserted = connection.execute(
                    comps_insert_sql(propertytype), params
                ).rowcount
            _emit(log, f"  {label}: {inserted} rows")

        connection.execute(text("ALTER TABLE production.comps_features SET LOGGED"))
        with _timed(log, "idx_prod_comps_features_ppd_id"):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX idx_prod_comps_features_ppd_id "
                    "ON production.comps_features (ppd_id)"
                )
            )
        connection.execute(text(f"DROP TABLE {build_table}"))
        _analyze(connection, ("production.comps_features",), log)
        row_count, with_comps = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE comp_txn_count > 0) "
                "FROM production.comps_features"
            )
        ).one()
    _emit(
        log,
        f"Created production.comps_features (k={k}, "
        f"window_months={window_months}, radius_m={radius_m}, "
        f"match_propertytype={match_propertytype}) with {row_count} rows, "
        f"{with_comps} of which found at least one comp",
    )


NEIGHBOUR_SEARCH_RADIUS_M = 1000
NEIGHBOUR_SEARCH_WINDOW_MONTHS = 24

NEIGHBOUR_SEARCH_MAX_K = 500

SPATIAL_LAG_RING_INNER_M = 500
SPATIAL_LAG_RING_OUTER_M = NEIGHBOUR_SEARCH_RADIUS_M
SPATIAL_LAG_WINDOW_MONTHS = 12

NEAREST20_K = 20
NEAREST20_WINDOW_MONTHS = 6

RECENCY_DECAY_LAMBDA = 0.10

PROPTYPE_MEDIAN_RADIUS_M = 800
PROPTYPE_MEDIAN_WINDOW_MONTHS = 6

EPC_BAND_SHARE_RADIUS_M = 500
EPC_BANDS = ("A", "B", "C", "D", "E", "F", "G")

FLOOR_AREA_PERCENTILE_RADIUS_M = 800
FLOOR_AREA_PERCENTILE_WINDOW_MONTHS = 12


def _neighbourhood_select_sql(build_table: str) -> Tuple[str, Dict]:
    """
    Return the neighbourhood SELECT as a format template and its bind parameters.

    ``{chunk_predicate}`` is TRUE for the WITH NO DATA table creation and
    ``mod(lh1.ppd_id, :jobs) = :chunk`` for each concurrent chunk, so column
    types match the inserts.
    """
    epc_share_select = ",\n                    ".join(
        f"AVG((nn.current_energy_rating = '{band}')::int::float) "
        f"FILTER (WHERE nn.dist <= :epc_radius_m) AS epc_band_share_{band.lower()}"
        for band in EPC_BANDS
    )
    epc_share_project = ",\n                   ".join(
        f"combined.epc_band_share_{band.lower()}" for band in EPC_BANDS
    )
    select_sql = f"""
            SELECT lh1.ppd_id,
                   combined.spatial_lag_price,
                   combined.median_price_per_sqm_20nn_6m,
                   combined.recency_weighted_price,
                   combined.proptype_local_median_800m_6m,
                   combined.floor_area_percentile_local,
                   {epc_share_project}
            FROM {build_table} lh1
            CROSS JOIN LATERAL (
                -- `nn`: ONE spatial fetch of every strictly-earlier sale
                -- within NEIGHBOUR_SEARCH_RADIUS_M/_WINDOW_MONTHS, shared by
                -- every column below (including nn20's top-20) instead of
                -- each re-running its own GIST scan + ST_Distance pass.
                -- MATERIALIZED pins that sharing: `nn` is referenced twice
                -- below (directly, and via nn20), and an inlined CTE would
                -- let the planner duplicate the scan right back.
                WITH nn AS MATERIALIZED (
                    SELECT lh2.price, lh2.total_floor_area, lh2.propertytype,
                           lh2.current_energy_rating,
                           ST_Distance(lh2.geom, lh1.geom) AS dist,
                           (lh1.dateoftransfer - lh2.dateoftransfer) / 30.4375 AS months_ago
                    FROM {build_table} lh2
                    WHERE lh2.dateoftransfer < lh1.dateoftransfer
                      AND lh2.dateoftransfer >= lh1.dateoftransfer
                          - (:search_window_interval)::interval
                      AND lh2.ppd_id <> lh1.ppd_id
                      AND ST_DWithin(lh2.geom, lh1.geom, :search_radius_m)
                    -- Bounded, same reasoning as create_comps_features_table's
                    -- own LIMIT k: without this, a dense inner-London
                    -- neighbourhood costs this row far more than the radius/
                    -- window alone implies. Nearest-first, so the tighter-
                    -- radius features below lose candidates last -- see
                    -- NEIGHBOUR_SEARCH_MAX_K's comment.
                    ORDER BY dist
                    LIMIT :max_neighbours
                ),
                -- Feature 2's "20 nearest in the 500m-1km ring, last 6
                -- months": a top-k over `nn`'s already-fetched rows (dist
                -- and months_ago are already computed), not a second
                -- spatial search -- `nn`'s radius/window is a superset of
                -- this one.
                nn20 AS (
                    SELECT price, total_floor_area
                    FROM nn
                    WHERE dist BETWEEN :spatial_lag_inner_m AND :spatial_lag_outer_m
                      AND months_ago <= :nearest20_window_months
                    ORDER BY dist
                    LIMIT :nearest20_k
                )
                SELECT
                    SUM(nn.price / nn.dist) FILTER (
                        WHERE nn.dist BETWEEN :spatial_lag_inner_m AND :spatial_lag_outer_m
                          AND nn.months_ago <= :spatial_lag_window_months
                    ) / NULLIF(SUM(1.0 / nn.dist) FILTER (
                        WHERE nn.dist BETWEEN :spatial_lag_inner_m AND :spatial_lag_outer_m
                          AND nn.months_ago <= :spatial_lag_window_months
                    ), 0) AS spatial_lag_price,
                    SUM(nn.price * exp(-:decay_lambda * nn.months_ago))
                        / NULLIF(SUM(exp(-:decay_lambda * nn.months_ago)), 0)
                        AS recency_weighted_price,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY nn.price) FILTER (
                        WHERE nn.dist <= :proptype_radius_m
                          AND nn.months_ago <= :proptype_window_months
                          AND nn.propertytype = lh1.propertytype
                    ) AS proptype_local_median_800m_6m,
                    AVG((nn.total_floor_area <= lh1.total_floor_area)::int::float) FILTER (
                        WHERE nn.dist <= :floor_area_radius_m
                          AND nn.months_ago <= :floor_area_window_months
                          AND nn.total_floor_area IS NOT NULL
                    ) AS floor_area_percentile_local,
                    {epc_share_select},
                    (
                        SELECT percentile_cont(0.5) WITHIN GROUP (
                            ORDER BY n20.price / NULLIF(n20.total_floor_area, 0)
                        )
                        FROM nn20 n20
                    ) AS median_price_per_sqm_20nn_6m
                FROM nn
            ) combined
            WHERE {{chunk_predicate}}
    """
    params = {
        "search_window_interval": f"{NEIGHBOUR_SEARCH_WINDOW_MONTHS} months",
        "search_radius_m": NEIGHBOUR_SEARCH_RADIUS_M,
        "max_neighbours": NEIGHBOUR_SEARCH_MAX_K,
        "spatial_lag_inner_m": SPATIAL_LAG_RING_INNER_M,
        "spatial_lag_outer_m": SPATIAL_LAG_RING_OUTER_M,
        "spatial_lag_window_months": SPATIAL_LAG_WINDOW_MONTHS,
        "decay_lambda": RECENCY_DECAY_LAMBDA,
        "proptype_radius_m": PROPTYPE_MEDIAN_RADIUS_M,
        "proptype_window_months": PROPTYPE_MEDIAN_WINDOW_MONTHS,
        "floor_area_radius_m": FLOOR_AREA_PERCENTILE_RADIUS_M,
        "floor_area_window_months": FLOOR_AREA_PERCENTILE_WINDOW_MONTHS,
        "epc_radius_m": EPC_BAND_SHARE_RADIUS_M,
        "nearest20_window_months": NEAREST20_WINDOW_MONTHS,
        "nearest20_k": NEAREST20_K,
    }
    return select_sql, params


def get_spatial_join_jobs(
    resources: Optional[HostResources] = None, log: Optional[logging.Logger] = None
) -> int:
    """
    Return how many chunks of a per-row spatial self-join to run concurrently.

    One per core, since each chunk is a single-backend statement, capped at 32
    to stay within ``max_connections``.
    """
    if resources is None:
        resources = detect_host_resources(log)
    return max(1, min(resources.vcpus, 32))


def _run_neighbourhood_chunk(
    engine: Engine,
    sql,
    params: Dict,
    chunk: int,
    jobs: int,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
) -> int:
    """
    Run one neighbourhood chunk on its own tuned connection and return rows inserted.

    Bulk tuning is re-applied because ``SET LOCAL`` does not follow a new
    connection.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        return connection.execute(
            sql, {**params, "jobs": jobs, "chunk": chunk}
        ).rowcount


def create_neighbourhood_features_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
) -> None:
    """
    Create ``production.neighbourhood_features`` over every ``production.ppd_table`` sale.

    Per sale: spatial-lag and recency-weighted prices, a 20-nearest median
    price per m², a property-type local median, the floor-area percentile
    against neighbours and the seven EPC band shares. Each row makes one
    ``AS MATERIALIZED`` neighbour fetch within ``NEIGHBOUR_SEARCH_RADIUS_M``
    and ``NEIGHBOUR_SEARCH_WINDOW_MONTHS``, capped at
    ``NEIGHBOUR_SEARCH_MAX_K``, and every aggregate reads it. Columns are NULL
    where a search finds nothing.

    Notes
    -----
    PostgreSQL will not parallelise a SELECT feeding an INSERT, nor place a
    Gather above a correlated LATERAL, so the work is split client-side into
    `get_spatial_join_jobs` interleaved ``mod(ppd_id, jobs)`` chunks on
    separate connections. The build and empty target tables are committed
    first; a failing chunk drops the partial target before re-raising.
    """
    _ensure_production_schema(engine)
    build_table = "staging._neighbourhood_geo_build"
    target = "production.neighbourhood_features"

    resources = detect_host_resources(log)
    jobs = get_spatial_join_jobs(resources, log)

    with _timed(log, "create_neighbourhood_features_table setup"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text(f"DROP TABLE IF EXISTS {build_table}"))
        with _timed(log, f"build {build_table}"):
            connection.execute(
                text(
                    f"""
                    CREATE UNLOGGED TABLE {build_table} AS
                    SELECT p.id AS ppd_id, p.dateoftransfer, p.price,
                           e.total_floor_area, p.propertytype,
                           e.current_energy_rating,
                           ST_Transform(
                               ST_SetSRID(
                                   ST_MakePoint(ac.longitude, ac.latitude), 4326
                               ),
                               27700
                           ) AS geom
                    FROM production.ppd_table p
                    JOIN production.address_coordinates ac
                      ON ac.address_id = p.address_id
                    {PPD_EPC_ASOF_LATERAL}
                    WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                      AND p.price >= {MIN_PLAUSIBLE_PRICE}
                    """
                )
            )
        connection.execute(text(f"DROP TABLE IF EXISTS {target} CASCADE"))
        with _timed(log, f"gist index on {build_table}"):
            connection.execute(text(f"CREATE INDEX ON {build_table} USING gist(geom)"))
        connection.execute(text(f"CREATE INDEX ON {build_table} (dateoftransfer)"))
        _analyze(connection, (build_table,), log)

    select_sql, params = _neighbourhood_select_sql(build_table)

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text(
                f"CREATE UNLOGGED TABLE {target} AS\n"
                + select_sql.format(chunk_predicate="TRUE")
                + "\nWITH NO DATA"
            ),
            params,
        )

    insert_sql = text(
        f"INSERT INTO {target}\n"
        + select_sql.format(
            chunk_predicate="mod(mod(lh1.ppd_id, :jobs) + :jobs, :jobs) = :chunk"
        )
    )

    _emit(
        log,
        f"neighbourhood features spatial self-join: {jobs} concurrent chunks "
        f"on {resources.vcpus} vCPU / {resources.memory_mib} MiB ({resources.source})",
    )
    chunk_engine = create_engine(engine.url, poolclass=NullPool)
    try:
        with _timed(log, "neighbourhood features spatial self-join"):
            if jobs == 1:
                inserted = _run_neighbourhood_chunk(
                    chunk_engine, insert_sql, params, 0, 1, cfg, log
                )
            else:
                with ThreadPoolExecutor(max_workers=jobs) as pool:
                    futures = [
                        pool.submit(
                            _run_neighbourhood_chunk,
                            chunk_engine,
                            insert_sql,
                            params,
                            chunk,
                            jobs,
                            cfg,
                            log,
                        )
                        for chunk in range(jobs)
                    ]
                    inserted = sum(future.result() for future in futures)
    except Exception:
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {target} CASCADE"))
            connection.execute(text(f"DROP TABLE IF EXISTS {build_table}"))
        raise
    finally:
        chunk_engine.dispose()

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"ALTER TABLE {target} SET LOGGED"))
        with _timed(log, "idx_prod_neighbourhood_features_ppd_id"):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX idx_prod_neighbourhood_features_ppd_id "
                    f"ON {target} (ppd_id)"
                )
            )
        connection.execute(text(f"DROP TABLE {build_table}"))
        _analyze(connection, (target,), log)
    _emit(log, f"Created {target} with {inserted} rows")


LSOA_TREND_RECENT_WINDOW_MONTHS = 12
LSOA_TREND_YEAR_AGO_OFFSET_MONTHS = 12
LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS = 60


def create_lsoa_price_trend_features_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
) -> None:
    """
    Create ``production.lsoa_price_trend_features``: MSOA price momentum per sale.

    ``msoa_price_momentum_1y`` is the MSOA's average price in the 12 months
    before the sale over the 12 months before that, minus 1;
    ``msoa_price_momentum_5y`` compares with the same window five years
    earlier. Windows end before the sale, so it is leakage-safe. NULL where a
    window is empty or the MSOA is unknown. The table keeps its ``lsoa`` name.
    """
    _ensure_production_schema(engine)
    build_table = "staging._lsoa_trend_build"
    with _timed(log, "create_lsoa_price_trend_features_table"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {build_table}"))
        with _timed(log, f"build {build_table}"):
            connection.execute(
                text(
                    f"""
                    CREATE UNLOGGED TABLE {build_table} AS
                    SELECT p.id AS ppd_id, p.dateoftransfer, p.price, lmf.msoa_code
                    FROM production.ppd_table p
                    JOIN production.lsoa_msoa_features lmf ON lmf.ppd_id = p.id
                    WHERE lmf.msoa_code IS NOT NULL
                      AND p.price >= {MIN_PLAUSIBLE_PRICE}
                    """
                )
            )
        connection.execute(
            text("DROP TABLE IF EXISTS production.lsoa_price_trend_features CASCADE")
        )
        with _timed(log, f"index on {build_table}"):
            connection.execute(text(f"CREATE INDEX ON {build_table} (msoa_code, dateoftransfer)"))
        _analyze(connection, (build_table,), log)

        connection.execute(
            text(
                """
                CREATE UNLOGGED TABLE production.lsoa_price_trend_features (
                    ppd_id BIGINT,
                    msoa_price_momentum_1y DOUBLE PRECISION,
                    msoa_price_momentum_5y DOUBLE PRECISION
                )
                """
            )
        )
        insert_sql = text(
            f"""
            INSERT INTO production.lsoa_price_trend_features
            SELECT lh1.ppd_id,
                   trend.recent_avg / NULLIF(trend.year_ago_avg, 0) - 1 AS msoa_price_momentum_1y,
                   trend.recent_avg / NULLIF(trend.five_year_ago_avg, 0) - 1 AS msoa_price_momentum_5y
            FROM {build_table} lh1
            CROSS JOIN LATERAL (
                SELECT
                    AVG(lh2.price) FILTER (
                        WHERE lh2.dateoftransfer >= lh1.dateoftransfer - (:recent_interval)::interval
                    ) AS recent_avg,
                    AVG(lh2.price) FILTER (
                        WHERE lh2.dateoftransfer < lh1.dateoftransfer - (:recent_interval)::interval
                          AND lh2.dateoftransfer >= lh1.dateoftransfer - (:year_ago_interval)::interval
                    ) AS year_ago_avg,
                    AVG(lh2.price) FILTER (
                        WHERE lh2.dateoftransfer < lh1.dateoftransfer
                              - (:five_year_recent_interval)::interval
                          AND lh2.dateoftransfer >= lh1.dateoftransfer
                              - (:five_year_ago_interval)::interval
                    ) AS five_year_ago_avg
                FROM {build_table} lh2
                WHERE lh2.msoa_code = lh1.msoa_code
                  AND lh2.dateoftransfer < lh1.dateoftransfer
                  AND lh2.dateoftransfer >= lh1.dateoftransfer - (:five_year_ago_interval)::interval
            ) trend
            """
        )
        params = {
            "recent_interval": f"{LSOA_TREND_RECENT_WINDOW_MONTHS} months",
            "year_ago_interval": f"{LSOA_TREND_RECENT_WINDOW_MONTHS + LSOA_TREND_YEAR_AGO_OFFSET_MONTHS} months",
            "five_year_recent_interval": f"{LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS} months",
            "five_year_ago_interval": f"{LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS + LSOA_TREND_RECENT_WINDOW_MONTHS} months",
        }
        with _timed(log, "msoa price trend windows"):
            inserted = connection.execute(insert_sql, params).rowcount

        connection.execute(text("ALTER TABLE production.lsoa_price_trend_features SET LOGGED"))
        with _timed(log, "idx_prod_lsoa_price_trend_features_ppd_id"):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX idx_prod_lsoa_price_trend_features_ppd_id "
                    "ON production.lsoa_price_trend_features (ppd_id)"
                )
            )
        connection.execute(text(f"DROP TABLE {build_table}"))
        _analyze(connection, ("production.lsoa_price_trend_features",), log)
    _emit(log, f"Created production.lsoa_price_trend_features with {inserted} rows")




def create_london_stations_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``production.london_stations`` from Doogal's station CSV.

    ``zone`` keeps the raw comma-separated zones; the proximity builder derives
    a single zone.
    """
    csv_fp = inputs.resolve("london_stations_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(f"London stations CSV not found: {csv_fp}")

    df = pd.read_csv(csv_fp)
    df = df.rename(
        columns={
            "Station": "station",
            "OS X": "os_x",
            "OS Y": "os_y",
            "Latitude": "latitude",
            "Longitude": "longitude",
            "Zone": "zone",
            "Postcode": "postcode",
        }
    )[["station", "os_x", "os_y", "latitude", "longitude", "zone", "postcode"]]

    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS production.london_stations"))
        connection.execute(
            text(
                """
                CREATE TABLE production.london_stations (
                    id SERIAL PRIMARY KEY,
                    station TEXT NOT NULL,
                    os_x INTEGER,
                    os_y INTEGER,
                    latitude DOUBLE PRECISION NOT NULL,
                    longitude DOUBLE PRECISION NOT NULL,
                    zone TEXT,
                    postcode TEXT
                )
                """
            )
        )
        df.to_sql(
            "london_stations",
            connection,
            schema="production",
            if_exists="append",
            index=False,
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_london_stations_zone ON production.london_stations (zone)"
            )
        )
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM production.london_stations")
        ).scalar()
    if log is not None:
        log.info(f"Created production.london_stations with {row_count} rows")


def create_boe_rate_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``production.boe_rate`` from the Bank Rate history CSV.

    Two-digit years parse as 1969-2068, correct for every row.
    """
    csv_fp = inputs.resolve("boe_rate_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(f"BoE base rate CSV not found: {csv_fp}")

    df = pd.read_csv(csv_fp)
    df.columns = ["date_changed", "rate"]
    df["date_changed"] = pd.to_datetime(df["date_changed"], format="%d %b %y").dt.date
    df["rate"] = pd.to_numeric(df["rate"])

    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS production.boe_rate"))
        connection.execute(
            text(
                """
                CREATE TABLE production.boe_rate (
                    date_changed DATE PRIMARY KEY,
                    rate NUMERIC(5, 2) NOT NULL
                )
                """
            )
        )
        df.to_sql(
            "boe_rate", connection, schema="production", if_exists="append", index=False
        )
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM production.boe_rate")
        ).scalar()
    if log is not None:
        log.info(f"Created production.boe_rate with {row_count} rows")


_UKHPI_COLUMN_MAP = {
    "Pivotable date": "period",
    "Average price All property types": "avg_price_all_property_types",
    "House price index All property types": "hpi_all_property_types",
    "Percentage change (yearly) All property types": "pct_change_yearly_all_property_types",
    "Sales volume All property types": "sales_volume_all_property_types",
    "Average price Detached houses": "avg_price_detached",
    "House price index Detached houses": "hpi_detached",
    "Average price Semi-detached houses": "avg_price_semi_detached",
    "House price index Semi-detached houses": "hpi_semi_detached",
    "Average price Terraced houses": "avg_price_terraced",
    "House price index Terraced houses": "hpi_terraced",
    "Average price Flats and maisonettes": "avg_price_flats_maisonettes",
    "House price index Flats and maisonettes": "hpi_flats_maisonettes",
}


def ukhpi_csv_path(cfg: Optional[Dict] = None) -> Path:
    """
    Return the UKHPI CSV path: ``cfg["data"]["ukhpi_csv_fp"]`` or the default file.

    The default filename is the download tool's, although the file now covers
    1995 onwards.
    """
    return inputs.resolve("ukhpi_csv_fp", cfg)


# HM Land Registry publishes a month's UKHPI on the third Wednesday of the
# month after next.
_UKHPI_PUBLISHED_DATE_SQL = """
UPDATE production.ukhpi
SET published_date = (
    date_trunc('month', period + interval '2 months')::date
    + mod(3 - EXTRACT(dow FROM date_trunc('month', period + interval '2 months'))::int + 7, 7)
    + 14
)
WHERE published_date IS DISTINCT FROM (
    date_trunc('month', period + interval '2 months')::date
    + mod(3 - EXTRACT(dow FROM date_trunc('month', period + interval '2 months'))::int + 7, 7)
    + 14
)
"""

# Bank of England quoted rates carry a ~2 month reporting lag.
_MORTGAGE_PUBLISHED_DATE_SQL = """
UPDATE production.mortgage_rates
SET published_date = (
    date_trunc('month', rate_date)
    + interval '2 months'
)::date
WHERE published_date IS DISTINCT FROM (
    date_trunc('month', rate_date)
    + interval '2 months'
)::date
"""


def create_ukhpi_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``production.ukhpi`` from the London UKHPI CSV.

    Keeps the `_UKHPI_COLUMN_MAP` subset and derives ``published_date`` at load
    time, raising if any row lacks one, since the view joins on it. The DROP is
    not CASCADE, so the view must be dropped first to reload.
    """
    csv_fp = ukhpi_csv_path(cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(f"UKHPI CSV not found: {csv_fp}")

    raw = pd.read_csv(csv_fp)
    df = raw[list(_UKHPI_COLUMN_MAP)].rename(columns=_UKHPI_COLUMN_MAP)
    df["period"] = pd.to_datetime(df["period"]).dt.date
    for col in df.columns:
        if col != "period":
            df[col] = pd.to_numeric(df[col], errors="coerce")

    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS production.ukhpi"))
        connection.execute(
            text(
                """
                CREATE TABLE production.ukhpi (
                    period DATE PRIMARY KEY,
                    avg_price_all_property_types NUMERIC,
                    hpi_all_property_types NUMERIC,
                    pct_change_yearly_all_property_types NUMERIC,
                    sales_volume_all_property_types NUMERIC,
                    avg_price_detached NUMERIC,
                    hpi_detached NUMERIC,
                    avg_price_semi_detached NUMERIC,
                    hpi_semi_detached NUMERIC,
                    avg_price_terraced NUMERIC,
                    hpi_terraced NUMERIC,
                    avg_price_flats_maisonettes NUMERIC,
                    hpi_flats_maisonettes NUMERIC,
                    -- Declared here, not ALTERed on afterwards, so this
                    -- function's drop-and-rebuild keeps it. The value is
                    -- derived below.
                    published_date DATE
                )
                """
            )
        )
        df.to_sql(
            "ukhpi", connection, schema="production", if_exists="append", index=False
        )
        connection.execute(text(_UKHPI_PUBLISHED_DATE_SQL))
        row_count, unpublished = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE published_date IS NULL) "
                "FROM production.ukhpi"
            )
        ).one()
    if unpublished:
        raise RuntimeError(
            f"{unpublished} production.ukhpi row(s) have a NULL published_date "
            "after the lag derivation -- london_houses.ukhpi_avg_price_at_sale "
            "would be NULL for every sale"
        )
    if log is not None:
        log.info(f"Created production.ukhpi with {row_count} rows")


def create_mortgage_rates_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``production.mortgage_rates`` from the BoE quoted rates CSV.

    Missing observations become NULL, and ``published_date`` carries the
    reporting lag.
    """
    csv_fp = inputs.resolve("mortgage_rates_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(f"Mortgage rates CSV not found: {csv_fp}")

    df = pd.read_csv(csv_fp)
    df.columns = [
        "rate_date",
        "rate_2y_95ltv",
        "rate_2y_90ltv",
        "rate_2y_75ltv",
        "rate_2y_60ltv",
        "rate_2y_85ltv",
    ]
    df["rate_date"] = pd.to_datetime(df["rate_date"], format="%d %b %y").dt.date
    for col in df.columns:
        if col != "rate_date":
            df[col] = pd.to_numeric(df[col], errors="coerce")

    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS production.mortgage_rates"))
        connection.execute(
            text(
                """
                CREATE TABLE production.mortgage_rates (
                    rate_date DATE PRIMARY KEY,
                    rate_2y_95ltv NUMERIC(5, 2),
                    rate_2y_90ltv NUMERIC(5, 2),
                    rate_2y_75ltv NUMERIC(5, 2),
                    rate_2y_60ltv NUMERIC(5, 2),
                    rate_2y_85ltv NUMERIC(5, 2),
                    -- Declared, not ALTERed on by a later revision. See
                    -- create_ukhpi_table for the same change and why.
                    published_date DATE
                )
                """
            )
        )
        df.to_sql(
            "mortgage_rates", connection, schema="production", if_exists="append", index=False
        )
        connection.execute(text(_MORTGAGE_PUBLISHED_DATE_SQL))
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM production.mortgage_rates")
        ).scalar()
    if log is not None:
        log.info(f"Created production.mortgage_rates with {row_count} rows")


def create_station_proximity_features_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``production.station_proximity_features``: nearest station per sale.

    For every sale with a coordinate, the distance, zone and name of the
    nearest zoned station, by KNN in EPSG:27700. The DROP is CASCADE because
    the view joins this table; rebuild the view afterwards.
    """
    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(
            text(
                """
                CREATE TEMP TABLE _station_geo AS
                SELECT s.station, z.min_zone AS zone,
                       ST_Transform(
                           ST_SetSRID(ST_MakePoint(s.longitude, s.latitude), 4326), 27700
                       ) AS geom
                FROM production.london_stations s
                CROSS JOIN LATERAL (
                    SELECT MIN(elem::int) AS min_zone
                    FROM unnest(string_to_array(s.zone, ',')) AS elem
                ) z
                WHERE s.zone IS NOT NULL
                """
            )
        )
        connection.execute(text("CREATE INDEX ON _station_geo USING gist(geom)"))
        connection.execute(text("ANALYZE _station_geo"))

        connection.execute(
            text(
                """
                CREATE TEMP TABLE _station_houses_geo AS
                SELECT p.id AS ppd_id,
                       ST_Transform(
                           ST_SetSRID(
                               ST_MakePoint(ac.longitude, ac.latitude), 4326
                           ), 27700
                       ) AS geom
                FROM production.ppd_table p
                JOIN production.address_coordinates ac
                  ON ac.address_id = p.address_id
                WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                """
            )
        )

        connection.execute(
            text(
                "DROP TABLE IF EXISTS production.station_proximity_features CASCADE"
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE production.station_proximity_features AS
                SELECT lh.ppd_id,
                       nn.station AS nearest_station_name,
                       nn.zone AS nearest_station_zone,
                       nn.distance_m / 1000.0 AS nearest_station_distance_km
                FROM _station_houses_geo lh
                CROSS JOIN LATERAL (
                    SELECT sg.station, sg.zone, sg.geom <-> lh.geom AS distance_m
                    FROM _station_geo sg
                    ORDER BY sg.geom <-> lh.geom
                    LIMIT 1
                ) nn
                """
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_station_proximity_features_ppd_id "
                "ON production.station_proximity_features (ppd_id)"
            )
        )
        connection.execute(text("DROP TABLE _station_geo, _station_houses_geo"))
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM production.station_proximity_features")
        ).scalar()
    if log is not None:
        log.info(f"Created production.station_proximity_features with {row_count} rows")




def create_london_green_spaces_tables(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load the OS Open Greenspace sites and access points into staging.

    Tables are created in SQL, loaded from the TQ GML with ogr2ogr (reprojected
    to EPSG:4326), and given metric areas and indexes afterwards.
    Drop-and-rebuild.
    """
    gml_fp = inputs.resolve("open_green_spaces_gml_fp", cfg)
    if not gml_fp.is_file():
        raise FileNotFoundError(f"OS Open Greenspace GML file not found: {gml_fp}")

    _ensure_production_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(
            text("DROP TABLE IF EXISTS staging.london_green_space_access_points")
        )
        connection.execute(text("DROP TABLE IF EXISTS staging.london_green_spaces"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.london_green_spaces (
                    id                  TEXT PRIMARY KEY,
                    space_function      TEXT NOT NULL,
                    distinctive_name_1  TEXT,
                    distinctive_name_2  TEXT,
                    geom                GEOMETRY(MultiPolygon, 4326) NOT NULL,
                    area_m2             DOUBLE PRECISION,
                    area_ha             DOUBLE PRECISION
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE staging.london_green_space_access_points (
                    id                      TEXT PRIMARY KEY,
                    access_type             TEXT NOT NULL,
                    ref_to_greenspace_site  TEXT NOT NULL
                        REFERENCES staging.london_green_spaces(id) ON DELETE CASCADE,
                    geom                    GEOMETRY(Point, 4326) NOT NULL
                )
                """
            )
        )
    if log is not None:
        log.debug(
            "Created empty staging.london_green_spaces / "
            "staging.london_green_space_access_points"
        )

    db_name = engine.url.database
    db_user = engine.url.username
    pg_conn_str = f"PG:dbname={db_name} user={db_user}"
    if engine.url.host:
        pg_conn_str += f" host={engine.url.host}"
    if engine.url.port:
        pg_conn_str += f" port={engine.url.port}"
    if engine.url.password:
        pg_conn_str += f" password={engine.url.password}"

    loads = [
        (
            "GreenspaceSite",
            "staging.london_green_spaces",
            "PROMOTE_TO_MULTI",
            "SELECT gml_id AS id, function AS space_function, "
            "distinctiveName1 AS distinctive_name_1, "
            "distinctiveName2 AS distinctive_name_2 FROM GreenspaceSite",
        ),
        (
            "AccessPoint",
            "staging.london_green_space_access_points",
            "POINT",
            "SELECT gml_id AS id, accessType AS access_type, "
            "refToGreenspaceSite AS ref_to_greenspace_site FROM AccessPoint",
        ),
    ]
    for layer, table, nlt, ogr_sql in loads:
        if log is not None:
            log.debug(f"Loading {layer} features into {table}...")
        result = subprocess.run(
            [
                "ogr2ogr",
                "-f",
                "PostgreSQL",
                "-append",
                pg_conn_str,
                str(gml_fp),
                "-nln",
                table,
                "-nlt",
                nlt,
                "-s_srs",
                "EPSG:27700",
                "-t_srs",
                "EPSG:4326",
                "-sql",
                ogr_sql,
                "-lco",
                "GEOMETRY_NAME=geom",
                "-lco",
                "FID=id",
                "-lco",
                "SPATIAL_INDEX=NONE",
                "--config",
                "OGR_GML_EXPOSE_FID",
                "YES",
                "--config",
                "PG_USE_COPY",
                "YES",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if log is not None:
                log.error(f"ogr2ogr failed for {layer}: {result.stderr}")
            raise RuntimeError(
                f"ogr2ogr failed for {layer} "
                f"(exit {result.returncode}): {result.stderr}"
            )
        if log is not None:
            log.debug(f"{layer} load complete.")

    if log is not None:
        log.debug("Computing area_m2/area_ha and building indexes...")
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text(
                """
                UPDATE staging.london_green_spaces
                SET area_m2 = ST_Area(ST_Transform(geom, 27700)),
                    area_ha = ST_Area(ST_Transform(geom, 27700)) / 10000.0
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_green_spaces_geom "
                "ON staging.london_green_spaces USING GIST (geom)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_green_spaces_function "
                "ON staging.london_green_spaces (space_function)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_green_spaces_name1_lower "
                "ON staging.london_green_spaces (lower(distinctive_name_1))"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_green_space_access_points_geom "
                "ON staging.london_green_space_access_points USING GIST (geom)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_green_space_access_points_site_ref "
                "ON staging.london_green_space_access_points (ref_to_greenspace_site)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_green_space_access_points_type "
                "ON staging.london_green_space_access_points (access_type)"
            )
        )
        connection.execute(text("ANALYZE staging.london_green_spaces"))
        connection.execute(text("ANALYZE staging.london_green_space_access_points"))

    with engine.begin() as connection:
        site_count, invalid_geom, null_area = connection.execute(
            text(
                "SELECT COUNT(*), "
                "COUNT(*) FILTER (WHERE NOT ST_IsValid(geom)), "
                "COUNT(*) FILTER (WHERE area_m2 IS NULL) "
                "FROM staging.london_green_spaces"
            )
        ).one()
        access_count, orphaned = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE gs.id IS NULL) "
                "FROM staging.london_green_space_access_points ap "
                "LEFT JOIN staging.london_green_spaces gs ON gs.id = ap.ref_to_greenspace_site"
            )
        ).one()
    if site_count <= 0:
        raise RuntimeError("No green spaces were loaded")
    if access_count <= 0:
        raise RuntimeError("No green space access points were loaded")
    if invalid_geom != 0:
        raise RuntimeError(f"{invalid_geom} green space geometries are invalid")
    if null_area != 0:
        raise RuntimeError(f"{null_area} green spaces have no computed area")
    if orphaned != 0:
        raise RuntimeError(
            f"{orphaned} access points reference a non-existent green space"
        )
    if log is not None:
        log.info(
            f"Created staging.london_green_spaces with {site_count} rows and "
            f"staging.london_green_space_access_points with {access_count} rows"
        )


# Composite index for the as-of EPC lateral, created by the backfill steps
# before their scans; london_houses_view has the shorter form. Both IF NOT EXISTS.
_EPC_ASOF_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_prod_epc_address_inspection "
    "ON production.epc_table "
    "(address_id, inspection_date DESC, lodgement_datetime DESC NULLS LAST, id DESC)"
)


_EPC_FLOOR_LEVEL_DEDUP_TABLE = "staging._epc_floor_level_dedup"


def _run_floor_level_chunk(
    engine: Engine,
    sql,
    jobs: int,
    chunk: int,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
) -> int:
    """Run one address-sharded chunk of the floor_level UPDATE on its own tuned connection."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        return connection.execute(sql, {"jobs": jobs, "chunk": chunk}).rowcount


def add_floor_level_to_epc_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Backfill ``production.epc_table.floor_level`` from ``staging.ukhpucl``.

    Takes each UPRN's most recently lodged real FLOOR_LEVEL (ignoring sentinel
    strings), normalised through `floor_level_case_sql`; values that are not a
    storey stay NULL. The dedup is built once with CREATE TABLE AS, which can
    run in parallel, and the UPDATE is sharded by address across
    `get_spatial_join_jobs` connections.
    """
    _emit(log, "Adding floor_level column to production.epc_table...")
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE production.epc_table ADD COLUMN IF NOT EXISTS floor_level VARCHAR"
            )
        )

    if log is not None:
        log.info(
            "Backfilling production.epc_table.floor_level from staging.ukhpucl "
            "(most recent non-null FLOOR_LEVEL per UPRN, via production.uprn_lookup)..."
        )

    dedup_table = _EPC_FLOOR_LEVEL_DEDUP_TABLE
    with _timed(log, "build floor_level dedup"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {dedup_table}"))
        connection.execute(
            text(
                f"""
                CREATE UNLOGGED TABLE {dedup_table} AS
                SELECT DISTINCT ON ("UPRN")
                    "UPRN" AS uprn,
                    {floor_level_case_sql()} AS floor_level
                FROM staging.ukhpucl
                -- FLOOR_LEVEL uses its own sentinel strings for "no value"
                -- rather than SQL NULL, and is restricted to spellings
                -- FLOOR_LEVEL_MAPPING recognises as an actual storey, so
                -- DISTINCT ON cannot pick 'NODATA!' or 'top floor' (a
                -- position, not a floor) just because it was lodged most
                -- recently.
                WHERE "FLOOR_LEVEL" IS NOT NULL
                  AND BTRIM("FLOOR_LEVEL") NOT IN ('', 'NODATA!', 'NO DATA!')
                  AND BTRIM("FLOOR_LEVEL") IN ({known_floor_levels_sql()})
                ORDER BY "UPRN", "LODGEMENT_DATETIME" DESC
                """
            )
        )
        connection.execute(text(f"CREATE UNIQUE INDEX ON {dedup_table} (uprn)"))
        with _timed(log, "idx_prod_epc_address_inspection"):
            connection.execute(text(_EPC_ASOF_INDEX_SQL))
        _analyze(
            connection,
            (dedup_table, "production.epc_table", "production.uprn_lookup"),
            log,
        )

    resources = detect_host_resources(log)
    jobs = get_spatial_join_jobs(resources, log)
    update_sql = text(f"""
    UPDATE production.epc_table e
    SET floor_level = dedup.floor_level
    FROM production.uprn_lookup ul
    INNER JOIN {dedup_table} dedup ON dedup.uprn = ul.uprn
    WHERE e.address_id = ul.address_id
      -- Only touch rows whose value actually changes: production.epc_table
      -- holds a property's whole certificate history, so an unguarded
      -- UPDATE would rewrite several million tuples to write back the
      -- value that was already there. Also makes a rerun close to free.
      AND e.floor_level IS DISTINCT FROM dedup.floor_level
      -- Client-side sharding by address_id -- `jobs` disjoint chunks run
      -- concurrently below, each its own single-core UPDATE (see the
      -- docstring's parallelism note). Double mod for the same reason
      -- create_neighbourhood_features_table's insert_sql uses it: mod()
      -- keeps the sign of its left argument, so a plain `mod(x, jobs) =
      -- chunk` would silently skip any negative address_id.
      AND mod(mod(e.address_id, :jobs) + :jobs, :jobs) = :chunk;
    """)

    _emit(
        log,
        f"floor_level backfill UPDATE: {jobs} concurrent chunks on "
        f"{resources.vcpus} vCPU / {resources.memory_mib} MiB ({resources.source})",
    )
    chunk_engine = create_engine(engine.url, poolclass=NullPool)
    try:
        with _timed(log, "UPDATE production.epc_table.floor_level"):
            if jobs == 1:
                updated = _run_floor_level_chunk(
                    chunk_engine, update_sql, 1, 0, cfg, log
                )
            else:
                with ThreadPoolExecutor(max_workers=jobs) as pool:
                    futures = [
                        pool.submit(
                            _run_floor_level_chunk,
                            chunk_engine,
                            update_sql,
                            jobs,
                            chunk,
                            cfg,
                            log,
                        )
                        for chunk in range(jobs)
                    ]
                    updated = sum(future.result() for future in futures)
    finally:
        chunk_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {dedup_table}"))

    _emit(
        log,
        f"Finished backfilling production.epc_table.floor_level "
        f"({updated} rows updated)!",
    )


def create_london_houses_materialized_view(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``production.london_houses`` from `london_houses_view.CREATE_VIEW_SQL`.

    Adds the unique ``ppd_id`` index that allows ``REFRESH MATERIALIZED VIEW
    CONCURRENTLY``. No superuser privileges are needed.
    """
    from hpldn.backend.migrations.london_houses_view import rebuild as _rebuild_view

    _ensure_production_schema(engine)
    with _timed(log, "create_london_houses_materialized_view"):
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            _analyze(connection, ("production.epc_table", "production.ppd_table"), log)
        row_count = _rebuild_view(engine, cfg, log)
    _emit(log, f"Created production.london_houses with {row_count} rows")


def create_api_readonly_role(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Set the ``hpldn_api`` role's password and resource limits.

    The role, its grants and default privileges come from
    ``src/hpldn/backend/db/sql/db_init.sql``; only the secret lives here. The password
    comes from ``cfg["api_role_password"]`` or ``HPLDN_API_ROLE_PASSWORD``.
    Sets ``statement_timeout = 5s`` and a small ``work_mem``. Idempotent; the
    role is created only as a fallback. Needs CREATEROLE; locally use
    ``src/hpldn/backend/db/sql/db_enable_api_login.sh`` instead.

    Raises
    ------
    ValueError
        If no password is configured.
    """
    password = (cfg or {}).get("api_role_password") or os.environ.get(
        "HPLDN_API_ROLE_PASSWORD"
    )
    if not password:
        raise ValueError(
            "No API role password: set HPLDN_API_ROLE_PASSWORD or "
            "cfg['api_role_password']. Without one the DDL below would "
            "create a login role with the literal password 'None'."
        )
    safe_password = password.replace("'", "''")

    statements = [
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hpldn_api') THEN
                CREATE ROLE hpldn_api NOLOGIN;
            END IF;
        END
        $$
        """,
        f"ALTER ROLE hpldn_api WITH LOGIN PASSWORD '{safe_password}'",
        "ALTER ROLE hpldn_api SET statement_timeout = '5s'",
        f"ALTER ROLE hpldn_api SET work_mem = '{API_ROLE_WORK_MEM}'",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    if log is not None:
        log.info(
            "hpldn_api: password and per-role limits set. Grants come from "
            "src/hpldn/backend/db/sql/db_init.sql and db_grants_post_migration.sql -- "
            "verify with src/hpldn/backend/db/sql/db_verify_init.sql"
        )


LONDON_BBOX = {
    "min_latitude": 51.28,
    "max_latitude": 51.70,
    "min_longitude": -0.52,
    "max_longitude": 0.34,
}

_PTAL_BAND_RANKS = {
    "0": 0,
    "1a": 1,
    "1b": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6a": 7,
    "6b": 8,
}


def create_ptal_2023_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``staging.ptal_2023`` from TfL's PTAL grid CSV.

    A faithful copy of the 159,451 cells' accessibility measures, keyed on the
    integral ``grid_id``. Drop-and-rebuild.
    """
    csv_fp = inputs.resolve("ptal_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(f"PTAL grid CSV not found: {csv_fp}")

    df = pd.read_csv(csv_fp, encoding="utf-8-sig")
    df = df.rename(
        columns={
            "GridID": "grid_id",
            "BUS": "bus",
            "LUL": "lul",
            "RAIL": "rail",
            "TRAM": "tram",
            "AI": "ai",
            "PTAL_2023": "ptal_band",
            "Shape_Leng": "shape_leng",
            "Shape_Area": "shape_area",
        }
    )[
        [
            "grid_id",
            "bus",
            "lul",
            "rail",
            "tram",
            "ai",
            "ptal_band",
            "shape_leng",
            "shape_area",
        ]
    ]
    df["grid_id"] = df["grid_id"].astype("int64")
    df["ptal_band"] = df["ptal_band"].astype(str).str.strip()

    unknown_bands = sorted(set(df["ptal_band"]) - set(_PTAL_BAND_RANKS))
    if unknown_bands:
        raise ValueError(f"unexpected PTAL band(s) in {csv_fp}: {unknown_bands}")
    if not df["grid_id"].is_unique:
        raise ValueError(f"GridID is not unique in {csv_fp}")

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP MATERIALIZED VIEW IF EXISTS staging.ptal_london"))
        connection.execute(text("DROP TABLE IF EXISTS staging.ptal_coords"))
        connection.execute(text("DROP TABLE IF EXISTS staging.ptal_2023"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.ptal_2023 (
                    grid_id     INTEGER PRIMARY KEY,
                    bus         DOUBLE PRECISION NOT NULL,
                    lul         DOUBLE PRECISION NOT NULL,
                    rail        DOUBLE PRECISION NOT NULL,
                    tram        DOUBLE PRECISION NOT NULL,
                    ai          DOUBLE PRECISION NOT NULL,
                    ptal_band   TEXT NOT NULL,
                    shape_leng  DOUBLE PRECISION,
                    shape_area  DOUBLE PRECISION,
                    CONSTRAINT ck_ptal_2023_band CHECK (
                        ptal_band IN ('0','1a','1b','2','3','4','5','6a','6b')
                    ),
                    CONSTRAINT ck_ptal_2023_ai_non_negative CHECK (ai >= 0)
                )
                """
            )
        )
        df.to_sql(
            "ptal_2023",
            connection,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=10000,
            method="multi",
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_ptal_2023_band ON staging.ptal_2023 (ptal_band)"
            )
        )
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.ptal_2023")
        ).scalar()
    if log is not None:
        log.info(f"Created staging.ptal_2023 with {row_count} rows")


def create_ptal_coords_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``staging.ptal_coords``: each PTAL cell's centroid.

    One row per cell, foreign-keyed to ``staging.ptal_2023``, in WGS84 and
    British National Grid, with a GIST-indexed point. The load checks every
    cell has exactly one coordinate.
    """
    csv_fp = inputs.resolve("ptal_centroids_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(
            f"PTAL grid centroid CSV not found: {csv_fp}. It is fetched "
            f"once from the TfL ArcGIS FeatureServer -- see `python -m "
            f"hpldn.backend.scripts.fetch_ptal_centroids`."
        )

    df = pd.read_csv(csv_fp)
    df = df.rename(columns={"GridID": "grid_id"})[
        ["grid_id", "latitude", "longitude", "easting", "northing"]
    ]
    df["grid_id"] = df["grid_id"].astype("int64")
    if not df["grid_id"].is_unique:
        raise ValueError(f"GridID is not unique in {csv_fp}")
    if df[["latitude", "longitude"]].isna().any().any():
        raise ValueError(f"missing latitude/longitude in {csv_fp}")

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text("DROP MATERIALIZED VIEW IF EXISTS staging.ptal_london"))
        connection.execute(text("DROP TABLE IF EXISTS staging.ptal_coords"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.ptal_coords (
                    grid_id   INTEGER PRIMARY KEY
                              REFERENCES staging.ptal_2023 (grid_id) ON DELETE CASCADE,
                    latitude  DOUBLE PRECISION NOT NULL,
                    longitude DOUBLE PRECISION NOT NULL,
                    easting   DOUBLE PRECISION NOT NULL,
                    northing  DOUBLE PRECISION NOT NULL,
                    geom      geometry(Point, 4326),
                    CONSTRAINT ck_ptal_coords_latitude CHECK (latitude BETWEEN -90 AND 90),
                    CONSTRAINT ck_ptal_coords_longitude CHECK (longitude BETWEEN -180 AND 180)
                )
                """
            )
        )
        df.to_sql(
            "ptal_coords",
            connection,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=10000,
            method="multi",
        )
        connection.execute(
            text(
                "UPDATE staging.ptal_coords "
                "SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_ptal_coords_geom ON staging.ptal_coords USING GIST (geom)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_ptal_coords_lat_lon "
                "ON staging.ptal_coords (latitude, longitude)"
            )
        )
        missing = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_2023 p "
                "LEFT JOIN staging.ptal_coords c ON c.grid_id = p.grid_id "
                "WHERE c.grid_id IS NULL"
            )
        ).scalar()
        if missing != 0:
            raise RuntimeError(f"{missing} staging.ptal_2023 cells have no coordinate")
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.ptal_coords")
        ).scalar()
    if log is not None:
        log.info(f"Created staging.ptal_coords with {row_count} rows")


_PTAL_LONDON_VIEW_SQL = text(
    f"""
    CREATE MATERIALIZED VIEW staging.ptal_london AS
    SELECT
        p.grid_id,
        p.bus,
        p.lul,
        p.rail,
        p.tram,
        p.ai,
        p.ptal_band,
        CASE p.ptal_band
            {" ".join(f"WHEN '{band}' THEN {rank}" for band, rank in _PTAL_BAND_RANKS.items())}
        END::SMALLINT AS ptal_band_rank,
        p.shape_leng,
        p.shape_area,
        c.latitude,
        c.longitude,
        c.easting,
        c.northing,
        c.geom
    FROM staging.ptal_2023 p
    JOIN staging.ptal_coords c ON c.grid_id = p.grid_id
    WHERE c.latitude IS NOT NULL
      AND c.longitude IS NOT NULL
      AND c.latitude BETWEEN {LONDON_BBOX["min_latitude"]} AND {LONDON_BBOX["max_latitude"]}
      AND c.longitude BETWEEN {LONDON_BBOX["min_longitude"]} AND {LONDON_BBOX["max_longitude"]}
    """
)


def create_ptal_london_view(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``staging.ptal_london``, PTAL cells joined to their centroids.

    Adds ``ptal_band_rank``, a unique ``grid_id`` index for concurrent
    refresh, and GIST and rank indexes.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP MATERIALIZED VIEW IF EXISTS staging.ptal_london"))
        connection.execute(_PTAL_LONDON_VIEW_SQL)
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_staging_ptal_london_grid_id "
                "ON staging.ptal_london (grid_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_ptal_london_geom "
                "ON staging.ptal_london USING GIST (geom)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_ptal_london_band_rank "
                "ON staging.ptal_london (ptal_band_rank)"
            )
        )
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.ptal_london")
        ).scalar()
        source_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.ptal_2023")
        ).scalar()
    if log is not None:
        log.info(
            f"Created staging.ptal_london with {row_count} rows "
            f"({source_count - row_count} of {source_count} cells excluded as non-London)"
        )


PTAL_FALLBACK_MAX_DISTANCE_M = 150


def create_ptal_cell_polygons_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``staging.ptal_cell_polygons``: each PTAL cell's boundary.

    ``geom`` is ``geometry(MultiPolygon, 4326)`` because clipped boundary cells
    can be multi-part; WKT is loaded first, then converted.
    """
    csv_fp = inputs.resolve("ptal_polygons_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(
            f"PTAL grid polygon CSV not found: {csv_fp}. It is fetched "
            f"once from the TfL ArcGIS FeatureServer -- see `python -m "
            f"hpldn.backend.scripts.fetch_ptal_centroids --geometry polygon`."
        )

    df = pd.read_csv(csv_fp)
    df = df.rename(columns={"GridID": "grid_id"})[["grid_id", "wkt"]]
    df["grid_id"] = df["grid_id"].astype("int64")
    if not df["grid_id"].is_unique:
        raise ValueError(f"GridID is not unique in {csv_fp}")
    if df["wkt"].isna().any():
        raise ValueError(f"missing polygon geometry in {csv_fp}")

    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text("DROP TABLE IF EXISTS production.ptal_features"))
        connection.execute(text("DROP TABLE IF EXISTS staging.ptal_cell_polygons"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.ptal_cell_polygons (
                    grid_id INTEGER PRIMARY KEY
                            REFERENCES staging.ptal_2023 (grid_id) ON DELETE CASCADE,
                    wkt     TEXT NOT NULL,
                    geom    geometry(MultiPolygon, 4326)
                )
                """
            )
        )
        df.to_sql(
            "ptal_cell_polygons",
            connection,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=5000,
            method="multi",
        )
        connection.execute(
            text(
                "UPDATE staging.ptal_cell_polygons "
                "SET geom = ST_Multi(ST_SetSRID(ST_GeomFromText(wkt), 4326))"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE staging.ptal_cell_polygons ALTER COLUMN geom SET NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_ptal_cell_polygons_geom "
                "ON staging.ptal_cell_polygons USING GIST (geom)"
            )
        )
        missing = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_2023 p "
                "LEFT JOIN staging.ptal_cell_polygons c ON c.grid_id = p.grid_id "
                "WHERE c.grid_id IS NULL"
            )
        ).scalar()
        if missing != 0:
            raise RuntimeError(f"{missing} staging.ptal_2023 cells have no polygon")
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.ptal_cell_polygons")
        ).scalar()
    if log is not None:
        log.info(f"Created staging.ptal_cell_polygons with {row_count} rows")


def create_ptal_features_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``production.ptal_features``: PTAL band rank and index per sale.

    Each sale's point is matched to the cell polygon containing it, falling
    back to the nearest centroid within ``PTAL_FALLBACK_MAX_DISTANCE_M``;
    beyond that both columns are NULL.
    """
    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(
            text("DROP TABLE IF EXISTS production.ptal_features CASCADE")
        )
        connection.execute(
            text(
                """
                CREATE TEMP TABLE _ptal_geo AS
                SELECT p.id AS ppd_id,
                       ST_SetSRID(ST_MakePoint(ac.longitude, ac.latitude), 4326) AS geom
                FROM production.ppd_table p
                JOIN production.address_coordinates ac ON ac.address_id = p.address_id
                WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                """
            )
        )
        connection.execute(text("CREATE INDEX ON _ptal_geo USING gist(geom)"))
        connection.execute(text("ANALYZE _ptal_geo"))
        connection.execute(
            text(
                """
                CREATE TABLE production.ptal_features AS
                SELECT g.ppd_id,
                       COALESCE(contained.ptal_band_rank, nearest.ptal_band_rank) AS ptal_band_rank,
                       COALESCE(contained.ai, nearest.ai) AS ptal_ai
                FROM _ptal_geo g
                LEFT JOIN LATERAL (
                    SELECT pl.ptal_band_rank, pl.ai
                    FROM staging.ptal_cell_polygons poly
                    JOIN staging.ptal_london pl ON pl.grid_id = poly.grid_id
                    WHERE ST_Contains(poly.geom, g.geom)
                    LIMIT 1
                ) contained ON true
                LEFT JOIN LATERAL (
                    SELECT pl.ptal_band_rank, pl.ai
                    FROM staging.ptal_london pl
                    WHERE contained.ptal_band_rank IS NULL
                      AND ST_DWithin(
                              ST_Transform(pl.geom, 27700),
                              ST_Transform(g.geom, 27700),
                              :max_distance_m
                          )
                    ORDER BY ST_Transform(pl.geom, 27700) <-> ST_Transform(g.geom, 27700)
                    LIMIT 1
                ) nearest ON true
                """
            ),
            {"max_distance_m": PTAL_FALLBACK_MAX_DISTANCE_M},
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_ptal_features_ppd_id "
                "ON production.ptal_features (ppd_id)"
            )
        )
        connection.execute(text("DROP TABLE _ptal_geo"))
        row_count, contained_count, missing_count = connection.execute(
            text(
                """
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE ptal_band_rank IS NOT NULL),
                    COUNT(*) FILTER (WHERE ptal_band_rank IS NULL)
                FROM production.ptal_features
                """
            )
        ).one()
    if log is not None:
        log.info(
            f"Created production.ptal_features with {row_count} rows "
            f"({contained_count} resolved a cell, {missing_count} left NULL "
            f"beyond the {PTAL_FALLBACK_MAX_DISTANCE_M}m fallback bound)"
        )



RIVER_LONDON_BBOX_27700 = (502000, 154000, 563500, 203000)

RIVER_PROXIMITY_RADIUS_M = 1000


def create_staging_rivers_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``staging.rivers`` from OS Open Rivers.

    Loaded with ogr2ogr, clipped to ``RIVER_LONDON_BBOX_27700`` and reprojected
    to EPSG:4326, with a centroid per watercourse. Drop-and-rebuild.
    """
    gml_fp = inputs.resolve("open_rivers_gml_fp", cfg)
    if not gml_fp.is_file():
        raise FileNotFoundError(f"OS Open Rivers GML file not found: {gml_fp}")

    with engine.begin() as connection:
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text("DROP TABLE IF EXISTS staging.rivers"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.rivers (
                    id                            TEXT PRIMARY KEY,
                    watercourse_name              TEXT,
                    watercourse_name_alternative   TEXT,
                    form                          TEXT,
                    flow_direction                TEXT,
                    fictitious                    BOOLEAN,
                    length_m                      INTEGER,
                    geom                          GEOMETRY(LineString, 4326) NOT NULL,
                    latitude                      DOUBLE PRECISION,
                    longitude                     DOUBLE PRECISION
                )
                """
            )
        )
    if log is not None:
        log.debug("Created empty staging.rivers")

    db_name = engine.url.database
    db_user = engine.url.username
    pg_conn_str = f"PG:dbname={db_name} user={db_user}"
    if engine.url.host:
        pg_conn_str += f" host={engine.url.host}"
    if engine.url.port:
        pg_conn_str += f" port={engine.url.port}"
    if engine.url.password:
        pg_conn_str += f" password={engine.url.password}"

    minx, miny, maxx, maxy = RIVER_LONDON_BBOX_27700
    if log is not None:
        log.debug("Loading WatercourseLink features into staging.rivers...")
    result = subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "PostgreSQL",
            "-append",
            pg_conn_str,
            str(gml_fp),
            "-nln",
            "staging.rivers",
            "-nlt",
            "LINESTRING",
            "-s_srs",
            "EPSG:27700",
            "-t_srs",
            "EPSG:4326",
            "-spat",
            str(minx),
            str(miny),
            str(maxx),
            str(maxy),
            "-sql",
            "SELECT gml_id AS id, watercourseName AS watercourse_name, "
            "watercourseNameAlternative AS watercourse_name_alternative, "
            "form AS form, flowDirection AS flow_direction, "
            "fictitious AS fictitious, length AS length_m FROM WatercourseLink",
            "-lco",
            "GEOMETRY_NAME=geom",
            "-lco",
            "FID=id",
            "-lco",
            "SPATIAL_INDEX=NONE",
            "--config",
            "OGR_GML_EXPOSE_FID",
            "YES",
            "--config",
            "PG_USE_COPY",
            "YES",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if log is not None:
            log.error(f"ogr2ogr failed for WatercourseLink: {result.stderr}")
        raise RuntimeError(
            f"ogr2ogr failed for WatercourseLink "
            f"(exit {result.returncode}): {result.stderr}"
        )
    if log is not None:
        log.debug("WatercourseLink load complete.")

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text(
                """
                UPDATE staging.rivers
                SET latitude = ST_Y(ST_Centroid(geom)),
                    longitude = ST_X(ST_Centroid(geom))
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_rivers_geom "
                "ON staging.rivers USING GIST (geom)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_rivers_name_lower "
                "ON staging.rivers (lower(watercourse_name))"
            )
        )
        connection.execute(text("ANALYZE staging.rivers"))

    with engine.begin() as connection:
        row_count, invalid_geom, null_latlong = connection.execute(
            text(
                "SELECT COUNT(*), "
                "COUNT(*) FILTER (WHERE NOT ST_IsValid(geom)), "
                "COUNT(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL) "
                "FROM staging.rivers"
            )
        ).one()
    if row_count <= 0:
        raise RuntimeError("No rivers were loaded")
    if invalid_geom != 0:
        raise RuntimeError(f"{invalid_geom} river geometries are invalid")
    if null_latlong != 0:
        raise RuntimeError(f"{null_latlong} rivers have no computed latitude/longitude")
    if log is not None:
        log.info(f"Created staging.rivers with {row_count} rows")


def create_river_proximity_features_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    radius_m: int = RIVER_PROXIMITY_RADIUS_M,
) -> None:
    """
    Create ``production.river_proximity_features``: distance to the nearest watercourse.

    KNN in EPSG:27700 bounded by ``ST_DWithin(radius_m)``, which lets the index
    scan stop early without building a buffer. Sales with no river within the
    radius get NULL.
    """
    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(
            text(
                """
                CREATE TEMP TABLE _river_geo AS
                SELECT id, ST_Transform(geom, 27700) AS geom FROM staging.rivers
                """
            )
        )
        connection.execute(text("CREATE INDEX ON _river_geo USING gist(geom)"))
        connection.execute(text("ANALYZE _river_geo"))

        connection.execute(
            text(
                """
                CREATE TEMP TABLE _river_houses_geo AS
                SELECT p.id AS ppd_id,
                       ST_Transform(
                           ST_SetSRID(
                               ST_MakePoint(ac.longitude, ac.latitude), 4326
                           ), 27700
                       ) AS geom
                FROM production.ppd_table p
                JOIN production.address_coordinates ac
                  ON ac.address_id = p.address_id
                WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                """
            )
        )
        connection.execute(text("CREATE INDEX ON _river_houses_geo USING gist(geom)"))
        connection.execute(text("ANALYZE _river_houses_geo"))

        connection.execute(
            text(
                "DROP TABLE IF EXISTS production.river_proximity_features CASCADE"
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE production.river_proximity_features AS
                SELECT lh.ppd_id, nn.distance_to_river_m
                FROM _river_houses_geo lh
                LEFT JOIN LATERAL (
                    SELECT ROUND((rg.geom <-> lh.geom)::numeric)::smallint
                        AS distance_to_river_m
                    FROM _river_geo rg
                    WHERE ST_DWithin(rg.geom, lh.geom, :radius_m)
                    ORDER BY rg.geom <-> lh.geom
                    LIMIT 1
                ) nn ON true
                """
            ),
            {"radius_m": radius_m},
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_river_proximity_features_ppd_id "
                "ON production.river_proximity_features (ppd_id)"
            )
        )
        connection.execute(text("DROP TABLE _river_geo, _river_houses_geo"))
        _analyze(connection, ("production.river_proximity_features",), log)
        row_count, with_distance = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(distance_to_river_m) "
                "FROM production.river_proximity_features"
            )
        ).one()
    _emit(
        log,
        f"Created production.river_proximity_features (radius_m={radius_m}) "
        f"with {row_count} rows, {with_distance} within {radius_m}m of a river",
    )



LSOA_FALLBACK_MAX_DISTANCE_M = 150


def create_lsoa_boundaries_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``staging.lsoa_boundaries``: London's LSOA 2011 polygons.

    Filtered on load to the 4,835 London LSOAs from the England and Wales CSV.
    """
    _ensure_production_schema(engine)
    csv_fp = inputs.resolve("lsoa_boundaries_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(
            f"LSOA11 boundary CSV not found: {csv_fp}. Fetch it with "
            f"`python -m hpldn.backend.scripts.fetch_lsoa_boundaries`."
        )

    df = pd.read_csv(csv_fp, dtype=str)
    if df["wkt"].isna().any():
        raise ValueError(f"missing polygon geometry in {csv_fp}")
    if not df["lsoa11cd"].is_unique:
        raise ValueError(f"lsoa11cd is not unique in {csv_fp}")

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))

        london_codes = {
            row[0]
            for row in connection.execute(
                text("SELECT lsoa11cd FROM staging.lsoa WHERE lad22cd LIKE 'E09%'")
            )
        }
        london_df = df[df["lsoa11cd"].isin(london_codes)]
        missing_from_csv = london_codes - set(london_df["lsoa11cd"])
        if missing_from_csv:
            raise RuntimeError(
                f"{len(missing_from_csv)} London LSOA11s have no polygon in "
                f"{csv_fp}: {sorted(missing_from_csv)[:10]}"
            )

        connection.execute(text("DROP TABLE IF EXISTS staging.property_lsoa"))
        connection.execute(text("DROP TABLE IF EXISTS staging.lsoa_boundaries"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.lsoa_boundaries (
                    lsoa11cd VARCHAR PRIMARY KEY,
                    lsoa11nm VARCHAR,
                    wkt      TEXT NOT NULL,
                    geom     geometry(MultiPolygon, 4326)
                )
                """
            )
        )
        london_df.to_sql(
            "lsoa_boundaries",
            connection,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=2000,
            method="multi",
        )
        connection.execute(
            text(
                "UPDATE staging.lsoa_boundaries "
                "SET geom = ST_Multi(ST_SetSRID(ST_GeomFromText(wkt), 4326))"
            )
        )
        connection.execute(
            text("ALTER TABLE staging.lsoa_boundaries ALTER COLUMN geom SET NOT NULL")
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_lsoa_boundaries_geom "
                "ON staging.lsoa_boundaries USING GIST (geom)"
            )
        )
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.lsoa_boundaries")
        ).scalar()
    _emit(
        log,
        f"Created staging.lsoa_boundaries with {row_count} London LSOA11 "
        f"polygons (of {len(df)} in {csv_fp})",
    )


def create_property_lsoa_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``staging.property_lsoa``: the LSOA 2011 each sale's point falls in.

    Point-in-polygon, falling back to the nearest boundary within
    ``LSOA_FALLBACK_MAX_DISTANCE_M``; beyond that NULL. Date- and price-free,
    so it carries no leakage.
    """
    _ensure_production_schema(engine)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text("DROP TABLE IF EXISTS staging.property_lsoa"))
        connection.execute(
            text(
                """
                CREATE TEMP TABLE _property_lsoa_geo AS
                SELECT p.id AS ppd_id,
                       ST_SetSRID(ST_MakePoint(ac.longitude, ac.latitude), 4326) AS geom
                FROM production.ppd_table p
                JOIN production.address_coordinates ac ON ac.address_id = p.address_id
                WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                """
            )
        )
        connection.execute(text("CREATE INDEX ON _property_lsoa_geo USING gist(geom)"))
        connection.execute(text("ANALYZE _property_lsoa_geo"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.property_lsoa AS
                SELECT g.ppd_id,
                       COALESCE(contained.lsoa11cd, nearest.lsoa11cd) AS lsoa11cd,
                       CASE
                           WHEN contained.lsoa11cd IS NOT NULL THEN 'contains'
                           WHEN nearest.lsoa11cd IS NOT NULL THEN 'nearest'
                           ELSE NULL
                       END AS assignment_method,
                       CASE
                           WHEN contained.lsoa11cd IS NOT NULL THEN 0
                           ELSE nearest.dist_m
                       END AS assignment_dist_m
                FROM _property_lsoa_geo g
                LEFT JOIN LATERAL (
                    SELECT b.lsoa11cd
                    FROM staging.lsoa_boundaries b
                    WHERE ST_Contains(b.geom, g.geom)
                    LIMIT 1
                ) contained ON true
                LEFT JOIN LATERAL (
                    SELECT b.lsoa11cd,
                           ST_Distance(
                               ST_Transform(b.geom, 27700), ST_Transform(g.geom, 27700)
                           ) AS dist_m
                    FROM staging.lsoa_boundaries b
                    WHERE contained.lsoa11cd IS NULL
                      AND ST_DWithin(
                              ST_Transform(b.geom, 27700), ST_Transform(g.geom, 27700),
                              :max_distance_m
                          )
                    ORDER BY ST_Transform(b.geom, 27700) <-> ST_Transform(g.geom, 27700)
                    LIMIT 1
                ) nearest ON true
                """
            ),
            {"max_distance_m": LSOA_FALLBACK_MAX_DISTANCE_M},
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_property_lsoa_ppd_id "
                "ON staging.property_lsoa (ppd_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX idx_prod_property_lsoa_lsoa11cd "
                "ON staging.property_lsoa (lsoa11cd)"
            )
        )
        connection.execute(text("DROP TABLE _property_lsoa_geo"))
        _analyze(connection, ("staging.property_lsoa",), log)

        row_count, contains_count, nearest_count, null_count, distinct_lsoa = (
            connection.execute(
                text(
                    """
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE assignment_method = 'contains'),
                           COUNT(*) FILTER (WHERE assignment_method = 'nearest'),
                           COUNT(*) FILTER (WHERE lsoa11cd IS NULL),
                           COUNT(DISTINCT lsoa11cd)
                    FROM staging.property_lsoa
                    """
                )
            ).one()
        )

    contains_pct = contains_count / row_count if row_count else 0.0
    _emit(
        log,
        f"Created staging.property_lsoa with {row_count} rows: "
        f"{contains_count} contains ({contains_pct:.1%}), {nearest_count} "
        f"nearest-fallback, {null_count} NULL beyond "
        f"{LSOA_FALLBACK_MAX_DISTANCE_M}m, {distinct_lsoa} distinct lsoa11cd",
    )
    if contains_pct <= 0.95:
        raise RuntimeError(
            f"only {contains_pct:.1%} of property_lsoa rows resolved by "
            f"ST_Contains (want >95%) -- check the boundary generalisation "
            f"level (BGC, not BSC/BUC) before trusting this table"
        )
    if distinct_lsoa <= 100:
        raise RuntimeError(
            f"staging.property_lsoa resolved only {distinct_lsoa} distinct "
            f"lsoa11cd values -- a borough-level proxy, not a real "
            f"point-in-LSOA assignment"
        )


def create_lsoa_msoa_lookup_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """Create and load ``staging.lsoa_msoa_lu``, the England and Wales LSOA11 to MSOA11 lookup."""
    _ensure_production_schema(engine)
    csv_fp = inputs.resolve("lsoa_msoa_lookup_csv_fp", cfg)
    if not csv_fp.is_file():
        raise FileNotFoundError(
            f"LSOA11->MSOA11 lookup CSV not found: {csv_fp}. Fetch it with "
            f"`python -m hpldn.backend.scripts.fetch_lsoa_msoa_lookup`."
        )

    df = pd.read_csv(csv_fp, dtype=str)
    if df["msoa11cd"].isna().any():
        raise ValueError(f"missing msoa11cd in {csv_fp}")
    if not df["lsoa11cd"].is_unique:
        raise ValueError(f"lsoa11cd is not unique in {csv_fp}")

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS staging.lsoa_msoa_lu"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.lsoa_msoa_lu (
                    lsoa11cd VARCHAR PRIMARY KEY,
                    lsoa11nm VARCHAR,
                    msoa11cd VARCHAR NOT NULL,
                    msoa11nm VARCHAR
                )
                """
            )
        )
        df.to_sql(
            "lsoa_msoa_lu",
            connection,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=2000,
            method="multi",
        )
        connection.execute(
            text(
                "CREATE INDEX idx_staging_lsoa_msoa_lu_msoa11cd "
                "ON staging.lsoa_msoa_lu (msoa11cd)"
            )
        )
        row_count = connection.execute(
            text("SELECT COUNT(*) FROM staging.lsoa_msoa_lu")
        ).scalar()

        unmatched = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM staging.lsoa l
                LEFT JOIN staging.lsoa_msoa_lu m ON m.lsoa11cd = l.lsoa11cd
                WHERE l.lad22cd LIKE 'E09%' AND m.msoa11cd IS NULL
                """
            )
        ).scalar()
        london_msoa_count, staging_msoa_count = connection.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(DISTINCT m.msoa11cd)
                     FROM staging.lsoa l
                     JOIN staging.lsoa_msoa_lu m ON m.lsoa11cd = l.lsoa11cd
                     WHERE l.lad22cd LIKE 'E09%'),
                    (SELECT COUNT(DISTINCT msoa11cd) FROM staging.msoa
                     WHERE lad22cd LIKE 'E09%')
                """
            )
        ).one()

    _emit(
        log,
        f"Created staging.lsoa_msoa_lu with {row_count} LSOA11 rows "
        f"({london_msoa_count} distinct London MSOA11s)",
    )
    if unmatched:
        raise RuntimeError(
            f"{unmatched} London LSOA11s (staging.lsoa, lad22cd LIKE 'E09%') "
            f"have no msoa11cd in staging.lsoa_msoa_lu"
        )
    if london_msoa_count != staging_msoa_count:
        raise RuntimeError(
            f"staging.lsoa_msoa_lu resolves {london_msoa_count} distinct "
            f"London MSOA11s, staging.msoa has {staging_msoa_count} -- "
            f"expected an exact match"
        )



_IOD_DOMAIN_COLUMNS_2015_STYLE = {
    "Index of Multiple Deprivation (IMD) Score": "imd_score",
    "Index of Multiple Deprivation (IMD) Rank (where 1 is most deprived)": "imd_rank",
    "Income Score (rate)": "income_score",
    "Income Rank (where 1 is most deprived)": "income_rank",
    "Employment Score (rate)": "employment_score",
    "Employment Rank (where 1 is most deprived)": "employment_rank",
    "Education, Skills and Training Score": "education_score",
    "Education, Skills and Training Rank (where 1 is most deprived)": "education_rank",
    "Health Deprivation and Disability Score": "health_score",
    "Health Deprivation and Disability Rank (where 1 is most deprived)": "health_rank",
    "Crime Score": "crime_score",
    "Crime Rank (where 1 is most deprived)": "crime_rank",
    "Barriers to Housing and Services Score": "barriers_score",
    "Barriers to Housing and Services Rank (where 1 is most deprived)": "barriers_rank",
    "Living Environment Score": "living_env_score",
    "Living Environment Rank (where 1 is most deprived)": "living_env_rank",
    "Income Deprivation Affecting Children Index (IDACI) Score (rate)": "idaci_score",
    "Income Deprivation Affecting Children Index (IDACI) Rank (where 1 is most deprived)": "idaci_rank",
    "Income Deprivation Affecting Older People (IDAOPI) Score (rate)": "idaopi_score",
    "Income Deprivation Affecting Older People (IDAOPI) Rank (where 1 is most deprived)": "idaopi_rank",
    "Children and Young People Sub-domain Score": "children_score",
    "Adult Skills Sub-domain Score": "adult_skills_score",
    "Geographical Barriers Sub-domain Score": "geo_barriers_score",
    "Wider Barriers Sub-domain Score": "wider_barriers_score",
    "Indoors Sub-domain Score": "indoors_score",
    "Outdoors Sub-domain Score": "outdoors_score",
}

_IOD_COLUMN_MAP: Dict[int, Dict[str, str]] = {
    2010: {
        "LSOA CODE": "lsoa_code_raw",
        "POST 2009 LA CODE": "lad_code_raw",
        "IMD SCORE": "imd_score",
        "RANK OF IMD SCORE (where 1 is most deprived)": "imd_rank",
        "INCOME SCORE": "income_score",
        "RANK OF INCOME SCORE (where 1 is most deprived)": "income_rank",
        "EMPLOYMENT SCORE": "employment_score",
        "RANK OF EMPLOYMENT SCORE (where 1 is most deprived)": "employment_rank",
        "EDUCATION SKILLS AND TRAINING SCORE": "education_score",
        "RANK OF EDUCATION SKILLS AND TRAINING SCORE (where 1 is most deprived)": "education_rank",
        "HEALTH DEPRIVATION AND DISABILITY SCORE": "health_score",
        "RANK OF HEALTH DEPRIVATION AND DISABILITY SCORE (where 1 is most deprived)": "health_rank",
        "CRIME AND DISORDER SCORE": "crime_score",
        "RANK OF CRIME SCORE (where 1 is most deprived)": "crime_rank",
        "BARRIERS TO HOUSING AND SERVICES SCORE": "barriers_score",
        "RANK OF BARRIERS TO HOUSING AND SERVICES SCORE (where 1 is most deprived)": "barriers_rank",
        "LIVING ENVIRONMENT SCORE": "living_env_score",
        "RANK OF LIVING ENVIRONMENT SCORE (where 1 is most deprived)": "living_env_rank",
        "IDACI score": "idaci_score",
        "Rank of IDACI (where 1 is most deprived)": "idaci_rank",
        "IDAOPI score": "idaopi_score",
        "Rank of IDAOPI (where 1 is most deprived)": "idaopi_rank",
        "Children/Young People Sub-domain Score": "children_score",
        "Skills Sub-domain Score": "adult_skills_score",
        "Geographical Barriers Sub-domain Score": "geo_barriers_score",
        "Wider Barriers Sub-domain Score": "wider_barriers_score",
        "Indoors Sub-domain Score": "indoors_score",
        "Outdoors Sub-domain Score": "outdoors_score",
    },
    2015: {
        "LSOA code (2011)": "lsoa_code_raw",
        "Local Authority District code (2013)": "lad_code_raw",
        **_IOD_DOMAIN_COLUMNS_2015_STYLE,
        "Total population: mid 2012 (excluding prisoners)": "total_population",
    },
    2019: {
        "LSOA code (2011)": "lsoa_code_raw",
        "Local Authority District code (2019)": "lad_code_raw",
        **_IOD_DOMAIN_COLUMNS_2015_STYLE,
        "Total population: mid 2015 (excluding prisoners)": "total_population",
    },
    2025: {
        "LSOA code (2021)": "lsoa_code_raw",
        "Local Authority District code (2024)": "lad_code_raw",
        **_IOD_DOMAIN_COLUMNS_2015_STYLE,
        "Total population: mid 2022": "total_population",
    },
}

_IOD_EXPECTED_ROW_COUNTS = {2010: 32482, 2015: 32844, 2019: 32844, 2025: 33755}

_IOD_LSOA_RAW_DDL = """
CREATE TABLE staging.iod_lsoa_raw (
    release_year          SMALLINT NOT NULL,
    lsoa_code_raw          VARCHAR  NOT NULL,
    lad_code_raw           VARCHAR,
    imd_score              DOUBLE PRECISION,  imd_rank              INTEGER,
    income_score           DOUBLE PRECISION,  income_rank           INTEGER,
    employment_score       DOUBLE PRECISION,  employment_rank       INTEGER,
    education_score        DOUBLE PRECISION,  education_rank        INTEGER,
    health_score           DOUBLE PRECISION,  health_rank           INTEGER,
    crime_score            DOUBLE PRECISION,  crime_rank            INTEGER,
    barriers_score         DOUBLE PRECISION,  barriers_rank         INTEGER,
    living_env_score       DOUBLE PRECISION,  living_env_rank       INTEGER,
    idaci_score             DOUBLE PRECISION,  idaci_rank            INTEGER,
    idaopi_score            DOUBLE PRECISION,  idaopi_rank           INTEGER,
    children_score          DOUBLE PRECISION,
    adult_skills_score      DOUBLE PRECISION,
    geo_barriers_score      DOUBLE PRECISION,
    wider_barriers_score    DOUBLE PRECISION,
    indoors_score           DOUBLE PRECISION,
    outdoors_score          DOUBLE PRECISION,
    total_population        INTEGER,
    PRIMARY KEY (release_year, lsoa_code_raw)
)
"""


def create_iod_lsoa_raw_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create and load ``staging.iod_lsoa_raw`` from the four IoD release CSVs.

    Column names are harmonised through ``_IOD_COLUMN_MAP``; source deciles are
    not stored. BOMs and trailing header spaces are handled.
    """
    _ensure_production_schema(engine)
    data_dir = inputs.resolve("iod_csv_dir", cfg)

    frames = []
    for year, column_map in _IOD_COLUMN_MAP.items():
        csv_fp = data_dir / f"{year}.csv"
        if not csv_fp.is_file():
            raise FileNotFoundError(f"IoD {year} CSV not found: {csv_fp}")
        raw = pd.read_csv(csv_fp, encoding="utf-8-sig")
        raw.columns = [c.strip() for c in raw.columns]
        missing_headers = set(column_map) - set(raw.columns)
        if missing_headers:
            raise ValueError(
                f"IoD {year} CSV is missing expected headers: {missing_headers} "
                f"-- _IOD_COLUMN_MAP[{year}] needs updating for this download"
            )
        expected_rows = _IOD_EXPECTED_ROW_COUNTS[year]
        if len(raw) != expected_rows:
            raise ValueError(
                f"IoD {year} CSV has {len(raw)} rows, expected {expected_rows}"
            )

        df = raw[list(column_map)].rename(columns=column_map)
        df.insert(0, "release_year", year)
        if not df["lsoa_code_raw"].is_unique:
            raise ValueError(f"lsoa_code_raw is not unique in IoD {year} CSV")
        if df["imd_score"].isna().any():
            raise ValueError(f"imd_score has NULLs in IoD {year} CSV")
        for col in df.columns:
            if col not in ("release_year", "lsoa_code_raw", "lad_code_raw"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True, sort=False)

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS staging.iod_lsoa_raw"))
        connection.execute(text(_IOD_LSOA_RAW_DDL))
        combined.to_sql(
            "iod_lsoa_raw",
            connection,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=5000,
            method="multi",
        )
        counts = dict(
            connection.execute(
                text(
                    "SELECT release_year, COUNT(*) FROM staging.iod_lsoa_raw "
                    "GROUP BY release_year ORDER BY release_year"
                )
            ).all()
        )
    _emit(log, f"Created staging.iod_lsoa_raw with {len(combined)} rows: {counts}")


_IOD_RELEASES_DDL = """
CREATE TABLE staging.iod_releases (
    release_year     SMALLINT PRIMARY KEY,
    published_date   DATE NOT NULL,
    lsoa_vintage     SMALLINT NOT NULL,
    reference_year   SMALLINT,
    source_filename  TEXT NOT NULL
)
"""

_IOD_RELEASES = [
    {
        "release_year": 2010,
        "published_date": "2011-03-24",
        "lsoa_vintage": 2001,
        "reference_year": 2008,
        "source_filename": "2010.csv",
    },
    {
        "release_year": 2015,
        "published_date": "2015-09-30",
        "lsoa_vintage": 2011,
        "reference_year": 2012,
        "source_filename": "2015.csv",
    },
    {
        "release_year": 2019,
        "published_date": "2019-09-26",
        "lsoa_vintage": 2011,
        "reference_year": 2015,
        "source_filename": "2019.csv",
    },
    {
        "release_year": 2025,
        "published_date": "2025-10-30",
        "lsoa_vintage": 2021,
        "reference_year": 2021,
        "source_filename": "2025.csv",
    },
]


def create_iod_releases_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """Create and load ``staging.iod_releases``: the four releases' publication dates."""
    _ensure_production_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging.iod_releases"))
        connection.execute(text(_IOD_RELEASES_DDL))
        connection.execute(
            text(
                "INSERT INTO staging.iod_releases "
                "(release_year, published_date, lsoa_vintage, reference_year, source_filename) "
                "VALUES (:release_year, :published_date, :lsoa_vintage, :reference_year, :source_filename)"
            ),
            _IOD_RELEASES,
        )
    _emit(log, f"Created staging.iod_releases with {len(_IOD_RELEASES)} rows")



IOD_NEIGHBOUR_RADIUS_M = 1000

_IOD_LSOA_SPINE_DDL = """
CREATE UNLOGGED TABLE staging.iod_lsoa_spine (
    release_year          SMALLINT NOT NULL,
    lsoa11cd               VARCHAR  NOT NULL,
    lad_code_raw            VARCHAR,
    imd_score               DOUBLE PRECISION,  imd_rank              INTEGER,
    income_score            DOUBLE PRECISION,  income_rank           INTEGER,
    employment_score        DOUBLE PRECISION,  employment_rank       INTEGER,
    education_score         DOUBLE PRECISION,  education_rank        INTEGER,
    health_score            DOUBLE PRECISION,  health_rank           INTEGER,
    crime_score             DOUBLE PRECISION,  crime_rank            INTEGER,
    barriers_score          DOUBLE PRECISION,  barriers_rank         INTEGER,
    living_env_score        DOUBLE PRECISION,  living_env_rank       INTEGER,
    idaci_score              DOUBLE PRECISION,  idaci_rank            INTEGER,
    idaopi_score             DOUBLE PRECISION,  idaopi_rank           INTEGER,
    children_score           DOUBLE PRECISION,
    adult_skills_score       DOUBLE PRECISION,
    geo_barriers_score       DOUBLE PRECISION,
    wider_barriers_score     DOUBLE PRECISION,
    indoors_score            DOUBLE PRECISION,
    outdoors_score           DOUBLE PRECISION,
    total_population         INTEGER,
    imd_pct_london           DOUBLE PRECISION,
    income_pct_london        DOUBLE PRECISION,
    education_pct_london     DOUBLE PRECISION,
    outdoors_pct_london      DOUBLE PRECISION,
    imd_decile_national      SMALLINT,
    imd_score_vs_lad         DOUBLE PRECISION,
    imd_score_nbr_1km        DOUBLE PRECISION,
    imd_contrast_1km         DOUBLE PRECISION,
    income_contrast_1km      DOUBLE PRECISION,
    mapping_method           TEXT,
    PRIMARY KEY (release_year, lsoa11cd)
)
"""

_IOD_SPINE_INSERT_DIRECT = """
WITH matched AS (
    SELECT r.*
    FROM staging.iod_lsoa_raw r
    WHERE r.release_year = :release_year
      AND EXISTS (SELECT 1 FROM staging.lsoa l WHERE l.lsoa11cd = r.lsoa_code_raw)
),
computed AS (
    SELECT m.*,
        m.imd_score - AVG(m.imd_score) OVER (PARTITION BY m.lad_code_raw) AS imd_score_vs_lad,
        ntile(10) OVER (ORDER BY m.imd_rank) AS imd_decile_national
    FROM matched m
)
INSERT INTO staging.iod_lsoa_spine (
    release_year, lsoa11cd, lad_code_raw,
    imd_score, imd_rank, income_score, income_rank, employment_score, employment_rank,
    education_score, education_rank, health_score, health_rank, crime_score, crime_rank,
    barriers_score, barriers_rank, living_env_score, living_env_rank,
    idaci_score, idaci_rank, idaopi_score, idaopi_rank,
    children_score, adult_skills_score, geo_barriers_score, wider_barriers_score,
    indoors_score, outdoors_score, total_population, mapping_method,
    imd_score_vs_lad, imd_decile_national
)
SELECT c.release_year, c.lsoa_code_raw, c.lad_code_raw,
    c.imd_score, c.imd_rank, c.income_score, c.income_rank, c.employment_score, c.employment_rank,
    c.education_score, c.education_rank, c.health_score, c.health_rank, c.crime_score, c.crime_rank,
    c.barriers_score, c.barriers_rank, c.living_env_score, c.living_env_rank,
    c.idaci_score, c.idaci_rank, c.idaopi_score, c.idaopi_rank,
    c.children_score, c.adult_skills_score, c.geo_barriers_score, c.wider_barriers_score,
    c.indoors_score, c.outdoors_score, c.total_population, 'direct',
    c.imd_score_vs_lad, c.imd_decile_national
FROM computed c
"""

_IOD_SPINE_INSERT_2025_BESTFIT = """
WITH computed AS (
    SELECT r.*,
        r.imd_score - AVG(r.imd_score) OVER (PARTITION BY r.lad_code_raw) AS imd_score_vs_lad,
        ntile(10) OVER (ORDER BY r.imd_rank) AS imd_decile_national
    FROM staging.iod_lsoa_raw r
    WHERE r.release_year = 2025
)
INSERT INTO staging.iod_lsoa_spine (
    release_year, lsoa11cd, lad_code_raw,
    imd_score, imd_rank, income_score, income_rank, employment_score, employment_rank,
    education_score, education_rank, health_score, health_rank, crime_score, crime_rank,
    barriers_score, barriers_rank, living_env_score, living_env_rank,
    idaci_score, idaci_rank, idaopi_score, idaopi_rank,
    children_score, adult_skills_score, geo_barriers_score, wider_barriers_score,
    indoors_score, outdoors_score, total_population, mapping_method,
    imd_score_vs_lad, imd_decile_national
)
SELECT c.release_year, l.lsoa11cd, c.lad_code_raw,
    c.imd_score, c.imd_rank, c.income_score, c.income_rank, c.employment_score, c.employment_rank,
    c.education_score, c.education_rank, c.health_score, c.health_rank, c.crime_score, c.crime_rank,
    c.barriers_score, c.barriers_rank, c.living_env_score, c.living_env_rank,
    c.idaci_score, c.idaci_rank, c.idaopi_score, c.idaopi_rank,
    c.children_score, c.adult_skills_score, c.geo_barriers_score, c.wider_barriers_score,
    c.indoors_score, c.outdoors_score, c.total_population, 'lsoa21_bestfit',
    c.imd_score_vs_lad, c.imd_decile_national
FROM computed c
JOIN staging.lsoa l ON l.lsoa21cd = c.lsoa_code_raw
"""

_IOD_SPINE_UPDATE_PCT_LONDON = """
WITH ranks AS (
    SELECT s.release_year, s.lsoa11cd,
        percent_rank() OVER (PARTITION BY s.release_year ORDER BY s.imd_score) AS imd_pct_london,
        percent_rank() OVER (PARTITION BY s.release_year ORDER BY s.income_score) AS income_pct_london,
        percent_rank() OVER (PARTITION BY s.release_year ORDER BY s.education_score) AS education_pct_london,
        percent_rank() OVER (PARTITION BY s.release_year ORDER BY s.outdoors_score) AS outdoors_pct_london
    FROM staging.iod_lsoa_spine s
    JOIN staging.lsoa l ON l.lsoa11cd = s.lsoa11cd
    WHERE l.lad22cd LIKE 'E09%'
)
UPDATE staging.iod_lsoa_spine s
SET imd_pct_london = r.imd_pct_london,
    income_pct_london = r.income_pct_london,
    education_pct_london = r.education_pct_london,
    outdoors_pct_london = r.outdoors_pct_london
FROM ranks r
WHERE r.release_year = s.release_year AND r.lsoa11cd = s.lsoa11cd
"""

_IOD_SPINE_UPDATE_NBR_CONTRAST = """
WITH nbr AS (
    SELECT s.release_year, s.lsoa11cd,
        SUM(s2.imd_score * COALESCE(s2.total_population, 1))
            / NULLIF(SUM(COALESCE(s2.total_population, 1)), 0) AS imd_nbr,
        SUM(s2.income_score * COALESCE(s2.total_population, 1))
            / NULLIF(SUM(COALESCE(s2.total_population, 1)), 0) AS income_nbr
    FROM staging.iod_lsoa_spine s
    JOIN _iod_lsoa_centroids c1 ON c1.lsoa11cd = s.lsoa11cd
    JOIN _iod_lsoa_centroids c2
        ON c2.lsoa11cd != s.lsoa11cd
       AND ST_DWithin(c2.centroid::geography, c1.centroid::geography, :radius_m)
    JOIN staging.iod_lsoa_spine s2
        ON s2.release_year = s.release_year AND s2.lsoa11cd = c2.lsoa11cd
    GROUP BY s.release_year, s.lsoa11cd
)
UPDATE staging.iod_lsoa_spine s
SET imd_score_nbr_1km = nbr.imd_nbr,
    imd_contrast_1km = s.imd_score - nbr.imd_nbr,
    income_contrast_1km = s.income_score - nbr.income_nbr
FROM nbr
WHERE nbr.release_year = s.release_year AND nbr.lsoa11cd = s.lsoa11cd
"""


def create_iod_lsoa_spine_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    neighbour_radius_m: int = IOD_NEIGHBOUR_RADIUS_M,
) -> None:
    """
    Create ``staging.iod_lsoa_spine``: every IoD release harmonised onto LSOA 2011.

    Then adds London-relative percentiles and a population-weighted neighbour
    contrast within `neighbour_radius_m`. Both use only IoD and geometry.
    Built UNLOGGED and set LOGGED at the end.
    """
    _ensure_production_schema(engine)
    with _timed(log, "create_iod_lsoa_spine_table"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(text("DROP TABLE IF EXISTS staging.iod_lsoa_spine"))
        connection.execute(text(_IOD_LSOA_SPINE_DDL))

        with _timed(log, "iod_lsoa_spine direct-mapped releases (2010/2015/2019)"):
            for release_year in (2010, 2015, 2019):
                connection.execute(
                    text(_IOD_SPINE_INSERT_DIRECT), {"release_year": release_year}
                )
        with _timed(log, "iod_lsoa_spine 2025 lsoa21->lsoa11 best-fit"):
            connection.execute(text(_IOD_SPINE_INSERT_2025_BESTFIT))

        _analyze(connection, ("staging.iod_lsoa_spine",), log)

        with _timed(log, "iod_lsoa_spine pct_london"):
            connection.execute(text(_IOD_SPINE_UPDATE_PCT_LONDON))

        connection.execute(
            text(
                """
                CREATE TEMP TABLE _iod_lsoa_centroids AS
                SELECT lsoa11cd, ST_Centroid(geom) AS centroid
                FROM staging.lsoa_boundaries
                """
            )
        )
        connection.execute(
            text("CREATE INDEX ON _iod_lsoa_centroids USING gist(centroid)")
        )
        connection.execute(text("ANALYZE _iod_lsoa_centroids"))
        with _timed(log, "iod_lsoa_spine neighbour contrast"):
            connection.execute(
                text(_IOD_SPINE_UPDATE_NBR_CONTRAST), {"radius_m": neighbour_radius_m}
            )
        connection.execute(text("DROP TABLE _iod_lsoa_centroids"))

        connection.execute(text("ALTER TABLE staging.iod_lsoa_spine SET LOGGED"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_iod_lsoa_spine_release_lsoa "
                "ON staging.iod_lsoa_spine (release_year, lsoa11cd)"
            )
        )
        _analyze(connection, ("staging.iod_lsoa_spine",), log)

        counts = dict(
            connection.execute(
                text(
                    "SELECT release_year, COUNT(*) FROM staging.iod_lsoa_spine "
                    "GROUP BY release_year ORDER BY release_year"
                )
            ).all()
        )
        pct_out_of_range = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM staging.iod_lsoa_spine
                WHERE imd_pct_london < 0 OR imd_pct_london > 1
                   OR income_pct_london < 0 OR income_pct_london > 1
                   OR education_pct_london < 0 OR education_pct_london > 1
                   OR outdoors_pct_london < 0 OR outdoors_pct_london > 1
                """
            )
        ).scalar()

    _emit(log, f"Created staging.iod_lsoa_spine with rows per release: {counts}")
    if pct_out_of_range != 0:
        raise RuntimeError(
            f"{pct_out_of_range} staging.iod_lsoa_spine rows have a "
            f"*_pct_london value outside [0,1] -- rule L6 violation"
        )



IOD_PUBLICATION_EMBARGO_MONTHS = 3

_IOD_FEATURES_DDL = """
CREATE TABLE production.iod_features (
    ppd_id                      BIGINT,
    iod_release_year            SMALLINT,
    iod_release_age_years       DOUBLE PRECISION,
    iod_imd_score               DOUBLE PRECISION,
    iod_imd_pct_london          DOUBLE PRECISION,
    iod_imd_decile_national     SMALLINT,
    iod_income_score            DOUBLE PRECISION,
    iod_income_pct_london       DOUBLE PRECISION,
    iod_employment_score        DOUBLE PRECISION,
    iod_education_score         DOUBLE PRECISION,
    iod_education_pct_london    DOUBLE PRECISION,
    iod_health_score             DOUBLE PRECISION,
    iod_crime_score              DOUBLE PRECISION,
    iod_barriers_score           DOUBLE PRECISION,
    iod_wider_barriers_score     DOUBLE PRECISION,
    iod_geo_barriers_score       DOUBLE PRECISION,
    iod_living_env_score         DOUBLE PRECISION,
    iod_indoors_score            DOUBLE PRECISION,
    iod_outdoors_score           DOUBLE PRECISION,
    iod_outdoors_pct_london      DOUBLE PRECISION,
    iod_idaci_score               DOUBLE PRECISION,
    iod_idaopi_score              DOUBLE PRECISION,
    iod_children_score            DOUBLE PRECISION,
    iod_adult_skills_score        DOUBLE PRECISION,
    iod_imd_score_vs_lad          DOUBLE PRECISION,
    iod_imd_contrast_1km          DOUBLE PRECISION,
    iod_income_contrast_1km       DOUBLE PRECISION,
    iod_prev_release_year         SMALLINT,
    iod_imd_change                DOUBLE PRECISION,
    iod_income_change             DOUBLE PRECISION,
    iod_education_change          DOUBLE PRECISION,
    iod_living_env_change         DOUBLE PRECISION,
    iod_imd_pct_london_change     DOUBLE PRECISION,
    iod_years_between_releases    DOUBLE PRECISION,
    iod_backfilled                BOOLEAN
)
"""

_IOD_FEATURES_INSERT = """
INSERT INTO production.iod_features (
    ppd_id, iod_release_year, iod_release_age_years,
    iod_imd_score, iod_imd_pct_london, iod_imd_decile_national,
    iod_income_score, iod_income_pct_london,
    iod_employment_score,
    iod_education_score, iod_education_pct_london,
    iod_health_score,
    iod_crime_score,
    iod_barriers_score,
    iod_wider_barriers_score,
    iod_geo_barriers_score,
    iod_living_env_score,
    iod_indoors_score,
    iod_outdoors_score, iod_outdoors_pct_london,
    iod_idaci_score,
    iod_idaopi_score,
    iod_children_score,
    iod_adult_skills_score,
    iod_imd_score_vs_lad,
    iod_imd_contrast_1km,
    iod_income_contrast_1km,
    iod_prev_release_year,
    iod_imd_change,
    iod_income_change,
    iod_education_change,
    iod_living_env_change,
    iod_imd_pct_london_change,
    iod_years_between_releases,
    iod_backfilled
)
SELECT
    p.id,
    cur.release_year,
    (p.dateoftransfer - cur.published_date) / 365.25,
    cur.imd_score, cur.imd_pct_london, cur.imd_decile_national,
    cur.income_score, cur.income_pct_london,
    cur.employment_score,
    cur.education_score, cur.education_pct_london,
    cur.health_score,
    cur.crime_score,
    cur.barriers_score,
    cur.wider_barriers_score,
    cur.geo_barriers_score,
    cur.living_env_score,
    cur.indoors_score,
    cur.outdoors_score, cur.outdoors_pct_london,
    cur.idaci_score,
    cur.idaopi_score,
    cur.children_score,
    cur.adult_skills_score,
    cur.imd_score_vs_lad,
    cur.imd_contrast_1km,
    cur.income_contrast_1km,
    prv.release_year,
    cur.imd_score - prv.imd_score,
    cur.income_score - prv.income_score,
    cur.education_score - prv.education_score,
    cur.living_env_score - prv.living_env_score,
    cur.imd_pct_london - prv.imd_pct_london,
    (cur.published_date - prv.published_date) / 365.25,
    NOT cur.in_force
FROM production.ppd_table p
JOIN staging.property_lsoa pl ON pl.ppd_id = p.id
LEFT JOIN LATERAL (
    SELECT r.release_year, r.published_date,
           r.published_date <= p.dateoftransfer - make_interval(months => :embargo_months) AS in_force,
           s.imd_score, s.imd_pct_london, s.imd_decile_national,
           s.income_score, s.income_pct_london,
           s.employment_score,
           s.education_score, s.education_pct_london,
           s.health_score,
           s.crime_score,
           s.barriers_score, s.wider_barriers_score, s.geo_barriers_score,
           s.living_env_score,
           s.indoors_score,
           s.outdoors_score, s.outdoors_pct_london,
           s.idaci_score,
           s.idaopi_score,
           s.children_score,
           s.adult_skills_score,
           s.imd_score_vs_lad,
           s.imd_contrast_1km,
           s.income_contrast_1km
    FROM staging.iod_releases r
    JOIN staging.iod_lsoa_spine s
      ON s.release_year = r.release_year AND s.lsoa11cd = pl.lsoa11cd
    WHERE pl.lsoa11cd IS NOT NULL
    -- The latest release in force at the sale; before the first one, the
    -- earliest release (otherwise every pre-2011 sale is NULL).
    ORDER BY (r.published_date <= p.dateoftransfer - make_interval(months => :embargo_months)) DESC,
             CASE WHEN r.published_date <= p.dateoftransfer - make_interval(months => :embargo_months)
                  THEN r.published_date END DESC NULLS LAST,
             r.published_date ASC
    LIMIT 1
) cur ON true
LEFT JOIN LATERAL (
    SELECT r.release_year, r.published_date,
           s.imd_score, s.income_score, s.education_score, s.living_env_score,
           s.imd_pct_london
    FROM staging.iod_releases r
    JOIN staging.iod_lsoa_spine s
      ON s.release_year = r.release_year AND s.lsoa11cd = pl.lsoa11cd
    WHERE pl.lsoa11cd IS NOT NULL
      AND r.published_date <= p.dateoftransfer - make_interval(months => :embargo_months)
      AND r.published_date < cur.published_date
    ORDER BY r.published_date DESC
    LIMIT 1
) prv ON true
"""


def create_iod_features_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    embargo_months: int = IOD_PUBLICATION_EMBARGO_MONTHS,
) -> None:
    """
    Create ``production.iod_features``: IoD scores as of each sale.

    A sale sees only the release with the latest ``published_date`` at least
    `embargo_months` before its completion date. A sale before any release
    was in force takes the earliest release instead, marked
    ``iod_backfilled`` (a negative ``iod_release_age_years``): deprivation
    moves slowly, and the alternative left every pre-2011 sale without it.
    ``iod_*_change`` compares with the previous published release and is
    NULL without one. No column depends on price, and the table reads
    ``production.ppd_table`` directly.
    """
    _ensure_production_schema(engine)
    with _timed(log, "create_iod_features_table"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text("DROP TABLE IF EXISTS production.iod_features CASCADE")
        )
        connection.execute(text(_IOD_FEATURES_DDL))
        with _timed(log, "iod_features as-of join"):
            connection.execute(
                text(_IOD_FEATURES_INSERT), {"embargo_months": embargo_months}
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_iod_features_ppd_id "
                "ON production.iod_features (ppd_id)"
            )
        )
        _analyze(connection, ("production.iod_features",), log)

        row_count, with_imd = connection.execute(
            text("SELECT COUNT(*), COUNT(iod_imd_score) FROM production.iod_features")
        ).one()
        release_year_counts = dict(
            connection.execute(
                text(
                    "SELECT iod_release_year, COUNT(*) FROM production.iod_features "
                    "GROUP BY iod_release_year ORDER BY iod_release_year NULLS FIRST"
                )
            ).all()
        )

        embargo_violations = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM production.iod_features f
                JOIN production.ppd_table p ON p.id = f.ppd_id
                JOIN staging.iod_releases r ON r.release_year = f.iod_release_year
                WHERE r.published_date > p.dateoftransfer - make_interval(months => :embargo_months)
                  AND NOT f.iod_backfilled
                """
            ),
            {"embargo_months": embargo_months},
        ).scalar()
        # A back-filled row must carry the earliest release of its LSOA.
        backfill_violations = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM production.iod_features f
                JOIN staging.property_lsoa pl ON pl.ppd_id = f.ppd_id
                JOIN staging.iod_releases r ON r.release_year = f.iod_release_year
                WHERE f.iod_backfilled
                  AND EXISTS (
                      SELECT 1 FROM staging.iod_releases r2
                      JOIN staging.iod_lsoa_spine s2
                        ON s2.release_year = r2.release_year AND s2.lsoa11cd = pl.lsoa11cd
                      WHERE r2.published_date < r.published_date
                  )
                """
            )
        ).scalar()
        backfilled = connection.execute(
            text("SELECT COUNT(*) FROM production.iod_features WHERE iod_backfilled")
        ).scalar()
        backwards_deltas = connection.execute(
            text(
                "SELECT COUNT(*) FROM production.iod_features "
                "WHERE iod_prev_release_year >= iod_release_year"
            )
        ).scalar()
        non_positive_age = connection.execute(
            text(
                "SELECT COUNT(*) FROM production.iod_features "
                "WHERE iod_release_age_years IS NOT NULL AND iod_release_age_years <= 0 "
                "AND NOT iod_backfilled"
            )
        ).scalar()

    with_imd_pct = with_imd / row_count if row_count else 0.0
    _emit(
        log,
        f"Created production.iod_features with {row_count} rows "
        f"({with_imd} with a level, {with_imd_pct:.1%}; {backfilled} before any "
        f"release was in force, given the earliest); rows by "
        f"iod_release_year: {release_year_counts}",
    )
    if backfill_violations != 0:
        raise RuntimeError(
            f"{backfill_violations} back-filled production.iod_features rows do not "
            "carry their LSOA's earliest release"
        )
    if embargo_violations != 0:
        raise RuntimeError(
            f"{embargo_violations} production.iod_features rows see a release "
            f"published within {embargo_months} months of dateoftransfer -- "
            f"rule L1/L1a violation"
        )
    if backwards_deltas != 0:
        raise RuntimeError(
            f"{backwards_deltas} production.iod_features rows have "
            f"iod_prev_release_year >= iod_release_year -- rule L3 violation"
        )
    if non_positive_age != 0:
        raise RuntimeError(
            f"{non_positive_age} production.iod_features rows have "
            f"iod_release_age_years <= 0 despite the embargo -- rule L1a "
            f"violation"
        )



PPD_EPC_FUSION_WINDOW_START = date(2024, 11, 1)
PPD_EPC_FUSION_WINDOW_END = date(2026, 7, 31)

PPD_EPC_BASELINE_LINK_RATE = 54_137 / 126_287

PPD_EPC_GEO_THRESHOLD_M = 500

PPD_EPC_VIEW_COVERAGE_TOLERANCE = 0.05

EPC_DOMESTIC_HISTORY_YEAR_START = 2008
EPC_DOMESTIC_HISTORY_YEAR_END = 2026


_EPC_DOMESTIC_COLUMNS_SQL = """(
    certificate_number                      varchar,
    address1                                varchar,
    address2                                varchar,
    address3                                varchar,
    postcode                                varchar,
    posttown                                varchar,
    address                                 varchar,
    constituency                            varchar,
    constituency_label                      varchar,
    local_authority                         varchar,
    local_authority_label                   varchar,
    built_form                              varchar,
    co2_emiss_curr_per_floor_area           numeric,
    co2_emissions_current                   double precision,
    co2_emissions_potential                 double precision,
    construction_age_band                   varchar,
    current_energy_efficiency               numeric,
    current_energy_rating                   varchar,
    energy_consumption_current              numeric,
    energy_consumption_potential            numeric,
    energy_tariff                           varchar,
    environment_impact_current              numeric,
    environment_impact_potential            numeric,
    extension_count                         numeric,
    fixed_lighting_outlets_count            numeric,
    flat_storey_count                       numeric,
    flat_top_storey                         varchar,
    floor_description                       varchar,
    floor_energy_eff                        varchar,
    floor_height                            double precision,
    floor_level                             varchar,
    glazed_area                             varchar,
    glazed_type                             varchar,
    heat_loss_corridor                      varchar,
    heating_cost_current                    numeric,
    heating_cost_potential                  numeric,
    hot_water_cost_current                  numeric,
    hot_water_cost_potential                numeric,
    hot_water_energy_eff                    varchar,
    hot_water_env_eff                       varchar,
    hotwater_description                    varchar,
    inspection_date                         date,
    lighting_cost_current                   numeric,
    lighting_cost_potential                 numeric,
    lighting_description                    varchar,
    lighting_energy_eff                     varchar,
    lighting_env_eff                        varchar,
    lodgement_date                          date,
    lodgement_datetime                      timestamp without time zone,
    low_energy_lighting                     numeric,
    low_energy_fixed_lighting_outlets_count numeric,
    main_fuel                               varchar,
    mainheat_description                    varchar,
    mainheat_energy_eff                     varchar,
    mainheat_env_eff                        varchar,
    mainheatc_energy_eff                    varchar,
    mainheatc_env_eff                       varchar,
    mainheatcont_description                varchar,
    main_heating_controls                   varchar,
    mains_gas_flag                          varchar,
    mechanical_ventilation                  varchar,
    multi_glaze_proportion                  numeric,
    number_habitable_rooms                  numeric,
    number_heated_rooms                     numeric,
    number_open_fireplaces                  numeric,
    photo_supply                            double precision,
    potential_energy_efficiency             numeric,
    potential_energy_rating                 varchar,
    property_type                           varchar,
    report_type                             varchar,
    roof_description                        varchar,
    roof_energy_eff                         varchar,
    roof_env_eff                            varchar,
    secondheat_description                  varchar,
    sheating_energy_eff                     varchar,
    sheating_env_eff                        varchar,
    solar_water_heating_flag                varchar,
    tenure                                  varchar,
    total_floor_area                        double precision,
    transaction_type                        varchar,
    unheated_corridor_length                double precision,
    walls_description                       varchar,
    walls_energy_eff                        varchar,
    walls_env_eff                           varchar,
    wind_turbine_count                      numeric,
    windows_description                     varchar,
    windows_energy_eff                      varchar,
    windows_env_eff                         varchar,
    floor_env_eff                           varchar,
    region                                  varchar,
    country                                 varchar,
    uprn                                    bigint,
    uprn_source                             varchar
)"""

_EPC_DOMESTIC_HISTORY_DDL = (
    "CREATE TABLE staging.epc_domestic_history " + _EPC_DOMESTIC_COLUMNS_SQL
)

_EPC_EMISSIONS_JSON_COLUMNS = (
    "co2_emiss_curr_per_floor_area",
    "co2_emissions_current",
    "co2_emissions_potential",
)
_EPC_DOMESTIC_SCRATCH_COLUMNS_SQL = (
    _EPC_DOMESTIC_COLUMNS_SQL.replace(
        "co2_emiss_curr_per_floor_area           numeric,",
        "co2_emiss_curr_per_floor_area           varchar,",
    )
    .replace(
        "co2_emissions_current                   double precision,",
        "co2_emissions_current                   varchar,",
    )
    .replace(
        "co2_emissions_potential                 double precision,",
        "co2_emissions_potential                 varchar,",
    )
)
if _EPC_DOMESTIC_SCRATCH_COLUMNS_SQL == _EPC_DOMESTIC_COLUMNS_SQL:
    raise AssertionError(
        "_EPC_EMISSIONS_JSON_COLUMNS override matched nothing -- "
        "_EPC_DOMESTIC_COLUMNS_SQL must have changed shape"
    )

_EPC_DOMESTIC_HISTORY_SCRATCH_DDL = (
    "CREATE UNLOGGED TABLE staging._epc_domestic_history_scratch "
    + _EPC_DOMESTIC_SCRATCH_COLUMNS_SQL
)

_EPC_DOMESTIC_COLUMN_NAMES = re.findall(
    r"^\s*([a-z][a-z0-9_]*)\s+\S", _EPC_DOMESTIC_COLUMNS_SQL, re.MULTILINE
)


def _epc_emiss_numeric_sql(column: str, cast: str) -> str:
    """Return SQL extracting a numeric value from `column`, whether bare or a JSON blob."""
    return (
        f"NULLIF(CASE WHEN {column} ~ '^\\{{' "
        f'THEN substring({column} from \'"value":\\s*(-?[0-9.]+)\') '
        f"ELSE {column} END, '')::{cast} AS {column}"
    )


def _epc_domestic_history_select_sql() -> str:
    """Return the scratch-to-history SELECT list, casting the three JSON-quirk columns."""
    columns = []
    for name in _EPC_DOMESTIC_COLUMN_NAMES:
        if name == "co2_emiss_curr_per_floor_area":
            columns.append(_epc_emiss_numeric_sql(name, "numeric"))
        elif name in _EPC_EMISSIONS_JSON_COLUMNS:
            columns.append(_epc_emiss_numeric_sql(name, "double precision"))
        else:
            columns.append(name)
    return ",\n    ".join(columns)


def populate_epc_domestic_history(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    year_start: int = EPC_DOMESTIC_HISTORY_YEAR_START,
    year_end: int = EPC_DOMESTIC_HISTORY_YEAR_END,
) -> None:
    r"""
    Build ``staging.epc_domestic_history``, London's 2008-2026 EPC certificates.

    Per year, ``\copy`` the national certificates CSV into an UNLOGGED scratch
    table, insert the London rows (region E12000007) and truncate, so peak
    extra disk is one year. Files come from ``cfg["data"]["epc_domestic_dir"]``.
    """
    epc_dir = inputs.resolve("epc_domestic_dir", cfg)

    db_url = get_psql_url(engine)
    env = get_psql_env(engine, get_bulk_copy_env(log))

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging.epc_domestic_history"))
        connection.execute(text(_EPC_DOMESTIC_HISTORY_DDL))
        connection.execute(
            text("DROP TABLE IF EXISTS staging._epc_domestic_history_scratch")
        )
        connection.execute(text(_EPC_DOMESTIC_HISTORY_SCRATCH_DDL))

    total = 0
    for year in range(year_start, year_end + 1):
        csv_fp = epc_dir / f"certificates-{year}.csv"
        if not csv_fp.is_file():
            raise FileNotFoundError(f"EPC certificates CSV not found: {csv_fp}")
        cmd = [
            "psql",
            db_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "\\copy staging._epc_domestic_history_scratch FROM "
            f"'{csv_fp}' WITH (FORMAT CSV, HEADER TRUE)",
        ]
        with _timed(log, f"load {csv_fp.name} into scratch"):
            subprocess.run(cmd, check=True, env=env)

        with engine.begin() as connection:
            inserted = connection.execute(
                text(
                    "INSERT INTO staging.epc_domestic_history SELECT\n    "
                    + _epc_domestic_history_select_sql()
                    + "\nFROM staging._epc_domestic_history_scratch "
                    "WHERE region = 'E12000007'"
                )
            ).rowcount
            connection.execute(
                text("TRUNCATE TABLE staging._epc_domestic_history_scratch")
            )
        total += inserted
        _emit(log, f"populate_epc_domestic_history: {year} contributed {inserted} London EPC certificate rows")

    with engine.begin() as connection:
        connection.execute(
            text("DROP TABLE IF EXISTS staging._epc_domestic_history_scratch")
        )
        _analyze(connection, ("staging.epc_domestic_history",), log)

    _emit(
        log,
        f"populate_epc_domestic_history: staging.epc_domestic_history has {total} London certificate rows "
        f"({year_start}-{year_end})",
    )


_HPLDN_ADDRKEY_FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION staging.hpldn_pcnorm(pc text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT upper(replace(coalesce(pc, ''), ' ', ''))
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_addrkey(raw text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    -- 1. upper(), collapse every non-[A-Z0-9] run to one space, trim.
    -- 2. Collapse the flat vocabulary to one token: F.
    -- 3. Expand street abbreviations; drop the bare "NO" (as in "NO 12").
    -- array_to_string skips NULL elements (no third argument given), which
    -- is what makes the "NO" -> NULL case act as a drop rather than a token.
    SELECT trim(array_to_string(array(
        SELECT CASE tok
            WHEN 'NO'        THEN NULL
            WHEN 'FLAT'      THEN 'F'
            WHEN 'APARTMENT' THEN 'F'
            WHEN 'APT'       THEN 'F'
            WHEN 'FLT'       THEN 'F'
            WHEN 'UNIT'      THEN 'F'
            WHEN 'RD'        THEN 'ROAD'
            WHEN 'ST'        THEN 'STREET'
            WHEN 'AVE'       THEN 'AVENUE'
            WHEN 'AV'        THEN 'AVENUE'
            WHEN 'GDNS'      THEN 'GARDENS'
            WHEN 'CT'        THEN 'COURT'
            WHEN 'DR'        THEN 'DRIVE'
            WHEN 'PL'        THEN 'PLACE'
            WHEN 'SQ'        THEN 'SQUARE'
            WHEN 'TER'       THEN 'TERRACE'
            WHEN 'TERR'      THEN 'TERRACE'
            WHEN 'LN'        THEN 'LANE'
            WHEN 'CL'        THEN 'CLOSE'
            WHEN 'PK'        THEN 'PARK'
            WHEN 'GRN'       THEN 'GREEN'
            WHEN 'HSE'       THEN 'HOUSE'
            WHEN 'CRES'      THEN 'CRESCENT'
            WHEN 'GR'        THEN 'GROVE'
            ELSE tok
        END
        FROM unnest(regexp_split_to_array(
            trim(regexp_replace(upper(coalesce(raw, '')), '[^A-Z0-9]+', ' ', 'g')),
            ' '
        )) AS tok
        WHERE tok <> ''
    ), ' '))
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_tokenset(addrkey text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    -- Order-insensitive token set: distinct tokens of an hpldn_addrkey
    -- result, sorted -- Tier 2's join key.
    SELECT array_to_string(array(
        SELECT DISTINCT t
        FROM unnest(string_to_array(coalesce(addrkey, ''), ' ')) AS t
        WHERE t <> ''
        ORDER BY t
    ), ' ')
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_numsig(addrkey text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    -- Numeric signature: the digit-only tokens of an hpldn_addrkey result,
    -- sorted -- Tier 3's join key.
    SELECT array_to_string(array(
        SELECT t
        FROM unnest(string_to_array(coalesce(addrkey, ''), ' ')) AS t
        WHERE t ~ '^[0-9]+$'
        ORDER BY t
    ), ' ')
$$;
"""


def create_epc_addrkey_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Create ``staging.epc_addrkey``, the address gazetteer for PPD-to-UPRN matching.

    One row per London-UPRN certificate with the four tier keys precomputed by
    the ``staging.hpldn_*`` functions. Not deduplicated per UPRN; the tiers'
    uniqueness guards reject ambiguous keys.
    """
    with _timed(log, "create_epc_addrkey_table"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(_HPLDN_ADDRKEY_FUNCTIONS_SQL))
        connection.execute(text("DROP TABLE IF EXISTS staging.epc_addrkey"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.epc_addrkey AS
                SELECT DISTINCT
                    staging.hpldn_pcnorm(e.postcode) AS postcode_norm,
                    staging.hpldn_addrkey(
                        concat_ws(' ', e.address1, e.address2, e.address3)
                    ) AS k_exact,
                    staging.hpldn_tokenset(staging.hpldn_addrkey(
                        concat_ws(' ', e.address1, e.address2, e.address3)
                    )) AS k_tokenset,
                    staging.hpldn_numsig(staging.hpldn_addrkey(
                        concat_ws(' ', e.address1, e.address2, e.address3)
                    )) AS k_numsig,
                    e.uprn
                FROM staging.epc_domestic_history e
                WHERE e.region = 'E12000007' AND e.uprn IS NOT NULL
                """
            )
        )
        connection.execute(
            text("CREATE INDEX ON staging.epc_addrkey (postcode_norm, k_exact)")
        )
        connection.execute(
            text("CREATE INDEX ON staging.epc_addrkey (postcode_norm, k_tokenset)")
        )
        connection.execute(
            text("CREATE INDEX ON staging.epc_addrkey (postcode_norm, k_numsig)")
        )
        _analyze(connection, ("staging.epc_addrkey",), log)

        row_count, uprn_count = connection.execute(
            text("SELECT count(*), count(DISTINCT uprn) FROM staging.epc_addrkey")
        ).one()
    _emit(
        log,
        f"create_epc_addrkey_table: staging.epc_addrkey has {row_count} rows over {uprn_count} "
        f"distinct UPRNs",
    )


_CODEPOINT_OPEN_DDL = """
CREATE TABLE staging.codepoint_open (
    postcode             varchar,
    positional_quality   smallint,
    eastings             integer,
    northings            integer,
    country_code         varchar,
    nhs_regional_ha_code varchar,
    nhs_ha_code          varchar,
    admin_county_code    varchar,
    admin_district_code  varchar,
    admin_ward_code      varchar
)
"""

_CODEPOINT_OPEN_COLUMNS = (
    "postcode",
    "positional_quality",
    "eastings",
    "northings",
    "country_code",
    "nhs_regional_ha_code",
    "nhs_ha_code",
    "admin_county_code",
    "admin_district_code",
    "admin_ward_code",
)


def populate_codepoint_open_table(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Bulk-load ``staging.codepoint_open``, a centroid per postcode.

    Reads every CSV under ``cfg["data"]["codepoint_open_dir"]``.
    """
    data_dir = inputs.resolve("codepoint_open_dir", cfg)
    if not data_dir.is_dir():
        raise FileNotFoundError(f"OS Code-Point Open directory not found: {data_dir}")
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under {data_dir}")

    db_url = get_psql_url(engine)
    env = get_psql_env(engine, get_bulk_copy_env(log))
    columns_sql = ",".join(_CODEPOINT_OPEN_COLUMNS)

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging.codepoint_open"))
        connection.execute(text(_CODEPOINT_OPEN_DDL))

    for csv_fp in csv_files:
        cmd = [
            "psql",
            db_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"\\copy staging.codepoint_open ({columns_sql}) FROM '{csv_fp}' "
            "WITH (FORMAT CSV, HEADER FALSE)",
        ]
        with _timed(log, f"load {csv_fp.name} into staging.codepoint_open"):
            subprocess.run(cmd, check=True, env=env)

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX idx_codepoint_open_pcnorm ON staging.codepoint_open "
                "(staging.hpldn_pcnorm(postcode))"
            )
        )
        row_count = connection.execute(
            text("SELECT count(*) FROM staging.codepoint_open")
        ).scalar()
        _analyze(connection, ("staging.codepoint_open",), log)
    _emit(log, f"populate_codepoint_open_table: staging.codepoint_open loaded with {row_count} postcodes")


_PPD_UPRN_LINKAGE_TABLE = "staging.ppd_uprn_linkage"

# Pre-cut-over back-fill (migration 0017). A table of its own, because
# verify_epc_ppd_fusion_v2 requires every row of the main linkage to sit inside
# the fusion window.
PPD_UPRN_LINKAGE_BACKFILL_TABLE = "staging.ppd_uprn_linkage_backfill"

# The 1995-2023 Price Paid history (migration 0025): the sales 0005 did not
# load, because ukhpucl holds only sales linked to a certificate and 0005 kept
# only those whose certificate passed its field checks (a numeric paon among
# them, which drops every flat in a named building).
PPD_UPRN_LINKAGE_HISTORY_TABLE = "staging.ppd_uprn_linkage_history"
PPD_HISTORY_WINDOW_START = date(1995, 1, 1)
PPD_HISTORY_WINDOW_END = date(2023, 12, 31)

_PPD_UPRN_LINKAGE_DDL = """
CREATE TABLE {linkage_table} (
    transactionid   varchar PRIMARY KEY,
    uprn            bigint NOT NULL,
    address_id      bigint,
    tier            smallint NOT NULL,
    dateoftransfer  date,
    price           bigint,
    propertytype    varchar,
    duration        varchar
)
"""

_TIER_JOIN_PREDICATES = {
    1: "e.postcode_norm = t.postcode_norm AND e.k_exact = t.k_exact AND t.k_exact <> ''",
    2: (
        "e.postcode_norm = t.postcode_norm AND e.k_tokenset = t.k_tokenset "
        "AND t.k_tokenset <> ''"
    ),
    3: (
        "e.postcode_norm = t.postcode_norm AND e.k_numsig = t.k_numsig "
        "AND t.k_numsig <> ''"
    ),
    4: "e.postcode_norm = t.postcode_norm",
}

_TIER_APPLIES_UPRN_GUARD = {1: False, 2: False, 3: True, 4: True}


def _ppd_uprn_linkage_tier_sql(
    tier: int,
    target_table: str = "staging._ppd_target",
    linkage_table: str = _PPD_UPRN_LINKAGE_TABLE,
) -> str:
    """
    Return one tier's INSERT into `linkage_table`.

    Groups not-yet-linked targets of `target_table` by the tier key, keeps
    groups resolving to one UPRN, and applies the geometric guard (and, for
    tiers 3 and 4, the one-to-one-within-postcode guard).
    """
    join_predicate = _TIER_JOIN_PREDICATES[tier]
    uprn_guard_join = (
        "JOIN staging._epc_addrkey_uprn_ok ok "
        "ON ok.postcode_norm = c.postcode_norm AND ok.uprn = c.uprn"
        if _TIER_APPLIES_UPRN_GUARD[tier]
        else ""
    )
    return f"""
    INSERT INTO {linkage_table}
        (transactionid, uprn, tier, dateoftransfer, price, propertytype, duration)
    SELECT c.transactionid, c.uprn, {tier}, c.dateoftransfer, c.price,
           c.propertytype, c.duration
    FROM (
        SELECT t.transactionid, t.postcode_norm, t.dateoftransfer, t.price,
               t.propertytype, t.duration,
               (array_agg(DISTINCT e.uprn))[1] AS uprn
        FROM {target_table} t
        JOIN staging.epc_addrkey e ON {join_predicate}
        LEFT JOIN {linkage_table} done
               ON done.transactionid = t.transactionid
        WHERE done.transactionid IS NULL
        GROUP BY t.transactionid, t.postcode_norm, t.dateoftransfer, t.price,
                 t.propertytype, t.duration
        HAVING count(DISTINCT e.uprn) = 1
    ) c
    {uprn_guard_join}
    JOIN staging.ukhcs_table uk ON uk."UPRN" = c.uprn
    JOIN staging.codepoint_open cp
         ON staging.hpldn_pcnorm(cp.postcode) = c.postcode_norm
    WHERE sqrt(
              power(cp.eastings - uk."X_COORDINATE", 2)
            + power(cp.northings - uk."Y_COORDINATE", 2)
          ) <= :geo_threshold_m
    """


# Existing sales in the back-fill window, keyed the way PPD identifies a sale
# without a UPRN, so the back-fill never adds a second row for a sale ukhpucl
# already placed (possibly on a different UPRN).
_PPD_EXISTING_SALE_KEYS_TABLE = "staging._ppd_existing_sale_keys"

_PPD_EXISTING_SALE_KEYS_SQL = f"""
CREATE UNLOGGED TABLE {_PPD_EXISTING_SALE_KEYS_TABLE} AS
SELECT DISTINCT staging.hpldn_pcnorm(a.postcode) AS postcode_norm,
       p.dateoftransfer,
       p.price
FROM staging.ppd_table p
JOIN staging.address_table a ON a.id = p.address_id
WHERE p.dateoftransfer BETWEEN :window_start AND :window_end
"""


def create_ppd_uprn_linkage_table(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    geo_threshold_m: int = PPD_EPC_GEO_THRESHOLD_M,
    window_start: date = PPD_EPC_FUSION_WINDOW_START,
    window_end: date = PPD_EPC_FUSION_WINDOW_END,
    target_table: str = "staging._ppd_target",
    linkage_table: str = _PPD_UPRN_LINKAGE_TABLE,
    exclude_existing_sales: bool = False,
) -> Dict[int, int]:
    """
    Create `linkage_table`, the tiered PPD-to-UPRN matcher.

    Builds `target_table` (eligible London sales between `window_start` and
    `window_end`, kept for coverage checks), then links in four tiers, each
    skipping already-linked sales: T1 exact normalised address, T2 token set,
    T3 numeric signature, T4 a postcode with one UPRN. Every link must lie
    within `geo_threshold_m` of the postcode's Code-Point Open centroid.
    ``address_id`` is filled later by `mint_address_rows_for_unresolved_uprns`.

    Parameters
    ----------
    window_start, window_end : datetime.date
        Inclusive sale-date window. Default: the fusion window (0006).
    target_table, linkage_table : str
        Tables to build. Both are dropped and recreated.
    exclude_existing_sales : bool
        Leave out PPD sales whose (postcode, date, price) is already in
        ``staging.ppd_table``. Used by the pre-cut-over back-fill (0017), where
        ukhpucl already holds part of each month.

    Returns
    -------
    dict of int to int
        Transactions linked per tier.
    """
    params = {"window_start": window_start, "window_end": window_end}
    existing_filter = ""
    if exclude_existing_sales:
        existing_filter = f"""
                  AND NOT EXISTS (
                      SELECT 1 FROM {_PPD_EXISTING_SALE_KEYS_TABLE} k
                      WHERE k.postcode_norm = staging.hpldn_pcnorm(p.postcode)
                        AND k.dateoftransfer = p.dateoftransfer::date
                        AND k.price = p.price
                  )"""

    with _timed(log, f"create_ppd_uprn_linkage_table ({linkage_table})"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        if exclude_existing_sales:
            connection.execute(
                text(f"DROP TABLE IF EXISTS {_PPD_EXISTING_SALE_KEYS_TABLE}")
            )
            connection.execute(text(_PPD_EXISTING_SALE_KEYS_SQL), params)
            connection.execute(
                text(
                    f"CREATE INDEX ON {_PPD_EXISTING_SALE_KEYS_TABLE} "
                    "(postcode_norm, dateoftransfer, price)"
                )
            )
            _analyze(connection, (_PPD_EXISTING_SALE_KEYS_TABLE,), log)

        connection.execute(text(f"DROP TABLE IF EXISTS {target_table}"))
        connection.execute(
            text(
                f"""
                CREATE TABLE {target_table} AS
                SELECT
                    p.transactionid,
                    p.dateoftransfer::date AS dateoftransfer,
                    p.price,
                    p.propertytype,
                    p.duration,
                    staging.hpldn_pcnorm(p.postcode) AS postcode_norm,
                    staging.hpldn_addrkey(
                        concat_ws(' ', p.saon, p.paon, p.street)
                    ) AS k_exact,
                    staging.hpldn_tokenset(staging.hpldn_addrkey(
                        concat_ws(' ', p.saon, p.paon, p.street)
                    )) AS k_tokenset,
                    staging.hpldn_numsig(staging.hpldn_addrkey(
                        concat_ws(' ', p.saon, p.paon, p.street)
                    )) AS k_numsig
                FROM staging.ppd_domestic p
                WHERE p.county = 'GREATER LONDON'
                  AND p.categorytype = 'A'
                  AND p.recordstatus = 'A'
                  AND p.propertytype IN ('D','S','T','F')
                  AND p.duration IN ('F','L')
                  AND p.price > 0
                  AND p.dateoftransfer::date
                      BETWEEN :window_start AND :window_end{existing_filter}
                """
            ),
            params,
        )
        connection.execute(
            text(f"ALTER TABLE {target_table} ADD PRIMARY KEY (transactionid)")
        )
        for key in ("k_exact", "k_tokenset", "k_numsig"):
            connection.execute(
                text(f"CREATE INDEX ON {target_table} (postcode_norm, {key})")
            )
        _analyze(connection, (target_table,), log)

        connection.execute(text("DROP TABLE IF EXISTS staging._epc_addrkey_uprn_ok"))
        connection.execute(
            text(
                """
                CREATE TABLE staging._epc_addrkey_uprn_ok AS
                SELECT postcode_norm, uprn
                FROM staging.epc_addrkey
                GROUP BY postcode_norm, uprn
                HAVING count(DISTINCT k_exact) = 1
                """
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX ON staging._epc_addrkey_uprn_ok "
                "(postcode_norm, uprn)"
            )
        )
        _analyze(connection, ("staging._epc_addrkey_uprn_ok",), log)

        connection.execute(text(f"DROP TABLE IF EXISTS {linkage_table}"))
        connection.execute(
            text(_PPD_UPRN_LINKAGE_DDL.format(linkage_table=linkage_table))
        )

        tier_counts = {}
        for tier in (1, 2, 3, 4):
            with _timed(log, f"{linkage_table} tier {tier}"):
                inserted = connection.execute(
                    text(_ppd_uprn_linkage_tier_sql(tier, target_table, linkage_table)),
                    {"geo_threshold_m": geo_threshold_m},
                ).rowcount
            tier_counts[tier] = inserted
            _emit(log, f"{linkage_table}: tier {tier} linked {inserted} transactions")

        _analyze(connection, (linkage_table,), log)
        target_count = connection.execute(
            text(f"SELECT count(*) FROM {target_table}")
        ).scalar()
        if exclude_existing_sales:
            connection.execute(
                text(f"DROP TABLE IF EXISTS {_PPD_EXISTING_SALE_KEYS_TABLE}")
            )

    linked_count = sum(tier_counts.values())
    link_rate = linked_count / target_count if target_count else 0.0
    _emit(
        log,
        f"{linkage_table}: linked {linked_count}/{target_count} eligible PPD "
        f"transactions {window_start}..{window_end} ({link_rate:.1%}) -- by "
        f"tier: {tier_counts}",
    )
    return tier_counts


def load_ppd_history(
    engine: Engine,
    csv_fp: Path,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    window_start: date = PPD_HISTORY_WINDOW_START,
    window_end: date = PPD_HISTORY_WINDOW_END,
) -> int:
    """
    Load London's Price Paid history from HMLR ``pp-complete.csv`` into ``staging.ppd_domestic``.

    Only standard (category A), current (record status A) Greater London
    sales dated `window_start`..`window_end` are kept; the 2024+ national
    rows 0003 loaded are untouched. The 5.5 GB file is streamed through
    ``grep`` into an UNLOGGED scratch table (the county filter cuts it to
    ~4M lines), then filtered exactly in SQL. Re-runnable: rows already in
    the window are deleted first.

    Returns
    -------
    int
        Rows inserted.
    """
    from hpldn.backend.migrations.seed_registry import PPD_DOMESTIC_COLUMNS

    scratch = "staging._ppd_history_scratch"
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {scratch}"))
        connection.execute(
            text(f"CREATE UNLOGGED TABLE {scratch} (LIKE staging.ppd_domestic)")
        )
    columns = ",".join(PPD_DOMESTIC_COLUMNS)
    # psql runs the program through the client's shell; the file name is
    # double-quoted for it, and the county pre-filter is exact in SQL below.
    program = f'grep -F "GREATER LONDON" "{csv_fp}"'
    copy_sql = (
        f"\\copy {scratch} ({columns}) FROM PROGRAM '{program}' "
        "WITH (FORMAT CSV, HEADER FALSE)"
    )
    env = get_psql_env(engine, get_bulk_copy_env(log))
    with _timed(log, f"load {csv_fp.name} (Greater London lines) into {scratch}"):
        subprocess.run(
            ["psql", get_psql_url(engine), "-v", "ON_ERROR_STOP=1", "-c", copy_sql],
            check=True,
            env=env,
        )
    params = {"window_start": window_start, "window_end": window_end}
    with _timed(log, "load_ppd_history"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        deleted = connection.execute(
            text(
                "DELETE FROM staging.ppd_domestic "
                "WHERE dateoftransfer::date BETWEEN :window_start AND :window_end"
            ),
            params,
        ).rowcount
        inserted = connection.execute(
            text(
                f"""
                INSERT INTO staging.ppd_domestic ({columns})
                SELECT {columns}
                FROM {scratch}
                WHERE county = 'GREATER LONDON'
                  AND categorytype = 'A'
                  AND recordstatus = 'A'
                  AND dateoftransfer::date BETWEEN :window_start AND :window_end
                """
            ),
            params,
        ).rowcount
        scanned = connection.execute(text(f"SELECT count(*) FROM {scratch}")).scalar()
        connection.execute(text(f"DROP TABLE {scratch}"))
        _analyze(connection, ("staging.ppd_domestic",), log)
    _emit(
        log,
        f"load_ppd_history: {inserted} London category-A sale(s) {window_start}..{window_end} "
        f"loaded into staging.ppd_domestic from {scanned} Greater London line(s) of "
        f"{csv_fp.name} ({deleted} earlier history row(s) replaced)",
    )
    return inserted


def mint_address_rows_for_unresolved_uprns(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    linkage_table: str = _PPD_UPRN_LINKAGE_TABLE,
) -> None:
    """
    Mint address rows for linked UPRNs without one, then backfill linkage address ids.

    Address text comes from the EPC certificate, which carries flat detail.
    UPRNs missing from ``staging.ukhcs_table`` are reported and left NULL.
    `linkage_table` is the main fusion linkage by default, or the pre-cut-over
    back-fill (0017).
    """
    with _timed(
        log, "mint_address_rows_for_unresolved_uprns"
    ), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        orphaned = connection.execute(
            text(
                f"""
                SELECT count(DISTINCT l.uprn)
                FROM {linkage_table} l
                LEFT JOIN staging.ukhcs_table u ON u."UPRN" = l.uprn
                WHERE u.id IS NULL
                """
            )
        ).scalar()

        minted = connection.execute(
            text(
                f"""
                INSERT INTO staging.address_table
                    (uid, postnumber, poststreet, posttown, postcode, postcounty)
                SELECT DISTINCT ON (u.id)
                    u.id AS uid,
                    COALESCE(NULLIF(btrim(e.address1), ''), 'UNKNOWN') AS postnumber,
                    NULLIF(btrim(e.address2), '') AS poststreet,
                    COALESCE(
                        NULLIF(btrim(e.posttown), ''), e.local_authority_label, 'LONDON'
                    ) AS posttown,
                    btrim(e.postcode) AS postcode,
                    'GREATER LONDON'::varchar AS postcounty
                FROM (
                    SELECT DISTINCT l.uprn
                    FROM {linkage_table} l
                    JOIN staging.ukhcs_table uk ON uk."UPRN" = l.uprn
                    LEFT JOIN staging.address_table a ON a.uid = uk.id
                    WHERE a.id IS NULL
                ) unresolved
                JOIN staging.ukhcs_table u ON u."UPRN" = unresolved.uprn
                JOIN staging.epc_domestic_history e ON e.uprn = unresolved.uprn
                ORDER BY u.id, e.lodgement_datetime DESC NULLS LAST,
                         e.certificate_number DESC
                ON CONFLICT (uid) DO NOTHING
                """
            )
        ).rowcount

        backfilled = connection.execute(
            text(
                f"""
                UPDATE {linkage_table} l
                SET address_id = a.id
                FROM staging.ukhcs_table u
                JOIN staging.address_table a ON a.uid = u.id
                WHERE u."UPRN" = l.uprn
                  AND l.address_id IS DISTINCT FROM a.id
                """
            )
        ).rowcount

        still_unresolved = connection.execute(
            text(
                f"SELECT count(*) FROM {linkage_table} WHERE address_id IS NULL"
            )
        ).scalar()

    _emit(
        log,
        f"mint_address_rows_for_unresolved_uprns: minted {minted} staging.address_table rows for previously "
        f"unresolved UPRNs, backfilled address_id on {backfilled} linkage "
        f"rows ({orphaned} UPRNs absent from staging.ukhcs_table entirely, "
        f"leaving {still_unresolved} linkage rows with no address_id)",
    )
    if orphaned:
        _emit(
            log,
            f"mint_address_rows_for_unresolved_uprns: {orphaned} UPRNs linked by {linkage_table} do not "
            "exist in staging.ukhcs_table at all -- refresh OS Open UPRN to "
            "the current monthly release to pick these up",
            logging.WARNING,
        )


def insert_fused_ppd_rows(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    exclude_tiers: Tuple[int, ...] = (),
    linkage_table: str = _PPD_UPRN_LINKAGE_TABLE,
) -> int:
    """
    Insert the linkage's resolved sales into ``staging.ppd_table``.

    ``ON CONFLICT DO NOTHING`` on ``uq_ppd_table_transaction`` makes it
    idempotent. `exclude_tiers` holds tiers back (for example ``(3,)``) while
    keeping them in the linkage table.

    Returns
    -------
    int
        Rows inserted.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        result = connection.execute(
            text(
                f"""
                INSERT INTO staging.ppd_table
                    (address_id, price, dateoftransfer, propertytype, duration)
                SELECT l.address_id, l.price, l.dateoftransfer,
                       l.propertytype::bpchar, l.duration::bpchar
                FROM {linkage_table} l
                WHERE l.address_id IS NOT NULL
                  AND NOT (l.tier = ANY(:exclude_tiers))
                ON CONFLICT ON CONSTRAINT uq_ppd_table_transaction DO NOTHING
                """
            ),
            {"exclude_tiers": list(exclude_tiers)},
        )
        inserted = result.rowcount
    _emit(
        log,
        f"insert_fused_ppd_rows: inserted {inserted} rows into staging.ppd_table from "
        f"{linkage_table} (excluded tiers: {exclude_tiers or 'none'})",
    )
    return inserted


_EPC_PPD_PROPERTYTYPE_COMPATIBLE_SQL = """
    (epc_property_type IN ('Flat', 'Maisonette') AND ppd_propertytype = 'F')
    OR (epc_property_type IN ('House', 'Bungalow')
        AND ppd_propertytype IN ('D', 'S', 'T'))
"""


def verify_epc_ppd_fusion_v2(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Verify the EPC/PPD fusion.

    Fails on a certificate dated after its sale (checked only once the view
    exists) or a linkage row outside the fusion window. Logs EPC and PPD
    property-type agreement per tier, transaction uniqueness and collisions,
    and the link rate against ``PPD_EPC_BASELINE_LINK_RATE`` with a monthly
    view breakdown. Revision 0006 runs it before the view exists and 0016
    again after.

    Raises
    ------
    RuntimeError
        On an invariant violation.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        view_exists = connection.execute(
            text("SELECT to_regclass('production.london_houses') IS NOT NULL")
        ).scalar()

        if view_exists:
            future_row = connection.execute(
                text(
                    """
                    SELECT count(*) AS window_sales,
                           count(*) FILTER (WHERE epc_age_at_sale_days < 0)
                               AS future_epc_matches
                    FROM production.london_houses
                    WHERE dateoftransfer BETWEEN :window_start AND :window_end
                    """
                ),
                {
                    "window_start": PPD_EPC_FUSION_WINDOW_START,
                    "window_end": PPD_EPC_FUSION_WINDOW_END,
                },
            ).mappings().one()
            _emit(
                log,
                f"verify_epc_ppd_fusion_v2: {future_row['window_sales']} london_houses rows in the "
                f"fusion window, {future_row['future_epc_matches']} with a future "
                "EPC match",
            )
            if future_row["future_epc_matches"]:
                raise RuntimeError(
                    f"verify_epc_ppd_fusion_v2: {future_row['future_epc_matches']} production.london_houses "
                    "rows in the fusion window have epc_age_at_sale_days < 0 -- the "
                    "view's as-of join matched a certificate lodged/inspected after "
                    "the sale it describes"
                )
        else:
            _emit(
                log,
                "verify_epc_ppd_fusion_v2: production.london_houses does not exist yet -- the "
                "future-EPC check and the monthly view breakdown are deferred "
                "to revision 0016, which builds the view and re-runs this "
                "verification",
            )

        tier_agreement = connection.execute(
            text(
                f"""
                WITH latest_epc AS (
                    SELECT DISTINCT ON (eh.uprn)
                           eh.uprn, eh.property_type
                    FROM staging.epc_domestic_history eh
                    WHERE eh.uprn IS NOT NULL
                    ORDER BY eh.uprn, eh.lodgement_datetime DESC NULLS LAST,
                             eh.certificate_number DESC
                ),
                linked AS (
                    SELECT l.tier, l.propertytype AS ppd_propertytype,
                           e.property_type AS epc_property_type
                    FROM staging.ppd_uprn_linkage l
                    JOIN latest_epc e ON e.uprn = l.uprn
                )
                SELECT tier, count(*) AS n,
                       count(*) FILTER (
                           WHERE NOT ({_EPC_PPD_PROPERTYTYPE_COMPATIBLE_SQL})
                       ) AS disagreements
                FROM linked
                GROUP BY tier
                ORDER BY tier
                """
            )
        ).mappings().all()
        for row in tier_agreement:
            rate = row["disagreements"] / row["n"] if row["n"] else 0.0
            _emit(
                log,
                f"verify_epc_ppd_fusion_v2: tier {row['tier']} property-type disagreement rate "
                f"{rate:.2%} ({row['disagreements']}/{row['n']})",
            )

        duplicate_transactionids = connection.execute(
            text(
                "SELECT count(*) - count(DISTINCT transactionid) "
                "FROM staging.ppd_uprn_linkage"
            )
        ).scalar()
        key_collisions = connection.execute(
            text(
                """
                SELECT count(*) FROM (
                    SELECT address_id, dateoftransfer, price
                    FROM staging.ppd_uprn_linkage
                    WHERE address_id IS NOT NULL
                    GROUP BY address_id, dateoftransfer, price
                    HAVING count(*) > 1
                ) dup
                """
            )
        ).scalar()
        _emit(
            log,
            f"verify_epc_ppd_fusion_v2: {key_collisions} (address_id, dateoftransfer, price) "
            "collisions in staging.ppd_uprn_linkage (rows ON CONFLICT DO "
            "NOTHING would swallow in insert_fused_ppd_rows)",
        )
        if duplicate_transactionids:
            raise RuntimeError(
                f"verify_epc_ppd_fusion_v2: {duplicate_transactionids} duplicate transactionid values "
                "in staging.ppd_uprn_linkage -- its own PRIMARY KEY should "
                "have made this impossible"
            )

        eligible = connection.execute(
            text("SELECT count(*) FROM staging._ppd_target")
        ).scalar()
        linked = connection.execute(
            text("SELECT count(*) FROM staging.ppd_uprn_linkage")
        ).scalar()
        tier_table = dict(
            connection.execute(
                text(
                    "SELECT tier, count(*) FROM staging.ppd_uprn_linkage "
                    "GROUP BY tier ORDER BY tier"
                )
            ).all()
        )
        monthly_counts: Dict[str, int] = {}
        if view_exists:
            monthly_counts = dict(
                connection.execute(
                    text(
                        """
                        SELECT to_char(date_trunc('month', dateoftransfer), 'YYYY-MM'),
                               count(*)
                        FROM production.london_houses
                        WHERE dateoftransfer >= :window_start
                        GROUP BY 1 ORDER BY 1
                        """
                    ),
                    {"window_start": PPD_EPC_FUSION_WINDOW_START},
                ).all()
            )
        link_rate = linked / eligible if eligible else 0.0
        _emit(
            log,
            f"verify_epc_ppd_fusion_v2: coverage {linked}/{eligible} eligible sales linked "
            f"({link_rate:.1%}, baseline {PPD_EPC_BASELINE_LINK_RATE:.1%}) -- "
            f"by tier: {tier_table}; london_houses rows by month: "
            f"{monthly_counts}",
        )
        if link_rate <= PPD_EPC_BASELINE_LINK_RATE:
            raise RuntimeError(
                f"verify_epc_ppd_fusion_v2: eligible-sale link rate {link_rate:.1%} does not exceed "
                f"the Tier-A baseline {PPD_EPC_BASELINE_LINK_RATE:.1%} -- the "
                "tiered matcher should strictly beat the fixture it replaces"
            )

        pre_window_rows = connection.execute(
            text(
                "SELECT count(*) FROM staging.ppd_uprn_linkage "
                "WHERE dateoftransfer < :window_start"
            ),
            {"window_start": PPD_EPC_FUSION_WINDOW_START},
        ).scalar()
        if pre_window_rows:
            raise RuntimeError(
                f"verify_epc_ppd_fusion_v2: {pre_window_rows} staging.ppd_uprn_linkage rows carry "
                f"a dateoftransfer before {PPD_EPC_FUSION_WINDOW_START} -- the "
                "fusion pipeline should only ever touch sales inside its own "
                "window, and existing history is only unchanged if it never does"
            )


def backfill_epc_table_for_fusion_gap(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Backfill ``production.epc_table`` for fusion-window addresses with no certificate.

    Inserts every ``staging.epc_domestic_history`` certificate for those
    addresses, one per (uprn, inspection_date), encoded with the same mappings
    as the rest of the table, and appends directly to production so a
    re-promote would not have erased them. Only the habitable-rooms bound is
    applied, NULL-tolerantly; smallint columns pass through ``pg_temp.si`` so
    an out-of-range source value becomes NULL instead of failing the INSERT.
    Existing rows and sales are untouched.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        with _timed(log, "idx_prod_epc_address_inspection"):
            connection.execute(text(_EPC_ASOF_INDEX_SQL))
        _analyze(
            connection,
            (
                "production.ppd_table",
                "production.epc_table",
                "production.uprn_lookup",
                "staging.epc_domestic_history",
            ),
            log,
        )

        gap_count = connection.execute(
            text(
                """
                SELECT count(*) FROM (
                    SELECT DISTINCT p.address_id
                    FROM production.ppd_table p
                    WHERE p.dateoftransfer BETWEEN :window_start AND :window_end
                      AND NOT EXISTS (
                          SELECT 1 FROM production.epc_table e
                          WHERE e.address_id = p.address_id
                      )
                ) gap
                """
            ),
            {
                "window_start": PPD_EPC_FUSION_WINDOW_START,
                "window_end": PPD_EPC_FUSION_WINDOW_END,
            },
        ).scalar()
        _emit(
            log,
            f"backfill_epc_table_for_fusion_gap: {gap_count} fusion-window addresses have zero "
            "production.epc_table rows",
        )
        if not gap_count:
            _emit(log, "backfill_epc_table_for_fusion_gap: no gap addresses -- nothing to backfill")
            return

        with _timed(log, "sync production.epc_table id sequence"):
            connection.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence("
                    "'production.epc_table', 'id'), "
                    "(SELECT COALESCE(MAX(id), 0) FROM production.epc_table))"
                )
            )

        bounds = _get_epc_numeric_bounds(engine, cfg, log)
        nhr_low, nhr_high = bounds["number_habitable_rooms"]
        bounded_predicates = (
            f"(h.number_habitable_rooms IS NULL "
            f"OR h.number_habitable_rooms BETWEEN {nhr_low} AND {nhr_high})"
        )
        connection.execute(text(_EPC_SI_FUNCTION_SQL))

        eff_cases_sql = _eff_rating_case_sql("h")
        tenure_cases_sql = _generate_case_sql("tenure", TENURE_MAPPING, "h")
        build_form_cases_sql = _generate_case_sql(
            "built_form", BUILD_FORM_MAPPING, "h"
        )
        transaction_type_cases_sql = _generate_case_sql(
            "transaction_type", TRANSACTION_TYPE_MAPPING, "h"
        )
        energy_tariff_cases_sql = _generate_case_sql(
            "energy_tariff", ENERGY_TARIFF_MAPPING, "h"
        )
        glazed_type_cases_sql = _generate_case_sql(
            "glazed_type", GLAZED_TYPE_MAPPING, "h"
        )
        mains_gas_flag_sql = "BTRIM(UPPER(mains_gas_flag)) IN ('Y', 'N')"

        insert_sql = text(f"""
        WITH gap_addresses AS (
            SELECT DISTINCT p.address_id
            FROM production.ppd_table p
            WHERE p.dateoftransfer BETWEEN :window_start AND :window_end
              AND NOT EXISTS (
                  SELECT 1 FROM production.epc_table e
                  WHERE e.address_id = p.address_id
              )
        ),
        gap_certificates AS (
            -- DISTINCT ON (h.uprn, h.inspection_date), keeping the newest
            -- lodgement: staging.epc_domestic_history is loaded straight
            -- from the national certificates-<year>.csv files with no
            -- dedup of its own (unlike staging.epc_table_condensed, which
            -- create_epc_fused_staging_table's own deduplicated_epc CTE
            -- collapses this exact same way), so a property re-lodging the
            -- same inspection under a new certificate_number would
            -- otherwise reach the INSERT below as two rows sharing one
            -- (address_id, inspection_date) -- confirmed against this
            -- database (UniqueViolation on production.epc_table's
            -- (address_id, inspection_date) unique constraint).
            SELECT DISTINCT ON (h.uprn, h.inspection_date)
                ul.address_id,
                h.current_energy_rating,
                h.potential_energy_rating,
                pg_temp.si(h.current_energy_efficiency) AS current_energy_efficiency,
                pg_temp.si(h.potential_energy_efficiency) AS potential_energy_efficiency,
                {build_form_cases_sql},
                h.inspection_date,
                {transaction_type_cases_sql},
                pg_temp.si(h.environment_impact_current) AS environment_impact_current,
                pg_temp.si(h.environment_impact_potential) AS environment_impact_potential,
                pg_temp.si(h.energy_consumption_current) AS energy_consumption_current,
                pg_temp.si(h.energy_consumption_potential) AS energy_consumption_potential,
                h.co2_emissions_current,
                pg_temp.si(h.co2_emiss_curr_per_floor_area) AS co2_emiss_curr_per_floor_area,
                h.co2_emissions_potential,
                pg_temp.si(h.lighting_cost_current) AS lighting_cost_current,
                pg_temp.si(h.lighting_cost_potential) AS lighting_cost_potential,
                pg_temp.si(h.heating_cost_current) AS heating_cost_current,
                pg_temp.si(h.heating_cost_potential) AS heating_cost_potential,
                pg_temp.si(h.hot_water_cost_current) AS hot_water_cost_current,
                pg_temp.si(h.hot_water_cost_potential) AS hot_water_cost_potential,
                h.total_floor_area,
                {energy_tariff_cases_sql},
                h.mains_gas_flag,
                pg_temp.si(h.flat_storey_count) AS flat_storey_count,
                pg_temp.si(h.multi_glaze_proportion) AS multi_glaze_proportion,
                {glazed_type_cases_sql},
                pg_temp.si(h.extension_count) AS extension_count,
                pg_temp.si(h.number_habitable_rooms) AS number_habitable_rooms,
                pg_temp.si(h.number_heated_rooms) AS number_heated_rooms,
                pg_temp.si(h.low_energy_lighting) AS low_energy_lighting,
                {eff_cases_sql},
                h.construction_age_band,
                h.lodgement_datetime,
                {tenure_cases_sql}
            FROM staging.epc_domestic_history h
            JOIN production.uprn_lookup ul ON ul.uprn = h.uprn
            JOIN gap_addresses g ON g.address_id = ul.address_id
            WHERE h.inspection_date IS NOT NULL
              AND {bounded_predicates}
            ORDER BY h.uprn, h.inspection_date, h.lodgement_datetime DESC
        )
        INSERT INTO production.epc_table (
            address_id, current_energy_rating, potential_energy_rating,
            current_energy_efficiency, potential_energy_efficiency, build_form,
            inspection_date, transaction_type, environment_impact_current,
            environment_impact_potential, energy_consumption_current,
            energy_consumption_potential, co2_emissions_current,
            co2_emiss_curr_per_floor_area, co2_emissions_potential,
            lighting_cost_current, lighting_cost_potential, heating_cost_current,
            heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
            total_floor_area, energy_tariff, mains_gas_flag, flat_storey_count,
            multi_glaze_proportion, glazed_type, extension_count,
            number_habitable_rooms, number_heated_rooms, low_energy_lighting,
            hot_water_energy_eff, hot_water_env_eff, windows_energy_eff,
            windows_env_eff, walls_energy_eff, walls_env_eff, roof_energy_eff,
            roof_env_eff, mainheat_energy_eff, mainheat_env_eff,
            mainheatc_energy_eff, mainheatc_env_eff, lighting_energy_eff,
            lighting_env_eff, construction_age_band, lodgement_datetime, tenure
        )
        SELECT
            address_id, current_energy_rating, potential_energy_rating,
            current_energy_efficiency, potential_energy_efficiency,
            -- built_form, not build_form: gap_certificates' own build_form_
            -- cases_sql column is named after its source (staging.epc_
            -- domestic_history.built_form), not its production.epc_table
            -- target -- see backfill_epc_table_for_fusion_gap's comment on
            -- build_form_cases_sql. Selecting it into this SELECT list's
            -- 6th position is what lands it in the INSERT column list's
            -- build_form position above.
            built_form,
            inspection_date, transaction_type, environment_impact_current,
            environment_impact_potential, energy_consumption_current,
            energy_consumption_potential, co2_emissions_current,
            co2_emiss_curr_per_floor_area, co2_emissions_potential,
            lighting_cost_current, lighting_cost_potential, heating_cost_current,
            heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
            total_floor_area, energy_tariff,
            CASE WHEN {mains_gas_flag_sql} THEN BTRIM(UPPER(mains_gas_flag)) END,
            flat_storey_count,
            multi_glaze_proportion, glazed_type, extension_count,
            number_habitable_rooms, number_heated_rooms, low_energy_lighting,
            NULLIF(hot_water_energy_eff, -1), NULLIF(hot_water_env_eff, -1),
            NULLIF(windows_energy_eff, -1), NULLIF(windows_env_eff, -1),
            NULLIF(walls_energy_eff, -1), NULLIF(walls_env_eff, -1),
            NULLIF(roof_energy_eff, -1), NULLIF(roof_env_eff, -1),
            NULLIF(mainheat_energy_eff, -1), NULLIF(mainheat_env_eff, -1),
            NULLIF(mainheatc_energy_eff, -1), NULLIF(mainheatc_env_eff, -1),
            NULLIF(lighting_energy_eff, -1), NULLIF(lighting_env_eff, -1),
            construction_age_band, lodgement_datetime, tenure
        FROM gap_certificates
        -- Defensive, not the primary fix (the DISTINCT ON above is):
        -- production.uprn_lookup is only unique on uprn, not address_id,
        -- so two different UPRNs resolving to the same address_id could
        -- still collide here on (address_id, inspection_date) even after
        -- the per-uprn dedup. Same ON CONFLICT DO NOTHING shape
        -- insert_fused_ppd_rows uses for the equivalent reason on staging.ppd_table.
        ON CONFLICT (address_id, inspection_date) DO NOTHING
        """)
        with _timed(log, "INSERT gap certificates -> production.epc_table"):
            inserted = connection.execute(
                insert_sql,
                {
                    "window_start": PPD_EPC_FUSION_WINDOW_START,
                    "window_end": PPD_EPC_FUSION_WINDOW_END,
                },
            ).rowcount
        _analyze(connection, ("production.epc_table",), log)

        still_missing = connection.execute(
            text(
                """
                SELECT count(*) FROM (
                    SELECT DISTINCT p.address_id
                    FROM production.ppd_table p
                    WHERE p.dateoftransfer BETWEEN :window_start AND :window_end
                      AND NOT EXISTS (
                          SELECT 1 FROM production.epc_table e
                          WHERE e.address_id = p.address_id
                      )
                ) gap
                """
            ),
            {
                "window_start": PPD_EPC_FUSION_WINDOW_START,
                "window_end": PPD_EPC_FUSION_WINDOW_END,
            },
        ).scalar()

    _emit(
        log,
        f"backfill_epc_table_for_fusion_gap: inserted {inserted} certificate rows into production.epc_table "
        f"for {gap_count - still_missing}/{gap_count} gap addresses "
        f"({still_missing} still have zero rows -- no staging."
        "epc_domestic_history certificate for that UPRN survived the "
        "inspection_date/3-sigma filters)",
    )


_EPC_SI_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION pg_temp.si(numeric) RETURNS smallint
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN $1 BETWEEN -32768 AND 32767 THEN round($1)::smallint END $$
"""

_HISTORY_ADDRESSES_WITHOUT_EPC_ROWS_SQL = """
SELECT count(*) FROM (
    SELECT DISTINCT ul.address_id
    FROM {history_table} h
    JOIN production.uprn_lookup ul ON ul.uprn = h.uprn
    WHERE h.inspection_date IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM production.epc_table e
          WHERE e.address_id = ul.address_id
      )
) hist
"""


def backfill_epc_table_from_london_history(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    history_table: str = "staging.epc_domestic_history",
) -> None:
    """
    Backfill ``production.epc_table`` from every resolvable London history certificate.

    `backfill_epc_table_for_fusion_gap` without its window and empty-address
    restrictions. Existing rows are never overwritten, but newer certificates
    can change which one the view's as-of join picks, so **retrain after
    running it**. The dedup is built with CREATE TABLE AS, and smallint values
    pass through ``pg_temp.si``. `history_table` is a table shaped like
    ``staging.epc_domestic_history`` (0030 passes its UPRN-less certificates
    with their resolved UPRN).
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        with _timed(log, "idx_prod_epc_address_inspection"):
            connection.execute(text(_EPC_ASOF_INDEX_SQL))
        _analyze(
            connection,
            (
                "production.epc_table",
                "production.uprn_lookup",
                history_table,
            ),
            log,
        )

        history_addresses = connection.execute(
            text(
                f"""
                SELECT count(*) FROM (
                    SELECT DISTINCT ul.address_id
                    FROM {history_table} h
                    JOIN production.uprn_lookup ul ON ul.uprn = h.uprn
                    WHERE h.inspection_date IS NOT NULL
                ) hist
                """
            )
        ).scalar()
        missing_before = connection.execute(
            text(_HISTORY_ADDRESSES_WITHOUT_EPC_ROWS_SQL.format(history_table=history_table))
        ).scalar()
        _emit(
            log,
            f"backfill_epc_table_from_london_history: {history_addresses} addresses reachable from "
            f"staging.epc_domestic_history via production.uprn_lookup, of "
            f"which {missing_before} currently have zero production.epc_table "
            "rows",
        )
        if not history_addresses:
            _emit(
                log,
                "backfill_epc_table_from_london_history: no history addresses resolve to an address_id -- "
                "nothing to backfill",
            )
            return

        with _timed(log, "sync production.epc_table id sequence"):
            connection.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence("
                    "'production.epc_table', 'id'), "
                    "(SELECT COALESCE(MAX(id), 0) FROM production.epc_table))"
                )
            )

        bounds = _get_epc_numeric_bounds(engine, cfg, log)
        nhr_low, nhr_high = bounds["number_habitable_rooms"]
        bounded_predicates = (
            f"(h.number_habitable_rooms IS NULL "
            f"OR h.number_habitable_rooms BETWEEN {nhr_low} AND {nhr_high})"
        )

        connection.execute(text(_EPC_SI_FUNCTION_SQL))

        eff_cases_sql = _eff_rating_case_sql("h")
        tenure_cases_sql = _generate_case_sql("tenure", TENURE_MAPPING, "h")
        build_form_cases_sql = _generate_case_sql(
            "built_form", BUILD_FORM_MAPPING, "h"
        )
        transaction_type_cases_sql = _generate_case_sql(
            "transaction_type", TRANSACTION_TYPE_MAPPING, "h"
        )
        energy_tariff_cases_sql = _generate_case_sql(
            "energy_tariff", ENERGY_TARIFF_MAPPING, "h"
        )
        glazed_type_cases_sql = _generate_case_sql(
            "glazed_type", GLAZED_TYPE_MAPPING, "h"
        )
        mains_gas_flag_sql = "BTRIM(UPPER(mains_gas_flag)) IN ('Y', 'N')"

        history_build_table = "staging._epc_history_certificates_build"
        connection.execute(text(f"DROP TABLE IF EXISTS {history_build_table}"))
        build_sql = text(f"""
        CREATE UNLOGGED TABLE {history_build_table} AS
        -- DISTINCT ON (h.uprn, h.inspection_date), keeping the newest
        -- lodgement: staging.epc_domestic_history is loaded straight
        -- from the national certificates-<year>.csv files with no
        -- dedup of its own (unlike staging.epc_table_condensed, which
        -- create_epc_fused_staging_table's own deduplicated_epc CTE
        -- collapses this exact same way), so a property re-lodging the
        -- same inspection under a new certificate_number would
        -- otherwise reach the INSERT below as two rows sharing one
        -- (address_id, inspection_date) -- confirmed against this
        -- database (UniqueViolation on production.epc_table's
        -- (address_id, inspection_date) unique constraint).
        SELECT DISTINCT ON (h.uprn, h.inspection_date)
            ul.address_id,
            h.current_energy_rating,
            h.potential_energy_rating,
            pg_temp.si(h.current_energy_efficiency) AS current_energy_efficiency,
            pg_temp.si(h.potential_energy_efficiency) AS potential_energy_efficiency,
            {build_form_cases_sql},
            h.inspection_date,
            {transaction_type_cases_sql},
            pg_temp.si(h.environment_impact_current) AS environment_impact_current,
            pg_temp.si(h.environment_impact_potential) AS environment_impact_potential,
            pg_temp.si(h.energy_consumption_current) AS energy_consumption_current,
            pg_temp.si(h.energy_consumption_potential) AS energy_consumption_potential,
            h.co2_emissions_current,
            pg_temp.si(h.co2_emiss_curr_per_floor_area) AS co2_emiss_curr_per_floor_area,
            h.co2_emissions_potential,
            pg_temp.si(h.lighting_cost_current) AS lighting_cost_current,
            pg_temp.si(h.lighting_cost_potential) AS lighting_cost_potential,
            pg_temp.si(h.heating_cost_current) AS heating_cost_current,
            pg_temp.si(h.heating_cost_potential) AS heating_cost_potential,
            pg_temp.si(h.hot_water_cost_current) AS hot_water_cost_current,
            pg_temp.si(h.hot_water_cost_potential) AS hot_water_cost_potential,
            h.total_floor_area,
            {energy_tariff_cases_sql},
            h.mains_gas_flag,
            pg_temp.si(h.flat_storey_count) AS flat_storey_count,
            pg_temp.si(h.multi_glaze_proportion) AS multi_glaze_proportion,
            {glazed_type_cases_sql},
            pg_temp.si(h.extension_count) AS extension_count,
            pg_temp.si(h.number_habitable_rooms) AS number_habitable_rooms,
            pg_temp.si(h.number_heated_rooms) AS number_heated_rooms,
            pg_temp.si(h.low_energy_lighting) AS low_energy_lighting,
            {eff_cases_sql},
            h.construction_age_band,
            h.lodgement_datetime,
            {tenure_cases_sql}
        FROM {history_table} h
        JOIN production.uprn_lookup ul ON ul.uprn = h.uprn
        WHERE h.inspection_date IS NOT NULL
          AND {bounded_predicates}
        ORDER BY h.uprn, h.inspection_date, h.lodgement_datetime DESC
        """)
        with _timed(log, f"build {history_build_table}"):
            connection.execute(build_sql)
        connection.execute(
            text(
                f"CREATE INDEX ON {history_build_table} (address_id, inspection_date)"
            )
        )
        _analyze(connection, (history_build_table,), log)

        insert_sql = text(f"""
        INSERT INTO production.epc_table (
            address_id, current_energy_rating, potential_energy_rating,
            current_energy_efficiency, potential_energy_efficiency, build_form,
            inspection_date, transaction_type, environment_impact_current,
            environment_impact_potential, energy_consumption_current,
            energy_consumption_potential, co2_emissions_current,
            co2_emiss_curr_per_floor_area, co2_emissions_potential,
            lighting_cost_current, lighting_cost_potential, heating_cost_current,
            heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
            total_floor_area, energy_tariff, mains_gas_flag, flat_storey_count,
            multi_glaze_proportion, glazed_type, extension_count,
            number_habitable_rooms, number_heated_rooms, low_energy_lighting,
            hot_water_energy_eff, hot_water_env_eff, windows_energy_eff,
            windows_env_eff, walls_energy_eff, walls_env_eff, roof_energy_eff,
            roof_env_eff, mainheat_energy_eff, mainheat_env_eff,
            mainheatc_energy_eff, mainheatc_env_eff, lighting_energy_eff,
            lighting_env_eff, construction_age_band, lodgement_datetime, tenure
        )
        SELECT
            address_id, current_energy_rating, potential_energy_rating,
            current_energy_efficiency, potential_energy_efficiency,
            -- built_form, not build_form: the build table's own
            -- build_form_cases_sql column is named after its source
            -- (staging.epc_domestic_history.built_form), not its
            -- production.epc_table target -- see backfill_epc_table_for_
            -- fusion_gap's comment on build_form_cases_sql. Selecting it
            -- into this SELECT list's 6th position is what lands it in the
            -- INSERT column list's build_form position above.
            built_form,
            inspection_date, transaction_type, environment_impact_current,
            environment_impact_potential, energy_consumption_current,
            energy_consumption_potential, co2_emissions_current,
            co2_emiss_curr_per_floor_area, co2_emissions_potential,
            lighting_cost_current, lighting_cost_potential, heating_cost_current,
            heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
            total_floor_area, energy_tariff,
            CASE WHEN {mains_gas_flag_sql} THEN BTRIM(UPPER(mains_gas_flag)) END,
            flat_storey_count,
            multi_glaze_proportion, glazed_type, extension_count,
            number_habitable_rooms, number_heated_rooms, low_energy_lighting,
            NULLIF(hot_water_energy_eff, -1), NULLIF(hot_water_env_eff, -1),
            NULLIF(windows_energy_eff, -1), NULLIF(windows_env_eff, -1),
            NULLIF(walls_energy_eff, -1), NULLIF(walls_env_eff, -1),
            NULLIF(roof_energy_eff, -1), NULLIF(roof_env_eff, -1),
            NULLIF(mainheat_energy_eff, -1), NULLIF(mainheat_env_eff, -1),
            NULLIF(mainheatc_energy_eff, -1), NULLIF(mainheatc_env_eff, -1),
            NULLIF(lighting_energy_eff, -1), NULLIF(lighting_env_eff, -1),
            construction_age_band, lodgement_datetime, tenure
        FROM {history_build_table}
        -- Defensive, not the primary fix (the DISTINCT ON above is):
        -- production.uprn_lookup is only unique on uprn, not address_id,
        -- so two different UPRNs resolving to the same address_id could
        -- still collide here on (address_id, inspection_date) even after
        -- the per-uprn dedup. Same ON CONFLICT DO NOTHING shape
        -- insert_fused_ppd_rows uses for the equivalent reason on staging.ppd_table.
        ON CONFLICT (address_id, inspection_date) DO NOTHING
        """)
        with _timed(log, "INSERT London history certificates -> production.epc_table"):
            inserted = connection.execute(insert_sql).rowcount
        connection.execute(text(f"DROP TABLE IF EXISTS {history_build_table}"))
        _analyze(connection, ("production.epc_table",), log)

        still_missing = connection.execute(
            text(_HISTORY_ADDRESSES_WITHOUT_EPC_ROWS_SQL.format(history_table=history_table))
        ).scalar()

    _emit(
        log,
        f"backfill_epc_table_from_london_history: inserted {inserted} certificate rows into production.epc_table "
        f"across {history_addresses} history-reachable addresses; addresses "
        f"with zero rows there {missing_before} -> {still_missing} "
        f"({missing_before - still_missing} closed, {still_missing} still have "
        "zero rows -- no staging.epc_domestic_history certificate for that "
        "UPRN survived the inspection_date/number_habitable_rooms filters). "
        "Every EPC feature value on an existing production.london_houses row "
        "may now move to a fresher certificate: retrain before trusting any "
        "model artefact built before this ran",
    )


def verify_epc_ppd_fusion_view_coverage(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> None:
    """
    Check the view keeps the linked fusion-window sales.

    Compares distinct linked sales with those present in
    ``production.london_houses`` and fails beyond
    ``PPD_EPC_VIEW_COVERAGE_TOLERANCE``, breaking the shortfall down into
    addresses with no certificate and those whose certificates do not qualify.

    Raises
    ------
    RuntimeError
        If coverage falls short.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        view_count = connection.execute(
            text(
                """
                SELECT count(*) FROM production.london_houses
                WHERE dateoftransfer BETWEEN :window_start AND :window_end
                """
            ),
            {
                "window_start": PPD_EPC_FUSION_WINDOW_START,
                "window_end": PPD_EPC_FUSION_WINDOW_END,
            },
        ).scalar()

        _LINKAGE_PPD_CTE = """
            WITH linkage_ppd AS (
                SELECT DISTINCT p.id AS ppd_id, p.address_id
                FROM staging.ppd_uprn_linkage l
                JOIN production.ppd_table p
                  ON p.address_id = l.address_id
                 AND p.dateoftransfer = l.dateoftransfer
                 AND p.price = l.price
            ),
            missing AS (
                SELECT lp.address_id
                FROM linkage_ppd lp
                LEFT JOIN production.london_houses lh ON lh.ppd_id = lp.ppd_id
                WHERE lh.ppd_id IS NULL
            )
        """

        counts = connection.execute(
            text(
                _LINKAGE_PPD_CTE
                + """
                SELECT
                    (SELECT count(*) FROM linkage_ppd) AS linked,
                    (SELECT count(*) FROM missing) AS shortfall
                """
            )
        ).mappings().one()
        linked = counts["linked"]
        shortfall = counts["shortfall"]
        shortfall_rate = shortfall / linked if linked else 0.0
        _emit(
            log,
            f"verify_epc_ppd_fusion_view_coverage: production.london_houses holds {view_count} fusion-window "
            f"rows against {linked} staging.ppd_uprn_linkage linked rows "
            f"(shortfall {shortfall}, {shortfall_rate:.1%}, tolerance "
            f"{PPD_EPC_VIEW_COVERAGE_TOLERANCE:.1%})",
        )

        if shortfall_rate <= PPD_EPC_VIEW_COVERAGE_TOLERANCE:
            return

        breakdown = connection.execute(
            text(
                _LINKAGE_PPD_CTE
                + """
                SELECT
                    count(*) AS total_missing,
                    count(*) FILTER (
                        WHERE NOT EXISTS (
                            SELECT 1 FROM production.epc_table e
                            WHERE e.address_id = m.address_id
                        )
                    ) AS absent_entirely,
                    count(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM production.epc_table e
                            WHERE e.address_id = m.address_id
                        )
                    ) AS present_but_non_qualifying
                FROM missing m
                """
            )
        ).mappings().one()

        raise RuntimeError(
            f"verify_epc_ppd_fusion_view_coverage: production.london_houses' fusion-window row count falls "
            f"short of staging.ppd_uprn_linkage's linked count by "
            f"{shortfall} rows ({shortfall_rate:.1%}, exceeding the "
            f"{PPD_EPC_VIEW_COVERAGE_TOLERANCE:.1%} tolerance) -- of the "
            f"{breakdown['total_missing']} missing linked sales, "
            f"{breakdown['absent_entirely']} have zero production.epc_table "
            f"rows for their address at all and "
            f"{breakdown['present_but_non_qualifying']} have a "
            "production.epc_table row that just doesn't satisfy the view's "
            "as-of join / required-non-null predicate"
        )



_UKHCS_REFRESH_STAGE_TABLE = "staging._ukhcs_refresh"
_UKHCS_REFRESH_MARKER_TABLE = "staging._ukhcs_refresh_marker"

_UKHCS_EXTRACT_COLUMNS = (
    "UPRN",
    "X_COORDINATE",
    "Y_COORDINATE",
    "LATITUDE",
    "LONGITUDE",
)


def _get_uprn_refresh_csv_fp(cfg: Optional[Dict]) -> Path:
    """
    Resolve the newer OS Open UPRN extract's path.

    A separate input from ``osopenuprn_csv_fp``, so the original load is
    unchanged.
    """
    path = inputs.resolve("osopenuprn_refresh_csv_fp", cfg)
    if not path.is_file():
        raise FileNotFoundError(f"OS Open UPRN refresh extract not found: {path}")
    return path


def refresh_ukhcs_table_from_extract(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> int:
    r"""
    Insert UPRNs from a newer OS Open UPRN extract into ``staging.ukhcs_table``.

    Insert-only: new UPRNs are added; existing ones keep their coordinates
    (revisions are counted but not applied, since every spatial feature was
    built from them); retired ones are kept, because addresses reference them.
    The extract is ``\copy``-ed into a scratch table first, and a marker row
    records the pre-insert ``max(id)``. Safe to re-run.

    Returns
    -------
    int
        Rows inserted.
    """
    csv_fp = _get_uprn_refresh_csv_fp(cfg)
    copy_columns = ", ".join(f'"{c}"' for c in _UKHCS_EXTRACT_COLUMNS)

    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {_UKHCS_REFRESH_STAGE_TABLE}"))
        connection.execute(
            text(
                f"""
                CREATE UNLOGGED TABLE {_UKHCS_REFRESH_STAGE_TABLE} (
                    "UPRN" BIGINT,
                    "X_COORDINATE" DOUBLE PRECISION,
                    "Y_COORDINATE" DOUBLE PRECISION,
                    "LATITUDE" DOUBLE PRECISION,
                    "LONGITUDE" DOUBLE PRECISION
                )
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {_UKHCS_REFRESH_MARKER_TABLE} (
                    revision TEXT PRIMARY KEY,
                    extract_fp TEXT NOT NULL,
                    max_id_before BIGINT NOT NULL,
                    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )

    with _timed(log, f"\\copy {csv_fp.name} -> {_UKHCS_REFRESH_STAGE_TABLE}"):
        subprocess.run(
            [
                "psql",
                get_psql_url(engine),
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"\\copy {_UKHCS_REFRESH_STAGE_TABLE} ({copy_columns}) FROM "
                f"'{csv_fp}' WITH (FORMAT CSV, HEADER TRUE)",
            ],
            check=True,
            env=get_psql_env(engine),
        )

    with _timed(log, "refresh_ukhcs_table_from_extract"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)

        connection.execute(
            text(f'CREATE INDEX ON {_UKHCS_REFRESH_STAGE_TABLE} ("UPRN")')
        )
        _analyze(connection, [_UKHCS_REFRESH_STAGE_TABLE], log)

        extract_rows = connection.execute(
            text(f"SELECT count(*) FROM {_UKHCS_REFRESH_STAGE_TABLE}")
        ).scalar()

        connection.execute(
            text(
                f"""
                INSERT INTO {_UKHCS_REFRESH_MARKER_TABLE}
                    (revision, extract_fp, max_id_before)
                SELECT '0073', :extract_fp, COALESCE(max(id), 0)
                FROM staging.ukhcs_table
                ON CONFLICT (revision) DO NOTHING
                """
            ),
            {"extract_fp": str(csv_fp)},
        )

        with _timed(log, "insert new UPRNs into staging.ukhcs_table"):
            inserted = connection.execute(
                text(
                    f"""
                    INSERT INTO staging.ukhcs_table
                        ("UPRN", "X_COORDINATE", "Y_COORDINATE",
                         "LATITUDE", "LONGITUDE")
                    SELECT DISTINCT ON (r."UPRN")
                        r."UPRN", r."X_COORDINATE", r."Y_COORDINATE",
                        r."LATITUDE", r."LONGITUDE"
                    FROM {_UKHCS_REFRESH_STAGE_TABLE} r
                    WHERE r."UPRN" IS NOT NULL
                      AND r."X_COORDINATE" IS NOT NULL
                      AND r."Y_COORDINATE" IS NOT NULL
                      AND r."LATITUDE" IS NOT NULL
                      AND r."LONGITUDE" IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM staging.ukhcs_table u
                          WHERE u."UPRN" = r."UPRN"
                      )
                    ORDER BY r."UPRN"
                    ON CONFLICT DO NOTHING
                    """
                )
            ).rowcount

        with _timed(log, "measure retired and moved UPRNs"):
            retired, moved = connection.execute(
                text(
                    f"""
                    SELECT
                        count(*) FILTER (WHERE r."UPRN" IS NULL) AS retired,
                        count(*) FILTER (
                            WHERE r."UPRN" IS NOT NULL
                              AND (u."LATITUDE" IS DISTINCT FROM r."LATITUDE"
                                OR u."LONGITUDE" IS DISTINCT FROM r."LONGITUDE"
                                OR u."X_COORDINATE" IS DISTINCT FROM r."X_COORDINATE"
                                OR u."Y_COORDINATE" IS DISTINCT FROM r."Y_COORDINATE")
                        ) AS moved
                    FROM staging.ukhcs_table u
                    LEFT JOIN {_UKHCS_REFRESH_STAGE_TABLE} r
                        ON r."UPRN" = u."UPRN"
                    """
                )
            ).one()

        total_rows = connection.execute(
            text("SELECT count(*) FROM staging.ukhcs_table")
        ).scalar()

        connection.execute(text(f"DROP TABLE IF EXISTS {_UKHCS_REFRESH_STAGE_TABLE}"))
        _analyze(connection, ["staging.ukhcs_table"], log)

    _emit(
        log,
        f"refresh_ukhcs_table_from_extract: refreshed staging.ukhcs_table from {csv_fp.name} "
        f"({extract_rows} rows in the extract) -- inserted {inserted} new "
        f"UPRN(s), table now holds {total_rows}",
    )
    _emit(
        log,
        f"refresh_ukhcs_table_from_extract: left untouched {retired} UPRN(s) present in "
        "staging.ukhcs_table but retired from this extract (address_table."
        "uid and london_parcels.uprn reference them) and "
        f"{moved} UPRN(s) whose coordinates the extract revises (applying "
        "those would invalidate every already-computed spatial feature -- "
        "out of scope for this insert-only refresh)",
    )
    return inserted


def _production_column_list(connection, table: str) -> str:
    """
    Return the quoted, ordinal-ordered column list of a production table.

    Used instead of ``SELECT *`` so an append fails loudly if staging and
    production columns diverge.
    """
    columns = (
        connection.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'production' AND table_name = :table
                ORDER BY ordinal_position
                """
            ),
            {"table": table},
        )
        .scalars()
        .all()
    )
    if not columns:
        raise RuntimeError(f"production.{table} does not exist or has no columns")
    return ", ".join(f'"{c}"' for c in columns)


_UPRN_APPEND_SYNC_TABLES = ("address_table", "ppd_table")


def append_new_staging_rows_to_production(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    tables: Iterable[str] = _UPRN_APPEND_SYNC_TABLES,
) -> Dict[str, int]:
    """
    Append staging rows missing from each production table, by id.

    Used instead of a re-promote, which would erase certificates backfilled
    straight into ``production.epc_table``. Ids share one sequence, so the
    append is exact. Each production identity sequence is then advanced to
    its ``max(id)``.

    Returns
    -------
    dict of str to int
        Rows appended per table.
    """
    _ensure_production_schema(engine)
    appended: Dict[str, int] = {}
    with (
        _timed(log, "append_new_staging_rows_to_production"),
        engine.begin() as connection,
    ):
        apply_bulk_session_tuning(connection, cfg, log)
        for table in tables:
            columns = _production_column_list(connection, table)
            with _timed(log, f"append staging.{table} -> production.{table}"):
                appended[table] = connection.execute(
                    text(
                        f"""
                        INSERT INTO production.{table} ({columns})
                        SELECT {columns}
                        FROM staging.{table} s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM production.{table} p WHERE p.id = s.id
                        )
                        """
                    )
                ).rowcount
            connection.execute(
                text(
                    f"""
                    SELECT setval(
                        pg_get_serial_sequence('production.{table}', 'id'),
                        (SELECT COALESCE(max(id), 1) FROM production.{table})
                    )
                    """
                )
            )
        _analyze(connection, [f"production.{t}" for t in tables], log)

    for table, count in appended.items():
        _emit(log, f"append_new_staging_rows_to_production: appended {count} new row(s) to production.{table}")
    return appended


def rebuild_production_uprn_lookup(
    engine: Engine, cfg: Optional[Dict] = None, log: Optional[logging.Logger] = None
) -> int:
    """
    Rebuild ``production.uprn_lookup`` from the refreshed ``staging.ukhcs_table``.

    The DROP is CASCADE because the view joins this table; rebuild the view
    afterwards.

    Returns
    -------
    int
        The table's row count.
    """
    _ensure_production_schema(engine)
    with _timed(log, "rebuild_production_uprn_lookup"), engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS production.uprn_lookup CASCADE"))
        connection.execute(
            text(
                """
                CREATE TABLE production.uprn_lookup AS
                SELECT u."UPRN" AS uprn, a.id AS address_id
                FROM staging.ukhcs_table u
                INNER JOIN staging.address_table a ON a.uid = u.id
                """
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX idx_prod_uprn_lookup "
                "ON production.uprn_lookup (uprn)"
            )
        )
        row_count = connection.execute(
            text("SELECT count(*) FROM production.uprn_lookup")
        ).scalar()
        _analyze(connection, ["production.uprn_lookup"], log)
    _emit(log, f"rebuild_production_uprn_lookup: rebuilt production.uprn_lookup with {row_count} rows")
    return row_count
