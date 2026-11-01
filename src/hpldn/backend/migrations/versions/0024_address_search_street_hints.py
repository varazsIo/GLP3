"""
Search homes the register holds without a street under their postcode's street.

Measured on hpdbldn at 0023, 140,502 of 1,301,395 suggestible homes (10.8 %)
have no ``poststreet``: 232A in SW11 3AA is on Battersea Bridge Road, but the
register only says "232A". Such a home matched a postcode or a house number,
never a street, so "232a battersea bridge" listed 230, 234 and 236 but not 232A.

`migrations.address_search.CREATE_TABLE_SQL` now gives a street-less home its
postcode's street in ``search_text`` and ``name_text`` when every named home in
that postcode is on one street (100,016 homes at 0023), and keeps it in a new
``street_hint`` column the suggestions show. A postcode spanning several
streets gives no hint, as the home could be on any of them. The returned
``poststreet`` is untouched, because ``POST /price`` resolves a home by the
register's own fields.

The relation is rebuilt with `migrations.address_search.rebuild`, 0021's and
0022's checks run again, and the hinted homes are counted and sampled.

Restart the API afterwards: ``SearchCache`` keeps suggestions for ten minutes.

Revision ID: 0024
Revises: 0023
"""

from sqlalchemy import text

from hpldn.backend.migrations.address_search import rebuild
from hpldn.backend.migrations.chain import load_revision, no_downgrade
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# Floor, not an exact count: 100,016 street-less homes took a hint on hpdbldn.
STREET_HINTS_EXPECTED = 95_000

EXPECTED_COLUMNS = (
    "address_id",
    "postnumber",
    "poststreet",
    "posttown",
    "postcode",
    "search_text",
    "name_text",
    "street_hint",
)

# Street-less homes whose name_text now carries a street (it was the number).
_HINTED_SQL = text(
    """
    SELECT count(*)
    FROM production.address_search
    WHERE street_hint IS NOT NULL
      AND poststreet IS NULL
      AND name_text = production.hpldn_searchnorm(street_hint)
    """
)

# Street-less homes that are the only home of their number in a single-street
# postcode, sampled: each must now be found by its number and the street.
_SAMPLE_SQL = text(
    """
    SELECT count(*) AS sampled,
           count(*) FILTER (
               WHERE NOT EXISTS (
                   SELECT 1 FROM production.address_search s
                   WHERE s.address_id = sample.address_id
                     AND s.search_text ~ ('(^| )' || production.hpldn_searchnorm(sample.postnumber) || ' ')
                     AND s.name_text LIKE sample.street_norm || '%'
               )
           ) AS missing
    FROM (
        SELECT b.address_id, b.postnumber,
               min(production.hpldn_searchnorm(production.hpldn_street_key(n.poststreet))) AS street_norm
        FROM production.address_search b
        JOIN production.address_search n
          ON n.postcode = b.postcode AND n.poststreet IS NOT NULL
        WHERE b.poststreet IS NULL
        GROUP BY b.address_id, b.postnumber
        -- Streets compared by their key, as address_search's hint does since 0025.
        HAVING count(DISTINCT production.hpldn_street_key(n.poststreet)) = 1
        ORDER BY b.address_id
        LIMIT 500
    ) sample
    """
)


def _check_columns(engine, log, first) -> None:
    """Check the relation's columns lead with these, ``street_hint`` eighth (0031 appends the unit)."""
    with engine.connect() as connection:
        columns = tuple(row[0] for row in connection.execute(first._COLUMNS_SQL))
    if columns[: len(EXPECTED_COLUMNS)] != EXPECTED_COLUMNS:
        raise AssertionError(f"0024: production.address_search has columns {columns}, expected {EXPECTED_COLUMNS}")
    log.info("0024: columns checked -- street_hint present")


def _check_hints(engine, log) -> None:
    """Check enough street-less homes took a hint, and a sample is found by its street."""
    with engine.connect() as connection:
        hinted = connection.execute(_HINTED_SQL).scalar_one()
        sampled, missing = connection.execute(_SAMPLE_SQL).one()
    if hinted < STREET_HINTS_EXPECTED:
        raise AssertionError(
            f"0024: only {hinted:,} street-less homes carry a street hint, expected at "
            f"least {STREET_HINTS_EXPECTED:,}"
        )
    if sampled == 0 or missing:
        raise AssertionError(f"0024: {missing} of {sampled} sampled street-less homes lack their street")
    log.info("0024: %s street-less home(s) searchable by street; %d sampled", f"{hinted:,}", sampled)


def upgrade() -> None:
    """Rebuild ``production.address_search`` with street hints, then check it."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    first = load_revision("0021")
    widened = load_revision("0022")

    log.info("0024: rebuilding production.address_search with street hints")
    rows = rebuild(engine, cfg, log)

    first._check_shape(engine, log)
    _check_columns(engine, log, first)
    widened._check_rows(rows, log)
    first._check_covers_addresses_route(engine, log)
    widened._check_sold_homes_without_certificate(engine, log)
    _check_hints(engine, log)
    first._check_plan(engine, log)

    log.info(
        "0024: street hints in. Restart the API (or wait SearchCache's ten-minute "
        "TTL) so no worker serves suggestions from before the rebuild."
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
