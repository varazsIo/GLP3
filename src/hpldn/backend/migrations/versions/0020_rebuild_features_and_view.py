"""
Rebuild every per-sale feature and ``production.london_houses`` on the repaired data.

0017 added sales, 0018 addresses, streets and parcels, and 0019 certificates
and property types. Every ``ppd_id``-keyed feature table reads those inputs,
so this revision re-runs the builders 0010-0015 own, in their order, then
rebuilds the view with 0020's definition (``is_maisonette`` falls back to the
certificate's ``property_type``) and checks the content, not just the shape:

- the 0016 row and ``epc_matched`` counts as floors (the repairs only add);
- every month from six months before the fusion cut-over keeps at least
  60 % of raw London Price Paid sales (0017);
- per-year NULL-rate ceilings for ``poststreet``, ``lot_area_sqm`` and
  ``latitude`` from the fusion era on (0018);
- ``is_maisonette`` known for at least 90 % of flats sold in the latest 12
  months (0019).

Independent builders run concurrently (`chain.run_concurrently`); the
neighbourhood self-join dominates. The API has no view to read from 0019
until this revision completes.

Revision ID: 0020
Revises: 0019
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import load_revision, no_downgrade, run_concurrently
from hpldn.backend.migrations.london_houses_view import (
    CREATE_VIEW_SQL_0020,
    EPC_MATCHED_ROWS_EXPECTED,
    LONDON_HOUSES_ROWS_EXPECTED,
    rebuild,
)
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

FIRST_CHECKED_YEAR = 2024

# Ceilings on the share of a year's sales with a NULL column, from 2024 on.
# Measured after 0018 on the from-empty build: poststreet and lot_area_sqm
# below 1 %, latitude 0 %. Before the repairs 2025 stood at 45 % and 60 %.
NULL_RATE_CEILINGS = {
    "poststreet": 0.05,
    "lot_area_sqm": 0.05,
    "latitude": 0.01,
}

MAX_UNKNOWN_MAISONETTE_FLAG_RECENT_FLATS = 0.10

_NULL_RATES_SQL = """
SELECT extract(year FROM dateoftransfer)::int AS year,
       count(*) AS sales,
       {columns}
FROM production.london_houses
WHERE dateoftransfer >= make_date(:first_year, 1, 1)
GROUP BY 1
ORDER BY 1
"""

_RECENT_MAISONETTE_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE is_maisonette IS NULL),
       count(*) FILTER (WHERE is_maisonette)
FROM production.london_houses
WHERE propertytype = 'F'
  AND dateoftransfer > (SELECT max(dateoftransfer) FROM production.london_houses)
                       - interval '12 months'
"""

_COUNTS_SQL = """
SELECT count(*), count(*) FILTER (WHERE epc_matched) FROM production.london_houses
"""


def _rebuild_geography(engine, cfg, log, geography, time_series) -> None:
    """
    LSOA assignment, then the tables keyed off it (0011, 0015's momentum).

    ``iod_features`` reads ``staging.property_lsoa`` and the momentum table
    ``lsoa_msoa_features``, so both follow them, side by side.
    """
    steps.create_property_lsoa_table(engine, cfg, log)
    geography._rebuild_lsoa_msoa_features(engine, cfg, log)

    def momentum():
        steps.create_lsoa_price_trend_features_table(engine, cfg, log)
        time_series._check_trend_features(engine, log)

    run_concurrently(
        {
            "0020 iod_features (0011)": lambda: steps.create_iod_features_table(
                engine, cfg, log
            ),
            "0020 lsoa_price_trend_features (0015)": momentum,
        },
        log,
    )


def _rebuild_features(engine, cfg, log) -> None:
    """
    Re-run the per-sale feature builders of 0010-0015.

    ``address_coordinates`` comes first, since the spatial builders read it.
    The rest each write their own tables and read only inputs 0019 already
    settled, so they run at once; ``neighbourhood_features`` runs last and
    alone because it already shards its self-join across every core.
    """
    geography = load_revision("0011")
    comps = load_revision("0014")
    time_series = load_revision("0015")
    use_btree_gist = comps._btree_gist_available(engine)

    log.info("0020: production.address_coordinates (0010)")
    steps.create_address_coordinates_table(engine, cfg, log)

    log.info("0020: geography, PTAL, proximity, comps and previous sale, concurrently")
    run_concurrently(
        {
            "0020 geography chain (0011/0015)": lambda: _rebuild_geography(
                engine, cfg, log, geography, time_series
            ),
            "0020 ptal_features (0012)": lambda: steps.create_ptal_features_table(
                engine, cfg, log
            ),
            "0020 station_proximity_features (0013)": lambda: (
                steps.create_station_proximity_features_table(engine, cfg, log)
            ),
            "0020 river_proximity_features (0013)": lambda: (
                steps.create_river_proximity_features_table(engine, cfg, log)
            ),
            "0020 comps_features (0014)": lambda: steps.create_comps_features_table(
                engine, cfg, log, use_btree_gist=use_btree_gist
            ),
            "0020 prev_sale_features (0015)": lambda: time_series._build_prev_sale_features(
                engine, cfg, log
            ),
        },
        log,
    )

    log.info("0020: neighbourhood features (0014)")
    steps.create_neighbourhood_features_table(engine, cfg, log)


def _check_counts(engine, log) -> None:
    """
    Check the rebuilt view did not lose rows against the 0016 baseline.

    Raises
    ------
    RuntimeError
        If the row or ``epc_matched`` count fell.
    """
    with engine.connect() as connection:
        rows, matched = connection.execute(text(_COUNTS_SQL)).one()
    log.info(
        "0020: production.london_houses -- %d row(s) (+%d on 0016), %d "
        "epc_matched (+%d)",
        rows, rows - LONDON_HOUSES_ROWS_EXPECTED,
        matched, matched - EPC_MATCHED_ROWS_EXPECTED,
    )
    if rows < LONDON_HOUSES_ROWS_EXPECTED or matched < EPC_MATCHED_ROWS_EXPECTED:
        raise RuntimeError(
            f"production.london_houses has {rows} row(s) and {matched} "
            f"epc_matched, below 0016's {LONDON_HOUSES_ROWS_EXPECTED} and "
            f"{EPC_MATCHED_ROWS_EXPECTED}. 0017-0019 only add sales, "
            "addresses and certificates, so a smaller view means a rebuild "
            "dropped rows."
        )


def _check_null_rates(engine, log) -> None:
    """
    Check the per-year NULL-rate ceilings in `NULL_RATE_CEILINGS`.

    Raises
    ------
    RuntimeError
        Listing every (year, column) over its ceiling.
    """
    columns = ",\n       ".join(
        f"count(*) FILTER (WHERE {column} IS NULL) AS {column}"
        for column in NULL_RATE_CEILINGS
    )
    with engine.connect() as connection:
        rows = connection.execute(
            text(_NULL_RATES_SQL.format(columns=columns)),
            {"first_year": FIRST_CHECKED_YEAR},
        ).mappings().all()
    failing = []
    for row in rows:
        rates = {c: row[c] / row["sales"] for c in NULL_RATE_CEILINGS}
        log.info(
            "0020: %d -- %d sale(s), NULL %s", row["year"], row["sales"],
            ", ".join(f"{c} {100 * r:.1f}%" for c, r in rates.items()),
        )
        failing += [
            f"{row['year']} {c} {r:.1%} > {NULL_RATE_CEILINGS[c]:.0%}"
            for c, r in rates.items()
            if r > NULL_RATE_CEILINGS[c]
        ]
    if failing:
        raise RuntimeError(
            "production.london_houses breaks its per-year NULL-rate ceilings: "
            + "; ".join(failing)
            + ". The post-fusion street and parcel refresh (0018) did not reach "
            "these sales."
        )


def _check_recent_maisonette_flag(engine, log) -> None:
    """
    Check ``is_maisonette`` is known for recent flats.

    Raises
    ------
    RuntimeError
        If more than `MAX_UNKNOWN_MAISONETTE_FLAG_RECENT_FLATS` of flats sold
        in the latest 12 months have it NULL, or none is a maisonette.
    """
    with engine.connect() as connection:
        flats, unknown, maisonettes = connection.execute(
            text(_RECENT_MAISONETTE_SQL)
        ).one()
    share = unknown / flats if flats else 0.0
    log.info(
        "0020: latest 12 months -- %d flat sale(s), %d (%.1f%%) with unknown "
        "is_maisonette, %d maisonette(s)", flats, unknown, 100 * share, maisonettes,
    )
    if share > MAX_UNKNOWN_MAISONETTE_FLAG_RECENT_FLATS or not maisonettes:
        raise RuntimeError(
            f"is_maisonette is NULL for {share:.1%} of flats sold in the latest "
            f"12 months (want <= {MAX_UNKNOWN_MAISONETTE_FLAG_RECENT_FLATS:.0%}) "
            f"and {maisonettes} are maisonettes. Recent maisonettes are being "
            "treated as flats: check production.epc_table.property_type "
            "(0019)."
        )


def upgrade() -> None:
    """Rebuild the features and the view, then run the content guards."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    gap = load_revision("0017")
    view = load_revision("0016")

    _rebuild_features(engine, cfg, log)

    log.info("0020: rebuilding production.london_houses")
    rebuild(engine, cfg, log, create_sql=CREATE_VIEW_SQL_0020)
    view._check_columns(engine, log)

    _check_counts(engine, log)
    gap.check_monthly_completeness(
        gap.monthly_completeness(engine, "production", steps.PPD_EPC_FUSION_WINDOW_END),
        gap._add_months(steps.PPD_EPC_FUSION_WINDOW_START, -gap.GUARD_MONTHS_BEFORE_CUTOVER),
        "0020 production.ppd_table",
        log,
    )
    _check_null_rates(engine, log)
    _check_recent_maisonette_flag(engine, log)

    log.info("0020: verifying EPC/PPD fusion coverage in the view")
    steps.verify_epc_ppd_fusion_view_coverage(engine, cfg, log)

    log.info(
        "0020: data repairs complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  restart the API (its area cache reads the view once)\n"
        "  retrain hpp_rf / hpp_cb and re-run build_london_map and "
        "backtest_heuristic -- every figure derived from the view has moved"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
