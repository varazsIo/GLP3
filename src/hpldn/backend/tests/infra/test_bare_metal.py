"""
Tests for bare-metal PostgreSQL provisioning, seeding and finalizing (`scripts.bare_metal`).

Three layers: offline checks of the CLI, ``develop.env``, ``staging.yaml``,
URL resolution and dataset checks; read-only checks against hpdbldn
(`hpdbldn_ro`), the database `bare_metal` builds; and provisioning, the
schema revision and `finalize_database` against the ephemeral cluster
(`pg_cluster`, marker ``pg``).
"""

import logging
import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from hpldn.backend.common.db import get_migration_db_url
from hpldn.backend.common.utils import load_env_vars
from hpldn.backend.models.staging import AppBase, app_base_meta
from hpldn.backend.scripts import bare_metal

REPO_ROOT = Path(os.environ.get("HPLDN_DIR_FP", Path(__file__).resolve().parents[5]))
DEVELOP_ENV_FP = REPO_ROOT.joinpath("develop.env")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", None}

log = logging.getLogger("test_bare_metal")


def _local_db_url():
    """Return the URL `bare_metal` resolves (``POSTGRES_DEVELOP_*`` first), ignoring ``HPLDN_DB_URL``."""
    saved = os.environ.pop("HPLDN_DB_URL", None)
    try:
        load_env_vars()
        return get_migration_db_url()
    finally:
        if saved is not None:
            os.environ["HPLDN_DB_URL"] = saved


def test_cli_exposes_required_flags():
    """Check the CLI supports the documented provisioning options."""
    parser = bare_metal.build_parser()
    flags = {option for action in parser._actions for option in action.option_strings}
    for expected in (
        "--database",
        "--revision",
        "--schema-only",
        "--load-raw",
        "--check",
        "--skip-data-check",
        "--finalize",
        "--skip-finalize",
        "--keep-unlogged",
    ):
        assert expected in flags, f"bare_metal CLI is missing {expected}"


def test_develop_env_has_required_connection_keys():
    """Check ``develop.env`` defines every variable `get_migration_db_url` reads."""
    assert DEVELOP_ENV_FP.exists(), f"develop.env not found at {DEVELOP_ENV_FP}"
    keys = {
        line.split("=", 1)[0].strip()
        for line in DEVELOP_ENV_FP.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    required = {
        "SQLALCHEMY_ENGINE",
        "POSTGRES_DEVELOP_USERNAME",
        "POSTGRES_DEVELOP_PASSWORD",
        "POSTGRES_DEVELOP_HOST",
        "POSTGRES_DEVELOP_DATABASE",
        "DB_SCHEMA",
    }
    missing = required - keys
    assert not missing, f"develop.env is missing {missing}"


def test_staging_yaml_loads_and_every_seed_source_is_a_manifest_input():
    """
    Check ``staging.yaml`` loads and every dataset the pre-flight checks is in ``inputs.yaml``.

    Covers `seed_registry.SOURCES` and the ``load-raw`` inputs; paths live in
    the manifest, so ``staging.yaml`` needs no key for them.
    """
    from hpldn.backend.migrations import inputs
    from hpldn.backend.migrations.seed_registry import SOURCES

    cfg = bare_metal.get_cfg()
    assert isinstance(cfg, dict) and cfg, "staging.yaml is empty or missing"
    manifest = inputs.load_manifest()
    assert inputs.select(inputs.LOAD_RAW), "inputs.yaml marks no input required_by load-raw"
    assert SOURCES, "the seed registry declares no sources"
    for source in SOURCES:
        assert source.cfg_key in manifest, (
            f"inputs.yaml is missing {source.cfg_key} ({source.description})"
        )


def test_db_url_targets_local_host():
    """Check the resolved URL points at this machine."""
    assert DEVELOP_ENV_FP.exists(), f"develop.env not found at {DEVELOP_ENV_FP}"
    url = _local_db_url()
    assert url.host in LOCAL_HOSTS, (
        "POSTGRES_DEVELOP_HOST in develop.env must be localhost for the "
        "bare-metal deployment"
    )
    assert url.database, "no database configured in develop.env"


def test_schema_and_data_revisions_exist_in_migration_chain():
    """Check `SCHEMA_REVISION` and `SEED_REVISION` are real revisions."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_cfg = Config(str(REPO_ROOT.joinpath("src", "hpldn", "backend", "alembic.ini")))
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    known = {rev.revision for rev in script_dir.walk_revisions()}
    assert bare_metal.SCHEMA_REVISION in known
    assert bare_metal.SEED_REVISION in known
    assert int(bare_metal.SEED_REVISION) > int(bare_metal.SCHEMA_REVISION)


def test_check_data_requirements_fails_on_missing_dataset(tmp_path):
    """
    Check a missing dataset aborts before seeding, raising `DataSourceMissing`.

    The path is overridden under ``data``, which `require_data_path` honours
    ahead of the manifest path.
    """
    from hpldn.backend.scripts.migration_helpers import DataSourceMissing

    cfg = {"data": {"osopenuprn_csv_fp": str(tmp_path.joinpath("nope.csv"))}}
    with pytest.raises(DataSourceMissing, match="osopenuprn_csv_fp"):
        bare_metal.check_data_requirements(cfg, "head", False, log)


def test_check_data_requirements_ignores_later_revisions(tmp_path):
    """Check a target below `SEED_REVISION` (0002, since the comparison is strict) needs no dataset."""
    cfg = {"data": {"osopenuprn_csv_fp": str(tmp_path.joinpath("nope.csv"))}}
    bare_metal.check_data_requirements(cfg, "0002", False, log)


def test_check_data_requirements_checks_load_raw_inputs(tmp_path):
    """Check ``--load-raw`` requires the raw UCL CSV (a ``load-raw`` input) whatever the target revision."""
    existing = tmp_path.joinpath("uprn.csv")
    existing.write_text("UPRN\n")
    cfg = {
        "data": {
            "osopenuprn_csv_fp": str(existing),
            "ukhpucl_csv_fp": str(tmp_path.joinpath("nope.csv")),
        }
    }
    with pytest.raises(SystemExit, match="ukhpucl_csv_fp"):
        bare_metal.check_data_requirements(cfg, "0002", True, log)


def test_postgres_accepts_connections(hpdbldn_ro):
    """Check a query round-trips against the local server."""
    version = bare_metal.check_postgres_reachable(hpdbldn_ro, log)
    assert version is not None and "PostgreSQL" in version


def test_db_init_prerequisites_are_in_place_on_hpdbldn(hpdbldn_ro):
    """Check every ``db_init.sql`` prerequisite `bare_metal --check` verifies holds on hpdbldn."""
    assert bare_metal.check_db_init(hpdbldn_ro, log, strict=True) == []


def _known_revisions():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_cfg = Config(str(REPO_ROOT.joinpath("src", "hpldn", "backend", "alembic.ini")))
    return {rev.revision for rev in ScriptDirectory.from_config(alembic_cfg).walk_revisions()}


def test_migrations_stamped_revision_is_known(hpdbldn_ro):
    """Check hpdbldn's ``alembic_version`` holds a revision in the chain."""
    with hpdbldn_ro.connect() as conn:
        applied = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert applied in _known_revisions(), f"alembic_version contains unknown revision {applied!r}"


def test_ensure_database_creates_once_and_is_then_a_no_op(pg_cluster):
    """Check `ensure_database` creates a missing database and leaves an existing one alone."""
    url = pg_cluster.url(pg_cluster.superuser, database="hpldn_bare_metal_test")
    bare_metal.ensure_database(url, log)
    bare_metal.ensure_database(url, log)
    with pg_cluster.superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM pg_database WHERE datname = 'hpldn_bare_metal_test'")
        ).scalar() == 1


def test_ensure_schemas_is_idempotent(pg_cluster):
    """
    Check `ensure_schemas` leaves pre-provisioned schemas in place.

    Run as the migration role, which has no CREATE on the database: the
    regression was issuing CREATE SCHEMA, which fails for such a role even
    when the schema exists.
    """
    engine = pg_cluster.migration_engine
    bare_metal.ensure_schemas(engine, log)
    bare_metal.ensure_schemas(engine, log)
    with engine.connect() as conn:
        schemas = {
            row[0]
            for row in conn.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname IN ('public', :app_schema)"),
                {"app_schema": app_base_meta.schema},
            )
        }
    assert schemas == {app_base_meta.schema, "public"}


def test_schema_migration_provisions_tables(pg_cluster):
    """Check `run_migrations` applies the schema revision and every ORM table exists."""
    saved = os.environ.get("HPLDN_DB_URL")
    try:
        bare_metal.run_migrations(pg_cluster.url(pg_cluster.migration_role), bare_metal.SCHEMA_REVISION, log)
    finally:
        if saved is None:
            os.environ.pop("HPLDN_DB_URL", None)
        else:
            os.environ["HPLDN_DB_URL"] = saved
    inspector = inspect(pg_cluster.migration_engine)
    assert inspector.has_table("alembic_version")
    missing = {t.name for t in AppBase.metadata.sorted_tables} - set(
        inspector.get_table_names(schema=app_base_meta.schema)
    )
    assert not missing, f"schema revision tables missing: {missing}"


def test_finalize_sets_unlogged_tables_back_to_logged(pg_cluster):
    """Check `finalize_database` turns every UNLOGGED table LOGGED, unless asked to keep them."""
    engine = pg_cluster.migration_engine
    with engine.begin() as conn:
        conn.execute(text("CREATE UNLOGGED TABLE staging._finalize_probe (id int)"))
    url = pg_cluster.url(pg_cluster.migration_role)
    try:
        bare_metal.finalize_database(url, log, keep_unlogged=True)
        persistence = "SELECT relpersistence FROM pg_class WHERE oid = 'staging._finalize_probe'::regclass"
        with engine.connect() as conn:
            assert conn.execute(text(persistence)).scalar() == "u"
        bare_metal.finalize_database(url, log)
        with engine.connect() as conn:
            assert conn.execute(text(persistence)).scalar() == "p"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS staging._finalize_probe"))
