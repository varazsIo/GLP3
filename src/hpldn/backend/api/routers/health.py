"""Liveness/readiness endpoints for load-balancer target checks."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.deps import get_session
from hpldn.backend.api.market.cache import MarketCache, get_market_cache

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Report the process is alive."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    session: AsyncSession = Depends(get_session),
    cache: MarketCache = Depends(get_market_cache),
) -> dict:
    """Report the database is reachable and the market figures are loaded (loading them if not)."""
    await session.execute(text("SELECT 1"))
    await cache.get(session)
    return {"status": "ready"}
