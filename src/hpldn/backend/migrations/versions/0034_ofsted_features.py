"""
Give every sale the Ofsted ratings of the schools near it, as they stood on its date.

``production.ofsted_features`` holds one row per ``production.ppd_table``
sale with a location, joined to the view the way ``iod_features`` is. Each
sale looks up the spells of ``production.ofsted_rating_spells`` (0033) in
force on its completion date (``valid_from <= dateoftransfer < valid_to``)
within `ofsted_features.SEARCH_RADIUS_M` of it, through a GiST index on
(location, validity), and derives the twelve ``ofsted_*`` columns described
in `hpldn.backend.scripts.ofsted_features`, whose constants are bound as
parameters here and read by the serving code too. ``valid_from`` is the
report's publication date, so no embargo is needed; a guard still counts the
nearest spells a sale used that were not yet published, or had ended, on its
date, and fails on any.

The work is split into interleaved ``mod(ppd_id, jobs)`` chunks run on
separate connections (`chain.run_concurrently`). Then
``production.london_houses`` is rebuilt with `london_houses_view.CREATE_VIEW_SQL`,
which appends the twelve columns (107 in all). Run
``src/hpldn/backend/db/sql/db_grants_post_migration.sql`` afterwards, restart the API
and retrain (``hpp_cb --no-ofsted`` reproduces a model without them).

Revision ID: 0034
Revises: 0033
"""

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations.chain import load_revision, no_downgrade, run_concurrently, vacuum_analyze
from hpldn.backend.migrations.london_houses_view import VIEW_COLUMNS, rebuild
from hpldn.backend.scripts import ofsted_features as of
from hpldn.backend.scripts import pipeline_steps as steps
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
)

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

SPELLS_TABLE = "staging._ofsted_feature_spells"
TARGET = "production.ofsted_features"

# Concurrent chunks: the server runs at most a few dozen backends.
MAX_JOBS = 16

# Guards, from the build on hpdbldn (measured in the comments).
# Sales since 2008 with a primary school rated within 3 km (99.9 %).
MIN_PRIMARY_COVERAGE_SINCE_2008 = 0.98
# ... and with a secondary (99.7 %).
MIN_SECONDARY_COVERAGE_SINCE_2008 = 0.97

SPELLS_SQL = f"""
DROP TABLE IF EXISTS {SPELLS_TABLE};
CREATE TABLE {SPELLS_TABLE} AS
SELECT s.id, s.phase, s.geom, daterange(s.valid_from, s.valid_to) AS validity,
       s.valid_from, s.valid_to,
       s.rating_score::double precision AS score, s.grade_basis, s.inspection_date,
       s.phase = ANY(CAST(:primary_phases AS text[])) AS is_primary,
       s.phase = ANY(CAST(:secondary_phases AS text[])) AS is_secondary
  FROM production.ofsted_rating_spells s
 WHERE s.phase = ANY(CAST(:feature_phases AS text[])) AND s.geom IS NOT NULL;
CREATE INDEX ON {SPELLS_TABLE} USING gist (geom, validity);
ANALYZE {SPELLS_TABLE};
"""

TARGET_DDL = f"""
DROP TABLE IF EXISTS {TARGET} CASCADE;
CREATE TABLE {TARGET} (
    ppd_id                              bigint NOT NULL,
    ofsted_primary_nearest_m            double precision,
    ofsted_primary_nearest_score        double precision,
    ofsted_primary_nearest_age_years    double precision,
    ofsted_secondary_nearest_m          double precision,
    ofsted_secondary_nearest_score      double precision,
    ofsted_secondary_nearest_age_years  double precision,
    ofsted_primary_good_plus_1km        smallint NOT NULL,
    ofsted_primary_outstanding_1km      smallint NOT NULL,
    ofsted_secondary_good_plus_2km      smallint NOT NULL,
    ofsted_primary_idw_score_1500m      double precision,
    ofsted_secondary_idw_score_3km      double precision,
    ofsted_nearest_primary_basis        text,
    ofsted_primary_spell_id             bigint,
    ofsted_secondary_spell_id           bigint
);
COMMENT ON TABLE {TARGET} IS
    'Ofsted ratings of the schools near each sale, as published on its date (migration 0034; definitions in scripts/ofsted_features.py). Contains public sector information licensed under the Open Government Licence v3.0.';
COMMENT ON COLUMN {TARGET}.ofsted_primary_spell_id IS
    'production.ofsted_rating_spells.id of the nearest primary; for the leakage guard';
COMMENT ON COLUMN {TARGET}.ofsted_secondary_spell_id IS
    'production.ofsted_rating_spells.id of the nearest secondary; for the leakage guard';
"""

# One GiST probe per sale finds every school in force and in range; the
# nearest of each phase is the first by (distance, spell id), as in
# `ofsted_features.compute`.
INSERT_SQL = f"""
INSERT INTO {TARGET}
SELECT p.id,
       f.primary_m, f.primary_score, (p.dateoftransfer - f.primary_inspected) / :days_per_year,
       f.secondary_m, f.secondary_score, (p.dateoftransfer - f.secondary_inspected) / :days_per_year,
       coalesce(f.primary_good_plus, 0), coalesce(f.primary_outstanding, 0), coalesce(f.secondary_good_plus, 0),
       f.primary_idw, f.secondary_idw, f.primary_basis,
       f.primary_spell, f.secondary_spell
  FROM production.ppd_table p
  JOIN production.address_coordinates ac ON ac.address_id = p.address_id
 CROSS JOIN LATERAL (
       SELECT ST_Transform(ST_SetSRID(ST_MakePoint(ac.longitude, ac.latitude), 4326), 27700) AS pt
 ) g
  LEFT JOIN LATERAL (
       SELECT (array_agg(x.d ORDER BY x.d, x.id) FILTER (WHERE x.is_primary))[1] AS primary_m,
              (array_agg(x.score ORDER BY x.d, x.id) FILTER (WHERE x.is_primary))[1] AS primary_score,
              (array_agg(x.inspection_date ORDER BY x.d, x.id) FILTER (WHERE x.is_primary))[1] AS primary_inspected,
              (array_agg(x.grade_basis ORDER BY x.d, x.id) FILTER (WHERE x.is_primary))[1] AS primary_basis,
              (array_agg(x.id ORDER BY x.d, x.id) FILTER (WHERE x.is_primary))[1] AS primary_spell,
              (array_agg(x.d ORDER BY x.d, x.id) FILTER (WHERE x.is_secondary))[1] AS secondary_m,
              (array_agg(x.score ORDER BY x.d, x.id) FILTER (WHERE x.is_secondary))[1] AS secondary_score,
              (array_agg(x.inspection_date ORDER BY x.d, x.id) FILTER (WHERE x.is_secondary))[1] AS secondary_inspected,
              (array_agg(x.id ORDER BY x.d, x.id) FILTER (WHERE x.is_secondary))[1] AS secondary_spell,
              count(*) FILTER (WHERE x.is_primary AND x.d <= :primary_count_radius_m
                                 AND x.score < :good_plus_below) AS primary_good_plus,
              count(*) FILTER (WHERE x.is_primary AND x.d <= :primary_count_radius_m
                                 AND x.score < :outstanding_below) AS primary_outstanding,
              count(*) FILTER (WHERE x.is_secondary AND x.d <= :secondary_count_radius_m
                                 AND x.score < :good_plus_below) AS secondary_good_plus,
              sum(x.score * x.w) FILTER (WHERE x.is_primary AND x.d <= :primary_idw_radius_m)
                / sum(x.w) FILTER (WHERE x.is_primary AND x.d <= :primary_idw_radius_m) AS primary_idw,
              sum(x.score * x.w) FILTER (WHERE x.is_secondary AND x.d <= :secondary_idw_radius_m)
                / sum(x.w) FILTER (WHERE x.is_secondary AND x.d <= :secondary_idw_radius_m) AS secondary_idw
         FROM (
              SELECT s.id, s.is_primary, s.is_secondary, s.score, s.grade_basis, s.inspection_date, d.d,
                     power(greatest(d.d, :idw_min_distance_m), -:idw_power) AS w
                FROM {SPELLS_TABLE} s
               CROSS JOIN LATERAL (SELECT ST_Distance(s.geom, g.pt) AS d) d
               WHERE s.validity @> p.dateoftransfer
                 AND ST_DWithin(s.geom, g.pt, :search_radius_m)
         ) x
  ) f ON true
 WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
   AND mod(mod(p.id, :jobs) + :jobs, :jobs) = :chunk
"""

# A nearest spell the sale should not have seen: unpublished, or ended, on its date.
GUARD_SQL = f"""
SELECT count(*) FROM {TARGET} f
  JOIN production.ppd_table p ON p.id = f.ppd_id
  JOIN production.ofsted_rating_spells s ON s.id IN (f.ofsted_primary_spell_id, f.ofsted_secondary_spell_id)
 WHERE s.valid_from > p.dateoftransfer
    OR (s.valid_to IS NOT NULL AND s.valid_to <= p.dateoftransfer)
"""

COVERAGE_SQL = f"""
SELECT count(*) AS sales,
       count(f.ofsted_primary_nearest_m) AS with_primary,
       count(f.ofsted_secondary_nearest_m) AS with_secondary,
       avg(f.ofsted_primary_nearest_m) AS mean_primary_m,
       avg(f.ofsted_primary_nearest_score) AS mean_primary_score,
       avg(f.ofsted_primary_good_plus_1km) AS mean_primary_good_plus_1km
  FROM production.ppd_table p
  JOIN {TARGET} f ON f.ppd_id = p.id
 WHERE p.dateoftransfer >= DATE '2008-01-01'
"""


def _run_chunk(engine, cfg, log, chunk: int, jobs: int) -> int:
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        return connection.execute(text(INSERT_SQL), {**of.sql_params(), "jobs": jobs, "chunk": chunk}).rowcount


def build_features(engine, cfg, log) -> int:
    """Create and fill ``production.ofsted_features``; return its row count."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(SPELLS_SQL), of.sql_params())
        connection.execute(text(TARGET_DDL))
    jobs = max(1, min(steps.get_spatial_join_jobs(log=log), MAX_JOBS))
    log.info("0034: ofsted_features in %d concurrent chunk(s)", jobs)
    chunk_engine = create_engine(engine.url, poolclass=NullPool)
    try:
        inserted = run_concurrently(
            {f"ofsted_features chunk {chunk}": (lambda c=chunk: _run_chunk(chunk_engine, cfg, log, c, jobs))
             for chunk in range(jobs)},
            log,
        )
    except Exception:
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {TARGET} CASCADE"))
        raise
    finally:
        chunk_engine.dispose()
    with engine.begin() as connection:
        connection.execute(text(f"CREATE UNIQUE INDEX idx_prod_ofsted_features_ppd_id ON {TARGET} (ppd_id)"))
        connection.execute(text(f"ANALYZE {TARGET}"))
        connection.execute(text(f"DROP TABLE IF EXISTS {SPELLS_TABLE}"))
    rows = sum(inserted.values())
    log.info("0034: production.ofsted_features holds %d sale(s)", rows)
    return rows


def check_features(engine, log) -> None:
    """Fail on any sale that saw an unpublished or ended rating, or on thin coverage."""
    with engine.connect() as connection:
        leaks = connection.execute(text(GUARD_SQL)).scalar_one()
        coverage = connection.execute(text(COVERAGE_SQL)).mappings().one()
    log.info("0034: since 2008 %s", dict(coverage))
    failing = []
    if leaks:
        failing.append(f"{leaks} sale(s) use a spell not in force on their date")
    for column, floor in (("with_primary", MIN_PRIMARY_COVERAGE_SINCE_2008),
                          ("with_secondary", MIN_SECONDARY_COVERAGE_SINCE_2008)):
        share = coverage[column] / coverage["sales"] if coverage["sales"] else 0.0
        if share < floor:
            failing.append(f"{share:.1%} of sales since 2008 {column}, expected >= {floor:.0%}")
    if failing:
        raise AssertionError("0034: production.ofsted_features fails its checks: " + "; ".join(failing))
    log.info("0034: leakage guard passed (0 spells used before publication or after their end)")


def upgrade() -> None:
    """Build the per-sale Ofsted features, check them, then rebuild the view with them."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    view = load_revision("0016")

    build_features(engine, cfg, log)
    check_features(engine, log)

    log.info("0034: rebuilding production.london_houses with the Ofsted columns")
    rows = rebuild(engine, cfg, log)
    view._check_columns(engine, log, expected_columns=VIEW_COLUMNS)
    vacuum_analyze(engine, (TARGET, "production.london_houses"), log)
    log.info(
        "0034: complete (%d view row(s)). Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  restart the API (its caches read the view)\n"
        "  retrain hpp_cb (and compare with --no-ofsted) before swapping model.cbm",
        rows,
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
