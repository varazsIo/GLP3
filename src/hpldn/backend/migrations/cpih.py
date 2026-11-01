"""
UK consumer price inflation (CPIH), for ``production.cpih``.

The input ``ons_cpih_annual_rate_csv_fp`` (``inputs.yaml``; the file is
``$HPLDN_DATA_DIR/migration_inputs/ons_cpih_annual_rate.csv``) is ONS's CPIH 12-month rate
for all items (series L55O), one row per month from January 1989: a
``year_month`` such as ``"2017 AUG"`` and a ``percentage`` such as ``"2.7"``,
the rise in prices over the 12 months to that month.

A rate alone cannot say how much prices rose between any two months, so
`build_index` turns the rates into an index: each month is the same month a
year earlier times one plus its rate. That needs twelve starting values for
1988, which the file does not hold; they are spread evenly along 1989's
average rate. The index is then rebased so 2015 averages 100, like ONS's
own CPIH index (L522). The ratio of two months whole years apart is exact to
the rates' rounding; for other pairs the starting guess adds well under 1 %.

Refresh it by replacing the file with a later ONS download, updating its
sha256 and size in ``inputs.yaml``, pushing it (Tier A) and adding a
revision that reloads the table, as 0026 does for the rents.
"""

import csv
from datetime import date
from pathlib import Path
from typing import Optional

from hpldn.backend.migrations import inputs

INPUT_KEY = "ons_cpih_annual_rate_csv_fp"

SOURCE = "ONS CPIH annual rate, all items (L55O), rebased chained index 2015=100"

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# The year the index averages 100 in, as ONS's CPIH index does.
BASE_YEAR = 2015


def parse_month(raw: str) -> date:
    """Return the first of the month named by an ONS ``"1989 JAN"`` label."""
    year, month = raw.strip().split()
    return date(int(year), _MONTHS.index(month.upper()) + 1, 1)


def read_rates(path: Optional[Path] = None) -> dict[date, float]:
    """
    Return each month's CPIH 12-month rate, in percent, oldest first.

    `path` defaults to the `INPUT_KEY` migration input.

    Raises
    ------
    ValueError
        When a month repeats or the months are not consecutive.
    """
    path = path or inputs.resolve(INPUT_KEY)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [(parse_month(row["year_month"]), float(row["percentage"])) for row in csv.DictReader(handle)]
    rates = dict(sorted(rows))
    if len(rates) != len(rows):
        raise ValueError(f"{path}: a month appears more than once")
    months = list(rates)
    for previous, current in zip(months, months[1:]):
        if _months_between(previous, current) != 1:
            raise ValueError(f"{path}: no rate between {previous:%b %Y} and {current:%b %Y}")
    return rates


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def build_index(rates: dict[date, float]) -> dict[date, float]:
    """
    Return a monthly price index chained from 12-month rates, 2015 averaging 100.

    Parameters
    ----------
    rates : dict of date to float
        Consecutive months' 12-month rates in percent, starting in January.

    Returns
    -------
    dict of date to float
        Each month's index, in the order of `rates`.
    """
    months = list(rates)
    first = months[0]
    if first.month != 1 or len(months) < 12:
        raise ValueError("the rates must start in January and cover at least a year")
    # The year before the first: rising evenly along the first year's average rate.
    monthly_growth = (1 + sum(rates[m] for m in months[:12]) / 12 / 100) ** (1 / 12)
    previous_year = {month: monthly_growth ** (month - 12) for month in range(1, 13)}
    index: dict[date, float] = {}
    for month in months:
        year_before = date(month.year - 1, month.month, 1)
        base = index[year_before] if year_before in index else previous_year[month.month]
        index[month] = base * (1 + rates[month] / 100)
    base_months = [value for month, value in index.items() if month.year == BASE_YEAR]
    scale = 100 / (sum(base_months) / len(base_months)) if len(base_months) == 12 else 1.0
    return {month: value * scale for month, value in index.items()}
