"""
Random Forest baseline for London house-price prediction.

Trains scikit-learn's RandomForestRegressor, or cuML's on a CUDA host, on
``production.london_houses``. Rows are split chronologically (or by one of
several leakage-safe alternatives) so no evaluation row precedes a training
row, and every fitted statistic is fit on training rows only. On the GPU path
data stays in cuDF and cupy; only the fitted forest's Treelite copy lives in
host RAM, which is why GPU depth is capped. See ``scripts/README.md``.

Usage::

    python -m hpldn.backend.scripts.hpp_rf
    python -m hpldn.backend.scripts.hpp_rf --device gpu --n-estimators 500
    python -m hpldn.backend.scripts.hpp_rf --model-out /tmp/hpp_rf.joblib
"""

import argparse
import datetime
import json
import logging
import re
import sys
from collections import OrderedDict
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (
    mean_absolute_error,
    mean_absolute_percentage_error,
    mean_squared_error,
    r2_score,
)
from sklearn.preprocessing import OrdinalEncoder

from hpldn.backend.common.constants import MIN_PLAUSIBLE_PRICE
from hpldn.backend.common.db import get_model_db_url
from hpldn.backend.common.tuning import HostResources, detect_host_resources
from hpldn.backend.common.utils import load_env_vars
from hpldn.backend.scripts import ofsted_features as _ofsted

VIEW_NAME = "production.london_houses"
DATE_COLUMN = "dateoftransfer"
TARGET_COLUMN = "price"

# Which sales a run models. The view keeps every sale, but EPC fields are NULL
# for most pre-2012 ones, so "all" trains on 1995-2015 rows without floor area
# while testing on 2021-2026 rows with it. "epc_matched" keeps only the sales
# with a usable pre-sale certificate.
POPULATION_EPC_MATCHED = "epc_matched"
POPULATION_ALL = "all"
POPULATIONS = (POPULATION_EPC_MATCHED, POPULATION_ALL)
DEFAULT_POPULATION = POPULATION_EPC_MATCHED

# Features no run may silently lose. A split whose rows miss one of these more
# often than CORE_FEATURE_MAX_MISSING_RATE fails (see
# check_core_feature_availability), and hpp_cb's availability guard raises
# rather than dropping one.
#
# The EPC core features come from the certificate, which pre-2009 sales almost
# never have. Under --population all they are optional per training row (the
# rows still teach location, type and market structure) but mandatory in the
# saved model: the scored splits and the final fit keep the ceiling.
EPC_CORE_FEATURES = (
    "total_floor_area",
    "number_habitable_rooms",
    "current_energy_rating",
)
LOCATION_CORE_FEATURES = (
    "propertytype",
    "postcode_district",
    "latitude",
    "longitude",
)
CORE_FEATURES = EPC_CORE_FEATURES + LOCATION_CORE_FEATURES
CORE_FEATURE_MAX_MISSING_RATE = 0.5
CORE_FEATURE_MIN_ROWS = 50

MISSING_CATEGORY = "UNKNOWN"

INTERACTION_COLUMNS = [
    "postcode_propertytype",
    "postcode_duration",
    "propertytype_duration",
    "build_form_propertytype",
]

# Nullable categoricals, filled with the fixed "UNKNOWN" sentinel before any
# split (no leakage). `oldnew` is HM Land Registry's first-sale-of-a-new-build
# flag. engineer_features fills every raw CATEGORICAL_COLUMNS entry the same
# way, since the EPC LEFT JOIN leaves current_energy_rating NULL too (BLK-1).
NULLABLE_CATEGORICAL_COLUMNS = [
    "build_form",
    "mains_gas_flag",
    "tenure",
    "floor_level",
    "lsoa_code",
    "msoa_code",
    "construction_age_band",
    "oldnew",
]

CATEGORICAL_COLUMNS = [
    "propertytype",
    "duration",
    "postcode_district",
    "postcode_sector",
    "construction_age_band_propertytype",
    "current_energy_rating",
] + NULLABLE_CATEGORICAL_COLUMNS + INTERACTION_COLUMNS
NUMERIC_COLUMNS = [
    "number_habitable_rooms",
    "total_floor_area",
    "lot_area_sqm",
    "multi_glaze_proportion",
    "extension_count",
]

NUMERIC_DEPENDENT_COLUMNS = [
    "number_habitable_rooms",
    "total_floor_area",
    "multi_glaze_proportion",
    "extension_count",
    "area_per_room",
    "log_floor_area",
    "property_age_years",
]

LOT_AREA_DEPENDENT_COLUMNS = ["lot_area_sqm", "lot_to_floor_ratio", "log_lot_area"]

LOCATION_DEPENDENT_COLUMNS = ["latitude", "longitude", "distance_to_city_center_km"]

COMP_DEPENDENT_COLUMNS = ["comp_median_price", "comp_median_price_per_sqm"]

POSTCODE_STAT_COLUMNS = [
    "postcode_median_price",
    "postcode_median_price_per_sqm",
    "postcode_txn_count",
]

SECTOR_STAT_COLUMNS = [
    "postcode_sector_median_price",
    "postcode_sector_median_price_per_sqm",
    "postcode_sector_txn_count",
]

COMP_STAT_COLUMNS = ["comp_median_price", "comp_median_price_per_sqm", "comp_txn_count"]

TRANSIT_STAT_COLUMNS = ["nearest_station_distance_km", "nearest_station_zone"]

MACRO_STAT_COLUMNS = ["boe_rate_at_sale", "ukhpi_avg_price_at_sale"]

PTAL_DEPENDENT_COLUMNS = ["ptal_band_rank", "ptal_ai"]

PREV_SALE_DEPENDENT_COLUMNS = ["prev_sale_price", "months_since_prev_sale"]

RIVER_DEPENDENT_COLUMNS = ["distance_to_river"]

NEIGHBOURHOOD_STAT_COLUMNS = [
    "spatial_lag_price",
    "median_price_per_sqm_20nn_6m",
    "recency_weighted_price",
    "proptype_local_median_800m_6m",
    "floor_area_percentile_local",
    "epc_band_share_a",
    "epc_band_share_b",
    "epc_band_share_c",
    "epc_band_share_d",
    "epc_band_share_e",
    "epc_band_share_f",
    "epc_band_share_g",
]
NEIGHBOURHOOD_DEPENDENT_COLUMNS = NEIGHBOURHOOD_STAT_COLUMNS

MSOA_TREND_STAT_COLUMNS = ["msoa_price_momentum_1y", "msoa_price_momentum_5y"]
MSOA_TREND_DEPENDENT_COLUMNS = MSOA_TREND_STAT_COLUMNS

SAME_STREET_WINDOW_MONTHS = 12
SAME_STREET_STAT_COLUMNS = ["street_sales_count_12m", "street_sales_median_price_12m"]
SAME_STREET_DEPENDENT_COLUMNS = ["street_sales_median_price_12m"]

# The same-street group key. Migration 0025 stores it on the view as
# ``street_key``, computed by the same rules in SQL (`STREET_KEY_SQL`); a frame
# without that column (an older view, a test) derives it from ``poststreet``.
STREET_KEY_COLUMN = "street_key"

ROLLING_COMP_GROUP_COLUMNS = ["postcode_sector", "postcode_district"]
ROLLING_COMP_WINDOWS_MONTHS = (3, 6, 12, 24)

ROLLING_COMP_EB_PRIOR_K = 8.0


def _rolling_comp_column_names(
    group_cols: List[str], windows_months: Tuple[int, ...]
) -> Tuple[List[str], List[str]]:
    """
    Return the column names `add_rolling_area_comp_features` produces.

    Returns
    -------
    price_columns : list of str
        Smoothed price and price-per-m² columns, which need median filling.
    count_columns : list of str
        Transaction counts, which are never missing.
    """
    price_columns, count_columns = [], []
    for window in windows_months:
        for group_col in group_cols:
            price_columns += [
                f"{group_col}_{window}m_comp_median_price",
                f"{group_col}_{window}m_comp_median_price_per_sqm",
            ]
            count_columns.append(f"{group_col}_{window}m_comp_txn_count")
    return price_columns, count_columns


ROLLING_COMP_PRICE_COLUMNS, ROLLING_COMP_COUNT_COLUMNS = _rolling_comp_column_names(
    ROLLING_COMP_GROUP_COLUMNS, ROLLING_COMP_WINDOWS_MONTHS
)
ROLLING_COMP_DEPENDENT_COLUMNS = ROLLING_COMP_PRICE_COLUMNS
ROLLING_COMP_STAT_COLUMNS = ROLLING_COMP_PRICE_COLUMNS + ROLLING_COMP_COUNT_COLUMNS

# Market-relative features (`add_market_relative_features`). The target is
# log(price) - log(UKHPI at sale), while the price features are nominal pounds,
# so a tree had to learn the ratio itself; these are the ratio, in logs.
UKHPI_AT_SALE_COLUMN = "ukhpi_avg_price_at_sale"
UKHPI_AT_PREV_SALE_COLUMN = "ukhpi_at_prev_sale"

# Nominal price features given a ``rel_`` twin: log(x) - log(UKHPI at sale).
MARKET_RELATIVE_SOURCE_COLUMNS = (
    ["comp_median_price"]
    + [c for c in ROLLING_COMP_PRICE_COLUMNS if c.endswith("_comp_median_price")]
    + [
        "spatial_lag_price",
        "recency_weighted_price",
        "proptype_local_median_800m_6m",
        "street_sales_median_price_12m",
    ]
)
MARKET_RELATIVE_PRICE_COLUMNS = [f"rel_{c}" for c in MARKET_RELATIVE_SOURCE_COLUMNS]
# What the comparables' £/m² says this home is worth, relative to the market.
COMPARABLE_IMPLIED_COLUMNS = ["rel_comp_implied_value", "rel_nn20_implied_value"]
# The previous sale indexed to today's market (the repeat-sale signal).
REPEAT_SALE_COLUMNS = ["prev_sale_deflated", "prev_sale_indexed_price"]
MARKET_RELATIVE_COLUMNS = (
    MARKET_RELATIVE_PRICE_COLUMNS + COMPARABLE_IMPLIED_COLUMNS + REPEAT_SALE_COLUMNS
)

# Size-matched comparables (`add_sizeband_comp_features`; `hpp_inference`'s
# `EvidenceStore.sizeband_comp_features` at pricing time). The five nearest
# same-type sales of any size compare a 263 m² house with ~120 m² ones, and at
# the top end £/m² rises with size; these are the median £/m² of same-type
# sales within `SIZEBAND_COMP_RADII_M[0]` over the last
# `SIZEBAND_COMP_WINDOW_DAYS` whose floor area is within
# `SIZEBAND_AREA_TOLERANCE` of the home's (the wider radius when fewer than
# `SIZEBAND_COMP_MIN_SALES`), each moved to the sale date along the home's local
# price index, and that times the home's floor area.
SIZEBAND_COMP_COLUMNS = ["comp_sizeband_pps", "comp_sizeband_implied_value"]
SIZEBAND_COMP_RADII_M: Tuple[float, float] = (1000.0, 2000.0)
SIZEBAND_COMP_MIN_SALES = 5
SIZEBAND_COMP_WINDOW_DAYS = int(round(24 * 30.4375))
SIZEBAND_AREA_TOLERANCE = 0.35
# The previous sale moved to the sale date along the home's local repeat-sales
# index (district by type) rather than London's: prime central London has
# diverged from London, and the London index put a W11 house's 2016 £7m at
# £8.5m where W11's own terraces put it at £7.4m. ``rel_prev_sale_local`` is it
# over the anchor below, a scale-free ratio a £7m sale enters as a £400k one does.
LOCAL_REPEAT_SALE_COLUMNS = ["prev_sale_local_indexed", "rel_prev_sale_local"]
# What the home is worth at the sale date by the simplest evidence, the level an
# ``--target anchored`` model learns a correction to: the size-matched
# comparables' value, else the nearest comparables' £/m² times the floor area,
# else the sector's 24-month median £/m² times it, else the sector's median
# price, else -- only for the first sales of a type, with nothing before them --
# the market's (the UK HPI average for the type at the sale).
ANCHOR_COLUMN = "anchor_value"
ANCHOR_SOURCE_COLUMNS: Tuple[str, ...] = (
    "comp_sizeband_implied_value",
    "comp_median_price_per_sqm*total_floor_area",
    "postcode_sector_24m_comp_median_price_per_sqm*total_floor_area",
    "postcode_sector_24m_comp_median_price",
    UKHPI_AT_SALE_COLUMN,
)
ANCHOR_RELATIVE_COLUMNS = ["rel_comp_sizeband_implied_value", "rel_anchor_value"]
# Model inputs `add_market_relative_features` adds beside `MARKET_RELATIVE_COLUMNS`.
ANCHOR_FEATURE_COLUMNS = SIZEBAND_COMP_COLUMNS + LOCAL_REPEAT_SALE_COLUMNS + ANCHOR_RELATIVE_COLUMNS

UKHPI_PRICE_COLUMN_BY_TYPE = {
    "D": "avg_price_detached",
    "S": "avg_price_semi_detached",
    "T": "avg_price_terraced",
    "F": "avg_price_flats_maisonettes",
}
UKHPI_ALL_TYPES_COLUMN = "avg_price_all_property_types"

PRICE_BAND_EDGES = [0.0, 300_000.0, 600_000.0, 1_000_000.0, 2_000_000.0, np.inf]
PRICE_BAND_LABELS = ["£0-300k", "£300-600k", "£600k-1m", "£1-2m", "£2m+"]

DEFAULT_HIGH_VALUE_THRESHOLD = 2_000_000.0

MIN_HIGH_VALUE_TRAIN_ROWS = 30

EXPLAIN_PRICE_BAND_EDGES = [
    100_000.0, 150_000.0, 200_000.0, 250_000.0, 300_000.0, 400_000.0, 500_000.0,
    600_000.0, 700_000.0, 800_000.0, 900_000.0, 1_000_000.0, 2_000_000.0, np.inf,
]
EXPLAIN_PRICE_BAND_LABELS = [
    "£100k-150k", "£150k-200k", "£200k-250k", "£250k-300k", "£300k-400k",
    "£400k-500k", "£500k-600k", "£600k-700k", "£700k-800k", "£800k-900k",
    "£900k-1M", "£1M-2M", "£2M+",
]

PROPERTY_TYPE_LABELS = [
    "House", "Flat", "Semi-Detached", "End-Terrace", "Mid-Terrace",
    "Detached", "Maisonette",
]
_BUILD_FORM_TO_PROPERTY_TYPE = {
    "Detached": "Detached",
    "Semi-Detached": "Semi-Detached",
    "End-Terrace": "End-Terrace",
    "Enclosed End-Terrace": "End-Terrace",
    "Mid-Terrace": "Mid-Terrace",
    "Enclosed Mid-Terrace": "Mid-Terrace",
}

# Ofsted school features (migration 0034, `ofsted_features`): optional, on by
# default, off with ``--no-ofsted`` (`feature_layout`). The basis is categorical;
# the counts are 0 for a home with no school in range, and every numeric column
# is NaN for a sale without a location, which ``ofsted_missing`` reports.
OFSTED_NUMERIC_COLUMNS = list(_ofsted.NUMERIC_COLUMNS)
OFSTED_CATEGORICAL_COLUMNS = list(_ofsted.CATEGORICAL_COLUMNS)
OFSTED_COLUMNS = OFSTED_NUMERIC_COLUMNS + OFSTED_CATEGORICAL_COLUMNS
OFSTED_DEPENDENT_COLUMNS = OFSTED_NUMERIC_COLUMNS
OFSTED_MISSING_FLAG = "ofsted_missing"

ENGINEERED_COLUMNS = (
    [
        "sale_year",
        "sale_month",
        "property_age_years",
        "area_per_room",
        "log_floor_area",
        "log_lot_area",
        "lot_to_floor_ratio",
        "distance_to_city_center_km",
        "is_house",
        "is_maisonette",
    ]
    + POSTCODE_STAT_COLUMNS
    + SECTOR_STAT_COLUMNS
    + ROLLING_COMP_STAT_COLUMNS
)
# The numeric block of the matrix, after the categoricals, without the optional groups.
MATRIX_NUMERIC_COLUMNS = (
    NUMERIC_COLUMNS
    + ["latitude", "longitude"]
    + COMP_STAT_COLUMNS
    + TRANSIT_STAT_COLUMNS
    + MACRO_STAT_COLUMNS
    + ENGINEERED_COLUMNS
    + PTAL_DEPENDENT_COLUMNS
    + RIVER_DEPENDENT_COLUMNS
    + PREV_SALE_DEPENDENT_COLUMNS
    + SAME_STREET_STAT_COLUMNS
    + NEIGHBOURHOOD_DEPENDENT_COLUMNS
    + MSOA_TREND_DEPENDENT_COLUMNS
)

# is_maisonette is 1/0 for flats with a known flag, 0 for houses and NaN for a
# flat whose flag is unknown, so a shift in unknowns stays visible.
MAISONETTE_DEPENDENT_COLUMNS = ["is_maisonette"]

# Missing-indicator columns appended after FEATURE_COLUMNS, in matrix order:
# flag -> the train-median-filled columns it reports. The single source of that order.
MISSING_FLAG_GROUPS = OrderedDict(
    [
        ("lot_area_missing", LOT_AREA_DEPENDENT_COLUMNS),
        ("location_missing", LOCATION_DEPENDENT_COLUMNS),
        ("comp_missing", COMP_DEPENDENT_COLUMNS),
        ("rolling_comp_missing", ROLLING_COMP_DEPENDENT_COLUMNS),
        ("ptal_missing", PTAL_DEPENDENT_COLUMNS),
        ("river_missing", RIVER_DEPENDENT_COLUMNS),
        ("prev_sale_missing", PREV_SALE_DEPENDENT_COLUMNS),
        ("same_street_missing", SAME_STREET_DEPENDENT_COLUMNS),
        ("neighbourhood_missing", NEIGHBOURHOOD_DEPENDENT_COLUMNS),
        ("msoa_trend_missing", MSOA_TREND_DEPENDENT_COLUMNS),
        ("numeric_missing", NUMERIC_DEPENDENT_COLUMNS),
        ("maisonette_missing", MAISONETTE_DEPENDENT_COLUMNS),
    ]
)


def feature_layout(use_ofsted: bool = True) -> Dict[str, object]:
    """
    Return the feature matrix layout, with or without the Ofsted group.

    Returns
    -------
    dict
        ``categorical`` and ``numeric`` (the encoded and filled blocks, in
        matrix order), ``missing_flag_groups`` (flag -> the columns it
        reports), ``feature_columns`` (categorical then numeric) and
        ``matrix_columns`` (those then the flags): the single source of the
        order `build_feature_matrix` stacks.
    """
    categorical = list(CATEGORICAL_COLUMNS) + (OFSTED_CATEGORICAL_COLUMNS if use_ofsted else [])
    numeric = list(MATRIX_NUMERIC_COLUMNS) + (OFSTED_NUMERIC_COLUMNS if use_ofsted else [])
    groups = OrderedDict(_BASE_MISSING_FLAG_GROUPS)
    if use_ofsted:
        groups[OFSTED_MISSING_FLAG] = OFSTED_DEPENDENT_COLUMNS
    feature_columns = categorical + numeric
    return {
        "categorical": categorical,
        "numeric": numeric,
        "missing_flag_groups": groups,
        "feature_columns": feature_columns,
        "matrix_columns": feature_columns + list(groups),
    }


def require_ofsted_columns(columns) -> None:
    """
    Raise when a frame meant to carry the Ofsted group lacks its columns.

    Raises
    ------
    ValueError
        Naming the missing columns: the view predates migration 0034.
    """
    missing = [c for c in OFSTED_COLUMNS if c not in set(columns)]
    if missing:
        raise ValueError(
            f"the frame lacks the Ofsted columns {missing}: upgrade the database to migration 0034 "
            "(production.ofsted_features) or train with --no-ofsted"
        )


_BASE_MISSING_FLAG_GROUPS = OrderedDict(MISSING_FLAG_GROUPS)
_DEFAULT_LAYOUT = feature_layout(True)

# The default (Ofsted on) layout's names, which the inspection tools read.
FEATURE_COLUMNS = _DEFAULT_LAYOUT["feature_columns"]
MISSING_FLAG_GROUPS = _DEFAULT_LAYOUT["missing_flag_groups"]
MISSING_FLAG_COLUMNS = list(MISSING_FLAG_GROUPS)

# The full matrix impute_and_encode builds by default, and what a saved model records.
FEATURE_MATRIX_COLUMNS = _DEFAULT_LAYOUT["matrix_columns"]

PRICE_ORDERED_CATEGORICAL_COLUMNS = ["propertytype", "tenure"]

LONDON_CENTER_LATITUDE = 51.5074
LONDON_CENTER_LONGITUDE = -0.1278
EARTH_RADIUS_KM = 6371.0

GPU_SQL_CHUNK_ROWS = 250_000

GPU_DTYPE = "float32"

GPU_MAX_DEPTH_DEFAULT = 16


def set_global_seed(seed: int) -> None:
    """
    Seed Python's and NumPy's global RNGs.

    Defence in depth for anything not given an explicit ``random_state``;
    called once at the top of `main`.
    """
    import random

    random.seed(seed)
    np.random.seed(seed)


def get_logger(verbose: bool = False) -> logging.Logger:
    """Return a minimal stdout logger, independent of ``staging.yaml``."""
    logger = logging.getLogger("hpp_rf")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


def _is_cudf(obj) -> bool:
    """Return True for a cuDF DataFrame or Series, i.e. data already on the GPU."""
    return type(obj).__module__.startswith("cudf")


def _is_cupy(obj) -> bool:
    """Return True for a cupy ndarray."""
    return type(obj).__module__.startswith("cupy")


def _read_sql_to_cudf(sql: str, engine, log: Optional[logging.Logger] = None):
    """
    Stream a query into a cuDF DataFrame.

    A server-side cursor yields `GPU_SQL_CHUNK_ROWS`-row batches that are
    converted to cuDF one at a time, so at most one batch is in host RAM.
    """
    import cudf

    parts = []
    with engine.connect() as connection:
        connection = connection.execution_options(stream_results=True)
        for chunk in pd.read_sql(sql, connection, chunksize=GPU_SQL_CHUNK_ROWS):
            for col in chunk.columns:
                if chunk[col].dtype == object:
                    sample = chunk[col].dropna()
                    if not sample.empty and isinstance(
                        sample.iloc[0], (datetime.date, datetime.datetime)
                    ):
                        chunk[col] = pd.to_datetime(chunk[col])
            # An all-NULL batch must not become a string column on the GPU.
            _coerce_chunk_dtypes(chunk, resolve_column_kinds(chunk), "float64")
            parts.append(cudf.from_pandas(chunk))
            del chunk
    if log is not None:
        log.debug(
            f"Streamed {len(parts)} chunk(s) of up to {GPU_SQL_CHUNK_ROWS} rows "
            "from PostgreSQL straight into GPU memory"
        )
    if not parts:
        return cudf.DataFrame()
    return cudf.concat(parts, ignore_index=True)


EXACT_NUMERIC_COLUMNS = frozenset({TARGET_COLUMN, "ppd_id", "address_id", "uprn"})


def _view_column_kinds() -> Dict[str, Optional[str]]:
    """
    Return ``"num"``, ``"str"`` or None for every ``production.london_houses`` column.

    Read from the view's declared types (`models.production.LondonHouses`), so
    a column that is NULL across a whole loaded slice still gets its real type.
    Dates and booleans are None: pandas reads them correctly either way.
    """
    from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String

    from hpldn.backend.models.production import LondonHouses

    kinds: Dict[str, Optional[str]] = {}
    for column in LondonHouses.__table__.columns:
        kind = column.type
        if isinstance(kind, (Boolean, Date, DateTime)):
            kinds[column.name] = None
        elif isinstance(kind, (Integer, Numeric)):
            kinds[column.name] = "num"
        elif isinstance(kind, String):
            kinds[column.name] = "str"
        else:
            kinds[column.name] = None
    return kinds


VIEW_COLUMN_KINDS: Mapping[str, Optional[str]] = _view_column_kinds()


def _series_value_kind(series: pd.Series) -> Optional[str]:
    """
    Classify one chunk's column as ``"num"``, ``"str"`` or None (undecided).

    Chunked reads infer dtypes per batch, so an all-NULL batch would otherwise
    turn a float column into boxed Python objects on concatenation. None means
    the batch cannot say; the first decisive batch types the column, and
    `VIEW_COLUMN_KINDS` types one no batch decides.
    """
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return None
    if pd.api.types.is_numeric_dtype(dtype):
        return "num"
    if dtype != object and pd.api.types.is_string_dtype(dtype):
        return "str"
    if dtype != object:
        return None
    non_null = series.dropna()
    if len(non_null) == 0:
        return None
    value = non_null.iloc[0]
    if isinstance(value, str):
        return "str"
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal, np.integer, np.floating)):
        return "num"
    return None


def _coerce_chunk_dtypes(
    chunk: pd.DataFrame, kinds: Mapping[str, Optional[str]], float_dtype: str
) -> None:
    """
    Coerce one chunk's classified columns in place.

    Numeric columns become `float_dtype` (`EXACT_NUMERIC_COLUMNS` are only
    rescued from object dtype), string columns pandas' string dtype; undecided
    columns are left for a later pass.
    """
    for col, kind in kinds.items():
        if col not in chunk.columns:
            continue
        series = chunk[col]
        if kind == "num":
            if col in EXACT_NUMERIC_COLUMNS:
                if series.dtype == object:
                    chunk[col] = pd.to_numeric(series, errors="coerce")
            elif str(series.dtype) != float_dtype:
                chunk[col] = pd.to_numeric(series, errors="coerce").astype(float_dtype)
        elif kind == "str" and series.dtype == object:
            chunk[col] = series.astype("str")


def resolve_column_kinds(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Classify every column of `df` from its values, falling back to `VIEW_COLUMN_KINDS`."""
    return {
        col: _series_value_kind(df[col]) or VIEW_COLUMN_KINDS.get(col)
        for col in df.columns
    }


def population_sql(population: str) -> str:
    """
    Return the WHERE clause selecting a modelling population.

    Raises
    ------
    ValueError
        For a name outside `POPULATIONS`.
    """
    if population == POPULATION_EPC_MATCHED:
        return "WHERE epc_matched"
    if population == POPULATION_ALL:
        return ""
    raise ValueError(f"Unknown population {population!r}, expected one of {POPULATIONS}")


def dataset_sql(limit: Optional[int] = None, population: str = DEFAULT_POPULATION) -> str:
    """Return the query every model script loads ``production.london_houses`` with."""
    sql = f"SELECT * FROM {VIEW_NAME} {population_sql(population)} ORDER BY {DATE_COLUMN}, ppd_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return " ".join(sql.split())


def load_dataset(
    engine,
    limit: Optional[int] = None,
    log: Optional[logging.Logger] = None,
    use_gpu: bool = False,
    population: str = DEFAULT_POPULATION,
):
    """
    Read ``production.london_houses`` in date order.

    Every column is coerced to its declared type (`resolve_column_kinds`), so
    a numeric column that is NULL across the whole slice, as floor area is for
    1995 rows, arrives as float NaN rather than object None (BLK-1).

    Parameters
    ----------
    population : {"epc_matched", "all"}
        Which sales to model; see `POPULATIONS`.

    Returns
    -------
    DataFrame
        cuDF when `use_gpu` is set, otherwise pandas; downstream functions
        branch on which they receive.
    """
    sql = dataset_sql(limit, population)
    if use_gpu:
        df = _read_sql_to_cudf(sql, engine, log=log)
    else:
        df = pd.read_sql(sql, engine)
        _coerce_chunk_dtypes(df, resolve_column_kinds(df), "float64")
    if log is not None:
        log.info(f"Loaded {len(df)} rows from {VIEW_NAME} (population={population})")
    return df


def check_core_feature_availability(
    splits: Mapping[str, object],
    log: Optional[logging.Logger] = None,
    max_missing_rate: float = CORE_FEATURE_MAX_MISSING_RATE,
    columns: Sequence[str] = CORE_FEATURES,
    population: str = DEFAULT_POPULATION,
    is_final_fit: bool = True,
) -> Dict[str, Dict[str, float]]:
    """
    Log each split's missing rate for the `CORE_FEATURES`, and fail above a ceiling.

    A NaN or the "UNKNOWN" sentinel counts as missing. Splits with fewer than
    `CORE_FEATURE_MIN_ROWS` rows are reported but not held to the ceiling.

    Under ``population="all"`` the `EPC_CORE_FEATURES` of the ``train`` split
    are reported only: pre-2009 sales have no certificate, and those rows
    still carry location and market structure. The scored splits (``val``,
    ``test``) keep the ceiling, except in a cross-validation fold
    (`is_final_fit` False), whose validation block may itself predate
    certificates. `LOCATION_CORE_FEATURES` are always held to it.

    Returns
    -------
    dict
        ``{split: {column: missing rate}}``.

    Raises
    ------
    RuntimeError
        Naming every (split, column) above `max_missing_rate` (a model
        must not train on rows that mostly lack floor area and still report
        metrics).
    """
    rates: Dict[str, Dict[str, float]] = {}
    failing: List[str] = []
    for name, frame in splits.items():
        if frame is None or len(frame) == 0:
            continue
        rates[name] = {}
        for column in columns:
            if column not in frame.columns:
                continue
            values = frame[column]
            missing = values.isna()
            if not pd.api.types.is_numeric_dtype(values.dtype):
                missing = missing | (values == MISSING_CATEGORY)
            rate = float(missing.mean())
            rates[name][column] = rate
            exempt = (
                population == POPULATION_ALL
                and column in EPC_CORE_FEATURES
                and (name == "train" or not is_final_fit)
            )
            if rate > max_missing_rate and len(frame) >= CORE_FEATURE_MIN_ROWS and not exempt:
                failing.append(f"{name}.{column} {rate:.1%}")
    if log is not None:
        for name, by_column in rates.items():
            log.info(
                f"core feature missing rates, {name}: "
                + ", ".join(f"{c} {r:.1%}" for c, r in by_column.items())
            )
    if failing:
        raise RuntimeError(
            f"core feature(s) missing above {max_missing_rate:.0%}: {failing}. The model would "
            "train or be scored without inputs it depends on and still report metrics. Use "
            f"--population {POPULATION_EPC_MATCHED} (the default) to model "
            "sales that carry them (hpp_cb also has --train-from-year)."
        )
    return rates


def _construction_age_band_midpoint(band):
    r"""
    Estimate a construction year from a ``construction_age_band`` string.

    Extracts the ``\d{4}(?:-\d{4})?`` part first, so prefixed register text
    such as ``"England and Wales: 1900-1929"`` parses. A range yields its
    midpoint and a bare year itself; an unparseable band yields NaN. Works on
    pandas and cuDF string Series.
    """
    numeric = band.str.extract(r"(\d{4}(?:-\d{4})?)")[0]
    parts = numeric.str.split("-", n=1, expand=True)
    start = parts[0].astype("float64")
    end = parts[1].astype("float64") if parts.shape[1] > 1 else start
    end = end.fillna(start)
    return (start + end) / 2.0


def engineer_features(df, causal_stats: bool = True):
    """
    Return a copy of `df` with the engineered feature columns added.

    Adds ``sale_year`` and ``sale_month``; ``property_age_years``; floor and lot
    area ratios and logs; the `INTERACTION_COLUMNS`; distance to Charing Cross;
    ``is_house``; ``is_maisonette`` as float (1/0, 0 for houses, NaN for a flat
    whose flag is unknown); "UNKNOWN" fills for every raw
    `CATEGORICAL_COLUMNS` entry; and the causal rolling-comp and same-street
    statistics. Postcode and sector price statistics are not added here: they
    are fit on training rows, after the split. Works on pandas or cuDF.

    With `causal_stats` false the rolling-comp and same-street columns are not
    computed: they are statistics over the other rows of `df`, so a frame of
    unsold properties has to be given them from sales history instead (see
    `hpp_inference`).
    """
    df = df.copy()
    if _is_cudf(df):
        import cudf

        dates = cudf.to_datetime(df[DATE_COLUMN])
    else:
        dates = pd.to_datetime(df[DATE_COLUMN])
    df["sale_year"] = dates.dt.year
    df["sale_month"] = dates.dt.month

    construction_year = _construction_age_band_midpoint(df["construction_age_band"])
    df["property_age_years"] = df["sale_year"] - construction_year

    rooms = df["number_habitable_rooms"].clip(lower=1)
    df["area_per_room"] = df["total_floor_area"] / rooms
    df["log_floor_area"] = np.log1p(df["total_floor_area"])
    df["log_lot_area"] = np.log1p(df["lot_area_sqm"])
    df["lot_to_floor_ratio"] = df["lot_area_sqm"] / df["total_floor_area"]

    df["is_house"] = (df["propertytype"] != "F").astype("int8")

    if _is_cudf(df):
        maisonette = df["is_maisonette"].astype("float64")
    else:
        maisonette = df["is_maisonette"].map({True: 1.0, False: 0.0}).astype("float64")
    df["is_maisonette"] = maisonette.where(df["propertytype"] == "F", 0.0)

    df["months_since_prev_sale"] = df["years_since_prev_sale"] * 12.0

    # Before the interactions, which concatenate these columns.
    for col in CATEGORICAL_COLUMNS + OFSTED_CATEGORICAL_COLUMNS:
        if col in df.columns and col not in INTERACTION_COLUMNS:
            df[col] = df[col].fillna(MISSING_CATEGORY)

    df["postcode_propertytype"] = df["postcode_district"].str.cat(df["propertytype"], sep="_")
    df["postcode_duration"] = df["postcode_district"].str.cat(df["duration"], sep="_")
    df["propertytype_duration"] = df["propertytype"].str.cat(df["duration"], sep="_")

    lat_rad = np.radians(df["latitude"])
    lon_rad = np.radians(df["longitude"])
    center_lat_rad = np.radians(LONDON_CENTER_LATITUDE)
    center_lon_rad = np.radians(LONDON_CENTER_LONGITUDE)
    dlat = lat_rad - center_lat_rad
    dlon = lon_rad - center_lon_rad
    haversine_a = np.sin(dlat / 2.0) ** 2 + np.cos(lat_rad) * np.cos(center_lat_rad) * np.sin(dlon / 2.0) ** 2
    df["distance_to_city_center_km"] = EARTH_RADIUS_KM * 2.0 * np.arcsin(np.sqrt(haversine_a))

    df["build_form_propertytype"] = df["build_form"].str.cat(df["propertytype"], sep="_")

    if causal_stats:
        df = add_same_street_features(df)
        df, _ = add_rolling_area_comp_features(df)
    return df


def _rolling_causal_group_stats(
    df: pd.DataFrame, group_cols: List[str], window_months: int, value_col: str
) -> Tuple[pd.Series, pd.Series]:
    """
    Return the rolling median and count of `value_col` over prior months per group.

    The window is ``[date - window_months, date)`` (``closed="left"``), so a
    row's own sale never enters its statistic and the result is leakage-safe
    before any split.

    Computed by polars' multi-threaded ``rolling_quantile_by(...).over(groups)``
    rather than a pandas rolling window per group. The median is the mean of
    the lower and upper middle values, ``(lo + hi) / 2``, which is how pandas'
    rolling median rounds, so the result is bit for bit the per-group pandas
    one. Rows whose group key is missing get no statistic, as pandas'
    ``groupby`` drops them; NaN values are ignored.

    Returns
    -------
    median, count : pandas.Series
        Aligned to ``df.index``; the count is 0 and the median NaN where the
        window is empty.
    """
    import polars as pl  # noqa: PLC0415 - only the training path needs it

    n = len(df)
    if n == 0:
        return pd.Series(np.nan, index=df.index), pd.Series(0.0, index=df.index)
    window = f"{int(round(window_months * 30.4375))}d"
    columns = {
        "_date": pd.to_datetime(df[DATE_COLUMN]).to_numpy(dtype="datetime64[ns]"),
        "_value": pd.to_numeric(df[value_col], errors="coerce").to_numpy(dtype=np.float64),
        "_pos": np.arange(n, dtype=np.int64),
    }
    keyless = np.zeros(n, dtype=bool)
    group_names = []
    for i, col in enumerate(group_cols):
        # Integer codes: polars groups them faster than strings, and a
        # missing key (-1) is whatever null the column holds.
        codes, _ = pd.factorize(df[col], use_na_sentinel=True)
        keyless |= codes < 0
        name = f"_g{i}"
        columns[name] = codes.astype(np.int64)
        group_names.append(name)
    frame = pl.DataFrame(columns).with_columns(pl.col("_value").fill_nan(None)).sort("_date", maintain_order=True)
    kw = dict(window_size=window, closed="left")
    stats = frame.select(
        "_pos",
        pl.col("_value").rolling_quantile_by("_date", quantile=0.5, interpolation="lower", **kw).over(group_names).alias("lo"),
        pl.col("_value").rolling_quantile_by("_date", quantile=0.5, interpolation="higher", **kw).over(group_names).alias("hi"),
        pl.col("_value").is_not_null().cast(pl.Float64).rolling_sum_by("_date", **kw).over(group_names).alias("n"),
    )
    position = stats["_pos"].to_numpy()
    lo = np.empty(n)
    hi = np.empty(n)
    count = np.empty(n)
    lo[position] = stats["lo"].to_numpy(allow_copy=True).astype(np.float64)
    hi[position] = stats["hi"].to_numpy(allow_copy=True).astype(np.float64)
    count[position] = stats["n"].fill_null(0.0).to_numpy().astype(np.float64)
    median = (lo + hi) / 2
    median[keyless | (count == 0)] = np.nan
    count[keyless] = 0.0
    return pd.Series(median, index=df.index), pd.Series(count, index=df.index)


# A leading flat and house number, as EPC-format streets carry them
# ("Flat 3, 13 The Gardens", "100-102 Sutherland Avenue"). Written with
# explicit ASCII classes so the same pattern means the same thing in Python's
# `re` and PostgreSQL's regex engine: london_houses_view.STREET_KEY_FUNCTION_SQL
# embeds it verbatim, and migration 0025 checks the two agree on every street.
_WS = r"[ \t\n\r\f\v]"
STREET_KEY_PREFIX_PATTERN = (
    rf"^{_WS}*(FLAT{_WS}+[A-Z0-9_]+{_WS}*,?{_WS}*)?"
    rf"([0-9]+[A-Z]?(-[0-9]+[A-Z]?)?(?![A-Z0-9_]){_WS}*,?{_WS}*)?"
)
STREET_KEY_WHITESPACE_PATTERN = rf"{_WS}+"
_STREET_KEY_PREFIX_RE = re.compile(STREET_KEY_PREFIX_PATTERN)
_STREET_KEY_WHITESPACE_RE = re.compile(STREET_KEY_WHITESPACE_PATTERN)


def normalise_street_key(street) -> Optional[str]:
    """
    Return the same-street group key for one ``poststreet`` value, or None.

    Non-breaking spaces read as spaces, then upper-cased, with a leading
    ``FLAT x,`` and a leading house number or range (``13``, ``13A,``,
    ``100-102``) removed and whitespace collapsed, so the Land Registry's
    ``LAUSANNE ROAD`` and the certificate register's ``Lausanne Road`` or
    ``13 The Gardens`` fall in one group. A blank result is None.
    """
    if not isinstance(street, str):
        return None
    key = _STREET_KEY_PREFIX_RE.sub("", street.replace("\xa0", " ").upper(), count=1)
    key = _STREET_KEY_WHITESPACE_RE.sub(" ", key).strip(" ")
    return key or None


def street_key_series(df) -> pd.Series:
    """
    Return each row's same-street key as a pandas Series aligned to `df`.

    The view's ``street_key`` column where it has one, else
    `normalise_street_key` of ``poststreet``.
    """
    derived = df["poststreet"].map(normalise_street_key) if "poststreet" in df.columns else None
    if STREET_KEY_COLUMN in df.columns:
        stored = df[STREET_KEY_COLUMN].astype("object")
        stored = stored.where(stored.notna(), None)
        return stored if derived is None else stored.where(stored.notna(), derived)
    if derived is None:
        return pd.Series(None, index=df.index, dtype="object")
    return derived


def add_same_street_features(
    df, window_months: int = SAME_STREET_WINDOW_MONTHS
):
    """
    Add ``street_sales_count_12m`` and ``street_sales_median_price_12m``.

    Causal statistics grouped by postcode district and `street_key_series`
    through `_rolling_causal_group_stats`. Rows with no street get a NaN
    median and a zero count. Runs on pandas even for a cuDF `df`.
    """
    on_gpu = _is_cudf(df)
    working = df.to_pandas() if on_gpu else df

    augmented = working[[DATE_COLUMN, TARGET_COLUMN, "postcode_district"]].copy()
    augmented["_street_group"] = augmented["postcode_district"].str.cat(
        street_key_series(working), sep="_"
    )
    median, count = _rolling_causal_group_stats(augmented, ["_street_group"], window_months, TARGET_COLUMN)

    out = working.copy()
    out["street_sales_count_12m"] = count
    out["street_sales_median_price_12m"] = median

    if on_gpu:
        import cudf

        out = cudf.from_pandas(out)
    return out


def add_rolling_area_comp_features(
    df: pd.DataFrame,
    group_cols: List[str] = ROLLING_COMP_GROUP_COLUMNS,
    windows_months: Tuple[int, ...] = ROLLING_COMP_WINDOWS_MONTHS,
    eb_prior_k: float = ROLLING_COMP_EB_PRIOR_K,
    log: Optional[logging.Logger] = None,
) -> Tuple:
    """
    Add leakage-safe rolling comparable-sales signals, per property type.

    For each geography in `group_cols` and window in `windows_months`, adds a
    smoothed median price, a smoothed median price per m² and the raw
    transaction count. Statistics use only strictly earlier sales, so no
    per-split refitting is needed. Local medians are shrunk toward the
    citywide median for the same window and type::

        smoothed = (n * local + k * citywide) / (n + k)

    with ``k = eb_prior_k``. Runs on pandas even for a cuDF `df`, and keeps the
    input row order.

    Returns
    -------
    df : DataFrame
        A copy with the new columns.
    new_columns : list of str
        Their names.
    """
    on_gpu = _is_cudf(df)
    working = df.to_pandas() if on_gpu else df

    price_columns, count_columns = _rolling_comp_column_names(group_cols, windows_months)
    new_columns = price_columns + count_columns

    price_per_sqm = working[TARGET_COLUMN] / working["total_floor_area"]
    augmented = working[[DATE_COLUMN, "propertytype"] + group_cols].copy()
    augmented[TARGET_COLUMN] = working[TARGET_COLUMN]
    augmented["_price_per_sqm"] = price_per_sqm

    added = pd.DataFrame(index=working.index)
    for window in windows_months:
        city_median, _ = _rolling_causal_group_stats(augmented, ["propertytype"], window, TARGET_COLUMN)
        city_pps_median, _ = _rolling_causal_group_stats(augmented, ["propertytype"], window, "_price_per_sqm")

        for group_col in group_cols:
            local_median, local_count = _rolling_causal_group_stats(
                augmented, [group_col, "propertytype"], window, TARGET_COLUMN
            )
            local_pps_median, _ = _rolling_causal_group_stats(
                augmented, [group_col, "propertytype"], window, "_price_per_sqm"
            )

            n = local_count.fillna(0.0)
            smoothed_price = (n * local_median.fillna(0.0) + eb_prior_k * city_median) / (n + eb_prior_k)
            smoothed_pps = (n * local_pps_median.fillna(0.0) + eb_prior_k * city_pps_median) / (n + eb_prior_k)

            price_col = f"{group_col}_{window}m_comp_median_price"
            pps_col = f"{group_col}_{window}m_comp_median_price_per_sqm"
            count_col = f"{group_col}_{window}m_comp_txn_count"
            added[price_col] = smoothed_price
            added[pps_col] = smoothed_pps
            added[count_col] = n

    out = working.copy()
    for col in new_columns:
        out[col] = added[col]

    if log is not None:
        log.info(
            f"Added {len(new_columns)} leakage-safe rolling comp columns "
            f"(windows={windows_months}, groups={group_cols}, eb_prior_k={eb_prior_k})"
        )

    if on_gpu:
        import cudf

        out = cudf.from_pandas(out)
    return out, new_columns


def load_ukhpi(engine, log: Optional[logging.Logger] = None) -> pd.DataFrame:
    """
    Read ``production.ukhpi``'s published rows, as the view's as-of lookup sees them.

    Static reference data, loaded once per run for `add_market_relative_features`.
    """
    columns = ", ".join(["period", "published_date", UKHPI_ALL_TYPES_COLUMN, *UKHPI_PRICE_COLUMN_BY_TYPE.values()])
    ukhpi = pd.read_sql(
        f"SELECT {columns} FROM production.ukhpi WHERE published_date IS NOT NULL",
        engine,
    )
    for col in ("period", "published_date"):
        ukhpi[col] = pd.to_datetime(ukhpi[col])
    if log is not None:
        log.info(f"Loaded {len(ukhpi)} rows from production.ukhpi")
    return ukhpi


def ukhpi_as_of(dates, propertytypes, ukhpi: pd.DataFrame) -> np.ndarray:
    """
    Return the UK HPI average price for each (date, property type), as published by then.

    The view's rule for ``ukhpi_avg_price_at_sale``: the latest row with
    ``published_date <= date`` (the later ``period`` on a tie), the property
    type's column, falling back to all property types. NaN before the first
    publication or for a missing date.
    """
    dates = pd.to_datetime(pd.Series(np.asarray(dates)))
    types = pd.Series(np.asarray(propertytypes, dtype=object)).astype("object")
    table = (
        ukhpi.dropna(subset=["published_date"])
        .sort_values(["published_date", "period"])
        .drop_duplicates("published_date", keep="last")
    )
    out = np.full(len(dates), np.nan, dtype=np.float64)
    known = dates.notna().to_numpy()
    if not known.any() or table.empty:
        return out
    left = pd.DataFrame({"_date": dates[known].to_numpy(), "_pos": np.flatnonzero(known)})
    left = left.sort_values("_date", kind="stable")
    merged = pd.merge_asof(left, table, left_on="_date", right_on="published_date", direction="backward")
    fallback = pd.to_numeric(merged[UKHPI_ALL_TYPES_COLUMN], errors="coerce").to_numpy(dtype=np.float64)
    value = fallback.copy()
    row_types = types.to_numpy()[merged["_pos"].to_numpy()]
    for code, column in UKHPI_PRICE_COLUMN_BY_TYPE.items():
        is_type = row_types == code
        if is_type.any():
            typed = pd.to_numeric(merged[column], errors="coerce").to_numpy(dtype=np.float64)
            value[is_type] = np.where(np.isnan(typed[is_type]), fallback[is_type], typed[is_type])
    out[merged["_pos"].to_numpy()] = value
    return out


def _log_ratio(numerator, denominator) -> np.ndarray:
    """Return ``log(numerator) - log(denominator)``, NaN unless both are positive."""
    num = pd.to_numeric(pd.Series(np.asarray(numerator)), errors="coerce").to_numpy(dtype=np.float64)
    den = pd.to_numeric(pd.Series(np.asarray(denominator)), errors="coerce").to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((num > 0) & (den > 0), np.log(num) - np.log(den), np.nan)


def planar_metres(lat, lon) -> Tuple[np.ndarray, np.ndarray]:
    """
    Return easting and northing in metres from central London for WGS84 coordinates.

    An equirectangular projection about Charing Cross: within London it keeps
    distances to about 0.5 %, and training (`add_sizeband_comp_features`) and
    pricing (`hpp_inference.EvidenceStore`) measure with this same function,
    so the two agree on which sales lie inside a radius.
    """
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)
    metres_per_radian = EARTH_RADIUS_KM * 1000.0
    x = np.radians(lon - LONDON_CENTER_LONGITUDE) * metres_per_radian * np.cos(np.radians(LONDON_CENTER_LATITUDE))
    y = np.radians(lat - LONDON_CENTER_LATITUDE) * metres_per_radian
    return x, y


def sizeband_comp_value(
    comp_days: np.ndarray,
    comp_log_pps: np.ndarray,
    comp_dist: np.ndarray,
    sale_day: int,
    levels=None,
) -> float:
    """
    Return the size-matched comparables' median £/m² for one sale, or NaN.

    The candidates are already the same type, inside the window and the size
    band. Those within the inner radius are used when there are at least
    `SIZEBAND_COMP_MIN_SALES`, else all within the outer one. `levels` maps days
    since the epoch to the home's local log price level
    (`local_index.LocalIndex.log_levels` for its district and type); each
    comparable is moved from its own date to `sale_day` along it. The median is
    taken of log £/m², which is what a scale-free comparison asks for.
    """
    near = comp_dist <= SIZEBAND_COMP_RADII_M[0]
    use = near if int(near.sum()) >= SIZEBAND_COMP_MIN_SALES else comp_dist <= SIZEBAND_COMP_RADII_M[1]
    if not use.any():
        return np.nan
    logs = comp_log_pps[use]
    if levels is not None:
        logs = logs - levels(comp_days[use]) + levels(np.array([sale_day], dtype=np.int64))[0]
    return float(np.exp(np.median(logs)))


def sizeband_candidates(
    comp_area: np.ndarray, comp_days: np.ndarray, area: float, sale_day: int
) -> np.ndarray:
    """Return which candidate sales are inside the window before `sale_day` and the size band around `area`."""
    with np.errstate(invalid="ignore", divide="ignore"):
        in_band = np.abs(comp_area / area - 1.0) <= SIZEBAND_AREA_TOLERANCE
    return (
        in_band
        & (comp_days < sale_day)
        & (comp_days >= sale_day - SIZEBAND_COMP_WINDOW_DAYS)
    )


def _days_since_epoch(dates) -> np.ndarray:
    """Return dates as whole days since 1970-01-01."""
    return pd.to_datetime(pd.Series(np.asarray(dates))).to_numpy().astype("datetime64[D]").astype(np.int64)


# Grid cell (metres) the GPU radius search orders a block's subjects by, so
# each tile of subjects covers a compact area and prunes the pool to it.
SIZEBAND_SEARCH_CELL_M = 2000.0
# Device bytes per (subject, pool candidate) cell of a GPU search tile: the
# float64 offsets, their squared sum, the hit mask and the pair arrays.
_SIZEBAND_TILE_BYTES = 48


def _radius_pairs(xp, sx, sy, px, py, radius: float):
    """
    Return every (subject, pool) position pair within `radius` as two int64 arrays.

    A KD-tree on NumPy; on CuPy a brute-force search over the pool points
    inside the subjects' bounding box widened by `radius`.
    """
    if xp is np:
        from scipy.spatial import cKDTree  # noqa: PLC0415

        pairs = cKDTree(np.column_stack([sx, sy])).sparse_distance_matrix(
            cKDTree(np.column_stack([px, py])), radius, output_type="ndarray"
        )
        return pairs["i"].astype(np.int64), pairs["j"].astype(np.int64)
    box = (px >= sx.min() - radius) & (px <= sx.max() + radius) & (py >= sy.min() - radius) & (py <= sy.max() + radius)
    near = xp.nonzero(box)[0]
    dx = sx[:, None] - px[near][None, :]
    dy = sy[:, None] - py[near][None, :]
    i, j = xp.nonzero(dx * dx + dy * dy <= radius * radius)
    return i.astype(xp.int64), near[j].astype(xp.int64)


def _segment_medians(xp, segment, values, n_segments: int):
    """
    Return each segment's median of `values` (NaN for an empty one) as `numpy.median` computes it.

    One ``lexsort`` by (segment, value); an even count averages its two
    middle values as ``(a + b) / 2``, which is `numpy.median`'s rounding.
    """
    out = xp.full(n_segments, np.nan)
    if not values.size:
        return out
    order = xp.lexsort(xp.stack([values, segment.astype(values.dtype)]))
    ordered = values[order]
    counts = xp.bincount(segment, minlength=n_segments)
    starts = xp.concatenate([xp.zeros(1, dtype=counts.dtype), xp.cumsum(counts)[:-1]])
    has = counts > 0
    lower = starts[has] + (counts[has] - 1) // 2
    upper = starts[has] + counts[has] // 2
    out[has] = (ordered[lower] + ordered[upper]) / 2
    return out


def _sizeband_tile(xp, sx, sy, s_area, s_day, s_code, px, py, p_area, p_day, p_log_pps, table, table_first_day):
    """
    `StreamingExecutor` kernel: the size-matched median log £/m² of every sale in one tile, NaN without one.

    Per (sale, candidate) pair within the outer radius, exactly
    `sizeband_candidates` (size band and window), `sizeband_comp_value`'s
    inner-or-outer rule and its move along the local index (`table`, one row
    per district code, from `table_first_day`), then the median per sale.
    """
    n = sx.shape[0]
    inner, outer = SIZEBAND_COMP_RADII_M
    si, cand = _radius_pairs(xp, sx, sy, px, py, outer)
    with np.errstate(invalid="ignore", divide="ignore"):
        in_band = xp.abs(p_area[cand] / s_area[si] - 1.0) <= SIZEBAND_AREA_TOLERANCE
    keep = in_band & (p_day[cand] < s_day[si]) & (p_day[cand] >= s_day[si] - SIZEBAND_COMP_WINDOW_DAYS)
    si, cand = si[keep], cand[keep]
    dist = xp.hypot(px[cand] - sx[si], py[cand] - sy[si])
    near = dist <= inner
    n_near = xp.bincount(si[near], minlength=n)
    use = xp.where(n_near[si] >= SIZEBAND_COMP_MIN_SALES, near, dist <= outer)
    si, cand = si[use], cand[use]
    logs = p_log_pps[cand]
    if table is not None:
        code = s_code[si]
        logs = logs - table[code, p_day[cand] - table_first_day] + table[code, s_day[si] - table_first_day]
    return _segment_medians(xp, si, logs, n)


def _sizeband_level_table(local_index, kind: str, districts: Sequence[Optional[str]], first_day: int, last_day: int) -> np.ndarray:
    """
    Return ``local_index.log_levels`` for every day in ``[first_day, last_day]``, one row per district.

    Evaluated once per (district, type) and gathered per comparable, which is
    elementwise the same call `sizeband_comp_value` made per sale.
    """
    days = np.arange(first_day, last_day + 1, dtype=np.int64)
    table = np.empty((len(districts), days.size), dtype=np.float64)
    for row, district in enumerate(districts):
        table[row] = local_index.log_levels(days, district, kind)
    return table


def add_sizeband_comp_features(
    df,
    local_index=None,
    log: Optional[logging.Logger] = None,
    min_price: float = MIN_PLAUSIBLE_PRICE,
    backend: str = "auto",
):
    """
    Return a copy of `df` with the `SIZEBAND_COMP_COLUMNS` added.

    Causal: a sale's comparables are the rows of `df` sold strictly before it
    (`sizeband_candidates`), so no split needs it refitted. Sales under
    `min_price` are never comparables. `local_index` (`local_index.LocalIndex`,
    built from pairs up to the training cutoff) moves each comparable to the
    sale date; without one they are not moved. Runs on pandas even for a cuDF
    `df`.

    Vectorised per (type, sale month) block, through
    `common.accel.StreamingExecutor` (`_sizeband_tile`): every (sale,
    candidate) pair within the outer radius is found at once -- a KD-tree on
    the CPU, a tiled brute-force search on the GPU, its tiles sized from
    `hardware.ResourcePlan` -- and the size band, the window, the
    inner/outer radius rule, the move along the local index and the median
    are applied to the pairs as arrays. On the CPU the result is bit for bit
    the per-sale `sizeband_comp_value`; on the GPU it agrees to rounding
    (only CUDA's ``hypot`` differs, and only for a comparable on a radius).

    `backend` is ``"auto"`` (the GPU when `common.accel.gpu_enabled`),
    ``"cpu"`` or ``"gpu"``.
    """
    if backend not in ("auto", "cpu", "gpu"):
        raise ValueError(f"Unknown backend {backend!r}, expected auto/cpu/gpu")
    from hpldn.backend.common import accel, hardware  # noqa: PLC0415

    on_gpu = _is_cudf(df)
    out = (df.to_pandas() if on_gpu else df).copy()
    n = len(out)
    pps_out = np.full(n, np.nan)
    if n:
        use_gpu = backend != "cpu" and accel.gpu_enabled(log)
        if backend == "gpu" and not use_gpu:
            raise RuntimeError("add_sizeband_comp_features: backend='gpu' but no usable GPU")
        plan = hardware.plan_resources(device="auto" if use_gpu else "cpu")
        executor = accel.StreamingExecutor(chunk_rows=plan.chunk_rows, use_gpu=use_gpu, log=log)
        xp = executor.xp
        days = _days_since_epoch(out[DATE_COLUMN])
        price = pd.to_numeric(out[TARGET_COLUMN], errors="coerce").to_numpy(dtype=np.float64)
        area = pd.to_numeric(out["total_floor_area"], errors="coerce").to_numpy(dtype=np.float64)
        x, y = planar_metres(
            pd.to_numeric(out["latitude"], errors="coerce"), pd.to_numeric(out["longitude"], errors="coerce")
        )
        kinds = out["propertytype"].astype("str").to_numpy()
        districts = out["postcode_district"].astype("object").to_numpy()
        located = np.isfinite(x) & np.isfinite(y) & np.isfinite(area) & (area > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            log_pps = np.log(price / area)
        is_comp = located & np.isfinite(price) & (price >= max(min_price, 1.0))
        months = days.astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)
        cells = np.floor(np.column_stack([x, y]) / SIZEBAND_SEARCH_CELL_M)
        for kind in np.unique(kinds):
            comps = np.flatnonzero(is_comp & (kinds == kind))
            comps = comps[np.argsort(days[comps], kind="stable")]
            comp_days = days[comps]
            subjects = np.flatnonzero(located & (kinds == kind))
            if not subjects.size or not comps.size:
                continue
            code = np.zeros(n, dtype=np.int64)
            table = None
            table_first_day = int(min(comp_days.min(), days[subjects].min()))
            if local_index is not None:
                names = [d if isinstance(d, str) else None for d in districts[subjects]]
                unique = sorted({d for d in names if d is not None})
                lookup = {d: i for i, d in enumerate(unique)}
                code[subjects] = [lookup[d] if d is not None else len(unique) for d in names]
                last_day = int(max(comp_days.max(), days[subjects].max()))
                table = xp.asarray(_sizeband_level_table(local_index, kind, unique + [None], table_first_day, last_day))
            subject_months = months[subjects]
            for month in np.unique(subject_months):
                chunk = subjects[subject_months == month]
                lo = int(np.searchsorted(comp_days, days[chunk].min() - SIZEBAND_COMP_WINDOW_DAYS, side="left"))
                hi = int(np.searchsorted(comp_days, days[chunk].max(), side="left"))
                if hi <= lo:
                    continue
                pool = comps[lo:hi]
                if use_gpu:
                    # Compact tiles: the subjects in grid-cell order.
                    chunk = chunk[np.lexsort((cells[chunk, 0], cells[chunk, 1]))]
                    tile_rows = plan.chunk_rows_for(len(pool) * _SIZEBAND_TILE_BYTES)
                else:
                    tile_rows = len(chunk)
                medians = np.concatenate(
                    [
                        part[0]
                        for part in executor.run(
                            _sizeband_tile,
                            [x[chunk], y[chunk], area[chunk], days[chunk], code[chunk]],
                            consts=(x[pool], y[pool], area[pool], days[pool], log_pps[pool], table, table_first_day),
                            chunk_rows=tile_rows,
                        )
                    ]
                )
                found = np.isfinite(medians)
                pps_out[chunk[found]] = np.exp(medians[found])
        if log is not None:
            log.info(
                f"Added size-matched comparables: {int(np.isfinite(pps_out).sum()):,} of {n:,} rows "
                f"({'moved along the local index' if local_index is not None else 'not index-moved'}; "
                f"{'GPU' if use_gpu else 'CPU'} radius search)"
            )
    out["comp_sizeband_pps"] = pps_out
    with np.errstate(invalid="ignore"):
        out["comp_sizeband_implied_value"] = pps_out * pd.to_numeric(out["total_floor_area"], errors="coerce").to_numpy(
            dtype=np.float64
        )
    if on_gpu:
        import cudf

        out = cudf.from_pandas(out)
    return out


def local_indexed_prev_sale(df, local_index) -> np.ndarray:
    """
    Return each row's previous sale price moved to its sale date along its local index, NaN without one.

    `local_index` is a `local_index.LocalIndex`; rows are moved a district and
    type at a time.
    """
    n = len(df)
    out = np.full(n, np.nan)
    if local_index is None or "prev_sale_date" not in df.columns or "prev_sale_price" not in df.columns:
        return out
    prev_price = pd.to_numeric(df["prev_sale_price"], errors="coerce").to_numpy(dtype=np.float64)
    prev_dates = pd.to_datetime(pd.Series(np.asarray(df["prev_sale_date"])))
    has = np.isfinite(prev_price) & (prev_price > 0) & prev_dates.notna().to_numpy()
    if not has.any():
        return out
    sale_days = _days_since_epoch(df[DATE_COLUMN])
    prev_days = np.zeros(n, dtype=np.int64)
    prev_days[has] = prev_dates[has].to_numpy().astype("datetime64[D]").astype(np.int64)
    keys = pd.DataFrame(
        {
            "district": df["postcode_district"].astype("object").where(df["postcode_district"].notna(), "").to_numpy(),
            "kind": df["propertytype"].astype("str").to_numpy(),
        }
    )
    for (district, kind), group in keys[has].groupby(["district", "kind"]):
        idx = group.index.to_numpy()
        area = district or None
        moved = local_index.log_levels(sale_days[idx], area, kind) - local_index.log_levels(prev_days[idx], area, kind)
        out[idx] = prev_price[idx] * np.exp(moved)
    return out


def add_market_relative_features(df, ukhpi: Optional[pd.DataFrame] = None, local_index=None):
    """
    Return a copy of `df` with the `MARKET_RELATIVE_COLUMNS` and `ANCHOR_FEATURE_COLUMNS` added.

    - ``rel_<x>`` for each `MARKET_RELATIVE_SOURCE_COLUMNS` entry:
      ``log(x) - log(ukhpi_avg_price_at_sale)``;
    - ``rel_comp_implied_value`` and ``rel_nn20_implied_value``: the
      comparables' (or the 20 nearest sales') median £/m² times this home's
      floor area, relative to the market the same way;
    - ``prev_sale_deflated``: ``log(prev_sale_price) - log(UKHPI at the
      previous sale)``, the repeat-sale signal on the target's own scale, and
      ``prev_sale_indexed_price``, the previous price carried to the sale date
      by the index.

    The UK HPI at the previous sale is looked up in `ukhpi` with the view's
    as-of rule (`ukhpi_as_of`, by the sale's property type). Without `ukhpi`
    it is read from an ``ukhpi_at_prev_sale`` column, which ``hpp_inference``
    fills from `EvidenceStore.ukhpi_on`. Every column is recomputed from its
    sources, so calling this twice is harmless. Source columns a frame lacks
    give NaN. Works on pandas (a cuDF frame is converted and returned as such).

    Beside them, the `ANCHOR_FEATURE_COLUMNS`: ``prev_sale_local_indexed``
    (`local_indexed_prev_sale` with `local_index`; without it, read from the
    column ``hpp_inference`` fills from `EvidenceStore.time_factor`), the
    ``anchor_value`` (`ANCHOR_SOURCE_COLUMNS`, first present wins),
    ``rel_prev_sale_local`` (the indexed previous sale over the anchor), and
    the size-matched value and the anchor relative to the market. The
    `SIZEBAND_COMP_COLUMNS` themselves come from `add_sizeband_comp_features`
    (or the built row) and are only read here.
    """
    on_gpu = _is_cudf(df)
    out = (df.to_pandas() if on_gpu else df).copy()
    float_dtype = (
        "float32"
        if "total_floor_area" in out.columns and out["total_floor_area"].dtype == np.float32
        else "float64"
    )

    def _col(name: str) -> np.ndarray:
        if name not in out.columns:
            return np.full(len(out), np.nan)
        return pd.to_numeric(out[name], errors="coerce").to_numpy(dtype=np.float64)

    market = _col(UKHPI_AT_SALE_COLUMN)
    for source, target in zip(MARKET_RELATIVE_SOURCE_COLUMNS, MARKET_RELATIVE_PRICE_COLUMNS):
        out[target] = _log_ratio(_col(source), market).astype(float_dtype)

    area = _col("total_floor_area")
    out["rel_comp_implied_value"] = _log_ratio(_col("comp_median_price_per_sqm") * area, market).astype(float_dtype)
    out["rel_nn20_implied_value"] = _log_ratio(_col("median_price_per_sqm_20nn_6m") * area, market).astype(float_dtype)

    if ukhpi is not None and "prev_sale_date" in out.columns:
        out[UKHPI_AT_PREV_SALE_COLUMN] = ukhpi_as_of(out["prev_sale_date"], out["propertytype"], ukhpi)
    at_prev = _col(UKHPI_AT_PREV_SALE_COLUMN)
    prev_price = _col("prev_sale_price")
    out["prev_sale_deflated"] = _log_ratio(prev_price, at_prev).astype(float_dtype)
    with np.errstate(divide="ignore", invalid="ignore"):
        indexed = np.where(at_prev > 0, prev_price * market / at_prev, np.nan)
    out["prev_sale_indexed_price"] = indexed.astype(float_dtype)

    if local_index is not None:
        out["prev_sale_local_indexed"] = local_indexed_prev_sale(out, local_index)
    local_prev = _col("prev_sale_local_indexed")
    anchor = _col("comp_sizeband_implied_value")
    for fallback in (
        _col("comp_median_price_per_sqm") * area,
        _col("postcode_sector_24m_comp_median_price_per_sqm") * area,
        _col("postcode_sector_24m_comp_median_price"),
        market,
    ):
        missing = ~(np.isfinite(anchor) & (anchor > 0))
        anchor = np.where(missing, fallback, anchor)
    anchor = np.where(np.isfinite(anchor) & (anchor > 0), anchor, np.nan)
    out[ANCHOR_COLUMN] = anchor
    for col in SIZEBAND_COMP_COLUMNS:
        out[col] = _col(col).astype(float_dtype)
    out["prev_sale_local_indexed"] = local_prev.astype(float_dtype)
    out["rel_prev_sale_local"] = _log_ratio(local_prev, anchor).astype(float_dtype)
    out["rel_comp_sizeband_implied_value"] = _log_ratio(_col("comp_sizeband_implied_value"), market).astype(float_dtype)
    out["rel_anchor_value"] = _log_ratio(anchor, market).astype(float_dtype)

    if on_gpu:
        import cudf

        out = cudf.from_pandas(out)
    return out


def time_series_split(df, train_frac: float, val_frac: float) -> Tuple:
    """
    Split a date-sorted DataFrame into contiguous train, validation and test blocks.

    Positional slicing respects uneven volume over time while keeping every
    train row before every validation row before every test row.
    """
    if not 0 < train_frac < 1 or not 0 < val_frac < 1 or train_frac + val_frac >= 1:
        raise ValueError(
            f"train_frac ({train_frac}) and val_frac ({val_frac}) must each be in "
            "(0, 1) and leave a positive share for the test split"
        )
    n = len(df)
    train_end = int(n * train_frac)
    val_end = train_end + int(n * val_frac)
    train_df, val_df, test_df = df.iloc[:train_end], df.iloc[train_end:val_end], df.iloc[val_end:]
    for name, part in (("train", train_df), ("val", val_df), ("test", test_df)):
        if len(part) == 0:
            raise ValueError(f"time_series_split produced an empty {name} split from {n} rows")
    if not (
        train_df[DATE_COLUMN].max() <= val_df[DATE_COLUMN].min()
        and val_df[DATE_COLUMN].max() <= test_df[DATE_COLUMN].min()
    ):
        raise AssertionError(
            "time_series_split requires df sorted by dateoftransfer ascending"
        )
    return train_df, val_df, test_df




def grouped_time_series_split(df, train_frac: float, val_frac: float, log: Optional[logging.Logger] = None) -> Tuple:
    """
    Split chronologically, then drop validation and test rows whose UPRN is in train.

    Stops a resold property's later sale from being evaluated against a model
    that saw its earlier sale. Rows without a UPRN are kept; order is kept.
    """
    train_df, val_df, test_df = time_series_split(df, train_frac, val_frac)
    train_uprn_col = train_df.loc[train_df["uprn"].notna(), "uprn"]
    train_uprns = set(train_uprn_col.to_pandas() if _is_cudf(df) else train_uprn_col)

    train_uprn_list = list(train_uprns)

    def _drop_seen(part):
        """Drop rows whose UPRN appears in the training split."""
        seen = part["uprn"].notna() & part["uprn"].isin(train_uprn_list)
        return part[~seen]

    n_before = len(val_df) + len(test_df)
    val_df, test_df = _drop_seen(val_df), _drop_seen(test_df)
    if log is not None:
        log.info(
            f"grouped split: removed {n_before - len(val_df) - len(test_df)} val/test rows "
            f"whose uprn was also sold in train ({len(train_uprns)} distinct train uprns)"
        )
    return train_df, val_df, test_df


def purged_embargo_time_series_split(
    df,
    train_frac: float,
    val_frac: float,
    embargo_days: int = 30,
    log: Optional[logging.Logger] = None,
) -> Tuple:
    """
    Split chronologically, then drop an `embargo_days` buffer before each boundary.

    Purged and embargoed validation: rows close to a boundary share local
    price levels, so removing them gives a stricter out-of-sample read.
    Purely subtractive.
    """
    train_df, val_df, test_df = time_series_split(df, train_frac, val_frac)
    embargo = np.timedelta64(embargo_days, "D")
    val_start, test_start = val_df[DATE_COLUMN].min(), test_df[DATE_COLUMN].min()
    n_train_before, n_val_before = len(train_df), len(val_df)
    train_df = train_df[train_df[DATE_COLUMN] < (val_start - embargo)]
    val_df = val_df[val_df[DATE_COLUMN] < (test_start - embargo)]
    if log is not None:
        log.info(
            f"purged/embargoed split ({embargo_days}d): train {n_train_before}->{len(train_df)} rows, "
            f"val {n_val_before}->{len(val_df)} rows"
        )
    return train_df, val_df, test_df


def spatial_holdout_split(
    df,
    test_frac: float,
    val_frac: float,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
) -> Tuple:
    """
    Hold out whole postcode districts for validation and test.

    Measures generalisation to an unseen district rather than to the future,
    so read it alongside a time-based split. Districts are assigned by a
    seeded permutation sized by district count, not rows.
    """
    districts = df["postcode_district"].unique()
    districts = list(districts.to_pandas() if _is_cudf(df) else districts)
    shuffled = np.random.default_rng(random_state).permutation(districts)
    n_test = max(1, int(len(shuffled) * test_frac))
    n_val = max(1, int(len(shuffled) * val_frac))
    test_districts = list(shuffled[:n_test])
    val_districts = list(shuffled[n_test : n_test + n_val])
    train_districts = list(shuffled[n_test + n_val :])
    train_df = df[df["postcode_district"].isin(train_districts)]
    val_df = df[df["postcode_district"].isin(val_districts)]
    test_df = df[df["postcode_district"].isin(test_districts)]
    for name, part in (("train", train_df), ("val", val_df), ("test", test_df)):
        if len(part) == 0:
            raise ValueError(f"spatial_holdout_split produced an empty {name} split")
    if log is not None:
        log.info(
            f"spatial holdout split: {len(train_districts)} train / {len(val_districts)} val / "
            f"{len(test_districts)} test postcode_districts "
            f"({len(train_df)}/{len(val_df)}/{len(test_df)} rows)"
        )
    return train_df, val_df, test_df


def recency_windowed_time_series_split(
    df,
    train_frac: float,
    val_frac: float,
    train_window_years: float = 5.0,
    log: Optional[logging.Logger] = None,
) -> Tuple:
    """
    Split chronologically, then keep only the `train_window_years` before validation.

    Tests whether a smaller, more recent training set beats the full history
    across market regimes. Purely subtractive.
    """
    train_df, val_df, test_df = time_series_split(df, train_frac, val_frac)
    val_start = val_df[DATE_COLUMN].min()
    window_start = val_start - np.timedelta64(int(train_window_years * 365), "D")
    n_before = len(train_df)
    train_df = train_df[train_df[DATE_COLUMN] >= window_start]
    if log is not None:
        log.info(
            f"recency-windowed split ({train_window_years:.1f}y): kept {len(train_df)}/{n_before} train rows"
        )
    return train_df, val_df, test_df


def time_series_cv_folds(
    df, n_folds: int, test_frac: float, log: Optional[logging.Logger] = None
) -> Tuple[List[Tuple], object, object]:
    """
    Build expanding-window cross-validation folds and a final test block.

    The last `test_frac` of rows is the test block. Fold ``i`` trains on the
    first ``(i + 1) / (n_folds + 1)`` of the rest and validates on the next
    share.

    Returns
    -------
    folds : list of tuple
        ``(train_df, val_df)`` per fold.
    non_test_df : DataFrame
        Every row before the test block.
    test_df : DataFrame
        The test block.
    """
    n = len(df)
    test_start = int(n * (1 - test_frac))
    non_test_df, test_df = df.iloc[:test_start], df.iloc[test_start:]
    if len(test_df) == 0:
        raise ValueError("time_series_cv_folds produced an empty test split")
    chunk = len(non_test_df) // (n_folds + 1)
    if chunk == 0:
        raise ValueError(
            f"time_series_cv_folds: not enough rows ({len(non_test_df)}) for {n_folds} folds"
        )
    folds = []
    for i in range(n_folds):
        train_end = chunk * (i + 1)
        val_end = chunk * (i + 2) if i < n_folds - 1 else len(non_test_df)
        folds.append((non_test_df.iloc[:train_end], non_test_df.iloc[train_end:val_end]))
    if log is not None:
        log.info(
            f"walk-forward CV: {n_folds} folds over {len(non_test_df)} non-test rows "
            f"(~{chunk} rows/fold), {len(test_df)}-row final test block"
        )
    return folds, non_test_df, test_df


def log_split_diagnostics(train_df, val_df, test_df, log: Optional[logging.Logger]) -> None:
    """
    Log price, floor-area, year and modal district and type summaries per split.

    Makes distribution shift between validation and test easy to spot. A
    no-op without `log`.
    """
    if log is None:
        return
    for name, part in (("train", train_df), ("val", val_df), ("test", test_df)):
        price = part[TARGET_COLUMN]
        top_district = part["postcode_district"].value_counts().index[0]
        top_type = part["propertytype"].value_counts().index[0]
        log.info(
            f"{name} split diagnostics: n={len(part)} "
            f"price median=£{float(price.median()):,.0f} mean=£{float(price.mean()):,.0f} "
            f"std=£{float(price.std()):,.0f} "
            f"floor_area mean={float(part['total_floor_area'].mean()):.1f}m² "
            f"years={int(part['sale_year'].min())}-{int(part['sale_year'].max())} "
            f"top_postcode_district={top_district} top_propertytype={top_type}"
        )


def _group_price_stats(
    df, group_col: str, stat_columns: Tuple[str, str, str], deflator_col: Optional[str] = None
):
    """
    Return median price, median price per m² and count per `group_col` for the rows given.

    With `deflator_col` the medians are of ``price / deflator`` and ``price /
    floor area / deflator``: prices relative to the market at each sale, so a
    lookup pooled over decades is on one scale (see `_add_group_price_stats`).
    """
    median_col, pps_col, count_col = stat_columns
    columns = [group_col, TARGET_COLUMN, "total_floor_area"] + ([deflator_col] if deflator_col else [])
    working = df[columns].copy()
    if deflator_col:
        working["_price"] = working[TARGET_COLUMN] / working[deflator_col]
    else:
        working["_price"] = working[TARGET_COLUMN]
    working["_price_per_sqm"] = working["_price"] / working["total_floor_area"]
    grouped = working.groupby(group_col)
    stats = grouped["_price"].median().to_frame(median_col)
    stats[pps_col] = grouped["_price_per_sqm"].median()
    stats[count_col] = grouped[TARGET_COLUMN].count()
    return stats


def group_price_fallbacks(train_df, deflator_col: Optional[str] = None) -> Tuple[float, float]:
    """Return the train-wide median price and price per m² unseen groups fall back to."""
    price = train_df[TARGET_COLUMN]
    if deflator_col:
        price = price / train_df[deflator_col]
    return float(price.median()), float((price / train_df["total_floor_area"]).median())


def _add_group_price_stats(
    train_df,
    val_df,
    test_df,
    group_col: str,
    stat_columns: Tuple[str, str, str],
    n_folds: int = 5,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
    deflator_col: Optional[str] = None,
) -> Tuple:
    """
    Add per-group price statistics to each split without target leakage.

    Validation and test rows use statistics fit on all of train; unseen groups
    fall back to the train-wide median and a count of 0. Train rows are split
    into `n_folds` chronological blocks and each block uses statistics fit only
    on earlier blocks (fold 0 uses the global fallback), so no row's statistic
    includes its own or a later price. `train_df` must be date-sorted;
    `random_state` is accepted for compatibility only.

    With `deflator_col` (``hpp_cb`` passes ``ukhpi_avg_price_at_sale``) every
    statistic, fallbacks included, is of the price relative to the market at
    its sale. Nominal medians pooled over 30 years put train rows (statistics
    from 1995-2019 blocks) and test rows (1995-2025) on different price levels
    against a deflated target.

    Returns
    -------
    train_df, val_df, test_df : DataFrame
        The splits with `stat_columns` added.
    stats : DataFrame
        The full-train lookup, for inference.
    """
    on_gpu = _is_cudf(train_df)
    median_col, pps_col, count_col = stat_columns
    full_stats = _group_price_stats(train_df, group_col, stat_columns, deflator_col)
    global_median_price, global_median_pps = group_price_fallbacks(train_df, deflator_col)

    def _apply(df, stats):
        """Merge `stats` onto `df`, filling unseen groups with the global fallbacks."""
        merged = df.merge(stats, how="left", left_on=group_col, right_index=True)
        merged[median_col] = merged[median_col].fillna(global_median_price)
        merged[pps_col] = merged[pps_col].fillna(global_median_pps)
        merged[count_col] = merged[count_col].fillna(0)
        return merged

    val_df = _apply(val_df, full_stats)
    test_df = _apply(test_df, full_stats)

    n_train = len(train_df)
    block_size = -(-n_train // n_folds)  
    fold_ids = np.minimum(np.arange(n_train) // block_size, n_folds - 1)
    if on_gpu:
        import cudf

        fold_ids = cudf.Series(fold_ids, index=train_df.index)
    else:
        fold_ids = pd.Series(fold_ids, index=train_df.index)

    oof_parts = []
    for fold in range(n_folds):
        holdout_mask = fold_ids == fold
        earlier_mask = fold_ids < fold
        fold_stats = _group_price_stats(train_df[earlier_mask], group_col, stat_columns, deflator_col)
        oof_parts.append(_apply(train_df[holdout_mask], fold_stats))
    concat = cudf.concat if on_gpu else pd.concat
    train_df = concat(oof_parts).sort_index()

    if log is not None:
        log.info(
            f"Added leakage-safe {group_col} price stats "
            f"({n_folds}-fold expanding-window out-of-fold for train, "
            "train-fitted lookup for val/test); "
            f"{int((full_stats[count_col] > 0).sum())} distinct {group_col} values "
            f"seen in training data"
        )
    return train_df, val_df, test_df, full_stats


def add_postcode_price_stats(
    train_df,
    val_df,
    test_df,
    n_folds: int = 5,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
    deflator_col: Optional[str] = None,
) -> Tuple:
    """Add `POSTCODE_STAT_COLUMNS` per postcode district; see `_add_group_price_stats`."""
    return _add_group_price_stats(
        train_df,
        val_df,
        test_df,
        group_col="postcode_district",
        stat_columns=tuple(POSTCODE_STAT_COLUMNS),
        n_folds=n_folds,
        random_state=random_state,
        log=log,
        deflator_col=deflator_col,
    )


def add_postcode_sector_price_stats(
    train_df,
    val_df,
    test_df,
    n_folds: int = 5,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
    deflator_col: Optional[str] = None,
) -> Tuple:
    """
    Add `SECTOR_STAT_COLUMNS` per postcode sector; see `_add_group_price_stats`.

    Kept alongside the district statistics so the forest can fall back to them
    where a sector is sparse.
    """
    return _add_group_price_stats(
        train_df,
        val_df,
        test_df,
        group_col="postcode_sector",
        stat_columns=tuple(SECTOR_STAT_COLUMNS),
        n_folds=n_folds,
        random_state=random_state,
        log=log,
        deflator_col=deflator_col,
    )


def _categorical_encoder_categories(train_df, on_gpu: bool, columns: Optional[Sequence[str]] = None) -> List[list]:
    """
    Return explicit OrdinalEncoder categories for `columns` (default `CATEGORICAL_COLUMNS`).

    `PRICE_ORDERED_CATEGORICAL_COLUMNS` are ordered by train mean price; every
    other column is sorted, reproducing ``"auto"``, because the encoder cannot
    mix the two. Fit on train only. Values are compared as strings with nulls
    as "UNKNOWN", so a stray NULL can never make sorting compare a float with a
    string (BLK-1).
    """
    columns = list(CATEGORICAL_COLUMNS if columns is None else columns)
    train_pd = train_df[columns + [TARGET_COLUMN]].to_pandas() if on_gpu else train_df
    categories = []
    for col in columns:
        values = train_pd[col].astype("string").fillna(MISSING_CATEGORY)
        if col in PRICE_ORDERED_CATEGORICAL_COLUMNS:
            ordered = train_pd[TARGET_COLUMN].groupby(values).mean().sort_values().index
            categories.append([str(v) for v in ordered])
        else:
            categories.append(sorted(str(v) for v in values.unique()))
    return categories


def _train_medians(train_df, cols) -> Dict[str, float]:
    """
    Return the train median of each of `cols`, or 0.0 for an all-null column.

    Small walk-forward folds can leave a sparse column entirely null, and a NaN
    median would leave NaNs in the matrix.
    """
    medians = {}
    for col in cols:
        median = float(train_df[col].median())
        medians[col] = 0.0 if np.isnan(median) else median
    return medians


def impute_and_encode(train_df, val_df, test_df, use_ofsted: bool = True) -> Tuple:
    """
    Build the train, validation and test feature matrices.

    Categoricals are ordinal-encoded with a vocabulary fit on train (unseen
    values encode to -1). Each missing-value group in `MISSING_FLAG_GROUPS` is
    filled with train medians and gets one shared indicator column. On a cuDF
    input the encoder is cuML's and the matrices are cupy. Without
    `use_ofsted` the Ofsted group is left out (`feature_layout`).

    Returns
    -------
    X_train, X_val, X_test : ndarray
        Matrices with `feature_layout(use_ofsted)`'s ``matrix_columns``
        (`FEATURE_MATRIX_COLUMNS` by default).
    encoder : OrdinalEncoder
        The fitted encoder.
    medians : dict
        Fill values per column.
    feature_metadata : dict
        Including ``feature_columns``, the matrix layout.
    """
    on_gpu = _is_cudf(train_df)
    if use_ofsted:
        require_ofsted_columns(train_df.columns)
    layout = feature_layout(use_ofsted)
    categorical = layout["categorical"]
    categories = _categorical_encoder_categories(train_df, on_gpu, categorical)
    if on_gpu:
        import cudf
        from cudf.core.dtypes import CategoricalDtype
        from cuml.preprocessing import OrdinalEncoder as _OrdinalEncoder

        max_len = max(len(c) for c in categories)
        categories = cudf.DataFrame(
            {
                col: cudf.Series(
                    list(cats) + [cats[-1]] * (max_len - len(cats)),
                    dtype=CategoricalDtype(categories=cats, ordered=True),
                )
                for col, cats in zip(categorical, categories)
            }
        )

        encoder = _OrdinalEncoder(categories=categories, handle_unknown="ignore", output_type="cupy")
    else:
        encoder = OrdinalEncoder(categories=categories, handle_unknown="use_encoded_value", unknown_value=-1)

    encoder.fit(train_df[categorical])
    medians = {}
    for cols in (
        LOT_AREA_DEPENDENT_COLUMNS,
        LOCATION_DEPENDENT_COLUMNS,
        COMP_DEPENDENT_COLUMNS,
        ROLLING_COMP_DEPENDENT_COLUMNS,
        PTAL_DEPENDENT_COLUMNS,
        RIVER_DEPENDENT_COLUMNS,
        PREV_SALE_DEPENDENT_COLUMNS,
        SAME_STREET_DEPENDENT_COLUMNS,
        NEIGHBOURHOOD_DEPENDENT_COLUMNS,
        MSOA_TREND_DEPENDENT_COLUMNS,
        NUMERIC_DEPENDENT_COLUMNS,
        MAISONETTE_DEPENDENT_COLUMNS,
    ) + ((OFSTED_DEPENDENT_COLUMNS,) if use_ofsted else ()):
        medians.update(_train_medians(train_df, cols))

    return (
        build_feature_matrix(train_df, encoder, medians, use_ofsted),
        build_feature_matrix(val_df, encoder, medians, use_ofsted),
        build_feature_matrix(test_df, encoder, medians, use_ofsted),
        encoder,
        medians,
        {
            "feature_columns": list(layout["matrix_columns"]),
            "missing_flag_columns": list(layout["missing_flag_groups"]),
            "use_ofsted": use_ofsted,
        },
    )


def build_feature_matrix(df, encoder, medians: Mapping[str, float], use_ofsted: bool = True):
    """
    Encode, fill and stack one frame's `feature_layout` matrix.

    The single matrix layout for training (`impute_and_encode`) and for a
    saved model scoring new rows (`predict_with_saved_model`): categoricals
    through the fitted `encoder`, numerics filled with the train `medians`,
    then one missing indicator per missing-flag group, taken from its first
    column. A cuDF `df` gives a cupy matrix.
    """
    layout = feature_layout(use_ofsted)
    on_gpu = _is_cudf(df)
    if on_gpu:
        import cupy as xp

        dtype = GPU_DTYPE
    else:
        xp = np
        dtype = np.float64

    cat = xp.asarray(encoder.transform(df[layout["categorical"]]))
    if on_gpu:
        cat = xp.nan_to_num(cat, nan=-1.0)
    cat = cat.astype(dtype)
    num = df[layout["numeric"]].copy()
    for col, median in medians.items():
        if col in num.columns:
            num[col] = num[col].fillna(median)
    missing_flags = []
    for cols in layout["missing_flag_groups"].values():
        flag = df[cols[0]].isna().astype(dtype)
        missing_flags.append(flag.to_cupy().reshape(-1, 1) if on_gpu else flag.to_numpy().reshape(-1, 1))
    if on_gpu:
        num_arr = num.astype(dtype).to_cupy()
    else:
        num_arr = num.to_numpy(dtype=dtype)
    built = xp.hstack([cat, num_arr] + missing_flags)
    if built.shape[1] != len(layout["matrix_columns"]):
        raise RuntimeError(
            f"build_feature_matrix built a {built.shape[1]}-column matrix but "
            f"the layout names {len(layout['matrix_columns'])} -- the column list "
            "and the matrix have drifted apart (F6); a new feature must be added to "
            "FEATURE_COLUMNS, and a new missing flag to MISSING_FLAG_GROUPS."
        )
    return built


def resolve_device(
    device: str, log: Optional[logging.Logger] = None
) -> Tuple[bool, Optional[HostResources], Optional[str]]:
    """
    Decide once whether this run uses the GPU (cuDF and cuML) or the CPU.

    Returns
    -------
    use_gpu : bool
        Whether to use the GPU.
    resources : HostResources or None
        The detected host on the GPU path.
    reason : str or None
        Why the run fell back to CPU.
    """
    device = device.lower()
    if device not in ("cpu", "gpu", "auto"):
        raise ValueError(f"Unknown device '{device}', expected cpu/gpu/auto")

    if device == "cpu":
        return False, None, "requested"

    resources = detect_host_resources(log)
    if not resources.has_cuda_gpu:
        if device == "gpu":
            raise RuntimeError("--device gpu requested but no CUDA-capable GPU was detected")
        return False, resources, "no CUDA GPU detected"

    try:
        import cudf  # noqa: F401 - availability probe only
        import cuml  # noqa: F401 - availability probe only
    except ImportError as err:
        if device == "gpu":
            raise RuntimeError(
                "--device gpu requested but RAPIDS cuDF/cuML is not installed "
                "(pip install --extra-index-url https://pypi.nvidia.com "
                "libcuml-cu13 cudf-cu13)"
            ) from err
        return False, resources, "cuML/cuDF not installed"

    return True, resources, None


def resolve_regressor(device: str, log: Optional[logging.Logger] = None, **rf_kwargs):
    """
    Return an unfitted RandomForestRegressor and a backend description.

    ``"gpu"`` or ``"auto"`` use cuML on a CUDA host (``"gpu"`` raises rather
    than falling back). On cuML an unset ``max_depth`` becomes
    `GPU_MAX_DEPTH_DEFAULT`, bounding the host-side Treelite serialisation.
    """
    use_gpu, resources, reason = resolve_device(device, log)

    def _sklearn(reason: str):
        """Return a scikit-learn forest and a CPU backend description."""
        from sklearn.ensemble import RandomForestRegressor

        return RandomForestRegressor(**rf_kwargs), f"scikit-learn (cpu, {reason})"

    if not use_gpu:
        return _sklearn(reason)

    from cuml.ensemble import RandomForestRegressor as CuRandomForestRegressor

    cu_kwargs = {k: v for k, v in rf_kwargs.items() if k != "n_jobs"}
    if cu_kwargs.get("max_depth") is None:
        if log is not None:
            log.info(
                f"--max-depth not set: capping GPU forest to max_depth="
                f"{GPU_MAX_DEPTH_DEFAULT} (unlimited depth on this dataset size "
                "makes cuML's host-side Treelite serialization consume tens of "
                "GB of RAM instead of VRAM; pass --max-depth to override)"
            )
        cu_kwargs["max_depth"] = GPU_MAX_DEPTH_DEFAULT
    backend = (
        f"cuML (gpu: {resources.gpu_count}x {resources.gpu_manufacturer} {resources.gpu_name})".strip()
    )
    return CuRandomForestRegressor(**cu_kwargs), backend


def compute_metrics(y_true, y_pred) -> Dict[str, float]:
    """
    Return MAE, RMSE, R², MAPE and median absolute error.

    Cupy inputs are evaluated on the device, returning host floats.
    """
    if _is_cupy(y_true):
        import cupy as cp
        from cuml.metrics import mean_absolute_error as cu_mae
        from cuml.metrics import mean_squared_error as cu_mse
        from cuml.metrics import r2_score as cu_r2

        eps = cp.finfo(cp.float64).eps
        mape = cp.mean(cp.abs(y_pred - y_true) / cp.maximum(cp.abs(y_true), eps))
        medae = cp.median(cp.abs(y_pred - y_true))
        return {
            "mae": float(cu_mae(y_true, y_pred)),
            "rmse": float(cu_mse(y_true, y_pred, squared=False)),
            "r2": float(cu_r2(y_true, y_pred)),
            "mape": float(mape),
            "medae": float(medae),
        }
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(np.sqrt(mean_squared_error(y_true, y_pred))),
        "r2": float(r2_score(y_true, y_pred)),
        "mape": float(mean_absolute_percentage_error(y_true, y_pred)),
        "medae": float(np.median(np.abs(np.asarray(y_pred) - np.asarray(y_true)))),
    }


def price_band(price) -> pd.Categorical:
    """Bucket prices into `PRICE_BAND_LABELS`."""
    return pd.cut(np.asarray(price, dtype=float), bins=PRICE_BAND_EDGES, labels=PRICE_BAND_LABELS, right=False)


def compute_price_band_sample_weight(price_raw) -> np.ndarray:
    """
    Return inverse-frequency sample weights by price band, normalised to mean 1.

    Gives each price band comparable influence over split selection. Banding
    uses raw prices on the host; the result is a numpy float64 array.
    """
    price_host = price_raw.get() if _is_cupy(price_raw) else np.asarray(price_raw, dtype=float)
    bands = np.asarray(price_band(price_host))
    counts = pd.Series(bands).value_counts()
    n = len(price_host)
    band_weight = {
        label: n / (len(PRICE_BAND_LABELS) * counts[label])
        for label in PRICE_BAND_LABELS
        if counts.get(label, 0) > 0
    }
    weights = np.array([band_weight.get(b, 1.0) for b in bands], dtype=np.float64)
    mean_weight = weights.mean()
    return weights / mean_weight if mean_weight > 0 else weights


def sample_weight_for_training(y_train_raw, mode: str = "none") -> np.ndarray:
    """
    Return training sample weights for a ``--sample-weight`` mode.

    Parameters
    ----------
    y_train_raw : array_like
        Raw training prices.
    mode : {"none", "sqrt", "full"}, optional
        ``"none"`` (default) is uniform, matching the unweighted metrics
        reported. ``"full"`` is `compute_price_band_sample_weight`; ``"sqrt"``
        square-roots it and renormalises, narrowing the spread from about 13x
        to 3.6x, which kept CatBoost's GPU trainer stable.

    Returns
    -------
    numpy.ndarray
        One weight per row. Shared with `hpp_cb` so both models train on the
        same objective.
    """
    if mode not in ("none", "sqrt", "full"):
        raise ValueError(f"Unknown sample-weight mode '{mode}', expected none/sqrt/full")
    if mode == "none":
        n = len(y_train_raw)
        return np.ones(n, dtype=np.float64)
    weight = compute_price_band_sample_weight(y_train_raw)
    if mode == "sqrt":
        weight = np.sqrt(weight)
    mean_weight = weight.mean()
    return weight / mean_weight if mean_weight > 0 else weight


def compute_metrics_by_price_band(y_true, y_pred) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Return `compute_metrics` per `PRICE_BAND_LABELS` band of the true price.

    R² is omitted because it is meaningless within a narrow band. Empty bands
    get all-None entries, so every band key is always present.
    """
    if _is_cupy(y_true):
        y_true = y_true.get()
    if _is_cupy(y_pred):
        y_pred = y_pred.get()
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    bands = price_band(y_true)

    report = {}
    for label in PRICE_BAND_LABELS:
        mask = np.asarray(bands == label)
        n = int(mask.sum())
        if n == 0:
            report[label] = {"n": 0, "mae": None, "rmse": None, "mape": None, "medae": None}
            continue
        band_true, band_pred = y_true[mask], y_pred[mask]
        eps = np.finfo(np.float64).eps
        report[label] = {
            "n": n,
            "mae": float(mean_absolute_error(band_true, band_pred)),
            "rmse": float(np.sqrt(mean_squared_error(band_true, band_pred))),
            "mape": float(np.mean(np.abs(band_true - band_pred) / np.maximum(np.abs(band_true), eps))),
            "medae": float(np.median(np.abs(band_true - band_pred))),
        }
    return report


def property_type_label(df) -> np.ndarray:
    """Map each row to one of `PROPERTY_TYPE_LABELS`."""
    propertytype = df["propertytype"].to_numpy()
    is_maisonette = df["is_maisonette"].to_numpy() == 1
    build_form = df["build_form"].to_numpy()
    mapped = np.array([_BUILD_FORM_TO_PROPERTY_TYPE.get(bf, "House") for bf in build_form], dtype=object)
    return np.where(is_maisonette, "Maisonette", np.where(propertytype == "F", "Flat", mapped))


def _accuracy_within_pct(y_true: np.ndarray, y_pred: np.ndarray, pct: float = 0.10) -> float:
    """Return the share of predictions within `pct` of the true price."""
    eps = np.finfo(np.float64).eps
    return float(np.mean(np.abs(y_pred - y_true) <= pct * np.maximum(np.abs(y_true), eps)))


def _grouped_accuracy_report(
    group_labels: np.ndarray, labels: List[str], y_true: np.ndarray, y_pred: np.ndarray
) -> Dict[str, Dict[str, Optional[float]]]:
    """Return MAE and within-10% accuracy per label, with all-None entries for empty labels."""
    group_labels = np.asarray(group_labels)
    report = {}
    for label in labels:
        mask = group_labels == label
        n = int(mask.sum())
        if n == 0:
            report[label] = {"n": 0, "mae": None, "accuracy_within_10pct": None}
            continue
        band_true, band_pred = y_true[mask], y_pred[mask]
        report[label] = {
            "n": n,
            "mae": float(mean_absolute_error(band_true, band_pred)),
            "accuracy_within_10pct": _accuracy_within_pct(band_true, band_pred),
        }
    return report


def compute_price_band_accuracy(y_true, y_pred) -> Dict[str, Dict[str, Optional[float]]]:
    """Return MAE and within-10% accuracy per `EXPLAIN_PRICE_BAND_LABELS` band."""
    if _is_cupy(y_true):
        y_true = y_true.get()
    if _is_cupy(y_pred):
        y_pred = y_pred.get()
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    bands = np.asarray(
        pd.cut(y_true, bins=EXPLAIN_PRICE_BAND_EDGES, labels=EXPLAIN_PRICE_BAND_LABELS, right=False)
    )
    return _grouped_accuracy_report(bands, EXPLAIN_PRICE_BAND_LABELS, y_true, y_pred)


def compute_property_type_accuracy(df, y_true, y_pred) -> Dict[str, Dict[str, Optional[float]]]:
    """Return MAE and within-10% accuracy per `PROPERTY_TYPE_LABELS` label."""
    if _is_cupy(y_true):
        y_true = y_true.get()
    if _is_cupy(y_pred):
        y_pred = y_pred.get()
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return _grouped_accuracy_report(property_type_label(df), PROPERTY_TYPE_LABELS, y_true, y_pred)


def _fit_and_evaluate(
    train_df,
    val_df,
    test_df,
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
    include_price_bands: bool = False,
) -> Tuple:
    """
    Add price statistics, encode, fit one forest on train and evaluate it.

    The single fit-and-evaluate core every split method, backtest and spatial
    evaluation uses. The splits must already be separated.

    Returns
    -------
    model, encoder, medians, postcode_stats, sector_stats
        The fitted artefacts.
    results : dict
        ``backend``, row counts, ``feature_columns``, ``sample_weight`` and
        ``val`` and ``test`` metrics with price-band and property-type
        accuracy, plus ``by_price_band`` when `include_price_bands` is set,
        and ``core_feature_missing_rates``.

    Raises
    ------
    RuntimeError
        From `check_core_feature_availability`, before any fitting.
    """
    core_feature_missing_rates = check_core_feature_availability(
        {"train": train_df, "val": val_df, "test": test_df},
        log=log,
        population=getattr(args, "population", DEFAULT_POPULATION),
    )
    train_df, val_df, test_df, postcode_stats = add_postcode_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )
    train_df, val_df, test_df, sector_stats = add_postcode_sector_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )
    X_train, X_val, X_test, encoder, medians, feature_metadata = impute_and_encode(
        train_df, val_df, test_df, use_ofsted=getattr(args, "use_ofsted", True)
    )

    on_gpu = _is_cudf(train_df)
    if on_gpu:
        import cupy as xp

        dtype = GPU_DTYPE
    else:
        xp = np
        dtype = np.float64

    def _target(part):
        """Return the target column as a cupy or numpy array."""
        return part[TARGET_COLUMN].to_cupy().astype(dtype) if on_gpu else part[TARGET_COLUMN].to_numpy(dtype=dtype)

    y_train_raw = _target(train_df)
    y_train = xp.log1p(y_train_raw) if args.log_target else y_train_raw

    rf_kwargs = dict(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
        max_features=args.max_features,
        random_state=args.random_state,
        n_jobs=args.n_jobs,
    )
    model, backend = resolve_regressor(args.device, log, **rf_kwargs)
    if log is not None:
        log.info(f"Training on backend: {backend}")
    sample_weight = sample_weight_for_training(y_train_raw, mode=args.sample_weight)
    if on_gpu:
        sample_weight = xp.asarray(sample_weight, dtype=dtype)
    model.fit(X_train, y_train, sample_weight=sample_weight)
    if log is not None:
        log.info(f"Sample weighting: --sample-weight {args.sample_weight}")

    results = {
        "backend": backend,
        "n_train": len(train_df),
        "n_val": len(val_df),
        "n_test": len(test_df),
        "feature_columns": feature_metadata["feature_columns"],
        "sample_weight": args.sample_weight,
        "core_feature_missing_rates": core_feature_missing_rates,
    }
    for split_name, X, part in (("val", X_val, val_df), ("test", X_test, test_df)):
        preds = xp.asarray(model.predict(X))
        if args.log_target:
            preds = xp.expm1(preds)
        y_true = _target(part)
        metrics = compute_metrics(y_true, preds)
        if include_price_bands:
            metrics["by_price_band"] = compute_metrics_by_price_band(y_true, preds)
        metrics["price_band_accuracy"] = compute_price_band_accuracy(y_true, preds)
        metrics["property_type_accuracy"] = compute_property_type_accuracy(part, y_true, preds)
        results[split_name] = metrics
        if log is not None:
            log.info(
                f"{split_name}: MAE=£{metrics['mae']:,.0f} RMSE=£{metrics['rmse']:,.0f} "
                f"R2={metrics['r2']:.3f} MAPE={metrics['mape']:.1%}"
            )
    return model, encoder, medians, postcode_stats, sector_stats, results


def split_dataset(df, args: argparse.Namespace, log: Optional[logging.Logger] = None) -> Tuple:
    """
    Split `df` with the method named by ``args.split_method``.

    Returns
    -------
    train_df, val_df, test_df : DataFrame
        The splits.
    extra : dict or None
        For ``walk_forward``, the cross-validation aggregate. That method fits
        and logs each fold, then returns the last fold's own train and
        validation pair so the final refit's validation stays held out.
    """
    method = args.split_method
    if method == "chronological":
        return time_series_split(df, args.train_frac, args.val_frac) + (None,)
    if method == "grouped":
        return grouped_time_series_split(df, args.train_frac, args.val_frac, log=log) + (None,)
    if method == "purged":
        return purged_embargo_time_series_split(
            df, args.train_frac, args.val_frac, embargo_days=args.embargo_days, log=log
        ) + (None,)
    if method == "spatial":
        return spatial_holdout_split(
            df,
            test_frac=args.spatial_test_frac,
            val_frac=args.spatial_val_frac,
            random_state=args.random_state,
            log=log,
        ) + (None,)
    if method == "recency":
        return recency_windowed_time_series_split(
            df, args.train_frac, args.val_frac, train_window_years=args.train_window_years, log=log
        ) + (None,)
    if method == "walk_forward":
        test_frac = 1.0 - args.train_frac - args.val_frac
        folds, non_test_df, test_df = time_series_cv_folds(df, n_folds=args.cv_folds, test_frac=test_frac, log=log)
        run_mixture = bool(getattr(args, "two_stage", False))
        fold_mae, fold_r2 = [], []
        fold_mixture_mae = [] if run_mixture else None
        for i, (fold_train, fold_val) in enumerate(folds):
            if run_mixture:
                fold_results = evaluate_two_stage_mixture(
                    fold_train,
                    fold_val,
                    fold_val,
                    args,
                    high_value_threshold=args.high_value_threshold,
                    log=None,
                )
                fold_mae.append(fold_results["val"]["general"]["mae"])
                fold_r2.append(fold_results["val"]["general"]["r2"])
                fold_mixture_mae.append(fold_results["val"]["mixture"]["mae"])
                if log is not None:
                    log.info(
                        f"walk-forward fold {i + 1}/{len(folds)}: "
                        f"general MAE=£{fold_results['val']['general']['mae']:,.0f} "
                        f"R2={fold_results['val']['general']['r2']:.3f} "
                        f"vs mixture MAE=£{fold_results['val']['mixture']['mae']:,.0f}"
                    )
            else:
                _, _, _, _, _, fold_results = _fit_and_evaluate(fold_train, fold_val, fold_val, args, log=None)
                fold_mae.append(fold_results["val"]["mae"])
                fold_r2.append(fold_results["val"]["r2"])
                if log is not None:
                    log.info(
                        f"walk-forward fold {i + 1}/{len(folds)}: "
                        f"MAE=£{fold_results['val']['mae']:,.0f} R2={fold_results['val']['r2']:.3f}"
                    )
        if log is not None:
            summary_line = (
                f"walk-forward CV across {len(folds)} folds: "
                f"MAE=£{np.mean(fold_mae):,.0f}±£{np.std(fold_mae):,.0f} "
                f"R2={np.mean(fold_r2):.3f}±{np.std(fold_r2):.3f}"
            )
            if run_mixture:
                summary_line += (
                    f" | mixture MAE=£{np.mean(fold_mixture_mae):,.0f}±£{np.std(fold_mixture_mae):,.0f}"
                )
            log.info(summary_line)
        walk_forward_cv = {
            "n_folds": len(folds),
            "fold_mae": fold_mae,
            "fold_r2": fold_r2,
            "mae_mean": float(np.mean(fold_mae)),
            "mae_std": float(np.std(fold_mae)),
            "r2_mean": float(np.mean(fold_r2)),
            "r2_std": float(np.std(fold_r2)),
        }
        if run_mixture:
            walk_forward_cv["mixture_mae"] = fold_mixture_mae
            walk_forward_cv["mixture_mae_mean"] = float(np.mean(fold_mixture_mae))
            walk_forward_cv["mixture_mae_std"] = float(np.std(fold_mixture_mae))
        last_fold_train, last_fold_val = folds[-1]
        return last_fold_train, last_fold_val, test_df, {"walk_forward_cv": walk_forward_cv}
    raise ValueError(f"Unknown split method {method!r}")


def run_training(df, args: argparse.Namespace, log: Optional[logging.Logger] = None) -> Dict:
    """
    Engineer, split, encode, train and evaluate over a loaded DataFrame.

    Separate from `load_dataset` so it can be tested on synthetic data. A cuDF
    `df` keeps the whole run on the GPU.
    """
    df = engineer_features(df).sort_values(DATE_COLUMN).reset_index(drop=True)
    train_df, val_df, test_df, _ = split_dataset(df, args, log=log)
    log_split_diagnostics(train_df, val_df, test_df, log)
    model, encoder, medians, postcode_stats, sector_stats, results = _fit_and_evaluate(
        train_df, val_df, test_df, args, log=log
    )
    if args.model_out:
        import joblib

        joblib.dump(
            {
                "model": model,
                "encoder": encoder,
                "lot_medians": medians,
                "postcode_stats": postcode_stats,
                "sector_stats": sector_stats,
                "price_stat_globals": {
                    "median_price": float(train_df[TARGET_COLUMN].median()),
                    "median_pps": float((train_df[TARGET_COLUMN] / train_df["total_floor_area"]).median()),
                },
                "feature_columns": results["feature_columns"],
                "missing_flag_columns": list(feature_layout(args.use_ofsted)["missing_flag_groups"]),
                "categorical_columns": feature_layout(args.use_ofsted)["categorical"],
                "use_ofsted": args.use_ofsted,
                "log_target": args.log_target,
                "sample_weight": args.sample_weight,
                "population": args.population,
                "train_period": [str(train_df[DATE_COLUMN].min())[:10], str(train_df[DATE_COLUMN].max())[:10]],
            },
            args.model_out,
        )
        if log is not None:
            log.info(f"Saved model to {args.model_out}")

    return results


def load_model(model_path, trust_pickle: bool = False) -> Dict:
    """
    Load a Random Forest saved by ``--model-out``: offline only, and only with `trust_pickle`.

    The file is a joblib pickle, and unpickling runs whatever code it names.
    Pass ``trust_pickle=True`` (``--trust-pickle`` on the command lines) only
    for a model you trained; the API never loads one.

    Returns
    -------
    dict
        The saved artefact: ``model``, ``encoder``, ``lot_medians``, the
        postcode and sector lookups, ``log_target`` and, for models saved since
        they were recorded, ``price_stat_globals``, ``population`` and
        ``train_period``.

    Raises
    ------
    FileNotFoundError
        When nothing is saved at `model_path`.
    ValueError
        When the file is not a ``--model-out`` artefact.
    PermissionError
        Without `trust_pickle`.
    """
    import joblib

    if not trust_pickle:
        raise PermissionError(
            f"load_model: {model_path} is an hpp_rf pickle, which runs code when loaded; pass "
            "--trust-pickle (trust_pickle=True) only for a model you trained"
        )

    model_path = Path(model_path)
    if not model_path.is_file():
        raise FileNotFoundError(
            f"load_model: no Random Forest at {model_path} -- train one with "
            "python -m hpldn.backend.scripts.hpp_rf --model-out <path>"
        )
    artefact = joblib.load(model_path)
    missing = {"model", "encoder", "lot_medians", "postcode_stats", "sector_stats"} - set(artefact)
    if missing:
        raise ValueError(f"load_model: {model_path} is not an hpp_rf --model-out artefact (missing {sorted(missing)})")
    return artefact


def _saved_price_stat_globals(artefact: Mapping) -> Dict[str, float]:
    """
    Return the train-wide fallbacks for postcodes and sectors the model never saw.

    Artefacts saved before ``price_stat_globals`` was recorded fall back to
    the median of the saved district medians, which is close to, but not
    exactly, the train-wide median the model was fitted with.
    """
    saved = artefact.get("price_stat_globals")
    if saved:
        return dict(saved)
    stats = artefact["postcode_stats"]
    return {
        "median_price": float(stats[POSTCODE_STAT_COLUMNS[0]].median()),
        "median_pps": float(stats[POSTCODE_STAT_COLUMNS[1]].median()),
    }


def predict_with_saved_model(artefact: Mapping, df, engineered: bool = False) -> np.ndarray:
    """
    Price the rows of `df` with a loaded Random Forest.

    `df` has ``production.london_houses``' columns. Unless `engineered` is
    set, `engineer_features` runs first (which computes the rolling and
    same-street statistics from `df` itself). Then only saved state is
    applied: the postcode and sector lookups (unseen groups get the train-wide
    fallbacks and a count of 0), the train medians and the fitted encoder.
    Nothing is refitted.

    Returns
    -------
    numpy.ndarray
        Prices in pounds, in `df`'s row order.
    """
    if not engineered:
        df = engineer_features(df)
    globals_ = _saved_price_stat_globals(artefact)
    for group_col, stats, columns in (
        ("postcode_district", artefact["postcode_stats"], POSTCODE_STAT_COLUMNS),
        ("postcode_sector", artefact["sector_stats"], SECTOR_STAT_COLUMNS),
    ):
        median_col, pps_col, count_col = columns
        df = df.drop(columns=[c for c in columns if c in df.columns])
        df = df.merge(stats, how="left", left_on=group_col, right_index=True)
        df[median_col] = df[median_col].fillna(globals_["median_price"])
        df[pps_col] = df[pps_col].fillna(globals_["median_pps"])
        df[count_col] = df[count_col].fillna(0)
    # Artefacts saved before the Ofsted group existed were trained without it.
    X = build_feature_matrix(df, artefact["encoder"], artefact["lot_medians"], artefact.get("use_ofsted", False))
    preds = np.asarray(artefact["model"].predict(X), dtype=np.float64)
    return np.expm1(preds) if artefact.get("log_target", True) else preds




def reserve_final_holdout_period(df, months: int = 12) -> Tuple:
    """
    Split a date-sorted `df` into development rows and the final `months` months.

    The holdout must not be used for any fitting until the final evaluation.
    """
    if months <= 0:
        raise ValueError(f"months ({months}) must be positive")
    max_date = pd.Timestamp(pd.to_datetime(df[DATE_COLUMN]).max())
    cutoff = max_date - pd.DateOffset(months=months)
    is_holdout = pd.to_datetime(df[DATE_COLUMN]) > cutoff
    development_df, holdout_df = df[~is_holdout.to_numpy()], df[is_holdout.to_numpy()]
    if len(development_df) == 0 or len(holdout_df) == 0:
        raise ValueError(
            f"reserve_final_holdout_period(months={months}) produced an empty development "
            f"({len(development_df)} rows) or holdout ({len(holdout_df)} rows) split from {len(df)} rows"
        )
    return development_df, holdout_df


def expanding_window_year_folds(
    df, log: Optional[logging.Logger] = None
) -> List[Tuple[int, object, object]]:
    """
    Return expanding-window annual folds: train before year Y, test year Y.

    `df` must have ``sale_year`` and should exclude the final holdout.

    Returns
    -------
    list of tuple
        ``(test_year, train_df, test_df)``, oldest first.

    Raises
    ------
    ValueError
        If no year has both prior history and its own rows.
    """
    years = sorted(pd.Series(df["sale_year"]).unique())
    folds = []
    for test_year in years[1:]:  
        train_df = df[df["sale_year"] < test_year]
        test_df = df[df["sale_year"] == test_year]
        if len(train_df) == 0 or len(test_df) == 0:
            continue
        folds.append((int(test_year), train_df, test_df))
    if not folds:
        raise ValueError(
            "expanding_window_year_folds found no calendar year with both prior "
            "training history and its own rows to test on"
        )
    if log is not None:
        log.info(f"expanding-window backtest: {len(folds)} annual folds, test years {folds[0][0]}-{folds[-1][0]}")
    return folds


BACKTEST_METRIC_NAMES = ("mae", "rmse", "mape", "medae")


def aggregate_fold_metrics(fold_results: List[Dict], split_name: str = "test") -> Dict[str, Dict[str, float]]:
    """
    Return the row-weighted mean and unweighted standard deviation of fold metrics.

    Covers MAE, RMSE, MAPE and median absolute error from each fold's
    `split_name` results.
    """
    n_key = f"n_{split_name}"
    weights = np.array([fold[n_key] for fold in fold_results], dtype=float)
    aggregate = {}
    for metric in BACKTEST_METRIC_NAMES:
        values = np.array([fold[split_name][metric] for fold in fold_results], dtype=float)
        aggregate[metric] = {
            "weighted_mean": float(np.average(values, weights=weights)),
            "std": float(np.std(values)),
            "min": float(values.min()),
            "max": float(values.max()),
        }
    return aggregate


def backtest_expanding_window(
    df, args: argparse.Namespace, holdout_months: int = 12, log: Optional[logging.Logger] = None
) -> Dict:
    """
    Backtest with expanding annual folds and an untouched final holdout.

    Reserves the final `holdout_months`, evaluates one fold per qualifying year
    through `_fit_and_evaluate`, aggregates the fold metrics, then fits once on
    all development data and evaluates the holdout.

    Returns
    -------
    dict
        ``folds``, ``aggregate`` and ``final_holdout``.
    """
    df = engineer_features(df).sort_values(DATE_COLUMN).reset_index(drop=True)
    development_df, holdout_df = reserve_final_holdout_period(df, months=holdout_months)

    fold_reports = []
    for test_year, train_fold, test_fold in expanding_window_year_folds(development_df, log=log):
        _, _, _, _, _, fold_results = _fit_and_evaluate(
            train_fold, test_fold, test_fold, args, log=None, include_price_bands=True
        )
        fold_results["test_year"] = test_year
        fold_reports.append(fold_results)
        if log is not None:
            log.info(
                f"[backtest] train-through-{test_year - 1} -> test {test_year}: "
                f"MAE=£{fold_results['test']['mae']:,.0f} MedAE=£{fold_results['test']['medae']:,.0f} "
                f"RMSE=£{fold_results['test']['rmse']:,.0f} MAPE={fold_results['test']['mape']:.1%}"
            )

    aggregate = aggregate_fold_metrics(fold_reports, split_name="test")
    if log is not None:
        summary = ", ".join(
            f"{metric}={aggregate[metric]['weighted_mean']:,.3f}±{aggregate[metric]['std']:,.3f}"
            for metric in BACKTEST_METRIC_NAMES
        )
        log.info(f"[backtest] across {len(fold_reports)} annual folds: {summary}")

    _, _, _, _, _, final_holdout_results = _fit_and_evaluate(
        development_df, holdout_df, holdout_df, args, log=log, include_price_bands=True
    )
    if log is not None:
        log.info(
            f"[backtest] final {holdout_months}-month untouched holdout ({len(holdout_df)} rows): "
            f"MAE=£{final_holdout_results['test']['mae']:,.0f} MedAE=£{final_holdout_results['test']['medae']:,.0f}"
        )
    return {"folds": fold_reports, "aggregate": aggregate, "final_holdout": final_holdout_results}




def spatial_new_area_holdout(
    train_df,
    test_df,
    group_col: str = "postcode_sector",
    new_area_frac: float = 0.15,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
) -> Tuple:
    """
    Remove a share of test-period areas from training so they are genuinely unseen.

    Keeps the temporal split and holds out `new_area_frac` of the `group_col`
    areas present in both splits from all training rows. Areas only in test
    count as new too.

    Returns
    -------
    train_df : DataFrame
        Training rows without the held-out areas.
    known_area_test_df, new_area_test_df : DataFrame
        Test rows in seen and unseen areas.
    holdout_areas : set
        The areas removed from training.
    """
    train_groups = set(pd.Series(train_df[group_col]).unique())
    test_groups = set(pd.Series(test_df[group_col]).unique())
    overlapping = sorted(train_groups & test_groups)

    rng = np.random.default_rng(random_state)
    n_holdout = max(1, int(round(len(overlapping) * new_area_frac))) if overlapping else 0
    holdout_areas = set(rng.permutation(np.array(overlapping, dtype=object))[:n_holdout]) if overlapping else set()

    train_out = train_df[~train_df[group_col].isin(holdout_areas)]
    remaining_train_groups = set(pd.Series(train_out[group_col]).unique())
    is_new = test_df[group_col].isin(holdout_areas) | ~test_df[group_col].isin(remaining_train_groups)
    new_area_test_df = test_df[is_new]
    known_area_test_df = test_df[~is_new]

    if log is not None:
        log.info(
            f"spatial generalisation ({group_col}): held out {len(holdout_areas)}/{len(overlapping)} "
            f"overlapping areas from training -> {len(known_area_test_df)} known-area / "
            f"{len(new_area_test_df)} new-area test rows"
        )
    return train_out, known_area_test_df, new_area_test_df, holdout_areas


def evaluate_spatial_generalisation(
    df,
    args: argparse.Namespace,
    group_col: str = "postcode_sector",
    new_area_frac: float = 0.15,
    holdout_months: int = 12,
    log: Optional[logging.Logger] = None,
) -> Dict:
    """
    Report known-area and new-area performance within the final holdout period.

    Holds out areas with `spatial_new_area_holdout` and fits one model, whose
    validation metrics are known-area and test metrics new-area.

    Returns
    -------
    dict
        ``known_area``, ``new_area``, ``group_col``, ``holdout_areas``,
        ``backend`` and row counts.
    """
    df = engineer_features(df).sort_values(DATE_COLUMN).reset_index(drop=True)
    development_df, test_period_df = reserve_final_holdout_period(df, months=holdout_months)
    train_spatial, known_test, new_test, holdout_areas = spatial_new_area_holdout(
        development_df,
        test_period_df,
        group_col=group_col,
        new_area_frac=new_area_frac,
        random_state=args.random_state,
        log=log,
    )
    _, _, _, _, _, results = _fit_and_evaluate(
        train_spatial, known_test, new_test, args, log=log, include_price_bands=True
    )
    results["group_col"] = group_col
    results["holdout_areas"] = sorted(str(area) for area in holdout_areas)
    results["known_area"] = results.pop("val")
    results["new_area"] = results.pop("test")
    return results




def residual_slice_report(
    test_df,
    y_true,
    y_pred,
    group_cols: Tuple[str, ...] = ("postcode_district", "propertytype", "sale_year"),
    floor_area_col: str = "total_floor_area",
    floor_area_bins: Tuple[float, ...] = (0, 50, 75, 100, 150, 250, np.inf),
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """
    Break residuals down by district, type, year, floor-area band and price band.

    Distinguishes data problems from valid expensive homes the model fits
    poorly.

    Returns
    -------
    dict
        Per slice column, ``n``, ``mean_residual``, ``median_abs_error`` and
        ``mape`` for each value.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    residual = y_pred - y_true
    abs_error = np.abs(residual)
    eps = np.finfo(np.float64).eps

    frame = pd.DataFrame({"residual": residual, "abs_error": abs_error, "price": y_true}, index=test_df.index)
    for col in group_cols:
        frame[col] = test_df[col].to_numpy()
    frame["floor_area_band"] = pd.cut(np.asarray(test_df[floor_area_col], dtype=float), bins=floor_area_bins)
    frame["price_band"] = price_band(y_true)

    report = {}
    for col in list(group_cols) + ["floor_area_band", "price_band"]:
        slices = {}
        for key, sub in frame.groupby(col, observed=True):
            slices[str(key)] = {
                "n": int(len(sub)),
                "mean_residual": float(sub["residual"].mean()),
                "median_abs_error": float(sub["abs_error"].median()),
                "mape": float((sub["abs_error"] / sub["price"].clip(lower=eps)).mean()),
            }
        report[col] = slices
    return report


def fit_high_value_expert(
    X_train,
    y_train_raw,
    y_train,
    args: argparse.Namespace,
    high_value_threshold: float = DEFAULT_HIGH_VALUE_THRESHOLD,
    log: Optional[logging.Logger] = None,
):
    """
    Fit a forest on training rows priced at or above `high_value_threshold`.

    Reuses the general model's encoded matrix and target transform. Depth and
    leaf size default to the general model's, overridable with
    ``--high-value-max-depth`` and ``--high-value-min-samples-leaf``.

    Returns
    -------
    object or None
        The fitted expert, or None with fewer than
        `MIN_HIGH_VALUE_TRAIN_ROWS` qualifying rows.
    """
    on_gpu = _is_cupy(X_train)
    mask = np.asarray(y_train_raw.get() if on_gpu else y_train_raw) >= high_value_threshold
    n = int(mask.sum())
    if n < MIN_HIGH_VALUE_TRAIN_ROWS:
        if log is not None:
            log.info(
                f"two-stage mixture: only {n} train rows >= £{high_value_threshold:,.0f} "
                f"(< MIN_HIGH_VALUE_TRAIN_ROWS={MIN_HIGH_VALUE_TRAIN_ROWS}); skipping the high-value expert"
            )
        return None

    high_value_max_depth = getattr(args, "high_value_max_depth", None)
    high_value_min_samples_leaf = getattr(args, "high_value_min_samples_leaf", None)
    rf_kwargs = dict(
        n_estimators=args.n_estimators,
        max_depth=high_value_max_depth if high_value_max_depth is not None else args.max_depth,
        min_samples_leaf=(
            high_value_min_samples_leaf if high_value_min_samples_leaf is not None else args.min_samples_leaf
        ),
        max_features=args.max_features,
        random_state=args.random_state,
        n_jobs=args.n_jobs,
    )
    model, _ = resolve_regressor(args.device, None, **rf_kwargs)
    model.fit(X_train[mask], y_train[mask])
    if log is not None:
        log.info(f"two-stage mixture: fit high-value expert on {n} train rows >= £{high_value_threshold:,.0f}")
    return model


def predict_two_stage_mixture(
    general_model,
    high_value_model,
    X,
    args: argparse.Namespace,
    high_value_threshold: float = DEFAULT_HIGH_VALUE_THRESHOLD,
):
    """
    Predict with the general model, using the expert where that prediction is high.

    Routing uses the general model's prediction, never the true price, so it
    works at inference time. Without an expert the general predictions are
    returned unchanged.
    """
    preds = np.asarray(general_model.predict(X))
    if args.log_target:
        preds = np.expm1(preds)
    if high_value_model is None:
        return preds
    route_mask = preds >= high_value_threshold
    if route_mask.any():
        expert_preds = np.asarray(high_value_model.predict(X[route_mask]))
        if args.log_target:
            expert_preds = np.expm1(expert_preds)
        preds = preds.copy()
        preds[route_mask] = expert_preds
    return preds


def evaluate_two_stage_mixture(
    train_df,
    val_df,
    test_df,
    args: argparse.Namespace,
    high_value_threshold: float = DEFAULT_HIGH_VALUE_THRESHOLD,
    log: Optional[logging.Logger] = None,
) -> Dict:
    """
    Compare the general model with the general-plus-expert mixture on validation and test.

    Uses the same leakage-safe pipeline as `_fit_and_evaluate`, with matrices
    brought to host after encoding.

    Returns
    -------
    dict
        Metrics, including price bands, for both models.
    """
    train_df, val_df, test_df, postcode_stats = add_postcode_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )
    train_df, val_df, test_df, sector_stats = add_postcode_sector_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )
    X_train, X_val, X_test, encoder, medians, _feature_metadata = impute_and_encode(
        train_df, val_df, test_df, use_ofsted=getattr(args, "use_ofsted", True)
    )
    if _is_cudf(train_df):
        X_train, X_val, X_test = X_train.get(), X_val.get(), X_test.get()

    y_train_raw = train_df[TARGET_COLUMN].to_numpy(dtype=np.float64)
    y_train = np.log1p(y_train_raw) if args.log_target else y_train_raw

    rf_kwargs = dict(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
        max_features=args.max_features,
        random_state=args.random_state,
        n_jobs=args.n_jobs,
    )
    general_model, backend = resolve_regressor(args.device, log, **rf_kwargs)
    general_model.fit(
        X_train, y_train, sample_weight=sample_weight_for_training(y_train_raw, mode=args.sample_weight)
    )
    high_value_model = fit_high_value_expert(
        X_train, y_train_raw, y_train, args, high_value_threshold=high_value_threshold, log=log
    )

    results = {
        "backend": backend,
        "n_train": len(train_df),
        "n_val": len(val_df),
        "n_test": len(test_df),
        "high_value_threshold": high_value_threshold,
        "high_value_train_rows": int((y_train_raw >= high_value_threshold).sum()),
    }
    for split_name, X, part in (("val", X_val, val_df), ("test", X_test, test_df)):
        y_true = part[TARGET_COLUMN].to_numpy(dtype=np.float64)
        general_preds = np.asarray(general_model.predict(X))
        if args.log_target:
            general_preds = np.expm1(general_preds)
        mixture_preds = predict_two_stage_mixture(
            general_model, high_value_model, X, args, high_value_threshold=high_value_threshold
        )
        results[split_name] = {
            "general": {**compute_metrics(y_true, general_preds), "by_price_band": compute_metrics_by_price_band(y_true, general_preds)},
            "mixture": {**compute_metrics(y_true, mixture_preds), "by_price_band": compute_metrics_by_price_band(y_true, mixture_preds)},
        }
        if log is not None:
            log.info(
                f"{split_name} two-stage mixture: general MAE=£{results[split_name]['general']['mae']:,.0f} "
                f"vs mixture MAE=£{results[split_name]['mixture']['mae']:,.0f}"
            )
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Train a Random Forest to predict London house prices from production.london_houses."
    )
    parser.add_argument("--device", choices=["cpu", "gpu", "auto"], default="auto", help="Where to train (default: auto-detect)")
    parser.add_argument("--n-estimators", type=int, default=300)
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help=(
            "Max tree depth (default: unlimited on CPU; on GPU, unset falls back to "
            f"GPU_MAX_DEPTH_DEFAULT={GPU_MAX_DEPTH_DEFAULT} — see resolve_regressor, "
            "unlimited depth there blows up cuML's host-side Treelite RAM usage)"
        ),
    )
    parser.add_argument("--min-samples-leaf", type=int, default=2)
    parser.add_argument("--max-features", default="sqrt")
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--n-jobs", type=int, default=-1, help="scikit-learn (cpu) only")
    parser.add_argument("--train-frac", type=float, default=0.7)
    parser.add_argument("--val-frac", type=float, default=0.15)
    parser.add_argument(
        "--split-method",
        choices=["chronological", "grouped", "purged", "walk_forward", "spatial", "recency"],
        default="chronological",
        help=(
            "How to split train/val/test (default: chronological, a "
            "contiguous time-ordered cut). See split_dataset for what each alternative "
            "tests: grouped=no property (uprn) spans train and val/test; "
            "purged=embargo gap at each split boundary; walk_forward=K-fold "
            "rolling-origin CV averaged before a final refit; spatial=whole "
            "postcode_districts held out (geographic, not temporal, "
            "generalization); recency=train on only the last N years before val."
        ),
    )
    parser.add_argument(
        "--embargo-days", type=int, default=30, help="--split-method purged only"
    )
    parser.add_argument(
        "--cv-folds", type=int, default=4, help="--split-method walk_forward only"
    )
    parser.add_argument(
        "--spatial-test-frac", type=float, default=0.15, help="--split-method spatial only"
    )
    parser.add_argument(
        "--spatial-val-frac", type=float, default=0.15, help="--split-method spatial only"
    )
    parser.add_argument(
        "--train-window-years", type=float, default=5.0, help="--split-method recency only"
    )
    parser.add_argument(
        "--log-target",
        dest="log_target",
        action="store_true",
        default=True,
        help="Train on log1p(price) and invert for evaluation (default: on, prices are right-skewed)",
    )
    parser.add_argument("--no-log-target", dest="log_target", action="store_false")
    parser.add_argument(
        "--no-ofsted",
        dest="use_ofsted",
        action="store_false",
        help="Leave out the Ofsted school features (migration 0034) and their missing flag",
    )
    parser.add_argument(
        "--sample-weight",
        dest="sample_weight",
        choices=["none", "sqrt", "full"],
        default="none",
        help=(
            "Inverse-price-band training weight (see "
            "sample_weight_for_training, shared with hpp_cb). 'none' (the default) fits "
            "unweighted, which is the only mode that matches every reported metric "
            "(MAE/RMSE/MedAE/MAPE) being itself unweighted. 'full' applies "
            "the raw ~13x band spread; 'sqrt' is the compressed middle ground."
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of rows loaded, for a quick smoke run")
    parser.add_argument(
        "--population",
        choices=list(POPULATIONS),
        default=DEFAULT_POPULATION,
        help=(
            "Which sales to model (default: epc_matched, the sales with a usable pre-sale EPC, "
            "in the view). 'all' adds ~1M mostly pre-2009 sales without floor area "
            "or rating: the core-feature check then reports their EPC missingness on the train "
            "split instead of failing, but still fails when the val/test splits lack them"
        ),
    )
    parser.add_argument("--model-out", type=Path, default=None, help="Path to save the trained model+encoder (joblib)")
    parser.add_argument(
        "--backtest",
        action="store_true",
        help=(
            "Run task 1's expanding-window annual backtest (train-through-Y-1/test-Y "
            "folds, plus an untouched final holdout) instead of a single split — see "
            "backtest_expanding_window"
        ),
    )
    parser.add_argument(
        "--spatial-generalisation",
        dest="spatial_generalisation",
        action="store_true",
        help=(
            "Run task 2's known-area vs. new-area generalisation check instead of a "
            "single split — see evaluate_spatial_generalisation"
        ),
    )
    parser.add_argument(
        "--spatial-group-col",
        choices=ROLLING_COMP_GROUP_COLUMNS,
        default="postcode_sector",
        help="--spatial-generalisation only: geography held out (see evaluate_spatial_generalisation)",
    )
    parser.add_argument(
        "--new-area-frac", type=float, default=0.15, help="--spatial-generalisation only"
    )
    parser.add_argument(
        "--holdout-months",
        type=int,
        default=12,
        help="--backtest/--spatial-generalisation only: months reserved as the untouched final period (task 1.ii, 6-12 recommended)",
    )
    parser.add_argument(
        "--two-stage",
        action="store_true",
        help=(
            "Run task 4's two-stage/mixture evaluation (general model vs. a dedicated "
            "high-value expert) instead of a single split — see evaluate_two_stage_mixture"
        ),
    )
    parser.add_argument(
        "--high-value-threshold",
        type=float,
        default=DEFAULT_HIGH_VALUE_THRESHOLD,
        help="--two-stage only: price at/above which the high-value expert is trained/routed to (default £2m)",
    )
    parser.add_argument(
        "--high-value-max-depth",
        type=int,
        default=None,
        help=(
            "--two-stage only: max tree depth for the high-value expert (default: same as "
            "--max-depth). The expert trains on a dataset roughly 50-100x smaller than the "
            "general model, so GPU_MAX_DEPTH_DEFAULT's RAM-driven cap does not apply as "
            "tightly there"
        ),
    )
    parser.add_argument(
        "--high-value-min-samples-leaf",
        type=int,
        default=None,
        help="--two-stage only: min_samples_leaf for the high-value expert (default: same as --min-samples-leaf)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> Dict:
    """Load the dataset, run the selected training or evaluation mode and print JSON."""
    args = build_arg_parser().parse_args(argv)
    log = get_logger(args.verbose)
    set_global_seed(args.random_state)
    load_env_vars(log)
    from sqlalchemy import create_engine

    engine = create_engine(get_model_db_url(log))
    use_gpu, resources, _ = resolve_device(args.device, log)
    if use_gpu:
        log.info(
            f"CUDA GPU detected ({resources.gpu_count}x {resources.gpu_manufacturer} "
            f"{resources.gpu_name}); loading {VIEW_NAME} straight into GPU memory (cuDF)"
        )
    df = load_dataset(engine, limit=args.limit, log=log, use_gpu=use_gpu, population=args.population)
    if args.backtest:
        results = backtest_expanding_window(df, args, holdout_months=args.holdout_months, log=log)
    elif args.spatial_generalisation:
        results = evaluate_spatial_generalisation(
            df,
            args,
            group_col=args.spatial_group_col,
            new_area_frac=args.new_area_frac,
            holdout_months=args.holdout_months,
            log=log,
        )
    elif args.two_stage:
        df_engineered = engineer_features(df).sort_values(DATE_COLUMN).reset_index(drop=True)
        train_df, val_df, test_df, _ = split_dataset(df_engineered, args, log=log)
        results = evaluate_two_stage_mixture(
            train_df, val_df, test_df, args, high_value_threshold=args.high_value_threshold, log=log
        )
    else:
        results = run_training(df, args, log=log)
    print(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    main()
