#!/usr/bin/env bash
# =====================================================================
# One-time (idempotent) setup of the HPLDN serving host: Ubuntu 24.04 on an
# OCI Ampere A1.Flex VM (aarch64), or any x86_64/aarch64 Ubuntu 24.04 VPS.
#
#     sudo bash bootstrap.sh --domain hpldn.example.co.uk   # first run (deploy.sh --bootstrap)
#     sudo bash bootstrap.sh --configs-only       # re-install this bundle's configs (deploy.sh)
#     sudo bash bootstrap.sh --close-public-ssh   # break-glass only: shut port 22 again
#
# Secrets, each read from stdin (never argv, never the environment): one per
# line, or a tar stream for --set-tls. Interactively they are prompted for
# without echo; provision.py pipes them over SSH.
#     sudo bash bootstrap.sh --set-tls                # tar: origin.crt origin.key aop_ca.pem
#     sudo bash bootstrap.sh --set-recaptcha-keys     # site key, then secret key
#     sudo bash bootstrap.sh --set-cloudflare-token   # the guard's token (Account Rule Lists: Write)
#     sudo bash bootstrap.sh --set-cloudflare-account # the account id, for the guard's IP list
#     sudo bash bootstrap.sh --rotate-pass-secret     # new signing secret for reCAPTCHA passes
#     sudo bash bootstrap.sh --rotate-api-key         # new X-API-Key for nginx and the API
#
# It installs PostgreSQL 18 (PGDG) with pg_trgm, nginx, uv + Python 3.14 and
# Tailscale (its apt key pinned by fingerprint); creates the hpldn and
# hpldn-guard users, /opt/hpldn and the serving database with its owner role
# and peer-authenticated, read-only API role; writes /etc/hpldn/api.env
# (non-secret settings only), the secrets as systemd credentials
# (/etc/credstore.encrypted/hpldn.<name>, encrypted with the host key: A1 has
# no TPM), nginx's API-key include, /etc/hpldn/guard.yaml and the nginx
# limits rendered from it; installs the firewall, the dump verifier, the
# restore tool, the guard and the systemd units (the API socket-activated on
# /run/hpldn-api/api.sock); hardens sysctl, journald, apt (HTTPS) and sshd;
# turns on unattended upgrades. The host joins the tailnet at first boot
# (cloud-init); public SSH never opens unless this runs over it (break-glass).
# It does not deploy the application, the model or the data: deploy.sh does,
# from the laptop. Re-running it is safe; secrets that already exist are kept.
# =====================================================================
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT=/opt/hpldn
WEB_ROOT=/var/www/hpldn
ETC_DIR=/etc/hpldn
ENV_FILE=$ETC_DIR/api.env
GUARD_ENV_FILE=$ETC_DIR/guard.env
GUARD_CONFIG=$ETC_DIR/guard.yaml
GUARD_LOCAL_CONFIG=$ETC_DIR/guard.local.yaml
GUARD_USER=hpldn-guard
GUARD_STATE=/var/lib/hpldn-guard
TLS_DIR=$ETC_DIR/tls
NGINX_INC=/etc/nginx/hpldn
SHARE_DIR=/usr/local/share/hpldn
# This bundle's installed copy, from which the stdin modes are run later.
VPS_DIR=$APP_ROOT/vps
CREDSTORE=/etc/credstore.encrypted
PG_MAJOR=18
PG_CONF_DIR=/etc/postgresql/$PG_MAJOR/main
PG_UNIT=postgresql@$PG_MAJOR-main
DB=hpldn_serve
API_ROLE=hpldn_api
SERVICE_USER=hpldn
TS_HOSTNAME=hpldn
# cloud-init writes the single-use tailnet key here (0600) and shreds it
# after `tailscale up`; bootstrap.sh only uses it if cloud-init could not.
TS_KEY_FILE=/run/hpldn/ts.key
# pkgs.tailscale.com's signing key (Tailscale Inc. (Package repository
# signing key) <info@tailscale.com>). The downloaded keyring is refused
# unless its primary key has exactly this fingerprint.
TAILSCALE_KEY_FPR=2596A99EAAB33821893C0A79458CA832957F5868  # gitleaks:allow (public fingerprint or test fixture)
DEFAULT_MODEL_FP=$APP_ROOT/models/model_anchored.cbm

# Pinned by version and verified against its published SHA-256 before it is
# executed (scripts/install_tofu.sh pins tofu the same way). To move to a
# newer uv, bump the version and both digests together.
UV_VERSION=0.12.7
UV_SHA256_AARCH64=66393193038dd7eb108abd7a218d9cec04ac70ab98242b0720fa94de19223b7c
UV_SHA256_X86_64=788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21
PYTHON_VERSION=3.14

export DEBIAN_FRONTEND=noninteractive

log() { echo "=== $* ===" >&2; }
note() { echo "    $*" >&2; }
die() { echo "bootstrap: ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; }

MODE=full
DOMAIN=""
FORCE=0
# Set by install_tools when hpldn-api.socket changed (configure_api_unit recreates it).
API_SOCKET_CHANGED=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
        --configs-only) MODE=configs; shift ;;
        --rotate-api-key) MODE=rotate; shift ;;
        --close-public-ssh) MODE=close-ssh; shift ;;
        --set-recaptcha-keys) MODE=recaptcha-keys; shift ;;
        --set-cloudflare-token) MODE=cloudflare-token; shift ;;
        --rotate-pass-secret) MODE=rotate-pass; shift ;;
        --set-tls) MODE=tls; shift ;;
        --set-cloudflare-account) MODE=cloudflare-account; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run as root (sudo bash $0 ...)"


# ---------------------------------------------------------------------
# /etc/hpldn/api.env: KEY=value lines of NON-secret settings, root-only.
# /etc/hpldn itself is 0751: the API and the guard read guard.yaml in it by
# name, but nobody but root can list it or read api.env.
# ---------------------------------------------------------------------
file_get() { [[ -f "$1" ]] && sed -n "s/^$2=//p" "$1" | tail -n 1 || true; }

# file_set FILE KEY [VALUE]: replace KEY's line; without VALUE, remove it.
file_set() {
    local file="$1" key="$2" tmp
    install -d -m 0751 "$ETC_DIR"
    [[ -f "$file" ]] || install -m 0600 /dev/null "$file"
    tmp="$(mktemp "$(dirname "$file")/.$(basename "$file").XXXXXX")"
    grep -v "^$key=" "$file" >"$tmp" || true
    if [[ $# -ge 3 ]]; then printf '%s=%s\n' "$key" "$3" >>"$tmp"; fi
    chmod 0600 "$tmp"
    mv "$tmp" "$file"
}

env_get() { file_get "$ENV_FILE" "$1"; }
env_set() { file_set "$ENV_FILE" "$1" "$2"; }
env_unset() { file_set "$ENV_FILE" "$1"; }

env_default() { [[ -n "$(env_get "$1")" ]] || env_set "$1" "$2"; }

resolve_domain() {
    if [[ -z "$DOMAIN" && -f "$ETC_DIR/domain" ]]; then
        DOMAIN="$(cat "$ETC_DIR/domain")"
    fi
    [[ -n "$DOMAIN" ]] || die "pass --domain <name> on the first run"
    [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "not a domain name: $DOMAIN"
    install -d -m 0751 "$ETC_DIR"
    echo "$DOMAIN" >"$ETC_DIR/domain"
}

# ---------------------------------------------------------------------
# Secrets: systemd credentials, encrypted with the host key.
#
# /etc/credstore.encrypted/hpldn.<name> (root 0600) holds each one, sealed by
# `systemd-creds encrypt --with-key=host` with /var/lib/systemd/credential.secret
# (root 0400; A1 instances have no TPM). The units load them with
# LoadCredentialEncrypted=<name>:..., so systemd decrypts each into the
# unit's own /run/credentials/<unit>/<name> (0400): never in the environment,
# never in /proc/<pid>/environ, never in plaintext on disk. The only secret
# left in plaintext is nginx's API-key include (root 0600), whose value only
# opens the API's unix socket, which only nginx's group can reach anyway.
# ---------------------------------------------------------------------
cred_path() { echo "$CREDSTORE/hpldn.$1"; }
cred_exists() { [[ -s "$(cred_path "$1")" ]]; }

# cred_write NAME: seal stdin as credential NAME (atomically, root 0600).
cred_write() {
    local name="$1" out tmp
    out="$(cred_path "$name")"
    install -d -m 0700 -o root -g root "$CREDSTORE"
    tmp="$(mktemp "$CREDSTORE/.hpldn.$name.XXXXXX")"
    systemd-creds encrypt --with-key=host --name="$name" - "$tmp" \
        || { rm -f "$tmp"; die "systemd-creds could not encrypt $name"; }
    chmod 0600 "$tmp"
    mv "$tmp" "$out"
}

# cred_read NAME: the plaintext on stdout (root only; for nginx's include and checks).
cred_read() {
    systemd-creds decrypt --name="$1" "$(cred_path "$1")" - 2>/dev/null
}

cred_generate() {
    # cred_generate NAME BYTES: a new random hex secret, unless one exists.
    cred_exists "$1" || openssl rand -hex "$2" | tr -d '\n' | cred_write "$1"
}

# read_secret_line PROMPT VAR [silent]: one line from stdin into VAR, without
# echo when interactive. The value never touches argv or the environment.
read_secret_line() {
    local prompt="$1" __var="$2" silent="${3:-}" __value
    if [[ -t 0 ]]; then
        if [[ -n "$silent" ]]; then read -r -s -p "$prompt" __value || return 1; echo >&2
        else read -r -p "$prompt" __value || return 1; fi
    else
        IFS= read -r __value || [[ -n "$__value" ]] || return 1
    fi
    printf -v "$__var" '%s' "$__value"
}

# A host that ran an older bootstrap kept its secrets in api.env/guard.env:
# seal each into a credential and remove the plaintext.
migrate_plaintext_secrets() {
    local key name value
    for pair in HPLDN_API_KEY:api_key HPLDN_PASS_SECRET:pass_secret RECAPTCHA_SECRET_KEY:recaptcha_secret_key; do
        key="${pair%%:*}"; name="${pair#*:}"
        value="$(env_get "$key")"
        if [[ -n "$value" ]]; then
            cred_exists "$name" || printf '%s' "$value" | cred_write "$name"
            env_unset "$key"
            note "moved $key from $ENV_FILE into credential $name"
        fi
    done
    if [[ -n "$(env_get POSTGRES_API_PASSWORD)" ]]; then
        env_unset POSTGRES_API_PASSWORD
        note "removed POSTGRES_API_PASSWORD from $ENV_FILE (peer authentication now)"
    fi
    if [[ -f "$GUARD_ENV_FILE" ]]; then
        value="$(file_get "$GUARD_ENV_FILE" CLOUDFLARE_API_TOKEN)"
        if [[ -n "$value" ]]; then
            cred_exists cloudflare_api_token || printf '%s' "$value" | cred_write cloudflare_api_token
            note "moved CLOUDFLARE_API_TOKEN from $GUARD_ENV_FILE into credential cloudflare_api_token"
        fi
        shred -u "$GUARD_ENV_FILE" 2>/dev/null || rm -f "$GUARD_ENV_FILE"
    fi
}

# ---------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------
# At first boot apt-daily, unattended-upgrades and packagekit take apt's
# locks while bootstrap runs. DPkg::Lock::Timeout makes apt-get wait for the
# dpkg locks, but not for /var/lib/apt/lists/lock (apt-get update fails at
# once), so every apt call first waits until no one holds any of them.
APT_LOCKS=(/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock)

apt_locks_free() {
    # apt locks with fcntl (lockf), which flock(1) cannot see.
    python3 - "${APT_LOCKS[@]}" <<'PY'
import fcntl, os, sys
for path in sys.argv[1:]:
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        continue
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(1)
    finally:
        os.close(fd)
PY
}

apt_busy() { pgrep -a 'apt|dpkg|unattended|packagekit' | tr '\n' ';' || true; }

apt_wait() {
    local deadline=$((SECONDS + 1800)) said=0
    until apt_locks_free; do
        (( SECONDS < deadline )) || die "apt's locks still held after 30 min: $(apt_busy)"
        if (( ! said )); then
            note "apt is busy ($(apt_busy)); waiting for it"
            said=1
        fi
        sleep 5
    done
}

apt_get() {
    local attempt
    for attempt in 1 2 3; do
        apt_wait
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 "$@" && return 0
        note "apt-get $1 failed (attempt $attempt of 3)"
    done
    die "apt-get $* failed 3 times"
}

apt_https() {
    # Every http:// apt source whose HTTPS twin answers is switched to it.
    log "Switching apt sources to HTTPS"
    local file uri
    for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [[ -f "$file" ]] || continue
        while read -r uri; do
            if curl -fsS -o /dev/null --max-time 15 --proto '=https' "https://${uri#http://}/"; then
                sed -i "s|$uri|https://${uri#http://}|g" "$file"
                note "${uri} -> https"
            else
                note "WARNING: ${uri} has no HTTPS twin; left as is (apt still checks signatures)"
            fi
        done < <(grep -oE 'http://[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)*' "$file" | sort -u)
    done
}

install_packages() {
    log "Installing base packages"
    . /etc/os-release
    [[ "${ID:-}" == ubuntu ]] || note "not Ubuntu (${ID:-unknown}); continuing, but only Ubuntu 24.04 is tested"
    apt_https
    apt_get update -q
    # python3-yaml: render_guard_nginx.py runs under the system python3,
    # before the application's virtualenv exists. gnupg: the Tailscale key
    # fingerprint check.
    apt_get install -y -q ca-certificates curl gnupg jq zstd rsync openssl \
        nginx unattended-upgrades iptables postgresql-common python3-yaml
}

install_postgres() {
    if dpkg -s "postgresql-$PG_MAJOR" >/dev/null 2>&1; then
        note "postgresql-$PG_MAJOR already installed"
        return
    fi
    log "Installing PostgreSQL $PG_MAJOR from apt.postgresql.org"
    # The PGDG helper adds the signed repository for this release and arch.
    # It runs apt-get update itself, so it waits (and retries) like apt_get.
    local attempt
    for attempt in 1 2 3; do
        apt_wait
        /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y && break
        (( attempt < 3 )) || die "the PGDG helper failed 3 times"
        note "the PGDG helper failed (attempt $attempt of 3)"
    done
    apt_https
    apt_get install -y -q "postgresql-$PG_MAJOR"
    # pg_trgm ships in the server package (contrib is merged since PG 10).
}

install_uv() {
    local arch tarball sha
    if [[ -x /usr/local/bin/uv ]] && /usr/local/bin/uv --version | grep -q " $UV_VERSION"; then
        note "uv $UV_VERSION already installed"
    else
        log "Installing uv $UV_VERSION"
        arch="$(uname -m)"
        case "$arch" in
            aarch64) tarball=uv-aarch64-unknown-linux-gnu.tar.gz; sha=$UV_SHA256_AARCH64 ;;
            x86_64)  tarball=uv-x86_64-unknown-linux-gnu.tar.gz;  sha=$UV_SHA256_X86_64 ;;
            *) die "unsupported architecture $arch" ;;
        esac
        local tmp
        tmp="$(mktemp -d)"
        curl -LsSf --proto '=https' -o "$tmp/uv.tar.gz" \
            "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$tarball"
        echo "$sha  $tmp/uv.tar.gz" | sha256sum --check --status || die "uv tarball digest mismatch"
        tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
        install -m 0755 "$tmp"/uv-*/uv /usr/local/bin/uv
        install -m 0755 "$tmp"/uv-*/uvx /usr/local/bin/uvx
        rm -rf "$tmp"
    fi
    # The interpreter lives under /opt/hpldn, not /root, so the service user
    # can run it.
    log "Installing Python $PYTHON_VERSION for the service"
    UV_PYTHON_INSTALL_DIR=$APP_ROOT/python uv python install "$PYTHON_VERSION"
    chmod -R go-w $APP_ROOT/python
}

install_tailscale_repo() {
    # The keyring is downloaded, then refused unless its primary key is the
    # pinned one: a swapped key on pkgs.tailscale.com would sign anything.
    local tmp fpr
    tmp="$(mktemp -d)"
    . /etc/os-release
    curl -fsSL --proto '=https' "https://pkgs.tailscale.com/stable/ubuntu/$VERSION_CODENAME.noarmor.gpg" -o "$tmp/key.gpg"
    fpr="$(gpg --show-keys --with-colons "$tmp/key.gpg" 2>/dev/null | awk -F: '/^fpr/ {print $10; exit}')"
    [[ "$fpr" == "$TAILSCALE_KEY_FPR" ]] || { rm -rf "$tmp"; die "Tailscale apt key fingerprint is $fpr, pinned $TAILSCALE_KEY_FPR"; }
    install -m 0644 "$tmp/key.gpg" /usr/share/keyrings/tailscale-archive-keyring.gpg
    rm -rf "$tmp"
    echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu $VERSION_CODENAME main" \
        >/etc/apt/sources.list.d/tailscale.list
}

install_tailscale() {
    if command -v tailscale >/dev/null; then
        note "tailscale already installed"
        return
    fi
    log "Installing Tailscale from its signed apt repository (key pinned)"
    install_tailscale_repo
    apt_get update -q
    apt_get install -y -q tailscale
    systemctl enable --now tailscaled
}

# ---------------------------------------------------------------------
# Users and directories
# ---------------------------------------------------------------------
create_user_and_dirs() {
    log "Creating the $SERVICE_USER user and directories"
    id -u "$SERVICE_USER" >/dev/null 2>&1 \
        || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    # /opt/hpldn is closed to everyone but root and the service. nginx's
    # user never needs it: the page is served from /var/www/hpldn.
    install -d -m 0750 -o root -g "$SERVICE_USER" "$APP_ROOT"
    install -d -m 0750 -o root -g "$SERVICE_USER" "$APP_ROOT/models"
    install -d -m 0755 -o root -g root "$APP_ROOT/python"
    # The dumps: licensed raw data, root only (the restore reads them as root).
    install -d -m 0700 -o root -g root "$APP_ROOT/serving"
    install -d -m 0700 -o root -g root "$APP_ROOT/incoming"
    install -d -m 0755 -o root -g root "$WEB_ROOT"
    install -d -m 0751 -o root -g root "$ETC_DIR"
    install -d -m 0700 -o root -g root "$TLS_DIR"
    install -d -m 0700 -o root -g root "$NGINX_INC"
    install -d -m 0700 -o root -g root "$CREDSTORE"
    # The guard daemon's user: reads the venv (group hpldn), writes only its
    # own state, and gives nginx (www-data) its log socket.
    id -u "$GUARD_USER" >/dev/null 2>&1 \
        || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$GUARD_USER"
    usermod -a -G "$SERVICE_USER,www-data" "$GUARD_USER"
    install -d -m 0755 -o "$GUARD_USER" -g "$GUARD_USER" "$GUARD_STATE" "$GUARD_STATE/nginx"
    # nginx's geo includes must exist for nginx -t before the guard first runs.
    local seed
    for seed in banned.conf crawlers.conf; do
        [[ -e "$GUARD_STATE/nginx/$seed" ]] \
            || install -m 0644 -o "$GUARD_USER" -g "$GUARD_USER" /dev/null "$GUARD_STATE/nginx/$seed"
    done
}

# ---------------------------------------------------------------------
# Tools and units from this bundle
# ---------------------------------------------------------------------
install_bundle_copy() {
    # The stdin modes are run later from /opt/hpldn/vps (provision.py,
    # README.md); deploy.sh refreshes it with every app deploy.
    if [[ "$(cd "$BUNDLE_DIR" && pwd -P)" != "$(cd "$VPS_DIR" 2>/dev/null && pwd -P || true)" ]]; then
        install -d -m 0750 -o root -g root "$VPS_DIR"
        rsync -a --delete --exclude '__pycache__' "$BUNDLE_DIR/" "$VPS_DIR/"
        chown -R root:root "$VPS_DIR"
        chmod -R go-rwx "$VPS_DIR"
    fi
}

install_tools() {
    log "Installing the restore, dump-verify and firewall tools"
    install -m 0755 "$BUNDLE_DIR/restore_serving_db.sh" /usr/local/sbin/hpldn-restore-serving-db
    install -m 0755 "$BUNDLE_DIR/verify_dump_signature.sh" /usr/local/sbin/hpldn-verify-dump
    install -m 0755 "$BUNDLE_DIR/firewall.sh" /usr/local/sbin/hpldn-firewall
    install -d -m 0755 "$SHARE_DIR"
    # The public keys serving dumps must be signed with (cloud/vps/trusted_keys).
    install -d -m 0755 "$SHARE_DIR/trusted_keys"
    find "$SHARE_DIR/trusted_keys" -maxdepth 1 -name '*.pem' -delete
    local pem n=0
    for pem in "$BUNDLE_DIR"/trusted_keys/*.pem; do
        [[ -f "$pem" ]] || continue
        install -m 0644 "$pem" "$SHARE_DIR/trusted_keys/"
        n=$((n + 1))
    done
    (( n > 0 )) || note "WARNING: no dump signing keys in $BUNDLE_DIR/trusted_keys; every restore will be refused (export_serving_db --keygen)"
    # Rendered by deploy.sh (python -m hpldn.backend.db.render_vps_sql).
    local rendered
    for rendered in api_role.sql grants.sql; do
        [[ -f "$BUNDLE_DIR/postgres/$rendered" ]] \
            || die "$BUNDLE_DIR/postgres/$rendered is missing: deploy.sh renders it (python -m hpldn.backend.db.render_vps_sql)"
    done
    install -m 0644 "$BUNDLE_DIR/postgres/api_role.sql" "$SHARE_DIR/api_role.sql"
    install -m 0644 "$BUNDLE_DIR/postgres/grants.sql" "$SHARE_DIR/grants.sql"
    local unit
    for unit in hpldn-api.socket hpldn-api.service hpldn-firewall.service \
                hpldn-firewall-refresh.service hpldn-firewall-refresh.timer \
                hpldn-guard.service hpldn-guard-crawlers.service hpldn-guard-crawlers.timer \
                hpldn-nginx-reload.path hpldn-nginx-reload.service; do
        if [[ "$unit" == hpldn-api.socket ]] && ! cmp -s "$BUNDLE_DIR/systemd/$unit" "/etc/systemd/system/$unit"; then
            # A listening socket keeps the mode and owner it was created with:
            # configure_api_unit recreates it (17 Oct 2026: a DirectoryMode fix
            # sat unapplied and nginx got EACCES on api.sock until a restart).
            API_SOCKET_CHANGED=1
        fi
        install -m 0644 "$BUNDLE_DIR/systemd/$unit" "/etc/systemd/system/$unit"
    done
    install -m 0755 "$BUNDLE_DIR/hpldn-guard.sh" /usr/local/sbin/hpldn-guard
    systemctl daemon-reload
}

# ---------------------------------------------------------------------
# The guard: guard.yaml (every run) and its host-only overrides (once)
# ---------------------------------------------------------------------
install_guard_config() {
    log "Installing $GUARD_CONFIG"
    install -m 0644 "$BUNDLE_DIR/guard.yaml" "$GUARD_CONFIG"
    if [[ ! -e "$GUARD_LOCAL_CONFIG" ]]; then
        # Never overwritten: edit it on the host for host-only settings.
        cat >"$GUARD_LOCAL_CONFIG" <<EOF
# Host-only overrides, deep-merged on top of $GUARD_CONFIG (which every
# deploy replaces). Written once by bootstrap.sh; never overwritten
# (--set-cloudflare-account edits its cloudflare section).
captcha:
  # reCAPTCHA tokens made on any other host name are refused.
  allowed_hostnames: [$DOMAIN]
EOF
        chmod 0644 "$GUARD_LOCAL_CONFIG"
    fi
    # Until --set-cloudflare-token: an empty token, so the unit's
    # LoadCredentialEncrypted= has a file and the Cloudflare sink stays off.
    cred_exists cloudflare_api_token || printf '' | cred_write cloudflare_api_token
}

configure_guard_units() {
    systemctl enable --now hpldn-nginx-reload.path >/dev/null
    systemctl enable --now hpldn-guard-crawlers.timer >/dev/null
    systemctl enable hpldn-guard >/dev/null
    if [[ -x "$APP_ROOT/venv/bin/python" ]]; then
        # Picks up a new guard.yaml; bans live in guard.db and survive it.
        systemctl restart hpldn-guard
    else
        note "hpldn-guard is enabled and starts once deploy.sh has installed the application"
    fi
}

recaptcha_keys_missing() {
    [[ -z "$(env_get RECAPTCHA_SITE_KEY)" ]] || ! cred_exists recaptcha_secret_key
}

# ---------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------
from_tailnet() {
    # True when this run is not over SSH, or its SSH client is on the tailnet.
    local client="${SSH_CONNECTION:-}"; client="${client%% *}"
    [[ -z "$client" || "$client" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. || "$client" =~ ^fd7a:115c:a1e0: ]]
}

configure_firewall() {
    local mode="$1"
    log "Firewall: 443 from Cloudflare only, admin over Tailscale, metadata root-only"
    # Public SSH is never opened on purpose: the host joins the tailnet at
    # first boot and every session comes over it. Only a break-glass run (this
    # script over public SSH, with bootstrap_ssh=true in cloud/oci/main) keeps
    # 22 open, so the session is not cut; --close-public-ssh shuts it.
    if from_tailnet; then
        rm -f "$ETC_DIR/public-ssh"
    elif [[ ! -e "$ETC_DIR/public-ssh-closed" ]]; then
        note "WARNING: running over public SSH (break-glass); port 22 stays open until --close-public-ssh"
        touch "$ETC_DIR/public-ssh"
    fi
    if [[ "$mode" == refresh || ! -s "$ETC_DIR/cloudflare/ips-v4" ]]; then
        /usr/local/sbin/hpldn-firewall refresh
    else
        /usr/local/sbin/hpldn-firewall apply
    fi
    systemctl enable hpldn-firewall.service >/dev/null
    systemctl enable --now hpldn-firewall-refresh.timer >/dev/null
}

# ---------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------
pg() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 "$@"; }

configure_postgres() {
    log "Configuring PostgreSQL $PG_MAJOR"
    local changed=0 file mode
    [[ -d "$PG_CONF_DIR" ]] || die "$PG_CONF_DIR does not exist; is the $PG_MAJOR/main cluster created?"
    install -d -m 0755 -o postgres -g postgres "$PG_CONF_DIR/conf.d"
    if ! cmp -s "$BUNDLE_DIR/postgres/10-hpldn.conf" "$PG_CONF_DIR/conf.d/10-hpldn.conf"; then
        install -m 0644 -o postgres -g postgres "$BUNDLE_DIR/postgres/10-hpldn.conf" "$PG_CONF_DIR/conf.d/10-hpldn.conf"
        changed=1
    fi
    [[ -f "$PG_CONF_DIR/pg_hba.conf.dist" ]] || cp -p "$PG_CONF_DIR/pg_hba.conf" "$PG_CONF_DIR/pg_hba.conf.dist"
    [[ -f "$PG_CONF_DIR/pg_ident.conf.dist" ]] || cp -p "$PG_CONF_DIR/pg_ident.conf" "$PG_CONF_DIR/pg_ident.conf.dist" 2>/dev/null || true
    # Peer authentication only: pg_hba maps the OS user hpldn to hpldn_api
    # through pg_ident; no role has a password.
    for file in pg_hba.conf pg_ident.conf; do
        mode=0640
        if ! cmp -s "$BUNDLE_DIR/postgres/$file" "$PG_CONF_DIR/$file"; then
            install -m "$mode" -o postgres -g postgres "$BUNDLE_DIR/postgres/$file" "$PG_CONF_DIR/$file"
            changed=1
        fi
    done
    systemctl enable "$PG_UNIT" >/dev/null
    local restarted=0
    if (( changed )) || ! systemctl is-active --quiet "$PG_UNIT"; then
        # listen_addresses and shared_buffers need a restart, not a reload.
        systemctl restart "$PG_UNIT"
        restarted=1
    fi
    for _ in $(seq 30); do
        runuser -u postgres -- pg_isready -q && break
        sleep 1
    done
    runuser -u postgres -- pg_isready -q || die "PostgreSQL did not start; see journalctl -u $PG_UNIT"
    if [[ "$(pg -At -d postgres -c 'SHOW data_checksums')" != on ]]; then
        note "WARNING: data checksums are off on $PG_MAJOR/main (PostgreSQL 18 enables them at initdb); enable with pg_checksums --enable while it is stopped"
    fi
    if [[ -n "$(pg -At -d postgres -c 'SHOW listen_addresses')" ]]; then
        die "PostgreSQL listens on TCP; 10-hpldn.conf sets listen_addresses = '' and must win"
    fi

    if [[ "$(pg -At -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$DB'")" != 1 ]]; then
        pg -d postgres -c "CREATE DATABASE $DB"
    fi
    # In pg_catalog, as on the build database, so the dumped gin_trgm_ops
    # index resolves whatever the search path.
    pg -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA pg_catalog"
    # pg_prewarm(), for restore_serving_db.sh (autoprewarm needs no extension).
    pg -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS pg_prewarm SCHEMA pg_catalog"
    if (( restarted )) && [[ "$(pg -At -d "$DB" -c "SELECT 1 FROM pg_namespace WHERE nspname = 'production'")" == 1 ]]; then
        # A restarted cluster's buffers are empty until autoprewarm has a list.
        /usr/local/sbin/hpldn-restore-serving-db --prewarm-only || note "WARNING: prewarm after the PostgreSQL restart failed"
    fi
    # The API role (peer login, no password), its settings, and hpldn_owner.
    pg -d "$DB" -v api_role="$API_ROLE" -v db="$DB" <"$SHARE_DIR/api_role.sql"
}

# ---------------------------------------------------------------------
# Settings (api.env) and secrets (credentials); nginx's key include
# ---------------------------------------------------------------------
write_api_env() {
    log "Writing $ENV_FILE (settings) and the credentials in $CREDSTORE"
    env_set POSTGRES_API_USERNAME "$API_ROLE"
    # A socket directory: peer authentication, no password (config.py).
    env_set POSTGRES_API_HOST /var/run/postgresql
    env_set POSTGRES_API_PORT 5432
    env_set POSTGRES_API_DATABASE "$DB"
    # The unix socket carries no TLS.
    env_set POSTGRES_API_SSLMODE disable
    env_default HPLDN_PRICE_MODEL_FP "$DEFAULT_MODEL_FP"
    migrate_plaintext_secrets
    # Alphanumeric: it is written into nginx syntax.
    cred_generate api_key 24
    # Signs the reCAPTCHA passes; both workers read the same one.
    cred_generate pass_secret 32
    # The captcha gate is never off on the public host, its tokens are
    # always checked by Google (the fake siteverify is for the local stack),
    # and the API's docs are never served.
    local local_only
    for local_only in HPLDN_CAPTCHA_DISABLED HPLDN_RECAPTCHA_VERIFY_URL HPLDN_API_DOCS HPLDN_ALLOW_UNSIGNED_MODEL; do
        if [[ -n "$(env_get "$local_only")" ]]; then
            note "removing $local_only from $ENV_FILE"
            env_unset "$local_only"
        fi
    done
    write_api_key_include
}

write_api_key_include() {
    local key tmp
    key="$(cred_read api_key)" || die "cannot decrypt credential api_key"
    [[ "$key" =~ ^[A-Za-z0-9]{24,}$ ]] || die "credential api_key is missing or not alphanumeric"
    tmp="$(mktemp "$NGINX_INC/.api_key.conf.XXXXXX")"
    chmod 0600 "$tmp"
    printf 'proxy_set_header X-API-Key "%s";\n' "$key" >"$tmp"
    mv "$tmp" "$NGINX_INC/api_key.conf"
}

rotate_api_key() {
    log "Rotating the API key"
    openssl rand -hex 24 | tr -d '\n' | cred_write api_key
    write_api_key_include
    nginx -t -q
    # Workers cache the key; restart first so the window with a mismatched
    # key is the restart itself.
    restart_api_if_enabled
    systemctl reload nginx
    note "new key in credential api_key and $NGINX_INC/api_key.conf; the browser never holds it"
}

restart_api_if_enabled() {
    if systemctl is-enabled --quiet hpldn-api 2>/dev/null && [[ -x "$APP_ROOT/venv/bin/gunicorn" ]]; then
        systemctl restart hpldn-api
        note "hpldn-api restarted; the workers take a few minutes to load the model"
    fi
}

restart_guard_if_enabled() {
    if systemctl is-enabled --quiet hpldn-guard 2>/dev/null && [[ -x "$APP_ROOT/venv/bin/python" ]]; then
        systemctl restart hpldn-guard
    fi
}

set_recaptcha_keys() {
    local site secret
    log "Setting the reCAPTCHA v3 keys (Google Cloud console -> reCAPTCHA)"
    read_secret_line "Site key: " site || die "no site key on stdin"
    read_secret_line "Secret key: " secret silent || die "no secret key on stdin"
    [[ "$site" =~ ^[A-Za-z0-9_-]{20,100}$ ]] || die "that does not look like a reCAPTCHA site key"
    [[ "$secret" =~ ^[A-Za-z0-9_-]{20,100}$ ]] || die "that does not look like a reCAPTCHA secret key"
    [[ "$site" != "$secret" ]] || die "the site key and the secret key are the same"
    # The site key is public (the page fetches it); the secret is a credential.
    env_set RECAPTCHA_SITE_KEY "$site"
    printf '%s' "$secret" | cred_write recaptcha_secret_key
    note "site key in $ENV_FILE, secret key in credential recaptcha_secret_key"
    restart_api_if_enabled
}

set_cloudflare_token() {
    local token
    log "Setting the guard's Cloudflare API token (Account Rule Lists: Write)"
    read_secret_line "Cloudflare API token: " token silent || die "no token on stdin"
    [[ "$token" =~ ^[A-Za-z0-9_-]{30,}$ ]] || die "that does not look like a Cloudflare API token"
    printf '%s' "$token" | cred_write cloudflare_api_token
    note "written to credential cloudflare_api_token"
    restart_guard_if_enabled
}

set_cloudflare_account() {
    local account
    log "Setting the guard's Cloudflare account (its IP list lives there)"
    read_secret_line "Cloudflare account id: " account || die "no account id on stdin"
    [[ "$account" =~ ^[0-9a-f]{32}$ ]] || die "a Cloudflare account id is 32 hex characters"
    [[ -f "$GUARD_LOCAL_CONFIG" ]] || die "$GUARD_LOCAL_CONFIG is missing; run bootstrap.sh --domain ... first"
    # Edits only the cloudflare section of the host-only overrides.
    HPLDN_CF_ACCOUNT="$account" /usr/bin/python3 - "$GUARD_LOCAL_CONFIG" <<'PY'
import os, sys, yaml
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    data = yaml.safe_load(handle) or {}
section = data.setdefault("cloudflare", {})
section["enabled"] = True
section["account_id"] = os.environ["HPLDN_CF_ACCOUNT"]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write("# Host-only overrides, deep-merged on top of guard.yaml (bootstrap.sh).\n")
    yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=True)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
    note "cloudflare.enabled and account_id set in $GUARD_LOCAL_CONFIG"
    restart_guard_if_enabled
}

rotate_pass_secret() {
    log "Rotating the reCAPTCHA pass secret"
    openssl rand -hex 32 | tr -d '\n' | cred_write pass_secret
    # Every pass handed out so far stops working; the page gets a new one
    # on its next call without the visitor noticing.
    restart_api_if_enabled
}

# --set-tls: a tar stream on stdin holding origin.crt, origin.key (the
# Cloudflare Origin CA certificate and its ECDSA key) and aop_ca.pem (the
# CA our Authenticated Origin Pulls client certificate is issued from).
set_tls() {
    log "Installing the origin certificate and the origin-pull CA"
    [[ ! -t 0 ]] || die "--set-tls reads a tar stream on stdin (provision.py sends it)"
    local tmp name
    tmp="$(mktemp -d)"
    chmod 0700 "$tmp"
    # Plain files with these three names only: no paths, links or devices.
    tar -x -C "$tmp" --no-same-owner --no-same-permissions --no-overwrite-dir \
        origin.crt origin.key aop_ca.pem || { rm -rf "$tmp"; die "stdin is not a tar of origin.crt, origin.key, aop_ca.pem"; }
    for name in origin.crt origin.key aop_ca.pem; do
        [[ -f "$tmp/$name" && ! -L "$tmp/$name" ]] || { rm -rf "$tmp"; die "$name is missing from the tar"; }
    done
    openssl x509 -in "$tmp/origin.crt" -noout 2>/dev/null || { rm -rf "$tmp"; die "origin.crt is not a certificate"; }
    openssl pkey -in "$tmp/origin.key" -noout 2>/dev/null || { rm -rf "$tmp"; die "origin.key is not a private key"; }
    [[ "$(openssl x509 -in "$tmp/origin.crt" -noout -pubkey | openssl sha256)" == "$(openssl pkey -in "$tmp/origin.key" -pubout | openssl sha256)" ]] \
        || { rm -rf "$tmp"; die "origin.key does not belong to origin.crt"; }
    openssl x509 -in "$tmp/aop_ca.pem" -noout -ext basicConstraints 2>/dev/null | grep -q 'CA:TRUE' \
        || { rm -rf "$tmp"; die "aop_ca.pem is not a CA certificate"; }
    openssl x509 -in "$tmp/origin.crt" -noout -checkend 86400 >/dev/null \
        || { rm -rf "$tmp"; die "origin.crt expires within a day"; }
    install -m 0644 -o root -g root "$tmp/origin.crt" "$TLS_DIR/origin.crt.new"
    install -m 0600 -o root -g root "$tmp/origin.key" "$TLS_DIR/origin.key.new"
    install -m 0644 -o root -g root "$tmp/aop_ca.pem" "$TLS_DIR/aop_ca.pem.new"
    rm -rf "$tmp"
    for name in origin.crt origin.key aop_ca.pem; do
        [[ -f "$TLS_DIR/$name" ]] && cp -p "$TLS_DIR/$name" "$TLS_DIR/$name.old"
        mv "$TLS_DIR/$name.new" "$TLS_DIR/$name"
    done
    rm -f "$TLS_DIR/placeholder"
    if nginx -t -q; then
        rm -f "$TLS_DIR"/*.old
        systemctl reload nginx
        note "origin certificate $(openssl x509 -in "$TLS_DIR/origin.crt" -noout -enddate | cut -d= -f2), AOP CA installed; nginx reloaded"
    else
        for name in origin.crt origin.key aop_ca.pem; do
            [[ -f "$TLS_DIR/$name.old" ]] && mv "$TLS_DIR/$name.old" "$TLS_DIR/$name"
        done
        die "nginx -t failed with the new certificates; the previous ones are back"
    fi
}

# ---------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------
tls_placeholders() {
    # Until --set-tls: a short-lived self-signed certificate, and an AOP CA
    # whose key is thrown away at once. nginx starts, and no client can ever
    # present a certificate it accepts: the site stays shut (fail closed).
    local tmp
    if [[ ! -s "$TLS_DIR/origin.crt" || ! -s "$TLS_DIR/origin.key" ]]; then
        note "no origin certificate yet: a self-signed placeholder until --set-tls"
        openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 30 -subj "/CN=$DOMAIN" \
            -addext "subjectAltName=DNS:$DOMAIN" \
            -keyout "$TLS_DIR/origin.key" -out "$TLS_DIR/origin.crt" 2>/dev/null
        touch "$TLS_DIR/placeholder"
    fi
    if [[ ! -s "$TLS_DIR/aop_ca.pem" ]]; then
        tmp="$(mktemp -d)"
        openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 30 \
            -subj "/CN=hpldn placeholder origin-pull CA (no key exists)" \
            -addext "basicConstraints=critical,CA:TRUE" \
            -keyout "$tmp/k" -out "$TLS_DIR/aop_ca.pem" 2>/dev/null
        shred -u "$tmp/k"; rm -rf "$tmp"
        touch "$TLS_DIR/placeholder"
    fi
    chmod 0600 "$TLS_DIR/origin.key"
    chmod 0644 "$TLS_DIR/origin.crt" "$TLS_DIR/aop_ca.pem"
}

configure_nginx() {
    log "Configuring nginx for $DOMAIN"
    rm -f /etc/nginx/sites-enabled/default
    sed "s/@SERVER_NAME@/$DOMAIN/g" "$BUNDLE_DIR/nginx/hpldn.conf" >/etc/nginx/conf.d/hpldn.conf
    chmod 0644 /etc/nginx/conf.d/hpldn.conf
    install -m 0600 "$BUNDLE_DIR/nginx/api_proxy.conf" "$NGINX_INC/api_proxy.conf"
    install -m 0600 "$BUNDLE_DIR/nginx/api_cache.conf" "$NGINX_INC/api_cache.conf"
    # proxy_cache_path for /api/areas and /api/inflation (api_cache.conf).
    install -d -m 0755 /var/cache/nginx
    install -d -o www-data -g www-data -m 0700 /var/cache/nginx/hpldn
    install -m 0600 "$BUNDLE_DIR/nginx/security_headers.conf" "$NGINX_INC/security_headers.conf"
    # The optional origin-pull include is gone: AOP is in hpldn.conf, always.
    rm -f "$NGINX_INC"/origin_pull*.conf
    # Rate limits, timeouts and denied user agents from guard.yaml.
    /usr/bin/python3 "$BUNDLE_DIR/render_guard_nginx.py" --config "$GUARD_CONFIG" --out "$NGINX_INC" \
        || die "render_guard_nginx.py failed; fix $GUARD_CONFIG or $GUARD_LOCAL_CONFIG"
    tls_placeholders
    nginx -t -q
    systemctl enable nginx >/dev/null
    if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl start nginx; fi
}

# ---------------------------------------------------------------------
# The API's socket and unit
# ---------------------------------------------------------------------
configure_api_unit() {
    if (( API_SOCKET_CHANGED )) && systemctl is-active --quiet hpldn-api.socket; then
        # systemd refuses to restart a socket whose service is running, and
        # the service holds the socket's file descriptor: stop both, recreate
        # the socket from the new unit, then start the service again.
        log "hpldn-api.socket changed: recreating /run/hpldn-api/api.sock"
        local was_active=0
        systemctl is-active --quiet hpldn-api && was_active=1
        systemctl stop hpldn-api
        systemctl restart hpldn-api.socket
        if (( was_active )); then
            systemctl start hpldn-api
            note "hpldn-api restarted on the new socket; the workers take a minute to warm up"
        fi
    fi
    systemctl enable --now hpldn-api.socket >/dev/null
    systemctl enable hpldn-api >/dev/null
    # nginx's workers (www-data) must be able to open the socket.
    if [[ -S /run/hpldn-api/api.sock ]] && ! runuser -u www-data -- test -r /run/hpldn-api/api.sock -a -w /run/hpldn-api/api.sock; then
        die "www-data cannot open /run/hpldn-api/api.sock ($(stat -c '%A %U:%G' /run/hpldn-api /run/hpldn-api/api.sock | tr '\n' ' ')); nginx would answer 502"
    fi
    if [[ ! -x "$APP_ROOT/venv/bin/gunicorn" ]]; then
        note "hpldn-api is enabled and starts once deploy.sh has installed the application"
    fi
}

# ---------------------------------------------------------------------
# Host hardening
# ---------------------------------------------------------------------
configure_upgrades() {
    log "Enabling unattended security upgrades"
    cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    # PostgreSQL minor releases (security fixes) come from PGDG, not Ubuntu.
    cat >/etc/apt/apt.conf.d/51hpldn-unattended-upgrades <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=apt.postgresql.org,archive=${distro_codename}-pgdg";
    "origin=Tailscale";
};
// No automatic reboots: a restart costs minutes of API warm-up. Reboot by
// hand when /var/run/reboot-required appears (README.md).
Unattended-Upgrade::Automatic-Reboot "false";
EOF
    systemctl enable --now unattended-upgrades >/dev/null
}

harden_kernel() {
    log "Hardening sysctl"
    cat >/etc/sysctl.d/90-hpldn.conf <<'EOF'
# HPLDN serving host (bootstrap.sh).
# No kernel addresses or logs for unprivileged users.
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
# ptrace only with CAP_SYS_PTRACE: no process can read another's memory.
kernel.yama.ptrace_scope = 2
kernel.unprivileged_bpf_disabled = 1
net.core.bpf_jit_harden = 2
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
fs.protected_fifos = 2
fs.protected_regular = 2
fs.suid_dumpable = 0
# Reverse-path filtering; no redirects or source routing, in or out.
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.tcp_syncookies = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
EOF
    sysctl --quiet --system >/dev/null || note "WARNING: sysctl --system reported errors"
}

configure_journald() {
    log "Persistent, bounded journal"
    install -d -m 0755 /etc/systemd/journald.conf.d
    cat >/etc/systemd/journald.conf.d/90-hpldn.conf <<'EOF'
# HPLDN serving host (bootstrap.sh): kept across reboots, 500 MB and 30 days at most.
[Journal]
Storage=persistent
SystemMaxUse=500M
MaxRetentionSec=30day
EOF
    systemctl restart systemd-journald
}

harden_sshd() {
    log "Hardening sshd"
    install -d -m 0755 /etc/ssh/sshd_config.d
    # The same file cloud-init writes at first boot (cloud/oci/main/templates).
    cat >/etc/ssh/sshd_config.d/10-hpldn.conf <<'EOF'
# HPLDN: keys only, no root, ubuntu only, no forwarding. Admin access is SSH
# over the tailnet (README.md); its host keys are pinned on the laptop.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
AllowUsers ubuntu
MaxAuthTries 3
AllowTcpForwarding no
AllowAgentForwarding no
AllowStreamLocalForwarding no
PermitTunnel no
X11Forwarding no
EOF
    sshd -t
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
}

join_tailscale() {
    if tailscale status >/dev/null 2>&1; then
        note "Tailscale is up: $(tailscale ip -4 2>/dev/null | head -n 1)"
    elif [[ -s "$TS_KEY_FILE" ]]; then
        # Read from a file, never argv; shredded at once (single-use anyway).
        log "Joining the tailnet as $TS_HOSTNAME"
        tailscale up --auth-key="file:$TS_KEY_FILE" --advertise-tags=tag:hpldn --hostname="$TS_HOSTNAME" \
            || { shred -u "$TS_KEY_FILE"; die "tailscale up failed"; }
        shred -u "$TS_KEY_FILE"
    else
        note "Tailscale is not up and there is no key at $TS_KEY_FILE; cloud-init normally joins at first boot"
        note "(join by hand: sudo tailscale up --advertise-tags=tag:hpldn --hostname=$TS_HOSTNAME)"
    fi
    tailscale set --auto-update >/dev/null 2>&1 || true
}

close_public_ssh() {
    log "Closing public SSH"
    if [[ ! -e "$ETC_DIR/public-ssh" ]]; then
        touch "$ETC_DIR/public-ssh-closed"
        note "public SSH was never opened on the host (no break-glass); nothing to do"
        return
    fi
    tailscale status --json 2>/dev/null | jq -e '.BackendState == "Running"' >/dev/null \
        || die "Tailscale is not running; closing port 22 now would lock you out"
    if ! from_tailnet && [[ "$FORCE" != 1 ]]; then
        die "this session does not come from the tailnet; reconnect over Tailscale (ssh ubuntu@$TS_HOSTNAME) and re-run, or pass --force"
    fi
    rm -f "$ETC_DIR/public-ssh"
    touch "$ETC_DIR/public-ssh-closed"
    /usr/local/sbin/hpldn-firewall apply
    note "Port 22 is closed on the host. provision.py also removes the NSG's break-glass rule (bootstrap_ssh=false)."
}

summary() {
    log "Done"
    note "domain:        $DOMAIN"
    note "database:      $DB (role $API_ROLE, peer authentication over the socket only)"
    note "settings:      $ENV_FILE (no secrets)"
    note "secrets:       $CREDSTORE/hpldn.* (systemd-creds, host key), $NGINX_INC/api_key.conf (root only)"
    note "guard:         $GUARD_CONFIG (+ $GUARD_LOCAL_CONFIG), sudo hpldn-guard list|ban|unban"
    if [[ -e "$TLS_DIR/placeholder" ]]; then
        note "next:          the origin certificate and AOP CA (provision.py up, or bootstrap.sh --set-tls)"
    fi
    if recaptcha_keys_missing; then
        note "next:          sudo bash $VPS_DIR/bootstrap.sh --set-recaptcha-keys (the API will not start without them)"
    fi
    note "then:          from the laptop, python -m hpldn.backend.cloud.oci deploy (or cloud/vps/deploy.sh)"
}

case "$MODE" in
    full)
        resolve_domain
        install_packages
        install_postgres
        install_uv
        install_tailscale
        create_user_and_dirs
        install_bundle_copy
        install_tools
        configure_firewall refresh
        configure_postgres
        write_api_env
        install_guard_config
        configure_nginx
        configure_api_unit
        configure_guard_units
        configure_upgrades
        harden_kernel
        configure_journald
        harden_sshd
        join_tailscale
        summary
        ;;
    configs)
        resolve_domain
        create_user_and_dirs
        install_bundle_copy
        install_tools
        configure_firewall apply
        configure_postgres
        write_api_env
        install_guard_config
        configure_nginx
        configure_api_unit
        configure_guard_units
        if recaptcha_keys_missing; then
            note "WARNING: no reCAPTCHA keys: the API will not start while guard.yaml captcha.enabled is on (--set-recaptcha-keys)"
        fi
        ;;
    rotate)
        rotate_api_key
        ;;
    close-ssh)
        close_public_ssh
        ;;
    recaptcha-keys)
        set_recaptcha_keys
        ;;
    cloudflare-token)
        set_cloudflare_token
        ;;
    cloudflare-account)
        set_cloudflare_account
        ;;
    rotate-pass)
        rotate_pass_secret
        ;;
    tls)
        set_tls
        ;;
esac
