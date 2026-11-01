"""
DDL only: the raw landing tables and the four ``staging.hpldn_*`` functions.

Everything comes from `hpldn.backend.migrations.landing_ddl`, so all landing
DDL precedes every load in 0003.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.migrations.landing_ddl import (
    HPLDN_FUNCTION_NAMES,
    HPLDN_FUNCTIONS_SQL,
    LANDING_TABLES,
    STEP_OWNED_LANDING_TABLES,
)
from hpldn.backend.scripts.migration_helpers import get_migration_logger

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

def upgrade() -> None:
    r"""
    Create the normalisation functions, then drop and recreate each landing table.

    The functions come first because 0003's ``codepoint_open`` index uses
    ``hpldn_pcnorm``. Tables are dropped rather than created if missing, so a
    stale column list cannot survive to garble 0003's positional ``\copy``.
    """
    log = get_migration_logger()
    connection = op.get_bind()

    connection.execute(text(HPLDN_FUNCTIONS_SQL))
    log.info(
        "0002: created the IMMUTABLE normalisation functions %s",
        ", ".join(f"staging.{name}()" for name in HPLDN_FUNCTION_NAMES),
    )

    for name, ddl in LANDING_TABLES:
        connection.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        connection.execute(text(ddl))
        log.info("0002: created %s", name)

    log.info(
        "0002: %d landing table(s) created. The %d table(s) whose loader "
        "owns their DDL are created by 0003: %s",
        len(LANDING_TABLES),
        len(STEP_OWNED_LANDING_TABLES),
        ", ".join(STEP_OWNED_LANDING_TABLES),
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain.NO_DOWNGRADE`."""
    no_downgrade()
