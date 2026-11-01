"""
The ``hpldn-guard`` daemon: reads nginx's guard log, bans, and keeps the sinks in step.

nginx sends one JSON line per request (``log_format hpldn_guard``) as a syslog
datagram to the unix socket ``/run/hpldn-guard/log.sock``. The daemon feeds
each to the `Detector`, and on every tick (about once a second) offers the
active bans to nginx's ``banned.conf`` and the Cloudflare list. Once a minute
it lifts expired bans, prunes old strikes, and reloads the crawler and
Cloudflare ranges if their files changed. Changes made by the CLI
(``ban``/``unban``) are noticed through SQLite's ``data_version``.

Paths default to the host's and can be moved for a local run with
``HPLDN_GUARD_STATE_DIR``, ``HPLDN_GUARD_SOCKET`` and
``HPLDN_GUARD_CLOUDFLARE_DIR``.
"""

import grp
import json
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from hpldn.backend.common.secrets import SecretError
from hpldn.backend.guard.cloudflare import CloudflareClient, CloudflareError, api_token
from hpldn.backend.guard.config import GuardConfig
from hpldn.backend.guard.crawlers import CrawlerRanges
from hpldn.backend.guard.detector import CrawlerVerifier, Detector, Hit, Protection
from hpldn.backend.guard.sinks import CloudflareSink, NginxSink
from hpldn.backend.guard.store import Store

log = logging.getLogger("hpldn.guard")

TICK_S = 1.0
HOUSEKEEPING_S = 60.0
MAX_DATAGRAM = 65_535
# nginx's workers run as this group and must be able to send to the socket.
NGINX_GROUP = "www-data"


@dataclass(frozen=True)
class Paths:
    """Where the guard keeps its state and finds its inputs."""

    state_dir: Path
    socket: Path
    cloudflare_dir: Path

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(
            state_dir=Path(os.environ.get("HPLDN_GUARD_STATE_DIR", "/var/lib/hpldn-guard")),
            socket=Path(os.environ.get("HPLDN_GUARD_SOCKET", "/run/hpldn-guard/log.sock")),
            cloudflare_dir=Path(os.environ.get("HPLDN_GUARD_CLOUDFLARE_DIR", "/etc/hpldn/cloudflare")),
        )

    @property
    def database(self) -> Path:
        return self.state_dir / "guard.db"

    @property
    def banned_conf(self) -> Path:
        return self.state_dir / "nginx" / "banned.conf"

    @property
    def crawlers_conf(self) -> Path:
        return self.state_dir / "nginx" / "crawlers.conf"

    @property
    def crawlers_cache(self) -> Path:
        return self.state_dir / "crawlers.json"

    @property
    def cloudflare_files(self) -> list[Path]:
        """Cloudflare's published ranges, kept by ``hpldn-firewall``."""
        return [self.cloudflare_dir / "ips-v4", self.cloudflare_dir / "ips-v6"]

    def crawler_ranges(self) -> CrawlerRanges:
        return CrawlerRanges(self.crawlers_cache, self.crawlers_conf)

    def cloudflare_ranges(self) -> list[str]:
        return [line for path in self.cloudflare_files for line in _read_lines(path)]


def parse_datagram(data: bytes) -> dict | None:
    """Return the JSON object in one syslog datagram from nginx, or None when there is none."""
    text = data.decode("utf-8", "replace")
    start = text.find("{")
    if start < 0:
        return None
    try:
        record = json.loads(text[start:].rstrip("\x00\r\n "))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


class Guard:
    """The daemon's state: detector, store, sinks, and the ranges it protects."""

    def __init__(self, config: GuardConfig, paths: Paths, *, cloudflare_client: CloudflareClient | None = None) -> None:
        self.config = config
        self.paths = paths
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(paths.database)
        self.protection = Protection(allowlist=config.bans.allowlist_cidrs)
        self.crawlers = paths.crawler_ranges()
        self.detector = Detector(
            config.bans,
            self.store,
            protection=self.protection,
            crawler_user_agent=config.crawlers.user_agent_pattern,
            verify_crawler=CrawlerVerifier(config.crawlers.rdns_suffixes),
        )
        self.nginx = NginxSink(paths.banned_conf)
        self.cloudflare = self._cloudflare_sink(cloudflare_client)
        self._data_version = self.store.data_version()
        self._mtimes: dict[Path, float | None] = {}
        self._last_housekeeping = float("-inf")
        self.handled = 0

    def _cloudflare_sink(self, client: CloudflareClient | None) -> CloudflareSink | None:
        settings = self.config.cloudflare
        if not settings.enabled:
            log.info("cloudflare sink off (cloudflare.enabled is false)")
            return None
        if client is None and not settings.dry_run:
            try:
                client = CloudflareClient(api_token(), settings.account_id)
            except (CloudflareError, SecretError) as exc:
                log.error("cloudflare sink off: %s", exc)
                return None
        return CloudflareSink(client, settings.list_name, debounce=settings.sync_debounce, dry_run=settings.dry_run)

    def load_ranges(self, force: bool = False) -> None:
        """Reload the crawler and Cloudflare ranges whose files changed since the last load."""
        crawler_files = [self.paths.crawlers_cache]
        cloudflare_files = self.paths.cloudflare_files
        if force or any(self._changed(p) for p in crawler_files):
            self.protection.set("crawlers", self.crawlers.ranges(self.config.crawlers.range_urls))
            log.info("protecting %d crawler ranges", self.protection.size("crawlers"))
        if force or any(self._changed(p) for p in cloudflare_files):
            self.protection.set("cloudflare", self.paths.cloudflare_ranges())
            log.info("protecting %d Cloudflare ranges", self.protection.size("cloudflare"))
        for path in crawler_files + cloudflare_files:
            self._mtimes[path] = _mtime(path)

    def _changed(self, path: Path) -> bool:
        return _mtime(path) != self._mtimes.get(path)

    def refresh_crawlers_if_stale(self) -> None:
        """Fetch the crawler ranges when the cache is missing or older than ``crawlers.refresh_interval``."""
        age = self.crawlers.age()
        if age is not None and age < self.config.crawlers.refresh_interval:
            return
        try:
            self.crawlers.refresh(self.config.crawlers.range_urls)
        except OSError as exc:
            log.error("could not write the crawler ranges: %s", exc)

    def handle(self, data: bytes) -> None:
        """Count one datagram from nginx."""
        record = parse_datagram(data)
        if record is None:
            return
        self.handled += 1
        self.detector.observe(Hit.from_log(record, time.time()))

    def tick(self, now: float | None = None) -> None:
        """Pick up CLI changes, do the minute's housekeeping when due, and offer the bans to the sinks."""
        now = time.time() if now is None else now
        version = self.store.data_version()
        if version != self._data_version:
            self._data_version = version
            self.detector.reload(now)
        if now - self._last_housekeeping >= HOUSEKEEPING_S:
            self._last_housekeeping = now
            self.detector.expire(now)
            self.load_ranges()
        bans = self.store.active_bans(now)
        self.nginx.offer(bans)
        if self.cloudflare is not None:
            self.cloudflare.offer(bans)


def _bind(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(str(path))
    os.chmod(path, 0o660)
    try:
        os.chown(path, -1, grp.getgrnam(NGINX_GROUP).gr_gid)
    except (KeyError, PermissionError) as exc:
        log.warning("cannot give %s to group %s (%s); nginx may not be able to log to it", path, NGINX_GROUP, exc)
    sock.settimeout(TICK_S)
    return sock


def run(config: GuardConfig, paths: Paths) -> None:
    """Run the daemon until SIGTERM or SIGINT."""
    guard = Guard(config, paths)
    guard.refresh_crawlers_if_stale()
    guard.load_ranges(force=True)
    sock = _bind(paths.socket)
    stopping = False

    def _stop(_signum, _frame) -> None:
        # No logging here: the handler can interrupt a log write.
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info(
        "listening on %s; %d active bans; config %s",
        paths.socket,
        guard.store.count_active(time.time()),
        " + ".join(config.source) or "defaults",
    )
    next_tick = 0.0
    try:
        while not stopping:
            try:
                data = sock.recv(MAX_DATAGRAM)
            except TimeoutError:
                data = None
            except InterruptedError:
                continue
            if data:
                guard.handle(data)
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + TICK_S
                guard.tick()
    finally:
        log.info("stopping")
        sock.close()
        paths.socket.unlink(missing_ok=True)
        guard.store.close()
