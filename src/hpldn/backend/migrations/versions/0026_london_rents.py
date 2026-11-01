"""
Load ``production.london_rents`` and drop ``production.borough_rents``.

0023's ``borough_rents`` held one figure per borough: the overall mean rent for
July 2026, rounded to £25. This revision replaces it with London's slice of the
ONS Price Index of Private Rents monthly price statistics (release of
16 September 2026), January 2015 to August 2026: for London (``E12000007``) and
each of its 32 boroughs, every month's index, monthly and annual change and
mean rental price for all homes, one to four-or-more bedrooms, and four
property types. The yield calculator prices a home at its borough's rent for
its number of bedrooms, and an area at the borough's overall mean.

The rows come from ``$HPLDN_DATA_DIR/migration_inputs/ons_pipr_london_rents.csv``, written by
`hpldn.backend.migrations.london_rents.extract` from the raw ONS CSV (which
lives outside git). ONS publishes no City of London figure, so the City has no
row and its areas are served without a rent or yield. ``area_name`` is ONS's
borough name, which equals ``london_houses.local_authority``.

0023 stays in the chain unchanged: a replay from empty passes through it and
this revision then drops its table. The table is small and drop-and-rebuild; a
new ONS release is loaded by a later revision reloading the refreshed fixture.

Revision ID: 0026
Revises: 0025
"""

from datetime import date

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.migrations.london_rents import BEDROOM_MEASURES, MEASURES, read_fixture
from hpldn.backend.scripts.migration_helpers import get_migration_engine, get_migration_logger

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# London (E12000007) and its 32 boroughs; ONS publishes nothing for the City.
EXPECTED_AREA_CODES = 33
EXPECTED_BOROUGHS = 32
LATEST_MONTH = date(2026, 8, 1)
CITY_OF_LONDON = "E09000001"

# The measures every borough must price in the latest month: the overall mean
# (area figures) and the four bedroom buckets (a specific home).
REQUIRED_MEASURES = ("all", *BEDROOM_MEASURES.values())

_MEASURE_LIST = ", ".join(f"'{measure}'" for measure in MEASURES)

CREATE_SQL = text(
    f"""
    DROP TABLE IF EXISTS production.london_rents;
    CREATE TABLE production.london_rents (
        area_code      text    NOT NULL,
        area_name      text    NOT NULL,
        month          date    NOT NULL,
        measure        text    NOT NULL CHECK (measure IN ({_MEASURE_LIST})),
        price_index    numeric(6,1),
        monthly_change numeric(5,1),
        annual_change  numeric(5,1),
        rental_price   integer CHECK (rental_price > 0),
        source         text    NOT NULL,
        PRIMARY KEY (area_code, month, measure)
    );
    CREATE INDEX london_rents_area_measure_month_idx
        ON production.london_rents (area_name, measure, month DESC);
    COMMENT ON TABLE production.london_rents IS
        'ONS Price Index of Private Rents monthly price statistics for London and its boroughs (migration 0026)';
    DROP TABLE IF EXISTS production.borough_rents;
    """
)

INSERT_SQL = text(
    """
    INSERT INTO production.london_rents (
        area_code, area_name, month, measure,
        price_index, monthly_change, annual_change, rental_price, source
    )
    VALUES (
        :area_code, :area_name, :month, :measure,
        :price_index, :monthly_change, :annual_change, :rental_price, :source
    )
    """
)

SHAPE_SQL = text(
    """
    SELECT count(DISTINCT area_code)                          AS area_codes,
           max(month)                                         AS latest_month,
           count(*) FILTER (WHERE area_code = :city
                               OR area_name = 'City of London') AS city_rows
      FROM production.london_rents
    """
)

# Boroughs missing a required measure's rent in the latest month.
INCOMPLETE_SQL = text(
    """
    SELECT b.area_name, m.measure
      FROM (SELECT DISTINCT area_code, area_name
              FROM production.london_rents
             WHERE area_code LIKE 'E09%') b
     CROSS JOIN unnest(CAST(:measures AS text[])) AS m(measure)
     WHERE NOT EXISTS (
           SELECT 1
             FROM production.london_rents r
            WHERE r.area_code = b.area_code
              AND r.measure = m.measure
              AND r.month = :latest
              AND r.rental_price IS NOT NULL
     )
     ORDER BY 1, 2
    """
)

BOROUGHS_SQL = text(
    "SELECT count(DISTINCT area_code) FROM production.london_rents WHERE area_code LIKE 'E09%'"
)


def upgrade() -> None:
    """Create and load ``production.london_rents``, drop ``borough_rents``, then check the load."""
    engine = get_migration_engine()
    log = get_migration_logger()
    rows = read_fixture()
    with engine.begin() as connection:
        connection.execute(CREATE_SQL)
        connection.execute(INSERT_SQL, rows)
        shape = connection.execute(SHAPE_SQL, {"city": CITY_OF_LONDON}).mappings().one()
        boroughs = connection.execute(BOROUGHS_SQL).scalar_one()
        incomplete = connection.execute(
            INCOMPLETE_SQL, {"measures": list(REQUIRED_MEASURES), "latest": LATEST_MONTH}
        ).all()
        borough_rents = connection.execute(
            text("SELECT to_regclass('production.borough_rents')")
        ).scalar_one()
    if shape["area_codes"] != EXPECTED_AREA_CODES:
        raise AssertionError(
            f"0026: production.london_rents holds {shape['area_codes']} area code(s), "
            f"expected {EXPECTED_AREA_CODES}"
        )
    if boroughs != EXPECTED_BOROUGHS:
        raise AssertionError(
            f"0026: production.london_rents holds {boroughs} borough(s), expected {EXPECTED_BOROUGHS}"
        )
    if shape["latest_month"] != LATEST_MONTH:
        raise AssertionError(
            f"0026: production.london_rents runs to {shape['latest_month']}, expected {LATEST_MONTH}"
        )
    if incomplete:
        missing = ", ".join(f"{name} {measure}" for name, measure in incomplete)
        raise AssertionError(f"0026: no {LATEST_MONTH:%b %Y} rent for {missing}")
    if shape["city_rows"]:
        raise AssertionError(
            f"0026: production.london_rents holds {shape['city_rows']} City of London row(s); "
            "ONS publishes none"
        )
    if borough_rents is not None:
        raise AssertionError("0026: production.borough_rents still exists")
    log.info(
        "0026: production.london_rents loaded, %d row(s), %d area code(s) to %s; borough_rents dropped",
        len(rows), shape["area_codes"], shape["latest_month"],
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
