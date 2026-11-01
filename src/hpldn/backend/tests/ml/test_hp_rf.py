"""
Tests for `hpldn.backend.scripts.hpp_rf`.

Most tests use a small synthetic frame shaped like ``production.london_houses``
and need no database: leakage-safe splits and train-only encoding and
statistics, feature engineering, regressor resolution, metrics, backtesting,
sample weights, the saved matrix layout and end-to-end training. A final
integration test reads hpdbldn (`hpdbldn_ro`, read-only).
"""


import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestRegressor

from hpldn.backend.scripts import hpp_cb, hpp_rf


def _make_dataset(n=400, seed=0, start="2015-01-01", periods_days=3000, unseen_district_in_test=False):
    """
    Return a synthetic stand-in for ``production.london_houses``.

    Price is built from floor area, rooms, type, tenure length, a time trend,
    transit access and noise, so a model scores well only by fitting that
    structure. Nullable columns mimic the view: ``oldnew`` is mostly N with a
    small Y share and rare NULLs, and the IoD columns are NULL where the LSOA
    is, plus a few more, so missing flags have variance.
    """
    rng = np.random.default_rng(seed)
    dates = pd.to_datetime(start) + pd.to_timedelta(
        np.sort(rng.integers(0, periods_days, size=n)), unit="D"
    )
    propertytype = rng.choice(["D", "S", "T", "F"], size=n)
    duration = rng.choice(["F", "L"], size=n)
    districts = ["SW1", "E17", "W5", "N1", "SE10"]
    postcode_district = rng.choice(districts, size=n)
    if unseen_district_in_test:
        postcode_district = postcode_district.astype(object)
        postcode_district[-1] = "ZZ9"
    total_floor_area = rng.uniform(40, 200, size=n)
    number_habitable_rooms = rng.integers(1, 8, size=n)
    lot_area_sqm = rng.uniform(50, 400, size=n)
    lot_area_sqm[rng.random(n) < 0.3] = np.nan
    construction_age_band = rng.choice(
        ["1900-1929", "1930-1949", "1996-2002"], size=n
    ).astype(object)
    construction_age_band[rng.random(n) < 0.05] = None
    current_energy_rating = rng.choice(["B", "C", "D", "E"], size=n)
    postcode_sector = np.array([f"{d}_{s}" for d, s in zip(postcode_district, rng.integers(1, 9, size=n))])
    construction_age_band_propertytype = np.array(
        [f"{c if c is not None else 'UNKNOWN'}_{p}" for c, p in zip(construction_age_band, propertytype)]
    )
    build_form = rng.choice(
        ["Detached", "Semi-Detached", "Mid-Terrace", "End-Terrace"], size=n
    ).astype(object)
    build_form[rng.random(n) < 0.05] = None
    mains_gas_flag = rng.choice(["Y", "N"], size=n).astype(object)
    mains_gas_flag[rng.random(n) < 0.05] = None
    oldnew = np.where(rng.random(n) < 0.05, "Y", "N").astype(object)
    oldnew[rng.random(n) < 0.02] = None
    tenure = rng.choice(["O", "P", "S"], size=n).astype(object)
    tenure[rng.random(n) < 0.05] = None
    floor_level = np.where(
        propertytype == "F",
        rng.choice(["ground floor", "1st", "2nd", "3rd", "4th", "5th or above"], size=n),
        None,
    ).astype(object)
    floor_level[(propertytype == "F") & (rng.random(n) < 0.15)] = None
    is_maisonette = np.where(propertytype == "F", rng.random(n) < 0.3, False).astype(object)
    is_maisonette[rng.random(n) < 0.02] = None
    multi_glaze_proportion = rng.integers(0, 101, size=n)
    extension_count = rng.integers(0, 3, size=n)
    latitude = rng.normal(51.5074, 0.08, size=n)
    longitude = rng.normal(-0.1278, 0.12, size=n)
    location_missing = rng.random(n) < 0.1
    latitude[location_missing] = np.nan
    longitude[location_missing] = np.nan

    type_premium = pd.Series(propertytype).map({"D": 3, "S": 2, "T": 1, "F": 0}).to_numpy()
    time_trend = (dates - dates.min()).days.to_numpy() * 50.0

    nearest_station_zone = rng.integers(1, 10, size=n)
    nearest_station_distance_km = np.abs(rng.normal(0.8, 0.5, size=n))
    boe_rate_at_sale = rng.uniform(0.25, 5.0, size=n)
    ukhpi_avg_price_at_sale = (
        280_000 + time_trend * 0.5 + type_premium * 20_000 + rng.normal(0, 15_000, size=n)
    )

    ptal_missing = rng.random(n) < 0.05
    ptal_band_rank = rng.integers(0, 9, size=n).astype("float64")
    ptal_ai = ptal_band_rank * 5.0 + rng.uniform(0, 5.0, size=n)
    ptal_band_rank[ptal_missing] = np.nan
    ptal_ai[ptal_missing] = np.nan

    distance_to_river = np.abs(rng.normal(1.5, 1.0, size=n))
    distance_to_river[rng.random(n) < 0.05] = np.nan
    lsoa_code = np.array(
        [f"{d}_LSOA{s}" for d, s in zip(postcode_district, rng.integers(1, 20, size=n))]
    ).astype(object)
    lsoa_code[rng.random(n) < 0.05] = None
    msoa_code = np.array(
        [f"{d}_MSOA{s}" for d, s in zip(postcode_district, rng.integers(1, 5, size=n))]
    ).astype(object)
    msoa_code[rng.random(n) < 0.05] = None

    price = (
        150_000
        + total_floor_area * 2000
        + number_habitable_rooms * 15_000
        + type_premium * 80_000
        + time_trend
        + (10 - nearest_station_zone) * 4_000
        - nearest_station_distance_km * 15_000
        + np.nan_to_num(ptal_band_rank, nan=4.0) * 3_000
        - np.array([total_floor_area[i] * 340.0 if is_maisonette[i] is True else 0.0 for i in range(n)])
        + rng.normal(0, 20_000, size=n)
    )
    price = np.clip(price, 10_000, None)

    comp_missing = rng.random(n) < 0.05
    comp_txn_count = rng.integers(1, 6, size=n)
    comp_txn_count[comp_missing] = 0
    comp_median_price = price * rng.uniform(0.9, 1.1, size=n)
    comp_median_price_per_sqm = comp_median_price / total_floor_area
    comp_median_price[comp_missing] = np.nan
    comp_median_price_per_sqm[comp_missing] = np.nan

    prev_sale_missing = rng.random(n) >= 0.204
    prev_sale_price = price * rng.uniform(0.85, 1.15, size=n)
    prev_sale_price[prev_sale_missing] = np.nan

    years_since_prev_sale = rng.uniform(0.1, 15.0, size=n)
    years_since_prev_sale[prev_sale_missing] = np.nan

    poststreet = rng.choice(
        ["High Street", "Church Road", "Station Road", "Park Avenue"], size=n
    ).astype(object)
    poststreet[rng.random(n) < 0.05] = None

    neighbourhood_missing = rng.random(n) < 0.05
    spatial_lag_price = price * rng.uniform(0.9, 1.1, size=n)
    median_price_per_sqm_20nn_6m = (price / total_floor_area) * rng.uniform(0.9, 1.1, size=n)
    recency_weighted_price = price * rng.uniform(0.9, 1.1, size=n)
    proptype_local_median_800m_6m = price * rng.uniform(0.9, 1.1, size=n)
    floor_area_percentile_local = rng.uniform(0, 1, size=n)
    epc_band_shares = rng.dirichlet(np.ones(7), size=n)
    for arr in (
        spatial_lag_price,
        median_price_per_sqm_20nn_6m,
        recency_weighted_price,
        proptype_local_median_800m_6m,
        floor_area_percentile_local,
    ):
        arr[neighbourhood_missing] = np.nan
    epc_band_shares[neighbourhood_missing] = np.nan

    msoa_trend_missing = pd.isna(msoa_code) | (rng.random(n) < 0.05)
    msoa_price_momentum_1y = rng.normal(0.03, 0.05, size=n)
    msoa_price_momentum_5y = rng.normal(0.15, 0.1, size=n)
    msoa_price_momentum_1y[msoa_trend_missing] = np.nan
    msoa_price_momentum_5y[msoa_trend_missing] = np.nan


    iod_missing = pd.isna(lsoa_code) | (rng.random(n) < 0.03)
    iod_columns = {}
    for col in hpp_cb.IOD_LEVEL_COLUMNS:
        values = (
            rng.uniform(0.0, 100.0, size=n)
            if col.endswith("_pct_london")
            else rng.normal(20.0, 8.0, size=n)
        )
        values[iod_missing] = np.nan
        iod_columns[col] = values
    for col in hpp_cb.IOD_CONTEXT_COLUMNS:
        values = (
            rng.uniform(0.0, 12.0, size=n)
            if col == "iod_release_age_years"
            else rng.normal(0.0, 5.0, size=n)
        )
        values[iod_missing] = np.nan
        iod_columns[col] = values
    for col in hpp_cb.IOD_CHANGE_COLUMNS:
        values = (
            rng.uniform(3.0, 6.0, size=n)
            if col == "iod_years_between_releases"
            else rng.normal(0.0, 3.0, size=n)
        )
        values[iod_missing] = np.nan
        iod_columns[col] = values
    imd_decile = rng.integers(1, 11, size=n).astype("float64")
    imd_decile[iod_missing] = np.nan
    iod_columns["iod_imd_decile_national"] = imd_decile
    release_year = rng.choice([2015, 2019], size=n).astype("float64")
    release_year[iod_missing] = np.nan
    iod_columns["iod_release_year"] = release_year

    # Ofsted school features (migration 0034): NaN without a location, counts 0 then.
    ofsted_missing = np.isnan(latitude) | (rng.random(n) < 0.02)
    ofsted_columns = {}
    for name in ("primary", "secondary"):
        nearest = rng.uniform(50.0, 2500.0, size=n)
        score = rng.choice([1.0, 1.5, 2.0, 2.25, 3.0, 4.0], size=n)
        age = rng.uniform(0.0, 12.0, size=n)
        for arr in (nearest, score, age):
            arr[ofsted_missing] = np.nan
        ofsted_columns[f"ofsted_{name}_nearest_m"] = nearest
        ofsted_columns[f"ofsted_{name}_nearest_score"] = score
        ofsted_columns[f"ofsted_{name}_nearest_age_years"] = age
    for col, high in (("ofsted_primary_good_plus_1km", 8), ("ofsted_primary_outstanding_1km", 3),
                      ("ofsted_secondary_good_plus_2km", 5)):
        counts = rng.integers(0, high + 1, size=n).astype("float64")
        counts[ofsted_missing] = np.nan
        ofsted_columns[col] = counts
    for col in ("ofsted_primary_idw_score_1500m", "ofsted_secondary_idw_score_3km"):
        values = rng.uniform(1.0, 3.5, size=n)
        values[ofsted_missing] = np.nan
        ofsted_columns[col] = values
    ofsted_columns["ofsted_nearest_primary_basis"] = np.where(
        ofsted_missing, None, rng.choice(["overall", "key_judgements", "report_card", "carried"], size=n)
    )

    return pd.DataFrame(
        {
            "ppd_id": np.arange(1, n + 1),
            "address_id": np.arange(1, n + 1),
            "uprn": np.arange(1, n + 1),
            "price": price,
            "dateoftransfer": dates,
            "propertytype": propertytype,
            "duration": duration,
            "postcode_district": postcode_district,
            "postcode_sector": postcode_sector,
            "construction_age_band": construction_age_band,
            "construction_age_band_propertytype": construction_age_band_propertytype,
            "is_maisonette": is_maisonette,
            "number_habitable_rooms": number_habitable_rooms,
            "current_energy_rating": current_energy_rating,
            "total_floor_area": total_floor_area,
            "lot_area_sqm": lot_area_sqm,
            "build_form": build_form,
            "mains_gas_flag": mains_gas_flag,
            "oldnew": oldnew,
            "tenure": tenure,
            "floor_level": floor_level,
            "multi_glaze_proportion": multi_glaze_proportion,
            "extension_count": extension_count,
            "latitude": latitude,
            "longitude": longitude,
            "comp_median_price": comp_median_price,
            "comp_median_price_per_sqm": comp_median_price_per_sqm,
            "comp_txn_count": comp_txn_count,
            "nearest_station_distance_km": nearest_station_distance_km,
            "nearest_station_zone": nearest_station_zone,
            "boe_rate_at_sale": boe_rate_at_sale,
            "ukhpi_avg_price_at_sale": ukhpi_avg_price_at_sale,
            "ptal_band_rank": ptal_band_rank,
            "ptal_ai": ptal_ai,
            "distance_to_river": distance_to_river,
            "lsoa_code": lsoa_code,
            "msoa_code": msoa_code,
            "prev_sale_price": prev_sale_price,
            "years_since_prev_sale": years_since_prev_sale,
            "poststreet": poststreet,
            "spatial_lag_price": spatial_lag_price,
            "median_price_per_sqm_20nn_6m": median_price_per_sqm_20nn_6m,
            "recency_weighted_price": recency_weighted_price,
            "proptype_local_median_800m_6m": proptype_local_median_800m_6m,
            "floor_area_percentile_local": floor_area_percentile_local,
            "epc_band_share_a": epc_band_shares[:, 0],
            "epc_band_share_b": epc_band_shares[:, 1],
            "epc_band_share_c": epc_band_shares[:, 2],
            "epc_band_share_d": epc_band_shares[:, 3],
            "epc_band_share_e": epc_band_shares[:, 4],
            "epc_band_share_f": epc_band_shares[:, 5],
            "epc_band_share_g": epc_band_shares[:, 6],
            "msoa_price_momentum_1y": msoa_price_momentum_1y,
            "msoa_price_momentum_5y": msoa_price_momentum_5y,
            **iod_columns,
            **ofsted_columns,
        }
    )


MORTGAGE_RATE_START = "2010-01-01"


def _make_mortgage_rates(start=MORTGAGE_RATE_START, months=300, seed=0):
    """
    Return a synthetic stand-in for ``production.mortgage_rates``.

    A monthly ``(rate_date, rate_2y_75ltv)`` series starting early enough to
    cover `_make_dataset`'s dates, for CatBoost functions that take the table.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=months, freq="MS")
    rate = 2.5 + np.cumsum(rng.normal(0.0, 0.05, size=months))
    return pd.DataFrame(
        {
            hpp_cb.MORTGAGE_RATE_DATE_COLUMN: dates,
            hpp_cb.MORTGAGE_RATE_COLUMN: np.clip(rate, 0.5, 9.0),
        }
    )


def _with_mortgage_features(df, mortgage_df=None):
    """Join the synthetic mortgage-rate series onto `df` as `hpp_cb.run_training` does."""
    return hpp_cb.add_mortgage_rate_features(
        df, mortgage_df if mortgage_df is not None else _make_mortgage_rates()
    )


def _make_args(**overrides):
    args = hpp_rf.build_arg_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args




def test_engineer_features_adds_expected_columns():
    raw = _make_dataset(n=50)
    df = hpp_rf.engineer_features(raw)
    for col in [
        "sale_year",
        "sale_month",
        "property_age_years",
        "area_per_room",
        "log_floor_area",
        "log_lot_area",
        "lot_to_floor_ratio",
        "distance_to_city_center_km",
        "is_house",
        "is_maisonette",
    ] + hpp_rf.INTERACTION_COLUMNS:
        assert col in df.columns
    assert df["property_age_years"].isna().equals(raw["construction_age_band"].isna())
    assert not df["area_per_room"].isna().any()
    assert not df["log_floor_area"].isna().any()
    assert not df["is_house"].isna().any()
    assert not df.loc[df["propertytype"] != "F", "is_maisonette"].isna().any()
    assert (df["postcode_propertytype"] == df["postcode_district"] + "_" + df["propertytype"]).all()
    assert (df["propertytype_duration"] == df["propertytype"] + "_" + df["duration"]).all()




def test_engineer_features_is_house_matches_propertytype():
    """Check ``is_house`` is 1 for D/S/T and 0 for F, and never NaN."""
    df = hpp_rf.engineer_features(_make_dataset(n=200))
    expected = (df["propertytype"] != "F").astype("int8")
    assert (df["is_house"] == expected).all()
    assert set(df["is_house"].unique()) <= {0, 1}
    assert (df.loc[df["propertytype"] == "F", "is_house"] == 0).all()
    assert (df.loc[df["propertytype"] != "F", "is_house"] == 1).all()


def test_engineer_features_floor_level_unknown_for_every_house_and_some_flats():
    """Check ``floor_level`` becomes "UNKNOWN" for every house and flats without an EPC."""
    raw = _make_dataset(n=300)
    assert raw.loc[raw["propertytype"] != "F", "floor_level"].isna().all()
    missing_flats = raw["propertytype"].eq("F") & raw["floor_level"].isna()
    assert missing_flats.any(), "fixture should include flats missing floor_level"

    df = hpp_rf.engineer_features(raw)
    assert df["floor_level"].isna().sum() == 0
    assert (df.loc[raw["propertytype"] != "F", "floor_level"] == "UNKNOWN").all()
    assert (df.loc[missing_flats, "floor_level"] == "UNKNOWN").all()
    populated_flats = raw["propertytype"].eq("F") & raw["floor_level"].notna()
    assert (df.loc[populated_flats, "floor_level"] == raw.loc[populated_flats, "floor_level"]).all()


def test_feature_columns_include_property_type_features():
    """Check ``is_house`` and ``floor_level`` are in `FEATURE_COLUMNS`."""
    assert "is_house" in hpp_rf.FEATURE_COLUMNS
    assert "is_house" in hpp_rf.ENGINEERED_COLUMNS
    assert "floor_level" in hpp_rf.FEATURE_COLUMNS
    assert "floor_level" in hpp_rf.CATEGORICAL_COLUMNS
    assert "floor_level" in hpp_rf.NULLABLE_CATEGORICAL_COLUMNS


def test_engineer_features_is_maisonette_keeps_an_unknown_flat_flag_unknown():
    """
    Check ``is_maisonette`` is 1/0 where known, 0 for houses, and NaN for a flat whose flag is unknown.

    Filling unknown with False turned every post-2024 maisonette into a
    flat and hid the shift from the availability guards.
    """
    raw = _make_dataset(n=300)
    unknown_flats = raw["is_maisonette"].isna() & raw["propertytype"].eq("F")
    assert unknown_flats.any(), "fixture should include flats with an unknown flag"
    df = hpp_rf.engineer_features(raw)
    assert df["is_maisonette"].dtype == np.float64
    assert df.loc[unknown_flats, "is_maisonette"].isna().all()
    known_flats = raw["is_maisonette"].notna() & raw["propertytype"].eq("F")
    assert (
        df.loc[known_flats, "is_maisonette"] == raw.loc[known_flats, "is_maisonette"].astype("float64")
    ).all()


def test_engineer_features_is_maisonette_never_true_outside_flats():
    """Check ``is_maisonette`` is 0 for every D/S/T row, known or not."""
    df = hpp_rf.engineer_features(_make_dataset(n=300))
    assert (df.loc[df["propertytype"] != "F", "is_maisonette"] == 0).all()
    flats = df.loc[df["propertytype"] == "F", "is_maisonette"]
    assert (flats.isin([0, 1]) | flats.isna()).all()


def test_engineer_features_build_form_propertytype_built_after_unknown_fill():
    """Check ``build_form_propertytype`` is built after the "UNKNOWN" fill, so it is never NULL."""
    raw = _make_dataset(n=300)
    missing_build_form = raw["build_form"].isna()
    assert missing_build_form.any(), "fixture should include missing build_form rows"
    df = hpp_rf.engineer_features(raw)
    assert df["build_form_propertytype"].isna().sum() == 0
    assert (
        df.loc[missing_build_form, "build_form_propertytype"] == "UNKNOWN_" + df.loc[missing_build_form, "propertytype"]
    ).all()
    populated = ~missing_build_form
    assert (
        df.loc[populated, "build_form_propertytype"]
        == df.loc[populated, "build_form"] + "_" + df.loc[populated, "propertytype"]
    ).all()


def test_feature_columns_include_new_interaction_and_lag_features():
    """Check the interaction, maisonette and previous-sale features are in `FEATURE_COLUMNS`."""
    assert "is_maisonette" in hpp_rf.FEATURE_COLUMNS
    assert "is_maisonette" in hpp_rf.ENGINEERED_COLUMNS
    assert "build_form_propertytype" in hpp_rf.CATEGORICAL_COLUMNS
    assert "build_form_propertytype" in hpp_rf.INTERACTION_COLUMNS
    assert "construction_age_band_propertytype" in hpp_rf.CATEGORICAL_COLUMNS
    assert "construction_age_band_propertytype" not in hpp_rf.INTERACTION_COLUMNS
    assert "prev_sale_price" in hpp_rf.FEATURE_COLUMNS
    assert "prev_sale_price" in hpp_rf.PREV_SALE_DEPENDENT_COLUMNS


def test_engineer_features_distance_null_exactly_with_latitude():
    df = hpp_rf.engineer_features(_make_dataset(n=200))
    missing = df["latitude"].isna()
    assert missing.any() and (~missing).any()
    assert df.loc[missing, "distance_to_city_center_km"].isna().all()
    assert df.loc[~missing, "distance_to_city_center_km"].notna().all()
    assert (df.loc[~missing, "distance_to_city_center_km"] >= 0).all()
    assert (df.loc[~missing, "distance_to_city_center_km"] < 50).all()


def test_engineer_features_fills_nullable_categoricals_with_unknown():
    df = hpp_rf.engineer_features(_make_dataset(n=200))
    for col in hpp_rf.NULLABLE_CATEGORICAL_COLUMNS:
        assert df[col].isna().sum() == 0
        assert "UNKNOWN" in set(df[col].unique())


def test_construction_age_band_midpoint():
    band = pd.Series(["1900-1929", "1996-2002", "2007"])
    midpoint = hpp_rf._construction_age_band_midpoint(band)
    assert midpoint.tolist() == pytest.approx([1914.5, 1999.0, 2007.0])


def test_construction_age_band_midpoint_parses_england_and_wales_prefixed_bands():
    band = pd.Series(
        [
            "England and Wales: 1900-1929",
            "England and Wales: before 1900",
            "England and Wales: 2012 onwards",
        ]
    )
    midpoint = hpp_rf._construction_age_band_midpoint(band)
    assert midpoint.tolist() == pytest.approx([1914.5, 1900.0, 2012.0])


def test_engineer_features_lot_derived_columns_null_exactly_with_lot_area_sqm():
    df = hpp_rf.engineer_features(_make_dataset(n=200))
    missing = df["lot_area_sqm"].isna()
    assert missing.any() and (~missing).any()
    assert df.loc[missing, "log_lot_area"].isna().all()
    assert df.loc[missing, "lot_to_floor_ratio"].isna().all()
    assert df.loc[~missing, "log_lot_area"].notna().all()
    assert df.loc[~missing, "lot_to_floor_ratio"].notna().all()




def _split(df, **kwargs):
    return hpp_rf.time_series_split(hpp_rf.engineer_features(df), **kwargs)


def test_add_postcode_price_stats_adds_columns_with_no_nulls():
    train_df, val_df, test_df = _split(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    train_df, val_df, test_df, stats = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    for part in (train_df, val_df, test_df):
        for col in hpp_rf.POSTCODE_STAT_COLUMNS:
            assert col in part.columns
            assert part[col].isna().sum() == 0
    assert set(stats.index) <= set(train_df["postcode_district"].unique())


def test_add_postcode_price_stats_val_test_use_train_only_lookup():
    train_df, val_df, test_df = _split(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    _, val_stat, _, stats = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    global_median = train_df["price"].median()
    expected = val_df["postcode_district"].map(stats["postcode_median_price"]).fillna(global_median)
    assert np.allclose(val_stat["postcode_median_price"].to_numpy(), expected.to_numpy())


def test_add_postcode_price_stats_unseen_district_falls_back_to_global_median():
    train_df, val_df, test_df = _split(
        _make_dataset(n=300, unseen_district_in_test=True), train_frac=0.7, val_frac=0.15
    )
    _, _, test_stat, stats = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    assert "ZZ9" not in stats.index
    assert test_stat["postcode_median_price"].iloc[-1] == pytest.approx(train_df["price"].median())
    assert test_stat["postcode_txn_count"].iloc[-1] == 0


def test_add_postcode_price_stats_train_is_leave_one_out_not_self_referential():
    train_df = pd.DataFrame(
        {
            "postcode_district": ["SW1", "E1", "E1", "E1", "E1"],
            "price": [999_000.0, 100_000.0, 110_000.0, 120_000.0, 130_000.0],
            "total_floor_area": [80.0, 50.0, 55.0, 60.0, 65.0],
        }
    )
    val_df, test_df = train_df.iloc[:1].copy(), train_df.iloc[:1].copy()
    train_stat, _, _, _ = hpp_rf.add_postcode_price_stats(
        train_df, val_df, test_df, n_folds=len(train_df)
    )
    sw1_row = train_stat[train_stat["postcode_district"] == "SW1"].iloc[0]
    assert sw1_row["postcode_median_price"] == pytest.approx(train_df["price"].median())
    assert sw1_row["postcode_txn_count"] == 0


def test_add_postcode_sector_price_stats_adds_columns_with_no_nulls():
    """Check sector price statistics are added with no NULLs, like the district ones."""
    train_df, val_df, test_df = _split(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    train_df, val_df, test_df, stats = hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)
    for part in (train_df, val_df, test_df):
        for col in hpp_rf.SECTOR_STAT_COLUMNS:
            assert col in part.columns
            assert part[col].isna().sum() == 0
    assert set(stats.index) <= set(train_df["postcode_sector"].unique())




def test_time_series_split_no_leakage():
    df = _make_dataset(n=200)
    train_df, val_df, test_df = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    assert len(train_df) + len(val_df) + len(test_df) == len(df)
    assert train_df[hpp_rf.DATE_COLUMN].max() <= val_df[hpp_rf.DATE_COLUMN].min()
    assert val_df[hpp_rf.DATE_COLUMN].max() <= test_df[hpp_rf.DATE_COLUMN].min()
    assert len(train_df) == pytest.approx(140, abs=1)


def test_time_series_split_rejects_unsorted_input():
    df = _make_dataset(n=100).iloc[::-1].reset_index(drop=True)
    with pytest.raises(AssertionError):
        hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)


@pytest.mark.parametrize("train_frac,val_frac", [(0.9, 0.2), (0.0, 0.5), (0.5, 1.0)])
def test_time_series_split_rejects_bad_fractions(train_frac, val_frac):
    df = _make_dataset(n=50)
    with pytest.raises(ValueError):
        hpp_rf.time_series_split(df, train_frac=train_frac, val_frac=val_frac)


def test_time_series_split_rejects_fractions_yielding_empty_split():
    df = _make_dataset(n=5)
    with pytest.raises(ValueError):
        hpp_rf.time_series_split(df, train_frac=0.01, val_frac=0.01)




def test_grouped_time_series_split_removes_repeat_uprns_from_eval():
    df = _make_dataset(n=300)
    df = df.sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    df.loc[250:259, "uprn"] = df.loc[0:9, "uprn"].to_numpy()
    train_df, val_df, test_df = hpp_rf.grouped_time_series_split(
        hpp_rf.engineer_features(df), train_frac=0.7, val_frac=0.15
    )
    train_uprns = set(train_df["uprn"])
    assert not (set(val_df["uprn"]) & train_uprns)
    assert not (set(test_df["uprn"]) & train_uprns)
    assert train_df[hpp_rf.DATE_COLUMN].max() <= val_df[hpp_rf.DATE_COLUMN].min()


def test_purged_embargo_time_series_split_drops_boundary_rows():
    df = hpp_rf.engineer_features(_make_dataset(n=400))
    plain_train, plain_val, _ = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    purged_train, purged_val, purged_test = hpp_rf.purged_embargo_time_series_split(
        df, train_frac=0.7, val_frac=0.15, embargo_days=60
    )
    assert len(purged_train) < len(plain_train)
    assert len(purged_val) < len(plain_val)
    val_start = purged_val[hpp_rf.DATE_COLUMN].min()
    assert (val_start - purged_train[hpp_rf.DATE_COLUMN].max()).days >= 60


def test_spatial_holdout_split_disjoint_districts_no_leakage():
    df = hpp_rf.engineer_features(_make_dataset(n=500))
    train_df, val_df, test_df = hpp_rf.spatial_holdout_split(
        df, test_frac=0.2, val_frac=0.2, random_state=0
    )
    train_d, val_d, test_d = (
        set(train_df["postcode_district"]),
        set(val_df["postcode_district"]),
        set(test_df["postcode_district"]),
    )
    assert not (train_d & val_d)
    assert not (train_d & test_d)
    assert not (val_d & test_d)
    assert len(train_df) + len(val_df) + len(test_df) == len(df)


def test_recency_windowed_time_series_split_shrinks_train():
    df = hpp_rf.engineer_features(_make_dataset(n=400))  
    plain_train, _, _ = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    windowed_train, windowed_val, windowed_test = hpp_rf.recency_windowed_time_series_split(
        df, train_frac=0.7, val_frac=0.15, train_window_years=2.0
    )
    assert len(windowed_train) < len(plain_train)
    assert windowed_train[hpp_rf.DATE_COLUMN].max() <= windowed_val[hpp_rf.DATE_COLUMN].min()
    span_years = (windowed_train[hpp_rf.DATE_COLUMN].max() - windowed_train[hpp_rf.DATE_COLUMN].min()).days / 365
    assert span_years <= 2.01


def test_time_series_cv_folds_walk_forward_ordering():
    df = hpp_rf.engineer_features(_make_dataset(n=500))
    folds, non_test_df, test_df = hpp_rf.time_series_cv_folds(df, n_folds=3, test_frac=0.15)
    assert len(folds) == 3
    assert non_test_df[hpp_rf.DATE_COLUMN].max() <= test_df[hpp_rf.DATE_COLUMN].min()
    prev_train_len = 0
    for fold_train, fold_val in folds:
        assert fold_train[hpp_rf.DATE_COLUMN].max() <= fold_val[hpp_rf.DATE_COLUMN].min()
        assert len(fold_train) > prev_train_len  
        prev_train_len = len(fold_train)


@pytest.mark.parametrize(
    "split_method", ["chronological", "grouped", "purged", "walk_forward", "spatial", "recency"]
)
def test_run_training_end_to_end_for_every_split_method(split_method):
    """Check every ``--split-method`` trains to completion with well-formed metrics."""
    df = _make_dataset(n=600, seed=3)
    args = _make_args(
        device="cpu",
        n_estimators=10,
        max_depth=6,
        n_jobs=1,
        random_state=0,
        split_method=split_method,
        cv_folds=2,
    )
    results = hpp_rf.run_training(df, args)
    assert results["n_train"] > 0 and results["n_val"] > 0 and results["n_test"] > 0
    for split in ("val", "test"):
        assert set(results[split]) == {
            "mae", "rmse", "r2", "mape", "medae", "price_band_accuracy", "property_type_accuracy",
        }
        assert results[split]["mae"] > 0




def _split_and_stat(df, **split_kwargs):
    """Run feature engineering, the split and the price statistics `impute_and_encode` expects."""
    df = hpp_rf.engineer_features(df)
    train_df, val_df, test_df = hpp_rf.time_series_split(df, **split_kwargs)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    return hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)


def test_impute_and_encode_unseen_category_encodes_to_unknown_value():
    train_df, val_df, test_df, _ = _split_and_stat(
        _make_dataset(n=200, unseen_district_in_test=True), train_frac=0.7, val_frac=0.15
    )
    X_train, X_val, X_test, encoder, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    district_col = hpp_rf.CATEGORICAL_COLUMNS.index("postcode_district")
    assert "ZZ9" not in encoder.categories_[district_col]
    assert X_test[-1, district_col] == -1
    assert X_train.shape[1] == X_val.shape[1] == X_test.shape[1] == len(hpp_rf.FEATURE_COLUMNS) + len(hpp_rf.MISSING_FLAG_GROUPS)


def test_impute_and_encode_encodes_floor_level_including_unknown():
    """Check the "UNKNOWN" ``floor_level`` is a fitted category, not the -1 unknown code."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, encoder, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)

    floor_level_col = hpp_rf.CATEGORICAL_COLUMNS.index("floor_level")
    assert "UNKNOWN" in encoder.categories_[floor_level_col]

    unknown_rows = (train_df["floor_level"] == "UNKNOWN").to_numpy()
    assert unknown_rows.any() and (~unknown_rows).any()
    assert (X_train[unknown_rows, floor_level_col] != -1).all()


def test_impute_and_encode_orders_propertytype_and_tenure_by_train_mean_price():
    """Check propertytype and tenure are ordered by train mean price while other categoricals stay alphabetical."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    _, _, _, encoder, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)

    propertytype_col = hpp_rf.CATEGORICAL_COLUMNS.index("propertytype")
    expected_order = list(train_df.groupby("propertytype")["price"].mean().sort_values().index)
    assert list(encoder.categories_[propertytype_col]) == expected_order
    assert list(encoder.categories_[propertytype_col]) != sorted(expected_order)

    tenure_col = hpp_rf.CATEGORICAL_COLUMNS.index("tenure")
    expected_tenure_order = list(train_df.groupby("tenure")["price"].mean().sort_values().index)
    assert list(encoder.categories_[tenure_col]) == expected_tenure_order

    age_band_col = hpp_rf.CATEGORICAL_COLUMNS.index("construction_age_band")
    assert list(encoder.categories_[age_band_col]) == sorted(train_df["construction_age_band"].unique())


def test_impute_and_encode_is_house_carried_through_unimputed():
    """Check ``is_house`` passes through with no fill or flag."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)

    numeric_prefix = (
        hpp_rf.NUMERIC_COLUMNS
        + ["latitude", "longitude"]
        + hpp_rf.COMP_STAT_COLUMNS
        + hpp_rf.TRANSIT_STAT_COLUMNS
        + hpp_rf.MACRO_STAT_COLUMNS
        + hpp_rf.ENGINEERED_COLUMNS
    )
    is_house_idx = len(hpp_rf.feature_layout()["categorical"]) + numeric_prefix.index("is_house")
    expected = (train_df["propertytype"] != "F").astype(np.float64).to_numpy()
    assert np.array_equal(X_train[:, is_house_idx], expected)

    is_maisonette_idx = len(hpp_rf.feature_layout()["categorical"]) + numeric_prefix.index("is_maisonette")
    median = train_df["is_maisonette"].median()
    assert np.array_equal(
        X_train[:, is_maisonette_idx], train_df["is_maisonette"].fillna(median).astype(np.float64).to_numpy()
    )
    flag_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("maisonette_missing")
    assert np.array_equal(X_train[:, flag_idx], train_df["is_maisonette"].isna().astype(np.float64).to_numpy())


def test_impute_and_encode_fills_missing_lot_area_with_train_median():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, X_val, X_test, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians["lot_area_sqm"] == pytest.approx(train_df["lot_area_sqm"].median())

    lot_area_idx = len(hpp_rf.feature_layout()["categorical"]) + (
        hpp_rf.NUMERIC_COLUMNS + hpp_rf.ENGINEERED_COLUMNS
    ).index("lot_area_sqm")
    lot_missing_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("lot_area_missing")
    missing_rows = train_df["lot_area_sqm"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing lot_area_sqm rows"
    assert np.allclose(X_train[missing_rows, lot_area_idx], medians["lot_area_sqm"])
    assert np.array_equal(X_train[:, lot_missing_idx], missing_rows.astype(np.float64))


def test_impute_and_encode_fills_missing_location_with_train_median():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians["latitude"] == pytest.approx(train_df["latitude"].median())

    lat_idx = len(hpp_rf.feature_layout()["categorical"]) + (
        hpp_rf.NUMERIC_COLUMNS + ["latitude", "longitude"] + hpp_rf.ENGINEERED_COLUMNS
    ).index("latitude")
    location_missing_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("location_missing")
    missing_rows = train_df["latitude"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing latitude rows"
    assert np.allclose(X_train[missing_rows, lat_idx], medians["latitude"])
    assert np.array_equal(X_train[:, location_missing_idx], missing_rows.astype(np.float64))


def test_impute_and_encode_fills_missing_comps_with_train_median():
    """Check comparable-price columns are filled with the train median and flagged."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians["comp_median_price"] == pytest.approx(train_df["comp_median_price"].median())

    comp_idx = len(hpp_rf.feature_layout()["categorical"]) + (
        hpp_rf.NUMERIC_COLUMNS + ["latitude", "longitude"] + hpp_rf.COMP_STAT_COLUMNS + hpp_rf.ENGINEERED_COLUMNS
    ).index("comp_median_price")
    comp_missing_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("comp_missing")
    missing_rows = train_df["comp_median_price"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing comp_median_price rows"
    assert np.allclose(X_train[missing_rows, comp_idx], medians["comp_median_price"])
    assert np.array_equal(X_train[:, comp_missing_idx], missing_rows.astype(np.float64))




def test_engineer_features_adds_rolling_comp_columns():
    df = hpp_rf.engineer_features(_make_dataset(n=400, start="2010-01-01", periods_days=5500))
    for col in hpp_rf.ROLLING_COMP_STAT_COLUMNS:
        assert col in df.columns
    for col in hpp_rf.ROLLING_COMP_COUNT_COLUMNS:
        assert df[col].isna().sum() == 0
        assert (df[col] >= 0).all()


def test_rolling_causal_group_stats_never_uses_the_row_own_sale():
    """Check a row's own sale never enters its rolling statistic."""
    df = pd.DataFrame(
        {
            "dateoftransfer": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]),
            "sector": ["S1", "S1", "S1"],
            "price": [100_000.0, 999_000.0, 300_000.0],
        }
    )
    median, count = hpp_rf._rolling_causal_group_stats(df, ["sector"], 12, "price")
    assert count.iloc[0] == 0
    assert np.isnan(median.iloc[0])
    assert count.iloc[1] == 1
    assert median.iloc[1] == pytest.approx(100_000.0)
    assert count.iloc[2] == 2
    assert median.iloc[2] == pytest.approx((100_000.0 + 999_000.0) / 2)


def test_add_rolling_area_comp_features_smooths_sparse_groups_toward_citywide():
    """Check smoothing pulls sparse groups toward the citywide median and leaves dense groups near their own."""
    rng = np.random.default_rng(0)
    n = 500
    dates = pd.to_datetime("2015-01-01") + pd.to_timedelta(np.sort(rng.integers(0, 2000, size=n)), unit="D")
    sector = np.array(["SW1"] * (n - 1) + ["E1"])
    total_floor_area = np.full(n, 80.0)
    price = np.where(sector == "SW1", 900_000.0, 300_000.0)
    df = pd.DataFrame(
        {
            "dateoftransfer": dates,
            "postcode_sector": sector,
            "postcode_district": sector,
            "propertytype": ["F"] * n,
            "total_floor_area": total_floor_area,
            "price": price,
        }
    )
    out, new_columns = hpp_rf.add_rolling_area_comp_features(df, windows_months=(24,), eb_prior_k=8.0)
    assert new_columns == hpp_rf._rolling_comp_column_names(
        hpp_rf.ROLLING_COMP_GROUP_COLUMNS, (24,)
    )[0] + hpp_rf._rolling_comp_column_names(hpp_rf.ROLLING_COMP_GROUP_COLUMNS, (24,))[1]

    sw1_smoothed = out.loc[out["postcode_sector"] == "SW1", "postcode_sector_24m_comp_median_price"].iloc[-1]
    assert sw1_smoothed == pytest.approx(900_000.0, rel=0.01)

    e1_smoothed = out.loc[out["postcode_sector"] == "E1", "postcode_sector_24m_comp_median_price"].iloc[0]
    assert e1_smoothed == pytest.approx(900_000.0, rel=0.01)
    e1_count = out.loc[out["postcode_sector"] == "E1", "postcode_sector_24m_comp_txn_count"].iloc[0]
    assert e1_count == 0


def test_impute_and_encode_fills_missing_rolling_comp_with_train_median():
    """Check rolling comparable columns are filled with the train median and flagged."""
    train_df, val_df, test_df, _ = _split_and_stat(
        _make_dataset(n=300, start="2010-01-01", periods_days=60), train_frac=0.7, val_frac=0.15
    )
    col = "postcode_sector_3m_comp_median_price"
    assert train_df[col].isna().any(), "test fixture should include missing rolling comp rows"
    X_train, _, _, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians[col] == pytest.approx(train_df[col].median())

    rolling_comp_missing_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("rolling_comp_missing")
    missing_rows = train_df[col].isna().to_numpy()
    assert np.array_equal(X_train[:, rolling_comp_missing_idx], missing_rows.astype(np.float64))




def test_feature_columns_include_transit_and_macro_stats():
    """Check the transit and macro columns are in `FEATURE_COLUMNS`."""
    for col in hpp_rf.TRANSIT_STAT_COLUMNS + hpp_rf.MACRO_STAT_COLUMNS:
        assert col in hpp_rf.FEATURE_COLUMNS


def test_feature_columns_include_distance_to_river_and_geography_codes():
    """Check ``distance_to_river`` and the LSOA/MSOA codes are in `FEATURE_COLUMNS`."""
    assert "distance_to_river" in hpp_rf.FEATURE_COLUMNS
    assert "distance_to_river" in hpp_rf.RIVER_DEPENDENT_COLUMNS
    for col in ("lsoa_code", "msoa_code"):
        assert col in hpp_rf.FEATURE_COLUMNS
        assert col in hpp_rf.CATEGORICAL_COLUMNS
        assert col in hpp_rf.NULLABLE_CATEGORICAL_COLUMNS


def test_impute_and_encode_fills_missing_river_with_train_median():
    """Check ``distance_to_river`` is filled with the train median and flagged."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians["distance_to_river"] == pytest.approx(train_df["distance_to_river"].median())

    numeric_prefix = (
        hpp_rf.NUMERIC_COLUMNS
        + ["latitude", "longitude"]
        + hpp_rf.COMP_STAT_COLUMNS
        + hpp_rf.TRANSIT_STAT_COLUMNS
        + hpp_rf.MACRO_STAT_COLUMNS
        + hpp_rf.ENGINEERED_COLUMNS
        + hpp_rf.PTAL_DEPENDENT_COLUMNS
        + hpp_rf.RIVER_DEPENDENT_COLUMNS
    )
    river_idx = len(hpp_rf.feature_layout()["categorical"]) + numeric_prefix.index("distance_to_river")
    river_missing_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("river_missing")
    missing_rows = train_df["distance_to_river"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing distance_to_river rows"
    assert np.allclose(X_train[missing_rows, river_idx], medians["distance_to_river"])
    assert np.array_equal(X_train[:, river_missing_idx], missing_rows.astype(np.float64))


def test_impute_and_encode_fills_missing_prev_sale_price_with_train_median():
    """Check ``prev_sale_price``, NULL for about 80% of rows, is filled with the train median and flagged."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians["prev_sale_price"] == pytest.approx(train_df["prev_sale_price"].median())

    numeric_prefix = (
        hpp_rf.NUMERIC_COLUMNS
        + ["latitude", "longitude"]
        + hpp_rf.COMP_STAT_COLUMNS
        + hpp_rf.TRANSIT_STAT_COLUMNS
        + hpp_rf.MACRO_STAT_COLUMNS
        + hpp_rf.ENGINEERED_COLUMNS
        + hpp_rf.PTAL_DEPENDENT_COLUMNS
        + hpp_rf.RIVER_DEPENDENT_COLUMNS
        + hpp_rf.PREV_SALE_DEPENDENT_COLUMNS
    )
    prev_sale_idx = len(hpp_rf.feature_layout()["categorical"]) + numeric_prefix.index("prev_sale_price")
    prev_sale_missing_idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index("prev_sale_missing")
    missing_rows = train_df["prev_sale_price"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing prev_sale_price rows"
    assert np.allclose(X_train[missing_rows, prev_sale_idx], medians["prev_sale_price"])
    assert np.array_equal(X_train[:, prev_sale_missing_idx], missing_rows.astype(np.float64))


def test_impute_and_encode_geography_codes_fill_with_unknown():
    """Check LSOA/MSOA codes arrive "UNKNOWN"-filled, so none encodes to -1."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, encoder, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)

    for col in ("lsoa_code", "msoa_code"):
        assert (train_df[col] == "UNKNOWN").any(), f"test fixture should include missing {col} rows"
        col_idx = hpp_rf.CATEGORICAL_COLUMNS.index(col)
        assert set(train_df[col].unique()) <= set(encoder.categories_[col_idx])
        assert (X_train[:, col_idx] != -1).all()


def test_impute_and_encode_transit_and_macro_columns_pass_through_unimputed():
    """Check the always-populated transit and macro columns pass through unchanged with no extra flags."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, X_val, X_test, _, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert X_train.shape[1] == X_val.shape[1] == X_test.shape[1] == len(hpp_rf.FEATURE_COLUMNS) + len(hpp_rf.MISSING_FLAG_GROUPS)

    numeric_prefix = (
        hpp_rf.NUMERIC_COLUMNS
        + ["latitude", "longitude"]
        + hpp_rf.COMP_STAT_COLUMNS
        + hpp_rf.TRANSIT_STAT_COLUMNS
        + hpp_rf.MACRO_STAT_COLUMNS
        + hpp_rf.ENGINEERED_COLUMNS
    )
    for col in hpp_rf.TRANSIT_STAT_COLUMNS + hpp_rf.MACRO_STAT_COLUMNS:
        assert train_df[col].isna().sum() == 0, f"fixture should not have missing {col}"
        idx = len(hpp_rf.feature_layout()["categorical"]) + numeric_prefix.index(col)
        assert np.allclose(X_train[:, idx], train_df[col].to_numpy())


def test_run_training_feature_importance_reflects_transit_signal():
    """Check a forest gives the synthetic transit signal non-negligible importance."""
    df = _make_dataset(n=600, seed=4)
    args = _make_args(device="cpu", n_estimators=50, max_depth=10, n_jobs=1, random_state=0)
    df_engineered = hpp_rf.engineer_features(df).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    train_df, val_df, test_df = hpp_rf.time_series_split(df_engineered, args.train_frac, args.val_frac)
    model, _, _, _, _, _ = hpp_rf._fit_and_evaluate(train_df, val_df, test_df, args)

    importances = dict(zip(hpp_rf.FEATURE_COLUMNS, model.feature_importances_))
    transit_importance = importances["nearest_station_zone"] + importances["nearest_station_distance_km"]
    assert transit_importance > 0.0005




def test_resolve_regressor_cpu_returns_sklearn():
    model, backend = hpp_rf.resolve_regressor("cpu", n_estimators=5, n_jobs=1)
    assert isinstance(model, RandomForestRegressor)
    assert "scikit-learn" in backend


def test_resolve_regressor_gpu_raises_without_cuda_gpu(monkeypatch):
    class _NoGpu:
        has_cuda_gpu = False

    monkeypatch.setattr(hpp_rf, "detect_host_resources", lambda log=None: _NoGpu())
    with pytest.raises(RuntimeError, match="no CUDA-capable GPU"):
        hpp_rf.resolve_regressor("gpu", n_estimators=5)


def test_resolve_regressor_auto_falls_back_to_cpu_without_cuda_gpu(monkeypatch):
    class _NoGpu:
        has_cuda_gpu = False

    monkeypatch.setattr(hpp_rf, "detect_host_resources", lambda log=None: _NoGpu())
    model, backend = hpp_rf.resolve_regressor("auto", n_estimators=5, n_jobs=1)
    assert isinstance(model, RandomForestRegressor)
    assert "no CUDA GPU detected" in backend


def test_resolve_regressor_rejects_unknown_device():
    with pytest.raises(ValueError):
        hpp_rf.resolve_regressor("tpu")




def test_compute_metrics_perfect_prediction():
    y = np.array([100_000.0, 200_000.0, 300_000.0])
    metrics = hpp_rf.compute_metrics(y, y)
    assert metrics["mae"] == pytest.approx(0.0)
    assert metrics["rmse"] == pytest.approx(0.0)
    assert metrics["r2"] == pytest.approx(1.0)
    assert metrics["mape"] == pytest.approx(0.0)




def test_run_training_end_to_end_on_synthetic_data():
    df = _make_dataset(n=600, seed=1)
    args = _make_args(device="cpu", n_estimators=30, max_depth=8, n_jobs=1, random_state=0)
    results = hpp_rf.run_training(df, args)

    assert results["n_train"] + results["n_val"] + results["n_test"] == len(df)
    for split in ("val", "test"):
        metrics = results[split]
        assert set(metrics) == {
            "mae", "rmse", "r2", "mape", "medae", "price_band_accuracy", "property_type_accuracy",
        }
        assert metrics["mae"] > 0
        assert metrics["rmse"] >= metrics["mae"]
    assert results["val"]["r2"] > 0.5


def test_run_training_saves_model_when_requested(tmp_path):
    df = _make_dataset(n=300, seed=2)
    model_out = tmp_path / "hpp_rf.joblib"
    args = _make_args(device="cpu", n_estimators=10, n_jobs=1, model_out=model_out)
    hpp_rf.run_training(df, args)

    import joblib

    assert model_out.exists()
    bundle = joblib.load(model_out)
    assert set(bundle) == {
        "model",
        "encoder",
        "lot_medians",
        "postcode_stats",
        "sector_stats",
        "price_stat_globals",
        "feature_columns",
        "missing_flag_columns",
        "categorical_columns",
        "log_target",
        "sample_weight",
        "population",
        "train_period",
        "use_ofsted",
    }
    assert bundle["use_ofsted"] is True and "ofsted_missing" in bundle["missing_flag_columns"]
    # The saved artefact scores the rows it was trained on without refitting.
    # The test trained this pickle itself, so it may load it (trust_pickle).
    prices = hpp_rf.predict_with_saved_model(hpp_rf.load_model(model_out, trust_pickle=True), df)
    assert prices.shape == (len(df),) and np.all(prices > 0)




def test_set_global_seed_makes_numpy_and_random_deterministic():
    import random

    hpp_rf.set_global_seed(123)
    first = (np.random.rand(), random.random())
    hpp_rf.set_global_seed(123)
    second = (np.random.rand(), random.random())
    assert first == second




def test_price_band_assigns_expected_labels():
    prices = np.array([50_000, 300_000, 599_999, 1_000_000, 2_500_000])
    bands = hpp_rf.price_band(prices)
    assert list(bands) == ["£0-300k", "£300-600k", "£300-600k", "£1-2m", "£2m+"]


def test_compute_metrics_by_price_band_covers_every_label_and_perfect_band_is_zero():
    y_true = np.array([100_000.0, 100_000.0, 2_500_000.0])
    y_pred = np.array([100_000.0, 100_000.0, 2_000_000.0])
    report = hpp_rf.compute_metrics_by_price_band(y_true, y_pred)
    assert set(report) == set(hpp_rf.PRICE_BAND_LABELS)
    assert report["£0-300k"]["n"] == 2
    assert report["£0-300k"]["mae"] == pytest.approx(0.0)
    assert report["£2m+"]["n"] == 1
    assert report["£2m+"]["mae"] == pytest.approx(500_000.0)
    assert report["£600k-1m"]["n"] == 0
    assert report["£600k-1m"]["mae"] is None




def _multi_year_args(**overrides):
    args = _make_args(device="cpu", n_estimators=10, max_depth=6, n_jobs=1, random_state=0)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_reserve_final_holdout_period_splits_on_calendar_months():
    df = hpp_rf.engineer_features(_make_dataset(n=800, seed=2, start="2015-01-01", periods_days=3650))
    development_df, holdout_df = hpp_rf.reserve_final_holdout_period(df, months=12)
    assert len(development_df) + len(holdout_df) == len(df)
    cutoff = development_df[hpp_rf.DATE_COLUMN].max()
    assert holdout_df[hpp_rf.DATE_COLUMN].min() > cutoff
    span_days = (holdout_df[hpp_rf.DATE_COLUMN].max() - holdout_df[hpp_rf.DATE_COLUMN].min()).days
    assert span_days <= 366


def test_reserve_final_holdout_period_rejects_non_positive_months():
    df = hpp_rf.engineer_features(_make_dataset(n=100))
    with pytest.raises(ValueError):
        hpp_rf.reserve_final_holdout_period(df, months=0)


def test_expanding_window_year_folds_are_strictly_expanding_and_ordered():
    df = hpp_rf.engineer_features(_make_dataset(n=1500, seed=3, start="2015-01-01", periods_days=3650))
    development_df, _ = hpp_rf.reserve_final_holdout_period(df, months=12)
    folds = hpp_rf.expanding_window_year_folds(development_df)
    assert len(folds) >= 2
    prev_year, prev_train_len = None, 0
    for test_year, train_fold, test_fold in folds:
        assert train_fold[hpp_rf.DATE_COLUMN].max() < test_fold[hpp_rf.DATE_COLUMN].min()
        assert (test_fold["sale_year"] == test_year).all()
        assert len(train_fold) > prev_train_len  
        if prev_year is not None:
            assert test_year > prev_year
        prev_year, prev_train_len = test_year, len(train_fold)


def test_aggregate_fold_metrics_weighted_mean_and_variability():
    fold_results = [
        {"n_test": 100, "test": {"mae": 10.0, "rmse": 20.0, "mape": 0.1, "medae": 5.0}},
        {"n_test": 300, "test": {"mae": 30.0, "rmse": 40.0, "mape": 0.3, "medae": 15.0}},
    ]
    aggregate = hpp_rf.aggregate_fold_metrics(fold_results, split_name="test")
    expected_mae = (100 * 10.0 + 300 * 30.0) / 400
    assert aggregate["mae"]["weighted_mean"] == pytest.approx(expected_mae)
    assert aggregate["mae"]["std"] == pytest.approx(np.std([10.0, 30.0]))
    assert aggregate["mae"]["min"] == pytest.approx(10.0)
    assert aggregate["mae"]["max"] == pytest.approx(30.0)


def test_backtest_expanding_window_reports_folds_aggregate_and_holdout():
    df = _make_dataset(n=1500, seed=4, start="2015-01-01", periods_days=3650)
    args = _multi_year_args()
    results = hpp_rf.backtest_expanding_window(df, args, holdout_months=12)

    assert len(results["folds"]) >= 2
    prev_test_year = None
    for fold in results["folds"]:
        assert set(fold) >= {"test_year", "val", "test", "n_train", "n_val", "n_test"}
        assert "by_price_band" in fold["test"]
        assert set(fold["test"]["by_price_band"]) == set(hpp_rf.PRICE_BAND_LABELS)
        if prev_test_year is not None:
            assert fold["test_year"] > prev_test_year
        prev_test_year = fold["test_year"]

    for metric in hpp_rf.BACKTEST_METRIC_NAMES:
        assert set(results["aggregate"][metric]) == {"weighted_mean", "std", "min", "max"}

    assert results["final_holdout"]["n_test"] > 0
    assert "by_price_band" in results["final_holdout"]["test"]


def test_backtest_expanding_window_holdout_never_appears_in_any_fold():
    """Check no fold's test rows fall in the final holdout period."""
    df = _make_dataset(n=1500, seed=6, start="2015-01-01", periods_days=3650)
    args = _multi_year_args()
    results = hpp_rf.backtest_expanding_window(df, args, holdout_months=12)

    df_engineered = hpp_rf.engineer_features(df).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    development_df, holdout_df = hpp_rf.reserve_final_holdout_period(df_engineered, months=12)
    assert development_df[hpp_rf.DATE_COLUMN].max() < holdout_df[hpp_rf.DATE_COLUMN].min()
    max_fold_test_year = max(fold["test_year"] for fold in results["folds"])
    assert max_fold_test_year <= int(development_df["sale_year"].max())




def test_spatial_new_area_holdout_removes_areas_from_training_entirely():
    df = hpp_rf.engineer_features(_make_dataset(n=800, seed=5, start="2015-01-01", periods_days=3650))
    train_df, _, test_df = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    train_out, known_test, new_test, holdout_areas = hpp_rf.spatial_new_area_holdout(
        train_df, test_df, group_col="postcode_sector", new_area_frac=0.2, random_state=0
    )
    assert len(holdout_areas) > 0
    assert not (set(train_out["postcode_sector"].unique()) & holdout_areas)
    assert set(new_test["postcode_sector"].unique()) <= (
        holdout_areas | (set(test_df["postcode_sector"].unique()) - set(train_out["postcode_sector"].unique()))
    )
    assert len(known_test) + len(new_test) == len(test_df)
    assert not (set(known_test.index) & set(new_test.index))


def test_evaluate_spatial_generalisation_new_area_has_no_training_exposure():
    df = _make_dataset(n=1500, seed=7, start="2015-01-01", periods_days=3650)
    args = _multi_year_args()
    results = hpp_rf.evaluate_spatial_generalisation(
        df, args, group_col="postcode_sector", new_area_frac=0.3, holdout_months=12
    )
    assert results["group_col"] == "postcode_sector"
    assert len(results["holdout_areas"]) > 0
    assert "known_area" in results and "new_area" in results
    assert "by_price_band" in results["known_area"] and "by_price_band" in results["new_area"]
    assert results["n_val"] > 0 and results["n_test"] > 0




def test_residual_slice_report_covers_requested_and_default_slices():
    df = hpp_rf.engineer_features(_make_dataset(n=500, seed=8))
    y_true = df["price"].to_numpy()
    rng = np.random.default_rng(0)
    y_pred = y_true + rng.normal(0, 20_000, size=len(y_true))
    report = hpp_rf.residual_slice_report(df, y_true, y_pred)
    assert set(report) == {"postcode_district", "propertytype", "sale_year", "floor_area_band", "price_band"}
    for col_report in report.values():
        assert col_report  
        for stats in col_report.values():
            assert set(stats) == {"n", "mean_residual", "median_abs_error", "mape"}
            assert stats["n"] > 0


def test_fit_high_value_expert_returns_none_below_min_rows():
    df = hpp_rf.engineer_features(_make_dataset(n=200, seed=9))
    train_df, val_df, test_df = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)
    X_train, _, _, _, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    y_train_raw = train_df["price"].to_numpy()
    args = _make_args(device="cpu", n_estimators=10, n_jobs=1, random_state=0)
    model = hpp_rf.fit_high_value_expert(X_train, y_train_raw, y_train_raw, args, high_value_threshold=50_000_000.0)
    assert model is None


def test_evaluate_two_stage_mixture_reports_general_and_mixture_metrics():
    df = _make_dataset(n=1500, seed=10, start="2015-01-01", periods_days=3650)
    args = _multi_year_args()
    df_engineered = hpp_rf.engineer_features(df).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    train_df, val_df, test_df = hpp_rf.time_series_split(df_engineered, args.train_frac, args.val_frac)

    results = hpp_rf.evaluate_two_stage_mixture(train_df, val_df, test_df, args, high_value_threshold=400_000.0)
    assert results["high_value_train_rows"] > 0
    for split_name in ("val", "test"):
        for model_name in ("general", "mixture"):
            metrics = results[split_name][model_name]
            assert set(metrics) >= {"mae", "rmse", "r2", "mape", "medae", "by_price_band"}


def test_evaluate_two_stage_mixture_falls_back_to_general_when_no_expert_fitted():
    df = _make_dataset(n=600, seed=11)
    args = _make_args(device="cpu", n_estimators=10, n_jobs=1, random_state=0)
    df_engineered = hpp_rf.engineer_features(df).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    train_df, val_df, test_df = hpp_rf.time_series_split(df_engineered, args.train_frac, args.val_frac)

    results = hpp_rf.evaluate_two_stage_mixture(train_df, val_df, test_df, args, high_value_threshold=50_000_000.0)
    assert results["val"]["general"]["mae"] == pytest.approx(results["val"]["mixture"]["mae"])




def _load_production_sample(engine, limit=20_000, log=None):
    """
    Return the newest `limit` rows of ``production.london_houses`` as pandas.

    The newest rows are used because the oldest predate the EPC register and
    their all-NULL EPC columns type as object. Rows with a NULL
    ``current_energy_rating`` are excluded as a workaround: `hpp_rf` does not
    treat that column as nullable, so encoding would fail. Drop the filter
    once it is backfilled or treated as nullable.
    """
    sql = (
        f"SELECT * FROM {hpp_rf.VIEW_NAME} "
        "WHERE current_energy_rating IS NOT NULL "
        f"ORDER BY {hpp_rf.DATE_COLUMN} DESC, ppd_id DESC LIMIT {int(limit)}"
    )
    df = pd.read_sql(sql, engine)
    df = df.sort_values([hpp_rf.DATE_COLUMN, "ppd_id"]).reset_index(drop=True)
    if log is not None:
        log.info(f"Loaded {len(df)} rows from {hpp_rf.VIEW_NAME}")
    return df


def test_run_training_against_production_london_houses(hpdbldn_ro):
    df = _load_production_sample(hpdbldn_ro, limit=20_000)
    # A view before migration 0034 has no Ofsted columns.
    use_ofsted = set(hpp_rf.OFSTED_COLUMNS) <= set(df.columns)
    args = _make_args(device="cpu", n_estimators=20, max_depth=10, n_jobs=1, use_ofsted=use_ofsted)
    results = hpp_rf.run_training(df, args)
    assert results["n_train"] + results["n_val"] + results["n_test"] == len(df)
    assert results["val"]["mae"] > 0


def test_sample_weight_defaults_to_unweighted_and_is_shared_with_hpp_cb():
    """Check the default sample weight is uniform and the implementation is shared with `hpp_cb`."""
    from hpldn.backend.scripts import hpp_cb

    assert hpp_rf.build_arg_parser().parse_args([]).sample_weight == "none"
    assert hpp_cb.build_arg_parser().parse_args([]).sample_weight == "none"
    assert hpp_cb._sample_weight_for_training is hpp_rf.sample_weight_for_training

    prices = np.array([150_000.0, 250_000.0, 450_000.0, 900_000.0, 3_000_000.0])
    none_weight = hpp_rf.sample_weight_for_training(prices, mode="none")
    assert np.array_equal(none_weight, np.ones(len(prices)))

    full_weight = hpp_rf.sample_weight_for_training(prices, mode="full")
    sqrt_weight = hpp_rf.sample_weight_for_training(prices, mode="sqrt")
    assert np.allclose(full_weight, hpp_rf.compute_price_band_sample_weight(prices))
    assert full_weight.mean() == pytest.approx(1.0)
    assert sqrt_weight.mean() == pytest.approx(1.0)
    assert full_weight.max() / full_weight.min() > sqrt_weight.max() / sqrt_weight.min()

    with pytest.raises(ValueError, match="Unknown sample-weight mode"):
        hpp_rf.sample_weight_for_training(prices, mode="inverse")


def test_fit_and_evaluate_honours_sample_weight_mode(monkeypatch):
    """Check ``--sample-weight`` reaches the forest's fit, uniform by default."""
    seen = {}
    original_fit = RandomForestRegressor.fit

    def _recording_fit(self, X, y, sample_weight=None):
        seen["sample_weight"] = sample_weight
        return original_fit(self, X, y, sample_weight=sample_weight)

    monkeypatch.setattr(RandomForestRegressor, "fit", _recording_fit)

    df = hpp_rf.engineer_features(_make_dataset(n=300, seed=7))
    train_df, val_df, test_df = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)

    args = _make_args(device="cpu", n_estimators=5, max_depth=4, n_jobs=1)
    results = hpp_rf._fit_and_evaluate(train_df, val_df, test_df, args)[-1]
    assert results["sample_weight"] == "none"
    assert np.array_equal(seen["sample_weight"], np.ones(len(train_df)))

    weighted_args = _make_args(device="cpu", n_estimators=5, max_depth=4, n_jobs=1, sample_weight="full")
    hpp_rf._fit_and_evaluate(train_df, val_df, test_df, weighted_args)
    assert np.allclose(
        seen["sample_weight"],
        hpp_rf.compute_price_band_sample_weight(train_df[hpp_rf.TARGET_COLUMN].to_numpy(dtype=float)),
    )


def test_feature_matrix_columns_describe_the_matrix_that_is_actually_built():
    """
    Check `FEATURE_MATRIX_COLUMNS` describes the matrix actually built.

    Each flag column must be its group's missing indicator at the named index.
    """
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300, seed=8), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, _, feature_metadata = hpp_rf.impute_and_encode(train_df, val_df, test_df)

    assert list(hpp_rf.MISSING_FLAG_GROUPS) == hpp_rf.MISSING_FLAG_COLUMNS
    assert hpp_rf.FEATURE_MATRIX_COLUMNS[: len(hpp_rf.FEATURE_COLUMNS)] == list(hpp_rf.FEATURE_COLUMNS)
    assert len(hpp_rf.FEATURE_MATRIX_COLUMNS) == len(hpp_rf.FEATURE_COLUMNS) + len(hpp_rf.MISSING_FLAG_GROUPS)
    assert X_train.shape[1] == len(hpp_rf.FEATURE_MATRIX_COLUMNS)
    assert feature_metadata["feature_columns"] == list(hpp_rf.FEATURE_MATRIX_COLUMNS)
    assert feature_metadata["missing_flag_columns"] == hpp_rf.MISSING_FLAG_COLUMNS

    for flag_name, cols in hpp_rf.MISSING_FLAG_GROUPS.items():
        idx = hpp_rf.FEATURE_MATRIX_COLUMNS.index(flag_name)
        expected = train_df[cols[0]].isna().to_numpy().astype(np.float64)
        assert np.array_equal(X_train[:, idx], expected), flag_name


def test_run_training_saves_the_full_feature_matrix_layout(tmp_path):
    """Check a saved model records the full matrix layout, missing flags included."""
    import joblib

    model_out = tmp_path / "hpp_rf.joblib"
    args = _make_args(device="cpu", n_estimators=5, max_depth=4, n_jobs=1, model_out=model_out)
    results = hpp_rf.run_training(_make_dataset(n=300, seed=9), args)

    bundle = joblib.load(model_out)
    assert bundle["feature_columns"] == list(hpp_rf.FEATURE_MATRIX_COLUMNS)
    assert bundle["missing_flag_columns"] == hpp_rf.MISSING_FLAG_COLUMNS
    assert bundle["sample_weight"] == "none"
    assert results["feature_columns"] == list(hpp_rf.FEATURE_MATRIX_COLUMNS)
    assert bundle["model"].n_features_in_ == len(bundle["feature_columns"])



def _with_nulls_everywhere(df: pd.DataFrame, seed: int = 11, share: float = 0.2) -> pd.DataFrame:
    """Return `df` with a NULL share in every categorical and numeric feature column."""
    rng = np.random.default_rng(seed)
    df = df.copy()
    raw_features = [
        c for c in hpp_rf.CATEGORICAL_COLUMNS + hpp_rf.NUMERIC_COLUMNS
        if c in df.columns and c not in ("propertytype", "postcode_district")
    ]
    for col in raw_features + ["latitude", "longitude", "comp_median_price", "ptal_band_rank"]:
        mask = rng.random(len(df)) < share
        if pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].astype("float64")
            df.loc[mask, col] = np.nan
        else:
            df[col] = df[col].astype(object)
            df.loc[mask, col] = None
    return df


def test_null_categoricals_and_numerics_encode_without_error():
    """
    BLK-1: a NULL current_energy_rating once made sorting compare float with str.

    Every categorical and numeric feature carries NULLs here, as the real view does.
    """
    df = hpp_rf.engineer_features(_with_nulls_everywhere(_make_dataset(n=400)))
    for col in hpp_rf.CATEGORICAL_COLUMNS:
        assert not df[col].isna().any(), col
    train_df, val_df, test_df, _ = hpp_rf.time_series_split(df, 0.7, 0.15) + (None,)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)
    X_train, X_val, X_test, _, _, meta = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert X_train.shape[1] == len(meta["feature_columns"])


def test_categorical_categories_tolerate_a_stray_null():
    """Check the encoder vocabulary is built from strings even if a NULL slips through."""
    train = pd.DataFrame({col: ["A", "B"] for col in hpp_rf.CATEGORICAL_COLUMNS})
    train["current_energy_rating"] = ["C", None]
    train["price"] = [1.0, 2.0]
    categories = hpp_rf._categorical_encoder_categories(train, on_gpu=False)
    rating = categories[hpp_rf.CATEGORICAL_COLUMNS.index("current_energy_rating")]
    assert rating == ["C", "UNKNOWN"]


def test_load_coercion_types_an_all_null_numeric_column_as_float():
    """
    BLK-1: an all-NULL numeric column arrived as object None and broke ``np.log1p``.

    The declared view types decide a column no value can.
    """
    chunk = pd.DataFrame(
        {
            "total_floor_area": pd.Series([None, None], dtype=object),
            "current_energy_rating": pd.Series([None, "C"], dtype=object),
            "price": pd.Series([100_000, 200_000], dtype=object),
        }
    )
    kinds = hpp_rf.resolve_column_kinds(chunk)
    assert kinds == {"total_floor_area": "num", "current_energy_rating": "str", "price": "num"}
    hpp_rf._coerce_chunk_dtypes(chunk, kinds, "float64")
    assert chunk["total_floor_area"].dtype == np.float64
    assert np.isnan(np.log1p(chunk["total_floor_area"])).all()
    assert chunk["current_energy_rating"].isna().iloc[0]


def test_view_column_kinds_cover_every_view_column():
    """Check every ``production.london_houses`` column has a declared kind entry."""
    from hpldn.backend.migrations.london_houses_view import VIEW_COLUMNS

    assert set(hpp_rf.VIEW_COLUMN_KINDS) == set(VIEW_COLUMNS)
    assert hpp_rf.VIEW_COLUMN_KINDS["total_floor_area"] == "num"
    assert hpp_rf.VIEW_COLUMN_KINDS["current_energy_rating"] == "str"
    assert hpp_rf.VIEW_COLUMN_KINDS["is_maisonette"] is None


def test_dataset_sql_defaults_to_the_epc_matched_population():
    """A default run models sales with a pre-sale certificate."""
    assert "WHERE epc_matched" in hpp_rf.dataset_sql()
    assert "WHERE" not in hpp_rf.dataset_sql(population="all")
    assert hpp_rf.dataset_sql(limit=5).endswith("LIMIT 5")
    with pytest.raises(ValueError):
        hpp_rf.dataset_sql(population="everything")
    assert hpp_rf.build_arg_parser().parse_args([]).population == "epc_matched"


def test_core_feature_check_fails_a_split_without_floor_area():
    """A train split that mostly lacks floor area must fail, not report metrics."""
    df = hpp_rf.engineer_features(_make_dataset(n=300))
    train, val, test = hpp_rf.time_series_split(df, 0.7, 0.15)
    rates = hpp_rf.check_core_feature_availability({"train": train, "val": val, "test": test})
    assert rates["train"]["total_floor_area"] == 0.0

    train = train.copy()
    train["total_floor_area"] = np.nan
    train["current_energy_rating"] = "UNKNOWN"
    with pytest.raises(RuntimeError, match="train.total_floor_area 100.0%.*train.current_energy_rating"):
        hpp_rf.check_core_feature_availability({"train": train, "val": val, "test": test})


def test_core_feature_check_lets_population_all_train_without_floor_area():
    """Under --population all, pre-certificate train rows are reported, not failed."""
    df = hpp_rf.engineer_features(_make_dataset(n=300))
    train, val, test = hpp_rf.time_series_split(df, 0.7, 0.15)
    train = train.copy()
    train["total_floor_area"] = np.nan
    train["current_energy_rating"] = "UNKNOWN"
    rates = hpp_rf.check_core_feature_availability(
        {"train": train, "val": val, "test": test}, population="all"
    )
    assert rates["train"]["total_floor_area"] == 1.0

    train["latitude"] = np.nan
    with pytest.raises(RuntimeError, match="train.latitude"):
        hpp_rf.check_core_feature_availability(
            {"train": train, "val": val, "test": test}, population="all"
        )


def test_core_feature_check_still_fails_a_scored_split_without_floor_area_under_population_all():
    """The saved model must not be scored on splits that lack floor area."""
    df = hpp_rf.engineer_features(_make_dataset(n=300))
    train, val, test = hpp_rf.time_series_split(df, 0.7, 0.15)
    train, test = train.copy(), pd.concat([test] * 4).copy()
    train["total_floor_area"] = np.nan
    test["total_floor_area"] = np.nan
    splits = {"train": train, "val": val, "test": test}
    with pytest.raises(RuntimeError, match="test.total_floor_area"):
        hpp_rf.check_core_feature_availability(splits, population="all")
    # A walk-forward fold's validation block may predate certificates.
    rates = hpp_rf.check_core_feature_availability(splits, population="all", is_final_fit=False)
    assert rates["test"]["total_floor_area"] == 1.0
    assert set(hpp_rf.CORE_FEATURES) == set(hpp_rf.EPC_CORE_FEATURES) | set(hpp_rf.LOCATION_CORE_FEATURES)


def test_run_training_reports_core_feature_missing_rates():
    df = _make_dataset(n=500, seed=4)
    args = _make_args(device="cpu", n_estimators=5, n_jobs=1)
    results = hpp_rf.run_training(df, args)
    assert set(results["core_feature_missing_rates"]) == {"train", "val", "test"}


@pytest.mark.parametrize(
    "street, key",
    [
        ("LAUSANNE ROAD", "LAUSANNE ROAD"),
        ("Lausanne Road", "LAUSANNE ROAD"),
        ("13 The Gardens", "THE GARDENS"),
        ("13A, The Gardens", "THE GARDENS"),
        ("Flat 3, 13 The Gardens", "THE GARDENS"),
        ("flat B  high   street", "HIGH STREET"),
        ("1ST AVENUE", "1ST AVENUE"),
        ("100-102 Sutherland Avenue", "SUTHERLAND AVENUE"),
        ("\xa0DRYLANDS\xa0ROAD", "DRYLANDS ROAD"),
        ("08, Great West Road", "GREAT WEST ROAD"),
        ("  ", None),
        ("12", None),
        (None, None),
        (float("nan"), None),
    ],
)
def test_normalise_street_key(street, key):
    assert hpp_rf.normalise_street_key(street) == key


def test_same_street_features_group_mixed_case_and_numbered_streets():
    """The 2024+ back-fill's EPC-format streets join the Land Registry spelling's group."""
    df = _make_dataset(n=4, seed=2)
    df["postcode_district"] = "SW11"
    df["poststreet"] = ["LAUSANNE ROAD", "Lausanne Road", "7 Lausanne Road", "Flat 2, 9 LAUSANNE ROAD"]
    df["dateoftransfer"] = pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01"])
    out = hpp_rf.add_same_street_features(df)
    assert out["street_sales_count_12m"].tolist() == [0.0, 1.0, 2.0, 3.0]

    stored = df.assign(street_key=["A", "A", None, "B"])
    out = hpp_rf.add_same_street_features(stored)
    # The view's column wins; a NULL falls back to the street itself.
    assert out["street_sales_count_12m"].tolist() == [0.0, 1.0, 0.0, 0.0]


def _ukhpi_table():
    return pd.DataFrame(
        {
            "period": pd.to_datetime(["2010-01-01", "2010-02-01", "2010-03-01"]),
            "published_date": pd.to_datetime(["2010-03-15", "2010-04-15", "2010-04-15"]),
            "avg_price_all_property_types": [300_000.0, 310_000.0, 320_000.0],
            "avg_price_detached": [900_000.0, 910_000.0, 920_000.0],
            "avg_price_semi_detached": [600_000.0, 610_000.0, 620_000.0],
            "avg_price_terraced": [500_000.0, 510_000.0, np.nan],
            "avg_price_flats_maisonettes": [400_000.0, 410_000.0, 420_000.0],
        }
    )


def test_ukhpi_as_of_follows_the_views_published_date_rule():
    dates = pd.to_datetime(["2010-03-01", "2010-03-15", "2010-04-20", "2010-04-20", "2010-04-20", None])
    got = hpp_rf.ukhpi_as_of(dates, ["F", "F", "F", "T", "X", "F"], _ukhpi_table())
    # Before the first publication; on it; after a tie on published_date (the
    # later period wins); a NULL terraced figure falls back to all types.
    expected = [np.nan, 400_000.0, 420_000.0, 320_000.0, 320_000.0, np.nan]
    np.testing.assert_allclose(got, expected)


def test_add_market_relative_features_are_log_ratios_to_the_market():
    df = _make_dataset(n=50, seed=3)
    df["prev_sale_date"] = pd.to_datetime("2010-03-20")
    df.loc[df.index[:5], "prev_sale_price"] = np.nan
    out = hpp_rf.add_market_relative_features(hpp_rf.engineer_features(df), _ukhpi_table())
    assert set(hpp_rf.MARKET_RELATIVE_COLUMNS) <= set(out.columns)
    market = out["ukhpi_avg_price_at_sale"].to_numpy()
    np.testing.assert_allclose(
        out["rel_comp_median_price"], np.log(out["comp_median_price"]) - np.log(market), rtol=1e-6
    )
    implied = out["comp_median_price_per_sqm"] * out["total_floor_area"]
    np.testing.assert_allclose(
        out["rel_comp_implied_value"], np.log(implied) - np.log(market), rtol=1e-6
    )
    at_prev = hpp_rf.ukhpi_as_of(out["prev_sale_date"], out["propertytype"], _ukhpi_table())
    np.testing.assert_allclose(
        out["prev_sale_deflated"], np.log(out["prev_sale_price"]) - np.log(at_prev), rtol=1e-6
    )
    np.testing.assert_allclose(
        out["prev_sale_indexed_price"], out["prev_sale_price"] * market / at_prev, rtol=1e-6
    )
    assert out["prev_sale_deflated"].iloc[:5].isna().all()

    # hpp_inference supplies the index at the previous sale on the row instead.
    carried = hpp_rf.add_market_relative_features(out.drop(columns=["prev_sale_date"]))
    np.testing.assert_allclose(carried["prev_sale_deflated"], out["prev_sale_deflated"])


def test_group_price_stats_on_the_deflated_scale():
    """Each sale's price is divided by the market at its own sale before pooling."""
    df = pd.DataFrame(
        {
            "postcode_district": ["A", "A", "B"],
            "price": [100_000.0, 400_000.0, 300_000.0],
            "total_floor_area": [50.0, 100.0, 100.0],
            "ukhpi_avg_price_at_sale": [100_000.0, 200_000.0, 300_000.0],
        }
    )
    stats = hpp_rf._group_price_stats(
        df, "postcode_district", tuple(hpp_rf.POSTCODE_STAT_COLUMNS), deflator_col="ukhpi_avg_price_at_sale"
    )
    assert stats.loc["A", "postcode_median_price"] == pytest.approx(1.5)
    assert stats.loc["A", "postcode_median_price_per_sqm"] == pytest.approx(0.02)
    assert stats.loc["B", "postcode_txn_count"] == 1
    nominal = hpp_rf._group_price_stats(df, "postcode_district", tuple(hpp_rf.POSTCODE_STAT_COLUMNS))
    assert nominal.loc["A", "postcode_median_price"] == pytest.approx(250_000.0)
    assert hpp_rf.group_price_fallbacks(df, "ukhpi_avg_price_at_sale") == pytest.approx((1.0, 0.02))


def test_street_key_sql_embeds_the_python_patterns():
    """Migration 0025's SQL function must apply exactly `normalise_street_key`'s rule."""
    from hpldn.backend.migrations.london_houses_view import STREET_KEY_FUNCTION_SQL

    assert hpp_rf.STREET_KEY_PREFIX_PATTERN in STREET_KEY_FUNCTION_SQL
    assert f"'{hpp_rf.STREET_KEY_WHITESPACE_PATTERN}', ' ', 'g'" in STREET_KEY_FUNCTION_SQL
    assert "replace(street, chr(160), ' ')" in STREET_KEY_FUNCTION_SQL


def test_no_ofsted_leaves_the_ofsted_group_out_of_the_matrix():
    """``--no-ofsted`` drops the twelve ofsted_* columns and their flag; the default keeps them."""
    args = hpp_rf.build_arg_parser().parse_args(["--no-ofsted"])
    assert args.use_ofsted is False
    assert hpp_rf.build_arg_parser().parse_args([]).use_ofsted is True

    with_ofsted, without = hpp_rf.feature_layout(True), hpp_rf.feature_layout(False)
    added = set(with_ofsted["matrix_columns"]) - set(without["matrix_columns"])
    assert added == set(hpp_rf.OFSTED_COLUMNS) | {hpp_rf.OFSTED_MISSING_FLAG}
    assert hpp_rf.FEATURE_MATRIX_COLUMNS == with_ofsted["matrix_columns"]
    assert "ofsted_nearest_primary_basis" in with_ofsted["categorical"]

    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_on, *_ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    X_off, _, _, _, _, meta = hpp_rf.impute_and_encode(train_df, val_df, test_df, use_ofsted=False)
    assert X_on.shape[1] == len(with_ofsted["matrix_columns"])
    assert X_off.shape[1] == len(without["matrix_columns"]) == X_on.shape[1] - len(added)
    assert meta["use_ofsted"] is False and not any(c.startswith("ofsted") for c in meta["feature_columns"])
    # The flag marks rows without the features, which are median-filled.
    flag = X_on[:, with_ofsted["matrix_columns"].index("ofsted_missing")]
    assert np.array_equal(flag, train_df["ofsted_primary_nearest_m"].isna().to_numpy(dtype=float))
    col = X_on[:, with_ofsted["matrix_columns"].index("ofsted_primary_nearest_m")]
    assert not np.isnan(col).any()


def test_ofsted_on_without_the_columns_names_the_fix():
    """A frame from a view before 0034 fails with the migration and --no-ofsted named."""
    train_df, val_df, test_df, _ = _split_and_stat(
        _make_dataset(n=200).drop(columns=hpp_rf.OFSTED_COLUMNS), train_frac=0.7, val_frac=0.15
    )
    with pytest.raises(ValueError, match="--no-ofsted"):
        hpp_rf.impute_and_encode(train_df, val_df, test_df)
    hpp_rf.impute_and_encode(train_df, val_df, test_df, use_ofsted=False)


def test_saved_random_forest_without_the_flag_scores_without_ofsted(tmp_path):
    """An artefact saved before the Ofsted group (no ``use_ofsted``) still scores with its own layout."""
    import joblib

    df = _make_dataset(n=300, seed=4)
    model_out = tmp_path / "rf.joblib"
    hpp_rf.run_training(df, _make_args(n_estimators=5, model_out=model_out, use_ofsted=False))
    bundle = joblib.load(model_out)
    assert bundle["use_ofsted"] is False
    del bundle["use_ofsted"]
    prices = hpp_rf.predict_with_saved_model(bundle, df)
    assert prices.shape == (len(df),) and np.all(prices > 0)
