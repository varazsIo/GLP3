"""
Load ``production.borough_rents``: the mean monthly private rent per borough.

The area figures' typical rent and gross yield, in a table so the API reads
them like every other number:
the ONS Price Index of Private Rents mean monthly rent for July 2026 (release
of 19 August 2026, Table 1, rounded to £25), one row per London borough, from
``$HPLDN_DATA_DIR/migration_inputs/ons_pipr_borough_rents.csv``. ONS publishes no City of
London figure, so the City has no row and its areas are served without a
rent or yield rather than borrowing Westminster's.

A later release is loaded by adding its rows (a new ``month``); the API reads
each borough's latest month. The table is small and drop-and-rebuild.

Revision ID: 0023
Revises: 0022
"""

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import text

from hpldn.backend.migrations import inputs
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts.migration_helpers import get_migration_engine, get_migration_logger

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

FIXTURE = inputs.resolve("ons_pipr_borough_rents_csv_fp")

# The 32 London boroughs ONS publishes a rent for (not the City of London).
EXPECTED_BOROUGHS = 32

CREATE_SQL = text(
    """
    DROP TABLE IF EXISTS production.borough_rents;
    CREATE TABLE production.borough_rents (
        local_authority   text    NOT NULL,
        month             date    NOT NULL,
        mean_monthly_rent integer NOT NULL CHECK (mean_monthly_rent > 0),
        source            text    NOT NULL,
        PRIMARY KEY (local_authority, month)
    );
    """
)

INSERT_SQL = text(
    """
    INSERT INTO production.borough_rents (local_authority, month, mean_monthly_rent, source)
    VALUES (:local_authority, :month, :mean_monthly_rent, :source)
    """
)


def read_fixture(path: Path = FIXTURE) -> list[dict]:
    """Return the fixture's rows with typed values."""
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {
                "local_authority": row["local_authority"],
                "month": date.fromisoformat(row["month"]),
                "mean_monthly_rent": int(row["mean_monthly_rent"]),
                "source": row["source"],
            }
            for row in csv.DictReader(handle)
        ]


def upgrade() -> None:
    """Create and load ``production.borough_rents``, then check it holds every borough."""
    engine = get_migration_engine()
    log = get_migration_logger()
    rows = read_fixture()
    with engine.begin() as connection:
        connection.execute(CREATE_SQL)
        connection.execute(INSERT_SQL, rows)
        boroughs = connection.execute(
            text("SELECT count(DISTINCT local_authority) FROM production.borough_rents")
        ).scalar_one()
    if boroughs != EXPECTED_BOROUGHS:
        raise AssertionError(
            f"0023: production.borough_rents holds {boroughs} borough(s), expected {EXPECTED_BOROUGHS}"
        )
    log.info("0023: production.borough_rents loaded, %d borough(s)", boroughs)


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
