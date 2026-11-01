"""
The 3-sigma bounds the seeding SQL interpolates, and the null-variant check.
"""

import numpy as np
import pytest

from hpldn.backend.common.utils import check_none_variants, get_bounds_based_on_std

N = 200_000


def _normal(seed=0, n=N):
    return np.random.default_rng(seed).normal(0.0, 1.0, n)


def test_bounds_are_host_floats():
    lower, upper = get_bounds_based_on_std(_normal(n=10_000))
    assert isinstance(lower, float) and isinstance(upper, float)
    assert lower == 0.0  
    assert upper > 0.0


def test_bounds_accept_an_array_this_module_produced():
    """Check bounds accept an earlier `asarray` result, including a CuPy array on CUDA hosts."""
    from hpldn.backend.common.accel import asarray

    staged = asarray(_normal(n=10_000), dtype=np.float64)
    assert get_bounds_based_on_std(staged) == pytest.approx(
        get_bounds_based_on_std(_normal(n=10_000))
    )


@pytest.mark.parametrize(
    "value", [None, "", "N/A", "None", np.nan, float("nan"), np.float64("nan")]
)
def test_none_variants_are_detected(value):
    """Check computed NaNs, not just the ``np.nan`` singleton, are detected."""
    assert check_none_variants(value) is True


@pytest.mark.parametrize("value", [0, 0.0, "x", "0", -1])
def test_real_values_are_not_none_variants(value):
    assert check_none_variants(value) is False


def test_an_array_does_not_raise():
    """Check an array input returns False rather than raising an ambiguous-truth error."""
    assert check_none_variants(np.array([1.0, 2.0])) is False


def test_a_none_variant_is_logged_with_and_without_a_column(caplog):
    import logging

    log = logging.getLogger("test_utils")
    with caplog.at_level(logging.WARNING, logger="test_utils"):
        assert check_none_variants("N/A", log=log)
        assert check_none_variants("", "postcode", log=log)
    assert "Variable has value of N/A" in caplog.text
    assert "postcode has value of " in caplog.text


# -- load_env_vars / load_yaml_safe -------------------------------------


def test_load_env_vars_needs_hpldn_dir_fp(monkeypatch):
    from hpldn.backend.common.utils import load_env_vars

    monkeypatch.delenv("HPLDN_DIR_FP", raising=False)
    with pytest.raises(RuntimeError, match="HPLDN_DIR_FP is not set"):
        load_env_vars()


def _env_file(tmp_path, monkeypatch):
    (tmp_path / "develop.env").write_text(
        "# a comment\n\nHPLDN_T_NEW='from-file'\nHPLDN_T_KEPT=from-file\nnot a pair\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HPLDN_DIR_FP", str(tmp_path))
    monkeypatch.delenv("HPLDN_T_NEW", raising=False)
    monkeypatch.setenv("HPLDN_T_KEPT", "from-environment")


def test_load_env_vars_keeps_the_environment_by_default(tmp_path, monkeypatch, caplog):
    import logging
    import os

    from hpldn.backend.common.utils import load_env_vars

    _env_file(tmp_path, monkeypatch)
    monkeypatch.delenv("HPLDN_ENV_OVERRIDE", raising=False)
    with caplog.at_level(logging.INFO, logger="test_utils"):
        load_env_vars(logging.getLogger("test_utils"))
    assert os.environ["HPLDN_T_NEW"] == "from-file"
    assert os.environ["HPLDN_T_KEPT"] == "from-environment"
    assert "Successfully loaded environment variables" in caplog.text
    monkeypatch.delenv("HPLDN_T_NEW")


@pytest.mark.parametrize("no_override, expected", [(None, "from-file"), ("1", "from-environment")])
def test_load_env_vars_override(tmp_path, monkeypatch, no_override, expected):
    import os

    from hpldn.backend.common.utils import load_env_vars

    _env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("HPLDN_ENV_OVERRIDE", "1")
    if no_override is None:
        monkeypatch.delenv("HPLDN_ENV_NO_OVERRIDE", raising=False)
    else:
        monkeypatch.setenv("HPLDN_ENV_NO_OVERRIDE", no_override)
    load_env_vars()
    assert os.environ["HPLDN_T_KEPT"] == expected
    monkeypatch.delenv("HPLDN_T_NEW")


def test_load_yaml_safe_expands_variables_in_nested_strings(tmp_path, monkeypatch, caplog):
    import logging

    from hpldn.backend.common.utils import load_yaml_safe

    monkeypatch.setenv("HPLDN_T_HOST", "db.internal")
    monkeypatch.delenv("HPLDN_T_UNSET", raising=False)
    fp = tmp_path / "cfg.yaml"
    fp.write_text(
        "db:\n  host: ${HPLDN_T_HOST}\n  port: 5432\n"
        "hosts: [\"${HPLDN_T_HOST}:1\", \"x${HPLDN_T_UNSET}y\"]\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO, logger="test_utils"):
        cfg = load_yaml_safe(fp, logging.getLogger("test_utils"))
    assert cfg == {"db": {"host": "db.internal", "port": 5432}, "hosts": ["db.internal:1", "xy"]}
    assert "Successfully loaded yaml configuration file" in caplog.text
