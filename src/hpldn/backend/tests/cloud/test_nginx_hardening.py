"""
The serving host's nginx, systemd and PostgreSQL files: the hardening the OCI deployment relies on.

Read as text (``nginx -t`` over the whole rendered site is
`test_local_stack.test_nginx_accepts_the_render`).
"""

import re
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[2] / "cloud" / "vps"
SITE = (VPS / "nginx" / "hpldn.conf").read_text()
PROXY = (VPS / "nginx" / "api_proxy.conf").read_text()
HEADERS = (VPS / "nginx" / "security_headers.conf").read_text()


def _directives(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_authenticated_origin_pulls_are_unconditional():
    site = _directives(SITE)
    assert "ssl_client_certificate /etc/hpldn/tls/aop_ca.pem;" in site
    assert re.search(r"^\s*ssl_verify_client on;", site, re.MULTILINE)
    assert "ssl_verify_depth 1;" in site
    assert "origin_pull" not in site
    assert "ssl_verify_client optional" not in site
    # A client without the certificate gets no 400 page, just a closed connection.
    assert "error_page 495 496 = @hpldn_no_aop;" in site
    assert re.search(r"location @hpldn_no_aop \{\s*return 444;\s*\}", site)


def test_tls_is_ecdhe_aead_with_modern_curves():
    site = _directives(SITE)
    ciphers = re.search(r"ssl_ciphers\s+([^;]+);", site).group(1).split(":")
    assert ciphers and all(c.startswith("ECDHE-") and ("GCM" in c or "CHACHA20" in c) for c in ciphers)
    assert "ssl_ecdh_curve      X25519:prime256v1;" in site
    assert "ssl_protocols       TLSv1.2 TLSv1.3;" in site
    assert "listen 443 ssl http2;" in site
    assert "server_tokens off;" in site
    assert "ssl_reject_handshake on;" in site


def test_source_maps_and_the_asset_manifest_are_never_served():
    site = _directives(SITE)
    assert re.search(r"location ~\* \\\.map\$ \{\s*return 404;", site)
    assert re.search(r"location = /asset-manifest\.json \{\s*return 404;", site)


def test_the_api_upstream_is_the_unix_socket_only():
    site, proxy = _directives(SITE), _directives(PROXY)
    assert re.search(r"upstream hpldn_api \{\s*server unix:/run/hpldn-api/api\.sock;", site)
    assert "proxy_pass http://hpldn_api;" in proxy
    assert "127.0.0.1:8000" not in site + proxy
    assert "proxy_hide_header X-Powered-By;" in proxy


def test_the_security_headers():
    headers = _directives(HEADERS)
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains" always' in headers
    assert "preload" not in headers
    assert 'Cross-Origin-Opener-Policy "same-origin"' in headers
    assert 'Cross-Origin-Resource-Policy "same-origin"' in headers
    assert "script-src 'self' https://www.google.com/recaptcha/" in headers


def test_the_banned_answer_carries_the_security_headers():
    block = re.search(r"location = /__hpldn_banned \{(.*?)\n    \}", SITE, re.DOTALL).group(1)
    assert "include /etc/nginx/hpldn/security_headers.conf;" in block


def _unit(name: str) -> str:
    return (VPS / "systemd" / name).read_text()


def test_the_api_listens_on_a_socket_nginx_alone_can_open():
    socket_unit = _unit("hpldn-api.socket")
    assert "ListenStream=/run/hpldn-api/api.sock" in socket_unit
    assert "SocketUser=hpldn" in socket_unit and "SocketGroup=www-data" in socket_unit and "SocketMode=0660" in socket_unit
    # systemd makes the directory root:root: nginx needs o+x to get to the socket.
    assert "DirectoryMode=0755" in socket_unit
    service = _directives(_unit("hpldn-api.service"))
    assert "--bind" not in service
    assert "-k hpldn.backend.api.worker.HardenedUvicornWorker" in service
    assert "--limit-request-fields 50" in service


@pytest.mark.parametrize("unit", ["hpldn-api.service", "hpldn-guard.service"])
def test_secrets_are_credentials_and_private_ranges_are_unreachable(unit):
    text = _unit(unit)
    assert "LoadCredentialEncrypted=" in text
    assert "EnvironmentFile=-/etc/hpldn/guard.env" not in text
    deny = re.search(r"^IPAddressDeny=(.*)$", text, re.MULTILINE).group(1).split()
    assert {"169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fd00::/8"} <= set(deny)
    assert "SystemCallFilter=~@privileged @resources" in text
    # The API loads CatBoost, whose aarch64 .so has text relocations.
    mdwe = re.search(r"^MemoryDenyWriteExecute=yes$", text, re.MULTILINE) is not None
    assert mdwe == (unit != "hpldn-api.service")
    assert "Environment=HPLDN_ENV=production" in text


def test_the_guard_may_give_its_log_socket_to_nginx():
    # ~@privileged drops @chown; the guard chowns log.sock to www-data.
    filters = re.findall(r"^SystemCallFilter=(.*)$", _unit("hpldn-guard.service"), re.MULTILINE)
    assert filters.index("@chown") > filters.index("~@privileged @resources")


def test_the_api_credentials_are_its_three_secrets():
    names = re.findall(r"^LoadCredentialEncrypted=([a-z_]+):", _unit("hpldn-api.service"), re.MULTILINE)
    assert sorted(names) == ["api_key", "pass_secret", "recaptcha_secret_key"]


def test_postgres_peer_authentication_only():
    hba = _directives((VPS / "postgres" / "pg_hba.conf").read_text())
    rows = [line.split() for line in hba.splitlines() if line.strip()]
    assert ["local", "hpldn_serve", "hpldn_api", "peer", "map=hpldn_api"] in rows
    assert not any(row[0] == "host" for row in rows) and not any("scram" in " ".join(row) or "md5" in row for row in rows)
    ident = [line.split() for line in _directives((VPS / "postgres" / "pg_ident.conf").read_text()).splitlines() if line.strip()]
    assert ident == [["hpldn_api", "hpldn", "hpldn_api"]]
    conf = (VPS / "postgres" / "10-hpldn.conf").read_text()
    for line in ("listen_addresses = ''", "log_statement = 'none'", "log_parameter_max_length = 0", "log_parameter_max_length_on_error = 0"):
        assert line in conf


def test_bootstrap_has_every_stdin_mode_and_no_secret_on_argv():
    text = (VPS / "bootstrap.sh").read_text()
    for flag in ("--set-tls", "--set-recaptcha-keys", "--set-cloudflare-token", "--set-cloudflare-account",
                 "--rotate-pass-secret", "--rotate-api-key", "--close-public-ssh"):
        assert f"{flag})" in text
    assert "--authkey=" not in text and "TS_AUTHKEY" not in text
    assert "--auth-key=\"file:$TS_KEY_FILE\"" in text
    assert "systemd-creds encrypt --with-key=host" in text
    assert "POSTGRES_API_PASSWORD \"$" not in text


def test_deploy_pins_host_keys_and_ships_no_source():
    text = (VPS / "deploy.sh").read_text()
    assert "StrictHostKeyChecking=yes" in text and "accept-new" not in text
    assert "uv build" in text and "compileall -b" in text and "-name '*.py' -type f -delete" in text
    assert "npm run build:prod" in text and "--exclude '*.map'" in text
    # A deploy onto bootstrap's placeholder TLS fails (Cloudflare would answer 526).
    assert "/etc/hpldn/tls/placeholder" in text


def test_the_firewall_keeps_instance_metadata_to_root():
    text = (VPS / "firewall.sh").read_text()
    assert "--uid-owner 0" in text and "169.254.169.254/32" in text
