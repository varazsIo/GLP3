"""
`common.db`'s URL resolution, psql helpers and COPY column lists: no database is reached.

Every test points ``HPLDN_DIR_FP`` at an empty ``develop.env`` and clears the
``HPLDN_DB_URL`` / ``POSTGRES_*`` variables first, so the developer's own
configuration never leaks into the result.
"""

import logging

import pytest
from sqlalchemy import Engine, create_engine, make_url

from hpldn.backend.common import db
from hpldn.backend.models.staging import UKHPUCL

DB_VARIABLES = (
    "HPLDN_DB_URL",
    "SQLALCHEMY_ENGINE",
    *(
        f"{prefix}_{field}"
        for prefix in (db.DEVELOP_DB_ENV_PREFIX, db.TESTING_DB_ENV_PREFIX)
        for field in ("USERNAME", "PASSWORD", "HOST", "PORT", "DATABASE")
    ),
)
RESOLVERS = (db.get_migration_db_url, db.get_model_db_url)


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """An empty develop.env and no database variable in the environment."""
    (tmp_path / "develop.env").write_text("# no database configured\n", encoding="utf-8")
    monkeypatch.setenv("HPLDN_DIR_FP", str(tmp_path))
    monkeypatch.delenv("HPLDN_ENV_OVERRIDE", raising=False)
    for name in DB_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SQLALCHEMY_ENGINE", "postgresql")
    return monkeypatch


def _set_prefix(monkeypatch, prefix, database, port="5433"):
    monkeypatch.setenv(f"{prefix}_USERNAME", "smith")
    monkeypatch.setenv(f"{prefix}_PASSWORD", "pw-0123")  # gitleaks:allow (test fixture)
    monkeypatch.setenv(f"{prefix}_HOST", "db.internal")
    monkeypatch.setenv(f"{prefix}_DATABASE", database)
    if port is None:
        monkeypatch.delenv(f"{prefix}_PORT", raising=False)
    else:
        monkeypatch.setenv(f"{prefix}_PORT", port)


# -- get_db_url --------------------------------------------------------


def test_db_url_prefers_hpldn_db_url(clean_env, caplog):
    clean_env.setenv("HPLDN_DB_URL", "postgresql://u@h:5432/chosen")
    _set_prefix(clean_env, db.TESTING_DB_ENV_PREFIX, "ignored")
    with caplog.at_level(logging.DEBUG):
        url = db.get_db_url(logging.getLogger("test_db_urls"))
    assert url.database == "chosen"
    assert "HPLDN_DB_URL" in caplog.text


def test_db_url_falls_back_to_the_testing_variables(clean_env):
    _set_prefix(clean_env, db.TESTING_DB_ENV_PREFIX, "testing_db")
    url = db.get_db_url()
    assert (url.drivername, url.username, url.host, url.port, url.database) == (
        "postgresql",
        "smith",
        "db.internal",
        5433,
        "testing_db",
    )


def test_db_url_leaves_the_port_unset_when_it_is_missing(clean_env):
    _set_prefix(clean_env, db.TESTING_DB_ENV_PREFIX, "testing_db", port=None)
    assert db.get_db_url().port is None


def test_sql_dev_engine_is_built_without_connecting(clean_env):
    clean_env.setenv("HPLDN_DB_URL", "postgresql://u@127.0.0.1:1/never_opened")
    engine = db.get_sql_dev_engine()
    try:
        assert isinstance(engine, Engine)
        assert engine.url.database == "never_opened"
    finally:
        engine.dispose()


# -- get_migration_db_url / get_model_db_url ---------------------------


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_resolvers_prefer_hpldn_db_url(clean_env, caplog, resolve):
    clean_env.setenv("HPLDN_DB_URL", "postgresql://u@h/explicit")
    _set_prefix(clean_env, db.DEVELOP_DB_ENV_PREFIX, "hpdbldn")
    with caplog.at_level(logging.DEBUG):
        url = resolve(logging.getLogger("test_db_urls"))
    assert url.database == "explicit"
    assert "HPLDN_DB_URL" in caplog.text


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_resolvers_prefer_develop_over_testing(clean_env, caplog, resolve):
    _set_prefix(clean_env, db.DEVELOP_DB_ENV_PREFIX, "hpdbldn")
    _set_prefix(clean_env, db.TESTING_DB_ENV_PREFIX, "fallback")
    with caplog.at_level(logging.DEBUG):
        url = resolve(logging.getLogger("test_db_urls"))
    assert url.database == "hpdbldn"
    assert db.DEVELOP_DB_ENV_PREFIX in caplog.text


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_resolvers_skip_an_incomplete_develop_prefix(clean_env, resolve):
    """A develop prefix without a host falls through rather than building a URL to the wrong server."""
    _set_prefix(clean_env, db.DEVELOP_DB_ENV_PREFIX, "hpdbldn")
    clean_env.delenv(f"{db.DEVELOP_DB_ENV_PREFIX}_HOST")
    _set_prefix(clean_env, db.TESTING_DB_ENV_PREFIX, "fallback", port=None)
    url = resolve()
    assert url.database == "fallback"
    assert url.port is None


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_resolvers_refuse_when_nothing_resolves(clean_env, resolve):
    with pytest.raises(RuntimeError, match="No database URL could be resolved"):
        resolve()


def test_url_from_env_prefix_needs_username_host_and_database(clean_env):
    assert db._url_from_env_prefix(db.DEVELOP_DB_ENV_PREFIX) is None
    _set_prefix(clean_env, db.DEVELOP_DB_ENV_PREFIX, "hpdbldn")
    url = db._url_from_env_prefix(db.DEVELOP_DB_ENV_PREFIX)
    assert url is not None and url.password == "pw-0123"


# -- psql helpers ------------------------------------------------------

URL_WITH_PASSWORD = "postgresql+psycopg://smith:s3cret-0123@db.internal:5433/hpdbldn"  # gitleaks:allow (test fixture)


@pytest.mark.parametrize(
    "as_given",
    [
        lambda: URL_WITH_PASSWORD,
        lambda: make_url(URL_WITH_PASSWORD),
        lambda: create_engine(URL_WITH_PASSWORD),
    ],
    ids=["str", "URL", "Engine"],
)
def test_psql_url_drops_the_password_and_the_driver(as_given):
    rendered = db.get_psql_url(as_given())
    assert rendered == "postgresql://smith@db.internal:5433/hpdbldn"
    assert "s3cret" not in rendered


def test_psql_env_carries_the_password():
    env = db.get_psql_env(URL_WITH_PASSWORD, env={"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "PGPASSWORD": "s3cret-0123"}  # gitleaks:allow (test fixture)


def test_psql_env_copies_and_adds_nothing_without_a_password(monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    base = {"PATH": "/usr/bin"}
    env = db.get_psql_env("postgresql://smith@db.internal/hpdbldn", env=base)
    assert env == base and env is not base
    assert "PGPASSWORD" not in db.get_psql_env("postgresql://smith@db.internal/hpdbldn")


# -- COPY column lists -------------------------------------------------


def test_non_string_columns_are_numeric_or_dates_and_quoted_when_upper_case():
    columns = db.get_non_string_columns(UKHPUCL)
    assert "id" not in columns
    assert "price" in columns and '"UPRN"' in columns and '"LODGEMENT_DATETIME"' in columns
    assert "postcode" not in columns
    assert all(c.startswith('"') == c.strip('"').isupper() for c in columns)


def test_formatted_column_names_cover_every_column_but_id():
    names = db.get_formatted_column_names(UKHPUCL)
    expected = [c.name for c in UKHPUCL.__table__.columns if c.name != "id"]
    assert [n.strip('"') for n in names] == expected
    assert '"UPRN"' in names and "postcode" in names


def test_force_null_lists_the_non_string_columns():
    clause = db.get_force_null_str()
    assert clause == f"FORCE_NULL ({', '.join(db.get_non_string_columns(UKHPUCL))})"
