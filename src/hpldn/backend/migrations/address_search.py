"""
The one definition of ``production.address_search``, the address suggestion index.

``GET /addresses/search`` answers a partial address from this relation. It holds
one row per *priceable* home -- the same universe ``GET /addresses`` lists, so a
suggestion is never a home ``POST /price`` then 404s on -- with a normalised
``search_text`` a ``pg_trgm`` GIN index can match anywhere inside, and a
``name_text`` a ``text_pattern_ops`` btree can match as an anchored prefix.

Built by migration 0021, widened by 0022 to homes without a certificate, given
street hints for street-less homes by 0024, and rebuilt by `rebuild` whenever
``production.london_houses`` is refreshed: both derive from
``production.address_table``, ``production.epc_table`` and
``production.ppd_table``, and a stale search table silently stops suggesting
newly-loaded homes.

Why a table rather than a query over ``production.address_table``: that table is
11.7M rows / 2.4 GB, its two indexes are ``en_US.UTF-8``-collated (so they can
serve neither a prefix nor a substring ``LIKE``), and the search anyone would
write against it plans as a 1.9 s sequential scan. This relation is ~1.2M rows
and ~150 MB with its indexes, and stays in shared_buffers.
"""

from hpldn.backend.common.constants import MIN_EPC_NEIGHBOURS

# The priceability test over a register row ``a``, shared verbatim with
# `api.market.pricing.ADDRESSES_SQL` so the search universe and the postcode
# listing cannot drift. A home is priceable when any of its register rows has
#
#   1. a certificate carrying both a floor area and an energy rating; or
#   2. a residential sale of its own in ``production.london_houses`` (since
#      0022: ``hpp_inference`` estimates the missing certificate from
#      comparable homes nearby); or
#   3. a house number or name, in a postcode `RESIDENTIAL_POSTCODES_CTE` lists
#      (since 0022: never sold, never certified, but among homes that are).
#
# Building-level rows with no number ("CRESSALL HOUSE") stay out: branch 3
# requires a number, and `CREATE_TABLE_SQL` drops blank numbers.
#
# Needs a ``residential_postcodes`` relation in scope (`RESIDENTIAL_POSTCODES_CTE`).
_PRICEABLE_PREDICATE = """
    (
        EXISTS (
            SELECT 1
            FROM production.epc_table ec
            WHERE ec.address_id = a.id
              AND ec.total_floor_area IS NOT NULL
              AND ec.current_energy_rating IS NOT NULL
        )
        OR EXISTS (
            SELECT 1
            FROM production.ppd_table p
            JOIN production.london_houses lh ON lh.ppd_id = p.id
            WHERE p.address_id = a.id
              AND lh.propertytype IN ('D', 'S', 'T', 'F')
        )
        OR (
            a.postcode IN (SELECT postcode FROM residential_postcodes)
            AND a.postnumber ~ '[A-Za-z0-9]'
        )
    )
"""

# Postcodes with at least MIN_EPC_NEIGHBOURS usable certificates or one
# modelled sale. `postcode_filter` narrows both halves to one postcode for
# `api.market.pricing.ADDRESSES_SQL` (e.g. ``AND a.postcode = :postcode``);
# the full build passes nothing.
_RESIDENTIAL_POSTCODES_CTE_TEMPLATE = """
residential_postcodes AS (
    SELECT a.postcode
    FROM production.address_table a
    JOIN production.epc_table ec ON ec.address_id = a.id
    WHERE ec.total_floor_area IS NOT NULL
      AND ec.current_energy_rating IS NOT NULL
      {postcode_filter}
    GROUP BY a.postcode
    HAVING count(*) >= {min_neighbours}
    UNION
    SELECT a.postcode
    FROM production.address_table a
    JOIN production.ppd_table p ON p.address_id = a.id
    JOIN production.london_houses lh ON lh.ppd_id = p.id
    WHERE true
      {postcode_filter}
)
"""


def residential_postcodes_cte(postcode_filter: str = "") -> str:
    """Return the ``residential_postcodes`` CTE `_PRICEABLE_PREDICATE` reads, optionally narrowed."""
    return _RESIDENTIAL_POSTCODES_CTE_TEMPLATE.format(
        postcode_filter=postcode_filter, min_neighbours=MIN_EPC_NEIGHBOURS
    )


RESIDENTIAL_POSTCODES_CTE = residential_postcodes_cte()

# Upper-case, drop apostrophes so MARY'S keys as MARYS rather than MARY S, and
# collapse every other non-alphanumeric run to one space. The same rule the API
# applies to `q` in `api.market.address_search.normalise_query`; the two must
# agree or a typed address will not match its own indexed form.
#
# Named for, and placed beside, the `staging.hpldn_*` family in
# ``migrations/landing_ddl.py``; this one lives in ``production`` because that
# is the schema the relation and the reading role belong to.
SEARCHNORM_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION production.hpldn_searchnorm(raw text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT trim(regexp_replace(
        translate(upper(coalesce(raw, '')), '''’`', ''),
        '[^A-Z0-9]+', ' ', 'g'))
$$
"""

SEARCHNORM_FUNCTION_NAME = "production.hpldn_searchnorm"

# The migration role has no CREATE on the database -- deliberately, see the
# README's role table -- so the extension is installed through the same
# SECURITY DEFINER helper pattern ``src/hpldn/backend/db/sql/db_init.sql`` already uses for
# postgis and btree_gist. The direct CREATE is the fallback for a database whose
# owner is running the migration.
PG_TRGM_DEPLOY_SQL = "SELECT public.deploy_pg_trgm_extension()"
PG_TRGM_CREATE_SQL = "CREATE EXTENSION IF NOT EXISTS pg_trgm"
PG_TRGM_PRESENT_SQL = "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"

PG_TRGM_MISSING = (
    "production.address_search needs the pg_trgm extension, which is available "
    "but not installed, and this role may not install it.\n"
    "\n"
    "Run src/hpldn/backend/db/sql/db_init.sql as a superuser (it defines "
    "public.deploy_pg_trgm_extension() beside deploy_spatial_extension() and "
    "deploy_btree_gist_extension()), or install it directly:\n"
    "\n"
    "    sudo -u postgres psql -v ON_ERROR_STOP=1 -d <database> \\\n"
    "         -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'\n"
)

DROP_TABLE_SQL = "DROP TABLE IF EXISTS production.address_search"

# Supporting index for the priceability EXISTS; without it the build plans as a
# hash join over all 4.6M certificates.
EPC_ADDRESS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_prod_epc_address_id "
    "ON production.epc_table (address_id)"
)

# One row per home, with duplicate register rows folded exactly as
# `api.market.pricing.collapse_addresses` folds them for /addresses:
#
#   * rows are keyed on the normalised (postnumber, poststreet) within a
#     postcode, so "Flat 3" and "FLAT 3," are one home, and since 0031 on
#     the unit's key (`production.hpldn_unit_key`), so "Flat 6, 17 Woodville
#     Road" is its own home rather than folded into "17 Woodville Road";
#   * a blank-street row is dropped when the same number and unit in the same
#     postcode has exactly one named street, because that named row is the same home;
#     with two or more named streets the number is ambiguous and the blank row
#     stays, as it does in collapse_addresses;
#   * a row with no house number is not a home anyone can price, and is dropped.
#
# A home the register holds with no street ("232A", SW11 3AA) is searched under
# its postcode's street when every named home in that postcode is on one street
# (`hint`, since 0024): "232a battersea bridge" then finds it. Streets are
# compared by `production.hpldn_street_key` (since 0025), so a certificate-form
# "13 The Gardens" does not count as a second street beside "THE GARDENS", and
# the hint is that key. A row whose number
# already names the street ("BARCLAY ROAD" as the number) takes no hint. The hint feeds
# ``search_text`` and ``name_text``, and ``street_hint`` so a suggestion can say
# why it matched; ``poststreet`` stays as the register has it, because
# ``POST /price`` resolves the home by the register's own fields. The unit
# leads ``search_text`` ("FLAT 6 17 WOODVILLE ROAD ..."); ``name_text`` is
# unchanged, so a street prefix lists a building's flats together.
#
# The representative address_id is the lowest of the folded group, which is
# stable across rebuilds because address_table ids are never reissued.
_CREATE_TABLE_TEMPLATE = """
CREATE TABLE production.address_search AS
WITH %(residential_postcodes)s, priceable AS (
    SELECT a.id, a.postnumber, a.poststreet, a.posttown, a.postcode, %(unit)s AS unit
    FROM production.address_table a
    WHERE %(priceable)s
), normed AS (
    SELECT id, postnumber, poststreet, posttown, postcode, unit,
           production.hpldn_searchnorm(postnumber) AS num_norm,
           production.hpldn_searchnorm(poststreet) AS street_norm,
           coalesce(production.hpldn_unit_key(unit), '') AS unit_key
    FROM priceable
), homes AS (
    SELECT DISTINCT ON (postcode, num_norm, street_norm, unit_key)
           id AS address_id, postnumber, poststreet, posttown, postcode,
           num_norm, street_norm, unit, unit_key
    FROM normed
    WHERE num_norm <> ''
    ORDER BY postcode, num_norm, street_norm, unit_key, id
), named AS (
    SELECT postcode, num_norm, unit_key,
           count(*) FILTER (WHERE street_norm <> '') AS named_streets
    FROM homes
    GROUP BY postcode, num_norm, unit_key
), hint AS (
    SELECT postcode, min(production.hpldn_street_key(poststreet)) AS street,
           production.hpldn_searchnorm(min(production.hpldn_street_key(poststreet))) AS street_norm
    FROM homes
    WHERE street_norm <> ''
    GROUP BY postcode
    HAVING count(DISTINCT production.hpldn_street_key(poststreet)) = 1
)
SELECT h.address_id,
       h.postnumber,
       h.poststreet,
       h.posttown,
       h.postcode,
       production.hpldn_searchnorm(
           concat_ws(' ', h.unit, h.postnumber, coalesce(nullif(h.street_norm, ''), t.street),
                     h.posttown, h.postcode)
       ) AS search_text,
       coalesce(nullif(h.street_norm, ''), t.street_norm, h.num_norm) AS name_text,
       t.street AS street_hint,
       h.unit,
       nullif(h.unit_key, '') AS unit_key
FROM homes h
JOIN named n ON n.postcode = h.postcode AND n.num_norm = h.num_norm AND n.unit_key = h.unit_key
LEFT JOIN hint t ON t.postcode = h.postcode AND h.street_norm = ''
                 AND position(t.street_norm IN h.num_norm) = 0
WHERE h.street_norm <> '' OR n.named_streets <> 1
"""

# ``production.address_table.unit`` arrives with 0030; the rebuilds of
# 0021-0029 run before it, so the build takes NULL for it until then.
UNIT_COLUMN_SQL = """
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'production' AND table_name = 'address_table' AND column_name = 'unit'
)
"""


def create_table_sql(has_unit: bool = True) -> str:
    """Return the build statement, reading ``a.unit`` only when it exists (`CREATE_TABLE_SQL` does)."""
    return _CREATE_TABLE_TEMPLATE % {
        "residential_postcodes": RESIDENTIAL_POSTCODES_CTE.strip(),
        "priceable": _PRICEABLE_PREDICATE.strip(),
        "unit": "a.unit" if has_unit else "NULL::varchar",
    }

CREATE_TABLE_SQL = create_table_sql()

# gin_trgm_ops serves the infix and word-similarity matching; the two
# text_pattern_ops btrees serve the anchored prefixes, which a default
# en_US.UTF-8 btree cannot (measured: 101 ms sequential scan on
# address_table.postcode for LIKE 'SW11 1A%').
CREATE_INDEXES_SQL = """
CREATE UNIQUE INDEX idx_prod_address_search_address_id
    ON production.address_search (address_id);
CREATE INDEX idx_prod_address_search_text_trgm
    ON production.address_search USING gin (search_text gin_trgm_ops);
CREATE INDEX idx_prod_address_search_name_prefix
    ON production.address_search (name_text text_pattern_ops);
CREATE INDEX idx_prod_address_search_postcode_prefix
    ON production.address_search (postcode text_pattern_ops);
"""

# Floors, not exact counts: the relation grows with every data refresh. Measured
# on hpdbldn at revision 0020 (1,283,559 homes with a certificate); 0031
# raises its own floor (`ADDRESS_SEARCH_UNIT_ROWS_EXPECTED`) once units and
# 0030's certified homes are in.
ADDRESS_SEARCH_ROWS_EXPECTED = 1_100_000


def install_pg_trgm(engine, log=None) -> None:
    """
    Install ``pg_trgm``, through the deploy helper or directly.

    Each attempt runs in its own transaction, so a permission failure does not
    poison the connection the next attempt uses.

    Raises
    ------
    RuntimeError
        With `PG_TRGM_MISSING` when neither route works and the extension is
        still absent.
    """
    from sqlalchemy import text

    with engine.connect() as connection:
        if connection.execute(text(PG_TRGM_PRESENT_SQL)).first() is not None:
            return

    for statement in (PG_TRGM_DEPLOY_SQL, PG_TRGM_CREATE_SQL):
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except Exception as exc:  # noqa: BLE001 -- the next route is tried, then we raise
            if log is not None:
                log.info("pg_trgm not installed by %r: %s", statement, exc)
            continue
        if log is not None:
            log.info("pg_trgm installed by %r", statement)
        return

    raise RuntimeError(PG_TRGM_MISSING)


def rebuild(engine, cfg=None, log=None) -> int:
    """
    Drop and rebuild ``production.address_search`` and its four indexes.

    Call this whenever ``production.london_houses`` is refreshed: both derive
    from the same register and certificate tables.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Engine to open a dedicated committing connection on.
    cfg : dict, optional
        ``staging.yaml``, for bulk tuning overrides.
    log : logging.Logger, optional
        Logger for progress messages.

    Returns
    -------
    int
        The relation's row count.

    Notes
    -----
    Runs outside alembic's per-migration transaction, like
    `migrations.london_houses_view.rebuild`, so the build does not hold locks
    for the whole revision.
    """
    from sqlalchemy import text

    from hpldn.backend.common.tuning import apply_bulk_session_tuning
    from hpldn.backend.migrations.address_parse_sql import UNIT_KEY_SQL
    from hpldn.backend.migrations.london_houses_view import STREET_KEY_FUNCTION_SQL

    install_pg_trgm(engine, log)

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(SEARCHNORM_FUNCTION_SQL))
        connection.execute(text(STREET_KEY_FUNCTION_SQL))
        connection.execute(text(UNIT_KEY_SQL))
        connection.execute(text(EPC_ADDRESS_INDEX_SQL))
        has_unit = connection.execute(text(UNIT_COLUMN_SQL)).scalar_one()
        # The street hints' HAVING count(DISTINCT ...) = 1 is estimated at one
        # row (it is ~100K), which plans their join as a nested loop over every
        # home: 32 minutes at 0029 instead of a minute with a hash join.
        connection.execute(text("SET LOCAL enable_nestloop = off"))
        connection.execute(text(DROP_TABLE_SQL))
        connection.execute(text(create_table_sql(has_unit)))
        connection.execute(text(CREATE_INDEXES_SQL))
        connection.execute(text("ANALYZE production.address_search"))
        rows = connection.execute(
            text("SELECT count(*) FROM production.address_search")
        ).scalar_one()
    if log is not None:
        log.info("production.address_search rebuilt with %d row(s)", rows)
    return rows


def main(argv=None) -> int:
    """
    Rebuild ``production.address_search`` from the command line.

    Run after every ``REFRESH MATERIALIZED VIEW CONCURRENTLY
    production.london_houses``, against the database alembic targets
    (``HPLDN_DB_URL``, else ``POSTGRES_DEVELOP_*``)::

        python -m hpldn.backend.migrations.address_search

    Then restart the API, or wait ``SearchCache``'s ten-minute TTL, so no worker
    serves suggestions from before the rebuild.
    """
    import argparse
    import logging
    import os

    from sqlalchemy import create_engine

    from hpldn.backend.common.db import get_migration_db_url

    argparse.ArgumentParser(description=main.__doc__.strip().splitlines()[0]).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("hpldn.address_search")
    url = os.environ.get("HPLDN_DB_URL") or get_migration_db_url()
    engine = create_engine(url)
    try:
        rows = rebuild(engine, None, log)
    finally:
        engine.dispose()
    if rows < ADDRESS_SEARCH_ROWS_EXPECTED:
        log.error("only %d row(s); expected at least %d", rows, ADDRESS_SEARCH_ROWS_EXPECTED)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
