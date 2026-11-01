"""
The market routes over HTTP, with in-memory repository fakes.

Covers routing, authentication, validation, the cache and the camelCase
contract ``frontend/src/data/types.ts`` reads, without PostgreSQL; the SQL
itself is tested in ``test_market_postgres.py``.
"""

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest

from hpldn.backend.api.main import app
from hpldn.backend.api.market import cache as market_cache
from hpldn.backend.api.market import repository
from hpldn.backend.api.market.districts import district_members
from hpldn.backend.api.market.repository import DistrictStats
from hpldn.backend.api.routers import market as market_router
from hpldn.backend.api.schemas import (
    AreaStats,
    BedroomRents,
    Comparable,
    DataWindow,
    InflationMonth,
    InflationSeries,
    MortgageRates,
)

WINDOW = DataWindow(
    from_year=2023, to_year=2026, latest_sale=date(2026, 7, 31),
    total_sales=1_923_529, first_sale_year=1995,
)

AREAS = {
    "SW11": AreaStats(
        code="SW11", name="Battersea, Clapham Junction", borough="Wandsworth", average_price=610_000,
        average_price_per_sqm=9_230, estimator_price_per_sqm=9_230, movement1y=-6.9, movement5y=3.1, transport_score=3,
        walk_to_station_mins=9, rental_yield=4.4, typical_monthly_rent=2632, rent_month=date(2026, 8, 1),
        bedroom_rents=BedroomRents(one_bed=1938, two_bed=2463, three_bed=2810, four_plus_bed=3818),
        sales_count=3537, figures_year=2026, area_profile_percent=43.0, epc_band="C",
    ),
    "SE15": AreaStats(
        code="SE15", name="Nunhead, Peckham", borough="Southwark", average_price=560_000,
        average_price_per_sqm=7_450, estimator_price_per_sqm=7_450, movement1y=2.4, movement5y=6.0, transport_score=4,
        walk_to_station_mins=7, rental_yield=4.4, typical_monthly_rent=2050,
        sales_count=1801, figures_year=2026, area_profile_percent=71.0, epc_band="D",
    ),
    "SW1": AreaStats(
        code="SW1", name="Belgravia, Pimlico, Westminster", borough="Westminster",
        average_price=1_150_000, average_price_per_sqm=15_200, estimator_price_per_sqm=15_200, movement1y=-2.0, movement5y=1.0,
        transport_score=5, walk_to_station_mins=5, rental_yield=3.3, typical_monthly_rent=3175,
        sales_count=1050, figures_year=2026, area_profile_percent=30.0, epc_band="D",
    ),
    "BR": AreaStats(
        code="BR", name="Bromley", borough="Bromley", average_price=520_000,
        average_price_per_sqm=5_900, estimator_price_per_sqm=5_900, movement1y=0.5, movement5y=8.0, transport_score=3,
        walk_to_station_mins=11, rental_yield=3.9, typical_monthly_rent=1675,
        sales_count=9393, figures_year=2026, area_profile_percent=35.0, epc_band="D",
    ),
}

COMPARABLES = [
    Comparable(id="7846670", street="Lavender Hill", district="SW11", property_type="flat",
               floor_area_sqm=72, price=470_000, sold_on=date(2026, 6, 30), distance_km=1.0),
    Comparable(id="7855464", street="Circus Road West", district="SW11", property_type="flat",
               floor_area_sqm=70, price=365_000, sold_on=date(2025, 11, 20), distance_km=3.1),
    Comparable(id="7714126", street="Battersea Square", district="SW11", property_type="flat",
               floor_area_sqm=75, price=500_000, sold_on=date(2025, 8, 6), distance_km=1.5),
]

# The frontend's interfaces, field for field. A rename on either side fails
# here before it fails in a browser.
DATA_WINDOW_KEYS = {"fromYear", "toYear", "latestSale", "totalSales", "firstSaleYear"}
AREA_STATS_KEYS = {
    "code", "name", "borough", "averagePrice", "averagePricePerSqm", "movement1y", "movement5y",
    "transportScore", "walkToStationMins", "rentalYield", "typicalMonthlyRent", "rentMonth",
    "bedroomRents", "salesCount", "figuresYear", "areaProfilePercent", "epcBand", "lowData",
}
BEDROOM_RENTS_KEYS = {"oneBed", "twoBed", "threeBed", "fourPlusBed"}
MORTGAGE_RATES_KEYS = {"rateDate", "ltv60", "ltv75", "ltv85", "ltv90", "ltv95"}

RATES = MortgageRates(rate_date=date(2026, 7, 31), ltv60=4.61, ltv75=4.79, ltv85=4.89,
                      ltv90=5.07, ltv95=5.49)
INFLATION = InflationSeries(
    source="ONS CPIH annual rate, all items (L55O), rebased chained index 2015=100",
    latest_month=date(2026, 8, 1),
    latest_annual_rate=3.3,
    months=[
        InflationMonth(month=date(2017, 8, 1), index=104.1933),
        InflationMonth(month=date(2026, 8, 1), index=143.7596),
    ],
)
INFLATION_SERIES_KEYS = {"source", "latestMonth", "latestAnnualRate", "months"}
INFLATION_MONTH_KEYS = {"month", "index"}

COMPARABLE_KEYS = {
    "id", "street", "district", "propertyType", "floorAreaSqm", "price", "soldOn", "distanceKm",
    "matchType", "adjustedPrice",
}
DRIVER_KEYS = {"id", "label", "detail", "tone"}
ESTIMATE_REQUEST_KEYS = {"district", "postcode", "propertyType", "bedrooms", "floorAreaSqm", "saleDate"}
ESTIMATE_RESULT_KEYS = {
    "request", "value", "low", "high", "confidence", "spreadPercent", "drivers",
    "comparables", "suggestedMonthlyRent", "window", "basis", "projectionUplift", "projectedFrom",
    "indexArea",
}

TYPES_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "data" / "types.ts"

VALID_BODY = {"postcode": "sw11 2ab", "propertyType": "flat", "bedrooms": 2, "floorAreaSqm": 72}


class Calls:
    def __init__(self):
        self.window = 0
        self.stats = 0
        self.comparables: list[dict] = []
        self.similar: list[dict] = []
        self.centroids: list[str] = []
        self.outward_centroids: list[str] = []
        self.inflation: InflationSeries | None = INFLATION


@pytest.fixture
def calls(monkeypatch):
    """Fakes for every repository function, recording how they were called."""
    record = Calls()

    async def fetch_window(session):
        record.window += 1
        return WINDOW

    async def fetch_district_stats(session, window, codes=None):
        record.stats += 1
        return {code: DistrictStats(area=area, centre=(51.46, -0.17)) for code, area in AREAS.items()}

    async def fetch_comparables(session, **kwargs):
        record.comparables.append(kwargs)
        return COMPARABLES[: kwargs.get("limit", 3)]

    async def fetch_evidence_counts(session, **kwargs):
        record.similar.append(kwargs)
        return repository.EvidenceCounts(sales_count=3_000, similar_count=300)

    async def fetch_postcode_centroid(session, postcode):
        record.centroids.append(postcode)
        return None if postcode.startswith("BR9") else (51.4613, -0.1753)

    async def fetch_outward_centroid(session, outward):
        record.outward_centroids.append(outward)
        return None if outward == "BR9" else (51.40, 0.02)

    async def fetch_mortgage_rates(session):
        return RATES

    async def fetch_inflation(session):
        return record.inflation

    monkeypatch.setattr(repository, "fetch_window", fetch_window)
    monkeypatch.setattr(repository, "fetch_district_stats", fetch_district_stats)
    monkeypatch.setattr(repository, "fetch_mortgage_rates", fetch_mortgage_rates)
    monkeypatch.setattr(repository, "fetch_comparables", fetch_comparables)
    monkeypatch.setattr(repository, "fetch_evidence_counts", fetch_evidence_counts)
    monkeypatch.setattr(repository, "fetch_postcode_centroid", fetch_postcode_centroid)
    monkeypatch.setattr(repository, "fetch_outward_centroid", fetch_outward_centroid)
    monkeypatch.setattr(market_cache, "fetch_inflation", fetch_inflation)

    fresh = market_cache.MarketCache(ttl_s=3600)
    app.dependency_overrides[market_cache.get_market_cache] = lambda: fresh
    yield record
    app.dependency_overrides.pop(market_cache.get_market_cache, None)


MARKET_ROUTES = [
    ("get", "/areas", None),
    ("get", "/areas/SW11", None),
    ("get", "/areas/SW11/comparables?propertyType=flat&floorAreaSqm=72", None),
    ("post", "/estimate", VALID_BODY),
    ("get", "/inflation", None),
]


@pytest.mark.parametrize("method,route,body", MARKET_ROUTES)
def test_market_routes_need_the_api_key(client, calls, method, route, body):
    response = client.request(method.upper(), route, json=body)
    assert response.status_code == 401
    wrong = client.request(method.upper(), route, json=body, headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401
    assert calls.window == 0


def test_areas_returns_the_window_and_every_district(client, auth, calls):
    body = client.get("/areas", headers=auth).json()
    assert set(body) == {"window", "areas", "mortgageRates"}
    assert set(body["window"]) == DATA_WINDOW_KEYS
    assert body["window"]["latestSale"] == "2026-07-31"
    assert [area["code"] for area in body["areas"]] == ["SW11", "SE15", "SW1", "BR"]
    for area in body["areas"]:
        assert set(area) == AREA_STATS_KEYS
    sw11 = body["areas"][0]
    assert (sw11["typicalMonthlyRent"], sw11["rentMonth"]) == (2632, "2026-08-01")
    assert sw11["bedroomRents"] == {"oneBed": 1938, "twoBed": 2463, "threeBed": 2810, "fourPlusBed": 3818}
    assert body["areas"][1]["bedroomRents"] is None
    assert set(body["mortgageRates"]) == MORTGAGE_RATES_KEYS
    assert body["mortgageRates"]["ltv75"] == 4.79


def test_the_cache_loads_once_across_two_areas_calls(client, auth, calls):
    assert client.get("/areas", headers=auth).status_code == 200
    assert client.get("/areas", headers=auth).status_code == 200
    assert (calls.window, calls.stats) == (1, 1)


def test_the_cache_reloads_once_stale(monkeypatch):
    now = [0.0]
    loads = []

    async def fake_window(session):
        loads.append(now[0])
        return WINDOW

    async def fake_stats(session, window, codes=None):
        return {}

    async def fake_rates(session):
        return None

    monkeypatch.setattr(repository, "fetch_window", fake_window)
    monkeypatch.setattr(repository, "fetch_district_stats", fake_stats)
    monkeypatch.setattr(repository, "fetch_mortgage_rates", fake_rates)
    monkeypatch.setattr(market_cache, "fetch_inflation", fake_rates)
    cache = market_cache.MarketCache(ttl_s=10, clock=lambda: now[0])
    for tick in (0.0, 9.9, 10.0):
        now[0] = tick
        asyncio.run(cache.get(None))
    assert loads == [0.0, 10.0]


class _NoSession:
    """A stand-in session factory: the repository fakes ignore the session."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _fake_market(monkeypatch, loads, fail=None):
    async def fake_window(session):
        if fail and fail[0]:
            raise RuntimeError("database gone")
        loads.append(session)
        return WINDOW

    async def fake_stats(session, window, codes=None):
        return {}

    async def fake_none(session):
        return None

    monkeypatch.setattr(repository, "fetch_window", fake_window)
    monkeypatch.setattr(repository, "fetch_district_stats", fake_stats)
    monkeypatch.setattr(repository, "fetch_mortgage_rates", fake_none)
    monkeypatch.setattr(market_cache, "fetch_inflation", fake_none)


def test_a_warmed_cache_serves_without_loading(monkeypatch):
    loads = []
    _fake_market(monkeypatch, loads)
    cache = market_cache.MarketCache(ttl_s=3600)

    async def run():
        await cache.warm(_NoSession())
        await cache.get(None)
        await cache.get(None)

    asyncio.run(run())
    assert len(loads) == 1


def test_a_stale_snapshot_is_served_while_it_reloads_in_the_background(monkeypatch):
    now = [0.0]
    loads = []
    _fake_market(monkeypatch, loads)
    cache = market_cache.MarketCache(ttl_s=10, clock=lambda: now[0])

    async def run():
        await cache.warm(_NoSession())
        first = await cache.get(None)
        now[0] = 11.0
        stale = await cache.get(None)
        assert stale is first  # answered at once, not after a load
        assert len(loads) == 1
        await asyncio.sleep(0)  # let the background reload run
        await cache._refreshing
        assert len(loads) == 2
        assert await cache.get(None) is not first

    asyncio.run(run())


def test_a_failed_reload_keeps_the_previous_snapshot(monkeypatch):
    now = [0.0]
    loads = []
    fail = [False]
    _fake_market(monkeypatch, loads, fail)
    cache = market_cache.MarketCache(ttl_s=10, clock=lambda: now[0])

    async def run():
        await cache.warm(_NoSession())
        first = await cache.get(None)
        fail[0] = True
        now[0] = 11.0
        assert await cache.get(None) is first
        await cache._refreshing
        assert await cache.get(None) is first

    asyncio.run(run())


def test_a_failed_warm_leaves_the_first_request_to_load(monkeypatch):
    loads = []
    fail = [True]
    _fake_market(monkeypatch, loads, fail)
    cache = market_cache.MarketCache(ttl_s=3600)

    async def run():
        await cache.warm(_NoSession())
        assert not cache.loaded
        fail[0] = False
        await cache.get(None)
        assert cache.loaded

    asyncio.run(run())


@pytest.mark.parametrize("route", ["/areas", "/inflation"])
def test_public_figures_carry_an_etag_and_answer_304_when_unchanged(client, auth, calls, route):
    first = client.get(route, headers=auth)
    assert first.status_code == 200
    assert first.headers["cache-control"] == market_router.PUBLIC_CACHE_CONTROL
    etag = first.headers["etag"]
    again = client.get(route, headers={**auth, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    other = client.get(route, headers={**auth, "If-None-Match": '"something-else"'})
    assert other.status_code == 200 and other.json() == first.json()


def test_inflation_returns_the_cpih_series(client, auth, calls):
    body = client.get("/inflation", headers=auth).json()
    assert set(body) == INFLATION_SERIES_KEYS
    assert (body["latestMonth"], body["latestAnnualRate"]) == ("2026-08-01", 3.3)
    assert body["months"] == [
        {"month": "2017-08-01", "index": 104.1933},
        {"month": "2026-08-01", "index": 143.7596},
    ]


def test_inflation_is_unavailable_before_its_table_is_loaded(client, auth, calls):
    calls.inflation = None
    response = client.get("/inflation", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "Inflation figures are not available right now. Try again later."}
    # The rest of the snapshot is unaffected.
    assert client.get("/areas", headers=auth).status_code == 200


def test_an_inflation_series_maps_from_cpih_rows():
    from hpldn.backend.api.market.inflation import series_from_rows

    assert series_from_rows([]) is None
    series = series_from_rows(
        [
            {"month": date(2026, 7, 1), "annual_rate": 3.1, "price_index": 143.4, "source": "ONS"},
            {"month": date(2026, 8, 1), "annual_rate": 3.3, "price_index": 143.7596, "source": "ONS"},
        ]
    )
    assert (series.latest_month, series.latest_annual_rate, series.source) == (date(2026, 8, 1), 3.3, "ONS")
    assert [m.index for m in series.months] == [143.4, 143.7596]


def test_one_area_by_code_case_insensitively(client, auth, calls):
    body = client.get("/areas/se15", headers=auth).json()
    assert body["code"] == "SE15"
    assert set(body) == AREA_STATS_KEYS


@pytest.mark.parametrize("code", ["sw1v", "SW1", "SW1A"])
def test_a_sub_district_resolves_to_its_district(client, auth, calls, code):
    body = client.get(f"/areas/{code}", headers=auth).json()
    assert body["code"] == "SW1"


def test_an_outer_outward_code_resolves_to_its_postcode_area(client, auth, calls):
    assert client.get("/areas/br3", headers=auth).json()["code"] == "BR"


def test_an_uncovered_district_is_a_404_in_plain_words(client, auth, calls):
    response = client.get("/areas/ZZ9", headers=auth)
    assert response.status_code == 404
    assert response.json() == {"detail": market_router.NOT_COVERED}


def test_comparables_use_the_postcode_centre_when_given(client, auth, calls):
    response = client.get(
        "/areas/SW11/comparables",
        params={"propertyType": "flat", "floorAreaSqm": 72, "postcode": "sw11 2ab", "limit": 2},
        headers=auth,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    for comp in body:
        assert set(comp) == COMPARABLE_KEYS
    assert calls.centroids == ["SW11 2AB"]
    assert calls.comparables[0]["origin"] == (51.4613, -0.1753)
    assert calls.comparables[0]["limit"] == 2
    assert calls.comparables[0]["members"] == ["SW11"]


def test_comparables_for_a_bare_area_code_use_the_whole_area(client, auth, calls):
    params = {"propertyType": "terraced", "floorAreaSqm": 92}
    client.get("/areas/BR/comparables", params=params, headers=auth)
    assert calls.comparables[0]["district"] == "BR"
    assert calls.comparables[0]["members"] == district_members("BR")


def test_comparables_with_an_outer_postcode_stay_in_its_outward_code(client, auth, calls):
    params = {"propertyType": "terraced", "floorAreaSqm": 92, "postcode": "BR3 1AA"}
    client.get("/areas/BR/comparables", params=params, headers=auth)
    assert calls.comparables[0]["members"] == ["BR3"]


def test_comparables_fall_back_to_the_district_centre(client, auth, calls):
    client.get(
        "/areas/SW11/comparables",
        params={"propertyType": "flat", "floorAreaSqm": 72, "postcode": "SW11"},
        headers=auth,
    )
    assert calls.centroids == []
    assert calls.comparables[0]["origin"] == (51.46, -0.17)


def test_comparables_measure_from_the_outward_code_before_the_area(client, auth, calls):
    """BR3 is measured from BR3, not from the middle of all of Bromley."""
    client.post("/estimate", json=dict(VALID_BODY, postcode="BR3 9ZZ"), headers=auth)
    assert calls.outward_centroids == []
    assert calls.comparables[0]["origin"] == (51.4613, -0.1753)

    calls.comparables.clear()
    client.post("/estimate", json=dict(VALID_BODY, postcode="BR9 1AA"), headers=auth)
    assert calls.outward_centroids == ["BR9"]
    assert calls.comparables[0]["origin"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"propertyType": "castle", "floorAreaSqm": 72},
        {"propertyType": "flat", "floorAreaSqm": 4},
        {"propertyType": "flat", "floorAreaSqm": 1001},
        {"propertyType": "flat"},
        {"propertyType": "flat", "floorAreaSqm": 72, "limit": 0},
        {"propertyType": "flat", "floorAreaSqm": 72, "postcode": "banana"},
    ],
)
def test_comparables_validate_their_query(client, auth, calls, params):
    response = client.get("/areas/SW11/comparables", params=params, headers=auth)
    assert response.status_code == 422


def test_estimate_returns_the_full_contract(client, auth, calls):
    """Check the full response contract, and that the estimate used the live similar count and postcode centre."""
    response = client.post("/estimate", json=VALID_BODY, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ESTIMATE_RESULT_KEYS
    assert set(body["request"]) == ESTIMATE_REQUEST_KEYS
    assert set(body["window"]) == DATA_WINDOW_KEYS
    assert body["request"] == {
        "postcode": "SW11 2AB", "district": "SW11", "propertyType": "flat",
        # This body sends no date, so the value is for today.
        "bedrooms": 2, "floorAreaSqm": 72, "saleDate": None,
    }
    for driver in body["drivers"]:
        assert set(driver) == DRIVER_KEYS
    for comp in body["comparables"]:
        assert set(comp) == COMPARABLE_KEYS
    assert [d["id"] for d in body["drivers"]] == ["comparables", "momentum", "transport", "size"]
    assert len(body["comparables"]) == 3
    assert body["low"] < body["value"] < body["high"]
    # The fake SW11 sales (~£470k) sit a third below the area formula (~£625k),
    # so the estimate blends towards them, drops a level and its range reaches them.
    assert body["confidence"] == "medium"
    assert body["low"] <= 470_000
    # A 2-bed SW11 home rents at Wandsworth's 2-bed mean, not a yield on the value.
    assert body["suggestedMonthlyRent"] == 2463
    assert calls.similar[0]["members"] == ["SW11"]
    assert calls.comparables[0]["origin"] == (51.4613, -0.1753)


def test_estimate_ignores_a_district_sent_by_the_client(client, auth, calls):
    body = dict(VALID_BODY, district="SE15")
    response = client.post("/estimate", json=body, headers=auth)
    assert response.json()["request"]["district"] == "SW11"


def test_estimate_folds_a_sub_district_into_its_district(client, auth, calls):
    body = dict(VALID_BODY, postcode="SW1V 4AB")
    response = client.post("/estimate", json=body, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["request"]["district"] == "SW1"
    assert calls.similar[0]["members"] == district_members("SW1")
    assert calls.comparables[0]["members"] == district_members("SW1")


def test_estimate_in_outer_london_is_its_postcode_area_on_local_evidence(client, auth, calls):
    body = dict(VALID_BODY, postcode="BR3 1AA")
    response = client.post("/estimate", json=body, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["request"]["district"] == "BR"
    assert calls.similar[0]["members"] == ["BR3"]
    assert calls.comparables[0]["members"] == ["BR3"]
    assert calls.comparables[0]["district"] == "BR"


def test_estimate_in_a_london_postcode_we_do_not_cover_is_a_404(client, auth, calls):
    body = dict(VALID_BODY, postcode="TN16 3AA")
    response = client.post("/estimate", json=body, headers=auth)
    assert response.status_code == 404
    assert response.json() == {"detail": market_router.NOT_COVERED}
    assert calls.similar == []


def test_estimate_on_an_uncovered_district_is_a_404(client, auth, calls):
    body = dict(VALID_BODY, postcode="M1 1AE")
    response = client.post("/estimate", json=body, headers=auth)
    assert response.status_code == 404
    assert response.json() == {"detail": market_router.NOT_COVERED}
    assert calls.comparables == []


@pytest.mark.parametrize(
    "override",
    [
        {"postcode": "banana"},
        {"postcode": "12345"},
        {"propertyType": "castle"},
        {"bedrooms": 0},
        {"bedrooms": 7},
        {"floorAreaSqm": 14},
        {"floorAreaSqm": 1001},
    ],
)
def test_estimate_rejects_malformed_input_with_a_422(client, auth, calls, override):
    response = client.post("/estimate", json=dict(VALID_BODY, **override), headers=auth)
    assert response.status_code == 422
    assert calls.window == 0


def test_estimate_uses_the_injected_estimator(client, auth, calls):
    """Check an injected estimator changes only the figures, never the response shape."""
    from hpldn.backend.api.market.estimator import EstimateCore, get_estimator

    class Fixed:
        def estimate(self, request, area, comparables, similar_count, evidence_sales_count):
            return EstimateCore(value=123_000, low=100_000, high=150_000,
                                confidence="fair", spread_percent=13)

    app.dependency_overrides[get_estimator] = Fixed
    try:
        body = client.post("/estimate", json=VALID_BODY, headers=auth).json()
    finally:
        app.dependency_overrides.pop(get_estimator, None)
    assert (body["value"], body["low"], body["high"]) == (123_000, 100_000, 150_000)
    assert set(body) == ESTIMATE_RESULT_KEYS


def _interface_keys(source: str, name: str) -> set[str]:
    block = re.search(r"export interface " + name + r" \{(.*?)\n\}", source, re.S)
    assert block, f"interface {name} not found in types.ts"
    return set(re.findall(r"^\s+(\w+)\??:", block.group(1), re.M))


def test_the_key_snapshots_match_the_frontend_types():
    source = TYPES_TS.read_text(encoding="utf-8")
    assert _interface_keys(source, "DataWindow") == DATA_WINDOW_KEYS
    assert _interface_keys(source, "AreaStats") == AREA_STATS_KEYS
    assert _interface_keys(source, "BedroomRents") == BEDROOM_RENTS_KEYS
    assert _interface_keys(source, "Comparable") == COMPARABLE_KEYS
    assert _interface_keys(source, "Driver") == DRIVER_KEYS
    assert _interface_keys(source, "EstimateRequest") == ESTIMATE_REQUEST_KEYS
    assert _interface_keys(source, "EstimateResult") == ESTIMATE_RESULT_KEYS
    assert _interface_keys(source, "AreasResponse") == {"window", "areas", "mortgageRates"}
    assert _interface_keys(source, "MortgageRates") == MORTGAGE_RATES_KEYS
    assert _interface_keys(source, "InflationSeries") == INFLATION_SERIES_KEYS
    assert _interface_keys(source, "InflationMonth") == INFLATION_MONTH_KEYS


# --- input hardening -----------------------------------------------------------


def test_comparables_reject_a_postcode_outside_the_area(client, auth, calls):
    """A postcode in another area is a 422, not silently dropped."""
    response = client.get(
        "/areas/SW11/comparables",
        params={"propertyType": "flat", "floorAreaSqm": 70, "postcode": "E1 6AN"},
        headers=auth,
    )
    assert response.status_code == 422
    assert response.json() == {"detail": market_router.NOT_IN_AREA}


@pytest.mark.parametrize("bedrooms,floor_area,status", [(6, 15, 422), (6, 65, 200), (1, 15, 200), (3, 34, 422)])
def test_estimate_needs_room_for_the_bedrooms(client, auth, calls, bedrooms, floor_area, status):
    """6 bedrooms in 15 m² is rejected with a sentence."""
    body = dict(VALID_BODY, bedrooms=bedrooms, floorAreaSqm=floor_area)
    response = client.post("/estimate", json=body, headers=auth)
    assert response.status_code == status, response.text
    if status == 422:
        assert "bedrooms needs at least" in response.text


def test_one_client_estimating_in_a_loop_is_turned_away(client, auth, calls):
    """Past the burst, POST /estimate answers 429."""
    statuses = [client.post("/estimate", json=VALID_BODY, headers=auth).status_code for _ in range(14)]
    assert statuses[:10] == [200] * 10
    assert 429 in statuses[10:]
