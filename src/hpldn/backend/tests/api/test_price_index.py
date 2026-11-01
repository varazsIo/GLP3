"""
Tests for `hpldn.backend.api.market.price_index` and the adjustment it feeds.

The point of the index here is that nearby sales from different months stop
being compared at face value. These tests pin the level lookup, the cap that
stops one old sale being stretched to carry a figure, and the property that
matters most on screen: the adjusted figures the headline quotes are the same
ones listed against each sale.
"""

from datetime import date

import pytest

from hpldn.backend.api.market.price_index import MAX_TIME_SCALE, PriceIndex
from hpldn.backend.api.market.repository import MAX_SIZE_SCALE, adjusted_price


def _rows(levels: dict[str, float]) -> list[dict]:
    """Return index rows from ``{"YYYY-MM-01": flats level}``, all types alike."""
    return [
        {
            "period": date.fromisoformat(period),
            "avg_price_all_property_types": value,
            "avg_price_flats_maisonettes": value,
            "avg_price_terraced": value,
            "avg_price_semi_detached": value,
            "avg_price_detached": value,
        }
        for period, value in sorted(levels.items())
    ]


INDEX = PriceIndex(_rows({"2024-01-01": 400_000.0, "2025-01-01": 440_000.0, "2026-01-01": 484_000.0}))


def test_the_level_is_the_latest_month_at_or_before_the_day():
    assert INDEX.level(date(2025, 6, 30), "flat") == 440_000.0
    assert INDEX.level(date(2025, 1, 1), "flat") == 440_000.0
    assert INDEX.level(date(2024, 12, 31), "flat") == 400_000.0


def test_a_day_before_the_index_starts_takes_its_first_month():
    assert INDEX.level(date(1990, 1, 1), "flat") == 400_000.0


def test_a_day_past_the_last_month_carries_it_forward_without_a_projection():
    assert INDEX.level(date(2030, 1, 1), "flat") == 484_000.0


def test_a_projection_lifts_a_day_past_the_last_month():
    class DoublingProjection:
        """Stands in for a fitted projection: every future day is worth twice as much."""

        def factor(self, day, propertytype):
            return 2.0

    projected = PriceIndex(INDEX.rows, DoublingProjection())
    assert projected.level(date(2026, 1, 1), "flat") == 484_000.0
    assert projected.level(date(2027, 1, 1), "flat") == 968_000.0


def test_the_factor_moves_a_sale_to_the_valuation_date():
    # 400k -> 484k over two years is a 21% uplift.
    assert INDEX.factor(date(2024, 1, 1), date(2026, 1, 1), "flat") == pytest.approx(1.21)
    assert INDEX.factor(date(2026, 1, 1), date(2024, 1, 1), "flat") == pytest.approx(1 / 1.21)
    assert INDEX.factor(date(2025, 1, 1), date(2025, 6, 1), "flat") == 1.0


def test_the_time_adjustment_is_capped_both_ways():
    steep = PriceIndex(_rows({"2000-01-01": 10_000.0, "2026-01-01": 1_000_000.0}))
    assert steep.factor(date(2000, 1, 1), date(2026, 1, 1), "flat") == pytest.approx(MAX_TIME_SCALE)
    assert steep.factor(date(2026, 1, 1), date(2000, 1, 1), "flat") == pytest.approx(1 / MAX_TIME_SCALE)


def test_an_empty_index_adjusts_nothing():
    empty = PriceIndex([], None)
    assert empty.level(date(2026, 1, 1), "flat") is None
    assert empty.factor(date(2024, 1, 1), date(2026, 1, 1), "flat") == 1.0


def test_adjusted_price_scales_for_size_and_date_together():
    # 50 m2 sale, valuing a 60 m2 home: +20% for size, +21% for two years.
    got = adjusted_price(400_000, 50.0, 60.0, date(2024, 1, 1), date(2026, 1, 1), "flat", INDEX)
    assert got == round(400_000 * 1.2 * 1.21)


def test_adjusted_price_caps_the_size_scaling():
    # A 30 m2 sale cannot be stretched to value a 300 m2 home.
    got = adjusted_price(200_000, 30.0, 300.0, date(2026, 1, 1), date(2026, 1, 1), "flat", INDEX)
    assert got == round(200_000 * MAX_SIZE_SCALE)


def test_adjusted_price_without_an_index_or_date_is_the_price_paid():
    assert adjusted_price(500_000, 70.0, 70.0, date(2024, 1, 1), None, "flat", None) == 500_000
    assert adjusted_price(500_000, 70.0, 70.0, date(2024, 1, 1), date(2026, 1, 1), "flat", None) == 500_000


def test_the_quoted_middle_is_one_the_listed_sales_support():
    """
    Guards against a headline no reader could derive.

    Quoting the median of the prices *paid* while the estimate uses the
    size-adjusted median would summarise three sales of £1.36m, £910k and
    £840k as "Around £910k" -- neither their average nor the figure driving
    the estimate.
    """
    from hpldn.backend.api.market.estimator import adjusted_prices, comparables_value
    from hpldn.backend.api.schemas import Comparable

    comparables = [
        Comparable(id="1", street="Albert Bridge Road", district="SW11", property_type="flat",
                   floor_area_sqm=100, price=1_360_000, sold_on=date(2025, 11, 12),
                   distance_km=1.8, match_type="same", adjusted_price=1_400_000),
        Comparable(id="2", street="Plough Road", district="SW11", property_type="flat",
                   floor_area_sqm=100, price=910_000, sold_on=date(2025, 8, 8),
                   distance_km=0.5, match_type="same", adjusted_price=940_000),
        Comparable(id="3", street="Foxmore Street", district="SW11", property_type="flat",
                   floor_area_sqm=100, price=840_000, sold_on=date(2024, 8, 23),
                   distance_km=1.4, match_type="same", adjusted_price=900_000),
        # A broader-type sale is listed but never sets the figure.
        Comparable(id="4", street="Elsewhere Street", district="SW11", property_type="maisonette",
                   floor_area_sqm=100, price=5_000_000, sold_on=date(2025, 1, 1),
                   distance_km=2.0, match_type="broader", adjusted_price=5_100_000),
    ]
    shown = adjusted_prices(comparables)
    assert shown == [1_400_000, 940_000, 900_000]

    value, count = comparables_value(100.0, comparables)
    assert count == 3
    # The quoted middle is the median of exactly the figures shown on the rows.
    assert value == 940_000
    assert min(shown) <= value <= max(shown)


# ---------------------------------------------------------------------------
# ModelEstimator
# ---------------------------------------------------------------------------


class _Prediction:
    """A stand-in for `hpp_inference.PricePrediction`, carrying only what the estimator reads."""

    def __init__(self, value, low, high, uplift=None, projected_from=None):
        self.value = value
        self.low = low
        self.high = high
        self.projection_uplift = uplift
        self.projected_from = projected_from


class _Service:
    """A pricing service that answers with `outcome`, or raises it when it is an exception."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def price_details(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Pricer:
    def __init__(self, outcome):
        self._service = _Service(outcome)

    def service(self):
        return self._service


def _request(**overrides):
    from hpldn.backend.api.schemas import ResolvedEstimateRequest

    return ResolvedEstimateRequest(
        **{"postcode": "SW11 1AD", "district": "SW11", "property_type": "flat",
           "bedrooms": 2, "floor_area_sqm": 100.0, "sale_date": date(2027, 1, 1), **overrides}
    )


class _Fallback:
    """Records that it was asked, and answers with something recognisable."""

    def __init__(self):
        self.called = False

    def estimate(self, request, area, comparables, similar_count, evidence_sales_count):
        from hpldn.backend.api.market.estimator import EstimateCore

        self.called = True
        return EstimateCore(value=1, low=0, high=2, confidence="fair", spread_percent=13)


def test_the_model_estimator_prices_with_the_model_and_reports_its_basis():
    from hpldn.backend.api.market.estimator import ModelEstimator

    pricer = _Pricer(_Prediction(902_345.0, 800_111.0, 1_010_999.0, 0.021, date(2026, 6, 1)))
    fallback = _Fallback()
    core = ModelEstimator(pricer, fallback).estimate(_request(), None, [], 0, 0)

    assert fallback.called is False
    assert core.basis == "model"
    # The price to the pound, like POST /price; the range to the £1,000.
    assert (core.value, core.low, core.high) == (902_345, 800_000, 1_011_000)
    assert core.projection_uplift == 0.021 and core.projected_from == date(2026, 6, 1)
    # The spread is read off the measured band, not a confidence bucket.
    assert core.spread_percent == round(100 * (1_011_000 - 800_000) / (2 * 902_345))
    # The form asks for bedrooms; the model wants habitable rooms.
    assert pricer.service().calls[0]["habitable_rooms"] == 3.0
    assert pricer.service().calls[0]["sale_date"] == date(2027, 1, 1)


def test_a_wider_measured_band_lowers_the_confidence():
    from hpldn.backend.api.market.estimator import ModelEstimator

    tight = ModelEstimator(_Pricer(_Prediction(500_000.0, 475_000.0, 525_000.0))).estimate(
        _request(), None, [], 0, 0
    )
    wide = ModelEstimator(_Pricer(_Prediction(500_000.0, 380_000.0, 620_000.0))).estimate(
        _request(), None, [], 0, 0
    )
    assert tight.confidence == "high" and wide.confidence == "fair"
    assert tight.spread_percent < wide.spread_percent


def test_the_model_estimator_falls_back_when_the_model_cannot_price():
    from hpldn.backend.scripts.hpp_inference import NotEnoughData

    from hpldn.backend.api.market.estimator import ModelEstimator

    for outcome in (
        NotEnoughData("no sales near this postcode"),
        RuntimeError("model pricing unavailable"),
        ValueError("something unforeseen"),
        # A model with no measured range cannot draw a band, so it defers too.
        _Prediction(500_000.0, None, None),
    ):
        fallback = _Fallback()
        core = ModelEstimator(_Pricer(outcome), fallback).estimate(_request(), None, [], 0, 0)
        assert fallback.called is True, outcome
        assert core.basis == "area", outcome


def test_without_a_pricer_at_all_the_area_formula_answers():
    from hpldn.backend.api.market.estimator import ModelEstimator

    fallback = _Fallback()
    core = ModelEstimator(None, fallback).estimate(_request(), None, [], 0, 0)
    assert fallback.called is True and core.basis == "area"


def test_an_estimate_without_a_date_is_priced_for_tomorrow():
    from hpldn.backend.api.market.estimator import ModelEstimator

    pricer = _Pricer(_Prediction(500_000.0, 450_000.0, 550_000.0))
    ModelEstimator(pricer).estimate(_request(sale_date=None), None, [], 0, 0)
    # Must be after today, or `validate_sale_date` would refuse it.
    assert pricer.service().calls[0]["sale_date"] > date.today()
