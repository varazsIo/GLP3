"""
Tests for `hpldn.backend.scripts.hpi_projection`.

The projection decides how much a future sale date moves a price, so the tests
pin the three things that could quietly go wrong: the uplift must be zero at
the anchor and grow with the horizon, the shape must be the damped blend it
claims (recent rate near the anchor, long-run rate far out), and the gate must
actually reject a type whose fit does not beat carrying the index forward.
"""

import math

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.scripts import hpi_projection as hp


def _index(monthly_log_growth: float, months: int = 380, start: float = 100_000.0) -> pd.DataFrame:
    """Return a synthetic `production.ukhpi` frame growing at a constant log rate."""
    periods = pd.date_range("1995-01-01", periods=months, freq="MS")
    values = start * np.exp(monthly_log_growth * np.arange(months))
    frame = pd.DataFrame({"period": periods, hp.ALL_TYPES_COLUMN: values})
    for column in hp.PRICE_COLUMN_BY_TYPE.values():
        frame[column] = values
    return frame


def test_log_index_falls_back_to_all_types_for_a_type_the_index_omits():
    frame = _index(0.002)
    frame["avg_price_detached"] = np.nan
    detached = hp.log_index(frame, "D")
    everything = hp.log_index(frame, None)
    assert len(detached) == len(everything)
    assert np.allclose(detached.to_numpy(), everything.to_numpy())


def test_cumulative_uplift_is_zero_at_the_anchor_and_grows_with_the_horizon():
    params = hp.ProjectionParams(recent_years=5, long_years=20, half_life_years=1.5, shrink=1.0)
    assert hp.cumulative_uplift(0.03, 0.03, params, 0.0) == 0.0
    assert hp.cumulative_uplift(0.03, 0.03, params, -1.0) == 0.0
    steps = [hp.cumulative_uplift(0.03, 0.03, params, h) for h in (1, 2, 3, 4, 5)]
    assert steps == sorted(steps)
    # With both rates equal the blend is just that rate times the horizon.
    assert steps[4] == pytest.approx(0.03 * 5)


def test_the_blend_starts_at_the_recent_rate_and_decays_to_the_long_run_one():
    params = hp.ProjectionParams(recent_years=5, long_years=20, half_life_years=1.0, shrink=1.0)
    recent, long_run = -0.02, 0.04
    # Over a short horizon the recent rate dominates, so the path is near it.
    near = hp.cumulative_uplift(recent, long_run, params, 0.1) / 0.1
    assert near == pytest.approx(recent, abs=0.01)
    # Far out, the marginal rate has decayed to the long-run one.
    far = (
        hp.cumulative_uplift(recent, long_run, params, 20.0)
        - hp.cumulative_uplift(recent, long_run, params, 19.0)
    )
    assert far == pytest.approx(long_run, abs=1e-3)


def test_shrink_scales_the_whole_path_and_zero_means_carry_forward():
    full = hp.ProjectionParams(5, 20, 1.5, 1.0)
    half = hp.ProjectionParams(5, 20, 1.5, 0.5)
    assert hp.cumulative_uplift(0.03, 0.03, half, 4.0) == pytest.approx(
        0.5 * hp.cumulative_uplift(0.03, 0.03, full, 4.0)
    )
    assert hp.cumulative_uplift(0.03, 0.03, hp.CARRY_FORWARD, 5.0) == 0.0


def test_a_steadily_growing_index_is_projected_and_a_noisy_one_may_not_be():
    """The gate is what decides a type is projected, and it is measured, not assumed."""
    fitted = hp.fit_type(hp.log_index(_index(0.004), "F"))
    assert fitted is not None
    assert fitted["beats_carry_forward"] is True
    # On a trending series, carrying forward is worse at every horizon.
    for horizon in ("1", "5"):
        assert fitted["holdout_error"][horizon] < fitted["carry_forward_error"][horizon]


def test_a_flat_index_is_not_projected_because_carrying_forward_is_already_right():
    rng = np.random.default_rng(7)
    frame = _index(0.0)
    noise = np.cumsum(rng.normal(0.0, 0.01, len(frame)))
    for column in [hp.ALL_TYPES_COLUMN, *hp.PRICE_COLUMN_BY_TYPE.values()]:
        frame[column] = frame[column] * np.exp(noise)
    fitted = hp.fit_type(hp.log_index(frame, "F"))
    assert fitted is not None
    # Whatever the grid picks, a projection of a driftless walk cannot beat
    # carrying it forward at every horizon -- and if it did, it would only be
    # by fitting the noise, which is exactly what the gate exists to catch.
    assert fitted["beats_carry_forward"] is False


def test_an_unprojected_type_carries_the_index_forward_unchanged():
    frame = _index(0.004)
    payload = {"types": {"F": {"params": {"recent_years": 5, "long_years": 20,
                                          "half_life_years": 1.5, "shrink": 1.0},
                               "beats_carry_forward": False,
                               "holdout_error": {"1": 0.05, "5": 0.2},
                               "carry_forward_error": {"1": 0.04, "5": 0.1}}}}
    projection = hp.IndexProjection(frame, payload)
    assert projection.active() is False
    future = pd.Timestamp(frame["period"].max()) + pd.DateOffset(years=4)
    assert projection.factor(future, "F") == 1.0
    # A type with no entry at all is likewise carried forward.
    assert projection.factor(future, "D") == 1.0


def test_the_factor_is_one_up_to_the_anchor_and_rises_beyond_it():
    frame = _index(0.004)
    payload = hp.fit(frame)
    projection = hp.IndexProjection(frame, payload)
    anchor = projection.anchor
    assert projection.factor(anchor, "F") == 1.0
    assert projection.factor(anchor - pd.Timedelta(days=90), "F") == 1.0
    factors = [projection.factor(anchor + pd.DateOffset(years=h), "F") for h in (1, 2, 3, 4, 5)]
    assert factors == sorted(factors)
    assert factors[0] > 1.0


def test_sigma_is_zero_at_the_anchor_and_interpolates_between_measured_horizons():
    frame = _index(0.004)
    projection = hp.IndexProjection(frame, hp.fit(frame))
    assert projection.sigma("F", 0.0) == 0.0
    errors = projection.payload["types"]["F"]["holdout_error"]
    # Converted from a mean absolute deviation to a standard deviation.
    assert projection.sigma("F", 1.0) == pytest.approx(errors["1"] * math.sqrt(math.pi / 2))
    between = projection.sigma("F", 1.5)
    assert min(projection.sigma("F", 1.0), projection.sigma("F", 2.0)) <= between
    assert between <= max(projection.sigma("F", 1.0), projection.sigma("F", 2.0))


def test_a_horizon_inside_the_first_year_is_not_charged_a_whole_year_of_doubt():
    """Interpolation is anchored at the origin, or a sale months away looks a year uncertain."""
    payload = {"types": {"F": {"params": {"recent_years": 5, "long_years": 20,
                                          "half_life_years": 1.5, "shrink": 1.0},
                               "beats_carry_forward": True,
                               "holdout_error": {"1": 0.06, "2": 0.10, "3": 0.13, "4": 0.15, "5": 0.17},
                               "carry_forward_error": {"1": 0.07, "2": 0.12, "3": 0.16, "4": 0.20, "5": 0.25}}}}
    projection = hp.IndexProjection(_index(0.004), payload)
    one_year = projection.sigma("F", 1.0)
    third_of_a_year = projection.sigma("F", 1.0 / 3.0)
    assert 0.0 < third_of_a_year < one_year
    assert third_of_a_year == pytest.approx(one_year / 3.0, rel=1e-6)
    # Past the last measured horizon it holds at the five-year figure.
    assert projection.sigma("F", 9.0) == projection.sigma("F", 5.0)


def test_an_empty_or_missing_payload_never_projects():
    frame = _index(0.004)
    for payload in (None, {}, {"types": {}}):
        projection = hp.IndexProjection(frame, payload)
        assert projection.active() is False
        assert projection.factor(pd.Timestamp("2050-01-01"), "F") == 1.0
        assert projection.sigma("F", 5.0) == 0.0
    # No index at all is also survivable.
    empty = hp.IndexProjection(pd.DataFrame(columns=["period"]), None)
    assert empty.anchor is None and empty.horizon_years("2050-01-01") == 0.0


def test_fit_records_every_type_and_the_window_it_used():
    frame = _index(0.003)
    payload = hp.fit(frame)
    assert set(payload["types"]) == set(hp.PRICE_COLUMN_BY_TYPE)
    assert payload["index_to"] == str(pd.Timestamp(frame["period"].max()).date())
    assert payload["cv_folds"] == hp.CV_FOLDS
    for fitted in payload["types"].values():
        assert set(fitted["holdout_error"]) == {"1", "2", "3", "4", "5"}
        assert fitted["params"]["shrink"] in hp.GRID_SHRINK


def test_a_short_index_is_refused_rather_than_fitted_on_nothing():
    assert hp.fit_type(hp.log_index(_index(0.003, months=60), "F")) is None
