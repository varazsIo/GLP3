"""
Per-worker cache for the market figures that change monthly.

The data window, district figures, mortgage rates and CPIH take seconds to
compute on a cold database (2 s warm, 7 s after an idle night on the serving
host) but only change when PPD is refreshed. So no visitor ever waits for
them: each worker loads them at startup (`MarketCache.warm`, from the app's
lifespan, before the worker accepts a request) and reloads them in the
background every ``HPLDN_MARKET_CACHE_TTL_S`` seconds (default an hour). A
stale snapshot is served while its replacement loads, and a failed reload
keeps the old one.

The ``/areas`` and ``/inflation`` bodies are serialised once per load
(`MarketSnapshot.areas_body`, `inflation_body`), with an ETag each.
"""

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hpldn.backend.api.market import repository
from hpldn.backend.api.market.inflation import fetch_inflation
from hpldn.backend.api.market.price_index import PriceIndex, fetch_price_index
from hpldn.backend.api.market.repository import DistrictStats
from hpldn.backend.api.schemas import AreasResponse, DataWindow, InflationSeries, MortgageRates

DEFAULT_TTL_S = 3600.0

log = logging.getLogger("uvicorn.error")


def _etag(body: bytes) -> str:
    """A strong ETag for `body`."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


@dataclass(frozen=True)
class MarketSnapshot:
    """The data window, every area's figures and the latest mortgage rates, loaded together."""

    window: DataWindow
    districts: dict[str, DistrictStats]
    mortgage_rates: MortgageRates | None = None
    # The price index nearby sales are moved along to the day being valued.
    price_index: PriceIndex | None = None
    # CPIH, for the price card's inflation-corrected figures; None before migration 0032.
    inflation: InflationSeries | None = None
    # The GET /areas and GET /inflation bodies, serialised once, and their ETags.
    areas_body: bytes = field(init=False, repr=False)
    areas_etag: str = field(init=False, repr=False)
    inflation_body: bytes | None = field(init=False, repr=False)
    inflation_etag: str | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        areas = AreasResponse(
            window=self.window,
            areas=[stats.area for stats in self.districts.values()],
            mortgage_rates=self.mortgage_rates,
        ).model_dump_json(by_alias=True).encode()
        inflation = None if self.inflation is None else self.inflation.model_dump_json(by_alias=True).encode()
        object.__setattr__(self, "areas_body", areas)
        object.__setattr__(self, "areas_etag", _etag(areas))
        object.__setattr__(self, "inflation_body", inflation)
        object.__setattr__(self, "inflation_etag", None if inflation is None else _etag(inflation))


async def load_snapshot(session: AsyncSession) -> MarketSnapshot:
    """Load every figure in the snapshot through `session`."""
    window = await repository.fetch_window(session)
    districts = await repository.fetch_district_stats(session, window)
    rates = await repository.fetch_mortgage_rates(session)
    price_index = await fetch_price_index(session)
    inflation = await fetch_inflation(session)
    return MarketSnapshot(
        window=window,
        districts=districts,
        mortgage_rates=rates,
        price_index=price_index,
        inflation=inflation,
    )


class MarketCache:
    """
    Hold one `MarketSnapshot`, reloading it in the background once it is older than its time-to-live.

    Parameters
    ----------
    ttl_s : float, optional
        Lifetime in seconds; ``HPLDN_MARKET_CACHE_TTL_S`` or
        `DEFAULT_TTL_S` by default.
    clock : callable, optional
        Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        ttl_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_s is None:
            ttl_s = float(os.environ.get("HPLDN_MARKET_CACHE_TTL_S", DEFAULT_TTL_S))
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._snapshot: MarketSnapshot | None = None
        self._loaded_at = 0.0
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._refreshing: asyncio.Task | None = None
        self._periodic: asyncio.Task | None = None

    @property
    def loaded(self) -> bool:
        """True once a snapshot is held."""
        return self._snapshot is not None

    def _stale(self) -> bool:
        return self._clock() - self._loaded_at >= self.ttl_s

    async def _load(self, session: AsyncSession) -> MarketSnapshot:
        """Load a snapshot through `session` and keep it."""
        started = time.perf_counter()
        snapshot = await load_snapshot(session)
        self._snapshot = snapshot
        self._loaded_at = self._clock()
        log.info(
            "Market figures loaded: %d areas in %.0f ms",
            len(snapshot.districts),
            (time.perf_counter() - started) * 1000,
        )
        return snapshot

    async def get(self, session: AsyncSession) -> MarketSnapshot:
        """
        Return the snapshot.

        A stale one is returned at once and replaced in the background (when
        the cache has a session factory, from `warm`). Only with no snapshot
        at all does the caller wait for a load through `session`; concurrent
        callers then wait for one load, not one each.
        """
        snapshot = self._snapshot
        if snapshot is not None:
            if self._stale():
                if self._sessionmaker is not None:
                    self._refresh_in_background()
                    return snapshot
            else:
                return snapshot
        async with self._lock:
            if self._snapshot is not None and not self._stale():
                return self._snapshot
            return await self._load(session)

    async def warm(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        """
        Load the snapshot now and keep `sessionmaker` for the background reloads.

        A failure is logged, not raised: `get` then loads on the first request.
        """
        self._sessionmaker = sessionmaker
        try:
            await self.refresh()
        except Exception:
            log.exception("Market figures could not be loaded at startup; the first request will load them")

    async def refresh(self) -> None:
        """Reload the snapshot through a session of its own, keeping the old one if that fails."""
        if self._sessionmaker is None:
            raise RuntimeError("MarketCache.refresh needs warm() first")
        async with self._lock:
            async with self._sessionmaker() as session:
                await self._load(session)

    def _refresh_in_background(self) -> None:
        """Start one background reload unless one is already running."""
        if self._refreshing is not None and not self._refreshing.done():
            return
        self._refreshing = asyncio.get_running_loop().create_task(self._refresh_logged())

    async def _refresh_logged(self) -> None:
        try:
            await self.refresh()
        except Exception:
            log.exception("Market figures reload failed; still serving the previous ones")

    def start_periodic_refresh(self) -> None:
        """Reload every `ttl_s` seconds, so a quiet site never serves a long-stale snapshot."""

        async def loop() -> None:
            while True:
                await asyncio.sleep(self.ttl_s)
                await self._refresh_logged()

        if self._periodic is None or self._periodic.done():
            self._periodic = asyncio.get_running_loop().create_task(loop())

    async def stop(self) -> None:
        """Cancel the background reloads (app shutdown)."""
        for task in (self._periodic, self._refreshing):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._periodic = self._refreshing = None

    def clear(self) -> None:
        """Drop the snapshot so the next `get` reloads it."""
        self._snapshot = None
        self._loaded_at = 0.0


_cache = MarketCache()


def get_market_cache() -> MarketCache:
    """Return this worker's cache (FastAPI dependency)."""
    return _cache
