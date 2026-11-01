"""
Parity and wiring tests for the CatBoost training-speed code paths.

Every vectorised hotspot is compared with the implementation it replaced
(kept verbatim in `_training_speed_reference`): the rolling causal statistics, the
size-matched comparables and the local index's batched solves must be bit
for bit the old ones on the CPU, and agree to rounding on the GPU (marker
``gpu``). Then the "do work once" and fit-strategy wiring: categoricals cast
once, the feature cache's round trip, ``--fold-refit``, concurrent folds, the
fit gate, the batched property loader (read-only on hpdbldn) and an
end-to-end `hpp_cb.main` run that profiles itself and hits its cache.
"""

import json

import numpy as np
import pandas as pd
import pytest

from hpldn.backend.common import accel
from hpldn.backend.scripts import cb_fit_gate, feature_cache, hpp_cb, hpp_rf, local_index
from hpldn.backend.tests.ml import _training_speed_reference as reference
from hpldn.backend.tests.ml.test_hp_rf import _make_dataset, _make_mortgage_rates


def _make_args(**overrides):
    args = hpp_cb.build_arg_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _require_gpu():
    accel.reset_backend()
    if not accel.gpu_enabled():
        pytest.fail("missing prerequisite: a CUDA GPU CuPy can use (pip install '.[gpu]')", pytrace=False)


# ---------------------------------------------------------------------------
# Rolling causal statistics (polars)
# ---------------------------------------------------------------------------


def _rolling_frame(n=20_000, seed=1):
    rng = np.random.default_rng(seed)
    days = rng.integers(0, 2500, n)
    df = pd.DataFrame(
        {
            hpp_rf.DATE_COLUMN: (pd.Timestamp("2012-01-01") + pd.to_timedelta(days, "D")).date,
            "sector": rng.integers(0, 200, n).astype(str).astype(object),
            "kind": rng.choice(list("DFST"), n),
            "price": rng.lognormal(12, 0.6, n).astype(np.float32),
        }
    )
    df.loc[rng.random(n) < 0.05, "price"] = np.nan
    # Both null spellings a key column carries.
    df.loc[rng.random(n) < 0.01, "sector"] = None
    df.loc[rng.random(n) < 0.01, "sector"] = np.nan
    df.index = rng.permutation(n) + 11
    return df


@pytest.mark.parametrize("group_cols", [["sector", "kind"], ["kind"], ["sector"]])
@pytest.mark.parametrize("window_months", [3, 12, 24])
def test_rolling_causal_group_stats_is_bit_for_bit_the_pandas_loop(group_cols, window_months):
    df = _rolling_frame()
    expected_median, expected_count = reference.rolling_causal_group_stats(df, group_cols, window_months, "price")
    median, count = hpp_rf._rolling_causal_group_stats(df, group_cols, window_months, "price")
    pd.testing.assert_series_equal(median, expected_median, check_exact=True, check_names=False)
    pd.testing.assert_series_equal(count, expected_count, check_exact=True, check_names=False)


def test_rolling_causal_group_stats_on_an_empty_frame():
    df = _rolling_frame().iloc[0:0]
    median, count = hpp_rf._rolling_causal_group_stats(df, ["sector"], 12, "price")
    assert median.empty and count.empty


def test_engineered_frame_rolling_and_street_columns_match_the_reference():
    """The columns engineer_features builds through the polars path equal the old per-group loop's."""
    df = _make_dataset(n=1500, seed=4)
    engineered = hpp_rf.engineer_features(df)
    augmented = engineered[[hpp_rf.DATE_COLUMN, hpp_rf.TARGET_COLUMN, "postcode_district"]].copy()
    augmented["_street_group"] = augmented["postcode_district"].str.cat(hpp_rf.street_key_series(engineered), sep="_")
    expected_median, expected_count = reference.rolling_causal_group_stats(
        augmented, ["_street_group"], hpp_rf.SAME_STREET_WINDOW_MONTHS, hpp_rf.TARGET_COLUMN
    )
    np.testing.assert_array_equal(engineered["street_sales_median_price_12m"].to_numpy(), expected_median.to_numpy())
    np.testing.assert_array_equal(engineered["street_sales_count_12m"].to_numpy(), expected_count.to_numpy())


# ---------------------------------------------------------------------------
# Size-matched comparables
# ---------------------------------------------------------------------------


def _ukhpi(start="2000-01-01", end="2020-12-01"):
    months = pd.date_range(start, end, freq="MS")
    t = np.arange(len(months))
    return pd.DataFrame(
        {
            "period": months,
            "avg_price_all_property_types": 300_000 * np.exp(0.003 * t),
            "avg_price_detached": 300_000 * np.exp(0.004 * t),
            "avg_price_semi_detached": 300_000 * np.exp(0.003 * t),
            "avg_price_terraced": 300_000 * np.exp(0.002 * t),
            "avg_price_flats_maisonettes": 300_000 * np.exp(0.001 * t),
        }
    )


def _repeat_pairs(n=6000, seed=3, districts=("SW1", "SE10", "N1", "E17"), types=("F", "T")):
    """Synthetic repeat-sale pairs with district-specific drift, enough for district and borough series."""
    rng = np.random.default_rng(seed)
    district = rng.choice(list(districts), n)
    borough = np.where(np.isin(district, ["SW1", "SE10"]), "Westminster", "Islington")
    first = pd.Timestamp("2001-01-01") + pd.to_timedelta(rng.integers(0, 4000, n), "D")
    hold = pd.to_timedelta(rng.integers(200, 3000, n), "D")
    second = first + hold
    drift = np.select([district == "SW1", district == "SE10", district == "N1"], [0.006, 0.002, 0.004], 0.001)
    months = (hold.days / 30.4).to_numpy()
    prev_price = rng.lognormal(12.5, 0.4, n)
    price = prev_price * np.exp(drift * months + rng.normal(0, 0.05, n))
    pairs = pd.DataFrame(
        {
            "postcode_district": district,
            "borough": borough,
            "propertytype": rng.choice(list(types), n),
            "prev_date": first.date,
            "dateoftransfer": second.date,
            "prev_price": prev_price,
            "price": price,
        }
    )
    return pairs[pd.to_datetime(pairs["dateoftransfer"]) <= pd.Timestamp("2020-12-01")].reset_index(drop=True)


@pytest.fixture(scope="module")
def district_index():
    """A local index with district and borough series, whose anchor (2020-12) some sales lie beyond."""
    return local_index.LocalIndex.build(_repeat_pairs(), _ukhpi(), {"smoothing": {"borough": 300.0, "district": 1000.0}})


def _comparables_frame(n=6000, seed=5, start="2018-01-01"):
    df = _make_dataset(n=n, seed=seed, start=start, periods_days=1500)
    df["latitude"] = 51.5074 + (df["latitude"] - 51.5074) * 0.15
    df["longitude"] = -0.1278 + (df["longitude"] + 0.1278) * 0.15
    # Ties on the radius rule and a district the index has no series for.
    df.loc[df.index[::97], "postcode_district"] = None
    return df


def test_district_index_has_local_series(district_index):
    assert any(key.startswith("district:") for key in district_index.pairs)
    assert any(key.startswith("borough:") for key in district_index.pairs)


@pytest.mark.parametrize("with_index", [False, True])
def test_sizeband_comparables_cpu_are_bit_for_bit_the_per_row_loop(with_index, district_index):
    df = _comparables_frame()
    index = district_index if with_index else None
    expected = reference.add_sizeband_comp_features(df, index)
    got = hpp_rf.add_sizeband_comp_features(df, index, backend="cpu")
    assert int(np.isfinite(expected["comp_sizeband_pps"]).sum()) > len(df) // 2
    for col in hpp_rf.SIZEBAND_COMP_COLUMNS:
        pd.testing.assert_series_equal(got[col], expected[col], check_exact=True)
    pd.testing.assert_frame_equal(got.drop(columns=hpp_rf.SIZEBAND_COMP_COLUMNS), df)


@pytest.mark.gpu
@pytest.mark.parametrize("with_index", [False, True])
def test_sizeband_comparables_gpu_agree_with_the_per_row_loop(with_index, district_index):
    _require_gpu()
    df = _comparables_frame()
    index = district_index if with_index else None
    expected = reference.add_sizeband_comp_features(df, index)
    got = hpp_rf.add_sizeband_comp_features(df, index, backend="gpu")
    for col in hpp_rf.SIZEBAND_COMP_COLUMNS:
        np.testing.assert_array_equal(np.isfinite(got[col]), np.isfinite(expected[col]))
        np.testing.assert_allclose(got[col], expected[col], rtol=1e-12, equal_nan=True)


def test_sizeband_backend_validation():
    with pytest.raises(ValueError, match="backend"):
        hpp_rf.add_sizeband_comp_features(_comparables_frame(n=50), backend="tpu")


def test_segment_medians_are_numpy_medians():
    rng = np.random.default_rng(0)
    segment = rng.integers(0, 50, 5000)
    values = rng.normal(size=5000)
    got = hpp_rf._segment_medians(np, segment, values, 52)
    for k in range(52):
        chosen = values[segment == k]
        if chosen.size:
            assert got[k] == np.median(chosen)
        else:
            assert np.isnan(got[k])


# ---------------------------------------------------------------------------
# Local index: batched solves and the uplift cache
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def prepared_pairs():
    frame, periods, _ = local_index.prepare_pairs(_repeat_pairs(), _ukhpi())
    return frame, len(periods)


def test_estimate_offsets_batched_is_bit_for_bit_the_per_series_loop(prepared_pairs):
    frame, n_months = prepared_pairs
    smoothing = {"borough": 300.0, "district": 1000.0}
    expected = reference.estimate_offsets(frame, n_months, smoothing)
    got = local_index.estimate_offsets(frame, n_months, smoothing)
    assert got[1] == expected[1] and got[2] == expected[2] and expected[0]
    for key, offsets in expected[0].items():
        np.testing.assert_array_equal(got[0][key], offsets)


def test_choose_smoothing_batched_is_bit_for_bit_the_per_series_loop(prepared_pairs):
    frame, n_months = prepared_pairs
    assert local_index.choose_smoothing(frame, n_months) == reference.choose_smoothing(frame, n_months)


def test_solve_batch_spans_several_batches(prepared_pairs, monkeypatch):
    frame, n_months = prepared_pairs
    smoothing = {"borough": 300.0, "district": 1000.0}
    expected = local_index.estimate_offsets(frame, n_months, smoothing)
    monkeypatch.setattr(local_index, "SOLVE_BATCH", 1)
    got = local_index.estimate_offsets(frame, n_months, smoothing)
    for key, offsets in expected[0].items():
        np.testing.assert_array_equal(got[0][key], offsets)


@pytest.mark.gpu
def test_batched_solves_on_the_gpu_agree_to_rounding(prepared_pairs):
    _require_gpu()
    frame, n_months = prepared_pairs
    smoothing = {"borough": 300.0, "district": 1000.0}
    expected = local_index.estimate_offsets(frame, n_months, smoothing)
    got = local_index.estimate_offsets(frame, n_months, smoothing, xp=accel.get_array_module())
    for key, offsets in expected[0].items():
        np.testing.assert_allclose(got[0][key], offsets, rtol=1e-9, atol=1e-12)
    assert local_index.choose_smoothing(frame, n_months, xp=accel.get_array_module())[0] == (
        local_index.choose_smoothing(frame, n_months)[0]
    )


def test_uplift_cache_returns_the_uncached_levels(district_index):
    days = np.arange(18_000, 19_500, 7, dtype=np.int64)  # 2019-04 .. 2023-05, past the 2020-12 anchor
    first = district_index.log_levels(days, "SW1", "F")
    district_index._uplift_cache.clear()
    fresh = district_index.log_levels(days, "SW1", "F")
    np.testing.assert_array_equal(first, fresh)
    assert district_index._uplift_cache


# ---------------------------------------------------------------------------
# Categoricals cast once
# ---------------------------------------------------------------------------


def test_canonical_categoricals_give_the_same_feature_frames():
    df = hpp_rf.engineer_features(_make_dataset(n=900, seed=12)).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    df = hpp_cb.add_mortgage_rate_features(df, _make_mortgage_rates())
    if "iod_release_year" in df.columns:
        df.loc[df.index[::3], "iod_release_year"] = 2019.0
    shadowed = hpp_cb.add_canonical_categoricals(df)
    assert all(hpp_cb.CANONICAL_CATEGORICAL_PREFIX + c in shadowed.columns for c in hpp_rf.CATEGORICAL_COLUMNS)
    split = len(df) * 3 // 4

    def _features(frame):
        train, val, test, *_ = hpp_cb._add_price_stat_columns(
            frame.iloc[:split], frame.iloc[split:], frame.iloc[split:], random_state=0, deflate=False
        )
        return hpp_cb.prepare_features(train, val, test)

    plain, cast_once = _features(df), _features(shadowed)
    for a, b in zip(plain[:3], cast_once[:3]):
        pd.testing.assert_frame_equal(a, b, check_exact=True)
    assert plain[3] == cast_once[3] and plain[5]["feature_columns"] == cast_once[5]["feature_columns"]
    # Idempotent: adding them twice changes nothing.
    pd.testing.assert_frame_equal(hpp_cb.add_canonical_categoricals(shadowed), shadowed)


# ---------------------------------------------------------------------------
# Pools, fold refit and concurrent folds
# ---------------------------------------------------------------------------


def test_pool_slices_train_exactly_like_fresh_pools():
    """The property `_fit_and_evaluate`'s single Pool relies on (guards a CatBoost upgrade)."""
    from catboost import CatBoostRegressor, Pool

    rng = np.random.default_rng(0)
    n = 3000
    X = pd.DataFrame({"a": rng.normal(size=n), "c": rng.integers(0, 40, n).astype(str)})
    y = X["a"].to_numpy() * 2 + (X["c"].astype(int) % 5).to_numpy() + rng.normal(size=n) * 0.1
    w = rng.uniform(0.5, 1.5, n)
    kw = dict(iterations=60, random_seed=1, verbose=False, allow_writing_files=False, has_time=True, thread_count=2)
    full = Pool(X, y, cat_features=["c"], weight=w)
    fit_slice, eval_slice = full.slice(np.arange(2400)), full.slice(np.arange(2400, n))
    fit_slice.set_weight(w[:2400] * 2)
    eval_slice.set_weight(w[2400:] * 2)
    fresh_fit = Pool(X.iloc[:2400], y[:2400], cat_features=["c"], weight=w[:2400] * 2)
    fresh_eval = Pool(X.iloc[2400:], y[2400:], cat_features=["c"], weight=w[2400:] * 2)
    a = CatBoostRegressor(**kw, early_stopping_rounds=10).fit(fit_slice, eval_set=eval_slice, use_best_model=True)
    b = CatBoostRegressor(**kw, early_stopping_rounds=10).fit(fresh_fit, eval_set=fresh_eval, use_best_model=True)
    np.testing.assert_array_equal(a.predict(X), b.predict(X))


def _backtest_args(**overrides):
    settings = dict(
        device="cpu",
        iterations=15,
        early_stopping_rounds=5,
        thread_count=1,
        random_state=0,
        allow_truncated_holdout=True,
        backtest_min_train_rows=50,
    )
    return _make_args(**{**settings, **overrides})


def test_fold_refit_final_only_scores_folds_without_refitting():
    df = _make_dataset(n=1500, seed=9, start="2015-01-01", periods_days=3650)
    full = hpp_cb.backtest_expanding_window(df, _backtest_args(), _make_mortgage_rates())
    lean = hpp_cb.backtest_expanding_window(df, _backtest_args(fold_refit="final-only"), _make_mortgage_rates())
    assert all(f["fold_refit"] == "full" and f["refit_iterations"] for f in full["folds"])
    assert all(f["fold_refit"] == "final-only" and f["refit_iterations"] is None for f in lean["folds"])
    # The final holdout model is always refitted, so it is the same model.
    assert lean["final_holdout"]["fold_refit"] == "full"
    assert lean["final_holdout"]["test"]["mae"] == full["final_holdout"]["test"]["mae"]
    assert [f["test_year"] for f in lean["folds"]] == [f["test_year"] for f in full["folds"]]


def test_concurrent_folds_score_exactly_like_sequential_ones():
    df = _make_dataset(n=1500, seed=9, start="2015-01-01", periods_days=3650)
    sequential = hpp_cb.backtest_expanding_window(df, _backtest_args(fold_concurrency="1"), _make_mortgage_rates())
    concurrent = hpp_cb.backtest_expanding_window(
        df, _backtest_args(fold_concurrency="3", thread_count=None), _make_mortgage_rates()
    )
    assert len(sequential["folds"]) >= 2
    for a, b in zip(sequential["folds"], concurrent["folds"]):
        assert a["test_year"] == b["test_year"] and a["test"]["mae"] == b["test"]["mae"]
    assert sequential["aggregate"] == concurrent["aggregate"]


def test_resource_plan_is_cached_and_cpu_flags_reach_catboost(monkeypatch):
    args = _make_args(device="cpu", thread_count=3, used_ram_limit="2gb")
    kwargs, backend = hpp_cb._catboost_kwargs(args)
    assert kwargs["task_type"] == "CPU" and kwargs["thread_count"] == 3 and kwargs["used_ram_limit"] == "2gb"
    assert "devices" not in kwargs and "3 threads" in backend
    assert hpp_cb.resource_plan(args) is args.resource_plan
    monkeypatch.setenv("HPLDN_DISABLE_GPU", "1")
    auto = _make_args(device="auto")
    assert hpp_cb.resource_plan(auto).task_type == "CPU"


def test_fold_concurrency_flag_is_validated():
    with pytest.raises(ValueError, match="fold-concurrency"):
        hpp_cb._validate_args(_make_args(fold_concurrency="0"))
    with pytest.raises(ValueError, match="fold-concurrency"):
        hpp_cb._validate_args(_make_args(fold_concurrency="many"))
    hpp_cb._validate_args(_make_args(fold_concurrency="2"))


# ---------------------------------------------------------------------------
# Feature cache
# ---------------------------------------------------------------------------


def _engineered():
    df = _make_dataset(n=1200, seed=21)
    return hpp_cb._engineer_training_frame(df, _make_mortgage_rates(), None, market_features=True, local_index=None)


def test_feature_cache_round_trip_is_exact(tmp_path):
    df = _engineered()
    assert any(c.startswith(hpp_cb.CANONICAL_CATEGORICAL_PREFIX) for c in df.columns)
    assert feature_cache.load("missing", tmp_path) is None
    path = feature_cache.save("k1", df, {"state": {"rows": len(df)}}, tmp_path)
    assert path is not None and json.loads(path.with_suffix(".json").read_text())["rows"] == len(df)
    pd.testing.assert_frame_equal(feature_cache.load("k1", tmp_path), df, check_exact=True)


def test_feature_cache_key_tracks_state_parameters_and_code():
    state = {"alembic": "0034", "rows": 10, "max_ppd_id": 99}
    args = _make_args()
    params = feature_cache.run_parameters(args, use_gpu=False)
    key = feature_cache.cache_key(state, params, "code")
    assert key == feature_cache.cache_key(dict(state), dict(params), "code")
    assert key != feature_cache.cache_key({**state, "max_ppd_id": 100}, params, "code")
    assert key != feature_cache.cache_key(state, feature_cache.run_parameters(args, use_gpu=True), "code")
    assert key != feature_cache.cache_key(state, feature_cache.run_parameters(_make_args(backtest=True), False), "code")
    assert key != feature_cache.cache_key(state, params, "other code")
    assert len(feature_cache.feature_code_hash()) == 64


def test_feature_cache_prunes_to_the_newest_entries(tmp_path):
    df = pd.DataFrame({"a": [1.0]})
    for i in range(5):
        feature_cache.save(f"k{i}", df, {}, tmp_path)
    assert len(list(tmp_path.glob("*.parquet"))) == feature_cache.KEEP_ENTRIES
    assert len(list(tmp_path.glob("*.json"))) == feature_cache.KEEP_ENTRIES


def test_a_broken_cache_entry_is_a_miss(tmp_path):
    (tmp_path / "bad.parquet").write_text("not parquet")
    assert feature_cache.load("bad", tmp_path) is None


# ---------------------------------------------------------------------------
# Fit gate
# ---------------------------------------------------------------------------


def _backtest_results(mdape):
    return {"aggregate_scoped": {"mdape": {"weighted_mean": mdape}}}


def test_fit_gate_allows_at_most_a_tenth_of_a_point(tmp_path):
    base = _backtest_results(0.0770)
    assert cb_fit_gate.fit_gate(base, _backtest_results(0.0779))["passed"]
    assert cb_fit_gate.fit_gate(base, _backtest_results(0.0700))["passed"]
    failed = cb_fit_gate.fit_gate(base, _backtest_results(0.0790))
    assert not failed["passed"] and failed["delta_pp"] == pytest.approx(0.2)
    with pytest.raises(ValueError, match="--backtest"):
        cb_fit_gate.scoped_mdape({"test": {}})
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "c.json").write_text(json.dumps(_backtest_results(0.0790)))
    assert cb_fit_gate.main(["gate", str(tmp_path / "b.json"), str(tmp_path / "c.json")]) == 1


def test_fit_sweep_commands_and_summary(tmp_path):
    commands = cb_fit_gate.sweep_commands(tmp_path, ["--iterations", "100"], ["ctr1", "fold_refit_final_only"], save_models=True)
    assert set(commands) == {"baseline", "ctr1", "fold_refit_final_only"}
    assert "--backtest" in commands["ctr1"] and commands["ctr1"][-2:] == ["--model-out", str(tmp_path / "models" / "ctr1")]
    assert ["--max-ctr-complexity", "1"] == commands["ctr1"][commands["ctr1"].index("--max-ctr-complexity") :][:2]
    # Gate pre-existing results (a sweep never re-runs a finished one).
    for name, mdape, wall in (("baseline", 0.077, 1000.0), ("ctr1", 0.0775, 400.0), ("fold_refit_final_only", 0.080, 500.0)):
        (tmp_path / f"{name}.results.json").write_text(json.dumps(_backtest_results(mdape)))
        (tmp_path / f"{name}.profile.json").write_text(json.dumps({"wall_s": wall}))
    summary = cb_fit_gate.run_sweep(tmp_path, [], ["ctr1", "fold_refit_final_only"])
    assert summary["candidates"]["ctr1"]["passed"] and summary["candidates"]["ctr1"]["speedup"] == 2.5
    assert not summary["candidates"]["fold_refit_final_only"]["passed"]
    assert json.loads((tmp_path / "summary.json").read_text()) == summary


# ---------------------------------------------------------------------------
# Against hpdbldn (read-only)
# ---------------------------------------------------------------------------


def test_batched_property_loader_returns_the_single_id_records(hpdbldn_ro):
    from dataclasses import fields

    from sqlalchemy import text

    from hpldn.backend.scripts import hpp_inference

    with hpdbldn_ro.connect() as connection:
        ids = [
            int(r[0])
            for r in connection.execute(
                text(
                    "SELECT p.address_id FROM production.ppd_table p "
                    "JOIN production.london_houses lh ON lh.ppd_id = p.id ORDER BY p.id DESC LIMIT 60"
                )
            )
        ]
        single = {i: hpp_inference.load_property_by_ids(connection, [i]) for i in ids}
        batched = hpp_inference.load_properties_by_ids(connection, ids + ids[:5], batch=25)
    assert set(batched) == set(ids)
    for address_id in ids:
        a, b = single[address_id], batched[address_id]
        for field in fields(a):
            x, y = getattr(a, field.name), getattr(b, field.name)
            if isinstance(x, pd.DataFrame):
                assert (x.empty and y.empty) or x.astype(object).equals(y.astype(object)), (address_id, field.name)
            elif isinstance(x, pd.Series):
                assert x.equals(y), (address_id, field.name)
            else:
                assert x == y, (address_id, field.name)


def test_main_profiles_a_run_and_reuses_its_feature_cache(hpdbldn_ro, hpdbldn_url, tmp_path, monkeypatch):
    """`hpp_cb.main` end to end on 4,000 hpdbldn rows: profile JSON, results JSON, then a cache hit."""
    from hpldn.backend.tests.conftest import READ_ONLY_OPTIONS

    monkeypatch.setattr(hpp_cb, "get_model_db_url", lambda log=None: hpdbldn_url.update_query_dict({"options": READ_ONLY_OPTIONS}))
    monkeypatch.setenv("HPLDN_FEATURE_CACHE_DIR", str(tmp_path / "cache"))
    argv = [
        "--device", "cpu", "--limit", "4000", "--iterations", "20", "--early-stopping-rounds", "5",
        "--no-market-features", "--target", "log_price", "--thread-count", "2",
    ]
    with hpdbldn_ro.connect() as connection:
        columns = set(pd.read_sql("SELECT * FROM production.london_houses LIMIT 0", connection).columns)
    if not set(hpp_rf.OFSTED_COLUMNS) <= columns:
        argv.append("--no-ofsted")
    first = hpp_cb.main([*argv, "--profile-json", str(tmp_path / "p1.json"), "--results-json", str(tmp_path / "r1.json")])
    second = hpp_cb.main([*argv, "--profile-json", str(tmp_path / "p2.json")])
    profile1 = json.loads((tmp_path / "p1.json").read_text())
    profile2 = json.loads((tmp_path / "p2.json").read_text())
    names1 = {stage["name"] for stage in profile1["stages"]}
    assert {"load", "features/engineer_features", "fit/pass1", "fit/pass2", "feature_cache/write"} <= names1
    assert profile1["notes"]["feature_cache"]["hit"] is False and profile2["notes"]["feature_cache"]["hit"] is True
    assert "load" not in {stage["name"] for stage in profile2["stages"]}
    assert profile1["notes"]["engineered_frame"] == profile2["notes"]["engineered_frame"]
    assert profile1["notes"]["resource_plan"]["task_type"] == "CPU"
    assert json.loads((tmp_path / "r1.json").read_text())["test"]["mae"] == first["test"]["mae"]
    assert second["test"]["mae"] == first["test"]["mae"]


@pytest.mark.gpu
def test_the_gpu_plans_catboost_parameters_train():
    """Every parameter the GPU plan sets is one CatBoost accepts (pinned_memory_size only as a string)."""
    from catboost import CatBoostRegressor

    _require_gpu()
    args = _make_args(device="gpu")
    kwargs, backend = hpp_cb._catboost_kwargs(args)
    assert kwargs["task_type"] == "GPU" and kwargs["devices"] and "gpu" in backend
    assert isinstance(kwargs["pinned_memory_size"], str)
    kwargs.update(iterations=5, verbose=False)
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, 3))
    CatBoostRegressor(**kwargs).fit(X, X.sum(axis=1))
