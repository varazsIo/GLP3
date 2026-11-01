"""
Tests for `hpldn.backend.scripts.data_visualization`.

Organised around proving the plots show the features the models train on:
the column contract with `hpp_rf`'s matrix, the preparation pipeline for every
split and model, SHAP by both routes and its ranking, the correlation matrix
and redundancy report, the price-response tables, rendering, and end-to-end
runs on `test_hp_rf`'s synthetic dataset, plus an integration test on the
migrated hpdbldn (`hpdbldn_ro`, read-only).
"""

import json

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.scripts import data_visualization as dv
from hpldn.backend.scripts import hpp_cb, hpp_rf
from hpldn.backend.tests.ml.test_hp_rf import (
    _load_production_sample,
    _make_dataset,
    _make_mortgage_rates,
    _with_mortgage_features,
)


def _make_args(**overrides):
    """Return CLI defaults with small models, since tests check plot content, not model quality."""
    args = dv.build_arg_parser().parse_args([])
    args.viz_n_estimators = 8
    args.viz_max_depth = 6
    args.viz_iterations = 25
    args.viz_depth = 4
    args.n_jobs = 1
    args.shap_sample = 40
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _prepared(model="rf", n=300, args=None, df=None):
    """
    Run `prepare_visualization_data` with a mortgage-rate frame.

    CatBoost requires the mortgage columns; passing them for the forest too
    keeps both models' splits identical.
    """
    args = args or _make_args()
    return dv.prepare_visualization_data(
        df if df is not None else _make_dataset(n=n),
        args,
        model,
        _make_mortgage_rates(),
    )




def test_feature_matrix_columns_label_hpp_rf_matrix():
    """Check `FEATURE_MATRIX_COLUMNS` matches the width of `hpp_rf`'s unlabelled matrix."""
    df = hpp_rf.engineer_features(_make_dataset(n=200))
    train_df, val_df, test_df = hpp_rf.time_series_split(df, 0.7, 0.15)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)
    X_train, _, _, _, _, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)

    assert X_train.shape[1] == len(dv.FEATURE_MATRIX_COLUMNS)
    assert dv.FEATURE_MATRIX_COLUMNS[: len(hpp_rf.FEATURE_COLUMNS)] == list(hpp_rf.FEATURE_COLUMNS)
    assert dv.FEATURE_MATRIX_COLUMNS[len(hpp_rf.FEATURE_COLUMNS) :] == dv.MISSING_FLAG_COLUMNS


def test_feature_matrix_columns_do_not_describe_the_catboost_matrix():
    """
    Check `FEATURE_MATRIX_COLUMNS` is not CatBoost's column list.

    CatBoost adds IoD and mortgage-rate features and drops some defaults, so
    the lists must not be silently interchanged.
    """
    df = hpp_rf.engineer_features(_make_dataset(n=200))
    df = _with_mortgage_features(df)
    train_df, val_df, test_df = hpp_rf.time_series_split(df, 0.7, 0.15)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)
    X_train, _, _, cat_features, _, feature_metadata = hpp_cb.prepare_features(
        train_df, val_df, test_df
    )

    assert list(X_train.columns) == feature_metadata["feature_columns"]
    assert cat_features == hpp_rf.CATEGORICAL_COLUMNS + hpp_cb.IOD_CATEGORICAL_COLUMNS + hpp_rf.OFSTED_CATEGORICAL_COLUMNS
    cb_only = set(X_train.columns) - set(dv.FEATURE_MATRIX_COLUMNS)
    rf_only = set(dv.FEATURE_MATRIX_COLUMNS) - set(X_train.columns)
    c1_dropped = set(hpp_cb.C1_DEFAULT_DROPPED_COLUMNS)
    assert set(hpp_cb.IOD_NUMERIC_COLUMNS) - c1_dropped <= cb_only
    # Dropped by default since the mortgage table starts in 2007 (see hpp_cb).
    assert not set(hpp_cb.MORTGAGE_RATE_DEPENDENT_COLUMNS) & set(X_train.columns)
    assert set(hpp_cb.C1_DEFAULT_DROPPED_COLUMNS) & set(dv.FEATURE_MATRIX_COLUMNS) <= rf_only


def test_missing_flag_columns_come_from_hpp_rf_groups():
    """Check the missing-flag columns are `hpp_rf`'s, not CatBoost's larger set."""
    assert dv.MISSING_FLAG_COLUMNS == list(hpp_rf.MISSING_FLAG_GROUPS)
    assert set(dv.MISSING_FLAG_COLUMNS) <= set(hpp_cb._MISSING_FLAG_GROUPS)


def test_numeric_feature_columns_cover_every_numeric_family_and_no_categorical():
    """Check every numeric feature family is plottable and no encoded categorical is."""
    numeric = set(dv.NUMERIC_FEATURE_COLUMNS)
    expected = set(
        hpp_rf.NUMERIC_COLUMNS
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
        + hpp_rf.OFSTED_NUMERIC_COLUMNS
        + dv.MISSING_FLAG_COLUMNS
    )
    assert numeric == expected
    assert numeric.isdisjoint(hpp_rf.CATEGORICAL_COLUMNS + hpp_rf.OFSTED_CATEGORICAL_COLUMNS)
    for col in hpp_rf.ROLLING_COMP_STAT_COLUMNS + hpp_rf.PTAL_DEPENDENT_COLUMNS + hpp_rf.SECTOR_STAT_COLUMNS:
        assert col in numeric
    assert "is_house" in numeric
    assert "floor_level" not in numeric and "floor_level" in hpp_rf.CATEGORICAL_COLUMNS




def test_prepare_visualization_data_rf_returns_labelled_dense_matrix():
    prepared = _prepared("rf", n=300)
    assert list(prepared.features.columns) == dv.FEATURE_MATRIX_COLUMNS
    assert prepared.split == "train"
    assert len(prepared.features) == len(prepared.prices)
    assert not prepared.features.isna().to_numpy().any()
    assert (prepared.prices > 0).all()


def test_prepare_visualization_data_catboost_keeps_categoricals_as_strings():
    prepared = _prepared("catboost", n=300)
    assert list(prepared.features.columns) == prepared.feature_names
    for col in prepared.categorical_columns:
        assert pd.api.types.is_string_dtype(prepared.features[col])
    for col in prepared.numeric_columns:
        assert pd.api.types.is_numeric_dtype(prepared.features[col])


def test_prepare_visualization_data_includes_train_only_price_statistics():
    """Check the train-only postcode and sector price statistics are present."""
    prepared = _prepared("rf", n=400)
    for col in hpp_rf.POSTCODE_STAT_COLUMNS + hpp_rf.SECTOR_STAT_COLUMNS:
        assert col in prepared.features.columns
        assert prepared.features[col].notna().all()


def test_prepare_visualization_data_prices_align_with_the_chosen_split():
    df = _make_dataset(n=400)
    engineered = hpp_rf.engineer_features(df).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    train_df, val_df, test_df = hpp_rf.time_series_split(engineered, 0.7, 0.15)
    expected = {"train": train_df, "val": val_df, "test": test_df}

    for split, part in expected.items():
        prepared = dv.prepare_visualization_data(df, _make_args(analysis_split=split), "rf", _make_mortgage_rates())
        assert prepared.split == split
        assert len(prepared.features) == len(part)
        assert np.allclose(prepared.prices, part[hpp_rf.TARGET_COLUMN].to_numpy(dtype=np.float64))
        assert prepared.n_rows_total == len(engineered)


@pytest.mark.parametrize(
    "split_method", ["chronological", "grouped", "purged", "spatial", "recency", "walk_forward"]
)
def test_prepare_visualization_data_supports_every_split_method(split_method):
    prepared = dv.prepare_visualization_data(
        _make_dataset(n=400),
        _make_args(split_method=split_method),
        "rf",
        _make_mortgage_rates(),
    )
    assert len(prepared.features) > 0
    assert list(prepared.features.columns) == dv.FEATURE_MATRIX_COLUMNS


def test_walk_forward_degrades_to_chronological_without_fitting_folds():
    """Check ``walk_forward`` is downgraded to chronological so no fold models are fitted."""
    df = _make_dataset(n=400)
    walk = dv.prepare_visualization_data(df, _make_args(split_method="walk_forward"), "rf", _make_mortgage_rates())
    chrono = dv.prepare_visualization_data(df, _make_args(split_method="chronological"), "rf", _make_mortgage_rates())
    assert len(walk.features) == len(chrono.features)
    assert np.allclose(walk.prices, chrono.prices)


def test_prepare_visualization_data_rejects_unknown_model_and_split():
    df = _make_dataset(n=120)
    with pytest.raises(ValueError, match="Unknown model"):
        dv.prepare_visualization_data(df, _make_args(), "xgboost", _make_mortgage_rates())
    with pytest.raises(ValueError, match="Unknown analysis split"):
        dv.prepare_visualization_data(df, _make_args(analysis_split="holdout"), "rf", _make_mortgage_rates())




def test_subsample_caps_rows_and_keeps_prices_aligned():
    prepared = _prepared("rf", n=400)
    original = prepared.features["total_floor_area"].to_numpy()
    sampled = dv.subsample(prepared, 50, random_state=1)

    assert len(sampled.features) == 50
    assert len(sampled.prices) == 50
    assert sampled.feature_names == prepared.feature_names
    for value, price in zip(sampled.features["total_floor_area"], sampled.prices):
        matches = np.isclose(original, value)
        assert np.isclose(prepared.prices[matches], price).any()


def test_subsample_is_a_noop_when_not_needed():
    prepared = _prepared("rf", n=120)
    assert dv.subsample(prepared, None) is prepared
    assert dv.subsample(prepared, 0) is prepared
    assert dv.subsample(prepared, len(prepared.features) + 10) is prepared




@pytest.mark.parametrize("model", ["rf", "catboost"])
def test_compute_shap_values_shape_matches_the_feature_matrix(model):
    args = _make_args()
    prepared = dv.subsample(_prepared(model, n=300, args=args), 40, random_state=0)
    fitted = dv.fit_reference_model(prepared, args)
    values = dv.compute_shap_values(fitted, prepared)

    assert values.shape == (len(prepared.features), len(prepared.feature_names))
    assert np.isfinite(values).all()


def test_catboost_shap_values_are_additive():
    """Check CatBoost SHAP values plus the dropped base value reproduce predictions."""
    from catboost import Pool

    args = _make_args()
    prepared = dv.subsample(_prepared("catboost", n=300, args=args), 30, random_state=0)
    fitted = dv.fit_reference_model(prepared, args)

    pool = Pool(prepared.features, cat_features=prepared.categorical_columns)
    raw = np.asarray(fitted.get_feature_importance(pool, type="ShapValues"))
    dropped = dv.compute_shap_values(fitted, prepared)

    assert np.allclose(raw[:, :-1], dropped)
    assert np.allclose(dropped.sum(axis=1) + raw[:, -1], fitted.predict(prepared.features), atol=1e-4)


def test_random_forest_shap_values_are_additive():
    """Check forest SHAP values are additive, since the explainer's own check is disabled."""
    import shap

    args = _make_args()
    prepared = dv.subsample(_prepared("rf", n=300, args=args), 30, random_state=0)
    fitted = dv.fit_reference_model(prepared, args)
    values = dv.compute_shap_values(fitted, prepared)

    explainer = shap.TreeExplainer(fitted)
    expected = fitted.predict(prepared.features.to_numpy(dtype=np.float64))
    assert np.allclose(values.sum(axis=1) + explainer.expected_value, expected, atol=1e-4)


def test_global_feature_importance_ranks_by_mean_absolute_shap():
    """Check ranking uses mean absolute SHAP, so a cancelling feature still ranks first."""
    values = np.array(
        [
            [1.0, 0.0, 10.0],
            [-1.0, 0.5, -10.0],
            [1.0, -0.5, 10.0],
            [-1.0, 0.0, -10.0],
        ]
    )
    importance = dv.global_feature_importance(values, ["a", "b", "c"])

    assert importance["feature"].tolist() == ["c", "a", "b"]
    assert importance["mean_abs_shap"].tolist() == pytest.approx([10.0, 1.0, 0.25])
    assert importance["share"].sum() == pytest.approx(1.0)
    assert importance["share"].iloc[0] == pytest.approx(10.0 / 11.25)


def test_global_feature_importance_rejects_mismatched_inputs():
    with pytest.raises(ValueError, match="2-D"):
        dv.global_feature_importance(np.array([1.0, 2.0]), ["a", "b"])
    with pytest.raises(ValueError, match="feature names"):
        dv.global_feature_importance(np.zeros((3, 2)), ["a", "b", "c"])


def test_global_feature_importance_handles_an_all_zero_model():
    importance = dv.global_feature_importance(np.zeros((5, 3)), ["a", "b", "c"])
    assert importance["share"].tolist() == [0.0, 0.0, 0.0]


@pytest.mark.parametrize("model", ["rf", "catboost"])
def test_shap_ranking_puts_a_price_driving_feature_above_pure_noise(model):
    """
    Check a price-driving feature outranks independent noise.

    Compared with ``extension_count``, since CatBoost drops ``boe_rate_at_sale``
    by default.
    """
    args = _make_args()
    prepared = dv.subsample(_prepared(model, n=600, args=args), 80, random_state=0)
    fitted = dv.fit_reference_model(prepared, args)
    importance = dv.global_feature_importance(
        dv.compute_shap_values(fitted, prepared), prepared.feature_names
    ).set_index("feature")["mean_abs_shap"]

    assert importance["comp_median_price"] > importance["extension_count"]
    assert importance["comp_median_price"] > importance["multi_glaze_proportion"]


def test_load_reference_model_reads_an_hpp_rf_saved_model(tmp_path):
    model_path = tmp_path / "hpp_rf.joblib"
    args = hpp_rf.build_arg_parser().parse_args([])
    args.device, args.n_estimators, args.max_depth, args.n_jobs = "cpu", 5, 5, 1
    args.model_out = model_path
    hpp_rf.run_training(_make_dataset(n=300), args)

    with pytest.raises(PermissionError, match="--trust-pickle"):
        dv.load_reference_model(model_path, "rf")
    loaded = dv.load_reference_model(model_path, "rf", trust_pickle=True)
    assert hasattr(loaded, "predict")
    assert loaded.n_estimators == 5


def test_load_reference_model_reads_an_hpp_cb_saved_model(tmp_path):
    model_path = tmp_path / "hpp_cb"
    args = hpp_cb.build_arg_parser().parse_args([])
    args.device, args.iterations, args.early_stopping_rounds, args.thread_count = "cpu", 15, 5, 1
    args.model_out = model_path
    hpp_cb.run_training(_make_dataset(n=300), args, _make_mortgage_rates())

    loaded = dv.load_reference_model(model_path, "catboost")
    assert hasattr(loaded, "predict")




def test_feature_correlation_matrix_is_square_symmetric_and_numeric_only():
    prepared = _prepared("rf", n=400)
    corr = dv.feature_correlation_matrix(prepared)

    assert list(corr.index) == list(corr.columns)
    assert set(corr.columns).issubset(dv.NUMERIC_FEATURE_COLUMNS)
    assert set(corr.columns).isdisjoint(hpp_rf.CATEGORICAL_COLUMNS)
    assert np.allclose(np.diag(corr.to_numpy()), 1.0)
    assert np.allclose(corr.to_numpy(), corr.to_numpy().T, equal_nan=True)
    assert np.isfinite(corr.to_numpy()).all()


def test_feature_correlation_matrix_is_the_same_for_both_models():
    """Check both models give identical values for their shared numeric features."""
    df = _make_dataset(n=400)
    rf_corr = dv.feature_correlation_matrix(dv.prepare_visualization_data(df, _make_args(), "rf", _make_mortgage_rates()))
    cb_corr = dv.feature_correlation_matrix(dv.prepare_visualization_data(df, _make_args(), "catboost", _make_mortgage_rates()))
    shared = [c for c in rf_corr.columns if c in set(cb_corr.columns)]
    assert len(shared) > 20, "the two models should still share most of their numeric block"
    assert np.allclose(
        rf_corr.loc[shared, shared].to_numpy(),
        cb_corr.loc[shared, shared].to_numpy(),
    )


def test_feature_correlation_matrix_drops_constant_columns():
    prepared = _prepared("rf", n=200)
    prepared.features["extension_count"] = 3.0
    corr = dv.feature_correlation_matrix(prepared)
    assert "extension_count" not in corr.columns
    assert "total_floor_area" in corr.columns


def test_feature_correlation_matrix_drops_near_constant_columns():
    """Check near-constant columns are dropped from the correlation matrix only."""
    prepared = _prepared("rf", n=400)
    n = len(prepared.features)
    rare = np.zeros(n)
    rare[0] = 1.0
    prepared.features["comp_missing"] = rare
    prepared.features["rolling_comp_missing"] = rare.copy()

    corr = dv.feature_correlation_matrix(prepared, min_variation_frac=0.05)
    assert "comp_missing" not in corr.columns
    assert "rolling_comp_missing" not in corr.columns
    assert dv.redundant_feature_pairs(corr, threshold=0.99) != [
        ("comp_missing", "rolling_comp_missing", 1.0)
    ]
    assert "lot_area_missing" in dv.feature_correlation_matrix(prepared).columns


def test_varies_enough_uses_a_scale_invariant_floor():
    series = pd.Series([0.0] * 95 + [1.0] * 5)
    assert dv._varies_enough(series, 0.01)
    assert not dv._varies_enough(series, 0.10)
    assert not dv._varies_enough(pd.Series([3.0] * 50), 0.0)
    assert not dv._varies_enough(pd.Series([np.nan] * 50), 0.0)
    assert not dv._varies_enough(pd.Series([0.0] * 9 + [1.0]), 0.0)
    assert dv._varies_enough(pd.Series([0.0] * 8 + [1.0, 1.0]), 0.0)


def test_redundant_feature_pairs_finds_a_monotone_duplicate():
    """Check the monotone ``log_lot_area``/``lot_area_sqm`` pair is reported as redundant."""
    prepared = _prepared("rf", n=400)
    corr = dv.feature_correlation_matrix(prepared, method="spearman")
    pairs = dv.redundant_feature_pairs(corr, threshold=0.999)

    found = {frozenset((a, b)): value for a, b, value in pairs}
    assert frozenset(("lot_area_sqm", "log_lot_area")) in found
    assert found[frozenset(("lot_area_sqm", "log_lot_area"))] == pytest.approx(1.0)
    assert frozenset(("total_floor_area", "log_floor_area")) in found


def test_redundant_feature_pairs_reports_each_pair_once_sorted_by_strength():
    corr = pd.DataFrame(
        [
            [1.0, 0.95, -0.99, 0.10],
            [0.95, 1.0, 0.20, 0.30],
            [-0.99, 0.20, 1.0, 0.40],
            [0.10, 0.30, 0.40, 1.0],
        ],
        index=list("abcd"),
        columns=list("abcd"),
    )
    pairs = dv.redundant_feature_pairs(corr, threshold=0.9)

    assert pairs == [("a", "c", -0.99), ("a", "b", 0.95)]
    assert all(a != b for a, b, _ in pairs)
    assert dv.redundant_feature_pairs(corr, threshold=0.999) == []


def test_redundant_feature_pairs_ignores_nan_cells():
    corr = pd.DataFrame(
        [[1.0, np.nan], [np.nan, 1.0]], index=["a", "b"], columns=["a", "b"]
    )
    assert dv.redundant_feature_pairs(corr, threshold=0.5) == []


def test_cluster_order_is_a_permutation_of_the_matrix():
    prepared = _prepared("rf", n=300)
    corr = dv.feature_correlation_matrix(prepared)
    order = dv._cluster_order(corr)

    assert sorted(order) == sorted(corr.columns)
    reordered = corr.loc[order, order]
    assert np.allclose(np.diag(reordered.to_numpy()), 1.0)
    tiny = corr.iloc[:2, :2]
    assert dv._cluster_order(tiny) == list(tiny.columns)




def test_univariate_price_response_bins_and_orders_correctly():
    prepared = _prepared("rf", n=600)
    table = dv.univariate_price_response(
        prepared.features["total_floor_area"], prepared.prices, bins=10
    )

    assert list(table.columns) == ["x", "median_price", "q25", "q75", "count"]
    assert len(table) <= 10
    assert table["x"].is_monotonic_increasing
    assert (table["q25"] <= table["median_price"]).all()
    assert (table["median_price"] <= table["q75"]).all()
    assert table["count"].sum() == len(prepared.prices)


def test_univariate_price_response_groups_low_cardinality_features_by_value():
    """Check low-cardinality features are grouped by exact value rather than binned."""
    prepared = _prepared("rf", n=400)
    table = dv.univariate_price_response(prepared.features["is_house"], prepared.prices, bins=20)
    assert len(table) == 2
    assert table["x"].tolist() == [0.0, 1.0]
    assert table["count"].sum() == len(prepared.prices)


def test_univariate_price_response_recovers_a_known_monotone_relationship():
    values = np.arange(1000, dtype="float64")
    prices = 100_000.0 + values * 500.0
    table = dv.univariate_price_response(values, prices, bins=10)

    assert len(table) == 10
    assert table["median_price"].is_monotonic_increasing
    assert table["count"].tolist() == [100] * 10


def test_univariate_price_response_drops_missing_pairs():
    values = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
    prices = np.array([10.0, np.nan, 30.0, 40.0, 50.0])
    table = dv.univariate_price_response(values, prices, bins=4)
    assert table["count"].sum() == 3


def test_univariate_price_response_handles_an_empty_input():
    table = dv.univariate_price_response(np.array([np.nan, np.nan]), np.array([1.0, 2.0]))
    assert table.empty
    assert list(table.columns) == ["x", "median_price", "q25", "q75", "count"]


def test_rank_features_by_price_correlation_puts_price_drivers_first():
    prepared = _prepared("rf", n=600)
    ranked = dv.rank_features_by_price_correlation(prepared)

    assert list(ranked.columns) == ["feature", "correlation", "abs_correlation"]
    assert ranked["abs_correlation"].is_monotonic_decreasing
    assert set(ranked["feature"]).issubset(dv.NUMERIC_FEATURE_COLUMNS)
    assert "comp_median_price" in ranked["feature"].head(5).tolist()
    row = ranked.set_index("feature").loc["comp_median_price"]
    assert row["correlation"] > 0.5


def test_select_univariate_features_drops_near_duplicates():
    """Check only one of the monotone floor-area pair takes a panel."""
    prepared = _prepared("rf", n=600)
    ranked = dv.rank_features_by_price_correlation(prepared)["feature"].tolist()
    chosen = dv.select_univariate_features(prepared, 12, dedupe_threshold=0.9)

    assert len(chosen) <= 12
    assert len(set(chosen)) == len(chosen)
    assert not {"total_floor_area", "log_floor_area"}.issubset(chosen)
    assert not {"lot_area_sqm", "log_lot_area"}.issubset(chosen)
    assert set(chosen).issubset(ranked)
    assert chosen[0] == ranked[0]

    corr = np.array(dv.feature_correlation_matrix(prepared).loc[chosen, chosen].to_numpy(), copy=True)
    np.fill_diagonal(corr, 0.0)
    assert np.abs(corr).max() < 0.9


def test_select_univariate_features_without_dedupe_is_the_raw_ranking():
    prepared = _prepared("rf", n=400)
    ranked = dv.rank_features_by_price_correlation(prepared)["feature"].tolist()
    assert dv.select_univariate_features(prepared, 10, dedupe_threshold=None) == ranked[:10]


def test_rank_features_by_price_correlation_skips_constant_columns():
    prepared = _prepared("rf", n=300)
    prepared.features["multi_glaze_proportion"] = 50.0
    ranked = dv.rank_features_by_price_correlation(prepared)
    assert "multi_glaze_proportion" not in ranked["feature"].tolist()




def _nonempty_file(path):
    return path.exists() and path.stat().st_size > 0


def test_plot_global_feature_importance_writes_a_figure(tmp_path):
    importance = dv.global_feature_importance(
        np.random.default_rng(0).normal(size=(20, len(dv.FEATURE_MATRIX_COLUMNS))),
        dv.FEATURE_MATRIX_COLUMNS,
    )
    path = dv.plot_global_feature_importance(
        importance, tmp_path / "nested" / "importance.png", top_n=10, subtitle="test"
    )
    assert _nonempty_file(path)


def test_plot_feature_correlation_heatmap_writes_a_figure(tmp_path):
    corr = dv.feature_correlation_matrix(_prepared("rf", n=300))
    clustered = dv.plot_feature_correlation_heatmap(corr, tmp_path / "corr.png", subtitle="test")
    plain = dv.plot_feature_correlation_heatmap(corr, tmp_path / "corr_plain.svg", cluster=False)
    assert _nonempty_file(clustered)
    assert _nonempty_file(plain)


def test_plot_feature_correlation_heatmap_annotates_a_small_matrix(tmp_path):
    corr = dv.feature_correlation_matrix(_prepared("rf", n=300)).iloc[:6, :6]
    path = dv.plot_feature_correlation_heatmap(corr, tmp_path / "small.png", annotate_max_features=20)
    assert _nonempty_file(path)


def test_plot_univariate_price_response_writes_a_figure(tmp_path):
    prepared = _prepared("rf", n=400)
    path = dv.plot_univariate_price_response(
        prepared,
        ["total_floor_area", "distance_to_city_center_km", "is_house", "ptal_band_rank", "sale_year"],
        tmp_path / "univariate.png",
        bins=8,
        ncols=3,
    )
    assert _nonempty_file(path)


def test_plot_univariate_price_response_rejects_an_empty_selection(tmp_path):
    prepared = _prepared("rf", n=200)
    with pytest.raises(ValueError, match="No plottable numeric features"):
        dv.plot_univariate_price_response(prepared, ["not_a_column"], tmp_path / "x.png")


def test_compact_number_abbreviates_by_magnitude():
    assert dv._compact_number(180.0) == "180"
    assert dv._compact_number(450_000.0) == "450k"
    assert dv._compact_number(1_200_000.0) == "1.2m"
    assert dv._compact_number(4.85) == "4.85"




def test_generate_visualizations_writes_all_three_plots_for_both_models(tmp_path):
    """Check all three plots are written for both models; only the forest's width is pinned."""
    args = _make_args(
        model="both",
        out_dir=tmp_path,
        univariate_features=6,
        top_n=8,
        analysis_sample=0,
        fit_sample=0,
    )
    report = dv.generate_visualizations(_make_dataset(n=400), args, _make_mortgage_rates())

    assert set(report) == {"rf", "catboost"}
    assert report["rf"]["n_features"] == len(dv.FEATURE_MATRIX_COLUMNS)
    assert report["catboost"]["n_features"] > len(dv.FEATURE_MATRIX_COLUMNS)
    for model, entry in report.items():
        assert entry["split"] == "train"
        assert set(entry["outputs"]) == {"importance", "importance_csv", "correlation", "univariate"}
        for path in entry["outputs"].values():
            assert _nonempty_file(tmp_path / pd.Series([path]).iloc[0].split("/")[-1])
        assert len(entry["top_features"]) == 8
        assert len(entry["univariate_features"]) == 6
        assert isinstance(entry["redundant_pairs"], list)
    json.loads(json.dumps(report))


def test_generate_visualizations_importance_csv_matches_the_report(tmp_path):
    args = _make_args(out_dir=tmp_path, plots=["importance"], top_n=5)
    report = dv.generate_visualizations(_make_dataset(n=300), args, _make_mortgage_rates())
    entry = report["rf"]

    written = pd.read_csv(entry["outputs"]["importance_csv"])
    assert list(written.columns) == ["feature", "mean_abs_shap", "share"]
    assert len(written) == len(dv.FEATURE_MATRIX_COLUMNS)
    assert written["feature"].head(5).tolist() == [f["feature"] for f in entry["top_features"]]
    assert written["mean_abs_shap"].is_monotonic_decreasing


def test_generate_visualizations_honours_the_plots_selection(tmp_path):
    args = _make_args(out_dir=tmp_path, plots=["correlation"])
    report = dv.generate_visualizations(_make_dataset(n=300), args, _make_mortgage_rates())
    assert set(report["rf"]["outputs"]) == {"correlation"}
    assert "top_features" not in report["rf"]
    assert not list(tmp_path.glob("*importance*"))


def test_generate_visualizations_dedupes_univariate_panels_by_default(tmp_path):
    df = _make_dataset(n=400)
    deduped = dv.generate_visualizations(
        df,
        _make_args(out_dir=tmp_path / "a", plots=["univariate"], univariate_features=10),
        _make_mortgage_rates(),
    )
    raw = dv.generate_visualizations(
        df,
        _make_args(
            out_dir=tmp_path / "b", plots=["univariate"], univariate_features=10, univariate_dedupe=False
        ),
        _make_mortgage_rates(),
    )
    assert deduped["rf"]["univariate_features"] != raw["rf"]["univariate_features"]
    assert not {"total_floor_area", "log_floor_area"}.issubset(deduped["rf"]["univariate_features"])


def test_generate_visualizations_honours_an_explicit_feature_list(tmp_path):
    args = _make_args(
        out_dir=tmp_path, plots=["univariate"], features=["latitude", "longitude", "ptal_ai"]
    )
    report = dv.generate_visualizations(_make_dataset(n=300), args, _make_mortgage_rates())
    assert report["rf"]["univariate_features"] == ["latitude", "longitude", "ptal_ai"]


def test_generate_visualizations_can_rank_a_saved_model(tmp_path):
    """Check ``--model-in`` ranks a saved model's features."""
    model_path = tmp_path / "hpp_rf.joblib"
    train_args = hpp_rf.build_arg_parser().parse_args([])
    train_args.device, train_args.n_estimators, train_args.max_depth, train_args.n_jobs = "cpu", 5, 6, 1
    train_args.model_out = model_path
    df = _make_dataset(n=300)
    hpp_rf.run_training(df, train_args)

    args = _make_args(out_dir=tmp_path, plots=["importance"], model_in=model_path, top_n=5, trust_pickle=True)
    report = dv.generate_visualizations(df, args, _make_mortgage_rates())
    assert len(report["rf"]["top_features"]) == 5


def test_generate_visualizations_uses_the_requested_format_and_split(tmp_path):
    args = _make_args(out_dir=tmp_path, format="svg", analysis_split="test", plots=["correlation"])
    report = dv.generate_visualizations(_make_dataset(n=300), args, _make_mortgage_rates())
    assert report["rf"]["split"] == "test"
    assert report["rf"]["outputs"]["correlation"].endswith(".svg")


def test_build_arg_parser_defaults_match_the_models():
    args = dv.build_arg_parser().parse_args([])
    assert args.model == "rf"
    assert args.plots == list(dv.PLOT_CHOICES)
    assert args.analysis_split == "train"
    assert args.log_target is True
    assert args.cluster_heatmap is True
    rf_args = hpp_rf.build_arg_parser().parse_args([])
    assert args.split_method == rf_args.split_method
    assert (args.train_frac, args.val_frac) == (rf_args.train_frac, rf_args.val_frac)
    assert args.random_state == rf_args.random_state
    assert args.max_features == rf_args.max_features


def test_build_arg_parser_flags_can_be_turned_off():
    args = dv.build_arg_parser().parse_args(
        ["--no-cluster-heatmap", "--no-log-target", "--model", "catboost", "--plots", "importance"]
    )
    assert args.cluster_heatmap is False
    assert args.log_target is False
    assert dv.build_arg_parser().parse_args([]).min_variation_frac == dv.DEFAULT_MIN_VARIATION_FRAC
    assert dv.build_arg_parser().parse_args([]).univariate_dedupe is True
    assert dv.build_arg_parser().parse_args(["--no-dedupe-univariate"]).univariate_dedupe is False
    assert args.model == "catboost"
    assert args.plots == ["importance"]




def test_generate_visualizations_against_production_london_houses(tmp_path, hpdbldn_ro):
    engine = hpdbldn_ro
    df = _load_production_sample(engine, limit=20_000)
    args = _make_args(out_dir=tmp_path, analysis_sample=5_000, fit_sample=5_000, univariate_features=8)
    report = dv.generate_visualizations(df, args, hpp_cb.load_mortgage_rates(engine))

    entry = report["rf"]
    # A view before migration 0034 has no Ofsted columns, and they are left out.
    with_ofsted = set(hpp_rf.OFSTED_COLUMNS) <= set(df.columns)
    assert entry["n_features"] == len(hpp_rf.feature_layout(with_ofsted)["matrix_columns"])
    assert entry["n_rows"] > 0
    assert set(entry["outputs"]) == {"importance", "importance_csv", "correlation", "univariate"}
    for path in entry["outputs"].values():
        assert _nonempty_file(tmp_path / path.split("/")[-1])
