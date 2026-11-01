"""
Apply the data-quality fixes, then promote staging to production.

Fixes run on staging first, so production never holds a failing row. The
category tables, ``address_table``, ``ppd_table``, ``epc_table``,
``london_parcels`` and the derived ``uprn_lookup`` are promoted with ``LIKE
... INCLUDING ALL``; the view is not rebuilt (0016 builds it). Nothing else
is promoted, because every production table is readable by the API role.

Revision ID: 0007
Revises: 0006
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# Tables this revision must leave populated in production.
_EXPECTED_PRODUCTION_TABLES = tuple(steps._SERVING_TABLES) + (
    "uprn_lookup",
    "london_parcels",
)


def _assert_promoted(engine, log) -> None:
    """
    Check every expected production table is populated and PPD counts match staging.

    Raises
    ------
    RuntimeError
        On an empty table or a staging/production row-count mismatch.
    """
    with engine.connect() as connection:
        for table in _EXPECTED_PRODUCTION_TABLES:
            rows = connection.execute(
                text(f"SELECT count(*) FROM production.{table}")
            ).scalar_one()
            log.info("0007: production.%s holds %d row(s)", table, rows)
            if rows == 0:
                raise RuntimeError(
                    f"production.{table} is empty after the promotion -- "
                    "0016's view joins it and would produce NULLs or no rows"
                )

        staging_ppd, prod_ppd = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM staging.ppd_table), "
                "(SELECT count(*) FROM production.ppd_table)"
            )
        ).one()
    if staging_ppd != prod_ppd:
        raise RuntimeError(
            f"production.ppd_table has {prod_ppd} row(s) against "
            f"{staging_ppd} in staging -- the promotion is a straight copy "
            "and the two must match exactly"
        )
    log.info(
        "0007: promoted %d ppd_table row(s) with staging/production parity",
        prod_ppd,
    )


def upgrade() -> None:
    """Apply the data-quality fixes, promote, and check the result."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info(
        "0007: applying data-quality fixes to staging (domain repairs, then "
        "the CHECK constraints that make the domain explicit)"
    )
    steps.apply_data_quality_fixes(engine, cfg, log)

    log.info(
        "0007: promoting %s (+ uprn_lookup) to production",
        ", ".join(steps._SERVING_TABLES),
    )
    steps.promote_staging_to_production(engine, cfg, log, rebuild_view=False)

    log.info("0007: promoting %s", ", ".join(steps._ADDITIONAL_SERVING_TABLES))
    steps.promote_additional_serving_tables_to_production(engine, cfg, log)

    _assert_promoted(engine, log)


def downgrade() -> None:
    """Refuse; the data-quality fixes repair and delete staging rows in place."""
    no_downgrade()
