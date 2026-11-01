"""
Measure each sale-linking method's precision against ground truth, and gate it.

Ground truth (``staging.link_gt``):

- **GT1** (``hmlr``): HM Land Registry's PPD->UPRN lookup, for London standard
  sales. Authoritative, but only months of it exist (monthly since 28-08-2026,
  no back-fill), and its older-dated rows are amended records.
- **GT2** (``ucl``): held sales that 0027 attributed to UCL's link, whose
  address UPRN is UCL's (2.6M; a deterministic `GT2_SAMPLE_PER_MILLE` sample).

Each method runs, with the production SQL of `hpldn.backend.migrations.sale_linking`,
on the ground-truth sales as if they were unlinked, and precision is the share
of its valid links (one UPRN, in Open UPRN and in the sale's NSUL postcode)
that name the true UPRN. Evidence is hidden where it would give the answer
away:

- ``hmlr`` is measured on GT2 only and ``ucl`` on GT1 only (each is the other's
  truth). ``hmlr`` is authoritative and never gated off. ``ucl``'s GT1 overlap
  is a few hundred sales, so it is gated on its agreement with ``epc_exact``
  on the real unlinked sales instead (two independent sources naming one UPRN).
- The ``term_*`` methods run on ground-truth sales in live postcodes as if the
  postcode had been terminated (evidence within 250 m of its centroid).
- ``group`` predicts each GT2 sale from the *other* held sales of its address.
- The eliminations hide the sale's own UPRN assignment and its own address:
  a sale is linked iff its (postcode, street) or building would then have one
  unassigned UPRN and one unplaced address; the link is right iff that UPRN
  is the true one (`measure_elimination`).
- ``osm_polygon_isolated`` counts the true UPRN as unaddressed (as the home
  of an unlinked sale is), so an outline next to it is not isolated.
- ``flat_sequence`` and ``house_bracket`` run the production SQL with the true
  UPRN hidden from the assigned UPRNs and from the known flats and houses,
  and the sale's own flat number left out of its building
  (`measure_sequences`).
- The ``epc_*`` methods do not see certificates resolved from a sale's text
  (``cert_text``): a ground-truth sale's own text would give its UPRN away.

A method writes links only if precision >= `MIN_TIER_PRECISION` over at least
`MIN_TIER_SAMPLE` measured links. Ground truth is homes a certificate
describes; the sales left to link mostly are not, so `cross_check` also
switches a method off when, on the real sales, it disagrees with UCL or an
exact certificate-address link more than the gate allows. Results go to
``staging.ppd_link_precision``.

Usage (counts and precision, nothing linked)::

    python -m hpldn.backend.scripts.link_precision
"""

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import Engine, text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations import sale_linking as sl

GT_TABLE = "staging.link_gt"
GT_KEYS_TABLE = "staging.link_gt_keys"
GT_CANDIDATE_TABLE = "staging.link_gt_candidate"

# GT2 sample: sales whose hashtext(transactionid) mod 1000 falls below this.
GT2_SAMPLE_PER_MILLE = 100

# Methods that are authoritative: measured and reported, never gated off.
AUTHORITATIVE_METHODS = ("hmlr",)

# method -> ground truth it is measured on (the other is its own evidence).
_MEASURED_ON = {"hmlr": ("ucl",), "ucl": ("hmlr",)}

_GT_SQL = f"""
CREATE TABLE {GT_TABLE} AS
SELECT h.transactionid, h.uprn AS true_uprn, 'hmlr'::varchar AS source
FROM staging.hmlr_uprn_lookup h
JOIN staging.ppd_domestic p ON p.transactionid = h.transactionid
WHERE {sl.ELIGIBLE_SALE_PREDICATE}
  AND EXISTS (SELECT 1 FROM staging.ukhcs_table uk WHERE uk."UPRN" = h.uprn)
UNION ALL
SELECT p.transactionid, uk."UPRN", 'ucl'
FROM staging.ppd_table p
JOIN staging.address_table a ON a.id = p.address_id
JOIN staging.ukhcs_table uk ON uk.id = a.uid
JOIN staging.ukhpucl u ON u.transactionid = p.transactionid AND u."UPRN" = uk."UPRN"
WHERE p.link_method = 'ucl'
  AND abs(hashtext(p.transactionid)) % 1000 < :per_mille
  AND NOT EXISTS (SELECT 1 FROM staging.hmlr_uprn_lookup h WHERE h.transactionid = p.transactionid)
"""


def build_ground_truth(engine: Engine, cfg=None, log=None, per_mille: int = GT2_SAMPLE_PER_MILLE) -> Dict[str, int]:
    """
    Build `GT_TABLE` and `GT_KEYS_TABLE` (the ground-truth sales keyed like targets).

    Returns
    -------
    dict of str to int
        Ground-truth sales per source.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(sl.LINK_FUNCTIONS_SQL))
        connection.execute(text(f"DROP TABLE IF EXISTS {GT_TABLE}"))
        connection.execute(text(_GT_SQL), {"per_mille": per_mille})
        connection.execute(text(f"CREATE UNIQUE INDEX ON {GT_TABLE} (transactionid)"))
        connection.execute(text(f"ANALYZE {GT_TABLE}"))
        sl.create_sale_keys_table(
            connection,
            GT_KEYS_TABLE,
            f"(SELECT p.*, gt.true_uprn, gt.source FROM staging.ppd_domestic p "
            f"JOIN {GT_TABLE} gt ON gt.transactionid = p.transactionid)",
            extra_columns="s.true_uprn, s.source",
        )
        sl.mark_live_postcodes(connection, GT_KEYS_TABLE)
        rows = connection.execute(
            text(f"SELECT source, count(*) FROM {GT_TABLE} GROUP BY 1")
        ).all()
    counts = {source: n for source, n in rows}
    if log is not None:
        log.info("link_precision: ground truth %s", counts)
    return counts


def _record(connection, method: str, ground_truth: str, n: int, agree: int, log=None,
            passed: Optional[bool] = None) -> bool:
    precision = agree / n if n else None
    if passed is None:
        passed = method in AUTHORITATIVE_METHODS or (
            n >= sl.MIN_TIER_SAMPLE and precision is not None and precision >= sl.MIN_TIER_PRECISION
        )
    connection.execute(text(sl.PRECISION_DDL))
    connection.execute(
        text(
            f"""
            INSERT INTO {sl.PRECISION_TABLE} (method, ground_truth, n, agree, precision, passed)
            VALUES (:method, :gt, :n, :agree, :precision, :passed)
            """
        ),
        {"method": method, "gt": ground_truth, "n": n, "agree": agree,
         "precision": precision, "passed": passed},
    )
    if log is not None:
        log.info(
            "link_precision: %-20s on %-24s n=%7d agree=%7d precision=%s -> %s",
            method, ground_truth, n, agree,
            "n/a" if precision is None else f"{100 * precision:.2f}%",
            "ON" if passed else "OFF",
        )
    return passed


def measure_methods(engine: Engine, methods: Iterable[str], cfg=None, log=None) -> Dict[str, bool]:
    """
    Run `methods` on the ground-truth sales and gate each one.

    Returns
    -------
    dict of str to bool
        Whether each method passed (``ucl`` is gated by `measure_cross` too).
    """
    methods = list(methods)
    passed: Dict[str, bool] = {}
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {GT_CANDIDATE_TABLE}"))
        sl.run_candidate_tiers(connection, GT_KEYS_TABLE, GT_CANDIDATE_TABLE, methods, log,
                               simulate_terminated=True, hide_true_uprn=True)
        for method in methods:
            sources = _MEASURED_ON.get(method, ("hmlr", "ucl"))
            n, agree = _agreement(connection, method, sources)
            passed[method] = _record(connection, method, "+".join(sources), n, agree, log)
    return passed


def _agreement(connection, method: str, sources: Iterable[str]) -> Tuple[int, int]:
    """Valid ground-truth candidates of `method` on `sources`, and how many name the true UPRN."""
    return tuple(
        connection.execute(
            text(
                f"""
                SELECT count(*), count(*) FILTER (WHERE c.uprn = t.true_uprn)
                FROM {GT_CANDIDATE_TABLE} c
                JOIN {GT_KEYS_TABLE} t ON t.transactionid = c.transactionid
                WHERE c.method = :method AND t.source = ANY(:sources)
                  AND {sl.VALID_CANDIDATE_PREDICATE}
                """
            ),
            {"method": method, "sources": list(sources)},
        ).one()
    )


def measure_sequences(engine: Engine, tables: Dict[str, str], cfg=None, log=None) -> Dict[str, bool]:
    """
    Gate ``flat_sequence`` and ``house_bracket`` on ground truth, leave-one-out.

    `tables` are the working tables `sl.elimination_tables` built for the
    real run. Each ground-truth sale's true UPRN is hidden from the assigned
    UPRNs and the known flats/houses (`sl.run_sequence_tiers` with
    ``hide_true_uprn``), and a flat's own number is left out of its
    building, so a building or bracket must predict it from its neighbours.
    """
    results: Dict[str, bool] = {}
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(
            text(sl.CANDIDATE_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS").format(cand=GT_CANDIDATE_TABLE))
        )
        connection.execute(
            text(f"DELETE FROM {GT_CANDIDATE_TABLE} WHERE method = ANY(:methods)"),
            {"methods": list(sl.SEQUENCE_METHODS)},
        )
        sl.run_sequence_tiers(connection, GT_KEYS_TABLE, GT_CANDIDATE_TABLE, tables, log, hide_true_uprn=True)
        for method in sl.SEQUENCE_METHODS:
            n, agree = _agreement(connection, method, ("hmlr", "ucl"))
            results[method] = _record(connection, method, "hmlr+ucl:hidden", n, agree, log)
    return results


# The method's links as the ledger would take them, against the reference's
# UPRN before the duplicate check (which keeps the UPRN and only marks it
# ambiguous): otherwise a method whose UPRN carries the sale's address makes
# every disagreeing reference "duplicate", and the check reads 100 %.
_CROSS_SQL = f"""
SELECT count(*), count(*) FILTER (WHERE a.uprn = b.uprn)
FROM {sl.CANDIDATE_TABLE} a
JOIN {sl.CANDIDATE_TABLE} b ON b.transactionid = a.transactionid AND b.method = :reference
JOIN {sl.TARGET_TABLE} t ON t.transactionid = a.transactionid
WHERE a.method = :method
  AND {sl.VALID_CANDIDATE_PREDICATE.replace('c.', 'a.')}
  AND {sl.VALID_CANDIDATE_PREDICATE.replace('c.', 'b.').replace(
      'b.n_uprns = 1', "(b.n_uprns = 1 OR b.evidence LIKE '% duplicate:%')")}
"""

# Independent methods every other method is cross-checked against on the real
# targets, where both name one UPRN (see `cross_check`).
CROSS_REFERENCES = ("ucl", "epc_exact")
CROSS_MIN_OVERLAP = 500


def _wilson_upper(agree: int, n: int, z: float = 1.96) -> float:
    """Upper end of the 95 % Wilson interval of agree/n."""
    if not n:
        return 1.0
    p = agree / n
    centre = p + z * z / (2 * n)
    spread = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (centre + spread) / (1 + z * z / n)


def measure_cross(engine: Engine, method: str, reference: str, cfg=None, log=None) -> bool:
    """
    Gate `method` on its agreement with `reference` over the real targets both link.

    For ``ucl``, whose ground truth is too small: it passes as any method does
    (>= `MIN_TIER_PRECISION` over >= `MIN_TIER_SAMPLE`). Reads
    `sl.CANDIDATE_TABLE`, so run the two methods' candidates first.
    """
    with engine.begin() as connection:
        n, agree = connection.execute(
            text(_CROSS_SQL), {"method": method, "reference": reference}
        ).one()
        return _record(connection, method, f"cross:{reference}", n, agree, log)


def cross_check(engine: Engine, gated: Dict[str, bool], methods: Iterable[str], cfg=None, log=None) -> Dict[str, bool]:
    """
    Switch off a method whose links disagree with an independent reference on the real targets.

    The ground-truth sales are homes UCL linked to a certificate; the unlinked
    sales mostly are not, so a method can measure 99.7 % there and still be
    worse on the sales it is meant for. Where a method and a reference in
    `CROSS_REFERENCES` (itself switched on) both name one UPRN for at least
    `CROSS_MIN_OVERLAP` real sales, the method stays on only if the upper
    95 % bound of their agreement reaches `MIN_TIER_PRECISION`: a pair that
    disagrees more than that has at least one member below the gate, and
    the references have passed it. Updates and returns `gated`.
    """
    with engine.begin() as connection:
        for method in methods:
            if not gated.get(method) or method in AUTHORITATIVE_METHODS:
                continue
            for reference in CROSS_REFERENCES:
                if reference == method or not gated.get(reference):
                    continue
                n, agree = connection.execute(
                    text(_CROSS_SQL), {"method": method, "reference": reference}
                ).one()
                if n < CROSS_MIN_OVERLAP:
                    continue
                upper = _wilson_upper(agree, n)
                passed = upper >= sl.MIN_TIER_PRECISION
                _record(connection, method, f"cross:{reference}", n, agree, log, passed=passed)
                if not passed:
                    gated[method] = False
    return gated


def measure_group(engine: Engine, groups: str, cfg=None, log=None) -> bool:
    """
    Gate ``group`` (L4): predict each GT2 sale from the other held sales of its address.

    The prediction is the one UPRN the group's other held sales have; groups
    with a new-build sale after an earlier sale are skipped, as the tier does.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        n, agree = connection.execute(
            text(
                f"""
                WITH held AS ({sl._HELD_UPRNS_SQL}),
                gt AS (
                    SELECT g.pc, g.k_exact, t.transactionid, t.true_uprn
                    FROM {GT_KEYS_TABLE} t
                    JOIN {groups} g ON g.transactionid = t.transactionid
                    WHERE t.source = 'ucl'
                ),
                pred AS (
                    SELECT gt.transactionid, gt.true_uprn, min(h.uprn) AS uprn
                    FROM gt
                    JOIN {groups} o ON o.pc = gt.pc AND o.k_exact = gt.k_exact
                                   AND o.transactionid <> gt.transactionid
                    JOIN held h ON h.transactionid = o.transactionid
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {groups} nb
                        JOIN {groups} earlier
                          ON earlier.pc = nb.pc AND earlier.k_exact = nb.k_exact
                         AND earlier.dateoftransfer < nb.dateoftransfer
                        WHERE nb.pc = gt.pc AND nb.k_exact = gt.k_exact AND nb.oldnew = 'Y'
                    )
                    GROUP BY gt.transactionid, gt.true_uprn
                    HAVING count(DISTINCT h.uprn) = 1
                )
                SELECT count(*), count(*) FILTER (WHERE uprn = true_uprn) FROM pred
                """
            )
        ).one()
        return _record(connection, "group", "ucl:other-sales", n, agree, log)


def measure_elimination(engine: Engine, tables: Dict[str, str], cfg=None, log=None) -> Dict[str, bool]:
    """
    Gate the eliminations by hiding each ground-truth sale's own evidence.

    `tables` are the working tables `sl.elimination_tables` built for the
    real pending sales. For a ground-truth sale with true UPRN u: if u is in
    the candidate set (NSUL postcode, a USRN of the sale's street name, or the
    building's TOID), hiding it leaves the free set plus u, so the rule fires
    iff nothing else is free, and is right; if u is not in the candidate set
    the rule fires iff exactly one other UPRN is free, and is wrong. Either
    way it also needs no other unplaced address there.
    """
    results: Dict[str, bool] = {}
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        n, agree = connection.execute(
            text(
                f"""
                WITH gt AS (
                    SELECT t.transactionid, t.pc, t.street_norm, t.k_numsig_alnum, t.true_uprn,
                           EXISTS (
                               SELECT 1 FROM staging.nsul_london n
                               JOIN staging.uprn_usrn uu ON uu.uprn = n.uprn
                               JOIN staging.usrn_street us ON us.usrn = uu.usrn AND us.street_norm = t.street_norm
                               WHERE n.uprn = t.true_uprn AND n.postcode_norm = t.pc
                           ) AS in_candidates,
                           (SELECT count(*) FROM {tables['free']} f
                            WHERE f.pc = t.pc AND f.street_norm = t.street_norm) AS free,
                           (SELECT count(*) FROM {tables['unplaced']} u
                            WHERE u.pc = t.pc AND u.street_norm = t.street_norm
                              AND u.address_key <> t.k_numsig_alnum) AS other_unplaced
                    FROM {GT_KEYS_TABLE} t
                    WHERE t.is_house AND t.street_norm IS NOT NULL AND t.k_numsig_alnum <> ''
                )
                SELECT count(*) FILTER (WHERE other_unplaced = 0 AND ((in_candidates AND free = 0) OR (NOT in_candidates AND free = 1))),
                       count(*) FILTER (WHERE other_unplaced = 0 AND in_candidates AND free = 0)
                FROM gt
                """
            )
        ).one()
        results["street_elimination"] = _record(connection, "street_elimination", "hmlr+ucl:hidden", n, agree, log)

        n, agree = connection.execute(
            text(
                f"""
                WITH gt AS (
                    SELECT t.transactionid, t.pc, t.k_exact, t.true_uprn, b.toid,
                           EXISTS (
                               SELECT 1 FROM staging.uprn_toid ut
                               JOIN staging.nsul_london n ON n.uprn = ut.uprn
                               WHERE ut.uprn = t.true_uprn AND ut.toid = b.toid AND n.postcode_norm = t.pc
                           ) AS in_candidates,
                           (SELECT count(*) FROM staging.uprn_toid ut
                            JOIN staging.nsul_london n ON n.uprn = ut.uprn
                            WHERE ut.toid = b.toid AND n.postcode_norm = t.pc
                              AND ut.uprn <> t.true_uprn
                              AND NOT EXISTS (SELECT 1 FROM {tables['assigned']} a WHERE a.uprn = ut.uprn)) AS free,
                           (SELECT count(DISTINCT p.k_exact) FROM {sl.TARGET_TABLE} p
                            WHERE p.pc = t.pc AND p.paon_norm = t.paon_norm AND p.street_norm = t.street_norm
                              AND NOT p.is_house AND p.k_exact <> t.k_exact
                              AND NOT EXISTS (SELECT 1 FROM {sl.LEDGER_TABLE} l WHERE l.transactionid = p.transactionid)
                           ) AS other_unplaced
                    FROM {GT_KEYS_TABLE} t
                    JOIN {tables['buildings']} b
                      ON b.pc = t.pc AND b.paon_norm = t.paon_norm AND b.street_norm = t.street_norm
                    WHERE NOT t.is_house AND t.k_exact <> ''
                )
                SELECT count(*) FILTER (WHERE other_unplaced = 0 AND ((in_candidates AND free = 0) OR (NOT in_candidates AND free = 1))),
                       count(*) FILTER (WHERE other_unplaced = 0 AND in_candidates AND free = 0)
                FROM gt
                """
            )
        ).one()
        results["building_elimination"] = _record(connection, "building_elimination", "hmlr+ucl:hidden", n, agree, log)
    return results


def main() -> None:
    """
    Resolve UPRN-less certificates, then run `sl.link_sales` (ground truth, precision, every tier) into the ledger.

    Nothing reaches ``ppd_table``: only the migrations (0028, 0029) carry the
    ledger across. Needs 0027 and 0028's source loads. Prints each method's
    precision rows from this run and the links it would make.
    """
    import sys

    from sqlalchemy import create_engine

    from hpldn.backend.common.config import get_cfg
    from hpldn.backend.common.db import get_migration_db_url

    log = logging.getLogger("link_precision")
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler(sys.stdout))
    engine = create_engine(get_migration_db_url(log))
    cfg = get_cfg()
    with engine.connect() as connection:
        started = connection.execute(text("SELECT now()")).scalar_one()
    sl.refresh_certificate_gazetteer(engine, cfg, log)
    result = sl.link_sales(engine, cfg, log)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT method, ground_truth, n, agree, precision, passed FROM {sl.PRECISION_TABLE} "
                f"WHERE measured_at >= :started ORDER BY method, measured_at"
            ),
            {"started": started},
        ).all()
    for method, ground_truth, n, agree, precision, passed in rows:
        print(f"{method:22} {ground_truth:24} n={n:8,} agree={agree:8,} "
              f"{'n/a' if precision is None else f'{100 * precision:.2f}%':>8} {'ON' if passed else 'OFF'}")
    print(result)


if __name__ == "__main__":
    main()
