-- =====================================================================
-- HPLDN database initialisation — everything the alembic chain assumes
-- but cannot create itself.
--
-- Run ONCE, as the PostgreSQL superuser, BEFORE any migration:
--
--     sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres \
--          -f src/hpldn/backend/db/sql/db_init.sql
--
-- Non-default role / database:
--
--     sudo -u postgres psql -v ON_ERROR_STOP=1 \
--          -v migration_role=alice -v db_name=hpdbldn_dev \
--          -d postgres -f src/hpldn/backend/db/sql/db_init.sql
--
-- Defaults: migration_role=smith, db_name=hpdbldn, api_role=hpldn_api.
--
-- PROPERTIES
--   * Idempotent. Every statement is IF NOT EXISTS / CREATE OR REPLACE, or
--     guarded by an existence check + \gexec. A second run is a clean no-op.
--   * No transaction block. CREATE DATABASE cannot run inside one, so this
--     file is deliberately not wrapped in BEGIN/COMMIT. Run it with
--     ON_ERROR_STOP=1 so a failure halts rather than skipping ahead.
--   * No passwords. Both roles are created without one; set them out of
--     band (`ALTER ROLE ... PASSWORD`, or `db_enable_api_login.sh` for the
--     API role). This file must stay safe to commit.
--   * Cluster settings (shared_buffers, work_mem, wal_*, ...) are NOT
--     database state and are not here. They come from postgresql.conf /
--     conf.d drop-ins, owned by hpldn.backend.common.tuning and
--     cloud/vps/postgres/10-hpldn.conf.
--
-- Verify afterwards with:
--     psql -v ON_ERROR_STOP=1 -d <db_name> -f src/hpldn/backend/db/sql/db_verify_init.sql
--     python -m hpldn.backend.scripts.bare_metal --check
-- =====================================================================

\set ON_ERROR_STOP on
\set QUIET on

-- ---------------------------------------------------------------------
-- Parameters. `\if :{?var}` tests whether the variable was supplied with
-- -v; when it was not, the default below is used.
-- ---------------------------------------------------------------------
\if :{?migration_role} \else \set migration_role smith \endif
\if :{?db_name}        \else \set db_name hpdbldn      \endif
\if :{?api_role}       \else \set api_role hpldn_api   \endif

\set QUIET off
\echo 'db_init.sql: migration_role =' :'migration_role'
\echo 'db_init.sql: db_name        =' :'db_name'
\echo 'db_init.sql: api_role       =' :'api_role'

-- =====================================================================
-- 1. Migration role
-- =====================================================================
-- LOGIN and nothing else: no SUPERUSER, no CREATEDB, no CREATEROLE, no
-- REPLICATION, no BYPASSRLS. The
-- password is set out of band; a role with no password cannot authenticate
-- under scram-sha-256, which is the intended state until someone sets one.
SELECT format('CREATE ROLE %I LOGIN', :'migration_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migration_role')
\gexec

-- =====================================================================
-- 2. Read-only API role (NOLOGIN stub)
-- =====================================================================
-- Created here rather than by pipeline_steps.create_api_readonly_role so
-- that step 6's ALTER DEFAULT PRIVILEGES can name it. A NOLOGIN role with
-- no password cannot authenticate, so the stub is not a security
-- regression; create_api_readonly_role keeps only the half that needs a
-- secret (LOGIN PASSWORD, statement_timeout, work_mem).
SELECT format('CREATE ROLE %I NOLOGIN', :'api_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'api_role')
\gexec

-- =====================================================================
-- 3. Database
-- =====================================================================
-- TEMPLATE template0 so the collation/ctype below are honoured rather than
-- inherited from template1. CREATE DATABASE cannot run in a transaction
-- block, which is why this file has no BEGIN.
SELECT format(
    'CREATE DATABASE %I OWNER postgres ENCODING ''UTF8'' '
    'LC_COLLATE ''en_US.UTF-8'' LC_CTYPE ''en_US.UTF-8'' TEMPLATE template0',
    :'db_name'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name')
\gexec

-- CONNECT only — deliberately no CREATE and no TEMP.
--
-- Withholding CREATE on the database is the single most confusing thing
-- about this setup, and it is load-bearing. It is what makes the four
-- deploy_* SECURITY DEFINER helpers below necessary, and what
-- pipeline_steps._ensure_production_schema's three-way fallback is written
-- around: `CREATE SCHEMA IF NOT EXISTS` verifies the CREATE privilege
-- BEFORE the IF NOT EXISTS short-circuits, so the naive statement fails
-- with InsufficientPrivilege on a host where the schema already exists.
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'migration_role')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'api_role')
\gexec

\connect :db_name

-- =====================================================================
-- 4. Extensions
-- =====================================================================
-- plpgsql arrives with template1; assert it rather than create it, so a
-- cluster that somehow lacks it fails loudly here instead of at the first
-- SECURITY DEFINER function body.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'plpgsql') THEN
        RAISE EXCEPTION 'plpgsql is not installed in %; it normally arrives '
                        'with template1', current_database();
    END IF;
END;
$$;

CREATE EXTENSION IF NOT EXISTS postgis;

-- btree_gist: `USING gist(geom, dateoftransfer)` moves the twelve-month comps
-- window out of the heap-fetch filter and into the index — for terraced
-- properties the current scan walks ~2,000 candidates per row to find 5, at
-- a random page read each. A date has no GIST opclass without this
-- extension. The index it unlocks belongs in migration
-- 0014_comps_and_neighbourhood, not here.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- pg_trgm: the trigram GIN index on production.address_search that migration
-- 0021_address_search_index builds for GET /addresses/search (typo-tolerant
-- address suggestions). The migration role cannot CREATE EXTENSION itself,
-- so a database built before this line existed gets it through
-- deploy_pg_trgm_extension() below.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- =====================================================================
-- 5. Schemas — the ownership split
-- =====================================================================
-- staging is owned by postgres with USAGE, CREATE granted to the migration
-- role; production is owned by the migration role outright. The promote
-- steps rely on that asymmetry (they DROP and
-- re-CREATE tables in production).
CREATE SCHEMA IF NOT EXISTS staging AUTHORIZATION postgres;

SELECT format('GRANT USAGE, CREATE ON SCHEMA staging TO %I', :'migration_role')
\gexec

SELECT format('CREATE SCHEMA IF NOT EXISTS production AUTHORIZATION %I', :'migration_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'production')
\gexec

SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'migration_role')
\gexec

SELECT format('GRANT USAGE ON SCHEMA production TO %I', :'api_role')
\gexec

-- =====================================================================
-- 6. Default privileges
-- =====================================================================
-- Two pg_default_acl rows. Written as GRANT ALL PRIVILEGES / GRANT SELECT,
-- UPDATE, USAGE rather than enumerating ACL letters: the `m` (MAINTAIN) bit
-- is PostgreSQL 17+, so spelling out `arwdDxtm` would not replay on an
-- older server.
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public, staging '
    'GRANT ALL PRIVILEGES ON TABLES TO %I', :'migration_role')
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public, staging '
    'GRANT SELECT, UPDATE, USAGE ON SEQUENCES TO %I', :'migration_role')
\gexec

-- The statement that removes the ordering problem: issued at init time by
-- the superuser ON BEHALF OF the migration role, so every table the chain
-- later creates in production is SELECTable by the API role automatically,
-- with no post-hoc grant pass and no window in which a new feature table is
-- unreadable. It is also why scratch and non-serving tables belong in
-- staging: anything left in production is in the API role's grant surface.
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA production '
    'GRANT SELECT ON TABLES TO %I', :'migration_role', :'api_role')
\gexec

-- =====================================================================
-- 7. SECURITY DEFINER deploy helpers
-- =====================================================================
-- Four helpers: deploy_spatial_extension, deploy_btree_gist_extension,
-- deploy_pg_trgm_extension (migration 0021, migrations/address_search.py
-- install_pg_trgm) and deploy_production_schema. Two properties matter:
--
--   (a) deploy_production_schema's `AUTHORIZATION <role>` is generated from
--       :'migration_role' through format(%I) + \gexec, never a literal.
--   (b) each has `SET search_path = pg_catalog, public`. A SECURITY
--       DEFINER function without a pinned search_path is a standard
--       privilege-escalation shape (the caller controls name resolution
--       inside a body running as postgres).
--
-- Each is locked down to the migration role afterwards (ACL
-- `{postgres=X/postgres,<migration_role>=X/postgres}`).

CREATE OR REPLACE FUNCTION public.deploy_spatial_extension()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    -- Strictly restricts execution to only postgis
    CREATE EXTENSION IF NOT EXISTS postgis;
END;
$function$;

CREATE OR REPLACE FUNCTION public.deploy_btree_gist_extension()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    -- Strictly restricted to installing this one extension.
    CREATE EXTENSION IF NOT EXISTS btree_gist;
END;
$function$;

CREATE OR REPLACE FUNCTION public.deploy_pg_trgm_extension()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    -- Strictly restricted to installing this one extension (migration 0021).
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
END;
$function$;

SELECT format($fmt$
CREATE OR REPLACE FUNCTION public.deploy_production_schema()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    -- Strictly restricted to creating the production serving schema
    CREATE SCHEMA IF NOT EXISTS production AUTHORIZATION %I;
END;
$function$
$fmt$, :'migration_role')
\gexec

REVOKE ALL ON FUNCTION public.deploy_spatial_extension()     FROM PUBLIC;
REVOKE ALL ON FUNCTION public.deploy_btree_gist_extension()  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.deploy_pg_trgm_extension()     FROM PUBLIC;
REVOKE ALL ON FUNCTION public.deploy_production_schema()     FROM PUBLIC;

SELECT format('GRANT EXECUTE ON FUNCTION public.deploy_spatial_extension() TO %I',
              :'migration_role') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION public.deploy_btree_gist_extension() TO %I',
              :'migration_role') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION public.deploy_pg_trgm_extension() TO %I',
              :'migration_role') \gexec
SELECT format('GRANT EXECUTE ON FUNCTION public.deploy_production_schema() TO %I',
              :'migration_role') \gexec

-- =====================================================================
-- 8. OPTIONAL hardening — commented out on purpose
-- =====================================================================
-- The database keeps PostgreSQL's default `=Tc` grant to PUBLIC.
-- db_verify_init.sql asserts that ACL and will start failing if you
-- uncomment this.
--
-- SELECT format('REVOKE TEMP, CONNECT ON DATABASE %I FROM PUBLIC', :'db_name')
-- \gexec

\echo 'db_init.sql: complete (extensions postgis, btree_gist, pg_trgm; four deploy_* helpers).'
\echo 'Next:  alembic upgrade 0001'
\echo '       python -m hpldn.backend.scripts.bare_metal --load-raw   (staging.ukhpucl)'
\echo '       alembic upgrade head'
\echo '       psql -f src/hpldn/backend/db/sql/db_grants_post_migration.sql'
\echo '       psql -f src/hpldn/backend/db/sql/db_verify_init.sql'
