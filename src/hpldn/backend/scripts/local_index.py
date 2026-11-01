"""
A local house price index: postcode district by property type, from repeat sales.

``production.ukhpi`` is London-wide. It breaks prices out by property type but
not by area, so on its own it moves a Battersea terrace exactly as it moves a
Harrow one -- while SW11 terraces fell about 17 % between 2025 and 2026 and
London terraces as a whole held level. This module measures each area's own
path from the sales the database holds, and it is what moves a price from the
evidence date to the sale date a landlord picks (`hpp_inference`).

Method
------
**Repeat sales.** Every pair of consecutive sales of the same home
(``uprn``), both of the same property type, held at least `MIN_HOLD_DAYS`, the
first not a new build, both at or above ``MIN_PLAUSIBLE_PRICE``. The same home
sold twice cancels its size, condition and location, so what is left is the
market's move between the two dates -- the Case-Shiller idea.

**Relative to the parent series.** Each pair's log price change is first
reduced by the London UK HPI's change for its type over the same months. A
borough's index is then estimated from what remains as a monthly log offset
from London, and a postcode district's as an offset from its borough. Per
series this is the least-squares problem

    min_r  sum_pairs (d - (r[u] - r[s]))^2  +  lambda * sum_t (r[t] - r[t-1])^2

where ``s``/``u`` are the months of the two sales and ``d`` the pair's change
beyond the parent's. The penalty is the shrinkage: a month's move away from
the parent costs ``lambda`` pair-equivalents, so a month with many pairs
follows its own data and a thin one (including the last months, which
registration lag leaves short) moves with its parent. ``lambda`` is chosen per
level by held-out pairs (`choose_smoothing`), the result is smoothed over
`SMOOTH_MONTHS` months, and a series with fewer than `MIN_SERIES_PAIRS` pairs
is not estimated at all -- it *is* its parent.

**Any day.** Levels sit on the first of each month, like the index they are
built on; a day between two months is interpolated log-linearly, so the level
moves every day. Beyond the last published month the level is projected with
`hpi_projection`'s damped trend blend, fitted per series under the same
blocked cross-validation and the same gate: a series is projected only where
it beat carrying the index forward at every horizon. A series that fails (or
was not fitted) takes its growth from its parent -- the borough, then London,
whose projection is the model's own ``.projection.json``.

The fitted hyperparameters live beside the model as ``*.cbm.local_index.json``
(``hpp_inference fit-local-index --write``); the levels themselves are rebuilt
from the live table whenever the evidence is loaded, so a refreshed view moves
the index without refitting it.
"""

import logging
import math
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text
from threadpoolctl import ThreadpoolController

from hpldn.backend.common.constants import MIN_PLAUSIBLE_PRICE
from hpldn.backend.scripts import hpi_projection

log = logging.getLogger(__name__)

# `*.cbm.local_index.json`, beside `*.cbm.projection.json`.
# The local index's file in a model bundle (`models.artifact.LOCAL_INDEX_FILE`).
LOCAL_INDEX_FILE = "local_index.json"

PROPERTY_TYPES: Tuple[str, ...] = ("D", "F", "S", "T")

LEVEL_LONDON = "london"
LEVEL_BOROUGH = "borough"
LEVEL_DISTRICT = "district"
LONDON = "London"

# A pair held less than this is mostly a refurbish-and-resell or a
# non-market transfer, and says more about the home than the market.
MIN_HOLD_DAYS = 183

# A pair whose change beyond London's is more than this in logs (about 2x
# either way) is a renovation, a conversion or a data error, not a market move.
MAX_ABS_RESIDUAL = 0.7

# A borough or district series needs this many pairs before it is estimated;
# below it the area simply follows its parent.
MIN_SERIES_PAIRS = 150

# The month-to-month shrinkage, in pair-equivalents, used when no sidecar has
# chosen one by held-out pairs.
DEFAULT_SMOOTHING = 1000.0
SMOOTHING_GRID: Tuple[float, ...] = (100.0, 300.0, 1000.0, 3000.0, 10000.0)
# The share of pairs held out when `choose_smoothing` scores the grid.
SMOOTHING_HOLDOUT_FRAC = 0.2

# The final centred moving average over the estimated offsets, in months.
SMOOTH_MONTHS = 3

# Pins the offsets' overall level, which the pairs (differences only) leave free.
_RIDGE = 1e-6

# The LSOA names the view carries are "<borough> <3 digits><letter>".
_BOROUGH_SUFFIX = r"\s+\d{3}[A-Z]$"

PAIRS_SQL = text(
    f"""
    WITH ordered AS (
        SELECT uprn, dateoftransfer, price, propertytype::text AS propertytype, postcode_district,
               regexp_replace(lsoa_name, '{_BOROUGH_SUFFIX}', '') AS borough,
               lag(dateoftransfer) OVER w AS prev_date,
               lag(price) OVER w AS prev_price,
               lag(propertytype::text) OVER w AS prev_type,
               lag(oldnew::text) OVER w AS prev_oldnew
        FROM production.london_houses
        WHERE uprn IS NOT NULL
        WINDOW w AS (PARTITION BY uprn ORDER BY dateoftransfer, ppd_id)
    )
    SELECT postcode_district, borough, propertytype, prev_date, dateoftransfer, prev_price, price
    FROM ordered
    WHERE prev_date IS NOT NULL
      AND prev_type = propertytype
      AND prev_oldnew IS DISTINCT FROM 'Y'
      AND dateoftransfer - prev_date >= :min_hold_days
      AND price >= :min_price AND prev_price >= :min_price
    """
)

UKHPI_SQL = text(
    f"""
    SELECT period, {hpi_projection.ALL_TYPES_COLUMN}, {", ".join(hpi_projection.PRICE_COLUMN_BY_TYPE.values())}
    FROM production.ukhpi
    WHERE published_date IS NOT NULL
    ORDER BY period
    """
)


def borough_of(lsoa_name) -> Optional[str]:
    """Return the borough an LSOA name belongs to ("Wandsworth 012C" -> "Wandsworth"), or None."""
    if not isinstance(lsoa_name, str) or not lsoa_name.strip():
        return None
    import re

    return re.sub(_BOROUGH_SUFFIX, "", lsoa_name.strip()) or None


def series_key(level: str, area: str, propertytype: str) -> str:
    """Return the key a series is stored under, e.g. ``district:SW11:T``."""
    return f"{level}:{area}:{propertytype}"


def sidecar_path(model_path) -> Path:
    """Return the local index file in a model bundle."""
    return Path(model_path) / LOCAL_INDEX_FILE


def load_params(model_path) -> Optional[dict]:
    """Return the fitted local index in the bundle at `model_path` (signature and hash checked), or None."""
    from hpldn.backend.models import artifact

    return artifact.read_verified_json(model_path, LOCAL_INDEX_FILE)


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------


def _month_offsets(dates: pd.Series, first: pd.Timestamp) -> np.ndarray:
    """Return each date's month position counted from the month of `first`."""
    stamps = pd.to_datetime(dates)
    return ((stamps.dt.year - first.year) * 12 + (stamps.dt.month - first.month)).to_numpy(dtype=np.int64)


def _difference_penalty(n: int) -> np.ndarray:
    """Return ``D'D`` for the first-difference operator on `n` months."""
    diag = np.full(n, 2.0)
    diag[0] = diag[-1] = 1.0
    penalty = np.diag(diag)
    idx = np.arange(n - 1)
    penalty[idx, idx + 1] = -1.0
    penalty[idx + 1, idx] = -1.0
    return penalty


def _normal_equations(u: np.ndarray, s: np.ndarray, d: np.ndarray, n: int) -> Tuple[np.ndarray, np.ndarray]:
    """Return ``(X'X, X'd)`` for pairs whose row is +1 at month `u` and -1 at month `s`."""
    gram = (
        np.bincount(u * n + u, minlength=n * n)
        + np.bincount(s * n + s, minlength=n * n)
        - np.bincount(u * n + s, minlength=n * n)
        - np.bincount(s * n + u, minlength=n * n)
    ).reshape(n, n).astype(np.float64)
    moment = np.bincount(u, weights=d, minlength=n) - np.bincount(s, weights=d, minlength=n)
    return gram, moment


@lru_cache(maxsize=1)
def _blas() -> ThreadpoolController:
    """The process's BLAS libraries, found once."""
    return ThreadpoolController()


def _solve(
    u: np.ndarray, s: np.ndarray, d: np.ndarray, n: int, smoothing: float, penalty: np.ndarray
) -> np.ndarray:
    """
    Return the monthly log offsets that best explain the pairs, shrunk month to month.

    The system is a few hundred months square, which one BLAS thread solves in
    about a millisecond. OpenBLAS's own pool (one thread per CPU) spin-waits
    between calls, so two gunicorn workers building the index at once on the
    host's four cores starved each other: 0.6 s of solves took four minutes.
    """
    gram, moment = _normal_equations(u, s, d, n)
    system = gram + smoothing * penalty + _RIDGE * np.eye(n)
    with _blas().limit(limits=1, user_api="blas"):
        offsets = np.linalg.solve(system, moment)
    return offsets - offsets[-1]


# Series solved per batched ``linalg.solve`` call: each system is a few
# hundred months square (about 1 MB), so a batch is ~150 MB.
SOLVE_BATCH = 128


def _solve_batch(
    problems: Sequence[Tuple[np.ndarray, np.ndarray, np.ndarray]],
    n: int,
    smoothings: Sequence[float],
    penalty: np.ndarray,
    xp=None,
) -> List[List[np.ndarray]]:
    """
    Return `_solve` for every problem and every shrinkage value, solved in batches.

    `problems` are ``(u, s, d)`` per series; the result is indexed
    ``[problem][smoothing]``. Each series' normal equations are built once and
    reused for every value in `smoothings`. On the NumPy path each batch is
    one ``numpy.linalg.solve`` over a stack, which LAPACK solves matrix by
    matrix exactly as the single calls did, so the offsets are bit for bit
    `_solve`'s. With `xp` = CuPy the stack is solved on the GPU (cuBLAS
    batched LU), which agrees to rounding; training passes it, the API never
    does (its host has no GPU).
    """
    xp = np if xp is None else xp
    out: List[List[np.ndarray]] = [[None] * len(smoothings) for _ in problems]
    eye = np.eye(n)
    for start in range(0, len(problems), SOLVE_BATCH):
        batch = problems[start : start + SOLVE_BATCH]
        normal = [_normal_equations(u, s, d, n) for u, s, d in batch]
        grams = np.stack([g for g, _ in normal])
        moments = np.stack([m for _, m in normal])[..., None]
        for k, smoothing in enumerate(smoothings):
            systems = grams + smoothing * penalty + _RIDGE * eye
            if xp is np:
                with _blas().limit(limits=1, user_api="blas"):
                    solved = np.linalg.solve(systems, moments)[..., 0]
            else:
                solved = xp.asnumpy(xp.linalg.solve(xp.asarray(systems), xp.asarray(moments)))[..., 0]
            for i in range(len(batch)):
                out[start + i][k] = solved[i] - solved[i][-1]
    return out


def _centred_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Return a centred moving average, shortened at the ends rather than padded."""
    if window <= 1 or values.size < 2:
        return values
    half = window // 2
    padded = np.concatenate(([0.0], np.cumsum(values)))
    idx = np.arange(values.size)
    lo = np.maximum(0, idx - half)
    hi = np.minimum(values.size, idx + half + 1)
    return (padded[hi] - padded[lo]) / (hi - lo)


def prepare_pairs(pairs: pd.DataFrame, ukhpi: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DatetimeIndex, Dict[str, np.ndarray]]:
    """
    Return the usable pairs with their months and change beyond London, the months, and London's log levels.

    Pairs outside the published index, or whose change beyond London's is
    beyond `MAX_ABS_RESIDUAL`, are dropped.
    """
    ordered = ukhpi.sort_values("period")
    periods = pd.DatetimeIndex(pd.to_datetime(ordered["period"]))
    first = periods[0]
    london = {
        kind: hpi_projection.log_index(ordered, kind).reindex(periods).ffill().bfill().to_numpy(dtype=np.float64)
        for kind in PROPERTY_TYPES
    }
    frame = pairs.copy()
    frame["propertytype"] = frame["propertytype"].astype("str")
    frame = frame[frame["propertytype"].isin(PROPERTY_TYPES)]
    frame["s"] = _month_offsets(frame["prev_date"], first)
    frame["u"] = _month_offsets(frame["dateoftransfer"], first)
    frame = frame[(frame["s"] >= 0) & (frame["u"] < len(periods)) & (frame["u"] > frame["s"])]
    change = np.log(frame["price"].to_numpy(dtype=np.float64) / frame["prev_price"].to_numpy(dtype=np.float64))
    market = np.empty(len(frame), dtype=np.float64)
    kinds = frame["propertytype"].to_numpy()
    u, s = frame["u"].to_numpy(), frame["s"].to_numpy()
    for kind in PROPERTY_TYPES:
        mask = kinds == kind
        market[mask] = london[kind][u[mask]] - london[kind][s[mask]]
    frame["d"] = change - market
    frame = frame[np.abs(frame["d"].to_numpy()) <= MAX_ABS_RESIDUAL]
    frame["borough"] = frame["borough"].where(frame["borough"].notna() & (frame["borough"] != ""), None)
    return frame.reset_index(drop=True), periods, london


def _district_parents(frame: pd.DataFrame) -> Dict[str, str]:
    """Return each district's borough: the one most of its repeat sales lie in."""
    known = frame[frame["borough"].notna() & frame["postcode_district"].notna()]
    counts = known.groupby(["postcode_district", "borough"]).size().reset_index(name="n")
    counts = counts.sort_values(["postcode_district", "n", "borough"], ascending=[True, False, True])
    return dict(counts.drop_duplicates("postcode_district")[["postcode_district", "borough"]].itertuples(index=False))


def estimate_offsets(
    frame: pd.DataFrame,
    n_months: int,
    smoothing: Mapping[str, float],
    min_pairs: int = MIN_SERIES_PAIRS,
    xp=None,
) -> Tuple[Dict[str, np.ndarray], Dict[str, int], Dict[str, str]]:
    """
    Return every borough and district series' log offset from its parent, its pair count, and district parents.

    Borough offsets are from London; district offsets are from the district's
    borough (or from London when the borough has no series of that type).
    Each level's series are solved together (`_solve_batch`; `xp` = CuPy
    solves them on the GPU).
    """
    penalty = _difference_penalty(n_months)
    offsets: Dict[str, np.ndarray] = {}
    pairs: Dict[str, int] = {}
    parents = _district_parents(frame)

    keys, problems = [], []
    for (borough, kind), group in frame[frame["borough"].notna()].groupby(["borough", "propertytype"]):
        if len(group) < min_pairs:
            continue
        keys.append(series_key(LEVEL_BOROUGH, borough, kind))
        problems.append((group["u"].to_numpy(), group["s"].to_numpy(), group["d"].to_numpy()))
    for key, problem, solved in zip(keys, problems, _solve_batch(problems, n_months, [smoothing[LEVEL_BOROUGH]], penalty, xp)):
        offsets[key] = _centred_mean(solved[0], SMOOTH_MONTHS)
        pairs[key] = int(len(problem[0]))

    keys, problems = [], []
    for (district, kind), group in frame[frame["postcode_district"].notna()].groupby(["postcode_district", "propertytype"]):
        if len(group) < min_pairs:
            continue
        u, s = group["u"].to_numpy(), group["s"].to_numpy()
        parent = offsets.get(series_key(LEVEL_BOROUGH, parents.get(district, ""), kind))
        d = group["d"].to_numpy()
        if parent is not None:
            d = d - (parent[u] - parent[s])
        keys.append(series_key(LEVEL_DISTRICT, district, kind))
        problems.append((u, s, d))
    for key, problem, solved in zip(keys, problems, _solve_batch(problems, n_months, [smoothing[LEVEL_DISTRICT]], penalty, xp)):
        offsets[key] = _centred_mean(solved[0], SMOOTH_MONTHS)
        pairs[key] = int(len(problem[0]))
    return offsets, pairs, parents


def choose_smoothing(
    frame: pd.DataFrame,
    n_months: int,
    grid: Sequence[float] = SMOOTHING_GRID,
    holdout_frac: float = SMOOTHING_HOLDOUT_FRAC,
    random_state: int = 42,
    xp=None,
) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    """
    Return the shrinkage per level that best predicts held-out pairs, and every score.

    A random `holdout_frac` of pairs is set aside; each grid value is fitted on
    the rest and scored by the held-out pairs' mean squared error, pooled over
    every series of the level. Boroughs are chosen first, and districts are
    then scored against the borough offsets that choice gives. Every series
    and grid value of a level is solved in one `_solve_batch` pass (`xp` =
    CuPy solves on the GPU), and the errors are pooled in series order, so
    the scores are the per-series loop's.
    """
    rng = np.random.default_rng(random_state)
    held = rng.random(len(frame)) < holdout_frac
    train, test = frame[~held], frame[held]
    penalty = _difference_penalty(n_months)
    scores: Dict[str, Dict[str, float]] = {LEVEL_BOROUGH: {}, LEVEL_DISTRICT: {}}
    chosen: Dict[str, float] = {}

    def _score(level_col: str, parent_offsets: Optional[Dict[Tuple[str, str], np.ndarray]], parents) -> Dict[float, float]:
        test_groups = {key: group for key, group in test.groupby([level_col, "propertytype"])}
        problems, held_out = [], []
        for key, group in train.groupby([level_col, "propertytype"]):
            if len(group) < MIN_SERIES_PAIRS or key not in test_groups:
                continue
            area, kind = key
            test_group = test_groups[key]
            parent = None
            if parent_offsets is not None:
                parent = parent_offsets.get((parents.get(area, ""), kind))
            d_train = group["d"].to_numpy()
            d_test = test_group["d"].to_numpy()
            u, s = group["u"].to_numpy(), group["s"].to_numpy()
            tu, ts = test_group["u"].to_numpy(), test_group["s"].to_numpy()
            if parent is not None:
                d_train = d_train - (parent[u] - parent[s])
                d_test = d_test - (parent[tu] - parent[ts])
            problems.append((u, s, d_train))
            held_out.append((tu, ts, d_test))
        totals = {value: [0.0, 0] for value in grid}
        solved_all = _solve_batch(problems, n_months, list(grid), penalty, xp)
        for (tu, ts, d_test), solved_by_value in zip(held_out, solved_all):
            for value, solved in zip(grid, solved_by_value):
                smoothed = _centred_mean(solved, SMOOTH_MONTHS)
                residual = d_test - (smoothed[tu] - smoothed[ts])
                totals[value][0] += float(np.sum(residual**2))
                totals[value][1] += int(residual.size)
        return {value: (total / count if count else float("inf")) for value, (total, count) in totals.items()}

    borough_scores = _score("borough", None, {})
    chosen[LEVEL_BOROUGH] = min(borough_scores, key=borough_scores.get)
    scores[LEVEL_BOROUGH] = {str(k): round(v, 6) for k, v in borough_scores.items()}

    parents = _district_parents(frame)
    keys, problems = [], []
    for (borough, kind), group in train[train["borough"].notna()].groupby(["borough", "propertytype"]):
        if len(group) < MIN_SERIES_PAIRS:
            continue
        keys.append((borough, kind))
        problems.append((group["u"].to_numpy(), group["s"].to_numpy(), group["d"].to_numpy()))
    borough_offsets = {
        key: _centred_mean(solved[0], SMOOTH_MONTHS)
        for key, solved in zip(keys, _solve_batch(problems, n_months, [chosen[LEVEL_BOROUGH]], penalty, xp))
    }
    district_scores = _score("postcode_district", borough_offsets, parents)
    chosen[LEVEL_DISTRICT] = min(district_scores, key=district_scores.get)
    scores[LEVEL_DISTRICT] = {str(k): round(v, 6) for k, v in district_scores.items()}
    return chosen, scores


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


class LocalIndex:
    """
    Log price levels per district, borough and London by type, on any day.

    Built by `build` from repeat-sale pairs and the UK HPI; `params` is the
    sidecar payload (`fit`), which chooses the shrinkage and says which series
    are projected. `london_projection` is the model's
    `hpi_projection.IndexProjection`, London's growth beyond the index.
    """

    def __init__(
        self,
        periods: pd.DatetimeIndex,
        london: Mapping[str, np.ndarray],
        offsets: Mapping[str, np.ndarray],
        pairs: Mapping[str, int],
        parents: Mapping[str, str],
        params: Optional[Mapping] = None,
        london_projection: Optional["hpi_projection.IndexProjection"] = None,
    ) -> None:
        self.periods = pd.DatetimeIndex(periods)
        self._days = (self.periods.values.astype("datetime64[D]").astype(np.int64)).astype(np.float64)
        self.pairs = dict(pairs)
        self.parents = dict(parents)
        self.params = dict(params or {})
        # Usable repeat-sale pairs behind the whole index (set by `build`).
        self.n_pairs = 0
        self.london_projection = london_projection or hpi_projection.IndexProjection(None, None)
        self._levels: Dict[str, np.ndarray] = {}
        for kind, values in london.items():
            self._levels[series_key(LEVEL_LONDON, LONDON, kind)] = np.asarray(values, dtype=np.float64)
        for key, values in offsets.items():
            level, area, kind = key.split(":", 2)
            if level == LEVEL_BOROUGH:
                self._levels[key] = self._levels[series_key(LEVEL_LONDON, LONDON, kind)] + values
        for key, values in offsets.items():
            level, area, kind = key.split(":", 2)
            if level == LEVEL_DISTRICT:
                parent = self._levels.get(
                    series_key(LEVEL_BOROUGH, self.parents.get(area, ""), kind),
                    self._levels[series_key(LEVEL_LONDON, LONDON, kind)],
                )
                self._levels[key] = parent + values

        # Growth rates at the last month for every series that passed its gate.
        self._fitted: Dict[str, dict] = {}
        self._rates: Dict[str, Tuple[float, float]] = {}
        # `_uplift` per (series, type, horizon): `log_levels` asks for the same
        # days beyond the index once per district.
        self._uplift_cache: Dict[Tuple[str, str, float], float] = {}
        for key, fitted in (self.params.get("series") or {}).items():
            if key not in self._levels or not fitted.get("beats_carry_forward"):
                continue
            projection = hpi_projection.ProjectionParams(**fitted["params"])
            y = self._levels[key]
            g_recent, g_long = hpi_projection._rate_arrays(y, projection.recent_years, projection.long_years)
            self._fitted[key] = fitted
            self._rates[key] = (float(g_recent[-1]), float(g_long[-1]))

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        pairs: pd.DataFrame,
        ukhpi: pd.DataFrame,
        params: Optional[Mapping] = None,
        london_projection: Optional["hpi_projection.IndexProjection"] = None,
        xp=None,
    ) -> "LocalIndex":
        """Estimate every series from repeat-sale `pairs` and the UK HPI rows `ukhpi` (`xp`: see `_solve_batch`)."""
        frame, periods, london = prepare_pairs(pairs, ukhpi)
        smoothing = dict((params or {}).get("smoothing") or {})
        smoothing.setdefault(LEVEL_BOROUGH, DEFAULT_SMOOTHING)
        smoothing.setdefault(LEVEL_DISTRICT, DEFAULT_SMOOTHING)
        offsets, counts, parents = estimate_offsets(frame, len(periods), smoothing, xp=xp)
        index = cls(periods, london, offsets, counts, parents, params, london_projection)
        index.n_pairs = int(len(frame))
        return index

    # -- lookups ------------------------------------------------------------

    @property
    def anchor(self) -> pd.Timestamp:
        """The last month the index publishes; beyond it levels are projected."""
        return self.periods[-1]

    def horizon_years(self, day) -> float:
        """Return how far `day` lies beyond the last month, in years."""
        return max(0.0, (pd.Timestamp(day) - self.anchor).days / 365.25)

    def chain(self, district: Optional[str], propertytype: str) -> List[str]:
        """Return the series a home's level is read from, most local first, ending with London."""
        keys = []
        if isinstance(district, str) and district:
            key = series_key(LEVEL_DISTRICT, district, propertytype)
            if key in self._levels:
                keys.append(key)
            borough = self.parents.get(district)
            if borough:
                key = series_key(LEVEL_BOROUGH, borough, propertytype)
                if key in self._levels:
                    keys.append(key)
        keys.append(series_key(LEVEL_LONDON, LONDON, propertytype))
        return keys

    def _projected_by(self, chain: Sequence[str]) -> str:
        """Return the first series in `chain` with its own projection; London otherwise."""
        for key in chain:
            if key in self._rates:
                return key
        return chain[-1]

    def _uplift(self, key: str, propertytype: str, horizon_years: float) -> float:
        """Return the cumulative log growth series `key` projects over `horizon_years` (cached)."""
        if horizon_years <= 0:
            return 0.0
        cache_key = (key, propertytype, float(horizon_years))
        cached = self._uplift_cache.get(cache_key)
        if cached is None:
            cached = self._uplift_cache[cache_key] = self._compute_uplift(key, propertytype, horizon_years)
        return cached

    def _compute_uplift(self, key: str, propertytype: str, horizon_years: float) -> float:
        if key in self._rates:
            params = hpi_projection.ProjectionParams(**self._fitted[key]["params"])
            g_recent, g_long = self._rates[key]
            return hpi_projection.cumulative_uplift(g_recent, g_long, params, horizon_years)
        # London's growth is the model's own projection of the UK HPI.
        day = self.anchor + pd.Timedelta(days=horizon_years * 365.25)
        return math.log(self.london_projection.factor(day, propertytype))

    def log_level(self, day, district: Optional[str], propertytype: str) -> float:
        """
        Return the log price level for `district` and `propertytype` on `day`.

        Interpolated between months up to the last one, then carried by the
        projection of the first series in the home's chain that has one.
        """
        chain = self.chain(district, propertytype)
        levels = self._levels[chain[0]]
        stamp = pd.Timestamp(day)
        if stamp <= self.anchor:
            x = float(stamp.to_datetime64().astype("datetime64[D]").astype(np.int64))
            return float(np.interp(x, self._days, levels))
        return float(levels[-1]) + self._uplift(self._projected_by(chain), propertytype, self.horizon_years(stamp))

    def log_levels(self, days, district: Optional[str], propertytype: str) -> np.ndarray:
        """
        Return `log_level` for many days at once, as days since the Unix epoch or datetimes.

        What moves a comparable sale's price per m² to the subject's sale date
        (`hpp_rf.sizeband_comp_value`) and a previous sale to it
        (`hpp_rf.add_market_relative_features`), for a whole column at a time.
        """
        values = np.asarray(days)
        if np.issubdtype(values.dtype, np.datetime64):
            x = values.astype("datetime64[D]").astype(np.int64).astype(np.float64)
        else:
            x = values.astype(np.float64)
        chain = self.chain(district, propertytype)
        levels = self._levels[chain[0]]
        out = np.interp(x, self._days, levels)
        beyond = x > self._days[-1]
        if beyond.any():
            key = self._projected_by(chain)
            for day in np.unique(x[beyond]):
                horizon = (day - self._days[-1]) / 365.25
                out[x == day] = float(levels[-1]) + self._uplift(key, propertytype, horizon)
        return out

    def factor(self, from_day, to_day, district: Optional[str], propertytype: str) -> float:
        """Return what a price on `from_day` is worth on `to_day`, as a multiplier."""
        return float(
            math.exp(self.log_level(to_day, district, propertytype) - self.log_level(from_day, district, propertytype))
        )

    def sigma(self, district: Optional[str], propertytype: str, horizon_years: float) -> float:
        """Return the projection's log-scale standard deviation at `horizon_years` for this home's series."""
        key = self._projected_by(self.chain(district, propertytype))
        if key in self._fitted:
            return hpi_projection.fitted_sigma(self._fitted[key], horizon_years)
        return self.london_projection.sigma(propertytype, horizon_years)

    def area_of(self, district: Optional[str], propertytype: str) -> str:
        """Return the area whose projected growth prices this home ("SW11", "Wandsworth" or "London")."""
        return self._projected_by(self.chain(district, propertytype)).split(":", 2)[1]

    def level_area_of(self, district: Optional[str], propertytype: str) -> str:
        """Return the most local area whose measured level this home follows."""
        return self.chain(district, propertytype)[0].split(":", 2)[1]

    # -- fitting ------------------------------------------------------------

    def fit_projections(self, log_: Optional[logging.Logger] = None) -> Dict[str, dict]:
        """Fit `hpi_projection`'s damped blend to every borough and district series, with its gate."""
        fitted: Dict[str, dict] = {}
        keys = [key for key in self._levels if not key.startswith(LEVEL_LONDON + ":")]
        for position, key in enumerate(sorted(keys)):
            series = pd.Series(self._levels[key], index=self.periods)
            result = hpi_projection.fit_type(series)
            if result is None:
                continue
            result["pairs"] = int(self.pairs.get(key, 0))
            fitted[key] = result
            if log_ is not None and (position + 1) % 200 == 0:
                log_.info(f"fit-local-index: fitted {position + 1} of {len(keys)} series")
        return fitted


# --------------------------------------------------------------------------
# Loading and fitting against the database
# --------------------------------------------------------------------------


def read_inputs(engine) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Read the repeat-sale pairs and the UK HPI, lifting the API role's short statement timeout."""
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET LOCAL statement_timeout = '300s'"))
            pairs = pd.read_sql(
                PAIRS_SQL,
                connection,
                params={"min_hold_days": MIN_HOLD_DAYS, "min_price": MIN_PLAUSIBLE_PRICE},
            )
            ukhpi = pd.read_sql(UKHPI_SQL, connection)
    ukhpi["period"] = pd.to_datetime(ukhpi["period"])
    return pairs, ukhpi


def build_as_of(
    pairs: pd.DataFrame,
    ukhpi: pd.DataFrame,
    cutoff,
    params: Optional[Mapping] = None,
    log_: Optional[logging.Logger] = None,
    use_gpu: bool = False,
) -> LocalIndex:
    """
    Return the index a model trained to `cutoff` may see: built from pairs whose second sale is on or before it.

    `hpp_cb` indexes previous sales and comparables with this at training time,
    so no sale after the training period moves a level the model learns from.
    Without `params` the shrinkage is chosen on those pairs (`choose_smoothing`).
    With `use_gpu` (and a usable GPU) the batched solves run on it; the
    levels then agree with the CPU's to rounding rather than bit for bit.
    """
    xp = _gpu_module() if use_gpu else None
    cutoff = pd.Timestamp(cutoff)
    kept = pairs[pd.to_datetime(pairs["dateoftransfer"]) <= cutoff]
    if params is None:
        frame, periods, _ = prepare_pairs(kept, ukhpi)
        smoothing, scores = choose_smoothing(frame, len(periods), xp=xp)
        params = {"smoothing": smoothing}
        if log_ is not None:
            log_.info(f"Local price index to {cutoff.date()}: {len(frame):,} pairs, shrinkage {smoothing} ({scores})")
    return LocalIndex.build(kept, ukhpi, params, xp=xp)


def _gpu_module():
    """Return CuPy when `common.accel` computes on a GPU, else None (the NumPy path)."""
    from hpldn.backend.common import accel  # noqa: PLC0415

    return accel.get_array_module() if accel.gpu_enabled() else None


def fit(engine, log_: Optional[logging.Logger] = None) -> dict:
    """
    Choose the shrinkage, fit every series' projection and return the sidecar payload.

    Takes a few minutes: about a thousand series each go through
    `hpi_projection.fit_type`'s blocked cross-validation.
    """
    pairs, ukhpi = read_inputs(engine)
    frame, periods, london = prepare_pairs(pairs, ukhpi)
    if log_ is not None:
        log_.info(f"fit-local-index: {len(frame):,} usable repeat-sale pairs to {periods[-1].date()}")
    smoothing, scores = choose_smoothing(frame, len(periods))
    if log_ is not None:
        log_.info(f"fit-local-index: shrinkage {smoothing} (held-out pair MSE {scores})")
    offsets, counts, parents = estimate_offsets(frame, len(periods), smoothing)
    index = LocalIndex(periods, london, offsets, counts, parents, {"smoothing": smoothing})
    series = index.fit_projections(log_)
    projected = sum(1 for fitted in series.values() if fitted["beats_carry_forward"])
    if log_ is not None:
        log_.info(f"fit-local-index: {projected} of {len(series)} series beat carrying the index forward")
    return {
        "method": "repeat_sales_relative",
        "index_to": str(periods[-1].date()),
        "pairs": int(len(frame)),
        "min_series_pairs": MIN_SERIES_PAIRS,
        "smoothing": smoothing,
        "smoothing_scores": scores,
        "series": series,
    }


def load(
    engine,
    model_path=None,
    london_projection: Optional["hpi_projection.IndexProjection"] = None,
    log_: Optional[logging.Logger] = None,
    params: Optional[dict] = None,
) -> LocalIndex:
    """
    Build the local index the served model prices with, from the live table.

    `params` is the bundle's fitted local index when the caller has already
    verified it (`hpp_inference.SavedModel`); otherwise it is read from the
    bundle at `model_path`. Without one the levels are still measured, but no
    area has its own projection: every home's growth beyond the index is
    London's, and a warning says how to fit one.
    """
    if params is None and model_path is not None:
        params = load_params(model_path)
    if params is None:
        (log_ or log).warning(
            "Local price index: no %s in %s; measured local levels are used but growth beyond the "
            "published index is London's. Run: python -m hpldn.backend.scripts.hpp_inference "
            "fit-local-index --model %s --write",
            LOCAL_INDEX_FILE,
            model_path,
            model_path,
        )
    pairs, ukhpi = read_inputs(engine)
    index = LocalIndex.build(pairs, ukhpi, params, london_projection)
    if log_ is not None:
        log_.info(
            f"Local price index: {len(index.pairs):,} area series from {index.n_pairs:,} "
            f"repeat-sale pairs, {len(index._rates)} projected locally, index to {index.anchor.date()}"
        )
    return index


__all__ = [
    "LOCAL_INDEX_FILE",
    "LocalIndex",
    "borough_of",
    "build_as_of",
    "fit",
    "load",
    "load_params",
    "sidecar_path",
]
