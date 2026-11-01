"""
The serving host's sourceless install, as deploy.sh makes it: wheel, compile, delete every .py.

Builds the real wheel (``uv build --wheel``), unpacks its ``hpldn`` package,
compiles it with ``compileall -b`` and deletes the sources, then imports the
API and runs the CatBoost sandbox module from that tree alone.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture(scope="module")
def sourceless(tmp_path_factory) -> Path:
    uv = shutil.which("uv")
    if not uv:
        pytest.fail("missing prerequisite: uv (to build the wheel)", pytrace=False)
    work = tmp_path_factory.mktemp("wheel")
    subprocess.run([uv, "build", "--quiet", "--wheel", "--out-dir", str(work / "dist"), str(REPO_ROOT)], check=True)
    [wheel] = (work / "dist").glob("hpldn-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not [n for n in names if "/tests/" in n or n.endswith("conftest.py")], "the wheel ships tests"
        assert not [n for n in names if "/frontend/" in n], "the wheel ships the frontend"
        archive.extractall(work / "site")
    package = work / "site" / "hpldn"
    subprocess.run([sys.executable, "-I", "-m", "compileall", "-b", "-q", str(package)], check=True)
    for source in package.rglob("*.py"):
        source.unlink()
    for cache in list(package.rglob("__pycache__")):
        shutil.rmtree(cache)
    return work / "site"


def _python(site: Path, code: str, **kwargs) -> subprocess.CompletedProcess:
    """`code` under ``python -I`` with `site` first on the path (ahead of the editable checkout)."""
    prelude = f"import sys; sys.path.insert(0, {str(site)!r}); "
    return subprocess.run([sys.executable, "-I", "-c", prelude + code], capture_output=True, text=True, cwd="/", **kwargs)


def test_no_source_is_left(sourceless):
    assert not list((sourceless / "hpldn").rglob("*.py"))
    assert (sourceless / "hpldn" / "backend" / "api" / "main.pyc").is_file()


def test_the_api_imports_from_the_sourceless_tree(sourceless):
    done = _python(sourceless, "import hpldn, hpldn.backend.api.main, hpldn.backend.api.worker; print(hpldn.__file__)",
                   env={"HPLDN_API_SKIP_STARTUP_CHECK": "1"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(sourceless / "hpldn" / "__init__.pyc")


def test_the_model_sandbox_runs_as_a_module_from_the_sourceless_tree(sourceless, tmp_path):
    from hpldn.backend.models import artifact
    from hpldn.backend.tests._bundles import tiny_bundle

    bundle = tmp_path / "bundle"
    tiny_bundle(bundle)
    code = (
        "import runpy; "
        f"sys.argv = ['_cbm_sandbox', {str(bundle)!r}, {artifact.MODEL_FILE!r}]; "
        "runpy.run_module('hpldn.backend.models._cbm_sandbox', run_name='__main__', alter_sys=True)"
    )
    done = _python(sourceless, code, env=dict(artifact.SANDBOX_ENV))
    assert done.returncode == 0, done.stderr
    assert '"' in done.stdout and artifact.MODEL_FILE in done.stdout
