"""
Fuse the 2024-2026 EPC certificates and Price Paid transactions into the staging spine.

``staging.ukhpucl`` ends in December 2024, so later sales are fused from
``epc_domestic`` and ``ppd_domestic`` on address: derive London EPC
addresses, insert certificates, load the Tier A fixture's transactions, build
the ``epc_addrkey`` gazetteer, run the four-tier geometry-guarded matcher,
mint missing addresses, insert fused transactions, then verify the link rate.

Revision ID: 0006
Revises: 0005
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.migrations.epc_fusion_sql import (
    ADDRESS_DERIVATION_STATEMENTS,
    EPC_CONDENSED_INSERT_SQL,
    EPC_DEDUPE_STATEMENTS,
    EPC_TABLE_INSERT_SQL,
    SI_FUNCTION_SQL,
    TIERA_ADDRESS_MINT_SQL,
    TIERA_PPD_TABLE_INSERT_SQL,
)
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Fixture keys the four-tier matcher does not reproduce, resolved through the
# fixture's UPRN. Zero means the fixture can be retired.
_FIXTURE_SURPLUS_SQL = """
SELECT count(*)
FROM staging._ppd_uprn_linkage_tiera f
JOIN staging.ukhcs_table u ON u."UPRN" = f.uprn
JOIN staging.address_table a ON a.uid = u.id
WHERE NOT EXISTS (
    SELECT 1 FROM staging.ppd_uprn_linkage l
    WHERE l.address_id = a.id
      AND l.dateoftransfer = f.dateoftransfer
      AND l.price = f.price
)
"""

# Fixture rows whose UPRN is absent from ukhcs_table, so no transaction can be placed.
_TIERA_MISSING_ADDRESS_SQL = """
SELECT count(*)
FROM staging._ppd_uprn_linkage_tiera l
WHERE NOT EXISTS (
    SELECT 1
    FROM staging.ukhcs_table u
    JOIN staging.address_table a ON a.uid = u.id
    WHERE u."UPRN" = l.uprn
)
"""


def _derive_london_epc_addresses(engine, log) -> None:
    """
    Build the London EPC address tables, in two transactions.

    The first transaction's ANALYZE gives the planner statistics for the
    second's joins against ``ukhcs_table`` and ``address_table``.
    """
    with engine.begin() as connection:
        for statement in ADDRESS_DERIVATION_STATEMENTS:
            connection.execute(text(statement))
        uprns = connection.execute(
            text("SELECT count(*) FROM staging._epc_london_addr")
        ).scalar_one()
    log.info("0006: staging._epc_london_addr holds %d London EPC UPRN(s)", uprns)

    _rebuild_london_epc_pick(engine, log)


def _rebuild_london_epc_pick(engine, log) -> None:
    """
    Resolve London certificates to addresses, one per (address_id, inspection_date).

    Runs twice, because both address mints happen after the first pass.
    """
    with engine.begin() as connection:
        for statement in EPC_DEDUPE_STATEMENTS:
            connection.execute(text(statement))
        certs = connection.execute(
            text("SELECT count(*) FROM staging._epc_london_pick")
        ).scalar_one()
    log.info(
        "0006: staging._epc_london_pick holds %d deduplicated London "
        "certificate(s)", certs,
    )
    if not certs:
        raise RuntimeError(
            "staging._epc_london_pick is empty -- no London certificate "
            "resolved to an address_table row. Check that staging.epc_domestic "
            "loaded (0003) and that staging.address_table is populated (0005)."
        )


def _insert_certificates(engine, log) -> None:
    """
    Insert the picked certificates into the staging EPC tables.

    ``pg_temp.si()`` is per connection, so it is created in the same
    transaction as the inserts.
    """
    with engine.begin() as connection:
        connection.execute(text(SI_FUNCTION_SQL))
        inserted = connection.execute(text(EPC_TABLE_INSERT_SQL)).rowcount
    log.info("0006: inserted %d row(s) into staging.epc_table", inserted)

    with engine.begin() as connection:
        inserted = connection.execute(text(EPC_CONDENSED_INSERT_SQL)).rowcount
    log.info("0006: inserted %d row(s) into staging.epc_table_condensed", inserted)


def _insert_tiera_transactions(engine, log) -> int:
    """
    Mint the fixture's missing addresses, then insert its transactions.

    Both run in one transaction because the insert resolves each fixture row
    through ``address_table``.

    Returns
    -------
    int
        Transactions inserted.
    """
    with engine.begin() as connection:
        linked = connection.execute(
            text("SELECT count(*) FROM staging._ppd_uprn_linkage_tiera")
        ).scalar_one()
        minted = connection.execute(text(TIERA_ADDRESS_MINT_SQL)).rowcount
        missing_address = connection.execute(
            text(_TIERA_MISSING_ADDRESS_SQL)
        ).scalar_one()
        inserted = connection.execute(text(TIERA_PPD_TABLE_INSERT_SQL)).rowcount
    log.info(
        "0006: Tier A fixture holds %d linked transaction(s); minted %d "
        "staging.address_table row(s) for fixture-only UPRNs; %d inserted "
        "into staging.ppd_table (the rest already present)",
        linked, minted, inserted,
    )
    if missing_address:
        log.warning(
            "0006: %d Tier A fixture row(s) skipped -- their uprn is absent "
            "from staging.ukhcs_table, so no staging.address_table row "
            "exists to attach the transaction to",
            missing_address,
        )
    return linked


def _report_fixture_surplus(engine, log) -> int:
    """Log whether the Tier A fixture is still needed and return its surplus key count."""
    with engine.connect() as connection:
        surplus = connection.execute(text(_FIXTURE_SURPLUS_SQL)).scalar_one()
    if surplus:
        log.info(
            "0006: the Tier A fixture contributes %d (address_id, "
            "dateoftransfer, price) key(s) the four-tier matcher does not "
            "reproduce -- KEEP migration_inputs/ppd_uprn_linkage_tierA.csv",
            surplus,
        )
    else:
        log.info(
            "0006: the four-tier matcher reproduces every Tier A fixture row. "
            "migration_inputs/ppd_uprn_linkage_tierA.csv can be retired, "
            "along with its inputs.yaml and seed_registry entries and staging."
            "_ppd_uprn_linkage_tiera"
        )
    return surplus


def upgrade() -> None:
    """
    Run the fusion pipeline and verify it.

    Notes
    -----
    Certificates are derived a second time after both address mints, because
    the first pick only saw addresses that already existed; without it, minted
    addresses would lack certificates and ``epc_matched`` would fall short of
    its expected count. Both inserts are idempotent, so the second pass only
    adds what is new.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info("0006: deriving London EPC addresses from staging.epc_domestic")
    _derive_london_epc_addresses(engine, log)

    log.info("0006: inserting certificates into the staging EPC tables")
    _insert_certificates(engine, log)

    log.info("0006: loading the hand-built Tier A linkage")
    _insert_tiera_transactions(engine, log)

    log.info("0006: staging.epc_addrkey (four tier keys per certificate)")
    steps.create_epc_addrkey_table(engine, cfg, log)

    log.info(
        "0006: staging.ppd_uprn_linkage (four-tier match, geometry-guarded "
        "at %dm against Code-Point Open)",
        steps.PPD_EPC_GEO_THRESHOLD_M,
    )
    steps.create_ppd_uprn_linkage_table(engine, cfg, log)

    log.info("0006: minting address rows for matched UPRNs with none")
    steps.mint_address_rows_for_unresolved_uprns(engine, cfg, log)

    log.info("0006: re-deriving certificates for the addresses just minted")
    _rebuild_london_epc_pick(engine, log)
    _insert_certificates(engine, log)

    log.info("0006: inserting the fused PPD transactions")
    steps.insert_fused_ppd_rows(engine, cfg, log)

    _report_fixture_surplus(engine, log)

    log.info(
        "0006: verifying the %s..%s fusion window",
        steps.PPD_EPC_FUSION_WINDOW_START,
        steps.PPD_EPC_FUSION_WINDOW_END,
    )
    steps.verify_epc_ppd_fusion_v2(engine, cfg, log)


def downgrade() -> None:
    """Refuse; fused transactions carry no marker distinguishing them from ukhpucl rows."""
    no_downgrade()
