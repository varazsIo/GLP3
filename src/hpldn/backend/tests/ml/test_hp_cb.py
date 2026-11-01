"""
Tests for `hpldn.backend.scripts.hpp_cb`.

Reuses `test_hp_rf`'s synthetic dataset, since both models train on the same
columns and splits, and tests only what is CatBoost-specific: string
categoricals and train-median filling in `prepare_features`, task-type
resolution, end-to-end training for every split method, backtesting, the
two-pass refit and model saving. A final integration test uses the local
hpdbldn (`hpdbldn_ro`, read-only).
"""

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.scripts import hpp_cb, hpp_rf
from hpldn.backend.tests.ml.test_hp_rf import (
    _make_dataset,
    _make_mortgage_rates,
    _with_mortgage_features,
)


# _fit_and_evaluate reports compute_metrics' four headline numbers plus
# _bias_diagnostics' three signed-error diagnostics, _relative_medae's
# scale-free MedAE, MdAPE, the within-5/10/20 % shares and the row count
# (see hpp_cb._metrics_with_bias).
_EXPECTED_METRIC_NAMES = {
    "mae",
    "rmse",
    "r2",
    "mape",
    "medae",
    "medae_relative",
    "signed_mean_error",
    "signed_median_error",
    "pred_actual_ratio_median",
    "mdape",
    "within_5pct",
    "within_10pct",
    "within_20pct",
    "n",
}

# Nested reports beside the headline metrics: the test split always carries
# "scoped"; the rest depend on the protocol.
_NESTED_REPORTS = {"scoped", "by_segment", "by_price_band", "mixture", "plausibility_filtered"}


def _make_args(**overrides):
    args = hpp_cb.build_arg_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args




def _split_and_stat(df, **split_kwargs):
    """Run feature engineering, the split and the price statistics `prepare_features` expects."""
    df = _with_mortgage_features(hpp_rf.engineer_features(df))
    train_df, val_df, test_df = hpp_rf.time_series_split(df, **split_kwargs)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    return hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)


def test_prepare_features_keeps_categoricals_as_strings():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=200), train_frac=0.7, val_frac=0.15)
    X_train, X_val, X_test, cat_features, _, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert cat_features == hpp_rf.CATEGORICAL_COLUMNS + hpp_cb.IOD_CATEGORICAL_COLUMNS + hpp_rf.OFSTED_CATEGORICAL_COLUMNS
    for col in hpp_rf.CATEGORICAL_COLUMNS:
        assert pd.api.types.is_string_dtype(X_train[col])
        assert X_train[col].isna().sum() == 0


def test_prepare_features_unseen_category_kept_as_its_own_string():
    """Check an unseen category stays its own string rather than being remapped."""
    train_df, val_df, test_df, _ = _split_and_stat(
        _make_dataset(n=200, unseen_district_in_test=True), train_frac=0.7, val_frac=0.15
    )
    _, _, X_test, _, _, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert X_test["postcode_district"].iloc[-1] == "ZZ9"




def test_prepare_features_keeps_floor_level_as_string_and_carries_is_house():
    """Check ``floor_level`` stays a string categorical and ``is_house`` a numeric column."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, cat_features, _, _ = hpp_cb.prepare_features(train_df, val_df, test_df)

    assert "floor_level" in cat_features
    assert pd.api.types.is_string_dtype(X_train["floor_level"])
    assert X_train["floor_level"].isna().sum() == 0
    assert "UNKNOWN" in set(X_train["floor_level"].unique())

    assert "is_house" not in cat_features
    assert "is_house" in X_train.columns
    expected = (train_df["propertytype"] != "F").astype(np.int64)
    assert (X_train["is_house"].astype(np.int64).to_numpy() == expected.to_numpy()).all()


def test_prepare_features_fills_missing_lot_area_with_train_median():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert medians["lot_area_sqm"] == pytest.approx(train_df["lot_area_sqm"].median())

    missing_rows = train_df["lot_area_sqm"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing lot_area_sqm rows"
    assert np.allclose(X_train.loc[missing_rows, "lot_area_sqm"].to_numpy(), medians["lot_area_sqm"])
    assert np.array_equal(X_train["lot_area_missing"].to_numpy(), missing_rows.astype(np.float64))


def test_prepare_features_fills_missing_comps_with_train_median():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert medians["comp_median_price"] == pytest.approx(train_df["comp_median_price"].median())

    missing_rows = train_df["comp_median_price"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing comp_median_price rows"
    assert np.allclose(X_train.loc[missing_rows, "comp_median_price"].to_numpy(), medians["comp_median_price"])
    assert np.array_equal(X_train["comp_missing"].to_numpy(), missing_rows.astype(np.float64))




def test_prepare_features_includes_rolling_comp_columns():
    train_df, val_df, test_df, _ = _split_and_stat(
        _make_dataset(n=300, start="2010-01-01", periods_days=60), train_frac=0.7, val_frac=0.15
    )
    X_train, _, _, _, medians, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    for col in hpp_rf.ROLLING_COMP_STAT_COLUMNS:
        assert col in X_train.columns
        assert X_train[col].isna().sum() == 0

    dependent_col = hpp_rf.ROLLING_COMP_DEPENDENT_COLUMNS[0]
    assert dependent_col in medians
    assert medians[dependent_col] == pytest.approx(train_df[dependent_col].median())

    missing_rows = train_df[dependent_col].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing rolling comp rows"
    assert np.allclose(X_train.loc[missing_rows, dependent_col].to_numpy(), medians[dependent_col])
    assert np.array_equal(X_train["rolling_comp_missing"].to_numpy(), missing_rows.astype(np.float64))




def test_prepare_features_includes_transit_and_macro_columns_unimputed():
    """
    Check transit and macro columns pass through unchanged with no missing flag.

    ``boe_rate_at_sale`` is dropped by default (a function of the sale date)
    and asserted absent.
    """
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, _, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert "boe_rate_at_sale" not in X_train.columns
    flagged = {c for cols in hpp_cb._MISSING_FLAG_GROUPS.values() for c in cols}
    for col in hpp_rf.TRANSIT_STAT_COLUMNS + ["ukhpi_avg_price_at_sale"]:
        assert col in X_train.columns
        assert col not in flagged or col in hpp_cb.TRANSIT_DEPENDENT_COLUMNS
        assert X_train[col].isna().sum() == 0
        assert np.allclose(X_train[col].to_numpy(), train_df[col].to_numpy())
    c1_dropped = set(hpp_cb.C1_DEFAULT_DROPPED_COLUMNS)
    assert set(X_train.columns) - set(hpp_cb._MISSING_FLAG_GROUPS) == set(
        hpp_rf.CATEGORICAL_COLUMNS
        + hpp_rf.NUMERIC_COLUMNS
        + ["latitude", "longitude"]
        + hpp_rf.COMP_STAT_COLUMNS
        + hpp_rf.TRANSIT_STAT_COLUMNS
        + hpp_rf.MACRO_STAT_COLUMNS
        + hpp_rf.ENGINEERED_COLUMNS
        + hpp_rf.PTAL_DEPENDENT_COLUMNS
        + hpp_rf.RIVER_DEPENDENT_COLUMNS
        + hpp_rf.PREV_SALE_DEPENDENT_COLUMNS
        + hpp_rf.SAME_STREET_STAT_COLUMNS
        + hpp_rf.NEIGHBOURHOOD_DEPENDENT_COLUMNS
        + hpp_rf.MSOA_TREND_DEPENDENT_COLUMNS
        + hpp_rf.OFSTED_COLUMNS
        + hpp_cb.IOD_NUMERIC_COLUMNS
        + hpp_cb.IOD_ORDINAL_COLUMNS
        + hpp_cb.IOD_CATEGORICAL_COLUMNS
        + ["sale_month_sin", "sale_month_cos"]
    ) - c1_dropped - {"sale_month"}




def test_prepare_features_includes_distance_to_river_and_geography_codes():
    """Check ``distance_to_river`` is median-filled with a flag and LSOA/MSOA codes are string categoricals."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, cat_features, medians, _ = hpp_cb.prepare_features(train_df, val_df, test_df)

    assert "distance_to_river" in X_train.columns
    assert medians["distance_to_river"] == pytest.approx(train_df["distance_to_river"].median())
    missing_rows = train_df["distance_to_river"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing distance_to_river rows"
    assert np.allclose(
        X_train.loc[missing_rows, "distance_to_river"].to_numpy(), medians["distance_to_river"]
    )
    assert np.array_equal(X_train["river_missing"].to_numpy(), missing_rows.astype(np.float64))

    for col in ("lsoa_code", "msoa_code"):
        assert col in cat_features
        assert pd.api.types.is_string_dtype(X_train[col])
        assert X_train[col].isna().sum() == 0
        assert (X_train[col] == "UNKNOWN").any()




@pytest.fixture
def clear_task_type_cache():
    """
    Clear `hpp_cb._TASK_TYPE_CACHE` around a task-type test.

    Resolution is memoised per process, so an earlier caller on a GPU host
    would otherwise decide the result before a test's monkeypatch applies.
    """
    saved = dict(hpp_cb._TASK_TYPE_CACHE)
    hpp_cb._TASK_TYPE_CACHE.clear()
    yield
    hpp_cb._TASK_TYPE_CACHE.clear()
    hpp_cb._TASK_TYPE_CACHE.update(saved)


def test_resolve_task_type_cpu(clear_task_type_cache):
    use_gpu, resources, reason = hpp_cb.resolve_task_type("cpu")
    assert use_gpu is False
    assert reason == "requested"


def test_resolve_task_type_gpu_raises_without_cuda_gpu(monkeypatch, clear_task_type_cache):
    class _NoGpu:
        has_cuda_gpu = False

    monkeypatch.setattr(hpp_cb, "detect_host_resources", lambda log=None: _NoGpu())
    with pytest.raises(RuntimeError, match="no CUDA-capable GPU"):
        hpp_cb.resolve_task_type("gpu")


def test_resolve_task_type_auto_falls_back_to_cpu_without_cuda_gpu(
    monkeypatch, clear_task_type_cache
):
    class _NoGpu:
        has_cuda_gpu = False

    monkeypatch.setattr(hpp_cb, "detect_host_resources", lambda log=None: _NoGpu())
    use_gpu, resources, reason = hpp_cb.resolve_task_type("auto")
    assert use_gpu is False
    assert reason == "no CUDA GPU detected"


def test_resolve_task_type_rejects_unknown_device(clear_task_type_cache):
    with pytest.raises(ValueError):
        hpp_cb.resolve_task_type("tpu")




def test_run_training_end_to_end_on_synthetic_data():
    df = _make_dataset(n=600, seed=1)
    args = _make_args(device="cpu", iterations=200, early_stopping_rounds=50, thread_count=1, random_state=0)
    results = hpp_cb.run_training(df, args, _make_mortgage_rates())

    assert results["n_train"] + results["n_val"] + results["n_test"] == len(df)
    for split in ("val", "test"):
        metrics = results[split]
        assert set(metrics) - _NESTED_REPORTS == _EXPECTED_METRIC_NAMES
        assert metrics["mae"] > 0
        assert metrics["rmse"] >= metrics["mae"]
    assert results["val"]["r2"] > 0.5


@pytest.mark.parametrize(
    "split_method", ["chronological", "grouped", "purged", "walk_forward", "spatial", "recency"]
)
def test_run_training_end_to_end_for_every_split_method(split_method):
    """Check every ``--split-method`` trains to completion with well-formed metrics."""
    df = _make_dataset(n=600, seed=3)
    args = _make_args(
        device="cpu",
        iterations=15,
        early_stopping_rounds=5,
        thread_count=1,
        random_state=0,
        split_method=split_method,
        cv_folds=2,
    )
    results = hpp_cb.run_training(df, args, _make_mortgage_rates())
    assert results["n_train"] > 0 and results["n_val"] > 0 and results["n_test"] > 0
    for split in ("val", "test"):
        assert set(results[split]) - _NESTED_REPORTS == _EXPECTED_METRIC_NAMES
        assert results[split]["mae"] > 0


def test_run_training_saves_model_when_requested(tmp_path):
    df = _make_dataset(n=300, seed=2)
    model_out = tmp_path / "hpp_cb"
    args = _make_args(device="cpu", iterations=10, early_stopping_rounds=5, thread_count=1, model_out=model_out)
    hpp_cb.run_training(df, args, _make_mortgage_rates())

    from hpldn.backend.models import artifact

    # A signed bundle of data only: no pickle anywhere.
    verified = artifact.verify(model_out)
    assert verified.key_id is not None
    assert not [p for p in model_out.iterdir() if p.suffix in (".joblib", ".pkl")]
    assert verified.manifest["training_args"]["iterations"] == 10
    assert verified.manifest["dataset"]["row_count"] > 0
    model, meta = hpp_cb.load_model(model_out)
    assert model.is_fitted()
    bundle = verified.json[artifact.META_FILE]
    assert set(bundle) == {
        "lot_medians",
        "postcode_stats",
        "sector_stats",
        "price_stat_globals",
        "group_stats_scale",
        "feature_columns",
        "cat_features",
        "dropped_flags",
        "missing_flag_groups",
        "nan_mode",
        "drop_features",
        "categorical_columns",
        "use_iod",
        "use_ofsted",
        "log_target",
        "target",
        "anchor_columns",
        "smearing_factors",
        "high_value_expert",
        "high_value_threshold",
        "population",
        "train_period",
        "database",
    }




def test_backtest_expanding_window_reports_folds_and_holdout():
    """
    Check a backtest reports folds and a final holdout on synthetic data.

    ``--allow-truncated-holdout`` permits the deliberately tiny fit, and a low
    ``--backtest-min-train-rows`` stops every fold of the small fixture being
    skipped.
    """
    df = _make_dataset(n=1500, seed=9, start="2015-01-01", periods_days=3650)
    args = _make_args(
        device="cpu",
        iterations=15,
        early_stopping_rounds=5,
        thread_count=1,
        random_state=0,
        allow_truncated_holdout=True,
        backtest_min_train_rows=50,
    )
    results = hpp_cb.backtest_expanding_window(
        df, args, _make_mortgage_rates(), holdout_months=12
    )

    assert len(results["folds"]) >= 2
    prev_test_year = None
    for fold in results["folds"]:
        assert set(fold) >= {"test_year", "test", "n_train", "n_val", "n_test"}
        assert "by_price_band" in fold["test"]
        if prev_test_year is not None:
            assert fold["test_year"] > prev_test_year
        prev_test_year = fold["test_year"]

    for metric in tuple(hpp_rf.BACKTEST_METRIC_NAMES) + hpp_cb.EXTRA_BACKTEST_METRIC_NAMES:
        assert set(results["aggregate"][metric]) == {"weighted_mean", "std", "min", "max"}

    assert results["final_holdout"]["n_test"] > 0
    assert "by_price_band" in results["final_holdout"]["test"]


def test_evaluate_spatial_generalisation_reports_known_and_new_area():
    df = _make_dataset(n=1500, seed=9, start="2015-01-01", periods_days=3650)
    args = _make_args(device="cpu", iterations=15, early_stopping_rounds=5, thread_count=1, random_state=0)
    results = hpp_cb.evaluate_spatial_generalisation(
        df,
        args,
        _make_mortgage_rates(),
        group_col="postcode_sector",
        new_area_frac=0.3,
        holdout_months=12,
    )

    assert results["group_col"] == "postcode_sector"
    assert len(results["holdout_areas"]) > 0
    assert "known_area" in results and "new_area" in results
    assert "by_price_band" in results["known_area"] and "by_price_band" in results["new_area"]
    assert results["n_val"] > 0 and results["n_test"] > 0




def test_run_training_against_production_london_houses(hpdbldn_ro):
    """
    Check training against ``production.london_houses`` on hpdbldn.

    Training-row filters mean the three splits sum to at most the loaded rows.
    """
    engine = hpdbldn_ro
    df = hpp_cb.load_dataset(engine, limit=20_000)
    # A view before migration 0034 has no Ofsted columns.
    use_ofsted = set(hpp_rf.OFSTED_COLUMNS) <= set(df.columns)
    args = _make_args(device="cpu", iterations=30, early_stopping_rounds=10, thread_count=1, use_ofsted=use_ofsted)
    results = hpp_cb.run_training(df, args, hpp_cb.load_mortgage_rates(engine))
    assert 0 < results["n_train"] <= len(df)
    assert results["n_val"] > 0 and results["n_test"] > 0
    assert results["n_train"] + results["n_val"] + results["n_test"] <= len(df)
    assert results["val"]["mae"] > 0


def _empty_mortgage_rates() -> pd.DataFrame:
    """Return an empty ``production.mortgage_rates`` frame for synthetic data."""
    return pd.DataFrame(
        {
            hpp_cb.MORTGAGE_RATE_DATE_COLUMN: pd.Series(dtype="datetime64[us]"),
            hpp_cb.MORTGAGE_RATE_COLUMN: pd.Series(dtype="float64"),
        }
    )


def test_refit_kwargs_pin_the_first_pass_learning_rate():
    """
    Check pass 2 reuses pass 1's realised learning rate.

    With ``--learning-rate`` unset CatBoost tunes the rate from the iteration
    cap, which pass 2 changes.
    """
    from catboost import CatBoostRegressor

    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, 4))
    y = X[:, 0] * 3.0 + rng.normal(scale=0.1, size=400)
    first_pass = CatBoostRegressor(
        iterations=200, learning_rate=None, depth=4, verbose=False, allow_writing_files=False
    )
    first_pass.fit(X, y)

    cb_kwargs = dict(
        iterations=200, learning_rate=None, depth=4, verbose=False,
        allow_writing_files=False, early_stopping_rounds=20,
    )
    refit_kwargs = hpp_cb._refit_kwargs_from_first_pass(cb_kwargs, first_pass, 42)

    assert "early_stopping_rounds" not in refit_kwargs
    assert refit_kwargs["iterations"] == 42
    assert refit_kwargs["learning_rate"] == first_pass.get_all_params()["learning_rate"]
    assert cb_kwargs["learning_rate"] is None, "must not mutate the caller's kwargs"


def test_refit_kwargs_keep_an_explicitly_requested_learning_rate():
    """Check an explicitly requested learning rate is kept."""
    from catboost import CatBoostRegressor

    rng = np.random.default_rng(1)
    X = rng.normal(size=(200, 3))
    y = X[:, 0] + rng.normal(scale=0.1, size=200)
    first_pass = CatBoostRegressor(
        iterations=30, learning_rate=0.123, depth=3, verbose=False, allow_writing_files=False
    )
    first_pass.fit(X, y)

    refit_kwargs = hpp_cb._refit_kwargs_from_first_pass(
        dict(iterations=30, learning_rate=0.123, depth=3), first_pass, 10
    )
    assert refit_kwargs["learning_rate"] == pytest.approx(0.123)


def test_fit_and_evaluate_refits_at_the_first_pass_learning_rate(monkeypatch):
    """Check the reported pass-2 model is fit at pass 1's realised learning rate."""
    from catboost import CatBoostRegressor

    fitted = []
    original_fit = CatBoostRegressor.fit

    def _recording_fit(self, *a, **kw):
        result = original_fit(self, *a, **kw)
        fitted.append(self)
        return result

    monkeypatch.setattr(CatBoostRegressor, "fit", _recording_fit)

    args = _make_args(
        device="cpu", iterations=60, early_stopping_rounds=10, thread_count=1,
        random_state=0, learning_rate=None, use_iod=False,
    )
    df = hpp_rf.engineer_features(_make_dataset(n=600, seed=5))
    df = df.sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    df = hpp_cb.add_mortgage_rate_features(df, _empty_mortgage_rates())
    train_df, val_df, test_df = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    hpp_cb._fit_and_evaluate(train_df, test_df, args, val_df=val_df)

    assert len(fitted) == 2, "expected the two-pass fit (pass 1 + refit)"
    first_pass, refit = fitted
    assert refit.get_all_params()["learning_rate"] == first_pass.get_all_params()["learning_rate"]



def test_availability_guard_raises_instead_of_dropping_a_core_feature():
    """Floor area 75% missing throughout train and 0.5% in test must fail the run."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    train_df = train_df.copy()
    blanked = train_df.index[np.arange(len(train_df)) % 4 != 0]
    train_df.loc[blanked, "total_floor_area"] = np.nan
    with pytest.raises(RuntimeError, match="core feature"):
        hpp_cb.prepare_features(train_df, val_df, test_df)


def test_availability_guard_measures_the_trailing_training_window():
    """
    Missingness caused by era alone does not drop a feature.

    A comparable 90 % NULL over the early train rows but present in the
    latest ones is kept; measured over the whole train set it was dropped.
    """
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=600), train_frac=0.7, val_frac=0.15)
    train_df = train_df.copy()
    early = train_df.index[: int(len(train_df) * 0.8)]
    train_df.loc[early, "comp_median_price_per_sqm"] = np.nan
    _, _, _, _, _, meta = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert "comp_median_price_per_sqm" not in meta["dropped_features"]
    _, _, _, _, _, whole = hpp_cb.prepare_features(
        train_df, val_df, test_df, availability_window_frac=None
    )
    assert whole["dropped_features"]["comp_median_price_per_sqm"]["reason"] == "availability_shift"

    window = hpp_cb._availability_window(train_df, 0.1)
    assert len(window) == int(hpp_cb._carve_eval_mask(len(train_df), frac=0.1).sum())
    assert window.index[-1] == train_df.index[-1]


def _without_early_certificates(df, share=0.6):
    """Blank the EPC fields of the earliest `share` of rows, like pre-2009 sales."""
    df = df.copy()
    early = df.index[: int(len(df) * share)]
    df.loc[early, ["total_floor_area", "number_habitable_rooms"]] = np.nan
    df.loc[early, "current_energy_rating"] = None
    return df


def test_population_all_keeps_the_epc_core_features_the_guard_would_drop():
    """Pre-certificate train rows shift floor-area missingness, which is not a reason to drop it."""
    train_df, val_df, test_df, _ = _split_and_stat(
        _without_early_certificates(_make_dataset(n=400), share=0.65), train_frac=0.7, val_frac=0.15
    )
    with pytest.raises(RuntimeError, match="core feature"):
        hpp_cb.prepare_features(train_df, val_df, test_df)
    X_train, X_val, X_test, _, _, meta = hpp_cb.prepare_features(
        train_df, val_df, test_df, population="all", nan_mode="native"
    )
    for column in hpp_rf.EPC_CORE_FEATURES:
        assert column in X_train.columns
        assert column not in meta["dropped_features"]
    assert list(X_train.columns) == list(X_test.columns)
    assert meta["epc_missing_flag"] is True


def test_population_all_final_fit_losing_floor_area_still_raises():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    train_df = train_df.copy()
    train_df["total_floor_area"] = np.nan
    train_df["current_energy_rating"] = "UNKNOWN"
    with pytest.raises(RuntimeError, match="core feature"):
        hpp_cb.prepare_features(train_df, val_df, test_df, population="all", nan_mode="native")

    X_train, *_, meta = hpp_cb.prepare_features(
        train_df, val_df, test_df, population="all", nan_mode="native", is_final_fit=False
    )
    assert "total_floor_area" not in X_train.columns
    assert meta["dropped_features"]["total_floor_area"]["reason"] == "unavailable_in_fold_train"


def test_epc_missing_survives_the_flag_guards_and_is_rebuilt_from_a_saved_model():
    """The indicator is constant within every pre-2009 year: the sale-year-proxy guard must not drop it."""
    train_df, val_df, test_df, _ = _split_and_stat(
        _without_early_certificates(_make_dataset(n=500), share=0.6), train_frac=0.7, val_frac=0.15
    )
    for nan_mode in ("native", "impute"):
        # The whole train set: a 50-row trailing window is too noisy for a 5 % limit.
        X_train, _, X_test, _, _, meta = hpp_cb.prepare_features(
            train_df, val_df, test_df, population="all", nan_mode=nan_mode, availability_shift_limit=0.05,
            availability_window_frac=None,
        )
        assert hpp_cb.EPC_MISSING_FLAG in X_train.columns, nan_mode
        assert hpp_cb.EPC_MISSING_FLAG not in meta["dropped_flags"]
        expected = (
            train_df["total_floor_area"].isna() | (train_df["current_energy_rating"] == "UNKNOWN")
        ).to_numpy(dtype=float)
        np.testing.assert_array_equal(X_train[hpp_cb.EPC_MISSING_FLAG].to_numpy(), expected)
        assert X_test[hpp_cb.EPC_MISSING_FLAG].sum() == 0

    explicit = test_df.copy()
    explicit[hpp_cb.EPC_MISSING_FLAG] = 1.0
    assert hpp_cb._epc_missing_indicator(explicit).all()
    assert not hpp_cb._epc_missing_indicator(test_df).any()
    clean = _split_and_stat(_make_dataset(n=500), train_frac=0.7, val_frac=0.15)
    _, _, X_default, _, _, meta = hpp_cb.prepare_features(*clean[:3], nan_mode="impute")
    assert hpp_cb.EPC_MISSING_FLAG not in X_default.columns and meta["epc_missing_flag"] is False


def test_nan_mode_defaults_to_native_under_population_all():
    parser = hpp_cb.build_arg_parser()
    assert hpp_cb.resolve_nan_mode(parser.parse_args(["--population", "all"])) == "native"
    assert hpp_cb.resolve_nan_mode(parser.parse_args([])) == "impute"
    assert hpp_cb.resolve_nan_mode(parser.parse_args(["--population", "all", "--nan-mode", "impute"])) == "impute"


def test_walk_forward_training_on_population_all_saves_and_reloads(tmp_path):
    """The bug-3 command's shape: walk-forward folds that predate certificates, two-stage, saved."""
    df = _without_early_certificates(_make_dataset(n=900, seed=31), share=0.45)
    model_out = tmp_path / "all"
    args = _make_args(
        device="cpu", iterations=20, early_stopping_rounds=5, thread_count=1, random_state=42,
        split_method="walk_forward", population="all", smearing_correction=False, two_stage=True,
        high_value_threshold=float(np.quantile(df["price"], 0.9)), model_out=model_out,
    )
    mortgage = _make_mortgage_rates()
    results = hpp_cb.run_training(df, args, mortgage)
    assert results["population"] == "all" and results["nan_mode"] == "native"
    assert not set(results["dropped_features"]) & set(hpp_rf.CORE_FEATURES)
    assert hpp_cb.EPC_MISSING_FLAG in results["feature_columns"]

    model, meta = hpp_cb.load_model(model_out)
    assert (meta["population"], meta["nan_mode"]) == ("all", "native")
    X, _, deflator = hpp_cb.prepare_features_from_saved_model(df, mortgage, meta)
    assert list(X.columns) == meta["feature_columns"]
    general, _ = hpp_cb.predict_with_adjustments(model, X, meta["cat_features"], meta["target"], deflator)
    assert np.isfinite(general).all()


def test_prepare_features_reports_dropped_features_with_rates():
    """Check a non-core feature the guard drops is reported with its reason and rates."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=400), train_frac=0.7, val_frac=0.15)
    train_df = train_df.copy()
    train_df["ptal_ai"] = np.nan
    train_df.loc[train_df.index[:5], "ptal_ai"] = 1.0
    *_, meta = hpp_cb.prepare_features(train_df, val_df, test_df)
    dropped = meta["dropped_features"]["ptal_ai"]
    assert dropped["reason"] == "availability_shift"
    assert dropped["train_missing_rate"] > 0.9
    assert dropped["scored_missing_rate"] < 0.5


def test_run_training_results_carry_dropped_features_and_core_rates():
    df = _make_dataset(n=500, seed=5)
    args = _make_args(device="cpu", iterations=15, early_stopping_rounds=5, thread_count=1, random_state=0)
    results = hpp_cb.run_training(df, args, _make_mortgage_rates())
    assert isinstance(results["dropped_features"], dict)
    assert not set(results["dropped_features"]) & set(hpp_rf.CORE_FEATURES)
    assert set(results["core_feature_missing_rates"]) == {"train", "val", "test"}
    assert hpp_cb.build_arg_parser().parse_args([]).population == "epc_matched"


def test_runtime_guards_are_not_asserts():
    """MAJ-8: the leakage and shape guards must survive ``python -O``."""
    import inspect

    assert "assert " not in inspect.getsource(hpp_cb)


def test_saved_model_scores_exactly_as_the_training_run_reported(tmp_path):
    """
    MAJ-9: a reloaded model must apply the run's smearing factors and high-value expert.

    Trains with both on, saves, reloads, and rebuilds the test rows' features
    from the saved state: the general and mixture metrics must match the
    training run's to 1e-6.
    """
    df = _make_dataset(n=700, seed=6)
    threshold = float(np.quantile(df["price"], 0.7))
    model_out = tmp_path / "hpp_cb"
    args = _make_args(
        device="cpu", iterations=40, early_stopping_rounds=10, thread_count=1, random_state=0,
        target="log_price", smearing_correction=True, two_stage=True,
        high_value_threshold=threshold, model_out=model_out,
    )
    mortgage = _make_mortgage_rates()
    results = hpp_cb.run_training(df, args, mortgage)
    assert results["high_value_expert_fitted"]
    assert results["smearing_factors"] is not None
    assert hpp_cb.expert_model_path(model_out).exists()

    model, meta = hpp_cb.load_model(model_out)
    assert meta["high_value_model"] is not None
    assert meta["smearing_factors"] == results["smearing_factors"]

    X, y_raw, _ = hpp_cb.prepare_features_from_saved_model(df, mortgage, meta)
    n_test = results["n_test"]
    X_test, y_test = X.iloc[-n_test:], y_raw[-n_test:]
    general, mixture = hpp_cb.predict_with_adjustments(
        model, X_test, meta["cat_features"], meta["target"], None,
        smearing_factors=meta["smearing_factors"],
        high_value_model=meta["high_value_model"],
        high_value_threshold=meta["high_value_threshold"],
    )
    reported = results["test"]
    assert hpp_cb._metrics_with_bias(y_test, general)["mae"] == pytest.approx(reported["mae"], rel=1e-6)
    assert hpp_cb._metrics_with_bias(y_test, mixture)["mae"] == pytest.approx(reported["mixture"]["mae"], rel=1e-6)

    bare, _ = hpp_cb.predict_with_adjustments(model, X_test, meta["cat_features"], meta["target"], None)
    assert not np.allclose(bare, general), "smearing should move predictions, or this test has no teeth"

    scored = hpp_cb.run_prediction(df, _make_args(model_in=model_out), mortgage, model, meta)
    assert scored["smearing_applied"] and scored["high_value_expert_applied"]
    assert "mixture" in scored["metrics"]


def test_load_model_refuses_a_missing_expert(tmp_path):
    from hpldn.backend.models import artifact

    df = _make_dataset(n=700, seed=7)
    model_out = tmp_path / "hpp_cb"
    args = _make_args(
        device="cpu", iterations=15, early_stopping_rounds=5, thread_count=1, random_state=0,
        two_stage=True, high_value_threshold=float(np.quantile(df["price"], 0.7)), model_out=model_out,
    )
    hpp_cb.run_training(df, args, _make_mortgage_rates())
    hpp_cb.expert_model_path(model_out).unlink()
    with pytest.raises(artifact.BundleError, match="missing"):
        hpp_cb.load_model(model_out)


def test_implausible_low_price_mask_uses_comparables_with_a_no_area_fallback():
    """A sale far below its comparables' implied value is a non-market transfer."""
    df = pd.DataFrame(
        {
            "price": [100_000.0, 400_000.0, 100_000.0, 100_000.0, 100_000.0],
            "total_floor_area": [100.0, 100.0, np.nan, np.nan, np.nan],
            "comp_median_price_per_sqm": [5_000.0, 5_000.0, 5_000.0, np.nan, np.nan],
            "comp_median_price": [np.nan, np.nan, 500_000.0, 200_000.0, np.nan],
        }
    )
    # 100k vs 500k implied: flagged; 400k: kept; no area -> comp_median_price
    # (500k: flagged; 200k: kept); nothing to compare with: kept.
    assert hpp_cb._implausible_low_price_mask(df).tolist() == [True, False, True, False, False]


def test_drop_implausible_training_rows_applies_both_bounds():
    df = pd.DataFrame(
        {
            "price": [100_000.0, 500_000.0, 5_000_000.0],
            "total_floor_area": [100.0, 100.0, 100.0],
            "comp_median_price_per_sqm": [5_000.0, 5_000.0, 5_000.0],
            "comp_median_price": [np.nan] * 3,
        }
    )
    kept = hpp_cb._drop_implausible_training_rows(df, None, "test")
    assert kept["price"].tolist() == [500_000.0]


def test_scoped_exclusion_and_segment_masks():
    df = pd.DataFrame(
        {
            "price": [500_000.0, 12_000_000.0, 50_000.0, 600_000.0],
            "total_floor_area": [100.0, 400.0, 100.0, np.nan],
            "comp_median_price_per_sqm": [5_000.0, 25_000.0, 5_000.0, np.nan],
            "comp_median_price": [np.nan, np.nan, np.nan, 550_000.0],
            "address_id": [1, 68_000_001, 2, 68_500_000],
            "prev_sale_price": [np.nan, 1.0, np.nan, 2.0],
            "propertytype": ["F", "D", "F", "T"],
        }
    )
    assert hpp_cb._scoped_exclusion_mask(df).tolist() == [False, True, True, False]
    masks = hpp_cb._segment_masks(df)
    assert masks["backfilled_address"].tolist() == [False, True, False, True]
    assert masks["has_prev_sale"].tolist() == [False, True, False, True]
    assert masks["no_floor_area"].tolist() == [False, False, False, True]
    assert masks["propertytype_F"].tolist() == [True, False, True, False]


def test_percentage_error_metrics():
    y_true = np.array([100.0, 100.0, 100.0, 100.0])
    y_pred = np.array([104.0, 92.0, 115.0, 150.0])
    got = hpp_cb._percentage_error_metrics(y_true, y_pred)
    assert got["mdape"] == pytest.approx(0.115)
    assert got["within_5pct"] == pytest.approx(0.25)
    assert got["within_10pct"] == pytest.approx(0.5)
    assert got["within_20pct"] == pytest.approx(0.75)
    small = hpp_cb._metrics_on_mask(y_true, y_pred, np.array([True, False, False, False]))
    assert small == {"n": 1}


def test_eval_learning_curve_reports_best_loss_by_share_of_rounds():
    class _Model:
        def get_evals_result(self):
            return {"validation": {"RMSE": [5.0, 4.0, 3.0, 3.5, 2.0, 2.5, 2.2, 2.1]}}

    points = hpp_cb._eval_learning_curve(_Model())
    assert [p["iteration"] for p in points] == [2, 4, 6, 8]
    assert [p["best_loss"] for p in points] == [4.0, 3.0, 2.0, 2.0]


def test_backtest_start_year_skips_early_folds():
    df = hpp_rf.engineer_features(_make_dataset(n=900, start="2010-01-01", periods_days=6 * 365))
    df = df.sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    every = [year for year, _, _ in hpp_cb._expanding_window_year_folds_lazy(df)]
    later = [year for year, _, train in hpp_cb._expanding_window_year_folds_lazy(df, start_year=2013)]
    assert later == [year for year in every if year >= 2013]
    assert min(every) < 2013
    for year, train, test in hpp_cb._expanding_window_year_folds_lazy(df, start_year=2013):
        assert train["sale_year"].min() == df["sale_year"].min()
        assert (test["sale_year"] == year).all()


def test_mortgage_rate_features_are_dropped_by_default():
    """46-51 % missing on a 1995-onward train set and a time proxy under the deflated target."""
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, _, meta = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert not set(hpp_cb.MORTGAGE_RATE_DEPENDENT_COLUMNS) & set(X_train.columns)
    assert "mortgage_rate_missing" not in X_train.columns
    assert hpp_cb.build_arg_parser().parse_args([]).learning_rate == hpp_cb.DEFAULT_LEARNING_RATE


def test_market_relative_features_reach_the_matrix_and_a_saved_model(tmp_path):
    df = _make_dataset(n=600, seed=1)
    df["prev_sale_date"] = pd.to_datetime(df["dateoftransfer"]) - pd.DateOffset(years=3)
    ukhpi = pd.DataFrame(
        {
            "period": pd.to_datetime(["1990-01-01"]),
            "published_date": pd.to_datetime(["1990-01-01"]),
            "avg_price_all_property_types": [200_000.0],
            **{col: [200_000.0] for col in hpp_rf.UKHPI_PRICE_COLUMN_BY_TYPE.values()},
        }
    )
    model_out = tmp_path / "m"
    args = _make_args(device="cpu", iterations=20, early_stopping_rounds=5, thread_count=1, model_out=model_out)
    results = hpp_cb.run_training(df, args, _make_mortgage_rates(), ukhpi_df=ukhpi)
    assert set(hpp_rf.MARKET_RELATIVE_COLUMNS) <= set(results["feature_columns"])
    assert results["price_stat_globals"]["scale"] == hpp_cb.GROUP_STATS_SCALE_DEFLATED
    assert "scoped" in results["test"]

    model, meta = hpp_cb.load_model(model_out)
    assert meta["group_stats_scale"] == hpp_cb.GROUP_STATS_SCALE_DEFLATED
    X, _, _ = hpp_cb.prepare_features_from_saved_model(
        df.tail(20), _make_mortgage_rates(), meta, ukhpi_df=ukhpi
    )
    assert list(X.columns) == meta["feature_columns"]
    assert X["prev_sale_deflated"].notna().any()


def test_the_anchored_target_round_trips_through_save_and_load(tmp_path):
    """Fit, save, load and score: an anchored model prices built rows as its training run did."""
    from hpldn.backend.scripts import hpp_inference

    y = np.array([250_000.0, 1_000_000.0, 7_000_000.0])
    anchor = np.array([300_000.0, 900_000.0, 5_000_000.0])
    transformed = hpp_cb._target_transform(y, anchor, "anchored")
    np.testing.assert_allclose(transformed, np.log1p(y) - np.log(anchor))
    np.testing.assert_allclose(hpp_cb._target_inverse_transform(transformed, anchor, "anchored"), y)

    df = _make_dataset(n=400, seed=23)
    model_out = tmp_path / "anchored"
    args = _make_args(
        device="cpu", iterations=30, early_stopping_rounds=10, thread_count=1, model_out=model_out, target="anchored"
    )
    hpp_cb.run_training(df, args, _make_mortgage_rates())
    model_ready, meta = hpp_cb.load_model(model_out)
    assert meta["target"] == "anchored" and meta["anchor_columns"] == list(hpp_rf.ANCHOR_SOURCE_COLUMNS)
    X, _, deflator = hpp_cb.prepare_features_from_saved_model(df, _make_mortgage_rates(), meta)
    # The deflator is the anchor the row was built with, not the market index.
    engineered = hpp_rf.add_market_relative_features(
        hpp_rf.add_sizeband_comp_features(hpp_rf.engineer_features(df).sort_values("dateoftransfer", kind="stable"))
    )
    np.testing.assert_allclose(deflator, engineered[hpp_rf.ANCHOR_COLUMN].to_numpy(dtype=np.float64))
    expected, _ = hpp_cb.predict_with_adjustments(model_ready, X, meta["cat_features"], "anchored", deflator)
    assert np.all(np.isfinite(expected)) and np.all(expected > 0)

    saved = hpp_inference.SavedModel(model_out)
    frame = hpp_rf.add_sizeband_comp_features(hpp_rf.engineer_features(df)).sort_values("dateoftransfer", kind="stable")
    got = saved.predict(frame.reset_index(drop=True), _FakeStore(_make_mortgage_rates()))
    np.testing.assert_allclose(got, expected, rtol=1e-6)


class _FakeStore:
    """What `SavedModel.predict` reads from an `EvidenceStore`."""

    def __init__(self, mortgage_rates):
        self.mortgage_rates = mortgage_rates


def test_no_ofsted_drops_the_ofsted_group():
    """``--no-ofsted`` leaves out the twelve ofsted_* columns and their flag; the preset does the same."""
    assert hpp_cb.build_arg_parser().parse_args(["--no-ofsted"]).use_ofsted is False
    assert hpp_cb.build_arg_parser().parse_args([]).use_ofsted is True
    assert set(hpp_cb.DROP_FEATURES_PRESETS["no_ofsted"]) == set(hpp_rf.OFSTED_COLUMNS) | {"ofsted_missing"}
    assert hpp_cb._MISSING_FLAG_GROUPS["ofsted_missing"] == hpp_rf.OFSTED_NUMERIC_COLUMNS

    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_on, _, _, cat_on, _, meta_on = hpp_cb.prepare_features(train_df, val_df, test_df)
    X_off, _, _, cat_off, _, meta_off = hpp_cb.prepare_features(train_df, val_df, test_df, use_ofsted=False)
    X_preset, *_ = hpp_cb.prepare_features(
        train_df, val_df, test_df, drop_features=hpp_cb.DROP_FEATURES_PRESETS["no_ofsted"]
    )
    ofsted = set(hpp_rf.OFSTED_COLUMNS) | {"ofsted_missing"}
    assert ofsted <= set(X_on.columns) and "ofsted_nearest_primary_basis" in cat_on
    assert not ofsted & set(X_off.columns) and "ofsted_nearest_primary_basis" not in cat_off
    assert set(X_on.columns) - set(X_off.columns) == ofsted
    assert list(X_preset.columns) == list(X_off.columns)
    assert "ofsted_missing" not in meta_off["missing_flag_groups"]
    assert X_on["ofsted_nearest_primary_basis"].isna().sum() == 0


def test_saved_model_records_use_ofsted(tmp_path):
    """The meta records the flag, and a --no-ofsted model scores a frame without the columns."""
    df = _make_dataset(n=300, seed=3)
    model_out = tmp_path / "no_ofsted"
    args = _make_args(device="cpu", iterations=10, early_stopping_rounds=5, thread_count=1,
                      model_out=model_out, use_ofsted=False)
    hpp_cb.run_training(df, args, _make_mortgage_rates())

    _, meta = hpp_cb.load_model(model_out)
    assert meta["use_ofsted"] is False
    assert not any(c.startswith("ofsted") for c in meta["feature_columns"])
