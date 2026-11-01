"""
The certificate address parser and the unit key of migration 0030 (`migrations.address_parse_sql`).

`unit_key` needs no database; the SQL functions run against hpdbldn inside the
suite's rolled-back transaction (`hpdbldn_rollback`: created in pg_temp, so
nothing persists; marker ``db_write``).
"""

import pytest
from sqlalchemy import text

from hpldn.backend.migrations import address_parse_sql as parse


@pytest.mark.parametrize(
    ("unit", "key"),
    [("Flat 6", "6"), ("APARTMENT 6", "6"), ("6", "6"), ("Flat A", "A"), ("FLAT F", "F"),
     ("Ground Floor Flat", "GROUND FLOOR FLAT"), ("Flat 1/B", "1 B"), ("", None), (None, None), (float("nan"), None)],
)
def test_unit_key(unit, key):
    """ "Flat 6" = "Apartment 6" = "6"; the letter of "FLAT F" survives."""
    assert parse.unit_key(unit) == key


@pytest.fixture(scope="module")
def connection(hpdbldn_rollback):
    """hpdbldn with 0030's parser and unit key created in pg_temp, inside the rolled-back transaction."""
    hpdbldn_rollback.execute(text("SET LOCAL search_path = pg_temp, public"))
    hpdbldn_rollback.execute(text(parse.PARSE_EPC_ADDRESS_SQL.replace("staging.hpldn_parse", "pg_temp.hpldn_parse")))
    hpdbldn_rollback.execute(text(parse.UNIT_KEY_SQL.replace("production.hpldn_unit_key", "pg_temp.hpldn_unit_key")))
    return hpdbldn_rollback


# The fixture's rows: register SAON / PAON / street for each certificate.
PARSED = [
    (("Flat 161 Faunce House", "Doddington Grove", None), ("FLAT 161", "FAUNCE HOUSE", "DODDINGTON GROVE")),
    (("309 City Pavilion", "33 Britton Street", None), ("309", "CITY PAVILION, 33", "BRITTON STREET")),
    (("FLAT 97", "NEW CALEDONIAN WHARF", "6 ODESSA STREET"), ("FLAT 97", "NEW CALEDONIAN WHARF, 6", "ODESSA STREET")),
    (("Apartment 20 Block A", "36, Hornsey Road", None), ("APARTMENT 20", "BLOCK A, 36", "HORNSEY ROAD")),
    (("22 Exmoor House", "Clydesdale Way", None), ("22", "EXMOOR HOUSE", "CLYDESDALE WAY")),
    (("60, Kenilworth Gardens", None, None), (None, "60", "KENILWORTH GARDENS")),
    (("Flat 3 17 Woodville Road", None, None), ("FLAT 3", "17", "WOODVILLE ROAD")),
    (("107 SPA HILL", "UPPER NORWOOD", "LONDON"), (None, "107", "SPA HILL")),
    (("SUNNYSIDE", "GLYN ROAD", "ENFIELD"), (None, "SUNNYSIDE", "GLYN ROAD")),
    (("Garden Flat", "14, Milborne Grove", None), ("GARDEN FLAT", "14", "MILBORNE GROVE")),
    (("211 St. John's Hill", None, None), (None, "211", "ST JOHNS HILL")),
    (("7 Carberry", "Little Strand", None), ("7", "CARBERRY", "LITTLE STRAND")),
    (("77 b", "Shooters Hill Road", None), (None, "77B", "SHOOTERS HILL ROAD")),
    (("Bristol Cottage", "Telegraph Road", None), (None, "BRISTOL COTTAGE", "TELEGRAPH ROAD")),
]


@pytest.mark.parametrize(("lines", "expected"), PARSED)
def test_parse_epc_address(connection, lines, expected):
    """A certificate's lines split into the register's SAON, PAON and street."""
    row = connection.execute(text("SELECT * FROM pg_temp.hpldn_parse_epc_address(:a, :b, :c)"),
                             dict(zip("abc", lines))).one()
    assert tuple(row) == expected


@pytest.mark.parametrize("unit", ["Flat 6", "APARTMENT 6", "Flat F", "Ground Floor Flat", "Flat 1/B", "", None])
def test_sql_unit_key_matches_python(connection, unit):
    """The API keys units in Python, the search index in SQL: they must agree."""
    assert connection.execute(text("SELECT pg_temp.hpldn_unit_key(:u)"), {"u": unit}).scalar() == parse.unit_key(unit)


def test_parser_gate_matches_the_plan():
    """95 % agreement with the register, as the plan's acceptance gate says."""
    assert parse.MIN_PARSER_AGREEMENT == 0.95
