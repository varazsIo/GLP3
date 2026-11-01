"""
The API's hardening: no published contract, a generic 500, no server header, the unix-socket peer.
"""

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from hpldn.backend.api import access_log, main
from hpldn.backend.api.main import app
from hpldn.backend.api.worker import HardenedUvicornWorker


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_docs_and_the_contract_are_not_served(client, path):
    assert main.docs_enabled() is False
    assert client.get(path).status_code == 404


def test_the_app_names_no_product_or_release():
    schema = app.openapi()
    assert schema["info"] == {"title": "API", "version": "1"}


def test_an_unhandled_error_is_a_generic_500_with_the_request_id(_app_on_fixture_database, caplog):
    @app.get("/__test_boom")
    async def boom():
        raise RuntimeError("SELECT secret FROM production.thing WHERE input='<script>'")

    try:
        with TestClient(_app_on_fixture_database, raise_server_exceptions=False) as raw:
            response = raw.get("/__test_boom", headers={"X-Request-ID": "boom-request-0001"})
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test_boom"]
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error", "rid": "boom-request-0001"}
    assert "SELECT" not in response.text and "script" not in response.text
    # The detail is in the server's log only.
    assert any("unhandled RuntimeError" in r.getMessage() and "boom-request-0001" in r.getMessage() for r in caplog.records)


def test_the_worker_turns_off_the_server_and_date_headers_and_proxy_headers():
    kwargs = HardenedUvicornWorker.CONFIG_KWARGS
    assert kwargs["server_header"] is False
    assert kwargs["date_header"] is False
    assert kwargs["proxy_headers"] is False


def test_over_a_unix_socket_no_server_header_and_x_real_ip_is_the_client(tmp_path, monkeypatch):
    """A real uvicorn on a unix socket, configured as the hardened worker is: what nginx talks to."""
    lines = []
    monkeypatch.setattr(access_log.log, "info", lambda fmt, *args: lines.append(fmt % args))
    sock = tmp_path / "api.sock"
    # log_config=None: uvicorn's default config would reset the global "uvicorn"
    # logger (propagate=False) and silence caplog for every later test.
    config = uvicorn.Config(app, uds=str(sock), log_level="error", lifespan="off", log_config=None,
                            **{k: v for k, v in HardenedUvicornWorker.CONFIG_KWARGS.items() if k not in ("loop", "http")})
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(sock))
            conn.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nX-Real-IP: 203.0.113.9\r\n"
                         b"X-Request-ID: unix-socket-0001\r\nConnection: close\r\n\r\n")
            raw = b""
            while chunk := conn.recv(4096):
                raw += chunk
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    head, _, body = raw.partition(b"\r\n\r\n")
    headers = head.decode().lower()
    assert head.startswith(b"HTTP/1.1 200")
    assert "\r\nserver:" not in headers and "\r\ndate:" not in headers
    assert json.loads(body) == {"status": "ok"}
    # No peer address over the socket: nginx's X-Real-IP names the client.
    assert any(line.startswith("203.0.113.9 ") and "rid=unix-socket-0001" in line for line in lines)
