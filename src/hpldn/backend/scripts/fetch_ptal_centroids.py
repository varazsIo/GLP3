"""
Fetch per-cell geometry for TfL's PTAL 2023 100 m grid.

Writes cell centroids, or with ``--geometry polygon`` the cell polygons, as
CSV. The published PTAL extract has no geometry and its GridID cannot be
mapped to a position arithmetically, so geometry comes from TfL's ArcGIS
service. Centroids are fetched in EPSG:27700 and EPSG:4326 to cross-check
without a reprojection library; polygons are fetched as GeoJSON in EPSG:4326
and converted to WKT with string formatting. Standalone so ``alembic
upgrade`` stays offline.

Usage::

    python -m hpldn.backend.scripts.fetch_ptal_centroids
    python -m hpldn.backend.scripts.fetch_ptal_centroids --geometry polygon
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
    "https://services1.arcgis.com/YswvgzOodUvqkoCN/ArcGIS/rest/services/"
    "PTAL_2023_Grid_100m_100m/FeatureServer/33/query"
)
PAGE_SIZE = 2000  
EXPECTED_ROWS = 159451

# Output paths are these migration inputs (inputs.yaml), under HPLDN_DATA_DIR.
INPUT_KEY = "ptal_centroids_csv_fp"
POLYGONS_INPUT_KEY = "ptal_polygons_csv_fp"


def _fetch_page(offset: int, out_sr: int, attempts: int = 5) -> List[Dict]:
    """Fetch one page of GridID and centroid, retrying transient service errors."""
    params = {
        "where": "1=1",
        "outFields": "GridID",
        "returnGeometry": "false",
        "returnCentroid": "true",
        "outSR": str(out_sr),
        "resultRecordCount": str(PAGE_SIZE),
        "resultOffset": str(offset),
        "orderByFields": "GridID ASC",
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
            print(f"  offset {offset} sr {out_sr} attempt {attempt + 1} failed: {exc}")
            time.sleep(2 * (attempt + 1))
    return []


def fetch_all(out_sr: int) -> Dict[int, Tuple[float, float]]:
    """Return ``{grid_id: (x, y)}`` for the whole layer in spatial reference `out_sr`."""
    centroids: Dict[int, Tuple[float, float]] = {}
    offset = 0
    while True:
        features = _fetch_page(offset, out_sr)
        if not features:
            break
        for feature in features:
            centroid = feature.get("centroid")
            if centroid is not None:
                centroids[int(feature["attributes"]["GridID"])] = (
                    centroid["x"],
                    centroid["y"],
                )
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
    print(f"  sr {out_sr}: {len(centroids)} centroids")
    return centroids


def _fetch_geojson_page(offset: int, attempts: int = 5) -> List[Dict]:
    """Fetch one GeoJSON page of GridID and polygon, retrying transient service errors."""
    params = {
        "where": "1=1",
        "outFields": "GridID",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": str(PAGE_SIZE),
        "resultOffset": str(offset),
        "orderByFields": "GridID ASC",
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
            print(f"  offset {offset} (polygons) attempt {attempt + 1} failed: {exc}")
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
    raise ValueError(f"unexpected PTAL cell geometry type: {gtype}")


def fetch_all_polygons() -> Dict[int, str]:
    """Return ``{grid_id: wkt}`` for the whole layer in EPSG:4326."""
    polygons: Dict[int, str] = {}
    offset = 0
    while True:
        features = _fetch_geojson_page(offset)
        if not features:
            break
        for feature in features:
            grid_id = int(feature["properties"]["GridID"])
            polygons[grid_id] = _geometry_to_wkt(feature["geometry"])
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
    print(f"  polygons: {len(polygons)} features")
    return polygons


def main(out_fp: Optional[Path] = None, geometry: str = "centroid") -> None:
    """
    Fetch centroids or polygons and write them as CSV.

    Raises
    ------
    ValueError
        If `geometry` is not ``"centroid"`` or ``"polygon"``.
    RuntimeError
        If the row count is wrong or the grid ids are not ``1..N`` in both
        projections.
    """
    if geometry == "polygon":
        out_fp = Path(out_fp) if out_fp is not None else inputs.resolve(POLYGONS_INPUT_KEY)
        print("fetching EPSG:4326 polygons ...")
        polygons = fetch_all_polygons()
        if len(polygons) != EXPECTED_ROWS:
            raise RuntimeError(
                f"polygons: got {len(polygons)}, want {EXPECTED_ROWS}"
            )
        if set(polygons) != set(range(1, EXPECTED_ROWS + 1)):
            raise RuntimeError("grid ids are not 1..N")

        out_fp.parent.mkdir(parents=True, exist_ok=True)
        with open(out_fp, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["GridID", "wkt"])
            for grid_id in range(1, EXPECTED_ROWS + 1):
                writer.writerow([grid_id, polygons[grid_id]])
        print(f"wrote {out_fp} ({out_fp.stat().st_size / 1e6:.1f} MB)")
        return
    if geometry != "centroid":
        raise ValueError(f"Unknown geometry '{geometry}', expected centroid/polygon")

    out_fp = Path(out_fp) if out_fp is not None else inputs.resolve(INPUT_KEY)

    print("fetching EPSG:27700 (British National Grid) centroids ...")
    bng = fetch_all(27700)
    print("fetching EPSG:4326 (WGS-84) centroids ...")
    wgs = fetch_all(4326)

    if len(bng) != EXPECTED_ROWS:
        raise RuntimeError(f"27700: got {len(bng)}, want {EXPECTED_ROWS}")
    if len(wgs) != EXPECTED_ROWS:
        raise RuntimeError(f"4326: got {len(wgs)}, want {EXPECTED_ROWS}")
    if set(bng) != set(wgs):
        raise RuntimeError("grid id sets differ between the two projections")
    if set(bng) != set(range(1, EXPECTED_ROWS + 1)):
        raise RuntimeError("grid ids are not 1..N")

    on_lattice = sum(
        1
        for x, y in bng.values()
        if min((x - 45) % 100, 100 - (x - 45) % 100) < 0.05
        and min((y - 52) % 100, 100 - (y - 52) % 100) < 0.05
    )
    print(f"whole cells (centroid on the 100m lattice): {on_lattice}")
    print(f"cells clipped by the GLA boundary:          {EXPECTED_ROWS - on_lattice}")

    out_fp.parent.mkdir(parents=True, exist_ok=True)
    with open(out_fp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["GridID", "latitude", "longitude", "easting", "northing"])
        for grid_id in range(1, EXPECTED_ROWS + 1):
            longitude, latitude = wgs[grid_id]
            easting, northing = bng[grid_id]
            writer.writerow(
                [
                    grid_id,
                    f"{latitude:.8f}",
                    f"{longitude:.8f}",
                    round(easting, 1),
                    round(northing, 1),
                ]
            )
    print(f"wrote {out_fp} ({out_fp.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--geometry",
        choices=["centroid", "polygon"],
        default="centroid",
        help="centroid (default, migration 0028) or polygon (migration 0030)",
    )
    parser.add_argument("--out", type=Path, default=None, help="Override the output CSV path")
    args = parser.parse_args()
    main(out_fp=args.out, geometry=args.geometry)
