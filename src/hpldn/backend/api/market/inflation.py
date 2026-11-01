"""
UK consumer price inflation (CPIH) as the price card needs it.

The detailed analysis states past prices and rents in the pounds of the day a
home is priced for. ``production.cpih`` (migration 0032) holds ONS's CPIH
12-month rate for every month from January 1989 and an index chained from it;
the browser divides two months' index to move a figure between them, and
carries the latest month forward at its 12-month rate for a day past it.
"""

import logging

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.schemas import InflationMonth, InflationSeries

log = logging.getLogger(__name__)

CPIH_SQL = text("SELECT month, annual_rate, price_index, source FROM production.cpih ORDER BY month")


def series_from_rows(rows: list[dict]) -> InflationSeries | None:
    """Return the series for ``CPIH_SQL`` rows, or None when there are none."""
    if not rows:
        return None
    latest = rows[-1]
    return InflationSeries(
        source=latest["source"],
        latest_month=latest["month"],
        latest_annual_rate=float(latest["annual_rate"]),
        months=[InflationMonth(month=row["month"], index=float(row["price_index"])) for row in rows],
    )


async def fetch_inflation(session: AsyncSession) -> InflationSeries | None:
    """
    Return the CPIH series from ``production.cpih``.

    None when the table is empty or cannot be read (before migration 0032),
    so the rest of the market snapshot still loads.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(CPIH_SQL)).mappings().all()
    except DBAPIError as exc:
        log.warning("CPIH unavailable (%s): inflation-corrected figures served as unknown", exc.orig)
        return None
    return series_from_rows([dict(row) for row in rows])
