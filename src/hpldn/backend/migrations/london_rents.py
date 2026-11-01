"""
The London slice of the ONS Price Index of Private Rents, for ``production.london_rents``.

ONS publishes the monthly price statistics as one wide CSV for the whole UK
(``private_rents_monthly_prices.csv``, from the "Price Index of Private Rents,
UK: monthly price statistics" release): two title rows, a header, then one row
per area and month with an index, monthly change, annual change and mean
rental price for each of nine measures -- all homes, one to four-or-more
bedrooms, and four property types.

`extract` keeps London's rows (the region ``E12000007`` and its 32 boroughs;
ONS publishes nothing for the City of London) and writes them long, one row per
area, month and measure, to the ``ons_pipr_london_rents_csv_fp`` migration
input (``$HPLDN_DATA_DIR/migration_inputs/ons_pipr_london_rents.csv``, Tier A
in ``inputs.yaml``), which migration 0026 loads. Re-run `extract` for each new
ONS release, update the input's sha256 and size, push it and add a revision
that reloads the table::

    python -m hpldn.backend.migrations.london_rents extract \\
        data/private_rents_monthly_prices.csv --release YYYY-MM-DD
"""

import csv
from datetime import date, datetime
from pathlib import Path

from hpldn.backend.migrations import inputs

INPUT_KEY = "ons_pipr_london_rents_csv_fp"

LONDON_REGION_CODE = "E12000007"
LONDON_REGION_NAME = "London"

# Title rows above the CSV's header row.
PREAMBLE_ROWS = 2

# ONS's suppressed ([x]) and not-applicable ([z]) markers.
MISSING = {"[x]", "[z]", ""}

# measure -> the suffix ONS appends to its four column names for that measure.
MEASURES = {
    "all": "",
    "one_bed": " one bed",
    "two_bed": " two bed",
    "three_bed": " three bed",
    "four_plus_bed": " four or more bed",
    "detached": " detached",
    "semidetached": " semidetached",
    "terraced": " terraced",
    "flat_maisonette": " flat maisonette",
}

# Bedroom count -> measure; 4 stands for four or more.
BEDROOM_MEASURES = {1: "one_bed", 2: "two_bed", 3: "three_bed", 4: "four_plus_bed"}

FIELDS = (
    "area_code",
    "area_name",
    "month",
    "measure",
    "price_index",
    "monthly_change",
    "annual_change",
    "rental_price",
    "source",
)

_SOURCE = "ONS Price Index of Private Rents"


def source_label(release: date | None, latest: date) -> str:
    """Return the ``source`` text for a release: its publication date, else its latest month."""
    if release is not None:
        return f"{_SOURCE} (release {release.isoformat()})"
    return f"{_SOURCE} (to {latest:%b %Y})"


def _value(raw: str) -> str:
    raw = raw.strip()
    return "" if raw in MISSING else raw


def _price(raw: str) -> str:
    """Return a rental price as whole pounds; ONS writes a few as ``1884.0``."""
    value = _value(raw)
    if not value:
        return value
    pounds = float(value)
    if not pounds.is_integer():
        raise ValueError(f"rental price {raw!r} is not whole pounds")
    return str(int(pounds))


def _is_london(row: dict) -> bool:
    return row["Area code"] == LONDON_REGION_CODE or row["Region or country name"] == LONDON_REGION_NAME


def extract(source_csv: Path, fixture: Path | None = None, release: date | None = None) -> int:
    """
    Write London's rows of the ONS monthly price CSV to ``fixture``, long.

    Parameters
    ----------
    source_csv : Path
        ONS ``private_rents_monthly_prices.csv``.
    fixture : Path, optional
        Output CSV, with the columns in `FIELDS`; the `INPUT_KEY` input by default.
    release : date, optional
        The release's publication date, recorded in ``source``.

    Returns
    -------
    int
        Rows written.
    """
    with source_csv.open(newline="", encoding="utf-8-sig") as handle:
        for _ in range(PREAMBLE_ROWS):
            next(handle)
        rows = []
        for row in csv.DictReader(handle):
            if not _is_london(row):
                continue
            month = datetime.strptime(row["Time period"].strip(), "%b-%y").date()
            for measure, suffix in MEASURES.items():
                rows.append(
                    {
                        "area_code": row["Area code"].strip(),
                        "area_name": row["Area name"].strip(),
                        "month": month.isoformat(),
                        "measure": measure,
                        "price_index": _value(row[f"Index{suffix}"]),
                        "monthly_change": _value(row[f"Monthly change{suffix}"]),
                        "annual_change": _value(row[f"Annual change{suffix}"]),
                        "rental_price": _price(row[f"Rental price{suffix}"]),
                    }
                )
    if not rows:
        raise ValueError(f"{source_csv}: no London rows")
    source = source_label(release, max(date.fromisoformat(row["month"]) for row in rows))
    rows.sort(key=lambda row: (row["area_code"], row["month"], list(MEASURES).index(row["measure"])))
    fixture = fixture or inputs.resolve(INPUT_KEY)
    fixture.parent.mkdir(parents=True, exist_ok=True)
    with fixture.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({**row, "source": source} for row in rows)
    return len(rows)


def _optional(raw: str, cast):
    return cast(raw) if raw else None


def read_fixture(path: Path | None = None) -> list[dict]:
    """Return the input's rows with typed values; suppressed figures are None."""
    path = path or inputs.resolve(INPUT_KEY)
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {
                "area_code": row["area_code"],
                "area_name": row["area_name"],
                "month": date.fromisoformat(row["month"]),
                "measure": row["measure"],
                "price_index": _optional(row["price_index"], float),
                "monthly_change": _optional(row["monthly_change"], float),
                "annual_change": _optional(row["annual_change"], float),
                "rental_price": _optional(row["rental_price"], int),
                "source": row["source"],
            }
            for row in csv.DictReader(handle)
        ]


def main(argv=None) -> int:
    """
    Refresh the London rents fixture from an ONS release.

    ``extract <csv> [--release YYYY-MM-DD]`` rewrites the
    ``ons_pipr_london_rents_csv_fp`` migration input from ONS's
    ``private_rents_monthly_prices.csv``.
    """
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__.strip().splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    extract_cmd = commands.add_parser("extract", help="write the fixture from an ONS CSV")
    extract_cmd.add_argument("source_csv", type=Path)
    extract_cmd.add_argument("--fixture", type=Path, default=None)
    extract_cmd.add_argument(
        "--release", type=date.fromisoformat, default=None,
        help="the release's publication date (ISO), recorded in the source column",
    )
    args = parser.parse_args(argv)
    fixture = args.fixture or inputs.resolve(INPUT_KEY)
    rows = extract(args.source_csv, fixture, args.release)
    print(f"{fixture}: {rows} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
