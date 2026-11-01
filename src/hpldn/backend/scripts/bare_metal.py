"""
Provision and seed the HPLDN PostgreSQL database on a bare-metal host.

Resolves the target like the alembic environment (``HPLDN_DB_URL``,
``POSTGRES_DEVELOP_*``, ``POSTGRES_TESTING_*``), creates the database and
schema if missing, pre-flights the ``db_init.sql`` prerequisites and every
dataset, optionally bulk-loads ``staging.ukhpucl``, runs the migrations and
finalizes durability (`finalize_database`). Re-running resumes at the
revision that failed.

Usage::

    python -m hpldn.backend.scripts.bare_metal --check          # prerequisites and datasets
    python -m hpldn.backend.scripts.bare_metal --schema-only    # revision 0001 only
    python -m hpldn.backend.scripts.bare_metal --load-raw       # also bulk-load ukhpucl
    python -m hpldn.backend.scripts.bare_metal                  # schema and every seed step
    python -m hpldn.backend.scripts.bare_metal --revision 0003  # upgrade to a revision
    python -m hpldn.backend.scripts.bare_metal --skip-finalize  # stay UNLOGGED for more seeding
    python -m hpldn.backend.scripts.bare_metal --finalize       # only restore durability
"""

import argparse
import logging
import os
import subprocess
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import OperationalError

from hpldn.backend.common.db import get_migration_db_url
from hpldn.backend.common.tuning import get_vacuum_jobs
from hpldn.backend.common.utils import load_env_vars, setup_logger
from hpldn.backend.models.staging import app_base_meta
from hpldn.backend.common.config import get_cfg as _get_cfg
from hpldn.backend.common.config import get_repo_root

# StagingCopy loads staging.ukhpucl after SCHEMA_REVISION; SEED_REVISION
# loads every registered source.
SCHEMA_REVISION = "0001"

SEED_REVISION = "0003"

# Datasets come from seed_registry; the raw ukhpucl CSV and its header file are
# the extra inputs --load-raw needs, listed in migrations/inputs.yaml as
# required_by load-raw.


def get_cfg() -> dict:
    """Load ``staging.yaml`` through `hpldn.backend.common.config.get_cfg`."""
    return _get_cfg()


def get_bare_metal_logger(cfg: dict) -> logging.Logger:
    """Return the configured HPLDN logger, creating its log directory if needed."""
    log_fp = cfg.get("log_fp")
    if log_fp:
        Path(log_fp).parent.mkdir(parents=True, exist_ok=True)
    return setup_logger(cfg)


def check_postgres_reachable(engine, log: logging.Logger) -> str:
    """Run a query against the server and return its version string."""
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version()")).scalar()
    log.info(f"Connected to {engine.url.host}:{engine.url.port or 5432} — {version}")
    return version


def ensure_database(url: URL, log: logging.Logger) -> None:
    """
    Create ``url.database`` if it does not exist.

    Connects to the target first and only falls back to the ``postgres``
    maintenance database to CREATE DATABASE when it is missing.
    """
    engine = create_engine(url)
    try:
        with engine.connect():
            log.info(f"Database {url.database} already exists")
            return
    except OperationalError as err:
        if f'"{url.database}" does not exist' not in str(err):
            raise SystemExit(
                f"Cannot connect to PostgreSQL at {url.host} as {url.username}: {err}\n"
                "Is the local PostgreSQL service running "
                "(sudo systemctl status postgresql) and develop.env correct?"
            )
    finally:
        engine.dispose()

    admin_engine = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{url.database}"'))
        log.info(f"Created database {url.database}")
    finally:
        admin_engine.dispose()


def ensure_schemas(engine, log: logging.Logger) -> None:
    """
    Create the ``staging`` schema if missing.

    Existence is checked in ``pg_namespace`` first, because ``CREATE SCHEMA IF
    NOT EXISTS`` checks the CREATE privilege before short-circuiting.
    """
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :name"),
            {"name": app_base_meta.schema},
        ).scalar()
        if not exists:
            connection.execute(text(f"CREATE SCHEMA {app_base_meta.schema}"))
            log.info(f"Created schema {app_base_meta.schema} in {engine.url.database}")
            return
    log.info(f"Schema {app_base_meta.schema} already present in {engine.url.database}")


# db_init.sql prerequisites as (label, boolean SQL, remedy), matching
# src/hpldn/backend/db/sql/db_verify_init.sql.
DB_INIT_CHECKS = (
    (
        "extension postgis",
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser",
    ),
    (
        "extension btree_gist",
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser -- without it the comps "
        "12-month window stays a heap-fetch filter (migration 0014)",
    ),
    (
        "schema staging",
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'staging')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser",
    ),
    (
        "schema production",
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'production')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser",
    ),
    (
        "USAGE+CREATE on schema staging",
        "SELECT has_schema_privilege(current_user, 'staging', 'USAGE') "
        "AND has_schema_privilege(current_user, 'staging', 'CREATE')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser",
    ),
    (
        "deploy_spatial_extension()",
        "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND p.proname = 'deploy_spatial_extension' AND p.prosecdef)",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser -- pipeline_steps calls "
        "this helper from five places",
    ),
    (
        "deploy_btree_gist_extension()",
        "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND p.proname = 'deploy_btree_gist_extension' AND p.prosecdef)",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser",
    ),
    (
        "deploy_production_schema()",
        "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND p.proname = 'deploy_production_schema' AND p.prosecdef)",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser",
    ),
    (
        "role hpldn_api",
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hpldn_api')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser -- the NOLOGIN stub is "
        "what the production ALTER DEFAULT PRIVILEGES rule names",
    ),
    (
        "default SELECT on future production tables for hpldn_api",
        "SELECT EXISTS (SELECT 1 FROM pg_default_acl d "
        "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "WHERE n.nspname = 'production' AND d.defaclobjtype = 'r' "
        "AND array_to_string(d.defaclacl, ',') LIKE '%hpldn_api=r%')",
        "run src/hpldn/backend/db/sql/db_init.sql as superuser -- without this rule a "
        "feature table added later is invisible to the read API",
    ),
)


def check_db_init(engine, log: logging.Logger, strict: bool = True) -> list:
    """
    Check the ``src/hpldn/backend/db/sql/db_init.sql`` prerequisites are in place.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Target database.
    log : logging.Logger
        Logger for each check.
    strict : bool, optional
        Raise on any failure.

    Returns
    -------
    list
        Failed check labels and remedies.

    Raises
    ------
    SystemExit
        If `strict` and any check fails.
    """
    failures = []
    with engine.connect() as connection:
        for label, sql, remedy in DB_INIT_CHECKS:
            try:
                ok = bool(connection.execute(text(sql)).scalar())
            except Exception as err:
                ok, remedy = False, f"{remedy} (query error: {err})"
            if ok:
                log.debug(f"db_init prerequisite present: {label}")
            else:
                failures.append(f"{label}: MISSING — {remedy}")
    if not failures:
        log.info(
            f"All {len(DB_INIT_CHECKS)} src/hpldn/backend/db/sql/db_init.sql "
            f"prerequisites are present on {engine.url.database}"
        )
        return failures
    message = (
        f"{len(failures)} of {len(DB_INIT_CHECKS)} db_init.sql prerequisites "
        f"are missing on {engine.url.database}:\n  " + "\n  ".join(failures)
        + "\n\nBootstrap with:\n"
        "  sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres "
        "-f src/hpldn/backend/db/sql/db_init.sql\n"
        "then verify with:\n"
        f"  psql -v ON_ERROR_STOP=1 -d {engine.url.database} "
        "-f src/hpldn/backend/db/sql/db_verify_init.sql"
    )
    if strict:
        raise SystemExit(message)
    log.warning(message)
    return failures


def check_data_requirements(
    cfg: dict, revision: str, load_raw: bool, log: logging.Logger
) -> None:
    """
    Fail fast when a dataset the requested migrations read is missing.

    Every registered source is read by `SEED_REVISION`, so a target below it
    needs none and any other target needs all, checked through
    `hpldn.backend.migrations.seed_registry.check_sources`. ``load_raw`` adds
    the inputs ``inputs.yaml`` marks ``required_by: [load-raw]``.

    Raises
    ------
    SystemExit
        Listing every missing dataset.
    """
    from hpldn.backend.migrations import inputs
    from hpldn.backend.migrations.seed_registry import check_sources

    if load_raw:
        try:
            inputs.check(inputs.LOAD_RAW, cfg=cfg)
        except inputs.DataSourceMissing as err:
            raise SystemExit(f"Datasets required by --load-raw are missing:\n{err}") from err

    target = None if revision == "head" else int(revision)
    if target is not None and target < int(SEED_REVISION):
        log.info(
            "Target revision %s is below %s, which is where every dataset is "
            "loaded — skipping the dataset check",
            revision, SEED_REVISION,
        )
        return

    check_sources(cfg, log=log)


def run_migrations(db_url: URL, revision: str, log: logging.Logger) -> None:
    """
    Apply the alembic migrations up to `revision`.

    ``HPLDN_DB_URL`` is pinned first, so the alembic environment and every
    subprocess target the database this script provisioned.
    """
    os.environ["HPLDN_DB_URL"] = db_url.render_as_string(hide_password=False)
    alembic_cfg = AlembicConfig(str(get_repo_root().joinpath("src", "hpldn", "backend", "alembic.ini")))
    log.info(f"Applying alembic migrations up to '{revision}'...")
    alembic_command.upgrade(alembic_cfg, revision)
    log.info(f"Migrations up to '{revision}' applied")


def run_polars_copy(db_url: URL, log: logging.Logger) -> None:
    """Bulk-load the raw UCL CSV into ``staging.ukhpucl`` with ``StagingCopy``."""
    os.environ["HPLDN_DB_URL"] = db_url.render_as_string(hide_password=False)
    from hpldn.backend.preprocessing.staging import StagingCopy

    log.info("Bulk-loading raw UCL csv into staging.ukhpucl (polars copy)...")
    StagingCopy(copy_type="polars", log=log)


_UNLOGGED_TABLES_SQL = text(
    "SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relpersistence = 'u' AND c.relkind = 'r' "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "ORDER BY 1"
)


def finalize_database(db_url: URL, log: logging.Logger, keep_unlogged: bool = False) -> None:
    """
    Restore durable settings once seeding is done.

    In order: set every UNLOGGED table back to LOGGED (skipped with
    `keep_unlogged`), check the server's ``synchronous_commit`` default is
    back on, then ANALYZE in parallel with ``vacuumdb``. An UNLOGGED table is
    truncated by crash recovery, so this must run before the database is
    relied on. The seeding steps lower ``synchronous_commit`` per session
    only; a server-wide ``off`` was set by hand and is reported, not changed.
    """
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            unlogged = connection.execute(_UNLOGGED_TABLES_SQL).scalars().all()
            if keep_unlogged:
                if unlogged:
                    log.warning(
                        "--keep-unlogged set, leaving these tables UNLOGGED (crash "
                        "recovery will TRUNCATE them): " + ", ".join(unlogged)
                    )
            else:
                for table in unlogged:
                    log.info(f"SET LOGGED {table} (rewrites the table)...")
                    connection.execute(text(f"ALTER TABLE {table} SET LOGGED"))
            setting, source = connection.execute(
                text("SELECT reset_val, source FROM pg_settings WHERE name = 'synchronous_commit'")
            ).one()
    finally:
        engine.dispose()
    if setting != "on":
        log.warning(
            f"The server's synchronous_commit default is '{setting}' (from {source}); "
            "set it back to 'on' in postgresql.conf and reload before relying on this database."
        )

    jobs = get_vacuum_jobs(log=log)
    command = ["vacuumdb", "--analyze-only", f"--jobs={jobs}", "-d", db_url.database]
    if db_url.host:
        command += ["-h", db_url.host]
    if db_url.port:
        command += ["-p", str(db_url.port)]
    if db_url.username:
        command += ["-U", db_url.username]
    env = dict(os.environ)
    if db_url.password:
        # In the child's environment only, never argv.
        env["PGPASSWORD"] = str(db_url.password)
    log.info(f"Running ANALYZE across {db_url.database} ({jobs} parallel jobs)...")
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True)
    except FileNotFoundError:
        log.warning("vacuumdb is not on PATH; run ANALYZE by hand")
        return
    if result.returncode != 0:
        log.warning(f"vacuumdb --analyze-only failed: {result.stderr.strip()}")
    else:
        log.info("Statistics refreshed.")


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Provision the local (bare-metal) HPLDN PostgreSQL database "
        "and run the alembic seeding migrations against it."
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Database to provision/seed (default: POSTGRES_DEVELOP_DATABASE "
        "-- hpdbldn -- from develop.env, or the database in HPLDN_DB_URL)",
    )
    parser.add_argument(
        "--revision",
        default="head",
        help="Alembic revision to upgrade to (default: head)",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help=f"Stop after the schema revision ({SCHEMA_REVISION}) — no data seeding",
    )
    parser.add_argument(
        "--load-raw",
        action="store_true",
        help="Bulk-load the raw UCL csv into staging.ukhpucl (StagingCopy, "
        "polars mode) between the schema revision and the seeding revisions",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify connectivity, the database, the schemas, the "
        "src/hpldn/backend/db/sql/db_init.sql prerequisites and every dataset the "
        "migrations read, then exit without seeding",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Only run the post-seeding step: set every UNLOGGED table back to "
        "LOGGED, check synchronous_commit and ANALYZE the database in parallel. "
        "A full run does this automatically at the end",
    )
    parser.add_argument(
        "--skip-finalize",
        action="store_true",
        help="Leave UNLOGGED tables in place after the migrations — use when "
        "more seeding steps are still to come, then run --finalize once at the end",
    )
    parser.add_argument(
        "--keep-unlogged",
        action="store_true",
        help="During finalize, leave UNLOGGED tables as they are, skipping the "
        "(slow) rewrites. Crash recovery TRUNCATES an UNLOGGED table, so only "
        "use this for scratch data",
    )
    parser.add_argument(
        "--skip-data-check",
        action="store_true",
        help="Skip the up-front existence check of the datasets the seeding "
        "migrations read from configuration/staging.yaml",
    )
    return parser


def main() -> None:
    """
    Provision and seed the local database.

    The ``db_init.sql`` check is strict only under ``--check``; otherwise the
    schema creation and migration fallbacks may still succeed.
    """
    args = build_parser().parse_args()

    load_env_vars()
    cfg = get_cfg()
    log = get_bare_metal_logger(cfg)

    db_url = get_migration_db_url(log)
    if args.database:
        db_url = db_url.set(database=args.database)

    if args.finalize:
        finalize_database(db_url, log, keep_unlogged=args.keep_unlogged)
        return

    ensure_database(db_url, log)
    engine = create_engine(db_url)
    try:
        check_postgres_reachable(engine, log)
        check_db_init(engine, log, strict=args.check)
        ensure_schemas(engine, log)
    finally:
        engine.dispose()

    if args.check:
        if not args.skip_data_check:
            check_data_requirements(cfg, args.revision, args.load_raw, log)
        log.info(
            "--check set: connectivity, database, schemas, db_init.sql "
            "prerequisites and every registered dataset verified."
        )
        return

    if not args.skip_data_check and not args.schema_only:
        check_data_requirements(cfg, args.revision, args.load_raw, log)

    log.info(f"Applying schema revision {SCHEMA_REVISION}...")
    run_migrations(db_url, SCHEMA_REVISION, log)

    if args.schema_only:
        log.info("--schema-only set, stopping after schema creation.")
        return

    if args.load_raw:
        run_polars_copy(db_url, log)

    run_migrations(db_url, args.revision, log)
    log.info("Bare-metal provisioning and seeding complete.")

    if args.skip_finalize:
        log.warning(
            "--skip-finalize set: UNLOGGED tables are still UNLOGGED. Run "
            "'python -m hpldn.backend.scripts.bare_metal --finalize' before relying "
            "on the database."
        )
        return
    finalize_database(db_url, log, keep_unlogged=args.keep_unlogged)


if __name__ == "__main__":
    main()
