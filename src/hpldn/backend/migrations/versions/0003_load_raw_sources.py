"""
Load every raw source through `hpldn.backend.migrations.seed_registry`.

Checks ``staging.ukhpucl`` is loaded, then runs `run_sources`, which checks
every file exists before loading and truncates targets so a re-run after a
failure is safe. INSPIRE parcels load in 0005.

Revision ID: 0003
Revises: 0002
"""

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.migrations.seed_registry import EXTERNAL_SOURCES, SOURCES, run_sources
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# staging.ukhpucl is loaded out of band before this revision; refuse to build
# the rest of the chain on an empty one.
MIN_UKHPUCL_ROWS = 1_000_000


def _assert_ukhpucl_loaded(engine, log) -> None:
    """
    Fail now, not hours into 0005, if ``StagingCopy`` has not loaded ukhpucl.

    Raises
    ------
    RuntimeError
        If ``staging.ukhpucl`` holds fewer than `MIN_UKHPUCL_ROWS` rows.
    """
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT count(*) FROM staging.ukhpucl")
        ).scalar_one()
    if rows < MIN_UKHPUCL_ROWS:
        raise RuntimeError(
            f"staging.ukhpucl holds {rows} row(s), expected at least "
            f"{MIN_UKHPUCL_ROWS:,}. It is bulk-loaded OUT OF BAND by "
            "StagingCopy between revisions 0001 and 0002 -- run:\n"
            "    python -m hpldn.backend.scripts.bare_metal --load-raw\n"
            "Everything from 0005 onward derives from this table."
        )
    log.info("0003: staging.ukhpucl holds %d row(s)", rows)


def upgrade() -> None:
    """Load every registered raw source."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    _assert_ukhpucl_loaded(engine, log)

    log.info(
        "0003: loading %d registered source(s). Not loaded here: %s",
        len(SOURCES),
        "; ".join(EXTERNAL_SOURCES),
    )
    results = run_sources(engine, cfg, log)

    log.info(
        "0003: all sources loaded -- %s",
        ", ".join(f"{name}={rows}" for name, rows in results.items()),
    )


def downgrade() -> None:
    """Refuse; this revision loads 48M rows across 17 sources."""
    no_downgrade()
