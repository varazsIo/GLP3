"""
Backtest the estimator behind POST /estimate against real sales.

Read-only: connects as the read API does, inside a rolled-back READ ONLY
transaction. Every sized sale in the covered areas over the latest months is
priced by `HeuristicEstimator` with its own area, type, floor area, the
bedroom count its size suggests and its similar-sales count. Reports the share
within 10% and 20% of the price paid, and the similar-count figures the
confidence thresholds are calibrated on. In-sample, so slightly flattering.

Usage::

    python -m hpldn.backend.scripts.backtest_heuristic [--months 12]
"""

import argparse
import asyncio
import bisect
import statistics
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hpldn.backend.api.config import get_settings
from hpldn.backend.api.market import repository
from hpldn.backend.api.market.districts import (
    DISTRICTS,
    MAX_FLOOR_AREA,
    MIN_FLOOR_AREA,
    area_of,
    district_members,
    evidence_members,
)
from hpldn.backend.api.market.estimator import (
    HeuristicEstimator,
    confidence_for,
    expected_bedrooms,
)
from hpldn.backend.api.market.queries import _TYPE_KEY
from hpldn.backend.api.market.repository import SIMILAR_FLOOR_AREA_SHARE
from hpldn.backend.api.schemas import ResolvedEstimateRequest

BAND_LOW = 350_000
BAND_HIGH = 700_000
THRESHOLDS = (0.10, 0.20)

# Every sale with a type and floor area in the window; the backtest sample is
# the sized ones in the latest :months months before the latest sale.
SALES_SQL = text(
    f"""
    SELECT h.postcode_district AS outward,
           h.price,
           h.total_floor_area,
           {_TYPE_KEY} AS type_key,
           h.dateoftransfer > CAST(:latest AS date) - make_interval(months => :months)
               AS in_sample
      FROM production.london_houses h
     WHERE h.postcode_district = ANY(:members)
       AND h.dateoftransfer >= :date_from
       AND h.total_floor_area IS NOT NULL
    """
)


class SimilarCounter:
    """
    Answer ``SIMILAR_COUNT_SQL`` for many requests from memory.

    Floor areas are grouped per evidence scope and type and sorted, so each
    count is two bisects.

    Parameters
    ----------
    rows : iterable of mapping
        Sales with ``outward``, ``type_key`` and ``total_floor_area``.
    """

    def __init__(self, rows) -> None:
        areas: dict[tuple, list[float]] = defaultdict(list)
        for row in rows:
            if row["type_key"] is not None:
                areas[(self.scope(row["outward"]), row["type_key"])].append(
                    float(row["total_floor_area"])
                )
        self._areas = {key: sorted(values) for key, values in areas.items()}
        self._scope_sales: dict[tuple, int] = defaultdict(int)
        for (scope, _), values in self._areas.items():
            self._scope_sales[scope] += len(values)

    @staticmethod
    def scope(outward: str) -> tuple[str, ...]:
        """Return the evidence scope for an outward code."""
        return tuple(evidence_members(outward, area_of(outward)))

    def count(self, outward: str, property_type: str, floor_area: float) -> int:
        """Count same-type sales in scope within the similar floor-area band."""
        values = self._areas.get((self.scope(outward), property_type), [])
        low = floor_area * (1 - SIMILAR_FLOOR_AREA_SHARE)
        high = floor_area * (1 + SIMILAR_FLOOR_AREA_SHARE)
        return bisect.bisect_right(values, high) - bisect.bisect_left(values, low)

    def scope_sales(self, outward: str) -> int:
        """Count the sized sales in an outward code's evidence scope, the ``high`` threshold's input."""
        return self._scope_sales.get(self.scope(outward), 0)


def percentiles(values: list[int], points=(5, 10, 20, 25, 33, 50, 67, 75, 90)) -> str:
    """Format the given percentiles of `values` on one line."""
    ordered = sorted(values)
    if not ordered:
        return "no values"
    return " · ".join(
        f"P{p} {ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]:,}" for p in points
    )


@dataclass(frozen=True)
class Outcome:
    """One sale's price paid and estimated value."""

    price: int
    value: int

    @property
    def error(self) -> float:
        """Absolute error as a fraction of the price paid."""
        return abs(self.value - self.price) / self.price


def summarise(label: str, outcomes: list[Outcome]) -> str:
    """Format the within-threshold shares and median absolute error of `outcomes`."""
    if not outcomes:
        return f"{label}: no sales"
    errors = [outcome.error for outcome in outcomes]
    shares = "  ".join(
        f"within {int(t * 100)}%: {sum(e <= t for e in errors) / len(errors):6.1%}"
        for t in THRESHOLDS
    )
    return (
        f"{label:<24} n={len(outcomes):>6,}  {shares}  "
        f"median abs error: {statistics.median(errors):5.1%}"
    )


async def run(months: int) -> list[str]:
    """
    Run the backtest over the latest `months` months and return the report lines.

    Requests are built with ``model_construct`` because the estimator only
    reads type, bedrooms and floor area, and a sale has no full postcode.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, connect_args={"prepare_threshold": None})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    estimator = HeuristicEstimator()
    try:
        async with maker() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            try:
                window = await repository.fetch_window(session)
                districts = await repository.fetch_district_stats(session, window)
                rows = (
                    await session.execute(
                        SALES_SQL,
                        {
                            "members": [m for code in districts for m in district_members(code)],
                            "date_from": repository.window_start(window),
                            "latest": window.latest_sale,
                            "months": months,
                        },
                    )
                ).mappings().all()
            finally:
                await session.rollback()
    finally:
        await engine.dispose()

    similar = SimilarCounter(rows)
    outcomes: list[Outcome] = []
    confidences: dict[str, int] = defaultdict(int)
    calibration: list[int] = []
    by_type: dict[str, list[int]] = defaultdict(list)
    skipped = 0
    for row in rows:
        floor_area = float(row["total_floor_area"])
        if not MIN_FLOOR_AREA <= floor_area <= MAX_FLOOR_AREA:
            continue
        stats = districts.get(area_of(row["outward"]))
        if stats is None or row["type_key"] is None:
            skipped += row["in_sample"] and row["price"] > 0
            continue
        similar_count = similar.count(row["outward"], row["type_key"], floor_area)
        calibration.append(similar_count)
        by_type[row["type_key"]].append(similar_count)
        if not row["in_sample"] or row["price"] <= 0:
            continue
        request = ResolvedEstimateRequest.model_construct(
            postcode=row["outward"],
            property_type=row["type_key"],
            bedrooms=expected_bedrooms(floor_area),
            floor_area_sqm=floor_area,
            district=stats.area.code,
        )
        scope_sales = similar.scope_sales(row["outward"])
        core = estimator.estimate(
            request, stats.area, [], similar_count=similar_count, evidence_sales_count=scope_sales
        )
        # A live estimate is "fair" without nearby sales; the backtest fetches
        # none, so the mix assumes they exist, as they do for almost every sale.
        confidence, _ = confidence_for(scope_sales, similar_count, low_data=stats.area.low_data)
        confidences[confidence] += 1
        outcomes.append(Outcome(price=int(row["price"]), value=core.value))

    in_band = [o for o in outcomes if BAND_LOW <= o.price <= BAND_HIGH]
    below = [o for o in outcomes if o.price < BAND_LOW]
    above = [o for o in outcomes if o.price > BAND_HIGH]
    sales_counts = sorted(stats.area.sales_count for stats in districts.values())
    thinnest = sorted(districts.values(), key=lambda stats: stats.area.sales_count)[:6]
    mix = "  ".join(
        f"{name}: {confidences[name] / max(1, len(outcomes)):6.1%}" for name in ("high", "medium", "fair")
    )
    return [
        f"HeuristicEstimator backtest as {settings.safe_target}",
        f"sales: latest {months} months to {window.latest_sale.isoformat()}, sized "
        f"({MIN_FLOOR_AREA}-{MAX_FLOOR_AREA} m2), {len(districts)} of {len(DISTRICTS)} "
        f"areas; bedrooms = expected_bedrooms(floor area); {skipped} rows skipped",
        summarise("all sales", outcomes),
        summarise("£350k-£700k (price paid)", in_band),
        summarise("under £350k", below),
        summarise("over £700k", above),
        f"confidence mix           {mix}",
        "note: in-sample -- district £/m2 is computed over a window that includes these sales",
        "",
        f"calibration: {len(calibration):,} sized sales in the {window.from_year}-{window.to_year} "
        "window, each treated as a request",
        f"  similar_count percentiles  {percentiles(calibration)}",
        "  median by type             "
        + " · ".join(
            f"{name} {int(statistics.median(values)):,}"
            for name, values in sorted(by_type.items(), key=lambda item: -statistics.median(item[1]))
        ),
        f"  area sales_count           min {sales_counts[0]:,} · "
        f"median {int(statistics.median(sales_counts)):,} · max {sales_counts[-1]:,}",
        "  thinnest areas             "
        + " · ".join(f"{s.area.code} {s.area.sales_count:,}" for s in thinnest),
    ]


def main() -> None:
    """Parse ``--months`` and print the report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--months", type=int, default=12, help="how many months of the latest sales to test (default 12)"
    )
    args = parser.parse_args()
    for line in asyncio.run(run(args.months)):
        print(line)


if __name__ == "__main__":
    main()
