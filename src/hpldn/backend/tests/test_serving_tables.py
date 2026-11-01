"""
Drift guard: the serving dump must hold every table the runtime reads.

`export_serving_db.SERVING_TABLES` lists the ``production`` tables shipped
to the public host. A query added to the API (or to the pricing code it
calls) against a table missing from that list would pass every local test,
because the local database has all of them, and fail only in production.
These tests read the source, not the database, so they run anywhere.
"""

import re
from pathlib import Path

import pytest

from hpldn.backend.models import production as production_models
from hpldn.backend.scripts.export_serving_db import (
    SERVING_COLUMNS,
    SERVING_FUNCTIONS,
    SERVING_TABLES,
)

BACKEND = Path(__file__).resolve().parents[1]

# What runs in the API process: the API package and the pricing modules it
# imports at serve time (hpp_inference, and the loaders it calls in hpp_cb,
# hpp_rf, local_index and hpi_projection).
RUNTIME_SOURCES = sorted((BACKEND / "api").rglob("*.py")) + [
    BACKEND / "scripts" / name
    for name in (
        "hpp_inference.py",
        "local_index.py",
        "hpi_projection.py",
        "hpp_cb.py",
        "hpp_rf.py",
    )
]

# Named in the runtime sources but deliberately not shipped, with the reason.
NOT_SERVED = {
    "uprn_lookup": "read only by /properties (via the ORM), which nginx never proxies",
    "iod_features": "hpp_cb's training CLI help; its columns reach the API inside london_houses",
    "lsoa_price_trend_features": "a docstring in hpp_inference; the served feature is computed from london_houses",
    "ofsted_features": (
        "training-time features: their columns reach the API inside london_houses, and pricing "
        "recomputes them from ofsted_rating_spells (hpp_inference.EvidenceStore.ofsted_features)"
    ),
}

# `production.x`, but not `models.production.X` module paths.
_TABLE_REF = re.compile(r"(?<![\w.])production\.([a-z_][a-z0-9_]*)")
_ORM_IMPORT = re.compile(r"from hpldn\.backend\.models\.production import ([^\n(]+|\([^)]*\))")
_SQL_KEYWORDS = {
    "where", "join", "left", "right", "inner", "outer", "cross", "on", "using",
    "group", "order", "limit", "union", "as", "natural", "full", "lateral",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def referenced_tables() -> dict:
    """Return ``{name: [files]}`` for every ``production.<name>`` in the runtime sources."""
    found = {}
    for path in RUNTIME_SOURCES:
        for name in set(_TABLE_REF.findall(_read(path))):
            found.setdefault(name, []).append(str(path.relative_to(BACKEND)))
    return found


def orm_tables() -> dict:
    """Return ``{table: [files]}`` for production ORM models imported by the runtime sources."""
    found = {}
    for path in RUNTIME_SOURCES:
        for group in _ORM_IMPORT.findall(_read(path)):
            for name in re.findall(r"\w+", group):
                table = getattr(getattr(production_models, name, None), "__tablename__", None)
                if table:
                    found.setdefault(table, []).append(str(path.relative_to(BACKEND)))
    return found


def test_runtime_sources_exist():
    missing = [str(p) for p in RUNTIME_SOURCES if not p.is_file()]
    assert not missing, f"runtime source files moved: {missing}"


def test_serving_lists_are_consistent():
    assert len(SERVING_TABLES) == len(set(SERVING_TABLES))
    assert not set(SERVING_TABLES) & set(NOT_SERVED)
    assert set(SERVING_COLUMNS) <= set(SERVING_TABLES)


def test_every_referenced_table_is_served():
    refs = referenced_tables()
    unknown = {
        name: files
        for name, files in refs.items()
        if name not in SERVING_TABLES and name not in NOT_SERVED and name not in SERVING_FUNCTIONS
    }
    assert not unknown, (
        "The runtime reads production tables the serving dump does not ship. Add them to "
        "export_serving_db.SERVING_TABLES, or to NOT_SERVED here with the reason: "
        f"{unknown}"
    )


def test_every_orm_table_is_served_or_excused():
    unknown = {
        table: files
        for table, files in orm_tables().items()
        if table not in SERVING_TABLES and table not in NOT_SERVED
    }
    assert not unknown, f"ORM models of unshipped tables are used at runtime: {unknown}"


def test_not_served_entries_are_still_referenced():
    # A stale excuse would hide the day that table is read for real.
    seen = set(referenced_tables()) | set(orm_tables())
    stale = sorted(set(NOT_SERVED) - seen)
    assert not stale, f"NOT_SERVED names tables the runtime no longer mentions: {stale}"


def test_serving_functions_are_the_ones_called():
    refs = referenced_tables()
    for name in SERVING_FUNCTIONS:
        assert name.startswith("hpldn_")
    called = {name for name in refs if name.startswith("hpldn_")}
    assert called <= set(SERVING_FUNCTIONS), f"unshipped production functions called: {called}"


@pytest.mark.parametrize("table", sorted(SERVING_COLUMNS))
def test_pruned_table_columns_cover_the_runtime_sql(table):
    """Every ``alias.column`` read from a pruned table must be a kept column."""
    kept = set(SERVING_COLUMNS[table])
    ref = re.compile(rf"(?<![\w.])production\.{table}\b(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.IGNORECASE)
    problems = []
    for path in RUNTIME_SOURCES:
        text = _read(path)
        for match in ref.finditer(text):
            alias = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            where = f"{path.relative_to(BACKEND)}:{line}"
            if not alias or alias.lower() in _SQL_KEYWORDS:
                problems.append(f"{where}: production.{table} without an alias, columns cannot be checked")
                continue
            if re.search(rf"(?<![\w.]){alias}\.\*", text):
                problems.append(f"{where}: {alias}.* reads every column of production.{table}")
            used = set(re.findall(rf"(?<![\w.]){alias}\.([a-z_][a-z0-9_]*)", text))
            missing = sorted(used - kept)
            if missing:
                problems.append(f"{where}: {alias}.{{{', '.join(missing)}}} not exported")
    assert not problems, (
        f"export_serving_db.SERVING_COLUMNS[{table!r}] is missing columns the runtime reads:\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize("table", sorted(SERVING_COLUMNS))
def test_pruned_columns_exist_on_the_model(table):
    model = next(
        (m for m in vars(production_models).values() if getattr(m, "__tablename__", None) == table),
        None,
    )
    assert model is not None, f"no production ORM model for {table}"
    columns = {c.name for c in model.__table__.columns}
    assert set(SERVING_COLUMNS[table]) <= columns


def test_units_ship_with_the_address_tables():
    """0030's ``unit`` and 0031's ``unit_key`` reach the serving dump: neither table is pruned."""
    assert "address_table" in SERVING_TABLES and "address_search" in SERVING_TABLES
    assert "address_table" not in SERVING_COLUMNS and "address_search" not in SERVING_COLUMNS
    assert "unit" in {c.name for c in production_models.Address.__table__.columns}
    assert "hpldn_unit_key" in SERVING_FUNCTIONS
