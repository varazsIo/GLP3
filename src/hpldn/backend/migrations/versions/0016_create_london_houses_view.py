"""
Build ``production.london_houses`` from the features 0010-0015 build.

Uses `hpldn.backend.migrations.london_houses_view` and asserts the parity
guards: row count, exact ``epc_matched`` count, no NULL
``build_form_propertytype``, well-formed ``construction_age_band``, and fusion
coverage. Refresh later with ``REFRESH MATERIALIZED VIEW CONCURRENTLY``, not by
re-running this revision.

Revision ID: 0016
Revises: 0015
"""

from sqlalchemy import text

from hpldn.backend.migrations.chain import no_downgrade
from hpldn.backend.migrations.london_houses_view import (
    EPC_MATCHED_ROWS_EXPECTED,
    CREATE_VIEW_SQL_0020,
    LONDON_HOUSES_ROWS_EXPECTED,
    VIEW_COLUMNS_0020,
    rebuild,
)
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_GUARD_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE epc_matched),
       count(*) FILTER (WHERE build_form_propertytype IS NULL),
       count(*) FILTER (
           WHERE construction_age_band IS NOT NULL
             AND construction_age_band !~ '^[0-9]{4}(-[0-9]{4})?$'
       ),
       min(price),
       min(dateoftransfer),
       max(dateoftransfer)
FROM production.london_houses
"""


def _check_columns(engine, log, expected_columns=VIEW_COLUMNS_0020) -> None:
    """
    Check the view's columns match `expected_columns`, in order.

    The definition this revision and 0020 build by default; 0025, 0028 and
    0029 pass ``VIEW_COLUMNS_0025``, 0034 the current ``VIEW_COLUMNS``.

    Raises
    ------
    RuntimeError
        On a missing, extra or reordered column; order matters because the
        data-quality tests compare ordinal positions.
    """
    with engine.connect() as connection:
        actual = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT a.attname FROM pg_attribute a "
                    "WHERE a.attrelid = 'production.london_houses'::regclass "
                    "AND a.attnum > 0 AND NOT a.attisdropped "
                    "ORDER BY a.attnum"
                )
            )
        ]
    expected = list(expected_columns)
    if actual != expected:
        missing = [c for c in expected if c not in actual]
        extra = [c for c in actual if c not in expected]
        detail = f"missing={missing}, unexpected={extra}"
        if not missing and not extra:
            first = next(
                i for i, (a, e) in enumerate(zip(actual, expected)) if a != e
            )
            detail = (
                f"same columns in a different order; first difference at "
                f"position {first}: got {actual[first]!r}, "
                f"expected {expected[first]!r}"
            )
        raise RuntimeError(
            f"production.london_houses has {len(actual)} column(s) where the "
            f"definition declares {len(expected)} -- {detail}"
        )
    log.info("0016: production.london_houses has the expected %d columns, in order",
             len(actual))


def upgrade() -> None:
    """
    Build the view and its indexes, then run the guards.

    The fusion checks that need the view (no EPC dated after its sale, the
    monthly breakdown) are skipped in 0006 and asserted here.
    """
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()

    log.info(
        "0016: building production.london_houses -- ONCE. The chain this "
        "replaces rebuilt it 47 times on the upgrade path."
    )
    rebuild(engine, cfg, log, create_sql=CREATE_VIEW_SQL_0020)

    _check_columns(engine, log)

    with engine.connect() as connection:
        (
            rows, matched, no_build_form, bad_bands,
            min_price, min_date, max_date,
        ) = connection.execute(text(_GUARD_SQL)).one()

    log.info(
        "0016: production.london_houses -- %d row(s), %d (%.1f%%) epc_matched, "
        "price from GBP %s, sales %s..%s",
        rows, matched, 100.0 * matched / rows if rows else 0.0,
        min_price, min_date, max_date,
    )

    if matched != EPC_MATCHED_ROWS_EXPECTED:
        raise RuntimeError(
            f"production.london_houses has {matched} epc_matched row(s), "
            f"expected exactly {EPC_MATCHED_ROWS_EXPECTED}. That subset must "
            "reproduce the pre-0081 view exactly -- a different number means "
            "the definition changed which sales QUALIFY, not just which are "
            "admitted, and every comparison scoped to epc_matched (the "
            "data-quality budgets, the like-for-like retrain metric) is now "
            "measuring a different population than it thinks."
        )
    if rows != LONDON_HOUSES_ROWS_EXPECTED:
        raise RuntimeError(
            f"production.london_houses has {rows} row(s), expected "
            f"{LONDON_HOUSES_ROWS_EXPECTED} (the reference build). A "
            "from-empty build is meant to reproduce it, so a different count "
            "is a finding. If "
            "a refreshed source file moved it legitimately, update "
            "LONDON_HOUSES_ROWS_EXPECTED in hpldn.backend.migrations."
            "london_houses_view in the same commit as the file."
        )
    if no_build_form:
        raise RuntimeError(
            f"{no_build_form} row(s) have a NULL build_form_propertytype "
            "after the COALESCE, which should be impossible -- propertytype "
            "is NOT NULL on production.ppd_table"
        )
    if bad_bands:
        raise RuntimeError(
            f"{bad_bands} row(s) carry a construction_age_band outside "
            "'YYYY'/'YYYY-YYYY'. 0008's normalisation should have made that "
            "unreachable; hpp_rf._construction_age_band_midpoint cannot "
            "parse anything else."
        )

    log.info("0016: re-running the fusion verification against the view")
    steps.verify_epc_ppd_fusion_v2(engine, cfg, log)

    log.info("0016: verifying EPC/PPD fusion coverage in the view")
    steps.verify_epc_ppd_fusion_view_coverage(engine, cfg, log)

    log.info(
        "0016: chain complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  python -m hpldn.backend.scripts.bare_metal --finalize\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_verify_init.sql\n"
        "Ongoing refreshes use REFRESH MATERIALIZED VIEW CONCURRENTLY "
        "production.london_houses -- not a re-run of this revision."
    )


def downgrade() -> None:
    """Refuse; the revisions below this one are not reversible."""
    no_downgrade()
