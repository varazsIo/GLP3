"""
Pure logic of the market domain: no database, no HTTP.

Pins the behaviour the frontend relied on before the helpers and estimate
formula moved to the backend, so the move cannot quietly change a figure.
"""

import re
from datetime import date
from pathlib import Path

import pytest

from hpldn.backend.api.market import estimator
from hpldn.backend.api.market.districts import (
    DISTRICTS,
    OUTER_AREAS,
    area_of,
    district_members,
    district_of,
    evidence_members,
    haversine_km,
    is_full_postcode,
    is_postcode_shaped,
    normalise_postcode,
    property_type_key,
    round_significant,
    street_name,
    transport_score,
)
from hpldn.backend.api.market.repository import (
    LOW_DATA_SALES,
    MIN_COVERED_SALES,
    BoroughRent,
    area_stats_from_rows,
    borough_rent_from_row,
    comparables_from_rows,
    comparables_date_from,
    fallbacks_of,
)
from hpldn.backend.api.schemas import AreaStats, BedroomRents, Comparable, ResolvedEstimateRequest


def make_area(**overrides) -> AreaStats:
    fields = dict(
        code="SE15",
        name="Nunhead, Peckham",
        borough="Southwark",
        average_price=500_000,
        average_price_per_sqm=7_000,
        estimator_price_per_sqm=7_000,
        movement1y=1.0,
        movement5y=4.0,
        transport_score=3,
        walk_to_station_mins=8,
        rental_yield=4.9,
        typical_monthly_rent=2050,
        sales_count=1_500,
        figures_year=2026,
        area_profile_percent=60.0,
        epc_band="C",
    )
    fields.update(overrides)
    return AreaStats(**fields)


def make_request(**overrides) -> ResolvedEstimateRequest:
    fields = dict(
        postcode="SE15 4TQ",
        district="SE15",
        property_type="terraced",
        bedrooms=3,
        floor_area_sqm=92,
    )
    fields.update(overrides)
    return ResolvedEstimateRequest(**fields)


def make_comparable(
    floor_area_sqm: int, price: int, street: str = "Bellenden Road", match_type: str = "same"
) -> Comparable:
    return Comparable(
        id=f"{street}-{floor_area_sqm}",
        street=street,
        district="SE15",
        property_type="terraced",
        floor_area_sqm=floor_area_sqm,
        price=price,
        sold_on=date(2026, 5, 1),
        distance_km=0.4,
        match_type=match_type,
    )


class TestDistrictHelpers:
    def test_the_areas_are_the_hundred_and_thirty_two_the_page_covers(self):
        assert len(DISTRICTS) == 132
        assert DISTRICTS["SW11"] == "Battersea, Clapham Junction"
        assert DISTRICTS["EC1"] == "Barbican, Clerkenwell, Finsbury"
        assert DISTRICTS["BR"] == "Bromley"
        assert list(DISTRICTS)[:6] == ["EC1", "EC2", "EC3", "EC4", "WC1", "WC2"]
        assert list(DISTRICTS)[-12:] == list(OUTER_AREAS)

    @pytest.mark.parametrize(
        "value,rounded",
        [(32.49588, 32.5), (19.7878, 19.8), (9.0987, 9.1), (100.0, 100.0), (60.33, 60.3),
         (0.0, 0.0), (67.45, 67.5)],
    )
    def test_round_significant_keeps_three_figures_halves_up(self, value, rounded):
        assert round_significant(value) == pytest.approx(rounded)

    @pytest.mark.parametrize(
        "outward,area",
        [("SW1V", "SW1"), ("EC2M", "EC2"), ("E1W", "E1"), ("N1C", "N1"), ("W1G", "W1"),
         ("SE1P", "SE1"), ("BR3", "BR"), ("CR0", "CR"), ("KT17", "KT"), ("SW11", "SW11"),
         ("SW1", "SW1"), ("TN16", "TN16"), ("E22", "E22"), ("sw1v", "SW1")],
    )
    def test_area_of_folds_sub_districts_and_outer_codes(self, outward, area):
        assert area_of(outward) == area

    def test_district_of_a_postcode_is_its_area(self):
        assert district_of("SW1V 4AB") == "SW1"
        assert district_of("br3 1aa") == "BR"
        assert district_of("TN16 3AA") == "TN16"

    def test_district_members(self):
        sw1 = district_members("SW1")
        assert "SW1" in sw1 and "SW1V" in sw1 and "SW1A" in sw1
        assert "SW11" not in sw1
        assert "BR1" in district_members("BR") and "BR99" in district_members("BR")
        assert district_members("SW11") == ["SW11"]
        assert district_members("TN16") == ["TN16"]

    def test_every_member_maps_back_onto_its_area(self):
        for code in DISTRICTS:
            assert {area_of(member) for member in district_members(code)} == {code}

    def test_evidence_members(self):
        assert evidence_members("BR3", "BR") == ["BR3"]
        assert evidence_members("BR", "BR") == district_members("BR")
        assert evidence_members("SW1V", "SW1") == district_members("SW1")
        assert evidence_members("SW11", "SW11") == ["SW11"]

    @pytest.mark.parametrize(
        "rank,score",
        [(None, None), (float("nan"), None), (0, 1), (1, 1), (2, 2), (3, 2), (3.5, 3), (4, 3),
         (5, 4), (6, 4), (6.5, 4), (7, 5), (8, 5)],
    )
    def test_transport_score_folds_ptal_ranks_onto_five_bands(self, rank, score):
        assert transport_score(rank) == score

    @pytest.mark.parametrize(
        "raw,street",
        [
            ("17, BELLENDEN ROAD", "Bellenden Road"),
            ("FLAT 3 246-252 RYE LANE", "Flat Rye Lane"),
            ("Canal Grove", "Canal Grove"),
            ("12 McDermott Road", "McDermott Road"),
            ("42", ""),
            (None, ""),
        ],
    )
    def test_street_name_drops_numbers_and_title_cases_upper_case(self, raw, street):
        assert street_name(raw) == street

    @pytest.mark.parametrize(
        "code,maisonette,key",
        [("F", True, "maisonette"), ("F", False, "flat"), ("F", None, "flat"),
         ("T", None, "terraced"), ("S", False, "semi-detached"), ("D", None, "detached")],
    )
    def test_property_type_key_splits_flats_on_the_maisonette_flag(self, code, maisonette, key):
        assert property_type_key(code, maisonette) == key

    @pytest.mark.parametrize(
        "raw,district,normalised",
        [
            ("SW11 2AB", "SW11", "SW11 2AB"),
            ("sw112ab", "SW11", "SW11 2AB"),
            (" se15 4tq ", "SE15", "SE15 4TQ"),
            ("e1 6an", "E1", "E1 6AN"),
            ("EC1V 9AA", "EC1", "EC1V 9AA"),
            ("n1", "N1", "N1"),
        ],
    )
    def test_postcodes_mirror_the_frontend(self, raw, district, normalised):
        assert district_of(raw) == district
        assert normalise_postcode(raw) == normalised
        assert is_postcode_shaped(raw)

    def test_postcode_shape_rejects_nonsense(self):
        assert not is_postcode_shaped("banana")
        assert not is_postcode_shaped("12345")
        assert not is_postcode_shaped("CR")
        assert is_full_postcode("cr0 1aa")
        assert not is_full_postcode("CR0")

    def test_haversine_is_zero_at_a_point_and_about_right_across_london(self):
        """Battersea to Whitechapel is a little under 8 km."""
        assert haversine_km(51.5, -0.1, 51.5, -0.1) == 0
        assert 7 < haversine_km(51.4613, -0.1753, 51.5155, -0.0643) < 10


class TestAreaStatsMapping:
    """The served figures are calendar-year means; the window row supplies the rest."""

    ROW = {
        "code": "SW11",
        "sales_count": 2603,
        "price_per_sqm": 9_615.4,
        "ptal_band_rank": 4.5,
        "station_distance_km": 0.7,
        "epc_band": "C",
        "local_authority": "Wandsworth",
        "centre_latitude": 51.46,
        "centre_longitude": -0.17,
    }
    # SW11 in hpdbldn: 2026, 2025 and 2021 sales.
    YEAR_ROW = {
        "code": "SW11",
        "sales_count": 282,
        "sales_count_previous": 1053,
        "sales_count_5y_ago": 578,
        "average_price": 819_883.4,
        "average_price_previous": 983_263.0,
        "average_price_5y_ago": 1_152_499.0,
        "average_floor_area": 87.744,
        "imd_pct_london": 0.3249588,
    }
    RENTS = {
        "Wandsworth": BoroughRent(
            month=date(2026, 8, 1), rent=2632, by_bedrooms={1: 1938, 2: 2463, 3: 2810, 4: 3818}
        )
    }

    def stats(self, row=None, year_row=None, rents=None):
        return area_stats_from_rows(
            row or self.ROW, year_row or self.YEAR_ROW, 2026, self.RENTS if rents is None else rents
        )

    def test_figures_are_the_years_means(self):
        stats = self.stats()
        area = stats.area
        assert area.name == "Battersea, Clapham Junction"
        assert area.borough == "Wandsworth"
        assert area.average_price == 819_883
        assert area.average_price_per_sqm == 9_344
        assert area.movement1y == -16.6
        assert area.movement5y == -28.9
        assert area.area_profile_percent == 32.5
        assert area.sales_count == 282
        assert area.figures_year == 2026
        assert area.transport_score == 3
        assert area.walk_to_station_mins == 9
        assert area.typical_monthly_rent == 2632
        assert area.rent_month == date(2026, 8, 1)
        assert area.rental_yield == round(2632 * 12 / 819_883 * 100, 1)
        assert area.bedroom_rents.model_dump(by_alias=True) == {
            "oneBed": 1938, "twoBed": 2463, "threeBed": 2810, "fourPlusBed": 3818,
        }
        assert area.estimator_price_per_sqm == 9_620
        assert area.low_data is False
        assert fallbacks_of(area) == []
        assert stats.centre == (51.46, -0.17)

    def test_the_estimate_figure_never_reaches_the_wire(self):
        body = self.stats().area.model_dump(by_alias=True)
        assert "estimatorPricePerSqm" not in body and "estimator_price_per_sqm" not in body
        assert body["averagePrice"] == 819_883

    def test_missing_data_is_unknown_not_a_stand_in_figure(self):
        """MAJ-7: no "1 min walk", "0.0 %" or "0 %" invented for missing inputs."""
        row = dict(self.ROW, station_distance_km=None, ptal_band_rank=None, epc_band=None,
                   centre_latitude=None)
        year_row = dict(self.YEAR_ROW, average_price_previous=None, average_price_5y_ago=None,
                        imd_pct_london=None, average_floor_area=None)
        stats = self.stats(row, year_row)
        area = stats.area
        assert area.movement1y is None
        assert area.movement5y is None
        assert area.average_price_per_sqm is None
        assert area.walk_to_station_mins is None
        assert area.transport_score is None
        assert area.area_profile_percent is None
        assert stats.centre is None
        assert set(fallbacks_of(area)) == {
            "average_price_per_sqm", "movement1y", "movement5y", "walk_to_station_mins",
            "transport_score", "area_profile_percent",
        }

    def test_a_borough_without_a_rent_has_no_rent_or_yield(self):
        stats = self.stats(dict(self.ROW, local_authority="City of London"))
        assert stats.area.borough == "City of London"
        assert (stats.area.typical_monthly_rent, stats.area.rental_yield) == (None, None)
        assert stats.area.bedroom_rents is None
        assert self.stats(rents={}).area.rental_yield is None

    def test_a_suppressed_bedroom_rent_is_none_not_a_stand_in(self):
        rents = {"Wandsworth": BoroughRent(month=date(2026, 8, 1), rent=2632, by_bedrooms={2: 2463})}
        bedroom_rents = self.stats(rents=rents).area.bedroom_rents
        assert (bedroom_rents.one_bed, bedroom_rents.two_bed, bedroom_rents.four_plus_bed) == (None, 2463, None)

    def test_a_london_rents_row_maps_onto_a_borough_rent(self):
        row = {
            "area_name": "Wandsworth", "month": date(2026, 8, 1), "rent_all": 2632,
            "rent_one_bed": 1938, "rent_two_bed": 2463, "rent_three_bed": None, "rent_four_plus_bed": 3818,
        }
        rent = borough_rent_from_row(row)
        assert (rent.month, rent.rent) == (date(2026, 8, 1), 2632)
        assert rent.by_bedrooms == {1: 1938, 2: 2463, 4: 3818}

    def test_a_thin_year_is_flagged_and_an_area_without_sales_is_not_covered(self):
        assert self.stats(year_row=dict(self.YEAR_ROW, sales_count=LOW_DATA_SALES)).area.low_data is False
        thin = self.stats(year_row=dict(self.YEAR_ROW, sales_count=LOW_DATA_SALES - 1)).area
        assert thin.low_data is True
        assert "low_data" in fallbacks_of(thin)
        assert self.stats(dict(self.ROW, sales_count=MIN_COVERED_SALES - 1)) is None
        assert self.stats(dict(self.ROW, price_per_sqm=None)) is None
        assert area_stats_from_rows(self.ROW, None, 2026, self.RENTS) is None
        empty_year = dict(self.YEAR_ROW, sales_count=0, average_price=None)
        assert area_stats_from_rows(self.ROW, empty_year, 2026, self.RENTS) is None


class TestComparablesFromRows:
    def row(self, ppd_id, street, **overrides):
        base = {
            "ppd_id": ppd_id,
            "price": 500_000,
            "dateoftransfer": date(2026, 1, 1),
            "total_floor_area": 72.4,
            "poststreet": street,
            "latitude": 51.47,
            "longitude": -0.07,
            "type_key": "flat",
            "same_type": True,
        }
        base.update(overrides)
        return base

    def test_one_sale_per_street_in_sql_order_up_to_the_limit(self):
        rows = [
            self.row(1, "12 RYE LANE"),
            self.row(2, "14 Rye Lane"),
            self.row(3, "99"),
            self.row(4, "BELLENDEN ROAD"),
            self.row(5, "Choumert Grove"),
            self.row(6, "Lyndhurst Way"),
        ]
        picked = comparables_from_rows(rows, "SE15", (51.47, -0.07), limit=3)
        assert [c.street for c in picked] == ["Rye Lane", "Bellenden Road", "Choumert Grove"]
        assert [c.id for c in picked] == ["1", "4", "5"]
        assert picked[0].floor_area_sqm == 72
        assert picked[0].distance_km == 0.0

    def test_broader_types_only_fill_in_below_three_same_type_sales(self):
        """Another type never displaces the requested one, and is marked."""
        rows = [
            self.row(1, "Rye Lane"),
            self.row(2, "Bellenden Road", type_key="maisonette", same_type=False),
            self.row(3, "Choumert Grove", type_key="maisonette", same_type=False),
        ]
        picked = comparables_from_rows(rows, "SE15", (51.47, -0.07), limit=3)
        assert [(c.id, c.match_type) for c in picked] == [("1", "same"), ("2", "broader"), ("3", "broader")]

        same = [self.row(i, f"Street {chr(64 + i)}") for i in range(1, 4)]
        picked = comparables_from_rows(same + rows[1:], "SE15", (51.47, -0.07), limit=5)
        assert [c.match_type for c in picked] == ["same", "same", "same"]

    def test_no_candidates_is_an_empty_list(self):
        assert comparables_from_rows([], "SE15", (51.47, -0.07), limit=3) == []

    def test_nearby_sales_are_from_the_last_two_years(self):
        from hpldn.backend.api.schemas import DataWindow

        window = DataWindow(from_year=2023, to_year=2026, latest_sale=date(2026, 7, 31),
                            total_sales=1, first_sale_year=1995)
        assert comparables_date_from(window) == date(2024, 7, 31)


class TestHeuristicEstimator:
    def test_value_is_the_ported_formula_rounded_to_a_thousand(self):
        """Expected bedrooms for 92 m² is 3, so only transport score 4 adjusts, by +1.8%."""
        area = make_area(estimator_price_per_sqm=7_000, transport_score=4)
        request = make_request(property_type="terraced", bedrooms=3, floor_area_sqm=92)
        core = estimator.HeuristicEstimator().estimate(
            request, area, [], similar_count=100, evidence_sales_count=1_500
        )
        assert core.value == round(7_000 * 92 * 1.0 * 1.0 * 1.018 / 1000) * 1000
        assert core.value % 1000 == 0

    def test_unknown_transport_leaves_the_value_unadjusted(self):
        est = estimator.HeuristicEstimator()
        known = est.estimate(make_request(), make_area(transport_score=3), [], 100, 1_500)
        unknown = est.estimate(make_request(), make_area(transport_score=None), [], 100, 1_500)
        assert known.value == unknown.value

    def test_range_brackets_the_value_by_the_confidence_spread(self):
        area = make_area(sales_count=2_000)
        core = estimator.HeuristicEstimator().estimate(
            make_request(), area, [make_comparable(92, 640_000)], similar_count=300,
            evidence_sales_count=2_000,
        )
        assert core.confidence == "high"
        assert core.spread_percent == 7
        assert core.low < core.value < core.high
        assert core.low == round(core.value * 0.93 / 1000) * 1000
        assert core.high == round(core.value * 1.07 / 1000) * 1000

    def test_the_estimate_moves_with_the_property(self):
        est = estimator.HeuristicEstimator()
        area = make_area()
        base = est.estimate(make_request(), area, [], 100, 1_500).value
        assert est.estimate(make_request(floor_area_sqm=140), area, [], 100, 1_500).value > base
        assert est.estimate(make_request(property_type="detached"), area, [], 100, 1_500).value > base
        assert est.estimate(make_request(property_type="flat"), area, [], 100, 1_500).value < base
        assert est.estimate(make_request(bedrooms=5), area, [], 100, 1_500).value > base

    def test_it_is_deterministic(self):
        est = estimator.HeuristicEstimator()
        assert est.estimate(make_request(), make_area(), [], 50, 1_500) == est.estimate(
            make_request(), make_area(), [], 50, 1_500
        )

    def test_high_confidence_uses_the_evidence_scope_not_the_whole_area(self):
        """An outer outward code is judged on its own sales, not the borough's."""
        comps = [make_comparable(92, 640_000)]
        est = estimator.HeuristicEstimator()
        area = make_area(sales_count=9_000)
        assert est.estimate(make_request(), area, comps, 300, 9_000).confidence == "high"
        assert est.estimate(make_request(), area, comps, 300, 400).confidence == "medium"

    @pytest.mark.parametrize(
        "sales,similar,confidence,spread",
        [
            (750, 150, "high", 7),
            (749, 150, "medium", 10),
            (3_000, 149, "medium", 10),
            (300, 25, "medium", 10),
            (3_000, 24, "fair", 13),
            (3, 0, "fair", 13),
        ],
    )
    def test_confidence_buckets(self, sales, similar, confidence, spread):
        assert estimator.confidence_for(sales, similar) == (confidence, spread)

    def test_no_nearby_sales_or_a_low_data_area_is_fair(self):
        assert estimator.confidence_for(3_000, 300, has_comparables=False) == ("fair", 13)
        assert estimator.confidence_for(3_000, 300, low_data=True) == ("fair", 13)

    def test_nearby_sales_pull_the_value_towards_them(self):
        """The value blends the formula (£644k) with the same-type sales (£480k)."""
        est = estimator.HeuristicEstimator()
        comps = [make_comparable(92, 470_000, "A Road"), make_comparable(92, 480_000, "B Road"),
                 make_comparable(92, 490_000, "C Road")]
        formula = est.estimate(make_request(), make_area(), [], 300, 2_000).value
        blended = est.estimate(make_request(), make_area(), comps, 300, 2_000)
        assert formula == 644_000
        assert blended.value == round((0.4 * 644_000 + 0.6 * 480_000) / 1000) * 1000
        assert blended.low == 480_000 < blended.value < blended.high

    def test_one_nearby_sale_gets_a_third_of_the_full_weight(self):
        est = estimator.HeuristicEstimator()
        core = est.estimate(make_request(), make_area(), [make_comparable(92, 600_000)], 300, 2_000)
        assert core.value == round((0.8 * 644_000 + 0.2 * 600_000) / 1000) * 1000

    def test_broader_sales_never_set_the_value(self):
        est = estimator.HeuristicEstimator()
        broader = [make_comparable(92, 300_000, match_type="broader")]
        assert est.estimate(make_request(), make_area(), broader, 300, 2_000).value == 644_000

    def test_nearby_prices_are_scaled_to_the_size_within_a_bound(self):
        """A 46 m² sale at £300k is scaled by at most 1.25 to £375k, not doubled to £600k."""
        assert estimator.comparables_value(92, [make_comparable(46, 300_000)]) == (375_000, 1)
        assert estimator.comparables_value(92, [make_comparable(100, 500_000)]) == (460_000, 1)
        assert estimator.comparables_value(92, []) is None

    def test_disagreeing_evidence_lowers_the_confidence(self):
        est = estimator.HeuristicEstimator()
        agree = [make_comparable(92, 640_000)]
        disagree = [make_comparable(92, 450_000)]
        assert est.estimate(make_request(), make_area(), agree, 300, 2_000).confidence == "high"
        core = est.estimate(make_request(), make_area(), disagree, 300, 2_000)
        assert (core.confidence, core.spread_percent) == ("medium", 10)

    def test_bedroom_expectation_uses_half_up_rounding(self):
        """(78 - 18) / 24 = 2.5 rounds to 3, as JavaScript does, not to 2."""
        assert estimator.expected_bedrooms(78) == 3
        assert estimator.expected_bedrooms(15) == 1
        assert estimator.expected_bedrooms(1000) == 6

    def test_rent_is_the_boroughs_rent_for_the_bedrooms(self):
        area = make_area(bedroom_rents=BedroomRents(one_bed=1500, two_bed=1900, three_bed=2300, four_plus_bed=3100))
        assert [estimator.suggested_monthly_rent(beds, area) for beds in (1, 2, 3, 4)] == [1500, 1900, 2300, 3100]

    def test_five_and_six_bedrooms_take_the_four_plus_rent(self):
        area = make_area(bedroom_rents=BedroomRents(one_bed=1500, two_bed=1900, three_bed=2300, four_plus_bed=3100))
        assert estimator.suggested_monthly_rent(5, area) == estimator.suggested_monthly_rent(6, area) == 3100

    def test_rent_falls_back_to_the_boroughs_mean(self):
        suppressed = make_area(bedroom_rents=BedroomRents(one_bed=1500, two_bed=None, three_bed=2300, four_plus_bed=3100))
        assert estimator.suggested_monthly_rent(2, suppressed) == 2050
        assert estimator.suggested_monthly_rent(2, make_area(bedroom_rents=None)) == 2050

    def test_no_rent_where_the_borough_has_none(self):
        city = make_area(borough="City of London", rental_yield=None, typical_monthly_rent=None, bedroom_rents=None)
        assert estimator.suggested_monthly_rent(2, city) is None


class TestDrivers:
    COMPS = [make_comparable(80, 480_000, "A Road"), make_comparable(90, 520_000, "B Road"),
             make_comparable(100, 610_000, "C Road")]

    def test_four_drivers_in_the_order_the_page_shows_them(self):
        drivers = estimator.build_drivers(make_area(estimator_price_per_sqm=6_000), make_request(), self.COMPS)
        assert [d.id for d in drivers] == ["comparables", "momentum", "transport", "size"]
        # The range and the middle, so mean-vs-median cannot mislead: a reader
        # who averages the sales listed beside it does not get a different
        # number from the one quoted.
        # Scaled to one size the three sales converge (the prices paid span
        # £480k-£610k across 80-100 m2), which is the point of quoting them.
        assert drivers[0].detail == "£530k-£560k across 3 nearby sales, middle £550k, at this size today"
        assert drivers[0].tone == "neutral"

    def test_transport_words_match_the_area_tile(self):
        """The reason uses the same five words as area/TransportMeter.tsx."""
        tsx = (Path(__file__).parents[3] / "frontend/src/area/TransportMeter.tsx").read_text()
        words = re.search(r"const WORDS = \[(.*?)\];", tsx).group(1)
        assert tuple(re.findall(r"'([^']+)'", words)) == estimator.TRANSPORT_WORDS

    @pytest.mark.parametrize("price_per_sqm,tone", [(5_000, "positive"), (6_000, "neutral"), (7_000, "caution")])
    def test_comparables_tone_says_which_way_they_pulled(self, price_per_sqm, tone):
        """Nearby sales at ~£552k help a £460k formula and hold back a £644k one."""
        driver = estimator.build_drivers(make_area(estimator_price_per_sqm=price_per_sqm), make_request(), self.COMPS)[0]
        assert driver.tone == tone

    @pytest.mark.parametrize(
        "movement,label,tone,detail",
        [
            (2.0, "Prices here are rising", "positive", "Up 2.0% over the last year"),
            (0.0, "Prices here are steady", "neutral", "Up 0.0% over the last year"),
            (-3.5, "Prices here have eased", "caution", "Down 3.5% over the last year"),
        ],
    )
    def test_momentum_tones(self, movement, label, tone, detail):
        driver = estimator.build_drivers(make_area(movement1y=movement), make_request(), self.COMPS)[1]
        assert (driver.label, driver.tone, driver.detail) == (label, tone, detail)

    @pytest.mark.parametrize(
        "score,label,tone",
        [(5, "Excellent transport", "positive"), (4, "Very good transport", "positive"),
         (3, "Good transport", "neutral"), (2, "Modest transport", "caution"),
         (1, "Limited transport", "caution")],
    )
    def test_transport_tones(self, score, label, tone):
        driver = estimator.build_drivers(make_area(transport_score=score), make_request(), self.COMPS)[2]
        assert (driver.label, driver.tone) == (label, tone)

    @pytest.mark.parametrize(
        "floor_area,label,tone",
        [(104, "Larger than most homes nearby", "positive"),
         (76, "Smaller than most homes nearby", "caution"),
         (90, "A typical size for the street", "neutral")],
    )
    def test_size_is_judged_against_the_nearby_sales(self, floor_area, label, tone):
        driver = estimator.build_drivers(
            make_area(), make_request(floor_area_sqm=floor_area), self.COMPS
        )[3]
        assert (driver.label, driver.tone) == (label, tone)

    def test_size_falls_back_to_seventy_square_metres_without_nearby_sales(self):
        driver = estimator.build_drivers(make_area(), make_request(floor_area_sqm=70), [])[3]
        assert driver.tone == "neutral"
        assert "nearby" not in driver.label

    def test_no_nearby_sales_never_reads_around_zero_pounds(self):
        """WD3 1AA once showed "Around £0 on nearby streets"."""
        driver = estimator.build_drivers(make_area(), make_request(), [])[0]
        assert driver.id == "comparables"
        assert "£0" not in driver.detail
        assert driver.detail == "Not enough recent similar sales nearby to compare"
        assert driver.tone == "caution"

    def test_broader_sales_are_named_as_such_and_never_set_a_same_type_median(self):
        broader = [make_comparable(80, 400_000, "A Road", "broader"),
                   make_comparable(90, 600_000, "B Road", "broader")]
        driver = estimator.build_drivers(make_area(), make_request(), broader)[0]
        assert driver.label == "Few homes like this sold nearby"
        assert driver.detail == "Other houses nearby sold for around £500k"
        mixed = [make_comparable(92, 520_000)] + broader
        assert estimator.build_drivers(make_area(), make_request(), mixed)[0].detail == (
            "One nearby sale, worth £520k at this size today"
        )

    def test_unknown_momentum_and_transport_say_so(self):
        """MAJ-7: no "Up 0.0 %" or "1 min walk" for missing data."""
        area = make_area(movement1y=None, transport_score=None, walk_to_station_mins=None)
        _, momentum, transport, _ = estimator.build_drivers(area, make_request(), self.COMPS)
        assert momentum.label == "Not enough sales to show a trend"
        assert "%" not in momentum.detail
        assert transport.label == "Transport access not known"
        assert "min walk" not in transport.detail

    def test_compact_money_matches_the_frontend_formatter(self):
        assert estimator.format_gbp_compact(520_000) == "£520k"
        assert estimator.format_gbp_compact(1_340_000) == "£1.34m"
        assert estimator.format_gbp_compact(2_500) == "£3k"
        assert estimator.format_gbp_compact(0) == "£0"

    def test_no_backend_vocabulary_reaches_the_landlord(self):
        drivers = estimator.build_drivers(make_area(), make_request(), self.COMPS)
        text = " ".join(d.label + " " + d.detail for d in drivers).lower()
        for term in ("catboost", "ptal", "msoa", "lsoa", "shap", "rmse", "epc_", "spatial_lag"):
            assert term not in text
