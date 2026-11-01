"""
Feature ablation for the house-price models in `hpp_rf` and `hpp_cb`.

Measures hold-out error with features present versus absent, complementing
SHAP attribution. Four strategies share one pipeline and log format:
``forward`` adds features one at a time down a SHAP ranking, ``loo`` removes
each feature from the full set, ``greedy`` adds whichever remaining
feature lowers validation RMSE most, and ``group`` removes a whole feature
group (`FEATURE_GROUPS`, e.g. the Ofsted school features) from every feature
the model trains on, needing no ranking. Each run is a slice of the matrix the
real model trains on, and deltas are computed from the log files on disk.
See ``scripts/README.md`` for the log format and how to read the results.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from hpldn.backend.common import hardware
from hpldn.backend.common.db import get_model_db_url
from hpldn.backend.common.utils import load_env_vars
from hpldn.backend.scripts import hpp_cb, hpp_rf
from hpldn.backend.scripts.data_visualization import FEATURE_MATRIX_COLUMNS

load_env_vars()
_ROOT = Path(os.environ.get("HPLDN_DIR_FP") or Path.cwd())
DEFAULT_IMPORTANCE_CSV = _ROOT / "tmp" / "csv" / "rf_shap_feature_importance.csv"
DEFAULT_LOG_DIR = _ROOT / "tmp" / "log"

BASELINE_LOG_NAME = "baseline_ablation.log"
RUN_LOG_TEMPLATE = "ablation_run_{n}.log"

IMPORTANCE_FEATURE_COLUMN = "feature"
IMPORTANCE_SCORE_COLUMN = "mean_abs_shap"

DEFAULT_BASELINE_FEATURES = 10
DEFAULT_MAX_ABLATION_FEATURES = 128

STRATEGY_FORWARD = "forward"
STRATEGY_LOO = "loo"
STRATEGY_GREEDY = "greedy"
STRATEGY_GROUP = "group"
STRATEGIES = (STRATEGY_FORWARD, STRATEGY_LOO, STRATEGY_GREEDY, STRATEGY_GROUP)

# Feature groups ``--strategy group`` removes whole: name -> matrix columns.
FEATURE_GROUPS: Dict[str, Tuple[str, ...]] = {
    "ofsted": tuple(hpp_rf.OFSTED_COLUMNS) + (hpp_rf.OFSTED_MISSING_FLAG,),
}

MODEL_RF = "rf"
MODEL_CATBOOST = "catboost"
MODELS = (MODEL_RF, MODEL_CATBOOST)

_MODEL_CLASS_NAMES = {
    MODEL_RF: "hpp_rf.RandomForestRegressor",
    MODEL_CATBOOST: "hpp_cb.CatBoostRegressor",
}

GROUP_BASELINE_LOG_NAME = "group_baseline.log"
GROUP_RUN_LOG_TEMPLATE = "group_run_{n}.log"
LOO_BASELINE_LOG_NAME = "loo_baseline.log"
LOO_RUN_LOG_TEMPLATE = "loo_run_{n}.log"
GREEDY_BASELINE_LOG_NAME = "greedy_baseline.log"
GREEDY_RUN_LOG_TEMPLATE = "greedy_run_{n}.log"


def _qualify_log_name(name: str, model: str) -> str:
    """
    Prefix a log filename with the model name, except for the Random Forest.

    Random Forest logs keep their original names, so a CatBoost sweep can share
    a ``--log-dir`` without overwriting them.
    """
    return name if model == MODEL_RF else f"{model}_{name}"


SPLIT_NAMES = ("val", "test")

METRIC_NAMES = ("rmse", "mae", "mape", "r2", "medae")

BAND_FIELD_NAMES = ("n", "rmse", "mae", "mape", "r2", "medae")

DELTA_METRIC_NAMES = ("rmse", "mae")

_SECTION_RE = re.compile(r"^\[([A-Za-z0-9_.]+)\]$")
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):\s*(.*)$")
_FEATURE_LINE_RE = re.compile(r"^(\d+):\s*(\S+)$")

_NONE_TOKEN = "none"


def get_logger(verbose: bool = False) -> logging.Logger:
    """Return a minimal stdout logger for sweep progress, separate from the run log files."""
    logger = logging.getLogger("model_ablation")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("{asctime} [{levelname}] {name}: {message}", style="{")
        )
        logger.addHandler(handler)
    return logger




def load_feature_ranking(
    csv_path: Path = DEFAULT_IMPORTANCE_CSV,
    known_features: Sequence[str] = FEATURE_MATRIX_COLUMNS,
) -> List[str]:
    """
    Return feature names from `csv_path`, most important first.

    Rows are re-sorted by `IMPORTANCE_SCORE_COLUMN` rather than trusting file
    order.

    Raises
    ------
    ValueError
        If a name is not in `known_features`, meaning the ranking was built
        for a different feature matrix.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Feature importance ranking not found at {csv_path} — generate it with "
            "hpldn.backend.scripts.data_visualization (its SHAP feature-importance chart)."
        )
    rows = []
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {IMPORTANCE_FEATURE_COLUMN, IMPORTANCE_SCORE_COLUMN} - set(
            reader.fieldnames or []
        )
        if missing:
            raise ValueError(f"{csv_path} is missing column(s) {sorted(missing)}")
        for row in reader:
            rows.append(
                (row[IMPORTANCE_FEATURE_COLUMN], float(row[IMPORTANCE_SCORE_COLUMN]))
            )

    known = set(known_features)
    unknown = [name for name, _ in rows if name not in known]
    if unknown:
        raise ValueError(
            f"{csv_path} ranks feature(s) {unknown} that are not columns of the resolved feature "
            "matrix (known_features — run_ablation passes prepared.feature_names, the actual "
            "model's own prepared columns, not a fixed constant; see prepare_splits) — regenerate "
            "the ranking against this model's current feature set."
        )
    duplicates = [name for name, _ in rows if [n for n, _ in rows].count(name) > 1]
    if duplicates:
        raise ValueError(
            f"{csv_path} ranks feature(s) {sorted(set(duplicates))} more than once"
        )

    return [name for name, _ in sorted(rows, key=lambda item: -item[1])]


@dataclass(frozen=True)
class RunPlan:
    """
    One run of a sweep: which log file to write and what goes in it.

    Attributes
    ----------
    number : int or None
        None for the baseline, otherwise the 1-based run number.
    added_feature : str or None
        The feature a forward or greedy run adds.
    removed_feature : str or None
        The feature a leave-one-out run drops.
    strategy : str
        One of `STRATEGIES`; defaults to ``"forward"``.
    model : str
        One of `MODELS`; defaults to ``"rf"``.
    """

    number: Optional[int]
    log_name: str
    features: Tuple[str, ...]
    added_feature: Optional[str]
    removed_feature: Optional[str] = None
    strategy: str = STRATEGY_FORWARD
    model: str = MODEL_RF

    @property
    def is_baseline(self) -> bool:
        """True for the baseline run."""
        return self.number is None

    @property
    def label(self) -> str:
        """The run's label, e.g. ``"baseline"``, ``"run_3"`` or ``"loo_run_3"``."""
        if self.strategy == STRATEGY_FORWARD:
            return "baseline" if self.is_baseline else f"run_{self.number}"
        return (
            f"{self.strategy}_baseline"
            if self.is_baseline
            else f"{self.strategy}_run_{self.number}"
        )


def _validate_sweep_bounds(
    ranking: Sequence[str], baseline_features: int, max_ablation_features: int
) -> None:
    """
    Check the bounds of a baseline-to-maximum feature climb.

    Shared by the forward and greedy strategies.

    Raises
    ------
    ValueError
        If the bounds are impossible for `ranking`.
    """
    if baseline_features < 1:
        raise ValueError(f"baseline_features must be >= 1, got {baseline_features}")
    if baseline_features > len(ranking):
        raise ValueError(
            f"baseline_features={baseline_features} exceeds the {len(ranking)} features "
            "named by the ranking CSV"
        )
    if max_ablation_features < baseline_features:
        raise ValueError(
            f"max_ablation_features={max_ablation_features} is below baseline_features="
            f"{baseline_features} — the sweep would have nothing to add"
        )


def _resolve_loo_run_plan(
    ranking: Sequence[str], model: str = MODEL_RF
) -> List[RunPlan]:
    """
    Return the leave-one-out plan: the full feature set, then one run per removed feature.

    Runs are numbered in ranking order, so run 1 removes the top-ranked feature.
    """
    if not ranking:
        raise ValueError("ranking is empty — nothing to leave out")

    full = tuple(ranking)
    plans = [
        RunPlan(
            number=None,
            log_name=_qualify_log_name(LOO_BASELINE_LOG_NAME, model),
            features=full,
            added_feature=None,
            removed_feature=None,
            strategy=STRATEGY_LOO,
            model=model,
        )
    ]
    for index, feature in enumerate(ranking, start=1):
        plans.append(
            RunPlan(
                number=index,
                log_name=_qualify_log_name(LOO_RUN_LOG_TEMPLATE.format(n=index), model),
                features=tuple(f for f in ranking if f != feature),
                added_feature=None,
                removed_feature=feature,
                strategy=STRATEGY_LOO,
                model=model,
            )
        )
    return plans


def resolve_group_run_plan(
    features: Sequence[str], groups: Sequence[str], model: str = MODEL_RF
) -> List[RunPlan]:
    """
    Return the group plan: every feature, then one run per group with that group removed.

    A run's ``removed_feature`` is ``group:<name>``.

    Raises
    ------
    ValueError
        For an unknown group, or one with no column among `features`.
    """
    unknown = [g for g in groups if g not in FEATURE_GROUPS]
    if unknown:
        raise ValueError(f"unknown feature group(s) {unknown}, expected some of {sorted(FEATURE_GROUPS)}")
    full = tuple(features)
    plans = [
        RunPlan(
            number=None,
            log_name=_qualify_log_name(GROUP_BASELINE_LOG_NAME, model),
            features=full,
            added_feature=None,
            strategy=STRATEGY_GROUP,
            model=model,
        )
    ]
    for index, group in enumerate(groups, start=1):
        members = set(FEATURE_GROUPS[group])
        kept = tuple(f for f in full if f not in members)
        if len(kept) == len(full):
            raise ValueError(f"feature group {group!r} has no column in the model's matrix")
        plans.append(
            RunPlan(
                number=index,
                log_name=_qualify_log_name(GROUP_RUN_LOG_TEMPLATE.format(n=index), model),
                features=kept,
                added_feature=None,
                removed_feature=f"group:{group}",
                strategy=STRATEGY_GROUP,
                model=model,
            )
        )
    return plans


def resolve_run_plan(
    ranking: Sequence[str],
    baseline_features: int = DEFAULT_BASELINE_FEATURES,
    max_ablation_features: int = DEFAULT_MAX_ABLATION_FEATURES,
    strategy: str = STRATEGY_FORWARD,
    model: str = MODEL_RF,
) -> List[RunPlan]:
    """
    Return a sweep's baseline run followed by its runs.

    Parameters
    ----------
    ranking : sequence of str
        Features, most important first, for the chosen model.
    baseline_features : int, optional
        Features in the forward baseline.
    max_ablation_features : int, optional
        Total feature count the forward sweep climbs to, capped at
        ``len(ranking)``.
    strategy : {"forward", "loo"}, optional
        Sweep to plan; ``loo`` ignores the two bounds. ``greedy`` cannot be
        planned in advance; see `_run_greedy_forward_selection`.
    model : {"rf", "catboost"}, optional
        Stamped onto each plan and its log name.

    Returns
    -------
    list of RunPlan
        The baseline first.
    """
    if strategy == STRATEGY_LOO:
        return _resolve_loo_run_plan(ranking, model=model)
    if strategy != STRATEGY_FORWARD:
        raise ValueError(
            f"resolve_run_plan does not support strategy={strategy!r} — expected "
            f"{STRATEGY_FORWARD!r} or {STRATEGY_LOO!r} (greedy forward selection runs through "
            "run_ablation(strategy='greedy') instead, since it can't be resolved ahead of a fit)"
        )

    _validate_sweep_bounds(ranking, baseline_features, max_ablation_features)

    ceiling = min(max_ablation_features, len(ranking))
    plans = [
        RunPlan(
            number=None,
            log_name=_qualify_log_name(BASELINE_LOG_NAME, model),
            features=tuple(ranking[:baseline_features]),
            added_feature=None,
            model=model,
        )
    ]
    for index, total in enumerate(range(baseline_features + 1, ceiling + 1), start=1):
        plans.append(
            RunPlan(
                number=index,
                log_name=_qualify_log_name(RUN_LOG_TEMPLATE.format(n=index), model),
                features=tuple(ranking[:total]),
                added_feature=ranking[total - 1],
                model=model,
            )
        )
    return plans




@dataclass
class PreparedSplits:
    """
    Everything a sweep needs that does not depend on the feature subset.

    The pipeline up to encoding is fit on the train split independently of
    column choice, so it runs once per sweep.

    Attributes
    ----------
    feature_names : list of str
        Column labels of ``X_train``, ``X_val`` and ``X_test``, in order.
    model : str
        ``"rf"``: numpy or cupy arrays selected by position, with `xp` the
        array module. ``"catboost"``: pandas DataFrames selected by name, with
        `cat_features` naming the categorical columns.
    """

    X_train: object
    X_val: object
    X_test: object
    y_train: object
    y_val: object
    y_test: object
    feature_names: List[str]
    xp: object
    dtype: object
    n_train: int
    n_val: int
    n_test: int
    model: str = MODEL_RF
    cat_features: Optional[List[str]] = None

    def column_indices(self, features: Sequence[str]) -> List[int]:
        """
        Return the positions of `features` in the feature matrix, in order.

        Raises
        ------
        KeyError
            On an unknown feature name.
        """
        lookup = {name: index for index, name in enumerate(self.feature_names)}
        unknown = [name for name in features if name not in lookup]
        if unknown:
            raise KeyError(
                f"Unknown feature(s) {unknown} — not columns of the prepared matrix"
            )
        return [lookup[name] for name in features]


def prepare_splits(
    df,
    args: argparse.Namespace,
    model: str = MODEL_RF,
    log: Optional[logging.Logger] = None,
) -> PreparedSplits:
    """
    Run the model's own preprocessing once and return the encoded splits.

    Mirrors the first half of the model's training run, in its order:
    ``engineer_features``, ``split_dataset``, the train-only postcode and sector
    price statistics, then ``impute_and_encode`` (``"rf"``) or
    ``prepare_features`` (``"catboost"``). Any divergence would make the
    ablation describe a model that is never trained. ``"catboost"`` requires a
    pandas `df`.

    Returns
    -------
    PreparedSplits
        The encoded train, validation and test splits.
    """
    if model not in MODELS:
        raise ValueError(f"Unknown model {model!r}, expected one of {MODELS}")
    if model == MODEL_CATBOOST and hpp_rf._is_cudf(df):
        raise ValueError(
            "prepare_splits(model='catboost') requires a pandas DataFrame — CatBoost trains on a "
            "host-resident DataFrame regardless of --device (see hpp_cb's module docstring). Load "
            "with hpp_cb.load_dataset rather than hpp_rf.load_dataset(use_gpu=True)."
        )

    df = (
        hpp_rf.engineer_features(df)
        .sort_values(hpp_rf.DATE_COLUMN)
        .reset_index(drop=True)
    )
    if model == MODEL_CATBOOST:
        # Reads the UK HPI at each previous sale from `ukhpi_at_prev_sale`,
        # which `main` looks up once for the whole dataset.
        df = hpp_rf.add_market_relative_features(df)
    train_df, val_df, test_df, _ = hpp_rf.split_dataset(df, args, log=log)
    hpp_rf.log_split_diagnostics(train_df, val_df, test_df, log)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )

    cat_features: Optional[List[str]] = None
    if model == MODEL_CATBOOST:
        X_train, X_val, X_test, cat_features, _, feature_metadata = (
            hpp_cb.prepare_features(
                train_df, val_df, test_df, use_ofsted=getattr(args, "use_ofsted", True)
            )
        )
        feature_names = feature_metadata["feature_columns"]
        xp = np
        dtype = np.float64
        on_gpu = False
    else:
        X_train, X_val, X_test, _, _, rf_metadata = hpp_rf.impute_and_encode(
            train_df, val_df, test_df, use_ofsted=getattr(args, "use_ofsted", True)
        )
        feature_names = list(rf_metadata["feature_columns"])
        on_gpu = hpp_rf._is_cudf(train_df)
        if on_gpu:
            import cupy as xp

            dtype = hpp_rf.GPU_DTYPE
        else:
            xp = np
            dtype = np.float64

    def _target(part):
        """Return the target column as a cupy or numpy array of the chosen dtype."""
        return (
            part[hpp_rf.TARGET_COLUMN].to_cupy().astype(dtype)
            if on_gpu
            else part[hpp_rf.TARGET_COLUMN].to_numpy(dtype=dtype)
        )

    if X_train.shape[1] != len(feature_names):
        source = (
            "hpp_cb.prepare_features' own feature_metadata"
            if model == MODEL_CATBOOST
            else "FEATURE_MATRIX_COLUMNS in data_visualization"
        )
        raise ValueError(
            f"{'hpp_cb.prepare_features' if model == MODEL_CATBOOST else 'impute_and_encode'} returned "
            f"{X_train.shape[1]} columns but the resolved feature name list has {len(feature_names)} "
            f"({source} is out of date)."
        )

    return PreparedSplits(
        X_train=X_train,
        X_val=X_val,
        X_test=X_test,
        y_train=_target(train_df),
        y_val=_target(val_df),
        y_test=_target(test_df),
        feature_names=feature_names,
        xp=xp,
        dtype=dtype,
        n_train=len(train_df),
        n_val=len(val_df),
        n_test=len(test_df),
        model=model,
        cat_features=cat_features,
    )


def fit_subset(
    prepared: PreparedSplits,
    features: Sequence[str],
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
) -> Dict:
    """
    Train one model on `features` and score it on validation and test.

    Dispatches to `_fit_subset_rf` or `_fit_subset_catboost`.

    Returns
    -------
    dict
        ``backend``, row counts, and ``val`` and ``test`` metrics including
        ``by_price_band``.
    """
    if prepared.model == MODEL_CATBOOST:
        return _fit_subset_catboost(prepared, features, args, log=log)
    return _fit_subset_rf(prepared, features, args, log=log)


def _fit_subset_rf(
    prepared: PreparedSplits,
    features: Sequence[str],
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
) -> Dict:
    """
    Fit a Random Forest on a positional column slice of the prepared matrix.

    Uses `hpp_rf`'s regressor resolution and target handling, so a GPU sweep
    trains with cuML exactly as `hpp_rf` would.
    """
    indices = prepared.column_indices(features)
    xp = prepared.xp
    X_train = prepared.X_train[:, indices]
    y_train = xp.log1p(prepared.y_train) if args.log_target else prepared.y_train

    model, backend = hpp_rf.resolve_regressor(
        args.device,
        log,
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
        max_features=args.max_features,
        random_state=args.random_state,
        n_jobs=args.n_jobs,
    )
    model.fit(X_train, y_train)

    results = {
        "backend": backend,
        "n_train": prepared.n_train,
        "n_val": prepared.n_val,
        "n_test": prepared.n_test,
    }
    for split_name, X, y_true in (
        ("val", prepared.X_val, prepared.y_val),
        ("test", prepared.X_test, prepared.y_test),
    ):
        preds = xp.asarray(model.predict(X[:, indices]))
        if args.log_target:
            preds = xp.expm1(preds)
        metrics = hpp_rf.compute_metrics(y_true, preds)
        metrics["by_price_band"] = hpp_rf.compute_metrics_by_price_band(y_true, preds)
        results[split_name] = metrics
    return results


def _fit_subset_catboost(
    prepared: PreparedSplits,
    features: Sequence[str],
    args: argparse.Namespace,
    log: Optional[logging.Logger] = None,
) -> Dict:
    """
    Fit CatBoost on a named column slice of the prepared DataFrames.

    Mirrors ``hpp_cb._fit_and_evaluate``: the same ``--cb-*`` settings, device
    resolution and log target. Early stopping uses a tail carved from the
    train split (``--cb-eval-frac``), never validation, so the validation
    deltas the sweep ranks on are not fitted to. Pass 2 refits on all of train
    at pass 1's best iteration with its realised learning rate pinned.
    """
    from catboost import CatBoostRegressor, Pool

    selected = list(features)
    X_train_full = prepared.X_train[selected]
    X_val = prepared.X_val[selected]
    X_test = prepared.X_test[selected]
    cat_features = [f for f in selected if f in set(prepared.cat_features or ())]

    y_train_raw = prepared.y_train
    y_val_raw = prepared.y_val
    y_test_raw = prepared.y_test
    y_train_full = np.log1p(y_train_raw) if args.log_target else y_train_raw

    eval_mask = hpp_cb._carve_eval_mask(len(X_train_full), frac=args.cb_eval_frac)
    fit_mask = ~eval_mask
    X_fit_train = X_train_full.iloc[fit_mask]
    X_eval = X_train_full.iloc[eval_mask]
    y_fit_train = y_train_full[fit_mask]
    y_eval = y_train_full[eval_mask]

    use_gpu, _, reason = hpp_cb.resolve_task_type(args.device, log)
    cb_kwargs = dict(
        iterations=args.cb_iterations,
        learning_rate=args.cb_learning_rate,
        depth=args.cb_depth,
        l2_leaf_reg=args.cb_l2_leaf_reg,
        loss_function="RMSE",
        random_seed=args.random_state,
        early_stopping_rounds=args.cb_early_stopping_rounds,
        verbose=False,
        allow_writing_files=False,
    )
    # Devices, GPU memory and threads from the host's resource plan, as hpp_cb does.
    plan = hardware.plan_resources(
        device="gpu" if use_gpu else "cpu",
        overrides={"thread_count": args.cb_thread_count, "fold_concurrency": 1},
    )
    cb_kwargs.update(plan.catboost_params())
    if use_gpu:
        backend = f"CatBoost (gpu: devices {plan.devices}, {', '.join(plan.gpu_names)})".strip()
    else:
        backend = f"CatBoost (cpu, {reason})" if reason else "CatBoost (cpu)"

    train_pool = Pool(X_fit_train, y_fit_train, cat_features=cat_features)
    eval_pool = Pool(X_eval, y_eval, cat_features=cat_features)
    first_pass_model = CatBoostRegressor(**cb_kwargs)
    first_pass_model.fit(train_pool, eval_set=eval_pool, use_best_model=True)
    best_iteration = first_pass_model.get_best_iteration()

    refit_iterations = (best_iteration + 1) if best_iteration is not None else args.cb_iterations
    refit_kwargs = hpp_cb._refit_kwargs_from_first_pass(
        cb_kwargs, first_pass_model, refit_iterations
    )
    model = CatBoostRegressor(**refit_kwargs)
    model.fit(Pool(X_train_full, y_train_full, cat_features=cat_features))
    if log is not None:
        log.info(
            f"_fit_subset_catboost: refit on the full {len(X_train_full)}-row training slice "
            f"at iterations={refit_iterations}, learning_rate={refit_kwargs['learning_rate']} "
            f"(F2; eval carve of {int(eval_mask.sum())} rows used for pass-1 early stopping only)"
        )

    results = {
        "backend": backend,
        "n_train": prepared.n_train,
        "n_eval_carve": int(eval_mask.sum()),
        "n_val": prepared.n_val,
        "n_test": prepared.n_test,
        "best_iteration": best_iteration,
    }
    for split_name, X, y_true in (
        ("val", X_val, y_val_raw),
        ("test", X_test, y_test_raw),
    ):
        preds = model.predict(X)
        if args.log_target:
            preds = np.expm1(preds)
        metrics = hpp_rf.compute_metrics(y_true, preds)
        metrics["by_price_band"] = hpp_rf.compute_metrics_by_price_band(y_true, preds)
        results[split_name] = metrics
    return results




def _format_number(value: Optional[float]) -> str:
    """Format a number, count or None as it appears in a log file."""
    if value is None:
        return _NONE_TOKEN
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return str(int(value))
    value = float(value)
    if not np.isfinite(value):
        return repr(value)
    return f"{value:.10g}"


def _parse_number(token: str) -> Optional[float]:
    """Parse a log-file number, mapping the None token back to None."""
    token = token.strip()
    if token == _NONE_TOKEN:
        return None
    return float(token)


@dataclass
class AblationRecord:
    """
    A parsed ablation log file.

    Attributes
    ----------
    header : dict of str to str
        Untyped key/value lines above the first section.
    metrics, price_bands : dict
        Keyed by split name, ``"val"`` or ``"test"``.
    deltas : dict
        Empty for a baseline log.
    """

    path: Optional[Path] = None
    header: Dict[str, str] = field(default_factory=dict)
    features: List[str] = field(default_factory=list)
    metrics: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    price_bands: Dict[str, Dict[str, Dict[str, Optional[float]]]] = field(
        default_factory=dict
    )
    deltas: Dict[str, Optional[float]] = field(default_factory=dict)

    @property
    def run(self) -> str:
        """The run label."""
        return self.header.get("run", "")

    @property
    def n_features(self) -> int:
        """The number of features the run trained on."""
        return int(self.header["n_features"])

    @property
    def added_feature(self) -> Optional[str]:
        """The feature a forward or greedy run adds, or None."""
        value = self.header.get("added_feature", _NONE_TOKEN)
        return None if value == _NONE_TOKEN else value

    @property
    def removed_feature(self) -> Optional[str]:
        """The feature a leave-one-out run drops, or None."""
        value = self.header.get("removed_feature", _NONE_TOKEN)
        return None if value == _NONE_TOKEN else value

    @property
    def strategy(self) -> str:
        """The strategy, ``"forward"`` for logs written before the field existed."""
        return self.header.get("strategy", STRATEGY_FORWARD)

    @property
    def model_type(self) -> str:
        """The model, ``"rf"`` for logs written before ``--model`` existed."""
        return self.header.get("model_type", MODEL_RF)

    def metric(self, split: str, name: str) -> Optional[float]:
        """
        Return one metric.

        Raises
        ------
        KeyError
            Naming the log file when the metric is absent.
        """
        try:
            return self.metrics[split][name]
        except KeyError as exc:
            raise KeyError(
                f"{self.path or '<record>'} has no [metrics.{split}] {name} line"
            ) from exc


def _hyperparameter_lines(
    model: str, args: argparse.Namespace, results: Dict
) -> List[str]:
    """Return the header lines naming a run's regressor, hyperparameters and outcome."""
    if model == MODEL_CATBOOST:
        return [
            f"iterations: {args.cb_iterations}",
            f"learning_rate: {_NONE_TOKEN if args.cb_learning_rate is None else args.cb_learning_rate}",
            f"depth: {args.cb_depth}",
            f"l2_leaf_reg: {args.cb_l2_leaf_reg}",
            f"early_stopping_rounds: {args.cb_early_stopping_rounds}",
            f"eval_frac: {args.cb_eval_frac}",
            f"best_iteration: {results.get('best_iteration', _NONE_TOKEN)}",
        ]
    return [
        f"n_estimators: {args.n_estimators}",
        f"max_depth: {_NONE_TOKEN if args.max_depth is None else args.max_depth}",
        f"min_samples_leaf: {args.min_samples_leaf}",
        f"max_features: {args.max_features}",
    ]


def write_run_log(
    path: Path,
    plan: RunPlan,
    results: Dict,
    args: argparse.Namespace,
    generated_at: Optional[str] = None,
) -> Path:
    """
    Write one run's log in the format `parse_ablation_log` reads.

    The ``[delta_vs_baseline]`` section is appended later by
    `append_delta_section`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")

    if plan.is_baseline:
        comment = f"# {plan.strategy} baseline run: {len(plan.features)} feature(s)"
    elif plan.removed_feature is not None:
        comment = f"# {plan.strategy} run {plan.number}: {len(plan.features)} feature(s), {plan.removed_feature} removed"
    else:
        comment = (
            f"# {plan.strategy} run {plan.number}: {len(plan.features)} feature(s) from the "
            "SHAP feature ranking CSV (--importance-csv)"
        )

    lines = (
        [
            "# HPLDN feature ablation (hpldn.backend.scripts.model_ablation)",
            comment,
            "",
            f"run: {plan.label}",
            f"strategy: {plan.strategy}",
            f"model_type: {plan.model}",
            f"generated_at: {stamp}",
            f"model: {_MODEL_CLASS_NAMES[plan.model]}",
            f"backend: {results.get('backend', 'unknown')}",
            f"split_method: {args.split_method}",
            f"log_target: {str(bool(args.log_target)).lower()}",
        ]
        + _hyperparameter_lines(plan.model, args, results)
        + [
            f"random_state: {args.random_state}",
            f"n_features: {len(plan.features)}",
            f"added_feature: {plan.added_feature or _NONE_TOKEN}",
            f"removed_feature: {plan.removed_feature or _NONE_TOKEN}",
            f"n_train: {results.get('n_train', 0)}",
            f"n_val: {results.get('n_val', 0)}",
            f"n_test: {results.get('n_test', 0)}",
            "",
            "[features]",
        ]
    )
    lines += [f"{rank}: {name}" for rank, name in enumerate(plan.features, start=1)]

    for split in SPLIT_NAMES:
        split_results = results.get(split, {})
        lines += ["", f"[metrics.{split}]"]
        lines += [
            f"{name}: {_format_number(split_results.get(name))}"
            for name in METRIC_NAMES
        ]
        lines += ["", f"[price_bands.{split}]"]
        bands = split_results.get("by_price_band", {})
        for label in hpp_rf.PRICE_BAND_LABELS:
            band = bands.get(label, {})
            fields = " | ".join(
                f"{name}: {_format_number(band.get(name))}" for name in BAND_FIELD_NAMES
            )
            lines.append(f"band: {label} | {fields}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_ablation_log(path: Path) -> AblationRecord:
    """Read a log written by `write_run_log` and `append_delta_section` back in."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Ablation log not found: {path}")

    record = AblationRecord(path=path)
    section: Optional[str] = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1)
            continue

        if section is None:
            kv = _KV_RE.match(line)
            if kv:
                record.header[kv.group(1)] = kv.group(2).strip()
            continue

        if section == "features":
            feature_match = _FEATURE_LINE_RE.match(line)
            if feature_match:
                record.features.append(feature_match.group(2))
            continue

        if section.startswith("metrics."):
            kv = _KV_RE.match(line)
            if kv:
                record.metrics.setdefault(section.split(".", 1)[1], {})[kv.group(1)] = (
                    _parse_number(kv.group(2))
                )
            continue

        if section.startswith("price_bands."):
            split = section.split(".", 1)[1]
            fields = {}
            for chunk in line.split("|"):
                key, _, value = chunk.partition(":")
                fields[key.strip()] = value.strip()
            label = fields.pop("band", None)
            if label is None:
                continue
            record.price_bands.setdefault(split, {})[label] = {
                key: _parse_number(value) for key, value in fields.items()
            }
            continue

        if section == "delta_vs_baseline":
            kv = _KV_RE.match(line)
            if kv:
                key, value = kv.group(1), kv.group(2).strip()
                try:
                    record.deltas[key] = _parse_number(value)
                except ValueError:
                    record.header[key] = value
            continue

    if "n_features" not in record.header:
        raise ValueError(f"{path} is not an ablation log (no 'n_features' line)")
    return record


def compute_deltas(
    baseline: AblationRecord, run: AblationRecord
) -> Dict[str, Optional[float]]:
    """
    Return baseline error minus run error, per split, for RMSE and MAE.

    A positive delta means the run has lower error. Both operands come from
    parsed logs and are returned beside each delta (``val.baseline_rmse``,
    ``val.run_rmse``, ``val.rmse_delta``).
    """
    deltas: Dict[str, Optional[float]] = {}
    for split in SPLIT_NAMES:
        for metric in DELTA_METRIC_NAMES:
            baseline_value = baseline.metric(split, metric)
            run_value = run.metric(split, metric)
            deltas[f"{split}.baseline_{metric}"] = baseline_value
            deltas[f"{split}.run_{metric}"] = run_value
            deltas[f"{split}.{metric}_delta"] = (
                None
                if baseline_value is None or run_value is None
                else baseline_value - run_value
            )
    return deltas


def append_delta_section(
    path: Path, deltas: Dict[str, Optional[float]], baseline_path: Path
) -> Path:
    """
    Append ``[delta_vs_baseline]`` to a written run log.

    Appended afterwards because the deltas are computed from both files'
    text. Baselines never get this section.
    """
    path = Path(path)
    lines = [
        "",
        "[delta_vs_baseline]",
        f"baseline_log: {Path(baseline_path).name}",
        "# delta = baseline - run, so positive means this run has the lower error.",
        "# Select on the val deltas; the test deltas are for a single final read.",
    ]
    lines += [f"{key}: {_format_number(value)}" for key, value in deltas.items()]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path




def run_ablation(
    df,
    args: argparse.Namespace,
    log_dir: Path = DEFAULT_LOG_DIR,
    importance_csv: Path = DEFAULT_IMPORTANCE_CSV,
    baseline_features: int = DEFAULT_BASELINE_FEATURES,
    max_ablation_features: int = DEFAULT_MAX_ABLATION_FEATURES,
    strategy: str = STRATEGY_FORWARD,
    model: str = MODEL_RF,
    log: Optional[logging.Logger] = None,
    groups: Sequence[str] = ("ofsted",),
) -> List[AblationRecord]:
    """
    Run a whole sweep over a loaded ``production.london_houses`` DataFrame.

    Parameters
    ----------
    df : DataFrame
        The dataset.
    args : argparse.Namespace
        Parsed options, including every model hyperparameter.
    log_dir : pathlib.Path, optional
        Where run logs are written.
    importance_csv : pathlib.Path, optional
        SHAP ranking to sweep down.
    baseline_features, max_ablation_features : int, optional
        Bounds of the forward and greedy climbs.
    strategy : {"forward", "loo", "greedy", "group"}, optional
        Sweep to run.
    model : {"rf", "catboost"}, optional
        Model to ablate.
    log : logging.Logger, optional
        Logger for progress messages.
    groups : sequence of str, optional
        ``group`` only: the `FEATURE_GROUPS` to remove, one run each.

    Returns
    -------
    list of AblationRecord
        One record per run, re-read from the log file just written.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_splits(df, args, model=model, log=log)
    if strategy == STRATEGY_GROUP:
        plans = resolve_group_run_plan(prepared.feature_names, groups, model=model)
        return _run_plans(prepared, plans, args, log_dir, strategy, model, log)
    ranking = load_feature_ranking(
        importance_csv, known_features=prepared.feature_names
    )

    if strategy == STRATEGY_GREEDY:
        if log is not None:
            ceiling = min(max_ablation_features, len(ranking))
            log.info(
                f"Greedy forward selection ({model}): seed of {baseline_features} SHAP-ranked "
                f"feature(s), greedily growing to {ceiling} feature(s) (ceiling "
                f"{max_ablation_features}, ranking names {len(ranking)}); logs -> {log_dir}"
            )
        return _run_greedy_forward_selection(
            prepared,
            ranking,
            args,
            log_dir,
            baseline_features,
            max_ablation_features,
            model=model,
            log=log,
        )

    plans = resolve_run_plan(
        ranking,
        baseline_features,
        max_ablation_features,
        strategy=strategy,
        model=model,
    )

    return _run_plans(prepared, plans, args, log_dir, strategy, model, log)


def _run_plans(
    prepared: "PreparedSplits",
    plans: Sequence[RunPlan],
    args: argparse.Namespace,
    log_dir: Path,
    strategy: str,
    model: str,
    log: Optional[logging.Logger],
) -> List[AblationRecord]:
    """Fit and log each plan in turn, the baseline first, with each run's deltas against it."""
    if log is not None:
        log.info(
            f"Ablation sweep ({strategy}, {model}): baseline of {len(plans[0].features)} feature(s) "
            f"then {len(plans) - 1} run(s) up to {len(plans[-1].features)} feature(s); logs -> {log_dir}"
        )

    baseline_record: Optional[AblationRecord] = None
    baseline_path = log_dir / plans[0].log_name
    records: List[AblationRecord] = []

    for plan in plans:
        path = log_dir / plan.log_name
        results = fit_subset(prepared, plan.features, args, log=log)
        write_run_log(path, plan, results, args)
        record = parse_ablation_log(path)

        if plan.is_baseline:
            baseline_record = record
        else:
            if baseline_record is None:  
                raise RuntimeError(
                    "the baseline run has to be first in the plan; "
                    f"{plan.log_name} was reached without one"
                )
            append_delta_section(
                path, compute_deltas(baseline_record, record), baseline_path
            )
            record = parse_ablation_log(path)

        records.append(record)
        if log is not None:
            _log_run_summary(log, plan, record)

    return records


def _run_greedy_forward_selection(
    prepared: PreparedSplits,
    ranking: Sequence[str],
    args: argparse.Namespace,
    log_dir: Path,
    baseline_features: int = DEFAULT_BASELINE_FEATURES,
    max_ablation_features: int = DEFAULT_MAX_ABLATION_FEATURES,
    model: str = MODEL_RF,
    log: Optional[logging.Logger] = None,
) -> List[AblationRecord]:
    """
    Run greedy forward selection, writing each step's winning run.

    Seeded with the top `baseline_features` features, each step fits every
    remaining candidate in ranking order and keeps the one with the lowest
    validation RMSE (earlier-ranked wins ties), up to `max_ablation_features`.

    Returns
    -------
    list of AblationRecord
        The baseline and each step's record.
    """
    _validate_sweep_bounds(ranking, baseline_features, max_ablation_features)
    ceiling = min(max_ablation_features, len(ranking))

    selected = list(ranking[:baseline_features])
    remaining = [feature for feature in ranking if feature not in set(selected)]

    baseline_plan = RunPlan(
        number=None,
        log_name=_qualify_log_name(GREEDY_BASELINE_LOG_NAME, model),
        features=tuple(selected),
        added_feature=None,
        strategy=STRATEGY_GREEDY,
        model=model,
    )
    baseline_path = log_dir / baseline_plan.log_name
    baseline_results = fit_subset(prepared, baseline_plan.features, args, log=log)
    write_run_log(baseline_path, baseline_plan, baseline_results, args)
    baseline_record = parse_ablation_log(baseline_path)
    records: List[AblationRecord] = [baseline_record]
    if log is not None:
        _log_run_summary(log, baseline_plan, baseline_record)

    step = 0
    while len(selected) < ceiling and remaining:
        step += 1
        best_feature: Optional[str] = None
        best_results: Optional[Dict] = None
        best_val_rmse = float("inf")
        for candidate in remaining:
            trial_results = fit_subset(prepared, selected + [candidate], args, log=log)
            trial_val_rmse = trial_results["val"]["rmse"]
            if log is not None:
                log.debug(
                    f"greedy step {step}: +{candidate} -> val RMSE=£{trial_val_rmse:,.0f}"
                )
            if trial_val_rmse < best_val_rmse:
                best_feature, best_results, best_val_rmse = (
                    candidate,
                    trial_results,
                    trial_val_rmse,
                )

        selected.append(best_feature)
        remaining.remove(best_feature)

        plan = RunPlan(
            number=step,
            log_name=_qualify_log_name(GREEDY_RUN_LOG_TEMPLATE.format(n=step), model),
            features=tuple(selected),
            added_feature=best_feature,
            strategy=STRATEGY_GREEDY,
            model=model,
        )
        path = log_dir / plan.log_name
        write_run_log(path, plan, best_results, args)
        record = parse_ablation_log(path)
        append_delta_section(
            path, compute_deltas(baseline_record, record), baseline_path
        )
        record = parse_ablation_log(path)

        records.append(record)
        if log is not None:
            _log_run_summary(log, plan, record)

    return records


def _log_run_summary(
    log: logging.Logger, plan: RunPlan, record: AblationRecord
) -> None:
    """Log one progress line summarising a finished run."""
    val_rmse = record.metric("val", "rmse")
    test_rmse = record.metric("test", "rmse")
    if plan.is_baseline:
        prefix = f"{plan.strategy} baseline ({record.n_features} features)"
    elif plan.added_feature is not None:
        prefix = (
            f"run {plan.number} (+{plan.added_feature}, {record.n_features} features)"
        )
    else:
        prefix = (
            f"run {plan.number} (-{plan.removed_feature}, {record.n_features} features)"
        )
    suffix = ""
    if not plan.is_baseline:
        val_delta = record.deltas.get("val.rmse_delta")
        val_mae_delta = record.deltas.get("val.mae_delta")
        if val_delta is not None and val_mae_delta is not None:
            suffix = (
                f" | vs baseline: val RMSE {val_delta:+,.0f} MAE {val_mae_delta:+,.0f}"
            )
    log.info(
        f"{prefix}: val RMSE=£{val_rmse:,.0f} test RMSE=£{test_rmse:,.0f}{suffix} -> {record.path.name}"
    )


def summarize(records: Sequence[AblationRecord]) -> Dict:
    """Return a JSON-serialisable digest of a finished sweep."""
    runs = []
    for record in records:
        runs.append(
            {
                "run": record.run,
                "log": record.path.name if record.path else None,
                "n_features": record.n_features,
                "added_feature": record.added_feature,
                "removed_feature": record.removed_feature,
                "val": {name: record.metric("val", name) for name in METRIC_NAMES},
                "test": {name: record.metric("test", name) for name in METRIC_NAMES},
                "delta_vs_baseline": dict(record.deltas),
            }
        )
    improving = [
        run
        for run in runs
        if (run["delta_vs_baseline"].get("val.rmse_delta") or 0.0) > 0.0
    ]
    harmful_to_remove = [
        run
        for run in runs
        if run["removed_feature"] is not None
        and (run["delta_vs_baseline"].get("val.rmse_delta") or 0.0) < 0.0
    ]
    return {
        "n_runs": len(runs),
        "runs": runs,
        "features_improving_val_rmse": [run["added_feature"] for run in improving],
        "features_hurting_when_removed": [
            run["removed_feature"] for run in harmful_to_remove
        ],
    }




def build_arg_parser() -> argparse.ArgumentParser:
    """
    Return the parser: this script's options plus every `hpp_rf` training option.

    `hpp_rf`'s parser is the parent, so a sweep takes the same flags as the
    model it ablates. CatBoost hyperparameters are added with a ``--cb-``
    prefix so they cannot collide with the forest's. ``--max-ablation-features``
    is the feature ceiling because ``--max-features`` is scikit-learn's.
    """
    parent = hpp_rf.build_arg_parser()
    parser = argparse.ArgumentParser(
        parents=[parent],
        add_help=False,
        description=(
            "Feature ablation for hpldn.backend.scripts.hpp_rf's Random Forest or hpldn.backend.scripts.hpp_cb's "
            "CatBoost (--model), in one of three strategies (--strategy): forward (default) trains "
            "on the top-N SHAP-ranked features then adds one more at a time down the ranking; loo "
            "trains on every feature and once more per feature with just that one removed; greedy "
            "seeds from the same top-N features then, at each step, tries every remaining feature "
            "and keeps whichever lowers val RMSE the most. All three log RMSE/MAE/MAPE/R2/MedAE, "
            "error by price band, and the RMSE/MAE change against the baseline."
        ),
    )
    parser.add_argument(
        "--model",
        choices=MODELS,
        default=MODEL_RF,
        help=(
            f"Which model to ablate (default: {MODEL_RF}). '{MODEL_CATBOOST}' fits "
            "hpp_cb.CatBoostRegressor (--cb-iterations/--cb-learning-rate/--cb-depth/"
            "--cb-l2-leaf-reg/--cb-early-stopping-rounds/--cb-thread-count) on the same "
            "SHAP-ranked feature sets instead of hpp_rf's RandomForestRegressor — see the "
            "'Which model' section of this module's docstring."
        ),
    )
    parser.add_argument(
        "--cb-iterations",
        dest="cb_iterations",
        type=int,
        default=hpp_cb.DEFAULT_ITERATIONS,
        help="--model catboost only: max boosting rounds (early stopping usually cuts this short)",
    )
    parser.add_argument(
        "--cb-learning-rate",
        dest="cb_learning_rate",
        type=float,
        default=None,
        help="--model catboost only: CatBoost default (auto-tuned from dataset size) when unset",
    )
    parser.add_argument(
        "--cb-depth",
        dest="cb_depth",
        type=int,
        default=6,
        help="--model catboost only: tree depth",
    )
    parser.add_argument(
        "--cb-l2-leaf-reg",
        dest="cb_l2_leaf_reg",
        type=float,
        default=3.0,
        help="--model catboost only",
    )
    parser.add_argument(
        "--cb-early-stopping-rounds",
        dest="cb_early_stopping_rounds",
        type=int,
        default=hpp_cb.DEFAULT_EARLY_STOPPING_ROUNDS,
        help="--model catboost only",
    )
    parser.add_argument(
        "--cb-eval-frac",
        dest="cb_eval_frac",
        type=float,
        default=0.1,
        help=(
            "--model catboost only: share of the train split, chronologically last, carved out "
            "as CatBoost's early-stopping eval_set (mirrors hpp_cb's "
            "--eval-frac). Never the val split, which every run is ranked on and so must stay "
            "unfitted."
        ),
    )
    parser.add_argument(
        "--cb-thread-count",
        dest="cb_thread_count",
        type=int,
        default=-1,
        help="--model catboost only: CPU threads (--device cpu/auto-without-a-GPU)",
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default=STRATEGY_FORWARD,
        help=(
            f"Which sweep to run (default: {STRATEGY_FORWARD}). '{STRATEGY_LOO}' ignores "
            "--baseline-features/--max-ablation-features (every feature gets exactly one run); "
            f"'{STRATEGY_GREEDY}' uses both, the same way '{STRATEGY_FORWARD}' does, to size its "
            "seed and its ceiling."
        ),
    )
    parser.add_argument(
        "--groups",
        nargs="+",
        choices=sorted(FEATURE_GROUPS),
        default=["ofsted"],
        help="--strategy group only: the feature groups to remove, one run each (default: ofsted)",
    )
    parser.add_argument(
        "--importance-csv",
        "--feature-importance",
        dest="importance_csv",
        type=Path,
        default=DEFAULT_IMPORTANCE_CSV,
        help=(
            "Path to the SHAP feature ranking CSV that sets the order features are added in — any "
            "CSV with the expected columns works, named however you like, not just the RF/CatBoost "
            f"defaults below (default: {DEFAULT_IMPORTANCE_CSV} — data_visualization's Random "
            "Forest ranking, regardless of --model, since both models' prepared matrices share the "
            "same feature names; pass --feature-importance tmp/csv/catboost_shap_feature_importance.csv "
            "for a --model catboost-native ranking, or point it at any other ranking CSV you have)"
        ),
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=(
            "Where the sweep's logs are written — baseline_ablation.log/ablation_run_<n>.log for "
            "--strategy forward, loo_baseline.log/loo_run_<n>.log for loo, "
            f"greedy_baseline.log/greedy_run_<n>.log for greedy (default: {DEFAULT_LOG_DIR}); "
            "--model catboost prefixes every name (catboost_baseline_ablation.log, ...) so it can "
            "share a --log-dir with an rf sweep without overwriting its logs"
        ),
    )
    parser.add_argument(
        "--baseline-features",
        type=int,
        default=DEFAULT_BASELINE_FEATURES,
        help=f"How many top-ranked features the baseline run trains on (default: {DEFAULT_BASELINE_FEATURES})",
    )
    parser.add_argument(
        "--max-ablation-features",
        type=int,
        default=DEFAULT_MAX_ABLATION_FEATURES,
        help=(
            "Ceiling on the TOTAL feature count the sweep climbs to, capped at however many "
            f"features the ranking CSV names (default: {DEFAULT_MAX_ABLATION_FEATURES}). Not to be "
            "confused with --max-features, which is scikit-learn's per-split sampling."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> Dict:
    """Load the dataset, run the sweep and print its summary as JSON."""
    args = build_arg_parser().parse_args(argv)
    log = get_logger(args.verbose)
    hpp_rf.set_global_seed(args.random_state)
    load_env_vars(log)
    from sqlalchemy import create_engine

    engine = create_engine(get_model_db_url(log))
    if args.model == MODEL_CATBOOST:
        df = hpp_cb.load_dataset(engine, limit=args.limit, log=log)
        mortgage_df = hpp_cb.load_mortgage_rates(engine, log=log)
        df = hpp_cb.add_mortgage_rate_features(df, mortgage_df, log=log)
        df[hpp_rf.UKHPI_AT_PREV_SALE_COLUMN] = hpp_rf.ukhpi_as_of(
            df["prev_sale_date"], df["propertytype"], hpp_rf.load_ukhpi(engine, log=log)
        )
    else:
        use_gpu, resources, _ = hpp_rf.resolve_device(args.device, log)
        if use_gpu:
            log.info(
                f"CUDA GPU detected ({resources.gpu_count}x {resources.gpu_manufacturer} "
                f"{resources.gpu_name}); loading {hpp_rf.VIEW_NAME} straight into GPU memory (cuDF)"
            )
        df = hpp_rf.load_dataset(engine, limit=args.limit, log=log, use_gpu=use_gpu)
    records = run_ablation(
        df,
        args,
        log_dir=args.log_dir,
        importance_csv=args.importance_csv,
        baseline_features=args.baseline_features,
        max_ablation_features=args.max_ablation_features,
        strategy=args.strategy,
        model=args.model,
        log=log,
        groups=args.groups,
    )
    summary = summarize(records)
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
