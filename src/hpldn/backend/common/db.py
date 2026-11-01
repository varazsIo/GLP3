"""Engine, URL and column-statistics helpers for the HPLDN PostgreSQL database."""
import logging 
import os
from sqlalchemy import text
from sqlalchemy import URL, Engine
from sqlalchemy import create_engine, make_url
from sqlalchemy import Integer, BigInteger, Float, DateTime
from hpldn.backend.models.staging import AppBase, UKHPUCL
from hpldn.backend.models.staging import app_base_meta
from hpldn.backend.common.utils import load_env_vars
from typing import Union

def create_db(engine:Engine, log:Union[None,logging.Logger]=None):
    """
    Create the application schema if missing, then every `AppBase` table.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Target database.
    log : logging.Logger, optional
        Logger for progress messages.
    """
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :name"),
            {"name": app_base_meta.schema},
        ).scalar()
        if not exists:
            connection.execute(text(f"CREATE SCHEMA {app_base_meta.schema}"))
    AppBase.metadata.create_all(engine)
    if log is not None:
        log.debug("Created all database tables!")

def drop_db(engine:Engine, log:Union[None,logging.Logger]=None):
    """
    Drop every `AppBase` table.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Target database.
    log : logging.Logger, optional
        Logger for progress messages.
    """
    AppBase.metadata.drop_all(engine)
    if log is not None:
        log.debug("All tabeles deleted from database!")

def get_db_url(log:Union[None,logging.Logger]=None):
    """
    Return the URL of the testing database.

    Parameters
    ----------
    log : logging.Logger, optional
        Logger for progress messages.

    Returns
    -------
    sqlalchemy.URL
        ``HPLDN_DB_URL`` when set, so every entry point targets exactly the
        database the caller chose; otherwise built from ``POSTGRES_TESTING_*``.
    """
    load_env_vars(log)
    db_url = os.environ.get("HPLDN_DB_URL")
    if db_url:
        if log is not None:
            log.debug("Using database URL from HPLDN_DB_URL")
        return make_url(db_url)
    port = os.environ.get("POSTGRES_TESTING_PORT")
    url_object = URL.create(
        drivername=os.environ.get("SQLALCHEMY_ENGINE"),
        username=os.environ.get("POSTGRES_TESTING_USERNAME"),
        password=os.environ.get("POSTGRES_TESTING_PASSWORD"),
        host=os.environ.get("POSTGRES_TESTING_HOST"),
        port=int(port) if port else None,
        database=os.environ.get("POSTGRES_TESTING_DATABASE"),
    )
    return url_object

DEVELOP_DB_ENV_PREFIX = "POSTGRES_DEVELOP"
TESTING_DB_ENV_PREFIX = "POSTGRES_TESTING"

def _url_from_env_prefix(prefix:str) -> Union[URL,None]:
    """
    Build a URL from the ``<prefix>_*`` variables.

    Returns None when username, host or database is missing, so callers fall
    through to the next prefix instead of building a valid URL to the wrong
    server.
    """
    username = os.environ.get(f"{prefix}_USERNAME")
    host = os.environ.get(f"{prefix}_HOST")
    database = os.environ.get(f"{prefix}_DATABASE")
    if not (username and host and database):
        return None
    port = os.environ.get(f"{prefix}_PORT")
    return URL.create(
        drivername=os.environ.get("SQLALCHEMY_ENGINE"),
        username=username,
        password=os.environ.get(f"{prefix}_PASSWORD"),
        host=host,
        port=int(port) if port else None,
        database=database,
    )

def get_migration_db_url(log:Union[None,logging.Logger]=None):
    """
    Return the URL of the database the alembic chain builds.

    Parameters
    ----------
    log : logging.Logger, optional
        Logger for progress messages.

    Returns
    -------
    sqlalchemy.URL
        The first of ``HPLDN_DB_URL``, ``POSTGRES_DEVELOP_*`` (hpdbldn, the
        from-empty target) and ``POSTGRES_TESTING_*`` (a fallback database)
        that resolves.

    Raises
    ------
    RuntimeError
        If none of them resolves.

    Notes
    -----
    A database populated before the chain is never rebuilt (it is
    ``alembic stamp``ed), so the chain deliberately prefers hpdbldn:
    replaying it against such a database would drop
    ``production.london_houses``.
    """
    load_env_vars(log)
    db_url = os.environ.get("HPLDN_DB_URL")
    if db_url:
        if log is not None:
            log.debug("Using database URL from HPLDN_DB_URL")
        return make_url(db_url)
    for prefix in (DEVELOP_DB_ENV_PREFIX, TESTING_DB_ENV_PREFIX):
        url = _url_from_env_prefix(prefix)
        if url is not None:
            if log is not None:
                log.debug(f"Using database URL from {prefix}_* ({url.database})")
            return url
    raise RuntimeError(
        "No database URL could be resolved. Set HPLDN_DB_URL, or define "
        "POSTGRES_DEVELOP_USERNAME/_HOST/_DATABASE (hpdbldn) in develop.env."
    )

def get_model_db_url(log:Union[None,logging.Logger]=None):
    """
    Return the URL of the database the model scripts train and score on.

    Parameters
    ----------
    log : logging.Logger, optional
        Logger; the chosen database is logged at INFO.

    Returns
    -------
    sqlalchemy.URL
        The first of ``HPLDN_DB_URL``, ``POSTGRES_DEVELOP_*`` (hpdbldn, the
        database the read API serves) and ``POSTGRES_TESTING_*`` (the fallback
        database) that resolves, the same order as
        `get_migration_db_url`.

    Raises
    ------
    RuntimeError
        If none of them resolves.

    Notes
    -----
    `get_db_url` falls back to ``POSTGRES_TESTING_*`` directly. Training on
    that database while the API scores against hpdbldn gives a model whose
    features were built on a different schema and row set.
    """
    load_env_vars(log)
    db_url = os.environ.get("HPLDN_DB_URL")
    if db_url:
        url = make_url(db_url)
        if log is not None:
            log.info(f"Model database: {url.database} (from HPLDN_DB_URL)")
        return url
    for prefix in (DEVELOP_DB_ENV_PREFIX, TESTING_DB_ENV_PREFIX):
        url = _url_from_env_prefix(prefix)
        if url is not None:
            if log is not None:
                log.info(f"Model database: {url.database} (from {prefix}_*)")
            return url
    raise RuntimeError(
        "No database URL could be resolved for the model scripts. Set "
        "HPLDN_DB_URL, or define POSTGRES_DEVELOP_USERNAME/_HOST/_DATABASE "
        "(hpdbldn) in develop.env."
    )

def _as_url(url:Union[URL,Engine,str]) -> URL:
    """Coerce an Engine, string or URL to a `sqlalchemy.URL`."""
    if isinstance(url, Engine):
        return url.url
    if isinstance(url, str):
        return make_url(url)
    return url

def get_psql_url(url:Union[URL,Engine,str]):
    """
    Render a database URL for the psql CLI, without the password.

    Parameters
    ----------
    url : sqlalchemy.URL, sqlalchemy.Engine or str
        The database URL; a driver-qualified scheme is rewritten to
        ``postgresql://``.

    Returns
    -------
    str
        A URL safe to pass as a psql argument and to appear in a process list.

    See Also
    --------
    get_psql_env : Supplies the password through ``PGPASSWORD`` instead.
    """
    url = _as_url(url)
    # Not url.set(password=None): `URL.set` ignores None arguments, so the
    # password would stay in the rendered URL (and in the process list).
    return URL.create(
        drivername="postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)

def get_psql_env(
    url:Union[URL,Engine,str], env:Union[None,dict]=None
) -> dict:
    """
    Return the environment a psql subprocess needs to authenticate.

    Parameters
    ----------
    url : sqlalchemy.URL, sqlalchemy.Engine or str
        The database URL.
    env : dict, optional
        Base environment to copy; `os.environ` by default.

    Returns
    -------
    dict
        A copy of `env` with ``PGPASSWORD`` set when the URL has a password.
        Unlike argv, a process environment is not world-readable.
    """
    url = _as_url(url)
    env = dict(os.environ if env is None else env)
    if url.password:
        env["PGPASSWORD"] = url.password
    return env

def get_sql_dev_engine(log:Union[None,logging.Logger]=None):
    """Return an engine for the database `get_db_url` resolves."""
    engine = create_engine(get_db_url(log))
    return engine

def get_non_string_columns(model:UKHPUCL):
    """
    Return the model's integer, float and datetime column names, excluding ``id``.

    Upper-case names are double-quoted for use in SQL.
    """
    non_string_cols = []
    filtered_columns = [c for c in model.__table__.columns if c.name != 'id']
    for column in filtered_columns:
        if isinstance(column.type, (Integer, BigInteger, Float, DateTime)):
            if str(column.name).isupper():
                non_string_cols.append(f"\"{column.name}\"")
            else:
                non_string_cols.append(column.name)
    return non_string_cols

def get_formatted_column_names(model:UKHPUCL):
    """Return the model's column names excluding ``id``, upper-case names double-quoted."""
    filtered_columns = [c for c in model.__table__.columns if c.name != 'id']
    formatted_columns = list()
    for column in filtered_columns:
        if str(column.name).isupper():
            formatted_columns.append(f"\"{column.name}\"")
        else:
            formatted_columns.append(column.name)
    return formatted_columns

def get_force_null_str():
    """Return the ``FORCE_NULL (...)`` COPY option for the non-string `UKHPUCL` columns."""
    columns_to_null = get_non_string_columns(UKHPUCL)
    force_null_str = f"FORCE_NULL ({', '.join(columns_to_null)})"
    return force_null_str
