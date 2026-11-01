"""
Link the London sales ``ppd_table`` lacks to real UPRNs, from open evidence, and load them.

After 0025, 753,174 of the 3,724,168 London standard sales of 1995-2026 were
not held (608,563 of them before 2009; 353,471 flats). 0025's matcher could
only place a sale on a UPRN some certificate names, and about 630K of those
sales are homes the certificate register never saw. This revision links them
through `hpldn.backend.migrations.sale_linking` -- HMLR's own lookup, UCL's
link, the certificate register with an alphanumeric/range-aware matcher,
OpenStreetMap building addresses, GLA planning applications, street and
building elimination over NSUL + OS Open Linked Identifiers, terminated
postcodes, and address-group propagation -- each method gated by its measured
precision (`hpldn.backend.scripts.link_precision`), every link recorded with
its evidence in ``staging.ppd_uprn_link``, and every sale left unlinked
recorded with a reason in ``staging.ppd_unlinked``. **No UPRN is invented**:
every link names a UPRN in OS Open UPRN and (terminated postcodes aside) in
the sale's own NSUL postcode, so addresses get real coordinates.

The new sales are carried into ``staging.ppd_table`` with ``transactionid``
and ``link_method``, new homes get address rows (0005's text convention, from
the sale), and everything downstream is rebuilt as 0025 did: production
append, ``uprn_lookup``, certificates, property types, every per-sale feature
table, ``production.london_houses`` and ``production.address_search``. The
view is dropped while it is rebuilt: stop the API first, or restart it after.

OpenStreetMap-derived links (``link_method`` ``osm_*`` / ``term_osm``) are
ODbL; see `sale_linking`. In the link-precision harness OpenStreetMap alone
(building outlines 98.1 %, address points 95.1 %, terminated postcodes
92.8 %), planning numeric matches (98.7 % against UCL on the unlinked sales)
and building elimination (99.4 %) measured under 99.5 % and were switched
off; only ``osm_planning`` (OpenStreetMap and a planning application agree)
links.

Run ``src/hpldn/backend/db/sql/db_grants_post_migration.sql`` afterwards, restart the
API, retrain (the sale set grows, mostly before 2009) and run the serving drift
test before swapping ``model.cbm``.

Revision ID: 0028
Revises: 0027
"""

from pathlib import Path

from sqlalchemy import text

from hpldn.backend.common.config import get_repo_root
from hpldn.backend.migrations import link_carry
from hpldn.backend.migrations import sale_linking as sl
from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
    require_data_path,
)

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# Guards, set below what the build on hpdbldn measured (see the log lines they
# sit beside) so a regression fails and noise does not.
# Sales the ledger links: 135,549 on hpdbldn (ucl 65,533,
# planning_exact 45,526, planning_numsig 7,207, group 4,071, epc_exact 3,863,
# epc_numsig 2,811, planning_tokenset 2,326, street_elimination 1,358,
# term_ucl 1,329, term_epc 1,154, hmlr 303, epc_tokenset 68).
MIN_NEW_LINKS = 115_000
# Share of the raw London standard sales of 1995-2026 held afterwards: 79.5 %
# before, 83.02 % after on hpdbldn (131,198 sales carried).
MIN_HELD_SHARE = 0.825

# Street-less homes that take their postcode's street as a hint. 0024's floor
# (95,000) predates the street fill (`link_carry.refresh_newly_sold_addresses`),
# which gave 501,111 sold addresses their Price Paid street: 5,223 homes still
# needed a hint on hpdbldn.
MIN_STREET_HINTS = 4_000


def _load_sources(engine, cfg, log) -> None:
    """Load LIDS + Open Roads, OSM, the planning Datahub and postcode history; key the certificate register."""
    lids_dir = require_data_path(cfg, "lids_dir", "data/lids", description="migration 0028 (OS LIDS)", must_be_dir=True)
    gpkg = require_data_path(cfg, "open_roads_gpkg_fp", "data/open_roads/Data/oproad_gb.gpkg",
                             description="migration 0028 (OS Open Roads)")
    pbf = require_data_path(cfg, "osm_pbf_fp", "data/osm/greater-london-latest.osm.pbf",
                            description="migration 0028 (OpenStreetMap Greater London)")
    planning = require_data_path(cfg, "planning_applications_csv_fp", "data/planning_datahub_applications.csv",
                                 description="migration 0028 (Planning London Datahub)")
    postcodes = require_data_path(cfg, "london_postcodes_csv_fp", "data/All_London_Postcodes_Doogal.csv",
                                  description="migration 0028 (terminated London postcodes)")
    osm_config = get_repo_root().joinpath("configuration", "osm_addresses.ini")

    sl.build_epc_link_gazetteer(engine, cfg, log)
    sl.load_lids_and_street_names(engine, Path(lids_dir), Path(gpkg), cfg, log)
    sl.load_osm_addresses(engine, Path(pbf), osm_config, cfg, log)
    sl.load_planning_applications(engine, Path(planning), cfg, log)
    sl.load_postcode_history(engine, Path(postcodes), cfg, log)


def upgrade() -> None:
    """Load sources, link, check, carry, then rebuild production as 0025 did."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    _load_sources(engine, cfg, log)
    result = sl.link_sales(engine, cfg, log)
    link_carry.check_ledger(engine, result, log, "0028", MIN_NEW_LINKS)

    # The certificate back-fill bounds on staging.epc_table_condensed (UNLOGGED).
    steps.restore_crash_reset_london_staging(engine, cfg, log)
    sl.carry_links_into_staging(engine, cfg, log)
    link_carry.check_held(engine, log, "0028", MIN_HELD_SHARE, "79.5%")
    link_carry.refresh_newly_sold_addresses(engine, cfg, log)
    link_carry.rebuild_production(engine, cfg, log, "0028", MIN_STREET_HINTS)

    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {sl.CANDIDATE_TABLE}"))
    log.info(
        "0028: complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  restart the API (its caches read the view)\n"
        "  retrain hpp_cb and run the serving drift test before swapping model.cbm"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
