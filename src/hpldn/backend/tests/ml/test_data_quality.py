"""
Data-quality gate for every feature `hpp_rf` and `hpp_cb` train on.

Written against `hpp_rf`'s own column lists, so a feature added to either model
without a specification here fails
`test_every_model_feature_is_covered_by_this_suite`. Two tiers:

1. Source-data checks on ``production.london_houses`` in hpdbldn (read-only,
   `hpdbldn_ro`): missingness overall, by district and by year,
   cardinality, variance, outliers, domains, duplicates, cross-column
   consistency, temporal and spatial validity, and the missingness mechanism.
2. Leakage checks on the synthetic fixture: rewrite future or held-out prices
   and assert nothing a model sees for an earlier or training row moves.

Specifications come from the domain rather than the current data, so known
defects are strict xfails carrying their measurements; when a fix reaches the
data the xfail fails, signalling the marker should go. Open defects include
out-of-London coordinates and districts, estate-sized INSPIRE parcels,
single-snapshot PTAL and station data, price-dependent missingness for
``floor_level``, ``build_form`` coverage stepping back since 2025
(`COVERAGE_DRIFT_DEFECTS`), ``comp_missing`` mislabelling observed prices
(`DEPENDENT_COLUMN_DEFECTS`), and the certificate-derived columns over their
budgets (`MISSING_BUDGET_DEFECTS`). Measured on hpdbldn at 0034 (3,204,539
rows)::

    pytest src/hpldn/backend/tests/ml/test_data_quality.py -v
"""

import re

import numpy as np
import pandas as pd
import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from hpldn.backend.common.constants import FLOOR_LEVEL_CANONICAL, FLOOR_LEVEL_MAPPING
from hpldn.backend.scripts import hpp_cb, hpp_rf
from hpldn.backend.scripts.pipeline_steps import COMPS_K
from hpldn.backend.tests.ml.test_hp_rf import _make_dataset, _with_mortgage_features

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", None}
VIEW = hpp_rf.VIEW_NAME


RAW_FEATURE_COLUMNS = (
    "propertytype",
    "duration",
    "postcode_district",
    "postcode_sector",
    "construction_age_band",
    "number_habitable_rooms",
    "current_energy_rating",
    "total_floor_area",
    "build_form",
    "mains_gas_flag",
    "tenure",
    "multi_glaze_proportion",
    "extension_count",
    "floor_level",
    "latitude",
    "longitude",
    "lot_area_sqm",
    "comp_median_price",
    "comp_median_price_per_sqm",
    "comp_txn_count",
    "nearest_station_distance_km",
    "nearest_station_zone",
    "boe_rate_at_sale",
    "ukhpi_avg_price_at_sale",
    "ptal_band_rank",
    "ptal_ai",
)

# Coverage checks run over the EPC era only: the register did not exist before
# 2008, so earlier NULLs are an absent source, not a defect. Structural checks
# (domains, cardinality, duplicates, spatial, dates) still run over every year.
EPC_ERA_START_YEAR = 2009
EPC_ERA_PREDICATE = f"dateoftransfer >= DATE '{EPC_ERA_START_YEAR}-01-01'"

MAX_MISSING_FRACTION = {
    "propertytype": 0.0,
    "duration": 0.0,
    "postcode_district": 0.0,
    "postcode_sector": 0.0,
    # EPC-derived budgets: the as-of join correctly leaves pre-certificate sales
    # NULL. Budgets are the 2009+ rate plus headroom; comments give the observed rate.
    "construction_age_band": 0.07,          # 5.19%
    "number_habitable_rooms": 0.16,         # 6.35%
    "current_energy_rating": 0.06,          # 4.62%
    "total_floor_area": 0.08,               # 5.85%
    "multi_glaze_proportion": 0.08,         # 5.80%
    "extension_count": 0.08,                # 6.35%
    "latitude": 0.0,
    "longitude": 0.0,
    "nearest_station_distance_km": 0.0,
    "nearest_station_zone": 0.0,
    "boe_rate_at_sale": 0.0,
    "ukhpi_avg_price_at_sale": 0.0,
    "build_form": 0.08,                     # 6.07%
    "mains_gas_flag": 0.08,                 # 6.43%
    "tenure": 0.17,                         # 13.70%
    "floor_level": 0.85,                    # 74.11%
    "lot_area_sqm": 0.13,                   # 0.15%
    "comp_median_price": 0.001,             # 0.011%
    "comp_median_price_per_sqm": 0.02,      # 1.17%
    "comp_txn_count": 0.0,
    "ptal_band_rank": 0.001,
    "ptal_ai": 0.001,
}
# Part of the EPC headroom is consumed by the 2024-Q4 coverage step, which
# test_missingness_has_not_drifted_in_the_most_recent_year gates separately.

NUMERIC_DOMAIN_RANGES = {
    "number_habitable_rooms": (1, 30),
    "total_floor_area": (10.0, 2_000.0),
    "multi_glaze_proportion": (0, 100),
    "extension_count": (0, 10),
    "latitude": (51.25, 51.72),
    "longitude": (-0.55, 0.35),
    "lot_area_sqm": (5.0, 20_000.0),
    "comp_median_price": (20_000.0, 50_000_000.0),
    "comp_median_price_per_sqm": (300.0, 60_000.0),
    "comp_txn_count": (0, COMPS_K),
    "nearest_station_distance_km": (0.0, 15.0),
    "nearest_station_zone": (1, 9),
    "boe_rate_at_sale": (0.0, 20.0),
    "ukhpi_avg_price_at_sale": (50_000.0, 5_000_000.0),
    "ptal_band_rank": (0, 8),
    "ptal_ai": (0.0, 200.0),
}

RANGE_DEFECTS = {
    "latitude": "4 rows fall outside Greater London, worst at 53.2446049 (Cheshire)",
    "longitude": "worst at -2.9858609 (Newport, South Wales) and 0.7083977 (Essex)",
    "lot_area_sqm": "max 1,500,590 m2 (150 ha); 13,931 rows over 2 ha — INSPIRE parcel is an estate, not the property",
    "nearest_station_distance_km": "max 215.66 km, from the mis-geocoded CW8 row above",
    "comp_median_price_per_sqm": (
        "max 124,240 GBP/m2; 3 rows exceed 60,000 GBP/m2 -- a comparable whose "
        "own total_floor_area is implausibly small divides a normal price by "
        "it, so the outlier is inherited from the neighbour's EPC record, not "
        "from the subject property"
    ),
}

CATEGORICAL_DOMAINS = {
    "propertytype": {"D", "S", "T", "F"},
    "duration": {"F", "L"},
    "current_energy_rating": {"A", "B", "C", "D", "E", "F", "G"},
    "mains_gas_flag": {"Y", "N"},
    "tenure": {"O", "P", "S"},
    "build_form": {
        "Detached",
        "Semi-Detached",
        "Mid-Terrace",
        "End-Terrace",
        "Enclosed Mid-Terrace",
        "Enclosed End-Terrace",
    },
}

POSTCODE_DISTRICT_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?$")
POSTCODE_SECTOR_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?_[0-9]$")
CONSTRUCTION_AGE_BAND_RE = re.compile(
    r"^([0-9]{4}(-[0-9]{4})?"
    r"|England and Wales: ([0-9]{4}-[0-9]{4}|before [0-9]{4}|[0-9]{4} onwards))$"
)

LONDON_POSTCODE_AREAS = (
    "E", "EC", "N", "NW", "SE", "SW", "W", "WC",
    "BR", "CR", "DA", "EN", "HA", "IG", "KT", "RM", "SM", "TW", "UB", "WD", "TN",
)

NEAR_ZERO_VARIANCE_TOP_VALUE_FRACTION = 0.99

MIN_DISTINCT_VALUES = 2

MIN_GROUP_ROWS = 200

MAX_MISSINGNESS_RATIO_ACROSS_PRICE_BANDS = 2.5

MIN_MISSING_ROWS_PER_BAND = 20

MISSINGNESS_MECHANISM_DEFECTS = {
    "floor_level": (
        "hpdbldn at 0034: within flats the missing rate rises 16.31% -> "
        "46.82% across price bands (2.9x, limit 2.5x; 4,078-226,642 NULLs per "
        "band), so floor_level_missing is partly a price proxy"
    ),
}

AS_OF_REFERENCE_SOURCES = {
    "production.epc_table": (
        "inspection_date",
        (
            "total_floor_area", "number_habitable_rooms", "current_energy_rating",
            "build_form", "mains_gas_flag", "tenure", "multi_glaze_proportion",
            "extension_count", "floor_level",
        ),
    ),
    "production.boe_rate": ("date_changed", ("boe_rate_at_sale",)),
    "production.ukhpi": ("period", ("ukhpi_avg_price_at_sale",)),
    "production.london_stations": (
        None,
        ("nearest_station_distance_km", "nearest_station_zone"),
    ),
    "staging.ptal_2023": (None, ("ptal_band_rank", "ptal_ai")),
    # The rating in force from the report's publication date (migrations 0033-0034).
    "production.ofsted_rating_spells": ("valid_from", tuple(hpp_rf.OFSTED_COLUMNS)),
}

TEMPORAL_PROVENANCE = {
    **{c: "static" for c in (
        "propertytype", "duration", "postcode_district", "postcode_sector",
        "construction_age_band", "lot_area_sqm", "latitude", "longitude",
        "postcode_propertytype", "postcode_duration", "propertytype_duration",
        "sale_year", "sale_month", "property_age_years", "area_per_room",
        "log_floor_area", "log_lot_area", "lot_to_floor_ratio",
        "distance_to_city_center_km", "is_house",
        "distance_to_river", "lsoa_code", "msoa_code",
        # First sale of a new build: recorded on the transaction itself.
        "oldnew",
    )},
    **{c: "as_of" for c in (
        "current_energy_rating", "build_form", "mains_gas_flag", "tenure",
        "floor_level", "number_habitable_rooms", "total_floor_area",
        "multi_glaze_proportion", "extension_count",
    )},
    **{c: "as_of" for c in hpp_rf.MACRO_STAT_COLUMNS},
    # EPC-derived, taken from the as-of certificate like the columns above.
    "is_maisonette": "as_of",
    # An interaction is as safe as its least safe input; build_form is as_of.
    "construction_age_band_propertytype": "static",
    "build_form_propertytype": "as_of",
    # Neighbourhood, same-street, MSOA momentum and previous-sale features read
    # only sales completed before the row sold.
    **{
        c: "causal"
        for c in hpp_rf.NEIGHBOURHOOD_STAT_COLUMNS
        + hpp_rf.SAME_STREET_STAT_COLUMNS
        + hpp_rf.MSOA_TREND_STAT_COLUMNS
        + hpp_rf.PREV_SALE_DEPENDENT_COLUMNS
    },
    **{c: "causal" for c in hpp_rf.COMP_STAT_COLUMNS + hpp_rf.ROLLING_COMP_STAT_COLUMNS},
    **{c: "train_only" for c in hpp_rf.POSTCODE_STAT_COLUMNS + hpp_rf.SECTOR_STAT_COLUMNS},
    **{c: "snapshot" for c in hpp_rf.TRANSIT_STAT_COLUMNS + hpp_rf.PTAL_DEPENDENT_COLUMNS},
    # School ratings published on or before the sale date (production.ofsted_features).
    **{c: "as_of" for c in hpp_rf.OFSTED_COLUMNS},
}




@pytest.fixture(scope="module")
def view_engine(hpdbldn_ro):
    """
    Return hpdbldn (read-only), which holds ``production.london_houses``.

    This suite profiles whatever the view holds and never triggers a rebuild.
    """
    return hpdbldn_ro


@pytest.fixture(scope="module")
def row_count(view_engine):
    return _scalar(view_engine, f"SELECT count(*) FROM {VIEW}")


@pytest.fixture(scope="module")
def epc_era_row_count(view_engine):
    """Return the number of EPC-era rows the coverage checks measure over."""
    return _scalar(
        view_engine, f"SELECT count(*) FROM {VIEW} WHERE {EPC_ERA_PREDICATE}"
    )


def _scalar(engine, sql, **params):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar()


def _rows(engine, sql, **params):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).fetchall()


def _violating(engine, predicate, limit=5):
    """Return ``(count, sample)`` of view rows matching `predicate`."""
    count = _scalar(engine, f"SELECT count(*) FROM {VIEW} WHERE {predicate}")
    sample = _rows(
        engine,
        f"SELECT ppd_id, dateoftransfer, postcode_district, price "
        f"FROM {VIEW} WHERE {predicate} LIMIT {int(limit)}",
    )
    return count, sample


def _xfail_if(mapping, key):
    """Return a strict xfail mark when `key` names a known defect in `mapping`, else no marks."""
    if key in mapping:
        return pytest.mark.xfail(
            reason=f"known data defect — {mapping[key]}", strict=True
        )
    return ()


def _param(key, mapping=None):
    marks = _xfail_if(mapping, key) if mapping else ()
    return pytest.param(key, marks=marks, id=key)




def test_every_model_feature_is_covered_by_this_suite():
    """Check every model feature has a temporal-provenance classification."""
    unclassified = sorted(set(hpp_rf.FEATURE_COLUMNS) - set(TEMPORAL_PROVENANCE))
    assert not unclassified, (
        f"{len(unclassified)} model feature(s) have no TEMPORAL_PROVENANCE entry: "
        f"{unclassified}. Add one — 'would this value have been known at the "
        f"exact point the prediction is made?' — before training on them."
    )


def test_every_raw_view_feature_has_a_missingness_budget():
    assert set(RAW_FEATURE_COLUMNS) == set(MAX_MISSING_FRACTION)


def test_catboost_feature_set_extends_hpp_rf_rather_than_diverging():
    """
    Check CatBoost's features are `hpp_rf`'s plus and minus only the deliberate lists.

    The IoD and mortgage-rate additions and the default drops are the only
    permitted differences, so this suite still covers the shared block.
    """
    shared_columns = set(
        hpp_rf.CATEGORICAL_COLUMNS
        + hpp_rf.NUMERIC_COLUMNS
        + ["latitude", "longitude"]
        + hpp_rf.COMP_STAT_COLUMNS
        + hpp_rf.TRANSIT_STAT_COLUMNS
        + hpp_rf.MACRO_STAT_COLUMNS
        + hpp_rf.ENGINEERED_COLUMNS
        + hpp_rf.PTAL_DEPENDENT_COLUMNS
        + hpp_rf.RIVER_DEPENDENT_COLUMNS
        + hpp_rf.PREV_SALE_DEPENDENT_COLUMNS
        + hpp_rf.SAME_STREET_STAT_COLUMNS
        + hpp_rf.NEIGHBOURHOOD_STAT_COLUMNS
        + hpp_rf.MSOA_TREND_STAT_COLUMNS
        + hpp_rf.OFSTED_COLUMNS
    )
    assert shared_columns == set(hpp_rf.FEATURE_COLUMNS)

    catboost_only = set(hpp_cb.IOD_NUMERIC_COLUMNS) | set(
        hpp_cb.IOD_ORDINAL_COLUMNS
    ) | set(hpp_cb.IOD_CATEGORICAL_COLUMNS) | set(
        hpp_cb.MORTGAGE_RATE_DEPENDENT_COLUMNS
    ) | {"floor_level_ord"}

    rf_flag_groups = {
        tuple(getattr(hpp_rf, name))
        for name in dir(hpp_rf)
        if name.endswith("_DEPENDENT_COLUMNS")
    }
    for flag_col, group in hpp_cb._MISSING_FLAG_GROUPS.items():
        if tuple(group) in rf_flag_groups:
            continue
        assert set(group) <= catboost_only | set(hpp_rf.FEATURE_COLUMNS), (
            f"{flag_col} depends on column(s) neither model declares: "
            f"{sorted(set(group) - catboost_only - set(hpp_rf.FEATURE_COLUMNS))}"
        )


def test_target_and_identifiers_are_not_features():
    """Check the price and date-ordered identifiers such as ``ppd_id`` are not features."""
    for column in (hpp_rf.TARGET_COLUMN, hpp_rf.DATE_COLUMN, "ppd_id", "address_id", "uprn"):
        assert column not in hpp_rf.FEATURE_COLUMNS




# Over budget on hpdbldn at 0034, measured over 1,540,509 sales from 2009. The
# certificates leave these fields blank on 6-20 % of lodgements a year, and
# 0025's certificate fallback and 0022's no-EPC homes add sales without them,
# so the budgets are not met.
MISSING_BUDGET_DEFECTS = {
    "number_habitable_rooms": "16.86% NULL (259,726/1,540,509), budget 16%",
    "current_energy_rating": "6.72% NULL (103,599/1,540,509), budget 6%",
    "mains_gas_flag": "22.31% NULL (343,620/1,540,509), budget 8%",
    "tenure": "35.99% NULL (554,441/1,540,509), budget 17%",
    "multi_glaze_proportion": "11.69% NULL (180,159/1,540,509), budget 8%",
    "extension_count": "22.18% NULL (341,733/1,540,509), budget 8%",
}

# hpdbldn at 0034: postcode district E22 (245 sales) has no
# certificate-derived tenure or mains-gas value at all.
DEAD_DISTRICT_DEFECTS = {
    "mains_gas_flag": "100% NULL across all 245 rows in E22",
    "tenure": "100% NULL across all 245 rows in E22",
}


@pytest.mark.parametrize("column", [_param(c, MISSING_BUDGET_DEFECTS) for c in RAW_FEATURE_COLUMNS])
def test_missing_fraction_within_budget(view_engine, epc_era_row_count, column):
    """Check each column's EPC-era missing share is within its budget."""
    missing = _scalar(
        view_engine,
        f'SELECT count(*) FROM {VIEW} WHERE {EPC_ERA_PREDICATE} AND "{column}" IS NULL',
    )
    fraction = missing / epc_era_row_count
    assert fraction <= MAX_MISSING_FRACTION[column], (
        f"{column} is {fraction:.4%} NULL ({missing}/{epc_era_row_count}) over "
        f"sales from {EPC_ERA_START_YEAR} onward, budget "
        f"{MAX_MISSING_FRACTION[column]:.4%}"
    )


@pytest.mark.parametrize(
    "column", [_param(c, DEAD_DISTRICT_DEFECTS) for c in ("build_form", "mains_gas_flag", "tenure", "lot_area_sqm")]
)
def test_missingness_by_borough_has_no_dead_district(view_engine, column):
    """
    Check no district has a column entirely missing.

    Otherwise the missing flag silently encodes the district.
    """
    worst = _rows(
        view_engine,
        f'''SELECT postcode_district, count(*) n,
                   count(*) FILTER (WHERE "{column}" IS NULL)::float / count(*) frac
            FROM {VIEW} GROUP BY 1 HAVING count(*) >= :min_rows
            ORDER BY 3 DESC LIMIT 3''',
        min_rows=MIN_GROUP_ROWS,
    )
    assert worst, "no postcode_district met the minimum-support threshold"
    district, n, frac = worst[0]
    assert frac < 1.0, (
        f"{column} is 100% NULL across all {n} rows in postcode_district "
        f"{district} — systematic, not incidental, missingness"
    )


@pytest.mark.parametrize(
    "column", [_param(c) for c in ("build_form", "mains_gas_flag", "tenure", "lot_area_sqm")]
)
def test_missingness_by_year_has_no_dead_period(view_engine, column):
    """
    Check no EPC-era year has a column entirely missing.

    Otherwise a chronological split could train with a feature the test block
    never has.
    """
    worst = _rows(
        view_engine,
        f'''SELECT extract(year FROM dateoftransfer)::int y, count(*) n,
                   count(*) FILTER (WHERE "{column}" IS NULL)::float / count(*) frac
            FROM {VIEW} WHERE {EPC_ERA_PREDICATE} GROUP BY 1 HAVING count(*) >= :min_rows
            ORDER BY 3 DESC LIMIT 3''',
        min_rows=MIN_GROUP_ROWS,
    )
    assert worst, "no sale year met the minimum-support threshold"
    year, n, frac = worst[0]
    assert frac < 1.0, (
        f"{column} is 100% NULL across all {n} sales in {year} — the feature "
        f"does not exist for that period"
    )




@pytest.mark.parametrize("column", [_param(c) for c in RAW_FEATURE_COLUMNS])
def test_column_has_more_than_one_distinct_value(view_engine, column):
    """Check each column has more than one distinct value."""
    distinct = _scalar(
        view_engine, f'SELECT count(DISTINCT "{column}") FROM {VIEW}'
    )
    assert distinct >= MIN_DISTINCT_VALUES, (
        f"{column} has {distinct} distinct value(s) — constant feature"
    )


def test_high_cardinality_categoricals_are_the_expected_ones(view_engine):
    """
    Check only the geography categoricals are high-cardinality.

    Unexpected high cardinality memorises the target under CatBoost's
    ordered statistics and splits near-uniquely in a forest.
    """
    expected_high_cardinality = {"postcode_district", "postcode_sector"}
    for column in CATEGORICAL_DOMAINS:
        distinct = _scalar(view_engine, f'SELECT count(DISTINCT "{column}") FROM {VIEW}')
        assert distinct <= 20, f"{column} has {distinct} distinct values"
    for column in expected_high_cardinality:
        distinct = _scalar(view_engine, f'SELECT count(DISTINCT "{column}") FROM {VIEW}')
        assert distinct > 20, f"{column} unexpectedly collapsed to {distinct} values"


def test_floor_level_uses_one_canonical_encoding(view_engine):
    """Check ``floor_level`` has one canonical spelling per storey."""
    values = [
        r[0]
        for r in _rows(
            view_engine, f"SELECT DISTINCT floor_level FROM {VIEW} WHERE floor_level IS NOT NULL"
        )
    ]
    numeric_style = {v for v in values if re.fullmatch(r"-?[0-9]{1,2}", v)}
    ordinal_style = {v for v in values if re.fullmatch(r"[0-9]{1,2}(st|nd|rd|th)", v)}
    assert not (numeric_style and ordinal_style), (
        f"floor_level mixes {len(numeric_style)} numeric encodings "
        f"({sorted(numeric_style)[:5]}...) with {len(ordinal_style)} ordinal ones "
        f"({sorted(ordinal_style)[:5]}...) for the same storeys"
    )
    unexpected = sorted(set(values) - set(FLOOR_LEVEL_CANONICAL))
    assert not unexpected, (
        f"floor_level holds {len(unexpected)} value(s) outside the canonical "
        f"set: {unexpected[:10]}"
    )


def test_the_floor_level_normalisation_covers_every_stored_spelling(view_engine):
    """Check the floor-level mapping recognises every spelling actually stored."""
    values = {
        r[0]
        for r in _rows(
            view_engine,
            f"SELECT DISTINCT floor_level FROM {VIEW} WHERE floor_level IS NOT NULL",
        )
    }
    unmapped = sorted(
        v for v in values
        if v not in FLOOR_LEVEL_MAPPING and v not in FLOOR_LEVEL_CANONICAL
    )
    assert not unmapped, (
        f"{len(unmapped)} floor_level spelling(s) have no entry in "
        f"constants.FLOOR_LEVEL_MAPPING and would normalise to NULL: {unmapped}"
    )




@pytest.mark.parametrize(
    "column",
    [
        _param(c, {"comp_txn_count": "hpdbldn at 0034: 3,200,279 of 3,204,539 rows "
                                     "(99.87%) carry the value 5 even with the 2 km "
                                     "COMPS_RADIUS_M bound migrations 0014/0020 apply: "
                                     "five earlier sales lie within 2 km of almost every "
                                     "London home, so the count says nothing the median "
                                     "does not"})
        for c in RAW_FEATURE_COLUMNS
    ],
)
def test_no_feature_is_near_zero_variance(view_engine, row_count, column):
    """Check no feature is dominated by a single value."""
    top_fraction = _scalar(
        view_engine,
        f'''SELECT count(*)::float / :total FROM {VIEW}
            WHERE "{column}" IS NOT NULL GROUP BY "{column}"
            ORDER BY 1 DESC LIMIT 1''',
        total=row_count,
    )
    assert top_fraction <= NEAR_ZERO_VARIANCE_TOP_VALUE_FRACTION, (
        f"{column}: one value covers {top_fraction:.4%} of rows "
        f"(threshold {NEAR_ZERO_VARIANCE_TOP_VALUE_FRACTION:.2%})"
    )




@pytest.mark.xfail(reason="known data defect -- hpdbldn at 0034: 20,198 rows (0.63 %) have a price/m2 outside £500-£30,000, budget 0.1 %", strict=True)
def test_price_per_square_metre_has_no_impossible_values(view_engine, row_count):
    """
    Check price per m² rarely reaches impossible values.

    A share tolerance, not zero, because prime central London flats can
    exceed £20k/m².
    """
    impossible = _scalar(
        view_engine,
        f"SELECT count(*) FROM {VIEW} "
        f"WHERE price / total_floor_area > 30000 OR price / total_floor_area < 500",
    )
    fraction = impossible / row_count
    assert fraction <= 0.001, (
        f"{impossible} rows ({fraction:.4%}) have an implausible price/m2 "
        f"(<£500 or >£30,000)"
    )


def test_price_tail_is_not_a_single_pileup(view_engine, row_count):
    """Check the price tail is not a pile-up at a clipped bound."""
    max_price, at_max = _rows(
        view_engine,
        f"SELECT max(price), count(*) FILTER (WHERE price = (SELECT max(price) FROM {VIEW})) FROM {VIEW}",
    )[0]
    assert at_max / row_count < 0.001, (
        f"{at_max} rows sit exactly at the maximum price £{max_price:,} — "
        f"the target looks capped rather than observed"
    )


@pytest.mark.parametrize("column", [_param(c) for c in ("total_floor_area", "lot_area_sqm")])
def test_area_columns_are_strictly_positive(view_engine, column):
    """Check floor and lot areas are strictly positive, since both feed divisions."""
    count, sample = _violating(view_engine, f'"{column}" <= 0')
    assert count == 0, f"{count} rows have {column} <= 0, e.g. {sample}"




@pytest.mark.parametrize(
    "column",
    [_param(c, RANGE_DEFECTS) for c in NUMERIC_DOMAIN_RANGES],
)
def test_numeric_column_within_domain_range(view_engine, column):
    """
    Check numeric columns stay within their domain over the EPC era.

    Scoped because pre-2009 comparables have no EPC neighbours and fall
    outside any sensible domain.
    """
    low, high = NUMERIC_DOMAIN_RANGES[column]
    observed_low, observed_high = _rows(
        view_engine,
        f'SELECT min("{column}"), max("{column}") FROM {VIEW} WHERE {EPC_ERA_PREDICATE}',
    )[0]
    assert float(observed_low) >= low and float(observed_high) <= high, (
        f"{column} spans [{observed_low}, {observed_high}], outside the "
        f"domain range [{low}, {high}]"
    )


@pytest.mark.parametrize("column", [_param(c) for c in CATEGORICAL_DOMAINS])
def test_categorical_column_within_domain(view_engine, column):
    observed = {
        r[0]
        for r in _rows(
            view_engine, f'SELECT DISTINCT "{column}" FROM {VIEW} WHERE "{column}" IS NOT NULL'
        )
    }
    unexpected = observed - CATEGORICAL_DOMAINS[column]
    assert not unexpected, f"{column} holds undeclared value(s) {sorted(unexpected)}"


@pytest.mark.parametrize(
    "column,pattern",
    [
        ("postcode_district", POSTCODE_DISTRICT_RE),
        ("postcode_sector", POSTCODE_SECTOR_RE),
        ("construction_age_band", CONSTRUCTION_AGE_BAND_RE),
    ],
    ids=["postcode_district", "postcode_sector", "construction_age_band"],
)
def test_string_column_matches_its_format(view_engine, column, pattern):
    """Check the postcode and construction-age-band columns match the formats their parsers expect."""
    bad = [
        r[0]
        for r in _rows(
            view_engine,
            f'SELECT DISTINCT "{column}" FROM {VIEW} WHERE "{column}" !~ :re LIMIT 10',
            re=pattern.pattern,
        )
    ]
    assert not bad, f"{column} holds malformed value(s) {bad}"


@pytest.mark.xfail(reason="known data defect -- hpdbldn at 0034: a construction_age_band parses to 2202, past the 2100 bound (range 1685-2202)", strict=True)
def test_construction_age_band_parses_to_a_usable_year(view_engine):
    """Check every construction age band parses to a finite year."""
    bands = [
        r[0]
        for r in _rows(
            view_engine,
            f"SELECT DISTINCT construction_age_band FROM {VIEW} "
            "WHERE construction_age_band IS NOT NULL",
        )
    ]
    midpoints = hpp_rf._construction_age_band_midpoint(pd.Series(bands, dtype="string"))
    unparsed = [b for b, m in zip(bands, midpoints) if not np.isfinite(m)]
    assert not unparsed, f"construction_age_band value(s) {unparsed} do not parse to a year"
    assert midpoints.min() >= 1500 and midpoints.max() <= 2100




def test_no_duplicate_transactions_for_the_same_property_and_day(view_engine):
    """Check no property sells twice on one day for one price."""
    duplicates = _rows(
        view_engine,
        f"""SELECT uprn, dateoftransfer, price, count(*) n, array_agg(ppd_id) ids
            FROM {VIEW} GROUP BY 1, 2, 3 HAVING count(*) > 1
            ORDER BY 4 DESC LIMIT 5""",
    )
    total_extra = _scalar(
        view_engine,
        f"""SELECT COALESCE(sum(n - 1), 0) FROM (
                SELECT count(*) n FROM {VIEW} GROUP BY uprn, dateoftransfer, price
                HAVING count(*) > 1) t""",
    )
    assert not duplicates, (
        f"{int(total_extra)} redundant transaction row(s); worst offenders {duplicates}"
    )


def test_ppd_id_is_unique(view_engine, row_count):
    """Check ``ppd_id`` is unique, so no join fans out."""
    distinct = _scalar(view_engine, f"SELECT count(DISTINCT ppd_id) FROM {VIEW}")
    assert distinct == row_count, (
        f"{row_count - distinct} row(s) duplicate a ppd_id — a join fanned out"
    )


MIN_REPEAT_SALE_FRACTION = 0.25


def test_repeat_sales_of_one_property_are_reported(view_engine, row_count):
    """
    Check the repeat-sale share is large enough to require the grouped split.

    A floor, not a ceiling: the share (76.0% of 1,923,529) grows with the
    panel's length.
    """
    repeat_rows = _scalar(
        view_engine,
        f"""SELECT count(*) FROM {VIEW} WHERE uprn IN (
                SELECT uprn FROM {VIEW} GROUP BY uprn HAVING count(*) > 1)""",
    )
    fraction = repeat_rows / row_count
    assert fraction >= MIN_REPEAT_SALE_FRACTION, (
        f"only {fraction:.1%} of rows are repeat sales ({repeat_rows}/"
        f"{row_count}) — below the {MIN_REPEAT_SALE_FRACTION:.0%} that makes "
        f"--split-method grouped worth its cost; check the UPRN join before "
        f"relaxing this"
    )




@pytest.mark.xfail(reason="known data defect -- hpdbldn at 0034: 21,395 sales predate their property's construction band", strict=True)
def test_sale_date_is_not_before_construction(view_engine):
    """Check no property sells before its construction age band begins."""
    count, sample = _violating(
        view_engine,
        """extract(year FROM dateoftransfer) < CASE
               WHEN construction_age_band ~ '^[0-9]{4}$' THEN construction_age_band::int
               ELSE split_part(construction_age_band, '-', 1)::int END""",
    )
    assert count == 0, f"{count} sale(s) predate the property's construction band, e.g. {sample}"


def test_construction_age_band_ranges_are_ordered(view_engine):
    r"""
    Check every ``YYYY-YYYY`` construction age band ends after it starts.

    The range is extracted with ``\d{4}-\d{4}`` first, because stored bands
    may carry EPC register prefixes.
    """
    bad = _rows(
        view_engine,
        f"""SELECT DISTINCT construction_age_band FROM {VIEW}
            WHERE substring(construction_age_band from '[0-9]{{4}}-[0-9]{{4}}') IS NOT NULL
              AND split_part(substring(construction_age_band from '[0-9]{{4}}-[0-9]{{4}}'), '-', 1)::int
                  > split_part(substring(construction_age_band from '[0-9]{{4}}-[0-9]{{4}}'), '-', 2)::int""",
    )
    assert not bad, f"construction_age_band range(s) run backwards: {bad}"


def test_flats_are_leasehold_far_more_often_than_houses(view_engine):
    """Check flats are leasehold far more often than houses."""
    rows = dict(
        (r[0], float(r[1]))
        for r in _rows(
            view_engine,
            f"""SELECT CASE WHEN propertytype = 'F' THEN 'flat' ELSE 'house' END,
                       count(*) FILTER (WHERE duration = 'L')::float / count(*)
                FROM {VIEW} GROUP BY 1""",
        )
    )
    assert rows["flat"] > 0.9, f"only {rows['flat']:.1%} of flats are leasehold"
    assert rows["flat"] > rows["house"]


@pytest.mark.xfail(reason="known data defect -- hpdbldn at 0034: 11,452 rows (0.36 %) have a floor area per habitable room outside 3-150 m2, budget 0.1 %", strict=True)
def test_area_per_room_is_physically_plausible(view_engine, row_count):
    """Check floor area per habitable room is physically plausible."""
    implausible = _scalar(
        view_engine,
        f"""SELECT count(*) FROM {VIEW}
            WHERE total_floor_area / GREATEST(number_habitable_rooms, 1) NOT BETWEEN 3 AND 150""",
    )
    assert implausible / row_count <= 0.001, (
        f"{implausible} rows have an implausible floor area per habitable room "
        f"(outside 3-150 m2)"
    )


def test_comp_count_and_comp_price_agree(view_engine):
    """Check comparable counts and prices agree on which sales qualified."""
    inconsistent = _scalar(
        view_engine,
        f"""SELECT count(*) FROM {VIEW}
            WHERE (comp_txn_count > 0) <> (comp_median_price IS NOT NULL)""",
    )
    assert inconsistent == 0, (
        f"{inconsistent} rows disagree between comp_txn_count and comp_median_price"
    )


DEPENDENT_COLUMN_FAMILIES = {
    "location": ("latitude", "longitude"),
    "comp": ("comp_median_price", "comp_median_price_per_sqm"),
    "ptal": ("ptal_band_rank", "ptal_ai"),
}

DEPENDENT_COLUMN_DEFECTS = {
    "comp": (
        "10,963 EPC-era rows (987,812 across the whole view) have "
        "comp_median_price present while comp_median_price_per_sqm is NULL. "
        "The two are computed from the same neighbour set but the per-sqm "
        "figure additionally needs those neighbours to carry a floor area, "
        "so it goes NULL on its own wherever the comparables have no EPC "
        "record. comp_missing is _missing_indicator over both columns, so "
        "the flag is set for those rows -- but the median fill still writes "
        "a train median into a comp_median_price that was actually observed, "
        "which is the mislabelling this check is about"
    ),
}


@pytest.mark.parametrize(
    "family",
    [_param(f, DEPENDENT_COLUMN_DEFECTS) for f in DEPENDENT_COLUMN_FAMILIES],
)
def test_dependent_columns_are_missing_together(view_engine, family):
    """
    Check each dependent-column family is NULL together, as its shared flag assumes.

    Measured over the EPC era, when per-m² comparables have certificates.
    """
    first, second = DEPENDENT_COLUMN_FAMILIES[family]
    mismatched = _scalar(
        view_engine,
        f'SELECT count(*) FROM {VIEW} WHERE {EPC_ERA_PREDICATE} '
        f'AND ("{first}" IS NULL) <> ("{second}" IS NULL)',
    )
    assert mismatched == 0, (
        f"{mismatched} rows have {first} and {second} missing separately, but "
        f"the {family}_missing flag is derived from {first} alone"
    )




def test_no_transaction_is_dated_in_the_future(view_engine):
    count, sample = _violating(view_engine, "dateoftransfer > CURRENT_DATE")
    assert count == 0, f"{count} transaction(s) dated in the future, e.g. {sample}"


def test_transaction_dates_cover_a_contiguous_period(view_engine):
    """Check sale years form a contiguous period."""
    years = [
        r[0]
        for r in _rows(
            view_engine,
            f"SELECT DISTINCT extract(year FROM dateoftransfer)::int FROM {VIEW} ORDER BY 1",
        )
    ]
    assert years == list(range(years[0], years[-1] + 1)), (
        f"sale years are not contiguous: {years}"
    )


@pytest.mark.parametrize(
    "source",
    [
        _param(
            s,
            {
                "production.london_stations": (
                    "the station list is a current snapshot with no opening date, so "
                    "nearest_station_distance_km/nearest_station_zone credit a 2007 "
                    "sale with stations that opened later (the Elizabeth line, 2022). "
                    "572,748 of 620,538 rows (92%) sold before that snapshot's vintage"
                ),
                "staging.ptal_2023": (
                    "PTAL is the 2023 grid only, joined to sales back to 2007-01-02, "
                    "so ptal_band_rank/ptal_ai are future accessibility on 92% of rows. "
                    "TfL publishes PTAL per year; an as-of join needs the historical "
                    "grids loaded and a validity year to join on. NOT FIXABLE IN CODE "
                    "ALONE: unlike the EPC join (fixed by migration 0033, which "
                    "recovered a certificate history that was already in "
                    "staging.london_houses), the historical PTAL grids are not in this "
                    "repository at all. Closing this means fetching them from TfL, "
                    "loading them keyed by validity year, and switching the join to a "
                    "LATERAL 'most recent grid on or before dateoftransfer' -- a data "
                    "acquisition task, not a code change. The same is true of "
                    "production.london_stations, whose opening dates the current source "
                    "does not carry"
                ),
            },
        )
        for s in AS_OF_REFERENCE_SOURCES
    ],
)
def test_reference_source_supports_an_as_of_join(view_engine, source):
    """
    Check a reference source has a date column an as-of join can filter on.

    Without one it can only be joined as a snapshot, describing past sales
    with a world that did not yet exist.
    """
    schema, table = source.split(".")
    columns = {
        r[0]
        for r in _rows(
            view_engine,
            """SELECT a.attname FROM pg_attribute a
               JOIN pg_class c ON c.oid = a.attrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = :schema AND c.relname = :table
                 AND a.attnum > 0 AND NOT a.attisdropped""",
            schema=schema,
            table=table,
        )
    }
    assert columns, f"{source} is not present on hpdbldn"
    temporal = {c for c in columns if re.search(r"date|period|year|valid|opened", c)}
    _, dependent_features = AS_OF_REFERENCE_SOURCES[source]
    assert temporal, (
        f"{source} has no temporal column, so {list(dependent_features)} cannot be "
        f"joined as of each row's dateoftransfer; columns are {sorted(columns)}"
    )


def test_as_of_reference_joins_never_look_forward(view_engine):
    """Check as-of reference joins use the value in force on the sale date, re-derived independently."""
    lookahead = _scalar(
        view_engine,
        f"""SELECT count(*) FROM (
                SELECT lh.ppd_id, lh.dateoftransfer, lh.boe_rate_at_sale,
                       (SELECT br.rate FROM production.boe_rate br
                        WHERE br.date_changed <= lh.dateoftransfer
                        ORDER BY br.date_changed DESC LIMIT 1) AS expected
                FROM {VIEW} lh
                ORDER BY lh.ppd_id LIMIT 20000) t
            WHERE t.boe_rate_at_sale IS DISTINCT FROM t.expected""",
    )
    assert lookahead == 0, (
        f"{lookahead} sampled rows carry a boe_rate_at_sale other than the rate "
        f"in force on their own dateoftransfer"
    )


def test_ofsted_features_never_see_an_unpublished_or_ended_rating(hpdbldn_ro):
    """
    Check every sale's school features read only ratings in force on its date.

    Re-derived from ``production.ofsted_rating_spells``: the nearest spells a
    sale used, and every spell within its radius the counts could have used.
    """
    engine = hpdbldn_ro
    assert _scalar(engine, "SELECT to_regclass('production.ofsted_features') IS NOT NULL"), (
        "hpdbldn is before migration 0034"
    )
    leaks = _scalar(
        engine,
        """SELECT count(*) FROM production.ofsted_features f
             JOIN production.ppd_table p ON p.id = f.ppd_id
             JOIN production.ofsted_rating_spells s
               ON s.id IN (f.ofsted_primary_spell_id, f.ofsted_secondary_spell_id)
            WHERE s.valid_from > p.dateoftransfer
               OR (s.valid_to IS NOT NULL AND s.valid_to <= p.dateoftransfer)""",
    )
    assert leaks == 0, f"{leaks} sale(s) use a school rating not in force on their date"
    # Independently: no sale's nearest primary score predates that school's first publication.
    early = _scalar(
        engine,
        """SELECT count(*) FROM (
               SELECT f.ppd_id FROM production.ofsted_features f
                 JOIN production.ppd_table p ON p.id = f.ppd_id
                WHERE f.ofsted_primary_nearest_m IS NOT NULL AND mod(f.ppd_id, 97) = 0
                  AND p.dateoftransfer < (SELECT min(valid_from) FROM production.ofsted_rating_spells)
           ) t""",
    )
    assert early == 0, f"{early} sale(s) before the first publication carry a school rating"
    view_rows = _scalar(
        engine,
        f"""SELECT count(*) FROM (SELECT lh.ppd_id FROM {VIEW} lh
              JOIN production.ofsted_features f ON f.ppd_id = lh.ppd_id
             WHERE lh.ofsted_primary_nearest_score IS DISTINCT FROM f.ofsted_primary_nearest_score
             LIMIT 100) t""",
    )
    assert view_rows == 0, "the view's ofsted columns differ from production.ofsted_features"


@pytest.mark.xfail(reason="known data defect -- hpdbldn at 0034: 1,607,375 rows carry EPC attributes from a certificate inspected after their own sale -- the certificate fallback (0025) prices pre-register sales from a later certificate", strict=True)
def test_epc_attributes_are_never_measured_after_the_sale(view_engine):
    """
    Check no EPC-derived value comes from a certificate inspected after the sale.

    Re-derived from ``production.epc_table`` rather than trusting the view's
    LATERAL join.
    """
    lookahead = _scalar(
        view_engine,
        f"SELECT count(*) FROM {VIEW} WHERE epc_age_at_sale_days < 0",
    )
    assert lookahead == 0, (
        f"{lookahead} rows carry EPC attributes from a certificate inspected "
        f"after their own dateoftransfer"
    )
    mismatched = _scalar(
        view_engine,
        f"""SELECT count(*) FROM (
                SELECT lh.ppd_id, lh.total_floor_area,
                       (SELECT ec.total_floor_area FROM production.epc_table ec
                        WHERE ec.address_id = lh.address_id
                          AND ec.inspection_date <= lh.dateoftransfer
                        ORDER BY ec.inspection_date DESC LIMIT 1) AS expected
                FROM {VIEW} lh
                ORDER BY lh.ppd_id LIMIT 20000) t
            WHERE t.total_floor_area IS DISTINCT FROM t.expected""",
    )
    assert mismatched == 0, (
        f"{mismatched} sampled rows carry a total_floor_area other than the one "
        f"on the newest certificate predating their own sale"
    )


@pytest.mark.xfail(
    reason=(
        "known infra gap, measured on hpldn (the old 84-revision chain) on "
        "2026-09-10 and not yet re-measured on hpdbldn, where 0014/0020 build "
        "comps_features (this test writes, so it runs only with "
        "HPLDN_TEST_DB_WRITES=1) — production.comps_features was last recomputed "
        "by old-chain migration 0036, before create_comps_features_table gained its "
        "COMPS_RADIUS_M/COMPS_MATCH_PROPERTYTYPE bound (this test's own "
        "recompute now applies both, and that cut the sampled mismatch from "
        "101/152 to 2/152). The 2 residual rows are stale comps_features "
        "entries computed under the old unbounded/any-propertytype search, "
        "not a date-ordering leak -- ppd_id 595000 stored 532000 vs a fresh "
        "700000; ppd_id 1100000 stored 685000 vs 675000. Same root cause "
        "and same fix as test_production_tables_promoted: comps_features "
        "needs recomputing against the current contract, deliberately not "
        "done here as a side effect of a test run. This xfail flips to a "
        "failure once that recompute has run, which is the signal to "
        "delete it."
    ),
    strict=True,
)
@pytest.mark.db_write  # deploy_spatial_extension() and a TEMP table
def test_comps_are_drawn_only_from_earlier_sales(view_engine):
    """
    Check comparables are drawn only from earlier sales.

    Re-derives the neighbour set for a sample with the same KNN search, using
    literal parameters rather than the module's, and checks an unrestricted
    search would differ so the test cannot pass vacuously.
    """
    with view_engine.connect() as connection:
        connection.execute(text("SELECT deploy_spatial_extension();"))
        connection.execute(
            text(
                f"""CREATE TEMP TABLE _dq_comps_geo AS
                    SELECT ppd_id, dateoftransfer, price, total_floor_area, propertytype,
                           ST_Transform(ST_SetSRID(ST_MakePoint(longitude, latitude), 4326), 27700) AS geom
                    FROM {VIEW}
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL"""
            )
        )
        connection.execute(text("CREATE INDEX ON _dq_comps_geo USING gist(geom)"))
        connection.execute(text("ANALYZE _dq_comps_geo"))
        sample = connection.execute(
            text(
                f"""SELECT ppd_id, dateoftransfer, comp_median_price
                    FROM {VIEW} WHERE mod(ppd_id, 5000) = 0 AND comp_median_price IS NOT NULL
                    ORDER BY ppd_id"""
            )
        ).fetchall()
        assert len(sample) >= 20, f"only {len(sample)} sampled rows"

        causal_sql = text(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY nn.price),
                      max(nn.dateoftransfer), count(*)
               FROM (SELECT lh2.price, lh2.dateoftransfer
                     FROM _dq_comps_geo lh2, _dq_comps_geo lh1
                     WHERE lh1.ppd_id = :ppd_id
                       AND lh2.propertytype = lh1.propertytype
                       AND lh2.dateoftransfer < lh1.dateoftransfer
                       AND lh2.dateoftransfer >= lh1.dateoftransfer - interval '12 months'
                       AND lh2.ppd_id <> lh1.ppd_id
                       AND ST_DWithin(lh2.geom, lh1.geom, 2000)
                     ORDER BY lh2.geom <-> lh1.geom LIMIT 5) nn"""
        )
        leaky_sql = text(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY nn.price)
               FROM (SELECT lh2.price
                     FROM _dq_comps_geo lh2, _dq_comps_geo lh1
                     WHERE lh1.ppd_id = :ppd_id
                       AND lh2.propertytype = lh1.propertytype
                       AND lh2.ppd_id <> lh1.ppd_id
                       AND ST_DWithin(lh2.geom, lh1.geom, 2000)
                     ORDER BY lh2.geom <-> lh1.geom LIMIT 5) nn"""
        )

        differed = 0
        for ppd_id, sale_date, stored in sample:
            median, latest_comp, n = connection.execute(
                causal_sql, {"ppd_id": ppd_id}
            ).one()
            assert latest_comp is None or latest_comp < sale_date, (
                f"ppd_id {ppd_id} (sold {sale_date}) draws a comp from {latest_comp}"
            )
            assert median == pytest.approx(float(stored), rel=1e-9), (
                f"ppd_id {ppd_id}: stored comp_median_price {stored} does not match "
                f"the strictly-earlier neighbour median {median}"
            )
            leaky = connection.execute(leaky_sql, {"ppd_id": ppd_id}).scalar()
            if leaky is not None and leaky != median:
                differed += 1
        assert differed > 0, (
            "the unrestricted neighbour search returned the same median for every "
            "sampled row, so this test proves nothing about the date restriction"
        )
        connection.execute(text("DROP TABLE _dq_comps_geo"))




@pytest.mark.xfail(
    reason=(
        "known data defect — 4 rows sit outside any London bounding box: "
        "postcode districts CW8 (lat 53.2446, Cheshire), NP19 (lon -2.9859, "
        "Newport), SS1 (Southend) and EN8 (Waltham Cross). The CW8 row's "
        "nearest_station_distance_km is 215.7 km, and its comps/PTAL features "
        "are derived from that wrong point"
    ),
    strict=True,
)
def test_coordinates_are_inside_greater_london(view_engine):
    """Check every coordinate lies inside Greater London."""
    low_lat, high_lat = NUMERIC_DOMAIN_RANGES["latitude"]
    low_lon, high_lon = NUMERIC_DOMAIN_RANGES["longitude"]
    count, sample = _violating(
        view_engine,
        f"latitude NOT BETWEEN {low_lat} AND {high_lat} "
        f"OR longitude NOT BETWEEN {low_lon} AND {high_lon}",
    )
    assert count == 0, f"{count} row(s) geocoded outside Greater London, e.g. {sample}"


@pytest.mark.xfail(
    reason=(
        "known data defect — postcode districts CW8 (Northwich, Cheshire), "
        "NP19 (Newport, Wales) and SS1 (Southend) appear in a London-only "
        "dataset, 1 row each. The LONDON_DISTRICTS borough filter runs on the "
        "borough name in staging, so an address whose borough was recorded as a "
        "London borough but whose postcode is not survives it"
    ),
    strict=True,
)
def test_postcode_districts_are_london_outcodes(view_engine):
    """Check every postcode district is a London outcode, since location priors key on it."""
    areas = "|".join(sorted(LONDON_POSTCODE_AREAS, key=len, reverse=True))
    offenders = _rows(
        view_engine,
        f"""SELECT postcode_district, count(*) FROM {VIEW}
            WHERE postcode_district !~ :re GROUP BY 1 ORDER BY 2 DESC""",
        re=f"^({areas})[0-9]",
    )
    assert not offenders, f"non-London postcode district(s) present: {offenders}"


def test_coordinates_agree_with_the_distance_to_centre_feature(view_engine):
    """Check the distance-to-centre feature agrees with the coordinates."""
    sample = pd.DataFrame(
        _rows(
            view_engine,
            f"SELECT latitude, longitude FROM {VIEW} WHERE mod(ppd_id, 20000) = 0",
        ),
        columns=["latitude", "longitude"],
    )
    frame = _make_dataset(n=len(sample), seed=3).reset_index(drop=True)
    frame["latitude"] = sample["latitude"].to_numpy()
    frame["longitude"] = sample["longitude"].to_numpy()
    engineered = hpp_rf.engineer_features(frame)
    distance = engineered["distance_to_city_center_km"]
    assert (distance >= 0).all()
    outside = int((distance > 35).sum())
    assert outside <= 1, f"{outside} sampled rows are more than 35 km from central London"


def test_ptal_cell_resolution_is_effectively_total(view_engine, row_count):
    """Check nearly every property resolves a PTAL cell."""
    unresolved = _scalar(view_engine, f"SELECT count(*) FROM {VIEW} WHERE ptal_band_rank IS NULL")
    assert unresolved / row_count <= 0.001, (
        f"{unresolved} rows resolved no PTAL cell"
    )




@pytest.mark.parametrize(
    "column",
    [
        _param(c, MISSINGNESS_MECHANISM_DEFECTS)
        for c in ("build_form", "mains_gas_flag", "tenure", "lot_area_sqm", "floor_level")
    ],
)
def test_missingness_does_not_track_price(view_engine, column):
    """
    Check a column's missing rate does not track price.

    A price-dependent missing flag becomes a proxy for the target.
    ``floor_level`` is compared within flats, where its NULLs are not
    structural.
    """
    restriction = "WHERE propertytype = 'F'" if column == "floor_level" else ""
    bands = _rows(
        view_engine,
        f'''SELECT width_bucket(price, 0, 2000000, 4) band, count(*) n,
                   count(*) FILTER (WHERE "{column}" IS NULL) missing,
                   count(*) FILTER (WHERE "{column}" IS NULL)::float / count(*) frac
            FROM {VIEW} {restriction}
            GROUP BY 1 HAVING count(*) >= :min_rows ORDER BY 1''',
        min_rows=MIN_GROUP_ROWS,
    )
    judged = [b for b in bands if b.missing >= MIN_MISSING_ROWS_PER_BAND]
    if len(judged) < 2:
        # Fewer than 2 price bands with MIN_MISSING_ROWS_PER_BAND+ missing
        # rows: the column is too complete for its NULLs to proxy price.
        return
    rates = [float(b.frac) for b in judged]
    ratio = max(rates) / min(rates)
    assert ratio <= MAX_MISSINGNESS_RATIO_ACROSS_PRICE_BANDS, (
        f"{column} missing rate spans {min(rates):.4%}-{max(rates):.4%} across price "
        f"bands ({ratio:.1f}x, limit {MAX_MISSINGNESS_RATIO_ACROSS_PRICE_BANDS}x) — "
        f"the {column}_missing flag is partly a price proxy. Band detail "
        f"(band, rows, missing, rate): "
        f"{[(int(b.band), int(b.n), int(b.missing), round(float(b.frac), 5)) for b in judged]}"
    )


@pytest.mark.parametrize(
    "column", [_param(c) for c in ("build_form", "mains_gas_flag", "tenure", "lot_area_sqm")]
)
def test_missingness_does_not_concentrate_in_one_borough(view_engine, column):
    """Check a column's NULLs do not concentrate in one district."""
    overall = float(
        _scalar(
            view_engine,
            f'SELECT count(*) FILTER (WHERE "{column}" IS NULL)::float / count(*) FROM {VIEW}',
        )
    )
    if overall == 0:
        # No NULLs, so none can concentrate anywhere.
        return
    worst = _rows(
        view_engine,
        f'''SELECT postcode_district, count(*) n,
                   count(*) FILTER (WHERE "{column}" IS NULL)::float / count(*) frac
            FROM {VIEW} GROUP BY 1 HAVING count(*) >= :min_rows ORDER BY 3 DESC LIMIT 1''',
        min_rows=MIN_GROUP_ROWS,
    )
    district, n, frac = worst[0]
    assert float(frac) <= max(10 * overall, 0.25), (
        f"{column} is {float(frac):.2%} missing in postcode_district {district} "
        f"({n} rows) against {overall:.2%} overall"
    )




def _perturb_prices_from(df, cutoff):
    """Return a copy of `df` with every price from `cutoff` onward rewritten."""
    out = df.copy()
    future = pd.to_datetime(out[hpp_rf.DATE_COLUMN]) >= cutoff
    assert future.any() and (~future).any(), "cutoff does not split the frame"
    out.loc[future, hpp_rf.TARGET_COLUMN] = out.loc[future, hpp_rf.TARGET_COLUMN] * 7.0 + 1_000_000.0
    return out, future


def test_engineered_features_are_invariant_to_future_prices():
    """Check engineered features for sales before a cutoff ignore later prices."""
    df = _make_dataset(n=600, seed=11)
    cutoff = pd.to_datetime(df[hpp_rf.DATE_COLUMN]).quantile(0.5)
    perturbed, future = _perturb_prices_from(df, cutoff)

    baseline = hpp_rf.engineer_features(df)
    after = hpp_rf.engineer_features(perturbed)
    past = ~future.to_numpy()

    engineered = [c for c in hpp_rf.ENGINEERED_COLUMNS if c in baseline.columns]
    assert engineered, "no engineered columns produced"
    moved = [
        c
        for c in engineered
        if not baseline.loc[past, c].equals(after.loc[past, c])
    ]
    assert not moved, (
        f"engineered feature(s) {moved} for pre-{cutoff.date()} rows changed when "
        f"only post-cutoff prices were rewritten — they read the future"
    )


def test_rolling_comp_windows_exclude_the_row_own_sale():
    """Check rewriting a row's own price does not move its rolling comparable features."""
    df = _make_dataset(n=400, seed=12)
    baseline, columns = hpp_rf.add_rolling_area_comp_features(df)
    bumped = df.copy()
    target = len(df) - 1
    bumped.iloc[target, bumped.columns.get_loc(hpp_rf.TARGET_COLUMN)] *= 25.0
    after, _ = hpp_rf.add_rolling_area_comp_features(bumped)
    for column in columns:
        assert baseline.iloc[target][column] == pytest.approx(
            after.iloc[target][column], nan_ok=True
        ), f"{column} for a row moved when only that row's own price changed"


def test_postcode_price_stats_are_invariant_to_val_and_test_prices():
    """Check train postcode and sector statistics ignore validation and test prices."""
    df = hpp_rf.engineer_features(_make_dataset(n=600, seed=13))
    train, val, test = hpp_rf.time_series_split(df, 0.6, 0.2)

    def _fit(val_df, test_df):
        t, v, s, _ = hpp_rf.add_postcode_price_stats(train.copy(), val_df.copy(), test_df.copy())
        t, v, s, _ = hpp_rf.add_postcode_sector_price_stats(t, v, s)
        return t

    baseline = _fit(val, test)
    val_bumped = val.copy()
    test_bumped = test.copy()
    val_bumped[hpp_rf.TARGET_COLUMN] *= 9.0
    test_bumped[hpp_rf.TARGET_COLUMN] *= 9.0
    after = _fit(val_bumped, test_bumped)

    for column in hpp_rf.POSTCODE_STAT_COLUMNS + hpp_rf.SECTOR_STAT_COLUMNS:
        pd.testing.assert_series_equal(
            baseline[column], after[column], check_names=False,
            obj=f"train {column} moved with val/test prices",
        )


def test_postcode_price_stats_never_use_a_train_row_own_price():
    """Check a district with one training row falls back to the global median."""
    df = hpp_rf.engineer_features(_make_dataset(n=300, seed=14))
    train, val, test = hpp_rf.time_series_split(df, 0.6, 0.2)
    train = train.copy()
    solo_index = train.index[0]
    train.loc[solo_index, "postcode_district"] = "ZZ9"
    train.loc[solo_index, hpp_rf.TARGET_COLUMN] = 99_000_000.0

    fitted, _, _, _ = hpp_rf.add_postcode_price_stats(train, val.copy(), test.copy())
    statistic = fitted.loc[solo_index, "postcode_median_price"]
    assert statistic != pytest.approx(99_000_000.0), (
        "the only training row in a district received its own price back as that "
        "district's median — the out-of-fold encoding is self-referential"
    )
    assert fitted.loc[solo_index, "postcode_txn_count"] == 0


def test_imputation_medians_are_invariant_to_val_and_test_values():
    """Check imputation medians ignore validation and test values."""
    df = hpp_rf.engineer_features(_make_dataset(n=500, seed=15))
    train, val, test = hpp_rf.time_series_split(df, 0.6, 0.2)
    train, val, test, _ = hpp_rf.add_postcode_price_stats(train, val, test)
    train, val, test, _ = hpp_rf.add_postcode_sector_price_stats(train, val, test)

    baseline, _, _, _, medians, _ = hpp_rf.impute_and_encode(train, val, test)

    corrupted_val, corrupted_test = val.copy(), test.copy()
    for column in hpp_rf.LOT_AREA_DEPENDENT_COLUMNS + hpp_rf.LOCATION_DEPENDENT_COLUMNS + hpp_rf.COMP_DEPENDENT_COLUMNS:
        corrupted_val[column] = 12_345.0
        corrupted_test[column] = 12_345.0
    after, _, _, _, medians_after, _ = hpp_rf.impute_and_encode(train, corrupted_val, corrupted_test)

    np.testing.assert_array_equal(baseline, after)
    assert medians == medians_after


def test_catboost_feature_preparation_shares_the_leakage_contract():
    """Check CatBoost's feature preparation keeps train-only imputation."""
    df = _with_mortgage_features(hpp_rf.engineer_features(_make_dataset(n=500, seed=16)))
    train, val, test = hpp_rf.time_series_split(df, 0.6, 0.2)
    train, val, test, _ = hpp_rf.add_postcode_price_stats(train, val, test)
    train, val, test, _ = hpp_rf.add_postcode_sector_price_stats(train, val, test)

    baseline, _, _, _, medians, _ = hpp_cb.prepare_features(train, val, test)
    corrupted_val, corrupted_test = val.copy(), test.copy()
    for column in hpp_rf.LOT_AREA_DEPENDENT_COLUMNS + hpp_rf.PTAL_DEPENDENT_COLUMNS:
        corrupted_val[column] = 54_321.0
        corrupted_test[column] = 54_321.0
    after, _, _, _, medians_after, _ = hpp_cb.prepare_features(train, corrupted_val, corrupted_test)

    pd.testing.assert_frame_equal(baseline, after)
    assert medians == medians_after


@pytest.mark.parametrize(
    "split_method", ["chronological", "grouped", "purged", "recency"]
)
def test_split_never_places_a_later_sale_before_an_earlier_one(split_method):
    """Check every evaluation row post-dates every training row."""
    df = hpp_rf.engineer_features(_make_dataset(n=600, seed=17))
    args = hpp_rf.build_arg_parser().parse_args([])
    args.split_method = split_method
    train, val, test, _ = hpp_rf.split_dataset(df, args)

    train_max = pd.to_datetime(train[hpp_rf.DATE_COLUMN]).max()
    val_min = pd.to_datetime(val[hpp_rf.DATE_COLUMN]).min()
    test_min = pd.to_datetime(test[hpp_rf.DATE_COLUMN]).min()
    assert train_max <= val_min, f"{split_method}: training data overruns validation"
    assert pd.to_datetime(val[hpp_rf.DATE_COLUMN]).max() <= test_min, (
        f"{split_method}: validation data overruns test"
    )


def test_grouped_split_keeps_a_property_on_one_side_of_the_cut():
    """Check the grouped split keeps each property on one side of the cut."""
    df = hpp_rf.engineer_features(_make_dataset(n=400, seed=18))
    df = df.copy()
    df["uprn"] = np.tile(np.arange(50), len(df) // 50 + 1)[: len(df)]
    train, val, test = hpp_rf.grouped_time_series_split(df, 0.6, 0.2)
    train_uprns = set(train["uprn"])
    assert not train_uprns & set(val["uprn"])
    assert not train_uprns & set(test["uprn"])


def test_no_feature_is_a_deterministic_function_of_the_target():
    """Check no feature perfectly correlates with the target."""
    df = hpp_rf.engineer_features(_make_dataset(n=800, seed=19))
    train, val, test = hpp_rf.time_series_split(df, 0.6, 0.2)
    train, val, test, _ = hpp_rf.add_postcode_price_stats(train, val, test)
    train, val, test, _ = hpp_rf.add_postcode_sector_price_stats(train, val, test)

    target = train[hpp_rf.TARGET_COLUMN].astype(float)
    suspicious = {}
    for column in hpp_rf.FEATURE_COLUMNS:
        values = pd.to_numeric(train[column], errors="coerce")
        if values.notna().sum() < 50 or values.nunique(dropna=True) < 3:
            continue
        correlation = abs(values.corr(target))
        if correlation > 0.995:
            suspicious[column] = correlation
    assert not suspicious, (
        f"feature(s) {suspicious} are near-perfectly correlated with price on the "
        f"training rows — check they are not derived from the target"
    )



MAX_COVERAGE_DRIFT_RATIO = 2.0

COVERAGE_DRIFT_COLUMNS = tuple(
    c for c in RAW_FEATURE_COLUMNS if MAX_MISSING_FRACTION[c] > 0.0
) + ("poststreet",)

# Strict xfails for measured coverage regressions; they fail once re-running
# the enrichment steps fixes them.
COVERAGE_DRIFT_DEFECTS = {
    "build_form": (
        "5.96% NULL over the last 12 months vs 1.08% over the preceding 5 "
        "years (5.52x). Distinct from the others: build_form's rate climbs "
        "through 2025 (1.30% in 2025-Q2, 4.59% in 2025-Q4, 11.43% in "
        "2026-Q1) rather than stepping once, so this looks like a second, "
        "ongoing gap on top of the 2024-Q4 one"
    ),
}


@pytest.mark.parametrize(
    "column", [_param(c, COVERAGE_DRIFT_DEFECTS) for c in COVERAGE_DRIFT_COLUMNS]
)
def test_missingness_has_not_drifted_in_the_most_recent_year(view_engine, column):
    """
    Check each column's NULL rate in the latest 12 months has not drifted.

    Compares with the preceding five years, catching an enrichment step that
    silently stops being re-run for new rows, which concentrates in the
    window a chronological holdout evaluates.
    """
    (end,) = _rows(view_engine, f"SELECT max(dateoftransfer) FROM {VIEW}")[0]
    start = end - relativedelta(months=12)
    recent_n, recent_missing = _rows(
        view_engine,
        f'''SELECT count(*), count(*) FILTER (WHERE "{column}" IS NULL)
            FROM {VIEW} WHERE dateoftransfer >= :start''',
        start=start,
    )[0]
    base_n, base_missing = _rows(
        view_engine,
        f'''SELECT count(*), count(*) FILTER (WHERE "{column}" IS NULL)
            FROM {VIEW}
            WHERE dateoftransfer < :start AND dateoftransfer >= :base_start''',
        start=start,
        base_start=start - relativedelta(years=5),
    )[0]
    assert recent_n >= MIN_GROUP_ROWS and base_n >= MIN_GROUP_ROWS, (
        f"{column}: not enough rows to judge drift ({recent_n} recent, {base_n} base)"
    )
    if recent_missing < MIN_MISSING_ROWS_PER_BAND or base_missing < MIN_MISSING_ROWS_PER_BAND:
        # Too few NULLs in either window to have drifted: the column is
        # (nearly) complete in both, which is the outcome this guards.
        return
    recent_rate = recent_missing / recent_n
    base_rate = base_missing / base_n
    ratio = recent_rate / base_rate
    assert ratio <= MAX_COVERAGE_DRIFT_RATIO, (
        f"{column} is {recent_rate:.2%} NULL in the most recent 12 months "
        f"({recent_missing}/{recent_n}) vs {base_rate:.2%} over the "
        f"preceding 5 years ({base_missing}/{base_n}) — {ratio:.2f}x, limit "
        f"{MAX_COVERAGE_DRIFT_RATIO}x. A column this much staler in the "
        f"newest data than the base rate usually means an enrichment step "
        f"stopped being re-run against grown source data."
    )
