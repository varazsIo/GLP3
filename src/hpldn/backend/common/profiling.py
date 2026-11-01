"""
Stage timings for training runs: wall time, peak RSS and peak VRAM per stage.

`stage` is a context manager that records one named stage of a run::

    with profiling.stage("features/sizeband"):
        df = add_sizeband_comp_features(df, index)

Stages nest per thread, so a stage opened inside ``backtest/fold 2016`` is
recorded as ``backtest/fold 2016/pass1``. While any stage is open a sampler
thread reads the process's resident set (``/proc/self/statm``) and, once a GPU
is in use, the device memory in use (`hardware.gpu_memory_used_mib`), every
`SAMPLE_INTERVAL_S`; each stage keeps the largest value seen while it was
open. RSS is process-wide, so stages running concurrently in threads see each
other's memory. A spike shorter than the interval can be missed; the peak RSS
of the whole process (``VmHWM``) is recorded beside the stages as an upper
bound.

`Profiler.to_dict` / `write_json` give the ``--profile-json`` payload:
the stages in the order they finished, the host's `hardware.ResourcePlan` and
any parity facts a run attaches (`Profiler.note`), such as
`frame_fingerprint` of the engineered frame.
"""

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

import numpy as np

MIB = 1024 * 1024
SAMPLE_INTERVAL_S = 0.05

try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX
    _PAGE_SIZE = 4096


def rss_mib() -> Optional[float]:
    """Return this process's resident set in MiB, or None where ``/proc`` is absent."""
    try:
        with open("/proc/self/statm") as handle:
            return int(handle.read().split()[1]) * _PAGE_SIZE / MIB
    except (OSError, ValueError, IndexError):
        return None


def peak_rss_mib() -> Optional[float]:
    """Return the process's lifetime peak resident set (``VmHWM``) in MiB, or None."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _default_vram_probe() -> Optional[float]:
    """
    Return the GPU memory in use on every device, in MiB, once this process uses the GPU.

    Never initialises CUDA itself: it reads nothing until `accel` has chosen
    CuPy (or a caller marks the GPU in use with `Profiler.enable_vram`), so a
    CPU-only run is not given a CUDA context by profiling.
    """
    from hpldn.backend.common import hardware

    return hardware.gpu_memory_used_mib()


@dataclass
class StageRecord:
    """One finished stage."""

    name: str
    wall_s: float
    rss_start_mib: Optional[float] = None
    rss_end_mib: Optional[float] = None
    peak_rss_mib: Optional[float] = None
    vram_start_mib: Optional[float] = None
    peak_vram_mib: Optional[float] = None
    thread: str = ""
    extra: Dict[str, object] = field(default_factory=dict)


class _OpenStage:
    __slots__ = ("name", "start", "rss_start", "peak_rss", "vram_start", "peak_vram", "extra")

    def __init__(self, name: str, rss: Optional[float], vram: Optional[float], extra: Dict[str, object]):
        self.name = name
        self.start = time.perf_counter()
        self.rss_start = rss
        self.peak_rss = rss
        self.vram_start = vram
        self.peak_vram = vram
        self.extra = extra

    def observe(self, rss: Optional[float], vram: Optional[float]) -> None:
        if rss is not None and (self.peak_rss is None or rss > self.peak_rss):
            self.peak_rss = rss
        if vram is not None and (self.peak_vram is None or vram > self.peak_vram):
            self.peak_vram = vram


class Profiler:
    """
    Collects `StageRecord`s; see the module docstring.

    Parameters
    ----------
    sample_interval_s : float
        How often the sampler thread reads RSS and VRAM while a stage is open.
    vram_probe : callable, optional
        Returns device memory in use in MiB, or None. Called only after
        `enable_vram`.
    """

    def __init__(
        self,
        sample_interval_s: float = SAMPLE_INTERVAL_S,
        vram_probe: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.sample_interval_s = sample_interval_s
        self._vram_probe = vram_probe or _default_vram_probe
        self._vram_enabled = False
        self._lock = threading.Lock()
        self._local = threading.local()
        self._open: List[_OpenStage] = []
        self.records: List[StageRecord] = []
        self.notes: Dict[str, object] = {}
        self._sampler: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self.started = time.time()

    # -- sampling -------------------------------------------------------

    def enable_vram(self, enabled: bool = True) -> None:
        """Start (or stop) reading device memory; call once the run has chosen the GPU."""
        self._vram_enabled = enabled

    def _vram(self) -> Optional[float]:
        if not self._vram_enabled:
            return None
        try:
            return self._vram_probe()
        except Exception:  # noqa: BLE001 - a failed probe only loses the figure
            return None

    def _sample_loop(self) -> None:
        while True:
            with self._lock:
                if not self._open:
                    self._sampler = None
                    return
            rss, vram = rss_mib(), self._vram()
            with self._lock:
                for open_stage in self._open:
                    open_stage.observe(rss, vram)
            self._wake.wait(self.sample_interval_s)
            self._wake.clear()

    def _ensure_sampler(self) -> None:
        # Called with the lock held.
        if self._sampler is None:
            self._sampler = threading.Thread(target=self._sample_loop, name="hpldn-profiler", daemon=True)
            self._sampler.start()

    # -- stages ---------------------------------------------------------

    def _stack(self) -> List[str]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    @contextmanager
    def stage(self, name: str, **extra) -> Iterator[Dict[str, object]]:
        """
        Record the enclosed block as stage `name`, nested under any open stage of this thread.

        Yields the stage's ``extra`` dict, which the block may add facts to
        (row counts, iterations); they are saved with the record.
        """
        stack = self._stack()
        full_name = "/".join([*stack, name])
        rss, vram = rss_mib(), self._vram()
        open_stage = _OpenStage(full_name, rss, vram, dict(extra))
        with self._lock:
            self._open.append(open_stage)
            self._ensure_sampler()
        stack.append(name)
        try:
            yield open_stage.extra
        finally:
            stack.pop()
            rss_end, vram_end = rss_mib(), self._vram()
            with self._lock:
                open_stage.observe(rss_end, vram_end)
                self._open.remove(open_stage)
                self.records.append(
                    StageRecord(
                        name=full_name,
                        wall_s=round(time.perf_counter() - open_stage.start, 4),
                        rss_start_mib=_round(open_stage.rss_start),
                        rss_end_mib=_round(rss_end),
                        peak_rss_mib=_round(open_stage.peak_rss),
                        vram_start_mib=_round(open_stage.vram_start),
                        peak_vram_mib=_round(open_stage.peak_vram),
                        thread=threading.current_thread().name,
                        extra=open_stage.extra,
                    )
                )
            self._wake.set()

    def note(self, key: str, value: object) -> None:
        """Attach a run-level fact (the resource plan, a frame fingerprint, headline metrics)."""
        with self._lock:
            self.notes[key] = value

    # -- output ---------------------------------------------------------

    def totals(self) -> Dict[str, float]:
        """Return wall seconds per stage name, summed over repeats (e.g. one per fold)."""
        out: Dict[str, float] = {}
        for record in self.records:
            out[record.name] = round(out.get(record.name, 0.0) + record.wall_s, 4)
        return out

    def to_dict(self) -> Dict[str, object]:
        with self._lock:
            records = [asdict(r) for r in self.records]
            notes = dict(self.notes)
        return {
            "started": self.started,
            "wall_s": round(time.time() - self.started, 3),
            "process_peak_rss_mib": _round(peak_rss_mib()),
            "stages": records,
            "totals": self.totals(),
            "notes": notes,
        }

    def write_json(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=_json_default))
        return path


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 1)


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    return str(value)


# ---------------------------------------------------------------------------
# The process-wide profiler
# ---------------------------------------------------------------------------

_PROFILER = Profiler()


def get_profiler() -> Profiler:
    """Return the process-wide profiler every `stage` call records into."""
    return _PROFILER


def reset_profiler(**kwargs) -> Profiler:
    """Replace the process-wide profiler (a new run, or a test)."""
    global _PROFILER
    _PROFILER = Profiler(**kwargs)
    return _PROFILER


def stage(name: str, **extra):
    """`Profiler.stage` on the process-wide profiler."""
    return _PROFILER.stage(name, **extra)


def note(key: str, value: object) -> None:
    """`Profiler.note` on the process-wide profiler."""
    _PROFILER.note(key, value)


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


def frame_fingerprint(df, columns=None) -> Dict[str, object]:
    """
    Return a content hash of a DataFrame: rows, columns, dtypes and every value.

    Two frames with the same fingerprint hold the same values in the same
    order and column layout, so a before/after pair proves a refactor left the
    engineered frame unchanged. Float values are hashed bit for bit: a
    difference in the last place changes the hash.
    """
    import pandas as pd

    frame = df if columns is None else df[list(columns)]
    digest = hashlib.sha256()
    digest.update(json.dumps([[str(c), str(frame[c].dtype)] for c in frame.columns]).encode())
    digest.update(np.asarray(len(frame), dtype=np.int64).tobytes())
    per_column = {}
    for col in frame.columns:
        hashed = pd.util.hash_pandas_object(frame[col], index=False).to_numpy()
        column_digest = hashlib.sha256(hashed.tobytes()).hexdigest()
        per_column[str(col)] = column_digest[:16]
        digest.update(column_digest.encode())
    return {
        "rows": int(len(frame)),
        "columns": int(frame.shape[1]),
        "sha256": digest.hexdigest(),
        "columns_sha256": per_column,
    }


__all__ = [
    "Profiler",
    "StageRecord",
    "frame_fingerprint",
    "get_profiler",
    "note",
    "peak_rss_mib",
    "reset_profiler",
    "rss_mib",
    "stage",
]
