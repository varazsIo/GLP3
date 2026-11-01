#!/usr/bin/env bash
# =====================================================================
# Load a serving dump (export_serving_db.py) into the serving database,
# blue/green by schema, then restart the API on it.
#
#     sudo hpldn-restore-serving-db /opt/hpldn/serving/YYYY-MM-DD
#     sudo hpldn-restore-serving-db --no-restart DIR   # load and swap only
#     sudo hpldn-restore-serving-db --prewarm-only     # reload the live data into memory
#
# Installed as /usr/local/sbin/hpldn-restore-serving-db by bootstrap.sh; run
# as root. Steps:
#   1. verify the dump's Ed25519 SIGNATURE against the committed public keys
#      (hpldn-verify-dump), then its SHA256SUMS and manifest: nothing from an
#      unsigned or tampered dump is ever run;
#   2. apply api_role.sql (the hpldn_owner role, the API role's settings and
#      peer login), then rebuild schema production_next AS hpldn_owner, a
#      NOLOGIN non-superuser (PGOPTIONS -c role=hpldn_owner): schema.sql, one
#      binary COPY per table (row counts checked against the manifest),
#      post.sql (constraints and indexes), VACUUM (FREEZE, ANALYZE). The
#      dump's SQL never runs as the superuser, so COPY ... PROGRAM, file
#      reads and role changes are impossible whatever it holds;
#   3. apply the API role's grants to production_next;
#   4. in one transaction, as postgres (the only step that is): production ->
#      production_old, production_next -> production;
#   5. restart hpldn-api and wait until both workers have loaded the price
#      model; if they do not, swap back and restart on the old data;
#   6. drop production_old.
# The swap needs about twice the data on disk (~2 x 10 GB) while both exist.
# =====================================================================
set -euo pipefail

DB="${HPLDN_DB:-hpldn_serve}"
API_ROLE="${HPLDN_API_ROLE:-hpldn_api}"
SHARE_DIR="${HPLDN_SHARE_DIR:-/usr/local/share/hpldn}"
# gunicorn listens on a unix socket only (hpldn-api.socket).
API_SOCKET="${HPLDN_API_SOCKET:-/run/hpldn-api/api.sock}"
VERIFY_DUMP="${HPLDN_VERIFY_DUMP:-/usr/local/sbin/hpldn-verify-dump}"
OWNER_ROLE=hpldn_owner
READY_TIMEOUT_S="${HPLDN_READY_TIMEOUT_S:-900}"
LIVE=production
NEXT=production_next
OLD=production_old
MANIFEST_FORMAT=1

RESTART=1
PREWARM_ONLY=0
DUMP_DIR=""

log() { echo "[$(date -u +%H:%M:%S)] restore: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-restart) RESTART=0; shift ;;
        --prewarm-only) PREWARM_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) [[ -z "$DUMP_DIR" ]] || die "one dump directory only"; DUMP_DIR="$1"; shift ;;
    esac
done
(( PREWARM_ONLY )) || [[ -n "$DUMP_DIR" ]] || { usage; exit 2; }
(( ! PREWARM_ONLY )) || [[ -z "$DUMP_DIR" ]] || die "--prewarm-only takes no dump directory"
[[ $EUID -eq 0 ]] || die "run as root"
for cmd in jq zstd sha256sum runuser psql curl openssl; do
    command -v "$cmd" >/dev/null || die "$cmd is not installed"
done
(( PREWARM_ONLY )) || DUMP_DIR="$(cd "$DUMP_DIR" && pwd)"

# One restore at a time.
exec 9>/run/lock/hpldn-restore.lock
flock -n 9 || die "another restore is running"

pg() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d "$DB" "$@"; }
pg_value() { runuser -u postgres -- psql -X -q -At -v ON_ERROR_STOP=1 -d "$DB" "$@"; }

# Prewarm: a freshly restored schema (or a restarted cluster) is on disk only,
# and the first visitors' searches and figures would wait on it (a cold
# address search passes its 250 ms budget). Every relation of $LIVE goes into
# the OS page cache (prefetch, asynchronous); the address search table, the
# small lookup tables and every index the request path walks go into
# shared_buffers too. A failure only warns.
prewarm_sql() {
    cat <<SQL
SELECT count(pg_catalog.pg_prewarm(c.oid, 'prefetch'))
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = '$LIVE' AND c.relkind IN ('r', 'i', 'm');
SELECT count(pg_catalog.pg_prewarm(c.oid, 'buffer'))
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = '$LIVE'
   AND (c.relkind = 'i' OR c.relname IN ('address_search', 'london_rents', 'ofsted_rating_spells', 'cpih', 'ukhpi', 'mortgage_rates', 'boe_rate'));
SQL
}
prewarm_live() {
    local counts started=$SECONDS
    log "prewarming $LIVE"
    if counts="$(prewarm_sql | pg_value 2>&1)"; then
        log "prewarmed in $(( SECONDS - started ))s: $(head -n 1 <<<"$counts") relations into the page cache, $(tail -n 1 <<<"$counts") into shared_buffers"
    else
        log "WARNING: prewarm failed (bootstrap.sh creates the pg_prewarm extension): $counts"
    fi
}

if (( PREWARM_ONLY )); then
    prewarm_live
    exit 0
fi
# The dump's SQL and data, as the owner role. PGOPTIONS sets the session's
# role at connection start; extra -c settings may be appended by the caller.
pg_owner() {
    runuser -u postgres -- env PGOPTIONS="-c role=$OWNER_ROLE ${OWNER_PGOPTIONS:-}" \
        psql -X -q -v ON_ERROR_STOP=1 -d "$DB" "$@"
}

# ---------------------------------------------------------------------
# 1. Verify the dump.
# ---------------------------------------------------------------------
cd "$DUMP_DIR"
for f in manifest.json SHA256SUMS SIGNATURE schema.sql post.sql; do
    [[ -f "$f" ]] || die "$DUMP_DIR/$f is missing"
done
log "verifying the signature of $DUMP_DIR"
"$VERIFY_DUMP" "$DUMP_DIR" || die "the dump's signature does not verify; refusing to restore it"
log "verifying checksums in $DUMP_DIR"
sha256sum --check --quiet --strict SHA256SUMS || die "checksum mismatch; re-copy the dump"
jq -e --argjson f "$MANIFEST_FORMAT" '.format == $f' manifest.json >/dev/null \
    || die "manifest format is not $MANIFEST_FORMAT"
[[ "$(jq -r '.schema' manifest.json)" == "$LIVE" ]] || die "dump is not of schema $LIVE"
log "dump of $(jq -r '.source.database' manifest.json) at alembic $(jq -r '.source.alembic_revision' manifest.json), exported $(jq -r '.created_at' manifest.json)"

mapfile -t TABLES < <(jq -r '.tables[] | [.name, .file, (.rows | tostring)] | @tsv' manifest.json)
[[ ${#TABLES[@]} -gt 0 ]] || die "manifest lists no tables"
for entry in "${TABLES[@]}"; do
    IFS=$'\t' read -r name file rows <<<"$entry"
    # Names are interpolated into SQL below; accept plain identifiers only.
    [[ "$name" =~ ^[a-z_][a-z0-9_]*$ ]] || die "bad table name in manifest: $name"
    [[ "$file" == "data/$name.copy.zst" ]] || die "unexpected data file for $name: $file"
    [[ "$rows" =~ ^[0-9]+$ ]] || die "bad row count for $name: $rows"
done

# ---------------------------------------------------------------------
# 2. The owner role, then production_next built as it.
# ---------------------------------------------------------------------
STARTED=$SECONDS
# Creates hpldn_owner and hands it any serving schema a superuser made.
pg -v api_role="$API_ROLE" -v db="$DB" <"$SHARE_DIR/api_role.sql"
log "creating schema $NEXT in $DB as $OWNER_ROLE"
if [[ "$(pg_value -c "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = '$NEXT'")" =~ ^(|$OWNER_ROLE)$ ]]; then
    pg_owner -c "DROP SCHEMA IF EXISTS $NEXT CASCADE"
else
    pg -c "DROP SCHEMA $NEXT CASCADE"
fi
pg_owner -c "CREATE SCHEMA $NEXT"
# SQL goes in on stdin: the dump directory is root-only, and psql runs as postgres.
pg_owner -v schema="$NEXT" <schema.sql

for entry in "${TABLES[@]}"; do
    IFS=$'\t' read -r name file rows <<<"$entry"
    log "loading $name ($rows rows)"
    zstd -dc --quiet -- "$file" | pg_owner -c "COPY $NEXT.$name FROM STDIN (FORMAT binary)"
    loaded="$(pg_value -c "SELECT count(*) FROM $NEXT.$name")"
    [[ "$loaded" == "$rows" ]] || die "$name: loaded $loaded rows, manifest says $rows"
done

log "constraints and indexes"
OWNER_PGOPTIONS="-c maintenance_work_mem=2GB -c max_parallel_maintenance_workers=3" \
    pg_owner -v schema="$NEXT" <post.sql

log "VACUUM (FREEZE, ANALYZE)"
for entry in "${TABLES[@]}"; do
    IFS=$'\t' read -r name _ _ <<<"$entry"
    pg_owner -c "VACUUM (FREEZE, ANALYZE) $NEXT.$name"
done
foreign="$(pg_value -c "SELECT count(*) FROM pg_class WHERE relnamespace = '$NEXT'::regnamespace AND pg_get_userbyid(relowner) <> '$OWNER_ROLE'")"
[[ "$foreign" == 0 ]] || die "$foreign relations in $NEXT are not owned by $OWNER_ROLE"

for fn in $(jq -r '.functions[]' manifest.json); do
    [[ "$fn" =~ ^[a-z_][a-z0-9_]*$ ]] || die "bad function name in manifest: $fn"
    pg_value -c "SELECT to_regproc('$NEXT.$fn') IS NOT NULL" | grep -qx t \
        || die "function $NEXT.$fn is missing after schema.sql"
done

# ---------------------------------------------------------------------
# 3. Grants, before the API can see the schema.
# ---------------------------------------------------------------------
pg -v api_role="$API_ROLE" -v schema="$NEXT" <"$SHARE_DIR/grants.sql"
log "$NEXT built in $(( SECONDS - STARTED ))s"

# ---------------------------------------------------------------------
# 4. Swap.
# ---------------------------------------------------------------------
swap_in() {
    pg <<SQL
BEGIN;
SET LOCAL lock_timeout = '30s';
DROP SCHEMA IF EXISTS $OLD CASCADE;
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '$LIVE') THEN
        ALTER SCHEMA $LIVE RENAME TO $OLD;
    END IF;
END;
\$\$;
ALTER SCHEMA $NEXT RENAME TO $LIVE;
COMMIT;
SQL
}

swap_back() {
    pg <<SQL
BEGIN;
SET LOCAL lock_timeout = '30s';
DROP SCHEMA IF EXISTS $NEXT CASCADE;
ALTER SCHEMA $LIVE RENAME TO $NEXT;
ALTER SCHEMA $OLD RENAME TO $LIVE;
COMMIT;
SQL
}

log "swapping $NEXT in as $LIVE"
swap_in
install -m 0640 -o root -g root "$DUMP_DIR/manifest.json" /etc/hpldn/serving-manifest.json

# 4b. Prewarm the new data (prewarm_live, above).
prewarm_live

# ---------------------------------------------------------------------
# 5. Restart the API and wait for both workers' price models.
# ---------------------------------------------------------------------
workers() {
    local w
    w="$(systemctl show -p Environment hpldn-api | grep -o 'HPLDN_GUNICORN_WORKERS=[0-9]*' | cut -d= -f2 || true)"
    echo "${w:-2}"
}

wait_ready() {
    local since="$1" deadline=$(( SECONDS + READY_TIMEOUT_S )) want ready failed
    want="$(workers)"
    until curl -fsS -o /dev/null --unix-socket "$API_SOCKET" http://localhost/ready; do
        (( SECONDS < deadline )) || { log "API /ready did not answer in ${READY_TIMEOUT_S}s"; return 1; }
        systemctl is-active --quiet hpldn-api || { log "hpldn-api is not running"; return 1; }
        sleep 5
    done
    # Each worker logs one line when its price model and sales history are
    # loaded (api/market/pricing.py), or one when loading failed.
    while :; do
        ready="$(journalctl -u hpldn-api --since "$since" -o cat | grep -c 'Model pricing ready' || true)"
        failed="$(journalctl -u hpldn-api --since "$since" -o cat | grep -c 'Model pricing unavailable' || true)"
        (( failed == 0 )) || { log "a worker could not load the price model"; return 1; }
        (( ready >= want )) && return 0
        (( SECONDS < deadline )) || { log "price model not loaded by all workers in ${READY_TIMEOUT_S}s"; return 1; }
        sleep 10
    done
}

if (( RESTART )); then
    since="$(date '+%Y-%m-%d %H:%M:%S')"
    log "restarting hpldn-api"
    systemctl restart hpldn-api
    if ! wait_ready "$since"; then
        if pg_value -c "SELECT 1 FROM pg_namespace WHERE nspname = '$OLD'" | grep -qx 1; then
            log "rolling back to the previous data"
            swap_back
            systemctl restart hpldn-api
            die "the API did not come up on the new data; the previous data is live again and the new schema is kept as $NEXT for inspection"
        fi
        die "the API did not come up, and there was no previous schema to fall back to"
    fi
    log "API ready on the new data"
else
    log "--no-restart: the API still holds the old data in memory until it is restarted"
fi

# ---------------------------------------------------------------------
# 6. Drop the old data.
# ---------------------------------------------------------------------
pg -c "DROP SCHEMA IF EXISTS $OLD CASCADE"
log "done in $(( SECONDS - STARTED ))s: $(jq '[.tables[].rows] | add' manifest.json) rows in ${#TABLES[@]} tables"
