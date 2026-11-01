"""
The suite's own rules (``conftest.py``) and the ephemeral-cluster helpers (``_pg.py``).

The rules are proven by running pytest on small throwaway test files with
the root conftest loaded as a plugin: a skip, an ``importorskip`` and a
non-strict xfail must each fail the run, while a strict xfail and the
db-write policy's skip must not. The `_pg` helpers are checked without
starting a cluster.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hpldn.backend.tests import _pg

CASES = {
    "a_skip": ("import pytest\ndef test_x():\n    pytest.skip('no widget server')\n", 1, "missing prerequisite: no widget server"),
    "a_fixture_skip": (
        "import pytest\n@pytest.fixture\ndef widget():\n    pytest.skip('widget absent')\ndef test_x(widget):\n    pass\n",
        1, "missing prerequisite: widget absent"),
    "an_importorskip": ("import pytest\npytest.importorskip('no_such_module_xyz')\ndef test_x():\n    pass\n", 2,
                        "collection skipped"),
    "a_lax_xfail": ("import pytest\n@pytest.mark.xfail(reason='flaky', strict=False)\ndef test_x():\n    assert False\n", 1,
                    "non-strict xfail"),
    "a_strict_xfail": ("import pytest\n@pytest.mark.xfail(reason='known', strict=True)\ndef test_x():\n    assert False\n", 0,
                       "1 xfailed"),
    "a_db_writer": ("import pytest\n@pytest.mark.db_write\ndef test_x():\n    raise RuntimeError('must not run')\n", 0,
                    "db-write policy"),
    "a_pg_test": ("def test_x(pg_cluster):\n    raise RuntimeError('must not run')\n", 0, "db-write policy"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_skip_rules(tmp_path, case):
    source, exit_code, expected = CASES[case]
    (tmp_path / f"test_{case}.py").write_text(source)
    env = {k: v for k, v in os.environ.items() if k != "HPLDN_TEST_DB_WRITES"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "hpldn.backend.tests.conftest", "-p", "no:cacheprovider",
         "-c", os.devnull, "--rootdir", str(tmp_path), "-rA", str(tmp_path)],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    output = result.stdout + result.stderr
    assert result.returncode == exit_code, output
    assert expected in output, output


def test_find_pg_bindir_prefers_the_override(tmp_path, monkeypatch):
    for tool in ("initdb", "pg_ctl", "psql"):
        (tmp_path / tool).write_text("")
    monkeypatch.setenv("HPLDN_PG_BINDIR", str(tmp_path))
    assert _pg.find_pg_bindir() == tmp_path


def test_find_pg_bindir_names_what_it_tried(tmp_path, monkeypatch):
    monkeypatch.setenv("HPLDN_PG_BINDIR", str(tmp_path))
    monkeypatch.setattr(_pg.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match=str(tmp_path)):
        _pg.find_pg_bindir()


def test_free_port_is_a_bindable_loopback_port():
    import socket

    port = _pg.free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_a_cluster_url_and_the_pinned_db_url(monkeypatch):
    cluster = _pg.PgCluster(bindir=Path("/bin"), datadir=Path("/d"), socket_dir=Path("/s"), port=54321)
    url = cluster.url(cluster.migration_role)
    assert (url.host, url.port, url.username, url.database) == ("127.0.0.1", 54321, "hpldn_migration", "hpldn_test")
    monkeypatch.setenv("HPLDN_DB_URL", "postgresql://elsewhere/db")
    with cluster.pinned_db_url() as pinned:
        assert os.environ["HPLDN_DB_URL"] == pinned
        assert pinned.endswith("@127.0.0.1:54321/hpldn_test")
    assert os.environ["HPLDN_DB_URL"] == "postgresql://elsewhere/db"
    monkeypatch.delenv("HPLDN_DB_URL")
    with cluster.pinned_db_url():
        pass
    assert "HPLDN_DB_URL" not in os.environ


def test_psql_commands_target_the_cluster(monkeypatch):
    seen = []
    monkeypatch.setattr(_pg.subprocess, "run", lambda command, **kwargs: seen.append(command))
    cluster = _pg.PgCluster(bindir=Path("/pg/bin"), datadir=Path("/d"), socket_dir=Path("/s"), port=5999)
    cluster.psql("-c", "SELECT 1", role="hpldn_api")
    assert seen[0][:2] == ["/pg/bin/psql", "-X"]
    assert seen[0][seen[0].index("-p") + 1] == "5999"
    assert seen[0][seen[0].index("-U") + 1] == "hpldn_api"
    assert seen[0][seen[0].index("-d") + 1] == "hpldn_test"
