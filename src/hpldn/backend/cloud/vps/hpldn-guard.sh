#!/usr/bin/env bash
# =====================================================================
# hpldn-guard: the guard's admin commands, run as the hpldn-guard user.
#
#     sudo hpldn-guard list
#     sudo hpldn-guard ban 203.0.113.7 --for 24h
#     sudo hpldn-guard unban 203.0.113.7 [--forget]
#     sudo hpldn-guard cloudflare-setup
#     sudo hpldn-guard refresh-crawlers
#
# Installed as /usr/local/sbin/hpldn-guard by bootstrap.sh. Each command runs
# in a transient systemd unit as the daemon's own user (so guard.db and its
# WAL files stay writable by the daemon) that loads the Cloudflare token as a
# systemd credential, exactly as hpldn-guard.service does: the token is never
# in this shell's environment, the command line or a plaintext file.
# ban/unban only change the database: the running daemon applies them to
# nginx and Cloudflare within seconds.
# =====================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "hpldn-guard: run as root (sudo hpldn-guard ...)" >&2; exit 1; }
[[ -x /opt/hpldn/venv/bin/python ]] || { echo "hpldn-guard: the application is not deployed yet" >&2; exit 1; }
install -d -m 0755 -o hpldn-guard -g hpldn-guard /var/lib/hpldn-guard
CRED=/etc/credstore.encrypted/hpldn.cloudflare_api_token
props=(
    -p User=hpldn-guard -p Group=hpldn-guard -p SupplementaryGroups=hpldn
    -p WorkingDirectory=/var/lib/hpldn-guard
    -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes
    -p ReadWritePaths=/var/lib/hpldn-guard
    -E HPLDN_ENV=production -E PYTHONDONTWRITEBYTECODE=1
)
if [[ -s "$CRED" ]]; then
    props+=(-p "LoadCredentialEncrypted=cloudflare_api_token:$CRED")
fi
exec systemd-run --quiet --wait --collect --pipe --service-type=exec "${props[@]}" \
    /opt/hpldn/venv/bin/python -m hpldn.backend.guard "$@"
