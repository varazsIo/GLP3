"""
The served-model tier shared by `test_price_golden` and `test_hpp_inference`.

Both price real homes on hpdbldn with a saved CatBoost model. The model is
pinned, not discovered: the golden prices must not depend on whatever was
trained last. The pin is a bundle directory (`models.artifact`) and the
SHA-256 of its signed ``manifest.json``, which in turn pins every file in it;
the bundle is verified (signature against the tracked ``trusted_keys/``,
hashes, schemas, summary, probe) when `SavedModel` loads it.
``HPLDN_GOLDEN_MODEL_FP`` points the tier at another bundle and turns the
manifest pin off, for a deliberate re-baseline.

`pricing_service` is built once per session: loading the model and the
evidence takes seconds.
"""

import hashlib
import os
from pathlib import Path

import pytest

GOLDEN_MODEL_ENV = "HPLDN_GOLDEN_MODEL_FP"
GOLDEN_MODEL = Path(__file__).resolve().parents[5] / "tmp" / "models" / "model_anchored_0031"
# manifest.json of the bundle `models.artifact convert` made from
# model_anchored_0031.cbm (sha256 dd3d6d22...) and its .meta.joblib (e1c4ee72...),
# plus the calibration.json written by `hpp_inference evaluate --write-calibration
# --mask-epc` (re-signed; the model files are unchanged).
GOLDEN_MANIFEST_SHA256 = "3dbfb2c787758efe4d533ea4fda40a956f5dcd8ba412a442e03aea936bcfefc2"


@pytest.fixture(scope="session")
def pinned_model_path() -> Path:
    """Return the pinned model bundle, failing when it is missing or its manifest is not the pinned one."""
    override = os.environ.get(GOLDEN_MODEL_ENV)
    path = Path(override) if override else GOLDEN_MODEL
    manifest = path / "manifest.json"
    if not manifest.is_file():
        pytest.fail(f"missing prerequisite: the pinned price model bundle {path}", pytrace=False)
    if not override:
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == GOLDEN_MANIFEST_SHA256, (
            f"{path} is not the pinned golden model; re-baseline deliberately with {GOLDEN_MODEL_ENV}"
        )
    return path


@pytest.fixture(scope="session")
def pricing_service(hpdbldn_ro, pinned_model_path):
    """Return a `PricingService` on hpdbldn (read-only) with the pinned model."""
    from hpldn.backend.scripts.hpp_inference import PricingService, SavedModel

    return PricingService(hpdbldn_ro, SavedModel(pinned_model_path))
