#!/usr/bin/env bash
# =====================================================================
# Let the read-only API role log in to a LOCAL PostgreSQL server.
#
# Run as the PostgreSQL superuser, after db_init.sql (next to this script) has
# created the role:
#
#     sudo -u postgres bash src/hpldn/backend/db/sql/db_enable_api_login.sh \
#          [--db hpdbldn] [--role hpldn_api] [--host localhost] [--port 5432]
#
# What it does, in one transaction:
#   * ALTER ROLE <role> WITH LOGIN PASSWORD <freshly generated>
#   * the role settings in fragments/api_role_settings.sql (statement_timeout,
#     work_mem, read-only transactions), shared with the serving host
# then connects AS the role over TCP and counts production.london_houses,
# and prints the POSTGRES_API_* lines to paste into develop.env.
#
# Why a script and not pipeline_steps.create_api_readonly_role: ALTER ROLE
# needs CREATEROLE or superuser, which the migration role deliberately does
# not have (see db_init.sql). On the serving host bootstrap.sh sets the
# password; locally, this is the path.
#
# Properties:
#   * No secret is committed, written to disk or passed in argv. The
#     password is generated here (openssl rand -hex 32: hex, so it needs no
#     quoting in SQL or escaping in a URL), sent to psql on stdin, and
#     printed once to stdout.
#   * The server log does not receive it: the transaction sets
#     log_statement = 'none', log_min_error_statement = 'panic' and
#     log_min_duration_statement = -1 locally, and turns off
#     pg_stat_statements' utility tracking for itself.
#   * Re-running rotates the password. Paste the new lines into develop.env
#     and restart uvicorn.
#   * Grants are untouched: CONNECT, USAGE on production and SELECT on its
#     tables come from db_init.sql and db_grants_post_migration.sql.
# =====================================================================
set -euo pipefail

DB="hpdbldn"
ROLE="hpldn_api"
HOST="localhost"
PORT="5432"
SQL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE_SETTINGS="$SQL_DIR/fragments/api_role_settings.sql"

usage() {
    sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "db_enable_api_login: $*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)   DB="${2:?--db needs a value}"; shift 2 ;;
        --role) ROLE="${2:?--role needs a value}"; shift 2 ;;
        --host) HOST="${2:?--host needs a value}"; shift 2 ;;
        --port) PORT="${2:?--port needs a value}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

command -v psql >/dev/null || die "psql is not on PATH"
command -v openssl >/dev/null || die "openssl is not on PATH"
[[ -r "$ROLE_SETTINGS" ]] || die "cannot read $ROLE_SETTINGS"

# ---------------------------------------------------------------------
# Preconditions, over the local socket as the invoking (superuser) role.
# ---------------------------------------------------------------------
IS_SUPERUSER="$(psql -X -At -v ON_ERROR_STOP=1 -d "$DB" <<'SQL'
SELECT rolsuper FROM pg_roles WHERE rolname = current_user;
SQL
)" || die "cannot connect to database '$DB' over the local socket (is it created? run as: sudo -u postgres bash $0)"
[[ "$IS_SUPERUSER" == "t" ]] || die "must run as a PostgreSQL superuser (sudo -u postgres bash $0)"

ROLE_EXISTS="$(psql -X -At -v ON_ERROR_STOP=1 -d "$DB" -v role="$ROLE" <<'SQL'
SELECT 1 FROM pg_roles WHERE rolname = :'role';
SQL
)" || die "cannot query pg_roles in database '$DB'"
[[ "$ROLE_EXISTS" == "1" ]] || die "role '$ROLE' does not exist; create it with db_init.sql first"

# ---------------------------------------------------------------------
# Password and per-role limits, one transaction, SQL on stdin only.
# ---------------------------------------------------------------------
PASSWORD="$(openssl rand -hex 32)"
[[ "$PASSWORD" =~ ^[0-9a-f]{64}$ ]] || die "openssl did not return a 64-character hex password"

# Unquoted heredoc: ${PASSWORD} and ${ROLE_SETTINGS} are expanded by the
# shell; :'role' and :'api_role' by psql. Nothing else in it contains a $.
psql -X -q -v ON_ERROR_STOP=1 -d "$DB" -v role="$ROLE" -v api_role="$ROLE" <<SQL
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = 'panic';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL pg_stat_statements.track_utility = off;
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'role', '${PASSWORD}')
\gexec
\i ${ROLE_SETTINGS}
COMMIT;
SQL
echo "db_enable_api_login: $ROLE can now log in (settings from $ROLE_SETTINGS)" >&2

# ---------------------------------------------------------------------
# Post-check AS the API role, over TCP -- the path the API itself takes.
# PGPASSWORD is set only in psql's own environment.
# ---------------------------------------------------------------------
STATUS=0
if ROWS="$(PGPASSWORD="$PASSWORD" psql -X -At -v ON_ERROR_STOP=1 \
        -h "$HOST" -p "$PORT" -U "$ROLE" -d "$DB" \
        -c "select count(*) from production.london_houses" 2>&1)"; then
    echo "db_enable_api_login: connected as $ROLE@$HOST:$PORT/$DB -- production.london_houses has $ROWS rows" >&2
else
    STATUS=2
    cat >&2 <<HINT
db_enable_api_login: the password is set, but connecting as $ROLE over TCP failed:

    $ROWS

If that is an authentication or "no pg_hba.conf entry" error, pg_hba.conf needs a
password line for TCP connections from this host, e.g.

    host    $DB    $ROLE    127.0.0.1/32    scram-sha-256
    host    $DB    $ROLE    ::1/128         scram-sha-256

then reload (sudo -u postgres psql -c 'SELECT pg_reload_conf()') and re-run this
script, which rotates the password again. A permission error on
production.london_houses means the grants are missing: run
db_grants_post_migration.sql (next to this script) as the migration role.
HINT
fi

# ---------------------------------------------------------------------
# The lines for develop.env. Nothing is written to disk.
# ---------------------------------------------------------------------
echo "db_enable_api_login: paste these lines into develop.env (replacing any existing POSTGRES_API_* lines):" >&2
cat <<LINES
POSTGRES_API_USERNAME=$ROLE
POSTGRES_API_PASSWORD=$PASSWORD
POSTGRES_API_HOST=$HOST
POSTGRES_API_PORT=$PORT
POSTGRES_API_DATABASE=$DB
POSTGRES_API_SSLMODE=prefer
LINES
exit "$STATUS"
