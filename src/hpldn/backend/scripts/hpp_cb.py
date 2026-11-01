"""
CatBoost model for London house-price prediction.

Trains CatBoostRegressor on ``production.london_houses`` as the gradient-boosting
counterpart of `hpp_rf`, reusing its split functions and feature engineering.
Supports single-split training, expanding-window backtests, spatial
generalisation, a high-value expert mixture, and saving, scoring or
warm-starting from a model. See ``scripts/README.md``.

Usage::

    python -m hpldn.backend.scripts.hpp_cb
    python -m hpldn.backend.scripts.hpp_cb --device gpu --iterations 2000
    python -m hpldn.backend.scripts.hpp_cb --split-method spatial --model-out /tmp/hpp_cb.cbm
    python -m hpldn.backend.scripts.hpp_cb --model-in /tmp/hpp_cb.cbm
    python -m hpldn.backend.scripts.hpp_cb --model-in /tmp/hpp_cb.cbm --model-out /tmp/hpp_cb_v2.cbm --backtest
    python -m hpldn.backend.scripts.hpp_cb --two-stage --high-value-threshold 2000000
    python -m hpldn.backend.scripts.hpp_cb --backtest --target log_price --train-from-year 2009
    python -m hpldn.backend.scripts.hpp_cb --backtest --recency-halflife-years 6
    python -m hpldn.backend.scripts.hpp_cb --backtest --test-year 2026
"""

import argparse
import copy
import gc
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from hpldn.backend.common import hardware, profiling
from hpldn.backend.common.constants import FLOOR_LEVEL_CANONICAL
from hpldn.backend.common.db import get_model_db_url
from hpldn.backend.common.tuning import HostResources, detect_host_resources
from hpldn.backend.scripts.hpp_rf import (
    ANCHOR_COLUMN,
    ANCHOR_FEATURE_COLUMNS,
    ANCHOR_SOURCE_COLUMNS,
    BACKTEST_METRIC_NAMES,
    CATEGORICAL_COLUMNS,
    CORE_FEATURES,
    COMP_DEPENDENT_COLUMNS,
    COMP_STAT_COLUMNS,
    DATE_COLUMN,
    DEFAULT_HIGH_VALUE_THRESHOLD,
    DEFAULT_POPULATION,
    EPC_CORE_FEATURES,
    EXACT_NUMERIC_COLUMNS,
    ENGINEERED_COLUMNS,
    FEATURE_COLUMNS,
    LOCATION_DEPENDENT_COLUMNS,
    LOT_AREA_DEPENDENT_COLUMNS,
    MACRO_STAT_COLUMNS,
    MAISONETTE_DEPENDENT_COLUMNS,
    MARKET_RELATIVE_COLUMNS,
    MIN_HIGH_VALUE_TRAIN_ROWS,
    MSOA_TREND_DEPENDENT_COLUMNS,
    OFSTED_CATEGORICAL_COLUMNS,
    OFSTED_COLUMNS,
    OFSTED_DEPENDENT_COLUMNS,
    OFSTED_MISSING_FLAG,
    OFSTED_NUMERIC_COLUMNS,
    require_ofsted_columns,
    NEIGHBOURHOOD_DEPENDENT_COLUMNS,
    NUMERIC_COLUMNS,
    NUMERIC_DEPENDENT_COLUMNS,
    POPULATION_ALL,
    POPULATIONS,
    POSTCODE_STAT_COLUMNS,
    PREV_SALE_DEPENDENT_COLUMNS,
    PRICE_BAND_EDGES,
    PRICE_BAND_LABELS,
    PTAL_DEPENDENT_COLUMNS,
    RIVER_DEPENDENT_COLUMNS,
    ROLLING_COMP_DEPENDENT_COLUMNS,
    ROLLING_COMP_GROUP_COLUMNS,
    SAME_STREET_DEPENDENT_COLUMNS,
    SECTOR_STAT_COLUMNS,
    SAME_STREET_STAT_COLUMNS,
    TARGET_COLUMN,
    TRANSIT_STAT_COLUMNS,
    VIEW_COLUMN_KINDS,
    VIEW_NAME,
    _coerce_chunk_dtypes,
    _series_value_kind,
    add_market_relative_features,
    add_sizeband_comp_features,
    add_postcode_price_stats,
    add_postcode_sector_price_stats,
    aggregate_fold_metrics,
    check_core_feature_availability,
    compute_metrics,
    compute_metrics_by_price_band,
    dataset_sql,
    engineer_features,
    expanding_window_year_folds,
    group_price_fallbacks,
    grouped_time_series_split,
    load_ukhpi,
    log_split_diagnostics,
    price_band,
    purged_embargo_time_series_split,
    recency_windowed_time_series_split,
    reserve_final_holdout_period,
    sample_weight_for_training as _sample_weight_for_training,
    set_global_seed,
    spatial_holdout_split,
    spatial_new_area_holdout,
    time_series_cv_folds,
    time_series_split,
)

POSTCODE_STAT_DEPENDENT_COLUMNS = list(POSTCODE_STAT_COLUMNS) + list(
    SECTOR_STAT_COLUMNS
)
TRANSIT_DEPENDENT_COLUMNS = list(TRANSIT_STAT_COLUMNS)

_MISSING_FLAG_GROUPS = {
    "lot_area_missing": LOT_AREA_DEPENDENT_COLUMNS,
    "location_missing": LOCATION_DEPENDENT_COLUMNS,
    "comp_missing": COMP_DEPENDENT_COLUMNS,
    "rolling_comp_missing": ROLLING_COMP_DEPENDENT_COLUMNS,
    "ptal_missing": PTAL_DEPENDENT_COLUMNS,
    "river_missing": RIVER_DEPENDENT_COLUMNS,
    "prev_sale_missing": PREV_SALE_DEPENDENT_COLUMNS,
    "same_street_missing": SAME_STREET_DEPENDENT_COLUMNS,
    "neighbourhood_missing": NEIGHBOURHOOD_DEPENDENT_COLUMNS,
    "msoa_trend_missing": MSOA_TREND_DEPENDENT_COLUMNS,
    "numeric_missing": NUMERIC_DEPENDENT_COLUMNS,
    "postcode_stat_missing": POSTCODE_STAT_DEPENDENT_COLUMNS,
    "transit_missing": TRANSIT_DEPENDENT_COLUMNS,
    "maisonette_missing": MAISONETTE_DEPENDENT_COLUMNS,
}

MORTGAGE_RATE_TABLE = "production.mortgage_rates"
MORTGAGE_RATE_DATE_COLUMN = "rate_date"
MORTGAGE_RATE_COLUMN = "rate_2y_75ltv"
MORTGAGE_RATE_LAG_MONTHS = (3, 6, 12)
MORTGAGE_RATE_CHANGE_MONTHS = 12
MORTGAGE_RATE_AT_SALE_COLUMN = f"{MORTGAGE_RATE_COLUMN}_at_sale"
MORTGAGE_RATE_LAG_COLUMNS = [
    f"{MORTGAGE_RATE_COLUMN}_lag_{m}m" for m in MORTGAGE_RATE_LAG_MONTHS
]
MORTGAGE_RATE_CHANGE_COLUMN = (
    f"{MORTGAGE_RATE_COLUMN}_change_{MORTGAGE_RATE_CHANGE_MONTHS}m"
)
MORTGAGE_RATE_DEPENDENT_COLUMNS = (
    [MORTGAGE_RATE_AT_SALE_COLUMN]
    + MORTGAGE_RATE_LAG_COLUMNS
    + [MORTGAGE_RATE_CHANGE_COLUMN]
)
if MORTGAGE_RATE_CHANGE_MONTHS not in MORTGAGE_RATE_LAG_MONTHS:
    raise ValueError(
        f"MORTGAGE_RATE_CHANGE_MONTHS ({MORTGAGE_RATE_CHANGE_MONTHS}) must be one of "
        f"MORTGAGE_RATE_LAG_MONTHS {MORTGAGE_RATE_LAG_MONTHS}: the change feature is the "
        "rate at sale minus that lag"
    )

_MISSING_FLAG_GROUPS["mortgage_rate_missing"] = MORTGAGE_RATE_DEPENDENT_COLUMNS

IOD_LEVEL_COLUMNS = [
    "iod_education_score",
    "iod_education_pct_london",
    "iod_outdoors_score",
    "iod_outdoors_pct_london",
    "iod_indoors_score",
    "iod_income_score",
    "iod_income_pct_london",
    "iod_employment_score",
    "iod_health_score",
    "iod_crime_score",
    "iod_wider_barriers_score",
    "iod_geo_barriers_score",
    "iod_idaci_score",
    "iod_idaopi_score",
    "iod_children_score",
    "iod_adult_skills_score",
    "iod_imd_score",
    "iod_imd_pct_london",
]
IOD_CONTEXT_COLUMNS = [
    "iod_imd_score_vs_lad",
    "iod_imd_contrast_1km",
    "iod_income_contrast_1km",
    "iod_release_age_years",
]
IOD_CHANGE_COLUMNS = [
    "iod_imd_change",
    "iod_income_change",
    "iod_education_change",
    "iod_imd_pct_london_change",
    "iod_years_between_releases",
]
IOD_NUMERIC_COLUMNS = IOD_LEVEL_COLUMNS + IOD_CONTEXT_COLUMNS + IOD_CHANGE_COLUMNS
IOD_ORDINAL_COLUMNS = ["iod_imd_decile_national"]
IOD_CATEGORICAL_COLUMNS = ["iod_release_year"]

_MISSING_FLAG_GROUPS["iod_level_missing"] = IOD_LEVEL_COLUMNS
_MISSING_FLAG_GROUPS["iod_context_missing"] = IOD_CONTEXT_COLUMNS
_MISSING_FLAG_GROUPS["iod_change_missing"] = IOD_CHANGE_COLUMNS
# Ofsted school features (migration 0034; `hpp_rf.OFSTED_COLUMNS`), off with --no-ofsted.
_MISSING_FLAG_GROUPS[OFSTED_MISSING_FLAG] = OFSTED_DEPENDENT_COLUMNS

DROP_FEATURES_PRESETS: Dict[str, List[str]] = {
    "f1_mortgage_rate_extrapolation": [MORTGAGE_RATE_AT_SALE_COLUMN]
    + MORTGAGE_RATE_LAG_COLUMNS,
    "f2_iod_calendar_proxies": ["iod_release_year", "iod_release_age_years"],
    "f3_iod_pct_london": [
        "iod_education_pct_london",
        "iod_outdoors_pct_london",
        "iod_income_pct_london",
        "iod_imd_pct_london",
        "iod_imd_pct_london_change",
    ],
    "f4_iod_imd_composite": ["iod_imd_score", "iod_imd_change"],
    "f7_iod_level_missing": ["iod_level_missing"],
    "f8_high_cardinality_geo": ["lsoa_code", "msoa_code"],
    # The same matrix as --no-ofsted, through --drop-features (ablation runs).
    "no_ofsted": list(OFSTED_COLUMNS) + [OFSTED_MISSING_FLAG],
}

C1_DEFAULT_DROPPED_COLUMNS: List[str] = [
    "log_floor_area",
    "log_lot_area",
    "ptal_band_rank",
    "iod_imd_decile_national",
    "iod_years_between_releases",
    "epc_band_share_a",
    "epc_band_share_g",
    "boe_rate_at_sale",
]
DROP_FEATURES_PRESETS["c1_provably_redundant"] = C1_DEFAULT_DROPPED_COLUMNS

# Dropped by default too. production.mortgage_rates starts in 2007, so these are
# 46-51 % missing on a 1995-onward train set and the availability guard kept
# some and dropped others fold by fold; and under the deflated target they are
# time proxies (the 2023 rate spike reads as 2008). The f1 preset plus the
# 12-month change.
MORTGAGE_RATE_DEFAULT_DROPPED_COLUMNS: List[str] = list(MORTGAGE_RATE_DEPENDENT_COLUMNS)

DEFAULT_DROPPED_COLUMNS: List[str] = C1_DEFAULT_DROPPED_COLUMNS + MORTGAGE_RATE_DEFAULT_DROPPED_COLUMNS

DEFAULT_ITERATIONS = 8000
DEFAULT_EARLY_STOPPING_ROUNDS = 200

# Pinned rather than CatBoost's size-based auto rate (0.03 on 1.9M rows), which
# pass 1 realised and pass 2 inherited: the final refit stopped at 787 trees.
# Recommended run: --iterations 20000 --early-stopping-rounds 400.
DEFAULT_LEARNING_RATE = 0.06

# Points of the eval-carve learning curve logged after pass 1, as shares of
# the rounds run.
LEARNING_CURVE_FRACTIONS = (0.25, 0.5, 0.75, 1.0)


def _warn_if_iterations_truncated(
    model,
    iterations: int,
    early_stopping_rounds: int,
    log: Optional[logging.Logger],
    context: str,
    fatal: bool = False,
) -> None:
    """
    Warn when a fit likely hit ``--iterations`` rather than stopping early.

    Raises
    ------
    RuntimeError
        Instead of warning, when `fatal` is set.
    """
    best_iteration = model.get_best_iteration()
    truncated = (
        best_iteration is not None
        and best_iteration + early_stopping_rounds >= iterations
    )
    if not truncated:
        return
    message = (
        f"{context}: best_iteration={best_iteration} + early_stopping_rounds={early_stopping_rounds} "
        f">= the --iterations cap ({iterations}) -- early stopping could not have fired before the cap; "
        "this fit is under-trained. Raise --iterations."
    )
    if fatal:
        raise RuntimeError(message)
    if log is not None:
        log.warning(message)


def get_logger(verbose: bool = False) -> logging.Logger:
    """Return a timestamped stdout logger, independent of ``staging.yaml``."""
    logger = logging.getLogger("hpp_cb")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
    return logger


class _FoldLogAdapter:
    """
    Demote a logger's ``info`` calls to ``debug`` for per-fold fits.

    ``warning`` and ``debug`` pass through, so a multi-fold run is not flooded
    but nothing important is lost.

    Parameters
    ----------
    log : logging.Logger
        The logger to wrap.
    """

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def info(self, msg, *args, **kwargs) -> None:
        """Log `msg` at debug level."""
        self._log.debug(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs) -> None:
        """Log `msg` at warning level."""
        self._log.warning(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs) -> None:
        """Log `msg` at debug level."""
        self._log.debug(msg, *args, **kwargs)


def _fold_log(log: Optional[logging.Logger]) -> Optional[_FoldLogAdapter]:
    """Return a `_FoldLogAdapter` around `log`, or None without one."""
    return _FoldLogAdapter(log) if log is not None else None


DEFAULT_LOAD_CHUNK_ROWS = 250_000

DEFAULT_FEATURE_FLOAT_DTYPE = "float32"

# Undecided columns (NULL across every loaded batch) take their declared view
# type; the dtype helpers themselves are shared with hpp_rf.
_KNOWN_NUMERIC_FALLBACK_COLUMNS = frozenset(
    col for col, kind in VIEW_COLUMN_KINDS.items() if kind == "num"
) | frozenset(NUMERIC_COLUMNS) | frozenset(IOD_NUMERIC_COLUMNS)


def load_dataset(
    engine,
    limit: Optional[int] = None,
    log: Optional[logging.Logger] = None,
    float_dtype: str = DEFAULT_FEATURE_FLOAT_DTYPE,
    chunk_rows: int = DEFAULT_LOAD_CHUNK_ROWS,
    population: str = DEFAULT_POPULATION,
) -> pd.DataFrame:
    """
    Read ``production.london_houses`` in date order into pandas.

    CatBoost consumes host data even on GPU, so there is no cuDF path. Rows are
    streamed in `chunk_rows` batches with per-chunk dtype coercion, falling
    back to a single read when the engine cannot stream. `population` selects
    the modelled sales, as in `hpp_rf.load_dataset`.
    """
    if float_dtype not in ("float32", "float64"):
        raise ValueError(
            f"Unknown float_dtype '{float_dtype}', expected float32/float64"
        )
    sql = dataset_sql(limit, population)

    parts: List[pd.DataFrame] = []
    kinds: Dict[str, Optional[str]] = {}
    try:
        with engine.connect().execution_options(
            stream_results=True, max_row_buffer=chunk_rows
        ) as connection:
            for chunk in pd.read_sql(sql, connection, chunksize=chunk_rows):
                for col in chunk.columns:
                    if kinds.get(col) is None:
                        kinds[col] = _series_value_kind(chunk[col])
                _coerce_chunk_dtypes(chunk, kinds, float_dtype)
                parts.append(chunk)
    except Exception as exc:
        parts = []
        if log is not None:
            log.warning(
                f"load_dataset: streaming read unavailable ({type(exc).__name__}: {exc}); "
                "falling back to a single-shot pd.read_sql (much higher peak memory)"
            )
        df = pd.read_sql(sql, engine)
        kinds = {col: _series_value_kind(df[col]) for col in df.columns}
        for col in _KNOWN_NUMERIC_FALLBACK_COLUMNS:
            if col in kinds and kinds[col] is None:
                kinds[col] = "num"
        _coerce_chunk_dtypes(df, kinds, float_dtype)
    else:
        for col in _KNOWN_NUMERIC_FALLBACK_COLUMNS:
            if col in kinds and kinds[col] is None:
                kinds[col] = "num"
        if not parts:
            df = pd.DataFrame()
        else:
            for chunk in parts:
                _coerce_chunk_dtypes(chunk, kinds, float_dtype)
            df = parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)
        parts.clear()
        gc.collect()

    if log is not None:
        log.info(
            f"Loaded {len(df)} rows from {VIEW_NAME} in database "
            f"{getattr(getattr(engine, 'url', None), 'database', '?')} (population={population}, "
            f"{df.memory_usage(deep=True).sum() / 1e9:.2f} GB in memory, numeric columns as {float_dtype})"
        )
    return df


def _frame_float_dtype(df: pd.DataFrame) -> str:
    """
    Return the float width of `df`'s numeric features, ``"float32"`` or ``"float64"``.

    Columns this module adds match it rather than re-widening the frame;
    ``total_floor_area`` is the probe.
    """
    probe = df["total_floor_area"].dtype if "total_floor_area" in df.columns else None
    return "float32" if probe is not None and probe == np.float32 else "float64"


def load_mortgage_rates(engine, log: Optional[logging.Logger] = None) -> pd.DataFrame:
    """
    Read ``production.mortgage_rates`` ordered by rate date.

    Static reference data, loaded once per run and reused by every split.
    """
    sql = f"SELECT {MORTGAGE_RATE_DATE_COLUMN}, {MORTGAGE_RATE_COLUMN} FROM {MORTGAGE_RATE_TABLE} ORDER BY {MORTGAGE_RATE_DATE_COLUMN}"
    df = pd.read_sql(sql, engine)
    df[MORTGAGE_RATE_DATE_COLUMN] = pd.to_datetime(df[MORTGAGE_RATE_DATE_COLUMN])
    if log is not None:
        log.info(f"Loaded {len(df)} rows from {MORTGAGE_RATE_TABLE}")
    return df


def add_mortgage_rate_features(
    df: pd.DataFrame, mortgage_df: pd.DataFrame, log: Optional[logging.Logger] = None
) -> pd.DataFrame:
    """
    Join the 2-year 75% LTV mortgage rate onto `df` as of each sale date.

    Adds `MORTGAGE_RATE_DEPENDENT_COLUMNS`: the rate at sale, its 3, 6 and 12
    month lags, and its 12-month change.
    """
    out = df.copy(deep=False)
    sale_date = pd.to_datetime(out[DATE_COLUMN])
    rates = mortgage_df[[MORTGAGE_RATE_DATE_COLUMN, MORTGAGE_RATE_COLUMN]].dropna(
        subset=[MORTGAGE_RATE_DATE_COLUMN]
    )
    rates = rates.sort_values(MORTGAGE_RATE_DATE_COLUMN)

    def _as_of(target_dates: pd.Series) -> np.ndarray:
        """Return the latest rate on or before each date."""
        left = pd.DataFrame({"__asof_date": target_dates})
        merged = pd.merge_asof(
            left,
            rates,
            left_on="__asof_date",
            right_on=MORTGAGE_RATE_DATE_COLUMN,
            direction="backward",
        )
        return merged[MORTGAGE_RATE_COLUMN].to_numpy()

    rate_dtype = _frame_float_dtype(df)
    out[MORTGAGE_RATE_AT_SALE_COLUMN] = _as_of(sale_date).astype(rate_dtype)
    lag_values = {}
    for months, col in zip(MORTGAGE_RATE_LAG_MONTHS, MORTGAGE_RATE_LAG_COLUMNS):
        lag_values[months] = _as_of(sale_date - pd.DateOffset(months=months))
        out[col] = lag_values[months].astype(rate_dtype)
    out[MORTGAGE_RATE_CHANGE_COLUMN] = (
        out[MORTGAGE_RATE_AT_SALE_COLUMN] - lag_values[MORTGAGE_RATE_CHANGE_MONTHS]
    ).astype(rate_dtype)

    if log is not None:
        n_missing = int(out[MORTGAGE_RATE_AT_SALE_COLUMN].isna().sum())
        log.info(
            f"Joined mortgage rate features ({MORTGAGE_RATE_COLUMN}) as-of dateoftransfer; "
            f"{n_missing} rows before production.mortgage_rates' first observation"
        )
    return out


_INTEGER_VALUED_CATEGORICAL_COLUMNS = ["iod_release_year"]

_FLOOR_LEVEL_ORDINAL = {
    label: float(i) for i, label in enumerate(FLOOR_LEVEL_CANONICAL)
}

_MISSING_FLAG_GROUPS["floor_level_missing"] = ["floor_level_ord"]


_SALE_YEAR_PROXY_MIN_YEAR_COVERAGE = 0.95

DEFAULT_AVAILABILITY_SHIFT_LIMIT = 0.5

_AVAILABILITY_GUARD_MIN_ROWS = 50

_STRINGIFIED_NULL_CATEGORIES = frozenset({"nan", "<NA>", "None", "NaT", "NA"})

_MISSING_CATEGORICAL_SENTINEL = "UNKNOWN"

# Prefix of the shadow columns `add_canonical_categoricals` adds: each
# categorical already in the string form CatBoost is given.
CANONICAL_CATEGORICAL_PREFIX = "__cat__"


def _canonical_categorical(values: pd.Series, col: str) -> pd.Series:
    """
    Return a categorical column as the strings CatBoost is given, missing values as the sentinel.

    Integer-valued categoricals (`_INTEGER_VALUED_CATEGORICAL_COLUMNS`) are
    rounded first so ``2019.0`` and ``2019`` are one category. Row by row, so
    casting a whole frame once equals casting each fold's slice of it.
    """
    if col in _INTEGER_VALUED_CATEGORICAL_COLUMNS:
        values = pd.to_numeric(values, errors="coerce").round().astype("Int64")
    values = values.astype("object")
    return values.where(values.notna(), _MISSING_CATEGORICAL_SENTINEL).astype("str")


def add_canonical_categoricals(df: pd.DataFrame, columns: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """
    Return `df` with ``__cat__<col>``: every categorical cast to CatBoost's strings once.

    `prepare_features` reads these instead of casting each fold's train, val
    and test frames again (about 15 columns of up to 1.9 M rows, once per
    fold). The raw columns are kept: the availability guard measures their
    missingness. `columns` defaults to every categorical a model may use.
    """
    if columns is None:
        columns = list(CATEGORICAL_COLUMNS) + IOD_CATEGORICAL_COLUMNS + list(OFSTED_CATEGORICAL_COLUMNS)
    added = {
        CANONICAL_CATEGORICAL_PREFIX + col: _canonical_categorical(df[col], col)
        for col in dict.fromkeys(columns)
        if col in df.columns
    }
    if not added:
        return df
    out = df.drop(columns=[c for c in added if c in df.columns])
    return pd.concat([out, pd.DataFrame(added, index=df.index)], axis=1)


def _availability_rate(df: pd.DataFrame, column: str) -> float:
    """
    Return the share of `df[column]` carrying no information.

    NaN for numeric columns, NaN or the "UNKNOWN" sentinel for strings. Uses
    presence only, never values or the target, so it may be measured on
    scored frames.
    """
    values = df[column]
    missing = values.isna()
    if values.dtype == object or isinstance(values.dtype, pd.StringDtype):
        missing = missing | (values == _MISSING_CATEGORICAL_SENTINEL)
    return float(missing.mean()) if len(values) else 0.0


def _drop_unavailable_features(
    train_df: pd.DataFrame,
    scored_frames: Sequence[pd.DataFrame],
    columns: Sequence[str],
    limit: float,
    log: Optional[logging.Logger] = None,
    rates_out: Optional[Dict[str, Dict[str, float]]] = None,
    exempt: Sequence[str] = (),
) -> List[str]:
    """
    Return the columns whose missingness shifts by more than `limit` from train to scored frames.

    `train_df` should be the *trailing* training window (`_availability_window`),
    not the whole train set: a feature 55 % NULL over 1995-2025 only because
    the early sales predate its source is fully present in the latest years,
    and it is the latest years a scored frame resembles. A genuine train/serve
    gap still shows in the trailing window. Columns absent from `train_df` are
    skipped and fail later with a clearer message. Dropped columns are logged in one combined warning, and their
    train and worst scored missing rates written to `rates_out`. Columns in
    `exempt` are never dropped: under ``--population all`` the EPC core
    features are missing on pre-2009 training rows but present where the
    model is scored, which is the shift this guard exists to catch.

    Raises
    ------
    RuntimeError
        If a `CORE_FEATURES` column would be dropped: a model without floor
        area must fail, not report metrics.
    """
    if limit > 1.0:
        return []
    usable = [
        frame for frame in scored_frames if len(frame) >= _AVAILABILITY_GUARD_MIN_ROWS
    ]
    if not usable:
        return []

    dropped: List[str] = []
    details: List[str] = []
    exempt_set = set(exempt)
    for column in columns:
        if column not in train_df.columns or column in exempt_set:
            continue
        train_rate = _availability_rate(train_df, column)
        worst_rate, worst_shift = train_rate, 0.0
        for frame in usable:
            if column not in frame.columns:
                continue
            shift = abs(_availability_rate(frame, column) - train_rate)
            if shift > worst_shift:
                worst_rate, worst_shift = _availability_rate(frame, column), shift
        if worst_shift > limit:
            dropped.append(column)
            details.append(f"{column} ({train_rate:.1%}->{worst_rate:.1%})")
            if rates_out is not None:
                rates_out[column] = {
                    "train_missing_rate": round(train_rate, 4),
                    "scored_missing_rate": round(worst_rate, 4),
                }
    core_dropped = [c for c in dropped if c in CORE_FEATURES]
    if core_dropped:
        raise RuntimeError(
            f"prepare_features: the availability guard would drop core feature(s) {core_dropped} "
            f"(train->scored missingness shift > {limit:.1%}; all shifts: {details}). Train on a "
            "population that carries them -- --population epc_matched (the default) or "
            "--train-from-year -- instead of a model that silently lacks them."
        )
    if dropped and log is not None:
        log.warning(
            f"prepare_features: dropping {len(dropped)} feature(s) not available where the model "
            f"is used (train->scored missingness shift > {limit:.1%}): {details}"
        )
    return dropped


EPC_MISSING_FLAG = "epc_missing"


def _epc_missing_indicator(df: pd.DataFrame) -> np.ndarray:
    """
    Return whether each row lacks a usable certificate, as a bool ndarray.

    True when the floor area or the energy rating is missing (NaN or the
    "UNKNOWN" sentinel). A frame that already carries an ``epc_missing``
    column (``hpp_inference`` sets it for a home priced from an estimated
    certificate) is OR-ed in, so estimated values do not read as a real one.
    """
    out = np.zeros(len(df), dtype=bool)
    if "total_floor_area" in df.columns:
        np.logical_or(out, df["total_floor_area"].isna().to_numpy(), out=out)
    if "current_energy_rating" in df.columns:
        rating = df["current_energy_rating"]
        missing = rating.isna() | (rating.astype("object") == _MISSING_CATEGORICAL_SENTINEL)
        np.logical_or(out, missing.to_numpy(dtype=bool), out=out)
    if EPC_MISSING_FLAG in df.columns:
        explicit = pd.to_numeric(df[EPC_MISSING_FLAG], errors="coerce").fillna(0.0).to_numpy() > 0
        np.logical_or(out, explicit, out=out)
    return out


def resolve_nan_mode(args: argparse.Namespace, log: Optional[logging.Logger] = None) -> str:
    """
    Resolve ``--nan-mode`` when it was not given, store it on `args` and return it.

    ``native`` under ``--population all``: imputing would give pre-2009 rows
    the median floor area with nothing marking it as filled in. ``impute``
    otherwise, the long-standing default.
    """
    if getattr(args, "nan_mode", None) is None:
        population = getattr(args, "population", DEFAULT_POPULATION)
        args.nan_mode = "native" if population == POPULATION_ALL else "impute"
        if log is not None:
            log.info(
                f"--nan-mode not given: using {args.nan_mode!r} for --population {population}"
            )
    return args.nan_mode


def _missing_indicator(df: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    """Return whether any of `columns` is missing per row, as a bool ndarray."""
    out = np.zeros(len(df), dtype=bool)
    for col in columns:
        np.logical_or(out, df[col].isna().to_numpy(), out=out)
    return out


def _is_sale_year_proxy(sale_year: pd.Series, flag: pd.Series) -> bool:
    """
    Return True when `flag` is nearly constant within each sale year.

    Such a missing indicator is a proxy for sale year rather than a feature.
    """
    if flag.nunique(dropna=False) <= 1:
        return False
    per_year = flag.groupby(sale_year)
    per_year_nunique = per_year.nunique()
    if len(per_year_nunique) == 0:
        return False
    years_sorted = sorted(per_year_nunique.index)
    per_year_mean = per_year.mean()

    i = 0
    while i < len(years_sorted) and per_year_mean[years_sorted[i]] >= 1.0:
        i += 1
    had_constant_one_prefix = i > 0
    warmup_end = i
    if had_constant_one_prefix:
        while (
            warmup_end < len(years_sorted)
            and per_year_nunique[years_sorted[warmup_end]] > 1
        ):
            warmup_end += 1

    remaining_years = (
        years_sorted[warmup_end:] if had_constant_one_prefix else years_sorted
    )
    if not remaining_years:
        return True
    remaining_nunique = per_year_nunique.loc[remaining_years]
    return float((remaining_nunique <= 1).mean()) >= _SALE_YEAR_PROXY_MIN_YEAR_COVERAGE


DEFAULT_AVAILABILITY_WINDOW_FRAC = 0.1


def _availability_window(train_df: pd.DataFrame, frac: Optional[float]) -> pd.DataFrame:
    """
    Return the trailing `frac` of date-sorted `train_df` the availability guards measure.

    The same rows `_carve_eval_mask` selects for early stopping; the whole of
    `train_df` when `frac` is None.
    """
    if frac is None or len(train_df) <= 1:
        return train_df
    n_tail = int(_carve_eval_mask(len(train_df), frac=frac).sum())
    return train_df.iloc[len(train_df) - n_tail :]


def prepare_features(
    train_df,
    val_df,
    test_df,
    use_iod: bool = True,
    use_ofsted: bool = True,
    nan_mode: str = "impute",
    drop_features: Optional[Sequence[str]] = None,
    drop_sale_year: bool = False,
    availability_shift_limit: float = DEFAULT_AVAILABILITY_SHIFT_LIMIT,
    log: Optional[logging.Logger] = None,
    population: str = DEFAULT_POPULATION,
    is_final_fit: bool = True,
    availability_window_frac: Optional[float] = DEFAULT_AVAILABILITY_WINDOW_FRAC,
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    List[str],
    Dict[str, float],
    Dict[str, object],
]:
    """
    Build the feature frames CatBoost trains and predicts on.

    Numeric groups are median-filled from train (or left NaN for
    ``nan_mode="native"``) with missing flags; categoricals stay strings.
    Guards drop features whose availability shifts between the trailing
    `availability_window_frac` of train (`_availability_window`) and the
    scored frames, and flags that proxy sale year. The
    `hpp_rf.MARKET_RELATIVE_COLUMNS` a frame carries are numeric features;
    `DEFAULT_DROPPED_COLUMNS` are always dropped.

    Under ``population="all"`` the `hpp_rf.EPC_CORE_FEATURES` are optional
    per training row: the availability guard leaves them alone, an
    ``epc_missing`` indicator (no floor area or rating) is added and exempt
    from the flag guards, and in a cross-validation fold (`is_final_fit`
    False) whose train rows carry none of them they are dropped for that fold
    with reason ``unavailable_in_fold_train``. The final fit still raises.

    Returns
    -------
    X_train, X_val, X_test : pandas.DataFrame
        Feature frames with identical columns.
    cat_features : list of str
        Categorical column names.
    medians : dict
        Fill values.
    feature_metadata : dict
        Including ``feature_columns``, ``missing_flag_groups`` and
        ``dropped_features`` (each dropped feature with its reason and, for the
        availability guard, its train and scored missing rates).

    Raises
    ------
    RuntimeError
        If a guard would drop a `hpp_rf.CORE_FEATURES` column that was not
        dropped on request.
    """
    if nan_mode not in ("impute", "native"):
        raise ValueError(f"Unknown nan_mode '{nan_mode}', expected impute/native")
    if population not in POPULATIONS:
        raise ValueError(f"Unknown population {population!r}, expected one of {POPULATIONS}")
    epc_optional = population == POPULATION_ALL

    cat_features = (
        list(CATEGORICAL_COLUMNS)
        + (IOD_CATEGORICAL_COLUMNS if use_iod else [])
        + (OFSTED_CATEGORICAL_COLUMNS if use_ofsted else [])
    )
    if use_ofsted:
        require_ofsted_columns(train_df.columns)
    # Only when the caller ran hpp_rf.add_market_relative_features.
    market_relative_columns = [
        c for c in list(MARKET_RELATIVE_COLUMNS) + list(ANCHOR_FEATURE_COLUMNS) if c in train_df.columns
    ]
    numeric_dependent_columns = (
        LOT_AREA_DEPENDENT_COLUMNS
        + LOCATION_DEPENDENT_COLUMNS
        + COMP_DEPENDENT_COLUMNS
        + ROLLING_COMP_DEPENDENT_COLUMNS
        + PTAL_DEPENDENT_COLUMNS
        + RIVER_DEPENDENT_COLUMNS
        + PREV_SALE_DEPENDENT_COLUMNS
        + SAME_STREET_DEPENDENT_COLUMNS
        + NEIGHBOURHOOD_DEPENDENT_COLUMNS
        + MSOA_TREND_DEPENDENT_COLUMNS
        + NUMERIC_DEPENDENT_COLUMNS
        + MORTGAGE_RATE_DEPENDENT_COLUMNS
        + (IOD_NUMERIC_COLUMNS if use_iod else [])
        + (IOD_ORDINAL_COLUMNS if use_iod else [])
        + (OFSTED_NUMERIC_COLUMNS if use_ofsted else [])
        + ["floor_level_ord"]
        + market_relative_columns
    )
    numeric_columns = (
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
        + MORTGAGE_RATE_DEPENDENT_COLUMNS
        + (IOD_NUMERIC_COLUMNS if use_iod else [])
        + (IOD_ORDINAL_COLUMNS if use_iod else [])
        + (OFSTED_NUMERIC_COLUMNS if use_ofsted else [])
        + ["floor_level_ord"]
        + market_relative_columns
    )
    missing_flag_groups = dict(_MISSING_FLAG_GROUPS)
    dropped_flags: List[str] = []

    def _prune_missing_flag_groups(
        groups: Dict[str, List[str]], dropped_cols: Sequence[str], reason: str
    ) -> Dict[str, List[str]]:
        """
        Remove `dropped_cols` from every missing-flag group, dropping empty groups.

        Dropped groups are logged in one warning per call.
        """
        dropped_col_set = set(dropped_cols)
        if not dropped_col_set:
            return groups
        pruned: Dict[str, List[str]] = {}
        dropped_this_call: List[str] = []
        for flag_col, dependent_cols in groups.items():
            surviving = [c for c in dependent_cols if c not in dropped_col_set]
            if surviving:
                pruned[flag_col] = surviving
            else:
                dropped_flags.append(flag_col)
                dropped_this_call.append(flag_col)
        if dropped_this_call and log is not None:
            log.warning(
                f"prepare_features: dropping missing flag group(s) {sorted(dropped_this_call)} -- {reason}"
            )
        return pruned

    if not use_iod:
        missing_flag_groups = {
            k: v for k, v in missing_flag_groups.items() if not k.startswith("iod_")
        }
    if not use_ofsted:
        missing_flag_groups.pop(OFSTED_MISSING_FLAG, None)
    if nan_mode == "native":
        missing_flag_groups = {
            k: v
            for k, v in missing_flag_groups.items()
            if k in ("prev_sale_missing", "comp_missing")
        }

    def _with_derived_numeric(df: pd.DataFrame) -> pd.DataFrame:
        """Return `df` with the derived numeric columns added at the frame's float width."""
        float_dtype = _frame_float_dtype(df)
        df = df.copy(deep=False)
        df["floor_level_ord"] = (
            df["floor_level"].map(_FLOOR_LEVEL_ORDINAL).astype(float_dtype)
        )
        if use_iod:
            for col in IOD_ORDINAL_COLUMNS:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float_dtype)
        return df

    train_df = _with_derived_numeric(train_df)
    val_df = _with_derived_numeric(val_df)
    test_df = _with_derived_numeric(test_df)

    orphaned_medians = set(numeric_dependent_columns) - set(numeric_columns)
    if orphaned_medians:
        raise RuntimeError(
            f"prepare_features: column(s) get a train median but are not in numeric_columns "
            f"(would never reach CatBoost): {sorted(orphaned_medians)}"
        )
    for flag_col, dependent_cols in missing_flag_groups.items():
        orphaned_flag_cols = set(dependent_cols) - set(numeric_columns)
        if orphaned_flag_cols:
            raise RuntimeError(
                f"prepare_features: {flag_col}'s dependent column(s) are not in numeric_columns "
                f"(flag would describe a feature CatBoost never sees): {sorted(orphaned_flag_cols)}"
            )

    drop_set = set(DEFAULT_DROPPED_COLUMNS) | (
        set(drop_features) if drop_features else set()
    )
    known_names = (
        set(cat_features)
        | set(numeric_dependent_columns)
        | set(numeric_columns)
        | set(missing_flag_groups)
    )
    unknown = (set(drop_features) if drop_features else set()) - known_names
    if unknown and log is not None:
        log.warning(
            f"--drop-features named column(s) not found in any known feature list, ignored: {sorted(unknown)}"
        )
    cat_features = [c for c in cat_features if c not in drop_set]
    numeric_dependent_columns = [
        c for c in numeric_dependent_columns if c not in drop_set
    ]
    numeric_columns = [c for c in numeric_columns if c not in drop_set]
    missing_flag_groups = {
        flag_col: surviving
        for flag_col, dependent_cols in missing_flag_groups.items()
        if flag_col not in drop_set
        and (surviving := [c for c in dependent_cols if c not in drop_set])
    }

    numeric_columns = [c for c in numeric_columns if c != "sale_month"]
    if drop_sale_year:
        numeric_columns = [c for c in numeric_columns if c != "sale_year"]

    numeric_dependent_columns = list(
        dict.fromkeys(numeric_dependent_columns + numeric_columns)
    )

    dropped_features: Dict[str, Dict[str, object]] = {}
    availability_rates: Dict[str, Dict[str, float]] = {}
    availability_train = _availability_window(train_df, availability_window_frac)
    n_availability_tail = len(availability_train)
    unavailable = _drop_unavailable_features(
        availability_train,
        [val_df, test_df],
        list(cat_features) + list(numeric_columns),
        availability_shift_limit,
        log=log,
        rates_out=availability_rates,
        exempt=EPC_CORE_FEATURES if epc_optional else (),
    )
    for column, column_rates in availability_rates.items():
        dropped_features[column] = {"reason": "availability_shift", **column_rates}
    if unavailable:
        unavailable_set = set(unavailable)
        cat_features = [c for c in cat_features if c not in unavailable_set]
        numeric_columns = [c for c in numeric_columns if c not in unavailable_set]
        numeric_dependent_columns = [
            c for c in numeric_dependent_columns if c not in unavailable_set
        ]
        missing_flag_groups = _prune_missing_flag_groups(
            missing_flag_groups,
            unavailable,
            "every dependent column was dropped by the train/serve availability guard",
        )

    medians: Dict[str, float] = {}
    if nan_mode == "impute":
        medians = {
            col: float(train_df[col].median()) for col in numeric_dependent_columns
        }
        nan_median_cols = sorted(c for c, m in medians.items() if pd.isna(m))
        if nan_median_cols:
            if log is not None:
                log.warning(
                    f"prepare_features: all-NaN train median for {len(nan_median_cols)} column(s) this fit -- "
                    f"dropping rather than passing raw NaN through: {nan_median_cols}"
                )
            medians = {c: m for c, m in medians.items() if c not in nan_median_cols}
            for column in nan_median_cols:
                dropped_features[column] = {"reason": "all_nan_in_train"}
            numeric_columns = [c for c in numeric_columns if c not in nan_median_cols]
            missing_flag_groups = _prune_missing_flag_groups(
                missing_flag_groups,
                nan_median_cols,
                "every dependent column had an all-NaN train median this fit",
            )

    train_missing_indicators = {
        name: _missing_indicator(train_df, cols)
        for name, cols in missing_flag_groups.items()
    }
    zero_variance_flags = sorted(
        name
        for name, mask in train_missing_indicators.items()
        if not mask.any() or mask.all()
    )
    if zero_variance_flags:
        if log is not None:
            log.warning(
                f"prepare_features: dropping zero-train-variance missing flag(s): {zero_variance_flags}"
            )
        dropped_flags.extend(zero_variance_flags)
        missing_flag_groups = {
            k: v for k, v in missing_flag_groups.items() if k not in zero_variance_flags
        }

    sale_year_proxy_flags = sorted(
        name
        for name in missing_flag_groups
        if _is_sale_year_proxy(
            train_df["sale_year"],
            pd.Series(train_missing_indicators[name], index=train_df.index),
        )
    )
    if sale_year_proxy_flags:
        if log is not None:
            log.warning(
                f"prepare_features: dropping sale-year-proxy missing flag(s): {sale_year_proxy_flags}"
            )
        dropped_flags.extend(sale_year_proxy_flags)
        missing_flag_groups = {
            k: v
            for k, v in missing_flag_groups.items()
            if k not in sale_year_proxy_flags
        }

    availability_shift_flags: List[str] = []
    availability_shift_details: List[str] = []
    if availability_shift_limit <= 1.0:
        usable_frames = [
            f for f in (val_df, test_df) if len(f) >= _AVAILABILITY_GUARD_MIN_ROWS
        ]
        for name, cols in missing_flag_groups.items():
            train_rate = float(train_missing_indicators[name][-n_availability_tail:].mean())
            for frame in usable_frames:
                scored_rate = float(_missing_indicator(frame, cols).mean())
                if abs(scored_rate - train_rate) > availability_shift_limit:
                    availability_shift_flags.append(name)
                    availability_shift_details.append(
                        f"{name} ({train_rate:.1%}->{scored_rate:.1%})"
                    )
                    break
    if availability_shift_flags:
        if log is not None:
            log.warning(
                f"prepare_features: dropping {len(availability_shift_flags)} missing flag(s) -- "
                f"train/serve indicator(s), not a feature (shift > {availability_shift_limit:.1%}): "
                f"{availability_shift_details}"
            )
        dropped_flags.extend(availability_shift_flags)
        missing_flag_groups = {
            k: v
            for k, v in missing_flag_groups.items()
            if k not in set(availability_shift_flags)
        }

    zero_variance_numeric = sorted(
        col for col in numeric_columns if train_df[col].nunique(dropna=True) <= 1
    )
    if zero_variance_numeric:
        if log is not None:
            log.warning(
                f"prepare_features: dropping zero-train-variance numeric feature(s): {zero_variance_numeric}"
            )
        numeric_columns = [c for c in numeric_columns if c not in zero_variance_numeric]
        for column in zero_variance_numeric:
            dropped_features[column] = {"reason": "zero_train_variance"}
        missing_flag_groups = _prune_missing_flag_groups(
            missing_flag_groups,
            zero_variance_numeric,
            "every dependent column had zero train variance",
        )

    add_epc_missing = epc_optional and EPC_MISSING_FLAG not in drop_set
    if add_epc_missing:
        train_epc_missing = _epc_missing_indicator(train_df)
        if not train_epc_missing.any() or train_epc_missing.all():
            if log is not None:
                log.warning(
                    f"prepare_features: dropping zero-train-variance {EPC_MISSING_FLAG} flag "
                    f"(missing on {train_epc_missing.mean():.1%} of train rows)"
                )
            dropped_flags.append(EPC_MISSING_FLAG)
            add_epc_missing = False
        del train_epc_missing

    core_lost = sorted(
        c for c in dropped_features if c in CORE_FEATURES and c not in drop_set
    )
    if epc_optional and not is_final_fit:
        fold_unavailable = [c for c in core_lost if c in EPC_CORE_FEATURES]
        for column in fold_unavailable:
            dropped_features[column] = {
                "reason": "unavailable_in_fold_train",
                "guard": dropped_features[column]["reason"],
            }
        if fold_unavailable and log is not None:
            log.warning(
                f"prepare_features: EPC core feature(s) {fold_unavailable} carry no information in "
                "this cross-validation fold's train rows (population=all, pre-certificate sales); "
                "dropped for this fold only"
            )
        core_lost = [c for c in core_lost if c not in EPC_CORE_FEATURES]
    if core_lost:
        raise RuntimeError(
            f"prepare_features: core feature(s) {core_lost} were dropped by a runtime guard "
            f"({ {c: dropped_features[c]['reason'] for c in core_lost} }); a model without them "
            "must not be trained and reported"
        )

    for flag_col, dependent_cols in missing_flag_groups.items():
        orphaned_flag_cols = set(dependent_cols) - set(numeric_columns)
        if orphaned_flag_cols:
            raise RuntimeError(
                f"prepare_features: {flag_col}'s dependent column(s) are not in numeric_columns after the "
                f"runtime guards ran (flag would describe a feature CatBoost never sees): {sorted(orphaned_flag_cols)}"
            )

    def _build(df: pd.DataFrame) -> pd.DataFrame:
        """
        Assemble one split's feature frame in a single DataFrame construction.

        Column order is categoricals, numerics, sale-month sine and cosine, then
        surviving missing flags, at the frame's float width.
        """
        float_dtype = _frame_float_dtype(df)
        data: Dict[str, object] = {}
        for col in cat_features:
            canonical = CANONICAL_CATEGORICAL_PREFIX + col
            # Cast once for the whole frame by `add_canonical_categoricals` when it ran.
            data[col] = df[canonical] if canonical in df.columns else _canonical_categorical(df[col], col)
        for col in numeric_columns:
            values = df[col]
            if nan_mode == "impute" and col in medians:
                values = values.fillna(medians[col])
            data[col] = values.astype(float_dtype)
        month = df["sale_month"].astype(float)
        data["sale_month_sin"] = np.sin(2.0 * np.pi * month / 12.0).astype(float_dtype)
        data["sale_month_cos"] = np.cos(2.0 * np.pi * month / 12.0).astype(float_dtype)
        for flag_col, dependent_cols in missing_flag_groups.items():
            data[flag_col] = _missing_indicator(df, dependent_cols).astype(float_dtype)
        if add_epc_missing:
            data[EPC_MISSING_FLAG] = _epc_missing_indicator(df).astype(float_dtype)
        if len(data) != len(cat_features) + len(numeric_columns) + 2 + len(
            missing_flag_groups
        ) + int(add_epc_missing):
            raise RuntimeError(
                "prepare_features._build: duplicate column name across cat_features/numeric_columns/"
                "missing_flag_groups -- the dict-built frame would silently collapse it"
            )
        out = pd.DataFrame(data, index=df.index)
        del data

        stringified_nulls = sorted(
            col
            for col in cat_feature_names
            if col in out.columns and out[col].isin(_STRINGIFIED_NULL_CATEGORIES).any()
        )
        if stringified_nulls:
            raise RuntimeError(
                f"prepare_features._build: column(s) emitted a stringified null category instead of "
                f"'{_MISSING_CATEGORICAL_SENTINEL}': {stringified_nulls}"
            )

        n_nan = int(
            sum(int(out[c].isna().sum()) for c in out.columns)
        )
        if n_nan and nan_mode == "impute":
            offenders = sorted(c for c in out.columns if int(out[c].isna().sum()))
            raise RuntimeError(
                f"prepare_features: {n_nan} NaN value(s) remain after imputation (nan_mode=impute) "
                f"in column(s) {offenders} -- every numeric feature must be train-median filled and "
                f"every categorical sentinel-filled under this nan_mode. Use --nan-mode native to hand "
                f"raw NaN to CatBoost deliberately."
            )
        if n_nan and log is not None:
            log.debug(
                f"prepare_features: {n_nan} NaN value(s) remain (nan_mode=native, expected)"
            )
        return out

    cat_feature_names = list(cat_features)
    train_features = _build(train_df)
    del train_missing_indicators
    gc.collect()
    val_features = _build(val_df)
    test_features = _build(test_df)
    feature_metadata = {
        "feature_columns": list(train_features.columns),
        "dropped_flags": sorted(set(dropped_flags)),
        "dropped_features": dropped_features,
        "missing_flag_groups": {k: list(v) for k, v in missing_flag_groups.items()},
        "epc_missing_flag": add_epc_missing,
    }
    return (
        train_features,
        val_features,
        test_features,
        cat_feature_names,
        medians,
        feature_metadata,
    )


_TASK_TYPE_CACHE: Dict[str, Tuple[bool, Optional[HostResources], Optional[str]]] = {}


def resolve_task_type(
    device: str, log: Optional[logging.Logger] = None
) -> Tuple[bool, Optional[HostResources], Optional[str]]:
    """
    Decide once whether this run trains on GPU or CPU, cached per device string.

    Only a CUDA GPU is required, since CatBoost ships its own GPU trainer.

    Returns
    -------
    use_gpu : bool
        Whether to use the GPU.
    resources : HostResources or None
        The detected host.
    reason : str or None
        Why the run fell back to CPU.
    """
    device = device.lower()
    if device not in ("cpu", "gpu", "auto"):
        raise ValueError(f"Unknown device '{device}', expected cpu/gpu/auto")

    if device in _TASK_TYPE_CACHE:
        return _TASK_TYPE_CACHE[device]

    if device == "cpu":
        result = (False, None, "requested")
        _TASK_TYPE_CACHE[device] = result
        return result

    resources = detect_host_resources(log)
    if not resources.has_cuda_gpu:
        if device == "gpu":
            raise RuntimeError(
                "--device gpu requested but no CUDA-capable GPU was detected"
            )
        result = (False, resources, "no CUDA GPU detected")
    else:
        result = (True, resources, None)
    _TASK_TYPE_CACHE[device] = result
    return result


UKHPI_DEFLATOR_COLUMN = "ukhpi_avg_price_at_sale"

# Targets that are log price less the log of a per-row reference, and the
# column that reference is read from. ``deflated`` divides by the market (the
# UK HPI at the sale); ``anchored`` by the home's own anchor value
# (`hpp_rf.ANCHOR_COLUMN`: its size-matched comparables' value, with fallbacks),
# so the trees learn a bounded, scale-free correction while the level comes
# from the anchor, which scales linearly -- a tree cannot extrapolate past the
# targets it was trained on, which is what capped the dearest homes.
DEFLATOR_COLUMN_BY_TARGET = {"deflated": UKHPI_DEFLATOR_COLUMN, "anchored": ANCHOR_COLUMN}


def target_deflator(df: pd.DataFrame, target: str) -> Optional[np.ndarray]:
    """Return the per-row reference `target` is relative to, or None for a target without one."""
    column = DEFLATOR_COLUMN_BY_TARGET.get(target)
    if column is None:
        return None
    if column not in df.columns:
        raise ValueError(
            f"--target {target} needs the {column} column; it is added by hpp_rf.add_market_relative_features "
            "(drop --no-market-features)"
        )
    return pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=np.float64)


def _target_transform(
    y_raw: np.ndarray, deflator: Optional[np.ndarray], target: str
) -> np.ndarray:
    """
    Transform raw prices into the training target.

    ``"price"`` is raw pounds, ``"log_price"`` is ``log1p(price)``, and
    ``"deflated"`` subtracts the log of a market deflator so the model learns
    value relative to the market at the sale date, which trees can extrapolate;
    ``"anchored"`` does the same with the home's anchor value as the deflator
    (`DEFLATOR_COLUMN_BY_TARGET`).
    """
    if target == "price":
        return y_raw
    if target == "log_price":
        return np.log1p(y_raw)
    if target in DEFLATOR_COLUMN_BY_TARGET:
        if deflator is None:
            raise ValueError(f"target={target!r} requires a deflator array")
        safe_deflator = np.maximum(np.asarray(deflator, dtype=np.float64), 1e-6)
        return np.log1p(y_raw) - np.log(safe_deflator)
    raise ValueError(f"Unknown target '{target}', expected price/log_price/deflated/anchored")


def _target_inverse_transform(
    y_pred: np.ndarray, deflator: Optional[np.ndarray], target: str
) -> np.ndarray:
    """Invert `_target_transform`; applied before smearing correction."""
    if target == "price":
        return y_pred
    if target == "log_price":
        return np.expm1(y_pred)
    if target in DEFLATOR_COLUMN_BY_TARGET:
        if deflator is None:
            raise ValueError(f"target={target!r} requires a deflator array")
        safe_deflator = np.maximum(np.asarray(deflator, dtype=np.float64), 1e-6)
        return np.expm1(y_pred + np.log(safe_deflator))
    raise ValueError(f"Unknown target '{target}', expected price/log_price/deflated/anchored")


def _drop_rows_with_unusable_target(
    df: pd.DataFrame,
    args: argparse.Namespace,
    log: Optional[logging.Logger],
    context: str,
) -> pd.DataFrame:
    """Drop rows whose target cannot be transformed to a finite value under ``args.target``."""
    unusable = df[TARGET_COLUMN].isna()
    deflator_column = DEFLATOR_COLUMN_BY_TARGET.get(args.target)
    if deflator_column is not None:
        unusable = unusable | df[deflator_column].isna()
    n_unusable = int(unusable.sum())
    if n_unusable:
        if log is not None:
            log.warning(
                f"{context}: dropping {n_unusable} row(s) with an unusable target under "
                f"--target {args.target} (NULL {TARGET_COLUMN} and/or NULL "
                f"{deflator_column}) -- CatBoost cannot train or score on a NaN target"
            )
        df = df.loc[~unusable]
    return df


PLAUSIBILITY_FLOOR_AREA_COLUMN = "total_floor_area"

PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD = 45_000.0


def _implausible_price_per_sqm_mask(df: pd.DataFrame) -> np.ndarray:
    """
    Return True for rows above `PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD`.

    Rows without a positive floor area are never flagged.
    """
    floor_area = pd.to_numeric(
        df[PLAUSIBILITY_FLOOR_AREA_COLUMN], errors="coerce"
    ).to_numpy(dtype=np.float64)
    price = df[TARGET_COLUMN].to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        price_per_sqm = np.where(floor_area > 0, price / floor_area, np.nan)
    return price_per_sqm > PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD


# A sale below this share of what its comparables imply is a non-market or
# partial transfer (a lease extension, a shared-ownership tranche, a transfer
# between relatives): 584 such holdout sales carried 11.5 % of all APE at a
# 164 % MAPE.
PLAUSIBILITY_MIN_PPS_RATIO = 0.35

# Sales at or above this price are outside the scope the targets are judged on
# (48 holdout sales carried most of the squared error).
SCOPED_MAX_PRICE = 10_000_000.0

# First production.address_table id of the 2024+ back-fill (migration 0017):
# its homes' earlier sales were never loaded, and 28 % of their streets are in
# the certificate register's mixed case.
BACKFILL_ADDRESS_ID_MIN = 68_000_000


def _implausible_low_price_mask(df: pd.DataFrame) -> np.ndarray:
    """
    Return True for rows priced below `PLAUSIBILITY_MIN_PPS_RATIO` of what their comparables imply.

    The implied value is ``comp_median_price_per_sqm * total_floor_area``,
    falling back to ``comp_median_price`` where either is missing. Rows with
    neither are never flagged. Uses only the comparables (strictly earlier
    sales) and the row's own price, so it is a data-quality filter, not a
    feature.
    """
    price = df[TARGET_COLUMN].to_numpy(dtype=np.float64)

    def _col(name: str) -> np.ndarray:
        if name not in df.columns:
            return np.full(len(df), np.nan)
        return pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=np.float64)

    area = _col(PLAUSIBILITY_FLOOR_AREA_COLUMN)
    implied = _col("comp_median_price_per_sqm") * np.where(area > 0, area, np.nan)
    implied = np.where(np.isfinite(implied) & (implied > 0), implied, _col("comp_median_price"))
    with np.errstate(invalid="ignore"):
        return np.isfinite(implied) & (price < PLAUSIBILITY_MIN_PPS_RATIO * implied)


def _scoped_exclusion_mask(df: pd.DataFrame) -> np.ndarray:
    """
    Return True for rows outside the scoped metric set.

    Implausibly low (`_implausible_low_price_mask`) or high
    (`_implausible_price_per_sqm_mask`) £/m², or priced at or above
    `SCOPED_MAX_PRICE`.
    """
    price = df[TARGET_COLUMN].to_numpy(dtype=np.float64)
    return (
        _implausible_low_price_mask(df)
        | _implausible_price_per_sqm_mask(df)
        | (price >= SCOPED_MAX_PRICE)
    )


def _segment_masks(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    """
    Return the `by_segment` report's row masks, by segment name.

    The 2024+ back-filled addresses and the rest, with and without a previous
    sale, without a floor area, and each property type.
    """
    n = len(df)
    masks: Dict[str, np.ndarray] = {}
    if "address_id" in df.columns:
        address_id = pd.to_numeric(df["address_id"], errors="coerce").to_numpy(dtype=np.float64)
        backfilled = address_id >= BACKFILL_ADDRESS_ID_MIN
        masks["backfilled_address"] = backfilled
        masks["original_address"] = ~backfilled
    if "prev_sale_price" in df.columns:
        has_prev = pd.to_numeric(df["prev_sale_price"], errors="coerce").notna().to_numpy()
        masks["has_prev_sale"] = has_prev
        masks["no_prev_sale"] = ~has_prev
    if PLAUSIBILITY_FLOOR_AREA_COLUMN in df.columns:
        area = pd.to_numeric(df[PLAUSIBILITY_FLOOR_AREA_COLUMN], errors="coerce").to_numpy(dtype=np.float64)
        masks["no_floor_area"] = ~(area > 0)
    if "propertytype" in df.columns:
        types = df["propertytype"].astype("object").to_numpy()
        for code in sorted({t for t in types if isinstance(t, str)}):
            masks[f"propertytype_{code}"] = types == code
    return {name: np.asarray(mask, dtype=bool).reshape(n) for name, mask in masks.items()}


def _drop_implausible_training_rows(
    df: pd.DataFrame, log: Optional[logging.Logger], context: str
) -> pd.DataFrame:
    """
    Drop training rows outside the plausible £/m² at either end.

    Above `PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD` (D3: bulk transfers recorded
    against one title) and below `PLAUSIBILITY_MIN_PPS_RATIO` of the
    comparables (`_implausible_low_price_mask`). Always applied; each
    filter's count is logged.
    """
    too_high = _implausible_price_per_sqm_mask(df)
    too_low = _implausible_low_price_mask(df)
    implausible = too_high | too_low
    n_high, n_low = int(too_high.sum()), int(too_low.sum())
    if log is not None:
        log.info(
            f"{context}: plausibility filters -- {n_high} training row(s) above "
            f"£{PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD:,.0f}/m² (D3: bulk/portfolio transfers "
            f"recorded against a single title), {n_low} below {PLAUSIBILITY_MIN_PPS_RATIO:.0%} of "
            f"their comparables' implied value (non-market or partial transfers); "
            f"{int(implausible.sum())} of {len(df)} dropped"
        )
    if implausible.any():
        df = df.loc[~implausible]
    return df


def _duan_smearing_factors(
    bucket_preds_raw: np.ndarray, residuals_log: np.ndarray
) -> Dict[str, float]:
    """
    Return Duan smearing factors ``mean(exp(residual))`` per price band.

    Per band rather than global, because the log-scale bias is largest in the
    £1m+ bands.
    """
    bands = np.asarray(price_band(bucket_preds_raw))
    factors: Dict[str, float] = {}
    for label in PRICE_BAND_LABELS:
        mask = bands == label
        factors[label] = (
            float(np.mean(np.exp(residuals_log[mask]))) if mask.any() else 1.0
        )
    return factors


def _price_band_log_centers() -> np.ndarray:
    """
    Return one log-price anchor per price band, the log midpoint of its edges.

    Open-ended bands get a synthetic anchor beyond their finite edge for
    interpolation.
    """
    edges = np.asarray(PRICE_BAND_EDGES, dtype=np.float64)
    centers = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        if lo <= 0:
            lo = hi / 4.0
        if np.isinf(hi):
            hi = lo * 4.0
        centers.append(np.sqrt(lo * hi))
    return np.log(np.asarray(centers))


def _apply_smearing(preds_raw: np.ndarray, factors: Dict[str, float]) -> np.ndarray:
    """Multiply predictions by a smearing factor interpolated at each predicted price."""
    factor_values = np.array(
        [factors.get(label, 1.0) for label in PRICE_BAND_LABELS], dtype=np.float64
    )
    log_centers = _price_band_log_centers()
    log_preds = np.log(np.maximum(np.asarray(preds_raw, dtype=np.float64), 1.0))
    multiplier = np.interp(log_preds, log_centers, factor_values)
    return preds_raw * multiplier


def _bias_diagnostics(
    y_true: np.ndarray, y_pred: np.ndarray
) -> Dict[str, Optional[float]]:
    """Return signed mean and median error and the median prediction-to-actual ratio."""
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    if len(y_true) == 0:
        return {
            "signed_mean_error": None,
            "signed_median_error": None,
            "pred_actual_ratio_median": None,
        }
    error = y_pred - y_true
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(y_true != 0, y_pred / y_true, np.nan)
    return {
        "signed_mean_error": float(np.mean(error)),
        "signed_median_error": float(np.median(error)),
        "pred_actual_ratio_median": float(np.nanmedian(ratio))
        if np.isfinite(ratio).any()
        else None,
    }


def _relative_medae(y_true: np.ndarray, medae: float) -> float:
    """Return median absolute error as a fraction of the split's median price."""
    y_true = np.asarray(y_true, dtype=np.float64)
    median_true = float(np.median(y_true)) if len(y_true) else 0.0
    return float(medae / max(median_true, 1e-6))


WITHIN_TOLERANCES = (0.05, 0.10, 0.20)


def _percentage_error_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, Optional[float]]:
    """Return MdAPE and the share of predictions within 5, 10 and 20 % of the price."""
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ape = np.abs(y_pred - y_true) / np.abs(y_true)
    ape = ape[np.isfinite(ape)]
    out: Dict[str, Optional[float]] = {"mdape": float(np.median(ape)) if ape.size else None}
    for tolerance in WITHIN_TOLERANCES:
        out[f"within_{int(round(tolerance * 100))}pct"] = (
            float(np.mean(ape <= tolerance)) if ape.size else None
        )
    return out


def _metrics_with_bias(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, float]:
    """Return `compute_metrics` plus bias diagnostics, relative median error, MdAPE and within-X% shares."""
    metrics = compute_metrics(y_true, y_pred)
    metrics.update(_bias_diagnostics(y_true, y_pred))
    metrics["medae_relative"] = _relative_medae(y_true, metrics["medae"])
    metrics.update(_percentage_error_metrics(y_true, y_pred))
    metrics["n"] = int(len(y_true))
    return metrics


# Fewer rows than this and a segment's metrics are not computed (R² and RMSE
# of a handful of sales mean nothing).
MIN_SEGMENT_ROWS = 20


def _metrics_on_mask(y_true: np.ndarray, y_pred: np.ndarray, mask: np.ndarray) -> Dict[str, object]:
    """Return `_metrics_with_bias` over `mask`'s rows, or just ``n`` below `MIN_SEGMENT_ROWS`."""
    n = int(mask.sum())
    if n < MIN_SEGMENT_ROWS:
        return {"n": n}
    return _metrics_with_bias(y_true[mask], y_pred[mask])


def _format_headline(metrics: Dict[str, object]) -> str:
    """Return one log line's worth of a metrics dict."""
    parts = [
        f"n={metrics.get('n', '?')}",
        f"MAE=£{metrics['mae']:,.0f}",
        f"RMSE=£{metrics['rmse']:,.0f}",
        f"R2={metrics['r2']:.3f}",
        f"MAPE={metrics['mape']:.1%}",
    ]
    if metrics.get("mdape") is not None:
        parts.append(f"MdAPE={metrics['mdape']:.1%}")
    if metrics.get("within_10pct") is not None:
        parts.append(f"within10%={metrics['within_10pct']:.1%}")
    return " ".join(parts)


# The £2m+ band split by actual price. Grouped this way the top end's bias
# shows -- the model under-prices the dearest homes more the dearer they are --
# where one £2m+ band averages it away.
LUXURY_PRICE_BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("£2-3m", 2_000_000.0, 3_000_000.0),
    ("£3-5m", 3_000_000.0, 5_000_000.0),
    ("£5m+", 5_000_000.0, np.inf),
)


def _log_ratio_summary(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, Optional[float]]:
    """Return the median, p10 and p90 of ``log(actual / predicted)`` and the MdAPE, None when empty."""
    with np.errstate(divide="ignore", invalid="ignore"):
        log_ratio = np.log(y_true / y_pred)
    log_ratio = log_ratio[np.isfinite(log_ratio)]
    if not log_ratio.size:
        return {"median_log_ratio": None, "log_ratio_p10": None, "log_ratio_p90": None, "mdape": None}
    return {
        "median_log_ratio": float(np.median(log_ratio)),
        "log_ratio_p10": float(np.quantile(log_ratio, 0.10)),
        "log_ratio_p90": float(np.quantile(log_ratio, 0.90)),
        "mdape": _percentage_error_metrics(y_true, y_pred)["mdape"],
    }


def _metrics_by_price_band_with_bias(
    y_true: np.ndarray, y_pred: np.ndarray
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Return price-band metrics with bias diagnostics per band of the actual price.

    The `PRICE_BAND_LABELS` bands, then `LUXURY_PRICE_BANDS` splitting £2m+,
    each with the median and 10-90 spread of ``log(actual / predicted)`` and
    the MdAPE beside the pound errors.
    """
    report = compute_metrics_by_price_band(y_true, y_pred)
    y_true_arr = np.asarray(y_true, dtype=float)
    y_pred_arr = np.asarray(y_pred, dtype=float)
    bands = price_band(y_true_arr)
    for label in PRICE_BAND_LABELS:
        mask = np.asarray(bands == label)
        report[label].update(_bias_diagnostics(y_true_arr[mask], y_pred_arr[mask]))
        report[label].update(_log_ratio_summary(y_true_arr[mask], y_pred_arr[mask]))
    for label, low, high in LUXURY_PRICE_BANDS:
        mask = (y_true_arr >= low) & (y_true_arr < high)
        block: Dict[str, Optional[float]] = {"n": int(mask.sum())}
        if mask.any():
            block.update(compute_metrics(y_true_arr[mask], y_pred_arr[mask]))
            block.pop("r2", None)
        block.update(_bias_diagnostics(y_true_arr[mask], y_pred_arr[mask]))
        block.update(_log_ratio_summary(y_true_arr[mask], y_pred_arr[mask]))
        report[label] = block
    return report


def _carve_eval_mask(n: int, frac: float = 0.1, min_rows: int = 50) -> np.ndarray:
    """
    Return a mask selecting the trailing `frac` of date-sorted rows for early stopping.

    At least `min_rows` rows, leaving one to train on. The early-stopping set
    must come from train, never from a reported split.
    """
    if n <= 1:
        return np.zeros(n, dtype=bool)
    n_eval = min(max(min_rows, int(round(n * frac))), n - 1)
    mask = np.zeros(n, dtype=bool)
    mask[n - n_eval :] = True
    return mask


_PRICE_STAT_INPUT_COLUMNS = [
    "postcode_district",
    "postcode_sector",
    TARGET_COLUMN,
    "total_floor_area",
]

_PRICE_STAT_OUTPUT_COLUMNS = list(POSTCODE_STAT_COLUMNS) + list(SECTOR_STAT_COLUMNS)

# The scale of the postcode and sector statistics a model was trained with,
# recorded in its sidecar: prices relative to the UK HPI at each sale.
GROUP_STATS_SCALE_DEFLATED = "deflated"
GROUP_STATS_SCALE_NOMINAL = "nominal"


def _add_price_stat_columns(
    train_df,
    val_df,
    test_df,
    random_state: int,
    log: Optional[logging.Logger] = None,
    deflate: bool = True,
) -> Tuple:
    """
    Add postcode and sector price statistics using narrow projections of the frames.

    With `deflate` (the default) the statistics are of price and £/m²
    relative to ``ukhpi_avg_price_at_sale`` (`hpp_rf._add_group_price_stats`);
    rows without it do not contribute. The returned globals are the fallbacks
    for unseen groups, on the same scale, with the scale under ``"scale"``.
    """
    deflator_col = UKHPI_DEFLATOR_COLUMN if deflate else None
    global_median_price, global_median_pps = group_price_fallbacks(train_df, deflator_col)
    price_stat_globals = {
        "median_price": global_median_price,
        "median_pps": global_median_pps,
        "scale": GROUP_STATS_SCALE_DEFLATED if deflate else GROUP_STATS_SCALE_NOMINAL,
    }

    columns = _PRICE_STAT_INPUT_COLUMNS + ([UKHPI_DEFLATOR_COLUMN] if deflate else [])
    narrow = [df[columns] for df in (train_df, val_df, test_df)]
    narrow[0], narrow[1], narrow[2], postcode_stats = add_postcode_price_stats(
        narrow[0], narrow[1], narrow[2], random_state=random_state, log=log,
        deflator_col=deflator_col,
    )
    narrow[0], narrow[1], narrow[2], sector_stats = add_postcode_sector_price_stats(
        narrow[0], narrow[1], narrow[2], random_state=random_state, log=log,
        deflator_col=deflator_col,
    )
    out = []
    for original, stats_frame in zip((train_df, val_df, test_df), narrow):
        stats_frame = stats_frame.reindex(original.index)
        merged = original.copy(deep=False)
        for col in _PRICE_STAT_OUTPUT_COLUMNS:
            merged[col] = stats_frame[col].to_numpy()
        out.append(merged)
    del narrow
    gc.collect()
    return out[0], out[1], out[2], postcode_stats, sector_stats, price_stat_globals


PREDICT_BATCH_ROWS = 250_000


def _predict_in_batches(model, X: pd.DataFrame, cat_features: List[str]) -> np.ndarray:
    """Predict over `X` in `PREDICT_BATCH_ROWS`-row batches."""
    from catboost import Pool

    if len(X) <= PREDICT_BATCH_ROWS:
        return np.asarray(model.predict(Pool(X, cat_features=cat_features)))
    chunks = []
    for start in range(0, len(X), PREDICT_BATCH_ROWS):
        batch = Pool(
            X.iloc[start : start + PREDICT_BATCH_ROWS], cat_features=cat_features
        )
        chunks.append(np.asarray(model.predict(batch)))
        del batch
    return np.concatenate(chunks)


def _catboost_kwargs(
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
    warm_start: bool = False,
) -> Tuple[Dict, str]:
    """
    Return shared CatBoostRegressor keyword arguments and a backend label.

    Wires ``--nan-mode``, ``--loss-function``, ``--huber-delta``,
    ``--max-ctr-complexity``, ``--one-hot-max-size`` and ``--device``
    identically for every model this module builds. Early stopping is left to
    callers. `warm_start` only changes the backend label, because GPU
    warm starts use a baseline Pool rather than ``init_model``.
    """
    plan = resource_plan(args, log)
    loss_function = args.loss_function
    if loss_function == "Huber":
        loss_function = f"Huber:delta={args.huber_delta}"
    cb_kwargs = dict(
        iterations=args.iterations,
        learning_rate=args.learning_rate or None,
        depth=args.depth,
        l2_leaf_reg=args.l2_leaf_reg,
        loss_function=loss_function,
        random_seed=args.random_state,
        verbose=False,
        has_time=True,
        allow_writing_files=False,
    )
    if args.nan_mode == "native":
        cb_kwargs["nan_mode"] = "Min"
    if args.max_ctr_complexity is not None:
        cb_kwargs["max_ctr_complexity"] = args.max_ctr_complexity
    if args.one_hot_max_size is not None:
        cb_kwargs["one_hot_max_size"] = args.one_hot_max_size
    cb_kwargs.update(plan.catboost_params())
    if plan.use_gpu:
        backend = f"CatBoost (gpu: devices {plan.devices}, {', '.join(plan.gpu_names)})".strip()
        if warm_start:
            backend += ", warm-started via baseline+sum_models"
    else:
        backend = f"CatBoost (cpu, {plan.thread_count} threads, {plan.reason})"
    return cb_kwargs, backend


def _plan_overrides(args: argparse.Namespace) -> Dict[str, object]:
    """Return the resource-plan fields the CLI set (`hardware.PLAN_OVERRIDE_FIELDS`)."""
    concurrency = getattr(args, "fold_concurrency", None)
    return {
        "devices": getattr(args, "devices", None),
        "gpu_ram_part": getattr(args, "gpu_ram_part", None),
        "pinned_memory_size": getattr(args, "pinned_memory_size", None),
        "thread_count": getattr(args, "thread_count", None),
        "used_ram_limit": getattr(args, "used_ram_limit", None),
        "fold_concurrency": None if concurrency in (None, "auto") else int(concurrency),
        "chunk_rows": getattr(args, "chunk_rows", None),
    }


def resource_plan(
    args: argparse.Namespace, log: Optional[logging.Logger] = None, fold_memory_mib: Optional[int] = None
) -> "hardware.ResourcePlan":
    """
    Return the run's `hardware.ResourcePlan`, derived once and kept on `args`.

    The trainer comes from `resolve_task_type` (``--device``), and
    ``HPLDN_DISABLE_GPU=1`` turns ``--device auto`` into CPU, so one command
    runs on a GPU box and a CPU-only one. ``--devices``, ``--gpu-ram-part``,
    ``--pinned-memory-size``, ``--thread-count``, ``--used-ram-limit``,
    ``--fold-concurrency`` and ``--chunk-rows`` override the derived values.
    Without `fold_memory_mib` this is the plan of one fit at a time (every
    GPU, every CPU thread), cached on `args`. With it (the RAM one fold needs,
    from the frame size) the fold concurrency is re-derived for `_map_folds`
    and the result is not cached.
    """
    cached = getattr(args, "resource_plan", None)
    if cached is not None and fold_memory_mib is None:
        return cached
    device = args.device
    if device == "auto" and os.environ.get("HPLDN_DISABLE_GPU") == "1":
        device = "cpu"
    use_gpu, _, reason = resolve_task_type(device, log)
    overrides = _plan_overrides(args)
    if fold_memory_mib is None:
        overrides["fold_concurrency"] = 1
    plan = hardware.plan_resources(
        device="gpu" if use_gpu else "cpu", overrides=overrides, fold_memory_mib=fold_memory_mib
    )
    if not use_gpu and reason and reason != "requested":
        plan = replace(plan, reason=reason)
    if fold_memory_mib is None:
        args.resource_plan = plan
        if log is not None:
            log.info(f"Resource plan: {plan.describe()}")
    return plan


def _quantize_params(cb_kwargs: Mapping[str, object]) -> Dict[str, object]:
    """
    Return `catboost.Pool.quantize` arguments matching how the trainer would quantise.

    ``--quantize-once`` quantises the fold's full training Pool once, so pass
    1 (its slices) and pass 2 share the borders. They then come from every
    training row rather than pass 1's own, which can move the metrics; the
    option is gated like the other fit strategies (`fit_gate`).
    """
    params: Dict[str, object] = {"border_count": 128 if cb_kwargs.get("task_type") == "GPU" else 254}
    if "nan_mode" in cb_kwargs:
        params["nan_mode"] = cb_kwargs["nan_mode"]
    if "thread_count" in cb_kwargs:
        params["thread_count"] = cb_kwargs["thread_count"]
    return params


def _recency_decay_weight(dates, halflife_years: float) -> np.ndarray:
    """Return an exponential-decay weight halving every `halflife_years`, normalised to mean 1."""
    if halflife_years <= 0:
        raise ValueError(
            f"--recency-halflife-years must be positive, got {halflife_years}"
        )
    as_datetime = pd.to_datetime(pd.Series(np.asarray(dates)))
    age_years = (as_datetime.max() - as_datetime).dt.total_seconds().to_numpy() / (
        365.25 * 24 * 3600
    )
    weight = np.exp2(-age_years / halflife_years)
    mean_weight = weight.mean()
    return weight / mean_weight if mean_weight > 0 else weight


def _training_weights(y_raw: np.ndarray, dates, args: argparse.Namespace) -> np.ndarray:
    """Return price-band sample weights, times recency decay when ``--recency-halflife-years`` is set."""
    weight = _sample_weight_for_training(y_raw, mode=args.sample_weight)
    halflife = getattr(args, "recency_halflife_years", None)
    if halflife is None or dates is None:
        return weight
    weight = weight * _recency_decay_weight(dates, halflife)
    mean_weight = weight.mean()
    return weight / mean_weight if mean_weight > 0 else weight


def _apply_train_from_year(
    train_df, args: argparse.Namespace, log: Optional[logging.Logger] = None
):
    """
    Drop training rows sold before ``--train-from-year``.

    Falls back to the unfiltered rows, with a warning, if none would remain.
    Dates are converted defensively because they can arrive as
    ``datetime.date`` objects.
    """
    from_year = getattr(args, "train_from_year", None)
    if from_year is None:
        return train_df
    sale_dates = pd.to_datetime(train_df[DATE_COLUMN])
    kept = train_df[
        (sale_dates >= pd.Timestamp(year=int(from_year), month=1, day=1)).to_numpy()
    ]
    if len(kept) == 0:
        if log is not None:
            log.warning(
                f"--train-from-year {from_year} would remove every training row "
                f"(the split's train side ends {sale_dates.max()}) -- training on all "
                f"{len(train_df)} rows instead"
            )
        return train_df
    if log is not None:
        log.info(
            f"--train-from-year {from_year}: training on {len(kept)} of {len(train_df)} rows "
            f"({len(train_df) - len(kept)} pre-{from_year} sales dropped)"
        )
    return kept


def _apply_test_year_filter(
    test_df, args: argparse.Namespace, log: Optional[logging.Logger] = None
):
    """
    Restrict the test side to rows sold in ``--test-year``.

    Falls back to the unfiltered rows, with a warning, if none would remain.
    """
    test_year = getattr(args, "test_year", None)
    if test_year is None:
        return test_df
    sale_dates = pd.to_datetime(test_df[DATE_COLUMN])
    kept = test_df[(sale_dates.dt.year == int(test_year)).to_numpy()]
    if len(kept) == 0:
        if log is not None:
            log.warning(
                f"--test-year {test_year} would leave no test rows "
                f"(the test side covers {sale_dates.min()} to {sale_dates.max()}) -- "
                f"testing on all {len(test_df)} rows instead"
            )
        return test_df
    if log is not None:
        log.info(f"--test-year {test_year}: testing on {len(kept)} of {len(test_df)} rows")
    return kept


def _eval_learning_curve(model) -> List[Dict[str, float]]:
    """
    Return the eval-carve learning curve: the best loss reached by each `LEARNING_CURVE_FRACTIONS` share of rounds.

    Read from the first-pass model's ``validation`` history; empty when it has
    none. A curve still falling at 100 % means the cap or the learning rate,
    not early stopping, ended the fit.
    """
    history = (model.get_evals_result() or {}).get("validation") or {}
    losses = next(iter(history.values()), None) if history else None
    if not losses:
        return []
    losses = np.asarray(losses, dtype=np.float64)
    points = []
    for fraction in LEARNING_CURVE_FRACTIONS:
        n = max(1, int(round(len(losses) * fraction)))
        points.append(
            {
                "fraction": float(fraction),
                "iteration": n,
                "best_loss": float(np.min(losses[:n])),
            }
        )
    return points


def _scaled_refit_iterations(
    best_iteration: Optional[int], n_full: int, n_fit: int, fallback: int
) -> int:
    """Return pass 2's round count scaled from pass 1's best iteration, or `fallback`."""
    if best_iteration is None:
        return fallback
    base = best_iteration + 1
    if n_fit <= 0 or n_full <= n_fit:
        return base
    return max(base, int(round(base * (n_full / n_fit))))


def _refit_kwargs_from_first_pass(
    cb_kwargs: Dict, first_pass_model, refit_iterations: int
) -> Dict:
    """Return pass-2 kwargs: no early stopping, pinned iterations and pass 1's realised learning rate."""
    refit_kwargs = dict(cb_kwargs)
    refit_kwargs.pop("early_stopping_rounds", None)
    refit_kwargs["iterations"] = refit_iterations
    realised_lr = first_pass_model.get_all_params().get("learning_rate")
    if realised_lr is not None:
        refit_kwargs["learning_rate"] = realised_lr
    return refit_kwargs


def _resolve_init_model(
    args: argparse.Namespace, log: Optional[logging.Logger] = None
) -> Optional[str]:
    """
    Return the model file to warm-start from, or None.

    Only when both ``--model-in`` and ``--model-out`` are given; the saved
    model's trees are reused and preprocessing is recomputed.
    """
    model_in = getattr(args, "model_in", None)
    if not model_in or not getattr(args, "model_out", None):
        return None
    from hpldn.backend.models import artifact

    model_in = Path(model_in)
    if not artifact.is_bundle(model_in):
        raise FileNotFoundError(
            f"--model-in {model_in} is not a model bundle -- cannot use it as CatBoost's init_model to "
            "warm-start this run"
        )
    # Verified like any bundle before its trees are reused.
    load_model(model_in)
    if log is not None:
        log.info(
            f"Warm-starting from --model-in {model_in} (CatBoost init_model); result saved to --model-out"
        )
    return str(model_in / artifact.MODEL_FILE)


@dataclass
class FitArtifacts:
    """
    The post-fit adjustments a saved model must carry to reproduce its reported metrics.

    Attributes
    ----------
    smearing_factors : dict or None
        Duan factors per price band, set with ``--smearing-correction`` on a
        log or deflated target.
    high_value_model : CatBoostRegressor or None
        The ``--two-stage`` expert, when one was fitted.
    high_value_threshold : float or None
        Price at or above which general predictions are routed to the expert.
    """

    smearing_factors: Optional[Dict[str, float]] = None
    high_value_model: Optional[object] = None
    high_value_threshold: Optional[float] = None


def predict_with_adjustments(
    model,
    X: pd.DataFrame,
    cat_features: List[str],
    target: str,
    deflator: Optional[np.ndarray],
    smearing_factors: Optional[Dict[str, float]] = None,
    high_value_model=None,
    high_value_threshold: Optional[float] = None,
) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """
    Predict prices exactly as a training run reports them.

    The one prediction path for both `_fit_and_evaluate` and `run_prediction`
    (MAJ-9): invert the target transform, apply the smearing factors, and, with
    an expert, route general predictions at or above `high_value_threshold` to
    it (smeared the same way).

    Returns
    -------
    general : numpy.ndarray
        The general model's prices.
    mixture : numpy.ndarray or None
        The two-stage prices, or None without an expert.
    """
    general = _target_inverse_transform(
        _predict_in_batches(model, X, cat_features), deflator, target
    )
    if smearing_factors is not None:
        general = _apply_smearing(general, smearing_factors)
    if high_value_model is None:
        return general, None
    mixture = general.copy()
    route_positions = np.flatnonzero(general >= high_value_threshold)
    if route_positions.size:
        expert = _target_inverse_transform(
            _predict_in_batches(high_value_model, X.iloc[route_positions], cat_features),
            deflator[route_positions] if deflator is not None else None,
            target,
        )
        if smearing_factors is not None:
            expert = _apply_smearing(expert, smearing_factors)
        mixture[route_positions] = expert
    return general, mixture


def _fit_and_evaluate(
    train_df,
    test_df,
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
    include_price_bands: bool = False,
    val_df=None,
    fatal_if_truncated: bool = False,
    report_plausibility_filtered: bool = False,
    is_final_fit: bool = True,
):
    """
    Add price statistics, prepare features, fit CatBoost on train and evaluate.

    `is_final_fit` is False for a cross-validation or backtest fold, whose
    model is only measured: under ``--population all`` a fold whose rows
    predate energy certificates may then drop the EPC core features.

    Fits in two passes: early stopping on a tail carved from train, then a
    refit on all of train. Handles warm starts, sample weights, smearing and,
    with ``--two-stage``, a high-value expert mixture.

    Returns
    -------
    model, medians, postcode_stats, sector_stats
        The fitted artefacts.
    results : dict
        Backend, row counts, feature columns, ``dropped_features``,
        ``core_feature_missing_rates``, ``smearing_factors`` and per-split
        metrics.
    artifacts : FitArtifacts
        Smearing factors and high-value expert; `_save_model_if_requested`
        persists them so a saved model scores as reported.

    Raises
    ------
    RuntimeError
        From `hpp_rf.check_core_feature_availability` or `prepare_features`
        when a core feature is missing or would be dropped.
    """
    from catboost import CatBoostRegressor, Pool

    resolve_nan_mode(args, log)
    population = getattr(args, "population", DEFAULT_POPULATION)
    train_df = _apply_train_from_year(train_df, args, log=log)
    train_df = _drop_rows_with_unusable_target(
        train_df, args, log, context="_fit_and_evaluate/train"
    )
    train_df = _drop_implausible_training_rows(
        train_df, log, context="_fit_and_evaluate/train"
    )
    test_df = _apply_test_year_filter(test_df, args, log=log)
    test_df = _drop_rows_with_unusable_target(
        test_df, args, log, context="_fit_and_evaluate/test"
    )
    if val_df is not None:
        val_df = _drop_rows_with_unusable_target(
            val_df, args, log, context="_fit_and_evaluate/val"
        )
    core_feature_missing_rates = check_core_feature_availability(
        {"train": train_df, "val": val_df, "test": test_df},
        log=log,
        population=population,
        is_final_fit=is_final_fit,
    )
    val_or_placeholder = val_df if val_df is not None else train_df.iloc[:1]
    n_train, n_test = len(train_df), len(test_df)
    n_val = len(val_df) if val_df is not None else 0
    test_implausible_mask = (
        _implausible_price_per_sqm_mask(test_df)
        if report_plausibility_filtered
        else None
    )
    # Read off the raw test rows now: prepare_features keeps only model inputs.
    test_scoped_exclusion = _scoped_exclusion_mask(test_df)
    test_segments = _segment_masks(test_df) if include_price_bands else {}
    with profiling.stage("price_stats"):
        train_df, val_stat_df, test_df, postcode_stats, sector_stats, price_stat_globals = (
            _add_price_stat_columns(
                train_df,
                val_or_placeholder,
                test_df,
                random_state=args.random_state,
                log=log,
                deflate=(
                    getattr(args, "market_features", True) and UKHPI_DEFLATOR_COLUMN in train_df.columns
                ),
            )
        )
    if not train_df[DATE_COLUMN].is_monotonic_increasing:
        raise RuntimeError(
            "_fit_and_evaluate: train_df must be sorted by DATE_COLUMN ascending for has_time=True "
            "(see _catboost_kwargs/B4) -- got an out-of-order frame from the caller's split method"
        )
    y_train_raw_full = train_df[TARGET_COLUMN].to_numpy(dtype=np.float64)
    y_test_raw = test_df[TARGET_COLUMN].to_numpy(dtype=np.float64)
    deflator_train_full = target_deflator(train_df, args.target)
    deflator_test = target_deflator(test_df, args.target)
    deflator_val = target_deflator(val_df, args.target) if val_df is not None else None
    y_val_raw = (
        val_df[TARGET_COLUMN].to_numpy(dtype=np.float64) if val_df is not None else None
    )
    y_train_full = _target_transform(y_train_raw_full, deflator_train_full, args.target)
    dates_train_full = train_df[DATE_COLUMN].to_numpy()

    with profiling.stage("prepare_features"):
        X_train_full, X_val, X_test, cat_features, medians, feature_metadata = prepare_features(
            train_df,
            val_stat_df,
            test_df,
            use_iod=args.use_iod,
            use_ofsted=getattr(args, "use_ofsted", True),
            nan_mode=args.nan_mode,
            drop_features=args.drop_features,
            drop_sale_year=(args.target in DEFLATOR_COLUMN_BY_TARGET),
            availability_shift_limit=args.availability_shift_limit,
            log=log,
            population=population,
            is_final_fit=is_final_fit,
            availability_window_frac=args.eval_frac,
        )
    del train_df, val_stat_df, test_df, val_or_placeholder
    gc.collect()

    eval_mask = _carve_eval_mask(n_train, frac=args.eval_frac)
    n_eval_carve = int(eval_mask.sum())
    fit_end = n_train - n_eval_carve
    if eval_mask[:fit_end].any() or not eval_mask[fit_end:].all():
        raise RuntimeError(
            "_fit_and_evaluate: _carve_eval_mask no longer returns a trailing contiguous block; "
            "the positional slices below would no longer match it"
        )
    del eval_mask
    y_fit_train, y_eval = y_train_full[:fit_end], y_train_full[fit_end:]
    y_fit_train_raw, y_eval_raw = y_train_raw_full[:fit_end], y_train_raw_full[fit_end:]
    deflator_eval = (
        deflator_train_full[fit_end:] if deflator_train_full is not None else None
    )

    init_model_path = _resolve_init_model(args, log)
    cb_kwargs, backend = _catboost_kwargs(args, log, warm_start=bool(init_model_path))
    cb_kwargs["early_stopping_rounds"] = args.early_stopping_rounds
    refit = is_final_fit or getattr(args, "fold_refit", "full") == "full"

    gpu_warm_start = bool(init_model_path) and cb_kwargs["task_type"] == "GPU"
    init_model_obj = None
    if init_model_path is not None:
        init_model_obj = CatBoostRegressor()
        init_model_obj.load_model(init_model_path)

    # One Pool for the whole training frame; pass 1's fit rows and eval carve
    # are slices of it (`Pool.slice`), with their own weights, so the frame is
    # converted (categoricals hashed, floats copied) once rather than three
    # times. A slice trains exactly like a Pool built from the same rows.
    with profiling.stage("pool", rows=n_train):
        full_baseline = (
            init_model_obj.predict(Pool(X_train_full, cat_features=cat_features)) if gpu_warm_start else None
        )
        weight_train_full = _training_weights(y_train_raw_full, dates_train_full, args)
        full_train_pool = Pool(
            X_train_full,
            y_train_full,
            cat_features=cat_features,
            weight=weight_train_full,
            baseline=full_baseline,
        )
        if getattr(args, "quantize_once", False):
            full_train_pool.quantize(**_quantize_params(cb_kwargs))
        train_pool = full_train_pool.slice(np.arange(fit_end))
        train_pool.set_weight(_training_weights(y_fit_train_raw, dates_train_full[:fit_end], args))
        eval_pool = full_train_pool.slice(np.arange(fit_end, n_train))
        eval_pool.set_weight(_training_weights(y_eval_raw, dates_train_full[fit_end:], args))
    n_train_features = len(X_train_full)
    two_stage = bool(getattr(args, "two_stage", False))
    if not two_stage:
        del X_train_full
    del full_baseline
    gc.collect()
    first_pass_model = CatBoostRegressor(**cb_kwargs)
    if log is not None:
        log.info(f"Training on backend: {backend}")
    with profiling.stage("pass1", rows=fit_end) as stage_facts:
        first_pass_model.fit(
            train_pool,
            eval_set=eval_pool,
            use_best_model=True,
            init_model=None if gpu_warm_start else init_model_path,
        )
        stage_facts["trees"] = int(first_pass_model.tree_count_)
    _warn_if_iterations_truncated(
        first_pass_model,
        args.iterations,
        args.early_stopping_rounds,
        log,
        context="_fit_and_evaluate",
        fatal=fatal_if_truncated,
    )
    learning_curve = _eval_learning_curve(first_pass_model)
    if log is not None and learning_curve:
        log.info(
            "_fit_and_evaluate: eval-carve learning curve (best loss by share of rounds run): "
            + ", ".join(
                f"{point['fraction']:.0%} ({point['iteration']} it) {point['best_loss']:.5f}"
                for point in learning_curve
            )
        )
    best_iteration = first_pass_model.get_best_iteration()
    del train_pool
    gc.collect()

    smearing_factors = None
    if (
        args.smearing_correction
        and args.target in ("log_price", *DEFLATOR_COLUMN_BY_TARGET)
        and n_eval_carve
    ):
        eval_preds_transformed = np.asarray(first_pass_model.predict(eval_pool))
        eval_preds_raw = _target_inverse_transform(
            eval_preds_transformed, deflator_eval, args.target
        )
        smearing_factors = _duan_smearing_factors(
            eval_preds_raw, y_eval - eval_preds_transformed
        )
    del eval_pool
    gc.collect()

    if refit:
        refit_iterations = _scaled_refit_iterations(
            best_iteration, n_train, fit_end, args.iterations
        )
        refit_kwargs = _refit_kwargs_from_first_pass(
            cb_kwargs, first_pass_model, refit_iterations
        )
        del first_pass_model
        gc.collect()
        model = CatBoostRegressor(**refit_kwargs)
        with profiling.stage("pass2", rows=n_train, iterations=refit_iterations):
            model.fit(full_train_pool, init_model=None if gpu_warm_start else init_model_path)
        if gpu_warm_start:
            from catboost import sum_models

            model = sum_models([init_model_obj, model], weights=[1.0, 1.0])
        if log is not None:
            log.info(
                f"_fit_and_evaluate: refit on the full {n_train_features}-row training frame "
                f"at iterations={refit_iterations}, learning_rate={refit_kwargs['learning_rate']} "
                f"(pass 2; rate pinned from pass 1, count scaled to the larger frame)"
            )
    else:
        # --fold-refit final-only: a fold's model is only measured, so it is
        # scored as pass 1 left it (early-stopped on the eval carve).
        refit_iterations = None
        refit_kwargs = {"learning_rate": first_pass_model.get_all_params().get("learning_rate")}
        model = first_pass_model
        if gpu_warm_start:
            from catboost import sum_models

            model = sum_models([init_model_obj, model], weights=[1.0, 1.0])
        del first_pass_model
        if log is not None:
            log.info(
                f"_fit_and_evaluate: --fold-refit final-only, scoring the early-stopped pass-1 model "
                f"({best_iteration} best iteration) without a refit"
            )
    del full_train_pool
    gc.collect()

    high_value_model = None
    n_high_value_train = int((y_train_raw_full >= args.high_value_threshold).sum())
    if two_stage:
        high_value_model = fit_high_value_expert(
            X_train_full,
            y_train_raw_full,
            y_train_full,
            cat_features,
            args,
            high_value_threshold=args.high_value_threshold,
            sample_weight=weight_train_full,
            log=log,
        )
        del X_train_full
        gc.collect()
    del weight_train_full

    results = {
        "backend": backend,
        "n_train": n_train,
        "n_eval_carve": n_eval_carve,
        "n_val": n_val,
        "n_test": n_test,
        "best_iteration": best_iteration,
        "refit_iterations": refit_iterations,
        "feature_columns": feature_metadata["feature_columns"],
        "cat_features": list(cat_features),
        "dropped_flags": feature_metadata["dropped_flags"],
        "dropped_features": feature_metadata["dropped_features"],
        "core_feature_missing_rates": core_feature_missing_rates,
        "smearing_factors": smearing_factors,
        "missing_flag_groups": feature_metadata["missing_flag_groups"],
        "epc_missing_flag": feature_metadata["epc_missing_flag"],
        "population": population,
        "nan_mode": args.nan_mode,
        "target": args.target,
        "drop_features": list(args.drop_features) if args.drop_features else [],
        "price_stat_globals": price_stat_globals,
        "learning_rate": refit_kwargs.get("learning_rate"),
        "learning_curve": learning_curve,
        "fold_refit": "full" if refit else "final-only",
    }
    if two_stage:
        results["two_stage"] = True
        results["high_value_threshold"] = args.high_value_threshold
        results["high_value_train_rows"] = n_high_value_train
        results["high_value_expert_fitted"] = high_value_model is not None
    splits = [("test", X_test, y_test_raw, deflator_test)]
    if val_df is not None:
        splits.insert(0, ("val", X_val, y_val_raw, deflator_val))
    for split_name, X, y_true_raw, deflator in splits:
        with profiling.stage(f"predict_{split_name}", rows=len(X)):
            preds, mixture_preds = predict_with_adjustments(
                model,
                X,
                cat_features,
                args.target,
                deflator,
                smearing_factors=smearing_factors,
                high_value_model=high_value_model,
                high_value_threshold=args.high_value_threshold,
            )
        metrics = _metrics_with_bias(y_true_raw, preds)
        if include_price_bands:
            metrics["by_price_band"] = _metrics_by_price_band_with_bias(
                y_true_raw, preds
            )
        if two_stage:
            if mixture_preds is None:
                mixture_preds = preds
            mixture_metrics = _metrics_with_bias(y_true_raw, mixture_preds)
            if include_price_bands:
                mixture_metrics["by_price_band"] = _metrics_by_price_band_with_bias(
                    y_true_raw, mixture_preds
                )
            metrics["mixture"] = mixture_metrics
        if split_name == "test":
            scoped = _metrics_on_mask(y_true_raw, preds, ~test_scoped_exclusion)
            scoped["n_excluded"] = int(test_scoped_exclusion.sum())
            metrics["scoped"] = scoped
            if two_stage:
                metrics["mixture"]["scoped"] = _metrics_on_mask(
                    y_true_raw, mixture_preds, ~test_scoped_exclusion
                )
            if test_segments:
                metrics["by_segment"] = {
                    name: _metrics_on_mask(y_true_raw, preds, mask)
                    for name, mask in test_segments.items()
                }
        if split_name == "test" and test_implausible_mask is not None:
            keep = ~test_implausible_mask
            plausibility_metrics = _metrics_with_bias(y_true_raw[keep], preds[keep])
            plausibility_metrics["n_excluded"] = int(test_implausible_mask.sum())
            metrics["plausibility_filtered"] = plausibility_metrics
        del mixture_preds
        results[split_name] = metrics
        if log is not None:
            log.info(f"{split_name}: {_format_headline(metrics)}")
            if two_stage:
                log.info(
                    f"{split_name} two-stage mixture: general MAE=£{metrics['mae']:,.0f} "
                    f"vs mixture MAE=£{metrics['mixture']['mae']:,.0f} "
                    f"(general MedAE=£{metrics['medae']:,.0f} vs mixture "
                    f"MedAE=£{metrics['mixture']['medae']:,.0f})"
                )
            if "plausibility_filtered" in metrics:
                pf = metrics["plausibility_filtered"]
                log.info(
                    f"{split_name} plausibility_filtered ({pf['n_excluded']} row(s) above "
                    f"£{PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD:,.0f}/m² excluded): "
                    f"MAE=£{pf['mae']:,.0f} MedAE=£{pf['medae']:,.0f} RMSE=£{pf['rmse']:,.0f}"
                )
            if "scoped" in metrics and "mae" in metrics["scoped"]:
                log.info(
                    f"{split_name} scoped ({metrics['scoped']['n_excluded']} row(s) outside "
                    f"{PLAUSIBILITY_MIN_PPS_RATIO:.0%} of comparables..£"
                    f"{PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD:,.0f}/m² or at/above "
                    f"£{SCOPED_MAX_PRICE:,.0f} excluded): {_format_headline(metrics['scoped'])}"
                )
            for name, seg in metrics.get("by_segment", {}).items():
                if "mae" in seg:
                    log.info(f"{split_name} segment {name}: {_format_headline(seg)}")
    del splits, X_val, X_test
    gc.collect()
    artifacts = FitArtifacts(
        smearing_factors=smearing_factors,
        high_value_model=high_value_model,
        high_value_threshold=args.high_value_threshold if high_value_model is not None else None,
    )
    return model, medians, postcode_stats, sector_stats, results, artifacts


def split_dataset(
    df, args: argparse.Namespace, log: Optional[logging.Logger] = None
) -> Tuple:
    """
    Split `df` with the method named by ``args.split_method``.

    Same methods and semantics as `hpp_rf.split_dataset`, but ``walk_forward``
    fits this module's CatBoost model per fold.
    """
    method = args.split_method
    if method == "chronological":
        return time_series_split(df, args.train_frac, args.val_frac)
    if method == "grouped":
        return grouped_time_series_split(df, args.train_frac, args.val_frac, log=log)
    if method == "purged":
        return purged_embargo_time_series_split(
            df, args.train_frac, args.val_frac, embargo_days=args.embargo_days, log=log
        )
    if method == "spatial":
        return spatial_holdout_split(
            df,
            test_frac=args.spatial_test_frac,
            val_frac=args.spatial_val_frac,
            random_state=args.random_state,
            log=log,
        )
    if method == "recency":
        return recency_windowed_time_series_split(
            df,
            args.train_frac,
            args.val_frac,
            train_window_years=args.train_window_years,
            log=log,
        )
    if method == "walk_forward":
        test_frac = 1.0 - args.train_frac - args.val_frac
        folds, non_test_df, test_df = time_series_cv_folds(
            df, n_folds=args.cv_folds, test_frac=test_frac, log=log
        )
        fold_mae, fold_r2 = [], []

        def _fold(job, fold_args):
            i, (fold_train, fold_val) = job
            gc.collect()
            with profiling.stage(f"walk_forward/fold {i + 1}", rows=len(fold_train)):
                *_, fold_results, _ = _fit_and_evaluate(
                    fold_train, fold_val, fold_args, log=_fold_log(log), is_final_fit=False
                )
            return fold_results

        for i, fold_results in enumerate(_map_folds(_fold, list(enumerate(folds)), args, non_test_df, log)):
            unavailable = sorted(
                c
                for c, info in fold_results["dropped_features"].items()
                if info.get("reason") == "unavailable_in_fold_train"
            )
            if unavailable and log is not None:
                log.info(
                    f"walk-forward fold {i + 1}/{len(folds)}: trained without {unavailable} "
                    "(unavailable_in_fold_train)"
                )
            fold_mae.append(fold_results["test"]["mae"])
            fold_r2.append(fold_results["test"]["r2"])
            if log is not None:
                log.info(
                    f"walk-forward fold {i + 1}/{len(folds)}: "
                    f"MAE=£{fold_results['test']['mae']:,.0f} R2={fold_results['test']['r2']:.3f}"
                )
        if log is not None:
            log.info(
                f"walk-forward CV across {len(folds)} folds: "
                f"MAE=£{np.mean(fold_mae):,.0f}±£{np.std(fold_mae):,.0f} "
                f"R2={np.mean(fold_r2):.3f}±{np.std(fold_r2):.3f}"
            )
        last_fold_train, last_fold_val = folds[-1]
        return last_fold_train, last_fold_val, test_df
    raise ValueError(f"Unknown split method {method!r}")


def expert_model_path(model_path) -> Path:
    """Return where the ``--two-stage`` expert of a saved model bundle lives: ``<bundle>/expert.cbm``."""
    from hpldn.backend.models import artifact

    return Path(model_path) / artifact.EXPERT_FILE


def _dataset_fingerprint(results: Dict, args: argparse.Namespace) -> Dict:
    """Return the bundle manifest's ``dataset`` block for a training run."""
    from hpldn.backend.models import artifact

    revision = None
    if getattr(args, "database", None):
        from sqlalchemy import create_engine

        revision = artifact.alembic_revision(create_engine(get_model_db_url()))
    return artifact.dataset_fingerprint(
        database=getattr(args, "database", None),
        alembic=revision,
        row_count=results.get("n_train"),
        date_range=results.get("train_period"),
    )


def _save_model_if_requested(
    model,
    medians,
    postcode_stats,
    sector_stats,
    results: Dict,
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
    artifacts: Optional[FitArtifacts] = None,
) -> None:
    """
    Save the model as a signed bundle directory at ``--model-out`` (`models.artifact`).

    The bundle holds the canonical ``model.cbm``, the preprocessing state as
    ``meta.json`` (the smearing factors and, with a fitted expert, its
    threshold and ``expert.cbm``), the probe rows and a manifest; it is signed
    with the local key when one exists. Without the smearing factors and the
    expert a reloaded model would score differently from the metrics its
    training run printed (MAJ-9).
    """
    if not args.model_out:
        return
    from hpldn.backend.models import artifact

    artifacts = artifacts or FitArtifacts()
    meta = {
        "lot_medians": medians,
        "postcode_stats": postcode_stats,
        "sector_stats": sector_stats,
        "price_stat_globals": results["price_stat_globals"],
        "group_stats_scale": results["price_stat_globals"].get("scale", GROUP_STATS_SCALE_NOMINAL),
        "feature_columns": results["feature_columns"],
        "cat_features": results["cat_features"],
        "dropped_flags": results["dropped_flags"],
        "missing_flag_groups": results["missing_flag_groups"],
        "nan_mode": args.nan_mode,
        "drop_features": list(args.drop_features) if args.drop_features else [],
        "categorical_columns": list(CATEGORICAL_COLUMNS)
        + (IOD_CATEGORICAL_COLUMNS if args.use_iod else [])
        + (OFSTED_CATEGORICAL_COLUMNS if args.use_ofsted else []),
        "use_iod": args.use_iod,
        "use_ofsted": args.use_ofsted,
        "log_target": args.target in ("log_price", *DEFLATOR_COLUMN_BY_TARGET),
        "target": args.target,
        # What an anchored target is relative to, first present wins
        # (`hpp_rf.add_market_relative_features` rebuilds it at scoring).
        "anchor_columns": list(ANCHOR_SOURCE_COLUMNS) if args.target == "anchored" else None,
        "smearing_factors": artifacts.smearing_factors,
        "high_value_expert": artifacts.high_value_model is not None,
        "high_value_threshold": artifacts.high_value_threshold,
        "population": getattr(args, "population", DEFAULT_POPULATION),
        "train_period": results.get("train_period"),
        "database": getattr(args, "database", None),
    }
    artifact.write_bundle(
        Path(args.model_out),
        model,
        meta,
        expert=artifacts.high_value_model,
        training_args=artifact.jsonable_args(args),
        dataset=_dataset_fingerprint(results, args),
        logger=log,
    )
    if log is not None:
        log.info(f"Saved model bundle to {args.model_out}")
    _write_price_index_sidecars(args, log)


def _write_price_index_sidecars(args: argparse.Namespace, log: Optional[logging.Logger] = None) -> None:
    """
    Fit and save the UK HPI projection and the local price index into the ``--model-out`` bundle.

    Pricing moves every future price along them; a model saved without them
    priced every future date the same (the index frozen at its last month).
    Written only for a run against a database (`main` sets ``args.database``),
    and a failure is logged rather than losing the model: pricing refits the
    projection from the live index when the sidecar is missing, and says so.
    The bundle is re-hashed and re-signed after they are added.
    """
    if not getattr(args, "database", None):
        return
    try:
        from sqlalchemy import create_engine

        from hpldn.backend.models import artifact
        from hpldn.backend.scripts import hpp_inference, local_index

        engine = create_engine(get_model_db_url(log))
        projection = hpp_inference.fit_projection(engine, log)
        index = local_index.fit(engine, log)
        artifact.update_json_files(
            Path(args.model_out),
            {artifact.PROJECTION_FILE: projection, artifact.LOCAL_INDEX_FILE: index},
            logger=log,
        )
        if log is not None:
            log.info(f"Saved the price index projection and local index in {args.model_out}")
    except Exception as exc:  # noqa: BLE001 -- the model itself is saved; see the docstring
        if log is not None:
            log.error(
                f"Could not write the price index sidecars into {args.model_out} ({exc}); run "
                f"hpp_inference fit-projection and fit-local-index --model {args.model_out} --write"
            )


def _engineer_training_frame(
    df,
    mortgage_df: pd.DataFrame,
    ukhpi_df: Optional[pd.DataFrame],
    log: Optional[logging.Logger] = None,
    market_features: bool = True,
    local_index=None,
) -> pd.DataFrame:
    """
    Return `df` date-sorted with every unfitted feature a model reads.

    `hpp_rf.engineer_features`, `hpp_rf.add_market_relative_features` (the UK
    HPI at each previous sale is looked up in `ukhpi_df`; without it the
    repeat-sale features are NaN unless `df` carries ``ukhpi_at_prev_sale``),
    `add_mortgage_rate_features` and `add_canonical_categoricals` (each
    categorical cast once for every fold). Without `market_features`
    (``--no-market-features``) the market-relative columns are not added.
    This is the frame `feature_cache` saves.

    With them, the size-matched comparables (`hpp_rf.add_sizeband_comp_features`)
    and the anchor features, moved along `local_index` -- the district index
    built only from repeat sales up to the training cutoff (`_training_local_index`),
    so nothing after the training period moves a level the model learns from.
    """
    with profiling.stage("features/engineer_features", rows=len(df)):
        df = engineer_features(df).sort_values(DATE_COLUMN).reset_index(drop=True)
    gc.collect()
    if market_features:
        with profiling.stage("features/sizeband_comps"):
            df = add_sizeband_comp_features(df, local_index, log=log)
        with profiling.stage("features/market_relative"):
            df = add_market_relative_features(df, ukhpi_df, local_index)
        if log is not None:
            n_prev = int(df["prev_sale_price"].notna().sum()) if "prev_sale_price" in df.columns else 0
            n_deflated = int(df["prev_sale_deflated"].notna().sum())
            log.info(
                f"Added {len(MARKET_RELATIVE_COLUMNS)} market-relative feature(s); "
                f"prev_sale_deflated set on {n_deflated} of {n_prev} row(s) with a previous sale"
            )
    with profiling.stage("features/mortgage_rates"):
        df = add_mortgage_rate_features(df, mortgage_df, log=log)
    with profiling.stage("features/canonical_categoricals"):
        return add_canonical_categoricals(df)


def run_training(
    df,
    args: argparse.Namespace,
    mortgage_df: pd.DataFrame,
    log: Optional[logging.Logger] = None,
    ukhpi_df: Optional[pd.DataFrame] = None,
    engineered: bool = False,
) -> Dict:
    """
    Split, prepare features, train and evaluate over a loaded DataFrame.

    `mortgage_df` is passed in so tests can supply synthetic data and a run
    loads it once. With `engineered`, `df` is already
    `_engineer_training_frame`'s output (`main`, possibly from the
    `feature_cache`).
    """
    if not engineered:
        df = _engineer_training_frame(
            df, mortgage_df, ukhpi_df, log=log, market_features=getattr(args, "market_features", True),
            local_index=getattr(args, "local_index", None),
        )
    train_df, val_df, test_df = split_dataset(df, args, log=log)
    log_split_diagnostics(train_df, val_df, test_df, log)
    train_dates = pd.to_datetime(train_df[DATE_COLUMN])
    train_period = [str(train_dates.min().date()), str(train_dates.max().date())]
    with profiling.stage("fit"):
        model, medians, postcode_stats, sector_stats, results, artifacts = _fit_and_evaluate(
            train_df, test_df, args, log=log, val_df=val_df
        )
    results["train_period"] = train_period

    _save_model_if_requested(
        model, medians, postcode_stats, sector_stats, results, args, log=log,
        artifacts=artifacts,
    )

    return results


def load_model(model_path) -> Tuple[object, Dict]:
    """
    Verify and load a model bundle saved by ``--model-out`` (`models.artifact.load_bundle`).

    Returns
    -------
    model : CatBoostRegressor
        The loaded model.
    meta : dict
        Medians, price-stat lookups, feature columns, categoricals, missing
        flag groups, ``smearing_factors``, ``high_value_threshold`` and the
        loaded ``high_value_model`` (None without one).

    Raises
    ------
    FileNotFoundError
        When `model_path` is not a bundle directory; a pre-bundle ``.cbm`` with
        a ``.meta.joblib`` pickle is converted once with ``models.artifact convert``.
    models.artifact.BundleError
        When the bundle fails verification.
    """
    from hpldn.backend.models import artifact

    model_path = Path(model_path)
    if not artifact.is_bundle(model_path):
        hint = (
            f"; convert it once with python -m hpldn.backend.models.artifact convert {model_path} <bundle dir>"
            if model_path.is_file() and Path(f"{model_path}.meta.joblib").is_file()
            else ""
        )
        raise FileNotFoundError(
            f"load_model: {model_path} is not a model bundle -- pass the directory a previous "
            f"--model-out run wrote{hint}"
        )
    model, meta, _ = artifact.load_bundle(model_path)
    return model, meta


def _apply_saved_group_stats(
    df: pd.DataFrame,
    group_col: str,
    stats_frame: pd.DataFrame,
    stat_columns: Sequence[str],
    median_price: float,
    median_pps: float,
) -> pd.DataFrame:
    """
    Apply a price-statistics lookup saved at training time to new data.

    Unseen groups get the saved global medians and a count of 0. Nothing is
    refit, so the statistics stay those the model was trained on.
    """
    median_col, pps_col, count_col = stat_columns
    merged = df.merge(stats_frame, how="left", left_on=group_col, right_index=True)
    merged[median_col] = merged[median_col].fillna(median_price)
    merged[pps_col] = merged[pps_col].fillna(median_pps)
    merged[count_col] = merged[count_col].fillna(0)
    return merged


def prepare_features_from_saved_model(
    df: pd.DataFrame,
    mortgage_df: pd.DataFrame,
    meta: Dict,
    log: Optional[logging.Logger] = None,
    engineered: bool = False,
    ukhpi_df: Optional[pd.DataFrame] = None,
    local_index=None,
) -> Tuple[pd.DataFrame, Optional[np.ndarray], Optional[np.ndarray]]:
    """
    Rebuild the exact feature matrix a loaded model was trained on.

    Applies the same unfitted feature engineering as training, then only the
    saved medians, lookups and column list. With `engineered` set, `df` has
    already been through `hpp_rf.engineer_features` (``hpp_inference`` does
    that itself, to supply the rolling statistics from sales history), and
    is only sorted by date. The market-relative features are recomputed
    either way (`hpp_rf.add_market_relative_features`, with `ukhpi_df` or
    the frame's own ``ukhpi_at_prev_sale``). The saved postcode and sector
    statistics are applied as stored: on the deflated scale for a model
    trained since they were deflated, nominal for an older one.

    Returns
    -------
    X : pandas.DataFrame
        Columns aligned to ``meta["feature_columns"]``.
    y_raw : numpy.ndarray or None
        The target, when `df` has one.
    deflator : numpy.ndarray or None
        Needed to invert a ``"deflated"`` or ``"anchored"`` target: the UK HPI
        at the sale, or the anchor value (`DEFLATOR_COLUMN_BY_TARGET`).
    """
    if not engineered:
        df = engineer_features(df)
        if "comp_sizeband_pps" not in df.columns:
            df = add_sizeband_comp_features(df, local_index, log=log)
    # Stable, so rows on the same date keep the caller's order.
    df = df.sort_values(DATE_COLUMN, kind="stable").reset_index(drop=True)
    df = add_market_relative_features(df, ukhpi_df, local_index)
    df = add_mortgage_rate_features(df, mortgage_df, log=log)

    float_dtype = _frame_float_dtype(df)
    df = df.copy(deep=False)
    df["floor_level_ord"] = (
        df["floor_level"].map(_FLOOR_LEVEL_ORDINAL).astype(float_dtype)
    )
    if meta.get("use_iod", True):
        for col in IOD_ORDINAL_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float_dtype)

    df = _apply_saved_group_stats(
        df,
        "postcode_district",
        meta["postcode_stats"],
        POSTCODE_STAT_COLUMNS,
        meta["price_stat_globals"]["median_price"],
        meta["price_stat_globals"]["median_pps"],
    )
    df = _apply_saved_group_stats(
        df,
        "postcode_sector",
        meta["sector_stats"],
        SECTOR_STAT_COLUMNS,
        meta["price_stat_globals"]["median_price"],
        meta["price_stat_globals"]["median_pps"],
    )

    feature_columns: List[str] = meta["feature_columns"]
    cat_feature_set = set(meta["cat_features"])
    missing_flag_groups: Dict[str, List[str]] = meta["missing_flag_groups"]
    medians: Dict[str, float] = meta["lot_medians"]
    nan_mode = meta.get("nan_mode", "impute")

    month = df["sale_month"].astype(float)
    sale_month_sin = np.sin(2.0 * np.pi * month / 12.0).astype(float_dtype)
    sale_month_cos = np.cos(2.0 * np.pi * month / 12.0).astype(float_dtype)

    data: Dict[str, object] = {}
    for col in feature_columns:
        if col in cat_feature_set:
            data[col] = _canonical_categorical(df[col], col)
        elif col == "sale_month_sin":
            data[col] = sale_month_sin
        elif col == "sale_month_cos":
            data[col] = sale_month_cos
        elif col == EPC_MISSING_FLAG:
            data[col] = _epc_missing_indicator(df).astype(float_dtype)
        elif col in missing_flag_groups:
            data[col] = _missing_indicator(df, missing_flag_groups[col]).astype(
                float_dtype
            )
        else:
            values = df[col]
            if nan_mode == "impute" and col in medians:
                values = values.fillna(medians[col])
            data[col] = values.astype(float_dtype)

    X = pd.DataFrame(data, index=df.index)[feature_columns]
    y_raw = (
        df[TARGET_COLUMN].to_numpy(dtype=np.float64)
        if TARGET_COLUMN in df.columns
        else None
    )
    target = meta.get("target", "price")
    column = DEFLATOR_COLUMN_BY_TARGET.get(target, UKHPI_DEFLATOR_COLUMN)
    deflator = df[column].to_numpy(dtype=np.float64) if column in df.columns else None
    return X, y_raw, deflator


def run_prediction(
    df,
    args: argparse.Namespace,
    mortgage_df: pd.DataFrame,
    model,
    meta: Dict,
    log: Optional[logging.Logger] = None,
    ukhpi_df: Optional[pd.DataFrame] = None,
) -> Dict:
    """
    Score `df` with a loaded model instead of training.

    Predictions go through `predict_with_adjustments` with the saved smearing
    factors and expert, so they match the training run's. Metrics are reported
    when `df` has a target (``metrics`` for the general model, ``mixture`` for
    the two-stage prices, as in training); otherwise predictions are
    summarised, from the mixture when there is an expert.
    """
    X, y_raw, deflator = prepare_features_from_saved_model(
        df, mortgage_df, meta, log=log, ukhpi_df=ukhpi_df, local_index=getattr(args, "local_index", None)
    )
    expected_columns = meta["feature_columns"]
    if list(X.columns) != list(expected_columns):
        raise RuntimeError(
            "run_prediction: the feature matrix rebuilt from this data does not match the "
            f"loaded model's saved feature_columns -- got {list(X.columns)}, "
            f"expected {expected_columns}. The loaded model cannot score this data safely."
        )

    target = meta.get("target", "price")
    smearing_factors = meta.get("smearing_factors")
    high_value_model = meta.get("high_value_model")
    general, mixture = predict_with_adjustments(
        model,
        X,
        meta["cat_features"],
        target,
        deflator if target in DEFLATOR_COLUMN_BY_TARGET else None,
        smearing_factors=smearing_factors,
        high_value_model=high_value_model,
        high_value_threshold=meta.get("high_value_threshold"),
    )
    preds = mixture if mixture is not None else general

    results: Dict[str, object] = {
        "model_in": str(args.model_in),
        "n_scored": int(len(X)),
        "target": target,
        "smearing_applied": smearing_factors is not None,
        "high_value_expert_applied": high_value_model is not None,
        "predictions_preview": [round(float(p), 2) for p in preds[:5]],
    }
    has_target = y_raw is not None and bool(np.isfinite(y_raw).any())
    if has_target:
        valid = np.isfinite(y_raw)
        metrics = _metrics_with_bias(y_raw[valid], general[valid])
        if mixture is not None:
            metrics["mixture"] = _metrics_with_bias(y_raw[valid], mixture[valid])
        results["n_with_target"] = int(valid.sum())
        results["metrics"] = metrics
        if log is not None:
            log.info(
                f"Loaded model ({args.model_in}) scored {len(X)} row(s), {int(valid.sum())} with "
                f"a usable target: MAE=£{metrics['mae']:,.0f} RMSE=£{metrics['rmse']:,.0f} "
                f"R2={metrics['r2']:.3f} MAPE={metrics['mape']:.1%}"
            )
    else:
        if log is not None:
            log.info(
                f"Loaded model ({args.model_in}) scored {len(X)} row(s); no usable target present "
                "to compute metrics against"
            )
    return results


def _expanding_window_year_folds_lazy(
    df, log: Optional[logging.Logger] = None, start_year: Optional[int] = None
) -> Iterator[Tuple[int, pd.DataFrame, pd.DataFrame]]:
    """
    Yield `hpp_rf.expanding_window_year_folds`' folds one at a time, using slices when sorted.

    Folds testing a year before `start_year` are skipped; each remaining
    fold still trains on every earlier year.
    """
    fold_years = [
        test_year
        for test_year, _, _ in expanding_window_year_folds(df[["sale_year"]], log=log)
    ]
    if start_year is not None:
        skipped = [year for year in fold_years if year < start_year]
        fold_years = [year for year in fold_years if year >= start_year]
        if skipped and log is not None:
            log.info(
                f"[backtest] --backtest-start-year {start_year}: skipping {len(skipped)} fold(s) "
                f"testing {skipped[0]}-{skipped[-1]}"
            )
    gc.collect()
    sale_year = df["sale_year"].to_numpy()
    sorted_by_year = (
        bool(np.all(sale_year[:-1] <= sale_year[1:])) if len(sale_year) > 1 else True
    )
    for test_year in fold_years:
        if sorted_by_year:
            train_end = int(np.searchsorted(sale_year, test_year, side="left"))
            test_end = int(np.searchsorted(sale_year, test_year, side="right"))
            yield test_year, df.iloc[:train_end], df.iloc[train_end:test_end]
        else:
            yield test_year, df[sale_year < test_year], df[sale_year == test_year]


EXTRA_BACKTEST_METRIC_NAMES = ("medae_relative", "mdape", "within_10pct")

DEFAULT_RECENT_FOLDS_START_YEAR = 2015


def _aggregate_backtest_metrics(
    fold_reports: List[Dict], split_name: str = "test"
) -> Dict[str, Dict[str, float]]:
    """
    Aggregate backtest metrics across folds, including relative median error.

    Returns ``{}`` for no folds.
    """
    if not fold_reports:
        return {}
    aggregate = aggregate_fold_metrics(fold_reports, split_name=split_name)
    n_key = f"n_{split_name}"
    weights = np.array([fold[n_key] for fold in fold_reports], dtype=float)
    for metric in EXTRA_BACKTEST_METRIC_NAMES:
        values = np.array(
            [fold[split_name][metric] for fold in fold_reports], dtype=float
        )
        aggregate[metric] = {
            "weighted_mean": float(np.average(values, weights=weights)),
            "std": float(np.std(values)),
            "min": float(values.min()),
            "max": float(values.max()),
        }
    return aggregate


def _scoped_fold_reports(fold_reports: List[Dict]) -> List[Dict]:
    """Return fold reports whose ``test`` block is each fold's ``scoped`` metrics, for aggregation."""
    return [
        {"n_test": fold["test"]["scoped"]["n"], "test": fold["test"]["scoped"]}
        for fold in fold_reports
        if "mae" in fold["test"].get("scoped", {})
    ]


def _fold_memory_mib(frame: pd.DataFrame) -> int:
    """
    Return the RAM one concurrent fold is planned to need: four copies of `frame`.

    A fold holds its train and test slices, the feature frames
    `prepare_features` builds from them and the CatBoost Pool; four times
    the engineered frame bounds that for the largest fold.
    """
    return max(1, int(4 * frame.memory_usage(index=True, deep=False).sum() / hardware.MIB))


def _map_folds(fn, jobs: Sequence, args: argparse.Namespace, frame: pd.DataFrame, log: Optional[logging.Logger] = None) -> List:
    """
    Return ``fn(job, fold_args)`` for every job, in order, up to the plan's fold concurrency at a time.

    The concurrency is `hardware.ResourcePlan.fold_concurrency` re-derived
    with `_fold_memory_mib` of `frame` (``--fold-concurrency`` overrides
    it). Concurrent folds run in threads -- CatBoost releases the GIL while
    it trains -- each with its own copy of `args` carrying
    `hardware.ResourcePlan.for_worker`: one GPU each, or an equal share of
    the CPU threads. CatBoost's results do not depend on its thread count, so
    a fold scores the same however many run beside it.
    """
    if not jobs:
        return []
    plan = resource_plan(args, log, fold_memory_mib=_fold_memory_mib(frame))
    workers = max(1, min(plan.fold_concurrency, len(jobs)))
    if workers == 1:
        return [fn(job, args) for job in jobs]
    if log is not None:
        log.info(f"Running {len(jobs)} folds {workers} at a time ({plan.describe()})")

    def _worker_args(worker: int) -> argparse.Namespace:
        fold_args = copy.copy(args)
        fold_args.resource_plan = plan.for_worker(worker)
        return fold_args

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hpp_cb-fold") as pool:
        futures = [pool.submit(fn, job, _worker_args(i % workers)) for i, job in enumerate(jobs)]
        return [future.result() for future in futures]


def backtest_expanding_window(
    df,
    args: argparse.Namespace,
    mortgage_df: pd.DataFrame,
    holdout_months: int = 12,
    log: Optional[logging.Logger] = None,
    ukhpi_df: Optional[pd.DataFrame] = None,
    engineered: bool = False,
) -> Dict:
    """
    Backtest CatBoost with expanding annual folds; see `hpp_rf.backtest_expanding_window`.

    Folds run `fold_concurrency` at a time (`_map_folds`); with
    ``--fold-refit final-only`` each is scored by its early-stopped pass-1
    model and only the final holdout fit is refitted. With `engineered`, `df`
    is already `_engineer_training_frame`'s output.
    """
    if not engineered:
        df = _engineer_training_frame(
            df, mortgage_df, ukhpi_df, log=log, market_features=getattr(args, "market_features", True),
            local_index=getattr(args, "local_index", None),
        )
    development_df, holdout_df = reserve_final_holdout_period(df, months=holdout_months)

    del df
    gc.collect()

    jobs = []
    for test_year, train_fold, test_fold in _expanding_window_year_folds_lazy(
        development_df, log=log, start_year=getattr(args, "backtest_start_year", None)
    ):
        train_from_year = getattr(args, "train_from_year", None)
        if train_from_year is not None:
            fold_sale_dates = pd.to_datetime(train_fold[DATE_COLUMN])
            if not (
                fold_sale_dates
                >= pd.Timestamp(year=int(train_from_year), month=1, day=1)
            ).any():
                if log is not None:
                    log.info(
                        f"[backtest] skipping test year {test_year} entirely: --train-from-year "
                        f"{train_from_year} would drop every training row (this fold's train side "
                        f"ends {fold_sale_dates.max()}, before {train_from_year})"
                    )
                continue
        excluded = len(train_fold) < args.backtest_min_train_rows
        if excluded and log is not None:
            log.info(
                f"[backtest] skipping test year {test_year} from the headline aggregate: "
                f"only {len(train_fold)} train rows (< --backtest-min-train-rows={args.backtest_min_train_rows})"
            )
        jobs.append((test_year, train_fold, test_fold, excluded))

    def _fold(job, fold_args):
        test_year, train_fold, test_fold, excluded = job
        gc.collect()
        with profiling.stage(f"backtest/fold {test_year}", rows=len(train_fold)):
            *_, fold_results, _ = _fit_and_evaluate(
                train_fold,
                test_fold,
                fold_args,
                log=_fold_log(log),
                include_price_bands=True,
                is_final_fit=False,
            )
        fold_results["test_year"] = test_year
        if not excluded and log is not None:
            log.info(
                f"[backtest] train-through-{test_year - 1} -> test {test_year}: "
                f"MAE=£{fold_results['test']['mae']:,.0f} MedAE=£{fold_results['test']['medae']:,.0f} "
                f"RMSE=£{fold_results['test']['rmse']:,.0f} MAPE={fold_results['test']['mape']:.1%}"
            )
        return fold_results

    fold_reports = []
    fold_reports_excluded = []
    for job, fold_results in zip(jobs, _map_folds(_fold, jobs, args, development_df, log)):
        (fold_reports_excluded if job[3] else fold_reports).append(fold_results)
    del jobs

    all_metric_names = BACKTEST_METRIC_NAMES + EXTRA_BACKTEST_METRIC_NAMES

    aggregate = _aggregate_backtest_metrics(fold_reports, split_name="test")
    if log is not None:
        if fold_reports:
            summary = ", ".join(
                f"{metric}={aggregate[metric]['weighted_mean']:,.3f}±{aggregate[metric]['std']:,.3f}"
                for metric in all_metric_names
            )
            log.info(f"[backtest] across {len(fold_reports)} annual folds: {summary}")
        else:
            log.info("[backtest] no annual folds -- no fold to aggregate")

    recent_folds_start_year = getattr(
        args, "recent_folds_start_year", DEFAULT_RECENT_FOLDS_START_YEAR
    )
    recent_fold_reports = [
        f for f in fold_reports if f["test_year"] >= recent_folds_start_year
    ]
    aggregate_recent_folds = _aggregate_backtest_metrics(
        recent_fold_reports, split_name="test"
    )
    if log is not None:
        if recent_fold_reports:
            summary_recent = ", ".join(
                f"{metric}={aggregate_recent_folds[metric]['weighted_mean']:,.3f}"
                f"±{aggregate_recent_folds[metric]['std']:,.3f}"
                for metric in all_metric_names
            )
            log.info(
                f"[backtest] across {len(recent_fold_reports)} annual folds, test year "
                f"{recent_folds_start_year}+ (recent-folds subset): {summary_recent}"
            )
        else:
            log.info(
                f"[backtest] recent-folds subset (test year {recent_folds_start_year}+) is empty -- "
                "no fold to aggregate (a short --limit smoke run?)"
            )

    aggregate_scoped = _aggregate_backtest_metrics(_scoped_fold_reports(fold_reports))
    aggregate_recent_folds_scoped = _aggregate_backtest_metrics(
        _scoped_fold_reports(recent_fold_reports)
    )
    if log is not None:
        for label, block in (
            ("scoped, all folds", aggregate_scoped),
            (f"scoped, test year {recent_folds_start_year}+", aggregate_recent_folds_scoped),
        ):
            if block:
                log.info(
                    f"[backtest] {label}: "
                    + ", ".join(
                        f"{metric}={block[metric]['weighted_mean']:,.3f}±{block[metric]['std']:,.3f}"
                        for metric in all_metric_names
                    )
                )

    aggregate_including_excluded_folds = _aggregate_backtest_metrics(
        fold_reports + fold_reports_excluded, split_name="test"
    )
    if log is not None and fold_reports_excluded:
        summary_all = ", ".join(
            f"{metric}={aggregate_including_excluded_folds[metric]['weighted_mean']:,.3f}"
            f"±{aggregate_including_excluded_folds[metric]['std']:,.3f}"
            for metric in all_metric_names
        )
        log.info(
            f"[backtest] across all {len(fold_reports) + len(fold_reports_excluded)} annual folds "
            f"(including {len(fold_reports_excluded)} excluded from the headline aggregate): {summary_all}"
        )

    with profiling.stage("backtest/final_holdout", rows=len(development_df)):
        (
            final_model,
            final_medians,
            final_postcode_stats,
            final_sector_stats,
            final_holdout_results,
            final_artifacts,
        ) = _fit_and_evaluate(
            development_df,
            holdout_df,
            args,
            log=log,
            include_price_bands=True,
            fatal_if_truncated=not getattr(args, "allow_truncated_holdout", False),
            report_plausibility_filtered=True,
        )
    # The final model is fitted on everything before the holdout; recording
    # that span lets `hpp_inference evaluate` refuse an in-sample window.
    development_dates = pd.to_datetime(development_df[DATE_COLUMN])
    final_holdout_results["train_period"] = [
        str(development_dates.min().date()),
        str(development_dates.max().date()),
    ]
    _save_model_if_requested(
        final_model,
        final_medians,
        final_postcode_stats,
        final_sector_stats,
        final_holdout_results,
        args,
        log=log,
        artifacts=final_artifacts,
    )
    if log is not None:
        log.info(
            f"[backtest] final {holdout_months}-month untouched holdout ({len(holdout_df)} rows): "
            f"MAE=£{final_holdout_results['test']['mae']:,.0f} MedAE=£{final_holdout_results['test']['medae']:,.0f}"
        )
        if "mixture" in final_holdout_results["test"]:
            mixture = final_holdout_results["test"]["mixture"]
            log.info(
                f"[backtest] final holdout, two-stage mixture: "
                f"MAE=£{mixture['mae']:,.0f} MedAE=£{mixture['medae']:,.0f} "
                f"RMSE=£{mixture['rmse']:,.0f}"
            )
        if "plausibility_filtered" in final_holdout_results["test"]:
            pf = final_holdout_results["test"]["plausibility_filtered"]
            log.info(
                f"[backtest] final holdout, plausibility_filtered ({pf['n_excluded']} row(s) above "
                f"£{PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD:,.0f}/m² excluded): "
                f"MAE=£{pf['mae']:,.0f} MedAE=£{pf['medae']:,.0f} RMSE=£{pf['rmse']:,.0f}"
            )
        scoped = final_holdout_results["test"].get("scoped", {})
        if "mae" in scoped:
            log.info(
                f"[backtest] final holdout, scoped ({scoped['n_excluded']} excluded): "
                f"{_format_headline(scoped)}"
            )
    return {
        "folds": fold_reports,
        "folds_excluded": fold_reports_excluded,
        "aggregate": aggregate,
        "aggregate_recent_folds": aggregate_recent_folds,
        "recent_folds_start_year": recent_folds_start_year,
        "aggregate_including_excluded_folds": aggregate_including_excluded_folds,
        "aggregate_scoped": aggregate_scoped,
        "aggregate_recent_folds_scoped": aggregate_recent_folds_scoped,
        "final_holdout": final_holdout_results,
    }


def evaluate_spatial_generalisation(
    df,
    args: argparse.Namespace,
    mortgage_df: pd.DataFrame,
    group_col: str = "postcode_sector",
    new_area_frac: float = 0.15,
    holdout_months: int = 12,
    log: Optional[logging.Logger] = None,
    ukhpi_df: Optional[pd.DataFrame] = None,
    engineered: bool = False,
) -> Dict:
    """
    Evaluate CatBoost on known and new areas; see `hpp_rf.evaluate_spatial_generalisation`.

    With `engineered`, `df` is already `_engineer_training_frame`'s output.
    """
    if not engineered:
        df = _engineer_training_frame(
            df, mortgage_df, ukhpi_df, log=log, market_features=getattr(args, "market_features", True),
            local_index=getattr(args, "local_index", None),
        )
    development_df, test_period_df = reserve_final_holdout_period(
        df, months=holdout_months
    )
    train_spatial, known_test, new_test, holdout_areas = spatial_new_area_holdout(
        development_df,
        test_period_df,
        group_col=group_col,
        new_area_frac=new_area_frac,
        random_state=args.random_state,
        log=log,
    )
    with profiling.stage("fit"):
        model, medians, postcode_stats, sector_stats, results, artifacts = _fit_and_evaluate(
            train_spatial,
            new_test,
            args,
            log=log,
            include_price_bands=True,
            val_df=known_test,
        )
    spatial_dates = pd.to_datetime(train_spatial[DATE_COLUMN])
    results["train_period"] = [str(spatial_dates.min().date()), str(spatial_dates.max().date())]
    _save_model_if_requested(
        model, medians, postcode_stats, sector_stats, results, args, log=log,
        artifacts=artifacts,
    )
    results["group_col"] = group_col
    results["holdout_areas"] = sorted(str(area) for area in holdout_areas)
    results["known_area"] = results.pop("val")
    results["new_area"] = results.pop("test")
    return results


def fit_high_value_expert(
    X_train: pd.DataFrame,
    y_train_raw: np.ndarray,
    y_train: np.ndarray,
    cat_features: List[str],
    args: argparse.Namespace,
    high_value_threshold: float = DEFAULT_HIGH_VALUE_THRESHOLD,
    sample_weight: Optional[np.ndarray] = None,
    log: Optional[logging.Logger] = None,
):
    """
    Fit a CatBoost expert on training rows priced at or above `high_value_threshold`.

    Reuses the general model's prepared features and target transform.
    """
    from catboost import CatBoostRegressor, Pool

    mask = np.asarray(y_train_raw) >= high_value_threshold
    n = int(mask.sum())
    if n < MIN_HIGH_VALUE_TRAIN_ROWS:
        if log is not None:
            log.info(
                f"two-stage mixture: only {n} train rows >= £{high_value_threshold:,.0f} "
                f"(< MIN_HIGH_VALUE_TRAIN_ROWS={MIN_HIGH_VALUE_TRAIN_ROWS}); skipping the high-value expert"
            )
        return None

    high_value_depth = getattr(args, "high_value_depth", None)
    high_value_l2_leaf_reg = getattr(args, "high_value_l2_leaf_reg", None)
    cb_kwargs, _ = _catboost_kwargs(args, None)
    cb_kwargs["depth"] = (
        high_value_depth if high_value_depth is not None else args.depth
    )
    cb_kwargs["l2_leaf_reg"] = (
        high_value_l2_leaf_reg
        if high_value_l2_leaf_reg is not None
        else args.l2_leaf_reg
    )
    cb_kwargs["early_stopping_rounds"] = args.early_stopping_rounds

    X_hv = X_train.loc[mask]
    y_hv = y_train[mask]
    if sample_weight is None:
        w_hv = _sample_weight_for_training(
            np.asarray(y_train_raw)[mask], mode=args.sample_weight
        )
    else:
        w_hv = np.asarray(sample_weight, dtype=np.float64)[mask]
    hv_eval_mask = _carve_eval_mask(
        n, frac=args.eval_frac, min_rows=min(10, max(1, n // 5))
    )
    train_pool = Pool(
        X_hv.iloc[~hv_eval_mask],
        y_hv[~hv_eval_mask],
        cat_features=cat_features,
        weight=w_hv[~hv_eval_mask],
    )
    eval_pool = Pool(
        X_hv.iloc[hv_eval_mask],
        y_hv[hv_eval_mask],
        cat_features=cat_features,
        weight=w_hv[hv_eval_mask],
    )
    model = CatBoostRegressor(**cb_kwargs)
    model.fit(train_pool, eval_set=eval_pool, use_best_model=True)
    _warn_if_iterations_truncated(
        model,
        args.iterations,
        args.early_stopping_rounds,
        log,
        context="fit_high_value_expert",
    )
    if log is not None:
        log.info(
            f"two-stage mixture: fit high-value expert on {n} train rows >= £{high_value_threshold:,.0f} "
            f"({int((~hv_eval_mask).sum())} fit / {int(hv_eval_mask.sum())} eval, "
            f"best_iteration={model.get_best_iteration()})"
        )
    return model


def evaluate_two_stage_mixture(*_args, **_kwargs):
    """
    Raise; the two-stage mixture now lives in `_fit_and_evaluate`.

    ``--two-stage`` modifies any evaluation protocol.

    Raises
    ------
    NotImplementedError
        Always.
    """
    raise NotImplementedError(
        "hpp_cb.evaluate_two_stage_mixture was removed by C1: --two-stage is now a "
        "modifier on the selected evaluation protocol, handled inside _fit_and_evaluate. "
        "Call _fit_and_evaluate (or run_training/backtest_expanding_window/"
        "evaluate_spatial_generalisation) with args.two_stage set instead."
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Train CatBoost to predict London house prices from production.london_houses."
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "gpu", "auto"],
        default="auto",
        help="Where to train (default: auto-detect)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help="Max boosting rounds (early stopping usually cuts this short)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=DEFAULT_LEARNING_RATE,
        help=(
            f"Boosting learning rate (default {DEFAULT_LEARNING_RATE}); pass 0 for CatBoost's "
            "size-based auto rate, which pass 2 then inherits from pass 1"
        ),
    )
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--l2-leaf-reg", type=float, default=3.0)
    parser.add_argument(
        "--early-stopping-rounds", type=int, default=DEFAULT_EARLY_STOPPING_ROUNDS
    )
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument(
        "--thread-count",
        type=int,
        default=None,
        help="CPU threads per fit (default: the resource plan's -- the CPUs this process may use, shared among concurrent folds)",
    )
    parser.add_argument("--train-frac", type=float, default=0.7)
    parser.add_argument("--val-frac", type=float, default=0.15)
    parser.add_argument(
        "--split-method",
        choices=[
            "chronological",
            "grouped",
            "purged",
            "walk_forward",
            "spatial",
            "recency",
        ],
        default="chronological",
        help=(
            "How to split train/val/test (default: chronological). Same methods, "
            "same semantics, as hpp_rf --split-method — see hpp_rf.split_dataset."
        ),
    )
    parser.add_argument(
        "--embargo-days", type=int, default=30, help="--split-method purged only"
    )
    parser.add_argument(
        "--cv-folds", type=int, default=4, help="--split-method walk_forward only"
    )
    parser.add_argument(
        "--spatial-test-frac",
        type=float,
        default=0.15,
        help="--split-method spatial only",
    )
    parser.add_argument(
        "--spatial-val-frac",
        type=float,
        default=0.15,
        help="--split-method spatial only",
    )
    parser.add_argument(
        "--train-window-years",
        type=float,
        default=5.0,
        help="--split-method recency only",
    )
    parser.add_argument(
        "--target",
        choices=["price", "log_price", "deflated", "anchored"],
        default="deflated",
        help=(
            "target transform (default = deflated)"
            "deflated trains on log1p(price) minus log(ukhpi_avg_price_at_sale), i.e. a property's value "
            "RELATIVE to the market at its own sale date; 'anchored' on log1p(price) minus the log of the "
            "home's anchor value (its size-matched comparables' value, hpp_rf.ANCHOR_SOURCE_COLUMNS), a "
            "scale-free correction to it; 'log_price' trains on log1p(price); 'price' on raw price."
        ),
    )
    parser.add_argument(
        "--population",
        choices=list(POPULATIONS),
        default=DEFAULT_POPULATION,
        help=(
            "Which sales to model (default: epc_matched, the sales with a usable pre-sale EPC). "
            "'all' adds ~1M mostly pre-2009 sales without floor area or rating: the EPC core "
            "features become optional per training row (reported, not failed; an epc_missing "
            "flag is added; --nan-mode defaults to native) but stay mandatory in the val/test "
            "splits and the saved model"
        ),
    )
    parser.add_argument(
        "--train-from-year",
        type=int,
        default=None,
        help=(
            "drop training rows that sold before this calendar year (default: no rows dropped)"
        ),
    )
    parser.add_argument(
        "--test-year",
        type=int,
        default=None,
        help=(
            "restrict test/holdout rows to this calendar year only, e.g. --test-year 2026 "
            "to score a still-partial year on its own (default: no rows dropped)"
        ),
    )
    parser.add_argument(
        "--recency-halflife-years",
        type=float,
        default=None,
        help=(
            "Exponential decay weight on each training row's dateoftransfer, "
            "halving every N years back from the most recent training sale, multiplied "
            "into whatever --sample-weight produces (default: unset = no time weighting). "
        ),
    )
    parser.add_argument(
        "--allow-truncated-holdout",
        dest="allow_truncated_holdout",
        action="store_true",
        default=False,
        help=(
            "Downgrade the iteration-cap-truncation check on the FINAL untouched holdout fit from a hard "
            "failure back to a warning (default = off)"
        ),
    )
    parser.add_argument(
        "--availability-shift-limit",
        type=float,
        default=DEFAULT_AVAILABILITY_SHIFT_LIMIT,
        help=(
            "Drop any feature or missing flag whose "
            "MISSINGNESS RATE differs between the training rows and the rows being scored by "
            "more than this fraction (default 0.5 = 50 percentage points; pass a value above "
            "1.0 to disable). Reads availability only -- never the target, never a value -- so "
            "it is exactly as computable at deploy time as it is here"
        ),
    )
    parser.add_argument(
        "--eval-frac",
        type=float,
        default=0.1,
        help=(
            "Share of each fit's OWN train_df, chronologically "
            "last, carved out as CatBoost's early-stopping eval_set (never val_df/test_df -- "
            "see _fit_and_evaluate/_carve_eval_mask). The fix for the doc's central finding: "
            "early stopping and the final holdout were both being fitted on the exact split "
            "they were then scored against."
        ),
    )
    parser.add_argument(
        "--sample-weight",
        choices=["none", "sqrt", "full"],
        default="none",
        help=(
            "inverse-price-band training weight (default = none, fits unweighted model)"
        ),
    )
    parser.add_argument(
        "--smearing-correction",
        dest="smearing_correction",
        action="store_true",
        default=False,
        help=(
            "Apply Duan's smearing factor (per PRICE_BAND_LABELS "
            "band, smoothed by log-price interpolation across band edges) to correct "
            "downward bias on a log-target model's predictions (default: off) "
        ),
    )
    parser.add_argument(
        "--no-smearing-correction", dest="smearing_correction", action="store_false"
    )
    parser.add_argument(
        "--nan-mode",
        choices=["impute", "native"],
        default=None,
        help=(
            "Default: native under --population all, impute otherwise. 'impute' "
            "fills every nullable numeric + a missing flag per family; 'native' leaves numeric "
            'NaN untouched for CatBoost\'s own nan_mode="Min" per-split handling, keeping only '
            "prev_sale_missing/comp_missing flags (see prepare_features)."
        ),
    )
    parser.add_argument(
        "--drop-features",
        type=lambda s: [c.strip() for c in s.split(",") if c.strip()],
        default=[],
        help=(
            "Comma-separated column/flag names to "
            "exclude from the feature matrix, for A/B testing the doc's ranked removal list "
            "against --backtest one family at a time -- see DROP_FEATURES_PRESETS above for "
            "the doc's own ready-to-run values, e.g. "
            "--drop-features rate_2y_75ltv_at_sale,rate_2y_75ltv_lag_3m,rate_2y_75ltv_lag_6m,rate_2y_75ltv_lag_12m (F1). "
        ),
    )
    parser.add_argument(
        "--loss-function",
        choices=["RMSE", "MAE", "Huber"],
        default="RMSE",
        help=(
            "CatBoost's training loss (default: RMSE) "
            "RMSE-on-log is dominated by the £1m+ tail's few catastrophic misses "
            "(70%% of squared error from 2.6%% of rows in the doc's evidence log); 'MAE' or "
            "'Huber' (see --huber-delta) cap that leverage, typically improving MedAE/MAPE at "
            "a small RMSE cost."
        ),
    )
    parser.add_argument(
        "--huber-delta", type=float, default=1.0, help="--loss-function Huber only"
    )
    parser.add_argument(
        "--max-ctr-complexity",
        type=int,
        default=None,
        help=("CatBoost default when unset"),
    )
    parser.add_argument(
        "--one-hot-max-size",
        type=int,
        default=None,
        help="CatBoost's one_hot_max_size; CatBoost default when unset; see --max-ctr-complexity",
    )
    plan = parser.add_argument_group(
        "resource plan",
        "Derived from the host by common.hardware.plan_resources; each flag overrides one value of it",
    )
    plan.add_argument("--devices", default=None, help="CatBoost GPU devices, e.g. 0 or 0:1 (default: every usable GPU)")
    plan.add_argument("--gpu-ram-part", type=float, default=None, help="CatBoost gpu_ram_part (default: from free GPU memory)")
    plan.add_argument(
        "--pinned-memory-size", type=int, default=None, help="CatBoost pinned_memory_size in bytes (default: RAM / 16)"
    )
    plan.add_argument("--used-ram-limit", default=None, help="CatBoost CPU used_ram_limit, e.g. 12gb (default: from RAM)")
    plan.add_argument(
        "--fold-concurrency",
        default="auto",
        help="Folds fitted at once (default auto: one per GPU, or one per 8 CPU threads, capped by RAM)",
    )
    plan.add_argument("--chunk-rows", type=int, default=None, help="Rows per CuPy streaming-executor chunk")
    parser.add_argument(
        "--fold-refit",
        choices=["full", "final-only"],
        default="full",
        help=(
            "full (default): every backtest/walk-forward fold is refitted on all its training rows after "
            "early stopping, like the final model. final-only: folds score the early-stopped pass-1 model "
            "and only the final holdout model is refitted -- about half the fold fitting; adopt it only "
            "if fit_gate passes (scoped MdAPE within +0.1 pp)"
        ),
    )
    parser.add_argument(
        "--quantize-once",
        action="store_true",
        default=False,
        help=(
            "Quantise each fit's training Pool once and share the borders between pass 1 and pass 2 "
            "(pass 1 then uses borders from every training row; gated like --fold-refit)"
        ),
    )
    parser.add_argument(
        "--feature-cache",
        choices=["on", "off", "refresh"],
        default="on",
        help=(
            "Read/write the engineered frame in tmp/cache/features (scripts.feature_cache), keyed by the "
            "alembic revision, max ppd_id, row count and the feature code. refresh rebuilds it"
        ),
    )
    parser.add_argument(
        "--profile-json",
        type=Path,
        default=None,
        help=(
            "Write stage timings (wall, peak RSS, peak VRAM), the resource plan and the engineered "
            "frame's fingerprint to this JSON file (common.profiling)"
        ),
    )
    parser.add_argument("--results-json", type=Path, default=None, help="Also write the results JSON to this file")
    parser.add_argument(
        "--backtest-min-train-rows",
        type=int,
        default=25_000,
        help=(
            "folds whose train side has fewer "
            "rows than this are excluded from the headline aggregate "
        ),
    )
    parser.add_argument(
        "--backtest-start-year",
        type=int,
        default=None,
        help=(
            "--backtest only: skip the annual folds testing years before this one (default: "
            "every fold). The final holdout is unaffected; e.g. 2016 for an iteration run"
        ),
    )
    parser.add_argument(
        "--recent-folds-start-year",
        type=int,
        default=DEFAULT_RECENT_FOLDS_START_YEAR,
        help=(
            "in addition to the "
            "headline aggregate across every annual fold, also report one across folds whose "
            "test year is at/after this year (default: 2015). The all-fold aggregate's mae/"
            "rmse/medae are £-denominated and move with London's price level across three "
            "decades of folds (1996's ~£75k median home vs. 2025's ~£530k) regardless of what "
            "changed in the model -- this narrower, price-comparable window plus the "
            "medae_relative/mape columns (EXTRA_BACKTEST_METRIC_NAMES) are what a real change "
            "should be judged against. See _aggregate_backtest_metrics."
        ),
    )
    parser.add_argument(
        "--feature-dtype",
        choices=["float32", "float64"],
        default=DEFAULT_FEATURE_FLOAT_DTYPE,
        help=(
            "the float width every numeric FEATURE is carried at, from "
            "load_dataset through to the CatBoost Pool. 'float32' (default) halves the "
            "dataset and halves every copy of it the pipeline makes -- and costs nothing "
            "a model can see, because CatBoost stores float features as float32 "
            "internally and then quantises them to at most 254 split borders either way. "
            "The target price and the 64-bit identifiers (ppd_id/address_id/uprn) are "
            "never downcast, so every reported metric is still computed in full "
            "precision -- see EXACT_NUMERIC_COLUMNS. 'float64' restores the pre-"
            "optimisation dtypes exactly, for a bit-identical comparison against an "
            "older run's numbers, at roughly double the memory."
        ),
    )
    parser.add_argument(
        "--load-chunk-rows",
        type=int,
        default=DEFAULT_LOAD_CHUNK_ROWS,
        help=(
            "rows per server-side-cursor batch in load_dataset. Lower it if "
            "the load itself is the memory constraint; it does not affect the loaded frame."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of rows loaded, for a quick smoke run",
    )
    parser.add_argument(
        "--model-out",
        type=Path,
        default=None,
        help=(
            "Bundle directory to save the trained model in (models.artifact): canonical model.cbm, "
            "meta.json preprocessing state, probe rows and a signed manifest"
        ),
    )
    parser.add_argument(
        "--model-in",
        type=Path,
        default=None,
        help=(
            "Model bundle previously saved with --model-out. Alone (no --model-out), it is "
            "verified and loaded via load_model, and scores the loaded "
            "dataset with it instead of training a new model -- see run_prediction; cannot be "
            "combined with --model-out/--backtest/--spatial-generalisation/--two-stage in that "
            "case. Combined WITH --model-out, training continues from this model's trees (under "
            "whichever of --backtest/--spatial-generalisation/--two-stage/a plain split is "
            "selected) on either --device: CPU via CatBoost's own init_model, GPU via a baseline "
            "warm start (CatBoost's GPU trainer has no init_model support), and the resulting "
            "model -- the loaded one plus the new run's rounds -- is written to --model-out with "
            "its own fresh meta.json. See _fit_and_evaluate."
        ),
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run expanding-window annual backtest instead of a single split — see backtest_expanding_window",
    )
    parser.add_argument(
        "--spatial-generalisation",
        dest="spatial_generalisation",
        action="store_true",
        help="Run known-area vs. new-area generalisation check instead of a single split",
    )
    parser.add_argument(
        "--spatial-group-col",
        choices=ROLLING_COMP_GROUP_COLUMNS,
        default="postcode_sector",
        help="--spatial-generalisation only",
    )
    parser.add_argument(
        "--new-area-frac",
        type=float,
        default=0.15,
        help="--spatial-generalisation only",
    )
    parser.add_argument(
        "--holdout-months",
        type=int,
        default=12,
        help="--backtest/--spatial-generalisation only: months reserved as the untouched final period (6-12 recommended)",
    )
    parser.add_argument(
        "--two-stage",
        dest="two_stage",
        action="store_true",
        help=(
            "Fit a dedicated high-value expert alongside the general model and report "
            "general-vs-mixture metrics for every split. "
        ),
    )
    parser.add_argument(
        "--high-value-threshold",
        type=float,
        default=DEFAULT_HIGH_VALUE_THRESHOLD,
        help="--two-stage only: price at/above which the high-value expert is trained/routed to (default £2m)",
    )
    parser.add_argument(
        "--high-value-depth",
        type=int,
        default=None,
        help="--two-stage only: tree depth for the high-value expert (default: same as --depth)",
    )
    parser.add_argument(
        "--high-value-l2-leaf-reg",
        type=float,
        default=None,
        help="--two-stage only: l2_leaf_reg for the high-value expert (default: same as --l2-leaf-reg)",
    )
    parser.add_argument(
        "--no-iod",
        dest="use_iod",
        action="store_false",
        default=True,
        help=(
            "Drop every production.iod_features column from the feature matrix "
            "(migration 0011) -- the A/B control for measuring the IoD "
            "features' contribution"
        ),
    )
    parser.add_argument(
        "--no-ofsted",
        dest="use_ofsted",
        action="store_false",
        default=True,
        help=(
            "Drop the Ofsted school features (production.ofsted_features, migration "
            "0034) and their missing flag -- the A/B control for their contribution"
        ),
    )
    parser.add_argument(
        "--no-market-features",
        dest="market_features",
        action="store_false",
        default=True,
        help=(
            "Leave out the market-relative features (hpp_rf.MARKET_RELATIVE_COLUMNS) and keep the "
            "postcode/sector price statistics nominal -- the A/B control for plan section 1.3"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def training_cutoff(df, args: argparse.Namespace) -> pd.Timestamp:
    """
    Return the last sale date a run's model is fitted on.

    The day before the final holdout for ``--backtest`` and
    ``--spatial-generalisation`` (`reserve_final_holdout_period`'s cutoff); the
    ``--train-frac`` quantile of sale dates for a plain split; the latest sale
    when a saved model only scores.
    """
    dates = pd.to_datetime(df[DATE_COLUMN])
    latest = pd.Timestamp(dates.max())
    if args.model_in and not args.model_out:
        return latest
    if args.backtest or args.spatial_generalisation:
        return latest - pd.DateOffset(months=args.holdout_months)
    ordered = np.sort(dates.to_numpy())
    position = min(max(int(len(ordered) * args.train_frac) - 1, 0), len(ordered) - 1)
    return pd.Timestamp(ordered[position])


def _training_local_index(engine, df, args: argparse.Namespace, log: Optional[logging.Logger] = None):
    """
    Return the local price index a run may index its features with, or None if it cannot be built.

    Built by `local_index.build_as_of` from repeat-sale pairs whose second sale
    is on or before `training_cutoff`, so the holdout never moves a level the
    model learns from. Inference uses the evidence's own, fuller, index.
    (Annual backtest folds before the final holdout share this index, which
    has seen their test years' pairs; their fold metrics read slightly
    optimistic for these features.)
    """
    from hpldn.backend.scripts import local_index

    cutoff = training_cutoff(df, args)
    try:
        pairs, ukhpi = local_index.read_inputs(engine)
        return local_index.build_as_of(pairs, ukhpi, cutoff, log_=log, use_gpu=resource_plan(args, log).use_gpu)
    except Exception as exc:  # noqa: BLE001 -- the features fall back to not being index-moved
        if log is not None:
            log.error(f"Could not build the training-time local price index to {cutoff.date()} ({exc})")
        return None


def _validate_args(args: argparse.Namespace) -> None:
    """
    Reject incompatible flag combinations.

    ``--backtest`` and ``--spatial-generalisation`` are mutually exclusive
    protocols and ``--two-stage`` composes with either. ``--model-in`` alone
    scores and cannot combine with a protocol; with ``--model-out`` it
    warm-starts.

    Raises
    ------
    ValueError
        On an incompatible combination.
    """
    if getattr(args, "fold_concurrency", "auto") != "auto":
        try:
            if int(args.fold_concurrency) < 1:
                raise ValueError
        except ValueError:
            raise ValueError(f"--fold-concurrency must be 'auto' or a positive integer, got {args.fold_concurrency!r}")
    if args.backtest and args.spatial_generalisation:
        raise ValueError(
            "--backtest and --spatial-generalisation are alternative evaluation "
            "protocols and cannot be combined; run them separately. (--two-stage "
            "is a modifier and composes with either.)"
        )
    if args.model_in and not args.model_out:
        incompatible = [
            name
            for name, value in (
                ("--backtest", args.backtest),
                ("--spatial-generalisation", args.spatial_generalisation),
                ("--two-stage", args.two_stage),
            )
            if value
        ]
        if incompatible:
            raise ValueError(
                f"--model-in cannot be combined with {', '.join(incompatible)} unless --model-out "
                "is also given: with no --model-out, --model-in loads an already-trained model and "
                "only scores data with it (run_prediction) -- there is nowhere to write a newly "
                "trained model. Add --model-out to warm-start a new model from --model-in's trees "
                "and save the result, or drop --backtest/--spatial-generalisation/--two-stage to "
                "just score with the loaded model."
            )
    if args.target == "anchored" and not getattr(args, "market_features", True):
        raise ValueError(
            "--target anchored needs the anchor value hpp_rf.add_market_relative_features adds; "
            "drop --no-market-features"
        )
    if args.target in DEFLATOR_COLUMN_BY_TARGET and args.smearing_correction and args.two_stage:
        raise ValueError(
            f"--smearing-correction with --two-stage --target {args.target} is not supported: "
            "the smearing factors are fitted on the general model's own eval carve and "
            "have no calibration for the high-value expert's output. Drop one of the three."
        )


def _engineered_frame(engine, args: argparse.Namespace, log: Optional[logging.Logger] = None) -> pd.DataFrame:
    """
    Return the engineered training frame, from the `feature_cache` when it holds this run's key.

    On a miss (or ``--feature-cache refresh``) the dataset, mortgage rates
    and UK HPI are loaded, the training-cutoff local index is built and
    `_engineer_training_frame` runs; the result is saved unless
    ``--feature-cache off``.
    """
    from hpldn.backend.scripts import feature_cache

    mode = getattr(args, "feature_cache", "off")
    plan = resource_plan(args, log)
    key = description = None
    if mode != "off":
        with profiling.stage("feature_cache/key"):
            state = feature_cache.dataset_state(engine, args.population, args.limit)
            parameters = feature_cache.run_parameters(args, plan.use_gpu)
            code_hash = feature_cache.feature_code_hash()
            key = feature_cache.cache_key(state, parameters, code_hash)
            description = {"state": state, "parameters": parameters, "code": code_hash}
        if mode == "on":
            with profiling.stage("feature_cache/read"):
                cached = feature_cache.load(key, log=log)
            if cached is not None:
                profiling.note("feature_cache", {"key": key, "hit": True})
                return cached
    with profiling.stage("load", population=args.population):
        df = load_dataset(
            engine,
            limit=args.limit,
            log=log,
            float_dtype=args.feature_dtype,
            chunk_rows=args.load_chunk_rows,
            population=args.population,
        )
    with profiling.stage("load_side_tables"):
        mortgage_df = load_mortgage_rates(engine, log=log)
        ukhpi_df = load_ukhpi(engine, log=log)
    local_index = None
    if getattr(args, "market_features", True):
        with profiling.stage("local_index"):
            local_index = _training_local_index(engine, df, args, log)
    df = _engineer_training_frame(
        df, mortgage_df, ukhpi_df, log=log, market_features=getattr(args, "market_features", True),
        local_index=local_index,
    )
    if key is not None:
        with profiling.stage("feature_cache/write"):
            feature_cache.save(key, df, description, log=log)
        profiling.note("feature_cache", {"key": key, "hit": False})
    return df


def _headline(results: Mapping) -> Dict[str, object]:
    """Return the metrics a profile records beside its timings: the scoped test block of each mode."""
    out: Dict[str, object] = {}
    for name in ("aggregate_scoped", "aggregate_recent_folds_scoped"):
        if results.get(name):
            out[name] = {k: v.get("weighted_mean") for k, v in results[name].items() if isinstance(v, dict)}
    for name in ("test", "new_area", "known_area"):
        block = results.get(name)
        if isinstance(block, dict):
            out[name] = {k: block.get(k) for k in ("mae", "mape", "mdape", "within_10pct", "n")}
            if isinstance(block.get("scoped"), dict):
                out[f"{name}_scoped"] = {k: block["scoped"].get(k) for k in ("mae", "mape", "mdape", "within_10pct", "n")}
    holdout = results.get("final_holdout", {}).get("test")
    if isinstance(holdout, dict):
        out["final_holdout_scoped"] = {k: holdout.get("scoped", {}).get(k) for k in ("mae", "mape", "mdape", "within_10pct", "n")}
    return out


def main(argv: Optional[List[str]] = None) -> Dict:
    """Validate arguments, load data and run the selected mode, printing JSON."""
    args = build_arg_parser().parse_args(argv)
    _validate_args(args)
    log = get_logger(args.verbose)
    set_global_seed(args.random_state)
    resolve_nan_mode(args, log)
    profiler = profiling.reset_profiler()
    plan = resource_plan(args, log)
    profiler.enable_vram(plan.use_gpu)
    profiling.note("resource_plan", asdict(plan))
    profiling.note("argv", list(argv) if argv is not None else sys.argv[1:])
    from sqlalchemy import create_engine

    engine = create_engine(get_model_db_url(log))
    args.database = engine.url.database
    try:
        if args.model_in and not args.model_out:
            with profiling.stage("load", population=args.population):
                df = load_dataset(
                    engine,
                    limit=args.limit,
                    log=log,
                    float_dtype=args.feature_dtype,
                    chunk_rows=args.load_chunk_rows,
                    population=args.population,
                )
                mortgage_df = load_mortgage_rates(engine, log=log)
                ukhpi_df = load_ukhpi(engine, log=log)
            if getattr(args, "market_features", True):
                with profiling.stage("local_index"):
                    args.local_index = _training_local_index(engine, df, args, log)
            model, meta = load_model(args.model_in)
            with profiling.stage("predict"):
                results = run_prediction(df, args, mortgage_df, model, meta, log=log, ukhpi_df=ukhpi_df)
        else:
            df = _engineered_frame(engine, args, log)
            if args.profile_json is not None:
                with profiling.stage("fingerprint"):
                    profiling.note("engineered_frame", profiling.frame_fingerprint(df))
            dataset = [df]
            del df
            if args.backtest:
                results = backtest_expanding_window(
                    dataset.pop(), args, None, holdout_months=args.holdout_months, log=log, engineered=True
                )
            elif args.spatial_generalisation:
                results = evaluate_spatial_generalisation(
                    dataset.pop(),
                    args,
                    None,
                    group_col=args.spatial_group_col,
                    new_area_frac=args.new_area_frac,
                    holdout_months=args.holdout_months,
                    log=log,
                    engineered=True,
                )
            else:
                results = run_training(dataset.pop(), args, None, log=log, engineered=True)
        profiling.note("metrics", _headline(results))
    finally:
        if args.profile_json is not None:
            path = profiler.write_json(args.profile_json)
            log.info(f"Wrote the stage profile to {path}")
    payload = json.dumps(results, indent=2, default=str)
    if args.results_json is not None:
        args.results_json.parent.mkdir(parents=True, exist_ok=True)
        args.results_json.write_text(payload)
    print(payload)
    return results


if __name__ == "__main__":
    main()
