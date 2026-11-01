"""
``shellcheck`` over every shell script the deployment runs.

The binary comes from the ``shellcheck-py`` wheel in the dev extra, so no
system package is needed; a finding of any severity fails, and a deliberate
exception is a ``# shellcheck disable=SCnnnn`` with its reason beside it.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPTS = sorted([*(BACKEND / "cloud").rglob("*.sh"), *(BACKEND / "db" / "sql").glob("*.sh")])
REPO_SCRIPTS = sorted((BACKEND.parents[2] / "scripts").rglob("*.sh"))


def _shellcheck() -> str:
    found = shutil.which("shellcheck") or str(Path(sys.executable).with_name("shellcheck"))
    if not Path(found).is_file():
        pytest.fail("missing prerequisite: shellcheck (uv sync --extra dev installs shellcheck-py)", pytrace=False)
    return found


def test_the_scripts_are_found():
    names = {p.name for p in SCRIPTS}
    assert {"bootstrap.sh", "deploy.sh", "firewall.sh", "restore_serving_db.sh", "fetch_nginx.sh",
            "db_enable_api_login.sh"} <= names


@pytest.mark.parametrize("script", SCRIPTS + REPO_SCRIPTS, ids=lambda p: str(p.relative_to(BACKEND.parents[2])))
def test_shellcheck_is_clean(script):
    result = subprocess.run([_shellcheck(), "-x", "--shell=bash", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
