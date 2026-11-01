"""
Build the time-series features: previous sale and MSOA price momentum.

``prev_sale_features`` is a LAG over each UPRN's sales, filtered to
``MIN_PLAUSIBLE_PRICE`` before the window so nominal transfers never become a
previous price. ``lsoa_price_trend_features`` holds MSOA 1-year and 5-year
momentum; it keeps its ``lsoa`` name. The last inputs the view needs.

Revision ID: 0015
Revises: 0014
"""

from datetime import date

from sqlalchemy import text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# The price floor also used by the view, applied to the LAG's input.
MIN_PLAUSIBLE_PRICE = 10_000

# First sale date served by the fusion rather than ukhpucl, and how far the
# momentum NULL rate after it may exceed the overall rate.
SOURCE_CUTOVER_DATE = date(2024, 11, 1)

MAX_MOMENTUM_NULL_RATE_EXCESS = 0.01

_BUILD_TABLE = "staging.prev_sale_features_build"

# Build SQL, with the price floor inside the window's input.
_PREV_SALE_BUILD_SQL = f"""
CREATE UNLOGGED TABLE {_BUILD_TABLE} AS
SELECT s.ppd_id,
       s.prev_sale_price,
       s.prev_sale_date,
       (s.dateoftransfer - s.prev_sale_date) / 365.25 AS years_since_prev_sale
FROM (
    SELECT p.id AS ppd_id,
           p.dateoftransfer,
           lag(p.price)          OVER w AS prev_sale_price,
           lag(p.dateoftransfer) OVER w AS prev_sale_date
    FROM production.ppd_table p
    JOIN production.uprn_lookup ul ON ul.address_id = p.address_id
    WHERE p.price >= {MIN_PLAUSIBLE_PRICE}
    WINDOW w AS (PARTITION BY ul.uprn ORDER BY p.dateoftransfer, p.id)
) s
WHERE s.prev_sale_price IS NOT NULL
"""

_PREV_SALE_DDL = """
CREATE TABLE production.prev_sale_features (
    ppd_id                BIGINT,
    prev_sale_price       BIGINT,
    prev_sale_date        DATE,
    years_since_prev_sale NUMERIC
)
"""

_PREV_SALE_INSERT = f"""
INSERT INTO production.prev_sale_features
    (ppd_id, prev_sale_price, prev_sale_date, years_since_prev_sale)
SELECT ppd_id, prev_sale_price, prev_sale_date, years_since_prev_sale
FROM {_BUILD_TABLE}
"""

_PREV_SALE_INDEX = (
    "CREATE UNIQUE INDEX idx_prod_prev_sale_features_ppd_id "
    "ON production.prev_sale_features (ppd_id)"
)

_TREND_GUARD_SQL = """
SELECT COUNT(*),
       COUNT(msoa_price_momentum_1y),
       COUNT(msoa_price_momentum_5y)
FROM production.lsoa_price_trend_features
"""

_TREND_CUTOVER_SQL = """
SELECT COUNT(*) FILTER (WHERE t.msoa_price_momentum_1y IS NULL),
       COUNT(*),
       COUNT(*) FILTER (
           WHERE t.msoa_price_momentum_1y IS NULL
             AND p.dateoftransfer >= :cutover
       ),
       COUNT(*) FILTER (WHERE p.dateoftransfer >= :cutover)
FROM production.lsoa_price_trend_features t
JOIN production.ppd_table p ON p.id = t.ppd_id
"""


def _build_prev_sale_features(engine, cfg, log) -> int:
    """
    Build ``prev_sale_features`` through an UNLOGGED build table.

    The existing table is dropped with CASCADE because the view joins it on a
    re-run; 0016 rebuilds the view.

    Returns
    -------
    int
        Rows built.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {_BUILD_TABLE}"))
        connection.execute(text(_PREV_SALE_BUILD_SQL))
        connection.execute(
            text("DROP TABLE IF EXISTS production.prev_sale_features CASCADE")
        )
        connection.execute(text(_PREV_SALE_DDL))
        inserted = connection.execute(text(_PREV_SALE_INSERT)).rowcount
        connection.execute(text(_PREV_SALE_INDEX))
        connection.execute(text(f"DROP TABLE IF EXISTS {_BUILD_TABLE}"))
        connection.execute(text("ANALYZE production.prev_sale_features"))
    log.info(
        "0015: production.prev_sale_features holds %d prior-sale row(s) "
        "(price floor GBP %d applied to the LAG's input)",
        inserted, MIN_PLAUSIBLE_PRICE,
    )
    if not inserted:
        raise RuntimeError(
            "production.prev_sale_features is empty -- no property has a "
            "second qualifying sale, which cannot be true of 1.9M London "
            "transactions. Check production.uprn_lookup (0007)."
        )
    return inserted


def _check_trend_features(engine, log) -> None:
    """
    Check the momentum table covers every eligible sale and is not stale.

    Raises
    ------
    RuntimeError
        If its row count differs from eligible sales, or the 1-year NULL rate
        after `SOURCE_CUTOVER_DATE` exceeds the overall rate by more than
        `MAX_MOMENTUM_NULL_RATE_EXCESS`.
    """
    with engine.connect() as connection:
        rows, with_1y, with_5y = connection.execute(text(_TREND_GUARD_SQL)).one()
        eligible = connection.execute(
            text(
                "SELECT COUNT(*) FROM production.ppd_table p "
                "JOIN production.lsoa_msoa_features lmf ON lmf.ppd_id = p.id "
                "WHERE lmf.msoa_code IS NOT NULL AND p.price >= :min_price"
            ),
            {"min_price": MIN_PLAUSIBLE_PRICE},
        ).scalar_one()
        null_1y, total, null_after, after = connection.execute(
            text(_TREND_CUTOVER_SQL), {"cutover": SOURCE_CUTOVER_DATE}
        ).one()

    log.info(
        "0015: production.lsoa_price_trend_features holds %d row(s) against "
        "%d eligible sale(s) -- %d with momentum_1y, %d with momentum_5y",
        rows, eligible, with_1y, with_5y,
    )
    if rows != eligible:
        raise RuntimeError(
            f"production.lsoa_price_trend_features has {rows} row(s) against "
            f"{eligible} eligible in production.ppd_table (a resolved "
            f"msoa_code and price >= {MIN_PLAUSIBLE_PRICE}). It must cover "
            "every eligible sale."
        )

    overall = null_1y / total if total else 0.0
    post = null_after / after if after else 0.0
    log.info(
        "0015: msoa_price_momentum_1y NULL rate %.2f%% overall, %.2f%% on or "
        "after %s", overall * 100.0, post * 100.0, SOURCE_CUTOVER_DATE,
    )
    if post - overall > MAX_MOMENTUM_NULL_RATE_EXCESS:
        raise RuntimeError(
            f"msoa_price_momentum_1y is NULL for {post:.1%} of sales on or "
            f"after {SOURCE_CUTOVER_DATE} against {overall:.1%} overall. A "
            "gap this size means a stale table, not MSOA "
            "sparsity -- the trend table does not cover the fused sales."
        )


def upgrade() -> None:
    """Build the previous-sale and momentum tables and check them."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0015: production.prev_sale_features")
    _build_prev_sale_features(engine, cfg, log)

    log.info(
        "0015: production.lsoa_price_trend_features (momentum at MSOA, %d/%d "
        "month lookbacks)",
        steps.LSOA_TREND_YEAR_AGO_OFFSET_MONTHS,
        steps.LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS,
    )
    steps.create_lsoa_price_trend_features_table(engine, cfg, log)

    _check_trend_features(engine, log)
    log.info(
        "0015: every input to production.london_houses now exists -- 0016 "
        "builds the view once"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
