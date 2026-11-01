"""
The guard's memory: active bans and the strike history, in SQLite.

``/var/lib/hpldn-guard/guard.db`` on the host. The daemon and the
``hpldn-guard ban/unban/list`` commands open it together (WAL mode), and the
daemon notices the commands' changes through ``PRAGMA data_version``. Strikes
are kept for ``bans.strike_memory`` (30 days) and then pruned, as are bans
once they expire, so nothing about a client outlives that.
"""

import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bans (
    key     TEXT PRIMARY KEY,
    rule    TEXT NOT NULL,
    level   INTEGER NOT NULL,
    started REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS bans_expires ON bans (expires);
CREATE TABLE IF NOT EXISTS strikes (
    key  TEXT NOT NULL,
    rule TEXT NOT NULL,
    at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS strikes_key_at ON strikes (key, at);
CREATE INDEX IF NOT EXISTS strikes_at ON strikes (at);
"""


@dataclass(frozen=True)
class Ban:
    """
    One banned address or IPv6 prefix.

    Attributes
    ----------
    key : str
        An address (``203.0.113.7``) or a network (``2001:db8:1:2::/64``), as
        nginx's ``geo`` and a Cloudflare IP list both accept it.
    rule : str
        The rule that fired, or ``manual``.
    level : int
        The step of ``bans.durations`` it was given.
    started, expires : float
        Unix seconds; `expires` is ``math.inf`` for a permanent ban (the
        blacklist), which only ``hpldn-guard unban`` lifts.
    """

    key: str
    rule: str
    level: int
    started: float
    expires: float

    @property
    def permanent(self) -> bool:
        return math.isinf(self.expires)

    def until(self, utc: bool = False) -> str:
        """When the ban lifts, for people: a local (or UTC) time, or ``never (blacklisted)``."""
        if self.permanent:
            return "never (blacklisted)"
        if utc:
            return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(self.expires))
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.expires))

    def left(self, now: float) -> str:
        """The time left, as ``12h05m left``, or ``blacklisted``."""
        if self.permanent:
            return "blacklisted"
        seconds = max(0, int(self.expires - now))
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m left"


class Store:
    """
    Bans and strikes in one SQLite file (``":memory:"`` for tests).

    Every statement commits at once (autocommit), so a command run beside the
    daemon never waits on an open transaction.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA busy_timeout = 10000")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def data_version(self) -> int:
        """A number that changes whenever another connection commits to the file."""
        return int(self._db.execute("PRAGMA data_version").fetchone()[0])

    # Strikes

    def add_strike(self, key: str, rule: str, at: float) -> None:
        self._db.execute("INSERT INTO strikes (key, rule, at) VALUES (?, ?, ?)", (key, rule, at))

    def strikes_since(self, key: str, since: float) -> int:
        """How many strikes `key` has had since `since`."""
        row = self._db.execute("SELECT count(*) FROM strikes WHERE key = ? AND at >= ?", (key, since)).fetchone()
        return int(row[0])

    def forget_strikes(self, key: str) -> int:
        return self._db.execute("DELETE FROM strikes WHERE key = ?", (key,)).rowcount

    # Bans

    def put_ban(self, ban: Ban) -> None:
        """Record `ban`, replacing any ban of the same key."""
        self._db.execute(
            "INSERT OR REPLACE INTO bans (key, rule, level, started, expires) VALUES (?, ?, ?, ?, ?)",
            (ban.key, ban.rule, ban.level, ban.started, ban.expires),
        )

    def remove_ban(self, key: str) -> bool:
        return self._db.execute("DELETE FROM bans WHERE key = ?", (key,)).rowcount > 0

    def get_ban(self, key: str, now: float) -> Ban | None:
        row = self._db.execute("SELECT * FROM bans WHERE key = ? AND expires > ?", (key, now)).fetchone()
        return Ban(**row) if row else None

    def active_bans(self, now: float) -> list[Ban]:
        """Every unexpired ban, soonest to expire first."""
        rows = self._db.execute("SELECT * FROM bans WHERE expires > ? ORDER BY expires, key", (now,))
        return [Ban(**row) for row in rows]

    def count_active(self, now: float) -> int:
        return int(self._db.execute("SELECT count(*) FROM bans WHERE expires > ?", (now,)).fetchone()[0])

    def expire(self, now: float) -> list[Ban]:
        """Delete and return the bans that have expired by `now`."""
        rows = self._db.execute("SELECT * FROM bans WHERE expires <= ?", (now,)).fetchall()
        if rows:
            self._db.execute("DELETE FROM bans WHERE expires <= ?", (now,))
        return [Ban(**row) for row in rows]

    def prune(self, now: float, keep_s: float) -> int:
        """Delete strikes older than `keep_s` seconds; returns how many."""
        return self._db.execute("DELETE FROM strikes WHERE at < ?", (now - keep_s,)).rowcount
