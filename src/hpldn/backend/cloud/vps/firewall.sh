#!/usr/bin/env bash
# =====================================================================
# Host firewall and Cloudflare trust for the HPLDN serving host.
#
#     hpldn-firewall refresh   # fetch Cloudflare's ranges, then apply (weekly timer)
#     hpldn-firewall apply     # apply from the cached ranges (at boot)
#
# Installed as /usr/local/sbin/hpldn-firewall by bootstrap.sh; run as root.
#
# Inbound policy, in chain HPLDN_INPUT (jumped to first from INPUT, IPv4 and
# IPv6 alike), everything else is rejected:
#   * established/related traffic, loopback, ICMP (and ICMPv6, which IPv6
#     neighbour discovery needs);
#   * anything on tailscale0 (admin SSH, deploys) and Tailscale's own UDP;
#   * tcp/443 from Cloudflare's published ranges only (chain HPLDN_CF), so
#     nobody can reach nginx without passing Cloudflare's WAF and limits;
#   * tcp/22 only while /etc/hpldn/public-ssh exists (bootstrap creates it;
#     `bootstrap.sh --close-public-ssh` removes it once Tailscale SSH works).
# The same ranges become nginx's set_real_ip_from list, so CF-Connecting-IP
# is believed only from Cloudflare. This is the second layer under the OCI
# network security group (cloud/oci/main/network.tf), which admits the same.
#
# Outbound, in chain HPLDN_OUTPUT (jumped to first from OUTPUT): the instance
# metadata service (169.254.169.254, and OCI's IPv6 fd00:c1::a9fe:a9fe) is
# root-only over HTTP. Any other user's tcp/80 or tcp/443 to it is rejected,
# so no service -- or an SSRF through one -- can read instance metadata.
# DNS and NTP to that address (systemd-resolved, chrony) stay open.
#
# A failed download keeps the previous lists: the firewall is never opened
# or emptied because cloudflare.com was unreachable.
# =====================================================================
set -euo pipefail

STATE_DIR=/etc/hpldn/cloudflare
REALIP_CONF=/etc/nginx/hpldn/cloudflare_realip.conf
PUBLIC_SSH_FLAG=/etc/hpldn/public-ssh
TAILSCALE_PORT=41641
IMDS_V4=169.254.169.254/32
IMDS_V6=fd00:c1::a9fe:a9fe/128
URL_V4=https://www.cloudflare.com/ips-v4
URL_V6=https://www.cloudflare.com/ips-v6
# Cloudflare publishes ~15 IPv4 and ~7 IPv6 ranges; far fewer means a
# truncated or wrong download.
MIN_V4=10
MIN_V6=4

log() { echo "hpldn-firewall: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
MODE="${1:-refresh}"

valid_v4() { grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$' <<<"$1"; }
valid_v6() { grep -Eq '^[0-9a-fA-F:]+/[0-9]{1,3}$' <<<"$1"; }

fetch() {
    # fetch URL VALIDATOR MIN OUT
    local url="$1" check="$2" min="$3" out="$4" tmp line n=0
    tmp="$(mktemp)"
    if ! curl -fsS --max-time 20 --retry 3 "$url" -o "$tmp"; then
        rm -f "$tmp"; log "could not download $url; keeping the cached list"; return 0
    fi
    : >"$tmp.ok"
    # Reads $tmp and appends to $tmp.ok; $tmp is only removed on the way out.
    # shellcheck disable=SC2094
    while read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" ]] && continue
        "$check" "$line" || { rm -f "$tmp" "$tmp.ok"; log "unexpected line '$line' from $url; keeping the cached list"; return 0; }
        echo "$line" >>"$tmp.ok"; n=$((n + 1))
    done <"$tmp"
    rm -f "$tmp"
    if (( n < min )); then
        rm -f "$tmp.ok"; log "only $n ranges from $url; keeping the cached list"; return 0
    fi
    install -m 0644 "$tmp.ok" "$out"; rm -f "$tmp.ok"
    log "$n ranges from $url"
}

write_realip() {
    local tmp
    tmp="$(mktemp)"
    {
        echo "# Written by hpldn-firewall from $URL_V4 and $URL_V6; do not edit."
        echo "# The client address is CF-Connecting-IP, believed from Cloudflare only."
        while read -r cidr; do echo "set_real_ip_from $cidr;"; done <"$STATE_DIR/ips-v4"
        while read -r cidr; do echo "set_real_ip_from $cidr;"; done <"$STATE_DIR/ips-v6"
        echo "real_ip_header CF-Connecting-IP;"
        echo "real_ip_recursive off;"
    } >"$tmp"
    install -d -m 0755 "$(dirname "$REALIP_CONF")"
    if ! cmp -s "$tmp" "$REALIP_CONF" 2>/dev/null; then
        install -m 0644 "$tmp" "$REALIP_CONF"
        REALIP_CHANGED=1
    fi
    rm -f "$tmp"
}

rules() {
    # rules FAMILY(4|6) LIST: an iptables-restore --noflush script. Declaring
    # a chain with --noflush creates or empties it, so each table is
    # replaced in one atomic commit and 443 is never briefly open or shut.
    local family="$1" list="$2" icmp reject imds
    if [[ "$family" == 4 ]]; then
        icmp="-p icmp"; reject="--reject-with icmp-host-prohibited"; imds=$IMDS_V4
    else
        icmp="-p ipv6-icmp"; reject="--reject-with icmp6-adm-prohibited"; imds=$IMDS_V6
    fi
    echo "*filter"
    echo ":HPLDN_INPUT - [0:0]"
    echo ":HPLDN_CF - [0:0]"
    echo ":HPLDN_OUTPUT - [0:0]"
    echo "-A HPLDN_INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
    echo "-A HPLDN_INPUT -m conntrack --ctstate INVALID -j DROP"
    echo "-A HPLDN_INPUT -i lo -j ACCEPT"
    echo "-A HPLDN_INPUT $icmp -j ACCEPT"
    if [[ "$family" == 6 ]]; then
        # DHCPv6 replies from the link-local router, for a VCN with IPv6.
        echo "-A HPLDN_INPUT -s fe80::/10 -p udp --dport 546 -j ACCEPT"
    fi
    echo "-A HPLDN_INPUT -i tailscale0 -j ACCEPT"
    echo "-A HPLDN_INPUT -p udp --dport $TAILSCALE_PORT -j ACCEPT"
    echo "-A HPLDN_INPUT -p tcp --dport 443 -j HPLDN_CF"
    if [[ -e "$PUBLIC_SSH_FLAG" ]]; then
        echo "-A HPLDN_INPUT -p tcp --dport 22 -j ACCEPT"
    fi
    echo "-A HPLDN_INPUT -j REJECT $reject"
    while read -r cidr; do echo "-A HPLDN_CF -s $cidr -j ACCEPT"; done <"$list"
    echo "-A HPLDN_CF -j DROP"
    # IMDS over HTTP: root only.
    echo "-A HPLDN_OUTPUT -d $imds -p tcp -m multiport --dports 80,443 -m owner ! --uid-owner 0 -j REJECT --reject-with tcp-reset"
    echo "COMMIT"
}

apply_family() {
    local family="$1" list="$2" ipt restore
    if [[ "$family" == 4 ]]; then ipt=iptables; restore=iptables-restore
    else ipt=ip6tables; restore=ip6tables-restore; fi
    command -v "$ipt" >/dev/null || { log "$ipt not installed; skipping IPv$family"; return 0; }
    [[ -s "$list" ]] || die "no cached Cloudflare IPv$family ranges in $list; run: hpldn-firewall refresh"
    # Chains must exist before a rule can jump to them.
    "$ipt" -N HPLDN_INPUT 2>/dev/null || true
    "$ipt" -N HPLDN_CF 2>/dev/null || true
    "$ipt" -N HPLDN_OUTPUT 2>/dev/null || true
    rules "$family" "$list" | "$restore" --noflush
    "$ipt" -C INPUT -j HPLDN_INPUT 2>/dev/null || "$ipt" -I INPUT 1 -j HPLDN_INPUT
    "$ipt" -C OUTPUT -j HPLDN_OUTPUT 2>/dev/null || "$ipt" -I OUTPUT 1 -j HPLDN_OUTPUT
    log "IPv$family: $(wc -l <"$list") Cloudflare ranges on 443, public ssh $([[ -e $PUBLIC_SSH_FLAG ]] && echo open || echo closed), metadata root-only"
}

install -d -m 0755 "$STATE_DIR"
case "$MODE" in
    refresh)
        fetch "$URL_V4" valid_v4 "$MIN_V4" "$STATE_DIR/ips-v4"
        fetch "$URL_V6" valid_v6 "$MIN_V6" "$STATE_DIR/ips-v6"
        ;;
    apply) ;;
    *) die "usage: hpldn-firewall [refresh|apply]" ;;
esac

REALIP_CHANGED=0
[[ -s "$STATE_DIR/ips-v4" && -s "$STATE_DIR/ips-v6" ]] \
    || die "no cached Cloudflare ranges in $STATE_DIR; run: hpldn-firewall refresh"
write_realip
apply_family 4 "$STATE_DIR/ips-v4"
apply_family 6 "$STATE_DIR/ips-v6"

if (( REALIP_CHANGED )) && systemctl is-active --quiet nginx; then
    nginx -t -q && systemctl reload nginx && log "nginx reloaded with the new ranges"
fi
