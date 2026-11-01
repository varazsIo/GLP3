"""
The model price of a specific home on a chosen day.

``GET /addresses/search`` suggests priceable homes for a partial address,
``GET /addresses`` lists the homes in a postcode the model can price, and
``POST /price`` prices one of them for a sale date after today,
``POST /nearby`` lists what the homes nearest it recently sold for, and
``POST /schools`` lists the schools nearest it with their Ofsted ratings. All five are
exposed to the browser under ``/api`` by the dev proxy and nginx, like the
market routes. See `market.pricing` for how the model is loaded and
`market.address_search` for how a search is answered.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api import ratelimit
from hpldn.backend.api.deps import get_session, require_pass
from hpldn.backend.api.market import address_search, nearby, pricing, schools
from hpldn.backend.api.market.districts import is_full_postcode, normalise_postcode
from hpldn.backend.api.schemas import (
    AddressOption,
    AddressSearchParams,
    AddressSuggestion,
    HomeRequest,
    NearbySales,
    NearbySchools,
    PriceRequest,
    PriceResult,
)

router = APIRouter(tags=["pricing"])

# What the client is told for each pricing error: fixed sentences, never the
# exception's own text (which can carry the address as typed) nor anything
# else echoed from the request. The full message is logged server-side.
NOT_FOUND = (
    "We could not find this home in the property register. "
    "Check the house number or name and the street against the postcode."
)
BAD_SALE_DATE = "Choose a sale date after today and within the range the form offers: we can only price a future sale."
NOT_ENOUGH_DATA = "We do not have the records we need to price this home."

log = logging.getLogger("uvicorn.error")

# Browsers and any CDN may reuse a suggestion list for a minute, which collapses a
# backspace-and-retype; address data only changes on a data refresh.
SEARCH_CACHE_CONTROL = "public, max-age=60"

SEARCH_UNAVAILABLE = "Address search is not available right now. Try again in a moment."


@router.get("/addresses/search", response_model=list[AddressSuggestion], dependencies=[Depends(require_pass)])
async def search_addresses(
    request: Request,
    response: Response,
    q: str | None = Query(None, description="Part of an address, 3-64 characters"),
    limit: str | None = Query(None, description="How many homes, 1-10 (default 8)"),
    session: AsyncSession = Depends(get_session),
    cache: address_search.SearchCache = Depends(address_search.get_search_cache),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_search_limiter),
    metrics: address_search.SearchMetrics = Depends(address_search.get_search_metrics),
) -> list[AddressSuggestion]:
    """
    Suggest up to ``limit`` homes that can be priced, best match first.

    ``200 []`` when nothing matches. 422 for a query outside the bounds, with a
    sentence that never repeats it; 429 with ``Retry-After`` when one client
    searches faster than typing; 503 when the database cannot answer. Each
    suggestion is an `AddressOption` ``POST /price`` accepts unchanged, plus a
    display-only ``streetHint`` for a home the register holds no street for.

    ``q`` and ``limit`` are read as raw strings and validated here, not by
    FastAPI, because FastAPI's own 422 body echoes the rejected input.
    """
    started = time.perf_counter()
    try:
        params = AddressSearchParams(q=q, **({} if limit is None else {"limit": limit}))
    except ValidationError as exc:
        metrics.rejected_invalid += 1
        first = exc.errors()[0]
        sentence = (
            str(first.get("ctx", {}).get("error", ""))
            if first.get("loc") == ("q",)
            else f"limit must be a whole number from 1 to {address_search.MAX_LIMIT}"
        )
        raise HTTPException(status_code=422, detail=sentence or "Check the address and try again") from None

    key = (params.q, params.limit)
    async with limiter.acquire(ratelimit.client_key(request)):
        options = cache.get(key)
        path = "cache"
        if options is None:
            try:
                result = await address_search.search(session, params.q, params.limit)
            except DBAPIError:
                metrics.errors += 1
                log.warning(
                    "address_search len=%d outcome=error ms=%.1f",
                    len(params.q),
                    (time.perf_counter() - started) * 1000,
                )
                raise HTTPException(status_code=503, detail=SEARCH_UNAVAILABLE) from None
            options, path = result.options, result.path
            # A timed-out trigram statement may have missed better homes; answer
            # with what was found, but do not keep it.
            if not path.startswith("timeout"):
                cache.put(key, options)

    duration_ms = (time.perf_counter() - started) * 1000
    metrics.record(duration_ms, len(options), path)
    log.info(
        "address_search len=%d results=%d ms=%.1f outcome=%s",
        len(params.q),
        len(options),
        duration_ms,
        path,
    )
    response.headers["Cache-Control"] = SEARCH_CACHE_CONTROL
    return options


@router.get("/addresses/search/metrics", include_in_schema=False)
async def search_metrics(
    cache: address_search.SearchCache = Depends(address_search.get_search_cache),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_search_limiter),
    metrics: address_search.SearchMetrics = Depends(address_search.get_search_metrics),
) -> dict:
    """
    This worker's search figures: latency percentiles, cache hit and empty-result ratios, 429s.

    Key-protected and never proxied by nginx; read from the host or over an SSH tunnel.
    """
    return metrics.snapshot(cache, limiter.rejected)


@router.get("/addresses", response_model=list[AddressOption], dependencies=[Depends(require_pass)])
async def list_addresses(
    request: Request,
    postcode: str = Query(min_length=5, max_length=10),
    session: AsyncSession = Depends(get_session),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_form_limiter),
) -> list[AddressOption]:
    """List the homes in a full postcode that can be priced, in street and number order."""
    if not is_full_postcode(postcode):
        raise HTTPException(status_code=422, detail="Enter the full postcode, e.g. CR0 1JN")
    async with limiter.acquire(ratelimit.client_key(request)):
        options = await pricing.fetch_addresses(session, normalise_postcode(postcode))
    if not options:
        raise HTTPException(
            status_code=404,
            detail=f"We could not find any homes we can price in {normalise_postcode(postcode)}",
        )
    return options


@router.post("/price", response_model=PriceResult, dependencies=[Depends(require_pass)])
async def price_home(
    request: Request,
    body: PriceRequest,
    pricer: pricing.ModelPricer | None = Depends(pricing.get_pricer),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_price_limiter),
    cap: ratelimit.ConcurrencyCap = Depends(ratelimit.get_price_cap),
) -> PriceResult:
    """
    Price one home for a sale on ``saleDate``.

    404 when the address is not in the register, 422 for a date that is not
    after today or a home without the records a price needs (each with a
    sentence for the landlord), 429 when one client prices faster than a
    person would, 503 when no model is loaded or every model slot is busy.
    """
    from hpldn.backend.scripts.hpp_inference import (
        InvalidSaleDate,
        NotEnoughData,
        PropertyAddress,
        PropertyNotFound,
    )

    # Off the event loop: until the worker's warm-up has loaded the model this
    # waits on its lock, and a blocked loop stops the worker's heartbeat, so
    # gunicorn would kill it (and the warm-up) after --timeout.
    service = await run_in_threadpool(pricing.require_service, pricer)
    address = PropertyAddress(body.postnumber, body.poststreet, body.posttown, body.postcode, body.unit)
    try:
        async with limiter.acquire(ratelimit.client_key(request)), cap.acquire():
            prediction = await run_in_threadpool(service.price, address, body.sale_date)
    except PropertyNotFound as exc:
        log.info("price: not found: %s", exc)
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    except InvalidSaleDate as exc:
        log.info("price: invalid sale date: %s", exc)
        raise HTTPException(status_code=422, detail=BAD_SALE_DATE) from None
    except NotEnoughData as exc:
        log.info("price: not enough data: %s", exc)
        raise HTTPException(status_code=422, detail=NOT_ENOUGH_DATA) from None
    return pricing.to_result(prediction)


@router.post("/nearby", response_model=NearbySales, dependencies=[Depends(require_pass)])
async def nearby_sales(
    request: Request,
    body: HomeRequest,
    session: AsyncSession = Depends(get_session),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_form_limiter),
) -> NearbySales:
    """
    List the sales nearest one home in the latest 12 months, with their comparable-sales figures.

    404 when the address is not in the register; 503 when the database cannot answer.
    """
    try:
        async with limiter.acquire(ratelimit.client_key(request)):
            return await nearby.fetch_nearby(session, body)
    except nearby.HomeNotFound:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    except DBAPIError:
        raise HTTPException(status_code=503, detail="Nearby sales are not available right now. Try again in a moment.") from None


@router.post("/schools", response_model=NearbySchools, dependencies=[Depends(require_pass)])
async def nearby_schools(
    request: Request,
    body: HomeRequest,
    session: AsyncSession = Depends(get_session),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_form_limiter),
) -> NearbySchools:
    """
    List the primary and secondary schools nearest one home, with the Ofsted rating in force today.

    404 when the address is not in the register; 503 when the database cannot answer.
    """
    today = datetime.now(ZoneInfo("Europe/London")).date()
    try:
        async with limiter.acquire(ratelimit.client_key(request)):
            return await schools.fetch_schools(session, body, today)
    except nearby.HomeNotFound:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    except DBAPIError:
        raise HTTPException(status_code=503, detail="School ratings are not available right now. Try again in a moment.") from None
