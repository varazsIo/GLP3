"""
Build the frontend's postcode district map, ``frontend/src/data/londonGeo.ts``.

Read-only, against hpdbldn (``POSTGRES_DEVELOP_*``). Draws one SVG shape per
covered area plus uncovered leftovers inside Greater London, and the Thames,
on a 640-wide viewBox. Inner areas come from Doogal's district KML, outer areas
from Voronoi cells of in-use postcodes, all clipped to the union of London's
LSOA polygons and cleaned into an exact coverage. Fails without writing if an
area lacks a shape or the file exceeds 400 KB. See ``scripts/README.md``.

Usage::

    python -m hpldn.backend.scripts.build_london_map [--out PATH]
"""

import argparse
import csv
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from sqlalchemy import create_engine, text

from hpldn.backend.api.market.districts import DISTRICTS, area_of
from hpldn.backend.common.db import DEVELOP_DB_ENV_PREFIX, _url_from_env_prefix
from hpldn.backend.common.utils import load_env_vars

log = logging.getLogger("build_london_map")

REPO_ROOT = Path(__file__).resolve().parents[4]
KML_FP = REPO_ROOT / "data" / "All_London_Postcodes.kml"
POSTCODES_FP = REPO_ROOT / "data" / "All_London_Postcodes_Doogal.csv"
DEFAULT_OUT_FP = REPO_ROOT / "src" / "hpldn" / "frontend" / "src" / "data" / "londonGeo.ts"

KML_NS = "http://www.opengis.net/kml/2.2"

# The postcode areas Doogal's KML draws; every other letter code is outer.
INNER_LETTERS = frozenset({"EC", "WC", "E", "N", "NW", "SE", "SW", "W"})

VIEW_WIDTH = 640
MIN_UNCOVERED_KM2 = 0.05
SIMPLIFY_M = 40
# Detached parts smaller than this are dropped (a shape's largest part is kept).
MIN_PART_M2 = 20_000
# Holes left by dropped shapes up to this width are filled; the Thames stays water.
GAP_FILL_M = 400
# Closing the outline by this much fills the tidal Thames channel back in.
RIVER_CLOSE_M = 800
SIZE_BUDGET_BYTES = 400_000

# The outline, in metres.
OUTLINE_SQL = text(
    """
    SELECT ST_AsEWKB(ST_Union(ST_Transform(geom, 27700))) AS outline
      FROM staging.lsoa_boundaries
    """
)

# Step 2: one row per inner group, clipped to the outline.
INNER_SQL = text(
    """
    WITH placemarks AS (
        SELECT u.code,
               ST_MakeValid(ST_Transform(ST_GeomFromKML(u.kml), 27700)) AS geom
          FROM unnest(CAST(:codes AS text[]), CAST(:kmls AS text[])) AS u(code, kml)
    ),
    outline AS (SELECT ST_GeomFromEWKB(:outline) AS geom)
    SELECT p.code,
           ST_AsEWKB(ST_CollectionExtract(ST_Intersection(ST_Union(p.geom), o.geom), 3)) AS geom
      FROM placemarks p, outline o
     GROUP BY p.code, o.geom
    """
)

# Step 3: outer postcodes' Voronoi cells, unioned per letter code and clipped.
# Cells match sites through a 1 km bucket grid; CTEs are MATERIALIZED so the
# planner does not test every cell against every site.
OUTER_SQL = text(
    """
    WITH sites AS (
        SELECT ST_Transform(ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326), 27700) AS geom,
               u.letters
          FROM unnest(CAST(:lons AS float8[]), CAST(:lats AS float8[]),
                      CAST(:letters AS text[])) AS u(lon, lat, letters)
    ),
    outline AS (SELECT ST_GeomFromEWKB(:outline) AS geom),
    rest AS (
        SELECT ST_Difference(o.geom, ST_Union(ST_GeomFromEWKB(i.ewkb))) AS geom
          FROM outline o, unnest(CAST(:inner AS bytea[])) AS i(ewkb)
         GROUP BY o.geom
    ),
    cells AS MATERIALIZED (
        SELECT row_number() OVER () AS cell_id, d.geom
          FROM outline o,
               LATERAL ST_Dump(ST_VoronoiPolygons(
                   (SELECT ST_Collect(geom) FROM sites), 0, ST_Expand(o.geom, 2000))) AS d
    ),
    cell_buckets AS MATERIALIZED (
        SELECT c.cell_id, c.geom, bx, by_
          FROM cells c,
               generate_series(floor(ST_XMin(c.geom) / 1000)::int, floor(ST_XMax(c.geom) / 1000)::int) AS bx,
               generate_series(floor(ST_YMin(c.geom) / 1000)::int, floor(ST_YMax(c.geom) / 1000)::int) AS by_
    ),
    site_buckets AS MATERIALIZED (
        SELECT s.geom, s.letters,
               floor(ST_X(s.geom) / 1000)::int AS bx,
               floor(ST_Y(s.geom) / 1000)::int AS by_
          FROM sites s
    ),
    labelled AS (
        SELECT DISTINCT ON (b.cell_id) b.cell_id, b.geom, s.letters
          FROM cell_buckets b
          JOIN site_buckets s USING (bx, by_)
         WHERE ST_Intersects(b.geom, s.geom)
         ORDER BY b.cell_id, s.letters
    )
    SELECT l.letters AS code,
           ST_AsEWKB(ST_CollectionExtract(ST_Intersection(ST_SetSRID(ST_CoverageUnion(l.geom), 27700), r.geom), 3)) AS geom
      FROM labelled l, rest r
     GROUP BY l.letters, r.geom
    """
)

# Step 4: clean, simplify, fit and write. ST_AsSVG negates y, so fitting y as
# (y - north) * scale writes it top-down from 0.
FINISH_SQL = text(
    """
    WITH inputs AS (
        SELECT u.code, u.ord, ST_GeomFromEWKB(u.ewkb) AS geom
          FROM unnest(CAST(:codes AS text[]), CAST(:ords AS int[]), CAST(:geoms AS bytea[]))
               AS u(code, ord, ewkb)
    ),
    shapes AS (
        SELECT i.code, i.ord,
               (SELECT ST_Multi(ST_Collect(part.geom))
                  FROM (SELECT d.geom, rank() OVER (ORDER BY ST_Area(d.geom) DESC) AS size_rank
                          FROM ST_Dump(i.geom) AS d) AS part
                 WHERE part.size_rank = 1 OR ST_Area(part.geom) >= :min_part_m2) AS geom
          FROM inputs i
    ),
    cleaned AS (
        SELECT code, ord, ST_CoverageClean(geom, :gap_m) OVER () AS geom FROM shapes
    ),
    simplified AS (
        SELECT code, ord, ST_CoverageSimplify(geom, :simplify_m) OVER () AS geom FROM cleaned
    ),
    checked AS (
        SELECT code, ord, geom,
               ST_CoverageInvalidEdges(geom, 0) OVER () IS NOT NULL AS invalid_edges,
               sum(ST_Area(geom)) OVER () - ST_Area(ST_Union(geom) OVER ()) AS overlap_m2
          FROM simplified
         WHERE NOT ST_IsEmpty(geom)
    ),
    fitted AS (
        SELECT code, ord, invalid_edges, overlap_m2,
               ST_Affine(geom, :scale, 0, 0, :scale, -:x0 * :scale, -:y1 * :scale) AS geom
          FROM checked
    )
    SELECT f.code,
           f.invalid_edges,
           f.overlap_m2,
           ST_AsSVG(f.geom, 0, 1) AS path,
           ST_Area(f.geom) AS area,
           ST_X(label.geom) AS label_x,
           -ST_Y(label.geom) AS label_y
      FROM fitted f,
           LATERAL (
               SELECT ST_PointOnSurface(d.geom) AS geom
                 FROM ST_Dump(f.geom) AS d
                ORDER BY ST_Area(d.geom) DESC
                LIMIT 1
           ) AS label
     ORDER BY f.ord
    """
)

# Step 5: the Thames, clipped with its channel filled in. OS Open Rivers names
# some stretches by reach, so Upper Pool and Woolwich Reach and the unnamed
# tidal segments joining them are included.
THAMES_SQL = text(
    """
    WITH outline AS (SELECT ST_GeomFromEWKB(:outline) AS geom),
    named AS (
        SELECT ST_Union(ST_Transform(geom, 27700)) AS geom
          FROM staging.rivers
         WHERE watercourse_name IN ('River Thames', 'Upper Pool', 'Woolwich Reach')
    ),
    river AS (
        SELECT ST_Union(n.geom, coalesce(ST_Union(ST_Transform(u.geom, 27700)), n.geom)) AS geom
          FROM named n
          LEFT JOIN staging.rivers u
            ON u.watercourse_name IS NULL
           AND u.form = 'tidalRiver'
           AND ST_DWithin(ST_StartPoint(ST_Transform(u.geom, 27700)), n.geom, 5)
           AND ST_DWithin(ST_EndPoint(ST_Transform(u.geom, 27700)), n.geom, 5)
         GROUP BY n.geom
    ),
    clip AS (
        SELECT ST_Union(o.geom,
                        ST_Intersection(ST_Buffer(ST_Buffer(o.geom, :close_m), -:close_m),
                                        ST_Buffer(r.geom, :close_m))) AS geom
          FROM outline o, river r
    )
    SELECT ST_AsSVG(
               ST_Affine(
                   ST_SimplifyPreserveTopology(
                       ST_LineMerge(ST_CollectionExtract(ST_Intersection(r.geom, c.geom), 2)),
                       :simplify_m),
                   :scale, 0, 0, :scale, -:x0 * :scale, -:y1 * :scale),
               0, 1) AS path
      FROM river r, clip c
    """
)

EXTENT_SQL = text(
    """
    SELECT ST_XMin(g) AS x0, ST_YMin(g) AS y0, ST_XMax(g) AS x1, ST_YMax(g) AS y1
      FROM (SELECT ST_GeomFromEWKB(:outline) AS g) AS o
    """
)

AREA_KM2_SQL = text(
    """
    SELECT u.code, ST_Area(ST_GeomFromEWKB(u.ewkb)) / 1e6 AS km2
      FROM unnest(CAST(:codes AS text[]), CAST(:geoms AS bytea[])) AS u(code, ewkb)
    """
)


def letters_of(code: str) -> str:
    """Return the leading letters of a postcode code."""
    return re.match(r"[A-Z]+", code).group(0)


def read_placemarks(kml_fp: Path) -> tuple[list[str], list[str]]:
    """
    Read the KML placemarks as parallel lists.

    Returns
    -------
    areas : list of str
        The area each polygon belongs to.
    polygons : list of str
        Each polygon as a standalone KML element for ``ST_GeomFromKML``.
    """
    ET.register_namespace("", KML_NS)
    codes: list[str] = []
    kmls: list[str] = []
    names: set[str] = set()
    for placemark in ET.parse(kml_fp).getroot().iter(f"{{{KML_NS}}}Placemark"):
        name = placemark.findtext(f"{{{KML_NS}}}name", "").strip().upper()
        names.add(name)
        for polygon in placemark.iter(f"{{{KML_NS}}}Polygon"):
            codes.append(area_of(name))
            kmls.append(ET.tostring(polygon, encoding="unicode"))
    log.info("KML: %d placemarks, %d polygons", len(names), len(kmls))
    return codes, kmls


def read_outer_sites(postcodes_fp: Path) -> tuple[list[float], list[float], list[str]]:
    """
    Read in-use postcodes outside the inner areas, one site per distinct position.

    Returns
    -------
    longitudes, latitudes : list of float
        Site positions.
    letters : list of str
        Each site's postcode-area letters.
    """
    lons: list[float] = []
    lats: list[float] = []
    letters: list[str] = []
    seen: set[tuple[str, str]] = set()
    with postcodes_fp.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["In Use?"] != "Yes" or not row["Latitude"] or not row["Longitude"]:
                continue
            prefix = letters_of(row["Postcode"])
            position = (row["Latitude"], row["Longitude"])
            if prefix in INNER_LETTERS or position in seen:
                continue
            seen.add(position)
            lons.append(float(row["Longitude"]))
            lats.append(float(row["Latitude"]))
            letters.append(prefix)
    log.info("postcodes: %d outer sites", len(lons))
    return lons, lats, letters


def compact_svg(path: str) -> str:
    """Compact ``ST_AsSVG`` output: ``"M 1 2 L 3 4 Z"`` becomes ``"M1 2L3 4Z"``."""
    return re.sub(r"\s*([MLZ])\s*", r"\1", path).strip()


def render_ts(view_box: str, shapes: list[dict], thames: str) -> str:
    """Render the TypeScript module holding the viewBox, shapes and Thames path."""
    entries = "\n".join(
        "  {\n"
        f"    code: {json.dumps(shape['code'])},\n"
        f"    label: {json.dumps(shape['label'])},\n"
        f"    path: {json.dumps(shape['path'])},\n"
        f"    labelX: {shape['label_x']:.1f},\n"
        f"    labelY: {shape['label_y']:.1f},\n"
        "  },"
        for shape in shapes
    )
    return f"""// AUTO-GENERATED by `python -m hpldn.backend.scripts.build_london_map`. Do not edit.
//
// Greater London by postcode district: one shape per area the page covers
// (backend/api/market/districts.py), plus the uncovered leftovers inside
// London. Inner districts from Doogal's postcode district polygons
// (doogal.co.uk), outer postcode areas from Voronoi cells around Doogal's
// in-use postcodes, clipped to the ONS LSOA 2011 boundaries; the Thames from
// OS Open Rivers. Contains public sector information licensed under the Open
// Government Licence v3.0, and OS data (c) Crown copyright and database
// right. Simplified for display, so the outlines are indicative.
import type {{ DistrictShape }} from './types';

export const MAP_VIEW_BOX = {json.dumps(view_box)};

export const DISTRICT_SHAPES: DistrictShape[] = [
{entries}
];

export const DISTRICT_SHAPE_BY_CODE: Record<string, DistrictShape> = DISTRICT_SHAPES.reduce(
  (map, shape) => {{
    map[shape.code] = shape;
    return map;
  }},
  {{}} as Record<string, DistrictShape>,
);

/** The Thames through Greater London. */
export const THAMES_PATH = {json.dumps(thames)};
"""


def build(connection) -> str:
    """
    Build the map on `connection` and return the TypeScript source.

    Raises
    ------
    SystemExit
        If the shapes are not an exact coverage (invalid edges or overlap) or
        a covered area has no shape.
    """
    outline = connection.execute(OUTLINE_SQL).scalar_one()
    extent = connection.execute(EXTENT_SQL, {"outline": outline}).mappings().one()
    scale = VIEW_WIDTH / (extent["x1"] - extent["x0"])
    height = round((extent["y1"] - extent["y0"]) * scale)
    fit = {"scale": scale, "x0": extent["x0"], "y1": extent["y1"]}

    kml_codes, kmls = read_placemarks(KML_FP)
    inner = connection.execute(
        INNER_SQL, {"codes": kml_codes, "kmls": kmls, "outline": outline}
    ).all()
    log.info("inner: %d shapes", len(inner))

    lons, lats, letters = read_outer_sites(POSTCODES_FP)
    outer = connection.execute(
        OUTER_SQL,
        {
            "lons": lons,
            "lats": lats,
            "letters": letters,
            "outline": outline,
            "inner": [row.geom for row in inner],
        },
    ).all()
    log.info("outer: %d shapes", len(outer))

    geoms = {row.code: row.geom for row in [*inner, *outer]}
    areas = dict(
        connection.execute(
            AREA_KM2_SQL, {"codes": list(geoms), "geoms": list(geoms.values())}
        ).all()
    )
    for code, km2 in sorted(areas.items()):
        if code not in DISTRICTS and km2 < MIN_UNCOVERED_KM2:
            log.info("dropped uncovered %s: %.3f km2", code, km2)
            del geoms[code]

    order = list(DISTRICTS) + sorted(code for code in geoms if code not in DISTRICTS)
    codes = [code for code in order if code in geoms]
    rows = connection.execute(
        FINISH_SQL,
        {
            "codes": codes,
            "ords": list(range(len(codes))),
            "geoms": [geoms[code] for code in codes],
            "gap_m": GAP_FILL_M,
            "min_part_m2": MIN_PART_M2,
            "simplify_m": SIMPLIFY_M,
            **fit,
        },
    ).mappings().all()

    shapes = [
        {
            "code": row["code"],
            "label": DISTRICTS.get(row["code"], row["code"]),
            "path": compact_svg(row["path"]),
            "label_x": row["label_x"],
            "label_y": row["label_y"],
        }
        for row in rows
    ]
    invalid = [row["code"] for row in rows if row["invalid_edges"]]
    overlap_m2 = rows[0]["overlap_m2"] if rows else 0.0
    if invalid or overlap_m2 > 1.0:
        raise SystemExit(
            f"the shapes are not a clean coverage: invalid edges in {invalid}, "
            f"{overlap_m2:,.0f} m2 of overlap"
        )
    log.info("coverage: no invalid edges, %.3f m2 of overlap", overlap_m2)
    missing = [code for code in DISTRICTS if code not in {shape["code"] for shape in shapes}]
    if missing:
        raise SystemExit(f"no shape for {len(missing)} covered areas: {', '.join(missing)}")
    uncovered = [shape["code"] for shape in shapes if shape["code"] not in DISTRICTS]
    log.info("shapes: %d covered, uncovered: %s", len(shapes) - len(uncovered), ", ".join(uncovered))

    thames = connection.execute(
        THAMES_SQL, {"outline": outline, "close_m": RIVER_CLOSE_M, "simplify_m": SIMPLIFY_M, **fit}
    ).scalar_one()
    return render_ts(f"0 0 {VIEW_WIDTH} {height}", shapes, compact_svg(thames))


def main() -> None:
    """
    Build the map in a READ ONLY transaction and write it to ``--out``.

    Raises
    ------
    SystemExit
        If ``POSTGRES_DEVELOP_*`` is unset or the output exceeds
        `SIZE_BUDGET_BYTES`.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_FP, help="where to write londonGeo.ts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    load_env_vars()
    url = _url_from_env_prefix(DEVELOP_DB_ENV_PREFIX)
    if url is None:
        raise SystemExit("POSTGRES_DEVELOP_USERNAME/_HOST/_DATABASE are not set (develop.env)")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                source = build(connection)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    size = len(source.encode("utf-8"))
    if size > SIZE_BUDGET_BYTES:
        raise SystemExit(f"londonGeo.ts would be {size:,} bytes, over the {SIZE_BUDGET_BYTES:,} budget")
    args.out.write_text(source, encoding="utf-8")
    log.info("wrote %s (%s bytes)", args.out, f"{size:,}")


if __name__ == "__main__":
    main()
