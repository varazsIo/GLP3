"""
Build ``production.address_search``, the relation ``GET /addresses/search`` reads.

The address field on the home page had no server endpoint to search: it fell
back to a 4.9 MB static index in the browser that fanned one search out into up
to 24 ``GET /addresses`` calls. The endpoint that replaces it needs a relation it
can match a partial address against in single-digit milliseconds.

``production.address_table`` is not that relation: 11.7M rows / 2.4 GB, with two
``en_US.UTF-8``-collated btrees that can serve neither a prefix nor a substring
``LIKE``. Measured on hpdbldn at 0020, the search anyone would write against it
(``postcode ILIKE '%…%' OR poststreet ILIKE '%…%'``) is a 1,922 ms sequential
scan reading 123,087 buffers.

This revision installs ``pg_trgm`` (available but not installed), creates
``production.hpldn_searchnorm`` beside the ``staging.hpldn_*`` family, and builds
``production.address_search``: one row per priceable home, folded exactly as
``GET /addresses`` folds the register's duplicate rows, with a trigram GIN index
for infix matching and ``text_pattern_ops`` btrees for the anchored prefixes.

``production.address_search`` derives from the same tables as
``production.london_houses``, so it must be rebuilt whenever that view is
refreshed -- `migrations.address_search.rebuild` is the one entry point.

Revision ID: 0021
Revises: 0020
"""

from sqlalchemy import text

from hpldn.backend.migrations.address_search import (
    ADDRESS_SEARCH_ROWS_EXPECTED,
    rebuild,
)
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

EXPECTED_COLUMNS = (
    "address_id",
    "postnumber",
    "poststreet",
    "posttown",
    "postcode",
    "search_text",
    "name_text",
)

EXPECTED_INDEXES = {
    "idx_prod_address_search_address_id",
    "idx_prod_address_search_text_trgm",
    "idx_prod_address_search_name_prefix",
    "idx_prod_address_search_postcode_prefix",
}

_COLUMNS_SQL = text(
    """
    SELECT a.attname
    FROM pg_attribute a
    WHERE a.attrelid = 'production.address_search'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
    """
)

_INDEXES_SQL = text(
    "SELECT indexname FROM pg_indexes "
    "WHERE schemaname = 'production' AND tablename = 'address_search'"
)

# Every home /addresses can list must be suggestible, or the search bar would
# hide homes the postcode route offers. Sampled rather than counted in full: the
# equality join over 11.7M register rows costs minutes, this costs milliseconds.
_PARITY_SQL = text(
    """
    SELECT count(*)
    FROM (
        SELECT a.postcode
        FROM production.address_table a
        WHERE EXISTS (
            SELECT 1 FROM production.epc_table ec
            WHERE ec.address_id = a.id
              AND ec.total_floor_area IS NOT NULL
              AND ec.current_energy_rating IS NOT NULL
        )
        LIMIT 2000
    ) sample
    WHERE NOT EXISTS (
        SELECT 1 FROM production.address_search s WHERE s.postcode = sample.postcode
    )
    """
)

# The two statements the route runs, which must be answered from their indexes,
# not a sequential scan. The ORDER BY matters: without one a LIMIT 8 is cheapest
# as a sequential scan that stops at the eighth row.
_PLAN_SQL = {
    "idx_prod_address_search_text_trgm": text(
        """
        EXPLAIN SELECT address_id FROM production.address_search
        WHERE search_text %> 'BARCLAY ROAD'
        ORDER BY word_similarity('BARCLAY ROAD', search_text) DESC LIMIT 8
        """
    ),
    "idx_prod_address_search_name_prefix": text(
        """
        EXPLAIN SELECT address_id FROM production.address_search
        WHERE name_text LIKE 'BARCLAY R%' ORDER BY name_text, postcode LIMIT 8
        """
    ),
    "idx_prod_address_search_postcode_prefix": text(
        """
        EXPLAIN SELECT address_id FROM production.address_search
        WHERE postcode LIKE 'SW11 1%' ORDER BY postcode, name_text LIMIT 8
        """
    ),
}


def _check_shape(engine, log) -> None:
    """Check the relation's columns and its four indexes."""
    with engine.connect() as connection:
        columns = tuple(row[0] for row in connection.execute(_COLUMNS_SQL))
        indexes = {row[0] for row in connection.execute(_INDEXES_SQL)}
    # Later revisions append columns (0024: street_hint); these lead, in order.
    if columns[: len(EXPECTED_COLUMNS)] != EXPECTED_COLUMNS:
        raise AssertionError(
            f"0021: production.address_search has columns {columns}, "
            f"expected {EXPECTED_COLUMNS}"
        )
    missing = EXPECTED_INDEXES - indexes
    if missing:
        raise AssertionError(f"0021: production.address_search is missing {sorted(missing)}")
    log.info("0021: shape checked -- %d columns, %d indexes", len(columns), len(indexes))


def _check_rows(rows: int, log) -> None:
    """Check the relation holds at least the measured number of homes."""
    if rows < ADDRESS_SEARCH_ROWS_EXPECTED:
        raise AssertionError(
            f"0021: production.address_search holds {rows:,} rows, expected at "
            f"least {ADDRESS_SEARCH_ROWS_EXPECTED:,}. The priceable set has "
            "shrunk, or production.epc_table has not been rebuilt."
        )
    log.info("0021: %s home(s) suggestible", f"{rows:,}")


def _check_covers_addresses_route(engine, log) -> None:
    """Check every sampled postcode ``GET /addresses`` can serve is searchable."""
    with engine.connect() as connection:
        missing = connection.execute(_PARITY_SQL).scalar_one()
    if missing:
        raise AssertionError(
            f"0021: {missing} sampled priceable postcode(s) are absent from "
            "production.address_search; GET /addresses would list homes the "
            "search bar cannot suggest"
        )
    log.info("0021: sampled priceable postcodes are all searchable")


def _check_plan(engine, log) -> None:
    """Check the prefix and word-similarity searches plan on their indexes."""
    with engine.connect() as connection:
        for index, statement in _PLAN_SQL.items():
            plan = "\n".join(row[0] for row in connection.execute(statement))
            if "Seq Scan" in plan or index not in plan:
                raise AssertionError(f"0021: the address search does not use {index}. Plan:\n{plan}")
    log.info("0021: searches plan on the trigram and prefix indexes")


def upgrade() -> None:
    """Install ``pg_trgm``, build ``production.address_search``, then check it."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0021: building production.address_search")
    rows = rebuild(engine, cfg, log)

    _check_shape(engine, log)
    _check_rows(rows, log)
    _check_covers_addresses_route(engine, log)
    _check_plan(engine, log)

    log.info(
        "0021: address search ready. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "    (hpldn_api reads production.address_search through the schema's\n"
        "     ALTER DEFAULT PRIVILEGES rule; run the grants to be sure)\n"
        "  rebuild it with hpldn.backend.migrations.address_search.rebuild\n"
        "    whenever production.london_houses is refreshed"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
