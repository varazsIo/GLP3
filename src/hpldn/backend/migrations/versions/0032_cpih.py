"""
Load ``production.cpih``: UK consumer price inflation, month by month.

The price card's detailed analysis states past prices and rents in the pounds
of the day the home is priced for. It does that with ONS's CPIH (consumer
prices including owner occupiers' housing costs), the UK's headline inflation
measure: the 12-month rate for all items from January 1989 to August 2026
(series L55O, downloaded 2 October 2026), from
``$HPLDN_DATA_DIR/migration_inputs/ons_cpih_annual_rate.csv``.

Each row holds the month's published rate and an index chained from the
rates (2015 = 100; see `hpldn.backend.migrations.cpih`), so how much prices
rose between two months is the ratio of their ``price_index``. The table is
small and drop-and-rebuild; a later download is loaded by a revision that
reloads the refreshed fixture.

Revision ID: 0032
Revises: 0031
"""

from datetime import date

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.migrations.cpih import SOURCE, build_index, read_rates
from hpldn.backend.scripts.migration_helpers import get_migration_engine, get_migration_logger

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

FIRST_MONTH = date(1989, 1, 1)
LATEST_MONTH = date(2026, 8, 1)
# January 1989 to August 2026.
EXPECTED_MONTHS = 452

CREATE_SQL = text(
    """
    DROP TABLE IF EXISTS production.cpih;
    CREATE TABLE production.cpih (
        month        date          PRIMARY KEY CHECK (extract(day FROM month) = 1),
        annual_rate  numeric(4,1)  NOT NULL,
        price_index  numeric(10,4) NOT NULL CHECK (price_index > 0),
        source       text          NOT NULL
    );
    COMMENT ON TABLE production.cpih IS
        'ONS CPIH 12-month rate, all items, with an index chained from it (2015 = 100) (migration 0032)';
    COMMENT ON COLUMN production.cpih.annual_rate IS
        'Percentage rise in CPIH over the 12 months to this month, as ONS publishes it (L55O)';
    COMMENT ON COLUMN production.cpih.price_index IS
        'Index chained from annual_rate, 2015 averaging 100; inflation between two months is the ratio';
    """
)

INSERT_SQL = text(
    """
    INSERT INTO production.cpih (month, annual_rate, price_index, source)
    VALUES (:month, :annual_rate, :price_index, :source)
    """
)

SHAPE_SQL = text(
    """
    SELECT count(*) AS months, min(month) AS first_month, max(month) AS latest_month,
           avg(price_index) FILTER (WHERE extract(year FROM month) = 2015) AS base_mean
      FROM production.cpih
    """
)


def rows() -> list[dict]:
    """Return the fixture's months with their rate and chained index."""
    rates = read_rates()
    index = build_index(rates)
    return [
        {"month": month, "annual_rate": rate, "price_index": round(index[month], 4), "source": SOURCE}
        for month, rate in rates.items()
    ]


def upgrade() -> None:
    """Create and load ``production.cpih``, then check it covers every month."""
    engine = get_migration_engine()
    log = get_migration_logger()
    with engine.begin() as connection:
        connection.execute(CREATE_SQL)
        connection.execute(INSERT_SQL, rows())
        shape = connection.execute(SHAPE_SQL).mappings().one()
    if shape["months"] != EXPECTED_MONTHS:
        raise AssertionError(f"0032: production.cpih holds {shape['months']} month(s), expected {EXPECTED_MONTHS}")
    if (shape["first_month"], shape["latest_month"]) != (FIRST_MONTH, LATEST_MONTH):
        raise AssertionError(
            f"0032: production.cpih runs {shape['first_month']} to {shape['latest_month']}, "
            f"expected {FIRST_MONTH} to {LATEST_MONTH}"
        )
    if abs(float(shape["base_mean"]) - 100) > 0.01:
        raise AssertionError(f"0032: production.cpih 2015 averages {shape['base_mean']}, expected 100")
    log.info("0032: production.cpih loaded, %d month(s) to %s", shape["months"], shape["latest_month"])


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
