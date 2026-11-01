"""
The reCAPTCHA gate end to end: ``GET /session/config``, ``POST /session``, the gated routes, and ``GET /ready``.

Google is never called: `captcha.verify_token` is replaced by a fake that
returns each verdict, so the route's handling of every outcome is checked.
`client_without_pass` is a script with no pass; `client` is a page that
passed the check (see ``conftest.py``).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from hpldn.backend.api import deps
from hpldn.backend.api.deps import PASS_HEADER
from hpldn.backend.api.main import app
from hpldn.backend.api.routers import session as session_router
from hpldn.backend.guard import captcha
from hpldn.backend.guard.captcha import Verdict, Verification
from hpldn.backend.guard.config import Captcha
from hpldn.backend.tests.api.conftest import (
    PASS_SECRET,
    RECAPTCHA_SECRET_KEY,
    TEST_CLIENT_HOST,
    human_pass,
)

# Every route guard.yaml gates, each with an invalid request: the gate runs
# before validation, so a 403 means the gate refused and a 422 that it let
# the request through (without touching the fixture database's SQLite).
GATED = [
    ("get", "/addresses", {}),
    ("post", "/price", {"json": {}}),
    ("post", "/estimate", {"json": {}}),
    ("post", "/nearby", {"json": {}}),
    ("post", "/schools", {"json": {}}),
]


@pytest.fixture
def verdict(monkeypatch):
    """Make ``POST /session``'s Google check return the verdict the test sets; record its arguments."""
    state = {"result": Verification(Verdict.OK, "ok", 0.9), "calls": []}

    async def fake_verify(token, ip, *, secret, config, url):
        state["calls"].append({"token": token, "ip": ip, "secret": secret, "url": url})
        return state["result"]

    monkeypatch.setattr(session_router.captcha, "verify_token", fake_verify)
    return state


@pytest.mark.parametrize(("method", "path", "kwargs"), GATED, ids=[p for _, p, _ in GATED])
def test_a_gated_route_is_a_403_without_a_pass(client_without_pass, auth, method, path, kwargs):
    response = getattr(client_without_pass, method)(path, headers=auth, **kwargs)
    assert response.status_code == 403
    assert response.headers["X-HPLDN-Captcha"] == "required"
    assert response.headers["X-HPLDN-Reject"] == "captcha"
    assert response.json()["detail"] == deps.PASS_REQUIRED


@pytest.mark.parametrize(("method", "path", "kwargs"), GATED, ids=[p for _, p, _ in GATED])
def test_a_gated_route_lets_a_pass_through(client, auth, method, path, kwargs):
    response = getattr(client, method)(path, headers=auth, **kwargs)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "bad_pass",
    [
        human_pass(ip="198.51.100.77"),  # another address
        captcha.issue_pass(TEST_CLIENT_HOST, -1, secret=PASS_SECRET),  # expired
        captcha.issue_pass(TEST_CLIENT_HOST, 3600, secret="x" * 64),  # another server's
        "not-a-pass",
    ],
    ids=["other-address", "expired", "forged", "garbage"],
)
def test_a_bad_pass_is_a_403(client_without_pass, auth, bad_pass):
    response = client_without_pass.get("/addresses", headers={**auth, PASS_HEADER: bad_pass})
    assert response.status_code == 403


def test_open_routes_need_no_pass(client_without_pass, auth):
    for path in ("/health", "/session/config"):
        assert client_without_pass.get(path, headers=auth).status_code == 200


def test_address_search_needs_no_pass(client_without_pass, auth):
    # A first search must not wait for reCAPTCHA: past the (absent) gate, the
    # empty query is refused by validation, not by the gate.
    response = client_without_pass.get("/addresses/search", params={"q": ""}, headers=auth)
    assert response.status_code == 422, response.text
    assert "X-HPLDN-Captcha" not in response.headers


def test_session_config_tells_the_page_the_gate_is_on(client_without_pass, auth):
    response = client_without_pass.get("/session/config", headers=auth)
    assert response.json() == {"enabled": True, "siteKey": "test-site-key", "action": "session"}


def test_a_good_token_earns_a_pass_that_opens_the_gated_routes(client_without_pass, auth, verdict):
    response = client_without_pass.post("/session", json={"token": "good"}, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["expiresIn"] == 1800
    assert captcha.check_pass(body["pass"], TEST_CLIENT_HOST, secret=PASS_SECRET)
    assert verdict["calls"] == [
        {"token": "good", "ip": TEST_CLIENT_HOST, "secret": RECAPTCHA_SECRET_KEY, "url": captcha.SITEVERIFY_URL}
    ]
    opened = client_without_pass.get("/addresses", headers={**auth, PASS_HEADER: body["pass"]})
    assert opened.status_code == 422  # past the gate


@pytest.mark.parametrize("which", [Verdict.LOW_SCORE, Verdict.BROWSER_ERROR])
def test_a_refused_token_is_a_403_saying_why(client_without_pass, auth, verdict, which):
    verdict["result"] = Verification(which, "nope", 0.1)
    response = client_without_pass.post("/session", json={"token": "bad"}, headers=auth)
    assert response.status_code == 403
    assert response.headers["X-HPLDN-Captcha"] == str(which)
    assert response.headers["X-HPLDN-Reject"] == "captcha"
    assert "pass" not in response.json()


def test_google_down_fails_open_with_a_short_pass(client_without_pass, auth, verdict):
    verdict["result"] = Verification(Verdict.UNAVAILABLE, "down")
    response = client_without_pass.post("/session", json={"token": "t"}, headers=auth)
    assert response.status_code == 200
    assert response.json()["expiresIn"] == 300


def test_google_down_is_a_503_when_failing_closed(client_without_pass, auth, verdict):
    verdict["result"] = Verification(Verdict.UNAVAILABLE, "down")
    app.dependency_overrides[session_router.get_captcha_config] = lambda: Captcha(fail_open=False)
    try:
        response = client_without_pass.post("/session", json={"token": "t"}, headers=auth)
    finally:
        del app.dependency_overrides[session_router.get_captcha_config]
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"


def test_a_refused_secret_fails_closed_even_with_fail_open(client_without_pass, auth, verdict):
    verdict["result"] = Verification(Verdict.CONFIG_ERROR, "siteverify refused our secret")
    response = client_without_pass.post("/session", json={"token": "t"}, headers=auth)
    assert response.status_code == 503
    assert "pass" not in response.json()


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": "x" * 8193}, {"token": "t", "extra": 1}])
def test_a_malformed_session_request_is_a_422(client_without_pass, auth, verdict, body):
    assert client_without_pass.post("/session", json=body, headers=auth).status_code == 422
    assert verdict["calls"] == []


def test_session_needs_the_api_key(client_without_pass, verdict):
    assert client_without_pass.post("/session", json={"token": "t"}).status_code == 401


def test_a_client_asking_for_passes_in_a_loop_is_turned_away(client_without_pass, auth, verdict):
    statuses = [client_without_pass.post("/session", json={"token": "t"}, headers=auth).status_code for _ in range(8)]
    assert statuses[0] == 200
    assert 429 in statuses


def test_session_is_a_404_while_the_gate_is_off(client_without_pass, auth, monkeypatch, api_settings):
    from dataclasses import replace

    off = replace(api_settings, captcha_enabled=False)
    app.dependency_overrides[session_router.get_settings] = lambda: off
    try:
        response = client_without_pass.post("/session", json={"token": "t"}, headers=auth)
        config = client_without_pass.get("/session/config", headers=auth).json()
    finally:
        del app.dependency_overrides[session_router.get_settings]
    assert response.status_code == 404
    assert config == {"enabled": False, "siteKey": "", "action": "session"}


def test_ready_fails_when_the_database_does_not_answer(api_settings):
    async def broken_session():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        yield  # pragma: no cover

    app.dependency_overrides[deps.get_session] = broken_session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/ready")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
