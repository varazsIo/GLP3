"""
Signed model bundles (`hpldn.backend.models.artifact`): every check `verify` makes, and each way past it.

A one-iteration CatBoost bundle (`tests._bundles.tiny_bundle`) is written
once per module and copied per test. The tampering tests change one thing
each: a byte of every file, a pickle smuggled into the bundle (as an extra
file, in a subdirectory, and in place of ``meta.json``), a trailing payload on
``model.cbm``, a signature by an untrusted key, a probe expectation and a
summary. Where a test re-seals the bundle (re-hashes and re-signs it with the
trusted test key) it shows the later checks catch what a key holder could
still slip past the earlier ones.

The "payloads" are harmless: `mimic_harmful_payload` only prints, and the
tests assert it never ran.
"""

import base64
import inspect
import json
import pickle
import shutil
from pathlib import Path

import numpy as np
import pytest

from hpldn.backend.models import artifact
from hpldn.backend.tests._bundles import TINY_JSON_FILES, make_signing_keys, tiny_bundle, tiny_model_and_meta

PAYLOAD_MESSAGE = "This would include a harmful payload!"


def mimic_harmful_payload():
    print(PAYLOAD_MESSAGE)


class _Smuggled:
    """Unpickling this calls `mimic_harmful_payload`; nothing in a bundle check may unpickle it."""

    def __reduce__(self):
        return (mimic_harmful_payload, ())


SMUGGLED_PICKLE = pickle.dumps(_Smuggled())


@pytest.fixture(scope="module")
def built(tmp_path_factory, _session_signing_keys):
    """The module's bundle, signed with the session's throwaway key (passed explicitly)."""
    return tiny_bundle(tmp_path_factory.mktemp("bundle") / "tiny", _session_signing_keys)


@pytest.fixture
def bundle(built, tmp_path):
    """A fresh copy of the module's signed bundle."""
    copy = tmp_path / built.name
    shutil.copytree(built, copy)
    return copy


def reseal(bundle: Path, key: Path) -> None:
    """Re-hash every file into the manifest and re-sign it: what a holder of a trusted key could do."""
    manifest = json.loads((bundle / artifact.MANIFEST_FILE).read_text())
    manifest["files"] = {
        p.name: {"sha256": artifact.sha256_file(p), "size": p.stat().st_size}
        for p in sorted(bundle.iterdir())
        if p.name in artifact.CONTENT_FILES
    }
    (bundle / artifact.MANIFEST_FILE).write_bytes(artifact.dumps_canonical(manifest))
    artifact.sign(bundle, key)


def check_of(bundle: Path, **kwargs) -> str:
    with pytest.raises(artifact.BundleError) as raised:
        artifact.verify(bundle, **kwargs)
    return raised.value.check


# --------------------------------------------------------------------------
# A good bundle
# --------------------------------------------------------------------------


def test_a_written_bundle_passes_every_check(bundle, model_signing_keys):
    verified = artifact.verify(bundle)
    assert verified.checks == ["signature", "files", "schema", "cbm round-trip", "summary", "probe"]
    assert verified.key_id == model_signing_keys.key_id
    assert sorted(p.name for p in bundle.iterdir()) == sorted(
        [*artifact.REQUIRED_FILES, *TINY_JSON_FILES, artifact.MANIFEST_FILE, artifact.SIGNATURE_FILE]
    )
    manifest = verified.manifest
    assert manifest["schema_version"] == artifact.SCHEMA_VERSION
    assert manifest["dataset"]["row_count"] == 400
    summary = manifest["models"][artifact.MODEL_FILE]
    assert summary["tree_count"] == 1
    assert summary["feature_names"] == ["postcode_district", "total_floor_area", "rel_prev_sale_local"]
    assert summary["cat_features"] == ["postcode_district"]
    # CatBoost's metadata map moved out of the canonical .cbm, into the manifest.
    assert "params" in manifest["catboost_metadata"][artifact.MODEL_FILE]
    assert len(verified.json[artifact.PROBE_FILE]["rows"]) == artifact.PROBE_ROWS


def test_the_canonical_cbm_reads_back_as_its_own_bytes(bundle):
    from catboost import CatBoostRegressor

    raw = (bundle / artifact.MODEL_FILE).read_bytes()
    model = CatBoostRegressor()
    model.load_model(blob=raw)
    assert bytes(model._object._serialize_model()) == raw
    assert len(model.get_metadata().keys()) <= 1  # only CatBoost's empty "params" stub


def test_load_bundle_restores_the_model_and_meta_exactly(bundle):
    original, meta = tiny_model_and_meta()
    model, loaded, verified = artifact.load_bundle(bundle)
    artifact.assert_meta_equal(meta, {k: v for k, v in loaded.items() if k != "high_value_model"})
    assert loaded["high_value_model"] is None
    assert np.isnan(loaded["lot_medians"]["rel_prev_sale_local"])
    probe = verified.json[artifact.PROBE_FILE]
    assert artifact.probe_predictions(model, probe) == artifact.probe_predictions(original, probe)
    assert verified.payload(artifact.CALIBRATION_FILE) == TINY_JSON_FILES[artifact.CALIBRATION_FILE]


def test_updating_a_json_file_rehashes_and_resigns(bundle):
    artifact.update_json_files(bundle, {artifact.CALIBRATION_FILE: {"n": 99, "ratio": float("nan")}})
    verified = artifact.verify(bundle, deep=False)
    assert verified.payload(artifact.CALIBRATION_FILE)["n"] == 99
    assert np.isnan(verified.payload(artifact.CALIBRATION_FILE)["ratio"])
    assert artifact.read_verified_json(bundle, artifact.CALIBRATION_FILE)["n"] == 99
    with pytest.raises(artifact.BundleError, match="cannot be updated in place"):
        artifact.update_json_files(bundle, {artifact.META_FILE: {}})


# --------------------------------------------------------------------------
# Tampering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, check",
    [
        (artifact.MODEL_FILE, "files"),
        (artifact.META_FILE, "files"),
        (artifact.PROBE_FILE, "files"),
        (artifact.CALIBRATION_FILE, "files"),
        (artifact.PROJECTION_FILE, "files"),
        (artifact.LOCAL_INDEX_FILE, "files"),
        (artifact.MANIFEST_FILE, "signature"),
        (artifact.SIGNATURE_FILE, "signature"),
    ],
)
def test_tampering_with_any_file_fails(bundle, name, check):
    path = bundle / name
    data = bytearray(path.read_bytes())
    # A byte in the middle; for the signature, one inside the base64 signature itself.
    at = len(data) - 10 if name == artifact.SIGNATURE_FILE else len(data) // 2
    data[at] = ord("A") if data[at] != ord("A") else ord("B")
    path.write_bytes(bytes(data))
    assert check_of(bundle) == check
    assert check_of(bundle, deep=False) == check


def test_a_deleted_or_added_file_fails(bundle):
    (bundle / artifact.CALIBRATION_FILE).unlink()
    assert check_of(bundle) == "files"


def test_an_unlisted_file_fails(bundle):
    (bundle / "notes.txt").write_text("hello")
    assert check_of(bundle) == "files"


@pytest.mark.parametrize("name", ["meta.joblib", "model.pkl", "MODEL.PICKLE"])
def test_a_pickle_smuggled_into_the_bundle_is_refused_unread(bundle, name, capsys):
    (bundle / name).write_bytes(SMUGGLED_PICKLE)
    with pytest.raises(artifact.BundleError, match="pickle") as raised:
        artifact.verify(bundle)
    assert raised.value.check == "files"
    with pytest.raises(artifact.BundleError, match="pickle"):
        artifact.load_bundle(bundle)
    assert PAYLOAD_MESSAGE not in capsys.readouterr().out


def test_a_pickle_in_a_subdirectory_is_refused(bundle, capsys):
    (bundle / "extra").mkdir()
    (bundle / "extra" / "payload.pkl").write_bytes(SMUGGLED_PICKLE)
    with pytest.raises(artifact.BundleError, match="pickle"):
        artifact.verify(bundle)
    assert PAYLOAD_MESSAGE not in capsys.readouterr().out


def test_a_pickle_in_place_of_meta_json_fails_even_when_resealed(bundle, model_signing_keys, capsys):
    (bundle / artifact.META_FILE).write_bytes(SMUGGLED_PICKLE)
    assert check_of(bundle) == "files"
    reseal(bundle, model_signing_keys.key)
    assert check_of(bundle) == "schema"
    with pytest.raises(artifact.BundleError):
        artifact.load_bundle(bundle)
    assert PAYLOAD_MESSAGE not in capsys.readouterr().out


def test_a_trailing_payload_on_the_cbm_fails_the_round_trip(bundle, model_signing_keys, capsys):
    trailer = inspect.getsource(mimic_harmful_payload).encode() + SMUGGLED_PICKLE
    with (bundle / artifact.MODEL_FILE).open("ab") as handle:
        handle.write(trailer)
    assert check_of(bundle) == "files"
    # Re-hashed and re-signed, the file still loads in CatBoost, but does not
    # read back as its own bytes.
    reseal(bundle, model_signing_keys.key)
    with pytest.raises(artifact.BundleError, match="does not read back as its own bytes") as raised:
        artifact.verify(bundle)
    assert raised.value.check == "cbm"
    assert PAYLOAD_MESSAGE not in capsys.readouterr().out


def test_a_file_that_is_not_a_cbm_fails_the_magic(bundle, model_signing_keys):
    (bundle / artifact.MODEL_FILE).write_bytes(b"#!/bin/sh\necho hi\n")
    reseal(bundle, model_signing_keys.key)
    assert check_of(bundle, deep=False) == "cbm"


def test_a_bundle_signed_with_an_untrusted_key_fails(bundle, tmp_path):
    other = make_signing_keys(tmp_path / "other")
    artifact.sign(bundle, other.key)
    with pytest.raises(artifact.BundleError, match="not in") as raised:
        artifact.verify(bundle)
    assert raised.value.check == "signature"
    # And trusted, it passes.
    assert artifact.verify(bundle, trusted_dir=other.trusted_dir).key_id == other.key_id


def test_a_signature_by_a_trusted_key_over_other_bytes_fails(bundle, model_signing_keys, tmp_path):
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(model_signing_keys.key.read_bytes(), password=None)
    forged = base64.b64encode(key.sign(b"some other manifest")).decode()
    (bundle / artifact.SIGNATURE_FILE).write_text(f"ed25519 {model_signing_keys.key_id} {forged}\n")
    assert check_of(bundle) == "signature"


def test_a_probe_mismatch_fails(bundle, model_signing_keys):
    probe = json.loads((bundle / artifact.PROBE_FILE).read_text())
    probe["expected"][artifact.MODEL_FILE][0] += 1e-9
    (bundle / artifact.PROBE_FILE).write_bytes(artifact.dumps_canonical(probe))
    reseal(bundle, model_signing_keys.key)
    assert check_of(bundle) == "probe"
    with pytest.raises(artifact.BundleError) as raised:
        artifact.load_bundle(bundle)
    assert raised.value.check == "probe"


def test_a_summary_mismatch_fails(bundle, model_signing_keys):
    manifest = json.loads((bundle / artifact.MANIFEST_FILE).read_text())
    manifest["models"][artifact.MODEL_FILE]["tree_count"] = 2
    (bundle / artifact.MANIFEST_FILE).write_bytes(artifact.dumps_canonical(manifest))
    artifact.sign(bundle, model_signing_keys.key)
    assert check_of(bundle) == "summary"


def test_a_meta_json_that_breaks_its_schema_fails(bundle, model_signing_keys):
    meta = json.loads((bundle / artifact.META_FILE).read_text())
    meta["__class__"] = "os.system"
    (bundle / artifact.META_FILE).write_bytes(artifact.dumps_canonical(meta))
    reseal(bundle, model_signing_keys.key)
    with pytest.raises(artifact.BundleError, match="unexpected key '__class__'"):
        artifact.verify(bundle, deep=False)


def test_a_symlink_in_the_bundle_fails(bundle, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (bundle / artifact.CALIBRATION_FILE).unlink()
    (bundle / artifact.CALIBRATION_FILE).symlink_to(target)
    assert check_of(bundle) == "files"


# --------------------------------------------------------------------------
# Unsigned bundles and keys
# --------------------------------------------------------------------------


def test_an_unsigned_bundle_loads_only_when_allowed_and_never_in_production(bundle, monkeypatch):
    (bundle / artifact.SIGNATURE_FILE).unlink()
    assert check_of(bundle) == "signature"
    with pytest.raises(artifact.BundleError):
        artifact.load_bundle(bundle)
    monkeypatch.setenv(artifact.ALLOW_UNSIGNED_ENV, "1")
    model, meta, verified = artifact.load_bundle(bundle)
    assert verified.key_id is None
    monkeypatch.setenv(artifact.ENVIRONMENT_ENV, "production")
    with pytest.raises(artifact.BundleError, match="refused when HPLDN_ENV=production"):
        artifact.load_bundle(bundle)


def test_keygen_writes_a_private_key_only_its_owner_can_read(tmp_path):
    keys = make_signing_keys(tmp_path)
    assert keys.key.stat().st_mode & 0o777 == 0o600
    assert (keys.trusted_dir / f"{keys.key_id}.pem").is_file()
    with pytest.raises(FileExistsError):
        artifact.keygen(keys.key, keys.trusted_dir)
    keys.key.chmod(0o644)
    with pytest.raises(PermissionError, match="must be 0600"):
        artifact.load_signing_key(keys.key)


def test_a_trusted_key_file_must_be_named_after_its_key(tmp_path):
    keys = make_signing_keys(tmp_path)
    (keys.trusted_dir / f"{keys.key_id}.pem").rename(keys.trusted_dir / "renamed.pem")
    with pytest.raises(artifact.BundleError, match="rename it"):
        artifact.trusted_keys(keys.trusted_dir)


def test_a_bundle_written_without_a_key_is_unsigned(tmp_path):
    model, meta = tiny_model_and_meta()
    bundle = artifact.write_bundle(tmp_path / "nokey", model, meta, key_path=tmp_path / "absent.key")
    assert not (bundle / artifact.SIGNATURE_FILE).exists()
    assert artifact.verify(bundle, allow_unsigned=True).key_id is None


def test_write_bundle_refuses_to_replace_something_that_is_not_a_bundle(tmp_path):
    model, meta = tiny_model_and_meta()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "keep.txt").write_text("mine")
    with pytest.raises(artifact.BundleError, match="not a model bundle"):
        artifact.write_bundle(tmp_path / "data", model, meta)
    assert (tmp_path / "data" / "keep.txt").read_text() == "mine"


# --------------------------------------------------------------------------
# Strict JSON, the legacy conversion and the pickle-only paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '{"a": 1, "a": 2}'])
def test_bundle_json_is_parsed_strictly(text):
    with pytest.raises(ValueError):
        artifact.loads_strict(text.encode())


def test_jsonable_refuses_what_would_not_read_back():
    assert artifact.jsonable({"a": np.float32(1.5), "b": (1, 2), "c": float("inf")}) == {
        "a": 1.5, "b": [1, 2], "c": {"$float": "inf"},
    }
    with pytest.raises(TypeError, match="cannot be stored as JSON"):
        artifact.jsonable({"a": object()})
    with pytest.raises(TypeError, match="is not a string"):
        artifact.jsonable({1: 2})


def test_convert_turns_a_legacy_model_into_a_signed_bundle(tmp_path, model_signing_keys):
    import joblib

    original, meta = tiny_model_and_meta()
    legacy = tmp_path / "legacy.cbm"
    original.save_model(str(legacy))
    joblib.dump(meta, f"{legacy}.meta.joblib")  # the trusted, pre-bundle format convert reads once
    Path(f"{legacy}.projection.json").write_text(json.dumps(TINY_JSON_FILES[artifact.PROJECTION_FILE]))
    bundle = artifact.convert(legacy, tmp_path / "converted")
    verified = artifact.verify(bundle)
    assert verified.key_id == model_signing_keys.key_id
    assert verified.payload(artifact.PROJECTION_FILE) == TINY_JSON_FILES[artifact.PROJECTION_FILE]
    assert verified.manifest["training_args"]["converted_from"] == "legacy.cbm"
    model, loaded, _ = artifact.load_bundle(bundle)
    probe = verified.json[artifact.PROBE_FILE]
    assert artifact.probe_predictions(model, probe) == artifact.probe_predictions(original, probe)


def test_hpp_cb_load_model_refuses_a_legacy_model_and_points_at_convert(tmp_path):
    from hpldn.backend.scripts import hpp_cb

    legacy = tmp_path / "legacy.cbm"
    legacy.write_bytes(b"CBM1")
    Path(f"{legacy}.meta.joblib").write_bytes(SMUGGLED_PICKLE)
    with pytest.raises(FileNotFoundError, match="models.artifact convert"):
        hpp_cb.load_model(legacy)


def test_hpp_rf_load_model_needs_trust_pickle_and_reads_nothing_without_it(tmp_path, capsys):
    from hpldn.backend.scripts import hpp_rf

    path = tmp_path / "forest.rfm"
    path.write_bytes(SMUGGLED_PICKLE)
    with pytest.raises(PermissionError, match="--trust-pickle"):
        hpp_rf.load_model(path)
    assert PAYLOAD_MESSAGE not in capsys.readouterr().out


def test_export_json_writes_catboost_json_for_a_verified_bundle(bundle, tmp_path):
    written = artifact.export_json(bundle, tmp_path / "json")
    assert [p.name for p in written] == [f"{bundle.name}.model.json"]
    assert json.loads(written[0].read_text())["oblivious_trees"]


def test_the_cli_verifies_and_reports_the_failed_check(bundle, capsys):
    assert artifact.main(["verify", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    (bundle / artifact.META_FILE).write_text("{}")
    assert artifact.main(["verify", str(bundle)]) == 1
    assert json.loads(capsys.readouterr().err)["check"] == "files"
