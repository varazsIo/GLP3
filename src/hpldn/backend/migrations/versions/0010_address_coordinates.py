"""
Build ``production.address_coordinates``: a WGS84 latitude and longitude per address.

Resolved through ``staging.ukhcs_table``; every later spatial feature reads
it.

Revision ID: 0010
Revises: 0009
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# Coverage below this means the ukhcs_table join is failing.
MIN_COORDINATE_COVERAGE = 0.95


def upgrade() -> None:
    """
    Build the coordinates table and check its coverage.

    Raises
    ------
    RuntimeError
        If fewer than `MIN_COORDINATE_COVERAGE` of addresses get a coordinate.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0010: production.address_coordinates")
    steps.create_address_coordinates_table(engine, cfg, log)

    with engine.connect() as connection:
        rows, with_coords, addresses = connection.execute(
            text(
                """
                SELECT count(*),
                       count(*) FILTER (
                           WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                       ),
                       (SELECT count(*) FROM production.address_table)
                FROM production.address_coordinates
                """
            )
        ).one()

    coverage = with_coords / addresses if addresses else 0.0
    log.info(
        "0010: production.address_coordinates holds %d row(s), %d with a "
        "coordinate, against %d address_table row(s) -- %.1f%% coverage",
        rows, with_coords, addresses, coverage * 100.0,
    )
    if coverage < MIN_COORDINATE_COVERAGE:
        raise RuntimeError(
            f"only {coverage:.1%} of production.address_table rows have a "
            f"coordinate (want >= {MIN_COORDINATE_COVERAGE:.0%}). Every "
            "spatial feature built after this revision -- comps, "
            "neighbourhood, station, river, PTAL, LSOA -- reads this table, "
            "so a broken join here degrades all of them silently."
        )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
