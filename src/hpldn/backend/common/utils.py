"""Environment, YAML, logging and distribution-check helpers."""

from pathlib import Path
import os
import re
import yaml
import logging
import sys
import numpy as np
from typing import Union
from hpldn.backend.common.accel import asarray as accel_asarray
from hpldn.backend.common.accel import module_for, to_host


def load_env_vars(log: Union[logging.Logger, None] = None):
    """
    Load ``$HPLDN_DIR_FP/develop.env`` into `os.environ`.

    Values already in the environment win, so configuration delivered by a
    container or systemd unit is not overridden by a stale file. Set
    ``HPLDN_ENV_OVERRIDE=1`` to let the file win; ``HPLDN_ENV_NO_OVERRIDE=1``
    always keeps the environment.

    Parameters
    ----------
    log : logging.Logger, optional
        Logger for progress messages.

    Raises
    ------
    RuntimeError
        If ``HPLDN_DIR_FP`` is not set.
    """
    if log is not None:
        log.info("Loading environment variables...")
    override = (
        os.environ.get("HPLDN_ENV_OVERRIDE") == "1"
        and os.environ.get("HPLDN_ENV_NO_OVERRIDE") != "1"
    )
    no_override = not override
    hpldn_dir_fp = os.environ.get("HPLDN_DIR_FP")
    if hpldn_dir_fp is None:
        raise RuntimeError(
            "HPLDN_DIR_FP is not set, so develop.env cannot be located. Set "
            "it to the repository root (see the README's Configuration "
            "section); src/hpldn/backend/tests/conftest.py derives it for the "
            "test suite."
        )
    env_fp = Path(hpldn_dir_fp, "develop.env")
    with open(env_fp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if no_override and key in os.environ:
                    continue
                os.environ[key] = val
    if log is not None:
        log.info("Successfully loaded environment variables!")


def load_yaml_safe(
    fp: Path,
    log: Union[None, logging.Logger] = None,
):
    """
    Load a YAML file and expand ``${ENV_VAR}`` references in its strings.

    Parameters
    ----------
    fp : pathlib.Path
        The YAML file.
    log : logging.Logger, optional
        Logger for progress messages.

    Returns
    -------
    dict
        The loaded document. Unset variables expand to an empty string; use
        `hpldn.backend.common.config.interpolate_cfg` for ``${VAR:-default}``.
    """
    if log is not None:
        log.info("Loading yaml configuration file...")

    env_pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def expand_env(value):
        """Recursively expand variables in dicts, lists and strings."""
        if isinstance(value, dict):
            return {key: expand_env(val) for key, val in value.items()}

        if isinstance(value, list):
            return [expand_env(item) for item in value]

        if isinstance(value, str):
            return env_pattern.sub(
                lambda match: os.environ.get(match.group(1), ""),
                value,
            )

        return value

    with Path(fp).open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg = expand_env(cfg)

    if log is not None:
        log.info("Successfully loaded yaml configuration file!")

    return cfg


def setup_logger(cfg: dict):
    """
    Configure a logger that writes to a file and to stdout.

    Parameters
    ----------
    cfg : dict
        ``log_name``, ``log_level`` and ``log_fp`` settings.

    Returns
    -------
    logging.Logger
        The configured logger, with any previous handlers removed.
    """
    name = cfg.get("log_name", "app_logger")
    level = cfg.get("log_level", logging.INFO)
    filepath = cfg.get("log_fp", "app.log")
    formatter = logging.Formatter(
        "{asctime} [{levelname}] {name}: {message}", style="{"
    )

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.hasHandlers():
        logger.handlers.clear()

    file_handler = logging.FileHandler(filepath)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def check_none_variants(x, y=None, log=None):
    """
    Return True when `x` is a null-like scalar.

    Parameters
    ----------
    x : object
        Value to check. None, NaN, ``""``, ``"N/A"`` and ``"None"`` count as
        null; sequences and arrays never do.
    y : str, optional
        Column name used in the warning.
    log : logging.Logger, optional
        Logger that receives a warning when `x` is null-like.

    Returns
    -------
    bool
        Whether `x` is a null variant.
    """
    if isinstance(x, np.ndarray) or (
        hasattr(x, "__len__") and not isinstance(x, (str, bytes))
    ):
        none_variant = False
    elif x is None:
        none_variant = True
    elif isinstance(x, float) and np.isnan(x):
        none_variant = True
    else:
        none_variant = x in ("", "N/A", "None")
    if none_variant and y is None and isinstance(log, logging.Logger):
        log.warning(
            f"Variable has value of {x} when it should it should be None or null"
        )
    elif none_variant and isinstance(y, str) and isinstance(log, logging.Logger):
        log.warning(f"{y} has value of {x} when it should it should be None or null")
    return none_variant


def get_bounds_based_on_std(a: np.ndarray):
    """
    Return the 3-sigma ``(lower, upper)`` bounds as host numbers.

    Bounds are ints for int64 input, and the lower bound is floored at 0.
    They are interpolated into seeding SQL, so they are never device scalars.
    """
    dt = getattr(a, "dtype", None)
    b = accel_asarray(a, dtype=np.float64)
    xp = module_for(b)
    m = xp.mean(b)
    std = xp.std(b)
    lb = float(to_host(m - (3 * std)))
    ub = float(to_host(m + (3 * std)))
    if dt == np.int64:
        lb = int(round(lb, 0))
        ub = int(round(ub, 0))
    if lb < 0.0:
        lb = 0.0
    return (lb, ub)
