"""
Fetch the open sources migration 0028 links Price Paid sales to UPRNs with.

Standalone so ``alembic upgrade`` stays offline. Each source is written to its
path in ``migrations/inputs.yaml``, under ``HPLDN_DATA_DIR`` (``data/`` below):

- ``nsul``: ONS National Statistics UPRN Lookup (OGL), ``data/nsul/NSUL_*.zip``.
  The ArcGIS item id changes every release; pass ``--nsul-item`` for a newer one.
- ``lids``: OS Open Linked Identifiers (OGL), the four CSV zips under
  ``data/lids/`` (UPRN->USRN, UPRN->building TOID, Open Roads link->RoadLink
  TOID, RoadLink TOID->USRN).
- ``open-roads``: OS Open Roads GeoPackage (OGL), unzipped to
  ``data/open_roads/Data/oproad_gb.gpkg``.
- ``osm``: Geofabrik's Greater London OpenStreetMap extract (ODbL),
  ``data/osm/greater-london-latest.osm.pbf``.
- ``planning``: GLA Planning London Datahub applications (OGL) through its
  public Elasticsearch endpoint, ``data/planning_datahub_applications.csv``.
- ``hmlr``: nothing to fetch automatically; HM Land Registry publishes one
  ``pp-uprn-lookup-<mon>-<year>.csv`` a month, saved into ``data/`` by hand.

Usage::

    python -m hpldn.backend.scripts.fetch_linking_sources [nsul lids open-roads osm planning]
"""

import csv
import json
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional

from hpldn.backend.migrations import inputs


NSUL_ITEM = "e5c9409787b344dea64488d70fb0990a"  # NSUL June 2026 (Epoch 127)
NSUL_URL = "https://www.arcgis.com/sharing/rest/content/items/{item}/data"

LIDS_RELEASE = "2026-09"
LIDS_FILES = (
    "BLPU-UPRN-Street-USRN-11",
    "BLPU-UPRN-TopographicArea-TOID-5",
    "ORRoadLink-GUID-RoadLink-TOID-12",
    "RoadLink-TOID-Street-USRN-8",
)
LIDS_URL = (
    "https://api.os.uk/downloads/v1/products/LIDS/downloads"
    "?area=GB&format=CSV&fileName=lids-{release}_csv_{name}.zip&redirect"
)
OPEN_ROADS_URL = (
    "https://api.os.uk/downloads/v1/products/OpenRoads/downloads"
    "?area=GB&format=GeoPackage&redirect"
)
OSM_URL = (
    "https://download.geofabrik.de/europe/united-kingdom/england/"
    "greater-london-latest.osm.pbf"
)

PLANNING_URL = "https://planningdata.london.gov.uk/api-guest/applications/_search"
PLANNING_PAGE_SIZE = 5000
PLANNING_FIELDS = (
    "uprn", "site_number", "site_name", "street_name", "secondary_street_name",
    "postcode", "lpa_name", "valid_date", "application_type",
)


def _download(url: str, out_fp: Path) -> None:
    """Download `url` to `out_fp` with curl (resumable, follows redirects)."""
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {out_fp.name} ...")
    subprocess.run(
        ["curl", "-sSfL", "--retry", "5", "-C", "-", "-o", str(out_fp), url],
        check=True,
    )
    print(f"  wrote {out_fp} ({out_fp.stat().st_size / 1e6:.0f} MB)")


def fetch_nsul(item: str = NSUL_ITEM) -> None:
    """Download the NSUL release zip; migration 0028 reads its London (``_LN``) CSV in place."""
    out_fp = inputs.resolve("nsul_zip_fp").with_name("nsul.zip")
    _download(NSUL_URL.format(item=item), out_fp)
    with zipfile.ZipFile(out_fp) as archive:
        london = [n for n in archive.namelist() if n.endswith("_LN.csv")]
    if not london:
        raise RuntimeError(f"{out_fp} has no Data/NSUL_*_LN.csv member")
    named = out_fp.with_name(Path(london[0]).stem.removesuffix("_LN") + ".zip")
    out_fp.rename(named)
    print(f"  renamed to {named.name}")


def fetch_lids(release: str = LIDS_RELEASE) -> None:
    """Download the four LIDS CSV zips 0028 reads."""
    for name in LIDS_FILES:
        _download(
            LIDS_URL.format(release=release, name=name),
            inputs.resolve("lids_dir").joinpath(f"lids-{release}_csv_{name}.zip"),
        )


def fetch_open_roads() -> None:
    """Download and unzip OS Open Roads (GeoPackage)."""
    # The input is <open_roads>/Data/oproad_gb.gpkg; the zip lands in <open_roads>.
    zip_fp = inputs.resolve("open_roads_gpkg_fp").parents[1].joinpath("oproad_gpkg_gb.zip")
    _download(OPEN_ROADS_URL, zip_fp)
    with zipfile.ZipFile(zip_fp) as archive:
        archive.extract("Data/oproad_gb.gpkg", zip_fp.parent)


def fetch_osm() -> None:
    """Download Geofabrik's Greater London extract."""
    _download(OSM_URL, inputs.resolve("osm_pbf_fp"))


def _planning_page(search_after: Optional[List], attempts: int = 5) -> List[dict]:
    body = {
        "size": PLANNING_PAGE_SIZE,
        "_source": list(PLANNING_FIELDS),
        "query": {"exists": {"field": "uprn"}},
        "sort": [{"_id": "asc"}],
    }
    if search_after is not None:
        body["search_after"] = search_after
    request = urllib.request.Request(
        PLANNING_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)["hits"]["hits"]
        except Exception as exc:  # noqa: BLE001 - retry any transport/service error
            if attempt == attempts - 1:
                raise
            print(f"  page after {search_after} attempt {attempt + 1} failed: {exc}")
            time.sleep(5 * (attempt + 1))
    return []


def fetch_planning(out_fp: Optional[Path] = None) -> None:
    """
    Page every application with a UPRN out of the Datahub and write it as CSV.

    One row per application: its id, the fields in `PLANNING_FIELDS`, in that
    order. Paged with ``search_after`` on ``_id``.
    """
    out_fp = out_fp or inputs.resolve("planning_applications_csv_fp")
    rows = 0
    search_after = None
    with open(out_fp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("id",) + PLANNING_FIELDS)
        while True:
            hits = _planning_page(search_after)
            if not hits:
                break
            for hit in hits:
                source = hit["_source"]
                writer.writerow(
                    [hit["_id"]]
                    + [
                        "" if source.get(field) is None else str(source[field]).strip()
                        for field in PLANNING_FIELDS
                    ]
                )
            rows += len(hits)
            search_after = hits[-1]["sort"]
            if rows % 100_000 < PLANNING_PAGE_SIZE:
                print(f"  {rows} application(s)")
    print(f"wrote {out_fp} ({rows} application(s), {out_fp.stat().st_size / 1e6:.0f} MB)")


SOURCES = {
    "nsul": fetch_nsul,
    "lids": fetch_lids,
    "open-roads": fetch_open_roads,
    "osm": fetch_osm,
    "planning": fetch_planning,
}


def main(sources: Iterable[str]) -> None:
    """Fetch each named source in turn."""
    for source in sources:
        SOURCES[source]()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sources", nargs="*", choices=sorted(SOURCES), default=sorted(SOURCES),
        help="Sources to fetch (default: all)",
    )
    args = parser.parse_args()
    main(args.sources)
