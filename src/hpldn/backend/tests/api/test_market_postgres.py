"""
The market SQL against the real, populated hpdbldn database.

Reads hpdbldn through `hpdbldn_ro` (fails when it is unreachable). Every
statement runs in a rolled-back READ ONLY transaction and, after one warming
run, is held to a latency budget well inside ``hpldn_api``'s 5 s statement
timeout.
"""

import time
from datetime import date

import pytest
from sqlalchemy import text

from hpldn.backend.api.market import queries
from hpldn.backend.api.market.districts import (
    DISTRICTS,
    MAX_FLOOR_AREA,
    MIN_FLOOR_AREA,
    comparable_radius_km,
    round_significant,
    district_members,
    evidence_members,
    haversine_km,
    type_group,
)
from hpldn.backend.api.market.repository import (
    LOW_DATA_SALES,
    MAX_PRICE_PER_SQM,
    MIN_COVERED_SALES,
    area_stats_from_rows,
    borough_rent_from_row,
    comparables_from_rows,
)
from hpldn.backend.common.constants import MIN_PLAUSIBLE_PRICE

LATENCY_BUDGET_S = 1.0


@pytest.fixture(scope="module")
def engine(hpdbldn_ro):
    return hpdbldn_ro


@pytest.fixture
def readonly(engine):
    """Yield a connection in a READ ONLY transaction, always rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield connection
        finally:
            transaction.rollback()


def timed(connection, statement, params=None):
    """Run `statement` once to warm the pages it reads, then time the second run against the budget."""
    connection.execute(statement, params or {}).all()
    started = time.perf_counter()
    rows = connection.execute(statement, params or {}).mappings().all()
    elapsed = time.perf_counter() - started
    assert elapsed < LATENCY_BUDGET_S, f"took {elapsed:.3f}s"
    return rows


@pytest.fixture(scope="module")
def date_from(engine):
    with engine.connect() as connection:
        latest = connection.execute(
            text("SELECT max(dateoftransfer) FROM production.london_houses")
        ).scalar_one()
    return date(latest.year - 3, 1, 1)


def test_data_window(readonly):
    [row] = timed(readonly, queries.DATA_WINDOW_SQL)
    assert set(row) == {"latest_sale", "total_sales", "first_sale"}
    assert row["first_sale"] < row["latest_sale"]
    assert row["total_sales"] > 1_000_000


def _members_and_codes(codes):
    members, member_codes = [], []
    for code in codes:
        for member in district_members(code):
            members.append(member)
            member_codes.append(code)
    return members, member_codes


@pytest.fixture(scope="module")
def figures_year(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT extract(YEAR FROM max(dateoftransfer))::int FROM production.london_houses")
        ).scalar_one()


def _readonly_rows(engine, statement, params=None):
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return timed(connection, statement, params)
        finally:
            transaction.rollback()


@pytest.fixture(scope="module")
def district_rows(engine, date_from):
    members, codes = _members_and_codes(DISTRICTS)
    rows = _readonly_rows(
        engine,
        queries.DISTRICT_STATS_SQL,
        {"date_from": date_from, "members": members, "codes": codes,
         "min_floor_area": MIN_FLOOR_AREA, "max_floor_area": MAX_FLOOR_AREA},
    )
    return {row["code"]: row for row in rows}


@pytest.fixture(scope="module")
def year_rows(engine, figures_year):
    members, codes = _members_and_codes(DISTRICTS)
    rows = _readonly_rows(
        engine, queries.AREA_YEAR_STATS_SQL, {"year": figures_year, "members": members, "codes": codes}
    )
    return {row["code"]: dict(row) for row in rows}


@pytest.fixture(scope="module")
def rents(engine):
    rows = _readonly_rows(engine, queries.LONDON_RENTS_SQL)
    assert rows, "production.london_rents is empty (migration 0026)"
    return {row["area_name"]: borough_rent_from_row(dict(row)) for row in rows}


def _stats(code, district_rows, year_rows, figures_year, rents):
    return area_stats_from_rows(dict(district_rows[code]), year_rows.get(code), figures_year, rents)


def test_district_stats_cover_every_area(district_rows, year_rows, figures_year, rents):
    assert sorted(district_rows) == sorted(DISTRICTS)
    assert len(district_rows) == 132
    covered = 0
    for code, row in district_rows.items():
        stats = _stats(code, district_rows, year_rows, figures_year, rents)
        year_sales = (year_rows.get(code) or {}).get("sales_count") or 0
        if row["sales_count"] < MIN_COVERED_SALES or not year_sales:
            assert stats is None, code
            continue
        covered += 1
        assert stats.area.borough
        assert stats.area.average_price > 0
        assert stats.area.figures_year == figures_year
        if stats.area.borough == "City of London":
            assert stats.area.typical_monthly_rent is None, code
            assert stats.area.bedroom_rents is None, code
        else:
            assert stats.area.typical_monthly_rent > 0, f"{code}: {stats.area.borough}"
            assert stats.area.rent_month == date(2026, 8, 1), code
            assert None not in stats.area.bedroom_rents.model_dump().values(), code
        assert stats.area.transport_score is None or 1 <= stats.area.transport_score <= 5
        assert stats.area.low_data == (year_sales < LOW_DATA_SALES)
        assert stats.centre is not None
    assert covered >= 130


def test_london_rents_serve_every_borough_the_areas_name(district_rows, rents):
    """Every borough an area maps to has a rent: ONS's names equal ``local_authority``."""
    assert len(rents) == 32
    assert "City of London" not in rents
    boroughs = {row["local_authority"] for row in district_rows.values() if row["local_authority"]}
    missing = sorted(boroughs - set(rents) - {"City of London"})
    assert not missing, f"boroughs without a rent (name mismatch?): {missing}"
    wandsworth = rents["Wandsworth"]
    assert (wandsworth.month, wandsworth.rent) == (date(2026, 8, 1), 2632)
    assert wandsworth.by_bedrooms == {1: 1938, 2: 2463, 3: 2810, 4: 3818}


def test_london_rents_sql_is_fast(readonly, rents):
    rows = timed(readonly, queries.LONDON_RENTS_SQL)
    assert len(rows) == 32


def test_cpih_serves_every_month_from_1989_and_matches_its_rates(readonly):
    """0032's table reads as the /inflation series; a whole-years ratio is the product of the rates."""
    from hpldn.backend.api.market.inflation import CPIH_SQL, series_from_rows

    rows = timed(readonly, CPIH_SQL)
    series = series_from_rows([dict(row) for row in rows])
    assert series is not None and len(series.months) == 452
    assert (series.months[0].month, series.latest_month, series.latest_annual_rate) == (
        date(1989, 1, 1), date(2026, 8, 1), 3.3,
    )
    index = {m.month: m.index for m in series.months}
    rates = {row["month"]: float(row["annual_rate"]) for row in rows}
    chained = 1.0
    for year in range(2018, 2027):
        chained *= 1 + rates[date(year, 8, 1)] / 100
    assert index[date(2026, 8, 1)] / index[date(2017, 8, 1)] == pytest.approx(chained, rel=1e-5)


def test_no_area_invents_a_figure_for_missing_data(district_rows, year_rows, figures_year, rents):
    """MAJ-7: a one-minute walk or a zero movement must come from data, not a fallback."""
    for code, row in district_rows.items():
        stats = _stats(code, district_rows, year_rows, figures_year, rents)
        if stats is None:
            continue
        year_row = year_rows[code]
        if row["station_distance_km"] is None:
            assert stats.area.walk_to_station_mins is None, code
        if not year_row["sales_count_previous"]:
            assert stats.area.movement1y is None, code
        if not year_row["sales_count_5y_ago"]:
            assert stats.area.movement5y is None, code
        if year_row["imd_pct_london"] is None:
            assert stats.area.area_profile_percent is None, code


def test_well_sold_areas_have_every_figure(district_rows):
    """Check every well-sold area has every window figure."""
    for code, row in district_rows.items():
        if row["sales_count"] >= 250:
            nulls = [key for key, value in row.items() if value is None]
            assert nulls == [], f"{code} has null {nulls}"


# The brief's own SQL, independent of AREA_YEAR_STATS_SQL, with its area filter.
_BRIEF_SQL = """
    SELECT avg(price) AS average_price,
           avg(total_floor_area) AS average_floor_area,
           avg(iod_imd_pct_london) AS imd
      FROM production.london_houses
     WHERE EXTRACT(YEAR FROM dateoftransfer::date) = :year AND {where}
"""


@pytest.mark.parametrize(
    "code,where",
    [("SW11", "postcode_district = 'SW11'"), ("KT", "SUBSTRING(postcode_district, 1, 2) = 'KT'"),
     ("SW1", "postcode_district ~ '^SW1[A-Z]?$'")],
)
@pytest.mark.parametrize("year_offset", [0, 1, 5])
def test_year_figures_match_the_briefs_sql(readonly, year_rows, figures_year, code, where, year_offset):
    """Check each area's means equal a plain AVG over its sales in that calendar year."""
    row = readonly.execute(
        text(_BRIEF_SQL.format(where=where)), {"year": figures_year - year_offset}
    ).mappings().one()
    served = year_rows[code]
    key = {0: "average_price", 1: "average_price_previous", 5: "average_price_5y_ago"}[year_offset]
    assert float(served[key]) == pytest.approx(float(row["average_price"]))
    if year_offset == 0:
        assert float(served["average_floor_area"]) == pytest.approx(float(row["average_floor_area"]))
        assert float(served["imd_pct_london"]) == pytest.approx(float(row["imd"]))


# Each area's raw per-year aggregates, straight from london_houses with the
# brief's own filter: the expectation the served figures are checked against.
_AREA_YEARS_SQL = """
    SELECT EXTRACT(YEAR FROM dateoftransfer::date)::int AS year,
           count(*) AS sales,
           avg(price) AS price,
           avg(total_floor_area) AS floor_area,
           avg(iod_imd_pct_london) AS imd
      FROM production.london_houses
     WHERE EXTRACT(YEAR FROM dateoftransfer::date) IN (:year, :year - 1, :year - 5) AND {where}
     GROUP BY 1
"""


def _expected_area(readonly, where, year):
    """The figures an area's raw per-year aggregates give."""
    rows = {
        r["year"]: r
        for r in readonly.execute(text(_AREA_YEARS_SQL.format(where=where)), {"year": year}).mappings()
    }
    now = rows[year]

    def movement(earlier):
        before = rows.get(earlier)
        if before is None or not before["price"]:
            return None
        return round((float(now["price"]) / float(before["price"]) - 1) * 100, 1)

    return {
        "average_price": int(round(float(now["price"]))),
        "average_price_per_sqm": int(round(float(now["price"]) / float(now["floor_area"]))),
        "movement1y": movement(year - 1),
        "movement5y": movement(year - 5),
        "area_profile_percent": round_significant(float(now["imd"]) * 100),
        "sales_count": int(now["sales"]),
    }


@pytest.mark.parametrize(
    "code,where",
    [("SW11", "postcode_district = 'SW11'"), ("KT", "SUBSTRING(postcode_district, 1, 2) = 'KT'"),
     ("E20", "postcode_district = 'E20'")],
)
def test_sw11_and_kingston_serve_the_reports_figures(readonly, district_rows, year_rows, figures_year, rents, code, where):
    """
    SW11 and Kingston serve the figures their raw per-year aggregates give.

    The expectation is computed from ``production.london_houses`` for the
    data's own latest year, so the test holds as the data moves on.
    """
    expected = _expected_area(readonly, where, figures_year)
    area = _stats(code, district_rows, year_rows, figures_year, rents).area
    served = {key: getattr(area, key) for key in expected}
    assert served == expected


def test_the_latest_mortgage_rates_are_read(readonly):
    [row] = timed(readonly, queries.MORTGAGE_RATES_SQL)
    assert row["rate_date"] >= date(2026, 1, 1)
    assert 0 < row["rate_2y_60ltv"] <= row["rate_2y_95ltv"] < 15


def _sales_count(readonly, date_from, pattern):
    return readonly.execute(
        text(
            "SELECT count(*) FROM production.london_houses "
            "WHERE postcode_district ~ :pattern AND dateoftransfer >= :date_from"
        ),
        {"pattern": pattern, "date_from": date_from},
    ).scalar_one()


def test_an_area_counts_its_sub_districts_and_outward_codes(readonly, date_from, district_rows):
    assert district_rows["SW1"]["sales_count"] == _sales_count(readonly, date_from, "^SW1[A-Z]?$")
    assert district_rows["SW1"]["sales_count"] == _sales_count(readonly, date_from, "^SW1[A-Z]$")
    assert district_rows["BR"]["sales_count"] == _sales_count(readonly, date_from, "^BR[1-8]$")
    assert district_rows["SW11"]["sales_count"] == _sales_count(readonly, date_from, "^SW11$")


def _comparable_params(members, date_from, property_type, floor_area, origin, area, limit=40):
    return {
        "members": members, "date_from": date_from, "min_floor_area": MIN_FLOOR_AREA,
        "max_floor_area": MAX_FLOOR_AREA, "property_type": property_type,
        "type_group": type_group(property_type), "floor_area_sqm": floor_area,
        "origin_lat": origin[0], "origin_lon": origin[1],
        "max_distance_km": comparable_radius_km(area), "limit": limit,
        "min_price": MIN_PLAUSIBLE_PRICE, "max_price_per_sqm": MAX_PRICE_PER_SQM,
    }


PECKHAM = (51.4700, -0.0690)


def test_comparables_prefer_the_same_type_and_size(readonly, date_from):
    rows = timed(
        readonly,
        queries.COMPARABLES_SQL,
        _comparable_params(district_members("SE15"), date_from, "terraced", 92, PECKHAM, "SE15"),
    )
    assert len(rows) == 40
    assert set(rows[0]) == {"ppd_id", "price", "dateoftransfer", "total_floor_area",
                            "poststreet", "latitude", "longitude", "type_key",
                            "same_type", "distance_km"}
    assert all(row["type_key"] == "terraced" for row in rows)
    gaps = [abs(row["total_floor_area"] - 92) for row in rows]
    assert gaps == sorted(gaps)
    picked = comparables_from_rows([dict(r) for r in rows], "SE15", PECKHAM, limit=3)
    assert len(picked) == 3
    assert len({comp.street.lower() for comp in picked}) == 3


@pytest.mark.parametrize(
    "area,members,origin,property_type,floor_area",
    [
        ("BR", ["BR8"], (51.4000, 0.1700), "flat", 40),
        ("RM", ["RM14"], (51.5580, 0.2600), "detached", 150),
        ("EC3", district_members("EC3"), (51.5120, -0.0800), "detached", 900),
        ("E20", district_members("E20"), (51.5430, -0.0090), "maisonette", 70),
    ],
)
def test_comparables_are_within_the_cap_and_same_group(
    readonly, date_from, area, members, origin, property_type, floor_area
):
    """BR8/RM14/EC3/E20 requests: nothing far away or of the wrong group."""
    rows = timed(
        readonly,
        queries.COMPARABLES_SQL,
        _comparable_params(members, date_from, property_type, floor_area, origin, area),
    )
    cap = comparable_radius_km(area)
    for row in rows:
        assert haversine_km(origin[0], origin[1], row["latitude"], row["longitude"]) <= cap * 1.01
        assert type_group(row["type_key"]) == type_group(property_type)
        assert row["same_type"] == (row["type_key"] == property_type)
    for comp in comparables_from_rows([dict(r) for r in rows], area, origin, limit=3):
        assert comp.distance_km <= cap
        assert comp.match_type == ("same" if comp.property_type == property_type else "broader")


def _evidence(readonly, members, date_from, property_type, floor_area):
    [row] = timed(
        readonly,
        queries.EVIDENCE_COUNTS_SQL,
        {"members": members, "date_from": date_from, "property_type": property_type,
         "floor_area_low": floor_area * 0.8, "floor_area_high": floor_area * 1.2},
    )
    return row


def test_evidence_counts(readonly, date_from):
    row = _evidence(readonly, district_members("SW11"), date_from, "flat", 72)
    assert 0 < row["similar_count"] < row["sales_count"]


def test_evidence_scopes_run_as_members_arrays(readonly, date_from):
    """Check SW1V draws on all of SW1 and BR3 on BR3 alone."""
    def similar(members):
        return _evidence(readonly, members, date_from, "terraced", 92)["similar_count"]

    br3, br = similar(evidence_members("BR3", "BR")), similar(district_members("BR"))
    assert 0 < br3 < br
    assert similar(evidence_members("SW1V", "SW1")) == similar(district_members("SW1"))
    br3_sales = _evidence(readonly, ["BR3"], date_from, "terraced", 92)["sales_count"]
    br_sales = _evidence(readonly, district_members("BR"), date_from, "terraced", 92)["sales_count"]
    assert 0 < br3_sales < br_sales


def test_outward_centroid(readonly):
    [row] = timed(readonly, queries.OUTWARD_CENTROID_SQL, {"outward": "BR3"})
    assert 51.35 < row["latitude"] < 51.45
    assert -0.1 < row["longitude"] < 0.1
    [missing] = timed(readonly, queries.OUTWARD_CENTROID_SQL, {"outward": "ZZ9"})
    assert missing["latitude"] is None


def test_postcode_centroid(readonly):
    [row] = timed(readonly, queries.POSTCODE_CENTROID_SQL, {"postcode": "SW11 2AB"})
    assert 51.3 < row["latitude"] < 51.7
    assert -0.6 < row["longitude"] < 0.4
    [missing] = timed(readonly, queries.POSTCODE_CENTROID_SQL, {"postcode": "ZZ9 9ZZ"})
    assert missing["latitude"] is None


def test_addresses_list_the_priceable_homes_of_a_postcode(readonly):
    """``GET /addresses``' SQL: one option per home, only homes with a sized, rated certificate."""
    from hpldn.backend.api.market.pricing import ADDRESSES_SQL, collapse_addresses

    started = time.perf_counter()
    rows = [dict(row) for row in readonly.execute(ADDRESSES_SQL, {"postcode": "CR0 1JN"}).mappings()]
    assert time.perf_counter() - started < LATENCY_BUDGET_S
    assert rows and all(row["postcode"] == "CR0 1JN" for row in rows)
    options = collapse_addresses(rows)
    assert ("18", "BARCLAY ROAD") in {(o.postnumber, o.poststreet) for o in options}
    assert len({(o.unit, o.postnumber, o.poststreet) for o in options}) == len(options)


def test_schools_sql_lists_the_nearest_rated_schools_in_force(readonly):
    """``POST /schools``' SQL: up to three per phase, nearest first, rated today, within 3 km, quickly."""
    from hpldn.backend.api.market.schools import SCHOOLS_PER_PHASE, _SCHOOLS_SQL, _school
    from hpldn.backend.scripts.ofsted_features import PRIMARY_PHASES, SEARCH_RADIUS_M, SECONDARY_PHASES

    rows = timed(
        readonly,
        _SCHOOLS_SQL,
        {
            # 8 Maskelyne Close, SW11 4AA.
            "lat": 51.4795, "lon": -0.1659, "today": date(2026, 10, 2), "radius_m": SEARCH_RADIUS_M,
            "primary_phases": list(PRIMARY_PHASES), "secondary_phases": list(SECONDARY_PHASES),
            "per_phase": SCHOOLS_PER_PHASE,
        },
    )
    by_list = {"primary": [], "secondary": []}
    for row in rows:
        by_list[row["listed_as"]].append(row)
    for listed, phases in (("primary", PRIMARY_PHASES), ("secondary", SECONDARY_PHASES)):
        schools = by_list[listed]
        assert len(schools) == SCHOOLS_PER_PHASE
        distances = [float(s["distance_m"]) for s in schools]
        assert distances == sorted(distances) and distances[-1] <= SEARCH_RADIUS_M
        assert all(s["phase"] in phases for s in schools)
        for school in schools:
            served = _school(school)
            assert 1 <= served.rating_score <= 4 and served.rating_label
            assert served.published_on <= date(2026, 10, 2)
            assert served.report_url.endswith(f"/{served.urn}")
