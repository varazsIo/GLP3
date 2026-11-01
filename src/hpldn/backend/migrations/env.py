"""
Alembic environment for the HPLDN seeding migrations.

The target database is `hpldn.backend.common.db.get_migration_db_url`'s:
``HPLDN_DB_URL``, else ``POSTGRES_DEVELOP_*`` (hpdbldn), else
``POSTGRES_TESTING_*``.

Before an online upgrade, every input the pending revisions read is checked
against ``migrations/inputs.yaml`` (`check_pending_inputs`), so a missing file
fails at minute 0 rather than hours into the chain. ``HPLDN_SKIP_INPUTS_CHECK=1``
turns that off (for a ``stamp``, say).
"""

import os
from logging.config import fileConfig

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine, pool

from hpldn.backend.common.db import get_migration_db_url
from hpldn.backend.migrations import inputs
from hpldn.backend.models.staging import AppBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = AppBase.metadata


def get_database_url() -> str:
    """Return the target URL as a string, password included."""
    url = os.environ.get("HPLDN_DB_URL")
    if url:
        return url
    return get_migration_db_url().render_as_string(hide_password=False)


def pending_revisions(connection) -> list:
    """
    Return the revisions an upgrade to the requested target would apply.

    Empty for a command with no target (``current``), and when the target is
    not above the database's current revision (a downgrade or a no-op).
    """
    try:
        target = context.get_revision_argument() or "heads"
    except KeyError:
        return []
    script = ScriptDirectory.from_config(config)
    current = MigrationContext.configure(connection).get_current_heads()
    try:
        revisions = script.iterate_revisions(target, current or "base")
        return [revision.revision for revision in revisions]
    except CommandError:
        return []


def check_pending_inputs(connection) -> None:
    """Fail before the first revision runs if an input the pending revisions read is missing."""
    if os.environ.get("HPLDN_SKIP_INPUTS_CHECK") == "1":
        return
    pending = pending_revisions(connection)
    if pending:
        inputs.check(pending, cfg=inputs.cfg_or_none())


def run_migrations_offline() -> None:
    """
    Run migrations in offline mode, emitting SQL without a connection.

    Only pure-DDL revisions support this; seeding revisions need a live
    connection.
    """
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database, one transaction per revision."""
    connectable = create_engine(get_database_url(), poolclass=pool.NullPool)

    # A separate connection, so the probe's implicit transaction cannot wrap
    # the per-migration transactions alembic opens on its own.
    with connectable.connect() as probe:
        check_pending_inputs(probe)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
