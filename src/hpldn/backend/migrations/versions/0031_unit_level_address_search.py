"""
One suggestion per flat: rebuild ``production.address_search`` on the unit.

``address_search`` folded register rows on (postcode, number, street), so
after 0028 1,610,855 sold homes collapsed into 1,162,940 suggestions and
947,328 held sales (504,100 homes, 99 % flats) had no entry of their own:
"Flat 3, 12 X Road" could not be found, only "12 X Road". With 0030's
``unit`` the fold key gains ``production.hpldn_unit_key(unit)``
(`migrations.address_search`), ``search_text`` leads with the unit ("FLAT 6
17 WOODVILLE ROAD ...") and the relation carries ``unit`` and ``unit_key``,
which ``GET /addresses/search`` returns and ``POST /price`` narrows the home
by. 0030's certified homes join the relation too.

0021-0024 and 0028's checks run as 0029 ran them; this revision adds its
own row floor, the two new columns, and a check that sold flats of one
building are separate suggestions.

Revision ID: 0031
Revises: 0030
"""

from sqlalchemy import text

from hpldn.backend.migrations import link_carry
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

# Floor for the relation's rows: 2,990,785 on hpdbldn (1,422,852 before,
# 1,320,853 of them with a unit now).
ADDRESS_SEARCH_UNIT_ROWS_EXPECTED = 2_800_000
# 0028's floor for street hints.
MIN_STREET_HINTS = 4_000

EXPECTED_TRAILING_COLUMNS = ("street_hint", "unit", "unit_key")

_COLUMNS_SQL = text(
    """
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'production' AND table_name = 'address_search'
    ORDER BY ordinal_position
    """
)

# Sold homes (UPRNs with a sale in the view) and the suggestions that are
# theirs alone: an entry whose fold group holds one UPRN.
_OWN_ENTRY_SQL = text(
    """
    WITH sold AS (
        SELECT DISTINCT a.id, a.postcode,
               production.hpldn_searchnorm(a.postnumber) AS num_norm,
               production.hpldn_searchnorm(a.poststreet) AS street_norm,
               coalesce(production.hpldn_unit_key(a.unit), '') AS unit_key
        FROM production.ppd_table p
        JOIN production.london_houses lh ON lh.ppd_id = p.id
        JOIN production.address_table a ON a.id = p.address_id
    ), groups AS (
        SELECT postcode, num_norm, street_norm, unit_key, count(*) AS homes
        FROM sold
        GROUP BY 1, 2, 3, 4
    )
    SELECT (SELECT count(*) FROM sold) AS sold_homes,
           coalesce(sum(homes) FILTER (WHERE homes = 1), 0) AS own_entry
    FROM groups
    """
)

# Two sold flats of one building with different units: both must be
# suggestions of their own.
_FLAT_PAIR_SQL = text(
    """
    WITH flats AS (
        SELECT a.id, a.postcode, a.postnumber, a.poststreet, production.hpldn_unit_key(a.unit) AS unit_key
        FROM production.address_table a
        WHERE a.unit IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM production.ppd_table p
              JOIN production.london_houses lh ON lh.ppd_id = p.id
              WHERE p.address_id = a.id AND lh.propertytype = 'F'
          )
        ORDER BY a.id
        LIMIT 20000
    ), pairs AS (
        SELECT f1.id AS id1, f2.id AS id2
        FROM flats f1
        JOIN flats f2 ON f2.postcode = f1.postcode AND f2.postnumber = f1.postnumber
                     AND f2.poststreet IS NOT DISTINCT FROM f1.poststreet AND f2.unit_key > f1.unit_key
        LIMIT 200
    )
    SELECT count(*) AS pairs,
           count(*) FILTER (
               WHERE NOT EXISTS (
                   SELECT 1 FROM production.address_search s
                   JOIN production.address_table a ON a.id = pairs.id1
                   WHERE s.postcode = a.postcode AND s.unit_key = production.hpldn_unit_key(a.unit)
                     AND production.hpldn_searchnorm(s.postnumber) = production.hpldn_searchnorm(a.postnumber)
               )
               OR NOT EXISTS (
                   SELECT 1 FROM production.address_search s
                   JOIN production.address_table a ON a.id = pairs.id2
                   WHERE s.postcode = a.postcode AND s.unit_key = production.hpldn_unit_key(a.unit)
                     AND production.hpldn_searchnorm(s.postnumber) = production.hpldn_searchnorm(a.postnumber)
               )
           ) AS missing
    FROM pairs
    """
)


def _check(engine, rows: int, log) -> None:
    """
    Check the unit columns, the row floor and that sold flats are suggestions of their own.

    Raises
    ------
    AssertionError
        On a missing column, fewer rows than `ADDRESS_SEARCH_UNIT_ROWS_EXPECTED`,
        or a sampled pair of flats not each suggested.
    """
    with engine.connect() as connection:
        columns = tuple(row[0] for row in connection.execute(_COLUMNS_SQL))
        with_unit = connection.execute(
            text("SELECT count(*) FROM production.address_search WHERE unit IS NOT NULL")
        ).scalar_one()
        sold, own = connection.execute(_OWN_ENTRY_SQL).one()
        pairs, missing = connection.execute(_FLAT_PAIR_SQL).one()
    log.info(
        "0031: production.address_search holds %d home(s), %d with a unit; %d of %d sold home(s) "
        "have an entry of their own; %d of %d sampled pairs of flats not both suggested",
        rows, with_unit, own, sold, missing, pairs,
    )
    if columns[-len(EXPECTED_TRAILING_COLUMNS):] != EXPECTED_TRAILING_COLUMNS:
        raise AssertionError(f"0031: production.address_search has columns {columns}")
    if rows < ADDRESS_SEARCH_UNIT_ROWS_EXPECTED:
        raise AssertionError(f"0031: {rows:,} suggestion(s), expected at least {ADDRESS_SEARCH_UNIT_ROWS_EXPECTED:,}")
    if pairs == 0 or missing:
        raise AssertionError(f"0031: {missing} of {pairs} sampled pairs of flats are not both suggested")


def upgrade() -> None:
    """Rebuild ``production.address_search`` with units and check it."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    rows = link_carry.rebuild_address_search_checked(engine, cfg, log, "0031", MIN_STREET_HINTS)
    _check(engine, rows, log)
    log.info(
        "0031: complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  restart the API (its search cache holds folded suggestions)"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
