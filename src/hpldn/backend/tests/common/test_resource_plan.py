"""
Offline tests for the training resource plan, the stage profiler and the CuPy streaming executor.

`hardware.plan_resources` is driven from synthetic `HardwareSpec`s (a GPU
box, a multi-GPU box, a CPU-only box and the OCI A1.Flex shape), so the
derivations hold for any machine. The executor's GPU path is compared with
its NumPy fallback on this host's GPU (marker ``gpu``).
"""

import json
import threading
import time

import numpy as np
import pytest

from hpldn.backend.common import accel, hardware, profiling

GIB = 1024


def _spec(cpus=24, memory_mib=32 * GIB, gpus=()):
    return hardware.HardwareSpec(cpus=cpus, memory_mib=memory_mib, gpus=tuple(gpus), cpu_source="test", memory_source="test", gpu_source="test")


def _gpu(index=0, total=16 * GIB, free=15 * GIB, cc="12.0", vendor="NVIDIA"):
    return hardware.GpuInfo(index=index, name=f"GPU{index}", vendor=vendor, memory_total_mib=total, memory_free_mib=free, compute_capability=cc)


# ---------------------------------------------------------------------------
# ResourcePlan
# ---------------------------------------------------------------------------


def test_single_gpu_plan_uses_the_gpu_and_derives_its_memory_parameters():
    plan = hardware.plan_resources(_spec(gpus=[_gpu()]), environ={})
    assert plan.task_type == "GPU" and plan.devices == "0"
    assert plan.fold_concurrency == 1 and plan.thread_count == 24
    # 15/16 free, times CatBoost's own 0.95 default.
    assert plan.gpu_ram_part == pytest.approx(0.89, abs=0.01)
    assert plan.pinned_memory_size == 2 * GIB * hardware.MIB
    assert plan.vram_budget_bytes == int(15 * GIB * hardware.GPU_MEMORY_BUDGET) * hardware.MIB
    params = plan.catboost_params()
    assert params == {
        "task_type": "GPU", "devices": "0", "gpu_ram_part": plan.gpu_ram_part, "pinned_memory_size": str(plan.pinned_memory_size)
    }


def test_every_usable_gpu_is_a_device_and_folds_run_one_per_gpu():
    plan = hardware.plan_resources(_spec(memory_mib=128 * GIB, gpus=[_gpu(0), _gpu(1), _gpu(2, cc="3.0")]), environ={})
    assert plan.devices == "0:1", "compute capability 3.0 is below CatBoost's minimum"
    assert plan.fold_concurrency == 2 and plan.thread_count == 12
    assert plan.for_worker(0).devices == "0" and plan.for_worker(1).devices == "1"
    assert plan.for_worker(3).devices == "1" and plan.for_worker(0).fold_concurrency == 1


def test_fold_concurrency_is_capped_by_ram():
    spec = _spec(cpus=64, memory_mib=32 * GIB, gpus=[_gpu(0), _gpu(1), _gpu(2), _gpu(3)])
    assert hardware.plan_resources(spec, environ={}, fold_memory_mib=8 * GIB).fold_concurrency == 2
    assert hardware.plan_resources(spec, environ={}, fold_memory_mib=1 * GIB).fold_concurrency == 4
    assert hardware.plan_resources(spec, environ={}, fold_memory_mib=64 * GIB).fold_concurrency == 1


def test_cpu_plan_shares_threads_among_concurrent_folds():
    plan = hardware.plan_resources(_spec(cpus=24, memory_mib=64 * GIB), environ={})
    assert plan.task_type == "CPU" and plan.devices == ""
    assert plan.fold_concurrency == 3 and plan.thread_count == 8
    assert plan.used_ram_limit.endswith("mb")
    assert plan.catboost_params() == {"task_type": "CPU", "thread_count": 8, "used_ram_limit": plan.used_ram_limit}


def test_a1_flex_profile_trains_on_cpu_one_fold_at_a_time():
    """OCI A1.Flex (4 OCPU, 24 GB, no GPU): the same command runs on its CPUs."""
    plan = hardware.plan_resources(_spec(cpus=4, memory_mib=24 * GIB), device="auto", environ={})
    assert plan.task_type == "CPU" and plan.fold_concurrency == 1 and plan.thread_count == 4
    assert plan.chunk_rows >= hardware.MIN_CHUNK_ROWS


def test_disable_gpu_env_forces_cpu_and_gpu_request_then_fails():
    spec = _spec(gpus=[_gpu()])
    plan = hardware.plan_resources(spec, environ={"HPLDN_DISABLE_GPU": "1"})
    assert plan.task_type == "CPU" and "HPLDN_DISABLE_GPU" in plan.reason
    with pytest.raises(RuntimeError, match="--device gpu"):
        hardware.plan_resources(spec, device="gpu", environ={"HPLDN_DISABLE_GPU": "1"})
    with pytest.raises(RuntimeError, match="--device gpu"):
        hardware.plan_resources(_spec(gpus=[_gpu(vendor="AMD")]), device="gpu", environ={})


def test_cli_overrides_win_and_are_recorded():
    plan = hardware.plan_resources(
        _spec(gpus=[_gpu(0), _gpu(1)]),
        environ={},
        overrides={"devices": "1", "gpu_ram_part": 0.5, "thread_count": -1, "chunk_rows": 4096, "fold_concurrency": None},
    )
    assert plan.devices == "1" and plan.gpu_ram_part == 0.5 and plan.chunk_rows == 4096
    assert plan.thread_count == 12, "-1 means the plan's thread count"
    assert set(plan.overridden) == {"devices", "gpu_ram_part", "chunk_rows"}
    single = hardware.apply_overrides(plan, {"fold_concurrency": 1})
    assert single.fold_concurrency == 1 and single.thread_count == 24
    with pytest.raises(ValueError, match="trains on CPU"):
        hardware.plan_resources(_spec(), environ={}, overrides={"devices": "0"})
    with pytest.raises(ValueError, match="Unknown resource plan override"):
        hardware.plan_resources(_spec(), environ={}, overrides={"iterations": 5})


def test_chunk_rows_fit_the_smaller_of_vram_and_pinned_ram():
    plan = hardware.plan_resources(_spec(memory_mib=8 * GIB, gpus=[_gpu(free=15 * GIB)]), environ={})
    ram_half = plan.ram_budget_bytes // 2
    assert plan.chunk_rows_for(1024) == min(plan.vram_budget_bytes, ram_half) // 1024
    assert plan.chunk_rows_for(10**12) == hardware.MIN_CHUNK_ROWS
    assert plan.chunk_rows_for(1) == hardware.MAX_CHUNK_ROWS


def test_this_hosts_plan_is_derivable():
    plan = hardware.plan_resources(environ={"HPLDN_DISABLE_GPU": "1"})
    assert plan.task_type == "CPU" and plan.thread_count >= 1
    assert "fold_concurrency" in plan.describe()


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


def test_stages_nest_and_record_wall_time_rss_and_extra_facts(tmp_path):
    profiler = profiling.Profiler(sample_interval_s=0.005, vram_probe=lambda: 123.0)
    profiler.enable_vram()
    with profiler.stage("outer", rows=10) as facts:
        with profiler.stage("inner"):
            block = np.ones(4_000_000)  # 32 MB, seen by the sampler
            time.sleep(0.03)
            del block
        facts["trees"] = 7
    names = [r.name for r in profiler.records]
    assert names == ["outer/inner", "outer"]
    outer = profiler.records[1]
    assert outer.wall_s >= 0.03 and outer.extra == {"rows": 10, "trees": 7}
    inner = profiler.records[0]
    assert inner.peak_rss_mib >= inner.rss_start_mib and inner.peak_vram_mib == 123.0
    payload = profiler.to_dict()
    assert payload["totals"]["outer"] == outer.wall_s
    path = profiler.write_json(tmp_path / "p" / "profile.json")
    assert json.loads(path.read_text())["stages"][0]["name"] == "outer/inner"


def test_vram_is_not_read_until_enabled():
    calls = []
    profiler = profiling.Profiler(vram_probe=lambda: calls.append(1) or 1.0)
    with profiler.stage("cpu-only"):
        pass
    assert not calls and profiler.records[0].peak_vram_mib is None


def test_stages_in_threads_have_their_own_stack():
    profiler = profiling.Profiler(sample_interval_s=0.005)

    def work(i):
        with profiler.stage(f"fold {i}"):
            with profiler.stage("pass1"):
                time.sleep(0.01)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.name for r in profiler.records) == sorted(
        [f"fold {i}" for i in range(3)] + [f"fold {i}/pass1" for i in range(3)]
    )


def test_frame_fingerprint_sees_every_value_bit():
    import pandas as pd

    df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": ["x", "y", None]})
    same = profiling.frame_fingerprint(df.copy())
    assert profiling.frame_fingerprint(df) == same
    changed = df.copy()
    changed.loc[1, "a"] = np.nextafter(2.0, 3.0)
    assert profiling.frame_fingerprint(changed)["sha256"] != same["sha256"]
    assert profiling.frame_fingerprint(changed)["columns_sha256"]["b"] == same["columns_sha256"]["b"]
    assert profiling.frame_fingerprint(df.astype({"a": "float32"}))["sha256"] != same["sha256"]


def test_nvml_probe_returns_a_number_or_none():
    used = hardware.gpu_memory_used_mib()
    assert used is None or used >= 0


# ---------------------------------------------------------------------------
# Streaming executor
# ---------------------------------------------------------------------------


def _kernel(xp, x, y, scale):
    hits = xp.nonzero(x % 7 == 0)[0]
    return x * scale + y.sum(axis=1), hits


def test_numpy_executor_chunks_and_concatenates():
    x = np.arange(10_001, dtype=np.float64)
    y = np.random.default_rng(0).normal(size=(10_001, 3))
    executor = accel.StreamingExecutor(chunk_rows=1000, use_gpu=False)
    assert executor.xp is np
    chunks = executor.run(_kernel, [x, y], consts=(2.0,))
    assert len(chunks) == 11 and len(chunks[-1][0]) == 1
    values, _ = executor.map_rows(_kernel, [x, y], consts=(2.0,))
    np.testing.assert_array_equal(values, x * 2.0 + y.sum(axis=1))
    with pytest.raises(ValueError, match="same length"):
        executor.run(_kernel, [x, y[:5]], consts=(2.0,))


@pytest.mark.gpu
def test_gpu_executor_matches_the_numpy_fallback():
    """Pinned, double-buffered chunks through VRAM give the NumPy path's answer."""
    accel.reset_backend()
    if not accel.gpu_enabled():
        pytest.fail("missing prerequisite: a CUDA GPU CuPy can use")
    rng = np.random.default_rng(1)
    x = rng.integers(0, 1000, 250_003).astype(np.float64)
    y = rng.normal(size=(250_003, 4))
    cpu = accel.StreamingExecutor(chunk_rows=40_000, use_gpu=False).run(_kernel, [x, y], consts=(3.0,))
    gpu = accel.StreamingExecutor(chunk_rows=40_000, use_gpu=True, vram_limit_bytes=256 * 2**20).run(_kernel, [x, y], consts=(3.0,))
    assert len(cpu) == len(gpu) == 7
    for (cv, ch), (gv, gh) in zip(cpu, gpu):
        assert isinstance(gv, np.ndarray) and isinstance(gh, np.ndarray)
        np.testing.assert_allclose(gv, cv, rtol=1e-15, atol=0)
        np.testing.assert_array_equal(gh, ch)
    import cupy

    assert cupy.get_default_memory_pool().get_limit() >= 256 * 2**20
