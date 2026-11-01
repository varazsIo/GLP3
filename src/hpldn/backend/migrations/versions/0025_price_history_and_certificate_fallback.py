"""
Load the 1995-2023 Price Paid history and fill the model's data gaps.

Gaps measured on the database at 0024:

- **Certificates after the sale.** The view took a certificate only if it was inspected before the
  sale, so floor area was absent for every 1995-2008 sale and 5-8 % of
  2012-2016 ones although 99 % of those homes have a later certificate.
  `london_houses_view.CREATE_VIEW_SQL_0025` now falls back to the nearest later
  certificate for the time-invariant fields (floor area, rooms, build form,
  age band, floor level, property type); the rating and tenure stay as of
  the sale. ``epc_age_at_sale_days`` is signed and ``epc_after_sale`` marks
  the fallback. ``comps_features`` and ``neighbourhood_features`` read floor
  area the same way (`pipeline_steps.PPD_EPC_ASOF_LATERAL`), so pre-2009
  sales get £/m² comparables. ``epc_matched`` still means a usable
  certificate *before* the sale.
- **Street spellings.** The 2024+ back-fill's streets are in the certificate register's
  form (``Lausanne Road``, ``13 The Gardens``), which split same-street
  groups. ``production.hpldn_street_key`` normalises them, and the view
  carries the key as ``street_key``; `hpp_rf.normalise_street_key` is the
  same rule and is checked against it here on every distinct street.
- **Missing history.** Only 50.4 % of the 3.53M Greater London standard sales of
  1995-2023 were held: ``staging.ukhpucl`` has only sales linked to a
  certificate, and 0005 kept only those whose certificate passed its field
  checks (a numeric ``paon`` among them, which drops every flat in a named
  building). The pre-2024 sales of homes the 2024+ back-fill minted were
  never loaded at all: 0.6 % of their 2024+ sales had a previous sale,
  against 63 % elsewhere. HMLR ``pp-complete.csv``'s 1995-2023 Greater London
  sales are loaded into ``staging.ppd_domestic``, linked to UPRNs with 0006's
  tiered address matcher, excluding sales already held (by postcode, date
  and price; the insert's (address, date, price) key catches the rest), then
  carried into production as 0019 carried the 2024 back-fill.
- **IoD before 2011.** IoD features were NULL before the first release was in force
  (2011); such sales now take the earliest release (``iod_backfilled``).

Every per-sale feature table is rebuilt (0020's builders), then the view with
the new definition, then ``production.address_search`` (it reads the view).
The 0020 guards run again, plus this revision's own. Run
``src/hpldn/backend/db/sql/db_grants_post_migration.sql`` afterwards and retrain.

Revision ID: 0025
Revises: 0024
"""

from sqlalchemy import text

from hpldn.backend.migrations.address_search import rebuild as rebuild_address_search
from hpldn.backend.migrations.chain import (
    load_revision,
    no_downgrade,
    run_concurrently,
    vacuum_analyze,
)
from hpldn.backend.migrations.london_houses_view import (
    CREATE_VIEW_SQL_0025,
    STREET_KEY_FUNCTION_SQL,
    VIEW_COLUMNS_0025,
    rebuild,
)
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
    require_data_path,
)

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

HISTORY_TARGET_TABLE = "staging._ppd_target_history"

# First production.address_table id minted by the 2024+ back-fill (0017):
# the homes whose earlier sales this revision exists to load.
BACKFILL_ADDRESS_ID_MIN = 68_000_000

# Guards, set below what the build on hpdbldn measured (see the log lines
# they sit beside) so a regression fails and noise does not.
# Share of raw London category-A sales 1995-2023 held after the load: 50.4 %
# before; the linker places 1.01M of the 1.74M missing (58 %), so ~79 % after.
MIN_HISTORY_COMPLETENESS = 0.75
# 2024+ sales of back-filled homes with a previous sale: 0.6 % before, 17.5 %
# after (20.1 % of resales; their new-build first sales rightly have none, and
# most of the rest had earlier sales among the 42 % of history the linker
# cannot place). Every other home's 2024+ sales went from 63 % to 92.9 %.
MIN_BACKFILLED_PREV_SALE_SHARE = 0.10
# 1995-2008 sales with a floor area (0 % before).
MIN_PRE_2009_FLOOR_AREA_SHARE = 0.70
# Pre-2011 sales with IoD (0 % before).
MIN_PRE_2011_IOD_SHARE = 0.95

_OLDNEW_FROM_LINKAGE_SQL = f"""
UPDATE staging.ppd_table p
SET oldnew = d.oldnew
FROM {steps.PPD_UPRN_LINKAGE_HISTORY_TABLE} l
JOIN staging.ppd_domestic d ON d.transactionid = l.transactionid
WHERE p.address_id = l.address_id
  AND p.dateoftransfer = l.dateoftransfer
  AND p.price = l.price
  AND p.oldnew IS NULL
  AND d.oldnew IN ('Y', 'N')
"""

_STREETS_SQL = """
SELECT DISTINCT poststreet, production.hpldn_street_key(poststreet) AS street_key
FROM production.address_table
WHERE poststreet IS NOT NULL
"""

# Kept vs raw London category-A sales per year of the history window.
_HISTORY_COMPLETENESS_SQL = """
WITH raw AS (
    SELECT extract(year FROM dateoftransfer)::int AS year, count(*) AS n
    FROM staging.ppd_domestic
    WHERE county = 'GREATER LONDON' AND categorytype = 'A' AND recordstatus = 'A'
      AND propertytype IN ('D','S','T','F') AND duration IN ('F','L') AND price > 0
      AND dateoftransfer::date BETWEEN :window_start AND :window_end
    GROUP BY 1
), kept AS (
    SELECT extract(year FROM dateoftransfer)::int AS year, count(*) AS n
    FROM staging.ppd_table
    WHERE dateoftransfer BETWEEN :window_start AND :window_end
    GROUP BY 1
)
SELECT raw.year, coalesce(kept.n, 0) AS kept, raw.n AS raw
FROM raw LEFT JOIN kept USING (year)
ORDER BY raw.year
"""

_BACKFILLED_PREV_SALE_SQL = """
SELECT count(*), count(prev_sale_price)
FROM production.london_houses
WHERE address_id >= :min_id AND dateoftransfer >= DATE '2024-01-01'
"""

_EARLY_COVERAGE_SQL = """
SELECT count(*) FILTER (WHERE dateoftransfer < DATE '2009-01-01'),
       count(total_floor_area) FILTER (WHERE dateoftransfer < DATE '2009-01-01'),
       count(*) FILTER (WHERE dateoftransfer < DATE '2011-01-01'),
       count(iod_imd_score) FILTER (WHERE dateoftransfer < DATE '2011-01-01'),
       count(*) FILTER (WHERE epc_after_sale),
       count(*) FILTER (WHERE epc_after_sale AND NOT epc_age_at_sale_days < 0),
       count(*) FILTER (WHERE NOT epc_after_sale AND epc_age_at_sale_days < 0)
FROM production.london_houses
"""


def _install_street_key(engine, log) -> None:
    """
    Create ``production.hpldn_street_key`` and check it matches `hpp_rf.normalise_street_key`.

    Every distinct ``address_table`` street is keyed both ways: a model's
    training frame reads the view's key and ``hpp_inference`` may derive it,
    so the two must agree exactly.

    Raises
    ------
    RuntimeError
        Listing up to ten streets the two rules key differently.
    """
    from hpldn.backend.scripts.hpp_rf import normalise_street_key

    with engine.begin() as connection:
        connection.execute(text(STREET_KEY_FUNCTION_SQL))
        rows = connection.execute(text(_STREETS_SQL)).all()
    mismatched = [
        (street, key, normalise_street_key(street))
        for street, key in rows
        if normalise_street_key(street) != key
    ]
    changed = sum(1 for street, key in rows if key != street)
    log.info(
        "0025: production.hpldn_street_key keys %d distinct street(s), %d of them "
        "differently from the street itself; Python and SQL agree on %d",
        len(rows), changed, len(rows) - len(mismatched),
    )
    if mismatched:
        raise RuntimeError(
            f"production.hpldn_street_key and hpp_rf.normalise_street_key disagree on "
            f"{len(mismatched)} street(s), e.g. {mismatched[:10]} (street, SQL, Python)"
        )


def _load_and_link_history(engine, cfg, log) -> int:
    """
    Load the 1995-2023 London sales, link them to UPRNs and insert them into ``staging.ppd_table``.

    0017's sequence over the history window: link (excluding sales already
    held, by postcode, date and price), mint address rows for UPRNs without
    one, insert, then set ``oldnew`` from the Price Paid record.

    Returns
    -------
    int
        Sales inserted.
    """
    csv_fp = require_data_path(
        cfg,
        "ppd_complete_csv_fp",
        "data/pp-complete.csv",
        description="migration 0025 (the 1995-2023 Price Paid history)",
    )
    steps.load_ppd_history(engine, csv_fp, cfg, log)

    tiers = steps.create_ppd_uprn_linkage_table(
        engine, cfg, log,
        window_start=steps.PPD_HISTORY_WINDOW_START,
        window_end=steps.PPD_HISTORY_WINDOW_END,
        target_table=HISTORY_TARGET_TABLE,
        linkage_table=steps.PPD_UPRN_LINKAGE_HISTORY_TABLE,
        exclude_existing_sales=True,
    )
    log.info("0025: history links by tier: %s", tiers)

    steps.mint_address_rows_for_unresolved_uprns(
        engine, cfg, log, linkage_table=steps.PPD_UPRN_LINKAGE_HISTORY_TABLE
    )
    inserted = steps.insert_fused_ppd_rows(
        engine, cfg, log, linkage_table=steps.PPD_UPRN_LINKAGE_HISTORY_TABLE
    )
    with engine.begin() as connection:
        oldnew = connection.execute(text(_OLDNEW_FROM_LINKAGE_SQL)).rowcount
        connection.execute(text("ANALYZE staging.ppd_table"))
    log.info(
        "0025: inserted %d 1995-2023 sale(s) into staging.ppd_table; oldnew set on %d",
        inserted, oldnew,
    )
    return inserted


def _propagate(engine, cfg, log, repairs) -> None:
    """
    Carry the new addresses and sales into production, as 0019 did for the 2024+ back-fill.

    Drops the view, appends rows and syncs streets, rebuilds ``uprn_lookup``,
    checks staging/production parity, gives the new addresses their history
    certificates (with 0008's clean-up) and types every certificate.
    """
    repairs._drop_view(engine, log)
    run_concurrently(
        {
            "0025 append rows + sync poststreet": lambda: repairs._append_and_sync_streets(
                engine, cfg, log
            ),
            "0025 rebuild uprn_lookup": lambda: steps.rebuild_production_uprn_lookup(
                engine, cfg, log
            ),
        },
        log,
    )
    repairs._check_parity(engine, log)
    repairs._backfill_certificates(engine, cfg, log)
    repairs._backfill_property_type(engine, cfg, log)


def _check_history(engine, log) -> None:
    """
    Check the 1995-2023 sales ``staging.ppd_table`` holds against raw Price Paid, year by year.

    Runs before `_propagate` drops the view, so a failed load stops the
    revision with production untouched.

    Raises
    ------
    RuntimeError
        If fewer than `MIN_HISTORY_COMPLETENESS` of the window's raw sales
        are held overall.
    """
    params = {
        "window_start": steps.PPD_HISTORY_WINDOW_START,
        "window_end": steps.PPD_HISTORY_WINDOW_END,
    }
    with engine.connect() as connection:
        rows = connection.execute(text(_HISTORY_COMPLETENESS_SQL), params).all()
    for year, kept, raw in rows:
        log.info("0025: %d -- %d of %d raw London sale(s) held (%.1f%%)", year, kept, raw, 100 * kept / raw)
    kept_total = sum(kept for _, kept, _ in rows)
    raw_total = sum(raw for _, _, raw in rows)
    share = kept_total / raw_total if raw_total else 0.0
    log.info("0025: 1995-2023 -- %d of %d raw sale(s) held (%.1f%%)", kept_total, raw_total, 100 * share)
    if share < MIN_HISTORY_COMPLETENESS:
        raise RuntimeError(
            f"staging.ppd_table holds {share:.1%} of the 1995-2023 raw London "
            f"Price Paid sales, below {MIN_HISTORY_COMPLETENESS:.0%}: the history "
            "load or its linkage failed"
        )


def _check_view(engine, log) -> None:
    """
    Check what this revision set out to change, in the rebuilt view.

    Raises
    ------
    RuntimeError
        If back-filled homes' 2024+ sales still rarely have a previous sale,
        pre-2009 sales lack a floor area, pre-2011 sales lack IoD, or
        ``epc_after_sale`` and the sign of ``epc_age_at_sale_days`` disagree.
    """
    with engine.connect() as connection:
        sales, with_prev = connection.execute(
            text(_BACKFILLED_PREV_SALE_SQL), {"min_id": BACKFILL_ADDRESS_ID_MIN}
        ).one()
        (
            pre_2009, pre_2009_area, pre_2011, pre_2011_iod,
            after_sale, after_sale_bad_sign, before_sale_bad_sign,
        ) = connection.execute(text(_EARLY_COVERAGE_SQL)).one()
    prev_share = with_prev / sales if sales else 0.0
    area_share = pre_2009_area / pre_2009 if pre_2009 else 0.0
    iod_share = pre_2011_iod / pre_2011 if pre_2011 else 0.0
    log.info(
        "0025: back-filled homes' 2024+ sales with a previous sale: %d of %d (%.1f%%; 0.6%% before)",
        with_prev, sales, 100 * prev_share,
    )
    log.info(
        "0025: pre-2009 sales with a floor area: %d of %d (%.1f%%); pre-2011 sales with IoD: "
        "%d of %d (%.1f%%); %d sale(s) take their certificate from after the sale",
        pre_2009_area, pre_2009, 100 * area_share, pre_2011_iod, pre_2011, 100 * iod_share, after_sale,
    )
    failing = []
    if prev_share < MIN_BACKFILLED_PREV_SALE_SHARE:
        failing.append(f"previous-sale share {prev_share:.1%} < {MIN_BACKFILLED_PREV_SALE_SHARE:.0%}")
    if area_share < MIN_PRE_2009_FLOOR_AREA_SHARE:
        failing.append(f"pre-2009 floor-area share {area_share:.1%} < {MIN_PRE_2009_FLOOR_AREA_SHARE:.0%}")
    if iod_share < MIN_PRE_2011_IOD_SHARE:
        failing.append(f"pre-2011 IoD share {iod_share:.1%} < {MIN_PRE_2011_IOD_SHARE:.0%}")
    if after_sale_bad_sign or before_sale_bad_sign:
        failing.append(
            f"{after_sale_bad_sign} epc_after_sale row(s) with a non-negative age, "
            f"{before_sale_bad_sign} other row(s) with a negative one"
        )
    if failing:
        raise RuntimeError("0025: production.london_houses fails its checks: " + "; ".join(failing))


def _rebuild_address_search(engine, cfg, log) -> None:
    """Rebuild ``production.address_search`` from the new view and re-run 0021-0024's checks."""
    first = load_revision("0021")
    widened = load_revision("0022")
    hints = load_revision("0024")
    rows = rebuild_address_search(engine, cfg, log)
    first._check_shape(engine, log)
    hints._check_columns(engine, log, first)
    widened._check_rows(rows, log)
    first._check_covers_addresses_route(engine, log)
    widened._check_sold_homes_without_certificate(engine, log)
    hints._check_hints(engine, log)
    first._check_plan(engine, log)


def upgrade() -> None:
    """Load the history, rebuild the features and the view with the fallbacks, then check."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    gap = load_revision("0017")
    repairs = load_revision("0019")
    features = load_revision("0020")
    view = load_revision("0016")

    _install_street_key(engine, log)

    # The history certificate backfill bounds on staging.epc_table_condensed (UNLOGGED).
    steps.restore_crash_reset_london_staging(engine, cfg, log)
    _load_and_link_history(engine, cfg, log)
    _check_history(engine, log)
    _propagate(engine, cfg, log, repairs)

    log.info("0025: rebuilding every per-sale feature table (0020's builders)")
    features._rebuild_features(engine, cfg, log)

    log.info("0025: rebuilding production.london_houses (certificate fallback, street_key)")
    rebuild(engine, cfg, log, create_sql=CREATE_VIEW_SQL_0025)
    view._check_columns(engine, log, expected_columns=VIEW_COLUMNS_0025)

    features._check_counts(engine, log)
    gap.check_monthly_completeness(
        gap.monthly_completeness(engine, "production", steps.PPD_EPC_FUSION_WINDOW_END),
        gap._add_months(steps.PPD_EPC_FUSION_WINDOW_START, -gap.GUARD_MONTHS_BEFORE_CUTOVER),
        "0025 production.ppd_table",
        log,
    )
    features._check_null_rates(engine, log)
    features._check_recent_maisonette_flag(engine, log)
    steps.verify_epc_ppd_fusion_view_coverage(engine, cfg, log)
    _check_view(engine, log)

    log.info("0025: rebuilding production.address_search from the new view")
    _rebuild_address_search(engine, cfg, log)

    vacuum_analyze(
        engine,
        ("production.ppd_table", "production.address_table", "production.london_houses"),
        log,
    )
    # The linkage stays, as 0017's does: it records which UPRN each sale took.
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {HISTORY_TARGET_TABLE}"))
    log.info(
        "0025: complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  restart the API (its caches read the view)\n"
        "  retrain hpp_cb (the view gained ~history sales, floor areas and IoD)"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
