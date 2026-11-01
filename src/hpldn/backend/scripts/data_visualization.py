r"""
Render diagnostic plots of the data fed to the house-price models.

Draws (i) global feature importance by mean absolute SHAP value, (ii) a
feature correlation heatmap and (iii) univariate price-response panels, over
the prepared feature matrix `hpp_rf` or `hpp_cb` hands its regressor. Column
lists, splits and feature preparation are imported from those modules, so the
plots follow the models with no edits here, and statistics are fit on
training rows only.

Usage::

    python -m hpldn.backend.scripts.data_visualization --out-dir tmp/plots
    python -m hpldn.backend.scripts.data_visualization --model both --limit 50000
    python -m hpldn.backend.scripts.data_visualization --model catboost \
        --plots importance --split-method purged --out-dir tmp/plots
    python -m hpldn.backend.scripts.data_visualization --model rf \
        --model-in /tmp/hpp_rf.joblib --trust-pickle --plots importance
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from hpldn.backend.common.db import get_db_url
from hpldn.backend.common.utils import load_env_vars
from hpldn.backend.scripts import hpp_cb, hpp_rf
from hpldn.backend.scripts.hpp_rf import (
    CATEGORICAL_COLUMNS,
    DATE_COLUMN,
    FEATURE_COLUMNS,
    FEATURE_MATRIX_COLUMNS,
    MISSING_FLAG_COLUMNS,
    OFSTED_CATEGORICAL_COLUMNS,
    TARGET_COLUMN,
    VIEW_NAME,
)

# The Random Forest matrix's categoricals: hpp_rf's plus the Ofsted basis.
MATRIX_CATEGORICAL_COLUMNS = list(CATEGORICAL_COLUMNS) + list(OFSTED_CATEGORICAL_COLUMNS)
NUMERIC_FEATURE_COLUMNS = [c for c in FEATURE_MATRIX_COLUMNS if c not in set(MATRIX_CATEGORICAL_COLUMNS)]

SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
AXIS_LINE = "#c3c2b7"
SERIES_BLUE = "#2a78d6"
SERIES_BLUE_LIGHT = "#9ec5f4"

DIVERGING_ANCHORS = [
    "#0d366b",
    "#1c5cab",
    "#3987e5",
    "#9ec5f4",
    "#cde2fb",
    "#f0efec",
    "#f6cac9",
    "#ee8a89",
    "#e34948",
    "#b02f2e",
    "#7d1f1f",
]

DEFAULT_TOP_N = 25
DEFAULT_SHAP_SAMPLE = 500
DEFAULT_ANALYSIS_SAMPLE = 50_000
DEFAULT_CORR_THRESHOLD = 0.9
DEFAULT_UNIVARIATE_FEATURES = 24
DEFAULT_UNIVARIATE_BINS = 20

PLOT_CHOICES = ("importance", "correlation", "univariate")


def get_logger(verbose: bool = False) -> logging.Logger:
    """Return a minimal stdout logger, independent of ``staging.yaml``."""
    logger = logging.getLogger("data_visualization")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


def _pyplot():
    """
    Import ``matplotlib.pyplot`` with the headless Agg backend selected.

    Lazy, so importing this module for data-only helpers stays cheap.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _apply_chart_style(plt) -> None:
    """Apply the shared chart style: hairline grid, muted axes, no top or right spines."""
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": AXIS_LINE,
            "axes.labelcolor": INK_SECONDARY,
            "axes.titlecolor": INK_PRIMARY,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRIDLINE,
            "grid.linewidth": 0.6,
            "text.color": INK_PRIMARY,
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "xtick.labelcolor": INK_SECONDARY,
            "ytick.labelcolor": INK_SECONDARY,
            "font.family": "sans-serif",
            "axes.titlesize": 12,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
        }
    )


def _diverging_colormap():
    """Return the blue-to-red diverging colormap built from `DIVERGING_ANCHORS`."""
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("hpldn_diverging", DIVERGING_ANCHORS)


def _compact_number(value: float) -> str:
    """Return a short tick label for any magnitude, e.g. ``"180"``, ``"450k"``, ``"1.2m"``."""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{value / 1e9:,.1f}bn"
    if magnitude >= 1e6:
        return f"{value / 1e6:,.1f}m"
    if magnitude >= 1e4:
        return f"{value / 1e3:,.0f}k"
    if magnitude >= 100 or float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _title_block(ax, title: str, subtitle: str) -> None:
    """
    Write a title and one-line subtitle above `ax`.

    Positioned in offset points from the axes corner, so the gap is constant
    whatever the figure height.
    """
    ax.set_title(title, loc="left", pad=26 if subtitle else 12, fontweight="bold")
    if subtitle:
        ax.annotate(
            subtitle,
            xy=(0, 1),
            xycoords="axes fraction",
            xytext=(0, 7),
            textcoords="offset points",
            ha="left",
            va="bottom",
            fontsize=8.5,
            color=INK_SECONDARY,
            annotation_clip=False,
        )




@dataclass
class PreparedData:
    """
    One model's prepared feature matrix and what the plots need.

    Attributes
    ----------
    features : pandas.DataFrame
        The matrix the model is handed, labelled with `FEATURE_MATRIX_COLUMNS`:
        ordinal-coded categoricals for ``"rf"``, strings for ``"catboost"``.
    prices : numpy.ndarray
        The raw £ target aligned with `features`, not the log target.
    """

    model: str
    features: pd.DataFrame
    prices: np.ndarray
    split: str
    feature_names: List[str] = field(default_factory=lambda: list(FEATURE_MATRIX_COLUMNS))
    categorical_columns: List[str] = field(default_factory=lambda: list(MATRIX_CATEGORICAL_COLUMNS))
    numeric_columns: List[str] = field(default_factory=lambda: list(NUMERIC_FEATURE_COLUMNS))
    n_rows_total: int = 0


def prepare_visualization_data(
    df,
    args: argparse.Namespace,
    model: str,
    mortgage_df: Optional[pd.DataFrame] = None,
    log: Optional[logging.Logger] = None,
) -> PreparedData:
    """
    Run the models' own preparation pipeline and return one split's features.

    Runs ``engineer_features``, the mortgage-rate join, ``split_dataset``, the
    train-only postcode and sector price statistics, then
    ``impute_and_encode`` (``"rf"``) or ``prepare_features`` (``"catboost"``).

    Parameters
    ----------
    df : pandas.DataFrame
        Rows shaped like ``production.london_houses``.
    args : argparse.Namespace
        Parsed command-line options, including ``split_method`` and the
        CatBoost feature flags.
    model : {"rf", "catboost"}
        Which feature preparation to run.
    mortgage_df : pandas.DataFrame, optional
        ``production.mortgage_rates``; required for ``"catboost"``.
    log : logging.Logger, optional
        Logger for progress messages.

    Returns
    -------
    PreparedData
        The requested split's matrix and prices.

    Notes
    -----
    ``hpp_rf.split_dataset`` serves both models, and ``walk_forward`` is run as
    ``chronological`` so no per-fold model is trained just to draw a plot.
    """
    df = hpp_rf.engineer_features(df).sort_values(DATE_COLUMN).reset_index(drop=True)
    if mortgage_df is not None:
        df = hpp_cb.add_mortgage_rate_features(df, mortgage_df, log=log)
    n_rows_total = len(df)

    if args.split_method == "walk_forward":
        if log is not None:
            log.info("--split-method walk_forward falls back to chronological for visualisation")
        split_args = argparse.Namespace(**vars(args))
        split_args.split_method = "chronological"
    else:
        split_args = args

    train_df, val_df, test_df, _ = hpp_rf.split_dataset(df, split_args, log=log)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(
        train_df, val_df, test_df, random_state=args.random_state, log=log
    )

    # A view before migration 0034 has no Ofsted columns: plot without them.
    use_ofsted = getattr(args, "use_ofsted", True) and set(hpp_rf.OFSTED_COLUMNS) <= set(df.columns)
    if getattr(args, "use_ofsted", True) and not use_ofsted and log is not None:
        log.info("the loaded view has no Ofsted columns (before migration 0034): leaving them out")
    cb_cat_features = None
    if model == "rf":
        X_train, X_val, X_test, _, _, rf_metadata = hpp_rf.impute_and_encode(
            train_df, val_df, test_df, use_ofsted=use_ofsted
        )
        columns = rf_metadata["feature_columns"]
        frames = {
            "train": pd.DataFrame(np.asarray(X_train), columns=columns),
            "val": pd.DataFrame(np.asarray(X_val), columns=columns),
            "test": pd.DataFrame(np.asarray(X_test), columns=columns),
        }
    elif model == "catboost":
        X_train, X_val, X_test, cb_cat_features, _, _ = hpp_cb.prepare_features(
            train_df,
            val_df,
            test_df,
            use_iod=args.use_iod,
            use_ofsted=use_ofsted,
            nan_mode=args.nan_mode,
            drop_features=args.drop_features,
            log=log,
        )
        frames = {
            "train": X_train.reset_index(drop=True),
            "val": X_val.reset_index(drop=True),
            "test": X_test.reset_index(drop=True),
        }
    else:
        raise ValueError(f"Unknown model {model!r}, expected 'rf' or 'catboost'")

    parts = {"train": train_df, "val": val_df, "test": test_df}
    split = args.analysis_split
    if split not in parts:
        raise ValueError(f"Unknown analysis split {split!r}, expected train/val/test")

    prices = parts[split][TARGET_COLUMN].to_numpy(dtype=np.float64)
    feature_names = list(frames[split].columns)
    categorical_columns = list(cb_cat_features) if model == "catboost" else list(MATRIX_CATEGORICAL_COLUMNS)
    numeric_columns = [c for c in feature_names if c not in set(categorical_columns)]
    if log is not None:
        log.info(
            f"Prepared {model} features for the '{split}' split: "
            f"{len(frames[split])} rows x {len(feature_names)} features"
        )
    return PreparedData(
        model=model,
        features=frames[split],
        prices=prices,
        split=split,
        feature_names=feature_names,
        categorical_columns=categorical_columns,
        numeric_columns=numeric_columns,
        n_rows_total=n_rows_total,
    )


def subsample(prepared: PreparedData, n: Optional[int], random_state: int = 42) -> PreparedData:
    """
    Return a uniform random sample of at most `n` rows.

    Uniform rather than a head slice, because the split is chronological.
    `prepared` is returned unchanged when `n` is falsy or not smaller.
    """
    if not n or len(prepared.features) <= n:
        return prepared
    rng = np.random.default_rng(random_state)
    idx = np.sort(rng.choice(len(prepared.features), size=n, replace=False))
    return PreparedData(
        model=prepared.model,
        features=prepared.features.iloc[idx].reset_index(drop=True),
        prices=prepared.prices[idx],
        split=prepared.split,
        feature_names=list(prepared.feature_names),
        categorical_columns=list(prepared.categorical_columns),
        numeric_columns=list(prepared.numeric_columns),
        n_rows_total=prepared.n_rows_total,
    )




def fit_reference_model(prepared: PreparedData, args: argparse.Namespace, log: Optional[logging.Logger] = None):
    """
    Fit the reference model whose SHAP values plot (i) ranks.

    Deliberately smaller than a training run (``--viz-*`` options), since a
    shallower model ranks features the same way for far less TreeSHAP cost.
    CatBoost settings come from ``hpp_cb._catboost_kwargs``, so ``has_time``,
    loss, CTR, device and sample weighting match a real run. The target is
    ``log1p(price)`` with ``--log-target``, which does not change the ranking.

    Returns
    -------
    object
        A fitted RandomForestRegressor or CatBoostRegressor.
    """
    y = np.log1p(prepared.prices) if args.log_target else prepared.prices
    if prepared.model == "rf":
        from sklearn.ensemble import RandomForestRegressor

        model = RandomForestRegressor(
            n_estimators=args.viz_n_estimators,
            max_depth=args.viz_max_depth,
            min_samples_leaf=args.min_samples_leaf,
            max_features=args.max_features,
            random_state=args.random_state,
            n_jobs=args.n_jobs,
        )
        model.fit(prepared.features.to_numpy(dtype=np.float64), y)
    else:
        from catboost import CatBoostRegressor, Pool

        cb_args = argparse.Namespace(**vars(args))
        cb_args.iterations = args.viz_iterations
        cb_args.depth = args.viz_depth
        cb_kwargs, backend = hpp_cb._catboost_kwargs(cb_args, log)
        weight = hpp_cb._sample_weight_for_training(prepared.prices, mode=args.sample_weight)
        model = CatBoostRegressor(**cb_kwargs)
        model.fit(Pool(prepared.features, y, cat_features=prepared.categorical_columns, weight=weight))
        if log is not None:
            log.info(f"Reference CatBoost training backend: {backend}")
    if log is not None:
        log.info(f"Fitted a {prepared.model} reference model on {len(prepared.features)} rows for SHAP")
    return model


def load_reference_model(path: Path, model: str, trust_pickle: bool = False):
    """
    Load a model saved by an `hpp_rf` or `hpp_cb` ``--model-out`` run.

    An `hpp_cb` model is a bundle, verified before it is loaded; an `hpp_rf`
    one is a pickle and loads only with `trust_pickle`.
    """
    if model == "rf":
        return hpp_rf.load_model(path, trust_pickle=trust_pickle)["model"]
    return hpp_cb.load_model(path)[0]




def compute_shap_values(model, prepared: PreparedData) -> np.ndarray:
    """
    Return exact TreeSHAP values with shape ``(n_rows, n_features)``.

    CatBoost computes them natively (its base-value column is dropped), which
    also handles string categoricals. Random Forest uses ``shap.TreeExplainer``,
    imported lazily, with the additivity check disabled to avoid a second
    prediction pass and benign float mismatches.
    """
    if prepared.model == "catboost":
        from catboost import Pool

        pool = Pool(prepared.features, cat_features=prepared.categorical_columns)
        values = np.asarray(model.get_feature_importance(pool, type="ShapValues"))
        return values[:, :-1]

    try:
        import shap
    except ImportError as exc:  # pragma: no cover - depends on the host's installed packages
        raise ImportError(
            "The Random Forest SHAP plot needs the 'shap' package "
            "(pip install shap). The CatBoost path does not — it uses "
            "CatBoost's own native TreeSHAP implementation."
        ) from exc

    explainer = shap.TreeExplainer(model)
    values = explainer.shap_values(
        prepared.features.to_numpy(dtype=np.float64), check_additivity=False
    )
    return np.asarray(values)


def global_feature_importance(shap_values: np.ndarray, feature_names: Sequence[str]) -> pd.DataFrame:
    """
    Rank features by mean absolute SHAP value, the table behind plot (i).

    Returns
    -------
    pandas.DataFrame
        ``feature``, ``mean_abs_shap`` and ``share`` of the total, descending.
    """
    values = np.asarray(shap_values, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError(f"Expected a 2-D (n_rows, n_features) SHAP array, got shape {values.shape}")
    if values.shape[1] != len(feature_names):
        raise ValueError(
            f"SHAP array has {values.shape[1]} columns but {len(feature_names)} feature names were given"
        )
    mean_abs = np.abs(values).mean(axis=0)
    total = mean_abs.sum()
    importance = pd.DataFrame(
        {
            "feature": list(feature_names),
            "mean_abs_shap": mean_abs,
            "share": mean_abs / total if total > 0 else np.zeros_like(mean_abs),
        }
    )
    return importance.sort_values("mean_abs_shap", ascending=False).reset_index(drop=True)


def plot_global_feature_importance(
    importance: pd.DataFrame,
    out_path: Path,
    top_n: int = DEFAULT_TOP_N,
    title: str = "Global feature importance",
    subtitle: str = "",
    dpi: int = 140,
) -> Path:
    """Draw plot (i): the top `top_n` features as horizontal bars of mean absolute SHAP."""
    plt = _pyplot()
    _apply_chart_style(plt)

    top = importance.head(top_n).iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, max(3.0, 0.34 * len(top) + 1.6)))
    positions = np.arange(len(top))
    ax.barh(positions, top["mean_abs_shap"].to_numpy(), height=0.62, color=SERIES_BLUE, zorder=3)
    ax.set_yticks(positions)
    ax.set_yticklabels(top["feature"].tolist())
    ax.set_xlabel("mean |SHAP| (impact on the model's output)")
    ax.grid(axis="x", zorder=0)
    ax.grid(axis="y", visible=False)
    ax.set_axisbelow(True)

    span = float(top["mean_abs_shap"].max()) if len(top) else 0.0
    for pos, value, share in zip(positions, top["mean_abs_shap"], top["share"]):
        ax.text(
            value + span * 0.012,
            pos,
            f"{value:,.4g}  ({share:.1%})",
            va="center",
            ha="left",
            fontsize=7.5,
            color=INK_MUTED,
        )
    if span > 0:
        ax.set_xlim(0, span * 1.22)

    _title_block(ax, title, subtitle)
    fig.tight_layout()
    return _save(fig, out_path, dpi, plt)




DEFAULT_MIN_VARIATION_FRAC = 0.001
MIN_VARIATION_ROWS = 2


def _varies_enough(series: pd.Series, min_variation_frac: float) -> bool:
    """
    Return whether `series` varies enough for a correlation to mean anything.

    Near-constant columns (a handful of missing flags set) give correlations
    that flip between 0 and 1 across samples. The bar is a fraction of rows,
    with `MIN_VARIATION_ROWS` as a floor; such columns are only dropped from
    the heatmap.
    """
    values = series.dropna()
    if values.empty:
        return False
    modal_count = int(values.value_counts().iloc[0])
    floor = max(MIN_VARIATION_ROWS, int(np.ceil(min_variation_frac * len(values))))
    return (len(values) - modal_count) >= floor


def feature_correlation_matrix(
    prepared: PreparedData,
    method: str = "spearman",
    columns: Optional[Sequence[str]] = None,
    min_variation_frac: float = DEFAULT_MIN_VARIATION_FRAC,
) -> pd.DataFrame:
    """
    Return the correlation matrix of the numeric features fed to the model.

    Limited to `NUMERIC_FEATURE_COLUMNS` that vary enough (`_varies_enough`).
    Spearman by default, because many features are skewed or ordinal.
    """
    frame = prepared.features
    candidates = list(columns) if columns is not None else list(prepared.numeric_columns)
    usable = [
        c
        for c in candidates
        if c in frame.columns and _varies_enough(frame[c].astype("float64"), min_variation_frac)
    ]
    numeric = frame[usable].astype("float64")
    return numeric.corr(method=method)


def redundant_feature_pairs(
    corr: pd.DataFrame, threshold: float = DEFAULT_CORR_THRESHOLD
) -> List[Tuple[str, str, float]]:
    """Return every distinct feature pair with ``|corr| >= threshold``, most correlated first."""
    values = corr.to_numpy()
    names = list(corr.columns)
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            value = values[i, j]
            if np.isfinite(value) and abs(value) >= threshold:
                pairs.append((names[i], names[j], float(value)))
    return sorted(pairs, key=lambda pair: abs(pair[2]), reverse=True)


def _cluster_order(corr: pd.DataFrame) -> List[str]:
    """
    Return a feature order that places mutually correlated features together.

    Average-linkage clustering on ``1 - |corr|``; falls back to the existing
    order when scipy is missing or the matrix is degenerate.
    """
    if len(corr) < 3:
        return list(corr.columns)
    try:
        from scipy.cluster.hierarchy import leaves_list, linkage
        from scipy.spatial.distance import squareform
    except ImportError:  # pragma: no cover - scipy ships with scikit-learn
        return list(corr.columns)

    distance = 1.0 - corr.abs().to_numpy()
    distance = np.nan_to_num(distance, nan=1.0)
    distance = (distance + distance.T) / 2.0
    np.fill_diagonal(distance, 0.0)
    try:
        order = leaves_list(linkage(squareform(distance, checks=False), method="average"))
    except ValueError:  # pragma: no cover - degenerate matrices only
        return list(corr.columns)
    return [corr.columns[i] for i in order]


def plot_feature_correlation_heatmap(
    corr: pd.DataFrame,
    out_path: Path,
    title: str = "Feature correlation heatmap",
    subtitle: str = "",
    cluster: bool = True,
    annotate_max_features: int = 20,
    dpi: int = 140,
) -> Path:
    """
    Draw plot (ii): the correlation matrix as a diverging heatmap fixed to [-1, 1].

    Cells are annotated only when there are at most `annotate_max_features`.
    """
    plt = _pyplot()
    _apply_chart_style(plt)

    if cluster:
        order = _cluster_order(corr)
        corr = corr.loc[order, order]

    n = len(corr)
    size = max(6.5, min(26.0, 0.32 * n + 3.0))
    fig, ax = plt.subplots(figsize=(size, size * 0.92))
    ax.grid(visible=False)
    image = ax.imshow(corr.to_numpy(), cmap=_diverging_colormap(), vmin=-1.0, vmax=1.0, aspect="auto")

    labels = list(corr.columns)
    tick_size = 7.5 if n <= 30 else 6.0
    ax.set_xticks(np.arange(n))
    ax.set_yticks(np.arange(n))
    ax.set_xticklabels(labels, rotation=90, fontsize=tick_size)
    ax.set_yticklabels(labels, fontsize=tick_size)
    ax.set_xticks(np.arange(n + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(n + 1) - 0.5, minor=True)
    ax.tick_params(which="minor", length=0)
    ax.grid(which="minor", color=SURFACE, linewidth=1.4, visible=True)

    if n <= annotate_max_features:
        for i in range(n):
            for j in range(n):
                value = corr.iat[i, j]
                if not np.isfinite(value):
                    continue
                ax.text(
                    j,
                    i,
                    f"{value:.2f}",
                    ha="center",
                    va="center",
                    fontsize=6.5,
                    color=INK_PRIMARY if abs(value) < 0.6 else SURFACE,
                )

    bar = fig.colorbar(image, ax=ax, fraction=0.026, pad=0.02)
    bar.set_label("correlation coefficient", color=INK_SECONDARY, fontsize=9)
    bar.outline.set_edgecolor(AXIS_LINE)
    bar.ax.tick_params(colors=INK_MUTED, labelsize=8)

    _title_block(ax, title, subtitle)
    fig.tight_layout()
    return _save(fig, out_path, dpi, plt)




def univariate_price_response(
    values: Sequence[float], prices: Sequence[float], bins: int = DEFAULT_UNIVARIATE_BINS
) -> pd.DataFrame:
    """
    Return one feature's binned price response.

    Quantile bins, or exact values for features with at most `bins` levels.

    Returns
    -------
    pandas.DataFrame
        ``x``, ``median_price``, ``q25``, ``q75`` and ``count`` per bin.
    """
    series = pd.Series(np.asarray(values, dtype="float64"))
    target = pd.Series(np.asarray(prices, dtype="float64"))
    keep = series.notna() & target.notna()
    series, target = series[keep], target[keep]
    if series.empty:
        return pd.DataFrame(columns=["x", "median_price", "q25", "q75", "count"])

    if series.nunique() <= bins:
        groups = series
    else:
        groups = pd.qcut(series, q=bins, duplicates="drop")

    grouped = target.groupby(groups, observed=True)
    table = pd.DataFrame(
        {
            "x": series.groupby(groups, observed=True).mean(),
            "median_price": grouped.median(),
            "q25": grouped.quantile(0.25),
            "q75": grouped.quantile(0.75),
            "count": grouped.size(),
        }
    )
    return table.sort_values("x").reset_index(drop=True)


def rank_features_by_price_correlation(
    prepared: PreparedData, method: str = "spearman", columns: Optional[Sequence[str]] = None
) -> pd.DataFrame:
    """Rank numeric features by absolute correlation with price, strongest first."""
    frame = prepared.features
    candidates = list(columns) if columns is not None else list(prepared.numeric_columns)
    target = pd.Series(prepared.prices, index=frame.index)
    rows = []
    for col in candidates:
        if col not in frame.columns:
            continue
        series = frame[col].astype("float64")
        if series.nunique(dropna=True) <= 1:
            continue
        value = series.corr(target, method=method)
        rows.append({"feature": col, "correlation": float(value) if np.isfinite(value) else 0.0})
    ranked = pd.DataFrame(rows, columns=["feature", "correlation"])
    if ranked.empty:
        return ranked.assign(abs_correlation=pd.Series(dtype="float64"))
    ranked["abs_correlation"] = ranked["correlation"].abs()
    return ranked.sort_values("abs_correlation", ascending=False).reset_index(drop=True)


def select_univariate_features(
    prepared: PreparedData,
    limit: int,
    method: str = "spearman",
    dedupe_threshold: Optional[float] = DEFAULT_CORR_THRESHOLD,
) -> List[str]:
    """
    Choose the features plot (iii) draws.

    Takes the strongest price correlations first, skipping any feature already
    `dedupe_threshold`-correlated with a chosen one, so near-duplicate price
    statistics do not fill the figure. Pass None to disable de-duplication.
    """
    ranked = rank_features_by_price_correlation(prepared, method=method)
    candidates = ranked["feature"].tolist()
    if not dedupe_threshold:
        return candidates[:limit]

    corr = feature_correlation_matrix(prepared, method=method, columns=candidates)
    chosen: List[str] = []
    for feature in candidates:
        if len(chosen) >= limit:
            break
        if feature not in corr.columns:
            continue
        if any(abs(corr.at[feature, kept]) >= dedupe_threshold for kept in chosen):
            continue
        chosen.append(feature)
    return chosen


def plot_univariate_price_response(
    prepared: PreparedData,
    features: Sequence[str],
    out_path: Path,
    bins: int = DEFAULT_UNIVARIATE_BINS,
    ncols: int = 4,
    title: str = "Univariate price response",
    subtitle: str = "",
    dpi: int = 140,
) -> Path:
    """
    Draw plot (iii): median sale price against each feature, one panel each.

    The shaded band is the 25th-75th percentile of price per bin.
    """
    plt = _pyplot()
    _apply_chart_style(plt)
    from matplotlib.ticker import MaxNLocator

    features = [f for f in features if f in prepared.features.columns]
    if not features:
        raise ValueError("No plottable numeric features were selected")

    ncols = max(1, min(ncols, len(features)))
    nrows = int(np.ceil(len(features) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.5 * ncols, 2.7 * nrows), squeeze=False)

    for index, feature in enumerate(features):
        ax = axes[index // ncols][index % ncols]
        table = univariate_price_response(prepared.features[feature], prepared.prices, bins=bins)
        if table.empty:
            ax.set_axis_off()
            continue
        ax.fill_between(
            table["x"], table["q25"], table["q75"], color=SERIES_BLUE_LIGHT, alpha=0.45, linewidth=0, zorder=2
        )
        ax.plot(table["x"], table["median_price"], color=SERIES_BLUE, linewidth=2.0, zorder=3)
        ax.scatter(table["x"], table["median_price"], s=14, color=SERIES_BLUE, zorder=4)
        ax.set_title(feature, loc="left", fontsize=9, color=INK_PRIMARY)
        ax.set_axisbelow(True)
        ax.yaxis.set_major_formatter(lambda value, _pos: f"£{value / 1000:,.0f}k")
        ax.xaxis.set_major_locator(MaxNLocator(nbins=5, prune="both"))
        ax.xaxis.set_major_formatter(lambda value, _pos: _compact_number(value))
        ax.tick_params(labelsize=7)

    for index in range(len(features), nrows * ncols):
        axes[index // ncols][index % ncols].set_axis_off()

    height = fig.get_figheight()
    header = "line: median sale price per bin · band: 25th-75th percentile"
    if subtitle:
        header = f"{subtitle} · {header}"
    fig.suptitle(
        title, x=0.006, y=1 - 0.24 / height, ha="left", va="top", fontsize=13, fontweight="bold", color=INK_PRIMARY
    )
    fig.text(0.006, 1 - 0.50 / height, header, ha="left", va="top", fontsize=8.5, color=INK_SECONDARY)
    fig.tight_layout(rect=(0, 0, 1, 1 - 0.70 / height))
    return _save(fig, out_path, dpi, plt)


def _save(fig, out_path: Path, dpi: int, plt) -> Path:
    """Save and close `fig` at `out_path`, creating the directory, and return the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return out_path




def generate_visualizations(
    df,
    args: argparse.Namespace,
    mortgage_df: Optional[pd.DataFrame] = None,
    log: Optional[logging.Logger] = None,
) -> Dict:
    """
    Run the requested plots for the requested models over a loaded DataFrame.

    Parameters
    ----------
    df : pandas.DataFrame
        Rows shaped like ``production.london_houses``.
    args : argparse.Namespace
        Parsed command-line options.
    mortgage_df : pandas.DataFrame, optional
        ``production.mortgage_rates``, forwarded to
        `prepare_visualization_data`.
    log : logging.Logger, optional
        Logger for progress messages.

    Returns
    -------
    dict
        Per model, the paths written and the findings behind them (top SHAP
        features, redundant pairs).
    """
    models = ["rf", "catboost"] if args.model == "both" else [args.model]
    plots = list(args.plots)
    out_dir = Path(args.out_dir)
    report: Dict[str, Dict] = {}

    for model in models:
        prepared = prepare_visualization_data(df, args, model, mortgage_df, log=log)
        label = "Random Forest" if model == "rf" else "CatBoost"
        context = (
            f"{label} · {len(prepared.features):,} rows of the '{prepared.split}' split "
            f"· {args.split_method} split · {VIEW_NAME}"
        )
        entry: Dict = {
            "split": prepared.split,
            "n_rows": int(len(prepared.features)),
            "n_features": int(len(prepared.feature_names)),
            "outputs": {},
        }

        if "importance" in plots:
            shap_data = subsample(prepared, args.shap_sample, random_state=args.random_state)
            if args.model_in:
                fitted = load_reference_model(Path(args.model_in), model, getattr(args, "trust_pickle", False))
            else:
                fitted = fit_reference_model(
                    subsample(prepared, args.fit_sample, random_state=args.random_state), args, log=log
                )
            importance = global_feature_importance(
                compute_shap_values(fitted, shap_data), prepared.feature_names
            )
            units = "log1p(£)" if args.log_target else "£"
            path = plot_global_feature_importance(
                importance,
                out_dir / f"{model}_shap_feature_importance.{args.format}",
                top_n=args.top_n,
                title=f"{label}: global feature importance (mean |SHAP|)",
                subtitle=f"{context} · {len(shap_data.features):,} rows explained · units: {units}",
                dpi=args.dpi,
            )
            csv_path = out_dir / f"{model}_shap_feature_importance.csv"
            importance.to_csv(csv_path, index=False)
            entry["outputs"]["importance"] = str(path)
            entry["outputs"]["importance_csv"] = str(csv_path)
            entry["top_features"] = importance.head(args.top_n).to_dict(orient="records")
            if log is not None:
                log.info(f"Wrote {path}")

        analysis = subsample(prepared, args.analysis_sample, random_state=args.random_state)

        if "correlation" in plots:
            corr = feature_correlation_matrix(
                analysis, method=args.corr_method, min_variation_frac=args.min_variation_frac
            )
            pairs = redundant_feature_pairs(corr, threshold=args.corr_threshold)
            path = plot_feature_correlation_heatmap(
                corr,
                out_dir / f"{model}_feature_correlation_heatmap.{args.format}",
                title=f"{label}: feature correlation ({args.corr_method})",
                subtitle=f"{context} · {len(corr)} numeric features",
                cluster=args.cluster_heatmap,
                dpi=args.dpi,
            )
            entry["outputs"]["correlation"] = str(path)
            entry["redundant_pairs"] = [
                {"feature_a": a, "feature_b": b, "correlation": round(v, 4)} for a, b, v in pairs
            ]
            if log is not None:
                log.info(f"Wrote {path} ({len(pairs)} pairs at |r| >= {args.corr_threshold})")

        if "univariate" in plots:
            chosen = args.features or select_univariate_features(
                analysis,
                args.univariate_features,
                method=args.corr_method,
                dedupe_threshold=args.corr_threshold if args.univariate_dedupe else None,
            )
            path = plot_univariate_price_response(
                analysis,
                chosen,
                out_dir / f"{model}_univariate_price_response.{args.format}",
                bins=args.univariate_bins,
                ncols=args.univariate_cols,
                title=f"{label}: univariate price response",
                subtitle=context,
                dpi=args.dpi,
            )
            entry["outputs"]["univariate"] = str(path)
            entry["univariate_features"] = chosen
            if log is not None:
                log.info(f"Wrote {path} ({len(chosen)} features)")

        report[model] = entry

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Visualise the data fed to the Random Forest (hpp_rf) and CatBoost (hpp_cb) "
            "house-price models: SHAP feature importance, a feature correlation heatmap, "
            "and univariate price-response plots."
        )
    )
    parser.add_argument(
        "--model",
        choices=["rf", "catboost", "both"],
        default="rf",
        help="Which model's prepared feature matrix to visualise (default: rf)",
    )
    parser.add_argument(
        "--plots",
        nargs="+",
        choices=list(PLOT_CHOICES),
        default=list(PLOT_CHOICES),
        help="Which plots to render (default: all three)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("tmp/plots"), help="Directory to write figures into")
    parser.add_argument("--format", choices=["png", "svg", "pdf"], default="png")
    parser.add_argument("--dpi", type=int, default=140)

    parser.add_argument("--limit", type=int, default=None, help="Cap the number of rows loaded, for a quick run")
    parser.add_argument(
        "--analysis-split",
        choices=["train", "val", "test"],
        default="train",
        help=(
            "Which split to visualise (default: train — the data actually fed to the model; "
            "val/test show what it is asked to generalise to)"
        ),
    )
    parser.add_argument(
        "--analysis-sample",
        type=int,
        default=DEFAULT_ANALYSIS_SAMPLE,
        help="Rows sampled for the correlation/univariate plots (0 = all)",
    )
    parser.add_argument(
        "--shap-sample",
        type=int,
        default=DEFAULT_SHAP_SAMPLE,
        help="Rows explained by TreeSHAP (0 = all; TreeSHAP is per-row, so this dominates runtime)",
    )
    parser.add_argument(
        "--fit-sample",
        type=int,
        default=DEFAULT_ANALYSIS_SAMPLE,
        help="Rows the reference model for the SHAP plot is fitted on (0 = all)",
    )

    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="Features shown in the importance chart")
    parser.add_argument(
        "--corr-method", choices=["spearman", "pearson", "kendall"], default="spearman"
    )
    parser.add_argument(
        "--corr-threshold",
        type=float,
        default=DEFAULT_CORR_THRESHOLD,
        help="|r| at or above which a feature pair is reported as redundant",
    )
    parser.add_argument(
        "--min-variation-frac",
        type=float,
        default=DEFAULT_MIN_VARIATION_FRAC,
        help=(
            "Heatmap only: a feature whose rarer value covers less than this fraction of rows is "
            "dropped, since a correlation against a near-constant column is uninterpretable"
        ),
    )
    parser.add_argument("--cluster-heatmap", dest="cluster_heatmap", action="store_true", default=True)
    parser.add_argument(
        "--no-cluster-heatmap",
        dest="cluster_heatmap",
        action="store_false",
        help="Keep the feature matrix's own column order instead of clustering correlated features together",
    )
    parser.add_argument(
        "--univariate-features",
        type=int,
        default=DEFAULT_UNIVARIATE_FEATURES,
        help="How many features to draw price-response panels for, ranked by |correlation with price|",
    )
    parser.add_argument(
        "--features", nargs="+", default=None, help="Draw price-response panels for exactly these features"
    )
    parser.add_argument("--dedupe-univariate", dest="univariate_dedupe", action="store_true", default=True)
    parser.add_argument(
        "--no-dedupe-univariate",
        dest="univariate_dedupe",
        action="store_false",
        help=(
            "Keep the raw |correlation with price| ranking for the price-response panels instead of "
            "skipping features already represented by a --corr-threshold-correlated one"
        ),
    )
    parser.add_argument("--univariate-bins", type=int, default=DEFAULT_UNIVARIATE_BINS)
    parser.add_argument("--univariate-cols", type=int, default=4, help="Panels per row")

    parser.add_argument(
        "--model-in",
        type=Path,
        default=None,
        help="Rank SHAP importance for a model saved by hpp_rf/hpp_cb --model-out instead of fitting a fresh one",
    )
    parser.add_argument(
        "--trust-pickle",
        action="store_true",
        help="Allow --model-in to load an hpp_rf pickle (runs code from the file; only for models you trained)",
    )
    parser.add_argument("--viz-n-estimators", type=int, default=100, help="Reference Random Forest size")
    parser.add_argument("--viz-max-depth", type=int, default=12, help="Reference Random Forest depth")
    parser.add_argument("--min-samples-leaf", type=int, default=1)
    parser.add_argument("--max-features", default="sqrt")
    parser.add_argument("--n-jobs", type=int, default=-1)
    parser.add_argument("--viz-iterations", type=int, default=300, help="Reference CatBoost boosting rounds")
    parser.add_argument("--viz-depth", type=int, default=6, help="Reference CatBoost tree depth")

    parser.add_argument("--device", choices=["cpu", "gpu", "auto"], default="auto", help="CatBoost only: where to train (default: auto-detect)")
    parser.add_argument("--learning-rate", type=float, default=None, help="CatBoost only: CatBoost default (auto-tuned) when unset")
    parser.add_argument("--l2-leaf-reg", type=float, default=3.0, help="CatBoost only")
    parser.add_argument("--thread-count", type=int, default=-1, help="CatBoost only: CPU backend only")
    parser.add_argument("--loss-function", choices=["RMSE", "MAE", "Huber"], default="RMSE", help="CatBoost only")
    parser.add_argument("--huber-delta", type=float, default=1.0, help="CatBoost only: --loss-function Huber only")
    parser.add_argument("--max-ctr-complexity", type=int, default=None, help="CatBoost only: CatBoost default when unset")
    parser.add_argument("--one-hot-max-size", type=int, default=None, help="CatBoost only: CatBoost default when unset")
    parser.add_argument(
        "--nan-mode",
        choices=["impute", "native"],
        default="impute",
        help=(
            "CatBoost only: 'impute' (default) train-median fills every nullable numeric + a "
            "missing flag per family, matching the Random Forest matrix; 'native' leaves numeric "
            "NaN untouched for CatBoost's own nan_mode=\"Min\" per-split handling, keeping only "
            "prev_sale_missing/comp_missing flags — see hpp_cb.prepare_features"
        ),
    )
    parser.add_argument(
        "--drop-features",
        type=lambda s: [c.strip() for c in s.split(",") if c.strip()],
        default=[],
        help="CatBoost only: comma-separated column/flag names to exclude, exactly hpp_cb's --drop-features",
    )
    parser.add_argument(
        "--sample-weight",
        choices=["none", "sqrt", "full"],
        default="none",
        help="CatBoost only: inverse-price-band weight for the reference SHAP fit — see hpp_cb._sample_weight_for_training",
    )
    parser.add_argument(
        "--no-iod",
        dest="use_iod",
        action="store_false",
        default=True,
        help="CatBoost only: drop every production.iod_features column from the feature matrix (see hpp_cb --no-iod)",
    )
    parser.add_argument(
        "--no-ofsted",
        dest="use_ofsted",
        action="store_false",
        default=True,
        help="Drop the Ofsted school features (see hpp_cb/hpp_rf --no-ofsted); off anyway on a view before 0034",
    )

    parser.add_argument("--train-frac", type=float, default=0.7)
    parser.add_argument("--val-frac", type=float, default=0.15)
    parser.add_argument(
        "--split-method",
        choices=["chronological", "grouped", "purged", "walk_forward", "spatial", "recency"],
        default="chronological",
        help="Same methods, same semantics, as hpp_rf/hpp_cb --split-method",
    )
    parser.add_argument("--embargo-days", type=int, default=30, help="--split-method purged only")
    parser.add_argument("--cv-folds", type=int, default=4, help="--split-method walk_forward only")
    parser.add_argument("--spatial-test-frac", type=float, default=0.15, help="--split-method spatial only")
    parser.add_argument("--spatial-val-frac", type=float, default=0.15, help="--split-method spatial only")
    parser.add_argument("--train-window-years", type=float, default=5.0, help="--split-method recency only")
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument(
        "--log-target",
        dest="log_target",
        action="store_true",
        default=True,
        help="Fit the reference model on log1p(price), matching hpp_rf/hpp_cb's default",
    )
    parser.add_argument("--no-log-target", dest="log_target", action="store_false")
    parser.add_argument("--report-out", type=Path, default=None, help="Write the JSON report to this path too")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> Dict:
    """Load the dataset, generate the plots and print the JSON report."""
    args = build_arg_parser().parse_args(argv)
    log = get_logger(args.verbose)
    hpp_rf.set_global_seed(args.random_state)
    load_env_vars(log)
    from sqlalchemy import create_engine

    engine = create_engine(get_db_url(log))
    df = hpp_rf.load_dataset(engine, limit=args.limit, log=log)
    mortgage_df = hpp_cb.load_mortgage_rates(engine, log=log)
    report = generate_visualizations(df, args, mortgage_df, log=log)
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(json.dumps(report, indent=2))
        log.info(f"Wrote {args.report_out}")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
