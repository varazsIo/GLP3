"""
Load Ofsted school inspections 2005-2026 into ``production.ofsted`` and its rating spells.

School quality moves London house prices, and the model had no school data.
This revision lands the files ``scripts/fetch_ofsted.py`` wrote under
``data/ofsted/`` (state-funded school inspections September 2005 - August
2026 and DfE Get Information about Schools; see `hpldn.backend.migrations.ofsted`)
and cleans them in SQL:

``staging.ofsted_releases``
    One row per file: URL, sha256, published date, coverage, era (from
    ``manifest.json``).
``staging.ofsted_inspections_raw``
    Every inspection row of every file as text, under the common column names
    of `ofsted.RAW_COLUMNS`, with ``source_file``, ``source_row`` and ``era``.
``staging.gias_establishments_raw`` / ``staging.gias_links_raw``
    Every establishment (closed ones included) and predecessor/successor link.
``production.ofsted``
    One row per inspection of a school in London or within
    `ofsted.BOUNDARY_BUFFER_M` of it: dates, the inspection type
    (``graded``, ``ungraded``, ``report_card``, ``monitoring``, ``other``),
    the judgements, a harmonised ``rating_score`` from 1 (best) to 4 and the
    ``grade_basis`` it was set on (``overall`` effectiveness to August 2024,
    the mean of the four ``key_judgements`` September 2024 - October 2025, the
    mean of the six ``report_card`` areas from November 2025, 4 when
    safeguarding is not met), the school's phase and location.
    An inspection a year's file missed (published after its release) is
    filled from the cross-check file and from the next inspection's
    "previous inspection" columns; duplicates keep the newest release's row.
``production.ofsted_rating_spells``
    The rating in force at each school over time: from the publication date
    of a graded inspection or report card to the next one (or the school's
    closure). An ungraded inspection confirms the score in force
    (``grade_basis = 'carried'``) and resets its age. A successor URN (an
    academy conversion, from the GIAS links) inherits its predecessor's
    rating from opening until its own first publication, as Ofsted's own
    statistics do.

Monitoring visits stay in ``production.ofsted`` but never set a rating.
Everything is dropped and rebuilt, so the revision can be re-run; all tables
are logged (a crash empties UNLOGGED ones). Run
``src/hpldn/backend/db/sql/db_grants_post_migration.sql`` afterwards.

Revision ID: 0033
Revises: 0032
"""

from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from hpldn.backend.common.tuning import apply_bulk_session_tuning
from hpldn.backend.migrations import ofsted
from hpldn.backend.migrations.chain import no_downgrade, vacuum_analyze
from hpldn.backend.scripts.migration_helpers import (
    get_migration_cfg,
    get_migration_engine,
    get_migration_logger,
    require_data_path,
)

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

TO_SQL_CHUNK_ROWS = 5000

# Guards, set from the build on hpdbldn (measured value in the comment) with
# room for noise.
# London inspections of every type per academic year (435-892), the
# pandemic years (2019/20: 327, 2020/21: 87) and the year in progress (2025/26:
# 234, report cards from November after an autumn pause) apart.
LONDON_ROWS_PER_YEAR = (350, 1200)
LONDON_ROWS_SPECIAL_YEARS = {2019: (200, 700), 2020: (20, 300), 2025: (120, 900)}
# Authorities with at least one inspection in an academic year (32-33 of 33;
# 28 in 2020/21, 31 in 2025/26).
MIN_LAS_PER_YEAR = 31
MIN_LAS_SPECIAL_YEAR = 20
# Share of rated inspections in each harmonised band, per era: [min, max].
# Measured: outstanding 14-41 %, good 49-81 % (report cards: strong and
# expected standard), requires improvement 4-28 %, inadequate 0-6 %.
RATING_SHARE_BOUNDS = {
    "outstanding": (0.05, 0.55),
    "good": (0.35, 0.90),
    "requires_improvement": (0.01, 0.40),
    "inadequate": (0.0, 0.12),
}
# Schools with a spell must have a location (measured 100 %).
MIN_LOCATED_SHARE = 0.99
# 15,302 spells, 1,162 inherited by successor schools.
MIN_SPELLS = 13_000
MIN_INHERITED_SPELLS = 800

DATE_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION staging.hpldn_ofsted_date(value text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v text := btrim(value);
BEGIN
    IF v IS NULL OR v = '' THEN
        RETURN NULL;
    ELSIF v ~ '^\d{1,2}/\d{1,2}/\d{4}$' THEN
        RETURN to_date(v, 'DD/MM/YYYY');
    ELSIF v ~ '^\d{1,2}-\d{1,2}-\d{4}$' THEN
        RETURN to_date(v, 'DD-MM-YYYY');
    ELSIF v ~ '^\d{4}-\d{2}-\d{2}' THEN
        RETURN substr(v, 1, 10)::date;
    ELSIF v ~ '^\d{5}(\.\d+)?$' THEN
        -- Excel serial day (the 2005-2015 workbook).
        RETURN date '1899-12-30' + floor(v::numeric)::int;
    END IF;
    RETURN NULL;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;
"""

GRADE_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION staging.hpldn_ofsted_grade(value text) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN btrim(value) ~ '^[1-4](\.0+)?$' THEN btrim(value)::numeric::int
        ELSE CASE lower(btrim(value))
            WHEN 'outstanding' THEN 1
            WHEN 'good' THEN 2
            WHEN 'satisfactory' THEN 3
            WHEN 'requires improvement' THEN 3
            WHEN 'inadequate' THEN 4
            WHEN 'exceptional' THEN 1
            WHEN 'strong standard' THEN 1.5
            WHEN 'expected standard' THEN 2
            WHEN 'needs attention' THEN 3
            WHEN 'urgent improvement' THEN 4
        END
    END::numeric
$$;
"""

STAGING_DDL = """
DROP TABLE IF EXISTS staging.ofsted_releases;
CREATE TABLE staging.ofsted_releases (
    source_file     text PRIMARY KEY,
    file_key        text NOT NULL UNIQUE,
    url             text NOT NULL,
    sha256          text NOT NULL,
    published_date  date,
    coverage_from   date NOT NULL,
    coverage_to     date NOT NULL,
    era             text NOT NULL,
    row_count       integer NOT NULL
);
COMMENT ON TABLE staging.ofsted_releases IS
    'Ofsted and GIAS files loaded by migration 0033, from data/ofsted/manifest.json (OGL v3.0)';
"""

ERA_CASE_SQL = " ".join(
    f"WHEN e.start_date >= DATE '{start}' THEN '{name}'"
    for name, start in reversed(ofsted.ERA_STARTS)
)

# Every inspection the files hold: their own rows, the latest inspections the
# cross-check file carries, and each row's "previous inspection" when graded.
EVENTS_SQL = f"""
DROP TABLE IF EXISTS staging._ofsted_events;
CREATE TABLE staging._ofsted_events AS
WITH files AS (
    SELECT source_file, era,
           rank() OVER (ORDER BY published_date DESC NULLS LAST, coverage_to DESC) AS release_rank
      FROM staging.ofsted_releases
),
raw AS (
    SELECT r.*, f.release_rank FROM staging.ofsted_inspections_raw r JOIN files f USING (source_file)
),
unioned AS (
    -- The files' own rows.
    SELECT source_file, source_row, era AS file_era, release_rank, 1 AS origin_rank, 'direct' AS origin,
           urn, inspection_number, inspection_type, inspection_type_grouping, event_type_grouping,
           inspection_start_date, inspection_end_date, publication_date,
           overall_effectiveness, category_of_concern, quality_of_education, behaviour_and_attitudes,
           personal_development, leadership_and_management, safeguarding, ungraded_outcome,
           rc_inclusion, rc_curriculum_and_teaching, rc_achievement, rc_attendance_and_behaviour,
           rc_personal_development, rc_leadership_and_governance,
           school_name, ofsted_phase, postcode, predecessor_urn
      FROM raw WHERE era <> 'cross_check'
    UNION ALL
    -- Latest report card, from the cross-check file.
    SELECT source_file, source_row, era, release_rank, 2, 'cross_check',
           coalesce(nullif(inspected_urn, 'NULL'), urn), inspection_number, inspection_type,
           inspection_type_grouping, event_type_grouping,
           inspection_start_date, NULL, publication_date,
           NULL, category_of_concern, NULL, NULL, NULL, NULL, safeguarding, NULL,
           rc_inclusion, rc_curriculum_and_teaching, rc_achievement, rc_attendance_and_behaviour,
           rc_personal_development, rc_leadership_and_governance,
           school_name, ofsted_phase, postcode, NULL
      FROM raw WHERE era = 'cross_check' AND nullif(inspection_number, 'NULL') IS NOT NULL
    UNION ALL
    -- Latest graded inspection under the 2019 framework, from the cross-check file.
    SELECT source_file, source_row, era, release_rank, 2, 'cross_check',
           coalesce(nullif(oeif_urn, 'NULL'), urn), oeif_inspection_number, oeif_inspection_type,
           oeif_inspection_type, oeif_event_type_grouping,
           oeif_inspection_start_date, NULL, oeif_publication_date,
           oeif_overall_effectiveness, oeif_category_of_concern, oeif_quality_of_education,
           oeif_behaviour_and_attitudes, oeif_personal_development, oeif_leadership_and_management,
           oeif_safeguarding, NULL,
           NULL, NULL, NULL, NULL, NULL, NULL,
           school_name, ofsted_phase, postcode, NULL
      FROM raw WHERE era = 'cross_check' AND nullif(oeif_inspection_number, 'NULL') IS NOT NULL
    UNION ALL
    -- Latest ungraded inspection, from the cross-check file.
    SELECT source_file, source_row, era, release_rank, 2, 'cross_check',
           coalesce(nullif(ug_urn, 'NULL'), urn), ug_inspection_number, 'Ungraded inspection',
           'Ungraded inspection', NULL,
           ug_inspection_start_date, NULL, ug_publication_date,
           NULL, NULL, NULL, NULL, NULL, NULL, NULL, ug_outcome,
           NULL, NULL, NULL, NULL, NULL, NULL,
           school_name, ofsted_phase, postcode, NULL
      FROM raw WHERE era = 'cross_check' AND nullif(ug_inspection_number, 'NULL') IS NOT NULL
    UNION ALL
    -- The graded inspection before each row.
    SELECT source_file, source_row, era, release_rank, 3, 'previous',
           coalesce(nullif(prev_urn, 'NULL'), urn), prev_inspection_number, 'Previous graded inspection',
           NULL, NULL,
           prev_inspection_start_date, prev_inspection_end_date, prev_publication_date,
           prev_overall_effectiveness, prev_category_of_concern, prev_quality_of_education,
           prev_behaviour_and_attitudes, prev_personal_development, prev_leadership_and_management,
           NULL, NULL,
           NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL, NULL, NULL
      FROM raw
     WHERE nullif(prev_inspection_number, 'NULL') IS NOT NULL
       AND (staging.hpldn_ofsted_grade(prev_overall_effectiveness) IS NOT NULL
            OR staging.hpldn_ofsted_grade(prev_quality_of_education) IS NOT NULL)
),
parsed AS (
    SELECT u.*,
           CASE WHEN btrim(u.urn) ~ '^\\d+$' THEN btrim(u.urn)::int END AS urn_int,
           nullif(nullif(upper(btrim(u.inspection_number)), ''), 'NULL') AS inspection_key,
           staging.hpldn_ofsted_date(u.inspection_start_date) AS start_date,
           staging.hpldn_ofsted_date(u.inspection_end_date) AS end_date,
           staging.hpldn_ofsted_date(u.publication_date) AS pub_date,
           staging.hpldn_ofsted_grade(u.overall_effectiveness) AS g_overall,
           staging.hpldn_ofsted_grade(u.quality_of_education) AS g_quality,
           staging.hpldn_ofsted_grade(u.behaviour_and_attitudes) AS g_behaviour,
           staging.hpldn_ofsted_grade(u.personal_development) AS g_personal,
           staging.hpldn_ofsted_grade(u.leadership_and_management) AS g_leadership,
           staging.hpldn_ofsted_grade(u.rc_inclusion) AS rc1,
           staging.hpldn_ofsted_grade(u.rc_curriculum_and_teaching) AS rc2,
           staging.hpldn_ofsted_grade(u.rc_achievement) AS rc3,
           staging.hpldn_ofsted_grade(u.rc_attendance_and_behaviour) AS rc4,
           staging.hpldn_ofsted_grade(u.rc_personal_development) AS rc5,
           staging.hpldn_ofsted_grade(u.rc_leadership_and_governance) AS rc6,
           CASE
               WHEN lower(btrim(u.safeguarding)) IN ('yes', 'met') THEN true
               WHEN lower(btrim(u.safeguarding)) IN ('no', 'not met') THEN false
           END AS safeguarding_met,
           concat_ws(' | ', u.inspection_type, u.inspection_type_grouping, u.event_type_grouping) AS type_text
      FROM unioned u
)
SELECT p.*,
       num_nonnulls(p.g_quality, p.g_behaviour, p.g_personal, p.g_leadership) AS kj_count,
       num_nonnulls(p.rc1, p.rc2, p.rc3, p.rc4, p.rc5, p.rc6) AS rc_count
  FROM parsed p
 WHERE p.urn_int IS NOT NULL
   AND p.start_date BETWEEN DATE '{ofsted.FIRST_INSPECTION}' AND DATE '{ofsted.LAST_INSPECTION}';
"""

CLASSIFY_SQL = f"""
DROP TABLE IF EXISTS staging._ofsted_classified;
CREATE TABLE staging._ofsted_classified AS
WITH typed AS (
    SELECT e.*,
           CASE {ERA_CASE_SQL} END AS era,
           CASE
               WHEN e.rc_count >= 4 THEN 'report_card'
               WHEN e.g_overall IS NOT NULL THEN 'graded'
               WHEN e.kj_count >= 3 AND e.start_date >= DATE '2024-09-01' THEN 'graded'
               WHEN e.origin = 'previous' THEN 'other'
               WHEN e.type_text ~* 'monitor|visit|no formal designation|parental complaint|legacy' THEN 'monitoring'
               WHEN nullif(btrim(e.ungraded_outcome), 'NULL') IS NOT NULL
                    OR e.type_text ~* 'section 8|short inspection|ungraded|s8' THEN 'ungraded'
               ELSE 'other'
           END AS kind
      FROM staging._ofsted_events e
)
SELECT t.*,
       CASE
           WHEN t.kind = 'report_card' AND t.safeguarding_met IS FALSE THEN 4
           WHEN t.kind = 'report_card' THEN
               round((coalesce(t.rc1, 0) + coalesce(t.rc2, 0) + coalesce(t.rc3, 0) + coalesce(t.rc4, 0)
                      + coalesce(t.rc5, 0) + coalesce(t.rc6, 0)) / t.rc_count, 2)
           WHEN t.kind = 'graded' AND t.start_date < DATE '2024-09-01' THEN t.g_overall
           WHEN t.kind = 'graded' AND t.kj_count >= 3 THEN
               round((coalesce(t.g_quality, 0) + coalesce(t.g_behaviour, 0) + coalesce(t.g_personal, 0)
                      + coalesce(t.g_leadership, 0)) / t.kj_count, 2)
           WHEN t.kind = 'graded' THEN t.g_overall
       END AS rating_score,
       CASE
           WHEN t.kind = 'report_card' THEN 'report_card'
           WHEN t.kind = 'graded' AND t.start_date < DATE '2024-09-01' THEN 'overall'
           WHEN t.kind = 'graded' AND t.kj_count >= 3 THEN 'key_judgements'
           WHEN t.kind = 'graded' THEN 'overall'
           WHEN t.kind = 'ungraded' THEN 'carried'
       END AS grade_basis,
       CASE WHEN t.kind = 'ungraded' THEN
           CASE
               WHEN t.ungraded_outcome ~* 'concern|not as strong' THEN 'concerns'
               WHEN t.ungraded_outcome ~* '\\(improving\\)|improved significantly' THEN 'may_improve'
               WHEN t.ungraded_outcome ~* 'remains|maintained|^\\s*yes' THEN 'remains'
           END
       END AS ungraded_result,
       -- The grade an ungraded outcome names ("School remains Good"), for a
       -- school with no earlier rating to carry.
       CASE WHEN t.kind = 'ungraded' THEN
           CASE WHEN t.ungraded_outcome ~* 'outstanding' THEN 1 WHEN t.ungraded_outcome ~* 'good' THEN 2 END
       END AS implied_score
  FROM typed t;
"""

DEDUPE_SQL = """
DROP TABLE IF EXISTS staging._ofsted_deduped;
CREATE TABLE staging._ofsted_deduped AS
WITH by_number AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY coalesce(c.inspection_key, c.urn_int || '|' || c.start_date || '|' || c.kind)
               ORDER BY c.origin_rank, (c.kind IN ('graded', 'report_card')) DESC, c.release_rank, c.source_row
           ) AS number_rank
      FROM staging._ofsted_classified c
),
kept AS (SELECT * FROM by_number WHERE number_rank = 1)
SELECT k.*
  FROM (
    SELECT kept.*,
           row_number() OVER (
               PARTITION BY kept.urn_int, kept.start_date, kept.kind
               ORDER BY kept.origin_rank, kept.release_rank, kept.inspection_key NULLS LAST, kept.source_row
           ) AS key_rank
      FROM kept
  ) k
 WHERE k.key_rank = 1;
"""

CONFLICTS_SQL = """
SELECT count(*) FROM (
    SELECT inspection_key
      FROM staging._ofsted_classified
     WHERE inspection_key IS NOT NULL AND rating_score IS NOT NULL
     GROUP BY inspection_key
    HAVING count(DISTINCT rating_score) > 1
) d
"""

GLA_BOUNDARY_SQL = f"""
DROP TABLE IF EXISTS staging._ofsted_gla_buffer;
CREATE TABLE staging._ofsted_gla_buffer AS
SELECT ST_Subdivide(ST_Buffer(ST_Union(ST_Transform(geom, 27700)), {ofsted.BOUNDARY_BUFFER_M}), 256) AS geom
  FROM staging.lsoa_boundaries;
CREATE INDEX ON staging._ofsted_gla_buffer USING gist (geom);
"""

LONDON_LA_LIST = ", ".join(str(code) for code in ofsted.LONDON_LA_CODES)

# One row per school URN (GIAS's and any the inspections name that it lacks):
# its phase, location and dates.
SCHOOLS_SQL = f"""
DROP TABLE IF EXISTS staging._ofsted_schools;
CREATE TABLE staging._ofsted_schools AS
WITH gias AS (
    SELECT btrim(g.urn)::int AS urn,
           nullif(btrim(g.la_code), '')::int AS la_code,
           nullif(btrim(g.la_name), '') AS la_name,
           nullif(btrim(g.school_name), '') AS school_name,
           nullif(btrim(g.establishment_type), '') AS establishment_type,
           nullif(btrim(g.phase_of_education), '') AS phase_of_education,
           nullif(btrim(g.status), '') AS status,
           staging.hpldn_ofsted_date(g.open_date) AS open_date,
           staging.hpldn_ofsted_date(g.close_date) AS close_date,
           CASE WHEN btrim(g.statutory_low_age) ~ '^\\d+$' THEN btrim(g.statutory_low_age)::smallint END AS low_age,
           CASE WHEN btrim(g.statutory_high_age) ~ '^\\d+$' THEN btrim(g.statutory_high_age)::smallint END AS high_age,
           nullif(btrim(g.postcode), '') AS postcode,
           CASE WHEN btrim(g.easting) ~ '^\\d+$' AND btrim(g.easting)::int > 0 THEN btrim(g.easting)::int END AS easting,
           CASE WHEN btrim(g.northing) ~ '^\\d+$' AND btrim(g.northing)::int > 0 THEN btrim(g.northing)::int END AS northing
      FROM staging.gias_establishments_raw g
     WHERE btrim(g.urn) ~ '^\\d+$'
),
latest_event AS (
    SELECT DISTINCT ON (urn_int) urn_int AS urn, school_name, ofsted_phase, postcode
      FROM staging._ofsted_deduped
     WHERE origin <> 'previous'
     ORDER BY urn_int, start_date DESC
),
schools AS (
    SELECT coalesce(g.urn, l.urn) AS urn,
           g.la_code, g.la_name,
           coalesce(g.school_name, l.school_name) AS school_name,
           g.establishment_type, g.status, g.open_date, g.close_date, g.low_age, g.high_age,
           coalesce(g.postcode, nullif(l.postcode, 'NULL')) AS postcode,
           g.easting, g.northing,
           CASE
               WHEN g.establishment_type ~* 'pupil referral|alternative provision|secure' THEN 'pru'
               WHEN g.establishment_type ~* 'special' AND g.establishment_type !~* 'post 16' THEN 'special'
               WHEN g.phase_of_education ~* 'nursery' OR g.establishment_type ~* 'nursery' THEN 'nursery'
               WHEN g.phase_of_education ~* 'all-through' THEN 'all_through'
               WHEN g.phase_of_education ~* 'primary' THEN 'primary'
               WHEN g.phase_of_education ~* 'secondary' THEN 'secondary'
               WHEN g.phase_of_education ~* '16 plus' OR g.establishment_type ~* '16' THEN 'post16'
               WHEN l.ofsted_phase ~* 'pru|alternative' THEN 'pru'
               WHEN l.ofsted_phase ~* 'special' THEN 'special'
               WHEN l.ofsted_phase ~* 'nursery' THEN 'nursery'
               WHEN l.ofsted_phase ~* 'all.through' THEN 'all_through'
               WHEN l.ofsted_phase ~* 'primary' THEN 'primary'
               WHEN l.ofsted_phase ~* 'secondary' THEN 'secondary'
               WHEN l.ofsted_phase ~* '16' THEN 'post16'
           END AS phase
      FROM gias g
      FULL JOIN latest_event l ON l.urn = g.urn
),
located AS (
    SELECT s.*,
           CASE
               WHEN s.easting IS NOT NULL THEN ST_SetSRID(ST_MakePoint(s.easting, s.northing), 27700)
               WHEN pch.geom IS NOT NULL THEN ST_SetSRID(pch.geom, 27700)
               WHEN cpo.eastings IS NOT NULL THEN ST_SetSRID(ST_MakePoint(cpo.eastings, cpo.northings), 27700)
           END AS geom,
           CASE
               WHEN s.easting IS NOT NULL THEN 'gias'
               WHEN pch.geom IS NOT NULL THEN 'postcode_centroid_history'
               WHEN cpo.eastings IS NOT NULL THEN 'codepoint_open'
           END AS location_source
      FROM schools s
      LEFT JOIN staging.postcode_centroid_history pch
        ON s.easting IS NULL AND pch.postcode_norm = staging.hpldn_pcnorm(s.postcode)
      LEFT JOIN LATERAL (
          SELECT c.eastings, c.northings FROM staging.codepoint_open c
           WHERE s.easting IS NULL AND pch.geom IS NULL
             AND staging.hpldn_pcnorm(c.postcode) = staging.hpldn_pcnorm(s.postcode)
             AND c.eastings > 0
           LIMIT 1
      ) cpo ON true
)
SELECT l.*,
       ST_Y(ST_Transform(l.geom, 4326)) AS latitude,
       ST_X(ST_Transform(l.geom, 4326)) AS longitude,
       (l.la_code IN ({LONDON_LA_LIST})) AS in_london,
       (l.la_code IN ({LONDON_LA_LIST})
        OR EXISTS (SELECT 1 FROM staging._ofsted_gla_buffer b WHERE ST_Intersects(b.geom, l.geom))) AS in_scope
  FROM located l;
CREATE UNIQUE INDEX ON staging._ofsted_schools (urn);
"""

PRODUCTION_OFSTED_SQL = f"""
DROP TABLE IF EXISTS production.ofsted_rating_spells;
DROP TABLE IF EXISTS production.ofsted;
CREATE TABLE production.ofsted (
    id                        bigserial PRIMARY KEY,
    urn                       integer NOT NULL,
    inspection_number         text,
    inspection_type           text NOT NULL
        CHECK (inspection_type IN ('graded', 'ungraded', 'report_card', 'monitoring', 'other')),
    inspection_type_raw       text,
    era                       text NOT NULL,
    inspection_start_date     date NOT NULL,
    inspection_end_date       date,
    publication_date          date NOT NULL,
    publication_date_imputed  boolean NOT NULL,
    overall_effectiveness     smallint,
    quality_of_education      smallint,
    behaviour_and_attitudes   smallint,
    personal_development      smallint,
    leadership_and_management smallint,
    rc_inclusion              numeric(2,1),
    rc_curriculum_and_teaching numeric(2,1),
    rc_achievement            numeric(2,1),
    rc_attendance_and_behaviour numeric(2,1),
    rc_personal_development   numeric(2,1),
    rc_leadership_and_governance numeric(2,1),
    safeguarding_met          boolean,
    category_of_concern       text,
    ungraded_outcome          text CHECK (ungraded_outcome IN ('remains', 'may_improve', 'concerns')),
    ungraded_outcome_raw      text,
    rating_score              numeric(3,2) CHECK (rating_score BETWEEN 1 AND 4),
    grade_basis               text CHECK (grade_basis IN ('overall', 'key_judgements', 'report_card', 'carried')),
    rating_label              text,
    school_name               text,
    phase                     text,
    establishment_type        text,
    la_code                   smallint,
    la_name                   text,
    postcode                  text,
    open_date                 date,
    close_date                date,
    statutory_low_age         smallint,
    statutory_high_age        smallint,
    location_source           text,
    geom                      geometry(Point, 27700),
    latitude                  double precision,
    longitude                 double precision,
    origin                    text NOT NULL CHECK (origin IN ('direct', 'cross_check', 'previous')),
    source_file               text NOT NULL,
    source_row                integer NOT NULL,
    source                    text NOT NULL,
    UNIQUE (urn, inspection_start_date, inspection_type),
    CHECK (publication_date >= coalesce(inspection_end_date, inspection_start_date))
);
INSERT INTO production.ofsted (
    urn, inspection_number, inspection_type, inspection_type_raw, era,
    inspection_start_date, inspection_end_date, publication_date, publication_date_imputed,
    overall_effectiveness, quality_of_education, behaviour_and_attitudes, personal_development,
    leadership_and_management,
    rc_inclusion, rc_curriculum_and_teaching, rc_achievement, rc_attendance_and_behaviour,
    rc_personal_development, rc_leadership_and_governance,
    safeguarding_met, category_of_concern, ungraded_outcome, ungraded_outcome_raw,
    rating_score, grade_basis, rating_label,
    school_name, phase, establishment_type, la_code, la_name, postcode, open_date, close_date,
    statutory_low_age, statutory_high_age, location_source, geom, latitude, longitude,
    origin, source_file, source_row, source
)
SELECT d.urn_int, d.inspection_key, d.kind, nullif(d.type_text, ''), d.era,
       d.start_date, d.end_date,
       CASE WHEN d.pub_date >= coalesce(d.end_date, d.start_date) THEN d.pub_date
            ELSE coalesce(d.end_date, d.start_date) + {ofsted.IMPUTED_PUBLICATION_LAG_DAYS} END,
       NOT coalesce(d.pub_date >= coalesce(d.end_date, d.start_date), false),
       d.g_overall, d.g_quality, d.g_behaviour, d.g_personal, d.g_leadership,
       d.rc1, d.rc2, d.rc3, d.rc4, d.rc5, d.rc6,
       d.safeguarding_met, nullif(nullif(btrim(d.category_of_concern), ''), 'NULL'),
       d.ungraded_result, nullif(nullif(btrim(d.ungraded_outcome), ''), 'NULL'),
       d.rating_score, d.grade_basis,
       CASE
           WHEN d.rating_score IS NULL THEN NULL
           WHEN d.grade_basis = 'report_card' AND d.safeguarding_met IS FALSE THEN 'Safeguarding not met'
           WHEN d.grade_basis = 'report_card' THEN
               CASE WHEN d.rating_score < 1.25 THEN 'Exceptional'
                    WHEN d.rating_score < 1.75 THEN 'Strong standard'
                    WHEN d.rating_score < 2.5 THEN 'Expected standard'
                    WHEN d.rating_score < 3.5 THEN 'Needs attention'
                    ELSE 'Urgent improvement' END
           WHEN d.rating_score < 1.5 THEN 'Outstanding'
           WHEN d.rating_score < 2.5 THEN 'Good'
           WHEN d.rating_score < 3.5 THEN
               CASE WHEN d.start_date < DATE '{ofsted.REQUIRES_IMPROVEMENT_FROM}' THEN 'Satisfactory'
                    ELSE 'Requires improvement' END
           ELSE 'Inadequate'
       END,
       s.school_name, s.phase, s.establishment_type, s.la_code, s.la_name, s.postcode,
       s.open_date, s.close_date, s.low_age, s.high_age, s.location_source, s.geom, s.latitude, s.longitude,
       d.origin, d.source_file, d.source_row, :source
  FROM staging._ofsted_deduped d
  JOIN staging._ofsted_schools s ON s.urn = d.urn_int
 WHERE s.in_scope;
CREATE INDEX idx_prod_ofsted_urn_publication ON production.ofsted (urn, publication_date);
CREATE INDEX idx_prod_ofsted_geom ON production.ofsted USING gist (geom);
"""

COMMENTS_SQL = """
COMMENT ON TABLE production.ofsted IS
    'Ofsted state-funded school inspections Sep 2005 - Aug 2026 of schools in London or within 3 km of it, with DfE GIAS locations (migration 0033). Contains public sector information licensed under the Open Government Licence v3.0.';
COMMENT ON COLUMN production.ofsted.urn IS 'School unique reference number at the time of the inspection';
COMMENT ON COLUMN production.ofsted.inspection_number IS 'Ofsted inspection number, when the file gave one';
COMMENT ON COLUMN production.ofsted.inspection_type IS 'graded, ungraded (section 8 of a good or outstanding school), report_card (from Nov 2025), monitoring (never sets a rating) or other';
COMMENT ON COLUMN production.ofsted.inspection_type_raw IS 'Ofsted inspection type, type grouping and event type grouping as published';
COMMENT ON COLUMN production.ofsted.era IS 'Inspection framework by start date: pre2015, cif_2015, eif_2019, eif_2024_no_oe, report_card_2025';
COMMENT ON COLUMN production.ofsted.publication_date IS 'Report publication date: when the rating became public';
COMMENT ON COLUMN production.ofsted.publication_date_imputed IS 'True when the file gave no usable publication date: the last inspection day plus 35 days';
COMMENT ON COLUMN production.ofsted.overall_effectiveness IS 'Overall effectiveness 1 (outstanding) to 4 (inadequate); none from Sep 2024';
COMMENT ON COLUMN production.ofsted.quality_of_education IS 'Key judgement 1-4 (2019 framework)';
COMMENT ON COLUMN production.ofsted.behaviour_and_attitudes IS 'Key judgement 1-4 (2019 framework)';
COMMENT ON COLUMN production.ofsted.personal_development IS 'Key judgement 1-4 (2019 framework)';
COMMENT ON COLUMN production.ofsted.leadership_and_management IS 'Leadership and management 1-4';
COMMENT ON COLUMN production.ofsted.rc_inclusion IS 'Report card area: Exceptional 1, Strong standard 1.5, Expected standard 2, Needs attention 3, Urgent improvement 4';
COMMENT ON COLUMN production.ofsted.rc_curriculum_and_teaching IS 'Report card area, scored as rc_inclusion';
COMMENT ON COLUMN production.ofsted.rc_achievement IS 'Report card area, scored as rc_inclusion';
COMMENT ON COLUMN production.ofsted.rc_attendance_and_behaviour IS 'Report card area, scored as rc_inclusion';
COMMENT ON COLUMN production.ofsted.rc_personal_development IS 'Report card area (personal development and wellbeing), scored as rc_inclusion';
COMMENT ON COLUMN production.ofsted.rc_leadership_and_governance IS 'Report card area, scored as rc_inclusion';
COMMENT ON COLUMN production.ofsted.safeguarding_met IS 'Safeguarding effective / standards met';
COMMENT ON COLUMN production.ofsted.ungraded_outcome IS 'Ungraded inspection outcome: remains, may_improve or concerns';
COMMENT ON COLUMN production.ofsted.rating_score IS 'Harmonised rating 1 (best) to 4 on grade_basis; NULL for ungraded, monitoring and other inspections';
COMMENT ON COLUMN production.ofsted.grade_basis IS 'overall (to Aug 2024), key_judgements (mean of four, Sep 2024 - Oct 2025), report_card (mean of six areas, 4 when safeguarding not met), carried (ungraded: confirms the score in force)';
COMMENT ON COLUMN production.ofsted.rating_label IS 'Display label for rating_score in its era''s words';
COMMENT ON COLUMN production.ofsted.phase IS 'primary, secondary, all_through, special, pru, nursery or post16 (GIAS, else Ofsted phase)';
COMMENT ON COLUMN production.ofsted.location_source IS 'gias (easting/northing), postcode_centroid_history or codepoint_open (postcode centroid)';
COMMENT ON COLUMN production.ofsted.geom IS 'School location, British National Grid';
COMMENT ON COLUMN production.ofsted.origin IS 'direct (the file''s own row), cross_check (latest-inspections file) or previous (a later row''s previous-inspection columns)';
COMMENT ON COLUMN production.ofsted.source IS 'Ofsted / DfE GIAS, Open Government Licence v3.0';
"""

# Rated events per school: graded and report-card inspections set the score;
# an ungraded one carries the score in force (or the grade its outcome names).
SPELLS_SQL = """
DROP TABLE IF EXISTS staging._ofsted_spells;
CREATE TABLE staging._ofsted_spells AS
WITH events AS (
    SELECT o.id, o.urn, o.inspection_type, o.inspection_start_date, o.publication_date,
           o.rating_score, o.grade_basis, o.rating_label, o.ungraded_outcome_raw,
           count(o.rating_score) FILTER (WHERE o.inspection_type <> 'ungraded') OVER w AS rated_seen
      FROM production.ofsted o
     WHERE (o.inspection_type IN ('graded', 'report_card') AND o.rating_score IS NOT NULL)
        OR o.inspection_type = 'ungraded'
    WINDOW w AS (PARTITION BY o.urn ORDER BY o.publication_date, o.inspection_start_date, o.id
                 ROWS UNBOUNDED PRECEDING)
),
carried AS (
    SELECT e.*,
           first_value(e.rating_score) OVER g AS group_score,
           first_value(e.rating_label) OVER g AS group_label
      FROM events e
    WINDOW g AS (PARTITION BY e.urn, e.rated_seen ORDER BY e.publication_date, e.inspection_start_date, e.id)
),
scored AS (
    SELECT c.id, c.urn, c.inspection_start_date, c.publication_date,
           CASE WHEN c.inspection_type = 'ungraded' THEN
                CASE WHEN c.rated_seen > 0 THEN c.group_score
                     WHEN c.ungraded_outcome_raw ~* 'outstanding' THEN 1
                     WHEN c.ungraded_outcome_raw ~* 'good' THEN 2 END
                ELSE c.rating_score END AS rating_score,
           CASE WHEN c.inspection_type = 'ungraded' THEN 'carried' ELSE c.grade_basis END AS grade_basis,
           CASE WHEN c.inspection_type = 'ungraded' THEN
                CASE WHEN c.rated_seen > 0 THEN c.group_label
                     WHEN c.ungraded_outcome_raw ~* 'outstanding' THEN 'Outstanding'
                     WHEN c.ungraded_outcome_raw ~* 'good' THEN 'Good' END
                ELSE c.rating_label END AS rating_label,
           (c.inspection_type = 'ungraded') AS is_carried
      FROM carried c
),
one_per_day AS (
    SELECT DISTINCT ON (urn, publication_date) *
      FROM scored
     WHERE rating_score IS NOT NULL
     ORDER BY urn, publication_date, is_carried, inspection_start_date DESC, id DESC
)
SELECT p.urn,
       p.id AS ofsted_id,
       p.publication_date AS valid_from,
       lead(p.publication_date) OVER u AS open_valid_to,
       p.rating_score, p.grade_basis, p.rating_label,
       p.inspection_start_date AS inspection_date,
       p.publication_date,
       NULL::integer AS inherited_from_urn
  FROM one_per_day p
WINDOW u AS (PARTITION BY p.urn ORDER BY p.publication_date);
CREATE INDEX ON staging._ofsted_spells (urn, valid_from);
"""

# A successor school (GIAS "Predecessor" link) without a rating of its own
# takes its predecessor's, from opening until its own first publication. Only
# a conversion: the predecessor closed within `CONVERSION_WINDOW_DAYS` of the
# successor opening, in a compatible phase (a merger years after an academy
# opened, or a primary absorbed by a secondary, passes nothing on). Among
# several, the closest closure, then a plain "Predecessor" link, wins.
CONVERSION_WINDOW_DAYS = 366
INHERIT_SQL = f"""
WITH candidates AS (
    SELECT btrim(l.urn)::int AS successor, btrim(l.link_urn)::int AS predecessor, l.link_type,
           s.open_date, s.close_date AS successor_close, p.close_date AS predecessor_close
      FROM staging.gias_links_raw l
      JOIN staging._ofsted_schools s ON s.urn = btrim(l.urn)::int
      JOIN staging._ofsted_schools p ON p.urn = btrim(l.link_urn)::int
     WHERE l.link_type ~* '^predecessor' AND btrim(l.urn) ~ '^\\d+$' AND btrim(l.link_urn) ~ '^\\d+$'
       AND btrim(l.urn) <> btrim(l.link_urn)
       AND (s.phase IS NULL OR p.phase IS NULL OR s.phase = p.phase
            OR 'all_through' IN (s.phase, p.phase))
       AND (s.open_date IS NULL OR p.close_date IS NULL
            OR abs(s.open_date - p.close_date) <= {CONVERSION_WINDOW_DAYS})
       AND EXISTS (SELECT 1 FROM staging._ofsted_spells sp WHERE sp.urn = p.urn)
),
links AS (
    SELECT DISTINCT ON (successor) *
      FROM candidates
     ORDER BY successor, abs(coalesce(open_date - predecessor_close, 0)),
              (link_type = 'Predecessor') DESC, predecessor
),
todo AS (
    SELECT k.successor, k.predecessor,
           coalesce(k.open_date, k.predecessor_close + 1) AS starts,
           least((SELECT min(own.valid_from) FROM staging._ofsted_spells own
                   WHERE own.urn = k.successor AND own.inherited_from_urn IS NULL),
                 k.successor_close + 1) AS ends
      FROM links k
     WHERE NOT EXISTS (SELECT 1 FROM staging._ofsted_spells x
                        WHERE x.urn = k.successor AND x.inherited_from_urn IS NOT NULL)
       AND coalesce(k.open_date, k.predecessor_close + 1) IS NOT NULL
)
INSERT INTO staging._ofsted_spells
       (urn, ofsted_id, valid_from, open_valid_to, rating_score, grade_basis, rating_label,
        inspection_date, publication_date, inherited_from_urn)
SELECT t.successor, sp.ofsted_id,
       greatest(sp.valid_from, t.starts),
       CASE WHEN sp.open_valid_to IS NULL THEN t.ends ELSE least(sp.open_valid_to, t.ends) END,
       sp.rating_score, sp.grade_basis, sp.rating_label,
       sp.inspection_date, sp.publication_date, t.predecessor
  FROM todo t
  JOIN staging._ofsted_spells sp ON sp.urn = t.predecessor
 WHERE coalesce(sp.open_valid_to, 'infinity'::date) > t.starts
   AND (t.ends IS NULL OR sp.valid_from < t.ends)
   AND greatest(sp.valid_from, t.starts) < coalesce(least(sp.open_valid_to, t.ends), 'infinity'::date)
"""
MAX_INHERIT_PASSES = 5

PRODUCTION_SPELLS_SQL = """
CREATE TABLE production.ofsted_rating_spells (
    id                  bigserial PRIMARY KEY,
    urn                 integer NOT NULL,
    ofsted_id           bigint NOT NULL REFERENCES production.ofsted (id),
    phase               text,
    school_name         text,
    la_code             smallint,
    geom                geometry(Point, 27700),
    lat                 double precision,
    lon                 double precision,
    valid_from          date NOT NULL,
    valid_to            date,
    rating_score        numeric(3,2) NOT NULL CHECK (rating_score BETWEEN 1 AND 4),
    grade_basis         text NOT NULL,
    rating_label        text,
    inspection_date     date NOT NULL,
    publication_date    date NOT NULL,
    inherited_from_urn  integer,
    source              text NOT NULL,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (valid_from >= publication_date)
);
INSERT INTO production.ofsted_rating_spells (
    urn, ofsted_id, phase, school_name, la_code, geom, lat, lon, valid_from, valid_to,
    rating_score, grade_basis, rating_label, inspection_date, publication_date, inherited_from_urn, source
)
SELECT sp.urn, sp.ofsted_id, s.phase, s.school_name, s.la_code, s.geom, s.latitude, s.longitude,
       sp.valid_from,
       CASE WHEN sp.inherited_from_urn IS NOT NULL THEN sp.open_valid_to
            ELSE least(sp.open_valid_to, s.close_date + 1) END,
       sp.rating_score, sp.grade_basis, sp.rating_label, sp.inspection_date, sp.publication_date,
       sp.inherited_from_urn, :source
  FROM staging._ofsted_spells sp
  JOIN staging._ofsted_schools s ON s.urn = sp.urn
 WHERE s.in_scope
   AND (s.close_date IS NULL OR sp.inherited_from_urn IS NOT NULL OR s.close_date >= sp.valid_from);
CREATE INDEX idx_prod_ofsted_spells_geom ON production.ofsted_rating_spells USING gist (geom);
CREATE INDEX idx_prod_ofsted_spells_phase_from ON production.ofsted_rating_spells (phase, valid_from);
CREATE INDEX idx_prod_ofsted_spells_urn ON production.ofsted_rating_spells (urn, valid_from);
COMMENT ON TABLE production.ofsted_rating_spells IS
    'Ofsted rating in force at each school over time, from the report''s publication date (migration 0033). Contains public sector information licensed under the Open Government Licence v3.0.';
COMMENT ON COLUMN production.ofsted_rating_spells.valid_from IS 'Publication date of the report that set or confirmed the rating (later for an inherited spell: the successor''s opening)';
COMMENT ON COLUMN production.ofsted_rating_spells.valid_to IS 'Next publication, closure or the successor''s own first publication; NULL while in force';
COMMENT ON COLUMN production.ofsted_rating_spells.rating_score IS 'Harmonised rating 1 (best) to 4, as production.ofsted.rating_score';
COMMENT ON COLUMN production.ofsted_rating_spells.grade_basis IS 'overall, key_judgements, report_card, or carried (an ungraded inspection confirmed the score)';
COMMENT ON COLUMN production.ofsted_rating_spells.inspection_date IS 'Start of the inspection that set or last confirmed the rating (the rating''s age counts from here)';
COMMENT ON COLUMN production.ofsted_rating_spells.inherited_from_urn IS 'Predecessor URN whose rating a successor school (e.g. an academy conversion) carries until its own first report';
COMMENT ON COLUMN production.ofsted_rating_spells.phase IS 'primary, secondary, all_through, special, pru, nursery or post16';
"""

_ACADEMIC_YEAR = "(extract(year FROM inspection_start_date) - (extract(month FROM inspection_start_date) < 9)::int)::int"

SHAPE_SQL = {
    "london_rows_per_year": f"""
        SELECT {_ACADEMIC_YEAR} AS year, count(*) AS n, count(DISTINCT la_code) AS las
          FROM production.ofsted
         WHERE la_code IN ({LONDON_LA_LIST}) AND origin <> 'previous'
         GROUP BY 1 ORDER BY 1
    """,
    "future_spells": "SELECT count(*) FROM production.ofsted_rating_spells WHERE valid_from > current_date",
    "overlapping_spells": """
        SELECT count(*) FROM production.ofsted_rating_spells a
          JOIN production.ofsted_rating_spells b
            ON a.urn = b.urn AND a.id < b.id
           AND daterange(a.valid_from, a.valid_to) && daterange(b.valid_from, b.valid_to)
    """,
    "rating_shares": """
        SELECT era,
               avg((rating_score < 1.5)::int) AS outstanding,
               avg((rating_score >= 1.5 AND rating_score < 2.5)::int) AS good,
               avg((rating_score >= 2.5 AND rating_score < 3.5)::int) AS requires_improvement,
               avg((rating_score >= 3.5)::int) AS inadequate,
               count(*) AS n
          FROM production.ofsted
         WHERE rating_score IS NOT NULL AND inspection_type IN ('graded', 'report_card')
         GROUP BY era ORDER BY era
    """,
    "located": """
        SELECT count(DISTINCT urn) AS schools,
               count(DISTINCT urn) FILTER (WHERE geom IS NOT NULL) AS located,
               count(DISTINCT urn) FILTER (WHERE inherited_from_urn IS NOT NULL) AS successors,
               count(DISTINCT urn) FILTER (WHERE inherited_from_urn IS NOT NULL AND geom IS NOT NULL) AS successors_located,
               count(*) AS spells,
               count(*) FILTER (WHERE inherited_from_urn IS NOT NULL) AS inherited
          FROM production.ofsted_rating_spells
    """,
    "by_basis": """
        SELECT grade_basis, count(*) FROM production.ofsted_rating_spells GROUP BY 1 ORDER BY 1
    """,
}


def _load_frame(connection, frame: pd.DataFrame, table: str) -> None:
    schema, name = table.split(".")
    frame.to_sql(name, connection, schema=schema, if_exists="append", index=False,
                 chunksize=TO_SQL_CHUNK_ROWS, method="multi")


def load_staging(engine, directories: dict, log) -> dict:
    """
    Load the manifest's files into the four staging tables.

    `directories` maps each `ofsted.OfstedFile.directory_key` (``ofsted_dir``,
    which holds the manifest, and ``gias_dir``) to its path.

    Returns
    -------
    dict
        Rows loaded per file key.

    Raises
    ------
    AssertionError
        When a file's row count differs from the manifest's or is below its
        registry minimum.
    """
    data_dir = Path(directories["ofsted_dir"])
    manifest = ofsted.Manifest.read(data_dir)
    missing = [f.key for f in ofsted.FILES if f.key not in manifest.entries]
    if missing:
        raise AssertionError(
            f"0033: {data_dir / ofsted.MANIFEST_NAME} lacks {missing}; run "
            "python -m hpldn.backend.scripts.fetch_ofsted"
        )
    counts = {}
    raw_columns = ", ".join(f"{c} text" for c in ofsted.RAW_COLUMNS)
    gias_columns = ", ".join(f"{c} text" for c in ofsted.GIAS_COLUMNS.values())
    link_columns = ", ".join(f"{c} text" for c in ofsted.GIAS_LINK_COLUMNS.values())
    with engine.begin() as connection:
        connection.execute(text(STAGING_DDL))
        connection.execute(text(f"""
            DROP TABLE IF EXISTS staging.ofsted_inspections_raw;
            CREATE TABLE staging.ofsted_inspections_raw (
                {raw_columns}, source_file text NOT NULL, source_row integer NOT NULL, era text NOT NULL
            );
            DROP TABLE IF EXISTS staging.gias_establishments_raw;
            CREATE TABLE staging.gias_establishments_raw ({gias_columns}, source_file text NOT NULL);
            DROP TABLE IF EXISTS staging.gias_links_raw;
            CREATE TABLE staging.gias_links_raw ({link_columns}, source_file text NOT NULL);
        """))
        for spec in ofsted.FILES:
            entry = manifest.entries[spec.key]
            path = Path(directories[spec.directory_key]) / entry["file"]
            if spec.kind == "inspections":
                frame = ofsted.read_inspections(path, spec.era)
                frame["source_file"] = entry["file"]
                frame["era"] = spec.era
                _load_frame(connection, frame, "staging.ofsted_inspections_raw")
            else:
                columns = ofsted.GIAS_COLUMNS if spec.kind == "gias_establishments" else ofsted.GIAS_LINK_COLUMNS
                frame = ofsted.read_gias(path, columns)
                frame["source_file"] = entry["file"]
                table = "staging.gias_establishments_raw" if spec.kind == "gias_establishments" else "staging.gias_links_raw"
                _load_frame(connection, frame, table)
            rows = len(frame)
            if rows != entry["rows"] or rows < spec.min_rows:
                raise AssertionError(
                    f"0033: {entry['file']} loaded {rows} row(s); the manifest says {entry['rows']}, "
                    f"the minimum is {spec.min_rows}"
                )
            counts[spec.key] = rows
            connection.execute(
                text("""
                    INSERT INTO staging.ofsted_releases
                        (source_file, file_key, url, sha256, published_date, coverage_from, coverage_to, era, row_count)
                    VALUES (:file, :key, :url, :sha256, :published, :cfrom, :cto, :era, :rows)
                """),
                {
                    "file": entry["file"], "key": spec.key, "url": entry["url"], "sha256": entry["sha256"],
                    "published": entry.get("published_date"), "cfrom": entry["coverage_from"],
                    "cto": entry["coverage_to"], "era": spec.era, "rows": rows,
                },
            )
            log.info("0033: loaded %s (%d rows, %s)", entry["file"], rows, spec.era)
        connection.execute(text(
            "COMMENT ON TABLE staging.ofsted_inspections_raw IS "
            "'Ofsted inspection rows as published, under common column names (migration 0033, OGL v3.0)'"
        ))
        connection.execute(text(
            "COMMENT ON TABLE staging.gias_establishments_raw IS "
            "'DfE Get Information about Schools, every establishment (migration 0033, OGL v3.0)'"
        ))
        connection.execute(text(
            "COMMENT ON TABLE staging.gias_links_raw IS "
            "'DfE Get Information about Schools predecessor/successor links (migration 0033, OGL v3.0)'"
        ))
        for table in ("staging.ofsted_inspections_raw", "staging.gias_establishments_raw", "staging.gias_links_raw"):
            connection.execute(text(f"ANALYZE {table}"))
    return counts


def build_production(engine, cfg, log) -> dict:
    """Clean the staging rows into ``production.ofsted`` and ``production.ofsted_rating_spells``."""
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(DATE_FUNCTION_SQL))
        connection.execute(text(GRADE_FUNCTION_SQL))
        connection.execute(text(EVENTS_SQL))
        connection.execute(text(CLASSIFY_SQL))
        connection.execute(text(DEDUPE_SQL))
        conflicts = connection.execute(text(CONFLICTS_SQL)).scalar_one()
        stats = dict(connection.execute(text(
            "SELECT origin, count(*) FROM staging._ofsted_deduped GROUP BY origin"
        )).all())
        log.info(
            "0033: %d inspection(s) after de-duplication by origin %s; %d inspection number(s) "
            "carry conflicting ratings across files (the newest release's row is kept)",
            sum(stats.values()), stats, conflicts,
        )
        connection.execute(text(GLA_BOUNDARY_SQL))
        connection.execute(text(SCHOOLS_SQL))
        connection.execute(text(PRODUCTION_OFSTED_SQL), {"source": ofsted.SOURCE})
        connection.execute(text(COMMENTS_SQL))
        connection.execute(text("ANALYZE production.ofsted"))
        connection.execute(text(SPELLS_SQL))
        inherited = 0
        for _ in range(MAX_INHERIT_PASSES):
            added = connection.execute(text(INHERIT_SQL)).rowcount
            inherited += added
            if not added:
                break
        connection.execute(text(PRODUCTION_SPELLS_SQL), {"source": ofsted.SOURCE})
        connection.execute(text("ANALYZE production.ofsted_rating_spells"))
        rows = connection.execute(text(
            "SELECT inspection_type, count(*) FROM production.ofsted GROUP BY 1 ORDER BY 1"
        )).all()
        imputed = connection.execute(text(
            "SELECT count(*) FROM production.ofsted WHERE publication_date_imputed"
        )).scalar_one()
        for table in ("_ofsted_events", "_ofsted_classified", "_ofsted_deduped", "_ofsted_spells", "_ofsted_gla_buffer"):
            connection.execute(text(f"DROP TABLE IF EXISTS staging.{table}"))
    log.info(
        "0033: production.ofsted by type %s (%d publication date(s) imputed); %d inherited spell(s)",
        dict(rows), imputed, inherited,
    )
    return {"by_type": dict(rows), "imputed": imputed, "inherited": inherited, "conflicts": conflicts}


def check_shape(engine, log) -> None:
    """Assert the tables' shape (see the guard constants); raise AssertionError listing every failure."""
    failing = []
    with engine.connect() as connection:
        years = connection.execute(text(SHAPE_SQL["london_rows_per_year"])).mappings().all()
        future = connection.execute(text(SHAPE_SQL["future_spells"])).scalar_one()
        overlapping = connection.execute(text(SHAPE_SQL["overlapping_spells"])).scalar_one()
        shares = connection.execute(text(SHAPE_SQL["rating_shares"])).mappings().all()
        located = connection.execute(text(SHAPE_SQL["located"])).mappings().one()
        by_basis = dict(connection.execute(text(SHAPE_SQL["by_basis"])).all())
    log.info("0033: London inspections per academic year %s", [(r["year"], r["n"], r["las"]) for r in years])
    log.info("0033: rating shares by era %s", [
        (r["era"], r["n"], round(float(r["outstanding"]), 3), round(float(r["good"]), 3),
         round(float(r["requires_improvement"]), 3), round(float(r["inadequate"]), 3)) for r in shares
    ])
    log.info("0033: spells %s, by basis %s", dict(located), by_basis)

    for row in years:
        year = row["year"]
        if year < ofsted.FIRST_INSPECTION.year or year >= ofsted.LAST_INSPECTION.year:
            continue
        low, high = LONDON_ROWS_SPECIAL_YEARS.get(year, LONDON_ROWS_PER_YEAR)
        if not low <= row["n"] <= high:
            failing.append(f"{year}/{(year + 1) % 100:02d} has {row['n']} London inspection(s), expected {low}-{high}")
        min_las = MIN_LAS_SPECIAL_YEAR if year in LONDON_ROWS_SPECIAL_YEARS else MIN_LAS_PER_YEAR
        if row["las"] < min_las:
            failing.append(f"{year}/{(year + 1) % 100:02d} has inspections in {row['las']} London LA(s), expected >= {min_las}")
    expected_years = set(range(ofsted.FIRST_INSPECTION.year, ofsted.LAST_INSPECTION.year))
    absent = expected_years - {r["year"] for r in years}
    if absent:
        failing.append(f"no London inspections in academic year(s) {sorted(absent)}")
    if future:
        failing.append(f"{future} spell(s) start after today")
    if overlapping:
        failing.append(f"{overlapping} pair(s) of spells of one URN overlap")
    for row in shares:
        for band, (low, high) in RATING_SHARE_BOUNDS.items():
            value = float(row[band])
            if not low <= value <= high:
                failing.append(f"{row['era']}: {band} share {value:.3f} outside [{low}, {high}]")
    if located["spells"] < MIN_SPELLS:
        failing.append(f"{located['spells']} spell(s), expected at least {MIN_SPELLS}")
    if located["inherited"] < MIN_INHERITED_SPELLS:
        failing.append(f"{located['inherited']} inherited spell(s), expected at least {MIN_INHERITED_SPELLS}")
    for name, total, have in (
        ("schools", located["schools"], located["located"]),
        ("successor schools", located["successors"], located["successors_located"]),
    ):
        if total and have / total < MIN_LOCATED_SHARE:
            failing.append(f"{have}/{total} {name} with spells are located, expected >= {MIN_LOCATED_SHARE:.0%}")
    if failing:
        raise AssertionError("0033: Ofsted tables fail their checks: " + "; ".join(failing))
    log.info("0033: shape checks passed")


def upgrade() -> None:
    """Load the files, build ``production.ofsted`` and its spells, then check their shape."""
    engine = get_migration_engine()
    cfg = get_migration_cfg()
    log = get_migration_logger()
    directories = {
        key: require_data_path(
            cfg, key, f"data/{name}", description="migration 0033 (Ofsted)", must_be_dir=True
        )
        for key, name in ofsted.DEFAULT_DIRECTORIES.items()
    }
    load_staging(engine, directories, log)
    build_production(engine, cfg, log)
    check_shape(engine, log)
    vacuum_analyze(engine, ("production.ofsted", "production.ofsted_rating_spells"), log)
    log.info(
        "0033: complete. Next:\n"
        "  psql -v ON_ERROR_STOP=1 -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
        "  alembic upgrade 0034 (per-sale Ofsted features)"
    )


def downgrade() -> None:
    """Refuse; see `hpldn.backend.migrations.chain`."""
    no_downgrade()
