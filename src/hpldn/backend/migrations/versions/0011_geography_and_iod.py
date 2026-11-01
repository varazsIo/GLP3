"""
Build the geography and Indices of Deprivation features.

Assigns each sale's coordinate to an LSOA 2011 (point-in-polygon with a 150 m
fallback), builds ``production.lsoa_msoa_features`` over every sale, and
``production.iod_features`` joined on IoD publication date, never release
year. Guards check row-count parity with ``production.ppd_table``.

Revision ID: 0011
Revises: 0010
"""

from sqlalchemy import text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# Old 0062's thresholds, which catch a borough-level proxy standing in for LSOAs.
MIN_DISTINCT_LSOA = 4000
MIN_DISTINCT_MSOA = 900
MIN_LSOA_COVERAGE = 0.999

# Old 0062's rebuild SQL, verbatim, over the complete production.ppd_table.
_REBUILD_SQL = """
DROP TABLE IF EXISTS production.lsoa_msoa_features;

CREATE TABLE production.lsoa_msoa_features (
    ppd_id    bigint,
    lsoa_code varchar,
    lsoa_name varchar,
    msoa_code varchar,
    msoa_name varchar
);

INSERT INTO production.lsoa_msoa_features (ppd_id, lsoa_code, lsoa_name, msoa_code, msoa_name)
SELECT p.id,
       pl.lsoa11cd,
       lu.lsoa11nm,
       lu.msoa11cd,
       lu.msoa11nm
FROM production.ppd_table p
LEFT JOIN staging.property_lsoa pl ON pl.ppd_id  = p.id
LEFT JOIN staging.lsoa_msoa_lu  lu ON lu.lsoa11cd = pl.lsoa11cd;

CREATE UNIQUE INDEX idx_prod_lsoa_msoa_features_ppd_id
    ON production.lsoa_msoa_features (ppd_id);
"""

# Coverage guards plus row-count parity with production.ppd_table.
_GUARD_SQL = """
SELECT COUNT(*),
       COUNT(DISTINCT lsoa_code),
       COUNT(DISTINCT msoa_code),
       COUNT(*) FILTER (WHERE lsoa_code IS NOT NULL AND lsoa_code NOT LIKE 'E01%'),
       COUNT(*) FILTER (WHERE msoa_code IS NOT NULL AND msoa_code NOT LIKE 'E02%'),
       COUNT(*) FILTER (WHERE lsoa_code IS NOT NULL),
       (SELECT COUNT(*) FROM production.ppd_table)
FROM production.lsoa_msoa_features
"""


def _rebuild_lsoa_msoa_features(engine, cfg, log) -> None:
    """
    Rebuild ``production.lsoa_msoa_features`` and check it.

    Raises
    ------
    RuntimeError
        If it misses sales, has too few distinct codes, or malformed codes.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(_REBUILD_SQL))
        connection.execute(text("ANALYZE production.lsoa_msoa_features"))

    with engine.connect() as connection:
        (
            rows, distinct_lsoa, distinct_msoa,
            bad_lsoa, bad_msoa, non_null_lsoa, ppd_rows,
        ) = connection.execute(text(_GUARD_SQL)).one()

    coverage = non_null_lsoa / rows if rows else 0.0
    log.info(
        "0011: production.lsoa_msoa_features holds %d row(s) against %d in "
        "production.ppd_table -- %d distinct lsoa_code, %d distinct "
        "msoa_code, %.1f%% non-NULL lsoa_code coverage",
        rows, ppd_rows, distinct_lsoa, distinct_msoa, coverage * 100.0,
    )

    if rows != ppd_rows:
        raise RuntimeError(
            f"production.lsoa_msoa_features has {rows} row(s) against "
            f"{ppd_rows} in production.ppd_table -- it must cover every sale "
            "exactly once; a stale table built before the latest sales fails "
            "here."
        )
    if distinct_lsoa < MIN_DISTINCT_LSOA:
        raise RuntimeError(
            f"only {distinct_lsoa} distinct lsoa_code values (want >= "
            f"{MIN_DISTINCT_LSOA}) -- sales are being assigned a borough-level "
            "proxy instead of their LSOA"
        )
    if distinct_msoa < MIN_DISTINCT_MSOA:
        raise RuntimeError(
            f"only {distinct_msoa} distinct msoa_code values (want >= "
            f"{MIN_DISTINCT_MSOA}) -- borough-proxy failure mode"
        )
    if bad_lsoa:
        raise RuntimeError(
            f"{bad_lsoa} non-NULL lsoa_code value(s) are not LSOA11 codes (E01...)"
        )
    if bad_msoa:
        raise RuntimeError(
            f"{bad_msoa} non-NULL msoa_code value(s) are not MSOA11 codes (E02...)"
        )
    if coverage < MIN_LSOA_COVERAGE:
        raise RuntimeError(
            f"only {coverage:.1%} of rows have a non-NULL lsoa_code "
            f"(want >= {MIN_LSOA_COVERAGE:.1%})"
        )


def upgrade() -> None:
    """Assign LSOAs, then build the LSOA/MSOA and IoD feature tables."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info(
        "0011: staging.property_lsoa (point-in-polygon, %dm nearest-boundary "
        "fallback)", steps.LSOA_FALLBACK_MAX_DISTANCE_M,
    )
    steps.create_property_lsoa_table(engine, cfg, log)

    log.info("0011: production.lsoa_msoa_features")
    _rebuild_lsoa_msoa_features(engine, cfg, log)

    log.info("0011: staging.iod_releases (four in-code literals, no file)")
    steps.create_iod_releases_table(engine, cfg, log)

    log.info(
        "0011: staging.iod_lsoa_spine (four releases reconciled, %dm "
        "neighbour contrast)", steps.IOD_NEIGHBOUR_RADIUS_M,
    )
    steps.create_iod_lsoa_spine_table(engine, cfg, log)

    log.info(
        "0011: production.iod_features (as-of the sale, %d-month publication "
        "embargo)", steps.IOD_PUBLICATION_EMBARGO_MONTHS,
    )
    steps.create_iod_features_table(engine, cfg, log)


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
