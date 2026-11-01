"""
The pre-vectorisation implementations of the training hotspots, kept verbatim for parity tests.

Each is the pre-vectorisation code path: `rolling_causal_group_stats`
(per-group pandas rolling), `add_sizeband_comp_features` (a per-row KD-tree
loop), and `estimate_offsets`/`choose_smoothing` (one ``np.linalg.solve`` per
series and grid value). The tests compare the vectorised replacements with
these on synthetic frames; nothing in the package imports this module.
"""

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from hpldn.backend.common.constants import MIN_PLAUSIBLE_PRICE
from hpldn.backend.scripts.hpp_rf import (
    DATE_COLUMN,
    SIZEBAND_COMP_RADII_M,
    SIZEBAND_COMP_MIN_SALES,
    SIZEBAND_COMP_WINDOW_DAYS,
    TARGET_COLUMN,
    _days_since_epoch,
    _is_cudf,
    planar_metres,
    sizeband_candidates,
    sizeband_comp_value,
)
from hpldn.backend.scripts.local_index import (
    LEVEL_BOROUGH,
    LEVEL_DISTRICT,
    MIN_SERIES_PAIRS,
    SMOOTH_MONTHS,
    SMOOTHING_GRID,
    SMOOTHING_HOLDOUT_FRAC,
    _centred_mean,
    _difference_penalty,
    _district_parents,
    _solve,
    series_key,
)


def rolling_causal_group_stats(
    df: pd.DataFrame, group_cols: List[str], window_months: int, value_col: str
) -> Tuple[pd.Series, pd.Series]:
    """
    Return the rolling median and count of `value_col` over prior months per group.

    The window is ``[date - window_months, date)`` (``closed="left"``), so a
    row's own sale never enters its statistic and the result is leakage-safe
    before any split.

    Returns
    -------
    median, count : pandas.Series
        Aligned to ``df.index``; the count is 0 and the median NaN where the
        window is empty.
    """
    window = pd.Timedelta(days=int(round(window_months * 30.4375)))  
    ordered = df[[DATE_COLUMN, value_col] + group_cols].copy()
    ordered[DATE_COLUMN] = pd.to_datetime(ordered[DATE_COLUMN])
    ordered = ordered.sort_values(DATE_COLUMN)

    medians, counts = [], []
    for _, group in ordered.groupby(group_cols, sort=False, group_keys=False):
        rolling = group.rolling(window, on=DATE_COLUMN, closed="left")[value_col]
        medians.append(rolling.median())
        counts.append(rolling.count())
    median = pd.concat(medians).reindex(df.index) if medians else pd.Series(np.nan, index=df.index)
    count = (pd.concat(counts).reindex(df.index) if counts else pd.Series(0.0, index=df.index)).fillna(0.0)
    return median, count



def add_sizeband_comp_features(
    df, local_index=None, log=None, min_price: float = MIN_PLAUSIBLE_PRICE
):
    """
    Return a copy of `df` with the `SIZEBAND_COMP_COLUMNS` added.

    Causal: a sale's comparables are the rows of `df` sold strictly before it
    (`sizeband_candidates`), so no split needs it refitted. Rows are grouped by
    type and sale month; each month's candidates go into one KD-tree, the inner
    radius is searched for every sale and the outer one only for those with too
    few. Sales under `min_price` are never comparables. `local_index`
    (`local_index.LocalIndex`, built from pairs up to the training cutoff) moves
    each comparable to the sale date; without one they are not moved. Runs on
    pandas even for a cuDF `df`.
    """
    from scipy.spatial import cKDTree  # noqa: PLC0415

    on_gpu = _is_cudf(df)
    out = (df.to_pandas() if on_gpu else df).copy()
    n = len(out)
    pps_out = np.full(n, np.nan)
    if n:
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
        months = (days.astype("datetime64[D]").astype("datetime64[M]").astype(np.int64))
        for kind in np.unique(kinds):
            comps = np.flatnonzero(is_comp & (kinds == kind))
            comps = comps[np.argsort(days[comps], kind="stable")]
            comp_days = days[comps]
            subjects = np.flatnonzero(located & (kinds == kind))
            for month in np.unique(months[subjects]):
                chunk = subjects[months[subjects] == month]
                lo = int(np.searchsorted(comp_days, days[chunk].min() - SIZEBAND_COMP_WINDOW_DAYS, side="left"))
                hi = int(np.searchsorted(comp_days, days[chunk].max(), side="left"))
                if hi <= lo:
                    continue
                pool = comps[lo:hi]
                tree = cKDTree(np.column_stack([x[pool], y[pool]]))
                inner = tree.query_ball_point(np.column_stack([x[chunk], y[chunk]]), r=SIZEBAND_COMP_RADII_M[0])
                for row, hits in zip(chunk, inner):
                    if len(hits) >= SIZEBAND_COMP_MIN_SALES:
                        candidates = pool[np.asarray(hits, dtype=np.int64)]
                        keep = sizeband_candidates(area[candidates], days[candidates], area[row], days[row])
                        if int(keep.sum()) < SIZEBAND_COMP_MIN_SALES:
                            candidates = None
                    else:
                        candidates = None
                    if candidates is None:
                        wide = tree.query_ball_point([x[row], y[row]], r=SIZEBAND_COMP_RADII_M[1])
                        candidates = pool[np.asarray(wide, dtype=np.int64)]
                        keep = sizeband_candidates(area[candidates], days[candidates], area[row], days[row])
                    candidates = candidates[keep]
                    if not candidates.size:
                        continue
                    dist = np.hypot(x[candidates] - x[row], y[candidates] - y[row])
                    district = districts[row] if isinstance(districts[row], str) else None
                    levels = (
                        None if local_index is None
                        else (lambda d, _district=district, _kind=kind: local_index.log_levels(d, _district, _kind))
                    )
                    pps_out[row] = sizeband_comp_value(days[candidates], log_pps[candidates], dist, days[row], levels)
        if log is not None:
            log.info(
                f"Added size-matched comparables: {int(np.isfinite(pps_out).sum()):,} of {n:,} rows "
                f"({'moved along the local index' if local_index is not None else 'not index-moved'})"
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



def estimate_offsets(
    frame: pd.DataFrame,
    n_months: int,
    smoothing: Mapping[str, float],
    min_pairs: int = MIN_SERIES_PAIRS,
) -> Tuple[Dict[str, np.ndarray], Dict[str, int], Dict[str, str]]:
    """
    Return every borough and district series' log offset from its parent, its pair count, and district parents.

    Borough offsets are from London; district offsets are from the district's
    borough (or from London when the borough has no series of that type).
    """
    penalty = _difference_penalty(n_months)
    offsets: Dict[str, np.ndarray] = {}
    pairs: Dict[str, int] = {}
    parents = _district_parents(frame)

    for (borough, kind), group in frame[frame["borough"].notna()].groupby(["borough", "propertytype"]):
        if len(group) < min_pairs:
            continue
        key = series_key(LEVEL_BOROUGH, borough, kind)
        solved = _solve(
            group["u"].to_numpy(), group["s"].to_numpy(), group["d"].to_numpy(), n_months,
            smoothing[LEVEL_BOROUGH], penalty,
        )
        offsets[key] = _centred_mean(solved, SMOOTH_MONTHS)
        pairs[key] = int(len(group))

    for (district, kind), group in frame[frame["postcode_district"].notna()].groupby(["postcode_district", "propertytype"]):
        if len(group) < min_pairs:
            continue
        u, s = group["u"].to_numpy(), group["s"].to_numpy()
        parent = offsets.get(series_key(LEVEL_BOROUGH, parents.get(district, ""), kind))
        d = group["d"].to_numpy()
        if parent is not None:
            d = d - (parent[u] - parent[s])
        key = series_key(LEVEL_DISTRICT, district, kind)
        solved = _solve(u, s, d, n_months, smoothing[LEVEL_DISTRICT], penalty)
        offsets[key] = _centred_mean(solved, SMOOTH_MONTHS)
        pairs[key] = int(len(group))
    return offsets, pairs, parents



def choose_smoothing(
    frame: pd.DataFrame,
    n_months: int,
    grid: Sequence[float] = SMOOTHING_GRID,
    holdout_frac: float = SMOOTHING_HOLDOUT_FRAC,
    random_state: int = 42,
) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    """
    Return the shrinkage per level that best predicts held-out pairs, and every score.

    A random `holdout_frac` of pairs is set aside; each grid value is fitted on
    the rest and scored by the held-out pairs' mean squared error, pooled over
    every series of the level. Boroughs are chosen first, and districts are
    then scored against the borough offsets that choice gives.
    """
    rng = np.random.default_rng(random_state)
    held = rng.random(len(frame)) < holdout_frac
    train, test = frame[~held], frame[held]
    penalty = _difference_penalty(n_months)
    scores: Dict[str, Dict[str, float]] = {LEVEL_BOROUGH: {}, LEVEL_DISTRICT: {}}
    chosen: Dict[str, float] = {}

    def _score(level_col: str, parent_offsets: Optional[Dict[Tuple[str, str], np.ndarray]], parents) -> Dict[float, float]:
        totals = {value: [0.0, 0] for value in grid}
        test_groups = {key: group for key, group in test.groupby([level_col, "propertytype"])}
        for key, group in train.groupby([level_col, "propertytype"]):
            if len(group) < MIN_SERIES_PAIRS or key not in test_groups:
                continue
            area, kind = key
            held_out = test_groups[key]
            parent = None
            if parent_offsets is not None:
                parent = parent_offsets.get((parents.get(area, ""), kind))
            d_train = group["d"].to_numpy()
            d_test = held_out["d"].to_numpy()
            u, s = group["u"].to_numpy(), group["s"].to_numpy()
            tu, ts = held_out["u"].to_numpy(), held_out["s"].to_numpy()
            if parent is not None:
                d_train = d_train - (parent[u] - parent[s])
                d_test = d_test - (parent[tu] - parent[ts])
            for value in grid:
                solved = _centred_mean(_solve(u, s, d_train, n_months, value, penalty), SMOOTH_MONTHS)
                residual = d_test - (solved[tu] - solved[ts])
                totals[value][0] += float(np.sum(residual**2))
                totals[value][1] += int(residual.size)
        return {value: (total / count if count else float("inf")) for value, (total, count) in totals.items()}

    borough_scores = _score("borough", None, {})
    chosen[LEVEL_BOROUGH] = min(borough_scores, key=borough_scores.get)
    scores[LEVEL_BOROUGH] = {str(k): round(v, 6) for k, v in borough_scores.items()}

    parents = _district_parents(frame)
    borough_offsets = {}
    for (borough, kind), group in train[train["borough"].notna()].groupby(["borough", "propertytype"]):
        if len(group) < MIN_SERIES_PAIRS:
            continue
        borough_offsets[(borough, kind)] = _centred_mean(
            _solve(group["u"].to_numpy(), group["s"].to_numpy(), group["d"].to_numpy(), n_months,
                   chosen[LEVEL_BOROUGH], penalty),
            SMOOTH_MONTHS,
        )
    district_scores = _score("postcode_district", borough_offsets, parents)
    chosen[LEVEL_DISTRICT] = min(district_scores, key=district_scores.get)
    scores[LEVEL_DISTRICT] = {str(k): round(v, 6) for k, v in district_scores.items()}
    return chosen, scores
