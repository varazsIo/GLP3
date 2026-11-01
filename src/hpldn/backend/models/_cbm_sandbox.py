"""
Parse a model bundle's CatBoost files in a locked-down child process.

`artifact.verify` runs it as ``python -I -m hpldn.backend.models._cbm_sandbox``
(no user site, no ``PYTHON*`` variables, no current directory on the path),
with an empty environment, resource limits and, where the host allows it, no
network namespace (see `artifact.run_sandbox`). By module name, not file
path, so it runs from the host's sourceless install (only ``.pyc`` files).
Running it by module name imports the parent packages' ``__init__`` files
(``hpldn.backend.models`` declares the SQLAlchemy models: declarations
only, no I/O, no configuration); this module itself imports nothing from
``hpldn``, and only the standard library, numpy and catboost touch the
untrusted bytes.

For each ``.cbm`` named on the command line it:

1. checks the ``CBM1`` magic;
2. loads the bytes into CatBoost, serialises the model again in memory and
   hashes the result: a canonical bundle file reads back as exactly its own
   bytes, so anything CatBoost's schema does not parse (a trailing payload,
   a smuggled section) changes the hash;
3. summarises the model (`summarise`), for `artifact.verify` to compare with
   the manifest;
4. predicts the bundle's probe rows (`probe_pool`), for `artifact.verify` to
   compare with ``probe.json``.

The result is one JSON object on stdout. Nothing is written to disk: the
parent sets ``RLIMIT_FSIZE`` to 0.
"""

import hashlib
import json
import math
import re
import socket
import sys

CBM_MAGIC = b"CBM1"
COUNTER_TYPE = re.compile(r"counter_type=(\w+)")


def _no_network(*_args, **_kwargs):
    raise OSError("network access is disabled in the model sandbox")


def block_network() -> None:
    """Replace the socket constructors, for hosts where no network namespace can be made."""
    socket.socket = _no_network  # type: ignore[assignment,misc]
    socket.create_connection = _no_network  # type: ignore[assignment]
    socket.getaddrinfo = _no_network  # type: ignore[assignment]
    socket.socketpair = _no_network  # type: ignore[assignment]


def probe_pool(probe: dict, feature_names: list, cat_indices: list):
    """
    Return the probe rows as a CatBoost Pool, numeric None read as NaN.

    Built from plain lists rather than a DataFrame so this process needs no
    pandas; CatBoost reads numeric features as float32 either way.
    """
    from catboost import Pool

    cats = set(cat_indices)
    data = [
        [value if i in cats else (math.nan if value is None else float(value)) for i, value in enumerate(row)]
        for row in probe["rows"]
    ]
    return Pool(data, cat_features=sorted(cats), feature_names=list(feature_names))


def predict_raw(model, pool) -> list:
    """Return the model's raw predictions for `pool` as floats."""
    return [float(v) for v in model.predict(pool, prediction_type="RawFormulaVal")]


def summarise(model, pool) -> dict:
    """
    Return what the manifest records about a model, read from the model alone.

    The CTR types come from the trees' splits. CatBoost prints a split on a
    categorical feature only with a dataset to name its values from, so the
    probe pool is passed; the counts do not depend on it.
    """
    names = [str(n) for n in model.feature_names_]
    cat_indices = sorted(int(i) for i in model.get_cat_feature_indices())
    oblivious = bool(model._object._is_oblivious())
    leaf_counts = sorted({int(c) for c in model.get_tree_leaf_counts()})
    split_counts = {"float": 0}
    for tree in range(int(model.tree_count_)):
        for split in model._object._get_tree_splits(tree, pool):
            match = COUNTER_TYPE.search(split)
            key = f"ctr:{match.group(1)}" if match else "float"
            split_counts[key] = split_counts.get(key, 0) + 1
    scale, bias = model.get_scale_and_bias()
    return {
        "tree_count": int(model.tree_count_),
        "oblivious": oblivious,
        "depth": max(int(round(math.log2(c))) for c in leaf_counts) if oblivious and leaf_counts else None,
        "leaf_counts": leaf_counts,
        "feature_names": names,
        "cat_features": [names[i] for i in cat_indices],
        "ctr_types": sorted(k.split(":", 1)[1] for k in split_counts if k.startswith("ctr:")),
        "split_counts": dict(sorted(split_counts.items())),
        "scale": float(scale),
        "bias": [float(b) for b in bias] if isinstance(bias, (list, tuple)) else float(bias),
    }


def inspect(cbm_bytes: bytes, probe: dict) -> dict:
    """Return the magic check, canonical round-trip digest, summary and probe predictions of one model."""
    from catboost import CatBoostRegressor

    if cbm_bytes[: len(CBM_MAGIC)] != CBM_MAGIC:
        return {"magic": False}
    model = CatBoostRegressor()
    model.load_model(blob=cbm_bytes)
    reserialised = bytes(model._object._serialize_model())
    pool = probe_pool(probe, model.feature_names_, model.get_cat_feature_indices())
    return {
        "magic": True,
        "size": len(cbm_bytes),
        "sha256": hashlib.sha256(cbm_bytes).hexdigest(),
        "roundtrip_size": len(reserialised),
        "roundtrip_sha256": hashlib.sha256(reserialised).hexdigest(),
        "summary": summarise(model, pool),
        "predictions": predict_raw(model, pool),
    }


def main(argv: list) -> int:
    """``_cbm_sandbox.py BUNDLE_DIR NAME.cbm [NAME.cbm ...]``: print one JSON report."""
    block_network()
    bundle, names = argv[0], argv[1:]
    with open(f"{bundle}/probe.json", "rb") as handle:
        probe = json.loads(handle.read())
    report = {}
    for name in names:
        with open(f"{bundle}/{name}", "rb") as handle:
            data = handle.read()
        report[name] = inspect(data, probe)
        del data
    sys.stdout.write(json.dumps(report))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
