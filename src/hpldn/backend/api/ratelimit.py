"""
Per-client guard rails for the routes a browser can call.

nginx's ``limit_req`` is the first line of defence in production; this is the
second, because nginx is not the only ingress (``npm start``'s dev proxy, an SSH
tunnel, a future load balancer). Two limits, both per client and per worker:

* a token bucket: `rate_per_s` sustained, `burst` at once, answered ``429`` with
  ``Retry-After`` when empty;
* an in-flight cap: at most `max_concurrent` requests of one client inside the
  route at a time, so one client cannot hold every worker's connections.

``POST /price`` also passes a `ConcurrencyCap` across all clients, answered
``503``, because each request is seconds of CPU in the thread pool under load.

State is in process memory. With several gunicorn workers each holds its own
buckets, so the effective limit is up to ``workers x`` the configured one --
nginx's shared zone is the precise limit; this one bounds the damage without it.
A shared store (Redis) is only needed once the API runs on more than one host.

The sizes come from ``guard.yaml`` (``rate_limits.app``), read when a limiter
is first asked for; see `hpldn.backend.guard.config`.
"""

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from hpldn.backend.guard.config import load_guard_config

# Connections from these addresses are proxies we run: the dev proxy and the
# tests on the loopback. Their X-Real-IP names the real client. A connection
# over the API's unix socket (production: /run/hpldn-api/api.sock, which only
# nginx's group can open) has no peer address at all and is trusted the same
# way. Any other TCP peer's X-Real-IP is ignored, so a direct caller cannot
# pick its bucket.
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1", "localhost"})
UNIX_SOCKET_PEER = "unix"

TOO_MANY = "You are searching faster than we can keep up. Wait a moment and keep typing."

TOO_MANY_REQUESTS = "Too many requests from you at once. Wait a moment and try again."

BUSY = "We are busy pricing other homes. Try again in a moment."


def client_key(request: Request) -> str:
    """
    Return the address a request's limits are keyed on.

    ``X-Real-IP`` when the connection comes from the trusted proxy -- the unix
    socket (no peer address: ``request.client`` is None or empty) or a
    loopback address -- else the TCP peer itself.
    """
    peer = request.client.host if request.client else ""
    trusted = not peer or peer in TRUSTED_PROXIES
    if trusted:
        forwarded = request.headers.get("x-real-ip", "").strip()
        if forwarded:
            return forwarded
    return peer or UNIX_SOCKET_PEER


@dataclass
class _Bucket:
    tokens: float
    updated: float
    in_flight: int = 0


class ClientLimiter:
    """
    A token bucket and in-flight counter per client key, with bounded memory.

    Parameters
    ----------
    rate_per_s : float
        Tokens added per second.
    burst : int
        Bucket capacity: the requests a client may make at once.
    max_concurrent : int
        Requests of one client allowed inside the route at the same time.
    max_clients : int
        Least recently seen clients are forgotten beyond this; a forgotten
        client starts again with a full bucket.
    clock : callable, optional
        Monotonic clock, injectable for tests.
    message : str, optional
        The 429's ``detail``.
    """

    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        max_concurrent: int,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
        message: str = TOO_MANY,
    ) -> None:
        self.message = message
        self.rate_per_s = rate_per_s
        self.burst = burst
        self.max_concurrent = max_concurrent
        self.max_clients = max_clients
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = asyncio.Lock()
        self.rejected = 0

    def _bucket(self, key: str, now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), updated=now)
            self._buckets[key] = bucket
            while len(self._buckets) > self.max_clients:
                oldest, candidate = next(iter(self._buckets.items()))
                if candidate.in_flight:
                    self._buckets.move_to_end(oldest)
                    break
                self._buckets.popitem(last=False)
        else:
            bucket.tokens = min(float(self.burst), bucket.tokens + (now - bucket.updated) * self.rate_per_s)
            bucket.updated = now
            self._buckets.move_to_end(key)
        return bucket

    def _reject(self, retry_after_s: float) -> HTTPException:
        self.rejected += 1
        return HTTPException(
            status_code=429,
            detail=self.message,
            # X-HPLDN-Reject is logged by nginx for the guard daemon and
            # hidden from the client (api_proxy.conf).
            headers={"Retry-After": str(max(1, math.ceil(retry_after_s))), "X-HPLDN-Reject": "rate"},
        )

    @asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        """
        Hold one request's place for the duration of the block.

        Raises
        ------
        HTTPException
            429 with ``Retry-After`` when the bucket is empty or the client
            already has `max_concurrent` requests in flight.
        """
        async with self._lock:
            now = self._clock()
            bucket = self._bucket(key, now)
            if bucket.in_flight >= self.max_concurrent:
                raise self._reject(1)
            if bucket.tokens < 1.0:
                raise self._reject((1.0 - bucket.tokens) / self.rate_per_s)
            bucket.tokens -= 1.0
            bucket.in_flight += 1
        try:
            yield
        finally:
            bucket.in_flight -= 1

    def clear(self) -> None:
        """Forget every client."""
        self._buckets.clear()
        self.rejected = 0


# Built from guard.yaml's rate_limits.app on first use (where the sizing of
# each is explained), so importing this module reads no configuration.
_search_limiter: ClientLimiter | None = None
_price_limiter: ClientLimiter | None = None
_form_limiter: ClientLimiter | None = None
_session_limiter: ClientLimiter | None = None
_read_limiter: ClientLimiter | None = None
_price_cap: "ConcurrencyCap | None" = None


def _limiter(name: str, message: str) -> ClientLimiter:
    limit = getattr(load_guard_config().rate_limits.app, name)
    return ClientLimiter(
        rate_per_s=limit.rate_per_s,
        burst=limit.burst,
        max_concurrent=limit.max_concurrent,
        message=message,
    )


def get_search_limiter() -> ClientLimiter:
    """Return this worker's address-search limiter (FastAPI dependency)."""
    global _search_limiter
    if _search_limiter is None:
        _search_limiter = _limiter("search", TOO_MANY)
    return _search_limiter


class ConcurrencyCap:
    """
    A cap on requests inside a block across every client, answered ``503``.

    Guards CPU-bound work run in the thread pool (model pricing), so a flood
    from many clients queues nobody for seconds: past `limit` a request is
    turned away at once with ``Retry-After``.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0
        self.rejected = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """
        Hold one place for the duration of the block.

        Raises
        ------
        HTTPException
            503 with ``Retry-After`` when `limit` requests are already inside.
        """
        # No await between the check and the increment, so no lock is needed.
        if self.in_flight >= self.limit:
            self.rejected += 1
            raise HTTPException(status_code=503, detail=BUSY, headers={"Retry-After": "1"})
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1


def get_price_limiter() -> ClientLimiter:
    """Return this worker's ``POST /price`` per-client limiter (FastAPI dependency)."""
    global _price_limiter
    if _price_limiter is None:
        _price_limiter = _limiter("price", TOO_MANY_REQUESTS)
    return _price_limiter


def get_price_cap() -> ConcurrencyCap:
    """Return this worker's cap on model pricing in flight (FastAPI dependency)."""
    global _price_cap
    if _price_cap is None:
        _price_cap = ConcurrencyCap(limit=load_guard_config().rate_limits.app.price_cap_limit())
    return _price_cap


def get_form_limiter() -> ClientLimiter:
    """Return this worker's limiter for the forms, ``/nearby`` and ``/schools`` (FastAPI dependency)."""
    global _form_limiter
    if _form_limiter is None:
        _form_limiter = _limiter("form", TOO_MANY_REQUESTS)
    return _form_limiter


def get_session_limiter() -> ClientLimiter:
    """Return this worker's ``POST /session`` (reCAPTCHA check) limiter (FastAPI dependency)."""
    global _session_limiter
    if _session_limiter is None:
        _session_limiter = _limiter("session", TOO_MANY_REQUESTS)
    return _session_limiter


def get_read_limiter() -> ClientLimiter:
    """Return this worker's limiter for the open routes: ``/areas*``, ``/inflation``, ``/session/config``."""
    global _read_limiter
    if _read_limiter is None:
        _read_limiter = _limiter("read", TOO_MANY_REQUESTS)
    return _read_limiter


async def read_limit(request: Request, limiter: ClientLimiter = Depends(get_read_limiter)) -> AsyncIterator[None]:
    """
    Hold the open routes' per-client limit for the request (a route dependency).

    Those routes need no reCAPTCHA pass, so without it reloading the page in
    a loop would only meet nginx's limit.

    Raises
    ------
    HTTPException
        429 with ``Retry-After``, as `ClientLimiter.acquire`.
    """
    async with limiter.acquire(client_key(request)):
        yield
