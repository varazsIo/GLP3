"""
The pricing routes over HTTP, with a fake model and address lookup.

Covers authentication, validation, the mapping of pricing errors to 404, 422
and 503 with the landlord's sentence, the rounding of the response, the
folding of duplicate register rows into one home, and the camelCase contract
``frontend/src/data/types.ts`` reads. No model file or PostgreSQL is needed;
the real model is exercised in ``tests/ml/test_hpp_inference.py``.
"""

import re
from datetime import date
from pathlib import Path

import pytest

from hpldn.backend.api.main import app
from hpldn.backend.api.market import pricing
from hpldn.backend.api.routers import pricing as pricing_router
from hpldn.backend.api.schemas import AddressOption
from hpldn.backend.scripts.hpp_inference import (
    InvalidSaleDate,
    NotEnoughData,
    PricePrediction,
    PropertyAddress,
    PropertyNotFound,
)

TYPES_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "data" / "types.ts"

ADDRESS_OPTION_KEYS = {"postnumber", "poststreet", "posttown", "postcode", "unit"}
PRICE_REQUEST_KEYS = ADDRESS_OPTION_KEYS | {"saleDate"}
LAST_SALE_KEYS = {"price", "soldOn"}
PRICE_RESULT_KEYS = {
    "address", "saleDate", "value", "low", "high", "propertyType", "floorAreaSqm",
    "energyRating", "habitableRooms", "lastSale", "evidenceTo", "withinTenPercent",
    "epcSource", "estimatedFrom", "projectionUplift", "projectedFrom", "indexArea",
    "anchoredOnLastSale", "estimatedEnergyRating",
}

BODY = {
    "postnumber": " 18 ",
    "poststreet": "BARCLAY ROAD",
    "posttown": "CROYDON",
    "postcode": "cr01jn",
    "saleDate": "2026-09-15",
}


def _prediction(address: PropertyAddress, sale_date: date, **overrides) -> PricePrediction:
    values = dict(
        address=PropertyAddress("18", "BARCLAY ROAD", "CROYDON", address.postcode),
        sale_date=sale_date,
        evidence_date=date(2026, 8, 1),
        evidence_to=date(2026, 7, 31),
        value=243_749.33,
        low=188_512.7,
        high=276_581.2,
        propertytype="F",
        is_maisonette=False,
        floor_area_sqm=45.0,
        energy_rating="C",
        habitable_rooms=2.0,
        last_sale_date=date(2026, 6, 18),
        last_sale_price=260_000.0,
        basis="own_sale",
        model_kind="catboost",
        within_10_percent=0.4867,
    )
    values.update(overrides)
    return PricePrediction(**values)


class FakeService:
    def __init__(self, error: Exception | None = None, **overrides):
        self.calls: list[tuple[PropertyAddress, date]] = []
        self.error = error
        self.overrides = overrides

    def price(self, address, sale_date, today=None):
        self.calls.append((address, sale_date))
        if self.error is not None:
            raise self.error
        return _prediction(address, sale_date, **self.overrides)


class FakePricer:
    def __init__(self, service):
        self._service = service

    def service(self):
        if isinstance(self._service, Exception):
            raise RuntimeError("model pricing unavailable")
        return self._service


@pytest.fixture
def service():
    fake = FakeService()
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(fake)
    yield fake
    app.dependency_overrides.pop(pricing.get_pricer, None)


def test_pricing_routes_need_the_api_key(client, service):
    assert client.post("/price", json=BODY).status_code == 401
    assert client.get("/addresses?postcode=CR0%201JN").status_code == 401
    assert client.post("/schools", json={k: v for k, v in BODY.items() if k != "saleDate"}).status_code == 401
    assert service.calls == []


def test_price_returns_the_rounded_price_and_what_it_rests_on(client, auth, service):
    response = client.post("/price", json=BODY, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == PRICE_RESULT_KEYS
    assert set(body["address"]) == ADDRESS_OPTION_KEYS
    assert set(body["lastSale"]) == LAST_SALE_KEYS
    # The price to the pound (it moves every day), the range to the £1,000.
    assert (body["value"], body["low"], body["high"]) == (243_749, 189_000, 277_000)
    assert body["propertyType"] == "flat"
    assert body["saleDate"] == "2026-09-15"
    assert body["evidenceTo"] == "2026-07-31"
    assert body["lastSale"] == {"price": 260_000, "soldOn": "2026-06-18"}
    assert (body["floorAreaSqm"], body["energyRating"], body["habitableRooms"]) == (45, "C", 2.0)
    assert (body["epcSource"], body["estimatedFrom"]) == ("certificate", None)
    assert body["estimatedEnergyRating"] is None
    assert body["anchoredOnLastSale"] is False

    address, sale_date = service.calls[0]
    assert address == PropertyAddress("18", "BARCLAY ROAD", "CROYDON", "CR0 1JN")
    assert sale_date == date(2026, 9, 15)


def test_price_names_a_maisonette_and_leaves_unmeasured_figures_null(client, auth):
    fake = FakeService(is_maisonette=True, low=None, high=None, within_10_percent=None, last_sale_date=None)
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(fake)
    try:
        body = client.post("/price", json=BODY, headers=auth).json()
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert body["propertyType"] == "maisonette"
    assert body["low"] is None and body["high"] is None
    assert body["withinTenPercent"] is None and body["lastSale"] is None


def test_price_says_when_it_rests_mainly_on_the_homes_recent_sale(client, auth):
    fake = FakeService(anchored_on_last_sale=True)
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(fake)
    try:
        body = client.post("/price", json=BODY, headers=auth).json()
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert body["anchoredOnLastSale"] is True
    assert body["lastSale"] == {"price": 260_000, "soldOn": "2026-06-18"}


def test_price_serialises_a_home_priced_from_an_estimated_certificate(client, auth):
    fake = FakeService(
        epc_source="estimated",
        estimate_level="street",
        estimate_neighbours=6,
        estimated_from="6 detached houses on this street",
        propertytype="D",
        floor_area_sqm=331.4,
        energy_rating="E",
        habitable_rooms=9.0,
        low=3_900_000.0,
        high=5_600_000.0,
    )
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(fake)
    try:
        body = client.post("/price", json=BODY, headers=auth).json()
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert set(body) == PRICE_RESULT_KEYS
    assert body["epcSource"] == "estimated"
    assert body["estimatedFrom"] == "6 detached houses on this street"
    assert (body["floorAreaSqm"], body["habitableRooms"]) == (331, 9.0)
    assert body["energyRating"] is None  # a neighbourhood's modal rating is not this home's
    assert body["estimatedEnergyRating"] == "E"  # ...but the detailed analysis shows it as an estimate


def test_price_allows_an_estimated_home_with_no_floor_area(client, auth):
    fake = FakeService(epc_source="estimated", estimate_level="none", floor_area_sqm=None,
                       energy_rating=None, habitable_rooms=None)
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(fake)
    try:
        body = client.post("/price", json=BODY, headers=auth).json()
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert body["floorAreaSqm"] is None and body["habitableRooms"] is None
    assert body["epcSource"] == "estimated" and body["estimatedFrom"] is None


@pytest.mark.parametrize(
    "error,status,detail",
    [
        (PropertyNotFound("We could not find 999, <script>BARCLAY ROAD in the property register."), 404, pricing_router.NOT_FOUND),
        (InvalidSaleDate("Choose a sale date from 2026-09-15 onwards: we can only price a future sale."), 422, pricing_router.BAD_SALE_DATE),
        (NotEnoughData("We have no location for this home: there are no recorded sales in or near its postcode to compare it with, so we cannot price it."), 422, pricing_router.NOT_ENOUGH_DATA),
    ],
)
def test_price_answers_a_fixed_sentence_for_each_pricing_error(client, auth, error, status, detail):
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(FakeService(error=error))
    try:
        response = client.post("/price", json=BODY, headers=auth)
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert response.status_code == status
    # Never the exception's text, which can carry the address as typed.
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("pricer", [None, FakePricer(RuntimeError())])
def test_price_is_unavailable_without_a_loaded_model(client, auth, pricer):
    app.dependency_overrides[pricing.get_pricer] = lambda: pricer
    try:
        response = client.post("/price", json=BODY, headers=auth)
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert response.status_code == 503
    assert response.json() == {"detail": pricing.UNAVAILABLE}


@pytest.mark.parametrize(
    "override",
    [
        {"postcode": "CR0"},
        {"postcode": "not a postcode"},
        {"postnumber": ""},
        {"posttown": ""},
        {"saleDate": "15/09/2026"},
    ],
)
def test_price_rejects_malformed_input_before_pricing(client, auth, service, override):
    response = client.post("/price", json=dict(BODY, **override), headers=auth)
    assert response.status_code == 422
    assert service.calls == []


def test_addresses_lists_the_homes_for_a_full_postcode(client, auth, monkeypatch):
    seen = []

    async def fetch_addresses(session, postcode):
        seen.append(postcode)
        return [AddressOption(postnumber="18", poststreet="BARCLAY ROAD", posttown="CROYDON", postcode=postcode)]

    monkeypatch.setattr(pricing, "fetch_addresses", fetch_addresses)
    body = client.get("/addresses?postcode=cr0%201jn", headers=auth).json()
    assert seen == ["CR0 1JN"]
    assert body == [
        {"postnumber": "18", "poststreet": "BARCLAY ROAD", "posttown": "CROYDON", "postcode": "CR0 1JN", "unit": None}
    ]

    assert client.get("/addresses?postcode=CR0%20", headers=auth).status_code == 422


def test_addresses_is_a_404_when_nothing_can_be_priced(client, auth, monkeypatch):
    async def fetch_addresses(session, postcode):
        return []

    monkeypatch.setattr(pricing, "fetch_addresses", fetch_addresses)
    response = client.get("/addresses?postcode=ZZ1%201ZZ", headers=auth)
    assert response.status_code == 404
    assert response.json() == {"detail": "We could not find any homes we can price in ZZ1 1ZZ"}


def test_collapse_addresses_folds_the_blank_street_row_into_the_named_home():
    rows = [
        {"postnumber": "18", "poststreet": None, "posttown": "CROYDON", "postcode": "CR0 1JN", "priceable": True},
        {"postnumber": "18", "poststreet": "BARCLAY ROAD", "posttown": "CROYDON", "postcode": "CR0 1JN", "priceable": False},
        {"postnumber": "2", "poststreet": "BARCLAY ROAD", "posttown": "CROYDON", "postcode": "CR0 1JN", "priceable": True},
        {"postnumber": "16", "poststreet": None, "posttown": "CROYDON", "postcode": "CR0 1JN", "priceable": True},
        {"postnumber": "20", "poststreet": "BARCLAY ROAD", "posttown": "CROYDON", "postcode": "CR0 1JN", "priceable": False},
        {"postnumber": "  ", "poststreet": "BARCLAY ROAD", "posttown": "CROYDON", "postcode": "CR0 1JN", "priceable": True},
    ]
    options = pricing.collapse_addresses(rows)
    assert [(o.postnumber, o.poststreet) for o in options] == [("16", None), ("2", "BARCLAY ROAD"), ("18", "BARCLAY ROAD")]


def test_collapse_addresses_lists_each_flat_of_a_building():
    """Since 0031 homes are keyed on the unit too: "Flat 6" and "Apartment 6" are one flat, flat 7 another."""
    base = {"postnumber": "17", "poststreet": "WOODVILLE ROAD", "posttown": "LONDON", "postcode": "NW11 9TP"}
    rows = [
        {**base, "unit": "FLAT 6", "priceable": True},
        {**base, "unit": "Apartment 6", "priceable": False},
        {**base, "unit": "FLAT 7", "priceable": True},
        {**base, "unit": None, "priceable": True},
        {**base, "poststreet": None, "unit": "FLAT 7", "priceable": True},
    ]
    options = pricing.collapse_addresses(rows)
    assert [(o.unit, o.poststreet) for o in options] == [
        (None, "WOODVILLE ROAD"), ("FLAT 6", "WOODVILLE ROAD"), ("FLAT 7", "WOODVILLE ROAD"),
    ]


@pytest.fixture
def model_dir(monkeypatch, tmp_path):
    """A repository root at `tmp_path` with an empty ``tmp/models`` and no ``staging.yaml``."""
    monkeypatch.delenv(pricing.MODEL_PATH_ENV, raising=False)
    monkeypatch.setenv("HPLDN_DIR_FP", str(tmp_path))
    monkeypatch.setenv("HPLDN_CONFIG_FP", str(tmp_path / "configuration" / "staging.yaml"))
    models = tmp_path / "tmp" / "models"
    models.mkdir(parents=True)
    pricing.get_pricer.cache_clear()
    yield models
    pricing.get_pricer.cache_clear()


def _bundle_dir(path: Path) -> Path:
    """A directory `resolve_model_path` takes for a bundle (it is verified only when loaded)."""
    path.mkdir()
    (path / "manifest.json").write_text("{}")
    return path


def _write_yaml(root: Path, body: str) -> None:
    (root / "configuration").mkdir(exist_ok=True)
    (root / "configuration" / "staging.yaml").write_text(body)


def test_the_model_path_environment_variable_wins(monkeypatch, model_dir):
    chosen = _bundle_dir(model_dir / "elsewhere")
    _bundle_dir(model_dir / "model")
    monkeypatch.setenv(pricing.MODEL_PATH_ENV, str(chosen))
    assert pricing.resolve_model_path() == chosen
    pricer = pricing.get_pricer()
    assert (pricer.path, pricer.kind) == (chosen, "catboost")


def test_the_model_path_comes_from_the_staging_yaml_key(model_dir):
    root = model_dir.parents[1]
    chosen = _bundle_dir(model_dir / "configured")
    _bundle_dir(model_dir / "model")
    _write_yaml(root, "models:\n  catboost_bundle: \"${HPLDN_DIR_FP}/tmp/models/configured\"\n")
    assert pricing.configured_model_path() == chosen
    assert pricing.resolve_model_path() == chosen

    _write_yaml(root, "models:\n  catboost_bundle: tmp/models/configured\n")
    assert pricing.resolve_model_path() == chosen  # relative to the repository root


def test_the_model_path_defaults_to_the_repository_without_staging_yaml(model_dir):
    assert pricing.configured_model_path() == model_dir / "model"
    chosen = _bundle_dir(model_dir / "model")
    assert pricing.resolve_model_path() == chosen


def test_a_missing_configured_bundle_is_not_replaced_by_another_model(model_dir, caplog):
    root = model_dir.parents[1]
    _write_yaml(root, "models:\n  catboost_bundle: tmp/models/model\n")
    _bundle_dir(model_dir / "other")
    (model_dir / "model.cbm").write_bytes(b"CBM1")
    Path(f"{model_dir / 'model.cbm'}.meta.joblib").write_bytes(b"pickle")
    (model_dir / "model.rfm").write_bytes(b"pickle")
    assert pricing.resolve_model_path() is None
    (model_dir / "model").mkdir()  # a directory without a manifest is not a bundle
    assert pricing.resolve_model_path() is None
    with caplog.at_level("WARNING", logger="uvicorn.error"):
        assert pricing.get_pricer() is None
    assert "no model bundle" in caplog.text


def test_no_model_bundle_answers_503(client, auth, model_dir):
    assert pricing.get_pricer() is None
    pricing.get_pricer.cache_clear()
    response = client.post("/price", json=BODY, headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": pricing.UNAVAILABLE}


def test_a_bundle_that_fails_verification_answers_503(client, auth, model_dir, model_signing_keys, caplog):
    """The pricer refuses a tampered bundle at warm-up and the routes answer 503."""
    from hpldn.backend.tests._bundles import tiny_bundle

    bundle = tiny_bundle(model_dir / "model", model_signing_keys)
    with (bundle / "model.cbm").open("ab") as handle:
        handle.write(b"trailing bytes")
    pricer = pricing.ModelPricer(bundle)
    with caplog.at_level("ERROR", logger="uvicorn.error"):
        with pytest.raises(RuntimeError, match="model pricing unavailable"):
            pricer.service()
    assert "files: model.cbm is" in caplog.text
    app.dependency_overrides[pricing.get_pricer] = lambda: pricer
    try:
        response = client.post("/price", json=BODY, headers=auth)
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert response.status_code == 503


def test_saved_model_reads_rfm_as_a_random_forest_only_when_trusted(monkeypatch, tmp_path):
    from hpldn.backend.scripts import hpp_inference, hpp_rf

    calls = []

    def fake_load(path, trust_pickle=False):
        calls.append(trust_pickle)
        return {"model": object(), "population": "all"}

    monkeypatch.setattr(hpp_rf, "load_model", fake_load)
    model = hpp_inference.SavedModel(tmp_path / "model.rfm", trust_pickle=True)
    assert model.kind == "random_forest"
    assert model.population == "all"
    assert calls == [True]


def _interface_keys(source: str, name: str) -> set[str]:
    block = re.search(r"export interface " + name + r" \{(.*?)\n\}", source, re.S)
    assert block, f"interface {name} not found in types.ts"
    return set(re.findall(r"^\s+(\w+)\??:", block.group(1), re.M))


def test_the_pricing_key_snapshots_match_the_frontend_types():
    source = TYPES_TS.read_text(encoding="utf-8")
    assert _interface_keys(source, "AddressOption") == ADDRESS_OPTION_KEYS
    assert _interface_keys(source, "PriceRequest") == PRICE_REQUEST_KEYS
    assert _interface_keys(source, "LastSale") == LAST_SALE_KEYS
    assert _interface_keys(source, "PriceResult") == PRICE_RESULT_KEYS


# --- throttling and blank address fields --------------------------------------


def test_one_client_pricing_in_a_loop_is_turned_away(client, auth):
    """Past the burst, POST /price answers 429 with Retry-After."""
    app.dependency_overrides[pricing.get_pricer] = lambda: FakePricer(FakeService())
    try:
        statuses = [client.post("/price", json=BODY, headers=auth) for _ in range(10)]
    finally:
        app.dependency_overrides.pop(pricing.get_pricer, None)
    assert statuses[-1].status_code == 429
    assert statuses[-1].headers["Retry-After"]
    assert all(r.status_code != 429 for r in statuses[:6])


@pytest.mark.parametrize("field", ["postnumber", "posttown"])
def test_a_blank_address_field_is_a_422(client, auth, field):
    """Whitespace is stripped before the length check."""
    body = dict(BODY, **{field: "   "})
    response = client.post("/price", json=body, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


def test_the_pricing_cap_turns_requests_away_when_full():
    import asyncio

    from fastapi import HTTPException

    from hpldn.backend.api.ratelimit import ConcurrencyCap

    async def run():
        cap = ConcurrencyCap(limit=1)
        async with cap.acquire():
            with pytest.raises(HTTPException) as exc:
                async with cap.acquire():
                    pass
            assert exc.value.status_code == 503
        async with cap.acquire():  # released again
            pass
        return cap.rejected

    assert asyncio.run(run()) == 1


# --- nearby sales -------------------------------------------------------------

NEARBY_SALE_KEYS = {
    "address", "propertyType", "price", "soldOn", "distanceM", "compMedianPrice",
    "compMedianPricePerSqm", "compTxnCount", "localTypeMedian",
}
NEARBY_SALES_KEYS = {"sales", "windowFrom", "windowTo"}


def _nearby_sales():
    from hpldn.backend.api.schemas import NearbySale, NearbySales

    sale = NearbySale(
        address=AddressOption(postnumber="4", poststreet="LANGFORD MEWS", posttown="LONDON", postcode="SW11 1AE"),
        property_type="flat",
        price=375_000,
        sold_on=date(2026, 5, 1),
        distance_m=82,
        comp_median_price=736_101,
        comp_median_price_per_sqm=9_231,
        comp_txn_count=5,
        local_type_median=510_000,
    )
    return NearbySales(sales=[sale], window_from=date(2025, 7, 31), window_to=date(2026, 7, 31))


def test_nearby_lists_the_sales_near_a_home_in_camel_case(client, auth, monkeypatch):
    from hpldn.backend.api.market import nearby

    seen = []

    async def fetch_nearby(session, home):
        seen.append(home)
        return _nearby_sales()

    monkeypatch.setattr(nearby, "fetch_nearby", fetch_nearby)
    body = {key: value for key, value in BODY.items() if key != "saleDate"}
    response = client.post("/nearby", json=body, headers=auth)
    assert response.status_code == 200
    assert seen[0].postcode == "CR0 1JN" and seen[0].postnumber == "18"
    payload = response.json()
    assert set(payload) == NEARBY_SALES_KEYS
    assert set(payload["sales"][0]) == NEARBY_SALE_KEYS
    assert payload["sales"][0]["compMedianPrice"] == 736_101
    assert payload["windowTo"] == "2026-07-31"


def test_nearby_is_a_404_for_a_home_not_in_the_register(client, auth, monkeypatch):
    from hpldn.backend.api.market import nearby

    async def fetch_nearby(session, home):
        raise nearby.HomeNotFound("We could not find this home in the property register.")

    monkeypatch.setattr(nearby, "fetch_nearby", fetch_nearby)
    body = {key: value for key, value in BODY.items() if key != "saleDate"}
    response = client.post("/nearby", json=body, headers=auth)
    assert response.status_code == 404
    assert response.json() == {"detail": pricing_router.NOT_FOUND}
    assert client.post("/nearby", json={**body, "postcode": "CR0"}, headers=auth).status_code == 422


def test_nearby_matches_the_home_as_pricing_does():
    from hpldn.backend.api.market.nearby import match_rows

    rows = [
        {"id": 1, "postnumber": "18", "poststreet": "BARCLAY ROAD"},
        {"id": 2, "postnumber": "18", "poststreet": None},
        {"id": 3, "postnumber": "18", "poststreet": "OTHER ROAD"},
        {"id": 4, "postnumber": "20", "poststreet": "BARCLAY ROAD"},
    ]
    assert [row["id"] for row in match_rows(rows, " 18 ", "Barclay Road")] == [1, 2]
    assert [row["id"] for row in match_rows(rows, "18", "Unknown Street")] == [2]
    assert match_rows(rows, "99", None) == []

    flats = [
        {"id": 5, "postnumber": "17", "poststreet": "WOODVILLE ROAD", "unit": "FLAT 6"},
        {"id": 6, "postnumber": "17", "poststreet": "WOODVILLE ROAD", "unit": "FLAT 7"},
    ]
    assert [row["id"] for row in match_rows(flats, "17", "WOODVILLE ROAD", "Apartment 6")] == [5]
    assert [row["id"] for row in match_rows(flats, "17", "WOODVILLE ROAD")] == [5, 6]


def test_the_nearby_key_snapshots_match_the_frontend_types():
    source = TYPES_TS.read_text(encoding="utf-8")
    assert _interface_keys(source, "NearbySale") == NEARBY_SALE_KEYS
    assert _interface_keys(source, "NearbySales") == NEARBY_SALES_KEYS



# --- nearby schools -----------------------------------------------------------

NEARBY_SCHOOL_KEYS = {
    "urn", "name", "phase", "distanceM", "ratingLabel", "ratingScore", "gradeBasis",
    "inspectedOn", "publishedOn", "inheritedFromUrn", "reportUrl",
}
NEARBY_SCHOOLS_KEYS = {"primary", "secondary", "asOf", "radiusM", "source"}


def _nearby_schools():
    from hpldn.backend.api.schemas import NearbySchool, NearbySchools

    school = NearbySchool(
        urn=131520,
        name="Sacred Heart Catholic Primary School, Battersea",
        phase="primary",
        distance_m=795,
        rating_label="Good",
        rating_score=2.0,
        grade_basis="overall",
        inspected_on=date(2023, 6, 20),
        published_on=date(2023, 7, 20),
        inherited_from_urn=None,
        report_url="https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/131520",
    )
    return NearbySchools(
        primary=[school], secondary=[], as_of=date(2026, 10, 2), radius_m=3000, source="Ofsted; DfE GIAS (OGL v3.0)"
    )


def test_schools_lists_the_schools_near_a_home_in_camel_case(client, auth, monkeypatch):
    from hpldn.backend.api.market import schools

    seen = []

    async def fetch_schools(session, home, today):
        seen.append((home, today))
        return _nearby_schools()

    monkeypatch.setattr(schools, "fetch_schools", fetch_schools)
    body = {key: value for key, value in BODY.items() if key != "saleDate"}
    response = client.post("/schools", json=body, headers=auth)
    assert response.status_code == 200
    assert seen[0][0].postcode == "CR0 1JN" and seen[0][0].postnumber == "18"
    payload = response.json()
    assert set(payload) == NEARBY_SCHOOLS_KEYS
    assert set(payload["primary"][0]) == NEARBY_SCHOOL_KEYS
    assert payload["primary"][0]["gradeBasis"] == "overall"
    assert payload["radiusM"] == 3000 and payload["secondary"] == []


def test_schools_is_a_404_for_a_home_not_in_the_register_and_a_503_without_the_database(client, auth, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    from hpldn.backend.api.market import nearby, schools

    body = {key: value for key, value in BODY.items() if key != "saleDate"}

    async def not_found(session, home, today):
        raise nearby.HomeNotFound("We could not find this home in the property register.")

    monkeypatch.setattr(schools, "fetch_schools", not_found)
    response = client.post("/schools", json=body, headers=auth)
    assert response.status_code == 404
    assert response.json() == {"detail": pricing_router.NOT_FOUND}
    assert client.post("/schools", json={**body, "postcode": "CR0"}, headers=auth).status_code == 422

    async def down(session, home, today):
        raise DBAPIError("SELECT", {}, Exception("relation production.ofsted_rating_spells does not exist"))

    monkeypatch.setattr(schools, "fetch_schools", down)
    assert client.post("/schools", json=body, headers=auth).status_code == 503


def test_the_schools_key_snapshots_match_the_frontend_types():
    source = TYPES_TS.read_text(encoding="utf-8")
    assert _interface_keys(source, "NearbySchool") == NEARBY_SCHOOL_KEYS
    assert _interface_keys(source, "NearbySchools") == NEARBY_SCHOOLS_KEYS


@pytest.mark.parametrize(
    ("body", "echoed"),
    [
        (dict(BODY, postcode="<script>alert(1)</script>"), "<script>alert(1)</script>"),
        (dict(BODY, saleDate="'; DROP TABLE x;--"), "DROP TABLE"),
        (dict(BODY, postnumber="x" * 101), "x" * 101),
    ],
    ids=["html postcode", "sql date", "long number"],
)
def test_a_422_names_the_field_but_never_echoes_the_input(client, auth, body, echoed):
    """FastAPI's own 422 repeats `input` and `ctx`; ours keeps type, loc and msg only."""
    response = client.post("/price", json=body, headers=auth)
    assert response.status_code == 422
    assert echoed not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}
