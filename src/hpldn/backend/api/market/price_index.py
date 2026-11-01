"""
The UK HPI as the market layer needs it: a level on any day, past or projected.

Nearby sales are evidence from different months. Comparing a sale from August
2024 with one from November 2025 at face value, and calling the middle of them
"what similar homes sold for", quietly assumes the market stood still between
them -- next to a card that says prices moved 16 % over the year. This module
supplies the factor that moves each sale to the day being valued.

When the served model has loaded, the factor is read from its local price
index (`local_index`: the sale's postcode district and type, from repeat
sales), the same series `hpp_inference` moves the model price along, so a
comparable and the model price beside it are adjusted by one series rather
than two. Until then, or for an area it does not cover, the London UK HPI and
its projection (`hpi_projection`) are used.
"""

import logging
from datetime import date
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.schemas import PropertyType

# `production.ukhpi` breaks the average price out by the four Land Registry
# property types; `PropertyType` is the API's vocabulary, so map between them.
_INDEX_COLUMN_BY_TYPE: dict[str, str] = {
    "flat": "avg_price_flats_maisonettes",
    "maisonette": "avg_price_flats_maisonettes",
    "terraced": "avg_price_terraced",
    "semi-detached": "avg_price_semi_detached",
    "detached": "avg_price_detached",
}
_ALL_TYPES_COLUMN = "avg_price_all_property_types"

# The Land Registry type letter each API type is projected as, matching
# `hpi_projection.PRICE_COLUMN_BY_TYPE`.
_PPD_LETTER_BY_TYPE: dict[str, str] = {
    "flat": "F",
    "maisonette": "F",
    "terraced": "T",
    "semi-detached": "S",
    "detached": "D",
}

# A time adjustment is capped this far either way. The index is London-wide, so
# a very old comparable should not be stretched without limit to reach today.
MAX_TIME_SCALE = 1.35

_INDEX_SQL = text(
    f"""
    SELECT period, {_ALL_TYPES_COLUMN},
           {", ".join(sorted(set(_INDEX_COLUMN_BY_TYPE.values())))}
    FROM production.ukhpi
    WHERE published_date IS NOT NULL
    ORDER BY period
    """
)


log = logging.getLogger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp `value` into ``[low, high]``."""
    return min(high, max(low, value))


class PriceIndex:
    """
    Monthly London price levels by property type, with the fitted forward projection.

    `level` answers for any day: the month's published figure for a day the
    index covers, and the last figure carried forward by
    `hpi_projection.IndexProjection.factor` beyond it.
    """

    def __init__(self, rows: list[dict], projection=None, local_source: Optional[Callable[[], object]] = None) -> None:
        self.rows = sorted(rows, key=lambda r: r["period"])
        self.projection = projection
        self.last_period: Optional[date] = self.rows[-1]["period"] if self.rows else None
        # Returns the served model's `local_index.LocalIndex`, or None while it loads.
        self.local_source = local_source

    def _row_on(self, day: date) -> Optional[dict]:
        """Return the latest month at or before `day`, or the first month when `day` precedes it."""
        if not self.rows:
            return None
        found = None
        for row in self.rows:
            if row["period"] <= day:
                found = row
            else:
                break
        return found or self.rows[0]

    def level(self, day: date, property_type: PropertyType) -> Optional[float]:
        """Return the index level for `property_type` on `day`, projected past the last month."""
        row = self._row_on(day)
        if row is None:
            return None
        column = _INDEX_COLUMN_BY_TYPE.get(property_type, _ALL_TYPES_COLUMN)
        value = row.get(column) or row.get(_ALL_TYPES_COLUMN)
        if value is None:
            return None
        value = float(value)
        if self.last_period is not None and day > self.last_period and self.projection is not None:
            value *= self.projection.factor(day, _PPD_LETTER_BY_TYPE.get(property_type, "F"))
        return value

    def factor(
        self,
        sold_on: date,
        valuation_date: date,
        property_type: PropertyType,
        district: Optional[str] = None,
    ) -> float:
        """
        Return what a sale on `sold_on` would fetch on `valuation_date`, as a multiplier.

        Along the local index for `district` and the type when the served
        model has one loaded, otherwise the London index. 1.0 when either
        level is unavailable, and clamped to `MAX_TIME_SCALE` either way so
        one very old sale cannot be stretched to carry the figure.
        """
        local = None
        if self.local_source is not None:
            try:
                local = self.local_source()
            except Exception:  # noqa: BLE001 -- the London index still answers
                local = None
        if local is not None:
            try:
                moved = local.factor(sold_on, valuation_date, district, _PPD_LETTER_BY_TYPE.get(property_type, "F"))
                return _clamp(moved, 1.0 / MAX_TIME_SCALE, MAX_TIME_SCALE)
            except Exception as exc:  # noqa: BLE001 -- fall back to London below
                log.warning("local price index could not move a %s sale (%s); using London", property_type, exc)
        was = self.level(sold_on, property_type)
        now = self.level(valuation_date, property_type)
        if not was or not now:
            return 1.0
        return _clamp(now / was, 1.0 / MAX_TIME_SCALE, MAX_TIME_SCALE)


async def fetch_price_index(session: AsyncSession) -> PriceIndex:
    """
    Read ``production.ukhpi`` and pair it with the served model's fitted projection.

    Called once per cache refresh: the table is 378 rows and changes monthly.
    Without a model, or a model with no projection sidecar, the index simply
    stops at its last published month.

    An unreadable index returns an empty one rather than failing: every area
    figure is served off the same snapshot, and a time adjustment is worth less
    than the figures it would take down with it.
    """
    try:
        rows = [dict(row) for row in (await session.execute(_INDEX_SQL)).mappings()]
    except Exception as exc:  # noqa: BLE001 -- see the docstring: never fail the snapshot
        log.warning("price index unavailable (%s); nearby sales are not time-adjusted", exc)
        return PriceIndex([], None)
    projection = None
    try:
        from hpldn.backend.scripts import hpi_projection

        from hpldn.backend.api.market.pricing import resolve_model_path

        # The bundle's projection, read after its signature and hash are checked.
        path = resolve_model_path()
        if path is not None:
            import pandas as pd

            projection = hpi_projection.IndexProjection(
                pd.DataFrame(rows), hpi_projection.load_params(path)
            )
    except Exception:  # noqa: BLE001 -- an index without a projection still works
        projection = None
    return PriceIndex(rows, projection, local_source=_served_local_index)


def _served_local_index():
    """Return the served model's local price index once it has loaded, else None (never blocks)."""
    from hpldn.backend.api.market.pricing import loaded_local_index

    return loaded_local_index()


__all__ = ["PriceIndex", "fetch_price_index", "MAX_TIME_SCALE"]
