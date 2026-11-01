-- =====================================================================
-- Read grants for the API role on one schema: USAGE on it, SELECT on its
-- tables and EXECUTE on its functions, nothing else.
--
-- The one source for them: db_grants_post_migration.sql includes this file
-- with schema=production, and hpldn.backend.db.render_vps_sql inlines it
-- into the serving host's grants.sql, which restore_serving_db.sh runs
-- against production_next before the blue/green swap.
--
-- Needs the psql variables api_role and schema.
-- =====================================================================

-- psql does not interpolate :'var' inside a dollar-quoted block, so the
-- role name is handed to the DO block through a session-level custom GUC.
SELECT set_config('hpldn.api_role', :'api_role', false);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('hpldn.api_role')) THEN
        RAISE EXCEPTION 'role % does not exist; create it first (db_init.sql locally, api_role.sql on the serving host)',
            current_setting('hpldn.api_role');
    END IF;
END;
$$;

SELECT format('GRANT USAGE ON SCHEMA %I TO %I', :'schema', :'api_role') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', :'schema', :'api_role') \gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', :'schema', :'api_role') \gexec

-- Explicitly NOT granted: INSERT / UPDATE / DELETE / TRUNCATE anywhere, and
-- CREATE on any schema.
