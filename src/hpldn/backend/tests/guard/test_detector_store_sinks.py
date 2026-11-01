"""
`guard.detector`, `guard.store` and `guard.sinks`: strikes, escalating bans and their enforcement.

The store is SQLite in memory (the guard's own state file, not PostgreSQL).
The detector is replayed with explicit clocks; crawler verification is
driven with fake resolvers, so no DNS is asked.
"""

import math
import socket

import pytest

from hpldn.backend.guard import sinks
from hpldn.backend.guard.cloudflare import CloudflareError
from hpldn.backend.guard.config import Bans, BanRule
from hpldn.backend.guard.detector import CrawlerVerifier, Detector, Hit, Protection, ban_key, parse_networks
from hpldn.backend.guard.store import Ban, Store

T0 = 1_790_000_000.0
FLOOD = BanRule(count=3, window=10)


def _bans(**changes) -> Bans:
    values = {
        "rules": {
            "flood": FLOOD,
            "throttled": BanRule(count=2, window=60, status=429),
            "no_pass": BanRule(count=2, window=60, reject="captcha"),
            "scanner": BanRule(count=1, window=60, paths=(r"\.env",), min_level=2),
            "evading": BanRule(count=2, window=60, while_banned=True),
        },
        "durations": (60, 600, 3600, math.inf),
    }
    values.update(changes)
    return Bans(**values)


@pytest.fixture
def store():
    s = Store()
    yield s
    s.close()


def _flood(detector, ip, start, n=3, **hit):
    ban = None
    for i in range(n):
        ban = detector.observe(Hit(time=start + i, ip=ip, **hit)) or ban
    return ban


# ---------------------------------------------------------------------------
# Keys and hits
# ---------------------------------------------------------------------------


def test_clients_are_keyed_on_their_address_or_ipv6_64():
    assert ban_key("203.0.113.7") == "203.0.113.7"
    assert ban_key("2001:db8:1:2::5") == "2001:db8:1:2::/64"
    assert ban_key("2001:db8:1:2::5", 48) == "2001:db8:1::/48"
    assert ban_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert ban_key("not an ip") is None


def test_a_log_record_becomes_a_hit():
    record = {"t": "1790000000.5", "ip": "203.0.113.7", "m": "GET", "u": "/api/price%20x?q=1", "s": "429",
              "ua": "curl", "rj": "captcha", "b": "1", "c": "0"}
    hit = Hit.from_log(record, now=0)
    assert (hit.time, hit.uri, hit.status, hit.reject, hit.banned, hit.crawler) == (
        1790000000.5, "/api/price x", 429, "captcha", True, False)
    broken = Hit.from_log({"t": "soon", "s": "ok"}, now=T0)
    assert (broken.time, broken.status, broken.ip) == (T0, 0, "")


def test_parse_networks_skips_blanks_comments_and_junk():
    assert [str(n) for n in parse_networks(["10.0.0.0/8 # office", "", "# note", "junk", "2001:db8::1"])] == [
        "10.0.0.0/8", "2001:db8::1/128"]


# ---------------------------------------------------------------------------
# Detector rules and escalation
# ---------------------------------------------------------------------------


def test_a_flood_within_the_window_is_a_strike_and_a_ban(store):
    detector = Detector(_bans(), store)
    assert _flood(detector, "203.0.113.7", T0, n=2) is None
    ban = detector.observe(Hit(time=T0 + 2, ip="203.0.113.7"))
    assert (ban.key, ban.rule, ban.level, ban.expires) == ("203.0.113.7", "flood", 0, T0 + 2 + 60)
    assert detector.is_banned("203.0.113.7", T0 + 3)
    assert store.get_ban("203.0.113.7", T0 + 3) == ban


def test_requests_spread_wider_than_the_window_are_not_a_strike(store):
    detector = Detector(_bans(), store)
    for i in range(10):
        assert detector.observe(Hit(time=T0 + i * 6, ip="203.0.113.7")) is None


@pytest.mark.parametrize(
    ("hit", "rule"),
    [({"status": 429}, "throttled"), ({"reject": "captcha"}, "no_pass")],
)
def test_status_and_reject_rules_count_only_their_requests(store, hit, rule):
    detector = Detector(_bans(rules={rule: _bans().rules[rule]}), store)
    assert detector.observe(Hit(time=T0, ip="198.51.100.1")) is None
    assert detector.observe(Hit(time=T0 + 1, ip="198.51.100.1")) is None
    detector.observe(Hit(time=T0 + 2, ip="198.51.100.1", **hit))
    assert detector.observe(Hit(time=T0 + 3, ip="198.51.100.1", **hit)).rule == rule


def test_a_scanner_starts_at_its_min_level(store):
    detector = Detector(_bans(), store)
    ban = detector.observe(Hit(time=T0, ip="198.51.100.2", uri="/.env"))
    assert (ban.rule, ban.level, ban.expires) == ("scanner", 2, T0 + 3600)


def test_repeat_strikes_escalate_to_the_blacklist(store):
    detector = Detector(_bans(), store)
    levels = []
    for strike in range(5):
        start = T0 + strike * 10_000
        levels.append(_flood(detector, "203.0.113.9", start).level)
    assert levels == [0, 1, 2, 3, 3]
    ban = store.get_ban("203.0.113.9", T0 + 10**9)
    assert ban.permanent and ban.left(T0) == "blacklisted" and ban.until() == "never (blacklisted)"


def test_strikes_are_forgotten_after_strike_memory(store):
    detector = Detector(_bans(strike_memory=100), store)
    assert _flood(detector, "203.0.113.9", T0).level == 0
    assert _flood(detector, "203.0.113.9", T0 + 1000).level == 0


def test_a_banned_client_counts_only_towards_while_banned_rules_and_climbs(store):
    detector = Detector(_bans(), store)
    first = _flood(detector, "203.0.113.10", T0)
    # nginx marks the client banned; only "evading" counts now.
    assert detector.observe(Hit(time=T0 + 5, ip="203.0.113.10", banned=True)) is None
    climbed = detector.observe(Hit(time=T0 + 6, ip="203.0.113.10", banned=True))
    assert (climbed.rule, climbed.level) == ("evading", 1)
    assert climbed.expires > first.expires


def test_verified_crawler_traffic_and_non_addresses_are_not_counted(store):
    detector = Detector(_bans(), store)
    assert _flood(detector, "203.0.113.11", T0, n=5, crawler=True) is None
    assert _flood(detector, "garbage", T0, n=5) is None


def test_protected_ranges_are_never_banned(store):
    protection = Protection(allowlist=["203.0.113.0/24"], crawlers=["66.249.64.0/19"], cloudflare=["2400:cb00::/32"])
    detector = Detector(_bans(), store, protection=protection)
    assert _flood(detector, "203.0.113.12", T0) is None
    assert _flood(detector, "66.249.66.1", T0) is None
    assert _flood(detector, "2400:cb00:1::1", T0) is None
    assert protection.covering("198.51.100.1") is None and protection.size("crawlers") == 1
    assert protection.covering("not a key") is None


def test_a_self_declared_crawler_is_spared_only_when_verified(store):
    verified = {"66.102.0.1"}
    detector = Detector(_bans(), store, crawler_user_agent="(?i)googlebot", verify_crawler=lambda ip: ip in verified)
    assert _flood(detector, "66.102.0.1", T0, user_agent="Googlebot/2.1") is None
    assert _flood(detector, "198.51.100.3", T0, user_agent="Googlebot/2.1").rule == "flood"


def test_max_bans_lifts_the_soonest_to_expire(store):
    detector = Detector(_bans(max_bans=2), store)
    detector.ban("198.51.100.1", "manual", now=T0, duration=100)
    detector.ban("198.51.100.2", "manual", now=T0, duration=50)
    detector.ban("198.51.100.3", "manual", now=T0, duration=200)
    assert [b.key for b in store.active_bans(T0)] == ["198.51.100.1", "198.51.100.3"]


def test_a_manual_ban_never_shortens_an_existing_one(store):
    detector = Detector(_bans(), store)
    long = detector.ban("198.51.100.4", "manual", now=T0, duration=1000)
    short = detector.ban("198.51.100.4", "manual", now=T0 + 1, duration=10)
    assert short.expires == long.expires


def test_unban_reload_and_expire(store):
    detector = Detector(_bans(), store)
    detector.ban("198.51.100.5", "manual", now=T0, duration=10)
    detector.ban("2001:db8:9:9::1", "manual", now=T0, duration=1000)
    assert detector.unban("2001:db8:9:9::77")
    assert not detector.is_banned("2001:db8:9:9::/64", T0)
    other = Detector(_bans(), store)
    other.reload(T0)
    assert other.is_banned("198.51.100.5", T0)
    lifted = other.expire(T0 + 11)
    assert [b.key for b in lifted] == ["198.51.100.5"]
    assert not other.is_banned("198.51.100.5", T0 + 11)


# ---------------------------------------------------------------------------
# Crawler verification
# ---------------------------------------------------------------------------


def _verifier(ptr, forward, **kwargs):
    calls = []

    def reverse(ip):
        calls.append(ip)
        if ip not in ptr:
            raise socket.herror("no PTR")
        return ptr[ip]

    def resolve(name):
        if name not in forward:
            raise socket.gaierror("no A")
        return forward[name]

    return CrawlerVerifier(["googlebot.com", "search.msn.com."], reverse=reverse, forward=resolve, **kwargs), calls


def test_a_crawler_is_verified_by_reverse_dns_and_forward_confirmation():
    verifier, _ = _verifier(
        {"66.249.66.1": ["crawl-66-249-66-1.googlebot.com."], "157.55.39.1": ["msnbot-157-55-39-1.search.msn.com"]},
        {"crawl-66-249-66-1.googlebot.com": ["66.249.66.1"], "msnbot-157-55-39-1.search.msn.com": ["157.55.39.1"]},
    )
    assert verifier("66.249.66.1")
    assert verifier("157.55.39.1")


@pytest.mark.parametrize(
    ("ptr", "forward"),
    [
        ({}, {}),  # no PTR
        ({"203.0.113.5": ["googlebot.com.evil.example"]}, {"googlebot.com.evil.example": ["203.0.113.5"]}),
        ({"203.0.113.5": ["fakegooglebot.com"]}, {"fakegooglebot.com": ["203.0.113.5"]}),
        ({"203.0.113.5": ["crawl.googlebot.com"]}, {"crawl.googlebot.com": ["66.249.66.1"]}),  # forward mismatch
        ({"203.0.113.5": ["crawl.googlebot.com"]}, {}),  # forward fails
    ],
)
def test_a_spoofed_crawler_is_not_verified(ptr, forward):
    verifier, _ = _verifier(ptr, forward)
    assert not verifier("203.0.113.5")


def test_verdicts_are_cached_for_the_ttl_and_bounded():
    now = [T0]
    verifier, calls = _verifier({"66.249.66.1": ["a.googlebot.com"]}, {"a.googlebot.com": ["66.249.66.1"]},
                                ttl=100, clock=lambda: now[0], max_entries=1)
    assert verifier("66.249.66.1") and verifier("66.249.66.1")
    assert calls == ["66.249.66.1"]
    now[0] += 101
    verifier("66.249.66.1")
    assert len(calls) == 2
    verifier("203.0.113.1")
    assert list(verifier._cache) == ["203.0.113.1"]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_strikes_bans_expiry_and_pruning(store):
    store.add_strike("k", "flood", T0)
    store.add_strike("k", "flood", T0 + 10)
    assert store.strikes_since("k", T0 + 5) == 1
    store.put_ban(Ban("k", "flood", 0, T0, T0 + 60))
    store.put_ban(Ban("j", "manual", 1, T0, math.inf))
    assert store.count_active(T0) == 2
    assert [b.key for b in store.active_bans(T0)] == ["k", "j"]
    assert store.get_ban("k", T0 + 61) is None
    assert [b.key for b in store.expire(T0 + 61)] == ["k"]
    assert store.expire(T0 + 61) == []
    assert store.prune(T0 + 100, keep_s=95) == 1
    assert store.forget_strikes("k") == 1
    assert store.remove_ban("j") and not store.remove_ban("j")


def test_a_file_store_sees_another_connections_commit(tmp_path):
    path = tmp_path / "guard.db"
    daemon_side, cli_side = Store(path), Store(path)
    try:
        version = daemon_side.data_version()
        cli_side.put_ban(Ban("203.0.113.1", "manual", 0, T0, T0 + 60))
        assert daemon_side.data_version() != version
        assert daemon_side.get_ban("203.0.113.1", T0).rule == "manual"
    finally:
        daemon_side.close()
        cli_side.close()


def test_ban_times_for_people():
    ban = Ban("k", "flood", 0, T0, T0 + 3 * 3600 + 5 * 60)
    assert ban.left(T0) == "3h05m left"
    assert ban.left(T0 + 10**6) == "0h00m left"
    assert ban.until(utc=True).endswith("UTC")
    assert not ban.permanent


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


def test_nginx_sink_writes_once_per_change_and_debounces(tmp_path):
    now = [100.0]
    sink = sinks.NginxSink(tmp_path / "nginx" / "banned.conf", min_interval=10, clock=lambda: now[0])
    bans = [Ban("203.0.113.2", "flood", 0, T0, T0 + 60), Ban("2001:db8::/64", "flood", 0, T0, T0 + 60)]
    assert sink.offer(bans)
    text = (tmp_path / "nginx" / "banned.conf").read_text()
    assert text.splitlines()[1:] == ["2001:db8::/64 1;", "203.0.113.2 1;"]
    assert not sink.offer(list(reversed(bans)))  # same state
    extended = [Ban("203.0.113.2", "flood", 1, T0, T0 + 600), bans[1]]
    assert not sink.offer(extended)  # an extended ban is not a rewrite
    now[0] += 1
    assert not sink.offer(bans[:1])  # changed, but inside the interval
    now[0] += 10
    assert sink.offer(bans[:1])


def test_a_failing_sink_backs_off_and_retries(monkeypatch, tmp_path):
    now = [0.0]
    sink = sinks.NginxSink(tmp_path / "banned.conf", min_interval=10, clock=lambda: now[0])

    def fail(path, text, mode=0o644):
        raise OSError("disk full")

    monkeypatch.setattr(sinks, "write_atomic", fail)
    bans = [Ban("203.0.113.2", "flood", 0, T0, T0 + 60)]
    assert not sink.offer(bans)
    assert (sink.failures, sink._wait) == (1, 20)
    now[0] += 15
    assert not sink.offer(bans) and sink.failures == 1  # still waiting
    monkeypatch.undo()
    now[0] += 10
    assert sink.offer(bans) and sink.failures == 0


class _FakeClient:
    def __init__(self, lists=None, fail=False):
        self.lists = lists if lists is not None else [{"id": "L1", "name": "hpldn_banned", "kind": "ip"}]
        self.fail = fail
        self.puts = []

    def find_list(self, name):
        return next((item for item in self.lists if item["name"] == name), None)

    def replace_items(self, list_id, items):
        if self.fail:
            raise CloudflareError("boom")
        self.puts.append((list_id, items))
        return "op-1"


def test_cloudflare_sink_replaces_the_list_items():
    client = _FakeClient()
    sink = sinks.CloudflareSink(client, "hpldn_banned", debounce=0, clock=lambda: 0.0)
    assert sink.offer([Ban("203.0.113.2", "flood", 0, T0, math.inf)])
    [(list_id, items)] = client.puts
    assert list_id == "L1"
    assert items == [{"ip": "203.0.113.2", "comment": "hpldn-guard flood until never (blacklisted)"}]


def test_cloudflare_sink_fails_without_its_list_and_forgets_the_id_on_error():
    sink = sinks.CloudflareSink(_FakeClient(lists=[]), "hpldn_banned", debounce=0, clock=lambda: 0.0)
    assert not sink.offer([Ban("203.0.113.2", "flood", 0, T0, T0 + 60)]) and sink.failures == 1
    failing = sinks.CloudflareSink(_FakeClient(fail=True), "hpldn_banned", debounce=0, clock=lambda: 0.0)
    assert not failing.offer([Ban("203.0.113.2", "flood", 0, T0, T0 + 60)])
    assert failing._list_id is None


def test_cloudflare_sink_dry_run_needs_no_client():
    sink = sinks.CloudflareSink(None, "hpldn_banned", debounce=0, dry_run=True, clock=lambda: 0.0)
    assert sink.offer([Ban(f"203.0.113.{i}", "flood", 0, T0, T0 + 60) for i in range(25)])
    with pytest.raises(ValueError):
        sinks.CloudflareSink(None, "hpldn_banned", debounce=0)
