"""
Carry the 0017/0018 repairs into production and add the certificates' property type.

``is_maisonette`` came only from ``staging.ukhpucl``, which ends in October
2024, so it was NULL for every later sale and every recent maisonette was
treated as a flat. ``staging.epc_domestic_history`` carries the
register's PROPERTY_TYPE but no EPC table kept it. This revision adds
``property_type`` to both EPC tables and backfills it; 0020's view falls back
to it.

In order:

1. ``property_type`` on ``staging.epc_table`` and ``production.epc_table``
   (declared on the staging model, so a re-promote keeps it);
2. append the new ``address_table`` and ``ppd_table`` rows by id, and copy
   the refreshed ``poststreet`` values across;
3. re-promote ``london_parcels`` (the view is dropped first; 0020 rebuilds
   it) and rebuild ``uprn_lookup``;
4. backfill certificates for the new addresses from the London history, then
   re-apply 0008's floor level, bounds and age-band normalisation;
5. backfill ``property_type`` on both EPC tables (history register first,
   then ``staging.ukhpucl`` for certificates it does not type), as sharded
   concurrent UPDATEs, then VACUUM both.

Step 2's append, the parcel re-promote and the ``uprn_lookup`` rebuild write
disjoint tables and run concurrently once the view is dropped.

``promote_staging_to_production`` is deliberately not used: it rebuilds
``production.epc_table`` from staging and would delete the ~1.3M history
certificates 0008 backfilled straight into production. Every step is guarded
on what it writes, so a re-run is cheap.

Revision ID: 0019
Revises: 0018
"""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from hpldn.backend.common.tuning import apply_bulk_session_tuning, detect_host_resources
from hpldn.backend.migrations.chain import (
    load_revision,
    no_downgrade,
    run_concurrently,
    vacuum_analyze,
)
from hpldn.backend.migrations.london_houses_view import DROP_VIEW_SQL
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

EPC_PROPERTY_TYPES = ("Flat", "Maisonette", "House", "Bungalow", "Park home")

# Certificates of sold addresses left without a property type; the history
# register covers 2008 onwards and ukhpucl the rest.
MAX_SOLD_CERTIFICATES_WITHOUT_PROPERTY_TYPE = 0.02

_EPC_TABLES = ("staging.epc_table", "production.epc_table")

_PROPERTY_TYPE_BUILD = "staging._epc_property_type_build"
_PROPERTY_TYPE_MISSING = "staging._epc_property_type_missing_uprns"
_PROPERTY_TYPE_BY_ADDRESS = "staging._epc_property_type_by_address"

# First source: the history register, newest lodgement per inspection.
_PROPERTY_TYPE_BUILD_SQL = f"""
CREATE UNLOGGED TABLE {_PROPERTY_TYPE_BUILD} AS
SELECT DISTINCT ON (h.uprn, h.inspection_date)
       h.uprn, h.inspection_date, btrim(h.property_type) AS property_type
FROM staging.epc_domestic_history h
WHERE h.uprn IS NOT NULL
  AND h.inspection_date IS NOT NULL
  AND btrim(h.property_type) = ANY(:property_types)
ORDER BY h.uprn, h.inspection_date, h.lodgement_datetime DESC NULLS LAST
"""

# UPRNs with a certificate the history did not type (~10k). Materialised and
# analysed so the fallback below plans an index lookup into ukhpucl rather
# than a 45 GB scan. uprn_lookup is rebuilt from staging by _propagate, so it
# maps both EPC tables' address ids.
_PROPERTY_TYPE_MISSING_SQL = f"""
CREATE UNLOGGED TABLE {_PROPERTY_TYPE_MISSING} AS
SELECT DISTINCT ul.uprn
FROM (
    SELECT address_id, inspection_date FROM production.epc_table
    UNION
    SELECT address_id, inspection_date FROM staging.epc_table
) e
JOIN production.uprn_lookup ul ON ul.address_id = e.address_id
WHERE e.inspection_date IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM {_PROPERTY_TYPE_BUILD} b
      WHERE b.uprn = ul.uprn AND b.inspection_date = e.inspection_date
  )
"""

# Second source: ukhpucl (the EPC-fused Price Paid extract, to October 2024).
# Read straight from the logged ukhpucl through idx_uk_uprn: the London slice
# staging.london_houses is UNLOGGED, so a crash recovery empties it.
_PROPERTY_TYPE_FALLBACK_SQL = f"""
INSERT INTO {_PROPERTY_TYPE_BUILD} (uprn, inspection_date, property_type)
SELECT DISTINCT ON (lh."UPRN", lh."INSPECTION_DATE"::date)
       lh."UPRN", lh."INSPECTION_DATE"::date, btrim(lh."PROPERTY_TYPE")
FROM {_PROPERTY_TYPE_MISSING} m
JOIN staging.ukhpucl lh ON lh."UPRN" = m.uprn
WHERE lh."INSPECTION_DATE" IS NOT NULL
  AND btrim(lh."PROPERTY_TYPE") = ANY(:property_types)
ORDER BY lh."UPRN", lh."INSPECTION_DATE"::date, lh."LODGEMENT_DATETIME" DESC NULLS LAST
ON CONFLICT (uprn, inspection_date) DO NOTHING
"""

# Keyed by address so each UPDATE chunk joins a small indexed table instead
# of hashing the 11M-row uprn_lookup once per connection.
_PROPERTY_TYPE_BY_ADDRESS_SQL = f"""
CREATE UNLOGGED TABLE {_PROPERTY_TYPE_BY_ADDRESS} AS
SELECT ul.address_id, b.inspection_date, b.property_type
FROM {_PROPERTY_TYPE_BUILD} b
JOIN production.uprn_lookup ul ON ul.uprn = b.uprn
"""

# One shard of the UPDATE; shards run on separate connections. Double mod
# because mod() keeps the sign of a negative id (as the floor_level backfill).
_PROPERTY_TYPE_UPDATE_SQL = f"""
UPDATE {{table}} e
SET property_type = b.property_type
FROM {_PROPERTY_TYPE_BY_ADDRESS} b
WHERE e.address_id = b.address_id
  AND e.inspection_date = b.inspection_date
  AND e.property_type IS DISTINCT FROM b.property_type
  AND mod(mod(e.address_id, :jobs) + :jobs, :jobs) = :chunk
  AND mod(mod(b.address_id, :jobs) + :jobs, :jobs) = :chunk
"""

# Fail instead of queueing behind a session reading the view (the API): a
# DROP waiting on its lock also blocks every later reader, and looks stuck.
VIEW_DROP_LOCK_TIMEOUT = "60s"

_PROPERTY_TYPE_GUARD_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE e.property_type IS NULL),
       count(*) FILTER (WHERE e.property_type = 'Maisonette')
FROM production.epc_table e
WHERE EXISTS (SELECT 1 FROM production.ppd_table p WHERE p.address_id = e.address_id)
"""

_POSTSTREET_SYNC_SQL = """
UPDATE production.address_table pa
SET poststreet = sa.poststreet
FROM staging.address_table sa
WHERE sa.id = pa.id
  AND pa.poststreet IS DISTINCT FROM sa.poststreet
"""

_PARITY_SQL = """
SELECT (SELECT count(*) FROM staging.address_table),
       (SELECT count(*) FROM production.address_table),
       (SELECT count(*) FROM staging.ppd_table),
       (SELECT count(*) FROM production.ppd_table),
       (SELECT count(*) FROM staging.london_parcels),
       (SELECT count(*) FROM production.london_parcels)
"""


def _add_property_type_columns(engine) -> None:
    """Add ``property_type`` to both EPC tables if missing."""
    with engine.begin() as connection:
        for table in _EPC_TABLES:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS property_type VARCHAR")
            )


def _drop_view(engine, log) -> None:
    """
    Drop ``production.london_houses``, failing fast if a reader holds it.

    Raises
    ------
    RuntimeError
        If the lock is not granted within `VIEW_DROP_LOCK_TIMEOUT`.
    """
    try:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL lock_timeout = '{VIEW_DROP_LOCK_TIMEOUT}'"))
            connection.execute(text(DROP_VIEW_SQL))
    except OperationalError as exc:
        raise RuntimeError(
            f"could not lock production.london_houses within {VIEW_DROP_LOCK_TIMEOUT}; "
            "another session (the API?) is reading it. Stop it and re-run "
            "`alembic upgrade head`: see pg_stat_activity / pg_locks."
        ) from exc
    log.info("0019: dropped production.london_houses (0020 rebuilds it)")


def _append_and_sync_streets(engine, cfg, log) -> None:
    """Append new address and sale rows, then copy refreshed streets onto production."""
    appended = steps.append_new_staging_rows_to_production(engine, cfg, log)
    log.info("0019: appended to production: %s", appended)

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        synced = connection.execute(text(_POSTSTREET_SYNC_SQL)).rowcount
        connection.execute(text("ANALYZE production.address_table"))
    log.info("0019: synced poststreet onto %d production.address_table row(s)", synced)


def _propagate(engine, cfg, log) -> None:
    """
    Append new rows, sync streets, re-promote parcels and rebuild ``uprn_lookup``.

    The three write disjoint production tables and read only staging, so
    they run at once. The view is dropped first: parcels are dropped without
    CASCADE and ``uprn_lookup`` with it, and two concurrent DROPs of the one
    view would race.
    """
    _drop_view(engine, log)
    run_concurrently(
        {
            "0019 append rows + sync poststreet": lambda: _append_and_sync_streets(
                engine, cfg, log
            ),
            "0019 promote london_parcels": lambda: (
                steps.promote_additional_serving_tables_to_production(engine, cfg, log)
            ),
            "0019 rebuild uprn_lookup": lambda: steps.rebuild_production_uprn_lookup(
                engine, cfg, log
            ),
        },
        log,
    )


def _backfill_certificates(engine, cfg, log) -> None:
    """Give the new addresses their history certificates, then re-apply 0008's clean-up."""
    steps.backfill_epc_table_from_london_history(engine, cfg, log)
    steps.add_floor_level_to_epc_table(engine, cfg, log)
    load_revision("0008")._bound_and_normalise_epc(engine, cfg, log)


def _backfill_property_type(engine, cfg, log) -> None:
    """
    Backfill ``property_type`` on both EPC tables and check sold addresses have it.

    Raises
    ------
    RuntimeError
        If more than `MAX_SOLD_CERTIFICATES_WITHOUT_PROPERTY_TYPE` of the
        certificates of sold addresses have none, or no maisonette is found.
    """
    build_tables = (_PROPERTY_TYPE_BUILD, _PROPERTY_TYPE_MISSING, _PROPERTY_TYPE_BY_ADDRESS)
    property_types = {"property_types": list(EPC_PROPERTY_TYPES)}
    # Committed before the UPDATE shards start, which read it from other connections.
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        for table in build_tables:
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        connection.execute(text(_PROPERTY_TYPE_BUILD_SQL), property_types)
        connection.execute(
            text(f"CREATE UNIQUE INDEX ON {_PROPERTY_TYPE_BUILD} (uprn, inspection_date)")
        )
        connection.execute(text(f"ANALYZE {_PROPERTY_TYPE_BUILD}"))
        history = connection.execute(
            text(f"SELECT count(*) FROM {_PROPERTY_TYPE_BUILD}")
        ).scalar_one()

        connection.execute(text(_PROPERTY_TYPE_MISSING_SQL))
        connection.execute(text(f"ANALYZE {_PROPERTY_TYPE_MISSING}"))
        missing_uprns = connection.execute(
            text(f"SELECT count(*) FROM {_PROPERTY_TYPE_MISSING}")
        ).scalar_one()
        fallback = connection.execute(
            text(_PROPERTY_TYPE_FALLBACK_SQL), property_types
        ).rowcount

        connection.execute(text(_PROPERTY_TYPE_BY_ADDRESS_SQL))
        connection.execute(
            text(f"CREATE INDEX ON {_PROPERTY_TYPE_BY_ADDRESS} (address_id, inspection_date)")
        )
        connection.execute(text(f"ANALYZE {_PROPERTY_TYPE_BY_ADDRESS}"))
    log.info(
        "0019: %d typed inspection(s) from epc_domestic_history; %d UPRN(s) had an "
        "untyped certificate, %d inspection(s) typed from ukhpucl",
        history, missing_uprns, fallback,
    )

    # UPDATE never runs in parallel, so shard it by address across connections,
    # half the cores per table, both tables at once.
    total_jobs = steps.get_spatial_join_jobs(detect_host_resources(log), log)
    jobs = max(1, total_jobs // len(_EPC_TABLES))

    def update_chunk(table: str, chunk: int) -> int:
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            return connection.execute(
                text(_PROPERTY_TYPE_UPDATE_SQL.format(table=table)),
                {"jobs": jobs, "chunk": chunk},
            ).rowcount

    try:
        with ThreadPoolExecutor(max_workers=jobs * len(_EPC_TABLES)) as pool:
            futures = {
                table: [pool.submit(update_chunk, table, chunk) for chunk in range(jobs)]
                for table in _EPC_TABLES
            }
            updated = {
                table: sum(future.result() for future in chunk_futures)
                for table, chunk_futures in futures.items()
            }
    finally:
        with engine.begin() as connection:
            for table in build_tables:
                connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
    for table in _EPC_TABLES:
        log.info(
            "0019: set property_type on %d %s row(s) (%d concurrent shards)",
            updated[table], table, jobs,
        )
    # The first run rewrites every certificate row; reclaim before 0020's
    # as-of lookups scan these tables.
    vacuum_analyze(engine, _EPC_TABLES, log)

    with engine.connect() as connection:
        certificates, missing, maisonettes = connection.execute(
            text(_PROPERTY_TYPE_GUARD_SQL)
        ).one()
    share = missing / certificates if certificates else 0.0
    log.info(
        "0019: %d certificate(s) of sold addresses, %d (%.2f%%) without a "
        "property_type, %d maisonette(s)",
        certificates, missing, 100 * share, maisonettes,
    )
    if share > MAX_SOLD_CERTIFICATES_WITHOUT_PROPERTY_TYPE:
        raise RuntimeError(
            f"{share:.1%} of sold addresses' certificates have no property_type "
            f"(want <= {MAX_SOLD_CERTIFICATES_WITHOUT_PROPERTY_TYPE:.0%}); the "
            "view's is_maisonette fallback would stay NULL for those sales"
        )
    if not maisonettes:
        raise RuntimeError(
            "no certificate of a sold address is a Maisonette -- the register's "
            "PROPERTY_TYPE vocabulary has moved"
        )


def _check_parity(engine, log) -> None:
    """
    Check staging and production agree after the propagation.

    Raises
    ------
    RuntimeError
        On any staging/production row-count mismatch.
    """
    with engine.connect() as connection:
        s_addr, p_addr, s_ppd, p_ppd, s_parcels, p_parcels = connection.execute(
            text(_PARITY_SQL)
        ).one()
    log.info(
        "0019: address_table %d/%d, ppd_table %d/%d, london_parcels %d/%d "
        "(staging/production)",
        s_addr, p_addr, s_ppd, p_ppd, s_parcels, p_parcels,
    )
    mismatched = [
        name
        for name, s, p in (
            ("address_table", s_addr, p_addr),
            ("ppd_table", s_ppd, p_ppd),
            ("london_parcels", s_parcels, p_parcels),
        )
        if s != p
    ]
    if mismatched:
        raise RuntimeError(
            f"staging and production disagree on {', '.join(mismatched)} after "
            "the propagation; every feature table 0020 rebuilds keys off "
            "production, so the repairs would be lost"
        )


def upgrade() -> None:
    """Propagate the repairs, then backfill certificates and property types."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    # The history backfill bounds on staging.epc_table_condensed (UNLOGGED).
    steps.restore_crash_reset_london_staging(engine, cfg, log)
    _add_property_type_columns(engine)
    _propagate(engine, cfg, log)
    _check_parity(engine, log)
    _backfill_certificates(engine, cfg, log)
    _backfill_property_type(engine, cfg, log)
    log.info(
        "0019: production.london_houses is dropped until 0020 rebuilds the "
        "features and the view"
    )


def downgrade() -> None:
    """Refuse; appended rows and backfilled certificates keep no snapshot of the prior state."""
    no_downgrade()
