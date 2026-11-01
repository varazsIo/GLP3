"""
Address suggestions for a partial address: ``GET /addresses/search``.

One search is one request and at most two indexed statements against
``production.address_search`` (built by migration 0021, see
`hpldn.backend.migrations.address_search`):

1. **Prefix.** The typed text is split into a house number, a street prefix
   and a postcode prefix (`parse_query`). The street prefix is matched on the
   ``name_text`` ``text_pattern_ops`` btree and the postcode prefix on the
   ``postcode`` one; the house number filters the rows the index returns.
   Measured on hpdbldn: ~0.03 ms.
2. **Trigram.** Only when the prefix statement returns fewer homes than asked
   for, and not when a typed house number already matched -- a building name ("henry house"), a typo ("barcley road") or a word
   out of order -- ``search_text %> :q`` on the ``gin_trgm_ops`` index fills
   the rest, best ``word_similarity`` first. Measured: 15-55 ms.

Both run under a 250 ms ``statement_timeout`` (the ``hpldn_api`` role's own is
5 s), so a pathological query is cut off rather than queued: a trigram
statement that times out returns the prefix results instead of a 500. A
prefix statement that times out is tried once more under
`COLD_RETRY_TIMEOUT_MS`: on pages not yet in memory (the first search after a
restart or an idle night) the index walk itself can pass 250 ms, and that
first visitor should get homes, not "search unavailable". The path then ends
in ``+retry``.

Ranking does not depend on trailing whitespace: "18" and "18 " search alike.
The query text itself is never logged -- it is an address someone is typing.
"""

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.market.search_query import (  # noqa: F401 -- re-exported for callers
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    InvalidQuery,
    escape_like,
    normalise_query,
    validate_query,
)
from hpldn.backend.api.schemas import AddressSuggestion

log = logging.getLogger("uvicorn.error")

STATEMENT_TIMEOUT_MS = 250
# The prefix statement's one retry, on pages not yet in memory (see above).
COLD_RETRY_TIMEOUT_MS = 1000

# Above pg_trgm's 0.6 default. Measured on hpdbldn for the typo "barcley road":
# 0.6 matches 7,139 homes (every ROAD) in 74 ms; 0.7 matches 123 in 9 ms and
# still ranks BARCLAY ROAD first. Stated so it does not move with a server default.
WORD_SIMILARITY_THRESHOLD = 0.7

# Tokens that name the flat rather than the street; never part of a street prefix.
FLAT_WORDS = frozenset({"FLAT", "FLT", "APARTMENT", "APT", "UNIT", "NO", "ROOM", "STUDIO"})

# A postcode as it is typed so far: an outward code, optionally with the start of
# the inward code. Applied to the normalised text, where "SW111AB" and "SW11 1AB"
# have both been spaced.
_POSTCODE_PREFIX = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?(?: \d[A-Z]{0,2})?$")
_COMPACT_FULL_POSTCODE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)(\d[A-Z]{2})$")
_OUTWARD = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
_INWARD_PREFIX = re.compile(r"^\d[A-Z]{0,2}$")


@dataclass(frozen=True)
class ParsedQuery:
    """
    A normalised query, split for the prefix statement.

    Attributes
    ----------
    text : str
        The whole normalised query, for the trigram statement.
    numbers : tuple of str
        House-number tokens ("17", "3B"), each matched as the start of a word.
    street : str or None
        The street or building name typed so far, matched as a prefix.
    postcode : str or None
        The postcode typed so far, matched as a prefix.
    names_flat : bool
        A flat word ("flat", "apartment") was typed before the numbers.
    """

    text: str
    numbers: tuple[str, ...]
    street: str | None
    postcode: str | None
    names_flat: bool = False

    @property
    def has_house_number(self) -> bool:
        """True when the query names a house number."""
        return bool(self.numbers)


def _trailing_postcode(tokens: list[str]) -> tuple[list[str], str | None]:
    """Split a postcode prefix off the end of `tokens`, if the last one or two form one."""
    if len(tokens) >= 2 and _OUTWARD.match(tokens[-2]) and _INWARD_PREFIX.match(tokens[-1]):
        return tokens[:-2], tokens[-2] + " " + tokens[-1]
    if tokens:
        compact = _COMPACT_FULL_POSTCODE.match(tokens[-1])
        if compact:
            return tokens[:-1], compact.group(1) + " " + compact.group(2)
        # A lone outward code only counts once a street precedes it ("barclay road
        # cr0"); on its own "E17" is a postcode, but "17" is a house number.
        if _OUTWARD.match(tokens[-1]) and (len(tokens) == 1 or any(t.isalpha() for t in tokens[:-1])):
            return tokens[:-1], tokens[-1]
    return tokens, None


def parse_query(normalised: str) -> ParsedQuery:
    """
    Split a normalised query into house number, street prefix and postcode prefix.

    ``"FLAT 3 17 BARCLAY ROAD CR0"`` -> numbers ``("3", "17")``, street
    ``"BARCLAY ROAD"``, postcode ``"CR0"``. Leading tokens that carry a digit or
    name a flat are the house number; a trailing postcode prefix is split off;
    what is left is the street.
    """
    tokens = normalised.split()
    if _POSTCODE_PREFIX.match(normalised):
        return ParsedQuery(normalised, (), None, normalised)
    compact = _COMPACT_FULL_POSTCODE.match(normalised)
    if compact:
        return ParsedQuery(normalised, (), None, compact.group(1) + " " + compact.group(2))

    numbers: list[str] = []
    names_flat = False
    index = 0
    while index < len(tokens) and (any(c.isdigit() for c in tokens[index]) or tokens[index] in FLAT_WORDS):
        if tokens[index] not in FLAT_WORDS:
            numbers.append(tokens[index])
        else:
            names_flat = True
        index += 1
    rest, postcode = _trailing_postcode(tokens[index:])
    street = " ".join(rest) or None
    return ParsedQuery(normalised, tuple(numbers), street, postcode, names_flat)


# `unit` since 0031: each flat is its own suggestion.
_COLUMNS = "address_id, postnumber, poststreet, posttown, postcode, street_hint, unit"

# The last number in the house number, for "2, 18, 170" rather than "170, 18, 2".
_NATURAL_NUMBER = r"NULLIF(substring(postnumber FROM '(\d+)\D*$'), '')::numeric NULLS LAST"
# Then a building's flats in number order, the building itself (no unit) first.
_NATURAL_UNIT = r"NULLIF(substring(unit FROM '(\d+)\D*$'), '')::numeric NULLS FIRST, unit NULLS FIRST"


def _number_filters(numbers: tuple[str, ...], names_flat: bool = False) -> tuple[str, dict]:
    """
    Return the WHERE fragment and parameters that keep homes with every house number.

    Each number must start a word of ``search_text`` ("17" matches 17, 17A and
    170). A lone number with no flat word is a house number, so it must start
    a word of the house number itself: "17 barclay road" is not padded with
    Flat 17 of number 5 (since 0031 the unit leads ``search_text``). Tokens
    are ``[A-Z0-9]+`` after normalisation, so they carry no regex
    metacharacters; they are still bound, never interpolated.
    """
    clauses, params = [], {}
    for i, number in enumerate(numbers[:3]):
        clauses.append(f"search_text ~ ('(^| )' || :number_{i})")
        params[f"number_{i}"] = number
    if len(numbers) == 1 and not names_flat:
        clauses.append("production.hpldn_searchnorm(postnumber) ~ ('(^| )' || :number_0)")
    return "".join(" AND " + c for c in clauses), params


def _exact_number_rank(numbers: tuple[str, ...]) -> str:
    """
    Return ORDER BY terms putting the typed house number's homes first, then an exact match.

    The last number typed is the house number ("flat 6 17 woodville road"),
    so homes whose number ends with it lead: "17 Woodville Road" and its
    flats before "Flat 17, 5 Woodville Road". Then an exact match of the
    first number before a longer one ("6" before "60").
    """
    if not numbers:
        return ""
    last = min(len(numbers), 3) - 1
    return (
        f"(production.hpldn_searchnorm(postnumber) ~ ('(^| )' || :number_{last} || '$')) DESC, "
        "(search_text ~ ('(^| )' || :number_0 || '( |$)')) DESC, "
    )


def prefix_statement(parsed: ParsedQuery, limit: int) -> tuple[str, dict] | None:
    """
    Return the anchored-prefix SQL and its parameters, or None when nothing anchors it.

    The ORDER BY leads with the indexed column, so PostgreSQL reads the btree in
    order and stops at `limit` (an incremental sort) instead of sorting every
    home on a long street.
    """
    if parsed.street is None and parsed.postcode is None:
        return None
    numbers_sql, params = _number_filters(parsed.numbers, parsed.names_flat)
    params["limit"] = limit
    where = []
    if parsed.street is not None:
        where.append(r"name_text LIKE :street ESCAPE '\'")
        params["street"] = escape_like(parsed.street) + "%"
    if parsed.postcode is not None:
        where.append(r"postcode LIKE :postcode ESCAPE '\'")
        params["postcode"] = escape_like(parsed.postcode) + "%"
    if parsed.street is not None:
        order = f"name_text, {_exact_number_rank(parsed.numbers)}postcode, {_NATURAL_NUMBER}, postnumber, {_NATURAL_UNIT}"
    else:
        order = f"postcode, {_exact_number_rank(parsed.numbers)}name_text, {_NATURAL_NUMBER}, postnumber, {_NATURAL_UNIT}"
    sql = (
        f"SELECT {_COLUMNS} FROM production.address_search "
        f"WHERE {' AND '.join(where)}{numbers_sql} "
        f"ORDER BY {order} LIMIT :limit"
    )
    return sql, params


TRIGRAM_SQL = (
    f"SELECT {_COLUMNS} FROM production.address_search "
    "WHERE search_text %> :q AND NOT (address_id = ANY(CAST(:exclude AS bigint[]))) "
    "ORDER BY word_similarity(:q, search_text) DESC, name_text, postcode, "
    f"{_NATURAL_NUMBER}, postnumber, {_NATURAL_UNIT} "
    "LIMIT :limit"
)


def _option(row) -> AddressSuggestion:
    """Convert a result row into the wire format ``POST /price`` accepts unchanged."""
    return AddressSuggestion(
        postnumber=str(row["postnumber"]).strip(),
        poststreet=(row["poststreet"] or "").strip() or None,
        posttown=str(row["posttown"] or "").strip(),
        postcode=row["postcode"],
        street_hint=(row.get("street_hint") or "").strip() or None,
        unit=(row.get("unit") or "").strip() or None,
    )


def _is_statement_timeout(exc: DBAPIError) -> bool:
    """True when `exc` is PostgreSQL cancelling a statement for ``statement_timeout``."""
    return getattr(exc.orig, "sqlstate", None) == "57014"


@dataclass(frozen=True)
class SearchResult:
    """
    The homes found and how: ``"prefix"``, ``"trigram"`` or ``"timeout"`` (prefix only).

    ``+retry`` is appended when the prefix statement needed its cold-cache retry.
    """

    options: list[AddressSuggestion]
    path: str


async def _set_local_limits(session: AsyncSession) -> None:
    """Bound this transaction's statements; ``SET LOCAL`` so nothing leaks to the pool."""
    await session.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
    await session.execute(
        text(f"SET LOCAL pg_trgm.word_similarity_threshold = {float(WORD_SIMILARITY_THRESHOLD)}")
    )


async def search(session: AsyncSession, normalised: str, limit: int) -> SearchResult:
    """
    Return up to `limit` priceable homes for a validated, normalised query.

    Raises
    ------
    DBAPIError
        For any database failure other than the trigram statement timing out.
    """
    parsed = parse_query(normalised)
    found: list[AddressSuggestion] = []
    ids: list[int] = []
    path = "prefix"

    retried = False

    async with session.begin():
        await _set_local_limits(session)
        statement = prefix_statement(parsed, limit)
        if statement is not None:
            sql, params = statement
            try:
                async with session.begin_nested():
                    rows = (await session.execute(text(sql), params)).mappings().all()
            except DBAPIError as exc:
                if not _is_statement_timeout(exc):
                    raise
                retried = True
                await session.execute(text(f"SET LOCAL statement_timeout = {int(COLD_RETRY_TIMEOUT_MS)}"))
                async with session.begin_nested():
                    rows = (await session.execute(text(sql), params)).mappings().all()
                await session.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
            found = [_option(row) for row in rows]
            ids = [int(row["address_id"]) for row in rows]

        # Only a query naming a street or building is worth a similarity search:
        # "123" shares trigrams with a quarter of London, and a postcode prefix
        # has already been answered exactly by its index.
        # Nor once a typed house number has matched: topping up "17 barclay road"
        # with 1 and 8 Barclay Road only buries the homes asked for.
        if len(found) < limit and parsed.street is not None and not (found and parsed.has_house_number):
            try:
                async with session.begin_nested():
                    rows = (
                        await session.execute(
                            text(TRIGRAM_SQL),
                            {"q": normalised, "exclude": ids, "limit": limit - len(found)},
                        )
                    ).mappings().all()
            except DBAPIError as exc:
                if not _is_statement_timeout(exc):
                    raise
                path = "timeout"
            else:
                if rows:
                    path = "trigram" if not found else "prefix+trigram"
                found.extend(_option(row) for row in rows)
    return SearchResult(found, path + "+retry" if retried else path)


class SearchCache:
    """
    A per-worker LRU of search results with a time-to-live.

    Address data only changes on a data refresh, and typing traffic sits in the
    head of the distribution ("SW1", "HIG", "CHU"), so a few thousand entries for
    ten minutes absorb most of it. The same shape as `market.cache.MarketCache`:
    a monotonic, injectable clock and a `clear` for tests.

    Parameters
    ----------
    max_entries : int
        Least recently used entries are evicted beyond this.
    ttl_s : float
        Lifetime of an entry in seconds.
    clock : callable, optional
        Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        max_entries: int = 4096,
        ttl_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._clock = clock
        self._entries: OrderedDict[tuple[str, int], tuple[float, list[AddressSuggestion]]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[str, int]) -> list[AddressSuggestion] | None:
        """Return the fresh entry for `key`, or None."""
        entry = self._entries.get(key)
        if entry is None or self._clock() - entry[0] >= self.ttl_s:
            if entry is not None:
                del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry[1]

    def put(self, key: tuple[str, int], options: list[AddressSuggestion]) -> None:
        """Store `options` under `key`, evicting the least recently used beyond the bound."""
        self._entries[key] = (self._clock(), options)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry and reset the counters."""
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)


_cache = SearchCache()


def get_search_cache() -> SearchCache:
    """Return this worker's search cache (FastAPI dependency)."""
    return _cache


class SearchMetrics:
    """
    Per-worker counters and a latency reservoir for ``GET /addresses/search``.

    Aggregates only -- never the query text. A rising ``empty_ratio`` is the
    signal that ``production.address_search`` has gone stale against
    ``production.london_houses``; ``p99_ms`` above `STATEMENT_TIMEOUT_MS` means
    the trigram path is being cut off.
    """

    RESERVOIR = 1024

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        """Reset every counter."""
        self.requests = 0
        self.empty = 0
        self.rejected_invalid = 0
        self.timeouts = 0
        self.errors = 0
        self.paths: dict[str, int] = {}
        self._durations_ms: list[float] = []
        self._next = 0

    def record(self, duration_ms: float, results: int, path: str) -> None:
        """Count one answered search."""
        self.requests += 1
        self.empty += results == 0
        self.timeouts += path.startswith("timeout")
        self.paths[path] = self.paths.get(path, 0) + 1
        if len(self._durations_ms) < self.RESERVOIR:
            self._durations_ms.append(duration_ms)
        else:
            self._durations_ms[self._next] = duration_ms
            self._next = (self._next + 1) % self.RESERVOIR

    def _percentile(self, share: float) -> float | None:
        if not self._durations_ms:
            return None
        ordered = sorted(self._durations_ms)
        return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 2)

    def snapshot(self, cache: "SearchCache", rate_limited: int) -> dict:
        """Return the figures as a JSON-ready dict."""
        lookups = cache.hits + cache.misses
        return {
            "requests": self.requests,
            "p50Ms": self._percentile(0.50),
            "p95Ms": self._percentile(0.95),
            "p99Ms": self._percentile(0.99),
            "emptyRatio": round(self.empty / self.requests, 4) if self.requests else None,
            "cacheHitRatio": round(cache.hits / lookups, 4) if lookups else None,
            "cacheEntries": len(cache),
            "rateLimited": rate_limited,
            "invalid": self.rejected_invalid,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "paths": dict(self.paths),
        }


_metrics = SearchMetrics()


def get_search_metrics() -> SearchMetrics:
    """Return this worker's search metrics (FastAPI dependency)."""
    return _metrics
