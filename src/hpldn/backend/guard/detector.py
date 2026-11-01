"""
Turns nginx's guard log into strikes, and strikes into escalating bans.

Pure apart from the `Store` and the injected crawler check, and driven by the
time each request carries, so tests replay a log with any clock they like.

A client is keyed on its address, or its /64 for IPv6 (``bans.ipv6_prefix``),
since one IPv6 host holds the whole prefix. Each ``bans.rules`` entry counts the
client's matching requests over a sliding window; reaching its count is a
strike, and the ban is ``bans.durations[n]`` for the client's n-th strike
within ``bans.strike_memory`` (never less than the rule's ``min_level``). The
last step may be ``permanent``: the blacklist, which only ``unban`` lifts.

Requests from a client that is already banned count only towards the
``while_banned`` rules; such a strike moves the ban straight on to the next,
longer step, so a client hammering through its ban climbs to the blacklist.

Never banned, whatever they send:

* requests from a verified crawler range (the log's ``c`` flag);
* ``bans.allowlist_cidrs``, the crawlers' published ranges and Cloudflare's
  own ranges (`Protection`);
* a client whose user agent claims to be a search crawler and whose reverse
  DNS confirms it (`CrawlerVerifier`), even outside the published ranges.
"""

import ipaddress
import logging
import math
import re
import socket
import time
import urllib.parse
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from hpldn.backend.guard.config import BanRule, Bans
from hpldn.backend.guard.store import Ban, Store

log = logging.getLogger("hpldn.guard")

# Clients whose windows are kept at once; the least recently seen are forgotten.
MAX_TRACKED = 50_000

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Hit:
    """One request, as nginx's ``hpldn_guard`` log format records it; `uri` is the decoded path, without the query."""

    time: float
    ip: str
    method: str = "GET"
    uri: str = "/"
    status: int = 200
    user_agent: str = ""
    reject: str = ""
    banned: bool = False
    crawler: bool = False

    @classmethod
    def from_log(cls, record: dict, now: float) -> "Hit":
        """Build a hit from one decoded log line; `now` stands in for a missing time."""
        try:
            at = float(record.get("t") or now)
        except ValueError:
            at = now
        try:
            status = int(record.get("s") or 0)
        except ValueError:
            status = 0
        return cls(
            time=at,
            ip=str(record.get("ip", "")),
            method=str(record.get("m", "")),
            uri=urllib.parse.unquote(str(record.get("u", "")).split("?", 1)[0]),
            status=status,
            user_agent=str(record.get("ua", "")),
            reject=str(record.get("rj", "")),
            banned=str(record.get("b", "0")) not in ("", "0"),
            crawler=str(record.get("c", "0")) not in ("", "0"),
        )


def ban_key(ip: str, ipv6_prefix: int = 64) -> str | None:
    """Return the key `ip` is counted and banned under, or None when it is not an address."""
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/{ipv6_prefix}", strict=False))
    return str(address)


def key_network(key: str) -> Network:
    """The network a ban key covers (an address is its /32 or /128)."""
    return ipaddress.ip_network(key, strict=False)


def parse_networks(items: Iterable[str]) -> list[Network]:
    """Parse CIDRs and addresses, skipping blanks and comments; a bad line is logged and skipped."""
    networks = []
    for item in items:
        item = item.split("#", 1)[0].strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("ignoring %r: not an address range", item)
    return networks


class Protection:
    """The address ranges that are never banned: the allowlist, crawlers and Cloudflare."""

    def __init__(
        self,
        allowlist: Iterable[str] = (),
        crawlers: Iterable[str] = (),
        cloudflare: Iterable[str] = (),
    ) -> None:
        self._groups: dict[str, list[Network]] = {}
        self.set("allowlist", allowlist)
        self.set("crawlers", crawlers)
        self.set("cloudflare", cloudflare)

    def set(self, group: str, ranges: Iterable[str]) -> None:
        """Replace one group's ranges."""
        self._groups[group] = parse_networks(ranges)

    def size(self, group: str) -> int:
        return len(self._groups.get(group, ()))

    def covering(self, key: str) -> str | None:
        """Return the group whose ranges overlap `key`, or None."""
        try:
            network = key_network(key)
        except ValueError:
            return None
        for group, ranges in self._groups.items():
            for protected in ranges:
                if protected.version == network.version and protected.overlaps(network):
                    return group
        return None


Resolver = Callable[[str], list[str]]


def _reverse(ip: str) -> list[str]:
    name, aliases, _ = socket.gethostbyaddr(ip)
    return [name, *aliases]


def _forward(name: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(name, None)]


class CrawlerVerifier:
    """
    Confirms a self-declared search crawler by reverse DNS plus forward confirmation.

    The address's PTR name must end in one of `suffixes` and resolve back to
    the address, Google's and Bing's documented check. Results are cached for
    `ttl` seconds; a lookup failure is a "no".
    """

    def __init__(
        self,
        suffixes: Iterable[str],
        *,
        reverse: Resolver = _reverse,
        forward: Resolver = _forward,
        ttl: float = 86400,
        clock: Callable[[], float] = time.time,
        max_entries: int = 10_000,
    ) -> None:
        self.suffixes = tuple(s.lower().strip(".") for s in suffixes)
        self._reverse = reverse
        self._forward = forward
        self._ttl = ttl
        self._clock = clock
        self._max = max_entries
        self._cache: OrderedDict[str, tuple[bool, float]] = OrderedDict()

    def _check(self, ip: str) -> bool:
        try:
            names = self._reverse(ip)
        except OSError:
            return False
        for name in names:
            host = name.lower().rstrip(".")
            if not any(host == s or host.endswith("." + s) for s in self.suffixes):
                continue
            try:
                addresses = self._forward(host)
            except OSError:
                continue
            if any(ipaddress.ip_address(a) == ipaddress.ip_address(ip) for a in addresses):
                return True
        return False

    def __call__(self, ip: str) -> bool:
        now = self._clock()
        cached = self._cache.get(ip)
        if cached and cached[1] > now:
            return cached[0]
        verified = self._check(ip)
        self._cache[ip] = (verified, now + self._ttl)
        self._cache.move_to_end(ip)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return verified


class _Rule:
    """A compiled `BanRule`."""

    def __init__(self, name: str, rule: BanRule) -> None:
        self.name = name
        self.rule = rule
        self.paths = [re.compile(p, re.IGNORECASE) for p in rule.paths]

    def matches(self, hit: Hit) -> bool:
        if self.rule.status is not None and hit.status != self.rule.status:
            return False
        if self.rule.reject is not None and hit.reject != self.rule.reject:
            return False
        if self.paths and not any(p.search(hit.uri) for p in self.paths):
            return False
        return True


class Detector:
    """
    Counts each client's requests against the rules and bans on a strike.

    Parameters
    ----------
    config : Bans
        ``guard.yaml``'s ``bans`` section.
    store : Store
        Where strikes and bans are kept.
    protection : Protection, optional
        Ranges never banned.
    crawler_user_agent : str, optional
        A regex for user agents that claim to be a search crawler.
    verify_crawler : callable, optional
        ``ip -> bool``, asked only when a client that claims to be a crawler
        would be banned.
    """

    def __init__(
        self,
        config: Bans,
        store: Store,
        *,
        protection: Protection | None = None,
        crawler_user_agent: str | None = None,
        verify_crawler: Callable[[str], bool] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.protection = protection or Protection(allowlist=config.allowlist_cidrs)
        self._crawler_ua = re.compile(crawler_user_agent) if crawler_user_agent else None
        self._verify_crawler = verify_crawler
        rules = [_Rule(name, rule) for name, rule in config.rules.items()]
        self._rules = [rule for rule in rules if not rule.rule.while_banned]
        self._banned_rules = [rule for rule in rules if rule.rule.while_banned]
        # key -> rule name -> the times of its latest matching requests (at most `count`).
        self._windows: OrderedDict[str, dict[str, deque[float]]] = OrderedDict()
        self._banned: dict[str, float] = {}
        self.reload()

    def reload(self, now: float | None = None) -> None:
        """Re-read the active bans from the store (after a command changed them)."""
        now = time.time() if now is None else now
        self._banned = {ban.key: ban.expires for ban in self.store.active_bans(now)}

    def is_banned(self, key: str, now: float) -> bool:
        expires = self._banned.get(key)
        return expires is not None and expires > now

    def observe(self, hit: Hit) -> Ban | None:
        """
        Count one request; return the ban it triggered, if any.

        Requests nginx let through as a verified crawler are not counted; a
        banned client's (nginx says so, or the ban is newer than nginx's
        reload) count only towards the ``while_banned`` rules.
        """
        if hit.crawler:
            return None
        key = ban_key(hit.ip, self.config.ipv6_prefix)
        if key is None:
            return None
        rules = self._banned_rules if hit.banned or self.is_banned(key, hit.time) else self._rules
        if not rules:
            return None
        windows = self._windows.get(key)
        if windows is None:
            windows = self._windows[key] = {}
            while len(self._windows) > MAX_TRACKED:
                self._windows.popitem(last=False)
        else:
            self._windows.move_to_end(key)
        for rule in rules:
            if not rule.matches(hit):
                continue
            times = windows.get(rule.name)
            if times is None:
                times = windows[rule.name] = deque(maxlen=rule.rule.count)
            times.append(hit.time)
            if len(times) == rule.rule.count and times[0] > hit.time - rule.rule.window:
                return self._strike(key, rule.name, hit)
        return None

    def _strike(self, key: str, rule: str, hit: Hit) -> Ban | None:
        self._windows.pop(key, None)
        protected = self.protection.covering(key)
        if protected:
            log.info("not banning %s for %s: in the %s ranges", key, rule, protected)
            return None
        if self._crawler_ua and self._verify_crawler and self._crawler_ua.search(hit.user_agent):
            if self._verify_crawler(hit.ip):
                log.info("not banning %s for %s: a verified crawler (%s)", key, rule, hit.user_agent[:80])
                return None
            log.info("%s claims to be a crawler (%s) but is not one", key, hit.user_agent[:80])
        return self.ban(key, rule, now=hit.time)

    def ban(self, key: str, rule: str, *, now: float | None = None, duration: float | None = None) -> Ban:
        """
        Ban `key` (an address is turned into its key) and record the strike.

        Without `duration` the ban escalates through ``bans.durations`` with
        the strikes `key` has had within ``bans.strike_memory``. Past
        ``bans.max_bans`` the ban that would expire soonest is lifted.
        """
        now = time.time() if now is None else now
        key = ban_key(key, self.config.ipv6_prefix) or str(key_network(key))
        min_level = self.config.rules[rule].min_level if rule in self.config.rules else 0
        strikes = self.store.strikes_since(key, now - self.config.strike_memory)
        level = min(max(strikes, min_level), len(self.config.durations) - 1)
        if duration is None:
            duration = self.config.durations[level]
        self.store.add_strike(key, rule, now)
        existing = self.store.get_ban(key, now)
        if existing is None:
            while self.store.count_active(now) >= self.config.max_bans:
                soonest = self.store.active_bans(now)[0]
                log.warning("bans.max_bans (%d) reached: lifting %s early", self.config.max_bans, soonest.key)
                self.store.remove_ban(soonest.key)
                self._banned.pop(soonest.key, None)
        expires = max(now + duration, existing.expires if existing else 0)
        ban = Ban(key=key, rule=rule, level=level, started=now, expires=expires)
        self.store.put_ban(ban)
        self._banned[key] = expires
        self._windows.pop(key, None)
        if math.isinf(expires):
            log.warning("blacklisted %s permanently (%s, strike %d)", key, rule, strikes + 1)
        else:
            log.warning("banned %s for %ds (%s, strike %d, level %d)", key, expires - now, rule, strikes + 1, level)
        return ban

    def unban(self, key: str) -> bool:
        key = ban_key(key, self.config.ipv6_prefix) or str(key_network(key))
        self._banned.pop(key, None)
        return self.store.remove_ban(key)

    def expire(self, now: float | None = None) -> list[Ban]:
        """
        Lift the bans that have expired, forget idle clients' windows and old strikes.

        Returns the bans lifted.
        """
        now = time.time() if now is None else now
        lifted = self.store.expire(now)
        for ban in lifted:
            self._banned.pop(ban.key, None)
            log.info("ban on %s expired", ban.key)
        longest = max((r.rule.window for r in self._rules + self._banned_rules), default=0)
        for key in [k for k, w in self._windows.items() if all(not t or t[-1] < now - longest for t in w.values())]:
            del self._windows[key]
        self.store.prune(now, self.config.strike_memory)
        return lifted
