"""
Authentication on the read API's data routes.

Includes the non-ASCII key case: comparing ``str`` values with
`secrets.compare_digest` raises for non-ASCII input, which must not turn an
attacker-controlled header into a 500 instead of a 401.
"""

from datetime import date

import pytest

from hpldn.backend.api import config, deps
from hpldn.backend.api.main import app
from hpldn.backend.api.market import cache as market_cache
from hpldn.backend.api.schemas import DataWindow

DATA_ROUTES = (
    "/properties",
    "/properties/100023336956",
    "/properties/100023336956/transactions",
)


@pytest.mark.parametrize("route", DATA_ROUTES)
def test_missing_key_is_rejected(client, route):
    assert client.get(route).status_code == 401


@pytest.mark.parametrize("route", DATA_ROUTES)
def test_wrong_key_is_rejected(client, route):
    response = client.get(route, headers={"X-API-Key": "not-the-key"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "key",
    [
        "café",              
        "ключ",              
        "🔑",                
        "test-api-keyé",     
    ],
)
def test_non_ascii_key_is_a_401_not_a_500(client, key):
    """
    Check a non-ASCII key is rejected with 401 rather than raising.

    Sent as raw bytes, as a real header is; Starlette decodes it as latin-1,
    so `require_api_key` receives non-ASCII text either way.
    """
    response = client.get(
        "/properties", headers={"X-API-Key": key.encode("utf-8")}
    )
    assert response.status_code == 401, response.text


def test_correct_key_is_accepted(client, auth):
    assert client.get("/properties", headers=auth).status_code == 200


def test_empty_server_side_key_fails_closed(client, monkeypatch):
    """Check that no configured key rejects everything rather than accepting everything."""
    blank = config.ApiSettings(
        db_host="127.0.0.1", db_port=6432, db_name="postgres",
        db_user="hpldn_api", db_password="", api_key="",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: blank)
    assert client.get("/properties", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/properties", headers={"X-API-Key": "x"}).status_code == 401


def test_health_is_unauthenticated(client):
    """Check load-balancer health checks need no key."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_is_unauthenticated_and_touches_the_database(client, monkeypatch):
    loads = []

    async def load_snapshot(session):
        loads.append(session)
        return market_cache.MarketSnapshot(
            window=DataWindow(
                from_year=2023, to_year=2026, latest_sale=date(2026, 7, 31), total_sales=1, first_sale_year=1995,
            ),
            districts={},
        )

    monkeypatch.setattr(market_cache, "load_snapshot", load_snapshot)
    fresh = market_cache.MarketCache(ttl_s=3600)
    app.dependency_overrides[market_cache.get_market_cache] = lambda: fresh
    try:
        response = client.get("/ready")
    finally:
        app.dependency_overrides.pop(market_cache.get_market_cache, None)
    # Ready means the market figures are loaded too.
    assert len(loads) == 1 and fresh.loaded
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
