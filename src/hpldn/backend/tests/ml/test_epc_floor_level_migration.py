"""
Integration tests for ``production.epc_table.floor_level`` and its view column.

Reads the migrated hpdbldn database through `hpdbldn_ro` (read-only
sessions; fails when it is unreachable). Checks the column exists, is
nullable and was backfilled; that ``production.london_houses`` exposes it;
and that it is mostly NULL for houses and well populated for flats::

    pytest src/hpldn/backend/tests/ml/test_epc_floor_level_migration.py -s
"""


from sqlalchemy import text




def _scalar(engine, sql: str):
    with engine.connect() as connection:
        return connection.execute(text(sql)).scalar()


def test_epc_table_floor_level_column_exists_and_nullable(hpdbldn_ro):
    """Check ``epc_table.floor_level`` exists and accepts NULL."""
    with hpdbldn_ro.connect() as connection:
        row = connection.execute(
            text(
                "SELECT is_nullable, data_type FROM information_schema.columns "
                "WHERE table_schema = 'production' AND table_name = 'epc_table' "
                "AND column_name = 'floor_level'"
            )
        ).one_or_none()
    assert row is not None, "production.epc_table.floor_level does not exist"
    is_nullable, data_type = row
    assert is_nullable == "YES"
    assert data_type in ("character varying", "text")


def test_epc_table_floor_level_backfilled_not_left_empty(hpdbldn_ro):
    """Check the backfill populated real values rather than adding an empty column."""
    with hpdbldn_ro.connect() as connection:
        total, populated = connection.execute(
            text("SELECT COUNT(*), COUNT(floor_level) FROM production.epc_table")
        ).one()
    assert total > 0
    assert populated > 0, "floor_level was added but never backfilled"


def test_london_houses_floor_level_column_present(hpdbldn_ro):
    """
    Check ``production.london_houses`` exposes ``floor_level``.

    Uses ``pg_attribute``, because ``information_schema.columns`` does not
    list materialized-view columns.
    """
    exists = _scalar(
        hpdbldn_ro,
        "SELECT 1 FROM pg_attribute WHERE attrelid = 'production.london_houses'::regclass "
        "AND attname = 'floor_level' AND attnum > 0 AND NOT attisdropped",
    )
    assert exists == 1


def test_london_houses_floor_level_mostly_null_for_non_flats(hpdbldn_ro):
    """
    Check ``floor_level`` is overwhelmingly NULL for D, S and T houses.

    Not strictly NULL: 1.17% of non-flat rows carry a genuine value (likely
    converted buildings), against 97.0% coverage for flats.
    """
    with hpdbldn_ro.connect() as connection:
        total_non_flat, non_flat_with_floor_level = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(floor_level) FROM production.london_houses "
                "WHERE propertytype != 'F'"
            )
        ).one()
    assert total_non_flat > 0, "no non-flat rows in production.london_houses"
    assert non_flat_with_floor_level / total_non_flat < 0.05


def test_london_houses_floor_level_populated_for_some_flats(hpdbldn_ro):
    """Check the backfill reaches at least some flats, so the feature is usable."""
    with hpdbldn_ro.connect() as connection:
        total_flats, flats_with_floor_level = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(floor_level) FROM production.london_houses "
                "WHERE propertytype = 'F'"
            )
        ).one()
    assert total_flats > 0, "no flats in production.london_houses"
    assert flats_with_floor_level > 0
