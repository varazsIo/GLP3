"""
Ofsted school features of a home on a date: the constants and the reference computation.

Migration 0034 builds ``production.ofsted_features`` (one row per sale) in
SQL from ``production.ofsted_rating_spells``, binding these constants as
parameters, and `hpp_inference.EvidenceStore.ofsted_features` computes the
same columns in numpy with `compute` for a home being priced, so training and
serving read one definition.

A school counts on a date when one of its spells is in force then
(``valid_from <= date < valid_to``; ``valid_from`` is the report's
publication date, so nothing unpublished is seen) and it lies within
`SEARCH_RADIUS_M`. All-through schools count as both primary and secondary;
special schools, pupil referral units, nurseries and post-16 providers are
left out. Scores run from 1 (best) to 4 (`production.ofsted.rating_score`).

Columns (`OFSTED_COLUMNS`):

``ofsted_{primary,secondary}_nearest_m``
    Distance to the nearest school of the phase, metres.
``ofsted_{primary,secondary}_nearest_score``
    Its rating score.
``ofsted_{primary,secondary}_nearest_age_years``
    Years from the inspection that set or last confirmed its rating to the date.
``ofsted_primary_good_plus_1km`` / ``ofsted_primary_outstanding_1km`` / ``ofsted_secondary_good_plus_2km``
    Schools of the phase within the radius rated good or better
    (score < `GOOD_PLUS_BELOW`) / outstanding (score < `OUTSTANDING_BELOW`).
``ofsted_primary_idw_score_1500m`` / ``ofsted_secondary_idw_score_3km``
    Inverse-distance-weighted mean score within the radius, distances floored
    at `IDW_MIN_DISTANCE_M`, weights ``distance ** -IDW_POWER``.
``ofsted_nearest_primary_basis``
    The nearest primary's ``grade_basis`` (categorical).
"""

from typing import Dict, Sequence

import numpy as np

SEARCH_RADIUS_M = 3000.0
PRIMARY_PHASES = ("primary", "all_through")
SECONDARY_PHASES = ("secondary", "all_through")
FEATURE_PHASES = tuple(sorted(set(PRIMARY_PHASES) | set(SECONDARY_PHASES)))

# Score thresholds, strict: the labels round half up (2.5 is "Requires improvement").
GOOD_PLUS_BELOW = 2.5
OUTSTANDING_BELOW = 1.5

PRIMARY_COUNT_RADIUS_M = 1000.0
SECONDARY_COUNT_RADIUS_M = 2000.0
PRIMARY_IDW_RADIUS_M = 1500.0
SECONDARY_IDW_RADIUS_M = 3000.0
IDW_POWER = 1.0
IDW_MIN_DISTANCE_M = 50.0

DAYS_PER_YEAR = 365.25

BASIS_CATEGORIES = ("overall", "key_judgements", "report_card", "carried")

NUMERIC_COLUMNS = [
    "ofsted_primary_nearest_m",
    "ofsted_primary_nearest_score",
    "ofsted_primary_nearest_age_years",
    "ofsted_secondary_nearest_m",
    "ofsted_secondary_nearest_score",
    "ofsted_secondary_nearest_age_years",
    "ofsted_primary_good_plus_1km",
    "ofsted_primary_outstanding_1km",
    "ofsted_secondary_good_plus_2km",
    "ofsted_primary_idw_score_1500m",
    "ofsted_secondary_idw_score_3km",
]
CATEGORICAL_COLUMNS = ["ofsted_nearest_primary_basis"]
OFSTED_COLUMNS = NUMERIC_COLUMNS + CATEGORICAL_COLUMNS

# Count columns: 0, not missing, when a home has no school in range.
COUNT_COLUMNS = (
    "ofsted_primary_good_plus_1km",
    "ofsted_primary_outstanding_1km",
    "ofsted_secondary_good_plus_2km",
)


def sql_params() -> Dict[str, object]:
    """Return the constants as the bound parameters 0034's SQL names."""
    return {
        "search_radius_m": SEARCH_RADIUS_M,
        "primary_phases": list(PRIMARY_PHASES),
        "secondary_phases": list(SECONDARY_PHASES),
        "feature_phases": list(FEATURE_PHASES),
        "good_plus_below": GOOD_PLUS_BELOW,
        "outstanding_below": OUTSTANDING_BELOW,
        "primary_count_radius_m": PRIMARY_COUNT_RADIUS_M,
        "secondary_count_radius_m": SECONDARY_COUNT_RADIUS_M,
        "primary_idw_radius_m": PRIMARY_IDW_RADIUS_M,
        "secondary_idw_radius_m": SECONDARY_IDW_RADIUS_M,
        "idw_power": IDW_POWER,
        "idw_min_distance_m": IDW_MIN_DISTANCE_M,
        "days_per_year": DAYS_PER_YEAR,
    }


def empty() -> Dict[str, object]:
    """Return the features of a home with no rated school in range."""
    out: Dict[str, object] = {col: np.nan for col in NUMERIC_COLUMNS}
    for col in COUNT_COLUMNS:
        out[col] = 0.0
    out["ofsted_nearest_primary_basis"] = None
    return out


def _idw(dist: np.ndarray, score: np.ndarray, mask: np.ndarray) -> float:
    if not mask.any():
        return np.nan
    weight = np.maximum(dist[mask], IDW_MIN_DISTANCE_M) ** -IDW_POWER
    return float(np.sum(weight * score[mask]) / np.sum(weight))


def compute(
    dist_m: np.ndarray,
    phase: Sequence[str],
    score: np.ndarray,
    age_days: np.ndarray,
    basis: Sequence[str],
) -> Dict[str, object]:
    """
    Return the `OFSTED_COLUMNS` of one home from the spells in force on its date.

    Parameters
    ----------
    dist_m : numpy.ndarray
        Distance from the home to each school, metres.
    phase, basis : sequence of str
        Each school's phase and its spell's ``grade_basis``.
    score : numpy.ndarray
        Each spell's rating score.
    age_days : numpy.ndarray
        Days from the spell's ``inspection_date`` to the home's date.

    Notes
    -----
    Pass only spells in force on the date; schools beyond `SEARCH_RADIUS_M`
    are ignored here. Ties in distance go to the first school passed, as the
    SQL orders by distance then spell id.
    """
    out = empty()
    dist_m = np.asarray(dist_m, dtype=np.float64)
    if not dist_m.size:
        return out
    phase = np.asarray(phase, dtype=object)
    basis = np.asarray(basis, dtype=object)
    score = np.asarray(score, dtype=np.float64)
    age_days = np.asarray(age_days, dtype=np.float64)
    near = dist_m <= SEARCH_RADIUS_M
    primary = near & np.isin(phase, PRIMARY_PHASES)
    secondary = near & np.isin(phase, SECONDARY_PHASES)

    for name, mask in (("primary", primary), ("secondary", secondary)):
        if mask.any():
            idx = np.flatnonzero(mask)
            nearest = idx[np.argmin(dist_m[idx])]
            out[f"ofsted_{name}_nearest_m"] = float(dist_m[nearest])
            out[f"ofsted_{name}_nearest_score"] = float(score[nearest])
            out[f"ofsted_{name}_nearest_age_years"] = float(age_days[nearest] / DAYS_PER_YEAR)
            if name == "primary":
                out["ofsted_nearest_primary_basis"] = basis[nearest]

    within_1km = primary & (dist_m <= PRIMARY_COUNT_RADIUS_M)
    out["ofsted_primary_good_plus_1km"] = float(np.sum(within_1km & (score < GOOD_PLUS_BELOW)))
    out["ofsted_primary_outstanding_1km"] = float(np.sum(within_1km & (score < OUTSTANDING_BELOW)))
    out["ofsted_secondary_good_plus_2km"] = float(
        np.sum(secondary & (dist_m <= SECONDARY_COUNT_RADIUS_M) & (score < GOOD_PLUS_BELOW))
    )
    out["ofsted_primary_idw_score_1500m"] = _idw(dist_m, score, primary & (dist_m <= PRIMARY_IDW_RADIUS_M))
    out["ofsted_secondary_idw_score_3km"] = _idw(dist_m, score, secondary & (dist_m <= SECONDARY_IDW_RADIUS_M))
    return out
