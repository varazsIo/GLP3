"""
Fixtures for the read API tests.

The tests run against an in-memory SQLite database built from the ORM
metadata, so they need no PostgreSQL or network. The reCAPTCHA gate is on,
as in production (``guard.yaml``'s ``captcha.enabled``): `client` carries a
real pass issued with `guard.captcha.issue_pass` for the test client's
address, and `client_without_pass` does not, for the gate's own tests
(``test_session.py``). They cover
routing, authentication, serialisation and the response-schema contract;
PostgreSQL query plans and connection settings belong to the integration
suites.
"""

import os
from datetime import date, datetime

import pytest

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import CheckConstraint, MetaData, event, insert  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from hpldn.backend.api import config, deps, ratelimit  # noqa: E402
from hpldn.backend.api.deps import PASS_HEADER  # noqa: E402
from hpldn.backend.api.main import SKIP_STARTUP_CHECK_ENV, app  # noqa: E402
from hpldn.backend.guard.captcha import issue_pass  # noqa: E402
from hpldn.backend.models.production import (  # noqa: E402
    EPC,
    PPD,
    PRODUCTION_SCHEMA,
    Address,
    UprnLookup,
)

API_TABLES = [
    Address.__table__,
    PPD.__table__,
    EPC.__table__,
    UprnLookup.__table__,
]


def _sqlite_copies(tables):
    """
    Return copies of `tables` that SQLite can create.

    CHECK constraints (written in PostgreSQL syntax) and indexes (one uses
    ``DESC NULLS LAST``) are dropped. Only DDL is affected; inserts and queries
    use the original tables.
    """
    metadata = MetaData()
    copies = []
    for table in tables:
        copy = table.to_metadata(metadata)
        for constraint in [c for c in copy.constraints if isinstance(c, CheckConstraint)]:
            copy.constraints.discard(constraint)
        copy.indexes.clear()
        copies.append(copy)
    return metadata, copies


API_KEY = "test-api-key"
PASS_SECRET = "0123456789abcdef" * 4  # gitleaks:allow (public fingerprint or test fixture)
RECAPTCHA_SECRET_KEY = "test-recaptcha-secret"
# Starlette's TestClient connects as this host; ratelimit.client_key keys on it.
TEST_CLIENT_HOST = "testclient"

UPRN = 100_023_336_956
ADDRESS = {
    "postnumber": "12",
    "poststreet": "DOWNING STREET",
    "posttown": "LONDON",
    "postcode": "SW1A 2AA",
    "postcounty": "GREATER LONDON",
}


@pytest.fixture(autouse=True)
def _fresh_limiters():
    """Start every test with full per-client buckets on the form, price and session routes."""
    for limiter in (
        ratelimit.get_price_limiter(),
        ratelimit.get_form_limiter(),
        ratelimit.get_session_limiter(),
        ratelimit.get_read_limiter(),
    ):
        limiter.clear()
    yield


@pytest.fixture(scope="module")
def api_settings():
    """
    Return `ApiSettings` with a known API key and the reCAPTCHA gate on.

    Placeholder ``POSTGRES_API_*`` values and test reCAPTCHA keys are set in
    the environment, winning over ``develop.env``, and the startup database
    check is skipped. ``HPLDN_CAPTCHA_DISABLED`` is removed.
    """
    overrides = {
        "HPLDN_API_KEY": API_KEY,
        "POSTGRES_API_PASSWORD": "not-used-by-the-hermetic-tests",
        "POSTGRES_API_DATABASE": "not-used-by-the-hermetic-tests",
        "RECAPTCHA_SITE_KEY": "test-site-key",
        "RECAPTCHA_SECRET_KEY": RECAPTCHA_SECRET_KEY,
        "HPLDN_PASS_SECRET": PASS_SECRET,
        SKIP_STARTUP_CHECK_ENV: "1",
    }
    saved = {k: os.environ.get(k) for k in (*overrides, config.CAPTCHA_DISABLED_ENV)}
    os.environ.pop(config.CAPTCHA_DISABLED_ENV, None)
    os.environ.update(overrides)
    config.get_settings.cache_clear()
    settings = config.get_settings()
    assert settings.captcha_enabled, "guard.yaml's captcha.enabled is off; the gate tests need it on"
    yield settings
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    config.get_settings.cache_clear()


@pytest.fixture(scope="module")
def fixture_database():
    """
    Return an in-memory SQLite database holding one fully populated property.

    The ``production`` schema is ATTACHed as a second in-memory database so
    qualified names resolve, `StaticPool` keeps every checkout on that one
    connection, and PostgreSQL-only DDL is dropped (see `_sqlite_copies`).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _attach_schema(dbapi_connection, _record):
        dbapi_connection.execute(
            f"ATTACH DATABASE ':memory:' AS {PRODUCTION_SCHEMA}"
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _build():
        async with engine.begin() as connection:
            metadata, tables = _sqlite_copies(API_TABLES)
            await connection.run_sync(
                lambda sync_conn: metadata.create_all(sync_conn, tables=tables)
            )
            await connection.execute(
                insert(Address.__table__).values(id=1, uid=1, **ADDRESS)
            )
            await connection.execute(
                insert(UprnLookup.__table__).values(uprn=UPRN, address_id=1)
            )
            await connection.execute(
                insert(PPD.__table__).values(
                    [
                        {
                            "id": 1,
                            "address_id": 1,
                            "price": 450_000,
                            "dateoftransfer": date(2015, 6, 1),
                            "propertytype": "T",
                            "duration": "F",
                        },
                        {
                            "id": 2,
                            "address_id": 1,
                            "price": 780_000,
                            "dateoftransfer": date(2021, 3, 12),
                            "propertytype": "T",
                            "duration": "F",
                        },
                    ]
                )
            )
            await connection.execute(
                insert(EPC.__table__).values(
                    [
                        {
                            "id": 1,
                            "address_id": 1,
                            "current_energy_rating": "E",
                            "total_floor_area": 88.0,
                            "inspection_date": date(2011, 2, 3),
                            "lodgement_datetime": datetime(2011, 2, 5, 9, 0),
                        },
                        {
                            "id": 2,
                            "address_id": 1,
                            "current_energy_rating": "C",
                            "total_floor_area": 104.5,
                            "inspection_date": date(2019, 8, 20),
                            "lodgement_datetime": datetime(2019, 8, 22, 9, 0),
                        },
                    ]
                )
            )

    import asyncio

    asyncio.run(_build())
    yield maker
    asyncio.run(engine.dispose())


def human_pass(ip: str = TEST_CLIENT_HOST, ttl: float = 3600) -> str:
    """Return a pass the API accepts from `ip`, as ``POST /session`` would issue it."""
    return issue_pass(ip, ttl, secret=PASS_SECRET)


@pytest.fixture(scope="module")
def _app_on_fixture_database(api_settings, fixture_database):
    """Point `get_session` at the fixture database for the module."""

    async def _session():
        async with fixture_database() as session:
            yield session

    app.dependency_overrides[deps.get_session] = _session
    yield app
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def client(_app_on_fixture_database):
    """Return a TestClient on the fixture database that carries a valid pass, like a page that passed the check."""
    with TestClient(_app_on_fixture_database, headers={PASS_HEADER: human_pass()}) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def client_without_pass(_app_on_fixture_database):
    """Return a TestClient on the fixture database with no pass, like a script."""
    with TestClient(_app_on_fixture_database) as test_client:
        yield test_client


@pytest.fixture
def auth():
    return {"X-API-Key": API_KEY}
