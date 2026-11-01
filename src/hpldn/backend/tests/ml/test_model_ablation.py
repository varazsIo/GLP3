"""
Tests for `hpldn.backend.scripts.model_ablation`.

Covers ranking order and validation, the 128-feature ceiling, matrix column
names, log parsing, CLI options and an end-to-end sweep with the database
stubbed out, all on `test_hp_rf`'s synthetic dataset. No database or SHAP
ranking artefact is needed.
"""

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.scripts import hpp_cb, hpp_rf, model_ablation as ma
from hpldn.backend.scripts.data_visualization import FEATURE_MATRIX_COLUMNS
from hpldn.backend.tests.ml.test_hp_rf import _make_args, _make_dataset

BASELINE_FEATURES = 10
MAX_ABLATION_FEATURES = 16
EXPECTED_RUNS = MAX_ABLATION_FEATURES - BASELINE_FEATURES


def _empty_mortgage_rates() -> pd.DataFrame:
    """Return an empty ``production.mortgage_rates`` frame for synthetic data."""
    return pd.DataFrame(
        {
            hpp_cb.MORTGAGE_RATE_DATE_COLUMN: pd.Series(dtype="datetime64[us]"),
            hpp_cb.MORTGAGE_RATE_COLUMN: pd.Series(dtype="float64"),
        }
    )


def _fast_args(**overrides):
    """Return `hpp_rf`'s defaults shrunk to a small, fast forest (10 trees, depth 6)."""
    args = dict(device="cpu", n_estimators=10, max_depth=6, n_jobs=1, random_state=0)
    args.update(overrides)
    return _make_args(**args)




def test_load_feature_ranking_sorts_by_score_not_file_order(tmp_path):
    """Check the loader sorts by SHAP score rather than trusting file order."""
    path = tmp_path / "importance.csv"
    path.write_text(
        "feature,mean_abs_shap,share\n"
        "total_floor_area,0.01,0.1\n"
        "comp_median_price,0.05,0.5\n"
        "log_floor_area,0.03,0.3\n",
        encoding="utf-8",
    )
    assert ma.load_feature_ranking(path) == [
        "comp_median_price",
        "log_floor_area",
        "total_floor_area",
    ]


def test_load_feature_ranking_rejects_feature_not_in_matrix(tmp_path):
    path = tmp_path / "stale.csv"
    path.write_text(
        "feature,mean_abs_shap\ncomp_median_price,0.05\na_column_hpp_rf_dropped,0.01\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="a_column_hpp_rf_dropped"):
        ma.load_feature_ranking(path)


def test_load_feature_ranking_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ma.load_feature_ranking(tmp_path / "nope.csv")


def test_load_feature_ranking_rejects_missing_columns(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("feature,importance\ncomp_median_price,0.05\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mean_abs_shap"):
        ma.load_feature_ranking(path)




def test_resolve_run_plan_supports_128_features_when_the_ranking_has_them():
    """Check the 128-feature ceiling against a ranking long enough to reach it."""
    long_ranking = [f"feature_{i:03d}" for i in range(200)]
    plans = ma.resolve_run_plan(long_ranking, BASELINE_FEATURES, 128)
    assert len(plans[-1].features) == 128
    assert len(plans) == 128 - BASELINE_FEATURES + 1




@pytest.fixture(scope="module")
def prepared():
    return ma.prepare_splits(_make_dataset(n=600, seed=3), _fast_args())


def test_prepare_splits_matrix_columns_are_the_named_features(prepared):
    """Check column ``i`` of the prepared matrix is ``FEATURE_MATRIX_COLUMNS[i]``."""
    assert prepared.feature_names == list(FEATURE_MATRIX_COLUMNS)
    for X, n in (
        (prepared.X_train, prepared.n_train),
        (prepared.X_val, prepared.n_val),
        (prepared.X_test, prepared.n_test),
    ):
        assert X.shape == (n, len(FEATURE_MATRIX_COLUMNS))


def test_column_indices_rejects_unknown_feature(prepared):
    with pytest.raises(KeyError, match="not_a_feature"):
        prepared.column_indices(["total_floor_area", "not_a_feature"])


def test_parse_rejects_a_file_that_is_not_an_ablation_log(tmp_path):
    path = tmp_path / "notalog.log"
    path.write_text("some other program's output\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not an ablation log"):
        ma.parse_ablation_log(path)




def test_max_ablation_features_is_distinct_from_sklearn_max_features():
    """Check ``--max-ablation-features`` is distinct from scikit-learn's ``--max-features``."""
    args = ma.build_arg_parser().parse_args(["--max-ablation-features", "16", "--max-features", "log2"])
    assert args.max_ablation_features == 16
    assert args.max_features == "log2"


def test_cli_defaults_match_the_documented_ceiling():
    args = ma.build_arg_parser().parse_args([])
    assert args.baseline_features == ma.DEFAULT_BASELINE_FEATURES == 10
    assert args.max_ablation_features == ma.DEFAULT_MAX_ABLATION_FEATURES == 128
    assert args.log_dir == ma.DEFAULT_LOG_DIR
    assert args.importance_csv == ma.DEFAULT_IMPORTANCE_CSV


def _write_ranking_csv(path: Path, n_features: int) -> Path:
    """
    Write a ranking CSV of real feature-matrix columns to `path`.

    Keeps the end-to-end CLI test independent of the gitignored SHAP ranking
    artefact.
    """
    features = list(FEATURE_MATRIX_COLUMNS)[:n_features]
    lines = [f"{ma.IMPORTANCE_FEATURE_COLUMN},{ma.IMPORTANCE_SCORE_COLUMN}"]
    lines += [
        f"{name},{score}"
        for score, name in enumerate(features, start=1)
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_main_runs_the_sweep_with_the_database_stubbed_out(tmp_path, monkeypatch, capsys):
    """Check the CLI end to end with only the database read stubbed."""
    monkeypatch.setattr(ma, "load_env_vars", lambda log=None: None)
    monkeypatch.setattr(ma, "get_model_db_url", lambda log=None: "sqlite://")
    monkeypatch.setattr(
        hpp_rf, "load_dataset", lambda engine, limit=None, log=None, use_gpu=False: _make_dataset(n=600, seed=3)
    )
    importance_csv = _write_ranking_csv(
        tmp_path / "rf_shap_feature_importance.csv", BASELINE_FEATURES + 4
    )
    log_dir = tmp_path / "log"
    log_dir.mkdir()

    summary = ma.main(
        [
            "--device", "cpu",
            "--n-estimators", "10",
            "--max-depth", "6",
            "--n-jobs", "1",
            "--random-state", "0",
            "--baseline-features", str(BASELINE_FEATURES),
            "--max-ablation-features", str(BASELINE_FEATURES + 2),
            "--importance-csv", str(importance_csv),
            "--log-dir", str(log_dir),
        ]
    )

    assert summary["n_runs"] == 3
    assert {path.name for path in log_dir.iterdir()} == {
        ma.BASELINE_LOG_NAME,
        "ablation_run_1.log",
        "ablation_run_2.log",
    }
    stdout = capsys.readouterr().out
    printed = json.loads(stdout[re.search(r"^\{$", stdout, re.MULTILINE).start():])
    assert printed["n_runs"] == summary["n_runs"]
    assert [run["log"] for run in printed["runs"]] == [run["log"] for run in summary["runs"]]


@pytest.fixture(scope="module")
def prepared_catboost():
    """
    Return CatBoost `PreparedSplits` over the synthetic frame.

    Built by hand because `prepare_splits` enables IoD features, which the
    synthetic dataset lacks.
    """
    df = hpp_rf.engineer_features(_make_dataset(n=600, seed=3))
    df = df.sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    df = hpp_cb.add_mortgage_rate_features(df, _empty_mortgage_rates())
    train_df, val_df, test_df = hpp_rf.time_series_split(df, train_frac=0.7, val_frac=0.15)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)
    X_train, X_val, X_test, cat_features, _, feature_metadata = hpp_cb.prepare_features(
        train_df, val_df, test_df, use_iod=False
    )
    return ma.PreparedSplits(
        X_train=X_train,
        X_val=X_val,
        X_test=X_test,
        y_train=train_df[hpp_rf.TARGET_COLUMN].to_numpy(dtype=float),
        y_val=val_df[hpp_rf.TARGET_COLUMN].to_numpy(dtype=float),
        y_test=test_df[hpp_rf.TARGET_COLUMN].to_numpy(dtype=float),
        feature_names=feature_metadata["feature_columns"],
        xp=np,
        dtype=np.float64,
        n_train=len(train_df),
        n_val=len(val_df),
        n_test=len(test_df),
        model=ma.MODEL_CATBOOST,
        cat_features=list(cat_features),
    )


def test_fit_subset_catboost_never_early_stops_on_the_val_split(prepared_catboost):
    """Check CatBoost early-stops on a tail of train and never touches the validation split."""
    from catboost import CatBoostRegressor, Pool

    eval_row_counts = []
    original_fit = CatBoostRegressor.fit

    def _recording_fit(self, *a, **kw):
        eval_set = kw.get("eval_set")
        eval_row_counts.append(eval_set.num_row() if isinstance(eval_set, Pool) else None)
        return original_fit(self, *a, **kw)

    CatBoostRegressor.fit = _recording_fit
    try:
        args = ma.build_arg_parser().parse_args(
            [
                "--model", ma.MODEL_CATBOOST, "--device", "cpu",
                "--cb-iterations", "20", "--cb-early-stopping-rounds", "5",
                "--cb-thread-count", "1", "--cb-eval-frac", "0.1",
                "--random-state", "0",
            ]
        )
        results = ma.fit_subset(
            prepared_catboost, ["total_floor_area", "comp_median_price", "propertytype"], args
        )
    finally:
        CatBoostRegressor.fit = original_fit

    n_train = prepared_catboost.n_train
    expected_carve = int(hpp_cb._carve_eval_mask(n_train, frac=args.cb_eval_frac).sum())
    assert results["n_eval_carve"] == expected_carve
    assert expected_carve != prepared_catboost.n_val, "fixture must make the two sizes distinguishable"

    assert eval_row_counts == [expected_carve, None], (
        "expected a two-pass fit: pass 1 early-stopping on the carved train tail, "
        "pass 2 refitting on all of train with no eval_set"
    )
    assert prepared_catboost.n_val not in eval_row_counts, "val must never be the eval_set"
    for split in ("val", "test"):
        assert results[split]["mae"] > 0
