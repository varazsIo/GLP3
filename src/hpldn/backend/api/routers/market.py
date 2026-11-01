"""
The frontend's live data: district figures, nearby sales and estimates.

The only routes nginx exposes to the browser (under ``/api``, key added by the
proxy), so everything here is effectively public: read-only, bounded and
index-bound. District figures are loaded per worker at startup and refreshed in
the background (`market.cache`); the rest is live.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api import ratelimit
from hpldn.backend.api.deps import get_session, require_pass
from hpldn.backend.api.market import repository
from hpldn.backend.api.market.cache import MarketCache, MarketSnapshot, get_market_cache
from hpldn.backend.api.market.districts import (
    MAX_FLOOR_AREA,
    MIN_FLOOR_AREA,
    OUTER_AREAS,
    area_of,
    district_of,
    evidence_members,
    is_full_postcode,
    is_postcode_shaped,
    normalise_postcode,
    outward_of,
)
from hpldn.backend.api.market.estimator import (
    Estimator,
    build_drivers,
    get_estimator,
    suggested_monthly_rent,
)
from hpldn.backend.api.schemas import (
    AreasResponse,
    AreaStats,
    Comparable,
    EstimateRequest,
    EstimateResult,
    InflationSeries,
    PropertyType,
    ResolvedEstimateRequest,
)

router = APIRouter(tags=["market"])

MAX_COMPARABLES = 10

# Fixed sentences: nothing from the request is echoed back in an error.
NOT_COVERED = "We do not cover this area yet"
NOT_IN_AREA = "That postcode is not in this area"


def _district_or_404(snapshot: MarketSnapshot, value: str) -> repository.DistrictStats:
    """
    Return the stats for the area a code or postcode falls in.

    Raises
    ------
    HTTPException
        404 (`NOT_COVERED`) when the area is not covered.
    """
    area = district_of(value)
    stats = snapshot.districts.get(area)
    if stats is None:
        raise HTTPException(status_code=404, detail=NOT_COVERED)
    return stats


async def _origin(
    session: AsyncSession, postcode: str | None, outward: str, stats: repository.DistrictStats
) -> tuple[float, float] | None:
    """
    Return the point nearby-sale distances are measured from.

    In order: the full postcode's centre if held; the outward code's own
    centre; the area's centre only when `outward` is the whole area (a bare
    ``BR`` or ``SW11``). An outer outward code with no sales of its own has no
    origin, so no sale kilometres away is called nearby.
    """
    if postcode and is_full_postcode(postcode):
        centroid = await repository.fetch_postcode_centroid(session, normalise_postcode(postcode))
        if centroid is not None:
            return centroid
    if outward != stats.area.code:
        centroid = await repository.fetch_outward_centroid(session, outward)
        if centroid is not None:
            return centroid
        if stats.area.code in OUTER_AREAS:
            return None
    return stats.centre


# The same for every visitor and changed monthly, so nginx and Cloudflare may
# keep a copy: browsers for a minute, shared caches for five, and a stale copy
# while a fresh one is fetched.
PUBLIC_CACHE_CONTROL = "public, max-age=60, s-maxage=300, stale-while-revalidate=3600"


def _cached_json(request: Request, body: bytes, etag: str) -> Response:
    """Answer `body` (JSON) with its ETag, or 304 when the client already holds it."""
    headers = {"ETag": etag, "Cache-Control": PUBLIC_CACHE_CONTROL}
    if etag in (part.strip() for part in request.headers.get("if-none-match", "").split(",")):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/areas", response_model=AreasResponse, dependencies=[Depends(ratelimit.read_limit)])
async def list_areas(
    request: Request,
    session: AsyncSession = Depends(get_session),
    cache: MarketCache = Depends(get_market_cache),
) -> Response:
    """List the data window, every covered area and the latest mortgage rates (`AreasResponse`)."""
    snapshot = await cache.get(session)
    return _cached_json(request, snapshot.areas_body, snapshot.areas_etag)


@router.get("/inflation", response_model=InflationSeries, dependencies=[Depends(ratelimit.read_limit)])
async def get_inflation(
    request: Request,
    session: AsyncSession = Depends(get_session),
    cache: MarketCache = Depends(get_market_cache),
) -> Response:
    """
    Return UK consumer price inflation (CPIH) month by month, as an index (`InflationSeries`).

    503 when ``production.cpih`` is not loaded (before migration 0032).
    """
    snapshot = await cache.get(session)
    if snapshot.inflation_body is None or snapshot.inflation_etag is None:
        raise HTTPException(status_code=503, detail="Inflation figures are not available right now. Try again later.")
    return _cached_json(request, snapshot.inflation_body, snapshot.inflation_etag)


@router.get("/areas/{code}", response_model=AreaStats, dependencies=[Depends(ratelimit.read_limit)])
async def get_area(
    code: str,
    session: AsyncSession = Depends(get_session),
    cache: MarketCache = Depends(get_market_cache),
) -> AreaStats:
    """Return one area's figures."""
    return _district_or_404(await cache.get(session), code).area


@router.get("/areas/{code}/comparables", response_model=list[Comparable], dependencies=[Depends(ratelimit.read_limit)])
async def list_comparables(
    code: str,
    property_type: PropertyType = Query(alias="propertyType"),
    floor_area_sqm: float = Query(alias="floorAreaSqm", ge=MIN_FLOOR_AREA, le=MAX_FLOOR_AREA),
    postcode: str | None = Query(None),
    limit: int = Query(3, ge=1, le=MAX_COMPARABLES),
    session: AsyncSession = Depends(get_session),
    cache: MarketCache = Depends(get_market_cache),
) -> list[Comparable]:
    """
    List nearby sales for an area.

    The evidence scope follows the most specific outward code given: the
    postcode's, which must lie in this area (422 otherwise), else the path's.
    A bare area code (``BR``, ``SW1``) is the whole area.
    """
    if postcode is not None and not is_postcode_shaped(postcode):
        raise HTTPException(status_code=422, detail="Invalid UK Postcode")
    snapshot = await cache.get(session)
    stats = _district_or_404(snapshot, code)
    area = stats.area.code
    outward = outward_of(code)
    if postcode is not None:
        if area_of(outward_of(postcode)) != area:
            raise HTTPException(status_code=422, detail=NOT_IN_AREA)
        outward = outward_of(postcode)
    return await repository.fetch_comparables(
        session,
        district=area,
        members=evidence_members(outward, area),
        window=snapshot.window,
        property_type=property_type,
        floor_area_sqm=floor_area_sqm,
        origin=await _origin(session, postcode, outward, stats),
        limit=limit,
        valuation_date=date.today(),
        index=snapshot.price_index,
    )


@router.post("/estimate", response_model=EstimateResult, dependencies=[Depends(require_pass)])
async def estimate(
    request: Request,
    body: EstimateRequest,
    session: AsyncSession = Depends(get_session),
    cache: MarketCache = Depends(get_market_cache),
    estimator: Estimator = Depends(get_estimator),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_form_limiter),
) -> EstimateResult:
    """
    Estimate a home's value.

    Combines the cached district figures, live nearby sales and evidence
    counts, the estimator, and the resulting reasons and implied rent. 429
    when one client estimates faster than a person would.
    """
    async with limiter.acquire(ratelimit.client_key(request)):
        return await _estimate(body, session, cache, estimator)


async def _estimate(
    body: EstimateRequest, session: AsyncSession, cache: MarketCache, estimator: Estimator
) -> EstimateResult:
    """Make the estimate `estimate` answers with."""
    snapshot = await cache.get(session)
    stats = _district_or_404(snapshot, body.postcode)
    area = stats.area
    request = ResolvedEstimateRequest(**body.model_dump(), district=area.code)
    outward = outward_of(body.postcode)
    members = evidence_members(outward, area.code)

    # Nearby sales are moved to the day being valued, so the figure quoted
    # beside them is one the list supports.
    valuation_date = request.sale_date or date.today()
    comparables = await repository.fetch_comparables(
        session,
        district=area.code,
        members=members,
        window=snapshot.window,
        property_type=request.property_type,
        floor_area_sqm=request.floor_area_sqm,
        origin=await _origin(session, request.postcode, outward, stats),
        valuation_date=valuation_date,
        index=snapshot.price_index,
    )
    counts = await repository.fetch_evidence_counts(
        session,
        members=members,
        window=snapshot.window,
        property_type=request.property_type,
        floor_area_sqm=request.floor_area_sqm,
    )
    # The model estimator loads a few hundred megabytes on first use and scores
    # off the event loop, so run every estimator in the threadpool.
    core = await run_in_threadpool(
        estimator.estimate, request, area, comparables, counts.similar_count, counts.sales_count
    )

    return EstimateResult(
        request=request,
        value=core.value,
        low=core.low,
        high=core.high,
        confidence=core.confidence,
        spread_percent=core.spread_percent,
        drivers=build_drivers(area, request, comparables),
        comparables=comparables,
        suggested_monthly_rent=suggested_monthly_rent(request.bedrooms, area),
        window=snapshot.window,
        basis=core.basis,
        projection_uplift=core.projection_uplift,
        projected_from=core.projected_from,
        index_area=core.index_area,
    )
