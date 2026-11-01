"""
Where the bans are enforced: nginx's ``banned.conf`` and the Cloudflare IP list.

Each sink is handed the full set of active bans on every tick and works out
for itself whether anything changed. Writes are debounced (``NginxSink`` at most
every 10 s, so a burst of bans is one nginx reload; ``CloudflareSink`` every
``cloudflare.sync_debounce``), and a failed write is retried on a later tick
from the same full set, so neither ever drifts for long.
"""

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from hpldn.backend.guard.cloudflare import CloudflareClient, CloudflareError, list_items
from hpldn.backend.guard.crawlers import geo_lines, write_atomic
from hpldn.backend.guard.store import Ban

log = logging.getLogger("hpldn.guard")

NGINX_MIN_INTERVAL_S = 10.0
# The longest a failing sink waits between attempts.
MAX_BACKOFF_S = 300.0


class _Debounced:
    """Sends the latest wanted state at most every `interval` seconds, backing off on failure."""

    def __init__(self, interval: float, clock: Callable[[], float]) -> None:
        self.interval = interval
        self._clock = clock
        self._sent: object = None
        self._last_attempt = float("-inf")
        self._wait = interval
        self.failures = 0

    def _state(self, bans: list[Ban]) -> object:
        raise NotImplementedError

    def _send(self, bans: list[Ban]) -> None:
        raise NotImplementedError

    def offer(self, bans: Iterable[Ban]) -> bool:
        """Send `bans` if they differ from the last state sent and the interval has passed; True when sent."""
        bans = sorted(bans, key=lambda b: b.key)
        state = self._state(bans)
        if state == self._sent:
            return False
        now = self._clock()
        if now - self._last_attempt < self._wait:
            return False
        self._last_attempt = now
        try:
            self._send(bans)
        except (OSError, CloudflareError) as exc:
            self.failures += 1
            self._wait = min(MAX_BACKOFF_S, self.interval * 2**self.failures)
            log.error("%s failed (%s); retrying in %ds", type(self).__name__, exc, self._wait)
            return False
        self._sent = state
        self.failures = 0
        self._wait = self.interval
        return True


class NginxSink(_Debounced):
    """Rewrites the ``geo $hpldn_banned`` include, which ``hpldn-nginx-reload.path`` turns into a reload."""

    def __init__(
        self,
        path: Path,
        *,
        min_interval: float = NGINX_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(min_interval, clock)
        self.path = Path(path)

    def _state(self, bans: list[Ban]) -> object:
        # The expiry is not in the file, so a ban extended is not a rewrite.
        return tuple(b.key for b in bans)

    def _send(self, bans: list[Ban]) -> None:
        write_atomic(self.path, geo_lines((b.key for b in bans), "Banned clients, written by hpldn-guard"))
        log.info("nginx: %d banned in %s", len(bans), self.path)


class CloudflareSink(_Debounced):
    """
    Replaces the Cloudflare IP list's items with the current bans.

    With `dry_run` the request is logged and not sent, and no client is needed.
    """

    def __init__(
        self,
        client: CloudflareClient | None,
        list_name: str,
        *,
        debounce: float,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(debounce, clock)
        if client is None and not dry_run:
            raise ValueError("a CloudflareSink needs a client unless dry_run")
        self.client = client
        self.list_name = list_name
        self.dry_run = dry_run
        self._list_id: str | None = None

    def _state(self, bans: list[Ban]) -> object:
        return tuple((item["ip"], item["comment"]) for item in list_items(bans))

    def _send(self, bans: list[Ban]) -> None:
        items = list_items(bans)
        if self.dry_run:
            log.info(
                "cloudflare (dry run): would PUT %d items to list %s: %s",
                len(items),
                self.list_name,
                ", ".join(item["ip"] for item in items[:20]) + (" ..." if len(items) > 20 else ""),
            )
            return
        if self._list_id is None:
            found = self.client.find_list(self.list_name)
            if found is None:
                raise CloudflareError(f"no IP list named {self.list_name}; run hpldn-guard cloudflare-setup")
            self._list_id = found["id"]
        try:
            operation = self.client.replace_items(self._list_id, items)
        except CloudflareError:
            # The list may have been recreated; look it up again next time.
            self._list_id = None
            raise
        log.info("cloudflare: %d items in list %s (operation %s)", len(items), self.list_name, operation)
