"""
An ephemeral PostgreSQL 18 cluster for the tests that must create or change a database.

`pg_cluster` (session scope) runs ``initdb`` into a temporary directory and
``pg_ctl start`` on a free TCP port on the loopback (its unix socket in a
short temporary directory), with ``trust`` authentication: nothing leaves
the machine and no password exists. The bootstrap superuser is ``postgres``,
because ``db_init.sql`` names it as the owner of ``staging``; the OS user
is created as a second superuser. ``db_init.sql``
is applied as on a real host, with a non-superuser migration role and the API
role, which is then given LOGIN and the shared role settings. The cluster is
stopped (``-m immediate``) and deleted when the session ends.

Durability is switched off (``fsync``, ``synchronous_commit``,
``full_page_writes``): the cluster lives for one test run.

Every test that uses it is marked ``pg`` and so runs only with
``HPLDN_TEST_DB_WRITES=1`` (see ``conftest.py``). A missing ``initdb``,
``pg_ctl`` or extension fails the test; it never skips.
"""

import getpass
import os
import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional

import pytest
from sqlalchemy import URL, Engine, create_engine

from hpldn.backend.db import SQL_DIR

SUPERUSER = "postgres"
MIGRATION_ROLE = "hpldn_migration"
API_ROLE = "hpldn_api"
DATABASE = "hpldn_test"

_SERVER_OPTIONS = (
    "-c fsync=off -c synchronous_commit=off -c full_page_writes=off "
    "-c listen_addresses=127.0.0.1 -c max_connections=50"
)


def find_pg_bindir() -> Path:
    """
    Return the directory holding ``initdb``, ``pg_ctl`` and ``psql``.

    ``HPLDN_PG_BINDIR`` wins; otherwise ``pg_config --bindir`` (Debian keeps
    ``initdb`` off ``PATH`` under ``/usr/lib/postgresql/<major>/bin``).

    Raises
    ------
    FileNotFoundError
        When no directory holds all three.
    """
    candidates = []
    if os.environ.get("HPLDN_PG_BINDIR"):
        candidates.append(Path(os.environ["HPLDN_PG_BINDIR"]))
    pg_config = shutil.which("pg_config")
    if pg_config:
        result = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            candidates.append(Path(result.stdout.strip()))
    for candidate in candidates:
        if all((candidate / tool).is_file() for tool in ("initdb", "pg_ctl", "psql")):
            return candidate
    raise FileNotFoundError(
        "initdb/pg_ctl/psql not found (tried HPLDN_PG_BINDIR and pg_config --bindir: "
        f"{[str(c) for c in candidates]}); install the PostgreSQL 18 server package"
    )


def free_port() -> int:
    """Return a TCP port on 127.0.0.1 that was free a moment ago."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class PgCluster:
    """A running throwaway cluster and the three roles the code under test uses."""

    bindir: Path
    datadir: Path
    socket_dir: Path
    port: int
    database: str = DATABASE
    superuser: str = SUPERUSER
    migration_role: str = MIGRATION_ROLE
    api_role: str = API_ROLE
    _engines: Dict[str, Engine] = field(default_factory=dict, repr=False)

    def url(self, role: str, database: Optional[str] = None, drivername: str = "postgresql+psycopg2") -> URL:
        """
        Return a URL for `role` over TCP on the loopback (no password: ``trust``).

        TCP rather than the socket directory, so the URL survives a round
        trip through ``HPLDN_DB_URL`` and ``psql``/``pg_dump`` arguments.
        """
        return URL.create(
            drivername=drivername,
            username=role,
            host="127.0.0.1",
            port=self.port,
            database=database or self.database,
        )

    def engine(self, role: str) -> Engine:
        """Return a cached engine for `role` on `database`."""
        if role not in self._engines:
            self._engines[role] = create_engine(self.url(role))
        return self._engines[role]

    @property
    def superuser_engine(self) -> Engine:
        return self.engine(self.superuser)

    @property
    def migration_engine(self) -> Engine:
        return self.engine(self.migration_role)

    @property
    def api_engine(self) -> Engine:
        return self.engine(self.api_role)

    def psql(self, *args: str, role: Optional[str] = None, database: Optional[str] = None, **kwargs) -> subprocess.CompletedProcess:
        """Run ``psql`` against the cluster with ``ON_ERROR_STOP``; raise on failure."""
        command = [
            str(self.bindir / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1",
            "-h", "127.0.0.1", "-p", str(self.port),
            "-U", role or self.superuser, "-d", database or self.database, *args,
        ]
        return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)

    @contextmanager
    def pinned_db_url(self, role: Optional[str] = None) -> Iterator[str]:
        """Point ``HPLDN_DB_URL`` (what ``migrations/env.py`` and the pipeline read) at this cluster."""
        url = self.url(role or self.migration_role).render_as_string(hide_password=False)
        saved = os.environ.get("HPLDN_DB_URL")
        os.environ["HPLDN_DB_URL"] = url
        try:
            yield url
        finally:
            if saved is None:
                os.environ.pop("HPLDN_DB_URL", None)
            else:
                os.environ["HPLDN_DB_URL"] = saved

    def alembic_upgrade(self, revision: str) -> None:
        """Run ``alembic upgrade <revision>`` as the migration role, as ``bare_metal`` does."""
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with self.pinned_db_url():
            command.upgrade(cfg, revision)

    def dispose(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()


def start_cluster(base: Path) -> PgCluster:
    """
    ``initdb`` and start a cluster under `base`, bootstrap it with ``db_init.sql``, and return it.

    Raises
    ------
    FileNotFoundError
        When the PostgreSQL binaries are missing.
    subprocess.CalledProcessError
        When ``initdb``, ``pg_ctl`` or ``db_init.sql`` fails (``stderr`` attached).
    """
    bindir = find_pg_bindir()
    datadir = base / "data"
    # Unix socket paths are limited to ~107 bytes; pytest's tmp dirs can exceed it.
    socket_dir = Path(tempfile.mkdtemp(prefix="hpldn-pg-"))
    port = free_port()
    subprocess.run(
        [
            str(bindir / "initdb"), "-D", str(datadir), "-U", SUPERUSER, "-A", "trust",
            "--encoding=UTF8", "--locale=C.UTF-8", "--no-instructions", "--no-sync",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(bindir / "pg_ctl"), "-D", str(datadir), "-l", str(base / "server.log"), "-w", "-t", "60",
            "-o", f"-p {port} -k {socket_dir} {_SERVER_OPTIONS}", "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    cluster = PgCluster(bindir=bindir, datadir=datadir, socket_dir=socket_dir, port=port)
    os_user = getpass.getuser()
    if os_user != SUPERUSER:
        cluster.psql("-c", f'CREATE ROLE "{os_user}" SUPERUSER LOGIN', database="postgres")
    cluster.psql(
        "-v", f"migration_role={MIGRATION_ROLE}",
        "-v", f"db_name={DATABASE}",
        "-v", f"api_role={API_ROLE}",
        "-f", str(SQL_DIR / "db_init.sql"),
        database="postgres",
    )
    # db_init.sql leaves the API role a NOLOGIN stub; give it LOGIN (trust: no
    # password) and the settings the serving host's role has.
    cluster.psql("-c", f'ALTER ROLE "{API_ROLE}" LOGIN')
    cluster.psql("-v", f"api_role={API_ROLE}", "-f", str(SQL_DIR / "fragments" / "api_role_settings.sql"))
    return cluster


def stop_cluster(cluster: PgCluster) -> None:
    """Stop the cluster at once and delete its socket directory."""
    cluster.dispose()
    subprocess.run(
        [str(cluster.bindir / "pg_ctl"), "-D", str(cluster.datadir), "-m", "immediate", "-w", "stop"],
        capture_output=True,
        text=True,
    )
    shutil.rmtree(cluster.socket_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def pg_cluster(tmp_path_factory) -> Iterator[PgCluster]:
    """Yield a bootstrapped, running `PgCluster`; see the module docstring."""
    base = tmp_path_factory.mktemp("pg")
    try:
        cluster = start_cluster(base)
    except FileNotFoundError as exc:
        pytest.fail(f"missing prerequisite: {exc}", pytrace=False)
    except subprocess.CalledProcessError as exc:
        log = base / "server.log"
        tail = log.read_text()[-2000:] if log.exists() else ""
        pytest.fail(
            f"the ephemeral cluster did not start: {' '.join(map(str, exc.cmd))}\n"
            f"{exc.stderr}\n{tail}",
            pytrace=False,
        )
    try:
        yield cluster
    finally:
        stop_cluster(cluster)
