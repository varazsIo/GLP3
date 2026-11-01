"""
Run the market SQL and map its rows onto the API's schemas.

Every function takes the request's `AsyncSession`, so queries run under the
API's role (``hpldn_api`` in production: read-only, 5 s statement timeout).
Area prices are served to the pound, movements and yields to 0.1, the
deprivation percentile to three significant figures.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.market import queries
from hpldn.backend.api.market.districts import (
    DISTRICTS,
    MAX_FLOOR_AREA,
    MIN_FLOOR_AREA,
    WALK_KMH,
    comparable_radius_km,
    district_members,
    haversine_km,
    round_significant,
    round_to,
    street_name,
    transport_score,
    type_group,
)
from hpldn.backend.api.schemas import AreaStats, BedroomRents, Comparable, DataWindow, MortgageRates, PropertyType
from hpldn.backend.common.constants import MIN_PLAUSIBLE_PRICE

log = logging.getLogger(__name__)

WINDOW_FULL_YEARS = 3

COMPARABLE_CANDIDATES = 40

# Nearby sales must have sold within this many months of the latest sale.
COMPARABLE_RECENCY_MONTHS = 24

# Broader-type sales are only added while fewer same-type sales than this
# were found.
MIN_SAME_TYPE_COMPARABLES = 3

SIMILAR_FLOOR_AREA_SHARE = 0.20

# A nearby sale's price is scaled to the requested floor area by at most this
# factor either way, so one very different home cannot swing the figure. The
# same cap the estimator has always applied, moved here so `adjusted_price`
# carries it.
MAX_SIZE_SCALE = 1.25

# Price per m2 above which a Price Paid row is a bulk or non-arms-length
# transfer rather than a comparable. Mirrors hpp_cb's
# PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD, which drops the same rows from training.
MAX_PRICE_PER_SQM = 45_000.0

# An area with fewer sales in the figures' year is flagged `low_data`; one
# with fewer than `MIN_COVERED_SALES` over the window is not covered at all.
LOW_DATA_SALES = 50
MIN_COVERED_SALES = 10


# `LONDON_RENTS_SQL` column of each bedroom count's rent; 4 stands for four or more.
BEDROOM_RENT_COLUMNS = {1: "rent_one_bed", 2: "rent_two_bed", 3: "rent_three_bed", 4: "rent_four_plus_bed"}


@dataclass(frozen=True)
class BoroughRent:
    """
    A borough's latest mean monthly private rent and the month it describes.

    ``by_bedrooms`` maps a bedroom count (1-4, 4 = four or more) to that
    month's mean rent for such homes; a count ONS suppressed is absent.
    """

    month: date
    rent: int
    by_bedrooms: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DistrictStats:
    """
    A district's page figures and the median position of its sales.

    Attributes
    ----------
    area : AreaStats
        The figures served to the page.
    centre : tuple of float or None
        ``(latitude, longitude)`` of the area's sales, the origin for nearby
        sales when the request names the whole area.
    """

    area: AreaStats
    centre: tuple[float, float] | None


def window_start(window: DataWindow) -> date:
    """Return 1 January of the window's first year."""
    return date(window.from_year, 1, 1)


async def fetch_window(session: AsyncSession) -> DataWindow:
    """
    Return the data window: the latest sale's year and the three before it.

    Raises
    ------
    LookupError
        If ``production.london_houses`` holds no sales.
    """
    row = (await session.execute(queries.DATA_WINDOW_SQL)).mappings().one()
    latest: date = row["latest_sale"]
    if latest is None:
        raise LookupError("production.london_houses holds no sales")
    return DataWindow(
        from_year=latest.year - WINDOW_FULL_YEARS,
        to_year=latest.year,
        latest_sale=latest,
        total_sales=int(row["total_sales"]),
        first_sale_year=row["first_sale"].year,
    )


def _round1(value: float | None) -> float | None:
    """Round to one decimal place, keeping None."""
    return None if value is None else round(float(value), 1)


def _change(latest: float | None, earlier: float | None) -> float | None:
    """Return the percentage change from `earlier` to `latest`, or None without both."""
    if latest is None or not earlier:
        return None
    return (float(latest) / float(earlier) - 1) * 100


def area_stats_from_rows(
    row: dict[str, Any],
    year_row: dict[str, Any] | None,
    year: int,
    rents: dict[str, BoroughRent] | None = None,
) -> DistrictStats | None:
    """
    Map an area's ``DISTRICT_STATS_SQL`` and ``AREA_YEAR_STATS_SQL`` rows onto its figures.

    Parameters
    ----------
    row : dict
        The area's ``DISTRICT_STATS_SQL`` row, over the whole window: its
        borough, transport, EPC band, centre and the estimate's median £/m².
    year_row : dict or None
        The area's ``AREA_YEAR_STATS_SQL`` row: the figures shown.
    year : int
        The calendar year the figures describe (the latest sale's).
    rents : dict of str to BoroughRent, optional
        ``production.london_rents`` by borough; none by default.

    Returns
    -------
    DistrictStats or None
        None when the area has fewer than `MIN_COVERED_SALES` sales in the
        window, no sized sale to base an estimate on, or no sale in `year`.

    Notes
    -----
    The price is the mean price of the year's sales and the price per m² that
    mean over the year's mean floor area. The movements compare the year's
    mean price with the mean of the year before and of five years before,
    and are None when that year had no sales. The profile is the year's mean
    London deprivation percentile. See `AreaStats`.
    """
    code = row["code"]
    if int(row["sales_count"]) < MIN_COVERED_SALES or not row["price_per_sqm"]:
        return None
    if year_row is None or not year_row["sales_count"] or year_row["average_price"] is None:
        return None
    name = DISTRICTS[code]
    borough = row["local_authority"] or ""
    rent = (rents or {}).get(borough)
    average_price = int(round(float(year_row["average_price"])))
    floor_area = year_row["average_floor_area"]
    sales_count = int(year_row["sales_count"])
    movement1y = _change(year_row["average_price"], year_row["average_price_previous"])
    movement5y = _change(year_row["average_price"], year_row["average_price_5y_ago"])
    distance = row["station_distance_km"]
    imd = year_row["imd_pct_london"]

    area = AreaStats(
        code=code,
        name=name,
        borough=borough,
        average_price=average_price,
        average_price_per_sqm=(
            int(round(float(year_row["average_price"]) / float(floor_area))) if floor_area else None
        ),
        movement1y=_round1(movement1y),
        movement5y=_round1(movement5y),
        transport_score=transport_score(row["ptal_band_rank"]),
        walk_to_station_mins=max(1, round(distance / WALK_KMH * 60)) if distance is not None else None,
        rental_yield=round(rent.rent * 12 / average_price * 100, 1) if rent and average_price else None,
        typical_monthly_rent=rent.rent if rent else None,
        rent_month=rent.month if rent else None,
        bedroom_rents=(
            BedroomRents(
                one_bed=rent.by_bedrooms.get(1),
                two_bed=rent.by_bedrooms.get(2),
                three_bed=rent.by_bedrooms.get(3),
                four_plus_bed=rent.by_bedrooms.get(4),
            )
            if rent
            else None
        ),
        sales_count=sales_count,
        figures_year=year,
        area_profile_percent=round_significant(float(imd) * 100) if imd is not None else None,
        epc_band=row["epc_band"] or "",
        low_data=sales_count < LOW_DATA_SALES,
        estimator_price_per_sqm=round_to(float(row["price_per_sqm"]), 10),
    )
    lat, lon = row["centre_latitude"], row["centre_longitude"]
    return DistrictStats(area=area, centre=(lat, lon) if lat is not None and lon is not None else None)


def fallbacks_of(area: AreaStats) -> list[str]:
    """Return the names of `area`'s figures served as unknown, plus ``low_data``."""
    unknown = [
        name
        for name in (
            "average_price_per_sqm", "movement1y", "movement5y", "transport_score",
            "walk_to_station_mins", "rental_yield", "typical_monthly_rent", "area_profile_percent",
        )
        if getattr(area, name) is None
    ]
    return unknown + (["low_data"] if area.low_data else [])


async def fetch_district_stats(
    session: AsyncSession, window: DataWindow, codes: list[str] | None = None
) -> dict[str, DistrictStats]:
    """
    Return the areas' figures keyed by code, in `DISTRICTS` order.

    Parameters
    ----------
    session : AsyncSession
        The request's session.
    window : DataWindow
        The window the figures are computed over.
    codes : list of str, optional
        Areas to fetch; all of `DISTRICTS` by default.

    Returns
    -------
    dict of str to DistrictStats
        One entry per covered area (see `area_stats_from_row`). Which areas
        were dropped, and which figures were served as unknown, is logged
        once per load.
    """
    wanted = list(DISTRICTS) if codes is None else codes
    members: list[str] = []
    member_codes: list[str] = []
    for code in wanted:
        for member in district_members(code):
            members.append(member)
            member_codes.append(code)
    rows = (
        await session.execute(
            queries.DISTRICT_STATS_SQL,
            {
                "date_from": window_start(window),
                "members": members,
                "codes": member_codes,
                "min_floor_area": MIN_FLOOR_AREA,
                "max_floor_area": MAX_FLOOR_AREA,
            },
        )
    ).mappings().all()
    year_rows = (
        await session.execute(
            queries.AREA_YEAR_STATS_SQL,
            {"year": window.to_year, "members": members, "codes": member_codes},
        )
    ).mappings().all()
    by_year = {row["code"]: dict(row) for row in year_rows}
    rents = await fetch_london_rents(session)
    by_code = {
        row["code"]: area_stats_from_rows(dict(row), by_year.get(row["code"]), window.to_year, rents)
        for row in rows
    }
    dropped = sorted(code for code, stats in by_code.items() if stats is None)
    if dropped:
        log.info("areas not covered (fewer than %d sales, none sized or none in %d): %s",
                 MIN_COVERED_SALES, window.to_year, ", ".join(dropped))
    for code, stats in by_code.items():
        if stats is not None and fallbacks_of(stats.area):
            log.info("area %s served without: %s", code, ", ".join(fallbacks_of(stats.area)))
    return {code: by_code[code] for code in wanted if by_code.get(code) is not None}


def borough_rent_from_row(row: dict[str, Any]) -> BoroughRent:
    """Map a ``LONDON_RENTS_SQL`` row onto its `BoroughRent`."""
    return BoroughRent(
        month=row["month"],
        rent=int(row["rent_all"]),
        by_bedrooms={
            bedrooms: int(row[column])
            for bedrooms, column in BEDROOM_RENT_COLUMNS.items()
            if row[column] is not None
        },
    )


async def fetch_london_rents(session: AsyncSession) -> dict[str, BoroughRent]:
    """
    Return each borough's latest rents from ``production.london_rents``, keyed by borough name.

    Empty when the table does not exist yet (before migration 0026), so rents
    and yields are served as unknown rather than failing the page.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(queries.LONDON_RENTS_SQL)).mappings().all()
    except ProgrammingError:
        log.warning("production.london_rents is missing (migration 0026): rents and yields served as unknown")
        return {}
    return {row["area_name"]: borough_rent_from_row(dict(row)) for row in rows}


async def fetch_mortgage_rates(session: AsyncSession) -> MortgageRates | None:
    """Return the latest month's rates from ``production.mortgage_rates``, or None when it is empty."""
    row = (await session.execute(queries.MORTGAGE_RATES_SQL)).mappings().first()
    if row is None:
        return None

    def pct(value: Any) -> float | None:
        return None if value is None else float(value)

    return MortgageRates(
        rate_date=row["rate_date"],
        ltv60=pct(row["rate_2y_60ltv"]),
        ltv75=pct(row["rate_2y_75ltv"]),
        ltv85=pct(row["rate_2y_85ltv"]),
        ltv90=pct(row["rate_2y_90ltv"]),
        ltv95=pct(row["rate_2y_95ltv"]),
    )


def adjusted_price(
    price: float,
    sold_area: float,
    wanted_area: float,
    sold_on: date,
    valuation_date: date | None,
    property_type: PropertyType,
    index=None,
    district: str | None = None,
) -> int:
    """
    Return what a nearby sale implies for a home of `wanted_area` on `valuation_date`.

    Two adjustments, both capped: the price is scaled to the requested floor
    area by its price per m2 (`MAX_SIZE_SCALE`), and moved along the price
    index for `district` and its type from the day it sold to the day being valued
    (`price_index.PriceIndex.factor`). This is the figure the estimate is built
    from, and the one shown beside each sale, so the list and the headline
    agree.
    """
    scale = 1.0
    if sold_area and wanted_area:
        scale = min(MAX_SIZE_SCALE, max(1.0 / MAX_SIZE_SCALE, wanted_area / sold_area))
    time_factor = 1.0
    if index is not None and valuation_date is not None:
        time_factor = index.factor(sold_on, valuation_date, property_type, district)
    return int(round(price * scale * time_factor))


def comparables_from_rows(
    rows: list[dict[str, Any]],
    district: str,
    origin: tuple[float, float],
    limit: int,
    floor_area_sqm: float | None = None,
    valuation_date: date | None = None,
    index=None,
) -> list[Comparable]:
    """
    Pick nearby sales from candidate rows, one per street.

    Parameters
    ----------
    rows : list of dict
        ``COMPARABLES_SQL`` rows in their SQL order (same type first, then
        closest size, most recent), each with ``same_type``.
    district : str
        Area code to label each sale with.
    origin : tuple of float
        Point distances are measured from.
    limit : int
        Maximum number of sales to return.
    floor_area_sqm, valuation_date, index : optional
        What each sale is adjusted to, and the index it is moved along; without
        them ``adjusted_price`` is the price paid.

    Returns
    -------
    list of Comparable
        Same-type sales with a usable, not yet seen street name, up to
        `limit`. Broader-type sales (``match_type="broader"``) are added only
        while fewer than `MIN_SAME_TYPE_COMPARABLES` same-type ones were
        found, so the list may be shorter than `limit`, or empty.
    """
    def pick(candidates, match_type, cap, streets, picked):
        for row in candidates:
            if len(picked) >= cap:
                return
            street = street_name(row["poststreet"])
            if not street or street.lower() in streets:
                continue
            streets.add(street.lower())
            distance = haversine_km(origin[0], origin[1], row["latitude"], row["longitude"])
            area = float(row["total_floor_area"])
            picked.append(
                Comparable(
                    id=str(row["ppd_id"]),
                    street=street,
                    district=district,
                    property_type=row["type_key"],
                    floor_area_sqm=int(round(area)),
                    price=int(row["price"]),
                    sold_on=row["dateoftransfer"],
                    distance_km=round(distance, 1),
                    match_type=match_type,
                    adjusted_price=adjusted_price(
                        float(row["price"]),
                        area,
                        float(floor_area_sqm) if floor_area_sqm else area,
                        row["dateoftransfer"],
                        valuation_date,
                        row["type_key"],
                        index,
                        district,
                    ),
                )
            )

    picked: list[Comparable] = []
    streets: set[str] = set()
    pick([r for r in rows if r["same_type"]], "same", limit, streets, picked)
    if len(picked) < min(limit, MIN_SAME_TYPE_COMPARABLES):
        pick([r for r in rows if not r["same_type"]], "broader", limit, streets, picked)
    return picked


def _months_before(day: date, months: int) -> date:
    """Return `day` moved back `months` calendar months, clamped to the month's length."""
    index = day.year * 12 + day.month - 1 - months
    year, month = index // 12, index % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)


def comparables_date_from(window: DataWindow) -> date:
    """Return the first sale date a nearby sale may have: `COMPARABLE_RECENCY_MONTHS` back."""
    return max(window_start(window), _months_before(window.latest_sale, COMPARABLE_RECENCY_MONTHS))


async def fetch_comparables(
    session: AsyncSession,
    *,
    district: str,
    members: list[str],
    window: DataWindow,
    property_type: PropertyType,
    floor_area_sqm: float,
    origin: tuple[float, float] | None,
    limit: int = 3,
    valuation_date: date | None = None,
    index=None,
) -> list[Comparable]:
    """
    Return nearby sales from `members`, labelled with the area code `district`.

    Candidates are the requested type or its group, sold in the last
    `COMPARABLE_RECENCY_MONTHS` months within `comparable_radius_km` of
    `origin`; at least `COMPARABLE_CANDIDATES` are fetched before street names
    are cleaned and de-duplicated by `comparables_from_rows`. Without an
    origin nothing can be called nearby, so the list is empty.
    """
    if origin is None:
        return []
    rows = (
        await session.execute(
            queries.COMPARABLES_SQL,
            {
                "members": members,
                "date_from": comparables_date_from(window),
                "min_floor_area": MIN_FLOOR_AREA,
                "max_floor_area": MAX_FLOOR_AREA,
                "property_type": property_type,
                "type_group": type_group(property_type),
                "floor_area_sqm": floor_area_sqm,
                "origin_lat": float(origin[0]),
                "origin_lon": float(origin[1]),
                "max_distance_km": comparable_radius_km(district),
                "limit": max(COMPARABLE_CANDIDATES, limit * 4),
                "min_price": MIN_PLAUSIBLE_PRICE,
                "max_price_per_sqm": MAX_PRICE_PER_SQM,
            },
        )
    ).mappings().all()
    return comparables_from_rows(
        [dict(row) for row in rows],
        district,
        origin,
        limit,
        floor_area_sqm=floor_area_sqm,
        valuation_date=valuation_date,
        index=index,
    )


@dataclass(frozen=True)
class EvidenceCounts:
    """
    The sales an estimate's evidence scope holds.

    Attributes
    ----------
    sales_count : int
        Every sale in the scope over the window.
    similar_count : int
        Same-type sales within `SIMILAR_FLOOR_AREA_SHARE` of the floor area.
    """

    sales_count: int
    similar_count: int


async def fetch_evidence_counts(
    session: AsyncSession,
    *,
    members: list[str],
    window: DataWindow,
    property_type: PropertyType,
    floor_area_sqm: float,
) -> EvidenceCounts:
    """Count the scope's sales and its same-type sales within ``SIMILAR_FLOOR_AREA_SHARE`` of the floor area."""
    row = (
        await session.execute(
            queries.EVIDENCE_COUNTS_SQL,
            {
                "members": members,
                "date_from": window_start(window),
                "property_type": property_type,
                "floor_area_low": floor_area_sqm * (1 - SIMILAR_FLOOR_AREA_SHARE),
                "floor_area_high": floor_area_sqm * (1 + SIMILAR_FLOOR_AREA_SHARE),
            },
        )
    ).mappings().one()
    return EvidenceCounts(
        sales_count=int(row["sales_count"]), similar_count=int(row["similar_count"])
    )


async def fetch_postcode_centroid(
    session: AsyncSession, postcode: str
) -> tuple[float, float] | None:
    """Return the ``(latitude, longitude)`` centre of a postcode, or None if not held."""
    row = (
        await session.execute(queries.POSTCODE_CENTROID_SQL, {"postcode": postcode})
    ).mappings().one()
    if row["latitude"] is None or row["longitude"] is None:
        return None
    return float(row["latitude"]), float(row["longitude"])


async def fetch_outward_centroid(
    session: AsyncSession, outward: str
) -> tuple[float, float] | None:
    """Return the median position of an outward code's sales, or None if it has none."""
    row = (
        await session.execute(queries.OUTWARD_CENTROID_SQL, {"outward": outward})
    ).mappings().one()
    if row["latitude"] is None or row["longitude"] is None:
        return None
    return float(row["latitude"]), float(row["longitude"])
