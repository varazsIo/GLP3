"""
Widen ``production.address_search`` to homes without an energy certificate.

Before this revision a home was suggestible, listable by ``GET /addresses`` and priceable
only when one of its register rows had a certificate carrying a floor area and
an energy rating. Measured on hpdbldn at 0021, 6,470 homes that have sold --
17 LICHFIELD ROAD, RICHMOND, TW9 3JR among them (detached, £5.188M on
2026-07-09, no certificate) -- could not be found, and 30.3k numbered register
rows in residential postcodes had no usable certificate.

``hpp_inference.estimate_epc`` now prices such a home from a surrogate
certificate built from comparable certified homes nearby, so the search
universe widens to match (`migrations.address_search._PRICEABLE_PREDICATE`):
a usable certificate, OR a residential sale of its own, OR a house number in a
postcode with at least ``MIN_EPC_NEIGHBOURS`` certificates or one modelled
sale. Building-level rows with no number stay out.
``api.market.pricing.ADDRESSES_SQL`` imports the same fragment.

The relation is rebuilt with `migrations.address_search.rebuild` (the same
entry point 0021 used), then 0021's shape and plan checks run again, the row
count is held to this revision's measured floor, and a sample of sold homes
without a certificate must now be searchable.

Restart the API afterwards: ``SearchCache`` keeps suggestions for ten minutes.

Revision ID: 0022
Revises: 0021
"""

from sqlalchemy import text

from hpldn.backend.migrations.address_search import rebuild
from hpldn.backend.migrations.chain import load_revision, no_downgrade
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Floor, not an exact count. Measured on hpdbldn: 1,283,559 homes at 0021 and
# 1,301,395 with this revision's predicate; the floor sits above the former, so
# a rebuild that silently lost the widening fails, and just below the latter,
# so an ordinary data refresh does not.
ADDRESS_SEARCH_NO_EPC_ROWS_EXPECTED = 1_290_000

# Sold homes with no usable certificate on any register row. Every one sampled
# must now be suggestible under its own postcode and house number.
_SOLD_WITHOUT_CERTIFICATE_SQL = text(
    """
    SELECT count(*) AS sampled,
           count(*) FILTER (
               WHERE NOT EXISTS (
                   SELECT 1 FROM production.address_search s
                   WHERE s.postcode = sample.postcode
                     AND production.hpldn_searchnorm(s.postnumber)
                         = production.hpldn_searchnorm(sample.postnumber)
               )
           ) AS missing
    FROM (
        SELECT DISTINCT a.postcode, a.postnumber
        FROM production.ppd_table p
        JOIN production.london_houses lh ON lh.ppd_id = p.id
        JOIN production.address_table a ON a.id = p.address_id
        WHERE NOT lh.epc_matched
          AND lh.dateoftransfer >= DATE '2020-01-01'
          AND a.postnumber ~ '[A-Za-z0-9]'
          AND NOT EXISTS (
              SELECT 1 FROM production.epc_table ec
              WHERE ec.address_id = a.id
                AND ec.total_floor_area IS NOT NULL
                AND ec.current_energy_rating IS NOT NULL
          )
        LIMIT 500
    ) sample
    """
)

_WITHOUT_CERTIFICATE_COUNT_SQL = text(
    """
    SELECT count(*)
    FROM production.address_search s
    WHERE NOT EXISTS (
        SELECT 1 FROM production.epc_table ec
        WHERE ec.address_id = s.address_id
          AND ec.total_floor_area IS NOT NULL
          AND ec.current_energy_rating IS NOT NULL
    )
    """
)


def _check_rows(rows: int, log) -> None:
    """Check the widened relation holds at least the measured number of homes."""
    if rows < ADDRESS_SEARCH_NO_EPC_ROWS_EXPECTED:
        raise AssertionError(
            f"0022: production.address_search holds {rows:,} rows, expected at "
            f"least {ADDRESS_SEARCH_NO_EPC_ROWS_EXPECTED:,}. The widened priceability "
            "test is not in effect, or the register has shrunk."
        )
    log.info("0022: %s home(s) suggestible", f"{rows:,}")


def _check_sold_homes_without_certificate(engine, log) -> None:
    """Check a sample of sold homes without a certificate is now searchable."""
    with engine.connect() as connection:
        sampled, missing = connection.execute(_SOLD_WITHOUT_CERTIFICATE_SQL).one()
        without = connection.execute(_WITHOUT_CERTIFICATE_COUNT_SQL).scalar_one()
    if sampled == 0:
        raise AssertionError("0022: found no sold home without a certificate to check")
    if missing:
        raise AssertionError(
            f"0022: {missing} of {sampled} sampled sold homes without a certificate are "
            "absent from production.address_search"
        )
    log.info(
        "0022: all %d sampled sold homes without a certificate are searchable; "
        "%s suggestion(s) have no certificate on their representative row",
        sampled,
        f"{without:,}",
    )


def upgrade() -> None:
    """Rebuild ``production.address_search`` with the widened test, then check it."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    previous = load_revision("0021")

    log.info("0022: rebuilding production.address_search with homes without a certificate")
    rows = rebuild(engine, cfg, log)

    previous._check_shape(engine, log)
    previous._check_rows(rows, log)
    _check_rows(rows, log)
    previous._check_covers_addresses_route(engine, log)
    _check_sold_homes_without_certificate(engine, log)
    previous._check_plan(engine, log)

    log.info(
        "0022: address search widened. Restart the API (or wait SearchCache's "
        "ten-minute TTL) so no worker serves suggestions from before the rebuild."
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
