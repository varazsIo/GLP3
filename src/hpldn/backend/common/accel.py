"""
GPU-accelerated array maths, with a transparent numpy fallback.

The EDA statistics and outlier bounds computed around the database use CuPy on
CUDA hosts and numpy everywhere else. Set ``HPLDN_DISABLE_GPU=1`` to force
numpy. Anything leaving this layer must pass through `to_host`::

    xp = get_array_module()
    mean = float(to_host(xp.mean(xp.asarray(values, dtype=xp.float64))))

`StreamingExecutor` runs a row-chunked computation through VRAM in pinned,
double-buffered chunks, with the same code path on NumPy.
"""

import logging
import os
from typing import Dict, List, Optional, Sequence

import numpy as np

_ARRAY_MODULE = None
_GPU_STATUS: Optional[str] = None


def _resolve_array_module(log: Optional[logging.Logger] = None):
    """
    Choose the array module.

    Returns
    -------
    module : module
        `cupy` when a CUDA GPU and a working CuPy are present, else `numpy`.
    status : str
        Why that module was chosen.
    """
    if os.environ.get("HPLDN_DISABLE_GPU") == "1":
        return np, "disabled by HPLDN_DISABLE_GPU"

    from hpldn.backend.common.tuning import detect_host_resources

    resources = detect_host_resources(log)
    if not resources.has_cuda_gpu:
        if resources.gpu_count:
            return np, (
                f"{resources.gpu_count}x {resources.gpu_manufacturer} GPU present "
                "but not CUDA-capable"
            )
        return np, "no GPU detected"

    try:
        import cupy  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError:
        return np, "CUDA GPU present but cupy is not installed (pip install 'hpldn[gpu]')"

    try:
        cupy.zeros(1).sum()
    except Exception as err:  # pragma: no cover - depends on driver state
        return np, f"cupy present but unusable ({err})"

    return cupy, f"cupy on {resources.gpu_count}x {resources.gpu_manufacturer} {resources.gpu_name}".strip()


def get_array_module(log: Optional[logging.Logger] = None):
    """
    Return the array module to compute with, `cupy` on a CUDA host else `numpy`.

    The choice is cached for the process. Both modules expose the reductions
    used here, so callers do not branch.
    """
    global _ARRAY_MODULE, _GPU_STATUS
    if _ARRAY_MODULE is None:
        _ARRAY_MODULE, _GPU_STATUS = _resolve_array_module(log)
        if log is not None:
            log.debug("Array backend: %s (%s)", _ARRAY_MODULE.__name__, _GPU_STATUS)
    return _ARRAY_MODULE


def gpu_enabled(log: Optional[logging.Logger] = None) -> bool:
    """Return True when array maths is running on a GPU."""
    return get_array_module(log).__name__ != "numpy"


def gpu_status(log: Optional[logging.Logger] = None) -> str:
    """Return why the current backend was chosen, for logging."""
    get_array_module(log)
    return _GPU_STATUS or "unknown"


def module_for(a):
    """
    Return the module that can operate on `a`: `cupy` for device arrays, else `numpy`.

    Non-numeric data stays on the host even with the GPU backend active, so the
    module is a property of the array, not of the process.
    """
    if type(a).__module__.startswith("cupy"):
        return get_array_module()
    return np


def to_host(a):
    """Return an array or scalar as numpy; a no-op on the numpy path."""
    get = getattr(a, "get", None)
    if get is not None and type(a).__module__.startswith("cupy"):
        return get()
    return a


def asarray(values, dtype=None, log: Optional[logging.Logger] = None):
    """
    Build an array on whichever device the backend uses.

    Parameters
    ----------
    values : array_like
        Input values, possibly already a device array.
    dtype : data-type, optional
        Target dtype.
    log : logging.Logger, optional
        Logger for backend selection.

    Returns
    -------
    ndarray
        A CuPy array for numeric data on the GPU backend, otherwise numpy.

    Notes
    -----
    Values are staged through numpy because CuPy cannot ingest arbitrary
    Python object sequences. A device array is handled first, since
    ``np.array(cupy_array)`` raises rather than copying to the host, and the
    helpers routinely pass earlier `asarray` output back in.
    """
    xp = get_array_module(log)
    if type(values).__module__.startswith("cupy"):
        return values.astype(dtype) if dtype is not None else values
    if xp is np:
        return np.array(values, dtype=dtype)
    staged = np.array(values, dtype=dtype)
    if staged.dtype.kind not in "biufc":
        return staged
    return xp.asarray(staged)


def reset_backend() -> None:
    """Forget the cached backend choice, for tests toggling ``HPLDN_DISABLE_GPU``."""
    global _ARRAY_MODULE, _GPU_STATUS
    from hpldn.backend.common.tuning import reset_detection_cache

    _ARRAY_MODULE = None
    _GPU_STATUS = None
    reset_detection_cache()


# ---------------------------------------------------------------------------
# Streaming executor
# ---------------------------------------------------------------------------


class StreamingExecutor:
    """
    Run a row-chunked computation on the GPU when there is one, else in NumPy, by one code path.

    The inputs stay in host RAM; only `chunk_rows` rows of them are on the
    device at a time, so a frame larger than VRAM streams through it. On the
    GPU each chunk is copied into a pinned host buffer, sent on one of two
    CUDA streams (double buffering: while chunk *k* computes on one stream,
    chunk *k + 1* is uploaded on the other), computed by `fn` and copied back
    on the same stream. CuPy's memory pool is capped at `vram_limit_bytes`
    (`hardware.ResourcePlan.vram_budget_bytes`). On the NumPy fallback the
    same loop runs with plain host slices and ``xp = numpy``, so the two
    paths differ only in the module `fn` receives.

    `fn(xp, *chunks, *consts)` gets each input sliced to the chunk (rows on
    the first axis) and the `consts` whole, already on the device; it returns
    one array or a tuple of arrays of any length. `run` returns, per chunk, the
    outputs as NumPy arrays; `map_rows` concatenates them.

    Parameters
    ----------
    chunk_rows : int, optional
        Rows per chunk; `hardware.ResourcePlan.chunk_rows` when omitted.
    use_gpu : bool, optional
        Force the backend; default `gpu_enabled`.
    vram_limit_bytes : int, optional
        The memory-pool limit; default the plan's ``vram_budget_bytes``.
    """

    def __init__(
        self,
        chunk_rows: Optional[int] = None,
        use_gpu: Optional[bool] = None,
        vram_limit_bytes: Optional[int] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.use_gpu = gpu_enabled(log) if use_gpu is None else bool(use_gpu and gpu_enabled(log))
        plan = None
        if chunk_rows is None or (self.use_gpu and vram_limit_bytes is None):
            from hpldn.backend.common import hardware

            plan = hardware.plan_resources(device="auto" if self.use_gpu else "cpu")
        self.chunk_rows = int(chunk_rows if chunk_rows is not None else plan.chunk_rows)
        if self.chunk_rows < 1:
            raise ValueError(f"chunk_rows must be positive, got {self.chunk_rows}")
        self.xp = get_array_module(log) if self.use_gpu else np
        self._streams = None
        if self.use_gpu:
            import cupy  # noqa: PLC0415 - only on the GPU path

            limit = vram_limit_bytes if vram_limit_bytes is not None else plan.vram_budget_bytes
            if limit:
                pool = cupy.get_default_memory_pool()
                # Never lower a limit another executor in this process set higher.
                if pool.get_limit() == 0 or pool.get_limit() < limit:
                    pool.set_limit(size=int(limit))
            self._streams = (cupy.cuda.Stream(non_blocking=True), cupy.cuda.Stream(non_blocking=True))

    # -- chunking -------------------------------------------------------

    def chunks(self, n_rows: int, chunk_rows: Optional[int] = None):
        """Yield ``(start, stop)`` row ranges covering `n_rows`."""
        step = int(chunk_rows or self.chunk_rows)
        for start in range(0, n_rows, step):
            yield start, min(start + step, n_rows)

    def _n_rows(self, inputs: Sequence[np.ndarray]) -> int:
        if not inputs:
            raise ValueError("StreamingExecutor needs at least one row input")
        n = len(inputs[0])
        if any(len(a) != n for a in inputs):
            raise ValueError("StreamingExecutor row inputs must have the same length")
        return n

    # -- running --------------------------------------------------------

    def run(self, fn, inputs: Sequence[np.ndarray], consts: Sequence = (), chunk_rows: Optional[int] = None) -> List[tuple]:
        """Return `fn`'s outputs for every chunk, as tuples of NumPy arrays; see the class docstring."""
        inputs = [np.asarray(a) for a in inputs]
        n = self._n_rows(inputs)
        if not self.use_gpu:
            return [_as_tuple(fn(np, *[a[lo:hi] for a in inputs], *consts)) for lo, hi in self.chunks(n, chunk_rows)]
        return self._run_gpu(fn, inputs, consts, n, chunk_rows)

    def map_rows(self, fn, inputs: Sequence[np.ndarray], consts: Sequence = (), chunk_rows: Optional[int] = None):
        """`run`, with each output concatenated over the chunks; one array for a single output."""
        per_chunk = self.run(fn, inputs, consts, chunk_rows)
        if not per_chunk:
            return None
        joined = tuple(np.concatenate([chunk[i] for chunk in per_chunk]) for i in range(len(per_chunk[0])))
        return joined[0] if len(joined) == 1 else joined

    def _run_gpu(self, fn, inputs, consts, n: int, chunk_rows: Optional[int]) -> List[tuple]:
        import cupy  # noqa: PLC0415
        import cupyx  # noqa: PLC0415

        step = int(chunk_rows or self.chunk_rows)
        device_consts = tuple(
            cupy.asarray(c) if isinstance(c, np.ndarray) and c.dtype.kind in "biufc" else c for c in consts
        )
        # Two pinned staging buffers per input, one per stream.
        staging = [
            [cupyx.empty_pinned((min(step, n),) + a.shape[1:], dtype=a.dtype) for a in inputs] for _ in range(2)
        ]
        pending: List[Optional[tuple]] = [None, None]
        results: Dict[int, tuple] = {}

        def _collect(slot: int) -> None:
            entry = pending[slot]
            if entry is None:
                return
            index, host_outputs = entry
            self._streams[slot].synchronize()
            results[index] = tuple(np.array(h, copy=True) for h in host_outputs)
            pending[slot] = None

        for index, (lo, hi) in enumerate(self.chunks(n, step)):
            slot = index % 2
            stream = self._streams[slot]
            # The buffers of this slot are free once its previous chunk is back.
            _collect(slot)
            rows = hi - lo
            with stream:
                device_chunks = []
                for host, buffer in zip(inputs, staging[slot]):
                    view = buffer[:rows]
                    view[...] = host[lo:hi]
                    device = cupy.empty(view.shape, dtype=view.dtype)
                    device.set(view, stream=stream)
                    device_chunks.append(device)
                outputs = _as_tuple(fn(cupy, *device_chunks, *device_consts))
                host_outputs = []
                for out in outputs:
                    if not isinstance(out, cupy.ndarray):
                        host_outputs.append(np.asarray(out))
                        continue
                    pinned = cupyx.empty_pinned(out.shape, dtype=out.dtype)
                    out.get(stream=stream, out=pinned)
                    host_outputs.append(pinned)
            pending[slot] = (index, host_outputs)
        _collect(0)
        _collect(1)
        return [results[i] for i in sorted(results)]


def _as_tuple(outputs) -> tuple:
    return outputs if isinstance(outputs, tuple) else (outputs,)
