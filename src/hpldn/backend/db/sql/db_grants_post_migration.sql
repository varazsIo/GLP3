-- =====================================================================
-- Grants that cannot go in db_init.sql, because they need the tables to
-- exist.
--
-- Run as the MIGRATION ROLE (not the superuser — production is owned by the
-- migration role, and the ALTER DEFAULT PRIVILEGES below is issued by that
-- role for itself, which is the form pipeline_steps.create_api_readonly_role
-- already uses):
--
--     psql -v ON_ERROR_STOP=1 -d hpdbldn -f src/hpldn/backend/db/sql/db_grants_post_migration.sql
--
--     # non-default API role:
--     psql -v ON_ERROR_STOP=1 -v api_role=hpldn_api_ro -d hpdbldn \
--          -f src/hpldn/backend/db/sql/db_grants_post_migration.sql
--
-- WHEN TO RUN IT
--   * after every full seed (`alembic upgrade head`);
--   * after an `alembic stamp` on a database populated before the chain.
--
-- WHY IT EXISTS EVEN THOUGH db_init.sql SETS DEFAULT PRIVILEGES
--   ALTER DEFAULT PRIVILEGES applies to objects created AFTER it is issued
--   and only to objects created by the role it names. This file is the
--   backstop for the two cases that misses:
--     (a) a database whose production tables already existed before any
--         default-privileges rule was written;
--     (b) any table created in production by a role other than the
--         migration role.
--   It is idempotent and cheap; running it when it has nothing to do costs
--   one catalogue scan.
-- =====================================================================

\set ON_ERROR_STOP on
\if :{?api_role} \else \set api_role hpldn_api \endif

\echo 'db_grants_post_migration.sql: api_role =' :'api_role'

-- The role must already exist (db_init.sql creates it as a NOLOGIN stub);
-- the fragment fails rather than silently granting nothing. USAGE on
-- production, SELECT on its tables and EXECUTE on its functions come from
-- fragments/api_grants.sql, the same file the serving host's grants.sql is
-- rendered from.
\set schema production
\ir fragments/api_grants.sql

SELECT format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA production TO %I', :'api_role') \gexec

-- Issued by the current (migration) role for itself, so tables it creates
-- from here on are covered without re-running this file. db_init.sql issues
-- the same rule on the migration role's behalf from the superuser session;
-- both rows can coexist and the effect is the same.
SELECT format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA production GRANT SELECT ON TABLES TO %I',
    :'api_role') \gexec

-- Explicitly NOT granted, and asserted absent by db_verify_init.sql:
--   * anything on schema staging (scratch, landing and fusion working
--     tables live there and are not part of the serving contract);
--   * INSERT / UPDATE / DELETE / TRUNCATE anywhere.

\echo 'db_grants_post_migration.sql: complete.'
