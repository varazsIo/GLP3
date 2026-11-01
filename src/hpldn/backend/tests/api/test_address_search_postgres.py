"""
``production.address_search`` and the search SQL against the real hpdbldn database.

Reads hpdbldn through the suite's `hpdbldn_url`/`hpdbldn_ro` fixtures (fails
when it is unreachable); every session is ``default_transaction_read_only``. Holds each
representative query -- a three-character prefix, a house number and street, a
postcode, a typo, a building name and a no-match -- to `LATENCY_BUDGET_MS` warm,
checks the plans use the trigram and ``text_pattern_ops`` indexes (the test that
would have caught the 1.9 s sequential scan), and that suggestions are homes
``GET /addresses`` lists, so ``POST /price`` accepts them.
"""

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hpldn.backend.api.market import address_search, pricing
from hpldn.backend.api.market.search_query import validate_query

LATENCY_BUDGET_MS = 50.0

REPRESENTATIVE = [
    "bar",
    "barclay r",
    "17 barclay road",
    "18 barclay road cr0",
    "barcley road",
    "henry house",
    "flat 3 high street",
    "church road",
    "sw11 1",
    "cr0 1jn",
    "E17",
    "zqxwv",
]


READ_ONLY = {"connect_timeout": 3, "options": "-c default_transaction_read_only=on"}


@pytest.fixture(scope="module")
def engine(hpdbldn_ro):
    with hpdbldn_ro.connect() as connection:
        connection.execute(text("SELECT 1 FROM production.address_search LIMIT 1"))
    return hpdbldn_ro


@pytest.fixture(scope="module")
def async_url(hpdbldn_url):
    return hpdbldn_url.set(drivername="postgresql+psycopg")


@pytest.fixture(scope="module")
def run_search(engine, async_url):
    """Return a function running `address_search.search` on a read-only async session."""
    async_engine = create_async_engine(
        async_url, connect_args={**READ_ONLY, "prepare_threshold": None}
    )
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    loop = asyncio.new_event_loop()

    def run(typed: str, limit: int = 8):
        async def once():
            async with maker() as session:
                started = time.perf_counter()
                result = await address_search.search(session, validate_query(typed), limit)
                return result, (time.perf_counter() - started) * 1000

        return loop.run_until_complete(once())

    yield run
    loop.run_until_complete(async_engine.dispose())
    loop.close()


@pytest.mark.parametrize("typed", REPRESENTATIVE)
def test_representative_searches_answer_within_budget(run_search, typed):
    run_search(typed)  # warm the pages this query touches
    result, ms = run_search(typed)
    assert ms < LATENCY_BUDGET_MS, f"{typed!r} took {ms:.1f} ms ({result.path})"
    assert len(result.options) <= 8
    assert result.path != "timeout"


def test_a_street_prefix_finds_that_street(run_search):
    result, _ = run_search("barclay r")
    # The register sometimes writes the street in the house-number column, with no
    # street; /addresses lists those rows as they are, and so does the search. A
    # street-less home in a one-street postcode is found by that street (0024).
    assert result.options
    assert all(
        (o.poststreet or o.street_hint or o.postnumber).upper().startswith("BARCLAY R")
        for o in result.options
    )


def test_a_street_less_home_is_found_by_its_postcodes_street(run_search):
    """
    232A, SW11 3AA had no street in the register; it is on Battersea Bridge Road.

    0024 found it by its postcode's street hint; since 0028 filled sold homes'
    streets from their Price Paid sales it carries the street itself. Either
    way the search finds it first, on that street.
    """
    result, _ = run_search("232a battersea bridge")
    first = result.options[0]
    assert (first.postnumber, first.postcode) == ("232A", "SW11 3AA")
    assert (first.poststreet or first.street_hint).upper() == "BATTERSEA BRIDGE ROAD"


def test_a_house_number_puts_that_number_first(run_search):
    result, _ = run_search("18 barclay road cr0")
    first = result.options[0]
    assert (first.postnumber, first.poststreet, first.postcode) == ("18", "BARCLAY ROAD", "CR0 1JN")


def test_a_matched_house_number_is_not_padded_with_other_numbers(run_search):
    result, _ = run_search("17 barclay road")
    assert result.path == "prefix"
    assert result.options and all(o.postnumber.upper().startswith("17") for o in result.options)


def test_a_typo_still_finds_the_street(run_search):
    result, _ = run_search("barcley road")
    assert result.path == "trigram"
    assert result.options and "BARCLAY ROAD" in (result.options[0].poststreet or "").upper()


def test_no_match_is_empty(run_search):
    result, _ = run_search("zqxwv")
    assert result.options == []


def test_trailing_space_does_not_change_the_answer(run_search):
    assert run_search("18 barclay road ")[0].options == run_search("18 barclay road")[0].options


def test_a_full_postcode_suggests_exactly_the_homes_get_addresses_lists(engine, run_search, async_url):
    result, _ = run_search("cr0 1jn", limit=10)

    async def listed():
        async_engine = create_async_engine(async_url, connect_args=READ_ONLY)
        try:
            async with async_sessionmaker(async_engine)() as session:
                return await pricing.fetch_addresses(session, "CR0 1JN")
        finally:
            await async_engine.dispose()

    expected = asyncio.run(listed())
    # Since 0031 each flat is its own home, so the postcode lists more than ten
    # and the two orders differ: every suggestion is a listed home.
    key = lambda o: (o.unit or "", o.postnumber.upper(), (o.poststreet or "").upper(), o.postcode)  # noqa: E731
    assert len(result.options) == min(10, len(expected))
    assert set(map(key, result.options)) <= set(map(key, expected))


def test_a_sold_home_without_a_certificate_is_suggested_and_listed(engine, run_search, async_url):
    """17 LICHFIELD ROAD, TW9 3JR sold in 2026 with no energy certificate (migration 0022)."""
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert revision is not None and revision >= "0022", f"needs migration 0022 (hpdbldn at {revision})"
    result, _ = run_search("17 lichfield road tw9")
    assert any(
        (o.postnumber, o.poststreet, o.postcode) == ("17", "LICHFIELD ROAD", "TW9 3JR") for o in result.options
    )

    async def listed():
        async_engine = create_async_engine(async_url, connect_args=READ_ONLY)
        try:
            async with async_sessionmaker(async_engine)() as session:
                return await pricing.fetch_addresses(session, "TW9 3JR")
        finally:
            await async_engine.dispose()

    assert ("17", "LICHFIELD ROAD") in {(o.postnumber, o.poststreet) for o in asyncio.run(listed())}


def test_like_wildcards_in_a_street_are_literal(engine):
    parsed = address_search.ParsedQuery("X", (), "BARCLAY%", None)
    sql, params = address_search.prefix_statement(parsed, 8)
    assert params["street"] == "BARCLAY\\%%"
    with engine.connect() as connection:
        rows = connection.execute(text(sql), params).all()
    assert rows == []  # "BARCLAY%" as a wildcard would match every Barclay street


@pytest.mark.parametrize(
    "index,sql,params",
    [
        (
            "idx_prod_address_search_text_trgm",
            address_search.TRIGRAM_SQL,
            {"q": "BARCLEY ROAD", "exclude": [], "limit": 8},
        ),
        ("idx_prod_address_search_name_prefix", *address_search.prefix_statement(address_search.parse_query("17 BARCLAY R"), 8)),
        ("idx_prod_address_search_postcode_prefix", *address_search.prefix_statement(address_search.parse_query("SW11 1"), 8)),
    ],
)
def test_the_search_statements_plan_on_their_indexes(engine, index, sql, params):
    with engine.connect() as connection:
        connection.execute(text(f"SET pg_trgm.word_similarity_threshold = {address_search.WORD_SIMILARITY_THRESHOLD}"))
        plan = "\n".join(row[0] for row in connection.execute(text("EXPLAIN " + sql), params))
    assert index in plan, plan
    assert "Seq Scan" not in plan, plan


def test_a_prefix_statement_slower_than_its_budget_is_tried_once_more(run_search, monkeypatch):
    # Stands in for cold pages: the prefix statement takes 300 ms, past the
    # 250 ms budget but inside the 1 s retry, so the visitor still gets homes.
    expected, _ = run_search("barclay r")
    original = address_search.prefix_statement

    def slow(parsed, limit):
        sql, params = original(parsed, limit)
        return f"SELECT s.* FROM ({sql}) s CROSS JOIN pg_sleep(0.3)", params

    monkeypatch.setattr(address_search, "prefix_statement", slow)
    result, _ = run_search("barclay r")
    assert result.path.endswith("+retry")
    assert {o.postcode + o.postnumber for o in result.options} == {
        o.postcode + o.postnumber for o in expected.options
    }
