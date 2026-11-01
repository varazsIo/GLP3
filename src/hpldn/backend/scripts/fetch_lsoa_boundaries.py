"""
Fetch LSOA 2011 boundary polygons as EPSG:4326 WKT from the ONS Open Geography Portal.

All 34,753 England and Wales polygons are fetched, because the IoD spine
needs national percentiles; migrations filter to London on load. GeoJSON
pages are converted to WKT with plain string formatting, so no GIS library is
needed. Standalone so ``alembic upgrade`` stays offline.

Usage::

    python -m hpldn.backend.scripts.fetch_lsoa_boundaries [--out PATH]
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
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/ArcGIS/rest/services/"
    "LSOA_Dec_2011_Boundaries_Generalised_Clipped_BGC_EW_V3/FeatureServer/0/query"
)
PAGE_SIZE = 2000  
EXPECTED_ROWS = 34753

# The output path is the lsoa_boundaries_csv_fp migration input (inputs.yaml), under HPLDN_DATA_DIR.
INPUT_KEY = "lsoa_boundaries_csv_fp"


def _fetch_page(offset: int, attempts: int = 5) -> List[Dict]:
    """Fetch one GeoJSON page of LSOA polygons, retrying transient service errors."""
    params = {
        "where": "1=1",
        "outFields": "LSOA11CD,LSOA11NM",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": str(PAGE_SIZE),
        "resultOffset": str(offset),
        "orderByFields": "LSOA11CD ASC",
        "f": "geojson",
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


def _ring_wkt(ring: List[List[float]]) -> str:
    """Format a GeoJSON linear ring as a WKT ring, ``"(x y,x y,...)"``."""
    return "(" + ",".join(f"{x} {y}" for x, y in ring) + ")"


def _polygon_coords_wkt(coordinates: List[List[List[float]]]) -> str:
    """Format a GeoJSON Polygon's rings (exterior, then holes) as a WKT ring list."""
    return "(" + ",".join(_ring_wkt(ring) for ring in coordinates) + ")"


def _geometry_to_wkt(geometry: Dict) -> str:
    """Convert a GeoJSON Polygon or MultiPolygon geometry to WKT."""
    gtype = geometry["type"]
    if gtype == "Polygon":
        return f"POLYGON{_polygon_coords_wkt(geometry['coordinates'])}"
    if gtype == "MultiPolygon":
        parts = ",".join(_polygon_coords_wkt(poly) for poly in geometry["coordinates"])
        return f"MULTIPOLYGON({parts})"
    raise ValueError(f"unexpected LSOA boundary geometry type: {gtype}")


def fetch_all() -> Dict[str, Tuple[str, str]]:
    """Return ``{lsoa11cd: (lsoa11nm, wkt)}`` for England and Wales in EPSG:4326."""
    boundaries: Dict[str, Tuple[str, str]] = {}
    offset = 0
    while True:
        features = _fetch_page(offset)
        if not features:
            break
        for feature in features:
            props = feature["properties"]
            lsoa11cd = props["LSOA11CD"]
            boundaries[lsoa11cd] = (props["LSOA11NM"], _geometry_to_wkt(feature["geometry"]))
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
    print(f"  boundaries: {len(boundaries)} features")
    return boundaries


def main(out_fp: Optional[Path] = None) -> None:
    """
    Fetch the boundaries and write them as CSV.

    Raises
    ------
    RuntimeError
        If the polygon count is not `EXPECTED_ROWS`.
    """
    out_fp = Path(out_fp) if out_fp is not None else inputs.resolve(INPUT_KEY)

    print("fetching EPSG:4326 LSOA11 boundary polygons ...")
    boundaries = fetch_all()

    if len(boundaries) != EXPECTED_ROWS:
        raise RuntimeError(f"got {len(boundaries)} LSOAs, want {EXPECTED_ROWS}")

    out_fp.parent.mkdir(parents=True, exist_ok=True)
    with open(out_fp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["lsoa11cd", "lsoa11nm", "wkt"])
        for lsoa11cd in sorted(boundaries):
            lsoa11nm, wkt = boundaries[lsoa11cd]
            writer.writerow([lsoa11cd, lsoa11nm, wkt])
    print(f"wrote {out_fp} ({out_fp.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="Override the output CSV path")
    args = parser.parse_args()
    main(out_fp=args.out)
