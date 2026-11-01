"""
Baseline: the `AppBase` spine in ``staging``, at head column state.

Creates the ten spine tables through ``migration_metadata()`` (the chain's
only ``create_all``), pinned to the literal ``staging``. ``staging.ukhpucl``
is left empty and bulk-loaded by ``StagingCopy`` before 0002. The only
reversible revision.

Revision ID: 0001
Revises:
"""

from alembic import op
from sqlalchemy import text

from hpldn.backend.models.staging import MIGRATION_SCHEMA, migration_metadata
from hpldn.backend.scripts.migration_helpers import get_migration_logger
from hpldn.backend.scripts.pipeline_steps import _ensure_production_schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

BOOTSTRAP_HINT = (
    "sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres "
    "-f src/hpldn/backend/db/sql/db_init.sql"
)


def _ensure_staging_schema(connection, log) -> None:
    """
    Create ``staging`` if ``src/hpldn/backend/db/sql/db_init.sql`` has not been run.

    Tries an existing schema, ``CREATE SCHEMA``, then the SECURITY DEFINER
    helper, and fails naming the bootstrap command. ``CREATE SCHEMA IF NOT
    EXISTS`` is avoided because it checks the privilege before short-circuiting.
    """
    exists = connection.execute(
        text("SELECT 1 FROM pg_namespace WHERE nspname = :name"),
        {"name": MIGRATION_SCHEMA},
    ).scalar()
    if exists:
        return
    can_create = connection.execute(
        text(
            "SELECT has_database_privilege(current_user, current_database(), "
            "'CREATE')"
        )
    ).scalar()
    if not can_create:
        raise RuntimeError(
            f"schema {MIGRATION_SCHEMA} does not exist and the current role "
            f"cannot create it. Run the bootstrap first:\n  {BOOTSTRAP_HINT}"
        )
    connection.execute(text(f"CREATE SCHEMA {MIGRATION_SCHEMA}"))
    log.warning(
        "0001: created schema %s as a fallback -- this host was not "
        "bootstrapped with src/hpldn/backend/db/sql/db_init.sql, so the ownership "
        "split, the default privileges and the deploy_* helpers are all "
        "missing. Run: %s",
        MIGRATION_SCHEMA,
        BOOTSTRAP_HINT,
    )


def upgrade() -> None:
    """
    Create the spine tables, checking both schemas first.

    ``production`` is asserted now, although first needed at 0007, so a
    missing bootstrap fails in seconds rather than hours into the seed.
    """
    log = get_migration_logger()
    connection = op.get_bind()

    _ensure_staging_schema(connection, log)
    _ensure_production_schema(connection.engine)

    metadata = migration_metadata()
    metadata.create_all(connection, checkfirst=True)

    tables = sorted(t.split(".", 1)[-1] for t in metadata.tables)
    log.info(
        "0001: created %d AppBase tables in schema %s at head column state "
        "(%s). staging.ukhpucl is intentionally EMPTY -- StagingCopy "
        "bulk-loads it between this revision and 0002 "
        "(python -m hpldn.backend.scripts.bare_metal --load-raw)",
        len(tables),
        MIGRATION_SCHEMA,
        ", ".join(tables),
    )


def downgrade() -> None:
    """Drop the `AppBase` tables, which are still empty at this revision."""
    log = get_migration_logger()
    connection = op.get_bind()
    migration_metadata().drop_all(connection, checkfirst=True)
    log.info("0001 downgrade: dropped the AppBase tables from %s", MIGRATION_SCHEMA)
