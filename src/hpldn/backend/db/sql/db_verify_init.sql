-- =====================================================================
-- Read-only assertions that db_init.sql (and, where tables exist,
-- db_grants_post_migration.sql) actually produced the intended state.
--
--     psql -v ON_ERROR_STOP=1 -d hpdbldn -f src/hpldn/backend/db/sql/db_verify_init.sql
--
--     # non-default role names:
--     psql -v ON_ERROR_STOP=1 -v migration_role=alice -v api_role=hpldn_api_ro \
--          -d hpdbldn_dev -f src/hpldn/backend/db/sql/db_verify_init.sql
--
-- Every check is its own `DO $$ ... RAISE EXCEPTION ... $$`, so with
-- ON_ERROR_STOP=1 the first failure aborts with a non-zero exit status —
-- which is what makes this usable from CI and from a shell `&&` chain.
-- Nothing here writes.
--
-- psql's :'var' interpolation is NOT performed inside a dollar-quoted ($$)
-- block — substitution happens client-side before the query is sent, and a
-- dollar-quoted string is opaque to that scan. So :'migration_role' and
-- :'api_role' are handed in via session-level custom GUCs (set with plain
-- SQL below, where interpolation works normally) and every DO block reads
-- them back with current_setting() instead of referencing the psql
-- variables directly.
--
-- The assertions describe the state db_init.sql creates (written against
-- PostgreSQL 18), including the required btree_gist extension.
--
-- Python equivalent, for a fail-in-seconds pre-flight before a long seed:
--     python -m hpldn.backend.scripts.bare_metal --check
-- =====================================================================

\set ON_ERROR_STOP on
\if :{?migration_role} \else \set migration_role smith     \endif
\if :{?api_role}       \else \set api_role hpldn_api       \endif

\echo '=== db_verify_init.sql ==='
\echo 'database       :' :DBNAME
\echo 'migration_role :' :'migration_role'
\echo 'api_role       :' :'api_role'

-- Session-level custom GUCs so the DO $$ ... $$ blocks below can read these
-- values with current_setting() (see note above :'var' interpolation).
SELECT set_config('hpldn.migration_role', :'migration_role', false);
SELECT set_config('hpldn.api_role', :'api_role', false);

-- ---------------------------------------------------------------------
-- 1. Extensions
-- ---------------------------------------------------------------------
DO $$
DECLARE
    missing text[];
BEGIN
    SELECT array_agg(e) INTO missing
    FROM unnest(ARRAY['plpgsql', 'postgis', 'btree_gist', 'pg_trgm']) AS e
    WHERE NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = e);

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION
            'missing extension(s) %: run src/hpldn/backend/db/sql/db_init.sql as superuser',
            missing;
    END IF;
END;
$$;
\echo '  [ok] extensions plpgsql, postgis, btree_gist, pg_trgm present'

-- ---------------------------------------------------------------------
-- 2. Roles
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_role  text := current_setting('hpldn.migration_role');
    v_api   text := current_setting('hpldn.api_role');
    r       pg_roles%ROWTYPE;
BEGIN
    SELECT * INTO r FROM pg_roles WHERE rolname = v_role;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration role % does not exist', v_role;
    END IF;
    IF NOT r.rolcanlogin THEN
        RAISE EXCEPTION 'migration role % cannot LOGIN', v_role;
    END IF;
    -- The whole design assumes the migration role is unprivileged; a
    -- superuser here would make the deploy_* helpers and the schema
    -- ownership split meaningless and would mask a broken bootstrap.
    IF r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
       OR r.rolbypassrls THEN
        RAISE EXCEPTION
            'migration role % carries cluster privileges it must not have '
            '(super=% createdb=% createrole=% replication=% bypassrls=%)',
            v_role, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
            r.rolreplication, r.rolbypassrls;
    END IF;

    SELECT * INTO r FROM pg_roles WHERE rolname = v_api;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'API role % does not exist', v_api;
    END IF;
END;
$$;
\echo '  [ok] migration role exists, can log in, holds no cluster privileges'
\echo '  [ok] API role exists'

-- ---------------------------------------------------------------------
-- 3. Database-level privileges
--    CONNECT yes, CREATE no, TEMP no — withholding CREATE is what makes
--    the deploy_* helpers load-bearing (see db_init.sql §3).
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_role text := current_setting('hpldn.migration_role');
    v_api  text := current_setting('hpldn.api_role');
    v_db   text := current_database();
BEGIN
    IF NOT has_database_privilege(v_role, v_db, 'CONNECT') THEN
        RAISE EXCEPTION 'role % has no CONNECT on database %', v_role, v_db;
    END IF;
    IF has_database_privilege(v_role, v_db, 'CREATE') THEN
        RAISE EXCEPTION
            'role % has CREATE on database % — it must not; that privilege is '
            'what deploy_production_schema() exists to avoid needing',
            v_role, v_db;
    END IF;
    IF NOT has_database_privilege(v_api, v_db, 'CONNECT') THEN
        RAISE EXCEPTION 'API role % has no CONNECT on database %',
            v_api, v_db;
    END IF;
END;
$$;
\echo '  [ok] migration role has CONNECT but not CREATE on the database'

-- ---------------------------------------------------------------------
-- 4. Schema ownership and ACLs
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_role  text := current_setting('hpldn.migration_role');
    v_api   text := current_setting('hpldn.api_role');
    v_owner text;
BEGIN
    -- staging: owned by postgres, migration role granted USAGE + CREATE
    SELECT pg_get_userbyid(nspowner) INTO v_owner
    FROM pg_namespace WHERE nspname = 'staging';
    IF v_owner IS NULL THEN
        RAISE EXCEPTION 'schema staging does not exist';
    END IF;
    IF v_owner <> 'postgres' THEN
        RAISE EXCEPTION 'schema staging is owned by % (expected postgres)', v_owner;
    END IF;
    IF NOT has_schema_privilege(v_role, 'staging', 'USAGE')
       OR NOT has_schema_privilege(v_role, 'staging', 'CREATE') THEN
        RAISE EXCEPTION 'role % lacks USAGE/CREATE on schema staging', v_role;
    END IF;

    -- production: owned by the migration role outright
    SELECT pg_get_userbyid(nspowner) INTO v_owner
    FROM pg_namespace WHERE nspname = 'production';
    IF v_owner IS NULL THEN
        RAISE EXCEPTION 'schema production does not exist';
    END IF;
    IF v_owner <> v_role THEN
        RAISE EXCEPTION 'schema production is owned by % (expected %)',
            v_owner, v_role;
    END IF;

    -- public: migration role needs USAGE + CREATE (PostGIS lives there and
    -- migration 0006 creates the staging.hpldn_* helpers alongside it)
    IF NOT has_schema_privilege(v_role, 'public', 'USAGE')
       OR NOT has_schema_privilege(v_role, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'role % lacks USAGE/CREATE on schema public', v_role;
    END IF;

    -- API role: USAGE on production, nothing on staging
    IF NOT has_schema_privilege(v_api, 'production', 'USAGE') THEN
        RAISE EXCEPTION 'API role % lacks USAGE on schema production', v_api;
    END IF;
    IF has_schema_privilege(v_api, 'staging', 'USAGE')
       OR has_schema_privilege(v_api, 'staging', 'CREATE') THEN
        RAISE EXCEPTION
            'API role % has privileges on schema staging — it must have none; '
            'staging holds landing, scratch and fusion working tables that are '
            'not part of the serving contract', v_api;
    END IF;
END;
$$;
\echo '  [ok] schema ownership split: staging=postgres, production=migration role'
\echo '  [ok] API role has USAGE on production and nothing on staging'

-- ---------------------------------------------------------------------
-- 5. Default privileges (the two pg_default_acl rows db_init.sql writes,
--    plus the production-for-API-role one)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_role text := current_setting('hpldn.migration_role');
    v_api  text := current_setting('hpldn.api_role');
    n      int;
BEGIN
    SELECT count(*) INTO n
    FROM pg_default_acl d
    JOIN pg_namespace n2 ON n2.oid = d.defaclnamespace
    WHERE pg_get_userbyid(d.defaclrole) = 'postgres'
      AND n2.nspname IN ('public', 'staging')
      AND d.defaclobjtype = 'r'
      AND array_to_string(d.defaclacl, ',') LIKE '%' || v_role || '=%';
    IF n < 2 THEN
        RAISE EXCEPTION
            'expected 2 ALTER DEFAULT PRIVILEGES rows for postgres on TABLES in '
            'public+staging granting to %, found %', v_role, n;
    END IF;

    SELECT count(*) INTO n
    FROM pg_default_acl d
    JOIN pg_namespace n2 ON n2.oid = d.defaclnamespace
    WHERE pg_get_userbyid(d.defaclrole) = 'postgres'
      AND n2.nspname IN ('public', 'staging')
      AND d.defaclobjtype = 'S'
      AND array_to_string(d.defaclacl, ',') LIKE '%' || v_role || '=%';
    IF n < 2 THEN
        RAISE EXCEPTION
            'expected 2 ALTER DEFAULT PRIVILEGES rows for postgres on SEQUENCES '
            'in public+staging granting to %, found %', v_role, n;
    END IF;

    -- The one that removes the ordering problem: new production tables are
    -- readable by the API role without a post-hoc grant pass.
    SELECT count(*) INTO n
    FROM pg_default_acl d
    JOIN pg_namespace n2 ON n2.oid = d.defaclnamespace
    WHERE n2.nspname = 'production'
      AND d.defaclobjtype = 'r'
      AND array_to_string(d.defaclacl, ',') LIKE '%' || v_api || '=r%';
    IF n < 1 THEN
        RAISE EXCEPTION
            'no ALTER DEFAULT PRIVILEGES row grants SELECT on future production '
            'tables to % — a feature table added later would be invisible to '
            'the API', v_api;
    END IF;
END;
$$;
\echo '  [ok] default privileges present for tables, sequences and the API role'

-- ---------------------------------------------------------------------
-- 6. The four SECURITY DEFINER deploy helpers
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_role text := current_setting('hpldn.migration_role');
    fn     text;
    p      pg_proc%ROWTYPE;
BEGIN
    FOREACH fn IN ARRAY ARRAY['deploy_spatial_extension',
                              'deploy_btree_gist_extension',
                              'deploy_pg_trgm_extension',
                              'deploy_production_schema']
    LOOP
        SELECT pr.* INTO p
        FROM pg_proc pr
        JOIN pg_namespace ns ON ns.oid = pr.pronamespace
        WHERE ns.nspname = 'public' AND pr.proname = fn;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'public.%() does not exist', fn;
        END IF;
        IF NOT p.prosecdef THEN
            RAISE EXCEPTION 'public.%() is not SECURITY DEFINER', fn;
        END IF;
        -- Without a pinned search_path a SECURITY DEFINER body resolves
        -- names using the caller's path while running as its owner — the
        -- standard privilege-escalation shape; db_init.sql pins it.
        IF p.proconfig IS NULL
           OR NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) c
                          WHERE c LIKE 'search_path=%') THEN
            RAISE EXCEPTION
                'public.%() is SECURITY DEFINER with no pinned search_path', fn;
        END IF;
        IF has_function_privilege('public', format('public.%I()', fn), 'EXECUTE') THEN
            RAISE EXCEPTION 'public.%() is EXECUTEable by PUBLIC', fn;
        END IF;
        IF NOT has_function_privilege(v_role, format('public.%I()', fn), 'EXECUTE') THEN
            RAISE EXCEPTION 'role % cannot EXECUTE public.%()', v_role, fn;
        END IF;
    END LOOP;
END;
$$;
\echo '  [ok] deploy_spatial_extension / deploy_btree_gist_extension /'
\echo '       deploy_pg_trgm_extension / deploy_production_schema: SECURITY DEFINER, search_path pinned,'
\echo '       not public-executable, executable by the migration role'

-- ---------------------------------------------------------------------
-- 7. API role can SELECT every relation in production
--
--    This is the check that catches a feature table added outside the
--    default-privileges path — the failure mode db_grants_post_migration.sql
--    exists to repair. It is a no-op on a database with no production
--    tables yet (immediately after db_init.sql), by design.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_api  text := current_setting('hpldn.api_role');
    missing text[];
    total   int;
BEGIN
    SELECT count(*) INTO total
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'production' AND c.relkind IN ('r', 'p', 'v', 'm', 'f');

    IF total = 0 THEN
        RAISE NOTICE 'schema production holds no relations yet — skipping the '
                     'per-relation SELECT check (run this again after '
                     '`alembic upgrade head`)';
        RETURN;
    END IF;

    SELECT array_agg(n.nspname || '.' || c.relname ORDER BY c.relname)
    INTO missing
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'production'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND NOT has_table_privilege(v_api, c.oid, 'SELECT');

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION
            'API role % cannot SELECT % of % production relation(s): % — run '
            'src/hpldn/backend/db/sql/db_grants_post_migration.sql',
            v_api, array_length(missing, 1), total, missing;
    END IF;
    RAISE NOTICE 'API role % can SELECT all % production relation(s)', v_api, total;
END;
$$;
\echo '  [ok] API role can SELECT every relation in production'

-- ---------------------------------------------------------------------
-- 8. API role holds NO write privilege anywhere, and nothing in staging
-- ---------------------------------------------------------------------
DO $$
DECLARE
    v_api    text := current_setting('hpldn.api_role');
    offending text[];
BEGIN
    SELECT array_agg(n.nspname || '.' || c.relname ORDER BY n.nspname, c.relname)
    INTO offending
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('staging', 'production', 'public')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND (has_table_privilege(v_api, c.oid, 'INSERT')
        OR has_table_privilege(v_api, c.oid, 'UPDATE')
        OR has_table_privilege(v_api, c.oid, 'DELETE')
        OR has_table_privilege(v_api, c.oid, 'TRUNCATE'));

    IF offending IS NOT NULL THEN
        RAISE EXCEPTION
            'API role % holds write privileges on %; it must be read-only',
            v_api, offending;
    END IF;

    SELECT array_agg(n.nspname || '.' || c.relname ORDER BY c.relname)
    INTO offending
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'staging'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_table_privilege(v_api, c.oid, 'SELECT');

    IF offending IS NOT NULL THEN
        RAISE EXCEPTION
            'API role % can read staging relation(s) %; staging is not part of '
            'the serving contract', v_api, offending;
    END IF;
END;
$$;
\echo '  [ok] API role holds no write privilege and cannot read staging'

\echo '=== db_verify_init.sql: all checks passed ==='
