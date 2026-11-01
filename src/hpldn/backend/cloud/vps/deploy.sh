#!/usr/bin/env bash
# =====================================================================
# Deploy HPLDN from the laptop to the serving host, over SSH on the tailnet.
#
#     src/hpldn/backend/cloud/vps/deploy.sh                          # app + frontend + model
#     src/hpldn/backend/cloud/vps/deploy.sh --dump tmp/serving/YYYY-MM-DD   # ... and new data
#     src/hpldn/backend/cloud/vps/deploy.sh --skip-model --skip-frontend    # code only
#     src/hpldn/backend/cloud/vps/deploy.sh --bootstrap --domain hpldn.example.co.uk
#
# Usually run by `python -m hpldn.backend.cloud.oci up|deploy` (provision.py).
#
# Options:
#   --host H          SSH host (default: $HPLDN_HOST or "hpldn", the tailnet name)
#   --user U          SSH user with passwordless sudo (default: ubuntu)
#   --dump DIR        a SIGNED serving dump (export_serving_db --sign); restored blue/green
#   --model DIR       the model bundle (default: tmp/models/model_anchored)
#   --skip-app / --skip-frontend / --skip-model
#   --allow-dirty     deploy uncommitted changes to tracked files (untracked
#                     files are never deployed; `git add` them first)
#   --bootstrap       copy this bundle to the host and run bootstrap.sh (first
#                     run, over the tailnet); needs --domain
#
# SSH trusts only the host keys pinned in ~/.config/hpldn/known_hosts
# ($HPLDN_KNOWN_HOSTS), which provision.py writes from the instance's console
# output at first boot: StrictHostKeyChecking=yes, no trust on first use.
#
# What happens on the host (all as root via sudo):
#   app       a wheel built locally from the commit (uv build --wheel; no tests
#             or dev files are shipped), uv.lock and pyproject.toml; the
#             locked dependencies (uv sync --frozen --extra api) and the wheel
#             go into the idle venv slot (/opt/hpldn/venv-a|b), the hpldn
#             package is compiled to .pyc and every .py of it deleted (a
#             sourceless install), the import and model-sandbox smoke tests
#             run against that, then /opt/hpldn/venv is flipped to it; the
#             cloud/vps bundle goes to /opt/hpldn/vps and bootstrap.sh
#             --configs-only re-installs nginx, PostgreSQL and systemd files.
#   frontend  npm run build:prod locally (minified, no source maps; no Node
#             on the host), refused if any *.map, sourceMappingURL or
#             asset-manifest.json is left; rsync to /var/www/hpldn.
#   model     the signed bundle -> /opt/hpldn/models/<name>; verified
#             locally, then on the host as the hpldn user (signature against
#             the package's trusted_keys/, hashes, schemas, sandboxed CatBoost
#             round-trip, summary, probe) before HPLDN_PRICE_MODEL_FP moves.
#   data      dump -> /opt/hpldn/serving/<name>, hpldn-restore-serving-db
#             (which checks its signature first), which restarts the API;
#             otherwise the API is restarted here.
# It then waits until both workers have loaded the model, checks the API on
# its unix socket (/areas, the reCAPTCHA gate, /docs absent, no server
# header), checks nginx refuses a client without the origin-pull certificate,
# and restarts hpldn-guard.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
BUNDLE_REL=src/hpldn/backend/cloud/vps
FRONTEND_DIR=$REPO_ROOT/src/hpldn/frontend

HOST="${HPLDN_HOST:-hpldn}"
SSH_USER=ubuntu
DUMP=""
MODEL="$REPO_ROOT/tmp/models/model_anchored"
DO_APP=1
DO_FRONTEND=1
DO_MODEL=1
ALLOW_DIRTY=0
BOOTSTRAP=0
DOMAIN=""
KNOWN_HOSTS="${HPLDN_KNOWN_HOSTS:-$HOME/.config/hpldn/known_hosts}"

log() { echo "[$(date +%H:%M:%S)] deploy: $*" >&2; }
die() { echo "deploy: ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,49p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="${2:?}"; shift 2 ;;
        --user) SSH_USER="${2:?}"; shift 2 ;;
        --dump) DUMP="${2:?}"; shift 2 ;;
        --model) MODEL="${2:?}"; shift 2 ;;
        --skip-app) DO_APP=0; shift ;;
        --skip-frontend) DO_FRONTEND=0; shift ;;
        --skip-model) DO_MODEL=0; shift ;;
        --allow-dirty) ALLOW_DIRTY=1; shift ;;
        --bootstrap) BOOTSTRAP=1; shift ;;
        --domain) DOMAIN="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done

[[ -s "$KNOWN_HOSTS" ]] || die "no pinned host keys in $KNOWN_HOSTS; provision.py up writes them from the console (StrictHostKeyChecking=yes)"
SSH_OPTS=(-o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" -o GlobalKnownHostsFile=/dev/null
          -o HashKnownHosts=no -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ForwardAgent=no)
TARGET="$SSH_USER@$HOST"
remote() { ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" "$@"; }
push() { rsync -az --partial -e "ssh ${SSH_OPTS[*]} -o BatchMode=yes" --rsync-path="sudo rsync" "$@"; }

# SSH, then sudo, each with its own failure message. The public address has
# no port 22 (the NSG and the host firewall admit SSH over the tailnet only),
# unless break-glass is on.
check_ssh() {
    if ! remote true; then
        local hint="is this laptop on the tailnet (tailscale status) and is $HOST online there?"
        if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && ! "$HOST" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]; then
            hint="$HOST is not a tailnet address and public SSH is closed by design; use --host hpldn (the default), or for break-glass set bootstrap_ssh = true and admin_cidr in cloud/oci (README.md, Break-glass)"
        fi
        die "cannot SSH to $TARGET: $hint"
    fi
    remote "sudo -n true" || die "$SSH_USER on $HOST has no passwordless sudo"
}

# The serving host's postgres/api_role.sql and grants.sql are build output,
# rendered from db/sql/fragments (the source the local scripts use too), so
# render them before anything is copied.
log "rendering postgres/api_role.sql and postgres/grants.sql from db/sql"
(cd "$REPO_ROOT" && uv run --quiet python -m hpldn.backend.db.render_vps_sql >/dev/null) \
    || die "python -m hpldn.backend.db.render_vps_sql failed"

# ---------------------------------------------------------------------
# First run: copy the bundle and run bootstrap.sh.
# ---------------------------------------------------------------------
if (( BOOTSTRAP )); then
    [[ -n "$DOMAIN" ]] || die "--bootstrap needs --domain"
    check_ssh
    log "copying the bundle to $TARGET:hpldn-vps"
    rsync -az --delete --exclude '__pycache__' -e "ssh ${SSH_OPTS[*]}" "$REPO_ROOT/$BUNDLE_REL/" "$TARGET:hpldn-vps/"
    log "running bootstrap.sh"
    tty=()
    [[ -t 0 ]] && tty=(-t)
    ssh "${SSH_OPTS[@]}" "${tty[@]}" "$TARGET" sudo bash hpldn-vps/bootstrap.sh --domain "$DOMAIN"
    exit 0
fi

# ---------------------------------------------------------------------
# Local preflight: fail before touching the host.
# ---------------------------------------------------------------------
cd "$REPO_ROOT"
TREE=HEAD
REVISION="$(git rev-parse HEAD)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 0700 "$WORK"

if (( DO_APP )); then
    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
        (( ALLOW_DIRTY )) || die "uncommitted changes to tracked files; commit them or pass --allow-dirty"
        TREE="$(git stash create)"
        REVISION="$REVISION-dirty"
    fi
    untracked="$(git ls-files --others --exclude-standard -- src/hpldn/backend | head -n 5)"
    [[ -z "$untracked" ]] || log "WARNING: untracked files are not deployed: ${untracked//$'\n'/, }"
    [[ -f uv.lock ]] || die "uv.lock is missing at $REPO_ROOT"
    # The wheel is built from the commit (git archive), not the working tree:
    # only tracked files, and only the package (no tests/dev files are in it).
    log "app: building the wheel of $REVISION"
    mkdir -p "$WORK/src" "$WORK/dist"
    git archive --format=tar "$TREE" | tar -x -C "$WORK/src"
    uv build --quiet --wheel --out-dir "$WORK/dist" "$WORK/src" || die "uv build --wheel failed"
    WHEEL="$(find "$WORK/dist" -name 'hpldn-*.whl' | head -n 1)"
    [[ -n "$WHEEL" ]] || die "uv build produced no wheel"
    if python3 -c 'import sys, zipfile; print("\n".join(zipfile.ZipFile(sys.argv[1]).namelist()))' "$WHEEL" \
        | grep -Eq '(^|/)tests/|(^|/)conftest\.py$|/frontend/'; then
        die "the wheel holds tests or the frontend: check [tool.setuptools.packages.find]"
    fi
    cp "$WORK/src/pyproject.toml" "$WORK/src/uv.lock" "$WORK/dist/"
fi

MODEL_KEY=""
if (( DO_MODEL )); then
    MODEL="$(cd "$MODEL" 2>/dev/null && pwd)" || die "model bundle $MODEL does not exist"
    [[ -f "$MODEL/manifest.json" ]] || die "$MODEL is not a model bundle (no manifest.json); convert a .cbm with python -m hpldn.backend.models.artifact convert"
    [[ "$(basename "$MODEL")" =~ ^[A-Za-z0-9._-]+$ ]] || die "unexpected model bundle name $(basename "$MODEL")"
    log "verifying the model bundle locally"
    (cd "$REPO_ROOT" && uv run --quiet python -m hpldn.backend.models.artifact verify "$MODEL" >/dev/null) \
        || die "$MODEL fails verification (python -m hpldn.backend.models.artifact verify $MODEL)"
    MODEL_KEY="$(awk '{print $2}' "$MODEL/manifest.sig")"
    [[ "$MODEL_KEY" =~ ^[0-9a-f]{16}$ ]] || die "malformed key id in $MODEL/manifest.sig"
    git ls-files --error-unmatch "src/hpldn/backend/models/trusted_keys/$MODEL_KEY.pem" >/dev/null 2>&1 \
        || die "the bundle's signing key $MODEL_KEY is not committed under src/hpldn/backend/models/trusted_keys/; the host would refuse it"
fi

if [[ -n "$DUMP" ]]; then
    DUMP="$(cd "$DUMP" && pwd)"
    [[ -f "$DUMP/manifest.json" && -f "$DUMP/SHA256SUMS" ]] || die "$DUMP is not a serving dump (no manifest.json/SHA256SUMS)"
    [[ ! -e "$DUMP.partial" ]] || die "$DUMP.partial exists; is an export still running?"
    log "verifying the dump's signature and checksums locally"
    (cd "$REPO_ROOT" && uv run --quiet python -m hpldn.backend.scripts.export_serving_db --verify "$DUMP" >/dev/null) \
        || die "$DUMP is unsigned or does not verify (export_serving_db --sign-only $DUMP); the host would refuse it"
    DUMP_KEY="$(awk '{print $2}' "$DUMP/SIGNATURE")"
    git ls-files --error-unmatch "$BUNDLE_REL/trusted_keys/$DUMP_KEY.pem" >/dev/null 2>&1 \
        || die "the dump's signing key $DUMP_KEY is not committed under $BUNDLE_REL/trusted_keys/; the host would refuse it"
    if (( DO_MODEL )) && [[ -f "$DUMP/models.sha256" ]]; then
        if ! (cd "$(dirname "$MODEL")" && sha256sum --check --quiet --status "$DUMP/models.sha256" 2>/dev/null); then
            log "WARNING: the model differs from the one recorded when the dump was exported"
        fi
    fi
fi

if (( DO_FRONTEND )); then
    log "frontend: npm run build:prod (no source maps)"
    (
        cd "$FRONTEND_DIR"
        unset REACT_APP_API_BASE
        npm run build:prod --silent
    )
    [[ -f "$FRONTEND_DIR/build/index.html" ]] || die "the frontend build produced no index.html"
    leaks="$(find "$FRONTEND_DIR/build" \( -name '*.map' -o -name 'asset-manifest.json' \) | head -n 3)"
    [[ -z "$leaks" ]] || die "the build holds source maps or the asset manifest: ${leaks//$'\n'/, }"
    if grep -rlq 'sourceMappingURL' "$FRONTEND_DIR/build"; then
        die "the build still references a source map (sourceMappingURL)"
    fi
    # nginx serves these .gz files as they are (gzip_static), so a Cloudflare
    # cache miss fetches a third of the bytes and nginx compresses nothing.
    find "$FRONTEND_DIR/build/static" -type f \( -name '*.js' -o -name '*.css' \) -exec gzip -9kf {} +
fi

log "checking $TARGET"
check_ssh
remote "test -x /usr/local/sbin/hpldn-restore-serving-db" \
    || die "$HOST is not bootstrapped: python -m hpldn.backend.cloud.oci bootstrap (bootstrap.sh, then the secrets)"
remote "sudo grep -q '^RECAPTCHA_SITE_KEY=.' /etc/hpldn/api.env && sudo test -s /etc/credstore.encrypted/hpldn.recaptcha_secret_key" \
    || log "WARNING: no reCAPTCHA keys on the host: the API will not start while guard.yaml captcha.enabled is on (ssh $TARGET sudo bash /opt/hpldn/vps/bootstrap.sh --set-recaptcha-keys)"

# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------
if (( DO_APP )); then
    log "app: $REVISION ($(basename "$WHEEL"))"
    remote "sudo rm -rf /opt/hpldn/incoming/app /opt/hpldn/incoming/vps && sudo install -d -m 0700 /opt/hpldn/incoming/app /opt/hpldn/incoming/vps"
    push --chown=root:root --chmod=D700,F600 "$WHEEL" "$WORK/dist/pyproject.toml" "$WORK/dist/uv.lock" "$TARGET:/opt/hpldn/incoming/app/"
    # The host's bundle, with the rendered (untracked) api_role.sql and grants.sql.
    push --chown=root:root --chmod=D700,F600 --exclude '__pycache__' "$REPO_ROOT/$BUNDLE_REL/" "$TARGET:/opt/hpldn/incoming/vps/"
    remote "sudo bash -s -- '$REVISION' '$(basename "$WHEEL")'" <<'REMOTE'
set -euo pipefail
revision="$1"
wheel="$2"
cd /opt/hpldn
current="$(readlink venv 2>/dev/null || true)"
if [[ "$current" == venv-a ]]; then next=venv-b; else next=venv-a; fi
rm -rf "$next"
export UV_PYTHON_INSTALL_DIR=/opt/hpldn/python UV_PYTHON_PREFERENCE=only-managed \
       UV_CACHE_DIR=/var/cache/hpldn-uv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
       UV_PROJECT_ENVIRONMENT="/opt/hpldn/$next"
cd incoming/app
# The locked dependencies only (hpldn itself is the wheel, installed next).
uv sync --frozen --extra api --no-dev --no-install-project --python 3.14 --quiet
uv pip install --python "/opt/hpldn/$next/bin/python" --no-deps --quiet "./$wheel"
py="/opt/hpldn/$next/bin/python"
pkg="$("$py" -I -c 'import hpldn, os; print(os.path.dirname(hpldn.__file__))')"
[[ "$pkg" == /opt/hpldn/$next/* ]] || { echo "hpldn resolved to $pkg, not the new slot" >&2; exit 1; }
# Sourceless: legacy .pyc beside each module (-b), then no .py and no
# __pycache__ left in the hpldn package. Dependencies keep their sources.
"$py" -I -m compileall -b -q -j 0 "$pkg" >/dev/null
find "$pkg" -name '*.py' -type f -delete
find "$pkg" -name '__pycache__' -type d -prune -exec rm -rf {} +
if find "$pkg" -name '*.py' -print -quit | grep -q .; then echo "a .py file survived in $pkg" >&2; exit 1; fi
echo "$revision" >"/opt/hpldn/$next/REVISION"
chown -R root:hpldn "/opt/hpldn/$next"
chmod -R g-w,o-rwx "/opt/hpldn/$next"
chmod -R g+rX "/opt/hpldn/$next"
# Smoke tests against the sourceless tree, as the service user.
runuser -u hpldn -- env -i HOME=/nonexistent "$py" -I -c 'import hpldn.backend.api.main, hpldn.backend.api.worker, hpldn.backend.guard.__main__' >/dev/null
runuser -u hpldn -- env -i HOME=/nonexistent "$py" -I -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("hpldn.backend.models._cbm_sandbox") else 1)'
cd /opt/hpldn
ln -sfn "$next" venv.tmp && mv -T venv.tmp venv
echo "venv -> $next"
# The bundle the stdin modes and --configs-only run from.
rm -rf vps.old
[[ -d vps ]] && mv vps vps.old
mv incoming/vps vps
chmod -R go-rwx vps
echo "$revision" >vps/REVISION
rm -rf vps.old incoming/app
# The pre-wheel layout's source tree is gone for good.
rm -rf app
bash vps/bootstrap.sh --configs-only
REMOTE
fi

# ---------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------
if (( DO_FRONTEND )); then
    # Old hashed bundles under static/ are kept, so a visitor holding the
    # previous index.html can still load its chunks. Source maps never go.
    push --delete-delay --delay-updates --filter='P static/**' --exclude '*.map' --exclude 'asset-manifest.json' \
        --chown=root:root --chmod=D755,F644 \
        "$FRONTEND_DIR/build/" "$TARGET:/var/www/hpldn/"
fi

# ---------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------
if (( DO_MODEL )); then
    NAME="$(basename "$MODEL")"
    log "model: bundle $NAME signed by $MODEL_KEY ($(du -sh "$MODEL" | cut -f1))"
    if (( ! DO_APP )); then
        remote "sudo bash -c 'compgen -G \"/opt/hpldn/venv/lib/python3*/site-packages/hpldn/backend/models/trusted_keys/$MODEL_KEY.pem\" >/dev/null'" \
            || die "the host's app does not trust key $MODEL_KEY yet; deploy the app too (drop --skip-app)"
    fi
    remote "sudo install -d -o root -g hpldn -m 0750 /opt/hpldn/models && sudo rm -rf '/opt/hpldn/models/.incoming-$NAME'"
    push --chown=root:hpldn --chmod=D750,F640 "$MODEL/" "$TARGET:/opt/hpldn/models/.incoming-$NAME/"
    remote "sudo bash -s -- '$NAME'" <<'REMOTE'
set -euo pipefail
name="$1"
cd /opt/hpldn/models
# Every check, as the service user (never root) and before anything points at it.
runuser -u hpldn -- /opt/hpldn/venv/bin/python -m hpldn.backend.models.artifact verify ".incoming-$name"
rm -rf "$name.old"
[[ -d "$name" ]] && mv "$name" "$name.old"
mv ".incoming-$name" "$name"
rm -rf "$name.old"
sed -i "s|^HPLDN_PRICE_MODEL_FP=.*|HPLDN_PRICE_MODEL_FP=/opt/hpldn/models/$name|" /etc/hpldn/api.env
grep -q "^HPLDN_PRICE_MODEL_FP=/opt/hpldn/models/$name$" /etc/hpldn/api.env
REMOTE
fi

# ---------------------------------------------------------------------
# Data, then restart
# ---------------------------------------------------------------------
if [[ -n "$DUMP" ]]; then
    NAME="$(basename "$DUMP")"
    [[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "unexpected dump directory name $NAME"
    log "data: $NAME ($(du -sh "$DUMP" | cut -f1))"
    push --chown=root:root --chmod=D700,F600 "$DUMP/" "$TARGET:/opt/hpldn/serving/$NAME/"
    # Checks the signature, restores blue/green as hpldn_owner and restarts
    # the API on the new data; rolls back and fails if the API does not come up.
    remote "sudo hpldn-restore-serving-db /opt/hpldn/serving/$NAME"
    # Keep the newest two dumps for a quick re-restore.
    remote "sudo bash -c 'ls -1dt /opt/hpldn/serving/*/ | tail -n +3 | xargs -r rm -rf'"
else
    log "restarting hpldn-api"
    remote "sudo bash -s" <<'REMOTE'
set -euo pipefail
since="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart hpldn-api
deadline=$(( SECONDS + 900 ))
until curl -fsS -o /dev/null --unix-socket /run/hpldn-api/api.sock http://localhost/ready; do
    systemctl is-active --quiet hpldn-api || { journalctl -u hpldn-api -n 30 --no-pager; exit 1; }
    (( SECONDS < deadline )) || { echo "API not ready in 900s" >&2; exit 1; }
    sleep 5
done
while :; do
    ready="$(journalctl -u hpldn-api --since "$since" -o cat | grep -c 'Model pricing ready' || true)"
    failed="$(journalctl -u hpldn-api --since "$since" -o cat | grep -c 'Model pricing unavailable' || true)"
    (( failed == 0 )) || { journalctl -u hpldn-api --since "$since" -o cat | grep 'Model pricing unavailable' >&2; exit 1; }
    (( ready >= 2 )) && break
    (( SECONDS < deadline )) || { echo "price model not loaded by both workers in 900s" >&2; exit 1; }
    sleep 10
done
REMOTE
fi

# ---------------------------------------------------------------------
# Smoke test on the host. nginx answers only Cloudflare's origin-pull
# certificate, so the API is checked on its socket with the key (from its
# credential, in a 0600 header file: never argv), and nginx is checked to
# refuse a client without the certificate.
# ---------------------------------------------------------------------
log "smoke test: the API on its socket, open search, the reCAPTCHA gate, docs off, nginx's origin-pull check, / through Cloudflare"
remote "sudo bash -s" <<'REMOTE'
set -euo pipefail
domain="$(cat /etc/hpldn/domain)"
sock=/run/hpldn-api/api.sock
hdr="$(mktemp)"
trap 'rm -f "$hdr"' EXIT
chmod 0600 "$hdr"
printf 'X-API-Key: %s\n' "$(systemd-creds decrypt --name=api_key /etc/credstore.encrypted/hpldn.api_key -)" >"$hdr"
call() { curl -s -A hpldn-deploy-smoke -H "@$hdr" --unix-socket "$sock" "$@" "http://localhost$path"; }
path=/areas
code="$(call -o /dev/null -w '%{http_code}')"
[[ "$code" == 200 ]] || { echo "GET $path answered $code" >&2; exit 1; }
# The market figures are loaded before a worker accepts (api/market/cache.py):
# no visitor may wait for them. Each call may land on either worker.
for _ in 1 2 3 4; do
    ms="$(call -o /dev/null -w '%{time_total}' | awk '{printf "%d", $1 * 1000}')"
    (( ms < 250 )) || { echo "GET $path took ${ms} ms on a ready API; the market figures are loading on the request path" >&2; exit 1; }
done
headers="$(call -D - -o /dev/null)"
grep -qi '^cache-control: public' <<<"$headers" || { echo "GET $path has no public Cache-Control: nginx and Cloudflare would not keep it" >&2; exit 1; }
grep -qi '^etag: ' <<<"$headers" || { echo "GET $path has no ETag" >&2; exit 1; }
path=/session/config
body="$(call -f)" || { echo "GET $path failed" >&2; exit 1; }
jq -e '.enabled == true and (.siteKey | length > 0)' <<<"$body" >/dev/null \
    || { echo "GET $path answered $body, expected enabled with a site key" >&2; exit 1; }
# Address search is open (guard.yaml captcha.gated_routes): a first search
# must not wait for reCAPTCHA.
path='/addresses/search?q=SW11&limit=1'
code="$(call -o /dev/null -w '%{http_code}')"
[[ "$code" == 200 ]] || { echo "GET $path without a pass answered $code, expected 200 (it must not need a reCAPTCHA pass)" >&2; exit 1; }
path=/price
code="$(call -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{}')"
[[ "$code" == 403 ]] || { echo "POST $path without a pass answered $code, expected 403" >&2; exit 1; }
for path in /docs /redoc /openapi.json; do
    code="$(call -o /dev/null -w '%{http_code}')"
    [[ "$code" == 404 ]] || { echo "GET $path answered $code, expected 404 (HPLDN_API_DOCS must be unset)" >&2; exit 1; }
done
path=/health
if call -D - -o /dev/null | grep -qi '^server:'; then echo "the API sends a server header" >&2; exit 1; fi
# The origin certificate and AOP CA from --set-tls, not bootstrap.sh's
# placeholders: Cloudflare answers 526 for the self-signed one, and the
# placeholder CA refuses its client certificate too.
if [[ -e /etc/hpldn/tls/placeholder ]]; then
    echo "nginx still has bootstrap.sh's placeholder certificate and AOP CA (Cloudflare answers 526): run python -m hpldn.backend.cloud.oci bootstrap, which pushes --set-tls" >&2; exit 1
fi
# root reaches the socket whatever its directory's mode; nginx's workers must too.
sudo -u www-data test -w "$sock" || { echo "nginx (www-data) cannot open $sock: every /api/ call would be a 502" >&2; exit 1; }
# Without the origin-pull client certificate, nginx must not answer at all.
if curl -sk -o /dev/null --max-time 10 --resolve "$domain:443:127.0.0.1" "https://$domain/"; then
    echo "nginx answered a client without the origin-pull certificate" >&2; exit 1
fi
# Through Cloudflare, the page (no-cache: never answered from the edge) must
# answer within 1.5 s, best of three. A second, dead A record for the site
# makes every request Cloudflare cannot cache wait ~19 s.
best_ms=999999
for attempt in 1 2 3; do
    s="$(curl -fsS -o /dev/null --max-time 30 -A 'Mozilla/5.0 (hpldn deploy smoke test)' -w '%{time_total}' "https://$domain/?smoke=$attempt")" || s=999
    ms="$(awk '{printf "%d", $1 * 1000}' <<<"$s")"
    (( ms < best_ms )) && best_ms=$ms
done
(( best_ms < 1500 )) || { echo "GET https://$domain/ through Cloudflare took ${best_ms} ms at best (limit 1500): a stray origin record in the zone, or a slow origin" >&2; exit 1; }
echo "API: /areas 200 (warm, public, ETag), /session/config enabled, search 200 without a pass, POST /price without a pass 403, no docs, no server header; origin certificate installed, nginx refuses clients without the AOP certificate; / through Cloudflare in ${best_ms} ms; app $(cat /opt/hpldn/venv/REVISION 2>/dev/null || echo '?')"
REMOTE

log "restarting hpldn-guard"
remote "sudo bash -s" <<'REMOTE'
set -euo pipefail
systemctl restart hpldn-guard
sleep 3
systemctl is-active --quiet hpldn-guard || { journalctl -u hpldn-guard -n 30 --no-pager; exit 1; }
test -S /run/hpldn-guard/log.sock || { echo "hpldn-guard is up but has no log socket" >&2; exit 1; }
sudo -u www-data test -w /run/hpldn-guard/log.sock || { echo "nginx (www-data) cannot write /run/hpldn-guard/log.sock: the guard sees no traffic" >&2; exit 1; }
echo "hpldn-guard: running, $(hpldn-guard list 2>&1 >/dev/null | tail -n 1)"
REMOTE
log "done"
