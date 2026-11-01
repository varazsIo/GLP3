"""
A parquet cache of `hpp_cb`'s engineered training frame.

Engineering the frame (`hpp_cb._engineer_training_frame`: the rolling and
same-street statistics, the size-matched comparables, the local price index
and the market-relative features) is the same work for every run against the
same database with the same feature code, so `hpp_cb` saves it once in
``tmp/cache/features/<key>.parquet`` and later runs read it back instead of
loading and engineering again.

The key (`cache_key`) hashes:

* the database state (`dataset_state`): its alembic revision, and for the
  population and ``--limit`` being loaded the row count, the largest
  ``ppd_id`` and the last sale date, plus the row count and latest date of
  ``production.ukhpi`` and ``production.mortgage_rates``;
* the feature code (`feature_code_hash`): the source of every module the
  engineering runs;
* the run parameters that change the frame: population, limit, float width,
  ``--no-market-features``, the training cutoff the local index is built to
  (``--backtest``/``--spatial-generalisation`` holdout months, or
  ``--train-frac``) and whether the GPU computed it (the GPU path agrees with
  the CPU to rounding, not bit for bit).

A change the key cannot see -- rows edited in place without changing the
count or the largest id -- needs ``--feature-cache refresh``. Each entry has a
``.json`` sidecar with the key's inputs; only the newest `KEEP_ENTRIES` are
kept.
"""

import hashlib
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Mapping, Optional

import pandas as pd
from sqlalchemy import text

CACHE_SUBDIR = Path("tmp") / "cache" / "features"
KEEP_ENTRIES = 3
FORMAT_VERSION = 1

FEATURE_CACHE_MODES = ("on", "off", "refresh")


def cache_dir() -> Path:
    """Return ``<repo>/tmp/cache/features`` (``HPLDN_FEATURE_CACHE_DIR`` overrides it)."""
    override = os.environ.get("HPLDN_FEATURE_CACHE_DIR")
    if override:
        return Path(override)
    from hpldn.backend.common.config import get_repo_root  # noqa: PLC0415

    return get_repo_root() / CACHE_SUBDIR


def feature_code_hash() -> str:
    """
    Return a hash of the source of every module that shapes the engineered frame.

    Whole modules for the feature libraries; for `hpp_cb` only the functions
    the engineering runs, so a change to a training flag does not throw the
    cache away.
    """
    from hpldn.backend.common import constants  # noqa: PLC0415
    from hpldn.backend.scripts import hpi_projection, hpp_cb, hpp_rf, local_index  # noqa: PLC0415
    from hpldn.backend.scripts import ofsted_features  # noqa: PLC0415

    digest = hashlib.sha256(f"format={FORMAT_VERSION}".encode())
    for module in (constants, hpp_rf, local_index, hpi_projection, ofsted_features):
        digest.update(inspect.getsource(module).encode())
    for function in (
        hpp_cb._engineer_training_frame,
        hpp_cb.add_mortgage_rate_features,
        hpp_cb.add_canonical_categoricals,
        hpp_cb._canonical_categorical,
        hpp_cb.training_cutoff,
        hpp_cb._training_local_index,
    ):
        digest.update(inspect.getsource(function).encode())
    return digest.hexdigest()


def dataset_state(engine, population: str, limit: Optional[int]) -> Dict[str, object]:
    """Return the database facts the key is built from; see the module docstring."""
    from hpldn.backend.models import artifact  # noqa: PLC0415
    from hpldn.backend.scripts.hpp_rf import DATE_COLUMN, dataset_sql  # noqa: PLC0415

    selected = dataset_sql(limit, population)
    with engine.connect() as connection:
        rows, max_ppd_id, last_sale = connection.execute(
            text(f"SELECT count(*), max(ppd_id), max({DATE_COLUMN}) FROM ({selected}) AS selected")
        ).one()
        ukhpi = connection.execute(
            text("SELECT count(*), max(period), max(published_date) FROM production.ukhpi")
        ).one()
        mortgage = connection.execute(text("SELECT count(*), max(rate_date) FROM production.mortgage_rates")).one()
    return {
        "database": engine.url.database,
        "alembic": artifact.alembic_revision(engine),
        "rows": int(rows),
        "max_ppd_id": None if max_ppd_id is None else int(max_ppd_id),
        "last_sale": None if last_sale is None else str(last_sale),
        "ukhpi": [int(ukhpi[0]), str(ukhpi[1]), str(ukhpi[2])],
        "mortgage_rates": [int(mortgage[0]), str(mortgage[1])],
    }


def run_parameters(args, use_gpu: bool) -> Dict[str, object]:
    """Return the ``hpp_cb`` arguments that change the engineered frame."""
    protocol = "holdout" if (args.backtest or args.spatial_generalisation) else "split"
    return {
        "population": args.population,
        "limit": args.limit,
        "feature_dtype": args.feature_dtype,
        "market_features": bool(getattr(args, "market_features", True)),
        "cutoff": (
            {"protocol": protocol, "holdout_months": args.holdout_months}
            if protocol == "holdout"
            else {"protocol": protocol, "train_frac": args.train_frac}
        ),
        "gpu": bool(use_gpu),
    }


def cache_key(state: Mapping[str, object], parameters: Mapping[str, object], code_hash: Optional[str] = None) -> str:
    """Return the cache key for a database state, run parameters and feature code."""
    payload = {
        "state": dict(state),
        "parameters": dict(parameters),
        "code": code_hash or feature_code_hash(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def entry_path(key: str, directory: Optional[Path] = None) -> Path:
    return (directory or cache_dir()) / f"{key}.parquet"


def load(key: str, directory: Optional[Path] = None, log: Optional[logging.Logger] = None) -> Optional[pd.DataFrame]:
    """Return the cached frame for `key`, or None when there is none (or it cannot be read)."""
    path = entry_path(key, directory)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - a broken entry is a miss, never a failed run
        if log is not None:
            log.warning(f"Feature cache: could not read {path} ({exc}); rebuilding")
        return None
    os.utime(path)
    if log is not None:
        log.info(f"Feature cache hit: {path} ({len(df):,} rows, {df.shape[1]} columns)")
    return df


def save(
    key: str,
    df: pd.DataFrame,
    description: Mapping[str, object],
    directory: Optional[Path] = None,
    log: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """Write `df` under `key` (atomically, with a ``.json`` sidecar) and prune old entries; None on failure."""
    directory = directory or cache_dir()
    path = entry_path(key, directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f".parquet.{os.getpid()}.partial")
        df.to_parquet(partial, index=True)
        partial.replace(path)
        path.with_suffix(".json").write_text(
            json.dumps({"key": key, "written": time.time(), "rows": len(df), **description}, indent=2, default=str)
        )
    except Exception as exc:  # noqa: BLE001 - the run goes on without a cache
        if log is not None:
            log.warning(f"Feature cache: could not write {path} ({exc})")
        return None
    prune(directory, keep=KEEP_ENTRIES)
    if log is not None:
        log.info(f"Feature cache: saved {path} ({path.stat().st_size / 1e9:.2f} GB)")
    return path


def prune(directory: Path, keep: int = KEEP_ENTRIES) -> None:
    """Delete all but the `keep` most recently used entries."""
    entries = sorted(directory.glob("*.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in entries[keep:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".json").unlink(missing_ok=True)


__all__ = [
    "FEATURE_CACHE_MODES",
    "cache_dir",
    "cache_key",
    "dataset_state",
    "feature_code_hash",
    "load",
    "prune",
    "run_parameters",
    "save",
]
