"""
Model bundle helpers for the tests (`hpldn.backend.models.artifact`).

`test_signing_keys` makes one throwaway Ed25519 key per session and a trusted
directory holding it plus the tracked ``trusted_keys/`` (so the pinned golden
bundle still verifies). The root conftest points `artifact` at them for every
test: nothing in the suite signs with the developer's own key.

`tiny_bundle` writes a real, signed bundle around a one-iteration CatBoost
model, for tests that need a bundle but not a useful model.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from hpldn.backend.models import artifact

TINY_FEATURES = ["postcode_district", "total_floor_area", "rel_prev_sale_local"]
TINY_CATEGORICAL = ["postcode_district"]


@dataclass(frozen=True)
class SigningKeys:
    """A throwaway signing key and the trusted directory that holds its public key."""

    key: Path
    key_id: str
    trusted_dir: Path


def make_signing_keys(root: Path) -> SigningKeys:
    """Create a key under `root` and a trusted dir with it and every tracked trusted key."""
    trusted = root / "trusted_keys"
    trusted.mkdir(parents=True)
    for pem in artifact.TRUSTED_KEYS_DIR.glob("*.pem"):
        shutil.copy(pem, trusted / pem.name)
    key = root / "model-signing.key"
    kid = artifact.keygen(key, trusted)
    return SigningKeys(key=key, key_id=kid, trusted_dir=trusted)


def tiny_model_and_meta(seed: int = 0):
    """Return a one-iteration CatBoost model on three features and the meta `hpp_cb` would save with it."""
    from catboost import CatBoostRegressor

    rng = np.random.default_rng(seed)
    n = 400
    districts = np.array(["E1", "N1", "SW3", "W11"])
    X = pd.DataFrame(
        {
            "postcode_district": rng.choice(districts, n),
            "total_floor_area": rng.uniform(30, 200, n).astype("float32"),
            "rel_prev_sale_local": rng.normal(size=n).astype("float32"),
        }
    )
    y = 0.01 * X["total_floor_area"] + (X["postcode_district"] == "W11") * 2.0 + rng.normal(scale=0.1, size=n)
    model = CatBoostRegressor(
        iterations=1, depth=2, verbose=0, allow_writing_files=False, random_seed=seed, thread_count=1
    )
    model.fit(X, y, cat_features=TINY_CATEGORICAL)
    postcode_stats = pd.DataFrame(
        {
            "postcode_median_price": [500_000.0, 650_000.0, np.nan, 1_200_000.0],
            "postcode_median_price_per_sqm": [6_000.0, 7_500.0, 12_000.0, 14_000.0],
            "postcode_txn_count": [10, 20, 30, 40],
        },
        index=pd.Index(list(districts), name="postcode_district"),
    )
    sector_stats = pd.DataFrame(
        {
            "postcode_sector_median_price": [510_000.0],
            "postcode_sector_median_price_per_sqm": [6_100.0],
            "postcode_sector_txn_count": [5],
        },
        index=pd.Index(["E1 6"], name="postcode_sector"),
    )
    meta = {
        "lot_medians": {"total_floor_area": 80.0, "rel_prev_sale_local": float("nan")},
        "postcode_stats": postcode_stats,
        "sector_stats": sector_stats,
        "price_stat_globals": {"median_price": 0.91, "median_pps": 0.0125, "scale": "deflated"},
        "group_stats_scale": "deflated",
        "feature_columns": list(TINY_FEATURES),
        "cat_features": list(TINY_CATEGORICAL),
        "dropped_flags": [],
        "missing_flag_groups": {"prev_sale_missing": ["rel_prev_sale_local"]},
        "nan_mode": "native",
        "drop_features": [],
        "categorical_columns": list(TINY_CATEGORICAL),
        "use_iod": False,
        "use_ofsted": False,
        "log_target": True,
        "target": "anchored",
        "anchor_columns": ["comp_sizeband_implied_value"],
        "smearing_factors": None,
        "high_value_expert": False,
        "high_value_threshold": None,
        "population": "all",
        "train_period": ["2020-01-01", "2024-12-31"],
        "database": None,
    }
    return model, meta


TINY_JSON_FILES = {
    artifact.CALIBRATION_FILE: {"n": 10, "log_ratio_quantiles": {"0.1": -0.1, "0.9": 0.1}, "strata": {}},
    artifact.PROJECTION_FILE: {"method": "damped_blend", "types": {}},
    artifact.LOCAL_INDEX_FILE: {"method": "repeat_sales", "series": {}},
}


def tiny_bundle(path: Path, keys: Optional[SigningKeys] = None, json_files: bool = True) -> Path:
    """
    Write a signed bundle around `tiny_model_and_meta` at `path`.

    Signed with `keys`, or else `artifact.default_signing_key` (the session's
    throwaway key under the root conftest).
    """
    model, meta = tiny_model_and_meta()
    return artifact.write_bundle(
        path,
        model,
        meta,
        json_files=TINY_JSON_FILES if json_files else None,
        training_args={"iterations": 1, "depth": 2},
        dataset=artifact.dataset_fingerprint(database="test", row_count=400, date_range=["2020-01-01", "2024-12-31"]),
        key_path=keys.key if keys is not None else None,
    )
