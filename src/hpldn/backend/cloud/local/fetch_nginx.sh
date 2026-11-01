#!/usr/bin/env bash
# Unpack Ubuntu's nginx (and nginx-common, for mime.types) into a directory,
# without sudo, for the local stack when no nginx is installed:
#
#     bash fetch_nginx.sh tmp/local-stack/nginx-bin
#
# apt-get download fetches the .debs this machine's apt sources would install
# (the same build production's apt-get install nginx gets); dpkg-deb -x unpacks
# them. The binary lands at <dir>/usr/sbin/nginx. Run by
# `python -m hpldn.backend.cloud.local up` when nginx is not on PATH.
set -euo pipefail

dest="${1:?usage: fetch_nginx.sh <directory>}"
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cd "$work"
apt-get download nginx nginx-common >/dev/null
for deb in ./*.deb; do
    dpkg-deb -x "$deb" "$dest"
done
"$dest/usr/sbin/nginx" -v
