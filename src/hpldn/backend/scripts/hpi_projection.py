"""
Project the UK HPI forward, so a sale date beyond the published index moves the price.

The served CatBoost model trains on ``target="deflated"``: ``log1p(price)``
minus ``log(ukhpi_avg_price_at_sale)`` (`hpp_cb._target_transform`). Because of
that, ``sale_year`` is dropped from the feature set and ``sale_month`` becomes a
sin/cos pair, so **the deflator is the only channel through which the sale date
can move the price**. Carrying the last published index forward unchanged
would make a 2031 price equal tomorrow's.

This module supplies the missing piece: a damped-blend projection of the index,
fitted per property type against `production.ukhpi` itself.

The method, in log space, at the last published period:

    g_recent  OLS slope of log(avg_price) over the last `recent_years`
    g_long    CAGR over the last `long_years`
    uplift(H) shrink * [ g_long*H + (g_recent - g_long) * tau * (1 - e^(-H/tau)) ]

with ``tau = half_life_years / ln 2``. The recent trend therefore rules the near
horizon and decays into the long-run anchor, and `shrink` pulls the whole path
toward no growth.

Nothing here is hand-chosen. `fit` grid-searches the four hyperparameters
against a blocked cross-validation of the index: the origin timeline is cut into
`CV_FOLDS` contiguous blocks and, for each, the parameters are chosen on the
origins outside it -- embargoed by the horizon, so no training origin's outcome
reaches into the test block -- and scored on the block itself.

A single held-out tail was tried first and rejected: five years of monthly
origins is one overlapping window at the longest horizon, barely one
independent observation, and it judged the fit entirely by whichever market
that window happened to contain.

A property type is projected **only where its cross-validated error beats
carrying the index forward at every horizon**; otherwise it carries forward,
which is what the code did before. On the London index all four types pass, by
a widening margin -- at five years 0.147 against 0.187 for flats and 0.130
against 0.228 for detached houses. That same per-horizon error table is what
widens the price range with the forecast horizon
(`hpp_inference._range_from_calibration`).
"""

import logging
import math
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# The index column each property type is deflated by, and the fallback for a
# type the index does not break out. Mirrors `hpp_inference.UKHPI_PRICE_COLUMN_BY_TYPE`.
PRICE_COLUMN_BY_TYPE = {
    "D": "avg_price_detached",
    "S": "avg_price_semi_detached",
    "T": "avg_price_terraced",
    "F": "avg_price_flats_maisonettes",
}
ALL_TYPES_COLUMN = "avg_price_all_property_types"

# `*.cbm.projection.json`, beside `*.cbm.calibration.json`.
# The projection's file in a model bundle (`models.artifact.PROJECTION_FILE`).
PROJECTION_FILE = "projection.json"

# The horizons the backtest scores and the range widens at, in years. The API
# caps a sale date at five years out (`hpp_inference.MAX_HORIZON_DAYS`).
HORIZONS_YEARS: Tuple[int, ...] = (1, 2, 3, 4, 5)

# The hyperparameter grid. Deliberately coarse: it is searched against a
# backtest with a few hundred origins, and a fine grid would fit the noise.
GRID_RECENT_YEARS: Tuple[int, ...] = (3, 5, 8)
GRID_LONG_YEARS: Tuple[int, ...] = (15, 20, 30)
GRID_HALF_LIFE_YEARS: Tuple[float, ...] = (1.0, 1.5, 3.0)
GRID_SHRINK: Tuple[float, ...] = (0.4, 0.6, 0.8, 1.0)

# An origin needs this much history behind it before the longest lookback is
# anything but truncated, so the backtest starts here.
MIN_HISTORY_MONTHS = 120

# Contiguous blocks of the origin timeline used to cross-validate the fit.
CV_FOLDS = 5

MONTHS_PER_YEAR = 12

# Mean absolute deviation to standard deviation, for a normal: E|X| = sigma * sqrt(2/pi).
_MAE_TO_SIGMA = math.sqrt(math.pi / 2.0)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectionParams:
    """The four hyperparameters of one property type's damped blend."""

    recent_years: int
    long_years: int
    half_life_years: float
    shrink: float

    @property
    def tau(self) -> float:
        """The exponential time constant the half-life implies."""
        return self.half_life_years / math.log(2.0)


# Carrying the index forward unchanged is `shrink = 0`; the other three are
# then irrelevant, and this is what a type that fails the gate falls back to.
CARRY_FORWARD = ProjectionParams(recent_years=5, long_years=20, half_life_years=1.5, shrink=0.0)


def log_index(frame: pd.DataFrame, propertytype: Optional[str]) -> pd.Series:
    """
    Return ``log(avg_price)`` for `propertytype`, monthly, oldest first.

    Falls back to the all-types column for a type the index does not break out,
    and for any month where the type's own column is missing.
    """
    column = PRICE_COLUMN_BY_TYPE.get(propertytype or "", ALL_TYPES_COLUMN)
    ordered = frame.sort_values("period")
    values = pd.to_numeric(ordered.get(column), errors="coerce")
    if values is None or values.isna().all():
        values = pd.to_numeric(ordered[ALL_TYPES_COLUMN], errors="coerce")
    else:
        values = values.fillna(pd.to_numeric(ordered[ALL_TYPES_COLUMN], errors="coerce"))
    series = pd.Series(
        np.log(values.to_numpy(dtype=np.float64)),
        index=pd.to_datetime(ordered["period"]).to_numpy(),
    )
    return series[np.isfinite(series.to_numpy())]


def _rate_arrays(y: np.ndarray, recent_years: int, long_years: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Return ``(g_recent, g_long)`` at every position of the log index, per year.

    `g_recent` is the OLS slope over the trailing `recent_years` and `g_long`
    the annualised change over `long_years`; both are truncated at the start of
    the series rather than extrapolated behind it. Computed for every position
    at once because the grid search scores the same two windows under many
    damping and shrinkage settings.
    """
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    positions = np.arange(n, dtype=np.int64)

    # The trailing OLS slope from running sums, so a local index with a few
    # hundred series fits in minutes rather than hours. Within a window the
    # regressor is k = j - start (0 .. m-1), in months; the slope is scaled to
    # a year at the end. Identical to `np.polyfit` over the same window.
    start = np.maximum(0, positions - recent_years * MONTHS_PER_YEAR + 1)
    m = (positions - start + 1).astype(np.float64)
    c0 = np.concatenate(([0.0], np.cumsum(y)))
    c1 = np.concatenate(([0.0], np.cumsum(positions * y)))
    sum_y = c0[positions + 1] - c0[start]
    sum_ky = (c1[positions + 1] - c1[start]) - start * sum_y
    sum_k = m * (m - 1.0) / 2.0
    sum_kk = (m - 1.0) * m * (2.0 * m - 1.0) / 6.0
    denominator = m * sum_kk - sum_k**2
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(m >= 2, (m * sum_ky - sum_k * sum_y) / denominator, 0.0)
    g_recent = np.where(np.isfinite(slope), slope * MONTHS_PER_YEAR, 0.0)

    j = np.maximum(0, positions - long_years * MONTHS_PER_YEAR)
    span_years = (positions - j) / MONTHS_PER_YEAR
    with np.errstate(divide="ignore", invalid="ignore"):
        g_long = np.where(span_years > 0, (y - y[j]) / span_years, 0.0)
    return g_recent, g_long


def _rates(y: np.ndarray, i: int, params: ProjectionParams) -> Tuple[float, float]:
    """Return ``(g_recent, g_long)`` at one position, as `_rate_arrays` computes them."""
    g_recent, g_long = _rate_arrays(y, params.recent_years, params.long_years)
    return float(g_recent[i]), float(g_long[i])


def cumulative_uplift(g_recent: float, g_long: float, params: ProjectionParams, horizon_years: float) -> float:
    """
    Return the cumulative **log** uplift over `horizon_years`.

    The integral of a rate that starts at `g_recent` and decays exponentially
    to `g_long`, scaled by `shrink`. Zero at a horizon of zero or less.
    """
    if horizon_years <= 0:
        return 0.0
    tau = params.tau
    damped = (g_recent - g_long) * tau * (1.0 - math.exp(-horizon_years / tau))
    return params.shrink * (g_long * horizon_years + damped)


def _errors_over(
    y: np.ndarray,
    origins: np.ndarray,
    horizon: int,
    g_recent: np.ndarray,
    g_long: np.ndarray,
    half_life_years: float,
    shrink: float,
) -> float:
    """Return the mean absolute log error of one setting at one horizon, over `origins`."""
    if origins.size == 0:
        return float("nan")
    tau = half_life_years / math.log(2.0)
    decay = tau * (1.0 - math.exp(-horizon / tau))
    uplift = shrink * (g_long[origins] * horizon + (g_recent[origins] - g_long[origins]) * decay)
    outcome = y[origins + horizon * MONTHS_PER_YEAR]
    return float(np.mean(np.abs(y[origins] + uplift - outcome)))


def _all_origins(n: int, horizon: int) -> np.ndarray:
    """Return every origin that has both enough history behind it and an outcome ahead."""
    stop = n - horizon * MONTHS_PER_YEAR
    if stop <= MIN_HISTORY_MONTHS:
        return np.empty(0, dtype=np.int64)
    return np.arange(MIN_HISTORY_MONTHS, stop, dtype=np.int64)


def _select(
    y: np.ndarray,
    origins_for: Mapping[int, np.ndarray],
    rate_cache: Mapping[Tuple[int, int], Tuple[np.ndarray, np.ndarray]],
    horizons: Sequence[int],
) -> ProjectionParams:
    """Return the grid point with the lowest mean error across horizons over the given origins."""
    best: Optional[Tuple[float, ProjectionParams]] = None
    for recent, long_ in rate_cache:
        g_recent, g_long = rate_cache[(recent, long_)]
        for half_life, shrink in product(GRID_HALF_LIFE_YEARS, GRID_SHRINK):
            scores = [
                _errors_over(y, origins_for[h], h, g_recent, g_long, half_life, shrink)
                for h in horizons
            ]
            score = float(np.nanmean(scores))
            if best is None or score < best[0]:
                best = (score, ProjectionParams(recent, long_, half_life, shrink))
    if best is None:
        # The grid is a non-empty constant, so this cannot happen -- but
        # returning carry-forward is the safe answer if it ever did.
        return CARRY_FORWARD
    return best[1]


def fit_type(
    y: pd.Series,
    horizons: Sequence[int] = HORIZONS_YEARS,
    folds: int = CV_FOLDS,
) -> Optional[dict]:
    """
    Fit one property type by blocked cross-validation, or None when the series is too short.

    A single held-out tail would be one overlapping five-year window — barely
    one independent observation at the longest horizon, and entirely at the
    mercy of whichever market that window happened to contain. Instead the
    origin timeline is cut into `folds` contiguous blocks; for each block the
    hyperparameters are chosen on the origins outside it (embargoed by the
    horizon, so no training origin's outcome reaches into the test block) and
    scored on the block itself. The reported error is the mean across blocks:
    an honest estimate of how the *procedure* generalises, measured over the
    whole index rather than one window.

    The parameters returned are then refitted on every origin, which is the
    usual refit-on-all-data step once a procedure has been validated.
    """
    values = y.to_numpy(dtype=np.float64)
    n = values.size
    origins_all = {h: _all_origins(n, h) for h in horizons}
    if any(origins.size < folds * 2 for origins in origins_all.values()):
        log.warning("hpi_projection: only %d months of index; not enough to cross-validate", n)
        return None

    rate_cache = {
        (recent, long_): _rate_arrays(values, recent, long_)
        for recent, long_ in product(GRID_RECENT_YEARS, GRID_LONG_YEARS)
    }

    # Blocks are cut on the origin timeline shared by every horizon, so one
    # block means the same stretch of history at 1 year as at 5.
    shortest = origins_all[max(horizons)]
    edges = np.linspace(shortest[0], shortest[-1] + 1, folds + 1).astype(np.int64)

    fold_errors: Dict[int, list] = {h: [] for h in horizons}
    baseline_errors: Dict[int, list] = {h: [] for h in horizons}
    for k in range(folds):
        lo, hi = int(edges[k]), int(edges[k + 1])
        test = {h: origins_all[h][(origins_all[h] >= lo) & (origins_all[h] < hi)] for h in horizons}
        if any(t.size == 0 for t in test.values()):
            continue
        # Embargo: drop training origins whose outcome lands inside the test
        # block, and whose own position sits inside it.
        train = {}
        for h in horizons:
            offset = h * MONTHS_PER_YEAR
            o = origins_all[h]
            keep = (o + offset < lo) | (o >= hi)
            train[h] = o[keep]
        if any(t.size == 0 for t in train.values()):
            continue
        chosen = _select(values, train, rate_cache, horizons)
        g_recent, g_long = rate_cache[(chosen.recent_years, chosen.long_years)]
        for h in horizons:
            fold_errors[h].append(
                _errors_over(values, test[h], h, g_recent, g_long, chosen.half_life_years, chosen.shrink)
            )
            baseline_errors[h].append(
                _errors_over(values, test[h], h, g_recent, g_long, CARRY_FORWARD.half_life_years, 0.0)
            )

    if any(not fold_errors[h] for h in horizons):
        log.warning("hpi_projection: cross-validation produced no usable folds")
        return None

    cv_error = {h: float(np.mean(fold_errors[h])) for h in horizons}
    cv_baseline = {h: float(np.mean(baseline_errors[h])) for h in horizons}
    beats_baseline = all(cv_error[h] <= cv_baseline[h] for h in horizons)

    final = _select(values, origins_all, rate_cache, horizons)
    return {
        "params": asdict(final),
        "beats_carry_forward": bool(beats_baseline),
        "folds": folds,
        "origins": {str(h): int(origins_all[h].size) for h in horizons},
        # Mean absolute log error across the cross-validation blocks, per horizon in years.
        "holdout_error": {str(h): round(cv_error[h], 4) for h in horizons},
        "carry_forward_error": {str(h): round(cv_baseline[h], 4) for h in horizons},
    }


def fit(frame: pd.DataFrame, horizons: Sequence[int] = HORIZONS_YEARS) -> dict:
    """
    Fit every property type against the index `frame` and return the sidecar payload.

    `frame` is ``production.ukhpi`` with `period` and the ``avg_price_*``
    columns. A type whose cross-validated error does not beat carrying the
    index forward at every horizon is recorded with ``beats_carry_forward:
    false`` and is not projected.
    """
    ordered = frame.sort_values("period")
    types: Dict[str, dict] = {}
    for propertytype in sorted(PRICE_COLUMN_BY_TYPE):
        fitted = fit_type(log_index(ordered, propertytype), horizons)
        if fitted is not None:
            types[propertytype] = fitted
    return {
        "method": "damped_blend",
        "fitted_from": str(pd.Timestamp(ordered["period"].min()).date()),
        "index_to": str(pd.Timestamp(ordered["period"].max()).date()),
        "horizons_years": list(horizons),
        "cv_folds": CV_FOLDS,
        "types": types,
    }


def projection_path(model_path) -> Path:
    """Return the projection file in a model bundle."""
    return Path(model_path) / PROJECTION_FILE


def load_params(model_path) -> Optional[dict]:
    """
    Return the projection in the bundle at `model_path`, or None when there is none.

    Read through `models.artifact.read_verified_json`: the manifest's signature
    and the file's hash are checked first. A bundle that fails them is logged
    and treated as having no projection.
    """
    from hpldn.backend.models import artifact

    try:
        return artifact.read_verified_json(model_path, PROJECTION_FILE)
    except (OSError, ValueError) as exc:
        log.warning("hpi_projection: cannot read %s (%s); carrying the index forward", projection_path(model_path), exc)
        return None


def fitted_sigma(fitted: Mapping, horizon_years: float) -> float:
    """
    Return one fitted series' log-scale standard deviation at a horizon.

    Interpolated from its cross-validated mean absolute log errors (the
    carry-forward errors for a series that failed the gate) and converted
    assuming a normal. Zero at a horizon of zero.
    """
    if horizon_years <= 0:
        return 0.0
    errors = fitted.get("holdout_error") if fitted.get("beats_carry_forward") else fitted.get("carry_forward_error")
    if not errors:
        return 0.0
    # Anchored at (0, 0): `np.interp` clamps below its first point, so
    # without this a sale four months out would be charged the whole of the
    # one-year uncertainty. Beyond the last horizon it still clamps, which
    # is the right way round -- the API caps a sale date at five years.
    points = [(0.0, 0.0)] + sorted((float(h), float(v)) for h, v in errors.items())
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    mae = float(np.interp(horizon_years, xs, ys))
    return mae * _MAE_TO_SIGMA


class IndexProjection:
    """
    The fitted projection, ready to price with.

    Built from the live index and the sidecar's hyperparameters, so the rates
    track every new published month while the hyperparameters stay as the
    backtest chose them. Without a sidecar — or for a type that failed the gate
    — `factor` returns 1.0 and the index is carried forward unchanged.
    """

    def __init__(self, frame: pd.DataFrame, payload: Optional[Mapping] = None) -> None:
        self.payload = dict(payload or {})
        self._types: Dict[str, dict] = dict(self.payload.get("types") or {})
        self._rates: Dict[str, Tuple[float, float]] = {}
        self._anchor: Optional[pd.Timestamp] = None
        if frame is None or not len(frame):
            return
        ordered = frame.sort_values("period")
        self._anchor = pd.Timestamp(ordered["period"].max())
        for propertytype, fitted in self._types.items():
            if not fitted.get("beats_carry_forward"):
                continue
            series = log_index(ordered, propertytype)
            if series.size < 2:
                continue
            params = ProjectionParams(**fitted["params"])
            self._rates[propertytype] = _rates(series.to_numpy(dtype=np.float64), series.size - 1, params)

    @property
    def anchor(self) -> Optional[pd.Timestamp]:
        """The last index period held: horizons are measured from here."""
        return self._anchor

    def active(self) -> bool:
        """Whether any property type is projected at all."""
        return bool(self._rates)

    def horizon_years(self, day) -> float:
        """Return how far `day` lies beyond the last published index period, in years."""
        if self._anchor is None:
            return 0.0
        return max(0.0, (pd.Timestamp(day) - self._anchor).days / 365.25)

    def _params(self, propertytype: Optional[str]) -> Optional[ProjectionParams]:
        """Return the fitted parameters for a projected type, or None."""
        fitted = self._types.get(propertytype or "")
        if not fitted or not fitted.get("beats_carry_forward"):
            return None
        return ProjectionParams(**fitted["params"])

    def factor(self, day, propertytype: Optional[str]) -> float:
        """
        Return the multiplier the index carries from its last period to `day`.

        1.0 on or before the last published period, and for any type that is
        not projected.
        """
        params = self._params(propertytype)
        rates = self._rates.get(propertytype or "")
        if params is None or rates is None:
            return 1.0
        horizon = self.horizon_years(day)
        if horizon <= 0:
            return 1.0
        return float(math.exp(cumulative_uplift(rates[0], rates[1], params, horizon)))

    def sigma(self, propertytype: Optional[str], horizon_years: float) -> float:
        """
        Return the projection's log-scale standard deviation at a horizon.

        Interpolated from the held-out mean absolute log errors and converted
        assuming a normal, which is what widens the price range with the
        forecast horizon. Zero at a horizon of zero, or for a type that is not
        projected — a carried-forward index is still uncertain, but that
        uncertainty is already inside the model's measured errors.
        """
        fitted = self._types.get(propertytype or "")
        if not fitted:
            return 0.0
        return fitted_sigma(fitted, horizon_years)

    def describe(self, propertytype: Optional[str], day) -> Optional[str]:
        """Return a one-line note on what was projected, or None when nothing was."""
        factor = self.factor(day, propertytype)
        if factor == 1.0:
            return None
        return f"{factor - 1.0:+.1%} projected London {propertytype} price growth to {pd.Timestamp(day).date()}"
