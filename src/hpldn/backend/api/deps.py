"""Async database plumbing and request guards for the read API."""

import secrets
from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hpldn.backend.api.config import get_settings
from hpldn.backend.api.ratelimit import client_key
from hpldn.backend.guard.captcha import check_pass
from hpldn.backend.guard.config import load_guard_config


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, with server-side prepared statements disabled."""
    return create_async_engine(
        get_settings().database_url,
        connect_args={"prepare_threshold": None},
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, built once alongside the engine."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session for one request (FastAPI dependency)."""
    async with get_sessionmaker()() as session:
        yield session


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(provided: str | None = Security(_api_key_header)) -> None:
    """
    Check the ``X-API-Key`` header against the configured key.

    Parameters
    ----------
    provided : str or None
        The header value sent by the client.

    Raises
    ------
    HTTPException
        401 if the key is missing, wrong, or not configured server-side
        (the check fails closed).

    Notes
    -----
    Both sides are compared as UTF-8 bytes: `secrets.compare_digest` raises
    ``TypeError`` on non-ASCII ``str`` input, which would turn a header such
    as ``café`` into a 500 instead of a 401.
    """
    expected = get_settings().api_key
    if not expected or provided is None:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


PASS_HEADER = "X-HPLDN-Pass"

PASS_REQUIRED = "Reload the page to keep using this. We check that each visitor is a person."


async def require_pass(request: Request) -> None:
    """
    Check the ``X-HPLDN-Pass`` header carries a reCAPTCHA pass for this client.

    A no-op while the captcha is off (`ApiSettings.captcha_enabled`), and on a
    route ``guard.yaml``'s ``captcha.gated_routes`` does not list.

    Raises
    ------
    HTTPException
        403 when the pass is missing, expired, forged or for another address,
        with ``X-HPLDN-Captcha: required``, which tells the page to get a new
        pass and try once more, and ``X-HPLDN-Reject: captcha``, which nginx
        logs for the guard daemon and hides from the client.
    """
    settings = get_settings()
    if not settings.captcha_enabled:
        return
    route = request.scope.get("route")
    if getattr(route, "path", request.url.path) not in load_guard_config().captcha.gated_routes:
        return
    if not check_pass(request.headers.get(PASS_HEADER), client_key(request), secret=settings.pass_secret):
        raise HTTPException(
            status_code=403,
            detail=PASS_REQUIRED,
            headers={"X-HPLDN-Captcha": "required", "X-HPLDN-Reject": "captcha"},
        )
