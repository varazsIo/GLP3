"""
The local price index (`local_index`) on synthetic repeat sales: no database needed.

A known index is planted in the pairs: London moves one way, one borough and
one district move their own way on top of it. The estimate must recover those
paths, fall back to the parent series where pairs are thin, move every day,
and project each series by its own fitted growth.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.scripts import hpp_inference, local_index

MONTHS = pd.date_range("2000-01-01", "2020-12-01", freq="MS")


def _ukhpi(monthly_growth: dict[str, float]) -> pd.DataFrame:
    """A UK HPI frame whose type columns grow at a constant monthly log rate."""
    t = np.arange(len(MONTHS))
    frame = {"period": MONTHS, "avg_price_all_property_types": 300_000 * np.exp(0.003 * t)}
    for kind, column in (
        ("D", "avg_price_detached"),
        ("S", "avg_price_semi_detached"),
        ("T", "avg_price_terraced"),
        ("F", "avg_price_flats_maisonettes"),
    ):
        frame[column] = 300_000 * np.exp(monthly_growth.get(kind, 0.003) * t)
    return pd.DataFrame(frame)


def _pairs(n: int, district: str, borough: str, kind: str, true_log_level, rng, noise=0.02) -> pd.DataFrame:
    """Repeat-sale pairs whose prices follow `true_log_level(month)` plus noise."""
    first = rng.integers(0, len(MONTHS) - 12, size=n)
    gap = rng.integers(7, 60, size=n)
    second = np.minimum(first + gap, len(MONTHS) - 1)
    keep = second > first + 6
    first, second = first[keep], second[keep]
    base = 400_000 * np.exp(rng.normal(0, 0.3, size=first.size))
    p1 = base * np.exp(true_log_level(first) + rng.normal(0, noise, size=first.size))
    p2 = base * np.exp(true_log_level(second) + rng.normal(0, noise, size=first.size))
    return pd.DataFrame(
        {
            "postcode_district": district,
            "borough": borough,
            "propertytype": kind,
            "prev_date": MONTHS[first] + pd.Timedelta(days=10),
            "dateoftransfer": MONTHS[second] + pd.Timedelta(days=10),
            "prev_price": p1,
            "price": p2,
        }
    )


@pytest.fixture(scope="module")
def planted():
    """London terraces +0.3 %/month; AA2 +0.2 %/month on top of London; AA1 -0.6 %/month on top of AA2."""
    rng = np.random.default_rng(7)
    ukhpi = _ukhpi({"T": 0.003, "F": 0.001})
    london = lambda m: 0.003 * m  # noqa: E731
    borough = lambda m: london(m) + 0.002 * m  # noqa: E731
    district = lambda m: borough(m) - 0.006 * m  # noqa: E731
    pairs = pd.concat(
        [
            _pairs(3000, "AA1", "Borough A", "T", district, rng),
            _pairs(3000, "AA2", "Borough A", "T", borough, rng),
            _pairs(3000, "BB1", "Borough B", "T", london, rng),
            # Too few for its own series: it must follow Borough A.
            _pairs(40, "AA3", "Borough A", "T", borough, rng),
        ],
        ignore_index=True,
    )
    return pairs, ukhpi


def test_borough_names_come_from_lsoa_names():
    assert local_index.borough_of("Wandsworth 012C") == "Wandsworth"
    assert local_index.borough_of("Richmond upon Thames 003A") == "Richmond upon Thames"
    assert local_index.borough_of(None) is None


def test_recovers_the_planted_district_and_borough_paths(planted):
    pairs, ukhpi = planted
    index = local_index.LocalIndex.build(pairs, ukhpi, {"smoothing": {"borough": 100.0, "district": 100.0}})
    ten_years = (date(2005, 1, 1), date(2015, 1, 1))
    months = 120
    # AA1: +0.3 +0.2 -0.6 = -0.1 %/month; AA2: +0.5 %/month; London: +0.3 %/month.
    assert np.log(index.factor(*ten_years, "AA1", "T")) == pytest.approx(-0.001 * months, abs=0.03)
    assert np.log(index.factor(*ten_years, "AA2", "T")) == pytest.approx(0.005 * months, abs=0.03)
    assert np.log(index.factor(*ten_years, None, "T")) == pytest.approx(0.003 * months, abs=1e-9)


def test_a_thin_district_follows_its_borough(planted):
    pairs, ukhpi = planted
    index = local_index.LocalIndex.build(pairs, ukhpi)
    assert index.chain("AA3", "T") == ["borough:Borough A:T", "london:London:T"]
    # Borough A pools its districts, so a thin one sits between them.
    span = (date(2005, 1, 1), date(2015, 1, 1))
    assert index.factor(*span, "AA1", "T") < index.factor(*span, "AA3", "T") < index.factor(*span, "AA2", "T")
    assert index.level_area_of("AA3", "T") == "Borough A"
    # An unknown district, or a type with no local pairs, is London's.
    assert index.chain("ZZ9", "F") == ["london:London:F"]


def test_the_level_moves_every_day_between_months(planted):
    pairs, ukhpi = planted
    index = local_index.LocalIndex.build(pairs, ukhpi)
    days = pd.date_range("2010-03-01", "2010-04-01", freq="D")
    levels = np.array([index.log_level(day, "BB1", "T") for day in days])
    steps = np.diff(levels)
    assert np.all(steps != 0)
    # Continuous: no day jumps by more than a month's growth.
    assert np.max(np.abs(steps)) < 0.01


def test_each_series_is_projected_by_its_own_fitted_growth(planted):
    pairs, ukhpi = planted
    base = local_index.LocalIndex.build(pairs, ukhpi)
    fitted = {"smoothing": {"borough": 1000.0, "district": 1000.0}, "series": base.fit_projections()}
    index = local_index.LocalIndex.build(pairs, ukhpi, fitted)
    anchor = index.anchor
    later = anchor + pd.Timedelta(days=365)
    rising = index.factor(anchor, later, "AA2", "T")
    falling = index.factor(anchor, later, "AA1", "T")
    assert rising > 1.0 > falling
    assert index.area_of("AA2", "T") in {"AA2", "Borough A"}
    # Day to day the projected price moves too.
    assert index.factor(later, later + pd.Timedelta(days=1), "AA2", "T") != 1.0
    # Its measured forecast error widens the range beyond the index.
    assert index.sigma("AA2", "T", 1.0) > 0.0 and index.sigma("AA2", "T", 0.0) == 0.0


def test_types_move_at_their_own_rates(planted):
    pairs, ukhpi = planted
    index = local_index.LocalIndex.build(pairs, ukhpi)
    span = (date(2005, 1, 1), date(2015, 1, 1))
    assert index.factor(*span, None, "T") != pytest.approx(index.factor(*span, None, "F"))


# ---------------------------------------------------------------------------
# Area strata and the repeat-sale blend in `hpp_inference`
# ---------------------------------------------------------------------------


def test_area_strata_are_looked_up_before_band_strata():
    calibration = {
        "log_ratio_quantiles": {"p10": -0.3, "p50": 0.0, "p90": 0.3},
        "strata": {
            "£1-2m|T": {"n": 500, "log_ratio_quantiles": {"p10": -0.2, "p50": 0.0, "p90": 0.2}},
            "borough:Wandsworth|T": {"n": 300, "log_ratio_quantiles": {"p10": -0.15, "p50": -0.02, "p90": 0.1}},
            "district:SW11|T": {"n": 80, "log_ratio_quantiles": {"p10": -0.17, "p50": -0.037, "p90": 0.1}},
        },
    }
    quantiles, key = hpp_inference._quantiles_for(calibration, 1_500_000, "T", 1.0, "SW11", "Wandsworth")
    assert key == "district:SW11|T"
    _, key = hpp_inference._quantiles_for(calibration, 1_500_000, "T", 1.0, "SW18", "Wandsworth")
    assert key == "borough:Wandsworth|T"
    _, key = hpp_inference._quantiles_for(calibration, 1_500_000, "T", 1.0, None, None)
    assert key == "£1-2m|T"
    low, high, centre, _ = hpp_inference._range_from_calibration(
        1_500_000, calibration, propertytype="T", district="SW11", borough="Wandsworth"
    )
    # The area's own bias moves the price, and its asymmetric margins the range.
    assert centre == pytest.approx(1_500_000 * np.exp(-0.037))
    assert (centre - low) / centre != pytest.approx((high - centre) / centre, rel=0.01)


def test_area_quantiles_are_shrunk_toward_their_parent():
    rng = np.random.default_rng(3)
    n = 4000
    kinds = np.array(["T"] * n)
    district = np.array(["AA1"] * 30 + ["BB1"] * (n - 30), dtype=object)
    borough = np.array(["A"] * 30 + ["B"] * (n - 30), dtype=object)
    log_ratio = rng.normal(0.0, 0.1, size=n)
    log_ratio[:30] += 0.3  # a small area the model misreads badly
    blocks, prior, scores = hpp_inference._area_calibration(log_ratio, kinds, district, borough)
    assert prior in hpp_inference.AREA_PRIOR_GRID and set(scores) == {str(p) for p in hpp_inference.AREA_PRIOR_GRID}
    small = blocks["district:AA1|T"]["log_ratio_quantiles"]["p50"]
    # Pulled toward the type's median (about 0) but still showing the area's bias.
    assert 0.0 < small < 0.3 + 0.05


def test_repeat_sale_blend_is_switched_on_only_when_it_helps():
    rng = np.random.default_rng(11)
    n = 2000
    actual = 500_000 * np.exp(rng.normal(0, 0.3, n))
    months = rng.uniform(1, 200, n)
    model = actual * np.exp(rng.normal(0, 0.15, n))
    # A previous sale indexed to the date that is far more accurate.
    repeat = actual * np.exp(rng.normal(0, 0.03, n))
    blended, block = hpp_inference._repeat_sale_blend(actual, model, repeat, months)
    assert block["enabled"] is True and max(block["weights"]) > 0.5
    assert np.median(np.abs(blended - actual) / actual) < np.median(np.abs(model - actual) / actual)
    weight = hpp_inference.repeat_sale_weight({"repeat_sale_blend": block}, 12.0)
    assert weight == block["weights"][hpp_inference._repeat_sale_bucket(12.0)]

    # A misleading one (every previous sale reads about 2.7 times too high) stays off.
    biased = actual * np.exp(rng.normal(1.0, 0.3, n))
    unchanged, block = hpp_inference._repeat_sale_blend(actual, model, biased, months)
    assert block["enabled"] is False
    assert np.array_equal(unchanged, model)
    assert hpp_inference.repeat_sale_weight({"repeat_sale_blend": block}, 12.0) == 0.0


# ---------------------------------------------------------------------------
# Value-tier area strata (a £3m flat is not read by SW3's sub-£2m flats)
# ---------------------------------------------------------------------------


def test_tiered_area_strata_are_looked_up_before_untiered_and_band_strata():
    calibration = {
        "log_ratio_quantiles": {"p10": -0.3, "p50": 0.0, "p90": 0.3},
        "strata": {
            "£2m+|F": {"n": 500, "log_ratio_quantiles": {"p10": -0.25, "p50": -0.022, "p90": 0.2}},
            "district:SW3|F": {"n": 900, "log_ratio_quantiles": {"p10": -0.2, "p50": -0.056, "p90": 0.1}},
            "borough:Kensington and Chelsea|F|2m+": {
                "n": 60, "log_ratio_quantiles": {"p10": -0.24, "p50": -0.03, "p90": 0.19},
            },
            "district:SW3|F|2m+": {"n": 40, "log_ratio_quantiles": {"p10": -0.22, "p50": -0.02, "p90": 0.18}},
        },
    }
    _, key = hpp_inference._quantiles_for(calibration, 3_200_000, "F", 1.0, "SW3", "Kensington and Chelsea")
    assert key == "district:SW3|F|2m+"
    _, key = hpp_inference._quantiles_for(calibration, 3_200_000, "F", 1.0, "SW10", "Kensington and Chelsea")
    assert key == "borough:Kensington and Chelsea|F|2m+"
    # A cheaper SW3 flat has no tiered block here and takes the district's own.
    _, key = hpp_inference._quantiles_for(calibration, 800_000, "F", 1.0, "SW3", "Kensington and Chelsea")
    assert key == "district:SW3|F"
    _, _, centre, _ = hpp_inference._range_from_calibration(
        3_200_000, calibration, propertytype="F", district="SW3", borough="Kensington and Chelsea"
    )
    assert centre == pytest.approx(3_200_000 * np.exp(-0.02))


def test_tiered_area_strata_shrink_toward_the_band_block_not_the_type():
    rng = np.random.default_rng(5)
    n = 6000
    kinds = np.array(["F"] * n)
    # Mostly cheaper flats the model reads 6 % high, and a few dear ones it reads right.
    predicted = np.where(np.arange(n) < 5400, 600_000.0, 3_000_000.0)
    district = np.array(["SW3"] * 400 + ["ZZ9"] * 5000 + ["SW3"] * 30 + ["ZZ9"] * 570, dtype=object)
    borough = np.array(["K"] * 400 + ["Z"] * 5000 + ["K"] * 30 + ["Z"] * 570, dtype=object)
    log_ratio = np.where(predicted < 2e6, -0.06, 0.0) + rng.normal(0, 0.02, n)
    blocks, _, _ = hpp_inference._area_calibration(log_ratio, kinds, district, borough, predicted=predicted)
    assert "district:SW3|F" not in blocks and "district:SW3|F|<1m" in blocks
    dear = blocks["district:SW3|F|2m+"]
    assert dear["parent"] in ("borough:K|F|2m+", "£2m+|F")
    # Shrunk toward the £2m+ flats (about 0), not toward all flats (about -0.054).
    assert abs(dear["log_ratio_quantiles"]["p50"]) < 0.01
    assert blocks["district:SW3|F|<1m"]["log_ratio_quantiles"]["p50"] < -0.05
    # With no parent borough block the root is named for its band.
    assert hpp_inference._tier_root_name("F", "2m+") == "£2m+|F"
    assert hpp_inference._tier_root_name("F", "<1m") == "<1m|F"


# ---------------------------------------------------------------------------
# The recent-sale anchor
# ---------------------------------------------------------------------------


def _blend_calibration(weights=(0.15, 0.15, 0.15, 0.5, 0.2, 0.05), enabled=True):
    return {
        "repeat_sale_blend": {
            "enabled": enabled,
            "bucket_edges_months": list(hpp_inference.REPEAT_SALE_BUCKET_EDGES_MONTHS),
            "weights": list(weights),
        }
    }


def test_the_recent_sale_floor_applies_at_5_months_and_not_at_30():
    calibration = _blend_calibration()
    assert hpp_inference.repeat_sale_weight(calibration, 5.0, recent_eligible=True) == pytest.approx(0.9)
    assert hpp_inference.repeat_sale_weight(calibration, 5.0) == pytest.approx(0.15)
    assert hpp_inference.repeat_sale_weight(calibration, 10.0, recent_eligible=True) == pytest.approx(0.8)
    # Halfway from 12 to 24 months, halfway from 0.8 to the fitted weight at 24 (0.5).
    assert hpp_inference.repeat_sale_weight(calibration, 18.0, recent_eligible=True) == pytest.approx(0.65)
    assert hpp_inference.repeat_sale_weight(calibration, 30.0, recent_eligible=True) == pytest.approx(0.5)
    # Even with the fitted blend gated off, and with no calibration at all.
    off = _blend_calibration(enabled=False)
    assert hpp_inference.repeat_sale_weight(off, 5.0, recent_eligible=True) == pytest.approx(0.9)
    assert hpp_inference.repeat_sale_weight(off, 18.0, recent_eligible=True) == pytest.approx(0.4)
    assert hpp_inference.repeat_sale_weight(None, 30.0, recent_eligible=True) == 0.0


def _epcs(*certificates):
    rows = [
        {"inspection_date": pd.Timestamp(day), "lodgement_datetime": pd.Timestamp(day), "id": i,
         "total_floor_area": area, "current_energy_rating": rating}
        for i, (day, area, rating) in enumerate(certificates)
    ]
    return pd.DataFrame(rows)


def _home(epcs):
    return hpp_inference.PropertyRecord(
        address=hpp_inference.PropertyAddress("11D", "QUEENSDALE ROAD", "LONDON", "W11 4QF"),
        address_ids=[1], sales=pd.DataFrame(), epcs=epcs, coordinates=None,
    )


def test_the_floor_does_not_apply_after_a_later_certificate_shows_more_floor_area():
    sold = pd.Timestamp("2026-04-27")
    unchanged = _home(_epcs(("2026-03-01", 100.0, "D"), ("2026-08-01", 104.0, "D")))
    extended = _home(_epcs(("2026-03-01", 100.0, "D"), ("2026-08-01", 120.0, "D")))
    upgraded = _home(_epcs(("2026-03-01", 100.0, "E"), ("2026-08-01", 100.0, "C")))
    assert hpp_inference._recent_sale_eligible(unchanged, sold, 2_500_000.0)
    assert not hpp_inference._recent_sale_eligible(extended, sold, 2_500_000.0)
    assert not hpp_inference._recent_sale_eligible(upgraded, sold, 2_500_000.0)
    # A certificate after the day priced is not known then.
    assert hpp_inference._recent_sale_eligible(extended, sold, 2_500_000.0, day="2026-07-01")
    # No certificate at the sale to compare with is no signal; an implausible price never anchors.
    assert hpp_inference._recent_sale_eligible(_home(_epcs(("2026-08-01", 120.0, "B"))), sold, 2_500_000.0)
    assert not hpp_inference._recent_sale_eligible(unchanged, sold, 100.0)


def test_an_anchored_price_takes_its_range_from_the_recent_sale_block():
    calibration = {
        "log_ratio_quantiles": {"p10": -0.3, "p50": 0.0, "p90": 0.3},
        "strata": {"district:W11|T|2m+": {"n": 90, "log_ratio_quantiles": {"p10": -0.2, "p50": -0.04, "p90": 0.2}}},
        "recent_sale": {
            "≤12": {"n": 300, "log_ratio_quantiles": {"p10": -0.08, "p50": 0.01, "p90": 0.1}},
            "12–24": {"n": 200, "log_ratio_quantiles": {"p10": -0.12, "p50": 0.02, "p90": 0.15}},
        },
    }
    low, high, centre, stratum = hpp_inference._range_from_calibration(
        2_470_000, calibration, propertytype="T", district="W11", borough="Kensington and Chelsea",
        recent_sale="≤12",
    )
    assert stratum == "recent_sale:≤12"
    # Recentred by the block's own median only -- the area's -4 % is not stacked on it.
    assert centre == pytest.approx(2_470_000 * np.exp(0.01))
    assert high / low == pytest.approx(np.exp(0.18))
    _, _, _, stratum = hpp_inference._range_from_calibration(
        2_470_000, calibration, propertytype="T", district="W11", borough="Kensington and Chelsea",
    )
    assert stratum == "district:W11|T|2m+"
    # A calibration written before the block existed falls back to the strata.
    del calibration["recent_sale"]
    _, _, _, stratum = hpp_inference._range_from_calibration(
        2_470_000, calibration, propertytype="T", district="W11", borough="Kensington and Chelsea",
        recent_sale="≤12",
    )
    assert stratum == "district:W11|T|2m+"


def test_recent_sale_blocks_shrink_a_thin_bucket_toward_the_pooled_one():
    rng = np.random.default_rng(2)
    months = np.concatenate([rng.uniform(0, 12, 400), rng.uniform(12, 24, 20), rng.uniform(30, 100, 300)])
    log_ratio = np.concatenate([rng.normal(0.0, 0.05, 400), rng.normal(0.2, 0.05, 20), rng.normal(0, 0.2, 300)])
    blocks = hpp_inference._recent_sale_calibration(log_ratio, months, np.ones(months.size, dtype=bool))
    assert blocks["≤24"]["n"] == 420 and blocks["≤12"]["n"] == 400 and blocks["12–24"]["n"] == 20
    assert blocks["12–24"]["shrunk_toward"] == "≤24"
    assert 0.0 < blocks["12–24"]["log_ratio_quantiles"]["p50"] < 0.15
    assert hpp_inference._recent_sale_calibration(log_ratio[:30], months[:30], np.ones(30, dtype=bool)) == {}


def test_renovated_resales_are_left_out_of_the_fit_and_eligible_ones_get_their_floor():
    rng = np.random.default_rng(7)
    n = 3000
    actual = 800_000 * np.exp(rng.normal(0, 0.3, n))
    months = rng.uniform(1, 200, n)
    model = actual * np.exp(rng.normal(0, 0.12, n))
    repeat = actual * np.exp(rng.normal(0, 0.05, n))
    renovated = np.zeros(n, dtype=bool)
    renovated[:300] = True
    # Renovated homes resold well above their indexed purchase: they would drag the weights down.
    repeat[:300] = actual[:300] * 0.7
    eligible = ~renovated
    served, block = hpp_inference._repeat_sale_blend(
        actual, model, repeat, months, renovated=renovated, recent_eligible=eligible
    )
    assert block["n_renovated_excluded"] == 300 and block["n_repeat_sales"] == n
    recent = eligible & (months <= 6)
    weights = np.log(served[recent] / model[recent]) / np.log(repeat[recent] / model[recent])
    assert np.all(weights >= 0.9 - 1e-9)
