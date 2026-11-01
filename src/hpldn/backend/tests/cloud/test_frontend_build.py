"""
The frontend's production build check (``scripts/strip-build.mjs``, run by ``npm run build:prod``).

On a small stand-in build: every source map, ``sourceMappingURL`` and the
asset manifest is gone, the code itself is left as it was, and the final
check fails the build when it must. With a real ``build/`` present it is
checked too.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
SCRIPT = FRONTEND / "scripts" / "strip-build.mjs"
BUNDLE = "(function(exports){exports.answer=42})({});"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.fail("missing prerequisite: node", pytrace=False)
    return node


@pytest.fixture
def build(tmp_path) -> Path:
    root = tmp_path / "build"
    js = root / "static" / "js"
    css = root / "static" / "css"
    js.mkdir(parents=True)
    css.mkdir(parents=True)
    (js / "main.0123abcd.js").write_text(BUNDLE + "\n//# sourceMappingURL=main.0123abcd.js.map\n")
    (js / "main.0123abcd.js.map").write_text('{"version":3,"sources":["src/App.tsx"]}')
    (css / "main.0123abcd.css").write_text("body{margin:0}\n/*# sourceMappingURL=main.0123abcd.css.map*/")
    (css / "main.0123abcd.css.map").write_text("{}")
    (root / "asset-manifest.json").write_text('{"files":{"main.js":"/static/js/main.0123abcd.js"}}')
    (root / "index.html").write_text('<!doctype html><script defer src="/static/js/main.0123abcd.js"></script>')
    return root


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([_node(), str(SCRIPT), *args], capture_output=True, text=True, cwd=FRONTEND)


def test_the_build_is_stripped_of_maps(build):
    done = _run("--build", str(build))
    assert done.returncode == 0, done.stderr
    assert (build / "static" / "js" / "main.0123abcd.js").read_text().strip() == BUNDLE
    assert not list(build.rglob("*.map"))
    assert not (build / "asset-manifest.json").exists()
    assert not [p for p in build.rglob("*") if p.is_file() and "sourceMappingURL" in p.read_text()]


def test_a_build_without_bundles_fails(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html>")
    done = _run("--build", str(tmp_path))
    assert done.returncode == 1
    assert "react-scripts build first" in done.stderr


def test_the_check_refuses_a_build_with_a_map(build):
    done = _run("--build", str(build), "--check")
    assert done.returncode == 1
    assert "main.0123abcd.js.map" in done.stderr and "asset-manifest.json" in done.stderr


def test_a_real_build_ships_nothing_that_maps_it_back():
    real = FRONTEND / "build"
    if not (real / "index.html").is_file():
        return  # no build yet; deploy.sh and the script itself check each one
    if list(real.rglob("*.map")):
        return  # a plain `npm run build` (development tooling), not build:prod
    done = _run("--build", str(real), "--check")
    assert done.returncode == 0, done.stderr
