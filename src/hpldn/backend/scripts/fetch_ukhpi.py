"""Fetch the London UK House Price Index series from 1995-01 to the configured CSV.

The file at ``ukhpi_csv_fp`` is overwritten in place, so configuration needs
no edit. The download is validated before writing. Standalone so ``alembic
upgrade`` stays offline; migration 0009 loads the file into
``production.ukhpi``.

Usage::

    python -m hpldn.backend.scripts.fetch_ukhpi
    python -m hpldn.backend.scripts.fetch_ukhpi --from 1995-01-01 --to 2025-01-01

If the endpoint changes, download the London series as CSV from
https://landregistry.data.gov.uk/app/ukhpi and save it over the printed path.
"""

import argparse
import csv
import io
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from hpldn.backend.migrations import inputs

DOWNLOAD_URL = "https://landregistry.data.gov.uk/app/ukhpi/download/new"
LOCATION_URI = "http://landregistry.data.gov.uk/id/region/london"

DEFAULT_FROM = "1995-01-01"
DEFAULT_TO = "2026-08-01"

# The default output is the ukhpi_csv_fp migration input (inputs.yaml), whose
# file name carries DEFAULT_FROM and DEFAULT_TO.
INPUT_KEY = "ukhpi_csv_fp"

# The column create_ukhpi_table reads the period from, checked at fetch time.
PERIOD_COLUMN = "Pivotable date"

# Two decades of monthly rows; an error page or truncated response is far shorter.
MIN_EXPECTED_ROWS = 240


def build_url(date_from: str, date_to: str) -> str:
    """Return the UKHPI download tool's London CSV URL for ``[date_from, date_to]``."""
    query = urllib.parse.urlencode(
        {
            "from": date_from,
            "to": date_to,
            "location": LOCATION_URI,
            "format": "csv",
        }
    )
    return f"{DOWNLOAD_URL}?{query}"


def fetch(date_from: str, date_to: str, timeout: int = 120) -> str:
    """Download the CSV and return it as text, without writing anything."""
    url = build_url(date_from, date_to)
    print(f"GET {url}")
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8-sig")


def validate(text: str, date_from: str) -> int:
    """
    Check the payload is the expected CSV and reaches back to `date_from`.

    Returns
    -------
    int
        Data-row count.

    Raises
    ------
    RuntimeError
        If there are no data rows, no period column, fewer than
        `MIN_EXPECTED_ROWS` rows, or the data does not reach `date_from`.
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("UKHPI download returned no data rows")
    if PERIOD_COLUMN not in rows[0]:
        raise RuntimeError(
            f"UKHPI download has no {PERIOD_COLUMN!r} column -- "
            f"got {sorted(rows[0])[:8]}..."
        )
    if len(rows) < MIN_EXPECTED_ROWS:
        raise RuntimeError(
            f"UKHPI download has only {len(rows)} data row(s), expected at "
            f"least {MIN_EXPECTED_ROWS}"
        )
    earliest = min(row[PERIOD_COLUMN] for row in rows)
    if earliest > date_from:
        raise RuntimeError(
            f"UKHPI download starts at {earliest}, not {date_from} -- the "
            "service clamped the requested range"
        )
    return len(rows)


def main(argv: Optional[list] = None) -> int:
    """Fetch, validate and write the CSV; return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="date_from", default=DEFAULT_FROM)
    parser.add_argument("--to", dest="date_to", default=DEFAULT_TO)
    parser.add_argument("--out", dest="out_fp", type=Path, default=None)
    args = parser.parse_args(argv)
    args.out_fp = args.out_fp or inputs.resolve(INPUT_KEY)

    text = fetch(args.date_from, args.date_to)
    row_count = validate(text, args.date_from)

    args.out_fp.parent.mkdir(parents=True, exist_ok=True)
    args.out_fp.write_text(text, encoding="utf-8")
    print(
        f"Wrote {row_count} UKHPI row(s) from {args.date_from} to "
        f"{args.date_to} -> {args.out_fp}"
    )
    print(
        "Next: rebuild from migration 0009 (reference tables), which loads "
        "production.ukhpi from this file."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
