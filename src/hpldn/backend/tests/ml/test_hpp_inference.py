"""
Tests for `hpldn.backend.scripts.hpp_inference`.

The sales-history features are checked against the implementations they
mirror: the rolling and same-street statistics against `hpp_rf`'s own pandas
code on `test_hp_rf`'s synthetic dataset (exactly), and the comparables,
neighbourhood and MSOA momentum against the SQL definitions on hand-placed
sales. Saved CatBoost and Random Forest models must price built rows exactly
as their training-time preprocessing does, in the caller's row order. A final
integration test prices a real address on hpdbldn with the pinned model
(`ml/conftest.py`'s `pricing_service`, shared with `test_price_golden`).
"""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.scripts import hpp_cb, hpp_inference, hpp_rf
from hpldn.backend.scripts.hpp_inference import (
    EvidenceStore,
    InvalidSaleDate,
    PropertyAddress,
    PropertyNotFound,
    SavedModel,
    engineer_rows,
    match_address,
    validate_sale_date,
)
from hpldn.backend.tests.ml.test_hp_rf import _make_dataset, _make_mortgage_rates

EVIDENCE_COLUMNS = [
    "ppd_id", "dateoftransfer", "price", "propertytype", "total_floor_area",
    "current_energy_rating", "latitude", "longitude", "msoa_code",
    "postcode_district", "postcode_sector", "poststreet", "epc_matched",
]


def _store(sales: pd.DataFrame, latest_sale=None) -> EvidenceStore:
    """Return an `EvidenceStore` over `sales` with empty reference tables."""
    sales = sales.copy()
    if "epc_matched" not in sales:
        sales["epc_matched"] = True
    ukhpi = pd.DataFrame(
        {
            "period": pd.to_datetime(["2020-01-01"]),
            "published_date": pd.to_datetime(["2020-03-01"]),
            "avg_price_all_property_types": [500_000.0],
            **{col: [400_000.0] for col in hpp_inference.UKHPI_PRICE_COLUMN_BY_TYPE.values()},
        }
    )
    boe = pd.DataFrame({"date_changed": pd.to_datetime(["2020-01-01"]), "rate": [1.0]})
    latest = latest_sale if latest_sale is not None else pd.to_datetime(sales["dateoftransfer"]).max()
    return EvidenceStore(sales[EVIDENCE_COLUMNS], _make_mortgage_rates(), ukhpi, boe, latest)


def test_rolling_and_same_street_features_match_the_training_frame():
    df = _make_dataset(n=900, seed=11, periods_days=1400)
    expected = hpp_rf.add_same_street_features(df)
    expected, _ = hpp_rf.add_rolling_area_comp_features(expected)
    store = _store(df)

    for i in range(len(df)):
        row = df.iloc[i]
        got = store.rolling_comp_features(
            row["dateoftransfer"],
            row["propertytype"],
            {"postcode_sector": row["postcode_sector"], "postcode_district": row["postcode_district"]},
        )
        got.update(
            store.same_street_features(
                row["dateoftransfer"], row["postcode_district"], hpp_rf.normalise_street_key(row["poststreet"])
            )
        )
        for col, value in got.items():
            want = expected.iloc[i][col]
            if pd.isna(want):
                assert np.isnan(value), (i, col)
            else:
                assert value == pytest.approx(float(want), rel=1e-9, abs=1e-9), (i, col)


def _sale(ppd_id, day, price, lat, lon, ptype="F", area=50.0, rating="C", msoa="M1"):
    """Return one evidence sale."""
    return {
        "ppd_id": ppd_id, "dateoftransfer": pd.Timestamp(day), "price": float(price),
        "propertytype": ptype, "total_floor_area": area, "current_energy_rating": rating,
        "latitude": lat, "longitude": lon, "msoa_code": msoa, "postcode_district": "CR0",
        "postcode_sector": "CR0_1", "poststreet": "BARCLAY ROAD", "epc_matched": True,
    }


# About 111 m of latitude per 0.001 degree at London.
LAT0, LON0 = 51.5, -0.1
DEG_PER_100M = 0.0009


def test_comps_take_the_nearest_earlier_sales_of_the_type_within_radius_and_window():
    anchor = pd.Timestamp("2026-08-01")
    sales = pd.DataFrame(
        [_sale(i, "2026-06-01", 100_000 * i, LAT0 + i * DEG_PER_100M, LON0) for i in range(1, 8)]
        + [
            _sale(20, "2026-06-01", 9_000_000, LAT0, LON0, ptype="D"),  # other type
            _sale(21, "2025-06-01", 9_000_000, LAT0, LON0),  # outside the 12-month window
            _sale(22, "2026-08-01", 9_000_000, LAT0, LON0),  # on the anchor day: not earlier
            _sale(23, "2026-06-01", 9_000_000, LAT0 + 30 * DEG_PER_100M, LON0),  # 3.3 km away
            _sale(24, "2026-06-01", 50_000, LAT0 + 8 * DEG_PER_100M, LON0, area=np.nan),
        ]
    )
    comps = _store(sales, latest_sale=anchor).comps_features(anchor, LAT0, LON0, "F")
    assert comps["comp_txn_count"] == 5
    assert comps["comp_median_price"] == 300_000  # the five nearest: 100k..500k
    assert comps["comp_median_price_per_sqm"] == 6_000

    lonely = _store(sales, latest_sale=anchor).comps_features(anchor, LAT0, LON0, "S")
    assert lonely["comp_txn_count"] == 0 and np.isnan(lonely["comp_median_price"])


def test_neighbourhood_features_follow_their_rings_windows_and_null_rules():
    anchor = pd.Timestamp("2026-08-01")
    rows = [
        _sale(1, "2026-07-01", 400_000, LAT0 + 6 * DEG_PER_100M, LON0, area=80, rating="B"),  # ~600 m
        _sale(2, "2026-07-01", 200_000, LAT0 + 9 * DEG_PER_100M, LON0, area=40, rating="D"),  # ~900 m
        _sale(3, "2026-07-01", 300_000, LAT0 + 1 * DEG_PER_100M, LON0, area=60, rating=None),  # ~100 m
        _sale(4, "2025-01-01", 900_000, LAT0 + 7 * DEG_PER_100M, LON0, area=90, rating="A"),  # 19 months ago
    ]
    store = _store(pd.DataFrame(rows), latest_sale=anchor)
    got = store.neighbourhood_features(anchor, LAT0, LON0, "F", 60.0)

    d1, d2 = (hpp_inference._local_metres(LAT0, LON0, np.array([LAT0 + k * DEG_PER_100M]), np.array([LON0]))[0] for k in (6, 9))
    assert got["spatial_lag_price"] == pytest.approx((400_000 / d1 + 200_000 / d2) / (1 / d1 + 1 / d2))
    assert got["proptype_local_median_800m_6m"] == pytest.approx(350_000)  # sales 1 and 3 within 800 m
    # Sales 1 (80 m2) and 3 (60 m2) are within 800 m: one of the two is no larger than 60 m2.
    assert got["floor_area_percentile_local"] == pytest.approx(0.5)
    # Only sale 3 is within 500 m and it has no rating: the shares are NULL.
    assert np.isnan(got["epc_band_share_b"])
    assert got["median_price_per_sqm_20nn_6m"] == pytest.approx(np.median([400_000 / 80, 200_000 / 40]))
    months = np.array([31, 31, 31, 577]) / hpp_inference.DAYS_PER_MONTH
    weights = np.exp(-hpp_inference.RECENCY_DECAY_LAMBDA * months)
    assert got["recency_weighted_price"] == pytest.approx(
        np.sum(np.array([400_000, 200_000, 300_000, 900_000]) * weights) / weights.sum()
    )


def test_floor_area_percentile_counts_neighbours_no_larger():
    anchor = pd.Timestamp("2026-08-01")
    rows = [
        _sale(1, "2026-07-01", 1, LAT0 + DEG_PER_100M, LON0, area=50),
        _sale(2, "2026-07-01", 1, LAT0 + DEG_PER_100M, LON0, area=70),
        _sale(3, "2026-07-01", 1, LAT0 + DEG_PER_100M, LON0, area=90),
    ]
    store = _store(pd.DataFrame(rows), latest_sale=anchor)
    assert store.neighbourhood_features(anchor, LAT0, LON0, "F", 70.0)["floor_area_percentile_local"] == pytest.approx(2 / 3)
    assert np.isnan(store.neighbourhood_features(anchor, LAT0, LON0, "F", np.nan)["floor_area_percentile_local"])


def test_msoa_momentum_compares_the_right_windows():
    anchor = pd.Timestamp("2026-08-01")
    rows = [
        _sale(1, "2026-03-01", 330_000, LAT0, LON0),  # recent
        _sale(2, "2025-03-01", 300_000, LAT0, LON0),  # a year before
        _sale(3, "2021-03-01", 220_000, LAT0, LON0),  # five years before
        _sale(4, "2026-03-01", 999_000, LAT0, LON0, msoa="OTHER"),
    ]
    got = _store(pd.DataFrame(rows), latest_sale=anchor).msoa_trend_features(anchor, "M1")
    assert got["msoa_price_momentum_1y"] == pytest.approx(0.1)
    assert got["msoa_price_momentum_5y"] == pytest.approx(0.5)


def test_the_evidence_date_stops_the_day_after_the_latest_sale():
    store = _store(pd.DataFrame([_sale(1, "2026-07-31", 1, LAT0, LON0)]))
    assert store.evidence_date("2027-01-01") == pd.Timestamp("2026-08-01")
    assert store.evidence_date("2026-05-01") == pd.Timestamp("2026-05-01")


def _candidates():
    return pd.DataFrame(
        {
            "address_id": [1, 2, 3, 4, 5],
            "postnumber": ["18", "18", "18", "Flat 2", "16"],
            "poststreet": ["BARCLAY ROAD", None, "OTHER ROAD", "Barclay Court", None],
            "posttown": ["CROYDON", "CROYDON", "CROYDON", "LONDON", "CROYDON"],
            "postcode": ["CR0 1JN"] * 5,
        }
    )


def test_match_address_joins_the_blank_street_row_and_ignores_case_and_town():
    matched = match_address(_candidates(), PropertyAddress("18", "barclay road", "London", "cr0 1jn"))
    assert sorted(matched["address_id"]) == [1, 2]
    flat = match_address(_candidates(), PropertyAddress("FLAT 2", "BARCLAY COURT", "CROYDON", "CR0 1JN"))
    assert list(flat["address_id"]) == [4]
    blank = match_address(_candidates(), PropertyAddress("16", "SOME ROAD", "CROYDON", "CR0 1JN"))
    assert list(blank["address_id"]) == [5]


def test_match_address_narrows_to_the_unit_when_one_is_given():
    """A unit keeps only that flat's rows ("Apartment 6" is "Flat 6"); without one the building's rows pool."""
    flats = pd.DataFrame(
        {
            "address_id": [1, 2, 3],
            "postnumber": ["17", "17", "17"],
            "poststreet": ["WOODVILLE ROAD"] * 3,
            "posttown": ["LONDON"] * 3,
            "postcode": ["NW11 9TP"] * 3,
            "unit": ["FLAT 6", "FLAT 7", None],
        }
    )
    six = match_address(flats, PropertyAddress("17", "WOODVILLE ROAD", "LONDON", "NW11 9TP", "Apartment 6"))
    assert list(six["address_id"]) == [1]
    everyone = match_address(flats, PropertyAddress("17", "WOODVILLE ROAD", "LONDON", "NW11 9TP"))
    assert list(everyone["address_id"]) == [1, 2, 3]
    assert match_address(flats, PropertyAddress("17", "WOODVILLE ROAD", "LONDON", "NW11 9TP", "FLAT 9")).empty


def test_match_address_asks_for_the_street_when_the_number_is_on_two():
    with pytest.raises(PropertyNotFound, match="more than one street"):
        match_address(_candidates(), PropertyAddress("18", None, "CROYDON", "CR0 1JN"))


def test_sale_dates_must_be_in_the_future_and_within_the_horizon():
    today = date(2026, 9, 14)
    assert validate_sale_date(date(2026, 9, 15), today) == date(2026, 9, 15)
    with pytest.raises(InvalidSaleDate, match="from 2026-09-15 onwards"):
        validate_sale_date(today, today)
    with pytest.raises(InvalidSaleDate, match="no later than"):
        validate_sale_date(today + timedelta(days=hpp_inference.MAX_HORIZON_DAYS + 1), today)


def _view_rows(df: pd.DataFrame) -> list:
    """Return `df`'s rows as built rows carry them: every view column, with the causal stats attached."""
    stats = hpp_rf.add_same_street_features(df)
    stats, _ = hpp_rf.add_rolling_area_comp_features(stats)
    stats = hpp_rf.add_sizeband_comp_features(stats)
    rows = []
    for i in range(len(df)):
        row = {col: np.nan for col in hpp_inference.VIEW_COLUMNS}
        row.update(stats.iloc[i].to_dict())
        rows.append(row)
    return rows


def test_a_saved_catboost_prices_built_rows_as_its_training_preprocessing_does(tmp_path):
    df = _make_dataset(n=400, seed=21)
    model_out = tmp_path / "hpp_cb"
    args = hpp_cb.build_arg_parser().parse_args([])
    for key, value in dict(device="cpu", iterations=30, early_stopping_rounds=10, thread_count=1, model_out=model_out).items():
        setattr(args, key, value)
    mortgage = _make_mortgage_rates()
    hpp_cb.run_training(df, args, mortgage)

    model = SavedModel(model_out)
    assert model.kind == "catboost" and model.population == "epc_matched"
    assert model.train_period is not None
    store = _store(df)
    store.mortgage_rates = mortgage

    model_ready, meta = hpp_cb.load_model(model_out)
    X, _, deflator = hpp_cb.prepare_features_from_saved_model(df, mortgage, meta)
    expected, _ = hpp_cb.predict_with_adjustments(model_ready, X, meta["cat_features"], meta["target"], deflator)

    shuffled = np.random.default_rng(0).permutation(len(df))
    rows = _view_rows(df)
    got = model.predict(engineer_rows([rows[i] for i in shuffled], model.float_dtype), store)
    # prepare_features_from_saved_model returns date order; the synthetic dates are already sorted.
    np.testing.assert_allclose(got, expected[shuffled], rtol=1e-6)


def test_a_saved_random_forest_prices_built_rows_as_its_training_preprocessing_does(tmp_path):
    df = _make_dataset(n=400, seed=22)
    model_out = tmp_path / "hpp_rf.joblib"
    args = hpp_rf.build_arg_parser().parse_args([])
    for key, value in dict(device="cpu", n_estimators=10, n_jobs=1, model_out=model_out).items():
        setattr(args, key, value)
    hpp_rf.run_training(df, args)

    with pytest.raises(PermissionError, match="--trust-pickle"):
        SavedModel(model_out)
    model = SavedModel(model_out, trust_pickle=True)
    assert model.kind == "random_forest"
    assert set(model.meta) >= {"price_stat_globals", "population", "train_period"}
    expected = hpp_rf.predict_with_saved_model(model.meta, df)
    got = model.predict(engineer_rows(_view_rows(df), model.float_dtype), _store(df))
    np.testing.assert_allclose(got, expected, rtol=1e-9)
    assert np.all(got > 0)


def test_prices_a_real_address_with_the_trained_model(pricing_service):
    from sqlalchemy import text

    engine = pricing_service.engine
    with engine.connect() as connection:
        sale = connection.execute(
            text(
                "SELECT a.postnumber, a.poststreet, a.posttown, a.postcode "
                "FROM production.london_houses lh JOIN production.address_table a ON a.id = lh.address_id "
                "WHERE lh.epc_matched AND a.poststreet IS NOT NULL AND lh.postcode_district = 'CR0' "
                "ORDER BY lh.dateoftransfer DESC LIMIT 1"
            )
        ).mappings().one()
    service = pricing_service
    tomorrow = hpp_inference.today_in_london() + timedelta(days=1)
    prediction = service.price(PropertyAddress(**sale), tomorrow)
    assert 10_000 < prediction.value < 50_000_000
    assert prediction.evidence_to == service.store.latest_sale.date()
    later = service.price(PropertyAddress(**sale), tomorrow + timedelta(days=200))
    assert later.sale_date == tomorrow + timedelta(days=200)
    with pytest.raises(InvalidSaleDate):
        service.price(PropertyAddress(**sale), hpp_inference.today_in_london())


# --------------------------------------------------------------------------
# Homes without a certificate
# --------------------------------------------------------------------------


def _certificate(address_id, postnumber, street="BARCLAY ROAD", postcode="CR0 1JN", area=60.0, rooms=3,
                 rating="C", kind="House", build_form="Mid-Terrace", day="2020-01-01", lat=LAT0, lon=LON0,
                 floor_level=None):
    """Return one neighbour certificate as `_NEIGHBOUR_EPC_SQL` returns it."""
    return {
        "address_id": address_id, "postnumber": postnumber, "poststreet": street, "postcode": postcode,
        "construction_age_band": "1900-1929", "number_habitable_rooms": rooms, "current_energy_rating": rating,
        "total_floor_area": area, "build_form": build_form, "mains_gas_flag": "Y", "tenure": "O",
        "multi_glaze_proportion": 100, "extension_count": 1, "floor_level": floor_level, "property_type": kind,
        "inspection_date": pd.Timestamp(day), "latitude": lat, "longitude": lon,
    }


class FakeNeighbours:
    """An in-memory `NeighbourCertificates`."""

    def __init__(self, certificates):
        self.frame = pd.DataFrame(certificates)
        self.calls = []

    def certificates(self, postcodes):
        self.calls.append(tuple(postcodes))
        if self.frame.empty:
            return self.frame
        return self.frame[self.frame["postcode"].isin(list(postcodes))].reset_index(drop=True)

    def postcodes_with_prefix(self, prefix):
        if self.frame.empty:
            return []
        return sorted({p for p in self.frame["postcode"] if p.startswith(prefix)})


def _record(postnumber="17", street="BARCLAY ROAD", postcode="CR0 1JN", sales=None, epcs=None, coordinates=(LAT0, LON0)):
    return hpp_inference.PropertyRecord(
        address=PropertyAddress(postnumber, street, "CROYDON", postcode),
        address_ids=[999],
        sales=sales if sales is not None else pd.DataFrame(),
        epcs=epcs if epcs is not None else pd.DataFrame(),
        coordinates=coordinates,
    )


def test_estimate_epc_takes_the_tightest_pool_with_enough_homes():
    on_street = [_certificate(i, str(i), area=50.0 + i) for i in range(1, 6)]
    elsewhere = [_certificate(100 + i, str(i), street="OTHER ROAD", area=200.0) for i in range(1, 7)]
    flats = [_certificate(200 + i, f"FLAT {i}", kind="Flat", build_form=None) for i in range(1, 9)]
    source = FakeNeighbours(on_street + elsewhere + flats)

    values, estimate = hpp_inference.estimate_epc(_record(), "2026-09-30", None, propertytype="T", source=source)
    assert (estimate.source, estimate.level, estimate.n) == ("estimated", "street", 5)
    assert values["total_floor_area"] == 53.0 and values["number_habitable_rooms"] == 3
    assert values["current_energy_rating"] == "C" and values["build_form"] == "Mid-Terrace"
    assert estimate.description == "5 terraced houses on this street"
    assert estimate.floor_area_iqr == (52.0, 54.0)

    blank_street_duplicate = _certificate(300, "5", street=None, area=51.0, day="2019-01-01")  # OTHER ROAD 5
    source = FakeNeighbours(on_street[:4] + elsewhere + [blank_street_duplicate])
    _, estimate = hpp_inference.estimate_epc(_record(), "2026-09-30", None, propertytype="T", source=source)
    assert (estimate.level, estimate.n) == ("postcode", 10)


def test_estimate_epc_uses_the_building_before_the_street_for_a_flat():
    building = [
        _certificate(i, f"FLAT {i} CRESSALL HOUSE", kind="Flat", build_form=None, area=40.0, floor_level="2nd")
        for i in range(1, 6)
    ]
    street = [_certificate(50 + i, f"FLAT {i}", kind="Flat", build_form=None, area=90.0) for i in range(1, 9)]
    record = _record(postnumber="FLAT 9 CRESSALL HOUSE")
    values, estimate = hpp_inference.estimate_epc(
        record, "2026-09-30", None, propertytype="F", source=FakeNeighbours(building + street)
    )
    assert estimate.level == "building" and estimate.description == "5 flats in this building"
    assert values["total_floor_area"] == 40.0 and values["floor_level"] == "2nd"


def test_estimate_epc_never_estimates_a_home_from_its_own_certificate():
    own = [_certificate(1000 + i, "17", area=500.0, day=f"201{i}-01-01") for i in range(5)]
    others = [_certificate(i, str(i), area=60.0) for i in range(1, 5)]
    _, estimate = hpp_inference.estimate_epc(_record(), "2026-09-30", None, propertytype="T",
                                             source=FakeNeighbours(own + others))
    assert estimate.level != "street" or estimate.n == 4
    assert estimate.n == 4 and estimate.level == "none"


def test_estimate_epc_falls_back_to_sector_then_district_and_then_nothing():
    postcode = [_certificate(i, str(i)) for i in range(1, 4)]
    sector = [_certificate(10 + i, str(i), postcode="CR0 1AB", street="HIGH STREET", area=70.0) for i in range(1, 3)]
    far = [_certificate(20 + i, str(i), postcode="CR0 9ZZ", street="FAR ROAD", lat=LAT0 + 0.1) for i in range(1, 9)]
    near = [_certificate(40 + i, str(i), postcode="CR0 8AA", street="NEAR ROAD", area=80.0) for i in range(1, 6)]

    _, estimate = hpp_inference.estimate_epc(
        _record(), "2026-09-30", None, propertytype="T", source=FakeNeighbours(postcode + sector)
    )
    assert (estimate.level, estimate.n) == ("sector", 5)
    assert estimate.description == "5 terraced houses in the CR0 1 postcode sector"

    values, estimate = hpp_inference.estimate_epc(
        _record(), "2026-09-30", None, propertytype="T", source=FakeNeighbours(postcode + far + near)
    )
    # The 11 km-away homes are outside COMPS_RADIUS_M; the postcode's own 3 are inside.
    assert (estimate.level, estimate.n) == ("district", 8)
    assert values["total_floor_area"] == 80.0

    values, estimate = hpp_inference.estimate_epc(
        _record(), "2026-09-30", None, propertytype="T", source=FakeNeighbours(postcode + far)
    )
    assert (estimate.level, estimate.n) == ("none", 3)
    assert values.isna().all()


def test_the_property_type_of_a_home_without_a_sale_comes_from_its_address_then_the_postcode():
    flat = hpp_inference._ppd_type_from_address
    assert flat(PropertyAddress("FLAT 2", "BARCLAY ROAD", "CROYDON", "CR0 1JN")) == "F"
    assert flat(PropertyAddress("301", "LUMIERE APARTMENTS", "LONDON", "SW11 1AD")) == "F"
    assert flat(PropertyAddress("12A ROSE COURT", "HIGH STREET", "LONDON", "SW11 1AD")) == "F"
    assert flat(PropertyAddress("12A", "HIGH STREET", "LONDON", "SW11 1AD")) is None
    assert flat(PropertyAddress("17", "LICHFIELD ROAD", "RICHMOND", "TW9 3JR")) is None
    assert hpp_inference._building_token("FLAT 3, CRESSALL HOUSE") == "CRESSALL HOUSE"

    record = _record(postnumber="12A")
    record.postcode_modal_type = "S"
    assert hpp_inference.resolve_property_type(record) == "S"
    record.epcs = pd.DataFrame([dict(_certificate(1, "12A", kind="House", build_form="Detached"), id=1)])
    assert hpp_inference.resolve_property_type(record) == "D"


def _own_sale(ptype="D"):
    row = {col: np.nan for col in hpp_inference.VIEW_COLUMNS}
    row.update(
        ppd_id=1, dateoftransfer=pd.Timestamp("2026-07-09"), price=5_188_000.0, propertytype=ptype, duration="F",
        postcode_district="CR0", postcode_sector="CR0_1", poststreet="BARCLAY ROAD", latitude=LAT0,
        longitude=LON0, msoa_code="M1", uprn=1.0, epc_matched=False,
    )
    return pd.DataFrame([row])


def test_build_sale_row_without_a_certificate_prices_from_the_estimate():
    store = _store(pd.DataFrame([_sale(i, "2026-06-01", 500_000, LAT0, LON0, ptype="D") for i in range(1, 6)]))
    neighbours = FakeNeighbours(
        [_certificate(i, str(i), build_form="Detached", area=300.0 + i, rooms=9) for i in range(1, 7)]
    )
    record = _record(sales=_own_sale("D"))
    built = hpp_inference.build_sale_row(record, "2026-09-30", store, epc_source=neighbours)
    assert built.basis == "own_sale"
    assert (built.epc.source, built.epc.level, built.epc.n) == ("estimated", "street", 6)
    assert built.row["total_floor_area"] == 303.5 and built.row["number_habitable_rooms"] == 9
    assert built.row["epc_matched"] is False and built.row["epc_missing"] == 1.0
    assert np.isnan(built.row["epc_age_at_sale_days"]) and built.epc_inspected is None
    assert built.last_sale[1] == 5_188_000.0

    frame = engineer_rows([built.row], "float32")
    assert frame["epc_missing"].iloc[0] == 1.0

    missing = hpp_inference.build_sale_row(record, "2026-09-30", store, epc_source=neighbours, epc_strategy="nan")
    assert missing.epc.level == "none" and np.isnan(missing.row["total_floor_area"])
    # Every text EPC column missing on every row still engineers (calibration's NaN pass).
    engineered = engineer_rows([missing.row, missing.row], "float32")
    assert (engineered["construction_age_band"] == "UNKNOWN").all()

    certified = _record(sales=_own_sale("D"), epcs=pd.DataFrame(
        [dict(_certificate(999, "17", build_form="Detached", area=250.0), id=1, lodgement_datetime=pd.NaT)]
    ))
    built = hpp_inference.build_sale_row(certified, "2026-09-30", store, epc_source=neighbours)
    assert built.epc.source == "certificate" and built.row["total_floor_area"] == 250.0
    assert built.row["epc_matched"] is True and built.row["epc_missing"] == 0.0


def test_build_sale_row_needs_a_location_or_a_property_type():
    store = _store(pd.DataFrame([_sale(1, "2026-06-01", 500_000, LAT0, LON0)]))
    with pytest.raises(hpp_inference.NotEnoughData, match="no location"):
        hpp_inference.build_sale_row(_record(), "2026-09-30", store, epc_source=FakeNeighbours([]))

    record = _record(postnumber="12A")
    record.postcode_template = _own_sale("T").iloc[0]
    with pytest.raises(hpp_inference.NotEnoughData, match="type of home"):
        hpp_inference.build_sale_row(record, "2026-09-30", store, epc_source=FakeNeighbours([]))

    record.postcode_modal_type = "T"
    built = hpp_inference.build_sale_row(record, "2026-09-30", store, epc_source=FakeNeighbours([]))
    assert built.basis == "postcode_sale" and built.row["propertytype"] == "T"
    assert built.epc.level == "none" and np.isnan(built.row["uprn"])


def test_an_estimated_home_takes_its_range_from_the_estimated_calibration():
    calibration = {
        "log_ratio_quantiles": {"p10": -0.1, "p90": 0.1},
        "within_10_percent": 0.5,
        "estimated": {"log_ratio_quantiles": {"p10": -0.3, "p90": 0.3}, "within_10_percent": 0.3, "strategy": "nan"},
    }
    low, high, _, stratum = hpp_inference._range_from_calibration(100.0, calibration, "certificate")
    est_low, est_high, _, _ = hpp_inference._range_from_calibration(100.0, calibration, "estimated")
    assert est_low < low < high < est_high
    # No strata in this calibration, so both fall back to the global block.
    assert stratum == "global"
    assert hpp_inference.estimate_strategy(calibration) == "nan"
    assert hpp_inference.estimate_strategy({"log_ratio_quantiles": {}}) == "surrogate"
    del calibration["estimated"]
    assert hpp_inference._range_from_calibration(100.0, calibration, "estimated")[:2] == (low, high)


def test_a_price_takes_the_narrowest_measured_stratum_and_widens_with_the_horizon():
    """The band follows the home and the horizon, rather than one figure for every price."""
    calibration = {
        "log_ratio_quantiles": {"p10": -0.25, "p50": 0.0, "p90": 0.25},
        "strata": {
            # A tight, well-evidenced mid-price terrace.
            "£300-600k|T|thick": {"n": 500, "log_ratio_quantiles": {"p10": -0.05, "p50": 0.0, "p90": 0.05}},
            # Small flats are much harder, and the band says so.
            "£0-300k": {"n": 500, "log_ratio_quantiles": {"p10": -0.40, "p50": 0.0, "p90": 0.40}},
        },
    }
    tight_low, tight_high, _, tight_key = hpp_inference._range_from_calibration(
        450_000.0, calibration, propertytype="T", street_sales=20
    )
    wide_low, wide_high, _, wide_key = hpp_inference._range_from_calibration(
        200_000.0, calibration, propertytype="F", street_sales=1
    )
    assert tight_key == "£300-600k|T|thick" and wide_key == "£0-300k"
    # Different homes, different bands -- and so different marker positions.
    assert (tight_high - tight_low) / 450_000.0 < (wide_high - wide_low) / 200_000.0

    # A home with no measured stratum falls back to the global block.
    _, _, _, fallback = hpp_inference._range_from_calibration(
        5_000_000.0, calibration, propertytype="D", street_sales=1
    )
    assert fallback == "global"

    # The same price years out carries the projection's error too.
    near_low, near_high, _, _ = hpp_inference._range_from_calibration(
        450_000.0, calibration, propertytype="T", street_sales=20, index_sigma=0.0
    )
    far_low, far_high, _, _ = hpp_inference._range_from_calibration(
        450_000.0, calibration, propertytype="T", street_sales=20, index_sigma=0.15
    )
    assert far_high - far_low > near_high - near_low


def test_the_point_estimate_is_corrected_for_the_models_measured_bias():
    """A standing median bias moves the price, not just the band drawn around it."""
    biased = {"log_ratio_quantiles": {"p10": -0.15, "p50": -0.05, "p90": 0.05}}
    low, high, centre, _ = hpp_inference._range_from_calibration(100_000.0, biased)
    # log_ratio is log(actual / predicted): a negative p50 means the model reads high.
    assert centre == pytest.approx(100_000.0 * math.exp(-0.05))
    assert low < centre < high
    # The band is measured in log space, so it is symmetric there: the
    # arithmetic gap above the centre is the larger of the two.
    assert math.log(centre / low) == pytest.approx(math.log(high / centre))
    assert high - centre > centre - low


# --------------------------------------------------------------------------
# Size-matched comparables and the locally indexed previous sale (plan 2.2-2.4)
# --------------------------------------------------------------------------


def _synthetic_local_index():
    """A `local_index.LocalIndex` whose London series grow at different rates by type, 2000-2020."""
    from hpldn.backend.scripts import local_index

    months = pd.date_range("2000-01-01", "2020-12-01", freq="MS")
    t = np.arange(len(months))
    ukhpi = pd.DataFrame(
        {
            "period": months,
            "avg_price_all_property_types": 300_000 * np.exp(0.003 * t),
            "avg_price_detached": 300_000 * np.exp(0.004 * t),
            "avg_price_semi_detached": 300_000 * np.exp(0.003 * t),
            "avg_price_terraced": 300_000 * np.exp(0.002 * t),
            "avg_price_flats_maisonettes": 300_000 * np.exp(0.001 * t),
        }
    )
    empty = pd.DataFrame(
        columns=["postcode_district", "borough", "propertytype", "prev_date", "dateoftransfer", "prev_price", "price"]
    )
    return local_index.LocalIndex.build(empty, ukhpi)


def _dense_dataset():
    """`_make_dataset` squeezed into a few km so sales have size-matched neighbours within 1-2 km."""
    df = _make_dataset(n=1500, seed=31, start="2016-01-01", periods_days=1000)
    df["latitude"] = 51.5074 + (df["latitude"] - 51.5074) * 0.12
    df["longitude"] = -0.1278 + (df["longitude"] + 0.1278) * 0.12
    return df


@pytest.mark.parametrize("with_index", [False, True])
def test_size_matched_comparables_match_the_training_frame(with_index):
    df = _dense_dataset()
    index = _synthetic_local_index() if with_index else None
    expected = hpp_rf.add_sizeband_comp_features(df, index)
    store = _store(df)
    store.local_index = index
    got = np.array(
        [
            store.sizeband_comp_features(
                row["dateoftransfer"], row["latitude"], row["longitude"], row["propertytype"],
                row["total_floor_area"], row["postcode_district"], "all",
            )["comp_sizeband_pps"]
            for _, row in df.iterrows()
        ]
    )
    want = expected["comp_sizeband_pps"].to_numpy(dtype=np.float64)
    assert np.isfinite(want).sum() > len(df) // 2
    np.testing.assert_allclose(got, want, rtol=1e-9, equal_nan=True)
    implied = expected["comp_sizeband_implied_value"].to_numpy(dtype=np.float64)
    np.testing.assert_allclose(implied, want * df["total_floor_area"].to_numpy(dtype=np.float64), equal_nan=True)


def test_size_matched_comparables_keep_to_the_size_band_and_widen_when_thin():
    day = pd.Timestamp("2020-06-01")
    rows = []
    # Four 100 m² flats 500 m away at £10k/m², and big and small ones nearer at other rates.
    for i in range(4):
        rows.append((day - pd.Timedelta(days=30 + i), 1_000_000.0, 100.0, 0.0045))
    rows += [(day - pd.Timedelta(days=10), 3_000_000.0, 200.0, 0.001), (day - pd.Timedelta(days=11), 300_000.0, 40.0, 0.001)]
    # A fifth in-band flat 1.5 km away at £12k/m²: the inner radius has four, so the search widens to it.
    rows.append((day - pd.Timedelta(days=40), 1_200_000.0, 100.0, 0.0135))
    sales = pd.DataFrame(
        {
            "ppd_id": np.arange(len(rows)),
            "dateoftransfer": [r[0] for r in rows],
            "price": [r[1] for r in rows],
            "propertytype": "F",
            "total_floor_area": [r[2] for r in rows],
            "current_energy_rating": "C",
            "latitude": [LAT0 + r[3] for r in rows],
            "longitude": LON0,
            "msoa_code": "E1",
            "postcode_district": "CR0",
            "postcode_sector": "CR0_1",
            "poststreet": "BARCLAY ROAD",
        }
    )
    store = _store(sales)
    out = store.sizeband_comp_features(day, LAT0, LON0, "F", 110.0, "CR0", "all")
    assert out["comp_sizeband_pps"] == pytest.approx(np.exp(np.median(np.log([10_000.0] * 4 + [12_000.0]))))
    assert out["comp_sizeband_implied_value"] == pytest.approx(out["comp_sizeband_pps"] * 110.0)
    # No floor area, no size band.
    assert np.isnan(store.sizeband_comp_features(day, LAT0, LON0, "F", np.nan, "CR0", "all")["comp_sizeband_pps"])


def test_the_locally_indexed_previous_sale_and_its_ratio_match_training_and_pricing():
    index = _synthetic_local_index()
    frame = pd.DataFrame(
        {
            "dateoftransfer": pd.to_datetime(["2019-05-01", "2019-05-01", "2020-01-15", "2018-03-03"]),
            "prev_sale_date": pd.to_datetime(["2012-02-10", "2012-02-10", None, "2017-11-30"]),
            "prev_sale_price": [400_000.0, 400_000.0, np.nan, 1_000_000.0],
            "propertytype": ["T", "F", "T", "D"],
            "postcode_district": ["SW11", "SW11", "SW11", None],
            "total_floor_area": [90.0, 60.0, 80.0, 200.0],
            "comp_sizeband_implied_value": [700_000.0, np.nan, 650_000.0, np.nan],
            "comp_median_price_per_sqm": [7_000.0, 6_000.0, 7_000.0, np.nan],
            "postcode_sector_24m_comp_median_price": [600_000.0] * 4,
        }
    )
    trained = hpp_rf.add_market_relative_features(frame, local_index=index)
    # What `build_sale_row` stores: the price times `EvidenceStore.time_factor` along the same index.
    priced = frame.copy()
    priced["prev_sale_local_indexed"] = [
        p * index.factor(d0, d1, a, k) if np.isfinite(p) else np.nan
        for p, d0, d1, a, k in zip(
            frame["prev_sale_price"], frame["prev_sale_date"], frame["dateoftransfer"],
            frame["postcode_district"], frame["propertytype"],
        )
    ]
    served = hpp_rf.add_market_relative_features(priced)
    for col in hpp_rf.LOCAL_REPEAT_SALE_COLUMNS:
        np.testing.assert_allclose(
            served[col].to_numpy(dtype=np.float64), trained[col].to_numpy(dtype=np.float64), rtol=1e-6, equal_nan=True
        )
    # Terraces rose faster than flats here, so the same sale indexes higher as a terrace.
    assert trained["prev_sale_local_indexed"].iloc[0] > trained["prev_sale_local_indexed"].iloc[1]
    # The anchor: size-matched value, else comparables' £/m² x area, else the sector's median price.
    assert list(trained[hpp_rf.ANCHOR_COLUMN]) == pytest.approx([700_000.0, 360_000.0, 650_000.0, 600_000.0])
    assert trained["rel_prev_sale_local"].iloc[0] == pytest.approx(
        np.log(trained["prev_sale_local_indexed"].iloc[0] / 700_000.0), rel=1e-6
    )


# --------------------------------------------------------------------------
# Ofsted school features (migrations 0033-0034)
# --------------------------------------------------------------------------


def _spells(rows):
    return pd.DataFrame(
        rows,
        columns=["id", "phase", "lat", "lon", "valid_from", "valid_to", "rating_score", "grade_basis", "inspection_date"],
    )


def test_ofsted_features_read_only_the_spells_in_force_near_the_home():
    """Nearest per phase, counts and IDW use spells in force on the date; all-through counts twice."""
    from hpldn.backend.scripts import ofsted_features

    lat, lon = 51.5, -0.1
    north_500m = lat + 500 / 111_250
    rows = [
        # Nearest primary from 2015-03-01 to 2020-01-01, Good.
        (1, "primary", north_500m, lon, "2015-03-01", "2020-01-01", 2.0, "overall", "2015-02-01"),
        # Replaced by an Outstanding report from 2020-01-01.
        (2, "primary", north_500m, lon, "2020-01-01", None, 1.0, "overall", "2019-12-01"),
        # An all-through school 1.2 km away, Requires improvement.
        (3, "all_through", lat - 1200 / 111_250, lon, "2010-01-01", None, 3.0, "carried", "2018-01-01"),
        # Not yet published on 2016-06-01.
        (4, "secondary", lat, lon, "2017-01-01", None, 1.0, "overall", "2016-11-01"),
        # Too far away.
        (5, "secondary", lat + 0.1, lon, "2010-01-01", None, 1.0, "overall", "2009-11-01"),
    ]
    store = hpp_inference.EvidenceStore.__new__(hpp_inference.EvidenceStore)
    store.set_ofsted_spells(_spells(rows))

    got = store.ofsted_features(pd.Timestamp("2016-06-01"), lat, lon)
    assert got["ofsted_primary_nearest_m"] == pytest.approx(500, rel=0.005)
    assert got["ofsted_primary_nearest_score"] == 2.0
    assert got["ofsted_primary_nearest_age_years"] == pytest.approx(
        (pd.Timestamp("2016-06-01") - pd.Timestamp("2015-02-01")).days / 365.25
    )
    assert got["ofsted_nearest_primary_basis"] == "overall"
    assert got["ofsted_secondary_nearest_m"] == pytest.approx(1200, rel=0.005)
    assert got["ofsted_secondary_nearest_score"] == 3.0
    assert (got["ofsted_primary_good_plus_1km"], got["ofsted_primary_outstanding_1km"]) == (1.0, 0.0)
    assert got["ofsted_secondary_good_plus_2km"] == 0.0
    # Within 1.5 km: the primary (500 m, 2) and the all-through school (1.2 km, 3).
    expected_idw = (2.0 / 500 + 3.0 / 1200) / (1 / 500 + 1 / 1200)
    assert got["ofsted_primary_idw_score_1500m"] == pytest.approx(expected_idw, rel=0.005)

    later = store.ofsted_features(pd.Timestamp("2021-01-01"), lat, lon)
    assert later["ofsted_primary_nearest_score"] == 1.0
    assert later["ofsted_primary_outstanding_1km"] == 1.0
    assert later["ofsted_secondary_nearest_m"] == pytest.approx(0.0, abs=1e-6)
    assert later["ofsted_secondary_good_plus_2km"] == 1.0

    none_yet = store.ofsted_features(pd.Timestamp("2009-01-01"), lat, lon)
    assert np.isnan(none_yet["ofsted_primary_nearest_m"])
    assert none_yet["ofsted_primary_good_plus_1km"] == 0.0
    assert set(none_yet) == set(ofsted_features.OFSTED_COLUMNS)

    nowhere = store.ofsted_features(pd.Timestamp("2021-01-01"), np.nan, np.nan)
    assert all(nowhere[c] is None or np.isnan(nowhere[c]) for c in nowhere)
    store.set_ofsted_spells(None)
    assert np.isnan(store.ofsted_features(pd.Timestamp("2021-01-01"), lat, lon)["ofsted_primary_good_plus_1km"])


def test_ofsted_columns_are_computed_for_the_date_not_copied_from_a_template():
    """The time-varying ofsted_* columns are parity-checked and never static."""
    from hpldn.backend.scripts import ofsted_features

    assert not [c for c in hpp_inference.STATIC_COLUMNS if c.startswith("ofsted_")]
    assert set(ofsted_features.NUMERIC_COLUMNS) <= set(hpp_inference.PARITY_COLUMNS)


def test_ofsted_features_match_the_sql_table_for_sampled_sales(hpdbldn_ro):
    """`EvidenceStore.ofsted_features` reproduces production.ofsted_features (0034) for sampled sales."""
    from sqlalchemy import text

    from hpldn.backend.scripts import ofsted_features

    engine = hpdbldn_ro
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT to_regclass('production.ofsted_features') IS NOT NULL")
        ).scalar_one(), "hpdbldn is before migration 0034"
        spells = pd.read_sql(
            hpp_inference._OFSTED_SPELLS_SQL, connection, params={"phases": list(ofsted_features.FEATURE_PHASES)}
        )
        sample = pd.read_sql(
            text(
                "SELECT f.*, p.dateoftransfer, ac.latitude, ac.longitude "
                "FROM production.ofsted_features f "
                "JOIN production.ppd_table p ON p.id = f.ppd_id "
                "JOIN production.address_coordinates ac ON ac.address_id = p.address_id "
                "WHERE p.dateoftransfer >= DATE '2006-01-01' AND mod(f.ppd_id, 3001) = 17 "
                "ORDER BY f.ppd_id LIMIT 600"
            ),
            connection,
        )
    assert len(sample) >= 500
    store = hpp_inference.EvidenceStore.__new__(hpp_inference.EvidenceStore)
    store.set_ofsted_spells(spells)
    got = pd.DataFrame(
        [store.ofsted_features(r.dateoftransfer, r.latitude, r.longitude) for r in sample.itertuples()]
    )
    for col in ofsted_features.NUMERIC_COLUMNS:
        expected = pd.to_numeric(sample[col]).to_numpy(dtype=np.float64)
        actual = got[col].to_numpy(dtype=np.float64)
        assert (np.isfinite(expected) == np.isfinite(actual)).mean() >= 0.99, col
        both = np.isfinite(expected) & np.isfinite(actual)
        within = np.abs(expected[both] - actual[both]) <= np.maximum(0.01 * np.abs(expected[both]), 0.01)
        assert within.mean() >= 0.99, (col, within.mean())
    same_basis = (sample["ofsted_nearest_primary_basis"].fillna("") == got["ofsted_nearest_primary_basis"].fillna(""))
    assert same_basis.mean() >= 0.99
