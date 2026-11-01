"""
Tests for the local stack's render of production's nginx and guard configuration.

``python -m hpldn.backend.cloud.local up`` runs these files; here they are only
rendered into a temporary directory. The ``nginx -t`` check (marker
``nginx``) fails when no nginx binary is found (neither on ``PATH`` nor
unpacked by ``fetch_nginx.sh`` into ``tmp/local-stack/nginx-bin``)::

    pytest src/hpldn/backend/tests/infra/test_local_stack.py
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from hpldn.backend.api import config
from hpldn.backend.cloud.local import __main__ as launcher
from hpldn.backend.cloud.local import render as local_render
from hpldn.backend.cloud.local.render import PRODUCTION_PATHS, RenderError, Stack, find_nginx, nginx_command, render

API_KEY = "0123456789abcdef"  # gitleaks:allow (public fingerprint or test fixture)
SITE_CONF = local_render.VPS_DIR / "nginx" / "hpldn.conf"
LOCATION = re.compile(r"^\s*location\s+([^{]+?)\s*\{", re.MULTILINE)


def _directives(text: str) -> str:
    """`text` without its comment lines."""
    return "".join(line for line in text.splitlines(keepends=True) if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def nginx_binary() -> Path | None:
    return find_nginx(Stack.default())


@pytest.fixture(scope="module")
def stack(tmp_path_factory, nginx_binary) -> Stack:
    """A stack rendered into a temporary directory; the repository's frontend build path is kept."""
    rendered = Stack(tmp_path_factory.mktemp("ls"))
    render(rendered, api_key=API_KEY, nginx=nginx_binary)
    return rendered


def _rendered_nginx(stack: Stack) -> dict[str, str]:
    """The copied production files and the includes; nginx.conf (the stand-in for Ubuntu's) is left out."""
    paths = [stack.site_conf, *stack.includes.rglob("*.conf")]
    return {str(path.relative_to(stack.nginx_dir)): path.read_text() for path in paths}


def test_render_writes_every_file(stack):
    for path in (
        stack.guard_config,
        stack.etc / "guard.local.yaml",
        stack.nginx_conf,
        stack.site_conf,
        stack.includes / "api_proxy.conf",
        stack.includes / "api_cache.conf",
        stack.includes / "security_headers.conf",
        stack.includes / "cloudflare_realip.conf",
        stack.includes / "api_key.conf",
        stack.banned_conf,
        stack.crawlers_conf,
        stack.cloudflare_dir / "ips-v4",
        stack.cloudflare_dir / "ips-v6",
    ):
        assert path.is_file(), path
    assert (stack.includes / "api_key.conf").stat().st_mode & 0o777 == 0o600
    assert API_KEY in (stack.includes / "api_key.conf").read_text()
    assert "dry_run: true" in (stack.etc / "guard.local.yaml").read_text()
    # render_guard_nginx.py's limit includes.
    assert list(stack.includes.glob("limits/*.conf")) or list(stack.includes.glob("*limit*.conf"))


def test_no_production_path_survives(stack):
    for name, text in _rendered_nginx(stack).items():
        directives = _directives(text)
        for path in (*PRODUCTION_PATHS, "ssl_"):
            assert path not in directives, f"{name} keeps {path!r}"
    site = stack.site_conf.read_text()
    assert f"listen {local_render.LISTEN} default_server;" in site
    assert f"listen {local_render.LISTEN};" in site
    assert str(stack.socket) in site


def test_every_production_location_is_kept(stack):
    production = LOCATION.findall(_directives(SITE_CONF.read_text()))
    local = LOCATION.findall(stack.site_conf.read_text())
    assert production, "hpldn.conf has no location blocks"
    assert sorted(production) == sorted(local)


def test_realip_trusts_only_the_loopback(stack):
    realip = _directives((stack.includes / "cloudflare_realip.conf").read_text())
    assert re.findall(r"set_real_ip_from\s+(\S+);", realip) == ["127.0.0.1", "::1"]
    assert "real_ip_header CF-Connecting-IP;" in realip


def test_nginx_workers_follow_the_cpus_the_stack_sees(stack):
    """`auto` on the host: one worker per CPU the stack may use, not per CPU of the workstation."""
    workers = re.search(r"^worker_processes (\d+);", stack.nginx_conf.read_text(), re.MULTILINE)
    assert workers is not None
    assert int(workers.group(1)) == len(os.sched_getaffinity(0))


def test_a_reshaped_site_fails_the_render(stack):
    sources = {"hpldn.conf": SITE_CONF.read_text().replace("@SERVER_NAME@", "example.org")}
    with pytest.raises(RenderError, match="@SERVER_NAME@"):
        local_render.substitute(sources, stack)


def test_a_non_alphanumeric_api_key_is_refused(tmp_path):
    with pytest.raises(RenderError, match="HPLDN_API_KEY"):
        render(Stack(tmp_path / "ls"), api_key='x"; return 200; #')


@pytest.mark.nginx
def test_nginx_accepts_the_render(stack, nginx_binary):
    if nginx_binary is None:
        pytest.fail(
            "missing prerequisite: no nginx on PATH or in tmp/local-stack/nginx-bin "
            "(run src/hpldn/backend/cloud/local/fetch_nginx.sh)",
            pytrace=False,
        )
    result = subprocess.run(nginx_command(stack, nginx_binary, "-t"), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_host_cpus_match_the_production_host():
    cpus = launcher.host_cpus()
    assert len(cpus) == min(launcher.HOST_CPUS, len(os.sched_getaffinity(0)))
    assert set(cpus) <= os.sched_getaffinity(0)


@pytest.fixture
def clean_settings(monkeypatch):
    """`get_settings` with placeholder database settings, uncached before and after."""
    monkeypatch.setenv("POSTGRES_API_PASSWORD", "not-used")
    monkeypatch.setenv("POSTGRES_API_DATABASE", "not-used")
    monkeypatch.setenv(config.CAPTCHA_DISABLED_ENV, "1")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    "url",
    ["https://www.google.com/recaptcha/api/siteverify", "http://10.0.0.5:18999/siteverify", "file:///etc/passwd"],
)
def test_a_non_loopback_verify_url_fails_startup(clean_settings, monkeypatch, url):
    monkeypatch.setenv(config.VERIFY_URL_ENV, url)
    with pytest.raises(RuntimeError, match=config.VERIFY_URL_ENV):
        config.get_settings()


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "localhost"])
def test_a_loopback_verify_url_is_used(clean_settings, monkeypatch, host):
    url = f"http://{host}:18999/siteverify"
    monkeypatch.setenv(config.VERIFY_URL_ENV, url)
    assert config.get_settings().recaptcha_verify_url == url


def test_without_the_override_google_checks_the_tokens(clean_settings, monkeypatch):
    monkeypatch.setenv(config.VERIFY_URL_ENV, "")
    assert config.get_settings().recaptcha_verify_url == config.SITEVERIFY_URL
