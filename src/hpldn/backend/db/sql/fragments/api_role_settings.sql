-- =====================================================================
-- Per-role settings of the read API's database role.
--
-- The one source for them: db_enable_api_login.sh includes this file
-- locally, and hpldn.backend.db.render_vps_sql inlines it into the
-- serving host's api_role.sql. No password is set here.
--
-- Needs the psql variable api_role.
-- =====================================================================

-- work_mem is hpldn.backend.common.constants.API_ROLE_WORK_MEM (a test
-- holds the two equal).
SELECT format('ALTER ROLE %I SET statement_timeout = %L', :'api_role', '5s') \gexec
SELECT format('ALTER ROLE %I SET work_mem = %L', :'api_role', '16MB') \gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', :'api_role', '60s') \gexec
-- Nothing the API does writes.
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'api_role') \gexec
SELECT format('ALTER ROLE %I CONNECTION LIMIT 64', :'api_role') \gexec
