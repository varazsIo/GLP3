"""
Build the London staging spine: addresses, transactions, certificates and categories.

Derives, in dependency order: ``london_houses``, ``address_table``,
``london_houses_condensed``, ``poststreet``, ``ppd_table``,
``epc_table_condensed``, ``epc_fused_staging_table``, the category tables and
``epc_table``, then INSPIRE parcels and ``london_parcels``, in one pass.

Revision ID: 0005
Revises: 0004
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Tables this revision must leave non-empty; an INSERT ... SELECT whose filter
# matches nothing succeeds silently.
_REQUIRED_NON_EMPTY = (
    "staging.london_houses",
    "staging.address_table",
    "staging.london_houses_condensed",
    "staging.ppd_table",
    "staging.epc_table_condensed",
    "staging.epc_fused_staging_table",
    "staging.epc_table",
    "staging.london_parcels",
)

_CATEGORY_TABLES = (
    "staging.rating_category_table",
    "staging.build_form_category_table",
    "staging.transaction_type_category_table",
    "staging.energy_tariff_category_table",
    "staging.glazed_type_category_table",
)


def _assert_populated(engine, log) -> None:
    """
    Check every spine and category table holds rows.

    Raises
    ------
    RuntimeError
        Naming the empty tables.
    """
    with engine.connect() as connection:
        empty = []
        for table in _REQUIRED_NON_EMPTY + _CATEGORY_TABLES:
            rows = connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()
            log.info("0005: %s holds %d row(s)", table, rows)
            if rows == 0:
                empty.append(table)
    if empty:
        raise RuntimeError(
            f"0005 left {len(empty)} spine table(s) empty: {', '.join(empty)}. "
            "Every later revision keys off staging.address_table.id and "
            "staging.ppd_table.id, so this must not be allowed through."
        )


def upgrade() -> None:
    """
    Build the spine.

    Notes
    -----
    Re-runnable: ``london_houses`` and ``london_houses_condensed`` are dropped
    first because their builders issue a bare ``CREATE TABLE``, and
    ``address_table`` is truncated (CASCADE) because its insert relies on a
    fresh table for uniqueness.

    ``london_houses_condensed`` holds one row per (sale, certificate), which
    would violate ``uq_ppd_table_transaction`` in ``load_ppd_table``. So
    ``load_ppd_table`` is handed a copy collapsed to one row per
    (uprn, date, price), preferring a valid duration (F or L) and then the
    newest certificate, and the full table is restored for the certificate
    steps. Without the duration preference a sale whose newest row has an
    unknown duration would be dropped to ``ppd_table_errors``.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0005: staging.london_houses (London slice of ukhpucl)")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging.london_houses"))
    steps.create_london_houses_table(engine, cfg, log)

    log.info("0005: staging.address_table (one row per UPRN)")
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE staging.address_table CASCADE"))
    steps.create_address_table(engine, cfg, log)

    log.info("0005: staging.london_houses_condensed")
    with engine.begin() as connection:
        connection.execute(
            text("DROP TABLE IF EXISTS staging.london_houses_condensed")
        )
    steps.create_london_houses_condensed_table(engine, cfg, log)

    log.info("0005: address_table.poststreet from the EPC address lines")
    steps.add_poststreet_to_address_table(engine, cfg, log)

    log.info("0005: staging.ppd_table (the transaction record)")
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER INDEX IF EXISTS staging.idx_lhc_uprn "
                "RENAME TO idx_lhc_uprn_full"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE staging.london_houses_condensed "
                "RENAME TO london_houses_condensed_full"
            )
        )
        connection.execute(
            text(
                """
                CREATE UNLOGGED TABLE staging.london_houses_condensed AS
                SELECT DISTINCT ON (uprn, date, price) *
                FROM staging.london_houses_condensed_full
                ORDER BY uprn, date, price,
                         (duration IN ('F', 'L')) DESC NULLS LAST,
                         inspection_date DESC NULLS LAST;
                """
            )
        )

    steps.load_ppd_table(engine, cfg, log)

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE staging.london_houses_condensed"))
        connection.execute(
            text(
                "ALTER TABLE staging.london_houses_condensed_full "
                "RENAME TO london_houses_condensed"
            )
        )
        connection.execute(
            text(
                "ALTER INDEX IF EXISTS staging.idx_lhc_uprn_full "
                "RENAME TO idx_lhc_uprn"
            )
        )

    log.info("0005: staging.epc_table_condensed (one row per UPRN+inspection)")
    steps.load_epc_table_condensed(engine, cfg, log)

    log.info("0005: staging.epc_fused_staging_table")
    steps.create_epc_fused_staging_table(engine, cfg, log)

    log.info("0005: five category tables + staging.epc_table")
    steps.insert_epc_data_into_db(engine, cfg, log)

    log.info("0005: INSPIRE parcels -> staging.london_parcels lot areas")
    steps.insert_inspire_polygon_data(engine, cfg, log, full_rebuild=True)

    _assert_populated(engine, log)
    log.info("0005: London staging spine built")


def downgrade() -> None:
    """Refuse; the spine's keys are what everything downstream joins on."""
    no_downgrade()
