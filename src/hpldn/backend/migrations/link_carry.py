"""
Carry a sale-linking run into staging and production (migrations 0028 and 0029).

`hpldn.backend.migrations.sale_linking.link_sales` writes the ledger
(``staging.ppd_uprn_link``); a revision then checks it, carries it into
``staging.ppd_table``, gives the newly sold addresses a street and a parcel,
propagates everything to production and rebuilds the per-sale features,
``production.london_houses`` and ``production.address_search``. The steps
and their checks are the same for every revision that links sales, so they
live here; each revision passes its own label and guard values.
"""

from typing import Dict

from sqlalchemy import text

from hpldn.backend.migrations import sale_linking as sl
from hpldn.backend.migrations.address_search import rebuild as rebuild_address_search
from hpldn.backend.migrations.chain import load_revision, vacuum_analyze
from hpldn.backend.migrations.london_houses_view import (
    CREATE_VIEW_SQL_0025,
    VIEW_COLUMNS_0025,
    rebuild,
)
from hpldn.backend.scripts import pipeline_steps as steps

ELIGIBLE_BY_YEAR_SQL = f"""
WITH raw AS (
    SELECT extract(year FROM p.dateoftransfer)::int AS year, count(*) AS n
    FROM staging.ppd_domestic p
    WHERE {sl.ELIGIBLE_SALE_PREDICATE}
    GROUP BY 1
), held AS (
    SELECT extract(year FROM p.dateoftransfer)::int AS year, count(*) AS n
    FROM staging.ppd_domestic p
    WHERE {sl.ELIGIBLE_SALE_PREDICATE}
      AND EXISTS (SELECT 1 FROM staging.ppd_table h WHERE h.transactionid = p.transactionid)
    GROUP BY 1
)
SELECT raw.year, coalesce(held.n, 0), raw.n FROM raw LEFT JOIN held USING (year) ORDER BY 1
"""

# Every ledger link: one UPRN in Open UPRN, and (terminated postcodes aside)
# in the sale's NSUL postcode.
LEDGER_GUARD_SQL = f"""
SELECT count(*) FILTER (WHERE uk.id IS NULL),
       count(*) FILTER (WHERE l.method NOT LIKE 'term\\_%'
                        AND n.postcode_norm IS DISTINCT FROM staging.hpldn_pcnorm(d.postcode))
FROM {sl.LEDGER_TABLE} l
JOIN staging.ppd_domestic d ON d.transactionid = l.transactionid
LEFT JOIN staging.ukhcs_table uk ON uk."UPRN" = l.uprn
LEFT JOIN staging.nsul_london n ON n.uprn = l.uprn
"""

# Address groups (postcode + normalised address) whose linked or held sales
# name more than one UPRN, among groups the ledger touched.
GROUP_GUARD_SQL = f"""
WITH links AS (
    SELECT transactionid, uprn FROM {sl.LEDGER_TABLE}
    UNION ALL
    {sl._HELD_UPRNS_SQL}
)
SELECT count(*) FROM (
    SELECT g.pc, g.k_exact
    FROM staging._link_groups g
    JOIN links u ON u.transactionid = g.transactionid
    GROUP BY 1, 2
    HAVING count(DISTINCT u.uprn) > 1
       AND bool_or(g.transactionid IN (SELECT transactionid FROM {sl.LEDGER_TABLE}))
) x
"""

# OpenStreetMap-derived ledger rows missing from the ODbL table.
ODBL_GUARD_SQL = f"""
SELECT count(*)
FROM {sl.LEDGER_TABLE} l
WHERE {sl.ODBL_METHOD_PREDICATE}
  AND NOT EXISTS (
      SELECT 1 FROM {sl.ODBL_TABLE} o
      WHERE o.transactionid = l.transactionid AND o.uprn = l.uprn AND o.method = l.method
  )
"""

# Held links against HMLR's own lookup (0027's audit), overall.
HMLR_AGREEMENT_SQL = """
SELECT count(*), count(*) FILTER (WHERE uk."UPRN" = h.uprn)
FROM staging.ppd_table p
JOIN staging.hmlr_uprn_lookup h ON h.transactionid = p.transactionid
JOIN staging.address_table a ON a.id = p.address_id
JOIN staging.ukhcs_table uk ON uk.id = a.uid
"""


def check_ledger(engine, result: Dict[str, object], log, label: str, min_links: int) -> None:
    """
    Check the ledger before anything is carried.

    `min_links` bounds every ledger row (a re-run after a failure further
    down keeps the links an earlier run already carried, and links only what
    is left); only this run's (uncarried) links must come from methods the
    run switched on.

    Raises
    ------
    RuntimeError
        If a link names a UPRN outside Open UPRN or the sale's NSUL postcode,
        an address group maps to several UPRNs, a gated-off method linked, an
        OpenStreetMap link is missing from the ODbL table, or the ledger
        holds fewer than `min_links` links.
    """
    with engine.connect() as connection:
        not_open, wrong_postcode = connection.execute(text(LEDGER_GUARD_SQL)).one()
        split_groups = connection.execute(text(GROUP_GUARD_SQL)).scalar_one()
        methods = connection.execute(
            text(
                f"""
                SELECT DISTINCT method FROM {sl.LEDGER_TABLE} l
                WHERE NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.transactionid = l.transactionid)
                """
            )
        ).scalars().all()
        links = connection.execute(text(f"SELECT count(*) FROM {sl.LEDGER_TABLE}")).scalar_one()
        odbl_missing = connection.execute(text(ODBL_GUARD_SQL)).scalar_one()
    log.info(
        "%s: ledger holds %d link(s), %d of them new in this run: %s; %d outside Open UPRN, "
        "%d outside the sale's NSUL postcode, %d address group(s) on several UPRNs, "
        "%d OpenStreetMap link(s) missing from %s",
        label, links, sum(result["links"].values()), result["links"], not_open, wrong_postcode,
        split_groups, odbl_missing, sl.ODBL_TABLE,
    )
    failing = []
    if not_open or wrong_postcode:
        failing.append(f"{not_open} link(s) outside Open UPRN, {wrong_postcode} outside the NSUL postcode")
    if split_groups:
        failing.append(f"{split_groups} address group(s) map to several UPRNs")
    off = sorted(set(methods) - set(result["enabled"]))
    if off:
        failing.append(f"gated-off method(s) linked: {off}")
    if odbl_missing:
        failing.append(f"{odbl_missing} OpenStreetMap link(s) missing from {sl.ODBL_TABLE}")
    if links < min_links:
        failing.append(f"{links} ledger link(s) < {min_links}")
    if failing:
        raise RuntimeError(f"{label}: the ledger fails its checks: " + "; ".join(failing))


def check_held(engine, log, label: str, min_held_share: float, before: str) -> float:
    """
    Log the held share per year and check it overall.

    Returns
    -------
    float
        Share of the raw London standard sales of 1995-2026 held.

    Raises
    ------
    RuntimeError
        If fewer than `min_held_share` of them are held, or a transactionid
        is held twice.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(ELIGIBLE_BY_YEAR_SQL)).all()
        unique_tx = connection.execute(
            text(
                "SELECT count(*) = count(DISTINCT transactionid) FROM staging.ppd_table "
                "WHERE transactionid IS NOT NULL"
            )
        ).scalar_one()
    for year, held, raw in rows:
        log.info("%s: %d -- %d of %d raw London sale(s) held by transactionid (%.1f%%)",
                 label, year, held, raw, 100 * held / raw)
    held = sum(h for _, h, _ in rows)
    raw = sum(r for _, _, r in rows)
    share = held / raw if raw else 0.0
    log.info("%s: 1995-2026 -- %d of %d raw sale(s) held (%.2f%%; %s before)", label, held, raw, 100 * share, before)
    if not unique_tx:
        raise RuntimeError(f"{label}: staging.ppd_table.transactionid is not unique")
    if share < min_held_share:
        raise RuntimeError(f"{label}: {share:.1%} of raw London sales held, below {min_held_share:.1%}")
    return share


def check_hmlr_agreement(engine, log, label: str, min_agreement: float) -> float:
    """
    Audit every held link against HMLR's own lookup, as 0027 did.

    Raises
    ------
    RuntimeError
        If fewer than `min_agreement` of them agree.
    """
    with engine.connect() as connection:
        n, agree = connection.execute(text(HMLR_AGREEMENT_SQL)).one()
    share = agree / n if n else 1.0
    log.info("%s: HMLR agrees with %d of %d held link(s) (%.2f%%)", label, agree, n, 100 * share)
    if share < min_agreement:
        raise RuntimeError(f"{label}: held links agree with HMLR on {share:.2%} (want >= {min_agreement:.1%})")
    return share


# Newest sale of each sold address with no street, from ppd_table (which
# holds the ledger's sales; 0018's own source reads the linkage tables).
_POSTSTREET_FROM_SALES_BUILD = "staging._poststreet_from_sales_build"
_POSTSTREET_FROM_SALES_BUILD_SQL = f"""
CREATE UNLOGGED TABLE {_POSTSTREET_FROM_SALES_BUILD} AS
SELECT DISTINCT ON (p.address_id)
       p.address_id,
       upper(btrim(d.street)) AS poststreet
FROM staging.ppd_table p
JOIN staging.ppd_domestic d ON d.transactionid = p.transactionid
JOIN staging.address_table a ON a.id = p.address_id
WHERE a.poststreet IS NULL
  AND NULLIF(btrim(d.street), '') IS NOT NULL
ORDER BY p.address_id, p.dateoftransfer DESC, p.transactionid DESC
"""


def refresh_newly_sold_addresses(engine, cfg, log) -> None:
    """
    Give the addresses the carry made sold a street and a parcel, as 0018 did for fusion.

    About 45K UPRNs 0028 linked already had an address row, many minted by
    0018 from a certificate with no street; 0018 filled streets and parcels
    only for addresses sold at the time. Without this, 2024's sales break
    0020's NULL-rate ceilings (8.6 % without a street, 12.1 % without a lot
    area).
    """
    refresh = load_revision("0018")
    refresh._fill_poststreet(
        engine, cfg, log, _POSTSTREET_FROM_SALES_BUILD, _POSTSTREET_FROM_SALES_BUILD_SQL,
        "the newest held sale's Price Paid street",
    )
    refresh._fill_poststreet(
        engine, cfg, log, refresh._POSTSTREET_FROM_EPC_BUILD, refresh._POSTSTREET_FROM_EPC_BUILD_SQL,
        "EPC certificates' second address line",
    )
    refresh._insert_missing_parcels(engine, cfg, log)


def propagate(engine, cfg, log) -> None:
    """
    Carry the new addresses, sales, streets and parcels into production.

    0019's propagation (view dropped; rows appended and streets synced;
    ``london_parcels`` re-promoted; ``uprn_lookup`` rebuilt), then 0025's
    parity check and certificate back-fills. 0025's own propagation does not
    re-promote parcels, which the new sales need.
    """
    repairs = load_revision("0019")
    repairs._propagate(engine, cfg, log)
    repairs._check_parity(engine, log)
    repairs._backfill_certificates(engine, cfg, log)
    repairs._backfill_property_type(engine, cfg, log)


# 0024's sample, minus the homes address_search deliberately gives no hint:
# a number that already names the street ("PAGES EAST HILL STORES, 105" in
# an EAST HILL postcode), which its search_text finds by the street anyway.
HINT_SAMPLE_SQL = text(
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
        HAVING count(DISTINCT production.hpldn_street_key(n.poststreet)) = 1
           AND position(
               min(production.hpldn_searchnorm(production.hpldn_street_key(n.poststreet)))
               IN production.hpldn_searchnorm(b.postnumber)
           ) = 0
        ORDER BY b.address_id
        LIMIT 500
    ) sample
    """
)


def rebuild_address_search_checked(engine, cfg, log, label: str, min_street_hints: int) -> int:
    """
    Rebuild ``production.address_search`` and re-run 0021-0024's checks, as 0025 does.

    0024's hint count is replaced by `min_street_hints`; its sample (each
    street-less home of a single-street postcode is found by its street)
    still has to pass in full, less the homes whose number names the street
    (`HINT_SAMPLE_SQL`).

    Returns
    -------
    int
        Rows built.
    """
    first = load_revision("0021")
    widened = load_revision("0022")
    hints = load_revision("0024")
    rows = rebuild_address_search(engine, cfg, log)
    first._check_shape(engine, log)
    hints._check_columns(engine, log, first)
    widened._check_rows(rows, log)
    first._check_covers_addresses_route(engine, log)
    widened._check_sold_homes_without_certificate(engine, log)
    with engine.connect() as connection:
        hinted = connection.execute(hints._HINTED_SQL).scalar_one()
        sampled, missing = connection.execute(HINT_SAMPLE_SQL).one()
        streetless = connection.execute(
            text("SELECT count(*) FROM production.address_search WHERE poststreet IS NULL")
        ).scalar_one()
    log.info(
        "%s: %d street-less home(s) in address_search, %d with a street hint; %d of %d sampled lack it",
        label, streetless, hinted, missing, sampled,
    )
    if hinted < min_street_hints or sampled == 0 or missing:
        raise AssertionError(
            f"{label}: {hinted:,} street hint(s) (want >= {min_street_hints:,}); "
            f"{missing} of {sampled} sampled street-less homes lack their street"
        )
    first._check_plan(engine, log)
    return rows


def rebuild_production(
    engine,
    cfg,
    log,
    label: str,
    min_street_hints: int,
    create_sql: str = CREATE_VIEW_SQL_0025,
    view_columns=VIEW_COLUMNS_0025,
) -> None:
    """
    Propagate the carried sales to production and rebuild everything downstream, with 0020/0025's checks.

    Needs the ledger carried (`sale_linking.carry_links_into_staging`) and
    `refresh_newly_sold_addresses` run. The view is dropped while it is
    rebuilt: stop the API first, or restart it after.

    The view is 0025's definition (what 0028 and 0029 built); a revision
    after 0034 passes ``london_houses_view.CREATE_VIEW_SQL`` and
    ``VIEW_COLUMNS``, and rebuilds ``production.ofsted_features`` (0034's
    builder) first, so new sales get their school features.
    """
    history = load_revision("0025")
    features = load_revision("0020")
    view = load_revision("0016")
    gap = load_revision("0017")

    propagate(engine, cfg, log)
    log.info("%s: rebuilding every per-sale feature table (0020's builders)", label)
    features._rebuild_features(engine, cfg, log)
    log.info("%s: rebuilding production.london_houses", label)
    rebuild(engine, cfg, log, create_sql=create_sql)
    view._check_columns(engine, log, expected_columns=view_columns)

    features._check_counts(engine, log)
    gap.check_monthly_completeness(
        gap.monthly_completeness(engine, "production", steps.PPD_EPC_FUSION_WINDOW_END),
        gap._add_months(steps.PPD_EPC_FUSION_WINDOW_START, -gap.GUARD_MONTHS_BEFORE_CUTOVER),
        f"{label} production.ppd_table",
        log,
    )
    features._check_null_rates(engine, log)
    features._check_recent_maisonette_flag(engine, log)
    steps.verify_epc_ppd_fusion_view_coverage(engine, cfg, log)
    history._check_view(engine, log)

    log.info("%s: rebuilding production.address_search from the new view", label)
    rebuild_address_search_checked(engine, cfg, log, label, min_street_hints)

    vacuum_analyze(
        engine,
        ("production.ppd_table", "production.address_table", "production.london_houses"),
        log,
    )
