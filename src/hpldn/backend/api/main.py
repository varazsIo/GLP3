"""
FastAPI application serving read-only HPLDN housing data.

Run behind gunicorn with the hardened uvicorn worker (`api.worker`)::

    gunicorn hpldn.backend.api.main:app -k hpldn.backend.api.worker.HardenedUvicornWorker

or locally, with the ``POSTGRES_API_*`` lines in ``develop.env``::

    uvicorn hpldn.backend.api.main:app --reload

``/docs``, ``/redoc`` and ``/openapi.json`` exist only with ``HPLDN_API_DOCS=1``
(the local stack and the Playwright setup); the serving host never sets it,
so the API's contract is not published there. Any unhandled exception
answers ``{"detail": "internal error", "rid": ...}``: the traceback is logged
under that request id, never sent.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

import hpldn
from hpldn.backend.api import access_log
from hpldn.backend.api.config import DOCS_ENV, get_settings
from hpldn.backend.api.deps import get_engine, get_sessionmaker, require_api_key
from hpldn.backend.api.market import queries
from hpldn.backend.api.market.cache import get_market_cache
from hpldn.backend.api.market.pricing import get_pricer
from hpldn.backend.api.routers import health, market, pricing, properties, session
from hpldn.backend.common import redact

SKIP_STARTUP_CHECK_ENV = "HPLDN_API_SKIP_STARTUP_CHECK"

log = logging.getLogger("uvicorn.error")

_PROBE_SQL = text("SELECT 1 FROM production.london_houses LIMIT 1")


async def check_database() -> None:
    """
    Check the configured role can read ``production.london_houses``.

    Logs one line naming ``user@host:port/db`` (never the password), the row
    count, the latest sale and the ``hpldn`` install path, which exposes a
    non-editable install serving stale code.

    Raises
    ------
    RuntimeError
        If the probe fails, so a wrong password, a missing grant or a stale
        schema stops the server rather than surfacing as a 500.
    """
    settings = get_settings()
    try:
        async with get_engine().connect() as connection:
            await connection.execute(_PROBE_SQL)
            window = (await connection.execute(queries.DATA_WINDOW_SQL)).mappings().one()
    except Exception as exc:
        raise RuntimeError(
            f"Read API cannot read production.london_houses as "
            f"{settings.safe_target}: {exc}"
        ) from exc
    log.info(
        "Read API live data: %s -- %s rows in production.london_houses, latest sale %s "
        "(hpldn from %s)",
        settings.safe_target,
        f"{window['total_sales']:,}",
        window["latest_sale"],
        os.path.dirname(hpldn.__file__),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """
    Run the startup database check and load the market figures, unless disabled.

    Setting ``HPLDN_API_SKIP_STARTUP_CHECK=1`` skips it; the hermetic API tests
    in ``tests/api/conftest.py`` do so because they never reach PostgreSQL.
    The ``uvicorn.error`` logger is used so the startup line lands beside the
    access log, standalone or under gunicorn.
    """
    if os.environ.get(SKIP_STARTUP_CHECK_ENV) != "1":
        await check_database()
        # The price model and its sales history take seconds to load; start
        # now so the first POST /price does not wait. Without a model file
        # this only logs that model pricing is disabled.
        pricer = get_pricer()
        if pricer is not None:
            pricer.warm_in_background()
        # The market figures behind /areas and /inflation load before the
        # worker accepts a request, and reload in the background from then on,
        # so no visitor waits seconds for them (`market.cache`).
        cache = get_market_cache()
        await cache.warm(get_sessionmaker())
        cache.start_periodic_refresh()
        try:
            yield
        finally:
            await cache.stop()
        return
    yield


def docs_enabled() -> bool:
    """True with ``HPLDN_API_DOCS=1``: serve ``/docs``, ``/redoc`` and ``/openapi.json``."""
    return os.environ.get(DOCS_ENV) == "1"


_docs = docs_enabled()
# A generic title and version: nothing here names the product or its release.
app = FastAPI(
    title="API",
    version="1",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

# Secrets and tokens never reach a log line (`common.redact`).
redact.install()

# One timestamped line per request with the real client's address
# (`access_log`); run gunicorn without --access-logfile so it is the only one.
access_log.configure()
app.add_middleware(access_log.AccessLogMiddleware)


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    """
    Answer any unhandled exception with 500 and a fixed body naming the request id.

    The traceback is logged here, server side only (redacted); the client
    learns nothing about the code, the query or the data.
    """
    rid = access_log.current_request_id(request.scope)
    log.error("unhandled %s on %s %s rid=%s", type(exc).__name__, request.method, request.url.path, rid, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "internal error", "rid": rid})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Answer a request FastAPI could not validate with 422 and each error's type, location and message.

    FastAPI's own 422 also repeats each error's ``input`` and ``ctx``: whatever
    the client sent, a body of HTML or SQL included, echoed back. Only the field
    and what is wrong with it are returned.
    """
    detail = [
        {"type": error.get("type"), "loc": list(error.get("loc", ())), "msg": error.get("msg")}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})

app.include_router(health.router)
app.include_router(properties.router, dependencies=[Depends(require_api_key)])
app.include_router(market.router, dependencies=[Depends(require_api_key)])
app.include_router(pricing.router, dependencies=[Depends(require_api_key)])
app.include_router(session.router, dependencies=[Depends(require_api_key)])
