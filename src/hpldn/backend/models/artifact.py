"""
Transparent, signed price-model bundles.

A bundle is a directory, ``tmp/models/<name>/``, holding only data:

=====================  ===========================================================
``model.cbm``          the CatBoost model (FlatBuffers, in canonical form, below)
``expert.cbm``         the ``--two-stage`` high-value expert, when one was fitted
``meta.json``          the preprocessing state ``hpp_cb`` scores with (it was a
                       pickle, ``.meta.joblib``); the two stats frames are records
``calibration.json``   ``hpp_inference evaluate --write-calibration``
``projection.json``    ``hpp_inference fit-projection --write``
``local_index.json``   ``hpp_inference fit-local-index --write``
``probe.json``         64 fixed feature rows and each model's raw predictions
``manifest.json``      schema version, git sha, CatBoost version, training args,
                       dataset fingerprint, sha256 and size of every file above,
                       and a summary of each model read back from the model
``manifest.sig``       ``ed25519 <key id> <base64 signature of manifest.json>``
=====================  ===========================================================

Canonical form: CatBoost keeps a metadata map (``params``, ``training``,
``model_guid`` ...) whose JSON it re-serialises in a different key order on
every save, so a trained model never reads back as its own bytes. A bundle's
``.cbm`` has that map emptied (the values move to the manifest's
``catboost_metadata``) and is saved until it reaches a fixed point, after which
``load_model`` -> ``save_model`` reproduces it byte for byte. That is what lets
`verify` prove the file holds nothing CatBoost's schema does not parse.

`verify` checks, in order:

1. the signature, against the public keys tracked in ``trusted_keys/``;
2. the file allowlist and every file's size and sha256; a ``.joblib`` or
   ``.pkl`` anywhere fails it;
3. every JSON file parses strictly (no NaN, no duplicate keys) and matches its
   schema;
4. each ``.cbm`` has CatBoost's magic and survives the canonical round-trip, in
   a child process (`run_sandbox`) with an empty environment, resource limits
   and no network;
5. each model's summary matches the manifest's;
6. each model predicts the probe rows exactly as ``probe.json`` says.

The API runs 1-3 at warm-up (`load_bundle`) and 5-6 in process on the model it
loaded; the serving host runs all six before it switches models
(``cloud/vps/deploy.sh``). ``HPLDN_ALLOW_UNSIGNED_MODEL=1`` accepts a bundle
without a trusted signature, for local development only: it is refused when
``HPLDN_ENV=production``.

Command line::

    python -m hpldn.backend.models.artifact keygen
    python -m hpldn.backend.models.artifact convert tmp/models/model_anchored_0031.cbm tmp/models/model_anchored_0031
    python -m hpldn.backend.models.artifact sign tmp/models/model_anchored_0031
    python -m hpldn.backend.models.artifact verify tmp/models/model_anchored_0031
    python -m hpldn.backend.models.artifact export-json tmp/models/model_anchored_0031 --out tmp/model_json

``convert`` is the one place a pickle is still read: a one-time, trusted load
of a model saved before bundles existed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import resource
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

SCHEMA_VERSION = 1

MODEL_FILE = "model.cbm"
EXPERT_FILE = "expert.cbm"
META_FILE = "meta.json"
CALIBRATION_FILE = "calibration.json"
PROJECTION_FILE = "projection.json"
LOCAL_INDEX_FILE = "local_index.json"
PROBE_FILE = "probe.json"
MANIFEST_FILE = "manifest.json"
SIGNATURE_FILE = "manifest.sig"

MODEL_FILES = (MODEL_FILE, EXPERT_FILE)
REQUIRED_FILES = (MODEL_FILE, META_FILE, PROBE_FILE)
OPTIONAL_FILES = (EXPERT_FILE, CALIBRATION_FILE, PROJECTION_FILE, LOCAL_INDEX_FILE)
# Files the manifest lists, i.e. everything but the manifest and its signature.
CONTENT_FILES = REQUIRED_FILES + OPTIONAL_FILES
ALLOWED_FILES = frozenset(CONTENT_FILES + (MANIFEST_FILE, SIGNATURE_FILE))
PICKLE_SUFFIXES = (".joblib", ".pkl", ".pickle")
CBM_MAGIC = b"CBM1"

TRUSTED_KEYS_DIR = Path(__file__).resolve().with_name("trusted_keys")
SIGNING_KEY_ENV = "HPLDN_MODEL_SIGNING_KEY"
DEFAULT_SIGNING_KEY = Path("~/.config/hpldn/model-signing.key")
ALLOW_UNSIGNED_ENV = "HPLDN_ALLOW_UNSIGNED_MODEL"
ENVIRONMENT_ENV = "HPLDN_ENV"
SIGNATURE_SCHEME = "ed25519"

# Run as a module, not by file path, so it works from the host's sourceless
# install (deploy.sh compiles the package to .pyc and deletes every .py).
SANDBOX_MODULE = "hpldn.backend.models._cbm_sandbox"
SANDBOX_TIMEOUT_S = 900
SANDBOX_CPU_S = 600
# The child holds the model, its re-serialisation and CatBoost's own copy.
SANDBOX_MEMORY_HEADROOM = 4 << 30
SANDBOX_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2", "LC_ALL": "C"}

PROBE_ROWS = 64
PROBE_SEED = 20261004
# Categorical values the probe rows draw from when the bundle names none for a
# column; an unseen value still exercises the CTR priors.
PROBE_CATEGORIES = ("A", "B", "C", "D", "E", "F", "G", "L", "N", "O", "S", "T", "Y", "missing")
# Meta fields saved as pandas frames; every other field is plain JSON.
FRAME_FIELDS = ("postcode_stats", "sector_stats")
CANONICAL_ROUNDS = 6

log = logging.getLogger(__name__)


class BundleError(ValueError):
    """A bundle failed one of `verify`'s checks; `check` names which."""

    def __init__(self, check: str, message: str) -> None:
        super().__init__(f"{check}: {message}")
        self.check = check


# --------------------------------------------------------------------------
# Strict JSON and a small JSON-schema subset
# --------------------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError(f"non-standard JSON constant {name}")


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def loads_strict(data: bytes) -> Any:
    """Parse UTF-8 JSON, refusing NaN/Infinity and duplicate keys."""
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def dumps_canonical(value: Any) -> bytes:
    """
    Return `value` as strict JSON (no NaN), newline-terminated.

    Keys keep their order: a frame's columns and the medians are read back in
    the order they were saved.
    """
    return (json.dumps(value, indent=1, allow_nan=False) + "\n").encode("utf-8")


_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[name])


def validate(value: Any, schema: Mapping, where: str = "$") -> None:
    """
    Validate `value` against a JSON-schema subset, raising ValueError at the first mismatch.

    Supports ``type`` (a name or a list), ``enum``, ``properties``,
    ``required``, ``additionalProperties`` (bool or schema), ``items``,
    ``minItems``, ``maxItems``, ``pattern`` and ``minimum``.
    """
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_is_type(value, name) for name in names):
            raise ValueError(f"{where}: expected {' or '.join(names)}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                raise ValueError(f"{where}: missing required key {key!r}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{where}.{key}")
            elif extra is False:
                raise ValueError(f"{where}: unexpected key {key!r}")
            elif isinstance(extra, Mapping):
                validate(item, extra, f"{where}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{where}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                validate(item, schema["items"], f"{where}[{i}]")
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        raise ValueError(f"{where}: {value!r} does not match {schema['pattern']}")
    if "minimum" in schema and _is_type(value, "number") and value < schema["minimum"]:
        raise ValueError(f"{where}: {value} is below {schema['minimum']}")


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}
_SHA256 = {"type": "string", "pattern": "[0-9a-f]{64}"}
_COUNT = {"type": "integer", "minimum": 0}

SUMMARY_SCHEMA = {
    "type": "object",
    "required": [
        "tree_count", "oblivious", "depth", "leaf_counts", "feature_names", "cat_features",
        "ctr_types", "split_counts", "scale", "bias",
    ],
    "additionalProperties": False,
    "properties": {
        "tree_count": _COUNT,
        "oblivious": {"type": "boolean"},
        "depth": {"type": ["integer", "null"]},
        "leaf_counts": {"type": "array", "items": _COUNT},
        "feature_names": _STR_LIST,
        "cat_features": _STR_LIST,
        "ctr_types": _STR_LIST,
        "split_counts": {"type": "object", "additionalProperties": _COUNT},
        "scale": {"type": "number"},
        "bias": {"type": ["number", "array"], "items": {"type": "number"}},
    },
}

MANIFEST_SCHEMA = {
    "type": "object",
    "required": [
        "schema_version", "name", "created_at", "git_sha", "catboost_version", "training_args",
        "dataset", "files", "models", "catboost_metadata",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"enum": [SCHEMA_VERSION]},
        "name": {"type": "string", "pattern": r"[A-Za-z0-9._-]+"},
        "created_at": _STR,
        "git_sha": {"type": ["string", "null"]},
        "catboost_version": _STR,
        "training_args": {"type": "object"},
        "dataset": {
            "type": "object",
            "required": ["database", "alembic_revision", "row_count", "date_range"],
            "additionalProperties": False,
            "properties": {
                "database": {"type": ["string", "null"]},
                "alembic_revision": {"type": ["string", "null"]},
                "row_count": {"type": ["integer", "null"], "minimum": 0},
                "date_range": {"type": ["array", "null"], "items": _STR, "minItems": 2, "maxItems": 2},
            },
        },
        "files": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["sha256", "size"],
                "additionalProperties": False,
                "properties": {"sha256": _SHA256, "size": _COUNT},
            },
        },
        "models": {
            "type": "object",
            "required": [MODEL_FILE],
            "additionalProperties": False,
            "properties": {MODEL_FILE: SUMMARY_SCHEMA, EXPERT_FILE: SUMMARY_SCHEMA},
        },
        # CatBoost's own metadata map, moved out of the .cbm (see the module docstring).
        "catboost_metadata": {
            "type": "object",
            "additionalProperties": {"type": "object", "additionalProperties": _STR},
        },
    },
}

_FRAME_SCHEMA = {
    "type": "object",
    "required": ["index", "index_dtype", "dtypes", "records"],
    "additionalProperties": False,
    "properties": {
        "index": _STR,
        "index_dtype": _STR,
        "dtypes": {"type": "object", "additionalProperties": _STR},
        "records": {"type": "array", "items": {"type": "object"}},
    },
}
_NUMBER_OR_TAG = {"type": ["number", "object", "null"]}

META_SCHEMA = {
    "type": "object",
    "required": [
        "lot_medians", "postcode_stats", "sector_stats", "price_stat_globals", "feature_columns",
        "cat_features", "missing_flag_groups", "target",
    ],
    "additionalProperties": False,
    "properties": {
        "lot_medians": {"type": "object", "additionalProperties": _NUMBER_OR_TAG},
        "postcode_stats": _FRAME_SCHEMA,
        "sector_stats": _FRAME_SCHEMA,
        "price_stat_globals": {"type": "object"},
        "group_stats_scale": _STR,
        "feature_columns": _STR_LIST,
        "cat_features": _STR_LIST,
        "dropped_flags": _STR_LIST,
        "missing_flag_groups": {"type": "object", "additionalProperties": _STR_LIST},
        "nan_mode": _STR,
        "drop_features": _STR_LIST,
        "categorical_columns": _STR_LIST,
        "use_iod": {"type": "boolean"},
        "use_ofsted": {"type": "boolean"},
        "log_target": {"type": "boolean"},
        "target": _STR,
        "anchor_columns": {"type": ["array", "null"], "items": _STR},
        "smearing_factors": {"type": ["object", "null"], "additionalProperties": _NUMBER_OR_TAG},
        "high_value_expert": {"type": "boolean"},
        "high_value_threshold": {"type": ["number", "null"]},
        "population": {"type": ["string", "null"]},
        "train_period": {"type": ["array", "null"], "items": {"type": ["string", "null"]}},
        "database": {"type": ["string", "null"]},
    },
}

PROBE_SCHEMA = {
    "type": "object",
    "required": ["seed", "feature_names", "cat_features", "rows", "expected"],
    "additionalProperties": False,
    "properties": {
        "seed": {"type": "integer"},
        "feature_names": _STR_LIST,
        "cat_features": _STR_LIST,
        "rows": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "array", "items": {"type": ["number", "string", "null"]}},
        },
        "expected": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                MODEL_FILE: {"type": "array", "items": {"type": "number"}},
                EXPERT_FILE: {"type": "array", "items": {"type": "number"}},
            },
        },
    },
}

JSON_SCHEMAS = {
    META_FILE: META_SCHEMA,
    PROBE_FILE: PROBE_SCHEMA,
    CALIBRATION_FILE: {"type": "object"},
    PROJECTION_FILE: {"type": "object", "required": ["method", "types"]},
    LOCAL_INDEX_FILE: {"type": "object", "required": ["method", "series"]},
}


# --------------------------------------------------------------------------
# Keys and signatures
# --------------------------------------------------------------------------


def key_id(public_key) -> str:
    """Return a public key's id: the first 16 hex digits of the sha256 of its raw bytes."""
    from cryptography.hazmat.primitives import serialization

    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def default_signing_key() -> Path:
    """Return the signing key path: ``HPLDN_MODEL_SIGNING_KEY`` or ``~/.config/hpldn/model-signing.key``."""
    return Path(os.environ.get(SIGNING_KEY_ENV) or DEFAULT_SIGNING_KEY).expanduser()


def keygen(key_path: Optional[Path] = None, trusted_dir: Optional[Path] = None, force: bool = False) -> str:
    """
    Write a new Ed25519 signing key (0600) and trust its public key; return its id.

    The public key goes to `trusted_dir` (default the tracked ``trusted_keys/``)
    as ``<key id>.pem``: commit it, so every host that installs the package
    trusts bundles signed with it.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_path = Path(key_path or default_signing_key()).expanduser()
    trusted_dir = Path(trusted_dir or TRUSTED_KEYS_DIR)
    if key_path.exists() and not force:
        raise FileExistsError(f"{key_path} already exists; pass --force to replace it")
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    tmp = key_path.with_name(key_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    os.replace(tmp, key_path)
    kid = key_id(private.public_key())
    trusted_dir.mkdir(parents=True, exist_ok=True)
    (trusted_dir / f"{kid}.pem").write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return kid


def load_signing_key(key_path: Optional[Path] = None):
    """
    Return the Ed25519 private key at `key_path`.

    Raises
    ------
    FileNotFoundError
        When there is no key (run ``keygen``).
    PermissionError
        When the file is readable by anyone but its owner.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_path = Path(key_path or default_signing_key()).expanduser()
    if not key_path.is_file():
        raise FileNotFoundError(
            f"no model signing key at {key_path}; create one with python -m hpldn.backend.models.artifact keygen"
        )
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"{key_path} has mode {mode:o}; a signing key must be 0600")
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{key_path} is not an Ed25519 key")
    return key


def trusted_keys(trusted_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return ``{key id: public key}`` for every ``<key id>.pem`` in `trusted_dir` (default ``trusted_keys/``)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    trusted_dir = Path(trusted_dir or TRUSTED_KEYS_DIR)
    keys = {}
    for path in sorted(trusted_dir.glob("*.pem")) if trusted_dir.is_dir() else []:
        key = serialization.load_pem_public_key(path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise BundleError("signature", f"trusted key {path} is not an Ed25519 public key")
        kid = key_id(key)
        if path.stem != kid:
            raise BundleError("signature", f"trusted key {path} holds key {kid}; rename it {kid}.pem")
        keys[kid] = key
    return keys


def sign(bundle: Path, key_path: Optional[Path] = None) -> str:
    """Sign `bundle`'s ``manifest.json`` with the key at `key_path`; return the key id."""
    bundle = Path(bundle)
    private = load_signing_key(key_path)
    data = (bundle / MANIFEST_FILE).read_bytes()
    kid = key_id(private.public_key())
    signature = base64.b64encode(private.sign(data)).decode("ascii")
    _write_atomic(bundle / SIGNATURE_FILE, f"{SIGNATURE_SCHEME} {kid} {signature}\n".encode("ascii"))
    return kid


def _check_signature(bundle: Path, manifest_bytes: bytes, trusted_dir: Optional[Path]) -> str:
    """Return the id of the trusted key `manifest_bytes` is signed with, or raise BundleError."""
    from cryptography.exceptions import InvalidSignature

    sig_path = bundle / SIGNATURE_FILE
    if not sig_path.is_file() or sig_path.is_symlink():
        raise BundleError("signature", f"{bundle} has no {SIGNATURE_FILE}")
    parts = sig_path.read_bytes().decode("ascii", errors="replace").split()
    if len(parts) != 3 or parts[0] != SIGNATURE_SCHEME:
        raise BundleError("signature", f"{sig_path} is not '{SIGNATURE_SCHEME} <key id> <signature>'")
    _, kid, encoded = parts
    keys = trusted_keys(trusted_dir)
    if kid not in keys:
        raise BundleError("signature", f"{bundle} is signed with key {kid}, which is not in {trusted_dir or TRUSTED_KEYS_DIR}")
    try:
        keys[kid].verify(base64.b64decode(encoded, validate=True), manifest_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise BundleError("signature", f"{sig_path} does not match {MANIFEST_FILE} for key {kid}") from exc
    return kid


def unsigned_allowed() -> bool:
    """
    Return whether ``HPLDN_ALLOW_UNSIGNED_MODEL=1`` lets an unsigned bundle load.

    Raises
    ------
    BundleError
        When it is set with ``HPLDN_ENV=production``.
    """
    if os.environ.get(ALLOW_UNSIGNED_ENV) != "1":
        return False
    if os.environ.get(ENVIRONMENT_ENV, "").strip().lower() == "production":
        raise BundleError("signature", f"{ALLOW_UNSIGNED_ENV}=1 is refused when {ENVIRONMENT_ENV}=production")
    return True


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file, streamed."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def is_bundle(path) -> bool:
    """Return whether `path` is a bundle directory (it holds a ``manifest.json``)."""
    path = Path(path)
    return path.is_dir() and (path / MANIFEST_FILE).is_file()


def _scan(bundle: Path) -> List[str]:
    """
    Return the bundle's file names, refusing anything but allowlisted regular files.

    A pickle is named as such, before the generic allowlist message, wherever
    it sits: the API must never be one ``joblib.load`` away from code.
    """
    if not bundle.is_dir() or bundle.is_symlink():
        raise BundleError("files", f"{bundle} is not a bundle directory")
    names = []
    for entry in os.scandir(bundle):
        lowered = entry.name.lower()
        if lowered.endswith(PICKLE_SUFFIXES):
            raise BundleError("files", f"{bundle} holds a pickle ({entry.name}); bundles carry data only")
        if entry.is_dir(follow_symlinks=False):
            for _root, _dirs, files in os.walk(entry.path):
                if any(f.lower().endswith(PICKLE_SUFFIXES) for f in files):
                    raise BundleError("files", f"{entry.path} holds a pickle; bundles carry data only")
            raise BundleError("files", f"unexpected directory {entry.name} in {bundle}")
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise BundleError("files", f"{entry.name} in {bundle} is not a regular file")
        if entry.name not in ALLOWED_FILES:
            raise BundleError("files", f"unexpected file {entry.name} in {bundle}")
        names.append(entry.name)
    return sorted(names)


# --------------------------------------------------------------------------
# meta.json
# --------------------------------------------------------------------------


def _float_tag(value: float):
    """Return a finite float as itself and NaN/inf as a ``{"$float": ...}`` tag strict JSON can hold."""
    if math.isfinite(value):
        return value
    return {"$float": "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")}


def jsonable(value: Any, where: str = "$") -> Any:
    """
    Return `value` as plain JSON values, or raise TypeError naming what cannot be.

    numpy scalars become Python numbers, tuples lists, and non-finite floats
    ``{"$float": "nan"}`` tags. Anything else is refused rather than
    stringified, so nothing reaches a bundle that would not read back as itself.
    """
    import numpy as np

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return _float_tag(float(value))
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{where}: key {key!r} is not a string")
            out[key] = jsonable(item, f"{where}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [jsonable(item, f"{where}[{i}]") for i, item in enumerate(value)]
    raise TypeError(f"{where}: {type(value).__name__} cannot be stored as JSON")


def untag(value: Any) -> Any:
    """Invert `jsonable`'s float tags: what a reader of a bundle JSON file is handed."""
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return float(value["$float"])
        return {key: untag(item) for key, item in value.items()}
    if isinstance(value, list):
        return [untag(item) for item in value]
    return value


def frame_to_json(frame) -> Dict[str, Any]:
    """Return a one-index stats frame as records plus the dtypes that restore it exactly."""
    index = frame.index.name
    if index is None:
        raise TypeError("frame_to_json: the frame's index has no name")
    records = []
    for key, row in zip(frame.index, frame.itertuples(index=False, name=None)):
        record = {index: jsonable(key)}
        record.update({col: jsonable(value) for col, value in zip(frame.columns, row)})
        records.append(record)
    return {
        "index": str(index),
        "index_dtype": str(frame.index.dtype),
        "dtypes": {str(col): str(dtype) for col, dtype in frame.dtypes.items()},
        "records": records,
    }


def frame_from_json(payload: Mapping[str, Any]):
    """Rebuild a `frame_to_json` frame with its index name and dtypes."""
    import pandas as pd

    index = payload["index"]
    columns = list(payload["dtypes"])
    records = [untag(record) for record in payload["records"]]
    frame = pd.DataFrame.from_records(records, columns=[index, *columns])
    frame = frame.set_index(index)
    frame.index = frame.index.astype(payload["index_dtype"])
    for col, dtype in payload["dtypes"].items():
        frame[col] = frame[col].astype(dtype)
    return frame


def meta_to_json(meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Return `hpp_cb`'s preprocessing state as ``meta.json`` content (the loaded expert is dropped)."""
    out = {}
    for key, value in meta.items():
        if key == "high_value_model":
            continue
        out[key] = frame_to_json(value) if key in FRAME_FIELDS else jsonable(value, f"meta.{key}")
    validate(out, META_SCHEMA, "meta.json")
    return out


def meta_from_json(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the preprocessing state `hpp_cb.prepare_features_from_saved_model` reads."""
    return {key: frame_from_json(value) if key in FRAME_FIELDS else untag(value) for key, value in payload.items()}


def assert_meta_equal(original: Mapping[str, Any], restored: Mapping[str, Any]) -> None:
    """Raise AssertionError unless `restored` holds exactly `original`'s values and dtypes."""
    import pandas as pd

    keys = set(original) - {"high_value_model"}
    if keys != set(restored):
        raise AssertionError(f"meta keys differ: {sorted(keys ^ set(restored))}")
    for key in sorted(keys):
        a, b = original[key], restored[key]
        if key in FRAME_FIELDS:
            pd.testing.assert_frame_equal(a, b, check_exact=True)
        elif jsonable(a) != jsonable(b) or type(jsonable(a)) is not type(jsonable(b)):
            raise AssertionError(f"meta.{key} differs after the JSON round-trip: {a!r} != {b!r}")


# --------------------------------------------------------------------------
# CatBoost: canonical form, summary, probe
# --------------------------------------------------------------------------


def _sandbox_module():
    """Import `_cbm_sandbox` for the trusted parent (the child runs it with ``python -I -m``)."""
    from hpldn.backend.models import _cbm_sandbox

    return _cbm_sandbox


def canonical_cbm(model) -> tuple[bytes, Dict[str, str]]:
    """
    Return `model` as canonical ``.cbm`` bytes and the metadata map moved out of it.

    Raises
    ------
    BundleError
        When CatBoost does not reach a fixed point within `CANONICAL_ROUNDS` saves.
    """
    from catboost import CatBoostRegressor

    blob = bytes(model._object._serialize_model())
    current = CatBoostRegressor()
    current.load_model(blob=blob)
    metadata = current.get_metadata()
    moved = {str(key): str(metadata[key]) for key in metadata.keys()}
    for key in list(metadata.keys()):
        del metadata[key]
    blob = bytes(current._object._serialize_model())
    for _ in range(CANONICAL_ROUNDS):
        current = CatBoostRegressor()
        current.load_model(blob=blob)
        again = bytes(current._object._serialize_model())
        if again == blob:
            return blob, moved
        blob = again
    raise BundleError("canonical", f"CatBoost did not reach a byte-stable form in {CANONICAL_ROUNDS} saves")


def make_probe(model, meta: Mapping[str, Any], rows: int = PROBE_ROWS, seed: int = PROBE_SEED) -> Dict[str, Any]:
    """
    Return `rows` deterministic feature rows that exercise `model`'s splits.

    Numeric values are drawn from each feature's own borders (either side of
    the outermost, the midpoints between, and NaN) and stored as float32 values,
    which is what CatBoost reads. Categorical values come from the bundle's
    postcode lookups where the column is one of those, else `PROBE_CATEGORIES`.
    """
    import numpy as np

    names = [str(n) for n in model.feature_names_]
    cat_indices = set(int(i) for i in model.get_cat_feature_indices())
    borders = model.get_borders()
    vocab = {
        "postcode_district": [str(v) for v in meta["postcode_stats"].index[:200]] if "postcode_stats" in meta else [],
        "postcode_sector": [str(v) for v in meta["sector_stats"].index[:200]] if "sector_stats" in meta else [],
    }
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(rows):
        row = []
        for i, name in enumerate(names):
            if i in cat_indices:
                pool = vocab.get(name) or PROBE_CATEGORIES
                row.append(str(pool[int(rng.integers(len(pool)))]))
                continue
            edges = sorted(float(b) for b in borders.get(i, []))
            if edges:
                candidates = [math.nan, edges[0] - 1.0, edges[-1] + 1.0]
                candidates += [(a + b) / 2.0 for a, b in zip(edges, edges[1:])]
            else:
                candidates = [0.0]
            value = candidates[int(rng.integers(len(candidates)))]
            row.append(None if math.isnan(value) else float(np.float32(value)))
        out.append(row)
    return {
        "seed": seed,
        "feature_names": names,
        "cat_features": [names[i] for i in sorted(cat_indices)],
        "rows": out,
        "expected": {},
    }


def probe_predictions(model, probe: Mapping[str, Any]) -> List[float]:
    """Return `model`'s raw predictions for the probe rows, as `_cbm_sandbox` computes them."""
    sandbox = _sandbox_module()
    pool = sandbox.probe_pool(probe, model.feature_names_, model.get_cat_feature_indices())
    return sandbox.predict_raw(model, pool)


def summarise(model, probe: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the manifest's summary of `model`, as `_cbm_sandbox` computes it."""
    sandbox = _sandbox_module()
    pool = sandbox.probe_pool(probe, model.feature_names_, model.get_cat_feature_indices())
    return sandbox.summarise(model, pool)


@lru_cache(maxsize=1)
def network_isolation() -> tuple[tuple[str, ...], str]:
    """
    Return the command prefix that gives the sandbox no network, and what it does.

    ``unshare -rn`` (a user and network namespace) where the kernel lets this
    user make one; ``unshare -n`` as root. Otherwise (Ubuntu's AppArmor
    restricts unprivileged user namespaces) the child blocks its own socket
    constructors, which is all a parse of untrusted bytes can reach.
    """
    unshare = shutil.which("unshare")
    candidates = []
    if unshare:
        if os.geteuid() == 0:
            candidates.append(((unshare, "-n"), "network namespace (unshare -n)"))
        candidates.append(((unshare, "-rn"), "user + network namespace (unshare -rn)"))
    for prefix, label in candidates:
        try:
            done = subprocess.run([*prefix, "true"], capture_output=True, timeout=10, env={})
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return prefix, label
    return (), "socket constructors disabled in the child (this host cannot make a network namespace)"


def _limit_child(memory_bytes: int) -> None:
    """Resource limits for the sandbox child, set between fork and exec."""
    resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_CPU_S, SANDBOX_CPU_S))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def run_sandbox(bundle: Path, names: Iterable[str]) -> Dict[str, Any]:
    """
    Run `_cbm_sandbox` over `names` in `bundle` and return its report.

    The child is ``python -I`` with an empty environment (bar fixed thread
    caps), its working directory ``/``, stdin closed, no file it may write
    (``RLIMIT_FSIZE`` 0), CPU, address-space and file-descriptor limits, and
    `network_isolation`.

    Raises
    ------
    BundleError
        When the child fails, times out or prints anything but one JSON report.
    """
    bundle = Path(bundle).resolve()
    names = list(names)
    total = sum((bundle / name).stat().st_size for name in names)
    memory = 4 * total + SANDBOX_MEMORY_HEADROOM
    prefix, _ = network_isolation()
    command = [*prefix, sys.executable, "-I", "-m", SANDBOX_MODULE, str(bundle), *names]
    try:
        done = subprocess.run(
            command,
            env=dict(SANDBOX_ENV),
            cwd="/",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=SANDBOX_TIMEOUT_S,
            preexec_fn=lambda: _limit_child(memory),
            close_fds=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise BundleError("cbm", f"the model sandbox timed out after {SANDBOX_TIMEOUT_S} s") from exc
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
        raise BundleError("cbm", f"CatBoost could not parse the bundle's models (exit {done.returncode}): {' | '.join(tail)}")
    try:
        return loads_strict(done.stdout)
    except ValueError as exc:
        raise BundleError("cbm", f"the model sandbox printed something other than its report: {exc}") from exc


# --------------------------------------------------------------------------
# Writing bundles
# --------------------------------------------------------------------------


def git_sha() -> Optional[str]:
    """Return HEAD of the checkout this module runs from (``-dirty`` with tracked changes), or None."""
    root = Path(__file__).resolve().parents[4]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{sha}-dirty" if dirty else sha


def jsonable_args(args: Any) -> Dict[str, Any]:
    """Return an argparse namespace's JSON-able values; Paths become strings, objects are named, not stored."""
    values = vars(args) if not isinstance(args, Mapping) else dict(args)
    out, omitted = {}, []
    for key, value in sorted(values.items()):
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, (list, tuple)):
            value = [str(v) if isinstance(v, Path) else v for v in value]
        try:
            out[key] = jsonable(value, key)
        except TypeError:
            omitted.append(key)
    if omitted:
        out["_not_recorded"] = omitted
    return out


def alembic_revision(engine) -> Optional[str]:
    """Return the database's alembic revision, or None when it cannot be read."""
    from sqlalchemy import text

    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 -- a fingerprint field, never worth failing a save
        return None


def dataset_fingerprint(
    database: Optional[str] = None,
    alembic: Optional[str] = None,
    row_count: Optional[int] = None,
    date_range: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return the manifest's ``dataset`` block."""
    return {
        "database": database,
        "alembic_revision": alembic,
        "row_count": None if row_count is None else int(row_count),
        "date_range": None if date_range is None else [str(d) for d in date_range],
    }


def _manifest_files(bundle: Path) -> Dict[str, Dict[str, Any]]:
    return {
        name: {"sha256": sha256_file(bundle / name), "size": (bundle / name).stat().st_size}
        for name in _scan(bundle)
        if name in CONTENT_FILES
    }


def _sign_or_warn(bundle: Path, key_path: Optional[Path], logger: Optional[logging.Logger]) -> Optional[str]:
    """Sign `bundle` when a signing key exists; otherwise say how, and leave it unsigned."""
    key_path = Path(key_path or default_signing_key()).expanduser()
    if not key_path.is_file():
        (bundle / SIGNATURE_FILE).unlink(missing_ok=True)
        (logger or log).warning(
            f"{bundle} is UNSIGNED (no key at {key_path}); the API refuses it unless {ALLOW_UNSIGNED_ENV}=1. "
            f"Sign it with python -m hpldn.backend.models.artifact sign {bundle}"
        )
        return None
    kid = sign(bundle, key_path)
    (logger or log).info(f"Signed {bundle} with key {kid}")
    return kid


def write_bundle(
    bundle: Path,
    model,
    meta: Mapping[str, Any],
    expert=None,
    json_files: Optional[Mapping[str, Any]] = None,
    training_args: Optional[Mapping[str, Any]] = None,
    dataset: Optional[Mapping[str, Any]] = None,
    key_path: Optional[Path] = None,
    sign_bundle: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """
    Write `model` (and `expert`) with its preprocessing state as a bundle at `bundle`.

    Built in a sibling ``.<name>.partial`` directory, checked with the full
    `verify` (sandbox included) bar the signature, then moved into place,
    replacing an older bundle of the same name. `json_files` adds
    ``calibration.json`` / ``projection.json`` / ``local_index.json``.
    Signed with the key at `key_path` (default `default_signing_key`) when it
    exists; otherwise left unsigned with a warning.

    Raises
    ------
    BundleError
        When `bundle` is something other than a bundle, or the written bundle
        fails verification (the partial directory is removed).
    """
    import catboost

    bundle = Path(bundle)
    name = bundle.name
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise BundleError("files", f"bundle name {name!r} must be letters, digits, '.', '_' or '-'")
    if bundle.exists() and not is_bundle(bundle) and (bundle.is_file() or any(bundle.iterdir())):
        raise BundleError("files", f"{bundle} exists and is not a model bundle; refusing to replace it")
    partial = bundle.with_name(f".{name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        models = {MODEL_FILE: model, **({EXPERT_FILE: expert} if expert is not None else {})}
        metadata = {}
        for file_name, fitted in models.items():
            blob, metadata[file_name] = canonical_cbm(fitted)
            (partial / file_name).write_bytes(blob)
        meta_json = meta_to_json(meta)
        assert_meta_equal(meta, meta_from_json(meta_json))
        (partial / META_FILE).write_bytes(dumps_canonical(meta_json))
        for file_name, payload in (json_files or {}).items():
            if file_name not in (CALIBRATION_FILE, PROJECTION_FILE, LOCAL_INDEX_FILE):
                raise BundleError("files", f"{file_name} is not a bundle JSON file")
            if payload is not None:
                payload = jsonable(payload, file_name)
                validate(payload, JSON_SCHEMAS[file_name], file_name)
                (partial / file_name).write_bytes(dumps_canonical(payload))

        probe = make_probe(model, meta)
        (partial / PROBE_FILE).write_bytes(dumps_canonical(probe))
        report = run_sandbox(partial, models)
        summaries = {}
        for file_name, fitted in models.items():
            child = report[file_name]
            if child["predictions"] != probe_predictions(fitted, probe):
                raise BundleError("probe", f"{file_name}: the canonical model predicts differently from the trained one")
            if child["roundtrip_sha256"] != child["sha256"]:
                raise BundleError("cbm", f"{file_name} did not read back as its own bytes")
            probe["expected"][file_name] = child["predictions"]
            summaries[file_name] = child["summary"]
        (partial / PROBE_FILE).write_bytes(dumps_canonical(probe))

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "name": name,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "git_sha": git_sha(),
            "catboost_version": catboost.__version__,
            "training_args": jsonable(dict(training_args or {})),
            "dataset": dict(dataset or dataset_fingerprint()),
            "files": _manifest_files(partial),
            "models": summaries,
            "catboost_metadata": metadata,
        }
        validate(manifest, MANIFEST_SCHEMA, MANIFEST_FILE)
        (partial / MANIFEST_FILE).write_bytes(dumps_canonical(manifest))
        verify(partial, allow_unsigned=True, deep=False, warn_unsigned=False)
        if sign_bundle:
            _sign_or_warn(partial, key_path, logger)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    if bundle.exists():
        old = bundle.with_name(f".{name}.old")
        shutil.rmtree(old, ignore_errors=True)
        os.replace(bundle, old)
        os.replace(partial, bundle)
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(partial, bundle)
    (logger or log).info(f"Wrote model bundle {bundle}")
    return bundle


def update_json_files(
    bundle: Path,
    files: Mapping[str, Any],
    key_path: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Write calibration / projection / local index JSON into `bundle`, then re-hash and re-sign it.

    The bundle is verified first (unsigned allowed only as `unsigned_allowed`
    says), so a re-signature never blesses files nobody checked.
    """
    bundle = Path(bundle)
    verify(bundle, allow_unsigned=unsigned_allowed(), deep=False)
    for file_name, payload in files.items():
        if file_name not in (CALIBRATION_FILE, PROJECTION_FILE, LOCAL_INDEX_FILE):
            raise BundleError("files", f"{file_name} cannot be updated in place; rebuild the bundle")
        payload = jsonable(payload, file_name)
        validate(payload, JSON_SCHEMAS[file_name], file_name)
        _write_atomic(bundle / file_name, dumps_canonical(payload))
    manifest = loads_strict((bundle / MANIFEST_FILE).read_bytes())
    manifest["files"] = _manifest_files(bundle)
    validate(manifest, MANIFEST_SCHEMA, MANIFEST_FILE)
    _write_atomic(bundle / MANIFEST_FILE, dumps_canonical(manifest))
    _sign_or_warn(bundle, key_path, logger)


# --------------------------------------------------------------------------
# Verifying and loading
# --------------------------------------------------------------------------


@dataclass
class VerifiedBundle:
    """What `verify` checked, and the bundle's parsed JSON (read once, from the hashed bytes)."""

    path: Path
    manifest: Dict[str, Any]
    key_id: Optional[str]
    json: Dict[str, Any] = field(default_factory=dict)
    checks: List[str] = field(default_factory=list)
    sandbox: Optional[str] = None

    def payload(self, name: str) -> Optional[Any]:
        """Return a verified JSON file's content as its writer saved it (NaN restored), or None without it."""
        value = self.json.get(name)
        return None if value is None else untag(value)

    def model_files(self) -> List[str]:
        """Return the ``.cbm`` files the manifest lists, the main model first."""
        return [name for name in MODEL_FILES if name in self.manifest["files"]]


def _read_manifest(bundle: Path, trusted_dir: Optional[Path], allow_unsigned: bool, logger, warn: bool = True) -> tuple:
    manifest_path = bundle / MANIFEST_FILE
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BundleError("files", f"{bundle} has no {MANIFEST_FILE}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        kid = _check_signature(bundle, manifest_bytes, trusted_dir)
    except BundleError:
        if not allow_unsigned:
            raise
        kid = None
        if warn:
            (logger or log).warning(f"{bundle}: no trusted signature; loading it UNSIGNED ({ALLOW_UNSIGNED_ENV}=1)")
    try:
        manifest = loads_strict(manifest_bytes)
        validate(manifest, MANIFEST_SCHEMA, MANIFEST_FILE)
    except ValueError as exc:
        raise BundleError("schema", str(exc)) from exc
    return manifest, kid


def _check_files(bundle: Path, manifest: Mapping[str, Any]) -> Dict[str, bytes]:
    """Check the allowlist and every file's size and sha256; return the JSON files' verified bytes."""
    present = set(_scan(bundle)) - {MANIFEST_FILE, SIGNATURE_FILE}
    listed = set(manifest["files"])
    if present != listed:
        raise BundleError(
            "files",
            f"files on disk and in the manifest differ: unlisted {sorted(present - listed)}, missing {sorted(listed - present)}",
        )
    missing = [name for name in REQUIRED_FILES if name not in listed]
    if missing:
        raise BundleError("files", f"{bundle} lacks {missing}")
    if (EXPERT_FILE in listed) != (EXPERT_FILE in manifest["models"]):
        raise BundleError("files", f"{EXPERT_FILE} and the manifest's model summaries disagree")
    texts = {}
    for name in sorted(listed):
        expected = manifest["files"][name]
        path = bundle / name
        if path.stat().st_size != expected["size"]:
            raise BundleError("files", f"{name} is {path.stat().st_size} bytes, the manifest says {expected['size']}")
        if name.endswith(".json"):
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            texts[name] = data
        else:
            digest = sha256_file(path)
        if digest != expected["sha256"]:
            raise BundleError("files", f"{name} does not match its sha256 in the manifest")
    return texts


def _check_json(texts: Mapping[str, bytes]) -> Dict[str, Any]:
    parsed = {}
    for name, data in texts.items():
        try:
            parsed[name] = loads_strict(data)
            validate(parsed[name], JSON_SCHEMAS[name], name)
        except ValueError as exc:
            raise BundleError("schema", f"{name}: {exc}") from exc
    probe = parsed[PROBE_FILE]
    meta = parsed[META_FILE]
    if probe["feature_names"] != meta["feature_columns"]:
        raise BundleError("schema", f"{PROBE_FILE} and {META_FILE} name different feature columns")
    width = len(probe["feature_names"])
    if any(len(row) != width for row in probe["rows"]):
        raise BundleError("schema", f"{PROBE_FILE} has a row that is not {width} values wide")
    return parsed


def _check_model(name: str, summary: Mapping[str, Any], predictions: List[float], verified: VerifiedBundle) -> None:
    """Steps 5 and 6 for one model: its summary and probe predictions."""
    if dict(summary) != verified.manifest["models"][name]:
        raise BundleError("summary", f"{name} does not match the manifest's summary of it")
    meta = verified.json[META_FILE]
    if name == MODEL_FILE and (
        summary["feature_names"] != meta["feature_columns"]
        or sorted(summary["cat_features"]) != sorted(meta["cat_features"])
    ):
        raise BundleError("summary", f"{name}'s features differ from {META_FILE}'s")
    expected = verified.json[PROBE_FILE]["expected"].get(name)
    if expected is None or len(expected) != len(predictions) or any(a != b for a, b in zip(expected, predictions)):
        raise BundleError("probe", f"{name} does not predict the probe rows as {PROBE_FILE} records")


def verify(
    bundle,
    trusted_dir: Optional[Path] = None,
    allow_unsigned: bool = False,
    deep: bool = True,
    logger: Optional[logging.Logger] = None,
    warn_unsigned: bool = True,
) -> VerifiedBundle:
    """
    Run the checks of the module docstring over `bundle` and return what was verified.

    Without `deep`, step 4 checks only each model's magic and steps 5-6 are
    left to `check_loaded_model`, run on the model the caller loads.

    Raises
    ------
    BundleError
        At the first check that fails; ``.check`` names it.
    """
    bundle = Path(bundle)
    manifest, kid = _read_manifest(bundle, trusted_dir, allow_unsigned, logger, warn_unsigned)
    verified = VerifiedBundle(path=bundle, manifest=manifest, key_id=kid)
    verified.checks.append("signature" if kid else "signature (skipped: unsigned allowed)")
    texts = _check_files(bundle, manifest)
    verified.checks.append("files")
    verified.json = _check_json(texts)
    verified.checks.append("schema")
    for name in verified.model_files():
        with (bundle / name).open("rb") as handle:
            if handle.read(len(CBM_MAGIC)) != CBM_MAGIC:
                raise BundleError("cbm", f"{name} is not a CatBoost model (no {CBM_MAGIC!r} magic)")
    if not deep:
        verified.checks.append("cbm magic")
        return verified
    report = run_sandbox(bundle, verified.model_files())
    verified.sandbox = network_isolation()[1]
    for name in verified.model_files():
        child = report.get(name) or {}
        expected = manifest["files"][name]
        if not child.get("magic"):
            raise BundleError("cbm", f"{name} is not a CatBoost model")
        if child["sha256"] != expected["sha256"]:
            raise BundleError("cbm", f"{name} changed while it was being verified")
        if child["roundtrip_sha256"] != child["sha256"] or child["roundtrip_size"] != child["size"]:
            raise BundleError(
                "cbm",
                f"{name} does not read back as its own bytes ({child['size']} bytes in, {child['roundtrip_size']} "
                "out): it holds data CatBoost's schema does not parse",
            )
    verified.checks.append("cbm round-trip")
    for name in verified.model_files():
        child = report[name]
        _check_model(name, child["summary"], child["predictions"], verified)
    verified.checks += ["summary", "probe"]
    return verified


def check_loaded_model(model, name: str, verified: VerifiedBundle) -> None:
    """Steps 5 and 6 on a model the caller loaded from a `verify(deep=False)` bundle."""
    probe = verified.json[PROBE_FILE]
    _check_model(name, summarise(model, probe), probe_predictions(model, probe), verified)


def load_bundle(bundle, logger: Optional[logging.Logger] = None) -> tuple:
    """
    Verify and load a bundle for scoring: ``(model, meta, verified)``.

    `meta` is `hpp_cb.load_model`'s dict, with the expert loaded under
    ``high_value_model`` (None without one). Unsigned bundles load only as
    `unsigned_allowed` says.

    Raises
    ------
    BundleError
        When any check fails.
    """
    from catboost import CatBoostRegressor

    bundle = Path(bundle)
    verified = verify(bundle, allow_unsigned=unsigned_allowed(), deep=False, logger=logger)
    loaded = {}
    for name in verified.model_files():
        model = CatBoostRegressor()
        model.load_model(str(bundle / name))
        check_loaded_model(model, name, verified)
        loaded[name] = model
    verified.checks += ["summary", "probe"]
    meta = meta_from_json(verified.json[META_FILE])
    if bool(meta.get("high_value_expert")) != (EXPERT_FILE in loaded):
        raise BundleError("files", f"{META_FILE} and the bundle disagree on whether there is an {EXPERT_FILE}")
    meta["high_value_model"] = loaded.get(EXPERT_FILE)
    return loaded[MODEL_FILE], meta, verified


def read_verified_json(bundle, name: str, logger: Optional[logging.Logger] = None) -> Optional[Any]:
    """
    Return one JSON file of a bundle after checking the signature and that file's hash, or None without it.

    For readers that need a sidecar but not the model (the area price index's projection).
    """
    bundle = Path(bundle)
    manifest, _ = _read_manifest(bundle, None, unsigned_allowed(), logger)
    entry = manifest["files"].get(name)
    if entry is None:
        return None
    path = bundle / name
    if path.is_symlink():
        raise BundleError("files", f"{name} in {bundle} is not a regular file")
    data = path.read_bytes()
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise BundleError("files", f"{name} does not match its sha256 in the manifest")
    payload = loads_strict(data)
    validate(payload, JSON_SCHEMAS[name], name)
    return untag(payload)


# --------------------------------------------------------------------------
# convert and export-json
# --------------------------------------------------------------------------


def _load_legacy(model_fp: Path):
    """
    Load a pre-bundle ``<model>.cbm`` and its ``.meta.joblib`` pickle: TRUSTED input only.

    Unpickling runs whatever code the file names, so this is for converting a
    model this project trained, once, on the machine that trained it.
    """
    import joblib
    from catboost import CatBoostRegressor

    meta_path = Path(f"{model_fp}.meta.joblib")
    if not model_fp.is_file() or not meta_path.is_file():
        raise FileNotFoundError(f"convert needs {model_fp} and {meta_path}")
    model = CatBoostRegressor()
    model.load_model(str(model_fp))
    meta = joblib.load(meta_path)  # noqa: S301 -- the one trusted pickle load, see the docstring
    expert = None
    if meta.get("high_value_expert"):
        expert = CatBoostRegressor()
        expert.load_model(f"{model_fp}.expert.cbm")
    json_files = {}
    for file_name, suffix in ((CALIBRATION_FILE, ".calibration.json"), (PROJECTION_FILE, ".projection.json"),
                              (LOCAL_INDEX_FILE, ".local_index.json")):
        path = Path(f"{model_fp}{suffix}")
        if path.is_file():
            json_files[file_name] = loads_strict(path.read_bytes())
    return model, meta, expert, json_files


def convert(model_fp: Path, bundle: Path, key_path: Optional[Path] = None, logger=None) -> Path:
    """Convert a pre-bundle model (trusted) into a signed bundle; see `_load_legacy`."""
    model_fp = Path(model_fp)
    model, meta, expert, json_files = _load_legacy(model_fp)
    meta.pop("high_value_model", None)
    dataset = dataset_fingerprint(database=meta.get("database"), date_range=meta.get("train_period"))
    if meta.get("database"):
        try:
            from sqlalchemy import create_engine

            from hpldn.backend.common.db import get_model_db_url

            dataset["alembic_revision"] = alembic_revision(create_engine(get_model_db_url()))
        except Exception:  # noqa: BLE001 -- best effort; the field may stay null
            pass
    training_args = {
        "converted_from": model_fp.name,
        "converted_from_sha256": {
            path.name: sha256_file(path)
            for path in sorted(model_fp.parent.glob(f"{model_fp.name}*"))
            if path.is_file()
        },
    }
    return write_bundle(
        bundle, model, meta, expert=expert, json_files=json_files, training_args=training_args,
        dataset=dataset, key_path=key_path, logger=logger,
    )


def export_json(bundle: Path, out_dir: Path) -> List[Path]:
    """Export each verified model of `bundle` as CatBoost JSON in `out_dir`, for reading; never shipped."""
    from catboost import CatBoostRegressor

    bundle = Path(bundle)
    verified = verify(bundle, allow_unsigned=unsigned_allowed(), deep=True)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in verified.model_files():
        model = CatBoostRegressor()
        model.load_model(str(bundle / name))
        target = out_dir / f"{bundle.name}.{Path(name).stem}.json"
        model.save_model(str(target), format="json")
        written.append(target)
    return written


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(prog="python -m hpldn.backend.models.artifact", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    kg = sub.add_parser("keygen", help="Create the Ed25519 signing key and trust its public key")
    kg.add_argument("--key", type=Path, default=None, help=f"default ${SIGNING_KEY_ENV} or {DEFAULT_SIGNING_KEY}")
    kg.add_argument("--trusted-dir", type=Path, default=None, help="default the tracked trusted_keys/")
    kg.add_argument("--force", action="store_true", help="replace an existing key")
    sg = sub.add_parser("sign", help="Re-hash nothing; sign a bundle's manifest.json")
    sg.add_argument("bundle", type=Path)
    sg.add_argument("--key", type=Path, default=None)
    vf = sub.add_parser("verify", help="Run every check over a bundle")
    vf.add_argument("bundle", type=Path)
    vf.add_argument("--allow-unsigned", action="store_true", help="report a missing signature instead of failing")
    cv = sub.add_parser("convert", help="One-time trusted conversion of <model>.cbm + .meta.joblib into a bundle")
    cv.add_argument("model", type=Path, help="the legacy .cbm (its .meta.joblib and JSON sidecars beside it)")
    cv.add_argument("bundle", type=Path, help="the bundle directory to write")
    cv.add_argument("--key", type=Path, default=None)
    ex = sub.add_parser("export-json", help="Write CatBoost's JSON export of a verified bundle, for inspection")
    ex.add_argument("bundle", type=Path)
    ex.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run a subcommand; exit 1 with the failed check on a `BundleError`."""
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "keygen":
            kid = keygen(args.key, args.trusted_dir, args.force)
            print(json.dumps({"key_id": kid, "key": str(Path(args.key or default_signing_key()).expanduser()),
                              "trusted": str(Path(args.trusted_dir or TRUSTED_KEYS_DIR) / f"{kid}.pem")}))
        elif args.command == "sign":
            verify(args.bundle, allow_unsigned=True, deep=False)
            print(json.dumps({"bundle": str(args.bundle), "key_id": sign(args.bundle, args.key)}))
        elif args.command == "verify":
            verified = verify(args.bundle, allow_unsigned=args.allow_unsigned, deep=True)
            print(json.dumps({
                "bundle": str(verified.path), "ok": True, "key_id": verified.key_id, "checks": verified.checks,
                "network_isolation": verified.sandbox, "models": verified.model_files(),
            }, indent=2))
        elif args.command == "convert":
            bundle = convert(args.model, args.bundle, args.key)
            verified = verify(bundle, allow_unsigned=True, deep=True)
            print(json.dumps({"bundle": str(bundle), "key_id": verified.key_id, "checks": verified.checks}, indent=2))
        elif args.command == "export-json":
            print(json.dumps({"written": [str(p) for p in export_json(args.bundle, args.out)]}, indent=2))
    except BundleError as exc:
        print(json.dumps({"ok": False, "check": exc.check, "error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
