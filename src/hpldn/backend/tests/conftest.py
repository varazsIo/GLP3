"""
Root fixtures and rules for the backend test suite.

Three rules hold for every test under this directory:

* **A skip is a failure.** A test that skips, an ``xfail`` that is not
  ``strict``, or a module that skips at collection fails the run and names the
  missing prerequisite (`pytest_runtest_makereport`,
  `pytest_make_collect_report`). The suite either proves something or says
  why it could not.
* **Database writes are opt-in.** A test marked ``pg`` (the ephemeral
  cluster, `tests._pg.pg_cluster`) or ``db_write`` (anything that creates,
  updates or deletes, even inside a rolled-back transaction) is skipped
  unless ``HPLDN_TEST_DB_WRITES=1``; that skip is the only one allowed.
  ``scripts/ci/run_all.sh`` sets it.
* **No test signs with a real key.** Model bundles are signed with a
  throwaway key and checked against a trusted directory holding it and the
  tracked ``trusted_keys/`` (`model_signing_keys`, autouse);
  ``HPLDN_ALLOW_UNSIGNED_MODEL`` and ``HPLDN_ENV`` are cleared.
* **The real database is read-only.** `hpdbldn_ro` is the one way to read
  hpdbldn: every session it opens is ``default_transaction_read_only``.
  `hpdbldn_rollback` is the savepoint-and-rollback harness for checks that
  must write, and is ``db_write``.

Tests that use `hpdbldn_ro`, `hpdbldn_rollback` or `pg_cluster` are marked
``hpdbldn`` / ``pg`` automatically, so ``-m "not pg and not hpdbldn"`` is the
hermetic unit stage.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, text

from hpldn.backend.tests._pg import pg_cluster  # noqa: F401 - re-exported fixture

REPO_ROOT = Path(__file__).resolve().parents[4]

DB_WRITES_ENV = "HPLDN_TEST_DB_WRITES"
DB_WRITE_MARKERS = ("pg", "db_write")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"

# Paths the suite needs, derived from this checkout unless the caller set
# them (pytest-env in pyproject.toml cannot express a path relative to the
# rootdir). A fresh clone has no configuration/staging.yaml; the tracked
# template stands in.
os.environ.setdefault("HPLDN_DIR_FP", str(REPO_ROOT))
os.environ.setdefault("HPLDN_LOG_FP", str(REPO_ROOT / "tmp" / "logs" / "tests.log"))
if "HPLDN_CONFIG_FP" not in os.environ and not (REPO_ROOT / "configuration" / "staging.yaml").is_file():
    os.environ["HPLDN_CONFIG_FP"] = str(REPO_ROOT / "src" / "hpldn" / "backend" / "config" / "staging.example.yaml")


def db_writes_enabled() -> bool:
    """True when ``HPLDN_TEST_DB_WRITES=1`` opts into the tests that write to a database."""
    return os.environ.get(DB_WRITES_ENV) == "1"


def _policy_skipped(item: pytest.Item) -> bool:
    return not db_writes_enabled() and any(item.get_closest_marker(m) for m in DB_WRITE_MARKERS)


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Mark tests by the fixtures they use, then skip the database writers unless opted in."""
    for item in items:
        names = set(getattr(item, "fixturenames", ()))
        if "pg_cluster" in names:
            item.add_marker(pytest.mark.pg)
        if names & {"hpdbldn_ro", "hpdbldn_url", "hpdbldn_rollback"}:
            item.add_marker(pytest.mark.hpdbldn)
        if "hpdbldn_rollback" in names:
            item.add_marker(pytest.mark.db_write)
        if _policy_skipped(item):
            item.add_marker(
                pytest.mark.skip(
                    reason=f"db-write policy: writes to a database; set {DB_WRITES_ENV}=1 to run it"
                )
            )


def _skip_reason(report) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """Turn every skip and every non-strict xfail into a failure, except the db-write policy's."""
    outcome = yield
    report = outcome.get_result()
    if not report.skipped:
        return
    if hasattr(report, "wasxfail"):
        marker = item.get_closest_marker("xfail")
        strict = marker.kwargs.get("strict", item.config.getini("xfail_strict")) if marker else False
        if not strict:
            reason = report.wasxfail
            # pytest does not count a failed report that still says wasxfail.
            del report.wasxfail
            report.outcome = "failed"
            report.longrepr = (
                f"non-strict xfail is not allowed (reason: {reason}); make it "
                "strict=True so the fix is noticed, or fix the test"
            )
        return
    if _policy_skipped(item):
        return
    report.outcome = "failed"
    report.longrepr = f"skipped, which this suite treats as a failure -- missing prerequisite: {_skip_reason(report)}"


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Fail a module that skips itself at collection (``allow_module_level`` / ``importorskip``)."""
    outcome = yield
    report = outcome.get_result()
    if report.skipped:
        report.outcome = "failed"
        report.longrepr = f"collection skipped, which this suite treats as a failure -- missing prerequisite: {_skip_reason(report)}"


# ---------------------------------------------------------------------------
# Model bundles: a throwaway signing key
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _session_signing_keys(tmp_path_factory):
    from hpldn.backend.tests._bundles import make_signing_keys

    return make_signing_keys(tmp_path_factory.mktemp("model-keys"))


@pytest.fixture(autouse=True)
def model_signing_keys(_session_signing_keys, monkeypatch):
    """Point `models.artifact` at the session's throwaway key and its trusted directory."""
    from hpldn.backend.models import artifact

    monkeypatch.setattr(artifact, "TRUSTED_KEYS_DIR", _session_signing_keys.trusted_dir)
    monkeypatch.setenv(artifact.SIGNING_KEY_ENV, str(_session_signing_keys.key))
    monkeypatch.delenv(artifact.ALLOW_UNSIGNED_ENV, raising=False)
    monkeypatch.delenv(artifact.ENVIRONMENT_ENV, raising=False)
    return _session_signing_keys


# ---------------------------------------------------------------------------
# hpdbldn: the real, migrated database, read-only
# ---------------------------------------------------------------------------


def missing_prerequisite(what: str) -> None:
    """Fail the test, naming what this machine lacks."""
    pytest.fail(f"missing prerequisite: {what}", pytrace=False)


@pytest.fixture(scope="session")
def hpdbldn_url() -> URL:
    """
    Return the URL of the local hpdbldn database (``POSTGRES_DEVELOP_*`` in ``develop.env``).

    Fails when it is unset or not on this machine: these tests must never
    reach another host.
    """
    from hpldn.backend.common.utils import load_env_vars

    load_env_vars()
    host = os.environ.get("POSTGRES_DEVELOP_HOST")
    if host not in LOCAL_HOSTS:
        missing_prerequisite(f"POSTGRES_DEVELOP_HOST must name this machine (is {host!r}); see develop.env")
    return URL.create(
        drivername="postgresql+psycopg2",
        username=os.environ.get("POSTGRES_DEVELOP_USERNAME"),
        password=os.environ.get("POSTGRES_DEVELOP_PASSWORD"),
        host=host,
        port=int(os.environ.get("POSTGRES_DEVELOP_PORT", "5432")),
        database=os.environ.get("POSTGRES_DEVELOP_DATABASE", "hpdbldn"),
    )


@pytest.fixture(scope="session")
def hpdbldn_ro(hpdbldn_url):
    """
    Yield an engine on hpdbldn whose every session is ``default_transaction_read_only``.

    Fails when the database is unreachable or has no ``production.london_houses``.
    """
    engine = create_engine(
        hpdbldn_url,
        connect_args={"connect_timeout": 3, "options": READ_ONLY_OPTIONS},
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM production.london_houses LIMIT 1"))
            assert connection.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
    except Exception as exc:  # noqa: BLE001 - any failure here is a missing prerequisite
        engine.dispose()
        missing_prerequisite(f"hpdbldn with production.london_houses at {hpdbldn_url.render_as_string()}: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def hpdbldn_rollback(hpdbldn_url):
    """
    Yield a connection to hpdbldn inside a transaction that is always rolled back.

    The savepoint-and-rollback harness for revision SQL and constraint checks:
    whatever a module creates or changes never commits. One transaction per
    module, so a test that expects an error wraps it in ``begin_nested()``.
    Marked ``db_write``.
    """
    engine = create_engine(hpdbldn_url, connect_args={"connect_timeout": 3})
    try:
        connection = engine.connect()
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        missing_prerequisite(f"hpdbldn at {hpdbldn_url.render_as_string()}: {exc}")
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
