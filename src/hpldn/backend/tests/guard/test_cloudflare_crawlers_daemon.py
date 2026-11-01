"""
`guard.cloudflare`, `guard.crawlers` and `guard.daemon`, against mocked HTTP and a temporary state directory.
"""

import json
import math

import httpx
import pytest

from hpldn.backend.guard import cloudflare, daemon, sinks
from hpldn.backend.guard.cloudflare import CloudflareClient, CloudflareError
from hpldn.backend.guard.config import Bans, BanRule, Cloudflare, GuardConfig
from hpldn.backend.guard.crawlers import CrawlerRanges, geo_lines, parse_ranges, write_atomic
from hpldn.backend.guard.store import Ban

ACCOUNT = "0123456789abcdef0123456789abcdef"
T0 = 1_790_000_000.0


# ---------------------------------------------------------------------------
# Cloudflare client
# ---------------------------------------------------------------------------


def _client(handler):
    seen = []

    def recording(request):
        seen.append(request)
        return handler(request)

    return CloudflareClient("tok", ACCOUNT, transport=httpx.MockTransport(recording)), seen


def _ok(result):
    return httpx.Response(200, json={"success": True, "result": result, "errors": []})


def test_the_client_needs_a_token_and_an_account():
    with pytest.raises(CloudflareError, match=cloudflare.TOKEN_ENV):
        CloudflareClient("", ACCOUNT)
    with pytest.raises(CloudflareError, match="account_id"):
        CloudflareClient("tok", "")


def test_find_create_replace_and_status_call_the_lists_api():
    lists = [{"id": "L1", "name": "other", "kind": "ip"}, {"id": "L2", "name": "hpldn_banned", "kind": "ip"}]

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/rules/lists"):
            return _ok(lists)
        if request.method == "POST":
            return _ok({"id": "L3", **json.loads(request.content)})
        if request.method == "PUT":
            return _ok({"operation_id": "op-9"})
        return _ok({"status": "completed"})

    client, seen = _client(handler)
    assert client.find_list("hpldn_banned")["id"] == "L2"
    assert client.find_list("missing") is None
    assert client.create_list("new", "d")["id"] == "L3"
    assert client.replace_items("L2", [{"ip": "203.0.113.1", "comment": "c"}]) == "op-9"
    assert client.operation_status("op-9") == {"status": "completed"}
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    # Exact paths: Cloudflare answers .../rules/lists/ (a trailing slash) 404.
    base = f"/client/v4/accounts/{ACCOUNT}/rules/lists"
    assert [r.url.path for r in seen] == [base, base, base, f"{base}/L2/items", f"{base}/bulk_operations/op-9"]
    assert seen[3].method == "PUT"
    assert json.loads(seen[3].content) == [{"ip": "203.0.113.1", "comment": "c"}]
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}),
        httpx.Response(200, json={"success": False, "errors": [{"message": "nope"}]}),
        httpx.Response(502, text="<html>bad gateway</html>"),
    ],
)
def test_an_api_failure_is_a_cloudflare_error_with_its_message(response):
    client, _ = _client(lambda request: response)
    with pytest.raises(CloudflareError, match="answered"):
        client.find_list("hpldn_banned")


def test_a_transport_failure_is_a_cloudflare_error():
    def fail(request):
        raise httpx.ConnectError("down")

    client, _ = _client(fail)
    with pytest.raises(CloudflareError, match="ConnectError"):
        client.replace_items("L1", [])


def test_create_list_refuses_an_answer_without_an_id():
    client, _ = _client(lambda request: _ok({"name": "x"}))
    with pytest.raises(CloudflareError, match="returned"):
        client.create_list("x", "d")


def test_setup_is_idempotent_and_refuses_a_list_of_the_wrong_kind():
    state = {"lists": []}

    def handler(request):
        if request.method == "POST":
            created = {"id": "L9", **json.loads(request.content)}
            state["lists"].append(created)
            return _ok(created)
        return _ok(state["lists"])

    client, seen = _client(handler)
    assert cloudflare.setup(client, "hpldn_banned")["id"] == "L9"
    assert cloudflare.setup(client, "hpldn_banned")["id"] == "L9"
    assert [r.method for r in seen] == ["GET", "POST", "GET"]
    state["lists"][0]["kind"] = "asn"
    with pytest.raises(CloudflareError, match="not an ip list"):
        cloudflare.setup(client, "hpldn_banned")


def test_list_items_carry_the_rule_and_expiry():
    items = cloudflare.list_items([Ban("203.0.113.1", "flood", 0, T0, T0 + 3600)])
    assert items[0]["ip"] == "203.0.113.1" and items[0]["comment"].startswith("hpldn-guard flood until ")


# ---------------------------------------------------------------------------
# Crawler ranges
# ---------------------------------------------------------------------------

GOOGLE = "https://example.test/google.json"
BING = "https://example.test/bing.json"


def test_parse_ranges_accepts_the_published_shape_only():
    assert parse_ranges({"prefixes": [{"ipv4Prefix": "66.249.64.0/27"}, {"ipv6Prefix": "2001:4860:4801:10::/64"}]}) == [
        "66.249.64.0/27", "2001:4860:4801:10::/64"]
    for bad in ({}, {"prefixes": []}, {"prefixes": ["x"]}, {"prefixes": [{"other": 1}]},
                {"prefixes": [{"ipv4Prefix": "not-a-cidr"}]}, []):
        with pytest.raises(ValueError):
            parse_ranges(bad)


def test_refresh_writes_the_cache_and_nginx_conf_and_keeps_a_failed_urls_cache(tmp_path):
    ranges = CrawlerRanges(tmp_path / "crawlers.json", tmp_path / "nginx" / "crawlers.conf")
    good = {GOOGLE: {"prefixes": [{"ipv4Prefix": "66.249.64.0/27"}]}, BING: {"prefixes": [{"ipv6Prefix": "2620:1ec::/64"}]}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=good[str(request.url)]))
    assert ranges.refresh([GOOGLE, BING], transport=transport) == ["66.249.64.0/27", "2620:1ec::/64"]
    conf = (tmp_path / "nginx" / "crawlers.conf").read_text()
    assert "66.249.64.0/27 1;" in conf and "2620:1ec::/64 1;" in conf
    assert ranges.age() is not None and ranges.age() < 60

    # Google answers garbage and Bing 500: both keep their cached ranges.
    def broken(request):
        if str(request.url) == GOOGLE:
            return httpx.Response(200, json={"prefixes": []})
        return httpx.Response(500)

    assert ranges.refresh([GOOGLE, BING], transport=httpx.MockTransport(broken)) == ["66.249.64.0/27", "2620:1ec::/64"]
    # A URL no longer configured is dropped.
    assert ranges.refresh([GOOGLE], transport=httpx.MockTransport(broken)) == ["66.249.64.0/27"]
    assert ranges.ranges() == ["66.249.64.0/27"]


def test_a_corrupt_or_missing_cache_starts_empty(tmp_path):
    ranges = CrawlerRanges(tmp_path / "crawlers.json", tmp_path / "crawlers.conf")
    assert ranges.ranges() == [] and ranges.age() is None
    (tmp_path / "crawlers.json").write_text("{not json")
    assert ranges.ranges() == []


def test_write_atomic_and_geo_lines(tmp_path):
    path = tmp_path / "a" / "b.conf"
    write_atomic(path, geo_lines(["10.0.0.0/8"], "header"), mode=0o600)
    assert path.read_text() == "# header\n10.0.0.0/8 1;\n"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["b.conf"]


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'<190>Oct  3 12:00:00 host nginx: {"ip":"203.0.113.1","s":"429"}', {"ip": "203.0.113.1", "s": "429"}),
        (b'{"ip":"203.0.113.1"}\x00\n', {"ip": "203.0.113.1"}),
        (b"<190>no json here", None),
        (b"<190>nginx: {broken", None),
        (b"<190>nginx: [1, 2]", None),
        (b'\xff\xfe{"ip":"x"}', {"ip": "x"}),
    ],
)
def test_parse_datagram(data, expected):
    assert daemon.parse_datagram(data) == expected


def _paths(tmp_path) -> daemon.Paths:
    return daemon.Paths(state_dir=tmp_path / "state", socket=tmp_path / "log.sock", cloudflare_dir=tmp_path / "cf")


def _config(**cloudflare_settings) -> GuardConfig:
    return GuardConfig(
        bans=Bans(rules={"flood": BanRule(count=3, window=10)}, durations=(60, math.inf)),
        cloudflare=Cloudflare(**cloudflare_settings),
    )


def test_paths_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HPLDN_GUARD_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("HPLDN_GUARD_SOCKET", str(tmp_path / "x.sock"))
    monkeypatch.setenv("HPLDN_GUARD_CLOUDFLARE_DIR", str(tmp_path / "cf"))
    paths = daemon.Paths.from_env()
    assert paths.database == tmp_path / "s" / "guard.db"
    assert paths.banned_conf == tmp_path / "s" / "nginx" / "banned.conf"
    assert paths.cloudflare_files == [tmp_path / "cf" / "ips-v4", tmp_path / "cf" / "ips-v6"]


def test_the_daemon_turns_log_lines_into_a_ban_in_nginx(tmp_path):
    guard = daemon.Guard(_config(), _paths(tmp_path))
    try:
        assert guard.cloudflare is None
        for i in range(3):
            guard.handle(json.dumps({"t": T0 + i, "ip": "203.0.113.20", "u": "/api/price", "s": "200"}).encode())
        guard.handle(b"<190>nginx: not json")
        assert guard.handled == 3
        guard.tick(now=T0 + 3)
        assert "203.0.113.20 1;" in guard.paths.banned_conf.read_text()
    finally:
        guard.store.close()


def test_the_daemon_picks_up_a_cli_ban_and_lifts_it_when_expired(tmp_path):
    paths = _paths(tmp_path)
    guard = daemon.Guard(_config(), paths)
    guard.nginx = sinks.NginxSink(paths.banned_conf, min_interval=0)
    cli = daemon.Store(paths.database)
    try:
        guard.tick(now=T0)
        cli.put_ban(Ban("198.51.100.9", "manual", 0, T0, T0 + 30))
        guard.tick(now=T0 + 1)
        assert guard.detector.is_banned("198.51.100.9", T0 + 1)
        assert "198.51.100.9 1;" in paths.banned_conf.read_text()
        guard.tick(now=T0 + 120)  # housekeeping is due: the ban has expired
        assert not guard.detector.is_banned("198.51.100.9", T0 + 120)
        assert guard.store.count_active(T0 + 120) == 0
    finally:
        cli.close()
        guard.store.close()


def test_the_daemon_reloads_protected_ranges_when_their_files_change(tmp_path):
    paths = _paths(tmp_path)
    guard = daemon.Guard(_config(), paths)
    try:
        guard.load_ranges(force=True)
        assert guard.protection.size("cloudflare") == 0
        paths.cloudflare_dir.mkdir()
        (paths.cloudflare_dir / "ips-v4").write_text("173.245.48.0/20\n103.21.244.0/22\n")
        guard.load_ranges()
        assert guard.protection.size("cloudflare") == 2
        assert guard.protection.covering("173.245.48.7") == "cloudflare"
    finally:
        guard.store.close()


def test_the_cloudflare_sink_is_on_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(cloudflare.TOKEN_ENV, raising=False)
    guard = daemon.Guard(_config(enabled=True, account_id=ACCOUNT), _paths(tmp_path))
    assert guard.cloudflare is None  # no token: logged and off
    guard.store.close()
    dry = daemon.Guard(_config(enabled=True, account_id=ACCOUNT, dry_run=True), _paths(tmp_path / "dry"))
    assert dry.cloudflare is not None and dry.cloudflare.dry_run
    dry.tick(now=T0)
    dry.store.close()


def test_refresh_crawlers_only_when_stale(tmp_path, monkeypatch):
    guard = daemon.Guard(_config(), _paths(tmp_path))
    calls = []
    monkeypatch.setattr(guard.crawlers, "refresh", lambda urls: calls.append(urls) or [])
    monkeypatch.setattr(guard.crawlers, "age", lambda: None)
    guard.refresh_crawlers_if_stale()
    monkeypatch.setattr(guard.crawlers, "age", lambda: 10.0)
    guard.refresh_crawlers_if_stale()
    assert len(calls) == 1
    guard.store.close()
