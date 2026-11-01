"""
Offline tests for the resource-derived PostgreSQL tuning and the host probe.

The formulas are driven from synthetic host specs (a grid of CPU and RAM
sizes, plus the OCI A1.Flex shape the serving host runs on), so the
invariants hold for any machine without a database. `hardware` detection is
checked against a fake ``/proc`` and cgroup v2 tree.
"""

import itertools
import types

import pytest

from hpldn.backend.common import hardware, tuning
from hpldn.backend.common.accel import (
    asarray,
    get_array_module,
    gpu_enabled,
    module_for,
    reset_backend,
    to_host,
)
from hpldn.backend.common.tuning import (
    DEFAULT_POLICY,
    HostResources,
    TuningPolicy,
    _parse_mb,
    compute_postgres_settings,
    get_bulk_copy_env,
    get_bulk_session_settings,
    get_copy_jobs,
    get_index_jobs,
    get_max_parallel_maintenance_workers,
    get_max_parallel_workers_per_gather,
    get_seeding_settings,
    policy_from_cfg,
)

GIB = 1024

# OCI VM.Standard.A1.Flex as the serving host is sized: 4 OCPUs, 24 GB.
OCI_A1_FLEX_4_24 = HostResources(vcpus=4, memory_mib=24 * GIB, source="synthetic")

# 1-128 CPUs x 1-1024 GiB, every pairing a real host could plausibly have.
SYNTHETIC_HOSTS = [
    HostResources(vcpus=cpus, memory_mib=gib * GIB, source="synthetic")
    for cpus, gib in itertools.product([1, 2, 4, 8, 16, 24, 32, 64, 128], [1, 2, 4, 8, 16, 24, 32, 64, 128, 256, 512, 1024])
    if gib >= cpus // 8
] + [OCI_A1_FLEX_4_24]


def _id(resources: HostResources) -> str:
    return f"{resources.vcpus}cpu-{resources.memory_mib // GIB}gib"


@pytest.mark.parametrize("resources", SYNTHETIC_HOSTS, ids=_id)
def test_memory_settings_fit_in_the_budget(resources):
    """
    Check committed memory stays inside the configured share.

    The worst case is shared buffers, a fully parallel query at the configured
    concurrency and the maintenance reservations, which is what ``work_mem``
    is derived from. Below 2 GiB the 128 MB/256 MB floors win by design.
    """
    settings = compute_postgres_settings(resources)

    shared_buffers = _parse_mb(settings["shared_buffers"])
    work_mem = _parse_mb(settings["work_mem"])
    maintenance = _parse_mb(settings["maintenance_work_mem"])
    autovacuum = _parse_mb(settings["autovacuum_work_mem"])
    autovacuum_workers = int(settings["autovacuum_max_workers"])
    per_gather = int(settings["max_parallel_workers_per_gather"])

    worst_case = (
        shared_buffers
        + work_mem
        * (per_gather + 1)
        * DEFAULT_POLICY.concurrent_queries
        * DEFAULT_POLICY.memory_nodes_per_query
        + maintenance
        + autovacuum * autovacuum_workers
    )
    budget = resources.memory_mib * DEFAULT_POLICY.memory_fraction
    if resources.memory_mib < 2 * GIB:
        assert shared_buffers <= resources.memory_mib // 4 or shared_buffers == 128
        return
    assert worst_case <= budget, (
        f"{_id(resources)}: worst-case {worst_case} MiB exceeds the "
        f"{budget:.0f} MiB budget of {resources.memory_mib} MiB total"
    )


@pytest.mark.parametrize("resources", SYNTHETIC_HOSTS, ids=_id)
def test_parallelism_follows_the_core_count(resources):
    """Check worker settings follow vCPUs and stay consistent."""
    settings = compute_postgres_settings(resources)

    per_gather = int(settings["max_parallel_workers_per_gather"])
    max_parallel = int(settings["max_parallel_workers"])
    max_workers = int(settings["max_worker_processes"])

    assert max_workers == resources.vcpus
    assert max_parallel <= max_workers
    assert per_gather <= max_parallel
    assert per_gather <= DEFAULT_POLICY.max_workers_per_gather_cap
    assert per_gather == min(resources.vcpus // 2, DEFAULT_POLICY.max_workers_per_gather_cap) or resources.vcpus == 1


@pytest.mark.parametrize("resources", SYNTHETIC_HOSTS, ids=_id)
def test_settings_are_complete_and_well_formed(resources):
    """Check every generated value is a literal PostgreSQL accepts."""
    settings = compute_postgres_settings(resources)
    required = {
        "shared_buffers",
        "effective_cache_size",
        "work_mem",
        "maintenance_work_mem",
        "max_worker_processes",
        "max_parallel_workers_per_gather",
        "max_wal_size",
        "synchronous_commit",
    }
    assert required <= set(settings)
    assert settings["synchronous_commit"] == "on", (
        "the steady-state config must be durable; the seeding profile is what "
        "turns synchronous_commit off"
    )
    for name, value in settings.items():
        assert isinstance(value, str) and value, f"{name} is not a literal"
        assert "\n" not in value


@pytest.mark.parametrize("resources", SYNTHETIC_HOSTS, ids=_id)
def test_formulas(resources):
    """Check each sized setting is its documented formula of RAM and cores."""
    mem, cpus = resources.memory_mib, resources.vcpus
    settings = compute_postgres_settings(resources)
    assert settings["shared_buffers"] == tuning.format_mb(min(max(mem // 4, 128), 65536))
    assert _parse_mb(settings["maintenance_work_mem"]) == min(max(mem // 16, 256), 8192)
    assert _parse_mb(settings["autovacuum_work_mem"]) == min(max(mem // 32, 128), 2048)
    assert int(settings["autovacuum_max_workers"]) == min(max(cpus // 4 + 1, 1), 8)
    assert int(settings["max_parallel_maintenance_workers"]) == min(max(cpus // 2, 1), 8)
    assert int(settings["max_connections"]) == max(8, cpus * 4)
    assert 2048 <= _parse_mb(settings["max_wal_size"]) <= 32768


def test_oci_a1_flex_4_24_settings():
    """The serving shape: 4 OCPUs, 24 GB."""
    settings = compute_postgres_settings(OCI_A1_FLEX_4_24)
    assert settings["shared_buffers"] == "6GB"
    assert settings["effective_cache_size"] == f"{int(24 * GIB * 0.70)}MB"
    assert settings["maintenance_work_mem"] == "1536MB"
    assert settings["autovacuum_work_mem"] == "768MB"
    assert settings["max_worker_processes"] == "4"
    assert settings["max_parallel_workers_per_gather"] == "2"
    assert settings["autovacuum_max_workers"] == "2"
    assert settings["max_connections"] == "16"
    assert settings["max_wal_size"] == "12GB"
    assert get_copy_jobs(OCI_A1_FLEX_4_24) == 2
    assert get_index_jobs(OCI_A1_FLEX_4_24) == 1


def test_bulk_session_gets_more_memory_than_the_cluster_default():
    """Check a seeding session gets more ``work_mem`` than the cluster default."""
    resources = HostResources(vcpus=4, memory_mib=32 * GIB)
    cluster = compute_postgres_settings(resources)
    bulk = get_bulk_session_settings(resources)
    assert _parse_mb(bulk["work_mem"]) > _parse_mb(cluster["work_mem"])
    assert bulk["parallel_setup_cost"] == "0"
    assert bulk["parallel_tuple_cost"] == "0"
    assert bulk["min_parallel_table_scan_size"] == "0"
    assert bulk["synchronous_commit"] == "off"
    assert bulk["max_parallel_workers_per_gather"] == (
        cluster["max_parallel_workers_per_gather"]
    )


def test_seeding_profile_is_the_throughput_one():
    """Check the seeding drop-in only trades durability for throughput."""
    resources = HostResources(vcpus=4, memory_mib=32 * GIB)
    seeding = get_seeding_settings(resources, DEFAULT_POLICY, 100)
    steady = compute_postgres_settings(resources, DEFAULT_POLICY, 100)
    assert seeding["synchronous_commit"] == "off"
    assert seeding["autovacuum"] == "off"
    assert _parse_mb(seeding["max_wal_size"]) > _parse_mb(steady["max_wal_size"])


def test_bigger_hosts_get_bigger_settings():
    """Check sizing grows with RAM and cores."""
    sizes = [HostResources(vcpus=2**i, memory_mib=2**i * 8 * GIB) for i in range(1, 6)]
    shared = [_parse_mb(compute_postgres_settings(r)["shared_buffers"]) for r in sizes]
    workers = [int(compute_postgres_settings(r)["max_worker_processes"]) for r in sizes]
    assert shared == sorted(shared)
    assert workers == sorted(workers)


def test_single_vcpu_disables_parallel_query():
    """Check a single vCPU disables parallel query."""
    resources = HostResources(vcpus=1, memory_mib=4 * GIB)
    assert get_max_parallel_workers_per_gather(resources) == 0


@pytest.mark.parametrize("resources", SYNTHETIC_HOSTS, ids=_id)
def test_client_side_job_counts_stay_within_the_core_count(resources):
    """Check concurrent COPY and index jobs never oversubscribe the cores."""
    copy_jobs = get_copy_jobs(resources)
    index_jobs = get_index_jobs(resources)
    assert 1 <= copy_jobs <= max(resources.vcpus, 1)
    per_build = get_max_parallel_maintenance_workers(resources) + 1
    assert index_jobs == 1 or index_jobs * per_build <= resources.vcpus


def test_policy_overrides_come_from_staging_yaml():
    cfg = {"tuning": {"memory_fraction": 0.5, "concurrent_queries": 4}}
    policy = policy_from_cfg(cfg)
    assert policy.memory_fraction == 0.5
    assert policy.concurrent_queries == 4
    assert policy.max_workers_per_gather_cap == (
        DEFAULT_POLICY.max_workers_per_gather_cap
    )
    assert policy_from_cfg(None) == DEFAULT_POLICY
    assert policy_from_cfg({"tuning": {"nonsense": 1}}) == DEFAULT_POLICY


def test_lower_memory_fraction_lowers_the_settings():
    resources = HostResources(vcpus=4, memory_mib=32 * GIB)
    generous = compute_postgres_settings(resources, DEFAULT_POLICY)
    frugal = compute_postgres_settings(resources, TuningPolicy(memory_fraction=0.6))
    assert _parse_mb(frugal["work_mem"]) < _parse_mb(generous["work_mem"])


def test_bulk_copy_env_scopes_settings_to_the_psql_process():
    env = get_bulk_copy_env(resources=HostResources(vcpus=4, memory_mib=32768))
    assert "-c synchronous_commit=off" in env["PGOPTIONS"]
    assert "PATH" in env, "the process environment must be carried through"


def test_cuda_capability_is_vendor_specific():
    """Check an AMD GPU is not treated as CUDA-capable."""
    assert HostResources(vcpus=4, memory_mib=GIB, gpu_count=1, gpu_manufacturer="NVIDIA").has_cuda_gpu
    assert not HostResources(vcpus=4, memory_mib=GIB, gpu_count=1, gpu_manufacturer="AMD").has_cuda_gpu
    assert not HostResources(vcpus=4, memory_mib=GIB).has_cuda_gpu


# ---------------------------------------------------------------------------
# hardware: a fake /proc and cgroup v2 tree
# ---------------------------------------------------------------------------

SCOPE = "user.slice/user-1004.slice/hpldn.scope"


@pytest.fixture
def fake_host(tmp_path):
    """Return ``(proc_root, cgroup_root, write)`` for a host with 64 GiB and an unlimited scope."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text(f"1:net_cls:/\n0::/{SCOPE}\n")
    (proc / "meminfo").write_text("MemTotal:       67108864 kB\nMemFree:  1 kB\n")
    cgroup = tmp_path / "cgroup"
    (cgroup / SCOPE).mkdir(parents=True)

    def write(relative: str, name: str, value: str) -> None:
        directory = cgroup / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(value + "\n")

    return proc, cgroup, write


def test_hardware_reads_meminfo_without_a_cgroup_limit(fake_host):
    proc, cgroup, write = fake_host
    write(SCOPE, "memory.max", "max")
    assert hardware.detect_memory_mib(proc, cgroup) == (64 * GIB, "meminfo")
    assert hardware.cgroup_cpu_limit(proc, cgroup) is None


def test_hardware_takes_the_tightest_cgroup_memory_limit_on_the_path(fake_host):
    proc, cgroup, write = fake_host
    write(SCOPE, "memory.max", str(10 * GIB * hardware.MIB))
    write("user.slice", "memory.max", str(8 * GIB * hardware.MIB))
    assert hardware.detect_memory_mib(proc, cgroup) == (8 * GIB, "cgroup memory.max")


def test_hardware_ignores_a_cgroup_limit_above_physical_ram(fake_host):
    proc, cgroup, write = fake_host
    write(SCOPE, "memory.max", str(128 * GIB * hardware.MIB))
    assert hardware.detect_memory_mib(proc, cgroup) == (64 * GIB, "meminfo")


def test_hardware_lowers_cpus_to_the_cgroup_quota(fake_host, monkeypatch):
    proc, cgroup, write = fake_host
    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: set(range(24)))
    write(SCOPE, "cpu.max", "max 100000")
    write("user.slice/user-1004.slice", "cpu.max", "350000 100000")
    assert hardware.cgroup_cpu_limit(proc, cgroup) == pytest.approx(3.5)
    assert hardware.detect_cpus(proc, cgroup) == (4, "cgroup cpu.max")


def test_hardware_uses_the_affinity_mask_when_it_is_tighter(fake_host, monkeypatch):
    proc, cgroup, write = fake_host
    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: {0, 1})
    write(SCOPE, "cpu.max", "800000 100000")
    assert hardware.detect_cpus(proc, cgroup) == (2, "affinity")


def test_hardware_without_cgroup_v2_falls_back_to_the_kernel(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("1:cpu:/\n")
    (proc / "meminfo").write_text("MemTotal: 2097152 kB\n")
    monkeypatch.setattr(hardware.os, "sched_getaffinity", lambda pid: {0, 1, 2})
    spec = hardware.detect(probe_gpus=False, proc_root=proc, cgroup_root=tmp_path / "missing")
    assert (spec.cpus, spec.cpu_source) == (3, "affinity")
    assert (spec.memory_mib, spec.memory_source) == (2 * GIB, "meminfo")
    assert (spec.gpus, spec.gpu_source) == ((), "not probed")


def test_hardware_assumes_4_gib_when_memory_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, "_sysctl_memory_mib", lambda: None)
    spec = hardware.detect(probe_gpus=False, proc_root=tmp_path, cgroup_root=tmp_path)
    assert (spec.memory_mib, spec.memory_source) == (4096, "assumed")


def test_nvidia_smi_output_is_parsed():
    out = "0, NVIDIA GeForce RTX 5070 Ti, 16303, 15044, 12.0\n1, Tesla T4, 15360, 15000, [N/A]\n\n"
    gpus = hardware.parse_nvidia_smi(out)
    assert [g.name for g in gpus] == ["NVIDIA GeForce RTX 5070 Ti", "Tesla T4"]
    assert gpus[0].memory_total_mib == 16303 and gpus[0].memory_free_mib == 15044
    assert gpus[0].compute_capability == "12.0"
    assert gpus[1].compute_capability == ""


def test_gpus_come_from_cupy_then_nvidia_smi_then_none(monkeypatch):
    rtx = (hardware.GpuInfo(index=0, name="RTX", memory_total_mib=16000, compute_capability="12.0"),)
    monkeypatch.setattr(hardware, "_gpus_from_cupy", lambda: rtx)
    assert hardware.detect_gpus() == (rtx, "cupy")
    monkeypatch.setattr(hardware, "_gpus_from_cupy", lambda: None)
    monkeypatch.setattr(hardware, "_gpus_from_nvidia_smi", lambda: rtx)
    assert hardware.detect_gpus() == (rtx, "nvidia-smi")
    monkeypatch.setattr(hardware, "_gpus_from_nvidia_smi", lambda: None)
    assert hardware.detect_gpus() == ((), "none")


def test_cupy_probe_reads_count_memory_and_capability(monkeypatch):
    class _Device:
        def __init__(self, index):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    runtime = types.SimpleNamespace(
        getDeviceCount=lambda: 1,
        getDeviceProperties=lambda i: {"name": b"NVIDIA GeForce RTX 5070 Ti", "major": 12, "minor": 0},
        memGetInfo=lambda: (15 * GIB * hardware.MIB, 16 * GIB * hardware.MIB),
    )
    fake = types.SimpleNamespace(cuda=types.SimpleNamespace(runtime=runtime, Device=_Device))
    monkeypatch.setitem(__import__("sys").modules, "cupy", fake)
    [gpu] = hardware._gpus_from_cupy()
    assert gpu.name == "NVIDIA GeForce RTX 5070 Ti"
    assert gpu.compute_capability == "12.0"
    assert (gpu.memory_total_mib, gpu.memory_free_mib) == (16 * GIB, 15 * GIB)


def test_host_resources_from_hardware_and_env_override(monkeypatch):
    spec = hardware.HardwareSpec(
        cpus=4,
        memory_mib=24 * GIB,
        gpus=(hardware.GpuInfo(index=0, name="RTX", memory_total_mib=16000, compute_capability="12.0"),),
        cpu_source="affinity",
        memory_source="meminfo",
        gpu_source="cupy",
    )
    monkeypatch.setattr(tuning.hardware, "detect", lambda: spec)
    monkeypatch.delenv("HPLDN_HOST_VCPUS", raising=False)
    monkeypatch.delenv("HPLDN_HOST_MEMORY_MIB", raising=False)
    detected = tuning._detect_host_resources()
    assert (detected.vcpus, detected.memory_mib, detected.gpu_count) == (4, 24 * GIB, 1)
    assert detected.has_cuda_gpu and detected.gpu_compute_capability == "12.0"
    assert detected.source == "affinity/meminfo/cupy"
    monkeypatch.setenv("HPLDN_HOST_VCPUS", "2")
    monkeypatch.setenv("HPLDN_HOST_MEMORY_MIB", "2048")
    monkeypatch.setenv("HPLDN_HOST_GPU_COUNT", "0")
    overridden = tuning._detect_host_resources()
    assert (overridden.vcpus, overridden.memory_mib, overridden.source) == (2, 2048, "env")
    assert not overridden.has_cuda_gpu


def test_accel_falls_back_to_numpy_when_disabled(monkeypatch):
    import numpy as np

    monkeypatch.setenv("HPLDN_DISABLE_GPU", "1")
    reset_backend()
    try:
        assert get_array_module() is np
        assert not gpu_enabled()
    finally:
        reset_backend()


def test_accel_round_trip_matches_numpy():
    """Check the accelerated helpers agree with numpy on either backend."""
    import numpy as np

    values = list(range(1000))
    a = asarray(values, dtype=np.float64)
    xp = module_for(a)
    assert float(to_host(xp.mean(a))) == pytest.approx(np.mean(values))
    assert float(to_host(xp.std(a))) == pytest.approx(np.std(values))


def test_accel_keeps_non_numeric_data_on_the_host():
    """Check dates and strings stay on the host."""
    import numpy as np

    dates = np.array(["2024-01-01", "2024-06-01"], dtype=np.datetime64)
    staged = asarray(dates)
    assert module_for(staged) is np


def test_stats_helpers_return_host_values():
    """Check bounds helpers return plain Python numbers."""
    import numpy as np

    from hpldn.backend.common.utils import get_bounds_based_on_std

    a = np.array([10, 12, 11, 13, 12, 900], dtype=np.int64)
    lb, ub = get_bounds_based_on_std(a)
    assert isinstance(lb, (int, float)) and isinstance(ub, (int, float))
    assert lb <= ub


@pytest.mark.gpu
def test_this_host_has_a_cuda_gpu_cupy_can_use():
    """The GPU path is exercised, not assumed: CuPy sees the card, with its compute capability."""
    spec = hardware.detect()
    assert spec.gpu_source == "cupy", f"CuPy cannot see a GPU (source {spec.gpu_source}); pip install '.[gpu]'"
    gpu = spec.gpus[0]
    assert gpu.memory_total_mib > 0 and gpu.compute_capability
    major, minor = (int(part) for part in gpu.compute_capability.split("."))
    assert (major, minor) >= (5, 0)
    assert tuning.HostResources.from_hardware(spec).has_cuda_gpu
