"""
Detect the CPUs, RAM and GPUs this process may actually use.

What the process may use, not what the machine has: a systemd scope, a
container or ``taskset`` can grant fewer CPUs and less memory than the host
holds, and sizing PostgreSQL or a training run from the host's totals then
oversubscribes it. Each figure records where it came from:

* CPUs: the scheduler affinity mask (``sched_getaffinity``), then lowered to
  the cgroup v2 ``cpu.max`` quota of this process's cgroup or any ancestor.
* RAM: the tightest cgroup v2 ``memory.max`` on the same path, then
  ``/proc/meminfo``'s ``MemTotal`` (the smaller of the two wins), then macOS
  ``sysctl hw.memsize``.
* GPUs: CuPy (``getDeviceCount``, ``memGetInfo``, compute capability), then
  ``nvidia-smi``, then none. Only NVIDIA is probed: it is the only vendor CuPy
  and CatBoost's GPU trainer can drive.

`hpldn.backend.common.tuning` turns a `HardwareSpec` into PostgreSQL and
parallelism settings, and `plan_resources` into a training `ResourcePlan`
(CatBoost's device and memory parameters, fold concurrency and the CuPy chunk
size). ``python -m hpldn.backend.common.hardware`` prints what this host
reports and the plan derived from it.
"""

import ctypes
import ctypes.util
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

MIB = 1024 * 1024

PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class GpuInfo:
    """One GPU this process can see."""

    index: int
    name: str
    vendor: str = "NVIDIA"
    memory_total_mib: int = 0
    memory_free_mib: int = 0
    # "major.minor", e.g. "12.0" for Blackwell (sm_120).
    compute_capability: str = ""


@dataclass(frozen=True)
class HardwareSpec:
    """The resources available to this process, each with its source."""

    cpus: int
    memory_mib: int
    gpus: Tuple[GpuInfo, ...] = field(default_factory=tuple)
    cpu_source: str = "unknown"
    memory_source: str = "unknown"
    gpu_source: str = "none"


# ---------------------------------------------------------------------------
# cgroup v2
# ---------------------------------------------------------------------------


def _cgroup_dirs(proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT) -> list:
    """
    Return this process's cgroup v2 directory and its ancestors, leaf first.

    Empty when ``/proc/self/cgroup`` has no unified (``0::``) line or the
    hierarchy is not mounted at `cgroup_root`.
    """
    try:
        lines = (proc_root / "self" / "cgroup").read_text().splitlines()
    except OSError:
        return []
    relative = next((line[3:] for line in lines if line.startswith("0::")), None)
    if relative is None or not cgroup_root.is_dir():
        return []
    leaf = cgroup_root / relative.strip().lstrip("/")
    if not leaf.exists():
        return []
    return [d for d in (leaf, *leaf.parents) if d == cgroup_root or cgroup_root in d.parents]


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def cgroup_cpu_limit(proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT) -> Optional[float]:
    """
    Return the tightest ``cpu.max`` quota on this process's cgroup path, in CPUs.

    ``cpu.max`` is ``"<quota> <period>"`` or ``"max <period>"``; None when no
    level sets a quota.
    """
    limits = []
    for directory in _cgroup_dirs(proc_root, cgroup_root):
        raw = _read(directory / "cpu.max")
        if not raw:
            continue
        quota, _, period = raw.partition(" ")
        if quota == "max":
            continue
        try:
            limits.append(int(quota) / int(period or 100_000))
        except ValueError:
            continue
    return min(limits) if limits else None


def cgroup_memory_limit_mib(proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT) -> Optional[int]:
    """Return the tightest ``memory.max`` on this process's cgroup path in MiB, or None when unlimited."""
    limits = []
    for directory in _cgroup_dirs(proc_root, cgroup_root):
        raw = _read(directory / "memory.max")
        if not raw or raw == "max":
            continue
        try:
            limits.append(int(raw) // MIB)
        except ValueError:
            continue
    return min(limits) if limits else None


# ---------------------------------------------------------------------------
# CPUs and RAM
# ---------------------------------------------------------------------------


def detect_cpus(proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT) -> Tuple[int, str]:
    """Return ``(cpus, source)``: the affinity count, lowered to the cgroup quota (rounded up)."""
    try:
        cpus, source = len(os.sched_getaffinity(0)), "affinity"
    except (AttributeError, OSError):
        cpus, source = os.cpu_count() or 1, "cpu_count"
    quota = cgroup_cpu_limit(proc_root, cgroup_root)
    if quota is not None and math.ceil(quota) < cpus:
        return max(math.ceil(quota), 1), "cgroup cpu.max"
    return max(cpus, 1), source


def meminfo_total_mib(proc_root: Path = PROC_ROOT) -> Optional[int]:
    """Return ``MemTotal`` from ``/proc/meminfo`` in MiB, or None."""
    raw = _read(proc_root / "meminfo")
    if raw is None:
        return None
    for line in raw.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return None


def _sysctl_memory_mib() -> Optional[int]:
    if platform.system() != "Darwin" or not shutil.which("sysctl"):
        return None
    result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip().isdigit():
        return int(result.stdout.strip()) // MIB
    return None


def detect_memory_mib(proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT) -> Tuple[Optional[int], str]:
    """Return ``(MiB, source)``: the cgroup ``memory.max`` when tighter than ``MemTotal``; None if unknown."""
    limit = cgroup_memory_limit_mib(proc_root, cgroup_root)
    total = meminfo_total_mib(proc_root)
    if limit is not None and (total is None or limit < total):
        return limit, "cgroup memory.max"
    if total is not None:
        return total, "meminfo"
    total = _sysctl_memory_mib()
    if total is not None:
        return total, "sysctl"
    return None, "unknown"


# ---------------------------------------------------------------------------
# GPUs
# ---------------------------------------------------------------------------


def _gpus_from_cupy() -> Optional[Tuple[GpuInfo, ...]]:
    """Return the GPUs CuPy sees, or None when CuPy is absent or cannot reach a driver."""
    try:
        import cupy  # noqa: PLC0415 - optional dependency (the gpu extra)
    except ImportError:
        return None
    try:
        count = cupy.cuda.runtime.getDeviceCount()
        gpus = []
        for index in range(count):
            props = cupy.cuda.runtime.getDeviceProperties(index)
            with cupy.cuda.Device(index):
                free, total = cupy.cuda.runtime.memGetInfo()
            name = props.get("name", b"")
            gpus.append(
                GpuInfo(
                    index=index,
                    name=name.decode() if isinstance(name, bytes) else str(name),
                    memory_total_mib=int(total) // MIB,
                    memory_free_mib=int(free) // MIB,
                    compute_capability=f"{props.get('major', 0)}.{props.get('minor', 0)}",
                )
            )
        return tuple(gpus)
    except Exception:  # noqa: BLE001 - no driver, no device or a broken install all mean "ask nvidia-smi"
        return None


def parse_nvidia_smi(stdout: str) -> Tuple[GpuInfo, ...]:
    """Parse ``nvidia-smi --query-gpu=index,name,memory.total,memory.free,compute_cap --format=csv,noheader,nounits``."""
    gpus = []
    for line in stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue

        def _int(i: int) -> int:
            try:
                return int(float(parts[i]))
            except (IndexError, ValueError):
                return 0

        gpus.append(
            GpuInfo(
                index=int(parts[0]),
                name=parts[1],
                memory_total_mib=_int(2),
                memory_free_mib=_int(3),
                compute_capability=parts[4] if len(parts) > 4 and parts[4] not in ("", "[N/A]") else "",
            )
        )
    return tuple(gpus)


def _gpus_from_nvidia_smi() -> Optional[Tuple[GpuInfo, ...]]:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_nvidia_smi(result.stdout)


def detect_gpus() -> Tuple[Tuple[GpuInfo, ...], str]:
    """Return ``(gpus, source)`` from CuPy, else ``nvidia-smi``, else none."""
    gpus = _gpus_from_cupy()
    if gpus:
        return gpus, "cupy"
    gpus = _gpus_from_nvidia_smi()
    if gpus:
        return gpus, "nvidia-smi"
    return (), "none"


def detect(probe_gpus: bool = True, proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT) -> HardwareSpec:
    """
    Return the `HardwareSpec` of this process.

    Unknown memory falls back to 4 GiB with source ``"assumed"``, so a failed
    probe under-tunes rather than overcommits.
    """
    cpus, cpu_source = detect_cpus(proc_root, cgroup_root)
    memory_mib, memory_source = detect_memory_mib(proc_root, cgroup_root)
    if memory_mib is None:
        memory_mib, memory_source = 4096, "assumed"
    gpus, gpu_source = detect_gpus() if probe_gpus else ((), "not probed")
    return HardwareSpec(
        cpus=cpus,
        memory_mib=memory_mib,
        gpus=gpus,
        cpu_source=cpu_source,
        memory_source=memory_source,
        gpu_source=gpu_source,
    )


# ---------------------------------------------------------------------------
# GPU memory in use (NVML), for profiling
# ---------------------------------------------------------------------------


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


_NVML = None
_NVML_FAILED = False


def _nvml():
    """Return the initialised NVML library, or None. NVML reads memory without creating a CUDA context."""
    global _NVML, _NVML_FAILED
    if _NVML is not None or _NVML_FAILED:
        return _NVML
    try:
        name = ctypes.util.find_library("nvidia-ml") or "libnvidia-ml.so.1"
        lib = ctypes.CDLL(name)
        if lib.nvmlInit_v2() != 0:
            raise OSError("nvmlInit_v2 failed")
        _NVML = lib
    except OSError:
        _NVML_FAILED = True
    return _NVML


def gpu_memory_used_mib(indices: Optional[Tuple[int, ...]] = None) -> Optional[float]:
    """
    Return the memory in use on the NVIDIA GPUs `indices` (default all), summed, in MiB; None without NVML.

    Device-wide: other processes' allocations (a desktop) are included, so a
    stage's growth is its peak less its start.
    """
    lib = _nvml()
    if lib is None:
        return None
    count = ctypes.c_uint(0)
    if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
        return None
    total = 0
    for index in indices if indices is not None else range(count.value):
        handle = ctypes.c_void_p()
        memory = _NvmlMemory()
        if lib.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(index), ctypes.byref(handle)) != 0:
            return None
        if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) != 0:
            return None
        total += memory.used
    return total / MIB


# ---------------------------------------------------------------------------
# The training resource plan
# ---------------------------------------------------------------------------

# CatBoost's GPU trainer needs compute capability 3.5 or later.
CATBOOST_MIN_COMPUTE_CAPABILITY = (3, 5)
# CatBoost's own default share of GPU memory it may take; the plan never asks for more.
CATBOOST_MAX_GPU_RAM_PART = 0.95
# Share of free GPU memory CuPy work (the streaming executor's memory pool) may hold.
GPU_MEMORY_BUDGET = 0.8
# Share of the process's RAM a training run plans to use: concurrent folds,
# CatBoost's CPU ``used_ram_limit`` and the executor's pinned buffers.
RAM_BUDGET = 0.6
# CatBoost's pinned host buffer for GPU training: RAM / 16, inside these bounds.
PINNED_MEMORY_MIN = 256 * MIB
PINNED_MEMORY_MAX = 8 * 1024 * MIB
# Concurrent CPU folds only when each still gets this many threads.
MIN_THREADS_PER_FOLD = 8
# Assumed RAM per concurrent fold when the caller gives no frame-size hint.
DEFAULT_FOLD_MEMORY_MIB = 8 * 1024
# The executor's default row width (eight float64 columns).
CHUNK_BYTES_PER_ROW = 64
MIN_CHUNK_ROWS = 1024
MAX_CHUNK_ROWS = 1 << 26

PLAN_OVERRIDE_FIELDS = (
    "devices",
    "gpu_ram_part",
    "pinned_memory_size",
    "thread_count",
    "used_ram_limit",
    "fold_concurrency",
    "chunk_rows",
)


def _compute_capability(gpu: GpuInfo) -> Tuple[int, int]:
    try:
        major, _, minor = gpu.compute_capability.partition(".")
        return int(major), int(minor or 0)
    except ValueError:
        return (0, 0)


def _usable_for_catboost(gpu: GpuInfo) -> bool:
    """True for an NVIDIA GPU CatBoost can train on (an unknown capability is given the benefit of the doubt)."""
    if gpu.vendor.upper() != "NVIDIA":
        return False
    if not gpu.compute_capability:
        return True
    return _compute_capability(gpu) >= CATBOOST_MIN_COMPUTE_CAPABILITY


@dataclass(frozen=True)
class ResourcePlan:
    """
    How a training run uses this host, derived from a `HardwareSpec` by `plan_resources`.

    Attributes
    ----------
    task_type : {"GPU", "CPU"}
        CatBoost's trainer.
    devices : str
        CatBoost ``devices``: every usable GPU (``"0"``, ``"0:1"``); empty on CPU.
    gpu_ram_part : float or None
        CatBoost ``gpu_ram_part``: the share of each GPU's memory it may take,
        scaled down by what is already in use; None leaves CatBoost's default.
    pinned_memory_size : int or None
        CatBoost ``pinned_memory_size`` in bytes (GPU only).
    thread_count : int
        CPU threads per fit (the process's CPUs shared among concurrent folds).
    used_ram_limit : str or None
        CatBoost ``used_ram_limit`` for CPU training, e.g. ``"9216mb"``.
    fold_concurrency : int
        Folds that may fit at once: one per GPU, or one per
        `MIN_THREADS_PER_FOLD` CPUs on CPU, capped by RAM.
    chunk_rows : int
        Rows per chunk for the CuPy streaming executor at
        `CHUNK_BYTES_PER_ROW`; `chunk_rows_for` sizes other row widths.
    vram_budget_bytes, ram_budget_bytes : int
        What CuPy work may hold on the device and in pinned host memory.
    reason : str
        Why the trainer was chosen.
    overridden : tuple of str
        The fields a CLI flag set.
    """

    task_type: str
    devices: str
    gpu_ram_part: Optional[float]
    pinned_memory_size: Optional[int]
    thread_count: int
    used_ram_limit: Optional[str]
    fold_concurrency: int
    chunk_rows: int
    vram_budget_bytes: int
    ram_budget_bytes: int
    gpu_names: Tuple[str, ...] = ()
    reason: str = ""
    overridden: Tuple[str, ...] = ()

    @property
    def use_gpu(self) -> bool:
        return self.task_type == "GPU"

    @property
    def gpu_indices(self) -> Tuple[int, ...]:
        return tuple(int(i) for i in self.devices.split(":") if i.strip()) if self.devices else ()

    def catboost_params(self) -> Dict[str, object]:
        """Return the CatBoostRegressor keyword arguments this plan sets."""
        if self.use_gpu:
            params: Dict[str, object] = {"task_type": "GPU", "devices": self.devices}
            if self.gpu_ram_part is not None:
                params["gpu_ram_part"] = self.gpu_ram_part
            if self.pinned_memory_size is not None:
                # CatBoost parses this one only from a string of bytes.
                params["pinned_memory_size"] = str(int(self.pinned_memory_size))
            return params
        params = {"task_type": "CPU", "thread_count": self.thread_count}
        if self.used_ram_limit is not None:
            params["used_ram_limit"] = self.used_ram_limit
        return params

    def chunk_rows_for(self, bytes_per_row: int) -> int:
        """Return the rows per chunk that fit the budgets at `bytes_per_row` bytes a row."""
        budget = self.vram_budget_bytes if self.use_gpu or self.vram_budget_bytes else self.ram_budget_bytes
        if self.ram_budget_bytes:
            # Two pinned host buffers per input (double buffering).
            budget = min(budget, self.ram_budget_bytes // 2) if budget else self.ram_budget_bytes // 2
        rows = int(budget // max(int(bytes_per_row), 1))
        return max(MIN_CHUNK_ROWS, min(MAX_CHUNK_ROWS, rows))

    def for_worker(self, worker: int) -> "ResourcePlan":
        """
        Return the plan one of `fold_concurrency` concurrent folds runs under.

        On GPU each concurrent fold gets one device (round robin); a single
        fold keeps every device. On CPU `thread_count` is already the per-fold
        share.
        """
        if not self.use_gpu or self.fold_concurrency <= 1 or len(self.gpu_indices) <= 1:
            return self
        device = self.gpu_indices[worker % len(self.gpu_indices)]
        return replace(self, devices=str(device), fold_concurrency=1)

    def describe(self) -> str:
        if self.use_gpu:
            where = f"GPU devices={self.devices} ({', '.join(self.gpu_names)}) gpu_ram_part={self.gpu_ram_part}"
        else:
            where = f"CPU thread_count={self.thread_count} used_ram_limit={self.used_ram_limit}"
        overridden = f", overridden: {', '.join(self.overridden)}" if self.overridden else ""
        return (
            f"{where}, fold_concurrency={self.fold_concurrency}, chunk_rows={self.chunk_rows:,} "
            f"({self.reason}{overridden})"
        )


def plan_resources(
    spec: Optional[HardwareSpec] = None,
    device: str = "auto",
    overrides: Optional[Mapping[str, object]] = None,
    fold_memory_mib: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ResourcePlan:
    """
    Derive the training `ResourcePlan` from what this process may use.

    Parameters
    ----------
    spec : HardwareSpec, optional
        The host; `detect` when omitted.
    device : {"auto", "gpu", "cpu"}
        ``--device``. ``HPLDN_DISABLE_GPU=1`` in `environ` forces CPU like
        ``cpu``, so one command runs unchanged on a CPU-only host.
    overrides : mapping, optional
        CLI values for `PLAN_OVERRIDE_FIELDS`; None entries are ignored.
        ``thread_count`` of -1 means "the plan's".
    fold_memory_mib : int, optional
        RAM one fold needs, from the engineered frame's size; caps
        `fold_concurrency`. Defaults to `DEFAULT_FOLD_MEMORY_MIB`.

    Raises
    ------
    RuntimeError
        For ``device="gpu"`` with no usable GPU.
    """
    device = device.lower()
    if device not in ("cpu", "gpu", "auto"):
        raise ValueError(f"Unknown device '{device}', expected cpu/gpu/auto")
    environ = os.environ if environ is None else environ
    disabled = environ.get("HPLDN_DISABLE_GPU") == "1"
    if spec is None:
        spec = detect(probe_gpus=not disabled and device != "cpu")

    usable: Tuple[GpuInfo, ...] = ()
    if device == "cpu":
        reason = "cpu requested"
    elif disabled:
        reason = "HPLDN_DISABLE_GPU=1"
    else:
        usable = tuple(g for g in spec.gpus if _usable_for_catboost(g))
        if usable:
            reason = f"{len(usable)} usable GPU(s) from {spec.gpu_source}"
        elif spec.gpus:
            reason = "no GPU CatBoost can train on (vendor or compute capability)"
        else:
            reason = "no CUDA GPU detected"
    if device == "gpu" and not usable:
        raise RuntimeError(f"--device gpu requested but no CUDA-capable GPU was detected ({reason})")

    ram_budget_bytes = int(spec.memory_mib * RAM_BUDGET) * MIB
    fold_memory = int(fold_memory_mib or DEFAULT_FOLD_MEMORY_MIB)
    by_ram = max(1, int(spec.memory_mib * RAM_BUDGET) // max(fold_memory, 1))

    if usable:
        fold_concurrency = max(1, min(len(usable), by_ram))
        free = [g.memory_free_mib for g in usable if g.memory_total_mib]
        total = [g.memory_total_mib for g in usable if g.memory_total_mib]
        if free and total:
            share = min(f / t for f, t in zip(free, total))
            gpu_ram_part = round(min(CATBOOST_MAX_GPU_RAM_PART, max(0.1, share * CATBOOST_MAX_GPU_RAM_PART)), 2)
            vram_budget_bytes = int(min(free) * GPU_MEMORY_BUDGET) * MIB
        else:
            gpu_ram_part, vram_budget_bytes = None, 0
        plan = ResourcePlan(
            task_type="GPU",
            devices=":".join(str(g.index) for g in usable),
            gpu_ram_part=gpu_ram_part,
            pinned_memory_size=int(min(PINNED_MEMORY_MAX, max(PINNED_MEMORY_MIN, spec.memory_mib * MIB // 16))),
            thread_count=max(1, spec.cpus // fold_concurrency),
            used_ram_limit=None,
            fold_concurrency=fold_concurrency,
            chunk_rows=0,
            vram_budget_bytes=vram_budget_bytes,
            ram_budget_bytes=ram_budget_bytes,
            gpu_names=tuple(g.name for g in usable),
            reason=reason,
        )
    else:
        fold_concurrency = max(1, min(spec.cpus // MIN_THREADS_PER_FOLD, by_ram))
        plan = ResourcePlan(
            task_type="CPU",
            devices="",
            gpu_ram_part=None,
            pinned_memory_size=None,
            thread_count=max(1, spec.cpus // fold_concurrency),
            used_ram_limit=f"{max(256, int(spec.memory_mib * RAM_BUDGET) // fold_concurrency)}mb",
            fold_concurrency=fold_concurrency,
            chunk_rows=0,
            vram_budget_bytes=0,
            ram_budget_bytes=ram_budget_bytes,
            reason=reason,
        )
    plan = replace(plan, chunk_rows=plan.chunk_rows_for(CHUNK_BYTES_PER_ROW))
    return apply_overrides(plan, overrides)


def apply_overrides(plan: ResourcePlan, overrides: Optional[Mapping[str, object]]) -> ResourcePlan:
    """Return `plan` with the non-None CLI `overrides` applied; see `plan_resources`."""
    if not overrides:
        return plan
    changes: Dict[str, object] = {}
    for name, value in overrides.items():
        if name not in PLAN_OVERRIDE_FIELDS:
            raise ValueError(f"Unknown resource plan override {name!r}; expected one of {PLAN_OVERRIDE_FIELDS}")
        if value is None or (name == "thread_count" and int(value) == -1):
            continue
        changes[name] = value
    if "devices" in changes and not plan.use_gpu:
        raise ValueError(f"--devices {changes['devices']} given but the plan trains on CPU ({plan.reason})")
    if "fold_concurrency" in changes:
        changes["fold_concurrency"] = max(1, int(changes["fold_concurrency"]))
        if "thread_count" not in changes:
            spare = plan.thread_count * plan.fold_concurrency
            changes["thread_count"] = max(1, spare // changes["fold_concurrency"])
    if not changes:
        return plan
    return replace(plan, **changes, overridden=tuple(sorted(set(plan.overridden) | set(changes))))


def main() -> int:
    spec = detect()
    out = asdict(spec)
    try:
        out["training_plan"] = asdict(plan_resources(spec))
    except RuntimeError as exc:  # pragma: no cover - only with --device gpu
        out["training_plan"] = str(exc)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
