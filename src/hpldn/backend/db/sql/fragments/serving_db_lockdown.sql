-- =====================================================================
-- Serving host only: the API role is the only role that may connect to the
-- serving database, and nobody but the owner may create anything in its
-- public schema. Not run locally, where the migration role shares the
-- database.
--
-- Needs the psql variables api_role and db.
-- =====================================================================

SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
-- Spelled out, though ALL covers them: no temporary tables for anyone.
SELECT format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('REVOKE TEMPORARY, CREATE ON DATABASE %I FROM %I', :'db', :'api_role') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'api_role') \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
