#!/usr/bin/env bash
# =====================================================================
# Verify a serving dump's Ed25519 SIGNATURE with openssl alone.
#
#     verify_dump_signature.sh DUMP_DIR [TRUSTED_KEYS_DIR]
#
# Installed as /usr/local/sbin/hpldn-verify-dump by bootstrap.sh; run by
# restore_serving_db.sh before it trusts SHA256SUMS or runs any of the
# dump's SQL. TRUSTED_KEYS_DIR defaults to /usr/local/share/hpldn/trusted_keys
# (cloud/vps/trusted_keys/, committed public keys named <key id>.pem).
#
# SIGNATURE is "ed25519 <key id> <base64 signature>" over
#     hpldn-serving-dump-v1
#     <sha256>  manifest.json
#     <sha256>  SHA256SUMS
# i.e. `sha256sum manifest.json SHA256SUMS` after a domain line, as
# export_serving_db.signed_payload writes it. Exit 0 only when the signature
# is valid for a trusted key; the checksums themselves are checked after.
# =====================================================================
set -euo pipefail

DOMAIN=hpldn-serving-dump-v1

die() { echo "verify-dump: $*" >&2; exit 1; }

[[ $# -ge 1 && $# -le 2 ]] || die "usage: $0 DUMP_DIR [TRUSTED_KEYS_DIR]"
DUMP_DIR="$1"
TRUSTED_DIR="${2:-/usr/local/share/hpldn/trusted_keys}"
command -v openssl >/dev/null || die "openssl is not installed"

sig_file="$DUMP_DIR/SIGNATURE"
[[ -f "$sig_file" && ! -L "$sig_file" ]] || die "$DUMP_DIR is not signed (no SIGNATURE); export it with --sign"
for f in manifest.json SHA256SUMS; do
    [[ -f "$DUMP_DIR/$f" && ! -L "$DUMP_DIR/$f" ]] || die "$DUMP_DIR/$f is missing"
done

read -r scheme kid signature extra <"$sig_file" || die "SIGNATURE is empty"
[[ "$scheme" == ed25519 && -z "${extra:-}" ]] || die "SIGNATURE is not 'ed25519 <key id> <signature>'"
[[ "$kid" =~ ^[0-9a-f]{16}$ ]] || die "SIGNATURE names a malformed key id"
[[ "$signature" =~ ^[A-Za-z0-9+/]+={0,2}$ ]] || die "SIGNATURE holds no base64 signature"
pem="$TRUSTED_DIR/$kid.pem"
[[ -f "$pem" ]] || die "the dump is signed with key $kid, which is not in $TRUSTED_DIR"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
{
    echo "$DOMAIN"
    (cd "$DUMP_DIR" && sha256sum manifest.json SHA256SUMS)
} >"$work/payload"
base64 -d <<<"$signature" >"$work/sig" 2>/dev/null || die "SIGNATURE is not valid base64"
[[ "$(stat -c %s "$work/sig")" == 64 ]] || die "the signature is not 64 bytes"
openssl pkeyutl -verify -pubin -inkey "$pem" -rawin -in "$work/payload" -sigfile "$work/sig" >/dev/null 2>&1 \
    || die "the signature does not match manifest.json and SHA256SUMS for key $kid"
echo "verify-dump: $DUMP_DIR signed by $kid" >&2
