#!/usr/bin/env bash
# =====================================================================
# Install the pinned OpenTofu CLI (and the tools that check it) into
# ~/.local/bin on the operator's laptop.
#
#     scripts/install_tofu.sh              # tofu + cosign (to verify tofu)
#     scripts/install_tofu.sh --gitleaks   # ... and gitleaks (run_all.sh audit/lint)
#     scripts/install_tofu.sh --check      # print what is installed; install nothing
#
# The same pin-and-verify pattern bootstrap.sh uses for uv:
#   1. cosign is pinned by version and SHA-256 (it is what checks the rest);
#   2. tofu's SHA256SUMS is verified with `cosign verify-blob` against the
#      OpenTofu release workflow's Sigstore identity
#      (release.yml@refs/heads/v<major>.<minor>, GitHub's OIDC issuer);
#   3. the tarball is checked against that SHA256SUMS (sha256sum -c) AND
#      against the digest pinned below, so a re-signed release with a
#      different tarball is still refused.
# To move to a newer release, bump the version and both digests together
# (scripts/install_tofu.sh prints the published digests on a mismatch).
#
# OpenTofu 1.12.x: 1.10 is out of support. Providers are pinned per module
# (versions.tf) and locked for linux_amd64 + linux_arm64 (.terraform.lock.hcl,
# committed): tofu providers lock -platform=linux_amd64 -platform=linux_arm64
# =====================================================================
set -euo pipefail

TOFU_VERSION=1.12.7
TOFU_SHA256_AMD64=8b0967cc14715bfb03d5e04dd0da0a47c668dcb33cb3a84996413a48b1aa51c0
TOFU_SHA256_ARM64=f407848e706d50711937955bbfcddf97929767f73bda4480415c8aff2ea44671

COSIGN_VERSION=3.1.3
COSIGN_SHA256_AMD64=4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71
COSIGN_SHA256_ARM64=c5d324e091826b0d7a78eb16fef316450b4eb9aaec045611c08ba06f5e73220a

GITLEAKS_VERSION=8.30.1
GITLEAKS_SHA256_X64=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
GITLEAKS_SHA256_ARM64=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080

BIN_DIR="${HPLDN_BIN_DIR:-$HOME/.local/bin}"
OIDC_ISSUER=https://token.actions.githubusercontent.com

log() { echo "install_tofu: $*" >&2; }
die() { echo "install_tofu: ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

WITH_GITLEAKS=0
CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --gitleaks) WITH_GITLEAKS=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
done

case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) die "unsupported architecture $(uname -m)" ;;
esac
[[ "$(uname -s)" == Linux ]] || die "only Linux is supported (the laptop and the host are both Linux)"

installed_version() {
    # installed_version BINARY: the version it reports, or nothing.
    local bin="$BIN_DIR/$1"
    [[ -x "$bin" ]] || return 0
    case "$1" in
        tofu) "$bin" version 2>/dev/null | sed -n 's/^OpenTofu v//p' | head -n 1 ;;
        cosign) "$bin" version 2>/dev/null | sed -n 's/^GitVersion:[[:space:]]*v\{0,1\}//p' | head -n 1 ;;
        gitleaks) "$bin" version 2>/dev/null | sed 's/^v//' | head -n 1 ;;
    esac
}

if (( CHECK_ONLY )); then
    for tool in tofu cosign gitleaks; do
        printf '%-9s %s\n' "$tool" "$(installed_version "$tool" || true)"
    done
    exit 0
fi

command -v curl >/dev/null || die "curl is required"
command -v sha256sum >/dev/null || die "sha256sum is required"
install -d -m 0755 "$BIN_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch() { curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$2" "$1"; }

check_digest() {
    # check_digest FILE SHA256 WHAT
    echo "$2  $1" | sha256sum --check --status \
        || die "$3: SHA-256 mismatch (got $(sha256sum "$1" | cut -d' ' -f1), pinned $2)"
}

install_cosign() {
    if [[ "$(installed_version cosign)" == "$COSIGN_VERSION" ]]; then
        log "cosign $COSIGN_VERSION already installed"
        return
    fi
    local sha
    if [[ $ARCH == amd64 ]]; then sha=$COSIGN_SHA256_AMD64; else sha=$COSIGN_SHA256_ARM64; fi
    log "installing cosign $COSIGN_VERSION"
    fetch "https://github.com/sigstore/cosign/releases/download/v$COSIGN_VERSION/cosign-linux-$ARCH" "$WORK/cosign"
    check_digest "$WORK/cosign" "$sha" "cosign-linux-$ARCH"
    install -m 0755 "$WORK/cosign" "$BIN_DIR/cosign"
}

install_tofu() {
    if [[ "$(installed_version tofu)" == "$TOFU_VERSION" && -s "$BIN_DIR/.tofu-$TOFU_VERSION.sha256" ]]; then
        log "OpenTofu $TOFU_VERSION already installed"
        return
    fi
    local base="https://github.com/opentofu/opentofu/releases/download/v$TOFU_VERSION"
    local sums="tofu_${TOFU_VERSION}_SHA256SUMS"
    local tarball="tofu_${TOFU_VERSION}_linux_${ARCH}.tar.gz"
    local sha minor="${TOFU_VERSION%.*}"
    if [[ $ARCH == amd64 ]]; then sha=$TOFU_SHA256_AMD64; else sha=$TOFU_SHA256_ARM64; fi
    log "installing OpenTofu $TOFU_VERSION ($tarball)"
    fetch "$base/$sums" "$WORK/$sums"
    fetch "$base/$sums.sig" "$WORK/$sums.sig"
    fetch "$base/$sums.pem" "$WORK/$sums.pem"
    fetch "$base/$tarball" "$WORK/$tarball"
    # 1. SHA256SUMS was signed by OpenTofu's own release workflow for this minor.
    "$BIN_DIR/cosign" verify-blob \
        --certificate-identity "https://github.com/opentofu/opentofu/.github/workflows/release.yml@refs/heads/v$minor" \
        --certificate-oidc-issuer "$OIDC_ISSUER" \
        --certificate-github-workflow-repository opentofu/opentofu \
        --signature "$WORK/$sums.sig" \
        --certificate "$WORK/$sums.pem" \
        "$WORK/$sums" >/dev/null 2>&1 \
        || die "cosign could not verify $sums against the OpenTofu release identity"
    # 2. The tarball is the one that file names...
    (cd "$WORK" && grep -E "  $tarball\$" "$sums" | sha256sum --check --status) \
        || die "$tarball does not match the signed $sums"
    # 3. ...and the one pinned here.
    check_digest "$WORK/$tarball" "$sha" "$tarball"
    tar -xzf "$WORK/$tarball" -C "$WORK" tofu
    install -m 0755 "$WORK/tofu" "$BIN_DIR/tofu"
    # The verified binary's own digest, which `python -m hpldn.backend.cloud.oci
    # check` compares the tofu on PATH against before every run.
    sha256sum "$BIN_DIR/tofu" | cut -d' ' -f1 >"$BIN_DIR/.tofu-$TOFU_VERSION.sha256"
}

install_gitleaks() {
    if [[ "$(installed_version gitleaks)" == "$GITLEAKS_VERSION" ]]; then
        log "gitleaks $GITLEAKS_VERSION already installed"
        return
    fi
    local arch sha
    if [[ $ARCH == amd64 ]]; then arch=x64; sha=$GITLEAKS_SHA256_X64; else arch=arm64; sha=$GITLEAKS_SHA256_ARM64; fi
    local tarball="gitleaks_${GITLEAKS_VERSION}_linux_${arch}.tar.gz"
    log "installing gitleaks $GITLEAKS_VERSION"
    fetch "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/$tarball" "$WORK/$tarball"
    check_digest "$WORK/$tarball" "$sha" "$tarball"
    tar -xzf "$WORK/$tarball" -C "$WORK" gitleaks
    install -m 0755 "$WORK/gitleaks" "$BIN_DIR/gitleaks"
}

install_cosign
install_tofu
if (( WITH_GITLEAKS )); then install_gitleaks; fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) log "add $BIN_DIR to PATH" ;;
esac
log "$("$BIN_DIR/tofu" version | head -n 1)"
