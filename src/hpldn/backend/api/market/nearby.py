"""
What homes near a specific home recently sold for.

``POST /nearby`` resolves the home the way ``POST /price`` does (postcode,
then house number or name, then street or a blank street, then the unit
when one is given), takes its
coordinates, and lists the nearest sales in its postcode district in the 12
months to the latest sale held. Each sale carries its own rows of
``production.comps_features`` and ``production.neighbourhood_features``, read
as stored: nothing here is modelled or estimated.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.market.districts import normalise_postcode, property_type_key
from hpldn.backend.api.market.pricing import _normal
from hpldn.backend.api.schemas import AddressOption, HomeRequest, NearbySale, NearbySales
from hpldn.backend.migrations.address_parse_sql import unit_key

# How many sales the card lists, and how far back they reach.
NEARBY_LIMIT = 5
WINDOW_MONTHS = 12

_CANDIDATES_SQL = text(
    """
    SELECT a.id, a.postnumber, a.poststreet, a.unit, ac.latitude, ac.longitude
    FROM production.address_table a
    LEFT JOIN production.address_coordinates ac ON ac.address_id = a.id
    WHERE a.postcode = :postcode
    """
)

_WINDOW_SQL = text(
    f"""
    SELECT (max(dateoftransfer) - interval '{WINDOW_MONTHS} months')::date AS window_from,
           max(dateoftransfer) AS window_to
    FROM production.london_houses
    """
)

# `postcode_district` is indexed on the view; the distance is the haversine in metres.
_NEARBY_SQL = text(
    """
    SELECT a.postnumber, a.poststreet, a.posttown, a.postcode, a.unit,
           lh.propertytype, lh.is_maisonette, lh.price, lh.dateoftransfer,
           6371000 * 2 * asin(sqrt(
               power(sin(radians(lh.latitude - :lat) / 2), 2)
               + cos(radians(:lat)) * cos(radians(lh.latitude))
                 * power(sin(radians(lh.longitude - :lon) / 2), 2)
           )) AS distance_m,
           cf.comp_median_price, cf.comp_median_price_per_sqm, cf.comp_txn_count,
           nf.proptype_local_median_800m_6m
    FROM production.london_houses lh
    JOIN production.ppd_table p ON p.id = lh.ppd_id
    JOIN production.address_table a ON a.id = p.address_id
    LEFT JOIN production.comps_features cf ON cf.ppd_id = lh.ppd_id
    LEFT JOIN production.neighbourhood_features nf ON nf.ppd_id = lh.ppd_id
    WHERE lh.postcode_district = :district
      AND lh.dateoftransfer > :window_from
      AND lh.latitude IS NOT NULL AND lh.longitude IS NOT NULL
      AND NOT (p.address_id = ANY(:home_ids))
    ORDER BY (lh.latitude - :lat) ^ 2 + ((lh.longitude - :lon) * cos(radians(:lat))) ^ 2,
             lh.dateoftransfer DESC, lh.ppd_id DESC
    LIMIT :limit
    """
)


class HomeNotFound(Exception):
    """The address names no home in the register, with a sentence safe to show."""


def match_rows(rows: list[dict], number: str, street: str | None, unit: str | None = None) -> list[dict]:
    """
    Return the register rows that are the home, as `hpp_inference.match_address` matches them.

    The house number or name must match, and the unit's key when a unit is
    given; rows on the street given or with a blank street are the same
    home, or only blank-street rows when none carries that street.
    """
    number, street, wanted_unit = _normal(number), _normal(street), unit_key(unit)
    same_number = [
        row for row in rows
        if _normal(row["postnumber"]) == number
        and (wanted_unit is None or unit_key(row.get("unit")) == wanted_unit)
    ]
    if not street:
        return same_number
    if any(_normal(row["poststreet"]) == street for row in same_number):
        return [row for row in same_number if _normal(row["poststreet"]) in (street, "")]
    return [row for row in same_number if _normal(row["poststreet"]) == ""]


def _whole(value) -> int | None:
    """Return a stored figure to the nearest whole unit, or None when the table holds none."""
    return None if value is None else int(round(float(value)))


async def locate_home(session: AsyncSession, home: HomeRequest) -> tuple[list[dict], tuple[float, float] | None]:
    """
    Return the register rows that are `home` and where it is.

    The location is the mean of the matched rows' coordinates, or of the
    postcode's when none of them has any; None when the postcode has none.
    ``POST /schools`` places a home the same way.

    Raises
    ------
    HomeNotFound
        When no register row matches the address.
    """
    postcode = normalise_postcode(home.postcode)
    rows = [dict(row) for row in (await session.execute(_CANDIDATES_SQL, {"postcode": postcode})).mappings()]
    matched = match_rows(rows, home.postnumber, home.poststreet, home.unit)
    if not matched:
        raise HomeNotFound(
            "We could not find this home in the property register. "
            "Check the house number or name and the street against the postcode."
        )
    placed = [row for row in matched if row["latitude"] is not None] or [
        row for row in rows if row["latitude"] is not None
    ]
    if not placed:
        return matched, None
    lat = sum(float(row["latitude"]) for row in placed) / len(placed)
    lon = sum(float(row["longitude"]) for row in placed) / len(placed)
    return matched, (lat, lon)


async def fetch_nearby(session: AsyncSession, home: HomeRequest) -> NearbySales:
    """
    Return the sales nearest `home` in the latest `WINDOW_MONTHS`, nearest first.

    The home's own sales are left out. A home without coordinates of its own
    is placed at the middle of its postcode's.

    Raises
    ------
    HomeNotFound
        When no register row matches the address.
    """
    postcode = normalise_postcode(home.postcode)
    matched, location = await locate_home(session, home)
    window = (await session.execute(_WINDOW_SQL)).mappings().one()
    window_from: date = window["window_from"]
    window_to: date = window["window_to"]
    if location is None:
        return NearbySales(sales=[], window_from=window_from, window_to=window_to)
    lat, lon = location

    result = await session.execute(
        _NEARBY_SQL,
        {
            "lat": lat,
            "lon": lon,
            "district": postcode.split(" ")[0],
            "window_from": window_from,
            "home_ids": [int(row["id"]) for row in matched],
            "limit": NEARBY_LIMIT,
        },
    )
    sales = [
        NearbySale(
            address=AddressOption(
                postnumber=str(row["postnumber"] or "").strip(),
                poststreet=(row["poststreet"] or "").strip() or None,
                posttown=str(row["posttown"] or "").strip(),
                postcode=row["postcode"],
                unit=(row["unit"] or "").strip() or None,
            ),
            property_type=property_type_key(str(row["propertytype"]), row["is_maisonette"]),
            price=int(row["price"]),
            sold_on=row["dateoftransfer"],
            distance_m=int(round(float(row["distance_m"]))),
            comp_median_price=_whole(row["comp_median_price"]),
            comp_median_price_per_sqm=_whole(row["comp_median_price_per_sqm"]),
            comp_txn_count=None if row["comp_txn_count"] is None else int(row["comp_txn_count"]),
            local_type_median=_whole(row["proptype_local_median_800m_6m"]),
        )
        for row in result.mappings()
    ]
    return NearbySales(sales=sales, window_from=window_from, window_to=window_to)
