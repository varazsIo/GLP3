"""
Locate, load and interpolate ``configuration/staging.yaml``.

The file is resolved from ``HPLDN_CONFIG_FP`` when set, otherwise from
``<repo root>/configuration/staging.yaml``, where the repo root is
``HPLDN_DIR_FP`` or is derived from this module's location.
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Union

from hpldn.backend.common.utils import load_yaml_safe

CONFIG_DIR_NAME = "configuration"
CONFIG_FILE_NAME = "staging.yaml"


def get_repo_root() -> Path:
    """
    Return the HPLDN checkout root.

    ``HPLDN_DIR_FP`` wins; otherwise the root is four parents above this
    module, which is only correct from a source checkout.
    """
    root = os.environ.get("HPLDN_DIR_FP")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[4]


def get_cfg_fp() -> Path:
    """Return the path ``staging.yaml`` is expected at, without reading it."""
    explicit = os.environ.get("HPLDN_CONFIG_FP")
    if explicit:
        return Path(explicit)
    return get_repo_root().joinpath(CONFIG_DIR_NAME, CONFIG_FILE_NAME)


def get_cfg(log: Optional[logging.Logger] = None) -> Dict:
    """
    Load and interpolate ``staging.yaml``.

    Parameters
    ----------
    log : logging.Logger, optional
        Logger passed to `load_yaml_safe`.

    Returns
    -------
    dict
        The configuration with every ``${VAR}`` expanded.

    Raises
    ------
    FileNotFoundError
        If the file is missing, naming the path that was tried.
    """
    cfg_fp = get_cfg_fp()
    if not cfg_fp.is_file():
        raise FileNotFoundError(
            f"HPLDN configuration not found at {cfg_fp}. Set HPLDN_CONFIG_FP "
            f"to its location, or create {CONFIG_DIR_NAME}/{CONFIG_FILE_NAME} "
            f"in the repository root (see the README's Configuration section)."
        )
    return interpolate_cfg(load_yaml_safe(cfg_fp, log))


def resolve_cfg(cfg: Union[Dict, None] = None, log: Optional[logging.Logger] = None) -> Dict:
    """Return `cfg` when supplied, otherwise the loaded configuration file."""
    return cfg if cfg is not None else get_cfg(log)


_INTERPOLATION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}")

MAX_INTERPOLATION_PASSES = 8


def interpolate(value: str, env: Optional[Dict[str, str]] = None) -> str:
    """
    Expand ``${VAR}`` and ``${VAR:-default}`` in `value`.

    Parameters
    ----------
    value : str
        The string to expand. Any other ``$`` is left alone.
    env : dict of str to str, optional
        Variables to expand against; `os.environ` by default.

    Returns
    -------
    str
        The fully expanded string.

    Raises
    ------
    KeyError
        If ``${VAR}`` has no default and ``VAR`` is not set.
    ValueError
        If expansion has not reached a fixed point after
        `MAX_INTERPOLATION_PASSES`, e.g. a self-referential variable.

    Notes
    -----
    Defaults may themselves contain ``${...}`` (for example
    ``${HPLDN_DATA_DIR:-${HPLDN_DIR_FP}/data}``), so substitution repeats
    until the value stops changing.
    """
    env = os.environ if env is None else env

    def _sub(match: "re.Match") -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise KeyError(
            f"configuration references ${{{name}}}, which is not set in the "
            f"environment. develop.env defines HPLDN_DIR_FP; source it, or "
            f"give the key a literal path."
        )

    for _ in range(MAX_INTERPOLATION_PASSES):
        expanded = _INTERPOLATION_RE.sub(_sub, value)
        if expanded == value:
            return expanded
        value = expanded
    raise ValueError(
        f"configuration value did not stop expanding after "
        f"{MAX_INTERPOLATION_PASSES} passes: {value!r} — check for a "
        f"self-referential ${{VAR}}"
    )


def interpolate_cfg(node, env: Optional[Dict[str, str]] = None):
    """
    Interpolate every string leaf of a loaded configuration.

    Parameters
    ----------
    node : dict, list, str or object
        A configuration node; non-string leaves are returned unchanged.
    env : dict of str to str, optional
        Variables to expand against; `os.environ` by default.

    Returns
    -------
    object
        A new node of the same shape with strings expanded.
    """
    if isinstance(node, dict):
        return {key: interpolate_cfg(val, env) for key, val in node.items()}
    if isinstance(node, list):
        return [interpolate_cfg(val, env) for val in node]
    if isinstance(node, str):
        return interpolate(node, env)
    return node
