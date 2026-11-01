"""
What ``GET /addresses/search`` accepts as a query, and its normal form.

Kept apart from `market.address_search` so `api.schemas` can validate the
parameters without importing the SQL. `normalise_query` must agree with
``production.hpldn_searchnorm`` (`migrations.address_search`), or a typed
address will not match its own indexed form.
"""

import re
import unicodedata

MIN_QUERY_LENGTH = 3
MAX_QUERY_LENGTH = 64
DEFAULT_LIMIT = 8
MAX_LIMIT = 10

# A raw parameter longer than this is refused before it is normalised at all:
# NFKC can expand a character up to 18-fold, so the 64-character bound is only
# meaningful once the input is known to be small.
MAX_RAW_LENGTH = 256

# What a landlord types into an address: letters, digits, spaces, hyphens and
# apostrophes (straight or curly), plus the commas, full stops and slashes UK
# addresses are written with ("Flat 1/2, 3 St. Mary's Road"). Everything else --
# %, _, \, quotes, angle brackets, control characters -- is refused, not stripped.
ALLOWED_QUERY = re.compile(r"^[A-Za-z0-9 '’\-,./]+$")


class InvalidQuery(ValueError):
    """The query is outside the accepted bounds. The message never quotes it."""


def normalise_query(raw: str) -> str:
    """
    Return `raw` as ``production.hpldn_searchnorm`` would store it.

    NFKC-normalised, upper-cased, apostrophes dropped (MARY'S -> MARYS), every
    other non-alphanumeric run collapsed to one space, trimmed.
    """
    value = unicodedata.normalize("NFKC", raw).upper()
    value = re.sub(r"['’`]", "", value)
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", value).split())


def validate_query(raw: str | None) -> str:
    """
    Check `raw` and return its normalised form.

    Raises
    ------
    InvalidQuery
        When it is missing, too long, carries a character outside
        `ALLOWED_QUERY`, or normalises to fewer than `MIN_QUERY_LENGTH` or more
        than `MAX_QUERY_LENGTH` characters.
    """
    if raw is None:
        raise InvalidQuery("Type at least 3 characters of the address")
    if len(raw) > MAX_RAW_LENGTH:
        raise InvalidQuery("That address is too long to search for")
    stripped = unicodedata.normalize("NFKC", raw).strip()
    if not stripped:
        raise InvalidQuery("Type at least 3 characters of the address")
    if not ALLOWED_QUERY.match(stripped):
        raise InvalidQuery("Use only letters, numbers, spaces and , . / ' - in an address")
    collapsed = " ".join(stripped.split())
    if len(collapsed) > MAX_QUERY_LENGTH:
        raise InvalidQuery("That address is too long to search for")
    normalised = normalise_query(collapsed)
    if len(normalised) < MIN_QUERY_LENGTH:
        raise InvalidQuery("Type at least 3 characters of the address")
    return normalised


def escape_like(value: str) -> str:
    r"""Escape `value` for a ``LIKE … ESCAPE '\'`` pattern, so ``%``, ``_`` and ``\`` match themselves."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
