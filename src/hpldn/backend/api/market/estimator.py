"""
How an estimate is made, behind one interface.

``POST /estimate`` asks the `Estimator` returned by `get_estimator` for a
value, range and confidence, and builds the drivers, nearby sales and rent
around it. `HeuristicEstimator` is the formula the frontend once ran in the
browser, ported with JavaScript's half-up rounding; a model-backed estimator
can replace the dependency without changing the response shape.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from hpldn.backend.api.market.districts import round_half_up, round_to_half_up
from hpldn.backend.api.schemas import (
    AreaStats,
    Comparable,
    Confidence,
    Driver,
    DriverTone,
    PropertyType,
    ResolvedEstimateRequest,
)

TYPE_FACTOR: dict[PropertyType, float] = {
    "flat": 0.94,
    "maisonette": 0.97,
    "terraced": 1.0,
    "semi-detached": 1.06,
    "detached": 1.18,
}

# Calibrated with scripts/backtest_heuristic.py; see `confidence_for`.
HIGH_MIN_SIMILAR = 150
HIGH_MIN_DISTRICT_SALES = 750
MEDIUM_MIN_SIMILAR = 25

SPREAD_PERCENT: dict[Confidence, int] = {"high": 7, "medium": 10, "fair": 13}

# The weight the nearby same-type sales get against the area formula once
# `FULL_WEIGHT_COMPARABLES` of them are found (pro rata below that). Chosen by a
# leave-one-out backtest of 1,496 recent sales: 0.6 took the share within 10 %
# of the price paid from 35.5 % (formula alone) to 43.2 %.
COMPARABLES_WEIGHT = 0.6
FULL_WEIGHT_COMPARABLES = 3

# When the formula and the nearby sales disagree by more than this share, the
# confidence drops a level: the evidence points two ways.
DIVERGENCE_SHARE = 0.25

# The nearby sales help or hold back the value beyond this share of the formula.
COMPARABLES_TONE_SHARE = 0.05

# The word for each transport score from 1 to 5. Must match `WORDS` in the
# frontend's area/TransportMeter.tsx, so the reason and the area tile agree.
TRANSPORT_WORDS = ("Limited", "Modest", "Good", "Very good", "Excellent")

_LOWER: dict[Confidence, Confidence] = {"high": "medium", "medium": "fair", "fair": "fair"}

# A model price's range half-width, as a share of the value, at or below which
# the estimate is called "high" or "medium" confidence. The model's band is
# measured per price band, type and evidence thickness, so these read its own
# uncertainty rather than counting sales the way `confidence_for` does.
MODEL_HIGH_SPREAD = 8
MODEL_MEDIUM_SPREAD = 12

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EstimateCore:
    """
    The value, range and confidence an `Estimator` produces.

    ``basis`` says which estimator produced it: ``"model"`` for the trained
    CatBoost model, ``"area"`` for the area formula it falls back to.
    """

    value: int
    low: int
    high: int
    confidence: Confidence
    spread_percent: int
    basis: str = "area"
    projection_uplift: float | None = None
    projected_from: date | None = None
    index_area: str | None = None


class Estimator(Protocol):
    """
    Interface for anything that can price a resolved request.

    `evidence_sales_count` counts the sales in the estimate's evidence scope
    (an outer area's outward code, not the whole area) over the window.
    """

    def estimate(
        self,
        request: ResolvedEstimateRequest,
        area: AreaStats,
        comparables: list[Comparable],
        similar_count: int,
        evidence_sales_count: int,
    ) -> EstimateCore: ...


def confidence_for(
    sales_count: int,
    similar_count: int,
    has_comparables: bool = True,
    low_data: bool = False,
) -> tuple[Confidence, int]:
    """
    Return the confidence and the percentage the range spans either side.

    Parameters
    ----------
    sales_count : int
        Sales in the evidence scope over the window: the same scope
        `similar_count` is counted over.
    similar_count : int
        Same-type sales within 20% of the floor area in the evidence scope.
    has_comparables : bool, optional
        Whether any nearby sale backs the estimate; without one it is "fair".
    low_data : bool, optional
        Whether the area is flagged low-data; if so it is "fair".

    Returns
    -------
    confidence : {"high", "medium", "fair"}
        The confidence label.
    spread : int
        The range half-width in percent.

    Notes
    -----
    Thresholds were calibrated against every sized sale in the 2023-2026
    window treated as a request. ``"high"`` is the better-evidenced half of
    requests in an area with at least a median area's sales, which keeps the
    thin central groups out; ``"medium"`` is everything above the thinnest
    tenth; ``"fair"`` is the rest, mostly maisonettes and detached houses.
    Over the latest twelve months that splits about 43% / 49% / 8%.
    """
    if not has_comparables or low_data:
        confidence: Confidence = "fair"
    elif similar_count >= HIGH_MIN_SIMILAR and sales_count >= HIGH_MIN_DISTRICT_SALES:
        confidence = "high"
    elif similar_count >= MEDIUM_MIN_SIMILAR:
        confidence = "medium"
    else:
        confidence = "fair"
    return confidence, SPREAD_PERCENT[confidence]


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp `value` into ``[low, high]``."""
    return min(high, max(low, value))


def adjusted_prices(comparables: list[Comparable], floor_area_sqm: float | None = None) -> list[float]:
    """
    Return each same-type nearby sale at the requested size and the valuation date.

    `repository.adjusted_price` normally did the scaling when the sale was
    picked, so the figure here is exactly the one shown against that sale on
    screen. A `Comparable` built without it is still scaled for size, so the
    list is always "what these sales say about a home of this size" whatever
    built it -- returning the raw price would quietly drop the adjustment.

    Broader-type sales are left out: they never set the number.
    """
    from hpldn.backend.api.market.repository import adjusted_price

    same = [comp for comp in comparables if comp.match_type == "same" and comp.floor_area_sqm > 0]
    return [
        float(
            comp.adjusted_price
            if comp.adjusted_price is not None
            else adjusted_price(
                comp.price,
                comp.floor_area_sqm,
                floor_area_sqm if floor_area_sqm else comp.floor_area_sqm,
                comp.sold_on,
                None,
                comp.property_type,
                None,
            )
        )
        for comp in same
    ]


def comparables_value(floor_area_sqm: float, comparables: list[Comparable]) -> tuple[float, int] | None:
    """
    Return what the nearby same-type sales say the home is worth, and how many there are.

    The median of `adjusted_prices` -- each sale at the requested floor area
    and the day being valued. None without a same-type sale; broader-type sales
    never set the figure.
    """
    scaled = adjusted_prices(comparables, floor_area_sqm)
    if not scaled:
        return None
    return _median(scaled), len(scaled)


def expected_bedrooms(floor_area_sqm: float) -> int:
    """Return how many bedrooms a home of this floor area usually has."""
    return int(_clamp(round_half_up((floor_area_sqm - 18) / 24), 1, 6))


def area_formula_value(request: ResolvedEstimateRequest, area: AreaStats) -> float:
    """
    Return the area formula's value: price per m² times floor area, adjusted.

    The adjustments are for the type of home, the bedroom count against what the
    size suggests, and transport access (none when access is not known).
    """
    base = area.estimator_price_per_sqm * request.floor_area_sqm
    bedroom_gap = request.bedrooms - expected_bedrooms(request.floor_area_sqm)
    bedroom_factor = _clamp(1 + bedroom_gap * 0.015, 0.94, 1.06)
    score = 3 if area.transport_score is None else area.transport_score
    transport_factor = 1 + (score - 3) * 0.018
    return base * TYPE_FACTOR[request.property_type] * bedroom_factor * transport_factor


class HeuristicEstimator:
    """
    Price a home from its area's price per m² and the nearby sales.

    The formula (`area_formula_value`) is the district's median price per m²
    over the window (``AreaStats.estimator_price_per_sqm``) times floor area,
    adjusted. It is blended with `comparables_value`
    at `COMPARABLES_WEIGHT`, so the value never ignores the sales shown beside it;
    the confidence drops a level when the two disagree by `DIVERGENCE_SHARE`, and
    the range is stretched to reach the nearby sales' figure when it falls outside.
    """

    def estimate(
        self,
        request: ResolvedEstimateRequest,
        area: AreaStats,
        comparables: list[Comparable],
        similar_count: int,
        evidence_sales_count: int,
    ) -> EstimateCore:
        formula = area_formula_value(request, area)

        confidence, _ = confidence_for(
            evidence_sales_count,
            similar_count,
            has_comparables=bool(comparables),
            low_data=area.low_data,
        )
        raw = formula
        nearby = comparables_value(request.floor_area_sqm, comparables)
        if nearby is not None:
            nearby_value, count = nearby
            weight = COMPARABLES_WEIGHT * min(count, FULL_WEIGHT_COMPARABLES) / FULL_WEIGHT_COMPARABLES
            raw = (1 - weight) * formula + weight * nearby_value
            if abs(formula - nearby_value) / nearby_value > DIVERGENCE_SHARE:
                confidence = _LOWER[confidence]
        spread = SPREAD_PERCENT[confidence]

        value = round_to_half_up(raw, 1000)
        low = round_to_half_up(value * (1 - spread / 100), 1000)
        high = round_to_half_up(value * (1 + spread / 100), 1000)
        if nearby is not None:
            # The range always reaches what the nearby sales say.
            low = min(low, round_to_half_up(nearby[0], 1000))
            high = max(high, round_to_half_up(nearby[0], 1000))
        return EstimateCore(
            value=value,
            low=low,
            high=high,
            confidence=confidence,
            spread_percent=spread,
        )


def confidence_from_spread(spread_percent: int) -> Confidence:
    """Return the confidence a measured range half-width implies."""
    if spread_percent <= MODEL_HIGH_SPREAD:
        return "high"
    if spread_percent <= MODEL_MEDIUM_SPREAD:
        return "medium"
    return "fair"


class ModelEstimator:
    """
    Price a details estimate with the trained CatBoost model.

    `HeuristicEstimator` alone -- an area price per m2 times floor area,
    adjusted and blended with nearby sales -- is a formula, not the model that
    prices a named address, so the same home could be worth two different
    things depending on which form the landlord filled in. This runs the model, through
    `hpp_inference.PricingService.price_details`, which builds the row a sale
    of a home of this type and size in this postcode would have and scores it.

    The range is the model's own measured error for that price band, type and
    evidence thickness, so it is the same definition ``POST /price`` reports.

    `fallback` prices anything the model cannot: a postcode with no sales near
    it, a missing model, or any failure inside the pricing path. Falling back
    is logged, because silently answering with a different method is how the
    two numbers drifted apart in the first place.
    """

    def __init__(self, pricer, fallback: Estimator | None = None) -> None:
        self.pricer = pricer
        self.fallback = fallback or HeuristicEstimator()

    def estimate(
        self,
        request: ResolvedEstimateRequest,
        area: AreaStats,
        comparables: list[Comparable],
        similar_count: int,
        evidence_sales_count: int,
    ) -> EstimateCore:
        """Return the model's price, or the area formula's when the model cannot give one."""
        core = self._from_model(request)
        if core is not None:
            return core
        return self.fallback.estimate(request, area, comparables, similar_count, evidence_sales_count)

    def _from_model(self, request: ResolvedEstimateRequest) -> EstimateCore | None:
        """Return the model's price for `request`, or None when it cannot be had."""
        if self.pricer is None:
            return None
        from hpldn.backend.scripts.hpp_inference import PricingError

        try:
            service = self.pricer.service()
            prediction = service.price_details(
                postcode=request.postcode,
                property_type=request.property_type,
                floor_area_sqm=request.floor_area_sqm,
                # The form asks for bedrooms; the model was trained on
                # habitable rooms, which count the living room too.
                habitable_rooms=float(request.bedrooms) + 1.0,
                sale_date=request.sale_date or (date.today() + timedelta(days=1)),
            )
        except (PricingError, RuntimeError) as exc:
            log.info("estimate: model could not price %s (%s); using the area formula", request.postcode, exc)
            return None
        except Exception:  # noqa: BLE001 -- an estimate must still answer
            log.exception("estimate: model failed for %s; using the area formula", request.postcode)
            return None
        if prediction.low is None or prediction.high is None:
            log.info("estimate: model has no measured range; using the area formula")
            return None

        # To the pound, like POST /price: the price moves every day with the date.
        value = round_to_half_up(prediction.value, 1)
        low = round_to_half_up(prediction.low, 1000)
        high = round_to_half_up(prediction.high, 1000)
        spread = int(round_half_up(100 * (high - low) / (2 * value))) if value else 0
        return EstimateCore(
            value=value,
            low=low,
            high=high,
            confidence=confidence_from_spread(spread),
            spread_percent=spread,
            basis="model",
            projection_uplift=prediction.projection_uplift,
            projected_from=prediction.projected_from,
            index_area=getattr(prediction, "index_area", None),
        )


def get_estimator() -> Estimator:
    """
    Return the estimator ``POST /estimate`` uses (FastAPI dependency).

    The model when one is loaded, with the area formula behind it; the area
    formula alone when there is no model at all.
    """
    from hpldn.backend.api.market.pricing import get_pricer

    pricer = get_pricer()
    if pricer is None:
        return HeuristicEstimator()
    return ModelEstimator(pricer)


def _group(digits: int) -> str:
    """Format an integer with thousands separators."""
    return f"{digits:,}"


def format_gbp(value: float) -> str:
    """Format as whole pounds, e.g. ``"£520,000"``, matching the frontend's ``formatGBP``."""
    rounded = round_half_up(value)
    return ("-" if rounded < 0 else "") + "£" + _group(abs(rounded))


def format_gbp_compact(value: float) -> str:
    """Format compactly, e.g. ``"£520k"`` or ``"£1.34m"``, matching ``formatGBPCompact``."""
    magnitude = abs(value)
    sign = "-" if value < 0 else ""
    if magnitude >= 1_000_000:
        return sign + "£" + f"{round_half_up(magnitude / 10_000) / 100:.2f}" + "m"
    if magnitude >= 1_000:
        return sign + "£" + str(round_half_up(magnitude / 1_000)) + "k"
    return format_gbp(value)


def format_percent(value: float) -> str:
    """Format to one decimal place, e.g. ``"4.0%"``, matching ``formatPercent``."""
    return ("-" if value < 0 else "") + f"{abs(value):.1f}%"


def _median(values: list[float]) -> float | None:
    """Return the median of `values`, or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return (ordered[mid - 1] + ordered[mid]) / 2 if len(ordered) % 2 == 0 else ordered[mid]


def _walk_detail(area: AreaStats, place: str) -> str:
    """Describe the walk to the nearest station, or say it is not known."""
    if area.walk_to_station_mins is None:
        return "Walking distance to a station not known"
    return f"{area.walk_to_station_mins} min walk to {place}"


def _transport_driver(area: AreaStats) -> Driver:
    """Describe the area's transport access in the words the area's transport meter uses."""
    if area.transport_score is None:
        return Driver(
            id="transport",
            label="Transport access not known",
            detail=_walk_detail(area, "the nearest station"),
            tone="neutral",
        )
    score = int(_clamp(round_half_up(area.transport_score), 1, 5))
    label = TRANSPORT_WORDS[score - 1] + " transport"
    if score >= 4:
        return Driver(id="transport", label=label, detail=_walk_detail(area, "the Tube"), tone="positive")
    tone: DriverTone = "neutral" if score == 3 else "caution"
    return Driver(id="transport", label=label, detail=_walk_detail(area, "the nearest station"), tone=tone)


def _momentum_driver(area: AreaStats) -> Driver:
    """Describe the area's price movement over the last year."""
    if area.movement1y is None:
        return Driver(
            id="momentum",
            label="Not enough sales to show a trend",
            detail="Too few recent sales here to measure the last year",
            tone="neutral",
        )
    if area.movement1y >= 2:
        return Driver(
            id="momentum",
            label="Prices here are rising",
            detail="Up " + format_percent(area.movement1y) + " over the last year",
            tone="positive",
        )
    if area.movement1y >= 0:
        return Driver(
            id="momentum",
            label="Prices here are steady",
            detail="Up " + format_percent(area.movement1y) + " over the last year",
            tone="neutral",
        )
    return Driver(
        id="momentum",
        label="Prices here have eased",
        detail="Down " + format_percent(abs(area.movement1y)) + " over the last year",
        tone="caution",
    )


def _size_driver(
    area: AreaStats, request: ResolvedEstimateRequest, comparables: list[Comparable]
) -> Driver:
    """Describe the home's size against the nearby sales (70 m² without any)."""
    nearby = _median([comp.floor_area_sqm for comp in comparables])
    typical = nearby or 70
    ratio = request.floor_area_sqm / typical
    where = "the typical sale in " + area.name if nearby else "a typical 70 m² London home"
    if ratio >= 1.15:
        return Driver(
            id="size",
            label="Larger than most homes nearby" if nearby else "Larger than a typical home",
            detail="Roomier than " + where,
            tone="positive",
        )
    if ratio <= 0.85:
        return Driver(
            id="size",
            label="Smaller than most homes nearby" if nearby else "Smaller than a typical home",
            detail="Compact against " + where,
            tone="caution",
        )
    return Driver(
        id="size",
        label="A typical size for the street" if nearby else "A typical size",
        detail="In line with " + where,
        tone="neutral",
    )


def _comparables_driver(
    area: AreaStats, request: ResolvedEstimateRequest, comparables: list[Comparable]
) -> Driver:
    """
    Describe what the nearby sales sold for, or that there are none.

    The figure quoted is the one the estimate is built from: the *middle* of
    the same-type sales, each already scaled to the requested floor area and
    moved to the valuation date, not the plain median of the prices paid,
    which would be neither the number that drove the estimate nor anything a
    reader could derive from the sales listed beside it. Quoting the range as
    well as the middle, and saying "middle", leaves nothing to infer.

    The tone says whether the sales pulled the value up or down from the area
    formula (`COMPARABLES_TONE_SHARE` either way), since the estimate blends
    the two.
    """
    scaled = adjusted_prices(comparables, request.floor_area_sqm)
    if scaled:
        tone: DriverTone = "neutral"
        nearby = comparables_value(request.floor_area_sqm, comparables)
        formula = area_formula_value(request, area)
        if nearby is not None and formula:
            gap = nearby[0] / formula - 1
            if gap > COMPARABLES_TONE_SHARE:
                tone = "positive"
            elif gap < -COMPARABLES_TONE_SHARE:
                tone = "caution"
        middle = format_gbp_compact(round_to_half_up(_median(scaled), 5000))
        if len(scaled) == 1:
            detail = "One nearby sale, worth " + middle + " at this size today"
        else:
            detail = (
                format_gbp_compact(round_to_half_up(min(scaled), 5000))
                + "-"
                + format_gbp_compact(round_to_half_up(max(scaled), 5000))
                + " across "
                + str(len(scaled))
                + " nearby sales, middle "
                + middle
                + ", at this size today"
            )
        return Driver(
            id="comparables",
            label="What similar homes nearby sold for",
            detail=detail,
            tone=tone,
        )
    if comparables:
        kind = "flats and maisonettes" if request.property_type in ("flat", "maisonette") else "houses"
        return Driver(
            id="comparables",
            label="Few homes like this sold nearby",
            detail="Other "
            + kind
            + " nearby sold for around "
            + format_gbp_compact(
                round_to_half_up(
                    _median(
                        [
                            float(c.adjusted_price if c.adjusted_price is not None else c.price)
                            for c in comparables
                        ]
                    ),
                    5000,
                )
            ),
            tone="caution",
        )
    return Driver(
        id="comparables",
        label="Few homes like this sold nearby",
        detail="Not enough recent similar sales nearby to compare",
        tone="caution",
    )


def build_drivers(
    area: AreaStats, request: ResolvedEstimateRequest, comparables: list[Comparable]
) -> list[Driver]:
    """
    Return the plain-English reasons shown under the figure.

    Ordered nearby sales, price momentum, transport, then size. The wording
    names no model, table or column, and never shows a figure the data does
    not support.
    """
    return [
        _comparables_driver(area, request, comparables),
        _momentum_driver(area),
        _transport_driver(area),
        _size_driver(area, request, comparables),
    ]


def suggested_monthly_rent(bedrooms: int, area: AreaStats) -> int | None:
    """
    Return the borough's mean monthly rent for a home with `bedrooms`.

    Five or six bedrooms take the four-or-more figure. Falls back to the
    borough's overall mean when ONS publishes no figure for that count, and is
    None when the borough has no published rent (the City of London).
    """
    rents = area.bedroom_rents
    if rents is not None:
        by_bedrooms = {1: rents.one_bed, 2: rents.two_bed, 3: rents.three_bed, 4: rents.four_plus_bed}
        rent = by_bedrooms.get(min(max(bedrooms, 1), 4))
        if rent is not None:
            return rent
    return area.typical_monthly_rent
