"""
Build the PTAL features: ``staging.ptal_london`` and ``production.ptal_features``.

Joins each sale's coordinate to a TfL grid cell with ``ST_Contains`` against
the cell polygons, falling back to the nearest centroid within
``PTAL_FALLBACK_MAX_DISTANCE_M``. Its own revision because the spatial join is
expensive to repeat.

Revision ID: 0012
Revises: 0011
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Coverage below this means the ST_Contains join is failing.
MIN_PTAL_COVERAGE = 0.95


def upgrade() -> None:
    """
    Build the PTAL view and feature table and check coverage.

    Raises
    ------
    RuntimeError
        If coverage falls below `MIN_PTAL_COVERAGE`.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info(
        "0012: staging.ptal_london (grid + geometry, clipped to %s)",
        steps.LONDON_BBOX,
    )
    steps.create_ptal_london_view(engine, cfg, log)

    log.info(
        "0012: production.ptal_features (ST_Contains against the cell "
        "polygons, %dm nearest-centroid fallback)",
        steps.PTAL_FALLBACK_MAX_DISTANCE_M,
    )
    steps.create_ptal_features_table(engine, cfg, log)

    with engine.connect() as connection:
        rows, with_band, ppd_rows = connection.execute(
            text(
                """
                SELECT count(*),
                       count(ptal_band_rank),
                       (SELECT count(*) FROM production.ppd_table)
                FROM production.ptal_features
                """
            )
        ).one()

    coverage = with_band / ppd_rows if ppd_rows else 0.0
    log.info(
        "0012: production.ptal_features holds %d row(s), %d with a band, "
        "against %d sale(s) -- %.1f%% coverage",
        rows, with_band, ppd_rows, coverage * 100.0,
    )
    if coverage < MIN_PTAL_COVERAGE:
        raise RuntimeError(
            f"only {coverage:.1%} of sales resolved to a PTAL band (want >= "
            f"{MIN_PTAL_COVERAGE:.0%}). At this rate the ST_Contains join is "
            "failing -- check that staging.ptal_cell_polygons loaded with "
            "geometry and that its GIST index exists."
        )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
