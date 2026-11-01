"""
Link more of the unheld London sales: isolated OSM outlines, flat sequences, house brackets.

After 0028, 3,101,552 of the 3,724,168 London standard sales of 1995-2026
were held (83.3 %). The link-precision harness measured three more rules on
leave-one-out ground truth (true UPRN hidden), each over the gate
(`sale_linking.MIN_TIER_PRECISION`):

- ``osm_polygon_isolated``: an OpenStreetMap building outline's one UPRN,
  when no unaddressed UPRN lies within 10 m of it (every osm_polygon error was
  the home's real, unaddressed UPRN 3-30 m away in the next building);
- ``flat_sequence``: a flat of a building whose known flats all sit at
  UPRN = offset + flat number;
- ``house_bracket``: a house between neighbours n-2 and n+2 whose UPRNs are
  at most 4 apart, with one unassigned UPRN of the postcode between them.

The last two (L9) run on every target, a linked sale's own link hidden, so a
rule that disagrees with an earlier link puts the address group in conflict.
London certificates lodged without a UPRN are resolved where a held sale has
the same address text or the L9 rules place them
(``staging.epc_uprn_resolved``); they join the certificate gazetteer, so the
``epc_*`` tiers can link other sales of those homes, and 0030 mints their
homes. OpenStreetMap-derived links are copied to
``staging.ppd_uprn_link_odbl`` (ODbL; see `sale_linking` for the statements
that drop them).

No new sources: 0027/0028's loads are reused. The carry and every rebuild are
0028's (`hpldn.backend.migrations.link_carry`). The view is dropped while it
is rebuilt: stop the API first, or restart it after. Run
``src/hpldn/backend/db/sql/db_grants_post_migration.sql`` afterwards, restart the API,
retrain and run the serving drift test before swapping ``model.cbm``.

Revision ID: 0029
Revises: 0028
"""

from sqlalchemy import text

from hpldn.backend.migrations import link_carry
from hpldn.backend.migrations import sale_linking as sl
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# Ledger rows 0028 carried on hpdbldn.
LEDGER_LINKS_BEFORE = 145_449
# Guards, from the link-precision harness run on hpdbldn:
# new links this revision must add to the ledger -- 106,329 in that run
# (osm_polygon_isolated 39,275, house_bracket 38,491, flat_sequence 22,809,
# ucl 3,523, building_elimination 765, group 482, epc_exact 469,
# street_elimination 329, others 188; 2 earlier links dropped as L9 disagreed)
# -- and the held share of the raw London standard sales of 1995-2026
# afterwards (83.3 % before; about 86 % expected).
MIN_NEW_LINKS = 95_000
MIN_HELD_SHARE = 0.855
# Held links against HMLR's lookup: 99.16 % before 0029 (7,324 / 7,386);
# 0027's floor.
MIN_HMLR_AGREEMENT = 0.985
# UPRN-less London certificates resolved afterwards: 22,371 in a dry run
# (cert_text 21,403, cert_flat_sequence 904, cert_house_bracket 64).
MIN_RESOLVED_CERTIFICATES = 20_000
# 0028's floor for street hints (5,223 on hpdbldn after 0028).
MIN_STREET_HINTS = 4_000


def _check_resolved(engine, counts, log) -> None:
    """
    Check the resolved UPRN-less certificates.

    Raises
    ------
    RuntimeError
        If fewer than `MIN_RESOLVED_CERTIFICATES` are resolved, or one
        address resolves to several UPRNs.
    """
    total = sum(counts.values())
    with engine.connect() as connection:
        split = connection.execute(
            text(
                f"""
                SELECT count(*) FROM (
                    SELECT staging.hpldn_pcnorm(e.postcode),
                           staging.hpldn_addrkey(concat_ws(' ', e.address1, e.address2, e.address3))
                    FROM {sl.RESOLVED_TABLE} r
                    JOIN staging.epc_domestic_history e ON e.certificate_number = r.certificate_number
                    GROUP BY 1, 2
                    HAVING count(DISTINCT r.uprn) > 1
                ) x
                """
            )
        ).scalar_one()
    log.info("0029: %d UPRN-less certificate(s) resolved (%s); %d address(es) on several UPRNs", total, counts, split)
    if total < MIN_RESOLVED_CERTIFICATES or split:
        raise RuntimeError(
            f"0029: {total} resolved certificate(s) (want >= {MIN_RESOLVED_CERTIFICATES}); "
            f"{split} address(es) resolved to several UPRNs"
        )


def upgrade() -> None:
    """Resolve certificates, link, check, carry, then rebuild production as 0028 did."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    # The gazetteer gains the resolved certificates (by the rules that last
    # passed their gates) before the tiers read it.
    sl.refresh_certificate_gazetteer(engine, cfg, log)
    result = sl.link_sales(engine, cfg, log)
    link_carry.check_ledger(engine, result, log, "0029", LEDGER_LINKS_BEFORE + MIN_NEW_LINKS)

    # The certificate back-fill bounds on staging.epc_table_condensed (UNLOGGED).
    steps.restore_crash_reset_london_staging(engine, cfg, log)
    sl.carry_links_into_staging(engine, cfg, log)
    link_carry.check_held(engine, log, "0029", MIN_HELD_SHARE, "83.3%")
    link_carry.check_hmlr_agreement(engine, log, "0029", MIN_HMLR_AGREEMENT)

    # Again with this run's gates and sales, for 0030 and the next run.
    _check_resolved(engine, sl.refresh_certificate_gazetteer(engine, cfg, log), log)

    link_carry.refresh_newly_sold_addresses(engine, cfg, log)
    link_carry.rebuild_production(engine, cfg, log, "0029", MIN_STREET_HINTS)

    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {sl.CANDIDATE_TABLE}"))
    log.info(
        "0029: complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  restart the API (its caches read the view)\n"
        "  retrain hpp_cb and run the serving drift test before swapping model.cbm"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
