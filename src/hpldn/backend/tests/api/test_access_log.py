"""
The API's own access log (`api/access_log.py`) and the open routes' limit.

Every answered request gets one line with the client's address, the status,
the time taken and the request id, refused ones included.
"""

import logging
import re

import pytest
from starlette.requests import Request

from hpldn.backend.api import access_log, ratelimit
from hpldn.backend.api.main import app


@pytest.fixture
def lines(caplog):
    """The messages `access_log.log` writes during a test."""
    caplog.set_level(logging.INFO, logger=access_log.LOGGER_NAME)
    access_log.log.addHandler(caplog.handler)
    yield lambda: [r.getMessage() for r in caplog.records if r.name == access_log.LOGGER_NAME]
    access_log.log.removeHandler(caplog.handler)


def test_each_request_is_logged_with_its_client_status_time_and_id(client, lines):
    response = client.get("/health?probe=1", headers={"X-Request-ID": "browser-call-0001"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "browser-call-0001"
    [line] = lines()
    assert re.fullmatch(r'testclient "GET /health\?probe\[1\]" 200 \d+\.\dms rid=browser-call-0001', line)


def test_query_values_never_reach_the_log(client, lines):
    client.get("/health?q=CR0%201JN&limit=8&%3Cscript%3E=x")

    line = lines()[-1]
    assert '"GET /health?q[7]&limit[1]&?[1]"' in line
    assert "CR0" not in line and "script" not in line


def test_the_unix_socket_peer_is_the_trusted_proxy():
    scope = {"type": "http", "client": None, "headers": [(b"x-real-ip", b"203.0.113.9")]}
    assert ratelimit.client_key(Request(scope)) == "203.0.113.9"
    scope = {"type": "http", "client": ("", 0), "headers": []}
    assert ratelimit.client_key(Request(scope)) == "unix"


def test_a_tcp_peer_off_the_loopback_cannot_choose_its_address():
    scope = {"type": "http", "client": ("198.51.100.7", 4242), "headers": [(b"x-real-ip", b"203.0.113.9")]}
    assert ratelimit.client_key(Request(scope)) == "198.51.100.7"


def test_a_malformed_request_id_is_replaced(client, lines):
    response = client.get("/health", headers={"X-Request-ID": "bad id; DROP"})

    rid = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{16}", rid)
    assert lines()[-1].endswith(f"rid={rid}")


def test_the_line_names_the_client_nginx_forwards_from_the_loopback():
    scope = {
        "type": "http",
        "client": ("127.0.0.1", 0),
        "headers": [(b"x-real-ip", b"203.0.113.9")],
    }
    assert ratelimit.client_key(Request(scope)) == "203.0.113.9"


def test_the_formatter_stamps_the_date_time_and_zone():
    formatter = access_log.configure().handlers[0].formatter
    record = logging.LogRecord(access_log.LOGGER_NAME, logging.INFO, __file__, 1, "x", None, None)
    assert re.fullmatch(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}\] \[\d+\] \[ACCESS\] x", formatter.format(record))


def test_reloading_the_page_in_a_loop_meets_the_open_routes_limit_and_is_logged(client, auth, lines):
    limiter = ratelimit.ClientLimiter(rate_per_s=0.001, burst=2, max_concurrent=4)
    app.dependency_overrides[ratelimit.get_read_limiter] = lambda: limiter
    try:
        statuses = [client.get("/session/config", headers=auth).status_code for _ in range(3)]
    finally:
        del app.dependency_overrides[ratelimit.get_read_limiter]

    assert statuses == [200, 200, 429]
    assert lines()[-1].startswith('testclient "GET /session/config" 429 ')
    assert lines()[-1].endswith(" reject=rate")
