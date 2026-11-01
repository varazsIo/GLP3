"""
One timestamped line per request the API answers, naming the real client.

Replaces gunicorn's and uvicorn's access logs, which print no time and, behind
nginx, the proxy's own address (``127.0.0.1:0``)::

    [2026-10-03 12:16:41 +0100] [10222] [ACCESS] 203.0.113.9 "GET /addresses/search?q[3]&limit[1]" 429 0.4ms rid=5f0c2a9e41d84b7c reject=rate

* the client is `ratelimit.client_key`: nginx's ``X-Real-IP`` when the
  connection comes from the trusted proxy (the unix socket, or the
  loopback), else the peer;
* the query string is logged as parameter names and value lengths, never
  values: no address or postcode a visitor typed is written to a log;
* ``rid`` is the request id: the ``X-Request-ID`` nginx (or the page) sent,
  else a new one. It is echoed in the response's ``X-Request-ID``, so a line
  here, nginx's line and the browser's console line for one call share it;
* ``reject`` is the API's ``X-HPLDN-Reject`` (``rate``, ``captcha``) on a
  refused request.

Lines go to stdout through the ``hpldn.access`` logger: ``api.log`` in the
local stack, the journal (``journalctl -u hpldn-api``) on the host. Requests
nginx refuses itself never reach the API; nginx writes those (``hpldn_api``
in ``cloud/vps/nginx/hpldn.conf``).
"""

import logging
import re
import sys
import time
import uuid
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hpldn.backend.api.ratelimit import client_key

LOGGER_NAME = "hpldn.access"
REQUEST_ID_HEADER = "x-request-id"
# What a request id sent by nginx or the page may look like; anything else is replaced.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Query parameter names worth printing as they are; anything else shows as "?".
_PARAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_STATE_KEY = "hpldn_request_id"
# gunicorn's own timestamp format, so the lines in one file line up.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"

log = logging.getLogger(LOGGER_NAME)


def configure(stream=None) -> logging.Logger:
    """
    Give ``hpldn.access`` its timestamped handler on `stream` (stdout) once; returns the logger.

    It does not propagate, so neither gunicorn's nor uvicorn's handlers print
    the line a second time.
    """
    if not log.handlers:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(process)d] [ACCESS] %(message)s", TIME_FORMAT))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def request_id(scope: Scope) -> str:
    """The request's id: a well-formed ``X-Request-ID`` it carries, else a new one."""
    for name, value in scope.get("headers", ()):
        if name == REQUEST_ID_HEADER.encode():
            candidate = value.decode("latin-1").strip()
            if _REQUEST_ID.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex[:16]


def current_request_id(scope: Scope) -> str:
    """The id `AccessLogMiddleware` gave this request (kept in its state), else `request_id`."""
    state = scope.get("state")
    rid = state.get(_STATE_KEY) if isinstance(state, dict) else None
    return rid or request_id(scope)


def _target(scope: Scope) -> str:
    """
    The path, and the query string as parameter names and value lengths only.

    ``/addresses/search?q=CR0 1JN&limit=8`` is logged as
    ``/addresses/search?q[7]&limit[1]``: what a visitor typed (an address, a
    postcode) never reaches the log.
    """
    path = scope.get("path", "")
    query = scope.get("query_string", b"")
    if not query:
        return path
    shown = []
    for name, value in parse_qsl(query.decode("latin-1"), keep_blank_values=True):
        shown.append(f"{_safe_name(name)}[{len(value)}]")
    return f"{path}?{'&'.join(shown)}" if shown else f"{path}?[{len(query)}]"


def _safe_name(name: str) -> str:
    """A parameter name as logged: printable and short, else ``?``."""
    return name if _PARAM_NAME.match(name) else "?"


class AccessLogMiddleware:
    """ASGI middleware writing `log`'s line when each HTTP response is finished (or fails)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        rid = request_id(scope)
        # For the catch-all 500 handler (`api.main.internal_error`), which
        # answers with this id.
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[_STATE_KEY] = rid
        status = 500
        reject = ""

        async def send_wrapper(message: Message) -> None:
            nonlocal status, reject
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", ()))
                for name, value in headers:
                    if name.lower() == b"x-hpldn-reject":
                        reject = value.decode("latin-1")
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            log.info(
                '%s "%s %s" %d %.1fms rid=%s%s',
                client_key(Request(scope)),
                scope.get("method", ""),
                _target(scope),
                status,
                elapsed_ms,
                rid,
                f" reject={reject}" if reject else "",
            )
