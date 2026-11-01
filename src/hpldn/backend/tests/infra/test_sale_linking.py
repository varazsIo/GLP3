"""
The sale-linking keys and ledger rules of migrations 0027-0029 (`migrations.sale_linking`).

The SQL key functions run against hpdbldn inside the suite's rolled-back
transaction (`hpdbldn_rollback`: the functions are created in it, so nothing
persists; marker ``db_write``). The precision gate and method order need no
database.
"""

import pytest
from sqlalchemy import text

from hpldn.backend.migrations import chain
from hpldn.backend.migrations import sale_linking as sl
from hpldn.backend.scripts import link_precision as lp


@pytest.fixture(scope="module")
def connection(hpdbldn_rollback):
    """hpdbldn with the 0027-0029 key functions created inside the rolled-back transaction."""
    exists = hpdbldn_rollback.execute(text("SELECT to_regprocedure('staging.hpldn_addrkey(text)')")).scalar()
    assert exists is not None, "staging.hpldn_addrkey is not installed (hpdbldn is before 0006)"
    hpdbldn_rollback.execute(text(sl.LINK_FUNCTIONS_SQL))
    return hpdbldn_rollback


def _numsig(connection, address: str) -> str:
    return connection.execute(
        text("SELECT staging.hpldn_numsig_alnum(staging.hpldn_addrkey(:a))"), {"a": address}
    ).scalar_one()


@pytest.mark.parametrize(
    ("ppd", "certificate"),
    [
        ("23A CROMFORD ROAD", "23a, Cromford Road"),
        ("23A CROMFORD ROAD", "23 A Cromford Road"),
        ("FLAT 20 55-59 SAFFRON HILL", "Flat 20, 55 - 59 Saffron Hill"),
        ("FLAT A 12 HIGH STREET", "Flat A, 12 High St"),
    ],
)
def test_numeric_signature_keeps_alphanumeric_numbers_and_ranges(connection, ppd, certificate):
    """The plan's examples: 23A, ranges and lettered flats key alike on both sides."""
    assert _numsig(connection, ppd) == _numsig(connection, certificate) != ""


def test_numeric_signature_tells_lettered_and_plain_numbers_apart(connection):
    """"23A" and "23" are different homes; the old signature keyed both to "23" or none."""
    assert _numsig(connection, "23A CROMFORD ROAD") != _numsig(connection, "23 CROMFORD ROAD")
    assert _numsig(connection, "FLAT 1 12 X ROAD") != _numsig(connection, "12 X ROAD")


def test_building_words_must_all_appear(connection):
    """A numeric match needs every building-name word: Florence House is not Sidney House."""
    sale = connection.execute(
        text("SELECT staging.hpldn_alpha_tokens(staging.hpldn_addrkey('FLAT 1 FLORENCE HOUSE'))")
    ).scalar_one()
    right, wrong = connection.execute(
        text(
            "SELECT string_to_array(staging.hpldn_tokenset(staging.hpldn_addrkey(:r)), ' ') @> :s, "
            "string_to_array(staging.hpldn_tokenset(staging.hpldn_addrkey(:w)), ' ') @> :s"
        ),
        {"r": "Flat 1, Florence House, Gilbert Close", "w": "Flat 1, Sidney House, Gilbert Close", "s": sale},
    ).one()
    assert (right, wrong) == (True, False)


def test_number_sequence_keeps_address_order(connection):
    """Flat 3 of 5 and flat 5 of 3 share a sorted signature but not a sequence."""
    seq = connection.execute(
        text(
            "SELECT staging.hpldn_numseq(staging.hpldn_addrkey('FLAT 3 5 BINA GARDENS')), "
            "staging.hpldn_numseq(staging.hpldn_addrkey('Flat 5, 3 Bina Gardens')), "
            "staging.hpldn_numseq(staging.hpldn_addrkey('Flat 3, 5 Bina Gardens'))"
        )
    ).one()
    assert seq[0] == seq[2] != seq[1]


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [("100C", "Flat C, 100", True), ("E75 1", "Flat E, 75, 1", True), ("6B", "6C", False), ("5", "5A", False)],
)
def test_number_parts_equate_letter_suffix_and_lettered_flat(connection, a, b, equal):
    """"100C" and "Flat C, 100" are one home; "6B" and "6C" are not."""
    parts = connection.execute(
        text(
            "SELECT staging.hpldn_numparts(staging.hpldn_numsig_alnum(staging.hpldn_addrkey(:a))), "
            "staging.hpldn_numparts(staging.hpldn_numsig_alnum(staging.hpldn_addrkey(:b)))"
        ),
        {"a": a, "b": b},
    ).one()
    assert (parts[0] == parts[1]) is equal


@pytest.mark.parametrize(
    ("sale", "flat", "certificate", "same"),
    [
        ("19 MILL HILL ROAD", False, "Ground Floor Flat, 19 Mill Hill Road", False),
        ("23 RONA ROAD", False, "First Floor Front, 23 Rona Road", False),
        ("25 NORTH HYDE LANE", False, "Annexe Rear Of, 25 North Hyde Lane", False),
        ("16 AURELIA ROAD", False, "Land Adjoining 16 Aurelia Road", False),
        ("16 AURELIA ROAD", False, "16 Aurelia Road", True),
        ("FLAT B 94 PARK LANE", True, "94 Park Lane", False),
        ("FLAT B 94 PARK LANE", True, "Ground Floor Flat B, 94 Park Lane", True),
        ("APARTMENT 5 1 HIGH STREET", True, "Flat 5, 1 High Street", True),
        ("THE COACH HOUSE 2 MAIDA AVENUE", False, "Garden Flat, 2 Maida Avenue", False),
        ("6B WILLOUGHBY ROAD", True, "Flat B, 6 Willoughby Road", True),
        ("6B WILLOUGHBY ROAD", False, "Flat B, 6 Willoughby Road", False),
        ("6B WILLOUGHBY ROAD", True, "Annexe, 6B Willoughby Road", False),
        ("47 UPPER TEDDINGTON ROAD", False, "47 Upper Teddington Road", True),
        ("12 LODGE LANE", False, "12 Lodge Lane", True),
        ("12 LODGE LANE", False, "Ground Floor Flat, 12 Lodge Lane", False),
    ],
)
def test_same_unit(connection, sale, flat, certificate, same):
    """A house takes no flat/annexe/land address; a unit takes one naming the same kind of unit."""
    assert connection.execute(
        text("SELECT staging.hpldn_same_unit(staging.hpldn_addrkey(:s), staging.hpldn_addrkey(:c), :f)"),
        {"s": sale, "c": certificate, "f": flat},
    ).scalar_one() is same


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", "12"), ("12a", "12A"), (" 12 A ", "12A"), ("12-14", "12-14"), ("12 - 14", "12-14"),
     ("12;14", None), ("ROSE COTTAGE", None), (None, None)],
)
def test_housenumber(connection, raw, expected):
    """OSM ``addr:housenumber`` and a PPD PAON normalise alike; lists and names are not numbers."""
    assert connection.execute(text("SELECT staging.hpldn_housenumber(:r)"), {"r": raw}).scalar() == expected


def test_method_order_puts_authoritative_evidence_first_and_propagation_last():
    """The ledger keeps the first method in this order."""
    order = sl.METHOD_ORDER
    assert order[:2] == ("hmlr", "ucl")
    assert order[-1] == "group"
    assert order.index("epc_exact") < order.index("epc_numsig") < order.index("osm_polygon")
    assert order.index("osm_polygon") < order.index("osm_polygon_isolated")
    assert order.index("planning_numsig") < order.index("street_elimination") < order.index("term_ucl")
    assert set(sl.TIER_SQL) | set(sl.TERMINATED_TIER_SQL) | {
        "street_elimination", "building_elimination", "group", *sl.SEQUENCE_METHODS,
    } == set(order)
    assert order.index("planning_numsig") < order.index("osm_planning")


def test_l9_runs_after_elimination_and_before_terminated_and_group():
    """0029's flat sequences and house brackets follow L6 and precede L8 and L4."""
    tiers = list(sl.TIER_METHODS)
    assert sl.TIER_METHODS["L9"] == ("flat_sequence", "house_bracket")
    assert tiers.index("L6") < tiers.index("L9") < tiers.index("L8") < tiers.index("L4")


def test_osm_methods_are_flagged_for_odbl():
    """Every OpenStreetMap-derived method is recognisable by name (ODbL share-alike)."""
    osm = [m for m in sl.METHOD_ORDER if "osm" in m]
    assert osm == ["osm_polygon", "osm_point", "osm_polygon_isolated", "osm_planning", "term_osm"]
    assert "osm\\_%" in sl.ODBL_METHOD_PREDICATE and "term_osm" in sl.ODBL_METHOD_PREDICATE


def test_measured_rule_parameters_match_the_plan():
    """10 m isolation, >= 4 flats on one offset, neighbours at most 4 UPRNs apart."""
    assert (sl.ISOLATION_RADIUS_M, sl.FLAT_SEQUENCE_MIN_FLATS, sl.HOUSE_BRACKET_MAX_GAP) == (10, 4, 4)


def test_resolution_methods_follow_the_sale_rules_gates():
    """A UPRN-less certificate is placed only by a rule whose sale-linking method passed."""
    assert sl.RESOLUTION_GATES == {
        "cert_text": "group", "cert_flat_sequence": "flat_sequence", "cert_house_bracket": "house_bracket",
    }


@pytest.mark.parametrize(
    ("saon", "expected"),
    [("3", 3), ("FLAT 3", 3), ("Flat 12", 12), ("APARTMENT 7", 7), ("APT 7", 7),
     ("3A", None), ("FLAT A", None), ("GROUND FLOOR FLAT", None), (None, None)],
)
def test_flat_no(connection, saon, expected):
    """flat_sequence reads only a plain flat number from the SAON."""
    assert connection.execute(text("SELECT staging.hpldn_flat_no(:s)"), {"s": saon}).scalar() == expected


@pytest.mark.parametrize(("paon", "expected"), [("12", 12), (" 7 ", 7), ("12A", None), ("12-14", None), ("ROSE COTTAGE", None)])
def test_house_no(connection, paon, expected):
    """house_bracket brackets only plain house numbers."""
    assert connection.execute(text("SELECT staging.hpldn_house_no(:p)"), {"p": paon}).scalar() == expected


def test_sequence_sql_hides_the_true_uprn_everywhere():
    """The harness's hidden UPRN leaves the known sets, the assigned set and the flat's own number."""
    sql = sl.FLAT_SEQUENCE_SQL
    assert sql.count("{hidden}") == 2 and "k.flat_no <> f.flat_no" in sql
    assert sl.HOUSE_BRACKET_SQL.count("{hidden}") == 3 and "x.hidden" in sl.HOUSE_BRACKET_SQL
    assert "{hide}" in sl.ISOLATION_SQL


def test_gate_thresholds_match_the_plan():
    """99.5 % over at least 2,000 links; HMLR is authoritative."""
    assert (sl.MIN_TIER_PRECISION, sl.MIN_TIER_SAMPLE) == (0.995, 2000)
    assert lp.AUTHORITATIVE_METHODS == ("hmlr",)


def test_0027_to_0034_extend_the_chain():
    """0027 follows 0026, and each of 0028-0034 the one before it; 0034 is CHAIN_HEAD."""
    assert chain.load_revision("0027").down_revision == "0026"
    for revision, previous in (
        ("0028", "0027"), ("0029", "0028"), ("0030", "0029"), ("0031", "0030"), ("0032", "0031"),
        ("0033", "0032"), ("0034", "0033"),
    ):
        assert chain.load_revision(revision).down_revision == previous
    assert chain.CHAIN_HEAD == "0034"


def test_0028_and_0029_share_the_carry():
    """0029 reuses 0028's checks, carry and rebuild (`link_carry`) instead of copying them."""
    for revision in ("0028", "0029"):
        module = chain.load_revision(revision)
        assert module.link_carry.rebuild_production is not None
        assert not hasattr(module, "_propagate") and not hasattr(module, "_check_held")
    assert chain.load_revision("0029").LEDGER_LINKS_BEFORE == 145_449
