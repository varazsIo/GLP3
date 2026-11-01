"""
Build the two KNN feature tables: comparables and neighbourhood statistics.

``comps_features`` takes the 5 nearest same-type sales within 2000 m in the
prior 12 months (leakage-safe); ``neighbourhood_features`` adds spatial-lag
and local-median prices, floor-area percentile and EPC band shares. Both read
``production.ppd_table``, build into UNLOGGED tables with bulk tuning, and use
``btree_gist`` indexes when the extension exists.

Revision ID: 0014
Revises: 0013
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _btree_gist_available(engine) -> bool:
    """Return True when the ``btree_gist`` extension is installed."""
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
            ).first()
        )


def upgrade() -> None:
    """
    Build the comparables and neighbourhood tables.

    Raises
    ------
    RuntimeError
        If either table is empty. Row parity is not required: sales without a
        coordinate or below the price floor are skipped, and a sale with no
        qualifying comparable legitimately has none.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    has_btree_gist = _btree_gist_available(engine)
    if not has_btree_gist:
        log.warning(
            "0014: btree_gist is not installed, so the comps geometry index "
            "will be gist(geom) alone and the 12-month window stays a "
            "heap-fetch filter -- ~1,995 rejected random page reads per "
            "terraced row. Install it with src/hpldn/backend/db/sql/db_init.sql and "
            "re-run this revision to get gist(geom, dateoftransfer)."
        )

    log.info(
        "0014: production.comps_features (k=%d, %d-month window, %dm radius, "
        "propertytype match=%s, gist index on %s)",
        steps.COMPS_K, steps.COMPS_WINDOW_MONTHS, steps.COMPS_RADIUS_M,
        steps.COMPS_MATCH_PROPERTYTYPE,
        "geom, dateoftransfer" if has_btree_gist else "geom",
    )
    steps.create_comps_features_table(engine, cfg, log, use_btree_gist=has_btree_gist)

    log.info(
        "0014: production.neighbourhood_features (%d spatial join worker(s))",
        steps.get_spatial_join_jobs(log=log),
    )
    steps.create_neighbourhood_features_table(engine, cfg, log)

    with engine.connect() as connection:
        ppd_rows, comps, with_comps, neighbourhood = connection.execute(
            text(
                """
                SELECT (SELECT count(*) FROM production.ppd_table),
                       (SELECT count(*) FROM production.comps_features),
                       (SELECT count(*) FROM production.comps_features
                         WHERE comp_txn_count > 0),
                       (SELECT count(*) FROM production.neighbourhood_features)
                """
            )
        ).one()

    log.info(
        "0014: comps_features %d row(s), %d with at least one comparable; "
        "neighbourhood_features %d row(s); production.ppd_table %d",
        comps, with_comps, neighbourhood, ppd_rows,
    )
    if not comps or not neighbourhood:
        raise RuntimeError(
            f"0014 produced empty feature table(s): comps={comps}, "
            f"neighbourhood={neighbourhood}. Both read "
            "production.address_coordinates (0010) -- check its coverage."
        )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
