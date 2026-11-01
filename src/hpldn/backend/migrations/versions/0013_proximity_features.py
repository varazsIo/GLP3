"""
Build the station and river proximity features and the green-space reference tables.

The builders read ``production.address_coordinates`` and
``production.ppd_table``, never the view, which keeps the chain acyclic. Green
spaces have no consumer yet and stay in staging.

Revision ID: 0013
Revises: 0012
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Every London property has a nearest station; lower coverage means a broken join.
MIN_STATION_COVERAGE = 0.95


def upgrade() -> None:
    """
    Build the proximity tables and load green spaces.

    Raises
    ------
    RuntimeError
        If station coverage falls below `MIN_STATION_COVERAGE` or no green
        space site loaded.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0013: production.station_proximity_features")
    steps.create_station_proximity_features_table(engine, cfg, log)

    log.info(
        "0013: production.river_proximity_features (within %dm)",
        steps.RIVER_PROXIMITY_RADIUS_M,
    )
    steps.create_river_proximity_features_table(engine, cfg, log)

    log.info("0013: staging.london_green_spaces (+ access points)")
    steps.create_london_green_spaces_tables(engine, cfg, log)

    with engine.connect() as connection:
        ppd_rows = connection.execute(
            text("SELECT count(*) FROM production.ppd_table")
        ).scalar_one()
        stations = connection.execute(
            text(
                "SELECT count(nearest_station_distance_km) "
                "FROM production.station_proximity_features"
            )
        ).scalar_one()
        rivers = connection.execute(
            text(
                "SELECT count(distance_to_river_m) "
                "FROM production.river_proximity_features"
            )
        ).scalar_one()
        spaces = connection.execute(
            text("SELECT count(*) FROM staging.london_green_spaces")
        ).scalar_one()

    coverage = stations / ppd_rows if ppd_rows else 0.0
    log.info(
        "0013: %d sale(s) with a nearest station (%.1f%%), %d within %dm of "
        "a river, %d green space site(s)",
        stations, coverage * 100.0, rivers,
        steps.RIVER_PROXIMITY_RADIUS_M, spaces,
    )
    if coverage < MIN_STATION_COVERAGE:
        raise RuntimeError(
            f"only {coverage:.1%} of sales resolved to a nearest station "
            f"(want >= {MIN_STATION_COVERAGE:.0%}) -- check "
            "production.london_stations (0009) and "
            "production.address_coordinates (0010)"
        )
    if spaces == 0:
        raise RuntimeError(
            "staging.london_green_spaces is empty -- the OS Open Greenspace "
            "GML loaded no sites. Check that data.open_green_spaces_gml_fp "
            "points at an existing file."
        )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
