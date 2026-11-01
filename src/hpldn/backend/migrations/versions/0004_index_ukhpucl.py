"""
Index ``staging.ukhpucl`` after its bulk load.

Indexing once after the load is cheaper than maintaining indexes per inserted
row; builds run concurrently, sized by ``tuning.get_index_jobs``.

Revision ID: 0004
Revises: 0003
"""

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Build the ukhpucl indexes."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    log.info("0004: building staging.ukhpucl indexes")
    steps.create_ukhpucl_indexes(engine, cfg, log)


def downgrade() -> None:
    """Refuse, for chain consistency; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
