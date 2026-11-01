"""
Fetch the LSOA11 to MSOA11 lookup CSV from the ONS Open Geography Portal.

Queries the OA11-to-LSOA-to-MSOA-to-LAD (December 2011) lookup with distinct
values, collapsing 181,408 OA rows to 34,753 LSOA11 rows. Standalone so
``alembic upgrade`` stays offline.

Usage::

    python -m hpldn.backend.scripts.fetch_lsoa_msoa_lookup [--out PATH]
"""

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hpldn.backend.migrations import inputs

LAYER_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "OA11_LSOA11_MSOA11_LAD11_EW_LUv2_b3fe7c68f4b2420185eaff6284d4c125/"
    "FeatureServer/0/query"
)
PAGE_SIZE = 2000  
EXPECTED_ROWS = 34753  

# The output path is the lsoa_msoa_lookup_csv_fp migration input (inputs.yaml), under HPLDN_DATA_DIR.
INPUT_KEY = "lsoa_msoa_lookup_csv_fp"


def _fetch_page(offset: int, attempts: int = 5) -> List[Dict]:
    """Fetch one page of distinct LSOA/MSOA rows, retrying transient service errors."""
    params = {
        "where": "1=1",
        "outFields": "LSOA11CD,LSOA11NM,MSOA11CD,MSOA11NM",
        "returnDistinctValues": "true",
        "returnGeometry": "false",
        "resultRecordCount": str(PAGE_SIZE),
        "resultOffset": str(offset),
        "orderByFields": "LSOA11CD ASC",
        "f": "json",
    }
    url = f"{LAYER_URL}?{urllib.parse.urlencode(params)}"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                payload = json.load(response)
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload["features"]
        except Exception as exc:  # noqa: BLE001 - retry any transport/service error
            if attempt == attempts - 1:
                raise
            print(f"  offset {offset} attempt {attempt + 1} failed: {exc}")
            time.sleep(2 * (attempt + 1))
    return []


def fetch_all() -> Dict[str, Tuple[str, str, str]]:
    """Return ``{lsoa11cd: (lsoa11nm, msoa11cd, msoa11nm)}`` for England and Wales."""
    rows: Dict[str, Tuple[str, str, str]] = {}
    offset = 0
    while True:
        features = _fetch_page(offset)
        if not features:
            break
        for feature in features:
            attrs = feature["attributes"]
            lsoa11cd = attrs["LSOA11CD"]
            rows[lsoa11cd] = (attrs["LSOA11NM"], attrs["MSOA11CD"], attrs["MSOA11NM"])
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
    print(f"  lookup rows: {len(rows)} distinct LSOA11s")
    return rows


def main(out_fp: Optional[Path] = None) -> None:
    """
    Fetch the lookup and write it as CSV.

    Raises
    ------
    RuntimeError
        If the row count is not `EXPECTED_ROWS` or LSOA codes repeat.
    """
    out_fp = Path(out_fp) if out_fp is not None else inputs.resolve(INPUT_KEY)

    print("fetching LSOA11 -> MSOA11 lookup ...")
    rows = fetch_all()

    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"got {len(rows)} LSOA11s, want {EXPECTED_ROWS}")
    if len(set(rows)) != len(rows):
        raise RuntimeError("lsoa11cd is not unique in the fetched rows")

    out_fp.parent.mkdir(parents=True, exist_ok=True)
    with open(out_fp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["lsoa11cd", "lsoa11nm", "msoa11cd", "msoa11nm"])
        for lsoa11cd in sorted(rows):
            lsoa11nm, msoa11cd, msoa11nm = rows[lsoa11cd]
            writer.writerow([lsoa11cd, lsoa11nm, msoa11cd, msoa11nm])
    print(f"wrote {out_fp} ({out_fp.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="Override the output CSV path")
    args = parser.parse_args()
    main(out_fp=args.out)
