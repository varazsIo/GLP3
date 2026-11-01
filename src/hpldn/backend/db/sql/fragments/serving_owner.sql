-- =====================================================================
-- Serving host only: who owns the serving data, and how the API logs in.
--
-- * hpldn_owner (NOLOGIN) owns the serving schemas, their tables and
--   functions. restore_serving_db.sh runs the dump's SQL and COPY as this
--   role (PGOPTIONS -c role=hpldn_owner), never as the superuser, so a
--   tampered dump cannot COPY ... PROGRAM, read server files or alter roles.
--   It may create schemas in the database (CREATE) and nothing else.
-- * hpldn_api logs in with peer authentication over the unix socket
--   (pg_hba: local hpldn_serve hpldn_api peer map=hpldn_api; pg_ident maps
--   only the OS user hpldn), so it has no password at all: NULL here.
-- * Every existing serving schema is handed to hpldn_owner, so a host whose
--   schemas a superuser restore created converts on the next restore.
--
-- Needs the psql variables api_role and db.
-- =====================================================================

SELECT 'CREATE ROLE hpldn_owner NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hpldn_owner') \gexec
ALTER ROLE hpldn_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT format('GRANT CREATE ON DATABASE %I TO hpldn_owner', :'db') \gexec

-- Peer authentication only: no password to leak, rotate or log.
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD NULL NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'api_role') \gexec
-- Unqualified names resolve in the live schema only (and pg_catalog, always first).
SELECT format('ALTER ROLE %I SET search_path = production', :'api_role') \gexec

DO $$
DECLARE
    nsp text;
    obj record;
BEGIN
    FOREACH nsp IN ARRAY ARRAY['production', 'production_next', 'production_old'] LOOP
        CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = nsp);
        EXECUTE format('ALTER SCHEMA %I OWNER TO hpldn_owner', nsp);
        FOR obj IN
            SELECT c.relname, c.relkind FROM pg_class c
            WHERE c.relnamespace = nsp::regnamespace
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND pg_get_userbyid(c.relowner) <> 'hpldn_owner'
        LOOP
            EXECUTE format(
                'ALTER %s %I.%I OWNER TO hpldn_owner',
                CASE obj.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                    WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
                nsp, obj.relname);
        END LOOP;
        FOR obj IN
            SELECT p.oid::regprocedure AS sig FROM pg_proc p
            WHERE p.pronamespace = nsp::regnamespace AND pg_get_userbyid(p.proowner) <> 'hpldn_owner'
        LOOP
            EXECUTE format('ALTER ROUTINE %s OWNER TO hpldn_owner', obj.sig);
        END LOOP;
    END LOOP;
END;
$$;
