"""
What the alembic chain built: the schema from empty, and the state of hpdbldn at head.

* `pg_cluster` (marker ``pg``): ``alembic upgrade 0002`` on an empty,
  ``db_init.sql``-bootstrapped database creates every ORM table. Revisions
  from 0003 on load 48M rows, so the from-empty replay to head is not a
  pytest test: ``scripts/ci/replay_chain.sh`` runs it nightly.
* `hpdbldn_ro` (marker ``hpdbldn``): the data-quality fixes and constraints,
  the promotion to ``production``, the UPRN refresh, the reference and feature
  tables, PTAL, geography and the ``production.london_houses`` view, read
  from the migrated database in read-only sessions.
* `hpdbldn_rollback` (marker ``db_write``): the constraints reject bad rows,
  checked by inserting inside a transaction that is rolled back.

    uv run pytest src/hpldn/backend/tests/infra/test_database_upgrade.py
"""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from hpldn.backend.models.staging import AppBase, app_base_meta
from hpldn.backend.scripts.pipeline_steps import (
    _ADDITIONAL_SERVING_TABLES,
    _PTAL_BAND_RANKS,
    _SERVING_TABLES,
    LONDON_BBOX,
)

REPO_ROOT = Path(os.environ.get("HPLDN_DIR_FP", Path(__file__).resolve().parents[5]))

# Applies the data-quality fixes and the single promote to production.
DATA_QUALITY_REVISION = "0007"

# EPC-derived coverage is measured from 2009: the register did not exist
# before 2008, so earlier sales have no certificate by construction.
EPC_ERA_START_YEAR = 2009
EPC_ERA_PREDICATE = f"dateoftransfer >= DATE '{EPC_ERA_START_YEAR}-01-01'"


def _applied_revision(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()


def _chain_revisions() -> set:
    alembic_cfg = Config(str(REPO_ROOT.joinpath("src", "hpldn", "backend", "alembic.ini")))
    return {
        rev.revision
        for rev in ScriptDirectory.from_config(alembic_cfg).walk_revisions()
    }


def chain_has_been_applied(engine) -> bool:
    """
    Return True when ``alembic_version`` names a revision in this chain.

    A database stamped by the earlier 84-revision chain cannot be upgraded, and
    failing tests against it would report a migration bug that is not there.
    """
    return _applied_revision(engine) in _chain_revisions()


def _require_applied_chain(engine) -> None:
    assert chain_has_been_applied(engine), (
            f"alembic_version is {_applied_revision(engine)!r}, which is not a "
            "revision of the consolidated chain -- this instance was built by "
            "the pre-consolidation chain and has never had the current one "
            "applied. Re-stamp it against the consolidated chain (or rebuild "
            "it) before this assertion means anything."
        )


# The revisions of this chain that introduced each property checked below.
CERTIFICATE_HISTORY_REVISION = "0005"
GEOGRAPHY_LOOKUP_REVISION = "0003"
UPRN_REFRESH_REVISION = "0003"
UPRN_PROPAGATION_REVISION = "0007"


def _has_certificate_history(engine) -> bool:
    """Return True once the EPC tables are keyed per certificate, (address_id, inspection_date)."""
    return _at_least(engine, CERTIFICATE_HISTORY_REVISION)


EXPECTED_CONSTRAINTS = [
    ("staging.ppd_table", "uq_ppd_table_transaction", "u"),
    ("staging.ppd_table", "ck_ppd_price_positive", "c"),
    ("staging.address_table", "uq_address_table_uid", "u"),
    ("staging.rating_category_table", "uq_rating_category_table_rating_category", "u"),
    ("staging.build_form_category_table", "uq_build_form_category_table_build_form", "u"),
    (
        "staging.transaction_type_category_table",
        "uq_transaction_type_category_table_transaction_type",
        "u",
    ),
    (
        "staging.energy_tariff_category_table",
        "uq_energy_tariff_category_table_energy_tariff",
        "u",
    ),
    ("staging.glazed_type_category_table", "uq_glazed_type_category_table_glazed_type", "u"),
]


def _scalar(engine, sql: str):
    with engine.connect() as connection:
        return connection.execute(text(sql)).scalar()


def test_upgrade_reaches_head(hpdbldn_ro):
    """Check hpdbldn is at the chain's head, so every assertion below is about the current chain."""
    _require_applied_chain(hpdbldn_ro)
    version = _scalar(hpdbldn_ro, "SELECT version_num FROM alembic_version")
    alembic_cfg = Config(str(REPO_ROOT.joinpath("src", "hpldn", "backend", "alembic.ini")))
    assert version == ScriptDirectory.from_config(alembic_cfg).get_current_head()


def test_chain_builds_the_schema_from_empty(pg_cluster):
    """
    Check ``alembic upgrade 0002`` on a bootstrapped, empty database creates every ORM table.

    0001 creates the AppBase tables and 0002 the raw landing tables; both are
    DDL only. Everything after loads data (``scripts/ci/replay_chain.sh``).
    """
    pg_cluster.alembic_upgrade("0002")
    engine = pg_cluster.migration_engine
    assert _scalar(engine, "SELECT version_num FROM alembic_version") == "0002"
    tables = set(inspect(engine).get_table_names(schema=app_base_meta.schema))
    missing = {t.name for t in AppBase.metadata.sorted_tables} - tables
    assert not missing, f"0001/0002 did not create: {sorted(missing)}"


def test_epc_table_is_deduplicated(hpdbldn_ro):
    """Check ``epc_table`` holds one row per certificate, (address_id, inspection_date)."""
    key = (
        "address_id, inspection_date"
        if _has_certificate_history(hpdbldn_ro)
        else "address_id"
    )
    duplicates = _scalar(
        hpdbldn_ro,
        f"SELECT COUNT(*) FROM (SELECT 1 FROM staging.epc_table "
        f"GROUP BY {key} HAVING COUNT(*) > 1) d",
    )
    assert duplicates == 0


def test_ppd_table_no_duplicate_transactions(hpdbldn_ro):
    """Check ``ppd_table`` has no two rows for one (address, date, price) transaction."""
    key = (
        "address_id, price, dateoftransfer"
        if _has_certificate_history(hpdbldn_ro)
        else "address_id, price, dateoftransfer, propertytype, duration"
    )
    duplicates = _scalar(
        hpdbldn_ro,
        f"SELECT COUNT(*) FROM (SELECT 1 FROM staging.ppd_table "
        f"GROUP BY {key} HAVING COUNT(*) > 1) d",
    )
    assert duplicates == 0


def test_epc_unique_constraint_is_the_expected_one(hpdbldn_ro):
    """Check the EPC unique constraint is the per-certificate one."""
    expected = (
        "uq_epc_table_address_inspection"
        if _has_certificate_history(hpdbldn_ro)
        else "uq_epc_table_address_id"
    )
    contype = _scalar(
        hpdbldn_ro,
        f"SELECT contype FROM pg_constraint WHERE conname = '{expected}' "
        f"AND conrelid = to_regclass('staging.epc_table')",
    )
    assert contype == "u", f"constraint {expected} missing on staging.epc_table"


def test_constraints_exist(hpdbldn_ro):
    with hpdbldn_ro.connect() as connection:
        for table, name, contype in EXPECTED_CONSTRAINTS:
            row = connection.execute(
                text(
                    "SELECT contype FROM pg_constraint "
                    "WHERE conname = :name AND conrelid = to_regclass(:table)"
                ),
                {"name": name, "table": table},
            ).first()
            assert row is not None, f"constraint {name} missing on {table}"
            assert row.contype == contype


def test_propertytype_is_char1(hpdbldn_ro):
    row = _scalar(
        hpdbldn_ro,
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'staging.ppd_table'::regclass "
        "AND attname = 'propertytype'",
    )
    assert row == "character(1)"


def test_epc_unique_constraint_rejects_duplicates(hpdbldn_rollback):
    """Check re-inserting an existing certificate fails while a second certificate for a property is allowed."""
    connection = hpdbldn_rollback
    row = connection.execute(
        text("SELECT address_id, inspection_date FROM staging.epc_table LIMIT 1")
    ).first()
    assert row is not None, "staging.epc_table is empty"
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("INSERT INTO staging.epc_table (address_id, inspection_date) VALUES (:a, :d)"),
            {"a": row.address_id, "d": row.inspection_date},
        )
    with connection.begin_nested():
        connection.execute(
            text("INSERT INTO staging.epc_table (address_id, inspection_date) VALUES (:a, DATE '1900-01-01')"),
            {"a": row.address_id},
        )


def test_ppd_price_check_rejects_nonpositive(hpdbldn_rollback):
    connection = hpdbldn_rollback
    address_id = connection.execute(text("SELECT address_id FROM staging.ppd_table LIMIT 1")).scalar()
    assert address_id is not None, "staging.ppd_table is empty"
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text(
                "INSERT INTO staging.ppd_table "
                "(address_id, price, dateoftransfer, propertytype, duration) "
                "VALUES (:a, 0, CURRENT_DATE, 'D', 'F')"
            ),
            {"a": address_id},
        )


def test_production_tables_promoted(hpdbldn_ro):
    """Check every serving table is in production with staging's row count (epc_table: at least)."""
    _require_applied_chain(hpdbldn_ro)
    with hpdbldn_ro.connect() as connection:
        for table in _SERVING_TABLES:
            exists = connection.execute(
                text("SELECT to_regclass(:t)"), {"t": f"production.{table}"}
            ).scalar()
            assert exists is not None, f"production.{table} missing"
            staging_count = connection.execute(
                text(f"SELECT COUNT(*) FROM staging.{table}")
            ).scalar()
            production_count = connection.execute(
                text(f"SELECT COUNT(*) FROM production.{table}")
            ).scalar()
            if table == "epc_table":
                # Production deliberately holds more certificates: the
                # backfill appends to it (see
                # test_uprn_propagation_preserved_production_epc_backfill).
                assert production_count >= staging_count, table
            else:
                assert production_count == staging_count, table


def test_production_constraints_propagated(hpdbldn_ro):
    """Check ``LIKE ... INCLUDING ALL`` carried the data-quality constraints into production."""
    with hpdbldn_ro.connect() as connection:
        expected_key = (
            ["address_id", "inspection_date"]
            if _has_certificate_history(hpdbldn_ro)
            else ["address_id"]
        )
        unique_epc = connection.execute(
            text(
                "SELECT COUNT(*) FROM pg_index i "
                "WHERE i.indrelid = 'production.epc_table'::regclass "
                "AND i.indisunique "
                "AND (SELECT array_agg(a.attname ORDER BY "
                "         array_position(i.indkey::int2[], a.attnum))::text[]"
                "     FROM pg_attribute a "
                "     WHERE a.attrelid = i.indrelid "
                "     AND a.attnum = ANY(i.indkey)) = :key"
            ),
            {"key": expected_key},
        ).scalar()
        assert unique_epc >= 1
        price_check = connection.execute(
            text(
                "SELECT COUNT(*) FROM pg_constraint WHERE contype = 'c' "
                "AND conrelid = 'production.ppd_table'::regclass "
                "AND pg_get_constraintdef(oid) LIKE '%price > 0%'"
            )
        ).scalar()
        assert price_check == 1
        proptype = connection.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'production.ppd_table'::regclass "
                "AND attname = 'propertytype'"
            )
        ).scalar()
        assert proptype == "character(1)"


def test_production_uprn_lookup(hpdbldn_ro):
    with hpdbldn_ro.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM production.uprn_lookup")
        ).scalar()
        distinct = connection.execute(
            text("SELECT COUNT(DISTINCT uprn) FROM production.uprn_lookup")
        ).scalar()
        assert count > 0
        assert count == distinct


def _at_least(engine, revision: str) -> bool:
    """Return True once this instance has reached `revision` (zero-padded ids compare as strings)."""
    return (_applied_revision(engine) or "") >= revision


def test_ukhcs_refresh_added_uprns_in_place(hpdbldn_ro):
    """
    Check the OS Open UPRN refresh added rows without touching existing ids.

    ``address_table.uid`` and ``london_parcels.uprn`` reference those ids.
    """
    assert _at_least(hpdbldn_ro, UPRN_REFRESH_REVISION), f"hpdbldn is before {UPRN_REFRESH_REVISION}"
    with hpdbldn_ro.connect() as connection:
        watermark = connection.execute(
            text(
                "SELECT max_id_before FROM staging._ukhcs_refresh_marker "
                "WHERE revision = '0073'"
            )
        ).scalar()
        # The marker row keeps its old-chain label, '0073'; 0003 writes it.
        assert watermark is not None, "0003 recorded no OS Open UPRN refresh marker"

        added = connection.execute(
            text("SELECT COUNT(*) FROM staging.ukhcs_table WHERE id > :w"),
            {"w": watermark},
        ).scalar()
        assert added > 0, "0003's refresh added no UPRNs"

        count, distinct = connection.execute(
            text('SELECT COUNT(*), COUNT(DISTINCT "UPRN") FROM staging.ukhcs_table')
        ).one()
        assert count == distinct, "refresh introduced duplicate UPRNs"

        assert (
            connection.execute(
                text("SELECT to_regclass('staging._ukhcs_refresh')")
            ).scalar()
            is None
        )


def test_ukhcs_refresh_kept_referenced_uprns(hpdbldn_ro):
    """Check UPRNs retired by the newer extract survive, since sales history references them."""
    assert _at_least(hpdbldn_ro, UPRN_REFRESH_REVISION), f"hpdbldn is before {UPRN_REFRESH_REVISION}"
    with hpdbldn_ro.connect() as connection:
        orphaned_addresses = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM staging.address_table a
                WHERE NOT EXISTS (
                    SELECT 1 FROM staging.ukhcs_table u WHERE u.id = a.uid
                )
                """
            )
        ).scalar()
        assert orphaned_addresses == 0

        orphaned_parcels = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM staging.london_parcels p
                WHERE NOT EXISTS (
                    SELECT 1 FROM staging.ukhcs_table u WHERE u."UPRN" = p.uprn
                )
                """
            )
        ).scalar()
        assert orphaned_parcels == 0


def test_uprn_propagation_preserved_production_epc_backfill(hpdbldn_ro):
    """
    Check propagating the refresh kept the EPC certificates backfilled into production.

    Production deliberately holds more EPC rows than staging; a re-promote
    instead of an append would delete them.
    """
    assert _at_least(hpdbldn_ro, UPRN_PROPAGATION_REVISION), f"hpdbldn is before {UPRN_PROPAGATION_REVISION}"
    with hpdbldn_ro.connect() as connection:
        staging_epc, production_epc = connection.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM staging.epc_table), "
                "(SELECT COUNT(*) FROM production.epc_table)"
            )
        ).one()
        assert production_epc >= staging_epc, (
            "production.epc_table lost rows relative to staging -- 0007 "
            "re-promoted instead of appending, undoing 0008's EPC backfill"
        )

        has_floor_level = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = 'production' AND table_name = 'epc_table' "
                "AND column_name = 'floor_level'"
            )
        ).scalar()
        assert has_floor_level == 1


def test_uprn_lookup_matches_refreshed_ukhcs(hpdbldn_ro):
    """Check ``production.uprn_lookup`` matches the refreshed ``ukhcs_table`` and address join."""
    assert _at_least(hpdbldn_ro, UPRN_PROPAGATION_REVISION), f"hpdbldn is before {UPRN_PROPAGATION_REVISION}"
    with hpdbldn_ro.connect() as connection:
        expected = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM staging.ukhcs_table u
                INNER JOIN staging.address_table a ON a.uid = u.id
                """
            )
        ).scalar()
        actual = connection.execute(
            text("SELECT COUNT(*) FROM production.uprn_lookup")
        ).scalar()
        assert actual == expected

        unknown = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM production.uprn_lookup l
                WHERE NOT EXISTS (
                    SELECT 1 FROM staging.ukhcs_table u WHERE u."UPRN" = l.uprn
                )
                """
            )
        ).scalar()
        assert unknown == 0


def test_additional_serving_tables_promoted(hpdbldn_ro):
    """Check ``london_parcels`` is promoted with staging's row count."""
    with hpdbldn_ro.connect() as connection:
        for table in _ADDITIONAL_SERVING_TABLES:
            exists = connection.execute(
                text("SELECT to_regclass(:t)"), {"t": f"production.{table}"}
            ).scalar()
            assert exists is not None, f"production.{table} missing"
            staging_count = connection.execute(
                text(f"SELECT COUNT(*) FROM staging.{table}")
            ).scalar()
            production_count = connection.execute(
                text(f"SELECT COUNT(*) FROM production.{table}")
            ).scalar()
            assert production_count == staging_count, table


def test_production_london_parcels_no_fk_but_indexed_uprn(hpdbldn_ro):
    """Check production ``london_parcels`` has no foreign key but an indexed ``uprn`` for API joins."""
    with hpdbldn_ro.connect() as connection:
        fk_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM pg_constraint WHERE contype = 'f' "
                "AND conrelid = 'production.london_parcels'::regclass"
            )
        ).scalar()
        assert fk_count == 0
        uprn_indexed = connection.execute(
            text(
                "SELECT COUNT(*) FROM pg_index i "
                "WHERE i.indrelid = 'production.london_parcels'::regclass "
                "AND (SELECT array_agg(a.attname)::text[] FROM pg_attribute a "
                "     WHERE a.attrelid = i.indrelid "
                "     AND a.attnum = ANY(i.indkey)) = ARRAY['uprn']"
            )
        ).scalar()
        assert uprn_indexed >= 1


# Columns from the Land Registry transaction itself, present on every row.
_LONDON_HOUSES_REQUIRED_COLUMNS = [
    "ppd_id",
    "address_id",
    "price",
    "dateoftransfer",
    "propertytype",
    "duration",
    "postcode_district",
]

# EPC-derived columns and the minimum share of EPC-era rows that must carry a
# value; the view LEFT JOINs certificates, so coverage rather than NOT NULL.
# Regression floors for EPC-era (2009+) coverage, measured on hpdbldn at 0034
# (1,540,509 sales) and rounded down: construction_age_band
# 98.2 %, number_habitable_rooms 83.1 %, current_energy_rating 93.3 %,
# total_floor_area 97.7 %. The certificates themselves leave
# number_habitable_rooms and mains_gas_flag blank on 6-20 % of lodgements a
# year (production.epc_table, 2015-2024), so 100 % is not reachable.
_LONDON_HOUSES_EPC_COLUMNS = {
    "construction_age_band": 0.97,
    "number_habitable_rooms": 0.82,
    "current_energy_rating": 0.92,
    "total_floor_area": 0.96,
}


def test_london_houses_view_exists(hpdbldn_ro):
    exists = _scalar(
        hpdbldn_ro, "SELECT to_regclass('production.london_houses')"
    )
    assert exists is not None
    row_count = _scalar(hpdbldn_ro, "SELECT COUNT(*) FROM production.london_houses")
    assert row_count > 0


def test_london_houses_deduplicated_on_ppd_id(hpdbldn_ro):
    """Check the view has one row per PPD transaction, with no join fan-out."""
    with hpdbldn_ro.connect() as connection:
        total = connection.execute(
            text("SELECT COUNT(*) FROM production.london_houses")
        ).scalar()
        distinct_ppd_id = connection.execute(
            text("SELECT COUNT(DISTINCT ppd_id) FROM production.london_houses")
        ).scalar()
    assert total == distinct_ppd_id
    unique_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index WHERE indrelid = "
        "'production.london_houses'::regclass AND indisunique",
    )
    assert unique_index >= 1


def test_london_houses_required_columns_not_null(hpdbldn_ro):
    with hpdbldn_ro.connect() as connection:
        for column in _LONDON_HOUSES_REQUIRED_COLUMNS:
            null_count = connection.execute(
                text(f"SELECT COUNT(*) FROM production.london_houses WHERE {column} IS NULL")
            ).scalar()
            assert null_count == 0, f"{column} has NULLs in production.london_houses"


def _epc_era_coverage(engine, column: str) -> float:
    with engine.connect() as connection:
        total, populated = connection.execute(
            text(
                f"SELECT COUNT(*), COUNT({column}) FROM production.london_houses "
                f"WHERE {EPC_ERA_PREDICATE}"
            )
        ).one()
    assert total > 0
    return populated / total


@pytest.mark.parametrize(("column", "floor"), sorted(_LONDON_HOUSES_EPC_COLUMNS.items()))
def test_london_houses_epc_columns_are_covered_in_the_epc_era(hpdbldn_ro, column, floor):
    """Check EPC-derived columns resolve for most EPC-era sales."""
    coverage = _epc_era_coverage(hpdbldn_ro, column)
    assert coverage >= floor, (
        f"{column} resolves for only {coverage:.2%} of sales "
        f"from {EPC_ERA_START_YEAR} onward, floor {floor:.0%}"
    )


def test_london_houses_price_positive_and_dates_sane(hpdbldn_ro):
    with hpdbldn_ro.connect() as connection:
        row = connection.execute(
            text(
                "SELECT MIN(price), MIN(dateoftransfer), MAX(dateoftransfer) "
                "FROM production.london_houses"
            )
        ).one()
    min_price, min_date, max_date = row
    assert min_price > 0
    assert min_date <= max_date


def test_address_coordinates_table_exists(hpdbldn_ro):
    """Check ``address_coordinates`` has a unique address id and covers nearly every sale."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('production.address_coordinates')")
    assert exists is not None
    unique_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index WHERE indrelid = "
        "'production.address_coordinates'::regclass AND indisunique",
    )
    assert unique_index >= 1
    with hpdbldn_ro.connect() as connection:
        total, with_coord = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(ac.latitude) FROM production.london_houses lh "
                "LEFT JOIN production.address_coordinates ac ON ac.address_id = lh.address_id"
            )
        ).one()
    assert total > 0
    assert with_coord / total > 0.99


def test_comps_features_table_exists(hpdbldn_ro):
    """Check ``comps_features`` has a unique ``ppd_id`` and a comparable for nearly every sale."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('production.comps_features')")
    assert exists is not None
    unique_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index WHERE indrelid = "
        "'production.comps_features'::regclass AND indisunique",
    )
    assert unique_index >= 1
    with hpdbldn_ro.connect() as connection:
        total, with_comps = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE comp_txn_count > 0) "
                "FROM production.comps_features"
            )
        ).one()
    assert total > 0
    assert with_comps / total > 0.99


def test_london_houses_comp_columns_present_and_mostly_populated(hpdbldn_ro):
    """
    Check the comparable columns are in the view and mostly populated.

    ``comp_median_price_per_sqm`` needs neighbours with a certificate, so its
    floor is lower (observed 98.83%).
    """
    with hpdbldn_ro.connect() as connection:
        total = connection.execute(
            text(f"SELECT COUNT(*) FROM production.london_houses WHERE {EPC_ERA_PREDICATE}")
        ).scalar()
        assert total > 0
        floors = {
            "comp_median_price": 0.99,
            "comp_median_price_per_sqm": 0.98,
            "comp_txn_count": 0.99,
        }
        for column, floor in floors.items():
            populated = connection.execute(
                text(
                    f"SELECT COUNT({column}) FROM production.london_houses "
                    f"WHERE {EPC_ERA_PREDICATE}"
                )
            ).scalar()
            assert populated / total > floor, (
                f"{column} resolves for only {populated / total:.2%} of sales "
                f"from {EPC_ERA_START_YEAR} onward, floor {floor:.0%}"
            )
        distinct_values = connection.execute(
            text("SELECT COUNT(DISTINCT comp_median_price) FROM production.london_houses")
        ).scalar()
        assert distinct_values > 1


# Floors for the second round of feature columns, measured on hpdbldn at 0034
# and rounded down. Transaction-derived columns are total; EPC-derived ones
# carry the as-of join's tail (tenure 64.0 %, extension_count 77.8 %); the
# floors guard against further loss.
_NEW_FEATURE_FLOORS = {
    "postcode_sector": 0.99,
    "build_form": 0.92,
    "mains_gas_flag": 0.76,
    "tenure": 0.63,
    "multi_glaze_proportion": 0.87,
    "extension_count": 0.76,
    "latitude": 0.99,
    "longitude": 0.99,
}


@pytest.mark.parametrize(("column", "floor"), sorted(_NEW_FEATURE_FLOORS.items()))
def test_london_houses_new_feature_columns_present_and_mostly_populated(hpdbldn_ro, column, floor):
    """Check the second round of feature columns exist and are mostly populated."""
    coverage = _epc_era_coverage(hpdbldn_ro, column)
    assert coverage >= floor, (
        f"{column} resolves for only {coverage:.2%} of sales "
        f"from {EPC_ERA_START_YEAR} onward, floor {floor:.0%}"
    )


def test_london_stations_table_exists_and_populated(hpdbldn_ro):
    """Check ``london_stations`` is loaded (659 rows on this instance)."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('production.london_stations')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, with_zone = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(zone) FROM production.london_stations"
            )
        ).one()
    assert total > 0
    assert with_zone > 0
    assert with_zone < total  


def test_boe_rate_table_exists_and_populated(hpdbldn_ro):
    """Check ``boe_rate`` is loaded with one row per rate change, unique on date."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('production.boe_rate')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total = connection.execute(text("SELECT COUNT(*) FROM production.boe_rate")).scalar()
        distinct_dates = connection.execute(
            text("SELECT COUNT(DISTINCT date_changed) FROM production.boe_rate")
        ).scalar()
        min_rate, max_rate = connection.execute(
            text("SELECT MIN(rate), MAX(rate) FROM production.boe_rate")
        ).one()
    assert total > 0
    assert total == distinct_dates
    assert min_rate > 0
    assert max_rate < 20  


def test_ukhpi_table_exists_and_populated(hpdbldn_ro):
    """Check ``ukhpi`` is loaded with one row per month, unique on period, covering every sale date."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('production.ukhpi')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total = connection.execute(text("SELECT COUNT(*) FROM production.ukhpi")).scalar()
        distinct_periods = connection.execute(
            text("SELECT COUNT(DISTINCT period) FROM production.ukhpi")
        ).scalar()
        min_period, max_period = connection.execute(
            text("SELECT MIN(period), MAX(period) FROM production.ukhpi")
        ).one()
        min_txn_date = connection.execute(
            text("SELECT MIN(dateoftransfer) FROM production.london_houses")
        ).scalar()
    assert total > 0
    assert total == distinct_periods
    assert min_period <= min_txn_date <= max_period


def test_station_proximity_features_table_exists_and_populated(hpdbldn_ro):
    """
    Check ``station_proximity_features`` has a unique ``ppd_id`` and a zoned station per sale.

    The table covers every PPD sale, a superset of the view's rows.
    """
    exists = _scalar(
        hpdbldn_ro, "SELECT to_regclass('production.station_proximity_features')"
    )
    assert exists is not None
    unique_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index WHERE indrelid = "
        "'production.station_proximity_features'::regclass AND indisunique",
    )
    assert unique_index >= 1
    with hpdbldn_ro.connect() as connection:
        total, with_distance, with_zone = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(nearest_station_distance_km), "
                "COUNT(nearest_station_zone) FROM production.station_proximity_features"
            )
        ).one()
        min_distance = connection.execute(
            text("SELECT MIN(nearest_station_distance_km) FROM production.station_proximity_features")
        ).scalar()
        houses_total = connection.execute(
            text("SELECT COUNT(*) FROM production.london_houses")
        ).scalar()
    assert total >= houses_total
    assert (total - houses_total) / houses_total < 0.001
    assert with_distance == total
    assert with_zone == total
    assert min_distance >= 0


def test_london_houses_external_data_columns_present_and_fully_populated(hpdbldn_ro):
    """
    Check the station, Bank Rate and UKHPI columns are total in the view within the EPC era.

    Scoped to the EPC era because the UKHPI series starts after the earliest
    1995 sales.
    """
    with hpdbldn_ro.connect() as connection:
        total = connection.execute(
            text(f"SELECT COUNT(*) FROM production.london_houses WHERE {EPC_ERA_PREDICATE}")
        ).scalar()
        assert total > 0
        for column in (
            "nearest_station_distance_km",
            "nearest_station_zone",
            "boe_rate_at_sale",
            "ukhpi_avg_price_at_sale",
        ):
            populated = connection.execute(
                text(
                    f"SELECT COUNT({column}) FROM production.london_houses "
                    f"WHERE {EPC_ERA_PREDICATE}"
                )
            ).scalar()
            assert populated == total, (
                f"{column} has NULLs in production.london_houses for sales "
                f"from {EPC_ERA_START_YEAR} onward"
            )
        distinct_rates = connection.execute(
            text("SELECT COUNT(DISTINCT boe_rate_at_sale) FROM production.london_houses")
        ).scalar()
        assert distinct_rates > 1




def test_london_green_spaces_table_exists_and_populated(hpdbldn_ro):
    """Check ``london_green_spaces`` is loaded with valid geometry and an area for every site."""
    _require_applied_chain(hpdbldn_ro)
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.london_green_spaces')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, invalid_geom, null_area, distinct_functions = connection.execute(
            text(
                "SELECT COUNT(*), "
                "COUNT(*) FILTER (WHERE NOT ST_IsValid(geom)), "
                "COUNT(*) FILTER (WHERE area_ha IS NULL), "
                "COUNT(DISTINCT space_function) "
                "FROM staging.london_green_spaces"
            )
        ).one()
        min_area = connection.execute(
            text("SELECT MIN(area_ha) FROM staging.london_green_spaces")
        ).scalar()
    assert total > 0
    assert invalid_geom == 0
    assert null_area == 0
    assert distinct_functions > 1  
    assert min_area > 0


def test_london_green_spaces_geom_index(hpdbldn_ro):
    """Check ``london_green_spaces`` has a GIST index on ``geom``."""
    _require_applied_chain(hpdbldn_ro)
    with hpdbldn_ro.connect() as connection:
        gist_index = connection.execute(
            text(
                "SELECT COUNT(*) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_am am ON am.oid = c.relam "
                "WHERE i.indrelid = 'staging.london_green_spaces'::regclass "
                "AND am.amname = 'gist'"
            )
        ).scalar()
    assert gist_index >= 1


def test_london_green_space_access_points_table_exists_and_populated(hpdbldn_ro):
    """Check every green-space access point resolves its site foreign key."""
    _require_applied_chain(hpdbldn_ro)
    exists = _scalar(
        hpdbldn_ro, "SELECT to_regclass('staging.london_green_space_access_points')"
    )
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, orphaned, distinct_types = connection.execute(
            text(
                "SELECT COUNT(*), "
                "COUNT(*) FILTER (WHERE gs.id IS NULL), "
                "COUNT(DISTINCT ap.access_type) "
                "FROM staging.london_green_space_access_points ap "
                "LEFT JOIN staging.london_green_spaces gs "
                "ON gs.id = ap.ref_to_greenspace_site"
            )
        ).one()
    assert total > 0
    assert orphaned == 0
    assert distinct_types >= 1



_PTAL_AI_UPPER_BOUNDS = {
    "1a": 2.5,
    "1b": 5.0,
    "2": 10.0,
    "3": 15.0,
    "4": 20.0,
    "5": 25.0,
    "6a": 40.0,
}


def test_ptal_2023_table_exists_and_populated(hpdbldn_ro):
    """Check ``ptal_2023`` holds one row per grid cell, unique on ``grid_id``, with only published bands."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.ptal_2023')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, distinct_ids, min_ai = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(DISTINCT grid_id), MIN(ai) FROM staging.ptal_2023"
            )
        ).one()
        bands = {
            row[0]
            for row in connection.execute(
                text("SELECT DISTINCT ptal_band FROM staging.ptal_2023")
            )
        }
    assert total > 0
    assert total == distinct_ids
    assert min_ai >= 0
    assert bands == set(_PTAL_BAND_RANKS)


def test_ptal_2023_band_matches_published_ai_thresholds(hpdbldn_ro):
    """Check each cell's band agrees with TfL's published AI thresholds."""
    with hpdbldn_ro.connect() as connection:
        zero_band_max_ai = connection.execute(
            text("SELECT MAX(ai) FROM staging.ptal_2023 WHERE ptal_band = '0'")
        ).scalar()
        assert zero_band_max_ai == 0

        for band, upper in _PTAL_AI_UPPER_BOUNDS.items():
            min_ai, max_ai = connection.execute(
                text("SELECT MIN(ai), MAX(ai) FROM staging.ptal_2023 WHERE ptal_band = :b"),
                {"b": band},
            ).one()
            assert min_ai is not None, f"no rows in band {band}"
            assert max_ai <= upper, f"band {band} exceeds its {upper} AI ceiling"

        min_6b = connection.execute(
            text("SELECT MIN(ai) FROM staging.ptal_2023 WHERE ptal_band = '6b'")
        ).scalar()
        assert min_6b > _PTAL_AI_UPPER_BOUNDS["6a"]


def test_ptal_coords_covers_every_grid_cell(hpdbldn_ro):
    """Check ``ptal_coords`` covers every PTAL cell with no orphans."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.ptal_coords')")
    assert exists is not None
    unique_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index WHERE indrelid = "
        "'staging.ptal_coords'::regclass AND indisunique",
    )
    assert unique_index >= 1
    with hpdbldn_ro.connect() as connection:
        cells, coords = connection.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM staging.ptal_2023), "
                "(SELECT COUNT(*) FROM staging.ptal_coords)"
            )
        ).one()
        uncovered = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_2023 p "
                "LEFT JOIN staging.ptal_coords c ON c.grid_id = p.grid_id "
                "WHERE c.grid_id IS NULL"
            )
        ).scalar()
        null_geom = connection.execute(
            text("SELECT COUNT(*) FROM staging.ptal_coords WHERE geom IS NULL")
        ).scalar()
    assert cells > 0
    assert coords == cells
    assert uncovered == 0
    assert null_geom == 0


def test_ptal_coords_within_london_extent(hpdbldn_ro):
    """Check every centroid is inside Greater London and matches its latitude and longitude."""
    with hpdbldn_ro.connect() as connection:
        outside = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_coords WHERE "
                "latitude NOT BETWEEN :min_lat AND :max_lat OR "
                "longitude NOT BETWEEN :min_lon AND :max_lon"
            ),
            {
                "min_lat": LONDON_BBOX["min_latitude"],
                "max_lat": LONDON_BBOX["max_latitude"],
                "min_lon": LONDON_BBOX["min_longitude"],
                "max_lon": LONDON_BBOX["max_longitude"],
            },
        ).scalar()
        mismatched = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_coords WHERE "
                "ABS(ST_X(geom) - longitude) > 1e-9 OR ABS(ST_Y(geom) - latitude) > 1e-9"
            )
        ).scalar()
        srid = connection.execute(
            text("SELECT ST_SRID(geom) FROM staging.ptal_coords LIMIT 1")
        ).scalar()
        max_offset_m = connection.execute(
            text(
                "SELECT MAX(ST_Distance("
                "  ST_Transform(geom, 27700), ST_SetSRID(ST_MakePoint(easting, northing), 27700)"
                ")) FROM staging.ptal_coords"
            )
        ).scalar()
    assert outside == 0
    assert mismatched == 0
    assert srid == 4326
    assert max_offset_m < 5.0


def test_ptal_coords_geom_gist_index(hpdbldn_ro):
    """Check ``ptal_coords`` has a GIST index on ``geom``."""
    gist_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_am am ON am.oid = c.relam "
        "WHERE i.indrelid = 'staging.ptal_coords'::regclass AND am.amname = 'gist'",
    )
    assert gist_index >= 1


def test_ptal_london_is_materialized_view_with_one_row_per_cell(hpdbldn_ro):
    """Check ``ptal_london`` is a materialized view with one row per cell and a unique index."""
    relkind = _scalar(
        hpdbldn_ro,
        "SELECT relkind FROM pg_class WHERE relname = 'ptal_london' "
        "AND relnamespace = 'staging'::regnamespace",
    )
    assert relkind == "m"
    with hpdbldn_ro.connect() as connection:
        total, distinct_ids = connection.execute(
            text("SELECT COUNT(*), COUNT(DISTINCT grid_id) FROM staging.ptal_london")
        ).one()
    assert total > 0
    assert total == distinct_ids
    unique_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index WHERE indrelid = "
        "'staging.ptal_london'::regclass AND indisunique",
    )
    assert unique_index >= 1


def test_ptal_london_carries_columns_of_both_sources(hpdbldn_ro):
    """Check ``ptal_london`` exposes every column of both source tables."""
    with hpdbldn_ro.connect() as connection:
        def columns(relation: str) -> set:
            return {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT attname FROM pg_attribute "
                        "WHERE attrelid = to_regclass(:r) AND attnum > 0 AND NOT attisdropped"
                    ),
                    {"r": relation},
                )
            }

        view_columns = columns("staging.ptal_london")
        missing_from_ptal = columns("staging.ptal_2023") - view_columns
        missing_from_coords = columns("staging.ptal_coords") - view_columns
    assert missing_from_ptal == set(), f"ptal_london is missing {missing_from_ptal}"
    assert missing_from_coords == set(), f"ptal_london is missing {missing_from_coords}"
    assert "ptal_band_rank" in view_columns


def test_ptal_london_is_london_only_and_fully_joined(hpdbldn_ro):
    """Check ``ptal_london`` admits every published cell and leaves no coordinate NULL."""
    with hpdbldn_ro.connect() as connection:
        view_total, source_total = connection.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM staging.ptal_london), "
                "(SELECT COUNT(*) FROM staging.ptal_2023)"
            )
        ).one()
        null_coords = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_london "
                "WHERE latitude IS NULL OR longitude IS NULL"
            )
        ).scalar()
        outside = connection.execute(
            text(
                "SELECT COUNT(*) FROM staging.ptal_london WHERE "
                "latitude NOT BETWEEN :min_lat AND :max_lat OR "
                "longitude NOT BETWEEN :min_lon AND :max_lon"
            ),
            {
                "min_lat": LONDON_BBOX["min_latitude"],
                "max_lat": LONDON_BBOX["max_latitude"],
                "min_lon": LONDON_BBOX["min_longitude"],
                "max_lon": LONDON_BBOX["max_longitude"],
            },
        ).scalar()
    assert view_total == source_total
    assert null_coords == 0
    assert outside == 0


def test_ptal_london_band_rank_is_the_ordinal_form_of_the_band(hpdbldn_ro):
    """Check ``ptal_band_rank`` maps each band to its rank and rises with the accessibility index."""
    with hpdbldn_ro.connect() as connection:
        pairs = {
            row[0]: row[1]
            for row in connection.execute(
                text(
                    "SELECT ptal_band, MIN(ptal_band_rank) FROM staging.ptal_london "
                    "GROUP BY ptal_band"
                )
            )
        }
        inconsistent = connection.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT ptal_band FROM staging.ptal_london "
                "GROUP BY ptal_band HAVING COUNT(DISTINCT ptal_band_rank) > 1) d"
            )
        ).scalar()
        ranked = connection.execute(
            text(
                "SELECT ptal_band_rank, AVG(ai) FROM staging.ptal_london "
                "GROUP BY ptal_band_rank ORDER BY ptal_band_rank"
            )
        ).all()
    assert pairs == _PTAL_BAND_RANKS
    assert inconsistent == 0
    mean_ai = [float(row[1]) for row in ranked]
    assert mean_ai == sorted(mean_ai), "mean AI is not monotonic in ptal_band_rank"




def test_staging_lsoa_table_exists_and_populated(hpdbldn_ro):
    """Check ``staging.lsoa`` is loaded from the LSOA11/LSOA21/LAD22 lookup."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.lsoa')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, distinct_11, distinct_21 = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(DISTINCT lsoa11cd), COUNT(DISTINCT lsoa21cd) "
                "FROM staging.lsoa"
            )
        ).one()
    assert total > 0
    assert distinct_11 > 0
    assert distinct_21 > 0


def test_staging_msoa_table_exists_and_populated(hpdbldn_ro):
    """Check ``staging.msoa`` is loaded from the MSOA 2011 to 2021 best-fit lookup."""
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.msoa')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, distinct_11, distinct_21 = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(DISTINCT msoa11cd), COUNT(DISTINCT msoa21cd) "
                "FROM staging.msoa"
            )
        ).one()
    assert total > 0
    assert distinct_11 > 0
    assert distinct_21 > 0




def test_staging_lsoa_msoa_lu_covers_every_london_lsoa(hpdbldn_ro):
    """Check ``lsoa_msoa_lu`` gives every London LSOA an MSOA and London's MSOA count matches."""
    _require_applied_chain(hpdbldn_ro)
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.lsoa_msoa_lu')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        unmatched = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM staging.lsoa l
                LEFT JOIN staging.lsoa_msoa_lu m ON m.lsoa11cd = l.lsoa11cd
                WHERE l.lad22cd LIKE 'E09%' AND m.msoa11cd IS NULL
                """
            )
        ).scalar()
        london_msoa_count, staging_msoa_count = connection.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(DISTINCT m.msoa11cd)
                     FROM staging.lsoa l
                     JOIN staging.lsoa_msoa_lu m ON m.lsoa11cd = l.lsoa11cd
                     WHERE l.lad22cd LIKE 'E09%'),
                    (SELECT COUNT(DISTINCT msoa11cd) FROM staging.msoa WHERE lad22cd LIKE 'E09%')
                """
            )
        ).one()
    assert unmatched == 0
    assert london_msoa_count == staging_msoa_count


def test_production_property_lsoa_is_point_in_polygon(hpdbldn_ro):
    """Check ``property_lsoa`` is mostly point-in-polygon and resolves thousands of LSOAs."""
    _require_applied_chain(hpdbldn_ro)
    exists = _scalar(hpdbldn_ro, "SELECT to_regclass('staging.property_lsoa')")
    assert exists is not None
    with hpdbldn_ro.connect() as connection:
        total, contains, distinct_lsoa = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE assignment_method = 'contains'), "
                "COUNT(DISTINCT lsoa11cd) FROM staging.property_lsoa"
            )
        ).one()
    assert total > 0
    assert contains / total >= 0.95
    assert distinct_lsoa > 4000


def test_london_houses_geography_codes_are_real(hpdbldn_ro):
    """Check the view's LSOA and MSOA codes are real, granular and distinct, not a borough proxy."""
    with hpdbldn_ro.connect() as connection:
        distinct_lsoa, distinct_msoa, bad_lsoa, bad_msoa, same_code = connection.execute(
            text(
                """
                SELECT COUNT(DISTINCT lsoa_code),
                       COUNT(DISTINCT msoa_code),
                       COUNT(*) FILTER (WHERE lsoa_code IS NOT NULL AND lsoa_code NOT LIKE 'E01%'),
                       COUNT(*) FILTER (WHERE msoa_code IS NOT NULL AND msoa_code NOT LIKE 'E02%'),
                       COUNT(*) FILTER (WHERE lsoa_code IS NOT NULL AND lsoa_code = msoa_code)
                FROM production.london_houses
                """
            )
        ).one()
    assert distinct_lsoa > 4000
    assert distinct_msoa > 900
    assert bad_lsoa == 0
    assert bad_msoa == 0
    assert same_code == 0


def test_lsoa_code_matches_iod_geography(hpdbldn_ro):
    """Check the view's ``lsoa_code`` matches ``property_lsoa``, the geography IoD is keyed on."""
    _require_applied_chain(hpdbldn_ro)
    with hpdbldn_ro.connect() as connection:
        mismatched = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM production.london_houses lh
                JOIN staging.property_lsoa pl ON pl.ppd_id = lh.ppd_id
                WHERE lh.lsoa_code IS NOT NULL
                  AND pl.lsoa11cd IS NOT NULL
                  AND lh.lsoa_code IS DISTINCT FROM pl.lsoa11cd
                """
            )
        ).scalar()
    assert mismatched == 0


def test_ptal_london_geom_gist_index(hpdbldn_ro):
    """Check ``ptal_london`` has its own GIST index on ``geom``."""
    gist_index = _scalar(
        hpdbldn_ro,
        "SELECT COUNT(*) FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_am am ON am.oid = c.relam "
        "WHERE i.indrelid = 'staging.ptal_london'::regclass AND am.amname = 'gist'",
    )
    assert gist_index >= 1


def test_london_uprns(hpdbldn_ro):
    """
    Check (almost) every ``london_houses`` UPRN is in OS Open UPRN (``staging.ukhcs_table``).

    The tolerance is 1 % of the view's real distinct UPRNs, and an empty view
    fails.
    """
    with hpdbldn_ro.connect() as connection:
        distinct, missing = connection.execute(
            text(
                """
                WITH h AS (SELECT DISTINCT uprn FROM production.london_houses WHERE uprn IS NOT NULL)
                SELECT (SELECT count(*) FROM h),
                       (SELECT count(*) FROM h WHERE NOT EXISTS (
                            SELECT 1 FROM staging.ukhcs_table u WHERE u."UPRN" = h.uprn))
                """
            )
        ).one()
    assert distinct > 0, "production.london_houses carries no UPRN"
    assert missing <= 0.01 * distinct, (
        f"{missing} of {distinct} london_houses UPRNs are missing from staging.ukhcs_table (limit 1%)"
    )
