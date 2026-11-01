"""
Resource-derived PostgreSQL and parallelism tuning.

Settings are derived from the resources the process may use, which
`hpldn.backend.common.hardware` detects (affinity and cgroup limits included),
rather than hardcoded or looked up by cloud instance type. The bulk-session
helpers raise GUCs for one seeding statement. See ``common/README.md`` for
the sizing rationale.
"""

import logging
import math
import os
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

from hpldn.backend.common import hardware

LOCAL_HOSTS = frozenset({None, "", "localhost", "127.0.0.1", "::1"})

MIB = 1024 * 1024

_CACHED_RESOURCES = None


@dataclass(frozen=True)
class HostResources:
    """
    The resources of the machine PostgreSQL runs on.

    Attributes
    ----------
    vcpus : int
        Logical CPUs.
    memory_mib : int
        Total RAM in MiB.
    gpu_count : int
        Number of GPUs.
    gpu_manufacturer, gpu_name : str
        GPU vendor and model.
    gpu_memory_mib : int
        Memory of the first GPU.
    gpu_compute_capability : str
        Compute capability of the first GPU, e.g. ``"12.0"``.
    source : str
        Where the figures came from: ``"env"``, ``"pg_settings"`` or the
        `hardware` sources, e.g. ``"affinity/meminfo/cupy"``.
    """

    vcpus: int
    memory_mib: int
    gpu_count: int = 0
    gpu_manufacturer: str = ""
    gpu_name: str = ""
    gpu_memory_mib: int = 0
    gpu_compute_capability: str = ""
    source: str = "unknown"

    @property
    def has_cuda_gpu(self) -> bool:
        """True when an NVIDIA GPU CuPy can use is present (an AMD GPU cannot)."""
        return self.gpu_count > 0 and self.gpu_manufacturer.upper() == "NVIDIA"

    @classmethod
    def from_hardware(cls, spec: "hardware.HardwareSpec") -> "HostResources":
        """Build from a `hardware.HardwareSpec`."""
        first = spec.gpus[0] if spec.gpus else None
        return cls(
            vcpus=spec.cpus,
            memory_mib=spec.memory_mib,
            gpu_count=len(spec.gpus),
            gpu_manufacturer=first.vendor if first else "",
            gpu_name=first.name if first else "",
            gpu_memory_mib=first.memory_total_mib if first else 0,
            gpu_compute_capability=first.compute_capability if first else "",
            source=f"{spec.cpu_source}/{spec.memory_source}/{spec.gpu_source}",
        )


@dataclass(frozen=True)
class TuningPolicy:
    """
    Knobs that shape the tuning formulas, overridable from ``staging.yaml``.

    Attributes
    ----------
    memory_fraction : float
        Share of RAM PostgreSQL may use in total; the rest covers the OS and
        the seeding process.
    concurrent_queries : int
        Memory-hungry sessions the cluster defaults are sized for. A bulk
        session assumes it owns the machine and gets this many times more
        ``work_mem``.
    memory_nodes_per_query : int
        Concurrent sort/hash nodes assumed per query, since ``work_mem`` is
        a per-node limit.
    max_workers_per_gather_cap : int
        Ceiling on parallel workers per Gather node.
    """

    memory_fraction: float = 0.90
    concurrent_queries: int = 2
    memory_nodes_per_query: int = 2
    max_workers_per_gather_cap: int = 16


DEFAULT_POLICY = TuningPolicy()

_BULK_PARALLEL_GUCS = {
    "parallel_setup_cost": "0",
    "parallel_tuple_cost": "0",
    "min_parallel_table_scan_size": "0",
    "min_parallel_index_scan_size": "0",
    "synchronous_commit": "off",
}


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp `value` into ``[low, high]``."""
    return max(low, min(high, value))


def _align_mb(mb: int) -> int:
    """
    Round a megabyte figure down to a multiple of 16 MB.

    Values under 64 MB are returned unchanged. Alignment keeps the Python and
    HCL implementations bit-identical.
    """
    if mb < 64:
        return mb
    return (mb // 16) * 16


def _half_and_align_mb(mb: int) -> int:
    """
    Halve a megabyte figure, then round it down to a multiple of 16 MB.

    Deliberately conservative headroom for concurrent sessions. Values under
    64 MB are returned unchanged. Use `_align_mb` where only alignment is
    wanted.
    """
    if mb < 64:
        return mb
    return (mb // 32) * 16


def format_mb(mb: int) -> str:
    """Render a megabyte figure as a PostgreSQL memory literal, e.g. ``"8GB"``."""
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024}GB"
    return f"{mb}MB"


def detect_host_resources(log: Optional[logging.Logger] = None) -> HostResources:
    """
    Describe the machine this process is running on, cached per process.

    Parameters
    ----------
    log : logging.Logger, optional
        Logger for the chosen source.

    Returns
    -------
    HostResources
        From ``HPLDN_HOST_VCPUS`` and ``HPLDN_HOST_MEMORY_MIB`` when both are
        set, else `hardware.detect` (affinity and cgroup limits, then the
        kernel; CuPy, then ``nvidia-smi``). Unreadable memory falls back to
        4 GiB, so a failed probe under-tunes.
    """
    global _CACHED_RESOURCES
    if _CACHED_RESOURCES is None:
        _CACHED_RESOURCES = _detect_host_resources(log)
    return _CACHED_RESOURCES


def reset_detection_cache() -> None:
    """Forget the cached host resources, for tests that patch the environment."""
    global _CACHED_RESOURCES
    _CACHED_RESOURCES = None


def _detect_host_resources(log: Optional[logging.Logger] = None) -> HostResources:
    """Probe host resources without caching; see `detect_host_resources`."""
    spec = hardware.detect()
    if spec.memory_source == "assumed" and log is not None:
        log.warning(
            "Could not determine system memory; assuming 4096 MiB. Set "
            "HPLDN_HOST_MEMORY_MIB to tune explicitly."
        )
    detected = HostResources.from_hardware(spec)
    env_vcpus = os.environ.get("HPLDN_HOST_VCPUS")
    env_memory = os.environ.get("HPLDN_HOST_MEMORY_MIB")
    if env_vcpus and env_memory:
        detected = replace(
            detected,
            vcpus=int(env_vcpus),
            memory_mib=int(env_memory),
            gpu_count=int(os.environ.get("HPLDN_HOST_GPU_COUNT", detected.gpu_count)),
            gpu_manufacturer=os.environ.get("HPLDN_HOST_GPU_VENDOR", detected.gpu_manufacturer),
            source="env",
        )
    if log is not None:
        log.debug("Host resources: %s", describe(detected))
    return detected


def _server_resources_from_settings(connection) -> Optional[HostResources]:
    """
    Infer a remote server's resources from its own settings.

    ``max_worker_processes`` equals vCPUs and ``shared_buffers`` is 25 % of
    RAM on any server `compute_postgres_settings` tuned, so both invert
    exactly; elsewhere the result is a safe lower bound. Returns None when
    the settings cannot be read.
    """
    from sqlalchemy import text

    rows = connection.execute(
        text(
            "SELECT name, setting, unit FROM pg_settings "
            "WHERE name IN ('shared_buffers', 'max_worker_processes', 'block_size')"
        )
    ).all()
    settings = {row[0]: (row[1], row[2]) for row in rows}
    if "shared_buffers" not in settings or "max_worker_processes" not in settings:
        return None

    block_size = int(settings.get("block_size", ("8192", None))[0])
    shared_buffers_blocks = int(settings["shared_buffers"][0])
    shared_buffers_mib = shared_buffers_blocks * block_size // MIB
    memory_mib = max(shared_buffers_mib * 4, 1024)
    vcpus = max(int(settings["max_worker_processes"][0]), 1)
    return HostResources(
        vcpus=vcpus,
        memory_mib=memory_mib,
        source="pg_settings",
    )


def resolve_host_resources(
    connection=None, log: Optional[logging.Logger] = None
) -> HostResources:
    """
    Return the resources of the machine the database server runs on.

    Without a connection, or for a local one, this is `detect_host_resources`.
    For a remote database the figures are inferred from ``pg_settings``, so
    server-side memory is never sized from the client's RAM.
    """
    if connection is None:
        return detect_host_resources(log)

    url = getattr(getattr(connection, "engine", None), "url", None)
    host = getattr(url, "host", None)
    if host in LOCAL_HOSTS:
        return detect_host_resources(log)

    try:
        remote = _server_resources_from_settings(connection)
    except Exception as err:  # pragma: no cover - defensive, tuning is optional
        if log is not None:
            log.warning("Could not read server settings for tuning: %s", err)
        remote = None
    if remote is None:
        if log is not None:
            log.warning(
                "Falling back to local resource detection for a remote database "
                "at %s — session tuning may not match the server.",
                host,
            )
        return detect_host_resources(log)
    if log is not None:
        log.debug(
            "Inferred server resources from pg_settings on %s: %d vCPU, %d MiB",
            host,
            remote.vcpus,
            remote.memory_mib,
        )
    return remote


def policy_from_cfg(cfg: Optional[Dict]) -> TuningPolicy:
    """Build a `TuningPolicy` from the optional ``tuning:`` block of ``staging.yaml``."""
    overrides = (cfg or {}).get("tuning") or {}
    known = {f: getattr(DEFAULT_POLICY, f) for f in DEFAULT_POLICY.__dataclass_fields__}
    kwargs = {k: type(known[k])(v) for k, v in overrides.items() if k in known}
    return replace(DEFAULT_POLICY, **kwargs)


def get_max_parallel_workers_per_gather(
    resources: HostResources, policy: TuningPolicy = DEFAULT_POLICY
) -> int:
    """
    Return parallel workers for one Gather node: half the cores, capped.

    Half, not all, leaves cores for autovacuum and other sessions. A
    single-vCPU host gets 0, disabling parallel query.
    """
    if resources.vcpus < 2:
        return 0
    return _clamp(resources.vcpus // 2, 1, policy.max_workers_per_gather_cap)


def get_max_parallel_maintenance_workers(resources: HostResources) -> int:
    """Return parallel workers for CREATE INDEX and VACUUM: half the cores, capped at 8."""
    return _clamp(resources.vcpus // 2, 1, 8)


def get_autovacuum_max_workers(resources: HostResources) -> int:
    """
    Return autovacuum workers: a quarter of the cores plus one, capped at 8.

    Each worker may hold ``autovacuum_work_mem``, so the count feeds the
    ``work_mem`` budget in `compute_postgres_settings`.
    """
    return _clamp(resources.vcpus // 4 + 1, 1, 8)


def compute_postgres_settings(
    resources: HostResources,
    policy: TuningPolicy = DEFAULT_POLICY,
    data_volume_gb: Optional[int] = None,
) -> Dict[str, str]:
    """
    Compute cluster-level ``postgresql.conf`` settings for a dedicated host.

    Parameters
    ----------
    resources : HostResources
        vCPU and RAM of the machine PostgreSQL owns.
    policy : TuningPolicy, optional
        Memory-budget knobs.
    data_volume_gb : int, optional
        Size of the PGDATA volume, capping ``max_wal_size`` at a tenth of it.

    Returns
    -------
    dict of str to str
        Ordered ``{guc: literal}`` mapping.

    """
    mem = resources.memory_mib
    vcpus = max(resources.vcpus, 1)

    budget_mb = int(mem * policy.memory_fraction)
    shared_buffers_mb = _clamp(mem // 4, 128, 65536)
    effective_cache_size_mb = int(mem * 0.70)
    maintenance_work_mem_mb = _clamp(mem // 16, 256, 8192)
    autovacuum_max_workers = get_autovacuum_max_workers(resources)
    autovacuum_work_mem_mb = _clamp(mem // 32, 128, 2048)

    max_worker_processes = max(vcpus, 1)
    per_gather = get_max_parallel_workers_per_gather(resources, policy)
    maintenance_workers = get_max_parallel_maintenance_workers(resources)

    session_pool_mb = max(
        budget_mb
        - shared_buffers_mb
        - maintenance_work_mem_mb
        - autovacuum_work_mem_mb * autovacuum_max_workers,
        64,
    )
    slots = (
        (per_gather + 1) * policy.concurrent_queries * policy.memory_nodes_per_query
    )
    work_mem_mb = _half_and_align_mb(
        _clamp(session_pool_mb // max(slots, 1), 4, 8192)
    )

    max_wal_size_mb = mem // 2
    if data_volume_gb:
        max_wal_size_mb = min(max_wal_size_mb, data_volume_gb * 1024 // 10)
    max_wal_size_mb = _clamp(max_wal_size_mb, 2048, 32768)

    return {
        "shared_buffers": format_mb(shared_buffers_mb),
        "effective_cache_size": format_mb(effective_cache_size_mb),
        "work_mem": format_mb(work_mem_mb),
        "maintenance_work_mem": format_mb(maintenance_work_mem_mb),
        "autovacuum_work_mem": format_mb(autovacuum_work_mem_mb),
        "huge_pages": "try",
        "max_worker_processes": str(max_worker_processes),
        "max_parallel_workers": str(max_worker_processes),
        "max_parallel_workers_per_gather": str(per_gather),
        "max_parallel_maintenance_workers": str(maintenance_workers),
        "parallel_setup_cost": "100",
        "parallel_tuple_cost": "0.01",
        "min_parallel_table_scan_size": "8MB",
        "min_parallel_index_scan_size": "512kB",
        "max_connections": str(max(8, vcpus * 4)),
        "max_wal_size": format_mb(max_wal_size_mb),
        "min_wal_size": format_mb(_clamp(max_wal_size_mb // 8, 512, 4096)),
        "wal_buffers": format_mb(_clamp(shared_buffers_mb // 32, 16, 128)),
        "wal_compression": "on",
        "checkpoint_timeout": "15min",
        "checkpoint_completion_target": "0.9",
        "synchronous_commit": "on",
        "random_page_cost": "1.1",
        "effective_io_concurrency": "200",
        "maintenance_io_concurrency": "200",
        "default_statistics_target": "200",
        "autovacuum_max_workers": str(autovacuum_max_workers),
        "autovacuum_vacuum_cost_limit": "2000",
    }


def get_seeding_settings(
    resources: HostResources,
    policy: TuningPolicy = DEFAULT_POLICY,
    data_volume_gb: Optional[int] = None,
) -> Dict[str, str]:
    """
    Return cluster-level overrides that apply only while the database is seeded.

    Written as a separate ``conf.d`` drop-in, removed after seeding. Trades
    crash-safety for throughput on data that can be reloaded from source.
    """
    steady = compute_postgres_settings(resources, policy, data_volume_gb)
    max_wal_size_mb = _clamp(
        _parse_mb(steady["max_wal_size"]) * 2, 4096, 65536
    )
    return {
        "synchronous_commit": "off",
        "autovacuum": "off",
        "checkpoint_timeout": "120min",
        "max_wal_size": format_mb(max_wal_size_mb),
    }


def _parse_mb(literal: str) -> int:
    """Parse a PostgreSQL memory literal (``'512MB'``, ``'8GB'``) back to MB."""
    literal = literal.strip()
    if literal.upper().endswith("GB"):
        return int(literal[:-2]) * 1024
    if literal.upper().endswith("MB"):
        return int(literal[:-2])
    if literal.upper().endswith("KB"):
        return max(int(literal[:-2]) // 1024, 1)
    return int(literal)


def postgres_conf_lines(settings: Dict[str, str]) -> List[str]:
    """Render ``{guc: value}`` as ``postgresql.conf`` lines, quoting non-numerics."""
    return [f"{name} = {_conf_literal(value)}" for name, value in settings.items()]


def _conf_literal(value: str) -> str:
    """Quote a value unless ``postgresql.conf`` accepts it bare."""
    return value if _is_bare_literal(value) else f"'{value}'"


def _is_bare_literal(value: str) -> bool:
    """Return True for values ``postgresql.conf`` accepts unquoted (numbers, on/off)."""
    if value.lower() in ("on", "off", "true", "false", "try"):
        return True
    try:
        float(value)
        return True
    except ValueError:
        return False


def get_bulk_session_settings(
    resources: HostResources, policy: TuningPolicy = DEFAULT_POLICY
) -> Dict[str, str]:
    """
    Return the GUCs a single bulk seeding statement should run with.

    The session gets the cluster ``work_mem`` times
    ``policy.concurrent_queries``, and zeroed parallel cost settings so the
    planner never declines parallelism on a large scan.
    """
    cluster = compute_postgres_settings(resources, policy)
    per_gather = get_max_parallel_workers_per_gather(resources, policy)
    work_mem_mb = _align_mb(
        _clamp(_parse_mb(cluster["work_mem"]) * policy.concurrent_queries, 4, 8192)
    )
    settings = {
        "work_mem": format_mb(work_mem_mb),
        "maintenance_work_mem": cluster["maintenance_work_mem"],
        "max_parallel_workers_per_gather": str(per_gather),
        "max_parallel_maintenance_workers": cluster["max_parallel_maintenance_workers"],
    }
    settings.update(_BULK_PARALLEL_GUCS)
    return settings


def bulk_session_sql(
    resources: HostResources, policy: TuningPolicy = DEFAULT_POLICY
) -> str:
    """Return the bulk session settings as a runnable ``SET ...;`` script."""
    settings = get_bulk_session_settings(resources, policy)
    return "\n".join(
        f"SET {name} = {_conf_literal(value)};" for name, value in settings.items()
    )


def apply_bulk_session_tuning(
    connection,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    local: bool = True,
) -> HostResources:
    """
    Apply the bulk-load GUCs to `connection`.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        An open connection, normally inside a transaction.
    cfg : dict, optional
        ``staging.yaml``, for the optional ``tuning:`` overrides.
    log : logging.Logger, optional
        Logger for the applied settings.
    local : bool, optional
        Emit ``SET LOCAL`` so settings revert at transaction end. A plain
        ``SET`` would leak to the next user of the pooled connection; pass
        False only outside a transaction.

    Returns
    -------
    HostResources
        The resources the settings were derived from.
    """
    from sqlalchemy import text

    policy = policy_from_cfg(cfg)
    resources = resolve_host_resources(connection, log)
    settings = get_bulk_session_settings(resources, policy)
    scope = "SET LOCAL" if local else "SET"
    for name, value in settings.items():
        connection.execute(text(f"{scope} {name} = {_conf_literal(value)}"))
    if log is not None:
        log.debug(
            "Applied bulk session tuning for %d vCPU / %d MiB (%s): %s",
            resources.vcpus,
            resources.memory_mib,
            resources.source,
            ", ".join(f"{k}={v}" for k, v in settings.items()),
        )
    return resources


def get_bulk_copy_env(
    log: Optional[logging.Logger] = None,
    resources: Optional[HostResources] = None,
    policy: TuningPolicy = DEFAULT_POLICY,
) -> Dict[str, str]:
    r"""
    Return `os.environ` plus a ``PGOPTIONS`` that tunes a ``psql \copy`` bulk load.

    ``PGOPTIONS`` applies the settings to that psql backend only, so
    ``synchronous_commit = off`` cannot leak to other sessions.
    """
    if resources is None:
        resources = detect_host_resources(log)
    settings = {
        "synchronous_commit": "off",
        "maintenance_work_mem": compute_postgres_settings(resources, policy)[
            "maintenance_work_mem"
        ],
    }
    options = " ".join(f"-c {name}={value}" for name, value in settings.items())
    env = dict(os.environ)
    env["PGOPTIONS"] = f"{env['PGOPTIONS']} {options}".strip() if env.get("PGOPTIONS") else options
    return env


def get_copy_jobs(
    resources: Optional[HostResources] = None, log: Optional[logging.Logger] = None
) -> int:
    """Return how many CSV chunks to COPY concurrently: half the cores, capped at 8."""
    if resources is None:
        resources = detect_host_resources(log)
    return _clamp(resources.vcpus // 2, 1, 8)


def get_index_jobs(
    resources: Optional[HostResources] = None, log: Optional[logging.Logger] = None
) -> int:
    """
    Return how many CREATE INDEX statements to run concurrently.

    Each build already uses `get_max_parallel_maintenance_workers` workers
    plus a leader, so the cores are divided by that, capped at 8.
    """
    if resources is None:
        resources = detect_host_resources(log)
    per_build = get_max_parallel_maintenance_workers(resources) + 1
    return _clamp(resources.vcpus // per_build, 1, 8)


def get_vacuum_jobs(
    resources: Optional[HostResources] = None, log: Optional[logging.Logger] = None
) -> int:
    """Return parallel jobs for ``vacuumdb --analyze`` after seeding."""
    if resources is None:
        resources = detect_host_resources(log)
    return _clamp(resources.vcpus // 2, 1, 16)


def describe(resources: HostResources) -> str:
    """Return a one-line summary of `resources` for logs and CLI output."""
    gpu = "no GPU"
    if resources.gpu_count:
        gpu = f"{resources.gpu_count}x {resources.gpu_manufacturer} {resources.gpu_name}".strip()
    return (
        f"host: {resources.vcpus} vCPU, "
        f"{math.floor(resources.memory_mib / 1024)} GiB RAM, {gpu} "
        f"(via {resources.source})"
    )
