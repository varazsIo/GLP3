"""
Build the ``(engine, cfg, log)`` arguments and data paths the alembic revisions need.

Revisions stay thin wrappers around ``pipeline_steps`` functions, which all
take that signature.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional

from alembic import op
from sqlalchemy import Engine

from hpldn.backend.common.config import get_cfg
from hpldn.backend.common.db import get_psql_env as _get_psql_env
from hpldn.backend.common.db import get_psql_url as _get_psql_url
from hpldn.backend.common.utils import setup_logger
from hpldn.backend.migrations import inputs
from hpldn.backend.migrations.inputs import DataSourceMissing  # noqa: F401  (re-exported)


def get_migration_engine() -> Engine:
    """
    Return the Engine alembic is running the migrations with.

    Pipeline steps open their own connections, so they need the Engine rather
    than alembic's per-migration Connection.
    """
    return op.get_bind().engine


def get_migration_cfg() -> Dict:
    """Load ``staging.yaml`` through `hpldn.backend.common.config.get_cfg`."""
    return get_cfg()


def get_migration_logger() -> logging.Logger:
    """Return the configured HPLDN logger, or a stdout logger if its log file is not writable."""
    try:
        return setup_logger(get_migration_cfg())
    except (OSError, FileNotFoundError):
        logger = logging.getLogger("hpldn_migrations")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler(sys.stdout))
        return logger


def get_psql_url(engine: Engine) -> str:
    """Render the engine's URL for the psql CLI, without the password; see `hpldn.backend.common.db.get_psql_url`."""
    return _get_psql_url(engine)


def get_psql_env(engine: Engine, env: Optional[Dict] = None) -> Dict:
    """Return the environment a psql subprocess needs; see `hpldn.backend.common.db.get_psql_env`."""
    return _get_psql_env(engine, env)


# Commands that produce the seed files which are generated rather than
# downloaded, quoted when one is missing; alembic upgrade itself stays offline.
# Read from each input's ``fetch`` in migrations/inputs.yaml.
FETCH_COMMANDS = inputs.fetch_commands()


def resolve_data_path(
    cfg: Optional[Dict],
    key: str,
    default_relative: Optional[str] = None,
    section: str = "data",
) -> Path:
    """
    Return the path for one migration input, resolved but not checked.

    Parameters
    ----------
    cfg : dict or None
        Loaded, interpolated ``staging.yaml``; its `section` block overrides
        the manifest path.
    key : str
        The ``migrations/inputs.yaml`` key, e.g. ``"epc_domestic_dir"``.
    default_relative : str, optional
        Ignored. Kept because applied revisions still pass it; the manifest
        now holds every path.
    section : str, optional
        Configuration block holding overrides, ``"data"`` by default.

    Returns
    -------
    pathlib.Path
        See `hpldn.backend.migrations.inputs.resolve`.

    Raises
    ------
    KeyError
        If `key` is not in ``inputs.yaml``.
    """
    return inputs.resolve(key, {"data": (cfg or {}).get(section) or {}})


def require_data_path(
    cfg: Optional[Dict],
    key: str,
    default_relative: Optional[str] = None,
    description: str = "",
    section: str = "data",
    must_be_dir: bool = False,
) -> Path:
    """
    Resolve a migration input's path and check it exists.

    Parameters
    ----------
    cfg, key, default_relative, section
        As for `resolve_data_path`.
    description : str, optional
        What reads the file, quoted in the error.
    must_be_dir : bool, optional
        Require a directory rather than a file.

    Returns
    -------
    pathlib.Path
        The existing path.

    Raises
    ------
    DataSourceMissing
        Naming the key, path, reader and, for generated files, the command
        in `FETCH_COMMANDS` that produces it.
    """
    path = resolve_data_path(cfg, key, default_relative, section)
    ok = path.is_dir() if must_be_dir else path.exists()
    if ok:
        return path

    what = "directory" if must_be_dir else "file"
    lines = [
        f"seed source {what} not found: {path}",
        f"  manifest key      : {key} (src/hpldn/backend/migrations/inputs.yaml)",
    ]
    if description:
        lines.append(f"  read by           : {description}")
    fetch = FETCH_COMMANDS.get(key)
    if fetch:
        lines.append(f"  produce it with   : {fetch}")
        lines.append(
            "  (this file is generated, not downloaded -- see the README's "
            "'Fetching the derived source files')"
        )
    else:
        lines.append(
            "  place the dataset under HPLDN_DATA_DIR at the manifest path "
            "(its source_url says where it comes from), or override "
            f"{section}.{key} in configuration/staging.yaml"
        )
    raise DataSourceMissing("\n".join(lines))
