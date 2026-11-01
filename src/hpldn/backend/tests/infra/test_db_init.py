"""
Tests for the database bootstrap SQL in ``src/hpldn/backend/db/sql``.

Covers ``db_init.sql``, ``db_grants_post_migration.sql``,
``db_verify_init.sql`` and ``db_enable_api_login.sh``: idempotency, a
verifier that fails on broken state, the migration and API roles'
privileges, and the role settings and grants the serving host's rendered
SQL shares with them (`render_vps_sql`).

The database tests run on the ephemeral cluster (`pg_cluster`, marker
``pg``), in a scratch database with scratch roles of their own. The
from-empty replay to head is not a pytest test: ``scripts/ci/replay_chain.sh``.
"""

import os
import subprocess

import pytest
from sqlalchemy import create_engine, text

from hpldn.backend.common.constants import API_ROLE_WORK_MEM
from hpldn.backend.db import SQL_DIR, render_vps_sql

DB_INIT = SQL_DIR / "db_init.sql"
DB_GRANTS = SQL_DIR / "db_grants_post_migration.sql"
DB_VERIFY = SQL_DIR / "db_verify_init.sql"
DB_ENABLE_API_LOGIN = SQL_DIR / "db_enable_api_login.sh"
ROLE_SETTINGS = SQL_DIR / "fragments" / "api_role_settings.sql"

SCRATCH_DB = "hpldn_dbinit_test"
SCRATCH_ROLE = "hpldn_dbinit_role"
SCRATCH_API_ROLE = "hpldn_dbinit_api"


@pytest.fixture(scope="module")
def cluster_dsn(pg_cluster) -> str:
    """The cluster's superuser DSN on the ``postgres`` database."""
    return pg_cluster.url(pg_cluster.superuser, database="postgres").render_as_string(hide_password=False).replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def _psql(dsn: str, *args: str, check: bool = True, env=None) -> subprocess.CompletedProcess:
    """
    Run psql with ``ON_ERROR_STOP``, so a failed statement exits non-zero.

    ``db_verify_init.sql`` relies on that: each check raises on failure.
    """
    return subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "--no-psqlrc", *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_db_init(dsn: str, db_name: str) -> subprocess.CompletedProcess:
    return _psql(
        dsn,
        "-v", f"migration_role={SCRATCH_ROLE}",
        "-v", f"db_name={db_name}",
        "-v", f"api_role={SCRATCH_API_ROLE}",
        "-f", str(DB_INIT),
    )


def _drop_scratch(dsn: str, db_name: str) -> None:
    _psql(dsn, "-c", f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)', check=False)
    for role in (SCRATCH_API_ROLE, SCRATCH_ROLE):
        _psql(dsn, "-c", f'DROP OWNED BY "{role}" CASCADE', check=False)
        _psql(dsn, "-c", f'DROP ROLE IF EXISTS "{role}"', check=False)


@pytest.fixture(scope="module")
def bootstrapped_db(cluster_dsn):
    """Yield ``(superuser dsn, db name)`` for a scratch database with ``db_init.sql`` applied."""
    _drop_scratch(cluster_dsn, SCRATCH_DB)
    try:
        _run_db_init(cluster_dsn, SCRATCH_DB)
        yield cluster_dsn, SCRATCH_DB
    finally:
        _drop_scratch(cluster_dsn, SCRATCH_DB)


def _db_dsn(dsn: str, db_name: str) -> str:
    """Return `dsn` pointed at `db_name`."""
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{db_name}"


# The committed files.
def test_bootstrap_sql_files_exist():
    """Check the bootstrap SQL ships in ``db/sql``."""
    for path in (DB_INIT, DB_GRANTS, DB_VERIFY, DB_ENABLE_API_LOGIN, ROLE_SETTINGS):
        assert path.is_file(), f"{path} is missing"
        assert path.stat().st_size > 0, f"{path} is empty"


def test_role_settings_work_mem_is_the_constant():
    """The shared role settings use `API_ROLE_WORK_MEM`, the value `create_api_readonly_role` applies."""
    assert f"'{API_ROLE_WORK_MEM}'" in ROLE_SETTINGS.read_text()


def test_vps_sql_is_rendered_from_the_shared_fragments(tmp_path):
    """The serving host's api_role.sql/grants.sql inline the same fragments the local scripts include."""
    render_vps_sql.write(tmp_path)
    api_role = (tmp_path / "api_role.sql").read_text()
    grants = (tmp_path / "grants.sql").read_text()
    assert ROLE_SETTINGS.read_text() in api_role
    assert (SQL_DIR / "fragments" / "serving_db_lockdown.sql").read_text() in api_role
    assert (SQL_DIR / "fragments" / "api_grants.sql").read_text() in grants
    assert "\\ir fragments/api_grants.sql" in DB_GRANTS.read_text()
    assert "fragments/api_role_settings.sql" in DB_ENABLE_API_LOGIN.read_text()
    assert render_vps_sql.stale(tmp_path) == []
    (tmp_path / "grants.sql").write_text("-- edited by hand\n")
    assert render_vps_sql.stale(tmp_path) == ["grants.sql"]
    assert render_vps_sql.main(["--check", "--out-dir", str(tmp_path)]) == 1


@pytest.mark.parametrize("extension", ["postgis", "btree_gist", "pg_trgm"])
def test_db_init_installs_and_verifies_each_extension(extension):
    """
    Check every extension the chain needs is installed, verified and deployable.

    Migration 0021's ``install_pg_trgm`` calls ``deploy_pg_trgm_extension()``
    first; without it a fresh database cannot replay 0021, because the
    migration role cannot run ``CREATE EXTENSION`` itself.
    """
    init = DB_INIT.read_text()
    verify = DB_VERIFY.read_text()
    helper = (
        "deploy_spatial_extension" if extension == "postgis"
        else f"deploy_{extension}_extension"
    )
    assert f"CREATE EXTENSION IF NOT EXISTS {extension};" in init
    assert f"CREATE OR REPLACE FUNCTION public.{helper}()" in init
    assert f"REVOKE ALL ON FUNCTION public.{helper}()" in init
    assert f"GRANT EXECUTE ON FUNCTION public.{helper}() TO %I" in init
    assert f"'{extension}'" in verify
    assert f"'{helper}'" in verify


def test_db_init_contains_no_password():
    """
    Check ``db_init.sql`` sets no password and so stays safe to commit.

    Passwords are set out of band, and the API role is a NOLOGIN stub until
    `create_api_readonly_role` configures it.
    """
    body = DB_INIT.read_text().upper()
    assert "PASSWORD '" not in body
    assert "PASSWORD \"" not in body


# Idempotency.
def test_db_init_is_idempotent(bootstrapped_db):
    """
    Check a second run of ``db_init.sql`` is a clean no-op.

    It may be run on a database where every object already exists.
    """
    dsn, db_name = bootstrapped_db
    result = _run_db_init(dsn, db_name)
    assert result.returncode == 0, result.stderr
    assert "ERROR" not in result.stderr.upper(), result.stderr


def test_db_verify_init_passes_on_a_bootstrapped_database(bootstrapped_db):
    dsn, db_name = bootstrapped_db
    result = _psql(
        _db_dsn(dsn, db_name),
        "-v", f"migration_role={SCRATCH_ROLE}",
        "-v", f"api_role={SCRATCH_API_ROLE}",
        "-f", str(DB_VERIFY),
    )
    assert result.returncode == 0, result.stderr
    assert "all checks passed" in result.stdout


# The verifier must fail on a broken database.
@pytest.mark.parametrize(
    "break_sql,what",
    [
        (
            "REVOKE USAGE ON SCHEMA production FROM {api_role}",
            "the API role's USAGE on production",
        ),
        (
            "REVOKE EXECUTE ON FUNCTION public.deploy_spatial_extension() "
            "FROM {migration_role}",
            "EXECUTE on deploy_spatial_extension()",
        ),
        (
            "GRANT EXECUTE ON FUNCTION public.deploy_production_schema() TO PUBLIC",
            "the SECURITY DEFINER lockdown (granting it to PUBLIC)",
        ),
        (
            "DROP EXTENSION btree_gist",
            "the btree_gist extension migration 0014's comps index needs",
        ),
        (
            "REVOKE EXECUTE ON FUNCTION public.deploy_pg_trgm_extension() "
            "FROM {migration_role}",
            "EXECUTE on deploy_pg_trgm_extension()",
        ),
        (
            "DROP EXTENSION pg_trgm",
            "the pg_trgm extension migration 0021's address_search index needs",
        ),
    ],
)
def test_db_verify_init_fails_on_a_broken_database(bootstrapped_db, break_sql, what):
    """
    Check ``db_verify_init.sql`` fails when one bootstrap property is broken.

    Each case breaks one property, expects a non-zero exit, then repairs it by
    re-running ``db_init.sql``.
    """
    dsn, db_name = bootstrapped_db
    db_dsn = _db_dsn(dsn, db_name)
    statement = break_sql.format(
        api_role=SCRATCH_API_ROLE, migration_role=SCRATCH_ROLE
    )
    _psql(db_dsn, "-c", statement)
    try:
        result = _psql(
            db_dsn,
            "-v", f"migration_role={SCRATCH_ROLE}",
            "-v", f"api_role={SCRATCH_API_ROLE}",
            "-f", str(DB_VERIFY),
            check=False,
        )
        assert result.returncode != 0, (
            f"db_verify_init.sql passed after breaking {what} -- it is not "
            f"checking that property"
        )
    finally:
        _run_db_init(dsn, db_name)


# Role privileges.
def test_migration_role_has_connect_but_not_create(bootstrapped_db):
    """
    Check the migration role has CONNECT but not CREATE on the database.

    Withholding CREATE is what the ``deploy_*`` SECURITY DEFINER helpers and
    the schema-creation fallbacks exist for.
    """
    dsn, db_name = bootstrapped_db
    with create_engine(_db_dsn(dsn, db_name).replace("postgresql://", "postgresql+psycopg2://")).connect() as connection:
        can_connect, can_create = connection.execute(
            text(
                "SELECT has_database_privilege(:r, current_database(), 'CONNECT'), "
                "has_database_privilege(:r, current_database(), 'CREATE')"
            ),
            {"r": SCRATCH_ROLE},
        ).one()
    assert can_connect
    assert not can_create


def test_api_role_is_a_nologin_stub_with_no_password(bootstrapped_db):
    """
    Check the API role starts as a NOLOGIN stub with no password.

    It exists at bootstrap so default privileges can name it, and cannot
    authenticate until `create_api_readonly_role` sets its password.
    """
    dsn, db_name = bootstrapped_db
    with create_engine(_db_dsn(dsn, db_name).replace("postgresql://", "postgresql+psycopg2://")).connect() as connection:
        can_login, has_password = connection.execute(
            text(
                "SELECT r.rolcanlogin, a.rolpassword IS NOT NULL "
                "FROM pg_roles r LEFT JOIN pg_authid a ON a.oid = r.oid "
                "WHERE r.rolname = :r"
            ),
            {"r": SCRATCH_API_ROLE},
        ).one()
    assert not can_login
    assert not has_password


def test_future_production_tables_are_readable_by_the_api_role(bootstrapped_db):
    """
    Check tables created later in ``production`` are readable by the API role.

    The table is created under SET ROLE as the migration role, the role the
    ``ALTER DEFAULT PRIVILEGES`` rule names, with no further grant.
    """
    dsn, db_name = bootstrapped_db
    db_dsn = _db_dsn(dsn, db_name)
    _psql(
        db_dsn,
        "-c",
        f'SET ROLE "{SCRATCH_ROLE}"; '
        "CREATE TABLE production._grant_ordering_probe (id int)",
    )
    try:
        with create_engine(db_dsn.replace("postgresql://", "postgresql+psycopg2://")).connect() as connection:
            readable = connection.execute(
                text(
                    "SELECT has_table_privilege(:r, "
                    "'production._grant_ordering_probe', 'SELECT')"
                ),
                {"r": SCRATCH_API_ROLE},
            ).scalar_one()
            writable = connection.execute(
                text(
                    "SELECT has_table_privilege(:r, "
                    "'production._grant_ordering_probe', 'INSERT')"
                ),
                {"r": SCRATCH_API_ROLE},
            ).scalar_one()
        assert readable, (
            "a production table created after the bootstrap is not readable "
            "by the API role -- the ALTER DEFAULT PRIVILEGES rule in "
            "db_init.sql is not in effect"
        )
        assert not writable, "the API role must be read-only"
    finally:
        _psql(
            db_dsn, "-c", "DROP TABLE IF EXISTS production._grant_ordering_probe",
            check=False,
        )


def test_api_role_cannot_read_staging(bootstrapped_db):
    """Check the API role cannot read ``staging``, which is outside the serving contract."""
    dsn, db_name = bootstrapped_db
    with create_engine(_db_dsn(dsn, db_name).replace("postgresql://", "postgresql+psycopg2://")).connect() as connection:
        usage = connection.execute(
            text("SELECT has_schema_privilege(:r, 'staging', 'USAGE')"),
            {"r": SCRATCH_API_ROLE},
        ).scalar_one()
    assert not usage


def test_grants_post_migration_grants_the_api_role_and_the_verifier_passes(bootstrapped_db):
    """Check ``db_grants_post_migration.sql`` (through its ``\\ir`` of the shared fragment) leaves a verified database."""
    dsn, db_name = bootstrapped_db
    db_dsn = _db_dsn(dsn, db_name)
    _psql(db_dsn, "-c", f'SET ROLE "{SCRATCH_ROLE}"; CREATE TABLE production._grants_probe (id int)')
    try:
        result = _psql(
            db_dsn,
            "-c", f'SET ROLE "{SCRATCH_ROLE}"',
            "-v", f"api_role={SCRATCH_API_ROLE}",
            "-f", str(DB_GRANTS),
        )
        assert "complete" in result.stdout, result.stdout + result.stderr
        with create_engine(db_dsn.replace("postgresql://", "postgresql+psycopg2://")).connect() as connection:
            assert connection.execute(
                text("SELECT has_table_privilege(:r, 'production._grants_probe', 'SELECT')"),
                {"r": SCRATCH_API_ROLE},
            ).scalar_one()
        result = _psql(
            db_dsn,
            "-v", f"migration_role={SCRATCH_ROLE}",
            "-v", f"api_role={SCRATCH_API_ROLE}",
            "-f", str(DB_VERIFY),
        )
        assert "all checks passed" in result.stdout
    finally:
        _psql(db_dsn, "-c", "DROP TABLE IF EXISTS production._grants_probe", check=False)


def test_enable_api_login_applies_the_shared_role_settings(bootstrapped_db, pg_cluster):
    """
    Check ``db_enable_api_login.sh`` gives the API role LOGIN and the shared settings.

    psql reaches the cluster through ``PG*`` variables, as it would reach the
    local server over its socket; the post-check connects over TCP.
    """
    _, db_name = bootstrapped_db
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT=str(pg_cluster.port), PGUSER=pg_cluster.superuser)
    result = subprocess.run(
        ["bash", str(DB_ENABLE_API_LOGIN), "--db", db_name, "--role", SCRATCH_API_ROLE,
         "--host", "127.0.0.1", "--port", str(pg_cluster.port)],
        capture_output=True,
        text=True,
        env=env,
    )
    # Exit 2 is "password set, TCP post-check failed": production.london_houses
    # does not exist in a scratch database.
    assert result.returncode in (0, 2), result.stderr
    assert f"POSTGRES_API_USERNAME={SCRATCH_API_ROLE}" in result.stdout
    with pg_cluster.superuser_engine.connect() as connection:
        can_login, settings = connection.execute(
            text("SELECT rolcanlogin, rolconfig FROM pg_roles WHERE rolname = :r"), {"r": SCRATCH_API_ROLE}
        ).one()
    assert can_login
    assert {"statement_timeout=5s", f"work_mem={API_ROLE_WORK_MEM}", "default_transaction_read_only=on"} <= set(settings)
