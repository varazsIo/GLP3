"""
The state-funded schools nearest a specific home, with their Ofsted ratings.

``POST /schools`` places the home as ``POST /nearby`` does (`nearby.locate_home`)
and lists the `SCHOOLS_PER_PHASE` nearest primary and secondary schools within
`ofsted_features.SEARCH_RADIUS_M`, with the rating in force today from
the ``ofsted_rating_spells`` table (migration 0033): the same spells and
radius the model's ``ofsted_*`` features read. An all-through school counts as
both. Read as stored; nothing is modelled. Ofsted data is Crown copyright,
under the Open Government Licence v3.0.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.market.nearby import locate_home
from hpldn.backend.api.schemas import HomeRequest, NearbySchool, NearbySchools
from hpldn.backend.migrations.ofsted import ATTRIBUTION
from hpldn.backend.scripts.ofsted_features import PRIMARY_PHASES, SEARCH_RADIUS_M, SECONDARY_PHASES

SCHOOLS_PER_PHASE = 3

# Ofsted's report page for a school, whichever its phase.
REPORT_URL = "https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/{urn}"

# Haversine in metres, as `nearby` measures; no PostGIS on the serving host.
# The latitude/longitude window is a cheap pre-filter wider than the radius.
_SCHOOLS_SQL = text(
    """
    WITH in_force AS (
        SELECT s.urn, s.school_name, s.phase, s.rating_label, s.rating_score, s.grade_basis,
               s.inspection_date, s.publication_date, s.inherited_from_urn,
               6371000 * 2 * asin(sqrt(
                   power(sin(radians(s.lat - :lat) / 2), 2)
                   + cos(radians(:lat)) * cos(radians(s.lat)) * power(sin(radians(s.lon - :lon) / 2), 2)
               )) AS distance_m
        FROM production.ofsted_rating_spells s
        WHERE s.valid_from <= :today AND (s.valid_to IS NULL OR s.valid_to > :today)
          AND s.lat BETWEEN :lat - 0.03 AND :lat + 0.03
          AND s.lon BETWEEN :lon - 0.05 AND :lon + 0.05
    ),
    listed AS (
        SELECT 'primary' AS listed_as, i.* FROM in_force i
         WHERE i.phase = ANY(:primary_phases) AND i.distance_m <= :radius_m
        UNION ALL
        SELECT 'secondary', i.* FROM in_force i
         WHERE i.phase = ANY(:secondary_phases) AND i.distance_m <= :radius_m
    )
    SELECT * FROM (
        SELECT l.*, row_number() OVER (PARTITION BY l.listed_as ORDER BY l.distance_m, l.urn) AS rank
        FROM listed l
    ) ranked
    WHERE rank <= :per_phase
    ORDER BY listed_as, rank
    """
)


def _school(row) -> NearbySchool:
    return NearbySchool(
        urn=int(row["urn"]),
        name=str(row["school_name"] or "").strip() or f"School {row['urn']}",
        phase=row["phase"],
        distance_m=int(round(float(row["distance_m"]))),
        rating_label=row["rating_label"],
        rating_score=round(float(row["rating_score"]), 2),
        grade_basis=row["grade_basis"],
        inspected_on=row["inspection_date"],
        published_on=row["publication_date"],
        inherited_from_urn=None if row["inherited_from_urn"] is None else int(row["inherited_from_urn"]),
        report_url=REPORT_URL.format(urn=int(row["urn"])),
    )


async def fetch_schools(session: AsyncSession, home: HomeRequest, today: date) -> NearbySchools:
    """
    Return the schools nearest `home` with the ratings in force on `today`, nearest first.

    Raises
    ------
    nearby.HomeNotFound
        When no register row matches the address.
    """
    _, location = await locate_home(session, home)
    primary: list[NearbySchool] = []
    secondary: list[NearbySchool] = []
    if location is not None:
        lat, lon = location
        result = await session.execute(
            _SCHOOLS_SQL,
            {
                "lat": lat,
                "lon": lon,
                "today": today,
                "radius_m": SEARCH_RADIUS_M,
                "primary_phases": list(PRIMARY_PHASES),
                "secondary_phases": list(SECONDARY_PHASES),
                "per_phase": SCHOOLS_PER_PHASE,
            },
        )
        for row in result.mappings():
            (primary if row["listed_as"] == "primary" else secondary).append(_school(row))
    return NearbySchools(
        primary=primary, secondary=secondary, as_of=today, radius_m=int(SEARCH_RADIUS_M), source=ATTRIBUTION
    )
