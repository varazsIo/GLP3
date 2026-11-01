"""
Back-fill the London sales missing from the months before the fusion cut-over.

``staging.ukhpucl`` ends on 2024-10-31, but the UCL extract was cut before the
Land Registry registrations for its last months arrived: May-October 2024
kept 14-48 % of the raw Price Paid sales, against ~93 % in the fused era.
0006 only fuses sales *after* the ukhpucl end date, so those late
registrations were never loaded, and every "last year" movement whose earlier
12 months overlap the hole is computed from a skewed sample.

This revision runs the 0006 matcher over the months before the cut-over,
starting at the first month whose completeness falls away (and no later than
`GUARD_MONTHS_BEFORE_CUTOVER` months before the fusion window), for PPD sales
ukhpucl does not already hold. The links go to their own table,
``staging.ppd_uprn_linkage_backfill``, because ``verify_epc_ppd_fusion_v2``
requires the main linkage to sit inside the fusion window. Staging only:
0019 propagates to production and 0020 rebuilds the features and the view.

Revision ID: 0017
Revises: 0016
"""

from datetime import date, timedelta

from sqlalchemy import text

from hpldn.backend.migrations.chain import load_revision, no_downgrade
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

BACKFILL_TARGET_TABLE = "staging._ppd_target_backfill"

# A month is incomplete once its kept share of raw PPD sales falls below this
# fraction of the share kept in the first `BASELINE_MONTHS` months.
INCOMPLETE_MONTH_FRACTION = 0.8
BASELINE_MONTHS = 3

# The back-fill always covers at least this many months before the fusion
# window, and afterwards every month from there on must keep at least
# MIN_MONTHLY_COMPLETENESS of raw PPD.
GUARD_MONTHS_BEFORE_CUTOVER = 6
MIN_MONTHLY_COMPLETENESS = 0.60

# Raw London PPD sales per month, filtered as the matcher's target is.
_RAW_MONTHLY_SQL = """
SELECT date_trunc('month', p.dateoftransfer)::date AS month, count(*) AS n
FROM staging.ppd_domestic p
WHERE p.county = 'GREATER LONDON'
  AND p.categorytype = 'A'
  AND p.recordstatus = 'A'
  AND p.propertytype IN ('D','S','T','F')
  AND p.duration IN ('F','L')
  AND p.price > 0
  AND p.dateoftransfer::date <= :window_end
GROUP BY 1
"""

_KEPT_MONTHLY_SQL = """
SELECT date_trunc('month', dateoftransfer)::date AS month, count(*) AS n
FROM {schema}.ppd_table
WHERE dateoftransfer BETWEEN :window_start AND :window_end
GROUP BY 1
"""

_OLDNEW_FROM_LINKAGE_SQL = f"""
UPDATE staging.ppd_table p
SET oldnew = d.oldnew
FROM {steps.PPD_UPRN_LINKAGE_BACKFILL_TABLE} l
JOIN staging.ppd_domestic d ON d.transactionid = l.transactionid
WHERE p.address_id = l.address_id
  AND p.dateoftransfer = l.dateoftransfer
  AND p.price = l.price
  AND p.oldnew IS NULL
  AND d.oldnew IN ('Y', 'N')
"""


def _add_months(month: date, months: int) -> date:
    """Return the first day of the month `months` after `month` (negative for before)."""
    index = month.year * 12 + month.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def monthly_completeness(engine, schema: str, window_end: date) -> dict:
    """
    Return ``{month: (kept, raw)}`` for every month raw PPD covers up to `window_end`.

    Parameters
    ----------
    engine : sqlalchemy.Engine
    schema : str
        ``"staging"`` or ``"production"``: whose ``ppd_table`` to count.
    window_end : datetime.date
        Last sale date counted.
    """
    with engine.connect() as connection:
        raw = dict(
            connection.execute(text(_RAW_MONTHLY_SQL), {"window_end": window_end}).all()
        )
        if not raw:
            return {}
        kept = dict(
            connection.execute(
                text(_KEPT_MONTHLY_SQL.format(schema=schema)),
                {"window_start": min(raw), "window_end": window_end},
            ).all()
        )
    return {month: (kept.get(month, 0), raw[month]) for month in sorted(raw)}


def choose_backfill_start(completeness: dict, cutover: date) -> date:
    """
    Pick the first month to back-fill.

    The first month before `cutover` whose kept share falls below
    `INCOMPLETE_MONTH_FRACTION` of the median share of the first
    `BASELINE_MONTHS` months, clamped to no later than
    `GUARD_MONTHS_BEFORE_CUTOVER` months before `cutover`.

    Parameters
    ----------
    completeness : dict
        ``{month: (kept, raw)}`` from `monthly_completeness`.
    cutover : datetime.date
        First day of the fusion window.

    Returns
    -------
    datetime.date
        First day of the first back-filled month.
    """
    latest_allowed = _add_months(cutover.replace(day=1), -GUARD_MONTHS_BEFORE_CUTOVER)
    before = [(m, k / r) for m, (k, r) in completeness.items() if m < cutover and r]
    if len(before) <= BASELINE_MONTHS:
        return min([m for m, _ in before] + [latest_allowed])
    baseline = sorted(share for _, share in before[:BASELINE_MONTHS])[BASELINE_MONTHS // 2]
    for month, share in before[BASELINE_MONTHS:]:
        if share < INCOMPLETE_MONTH_FRACTION * baseline:
            return min(month, latest_allowed)
    return latest_allowed


def check_monthly_completeness(completeness: dict, first_month: date, label: str, log) -> None:
    """
    Raise if any month from `first_month` keeps less than `MIN_MONTHLY_COMPLETENESS`.

    Raises
    ------
    RuntimeError
        Listing every failing month.
    """
    failing = []
    for month, (kept, raw) in completeness.items():
        if month < first_month or not raw:
            continue
        share = kept / raw
        log.info("%s: %s kept %d of %d raw PPD sale(s) (%.0f%%)", label, month, kept, raw, 100 * share)
        if share < MIN_MONTHLY_COMPLETENESS:
            failing.append(f"{month:%Y-%m} {kept}/{raw} ({share:.0%})")
    if failing:
        raise RuntimeError(
            f"{label}: {len(failing)} month(s) from {first_month:%Y-%m} keep less "
            f"than {MIN_MONTHLY_COMPLETENESS:.0%} of raw London PPD sales: "
            f"{', '.join(failing)}. Every 'last year' movement whose window "
            "overlaps these months is computed from a skewed sample."
        )


def upgrade() -> None:
    """
    Link, mint and insert the pre-cut-over sales ukhpucl is missing, then check every month.

    Notes
    -----
    Re-runnable: the linkage tables are rebuilt, sales already in
    ``staging.ppd_table`` are excluded from the target by (postcode, date,
    price), and the insert is ``ON CONFLICT DO NOTHING``.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    fusion = load_revision("0006")

    cutover = steps.PPD_EPC_FUSION_WINDOW_START
    before = monthly_completeness(engine, "staging", steps.PPD_EPC_FUSION_WINDOW_END)
    if not before:
        raise RuntimeError(
            "staging.ppd_domestic holds no London sales -- 0003 must load it "
            "before the cut-over gap can be measured"
        )
    start = choose_backfill_start(before, cutover)
    end = cutover - timedelta(days=1)
    log.info(
        "0017: back-filling %s..%s (ukhpucl ends %s; fusion starts %s)",
        start, end, end, cutover,
    )

    tiers = steps.create_ppd_uprn_linkage_table(
        engine, cfg, log,
        window_start=start,
        window_end=end,
        target_table=BACKFILL_TARGET_TABLE,
        linkage_table=steps.PPD_UPRN_LINKAGE_BACKFILL_TABLE,
        exclude_existing_sales=True,
    )
    log.info("0017: back-fill links by tier: %s", tiers)

    steps.mint_address_rows_for_unresolved_uprns(
        engine, cfg, log, linkage_table=steps.PPD_UPRN_LINKAGE_BACKFILL_TABLE
    )

    log.info("0017: certificates for the addresses just minted (0006's EPC pick)")
    fusion._rebuild_london_epc_pick(engine, log)
    fusion._insert_certificates(engine, log)

    inserted = steps.insert_fused_ppd_rows(
        engine, cfg, log, linkage_table=steps.PPD_UPRN_LINKAGE_BACKFILL_TABLE
    )
    with engine.begin() as connection:
        oldnew = connection.execute(text(_OLDNEW_FROM_LINKAGE_SQL)).rowcount
        connection.execute(text("ANALYZE staging.ppd_table"))
    log.info(
        "0017: inserted %d back-filled sale(s) into staging.ppd_table; oldnew "
        "set on %d of them from staging.ppd_domestic", inserted, oldnew,
    )

    after = monthly_completeness(engine, "staging", steps.PPD_EPC_FUSION_WINDOW_END)
    check_monthly_completeness(
        after,
        _add_months(cutover, -GUARD_MONTHS_BEFORE_CUTOVER),
        "0017 staging.ppd_table",
        log,
    )


def downgrade() -> None:
    """Refuse; back-filled sales carry no marker distinguishing them from ukhpucl rows."""
    no_downgrade()
