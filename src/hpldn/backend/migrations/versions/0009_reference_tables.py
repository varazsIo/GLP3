"""
Load the four reference tables: Bank Rate, London stations, UKHPI and mortgage rates.

The only production tables not derived from transactions: small CSVs, each
drop-and-rebuild. ``published_date`` on ``ukhpi`` and ``mortgage_rates`` is
declared and derived at load, and ``create_ukhpi_table`` raises if any row
lacks one, since the view joins on publication date.

Revision ID: 0009
Revises: 0008
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Live row counts, logged as a drift signal; only emptiness raises.
_EXPECTED = {
    "production.boe_rate": 258,
    "production.london_stations": 659,
    "production.ukhpi": 378,
    "production.mortgage_rates": 235,
}


def _report(engine, log) -> None:
    """
    Log each table's row count against its reference count.

    Raises
    ------
    RuntimeError
        If a table is empty.
    """
    with engine.connect() as connection:
        for table, expected in _EXPECTED.items():
            rows = connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()
            if rows == 0:
                raise RuntimeError(
                    f"{table} is empty -- its source CSV loaded no rows"
                )
            log.info(
                "0009: %s holds %d row(s) (reference: %d, delta %+d)",
                table, rows, expected, rows - expected,
            )


def upgrade() -> None:
    """Load the four reference tables."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0009: production.boe_rate")
    steps.create_boe_rate_table(engine, cfg, log)

    log.info("0009: production.london_stations")
    steps.create_london_stations_table(engine, cfg, log)

    log.info("0009: production.ukhpi (1995-onward, published_date derived)")
    steps.create_ukhpi_table(engine, cfg, log)

    log.info("0009: production.mortgage_rates (published_date derived)")
    steps.create_mortgage_rates_table(engine, cfg, log)

    _report(engine, log)


def downgrade() -> None:
    """Refuse, for chain consistency; re-run upgrade() to reload the CSVs."""
    no_downgrade()
