"""
``GET /addresses/search`` over HTTP, and the pieces behind it, with no PostgreSQL.

Covers the 3/64-character and charset bounds, ``limit`` bounds, that a 422 never
echoes the query, ``%``/``_``/``\\`` as literals, ``200 []``, the cache and its
``Cache-Control``, the per-client 429 with ``Retry-After``, a database failure
as 503, query parsing and the camelCase contract. The SQL itself runs against
hpdbldn in ``test_address_search_postgres.py``.
"""

import asyncio
import re
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from hpldn.backend.api import ratelimit
from hpldn.backend.api.main import app
from hpldn.backend.api.market import address_search
from hpldn.backend.api.market.search_query import (
    InvalidQuery,
    escape_like,
    normalise_query,
    validate_query,
)
from hpldn.backend.api.schemas import AddressOption

TYPES_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "data" / "types.ts"

HOME = AddressOption(postnumber="18", poststreet="BARCLAY ROAD", posttown="CROYDON", postcode="CR0 1JN")


class FakeSearch:
    """Stands in for `address_search.search`, recording what reached the database."""

    def __init__(self, options=None, error: Exception | None = None, path: str = "prefix"):
        self.calls: list[tuple[str, int]] = []
        self.options = [HOME] if options is None else options
        self.error = error
        self.path = path

    async def __call__(self, session, normalised, limit):
        self.calls.append((normalised, limit))
        if self.error is not None:
            raise self.error
        return address_search.SearchResult(list(self.options[:limit]), self.path)


@pytest.fixture
def fake_search(monkeypatch):
    fake = FakeSearch()
    monkeypatch.setattr(address_search, "search", fake)
    return fake


@pytest.fixture(autouse=True)
def fresh_state():
    """Give every test an empty cache, limiter and metrics."""
    cache = address_search.SearchCache()
    limiter = ratelimit.ClientLimiter(rate_per_s=1000.0, burst=1000, max_concurrent=100)
    metrics = address_search.SearchMetrics()
    app.dependency_overrides[address_search.get_search_cache] = lambda: cache
    app.dependency_overrides[ratelimit.get_search_limiter] = lambda: limiter
    app.dependency_overrides[address_search.get_search_metrics] = lambda: metrics
    yield cache, limiter, metrics
    for dependency in (
        address_search.get_search_cache,
        ratelimit.get_search_limiter,
        address_search.get_search_metrics,
    ):
        app.dependency_overrides.pop(dependency, None)


def test_search_needs_the_api_key(client, fake_search):
    assert client.get("/addresses/search", params={"q": "barclay"}).status_code == 401
    assert fake_search.calls == []


def test_search_returns_address_options_best_first_with_cache_control(client, auth, fake_search):
    response = client.get("/addresses/search", params={"q": " 18  Barclay Rd, ", "limit": "8"}, headers=auth)
    assert response.status_code == 200
    assert response.json() == [
        {"postnumber": "18", "poststreet": "BARCLAY ROAD", "posttown": "CROYDON", "postcode": "CR0 1JN",
         "unit": None, "streetHint": None}
    ]
    assert response.headers["cache-control"] == "public, max-age=60"
    assert fake_search.calls == [("18 BARCLAY RD", 8)]


def test_search_defaults_to_eight(client, auth, fake_search):
    client.get("/addresses/search", params={"q": "barclay"}, headers=auth)
    assert fake_search.calls == [("BARCLAY", 8)]


def test_nothing_found_is_an_empty_200_not_a_404(client, auth, monkeypatch):
    monkeypatch.setattr(address_search, "search", FakeSearch(options=[]))
    response = client.get("/addresses/search", params={"q": "zqxwv"}, headers=auth)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "q",
    [
        None,
        "",
        "   ",
        "zz",
        "a'b",  # 3 characters typed, 2 once the apostrophe is dropped
        "a" * 65,
        "a" * 5_000,  # past MAX_RAW_LENGTH; httpx refuses a 100k URL itself
        "barclay%",
        "barclay_road",
        "barclay\\road",
        "<script>",
        'barclay "road"',
        "barclay;drop",
        "barclay\x00road",
    ],
    ids=lambda q: repr(q)[:24],
)
def test_search_rejects_a_query_outside_the_bounds_before_any_sql(client, auth, fake_search, q):
    params = {} if q is None else {"q": q}
    response = client.get("/addresses/search", params=params, headers=auth)
    assert response.status_code == 422
    assert fake_search.calls == []
    body = response.text
    if q and q.strip():
        # FastAPI's own 422 would repeat the input; ours never does.
        assert q.strip()[:20] not in body
    assert isinstance(response.json()["detail"], str)


def test_sixty_four_characters_is_accepted(client, auth, fake_search):
    q = ("barclay road " * 5)[:64]
    assert client.get("/addresses/search", params={"q": q}, headers=auth).status_code == 200


@pytest.mark.parametrize("limit", ["0", "11", "-1", "eight", "8.5", "1e3"])
def test_search_rejects_a_limit_outside_one_to_ten(client, auth, fake_search, limit):
    response = client.get("/addresses/search", params={"q": "barclay", "limit": limit}, headers=auth)
    assert response.status_code == 422
    assert fake_search.calls == []


@pytest.mark.parametrize("limit", ["1", "10"])
def test_search_accepts_the_limit_bounds(client, auth, fake_search, limit):
    assert client.get("/addresses/search", params={"q": "barclay", "limit": limit}, headers=auth).status_code == 200
    assert fake_search.calls == [("BARCLAY", int(limit))]


def test_a_repeated_search_is_answered_from_the_cache(client, auth, fake_search, fresh_state):
    cache, _, metrics = fresh_state
    for q in ("Barclay Road", "barclay road", "BARCLAY  ROAD "):
        assert client.get("/addresses/search", params={"q": q}, headers=auth).status_code == 200
    assert fake_search.calls == [("BARCLAY ROAD", 8)]
    assert (cache.hits, cache.misses) == (2, 1)
    assert metrics.paths == {"prefix": 1, "cache": 2}


def test_a_timed_out_search_is_answered_but_not_cached(client, auth, monkeypatch, fresh_state):
    cache, _, _ = fresh_state
    fake = FakeSearch(path="timeout")
    monkeypatch.setattr(address_search, "search", fake)
    for _ in range(2):
        assert client.get("/addresses/search", params={"q": "barclay"}, headers=auth).status_code == 200
    assert len(fake.calls) == 2
    assert len(cache) == 0


def test_a_database_failure_is_a_503_with_a_sentence(client, auth, monkeypatch, fresh_state):
    _, _, metrics = fresh_state
    error = DBAPIError("SELECT", {}, Exception("connection refused"))
    monkeypatch.setattr(address_search, "search", FakeSearch(error=error))
    response = client.get("/addresses/search", params={"q": "barclay"}, headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "Address search is not available right now. Try again in a moment."}
    assert metrics.errors == 1


def test_a_client_searching_faster_than_typing_gets_429_with_retry_after(client, auth, fake_search):
    limiter = ratelimit.ClientLimiter(rate_per_s=0.5, burst=3, max_concurrent=2)
    app.dependency_overrides[ratelimit.get_search_limiter] = lambda: limiter
    statuses = [
        client.get("/addresses/search", params={"q": f"barclay {i}a"}, headers=auth).status_code
        for i in range(5)
    ]
    assert statuses == [200, 200, 200, 429, 429]
    response = client.get("/addresses/search", params={"q": "barclay 9a"}, headers=auth)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    assert len(fake_search.calls) == 3
    assert limiter.rejected == 3


def test_the_metrics_route_reports_aggregates_and_never_the_query(client, auth, fake_search):
    client.get("/addresses/search", params={"q": "barclay road"}, headers=auth)
    client.get("/addresses/search", params={"q": "zz"}, headers=auth)
    response = client.get("/addresses/search/metrics", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["requests"] == 1 and body["invalid"] == 1
    assert body["emptyRatio"] == 0.0
    assert "BARCLAY" not in response.text.upper()
    assert client.get("/addresses/search/metrics").status_code == 401


# --- the limiter on its own ---------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_token_bucket_refills_at_its_rate():
    clock = Clock()
    limiter = ratelimit.ClientLimiter(rate_per_s=2.0, burst=2, max_concurrent=5, clock=clock)

    async def attempt():
        try:
            async with limiter.acquire("1.2.3.4"):
                return 200
        except Exception as exc:  # HTTPException
            return exc.status_code

    async def run():
        results = [await attempt() for _ in range(3)]
        clock.now += 0.5  # one token back
        results.append(await attempt())
        results.append(await attempt())
        return results

    assert asyncio.run(run()) == [200, 200, 429, 200, 429]


def test_the_limiter_caps_one_clients_requests_in_flight():
    limiter = ratelimit.ClientLimiter(rate_per_s=100.0, burst=100, max_concurrent=2, clock=Clock())

    async def run():
        async with limiter.acquire("a"):
            async with limiter.acquire("a"):
                with pytest.raises(Exception) as third:
                    async with limiter.acquire("a"):
                        pass
                async with limiter.acquire("b"):  # another client is unaffected
                    pass
        async with limiter.acquire("a"):  # released again
            pass
        return third.value.status_code

    assert asyncio.run(run()) == 429


def test_the_limiter_forgets_the_least_recent_clients_beyond_its_bound():
    limiter = ratelimit.ClientLimiter(rate_per_s=1.0, burst=1, max_concurrent=1, max_clients=3, clock=Clock())

    async def run():
        for key in "abcde":
            async with limiter.acquire(key):
                pass

    asyncio.run(run())
    assert list(limiter._buckets) == ["c", "d", "e"]


class FakeRequest:
    def __init__(self, peer, headers=None):
        self.client = type("Client", (), {"host": peer})()
        self.headers = headers or {}


def test_client_key_trusts_x_real_ip_only_from_our_own_proxy():
    assert ratelimit.client_key(FakeRequest("127.0.0.1", {"x-real-ip": "81.2.69.160"})) == "81.2.69.160"
    assert ratelimit.client_key(FakeRequest("203.0.113.9", {"x-real-ip": "81.2.69.160"})) == "203.0.113.9"
    assert ratelimit.client_key(FakeRequest("127.0.0.1")) == "127.0.0.1"


# --- the cache on its own -----------------------------------------------------


def test_the_cache_expires_entries_and_evicts_the_least_recently_used():
    clock = Clock()
    cache = address_search.SearchCache(max_entries=2, ttl_s=600, clock=clock)
    cache.put(("A", 8), [HOME])
    cache.put(("B", 8), [])
    assert cache.get(("A", 8)) == [HOME]  # A is now the most recent
    cache.put(("C", 8), [])
    assert cache.get(("B", 8)) is None
    assert cache.get(("A", 8)) == [HOME]
    clock.now += 600
    assert cache.get(("A", 8)) is None
    assert len(cache) == 1


# --- query validation and parsing ---------------------------------------------


def test_normalise_query_matches_hpldn_searchnorm():
    assert normalise_query("St Mary's Court, Flat 3") == "ST MARYS COURT FLAT 3"
    assert normalise_query("20-22 High St.") == "20 22 HIGH ST"
    assert normalise_query("Ｆｌａｔ １/２") == "FLAT 1 2"  # NFKC folds full-width forms
    assert normalise_query("O’Neill  Road") == "ONEILL ROAD"


def test_validate_query_messages_never_quote_the_input():
    for raw in ("zz", "barclay%", "x" * 65):
        with pytest.raises(InvalidQuery) as caught:
            validate_query(raw)
        assert raw not in str(caught.value)


def test_trailing_whitespace_does_not_change_the_query():
    assert validate_query("flat 1 barclay road 18 ") == validate_query("flat 1 barclay road 18")


def test_escape_like_makes_wildcards_literal():
    assert escape_like("100%_a\\b") == "100\\%\\_a\\\\b"


@pytest.mark.parametrize(
    "typed,numbers,street,postcode",
    [
        ("BARCLAY R", (), "BARCLAY R", None),
        ("17 BARCLAY ROAD", ("17",), "BARCLAY ROAD", None),
        ("FLAT 3 17 BARCLAY ROAD CR0", ("3", "17"), "BARCLAY ROAD", "CR0"),
        ("18 BARCLAY ROAD CR0 1J", ("18",), "BARCLAY ROAD", "CR0 1J"),
        ("BARCLAY ROAD CR01JN", (), "BARCLAY ROAD", "CR0 1JN"),
        ("SW11", (), None, "SW11"),
        ("SW11 1A", (), None, "SW11 1A"),
        ("SW111AB", (), None, "SW11 1AB"),
        ("E17", (), None, "E17"),
        ("123", ("123",), None, None),
        ("17 B", ("17",), "B", None),
    ],
)
def test_parse_query_splits_number_street_and_postcode(typed, numbers, street, postcode):
    parsed = address_search.parse_query(typed)
    assert (parsed.numbers, parsed.street, parsed.postcode) == (numbers, street, postcode)


def test_the_prefix_statement_binds_every_typed_value():
    parsed = address_search.parse_query("17 BARCLAY ROAD CR0")
    sql, params = address_search.prefix_statement(parsed, 8)
    assert "BARCLAY" not in sql and "17" not in sql and "CR0" not in sql
    assert params == {"number_0": "17", "limit": 8, "street": "BARCLAY ROAD%", "postcode": "CR0%"}
    assert sql.index("ORDER BY name_text") > 0
    assert address_search.prefix_statement(address_search.parse_query("123"), 8) is None


def test_the_route_is_in_the_openapi_contract(client):
    # The contract is generated, though not served (no HPLDN_API_DOCS in the tests).
    assert "/addresses/search" in app.openapi()["paths"]


def _interface_keys(source: str, name: str) -> set[str]:
    block = re.search(r"export interface " + name + r"(?: extends \w+)? \{(.*?)\n\}", source, re.S)
    assert block, f"interface {name} not found in types.ts"
    return set(re.findall(r"^\s+(\w+)\??:", block.group(1), re.M))


def test_search_results_match_the_frontend_address_option(client, auth, fake_search):
    body = client.get("/addresses/search", params={"q": "barclay"}, headers=auth).json()
    source = TYPES_TS.read_text(encoding="utf-8")
    # A suggestion is an AddressOption plus the display-only streetHint.
    assert set(body[0]) == _interface_keys(source, "AddressOption") | _interface_keys(source, "AddressSuggestion")
