"""
Link London Price Paid sales to real UPRNs from open evidence (migrations 0027-0029).

A home can be suggested by ``GET /addresses/search`` and priced only if it
has a ``production.address_table`` row, and every row hangs off a real UPRN
(OS Open UPRN, ``staging.ukhcs_table``). 0005-0025 held 2.97M of the 3.72M
London standard sales of 1995-2026; the rest had no link any tier could
make. This module links more of them, **only to real UPRNs**, and records
per sale which evidence did it:

- ``staging.ppd_uprn_link`` -- the ledger: one row per linked sale
  (transactionid, uprn, method, evidence). Every tier writes here and only
  here; `carry_links_into_staging` then mints address rows and inserts the
  sales, carrying ``transactionid`` and ``link_method`` onto
  ``ppd_table``.
- ``staging.ppd_unlinked`` -- every eligible sale still without a link, with
  the reason (``no_candidate``, ``ambiguous``, ``conflict``,
  ``postcode_mismatch``, ``terminated_postcode``, ``not_in_open_uprn``,
  ``below_precision``).

Tiers, strongest evidence first (the ledger keeps the first):

====  =================  ==============================================================
L1    ``hmlr``           HM Land Registry's own PPD->UPRN lookup, by transactionid
L2    ``ucl``            UCL's PPD<->EPC link (``staging.ukhpucl."UPRN"``), by transactionid
L3    ``epc_*``          the EPC register: exact address, token set, or house/flat numbers
                         (alphanumeric, ranges) with the street and every building-name
                         word contained
L5    ``osm_*``          an OpenStreetMap address (house number + street) on a building
                         polygon holding exactly one UPRN, or a point whose nearest UPRN's
                         building TOID holds only that UPRN; houses only.
                         ``osm_polygon_isolated``: an outline whose UPRN has no unaddressed
                         UPRN within `ISOLATION_RADIUS_M` (0029)
L7    ``planning_*``     GLA Planning London Datahub application addresses, as L3
L6    ``*_elimination``  the only UPRN in the sale's postcode and street (USRN) no source
                         has an address for, when the sale's is the only unplaced address
                         there; flats: the same within one building TOID
L9    ``flat_sequence``  a flat of a building whose known flats all sit at UPRN = offset +
                         flat number (at least `FLAT_SEQUENCE_MIN_FLATS`); 0029
L9    ``house_bracket``  a house whose neighbours n-2 and n+2 have UPRNs at most
                         `HOUSE_BRACKET_MAX_GAP` apart with one unassigned UPRN between; 0029
L8    ``term_*``         sales in terminated postcodes: L2/L3/L5 evidence within 250 m of
                         the old postcode centroid
L4    ``group``          every other sale of the same address (postcode + normalised
                         saon/paon/street) takes the one UPRN its linked or held sales have,
                         unless a later new-build sale says the address was rebuilt
====  =================  ==============================================================

Every link must resolve to exactly one UPRN that is in OS Open UPRN and (L8
aside) in the sale's own postcode according to ONS NSUL. If the candidates of
one address group disagree with each other or with the group's held sales,
the whole group goes to ``ppd_unlinked`` as ``conflict``.

Every tier is gated by `hpldn.backend.scripts.link_precision`: it writes
links only if its precision against ground truth is at least
`MIN_TIER_PRECISION` over at least `MIN_TIER_SAMPLE` sales.

UPRN-less certificates (0029): `resolve_uprnless_certificates` gives a London
certificate lodged without a UPRN the UPRN of a held sale with the same
address text (``cert_text``), or places it by the L9 rules
(``cert_flat_sequence``, ``cert_house_bracket``), into `RESOLVED_TABLE`. The
resolved certificates join ``staging.epc_link_gazetteer``, so the ``epc_*``
tiers can link other sales of those homes, and stop counting as unplaced
addresses for elimination.

Licences: NSUL, OS LIDS, OS Open Roads, OS Open UPRN, the Datahub and the HMLR
lookup are OGL. OpenStreetMap is ODbL: every link it made carries a method
starting ``osm_`` (or ``term_osm``), and `record_odbl_links` copies those links,
and the ``group`` links propagated from them, into `ODBL_TABLE`. Dropping the
ODbL-derived sales is one statement per table::

    DELETE FROM staging.ppd_table p USING staging.ppd_uprn_link_odbl o WHERE p.transactionid = o.transactionid;
    DELETE FROM production.ppd_table p USING staging.ppd_uprn_link_odbl o WHERE p.transactionid = o.transactionid;
    DELETE FROM staging.ppd_uprn_link l USING staging.ppd_uprn_link_odbl o WHERE l.transactionid = o.transactionid;

(then rebuild the per-sale features and ``production.london_houses`` as 0020 does).
"""

import logging
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import Engine, text

from hpldn.backend.common.tuning import apply_bulk_session_tuning, get_bulk_copy_env
from hpldn.backend.scripts.migration_helpers import get_psql_env, get_psql_url

# The precision gate (see hpldn.backend.scripts.link_precision).
MIN_TIER_PRECISION = 0.995
MIN_TIER_SAMPLE = 2000

LEDGER_TABLE = "staging.ppd_uprn_link"
UNLINKED_TABLE = "staging.ppd_unlinked"
TARGET_TABLE = "staging.ppd_link_target"
CANDIDATE_TABLE = "staging.ppd_link_candidate"
PRECISION_TABLE = "staging.ppd_link_precision"
ODBL_TABLE = "staging.ppd_uprn_link_odbl"
RESOLVED_TABLE = "staging.epc_uprn_resolved"
# UPRNs some source gives an address to (see `build_assigned_uprns`).
ASSIGNED_TABLE = "staging.link_assigned_uprn"

# Methods, in the order the ledger prefers them. The harness gates each one.
TIER_METHODS: Dict[str, Tuple[str, ...]] = {
    "L1": ("hmlr",),
    "L2": ("ucl",),
    "L3": ("epc_exact", "epc_tokenset", "epc_numsig"),
    "L5": ("osm_polygon", "osm_point", "osm_polygon_isolated"),
    "L7": ("planning_exact", "planning_tokenset", "planning_numsig", "osm_planning"),
    "L6": ("street_elimination", "building_elimination"),
    "L9": ("flat_sequence", "house_bracket"),
    "L8": ("term_ucl", "term_epc", "term_osm"),
    "L4": ("group",),
}
METHOD_ORDER: Tuple[str, ...] = tuple(m for methods in TIER_METHODS.values() for m in methods)

# Methods whose UPRN is dropped (``n_uprns`` set to 0, reason ``vetoed``) when
# another source already gives that UPRN an address with other numbers: the
# precision harness found their errors are next-door homes (an OSM outline offset onto the
# neighbour's UPRN point, a planning application filed against the adjoining
# property's UPRN). UCL's errors on the unlinked sales are mostly the parent
# building's UPRN for a flat ("94 PARK LANE" for "FLAT B, 94").
VETOED_METHODS = ("ucl", "osm_polygon", "osm_point", "planning_exact", "planning_tokenset", "planning_numsig")
KNOWN_ADDRESS_TABLE = "staging.link_known_address"

# Terminated postcodes: evidence must lie this close to the old centroid.
TERMINATED_RADIUS_M = 250
# An OSM address point takes the building TOID of the nearest UPRN this close.
OSM_POINT_SNAP_M = 15
# osm_polygon_isolated: no unaddressed UPRN this close to the outline's UPRN.
# The precision harness found every osm_polygon error was the outline's UPRN
# standing 3-30 m from the home's real, unaddressed one; at 10 m the rest
# measured 99.8 %.
ISOLATION_RADIUS_M = 10
# flat_sequence: known flats of the building that must share one offset.
FLAT_SEQUENCE_MIN_FLATS = 4
# house_bracket: the neighbours' UPRNs at most this far apart (wider gaps
# measured 97 % or lower).
HOUSE_BRACKET_MAX_GAP = 4

# The London standard sales every revision since 0017 counts.
ELIGIBLE_SALE_PREDICATE = """
    p.county = 'GREATER LONDON'
    AND p.categorytype = 'A'
    AND p.recordstatus = 'A'
    AND p.propertytype IN ('D','S','T','F')
    AND p.duration IN ('F','L')
    AND p.price > 0
"""

# ---------------------------------------------------------------------------
# Key functions
# ---------------------------------------------------------------------------

# staging.hpldn_numsig drops "23A" and every lettered flat, so "23A Cromford
# Road" had no numeric signature at all. This one keeps alphanumeric numbers,
# joins "23 A" into "23A", and keeps a single-letter flat id ("FLAT A").
# Ranges already agree ("55-59" and "55 - 59" both key to "55 59").
# staging.hpldn_alpha_tokens: the words (2+ letters) of an address key, so a
# numeric match can require every building-name word of the sale to appear
# in the certificate ("FLAT 1 FLORENCE HOUSE" must not take "FLAT 1 SIDNEY
# HOUSE" in the same postcode). A numeric match also needs the numbers in the
# same order (staging.hpldn_numseq) and the same part of the building
# (staging.hpldn_same_unit): on the unlinked sales the sorted signature took
# house sales onto "GROUND FLOOR F 19" / "ANNEXE REAR OF 25" certificates and
# "FLAT 3, 5" onto "FLAT 5, 3".
LINK_FUNCTIONS_SQL = r"""
CREATE OR REPLACE FUNCTION staging.hpldn_numsig_alnum(addrkey text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT array_to_string(array(
        SELECT x.t
        FROM (
            SELECT u.t, lag(u.t) OVER (ORDER BY u.i) AS prev
            FROM unnest(string_to_array(
                regexp_replace(coalesce(addrkey, ''), '(^| )([0-9]+) ([A-H])(?= |$)', '\1\2\3', 'g'),
                ' '
            )) WITH ORDINALITY AS u(t, i)
        ) x
        WHERE x.t ~ '^([0-9]+[A-Z]?|[A-Z][0-9]+[A-Z]?)$'
           OR (x.t ~ '^[A-Z]$' AND x.prev = 'F')
        ORDER BY x.t
    ), ' ')
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_alpha_tokens(addrkey text) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(DISTINCT t ORDER BY t), '{}')
    FROM unnest(string_to_array(coalesce(addrkey, ''), ' ')) AS t
    WHERE t ~ '^[A-Z]{2,}$'
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_numseq(addrkey text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- hpldn_numsig_alnum's numbers in address order: "F 3 5" is flat 3 of 5,
    -- "F 5 3" flat 5 of 3.
    SELECT array_to_string(array(
        SELECT x.t
        FROM (
            SELECT u.t, u.i, lag(u.t) OVER (ORDER BY u.i) AS prev
            FROM unnest(string_to_array(
                regexp_replace(coalesce(addrkey, ''), '(^| )([0-9]+) ([A-H])(?= |$)', '\1\2\3', 'g'),
                ' '
            )) WITH ORDINALITY AS u(t, i)
        ) x
        WHERE x.t ~ '^([0-9]+[A-Z]?|[A-Z][0-9]+[A-Z]?)$'
           OR (x.t ~ '^[A-Z]$' AND x.prev = 'F')
        ORDER BY x.i
    ), ' ')
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_numparts(numsig text) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- The digit runs and letter runs of a numeric signature: "100C" and
    -- "F C 100" ("100 C") both give {100, C}.
    SELECT coalesce(array_agg(DISTINCT m[1] ORDER BY m[1]), '{}')
    FROM regexp_matches(coalesce(numsig, ''), '([0-9]+|[A-Z]+)', 'g') AS m
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_unit_markers(addrkey text) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- What part of a building an address names: a flat, an annexe, a coach
    -- house, land. Flat words collapse to "F".
    SELECT coalesce(array_agg(DISTINCT m ORDER BY m), '{}')
    FROM (
        SELECT CASE
                   WHEN t IN ('FLAT', 'APARTMENT', 'APT', 'MAISONETTE', 'PENTHOUSE', 'STUDIO') THEN 'F'
                   WHEN t = 'ANNEX' THEN 'ANNEXE'
                   ELSE t
               END AS m
        FROM unnest(string_to_array(coalesce(addrkey, ''), ' ')) AS t
        WHERE t IN ('F', 'FLAT', 'APARTMENT', 'APT', 'MAISONETTE', 'PENTHOUSE', 'STUDIO', 'ANNEX',
                    'ANNEXE', 'OUTBUILDING', 'COACH', 'COTTAGE', 'REAR', 'GARAGE', 'WORKSHOP', 'LODGE',
                    'BARN', 'STABLE', 'LAND', 'SITE', 'PLOT', 'ADJOINING', 'ADJACENT', 'PART', 'UNIT', 'ROOM')
    ) s
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_has_unit(addrkey text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- Any marker, floors included: "FIRST FLOOR FRONT 23" is not the house 23.
    -- (Not UPPER/LOWER: street words, and "UPPER FLAT" has FLAT anyway.)
    SELECT cardinality(staging.hpldn_unit_markers(addrkey)) > 0
        OR coalesce(addrkey, '') ~ '(^| )(FLOOR|FLOORS|BASEMENT)( |$)'
$$;

-- A certificate address names the same part of a building as the sale's:
-- nothing when the sale names nothing, else the same markers. A flat sold
-- as "6B" names no unit word, but "F B 6" is the same home.
DROP FUNCTION IF EXISTS staging.hpldn_same_unit(text, text);
CREATE OR REPLACE FUNCTION staging.hpldn_same_unit(sale_key text, other_key text, sale_is_flat boolean DEFAULT false)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN NOT staging.hpldn_has_unit(sale_key) THEN
            NOT staging.hpldn_has_unit(other_key)
            OR (sale_is_flat AND staging.hpldn_unit_markers(other_key) <@ ARRAY['F'])
        ELSE staging.hpldn_unit_markers(sale_key) = staging.hpldn_unit_markers(other_key)
    END
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_flat_no(saon text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- The flat number of a Price Paid SAON ("3", "FLAT 3", "APARTMENT 3"), else NULL.
    SELECT (regexp_match(upper(btrim(coalesce(saon, ''))), '^(FLAT|APARTMENT|APT)?\s*([0-9]{1,5})$'))[2]::integer
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_house_no(raw text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- A plain house number ("12", not "12A" or "12-14"), else NULL.
    SELECT CASE WHEN btrim(coalesce(raw, '')) ~ '^[0-9]{1,4}$' THEN btrim(raw)::integer END
$$;

CREATE OR REPLACE FUNCTION staging.hpldn_housenumber(raw text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- "12a" / "12 A" -> "12A"; "12-14" / "12 - 14" -> "12-14"; else NULL.
    SELECT CASE
        WHEN n ~ '^[0-9]+[A-Z]?(-[0-9]+[A-Z]?)?$' THEN n
    END
    FROM (SELECT upper(regexp_replace(coalesce(raw, ''), '\s+', '', 'g')) AS n) s
$$;
"""


# ---------------------------------------------------------------------------
# Source loads (each rebuilds its staging tables)
# ---------------------------------------------------------------------------


def _emit(log: Optional[logging.Logger], msg: str, *args) -> None:
    if log is not None:
        log.info(msg, *args)


def _psql_copy(engine: Engine, copy_sql: str, log) -> None:
    """Run one ``\\copy`` through psql, as the multi-GB loads of 0003/0025 do."""
    env = get_psql_env(engine, get_bulk_copy_env(log))
    subprocess.run(
        ["psql", get_psql_url(engine), "-v", "ON_ERROR_STOP=1", "-c", copy_sql],
        check=True,
        env=env,
    )


def _ogr_pg_connection(engine: Engine) -> str:
    """Return the engine's database as an ogr2ogr ``PG:`` connection string."""
    url = engine.url
    parts = [f"dbname={url.database}", f"user={url.username}"]
    if url.host:
        parts.append(f"host={url.host}")
    if url.port:
        parts.append(f"port={url.port}")
    if url.password:
        parts.append(f"password={url.password}")
    return "PG:" + " ".join(parts)


def _ogr2ogr(engine: Engine, source: Path, layer: str, table: str, *args: str, env=None) -> None:
    """Load one OGR layer into `table` (EPSG:27700, no spatial index)."""
    import os

    result = subprocess.run(
        [
            "ogr2ogr", "-f", "PostgreSQL", _ogr_pg_connection(engine), str(source), layer,
            "-nln", table, "-overwrite",
            "-lco", "GEOMETRY_NAME=geom", "-lco", "SPATIAL_INDEX=NONE",
            "-t_srs", "EPSG:27700", "-gt", "65536", *args,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr {source.name}:{layer} -> {table} failed: {result.stderr}")


def load_nsul(engine: Engine, zip_fp: Path, cfg=None, log=None) -> int:
    """
    Load the London file of ONS NSUL into ``staging.nsul_london``.

    One row per London UPRN: its postcode (NULL for the ~110K UPRNs NSUL gives
    none), LSOA21 and OS Open UPRN point. The postcode is the tiers' guard:
    a link is kept only if the UPRN is in the sale's postcode.

    Returns
    -------
    int
        UPRNs loaded.
    """
    with zipfile.ZipFile(zip_fp) as archive:
        members = [n for n in archive.namelist() if n.endswith("_LN.csv")]
    if len(members) != 1:
        raise RuntimeError(f"{zip_fp} should hold one Data/NSUL_*_LN.csv, found {members}")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging._nsul_raw"))
        connection.execute(
            text("CREATE UNLOGGED TABLE staging._nsul_raw (uprn bigint, pcds text, lsoa21cd text)")
        )
    # Columns 1, 4 and 18: UPRN, PCDS, LSOA21CD.
    program = f'unzip -p "{zip_fp}" "{members[0]}" | tail -n +2 | cut -d, -f1,4,18'
    _psql_copy(
        engine,
        f"\\copy staging._nsul_raw FROM PROGRAM '{program}' WITH (FORMAT CSV)",
        log,
    )
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS staging.nsul_london"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.nsul_london AS
                SELECT n.uprn,
                       NULLIF(n.pcds, '') AS postcode,
                       NULLIF(staging.hpldn_pcnorm(NULLIF(n.pcds, '')), '') AS postcode_norm,
                       NULLIF(n.lsoa21cd, '') AS lsoa21cd,
                       ST_SetSRID(ST_MakePoint(uk."X_COORDINATE", uk."Y_COORDINATE"), 27700) AS geom
                FROM staging._nsul_raw n
                LEFT JOIN staging.ukhcs_table uk ON uk."UPRN" = n.uprn
                """
            )
        )
        connection.execute(text("ALTER TABLE staging.nsul_london ADD PRIMARY KEY (uprn)"))
        connection.execute(text("CREATE INDEX ON staging.nsul_london (postcode_norm)"))
        connection.execute(text("CREATE INDEX ON staging.nsul_london USING gist (geom)"))
        connection.execute(text("DROP TABLE staging._nsul_raw"))
        connection.execute(text("ANALYZE staging.nsul_london"))
        rows, postcodes = connection.execute(
            text("SELECT count(*), count(DISTINCT postcode_norm) FROM staging.nsul_london")
        ).one()
    _emit(log, "sale_linking: staging.nsul_london holds %d London UPRN(s) in %d postcode(s) (%s)",
          rows, postcodes, zip_fp.name)
    return rows


# (LIDS file stem, raw table, column definitions) -- IDENTIFIER_1 and _2 only.
_LIDS_FILES = (
    ("BLPU_UPRN_Street_USRN_11", "staging._lids_uprn_usrn", "uprn bigint, usrn bigint"),
    ("BLPU_UPRN_TopographicArea_TOID_5", "staging._lids_uprn_toid", "uprn bigint, toid text"),
    ("ORRoadLink_GUID_RoadLink_TOID_12", "staging._lids_guid_roadlink", "guid text, roadlink_toid text"),
    ("RoadLink_TOID_Street_USRN_8", "staging._lids_roadlink_usrn", "roadlink_toid text, usrn bigint"),
)


def _lids_zip(lids_dir: Path, stem: str) -> Path:
    suffix = stem.replace("_", "-")
    matches = sorted(lids_dir.glob(f"lids-*_csv_{suffix}.zip"))
    if not matches:
        raise FileNotFoundError(f"no lids-*_csv_{suffix}.zip in {lids_dir}")
    return matches[-1]


def load_lids_and_street_names(
    engine: Engine, lids_dir: Path, open_roads_gpkg: Path, cfg=None, log=None
) -> Dict[str, int]:
    """
    Load OS Open Linked Identifiers and OS Open Roads street names for London UPRNs.

    Builds ``staging.uprn_usrn`` (UPRN -> street USRN), ``staging.uprn_toid``
    (UPRN -> building TOID) and ``staging.usrn_street`` (USRN -> normalised
    street name, from Open Roads ``name_1``/``name_2`` through the LIDS
    RoadLink crosswalk). Needs ``staging.nsul_london`` for the London filter.
    """
    loads: Dict[str, int] = {}
    for stem, table, columns in _LIDS_FILES:
        zip_fp = _lids_zip(lids_dir, stem)
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
            connection.execute(text(f"CREATE UNLOGGED TABLE {table} ({columns})"))
        program = f'unzip -p "{zip_fp}" "{stem}.csv" | tail -n +2 | cut -d, -f2,5'
        _psql_copy(engine, f"\\copy {table} FROM PROGRAM '{program}' WITH (FORMAT CSV)", log)

    # Open Roads links inside a London bounding box, named only.
    _ogr2ogr(
        engine, open_roads_gpkg, "road_link", "staging._oproad_link",
        "-select", "id,name_1,name_2",
        "-spat", "500000", "150000", "565000", "205000",
        "-where", "name_1 IS NOT NULL",
    )

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        for table in ("staging.uprn_usrn", "staging.uprn_toid", "staging.usrn_street"):
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.uprn_usrn AS
                SELECT DISTINCT l.uprn, l.usrn
                FROM staging._lids_uprn_usrn l
                JOIN staging.nsul_london n ON n.uprn = l.uprn
                """
            )
        )
        connection.execute(text("CREATE INDEX ON staging.uprn_usrn (uprn)"))
        connection.execute(text("CREATE INDEX ON staging.uprn_usrn (usrn)"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.uprn_toid AS
                SELECT DISTINCT ON (l.uprn) l.uprn, l.toid
                FROM staging._lids_uprn_toid l
                JOIN staging.nsul_london n ON n.uprn = l.uprn
                ORDER BY l.uprn, l.toid
                """
            )
        )
        connection.execute(text("ALTER TABLE staging.uprn_toid ADD PRIMARY KEY (uprn)"))
        connection.execute(text("CREATE INDEX ON staging.uprn_toid (toid)"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.usrn_street AS
                SELECT DISTINCT ru.usrn, staging.hpldn_addrkey(v.name) AS street_norm
                FROM staging._oproad_link r
                JOIN staging._lids_guid_roadlink g ON g.guid = r.id
                JOIN staging._lids_roadlink_usrn ru ON ru.roadlink_toid = g.roadlink_toid
                CROSS JOIN LATERAL (VALUES (r.name_1), (r.name_2)) AS v(name)
                WHERE v.name IS NOT NULL AND btrim(v.name) <> ''
                """
            )
        )
        connection.execute(text("CREATE INDEX ON staging.usrn_street (street_norm, usrn)"))
        connection.execute(text("CREATE INDEX ON staging.usrn_street (usrn)"))
        for _, table, _ in _LIDS_FILES:
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("DROP TABLE staging._oproad_link"))
        for table in ("staging.uprn_usrn", "staging.uprn_toid", "staging.usrn_street"):
            connection.execute(text(f"ANALYZE {table}"))
            loads[table] = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    _emit(log, "sale_linking: LIDS / Open Roads loaded: %s", loads)
    return loads


def load_osm_addresses(engine: Engine, pbf_fp: Path, osm_config: Path, cfg=None, log=None) -> int:
    """
    Load OpenStreetMap addresses and resolve each to at most one UPRN (``staging.osm_address``).

    Polygons (addressed buildings) resolve to the UPRN when they contain
    exactly one OS Open UPRN point. Points resolve through the building TOID
    of the nearest UPRN within `OSM_POINT_SNAP_M`, when that TOID carries only
    that UPRN. ``n_uprns`` keeps the count, so the tier can tell an ambiguous
    object from an unresolved one. ODbL: see the module docstring.

    Returns
    -------
    int
        Address objects loaded.
    """
    env = {"OSM_CONFIG_FILE": str(osm_config), "OSM_USE_CUSTOM_INDEXING": "NO"}
    where = "addr_housenumber IS NOT NULL"
    _ogr2ogr(engine, pbf_fp, "multipolygons", "staging._osm_polygons", "-where", where, env=env)
    _ogr2ogr(engine, pbf_fp, "points", "staging._osm_points", "-where", where, env=env)
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS staging.osm_address"))
        connection.execute(
            text(
                f"""
                CREATE TABLE staging.osm_address AS
                WITH polygons AS (
                    SELECT 'way/' || coalesce(o.osm_way_id, o.osm_id) AS osm_id,
                           'polygon'::text AS kind, o.addr_housenumber, o.addr_street,
                           o.addr_postcode, c.n_uprns, c.uprn, c.postcodes
                    FROM staging._osm_polygons o
                    CROSS JOIN LATERAL (
                        SELECT count(*) AS n_uprns, min(n.uprn) AS uprn,
                               array_agg(DISTINCT n.postcode_norm) AS postcodes
                        FROM staging.nsul_london n
                        WHERE ST_Contains(o.geom, n.geom)
                    ) c
                ), points AS (
                    SELECT 'node/' || o.osm_id AS osm_id, 'point'::text AS kind,
                           o.addr_housenumber, o.addr_street, o.addr_postcode,
                           t.n_uprns, t.uprn, t.postcodes
                    FROM staging._osm_points o
                    LEFT JOIN LATERAL (
                        SELECT n.uprn
                        FROM staging.nsul_london n
                        WHERE ST_DWithin(n.geom, o.geom, {OSM_POINT_SNAP_M})
                        ORDER BY n.geom <-> o.geom
                        LIMIT 1
                    ) nearest ON true
                    LEFT JOIN staging.uprn_toid nt ON nt.uprn = nearest.uprn
                    LEFT JOIN LATERAL (
                        SELECT count(*) AS n_uprns, min(ut.uprn) AS uprn,
                               array_agg(DISTINCT n2.postcode_norm) AS postcodes
                        FROM staging.uprn_toid ut
                        JOIN staging.nsul_london n2 ON n2.uprn = ut.uprn
                        WHERE ut.toid = nt.toid
                    ) t ON true
                )
                SELECT osm_id, kind,
                       staging.hpldn_housenumber(addr_housenumber) AS housenumber,
                       NULLIF(staging.hpldn_addrkey(addr_street), '') AS street_norm,
                       NULLIF(staging.hpldn_pcnorm(addr_postcode), '') AS postcode_norm,
                       coalesce(n_uprns, 0) AS n_uprns,
                       CASE WHEN n_uprns = 1 THEN uprn END AS uprn,
                       coalesce(postcodes, '{{}}') AS nsul_postcodes
                FROM (SELECT * FROM polygons UNION ALL SELECT * FROM points) a
                WHERE staging.hpldn_housenumber(addr_housenumber) IS NOT NULL
                  AND addr_street IS NOT NULL
                """
            )
        )
        connection.execute(text("CREATE INDEX ON staging.osm_address (street_norm, housenumber)"))
        connection.execute(text("DROP TABLE staging._osm_polygons"))
        connection.execute(text("DROP TABLE staging._osm_points"))
        connection.execute(text("ANALYZE staging.osm_address"))
        rows, resolved = connection.execute(
            text("SELECT count(*), count(uprn) FROM staging.osm_address")
        ).one()
    _emit(log, "sale_linking: staging.osm_address holds %d OSM address(es), %d on one UPRN", rows, resolved)
    return rows


def load_planning_applications(engine: Engine, csv_fp: Path, cfg=None, log=None) -> int:
    """
    Load the Planning London Datahub export into ``staging.planning_link_gazetteer``.

    One row per (postcode, address key, UPRN); the postcode is the UPRN's NSUL
    postcode, since a quarter of applications carry none. UPRNs outside
    London's NSUL are dropped.

    Returns
    -------
    int
        Gazetteer rows.
    """
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging._planning_raw"))
        connection.execute(
            text(
                """
                CREATE UNLOGGED TABLE staging._planning_raw (
                    id text, uprn text, site_number text, site_name text, street_name text,
                    secondary_street_name text, postcode text, lpa_name text,
                    valid_date text, application_type text
                )
                """
            )
        )
    _psql_copy(
        engine,
        f"\\copy staging._planning_raw FROM '{csv_fp}' WITH (FORMAT CSV, HEADER TRUE)",
        log,
    )
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text("DROP TABLE IF EXISTS staging.planning_link_gazetteer"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.planning_link_gazetteer AS
                SELECT n.postcode_norm, k.k_exact,
                       staging.hpldn_tokenset(k.k_exact) AS k_tokenset,
                       staging.hpldn_numsig_alnum(k.k_exact) AS k_numsig_alnum,
                       string_to_array(staging.hpldn_tokenset(k.k_exact), ' ') AS k_tokens,
                       n.uprn, min(r.id) AS application_id
                FROM staging._planning_raw r
                JOIN staging.nsul_london n
                  ON r.uprn ~ '^[0-9]{1,12}$' AND n.uprn = r.uprn::bigint
                CROSS JOIN LATERAL (
                    SELECT staging.hpldn_addrkey(concat_ws(
                        ' ',
                        r.site_number,
                        CASE WHEN r.site_name IS DISTINCT FROM r.site_number THEN r.site_name END,
                        r.street_name
                    )) AS k_exact
                ) k
                WHERE n.postcode_norm IS NOT NULL AND k.k_exact <> ''
                GROUP BY 1, 2, 3, 4, 5, 6
                """
            )
        )
        for key in ("k_exact", "k_tokenset", "k_numsig_alnum"):
            connection.execute(text(f"CREATE INDEX ON staging.planning_link_gazetteer (postcode_norm, {key})"))
        connection.execute(text("DROP TABLE staging._planning_raw"))
        connection.execute(text("ANALYZE staging.planning_link_gazetteer"))
        rows, uprns = connection.execute(
            text("SELECT count(*), count(DISTINCT uprn) FROM staging.planning_link_gazetteer")
        ).one()
    _emit(log, "sale_linking: staging.planning_link_gazetteer holds %d row(s) over %d UPRN(s)", rows, uprns)
    return rows


def load_hmlr_lookups(engine: Engine, csv_fps: Sequence[Path], cfg=None, log=None) -> int:
    """
    Load every HMLR ``pp-uprn-lookup-*.csv`` into ``staging.hmlr_uprn_lookup``.

    Files are read oldest first by name order of their month; a later file's
    UPRN for a transaction replaces an earlier one's.

    Returns
    -------
    int
        Transactions held.
    """
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging._hmlr_raw"))
        connection.execute(
            text("CREATE UNLOGGED TABLE staging._hmlr_raw (transactionid text, uprn bigint, source_file text, seq int)")
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS staging.hmlr_uprn_lookup (
                    transactionid varchar PRIMARY KEY,
                    uprn bigint NOT NULL,
                    source_file varchar NOT NULL
                )
                """
            )
        )
    for seq, csv_fp in enumerate(csv_fps):
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS staging._hmlr_file"))
            connection.execute(text("CREATE UNLOGGED TABLE staging._hmlr_file (transactionid text, uprn bigint)"))
        _psql_copy(engine, f"\\copy staging._hmlr_file FROM '{csv_fp}' WITH (FORMAT CSV)", log)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO staging._hmlr_raw SELECT transactionid, uprn, :name, :seq "
                    "FROM staging._hmlr_file"
                ),
                {"name": csv_fp.name, "seq": seq},
            )
            connection.execute(text("DROP TABLE staging._hmlr_file"))
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO staging.hmlr_uprn_lookup (transactionid, uprn, source_file)
                SELECT DISTINCT ON (transactionid) transactionid, uprn, source_file
                FROM staging._hmlr_raw
                WHERE uprn IS NOT NULL
                ORDER BY transactionid, seq DESC
                ON CONFLICT (transactionid) DO UPDATE
                    SET uprn = EXCLUDED.uprn, source_file = EXCLUDED.source_file
                """
            )
        )
        connection.execute(text("DROP TABLE staging._hmlr_raw"))
        connection.execute(text("ANALYZE staging.hmlr_uprn_lookup"))
        rows = connection.execute(text("SELECT count(*) FROM staging.hmlr_uprn_lookup")).scalar_one()
    _emit(log, "sale_linking: staging.hmlr_uprn_lookup holds %d transaction(s) from %s",
          rows, ", ".join(p.name for p in csv_fps))
    return rows


def load_postcode_history(engine: Engine, csv_fp: Path, cfg=None, log=None) -> int:
    """
    Load London postcode centroids, live and terminated, into ``staging.postcode_centroid_history``.

    From Doogal's ONSPD-derived London postcode file (OGL): postcode, whether
    it is in use, and its 1 m centroid. L8 reads the terminated ones.

    Returns
    -------
    int
        Terminated postcodes.
    """
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging._doogal_raw"))
        connection.execute(
            text(
                "CREATE UNLOGGED TABLE staging._doogal_raw "
                "(postcode text, in_use text, easting text, northing text)"
            )
        )
    # Columns 1, 2, 5, 6: Postcode, In Use?, Easting, Northing. The file has
    # quoted fields with commas later in the row, so cut would miscount them;
    # csvcut is not guaranteed, so python's csv module does the projection.
    program = (
        'python3 -c "import csv,sys; r=csv.reader(open(sys.argv[1], encoding=\\"utf-8-sig\\")); '
        'next(r); w=csv.writer(sys.stdout); [w.writerow((x[0], x[1], x[4], x[5])) for x in r]" '
        f'"{csv_fp}"'
    )
    _psql_copy(engine, f"\\copy staging._doogal_raw FROM PROGRAM '{program}' WITH (FORMAT CSV)", log)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS staging.postcode_centroid_history"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.postcode_centroid_history AS
                SELECT DISTINCT ON (staging.hpldn_pcnorm(postcode))
                       staging.hpldn_pcnorm(postcode) AS postcode_norm,
                       in_use = 'Yes' AS in_use,
                       ST_SetSRID(ST_MakePoint(easting::float8, northing::float8), 27700) AS geom
                FROM staging._doogal_raw
                WHERE easting ~ '^[0-9.]+$' AND northing ~ '^[0-9.]+$'
                ORDER BY staging.hpldn_pcnorm(postcode), in_use = 'Yes' DESC
                """
            )
        )
        connection.execute(text("ALTER TABLE staging.postcode_centroid_history ADD PRIMARY KEY (postcode_norm)"))
        connection.execute(text("DROP TABLE staging._doogal_raw"))
        connection.execute(text("ANALYZE staging.postcode_centroid_history"))
        terminated = connection.execute(
            text("SELECT count(*) FROM staging.postcode_centroid_history WHERE NOT in_use")
        ).scalar_one()
    _emit(log, "sale_linking: staging.postcode_centroid_history holds %d terminated London postcode(s)", terminated)
    return terminated


def build_epc_link_gazetteer(engine: Engine, cfg=None, log=None) -> int:
    """
    Build ``staging.epc_link_gazetteer``: every London certificate address with a UPRN, keyed.

    Like 0006's ``staging.epc_addrkey`` plus the alphanumeric numeric
    signature and the token array. ``staging.epc_link_uprn_ok`` lists the
    (postcode, UPRN) pairs whose certificates all give one address, the
    guard 0006 applies to numeric matches. Certificates lodged without a
    UPRN that `resolve_uprnless_certificates` placed (`RESOLVED_TABLE`) join
    it keyed the same way, with ``resolved_method`` set; the register's own
    rows have it NULL, and only they make ``epc_link_uprn_ok``.

    Returns
    -------
    int
        Gazetteer rows.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(LINK_FUNCTIONS_SQL))
        connection.execute(text(RESOLVED_DDL))
        connection.execute(text("DROP TABLE IF EXISTS staging.epc_link_gazetteer"))
        connection.execute(
            text(
                f"""
                CREATE TABLE staging.epc_link_gazetteer AS
                SELECT staging.hpldn_pcnorm(e.postcode) AS postcode_norm, k.k_exact,
                       staging.hpldn_tokenset(k.k_exact) AS k_tokenset,
                       staging.hpldn_numsig_alnum(k.k_exact) AS k_numsig_alnum,
                       string_to_array(staging.hpldn_tokenset(k.k_exact), ' ') AS k_tokens,
                       coalesce(r.uprn, e.uprn) AS uprn, max(e.certificate_number) AS certificate_number,
                       min(r.method) AS resolved_method
                FROM staging.epc_domestic_history e
                LEFT JOIN {RESOLVED_TABLE} r ON r.certificate_number = e.certificate_number
                CROSS JOIN LATERAL (
                    SELECT staging.hpldn_addrkey(concat_ws(' ', e.address1, e.address2, e.address3)) AS k_exact
                ) k
                WHERE e.region = 'E12000007' AND coalesce(r.uprn, e.uprn) IS NOT NULL
                GROUP BY 1, 2, 3, 4, 5, 6, r.uprn IS NULL
                """
            )
        )
        for key in ("k_exact", "k_tokenset", "k_numsig_alnum"):
            connection.execute(text(f"CREATE INDEX ON staging.epc_link_gazetteer (postcode_norm, {key})"))
            connection.execute(text(f"CREATE INDEX ON staging.epc_link_gazetteer ({key})"))
        connection.execute(text("CREATE INDEX ON staging.epc_link_gazetteer (uprn)"))
        connection.execute(text("DROP TABLE IF EXISTS staging.epc_link_uprn_ok"))
        connection.execute(
            text(
                """
                CREATE TABLE staging.epc_link_uprn_ok AS
                SELECT postcode_norm, uprn
                FROM staging.epc_link_gazetteer
                WHERE resolved_method IS NULL
                GROUP BY postcode_norm, uprn
                HAVING count(DISTINCT k_exact) = 1
                """
            )
        )
        connection.execute(text("ALTER TABLE staging.epc_link_uprn_ok ADD PRIMARY KEY (postcode_norm, uprn)"))
        connection.execute(text("ANALYZE staging.epc_link_gazetteer"))
        connection.execute(text("ANALYZE staging.epc_link_uprn_ok"))
        rows, uprns, resolved = connection.execute(
            text(
                "SELECT count(*), count(DISTINCT uprn), count(*) FILTER (WHERE resolved_method IS NOT NULL) "
                "FROM staging.epc_link_gazetteer"
            )
        ).one()
    _emit(log, "sale_linking: staging.epc_link_gazetteer holds %d row(s) over %d UPRN(s), %d from resolved "
          "UPRN-less certificates", rows, uprns, resolved)
    return rows


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

# Keys for a set of sales `s` (columns of staging.ppd_domestic). Shared by the
# real run and the precision harness, so both key sales identically.
SALE_KEYS_SELECT = """
SELECT s.transactionid,
       staging.hpldn_pcnorm(s.postcode) AS pc,
       s.dateoftransfer::date AS dateoftransfer, s.price, s.propertytype, s.duration,
       s.oldnew, s.postcode, s.paon, s.saon, s.street, s.towncity,
       k.k_exact,
       staging.hpldn_tokenset(k.k_exact) AS k_tokenset,
       staging.hpldn_numsig_alnum(k.k_exact) AS k_numsig_alnum,
       staging.hpldn_alpha_tokens(staging.hpldn_addrkey(concat_ws(' ', s.saon, s.paon))) AS bldg_tokens,
       NULLIF(staging.hpldn_addrkey(s.street), '') AS street_norm,
       NULLIF(staging.hpldn_addrkey(s.paon), '') AS paon_norm,
       staging.hpldn_housenumber(s.paon) AS housenumber,
       (s.propertytype IN ('D','S','T') AND NULLIF(btrim(s.saon), '') IS NULL) AS is_house
FROM {source} s
CROSS JOIN LATERAL (
    SELECT staging.hpldn_addrkey(concat_ws(' ', s.saon, s.paon, s.street)) AS k_exact
) k
"""


def create_sale_keys_table(connection, table: str, source: str, extra_columns: str = "") -> int:
    """
    Create `table` from `source` (sales shaped like ``staging.ppd_domestic``) with the link keys.

    `extra_columns` are selected from ``s`` as well (the harness keeps its
    true UPRN this way).

    Returns
    -------
    int
        Rows.
    """
    select = SALE_KEYS_SELECT.format(source=source)
    if extra_columns:
        select = select.replace("FROM ", f", {extra_columns}\nFROM ", 1)
    connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
    connection.execute(text(f"CREATE TABLE {table} AS {select}"))
    connection.execute(text(f"ALTER TABLE {table} ADD PRIMARY KEY (transactionid)"))
    connection.execute(text(f"CREATE INDEX ON {table} (pc, k_exact)"))
    connection.execute(text(f"CREATE INDEX ON {table} (street_norm, housenumber)"))
    connection.execute(text(f"CREATE INDEX ON {table} (pc, street_norm, paon_norm)"))
    connection.execute(text(f"ANALYZE {table}"))
    return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


# Eligible London sales ppd_table does not hold: not by transactionid (0027
# back-filled it where the evidence allowed), nor, for the rows it could not
# name, by (postcode, date, price).
_UNHELD_SALES_SOURCE = f"""(
    SELECT p.*
    FROM staging.ppd_domestic p
    WHERE {ELIGIBLE_SALE_PREDICATE}
      AND NOT EXISTS (
          SELECT 1 FROM staging.ppd_table h WHERE h.transactionid = p.transactionid
      )
      AND NOT EXISTS (
          SELECT 1 FROM staging._ppd_unnamed_sale_keys k
          WHERE k.pc = staging.hpldn_pcnorm(p.postcode)
            AND k.dateoftransfer = p.dateoftransfer::date
            AND k.price = p.price
      )
)"""

_UNNAMED_SALE_KEYS_SQL = """
CREATE TABLE staging._ppd_unnamed_sale_keys AS
SELECT DISTINCT staging.hpldn_pcnorm(a.postcode) AS pc, p.dateoftransfer, p.price
FROM staging.ppd_table p
JOIN staging.address_table a ON a.id = p.address_id
WHERE p.transactionid IS NULL
"""


def build_link_targets(engine: Engine, cfg=None, log=None) -> int:
    """
    Build `TARGET_TABLE`: every eligible London sale ``staging.ppd_table`` does not hold.

    Returns
    -------
    int
        Sales to link.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(LINK_FUNCTIONS_SQL))
        connection.execute(text("DROP TABLE IF EXISTS staging._ppd_unnamed_sale_keys"))
        connection.execute(text(_UNNAMED_SALE_KEYS_SQL))
        connection.execute(text("CREATE INDEX ON staging._ppd_unnamed_sale_keys (pc, dateoftransfer, price)"))
        connection.execute(text("ANALYZE staging._ppd_unnamed_sale_keys"))
        rows = create_sale_keys_table(connection, TARGET_TABLE, _UNHELD_SALES_SOURCE)
        connection.execute(text("DROP TABLE staging._ppd_unnamed_sale_keys"))
    _emit(log, "sale_linking: %s holds %d eligible London sale(s) ppd_table does not", TARGET_TABLE, rows)
    return rows


# ---------------------------------------------------------------------------
# Tier candidates
# ---------------------------------------------------------------------------
#
# Each statement inserts into {cand} (transactionid, method, uprn, n_uprns,
# evidence) for sales of {target}: uprn is set only when the method's evidence
# names exactly one UPRN (n_uprns = 1); n_uprns > 1 records an ambiguity.
# Validity (Open UPRN, NSUL postcode) is judged later, in one place.

CANDIDATE_DDL = """
CREATE TABLE {cand} (
    transactionid varchar NOT NULL,
    method        varchar NOT NULL,
    uprn          bigint,
    n_uprns       integer NOT NULL,
    evidence      text,
    PRIMARY KEY (transactionid, method)
)
"""


def _gazetteer_tier_sql(prefix: str, gazetteer: str, evidence_column: str) -> Dict[str, str]:
    """Exact / token-set / numeric(+street, +building words) matches against one gazetteer."""
    # Resolved UPRN-less certificates (epc only) are hidden from the harness:
    # one resolved from a ground-truth sale's own text would give it away.
    gaz_filter = "{gaz_filter}" if prefix == "epc" else ""
    common = f"""
    INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, '{prefix}_{{kind}}',
           CASE WHEN count(DISTINCT g.uprn) = 1 {{only_ok}} THEN min(g.uprn) END,
           count(DISTINCT g.uprn), min(g.{evidence_column})
    FROM {{target}} t
    JOIN {gazetteer} g ON g.postcode_norm = t.pc AND {{join}} {gaz_filter}
    {{extra_join}}
    WHERE {{where}}
    GROUP BY t.transactionid
    """
    # A numeric match takes only a UPRN whose certificates agree on one
    # address (epc_link_uprn_ok), but every matching UPRN counts towards the
    # ambiguity: a second UPRN with the same address, however noisy its
    # certificates, makes the sale ambiguous.
    numsig_ok = prefix == "epc"
    kinds = {
        "exact": ("g.k_exact = t.k_exact", "", "t.k_exact <> ''", ""),
        "tokenset": ("g.k_tokenset = t.k_tokenset", "", "t.k_tokenset <> ''", ""),
        "numsig": (
            "g.k_numsig_alnum = t.k_numsig_alnum",
            (
                "LEFT JOIN staging.epc_link_uprn_ok ok "
                "ON ok.postcode_norm = g.postcode_norm AND ok.uprn = g.uprn"
                if numsig_ok else ""
            ),
            (
                "t.k_numsig_alnum <> '' AND t.street_norm IS NOT NULL "
                "AND position(' ' || t.street_norm || ' ' IN ' ' || g.k_exact || ' ') > 0 "
                "AND g.k_tokens @> t.bldg_tokens "
                "AND staging.hpldn_numseq(g.k_exact) = staging.hpldn_numseq(t.k_exact) "
                "AND staging.hpldn_same_unit(t.k_exact, g.k_exact, t.propertytype = 'F')"
            ),
            "AND bool_and(ok.uprn IS NOT NULL)" if numsig_ok else "",
        ),
    }
    if prefix == "planning":
        # A flat's application is usually filed against the building's UPRN.
        kinds = {k: (j, e, f"t.is_house AND {w}", o) for k, (j, e, w, o) in kinds.items()}
    return {
        f"{prefix}_{kind}": common.replace("{kind}", kind)
        .replace("{join}", join)
        .replace("{extra_join}", extra_join)
        .replace("{where}", where)
        .replace("{only_ok}", only_ok)
        for kind, (join, extra_join, where, only_ok) in kinds.items()
    }


TIER_SQL: Dict[str, str] = {
    "hmlr": """
    INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'hmlr', h.uprn, 1, h.source_file
    FROM {target} t
    JOIN staging.hmlr_uprn_lookup h ON h.transactionid = t.transactionid
    """,
    "ucl": """
    INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'ucl', u."UPRN", 1, 'ukhpucl:' || coalesce(u."UPRN_SOURCE", '')
    FROM {target} t
    JOIN staging.ukhpucl u ON u.transactionid = t.transactionid
    WHERE u."UPRN" IS NOT NULL
    """,
    **_gazetteer_tier_sql("epc", "staging.epc_link_gazetteer", "certificate_number"),
    **_gazetteer_tier_sql("planning", "staging.planning_link_gazetteer", "application_id"),
    "osm_polygon": """
    INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'osm_polygon',
           CASE WHEN count(DISTINCT o.uprn) = 1 AND bool_and(o.uprn IS NOT NULL) THEN min(o.uprn) END,
           greatest(count(DISTINCT o.uprn), max(o.n_uprns)), min(o.osm_id)
    FROM {target} t
    JOIN staging.osm_address o
      ON o.street_norm = t.street_norm AND o.housenumber = t.housenumber
     AND (o.postcode_norm IS NULL OR o.postcode_norm = t.pc)
    WHERE t.is_house AND o.kind = 'polygon' AND o.n_uprns > 0 AND t.pc = ANY(o.nsul_postcodes)
    GROUP BY t.transactionid
    """,
    "osm_point": """
    INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'osm_point',
           CASE WHEN count(DISTINCT o.uprn) = 1 AND bool_and(o.uprn IS NOT NULL) THEN min(o.uprn) END,
           greatest(count(DISTINCT o.uprn), max(o.n_uprns)), min(o.osm_id)
    FROM {target} t
    JOIN staging.osm_address o
      ON o.street_norm = t.street_norm AND o.housenumber = t.housenumber
     AND (o.postcode_norm IS NULL OR o.postcode_norm = t.pc)
    WHERE t.is_house AND o.kind = 'point' AND o.n_uprns > 0 AND t.pc = ANY(o.nsul_postcodes)
    GROUP BY t.transactionid
    """,
    # OpenStreetMap and a planning application, independently, name one UPRN
    # (both after the veto). Runs after the osm_* and planning_* methods.
    "osm_planning": """
    INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
    SELECT DISTINCT ON (o.transactionid) o.transactionid, 'osm_planning', o.uprn, 1,
           o.evidence || ' + ' || p.evidence
    FROM {cand} o
    JOIN {cand} p
      ON p.transactionid = o.transactionid AND p.method LIKE 'planning\\_%'
     AND p.n_uprns = 1 AND p.uprn = o.uprn
    JOIN {target} t ON t.transactionid = o.transactionid
    WHERE o.method LIKE 'osm\\_%' AND o.n_uprns = 1
    ORDER BY o.transactionid, o.method, p.method
    """,
    # osm_polygon's candidates after its veto and duplicate check; then
    # ISOLATION_SQL drops each whose UPRN has an unaddressed UPRN nearby.
    "osm_polygon_isolated": """
    INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
    SELECT c.transactionid, 'osm_polygon_isolated', c.uprn, c.n_uprns, c.evidence
    FROM {cand} c
    JOIN {target} t ON t.transactionid = c.transactionid
    WHERE c.method = 'osm_polygon'
    """,
}

# osm_polygon's errors (precision harness) share one pattern: the outline
# holds a UPRN nobody has an address for, and the home's real UPRN, equally
# unaddressed, stands 3-30 m away in another building TOID. So an outline's
# UPRN counts only when no UPRN within ISOLATION_RADIUS_M is unaddressed:
# absent from `ASSIGNED_TABLE` (certificates, UCL, HMLR, planning, OSM,
# address rows, the ledger). {hide} lets the harness treat a ground-truth
# sale's true UPRN as unaddressed, as it is for the sales left to link.
ISOLATED_METHODS = ("osm_polygon_isolated",)
ISOLATION_SQL = f"""
UPDATE {{cand}} c
SET n_uprns = 0, evidence = coalesce(c.evidence, '') || ' not-isolated'
FROM {{target}} t, staging.nsul_london cn
WHERE t.transactionid = c.transactionid AND cn.uprn = c.uprn
  AND c.method = :method AND c.n_uprns = 1
  AND EXISTS (
      SELECT 1 FROM staging.nsul_london o
      WHERE ST_DWithin(o.geom, cn.geom, {ISOLATION_RADIUS_M}) AND o.uprn <> c.uprn
        AND (NOT EXISTS (SELECT 1 FROM {ASSIGNED_TABLE} a WHERE a.uprn = o.uprn) {{hide}})
  )
"""

# Known addresses of UPRNs, as numeric signatures: certificates, held address
# rows, planning applications. The veto's evidence.
# Certificates and applications name a unit ("GROUND FLOOR F 19", "LAND
# ADJOINING 16"), so they keep their text (unit_key); held address rows name
# the building (0005's convention), so they do not.
_KNOWN_ADDRESS_SQL = f"""
CREATE TABLE {KNOWN_ADDRESS_TABLE} AS
SELECT uprn, k_numsig_alnum, k_exact AS unit_key, k_tokens FROM staging.epc_link_gazetteer
UNION
SELECT uk."UPRN", staging.hpldn_numsig_alnum(k.k_exact), NULL,
       string_to_array(staging.hpldn_tokenset(k.k_exact), ' ')
FROM staging.address_table a
JOIN staging.ukhcs_table uk ON uk.id = a.uid
JOIN staging.nsul_london n ON n.uprn = uk."UPRN"
CROSS JOIN LATERAL (SELECT staging.hpldn_addrkey(concat_ws(' ', a.postnumber, a.poststreet)) AS k_exact) k
UNION
SELECT uprn, k_numsig_alnum, k_exact, k_tokens FROM staging.planning_link_gazetteer
"""

# Methods a duplicate address makes ambiguous: every one but HMLR's own
# lookup and the terminated-postcode methods (which have no NSUL postcode).
DUPLICATE_CHECKED_METHODS = (
    "ucl", "epc_exact", "epc_tokenset", "epc_numsig", "osm_polygon", "osm_point",
    "planning_exact", "planning_tokenset", "planning_numsig", "osm_planning",
)

# Two live UPRNs of the sale's postcode can carry its address (an old UPRN
# and a rebuilt home's new one, a flat recorded twice). A method that sees
# only one of them is guessing, so its candidate becomes ambiguous when
# another UPRN of the postcode has a known address with the sale's numbers,
# naming the same part of the building, and either every word of the
# sale's or only words the sale has ("274A KEW ROAD" for "TEMPLETON HOUSE,
# 274A"; a held row "16A" with no street; not "FLAT 1 SIDNEY HOUSE" for
# "FLAT 1 FLORENCE HOUSE"). In the precision harness
# most planning/UCL disagreements were such pairs. {hide} lets the harness
# hide a ground-truth sale's own UPRN: its certificate would otherwise flag
# every wrong candidate, which the unlinked sales (mostly uncertified) lack.
DUPLICATE_SQL = f"""
WITH d AS (
    SELECT c.transactionid, (
        SELECT n.uprn
        FROM staging.nsul_london n
        JOIN {KNOWN_ADDRESS_TABLE} k ON k.uprn = n.uprn
        WHERE n.postcode_norm = t.pc AND n.uprn <> c.uprn {{hide}}
          AND k.k_numsig_alnum = t.k_numsig_alnum
          AND (k.k_tokens @> string_to_array(t.k_tokenset, ' ')
               OR NOT EXISTS (
                   SELECT 1 FROM unnest(k.k_tokens) w
                   WHERE w ~ '^[A-Z]{{{{2,}}}}$' AND w <> ALL (string_to_array(t.k_tokenset, ' '))
               ))
          AND (k.unit_key IS NULL OR staging.hpldn_same_unit(t.k_exact, k.unit_key, t.propertytype = 'F'))
        LIMIT 1
    ) AS other
    FROM {{cand}} c
    JOIN {{target}} t ON t.transactionid = c.transactionid
    WHERE c.method = :method AND c.n_uprns = 1 AND t.k_tokenset <> ''
)
UPDATE {{cand}} c
SET n_uprns = 2, evidence = coalesce(c.evidence, '') || ' duplicate:' || d.other
FROM d
WHERE d.transactionid = c.transactionid AND c.method = :method AND d.other IS NOT NULL
"""

# A candidate UPRN is vetoed when one of its known addresses is another home,
# comparing numbers as digit/letter parts ("100C" is "F C 100"):
#   * parts neither side contains ("23" for "25", "6C" for "6B", "26A" for
#     "30B"); one side adding a building number ("F 9 ... 60 FAIRFIELD ROAD"
#     for "FLAT 9, PARK WEST BUILDING") is the same home. Held address rows
#     name the building (0005's convention), so they are compared with the
#     sale's PAON only, and only when it has a number;
#   * a flat, annexe or plot for a house sale ("ANNEXE REAR OF 25"; street
#     words such as LODGE LANE cancel, as both sides have them);
#   * the building: a certificate or application with no unit that names
#     only part of the sale's PAON ("94 PARK LANE" for "FLAT B, 94", "5
#     SHELLEY CLOSE" for "5A").
VETO_SQL = f"""
UPDATE {{cand}} c
SET n_uprns = 0, evidence = coalesce(c.evidence, '') || ' vetoed'
FROM {{target}} t
CROSS JOIN LATERAL (
    SELECT staging.hpldn_numparts(t.k_numsig_alnum) AS parts,
           staging.hpldn_numparts(staging.hpldn_numsig_alnum(t.paon_norm)) AS paon_parts
) s
WHERE t.transactionid = c.transactionid
  AND c.method = :method AND c.n_uprns = 1
  AND EXISTS (
      SELECT 1 FROM {KNOWN_ADDRESS_TABLE} k
      CROSS JOIN LATERAL (SELECT staging.hpldn_numparts(k.k_numsig_alnum) AS parts) kn
      WHERE k.uprn = c.uprn AND k.k_numsig_alnum <> ''
        AND ((k.unit_key IS NOT NULL AND NOT (kn.parts <@ s.parts OR s.parts <@ kn.parts))
             OR (k.unit_key IS NULL AND cardinality(s.paon_parts) > 0
                 AND NOT (kn.parts <@ s.paon_parts OR s.paon_parts <@ kn.parts))
             OR t.k_numsig_alnum = ''
             OR (k.unit_key IS NOT NULL AND t.is_house AND NOT staging.hpldn_same_unit(t.k_exact, k.unit_key))
             OR (k.unit_key IS NOT NULL AND NOT staging.hpldn_has_unit(k.unit_key)
                 AND kn.parts <@ s.paon_parts AND kn.parts <> s.parts))
  )
"""


def build_known_addresses(connection, log=None) -> int:
    """Build `KNOWN_ADDRESS_TABLE`, the veto's evidence (needs the gazetteers loaded)."""
    connection.execute(text(f"DROP TABLE IF EXISTS {KNOWN_ADDRESS_TABLE}"))
    connection.execute(text(_KNOWN_ADDRESS_SQL))
    connection.execute(text(f"CREATE INDEX ON {KNOWN_ADDRESS_TABLE} (uprn)"))
    connection.execute(text(f"ANALYZE {KNOWN_ADDRESS_TABLE}"))
    rows = connection.execute(text(f"SELECT count(*) FROM {KNOWN_ADDRESS_TABLE}")).scalar_one()
    _emit(log, "sale_linking: %s holds %d (UPRN, address) pair(s)", KNOWN_ADDRESS_TABLE, rows)
    return rows


# L8: sales in terminated postcodes. Evidence anywhere within
# TERMINATED_RADIUS_M of the old centroid, since NSUL no longer knows the
# postcode. {target} must carry no live-postcode sales (see _terminated_target).
TERMINATED_TIER_SQL: Dict[str, str] = {
    "term_ucl": f"""
    INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'term_ucl', u."UPRN", 1, 'ukhpucl:' || coalesce(u."UPRN_SOURCE", '')
    FROM {{target}} t
    JOIN staging.postcode_centroid_history pc ON pc.postcode_norm = t.pc
    JOIN staging.ukhpucl u ON u.transactionid = t.transactionid
    JOIN staging.nsul_london n ON n.uprn = u."UPRN"
    WHERE ST_DWithin(n.geom, pc.geom, {TERMINATED_RADIUS_M})
    """,
    "term_epc": f"""
    INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'term_epc',
           CASE WHEN count(DISTINCT m.uprn) = 1 THEN min(m.uprn) END,
           count(DISTINCT m.uprn), min(m.certificate_number)
    FROM {{target}} t
    JOIN staging.postcode_centroid_history pc ON pc.postcode_norm = t.pc
    CROSS JOIN LATERAL (
        SELECT g.uprn, g.certificate_number
        FROM staging.nsul_london n
        JOIN staging.epc_link_gazetteer g ON g.uprn = n.uprn {{gaz_filter}}
        WHERE ST_DWithin(n.geom, pc.geom, {TERMINATED_RADIUS_M})
          AND ((g.k_exact = t.k_exact AND t.k_exact <> '')
               OR (g.k_numsig_alnum = t.k_numsig_alnum AND t.k_numsig_alnum <> ''
                   AND t.street_norm IS NOT NULL
                   AND position(' ' || t.street_norm || ' ' IN ' ' || g.k_exact || ' ') > 0
                   AND g.k_tokens @> t.bldg_tokens
                   AND staging.hpldn_numseq(g.k_exact) = staging.hpldn_numseq(t.k_exact)
                   AND staging.hpldn_same_unit(t.k_exact, g.k_exact, t.propertytype = 'F')))
    ) m
    GROUP BY t.transactionid
    """,
    "term_osm": f"""
    INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
    SELECT t.transactionid, 'term_osm',
           CASE WHEN count(DISTINCT o.uprn) = 1 AND bool_and(o.uprn IS NOT NULL) THEN min(o.uprn) END,
           greatest(count(DISTINCT o.uprn), max(o.n_uprns)), min(o.osm_id)
    FROM {{target}} t
    JOIN staging.postcode_centroid_history pc ON pc.postcode_norm = t.pc
    JOIN staging.osm_address o ON o.street_norm = t.street_norm AND o.housenumber = t.housenumber
    JOIN staging.nsul_london n ON n.uprn = o.uprn
    WHERE t.is_house AND o.n_uprns > 0 AND ST_DWithin(n.geom, pc.geom, {TERMINATED_RADIUS_M})
    GROUP BY t.transactionid
    """,
}


def run_candidate_tiers(
    connection, target: str, cand: str, methods: Iterable[str], log=None,
    simulate_terminated: bool = False, hide_true_uprn: bool = False,
) -> Dict[str, int]:
    """
    Run each method's candidate SQL for the sales of `target` into `cand` (created if absent).

    Terminated-postcode methods (``term_*``) see only sales whose postcode is
    not live in NSUL, unless `simulate_terminated` (the harness runs them on
    live postcodes as if terminated). `hide_true_uprn` (the harness; `target`
    has ``true_uprn``) keeps a sale's own UPRN out of the duplicate check,
    counts it unaddressed for the isolation check, and hides the resolved
    UPRN-less certificates from the ``epc_*`` methods. The isolation check
    reads `ASSIGNED_TABLE` (`build_assigned_uprns`). Returns candidate rows
    per method.
    """
    connection.execute(
        text(CANDIDATE_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS").format(cand=cand))
    )
    counts: Dict[str, int] = {}
    gaz_filter = "AND g.resolved_method IS NULL" if hide_true_uprn else ""
    for method in methods:
        if method in TERMINATED_TIER_SQL:
            # The harness simulates on a tenth of its sales: each one scans
            # every UPRN within the radius.
            live_filter = (
                " WHERE abs(hashtext(transactionid)) % 10 = 0" if simulate_terminated
                else " WHERE NOT is_live_pc"
            )
            sql = TERMINATED_TIER_SQL[method].format(
                cand=cand, target=f"(SELECT * FROM {target}{live_filter})", gaz_filter=gaz_filter
            )
        else:
            sql = TIER_SQL[method].format(cand=cand, target=target, gaz_filter=gaz_filter)
        counts[method] = connection.execute(text(sql)).rowcount
        vetoed = duplicates = 0
        if method in ISOLATED_METHODS:
            vetoed = connection.execute(
                text(ISOLATION_SQL.format(
                    cand=cand, target=target, hide="OR o.uprn = t.true_uprn" if hide_true_uprn else "",
                )),
                {"method": method},
            ).rowcount
        if method in VETOED_METHODS:
            vetoed = connection.execute(
                text(VETO_SQL.format(cand=cand, target=target)), {"method": method}
            ).rowcount
        if method in DUPLICATE_CHECKED_METHODS:
            duplicates = connection.execute(
                text(DUPLICATE_SQL.format(
                    cand=cand, target=target,
                    hide="AND n.uprn <> t.true_uprn" if hide_true_uprn else "",
                )),
                {"method": method},
            ).rowcount
        _emit(
            log, "sale_linking: %s -> %d candidate(s) in %s (%d vetoed, %d on a duplicate address)",
            method, counts[method], cand, vetoed, duplicates,
        )
    connection.execute(text(f"ANALYZE {cand}"))
    return counts


# A candidate is valid when it names one UPRN that is in OS Open UPRN and,
# except for terminated postcodes, in the sale's own NSUL postcode.
VALID_CANDIDATE_PREDICATE = """
    c.n_uprns = 1 AND c.uprn IS NOT NULL
    AND EXISTS (SELECT 1 FROM staging.ukhcs_table uk WHERE uk."UPRN" = c.uprn)
    AND (
        c.method LIKE 'term\\_%'
        OR EXISTS (
            SELECT 1 FROM staging.nsul_london n
            WHERE n.uprn = c.uprn AND n.postcode_norm = t.pc
        )
    )
"""


def mark_live_postcodes(connection, target: str) -> None:
    """Add ``is_live_pc`` to `target`: the sale's postcode has a UPRN in NSUL (so is live)."""
    connection.execute(text(f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS is_live_pc boolean"))
    connection.execute(
        text(
            f"""
            UPDATE {target} t
            SET is_live_pc = EXISTS (
                SELECT 1 FROM staging.nsul_london n WHERE n.postcode_norm = t.pc
            )
            """
        )
    )


# ---------------------------------------------------------------------------
# L6 elimination
# ---------------------------------------------------------------------------

# UPRNs some source already gives an address to: they are somebody's, so
# elimination and the L9 rules never hand them to a sale, and they are not
# the unaddressed neighbours osm_polygon_isolated looks for. The gazetteer
# includes the resolved UPRN-less certificates.
_ASSIGNED_UPRNS_SQL = """
CREATE TABLE {assigned} AS
SELECT uprn FROM {certified} AS certified_uprns
UNION SELECT "UPRN" FROM staging.ukhpucl WHERE "UPRN" IS NOT NULL
UNION SELECT uprn FROM staging.hmlr_uprn_lookup
UNION SELECT uprn FROM staging.planning_link_gazetteer
UNION SELECT uprn FROM staging.osm_address WHERE uprn IS NOT NULL
UNION SELECT uk."UPRN" FROM staging.address_table a JOIN staging.ukhcs_table uk ON uk.id = a.uid
UNION SELECT uprn FROM {ledger} AS ledger_links
"""

# Addresses on a (postcode, street) that no UPRN is known for: the unplaced
# sales' address groups, plus certificates lodged without a UPRN (a home the
# gazetteer cannot see but which does exist) that `resolve_uprnless_certificates`
# could not place. Keyed by numeric signature.
_UNPLACED_ADDRESSES_SQL = """
CREATE TABLE {unplaced} AS
SELECT DISTINCT t.pc, t.street_norm, t.k_numsig_alnum AS address_key
FROM {pending} t
WHERE t.street_norm IS NOT NULL AND t.k_numsig_alnum <> ''
UNION
SELECT DISTINCT staging.hpldn_pcnorm(e.postcode), s.street_norm, e.k_numsig_alnum
FROM (
    SELECT e.postcode,
           staging.hpldn_numsig_alnum(staging.hpldn_addrkey(concat_ws(' ', e.address1, e.address2, e.address3))) AS k_numsig_alnum,
           staging.hpldn_addrkey(concat_ws(' ', e.address1, e.address2, e.address3)) AS k_exact
    FROM staging.epc_domestic_history e
    WHERE e.region = 'E12000007' AND e.uprn IS NULL
  AND NOT EXISTS (SELECT 1 FROM {resolved} r WHERE r.certificate_number = e.certificate_number)
) e
JOIN (SELECT DISTINCT pc, street_norm FROM {pending} WHERE street_norm IS NOT NULL) s
  ON s.pc = staging.hpldn_pcnorm(e.postcode)
 AND position(' ' || s.street_norm || ' ' IN ' ' || e.k_exact || ' ') > 0
WHERE e.k_numsig_alnum <> ''
"""

# Unassigned UPRNs on each (postcode, street): in the postcode per NSUL, on a
# USRN of that name per LIDS + Open Roads, and in OS Open UPRN.
_STREET_CANDIDATES_SQL = """
CREATE TABLE {free} AS
SELECT DISTINCT s.pc, s.street_norm, n.uprn
FROM (SELECT DISTINCT pc, street_norm FROM {pending} WHERE street_norm IS NOT NULL) s
JOIN staging.nsul_london n ON n.postcode_norm = s.pc
JOIN staging.uprn_usrn uu ON uu.uprn = n.uprn
JOIN staging.usrn_street us ON us.usrn = uu.usrn AND us.street_norm = s.street_norm
JOIN staging.ukhcs_table uk ON uk."UPRN" = n.uprn
WHERE NOT EXISTS (SELECT 1 FROM {assigned} a WHERE a.uprn = n.uprn)
"""

# Street elimination: one unassigned UPRN and one unplaced address on the
# (postcode, street); every pending sale of that address takes the UPRN.
STREET_ELIMINATION_SQL = """
INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
SELECT t.transactionid, 'street_elimination', f.uprn, 1,
       'usrn-street:' || t.street_norm
FROM {pending} t
JOIN (
    SELECT pc, street_norm, min(uprn) AS uprn
    FROM {free} GROUP BY pc, street_norm HAVING count(*) = 1
) f ON f.pc = t.pc AND f.street_norm = t.street_norm
JOIN (
    SELECT pc, street_norm, min(address_key) AS address_key
    FROM {unplaced} GROUP BY pc, street_norm HAVING count(*) = 1
) u ON u.pc = t.pc AND u.street_norm = t.street_norm AND u.address_key = t.k_numsig_alnum
WHERE t.is_house
"""

# Building elimination (flats): the building is the one TOID the linked or
# held flats of the same (postcode, paon, street) sit in; one unassigned UPRN
# on that TOID and one unplaced flat of the building.
_BUILDING_TOID_SQL = """
CREATE TABLE {buildings} AS
SELECT b.pc, b.paon_norm, b.street_norm, min(ut.toid) AS toid
FROM (
    SELECT staging.hpldn_pcnorm(a.postcode) AS pc,
           NULLIF(staging.hpldn_addrkey(a.postnumber), '') AS paon_norm,
           NULLIF(staging.hpldn_addrkey(a.poststreet), '') AS street_norm,
           uk."UPRN" AS uprn
    FROM staging.address_table a
    JOIN staging.ukhcs_table uk ON uk.id = a.uid
    WHERE staging.hpldn_pcnorm(a.postcode) IN (SELECT DISTINCT pc FROM {pending} WHERE NOT is_house)
    UNION ALL
    SELECT t.pc, t.paon_norm, t.street_norm, l.uprn
    FROM {ledger} l JOIN {pending_all} t ON t.transactionid = l.transactionid
) b
JOIN staging.uprn_toid ut ON ut.uprn = b.uprn
WHERE b.paon_norm IS NOT NULL AND b.street_norm IS NOT NULL
GROUP BY b.pc, b.paon_norm, b.street_norm
HAVING count(DISTINCT ut.toid) = 1
"""

BUILDING_ELIMINATION_SQL = """
INSERT INTO {cand} (transactionid, method, uprn, n_uprns, evidence)
SELECT t.transactionid, 'building_elimination', f.uprn, 1, 'toid:' || b.toid
FROM {pending} t
JOIN {buildings} b ON b.pc = t.pc AND b.paon_norm = t.paon_norm AND b.street_norm = t.street_norm
JOIN (
    SELECT ut.toid, n.postcode_norm, min(ut.uprn) AS uprn
    FROM staging.uprn_toid ut
    JOIN staging.nsul_london n ON n.uprn = ut.uprn
    WHERE NOT EXISTS (SELECT 1 FROM {assigned} a WHERE a.uprn = ut.uprn)
      AND ut.toid IN (SELECT toid FROM {buildings})
    GROUP BY ut.toid, n.postcode_norm
    HAVING count(*) = 1
) f ON f.toid = b.toid AND f.postcode_norm = t.pc
JOIN (
    SELECT pc, paon_norm, street_norm, min(k_exact) AS k_exact
    FROM {pending}
    WHERE NOT is_house AND k_exact <> ''
    GROUP BY pc, paon_norm, street_norm
    HAVING count(DISTINCT k_exact) = 1
) one ON one.pc = t.pc AND one.paon_norm = t.paon_norm AND one.street_norm = t.street_norm
     AND one.k_exact = t.k_exact
WHERE NOT t.is_house
"""


# ---------------------------------------------------------------------------
# L9: flat sequences and house brackets (0029)
# ---------------------------------------------------------------------------
#
# Where a building's or a street's UPRNs were issued in address order, the
# known neighbours pin down an unknown home's UPRN. Both rules take only a
# UPRN in the sale's NSUL postcode that no source gives an address to
# ({assigned}); {hidden} is a UPRN treated as unknown everywhere -- the
# harness's true UPRN, or in the real run the sale's own uncarried ledger
# link, so that the rules can be run on sales already linked and a
# disagreement becomes a conflict (`resolve_candidates`).

# Known flats: (postcode, PAON, street, flat number) -> the one UPRN that UCL's
# link, a held sale (by transactionid) or the ledger gives that flat.
_KNOWN_FLATS_SQL = """
CREATE TABLE {known_flats} AS
SELECT pc, paon_norm, street_norm, flat_no, min(uprn) AS uprn
FROM (
    SELECT staging.hpldn_pcnorm(u.postcode) AS pc,
           NULLIF(staging.hpldn_addrkey(u.paon), '') AS paon_norm,
           NULLIF(staging.hpldn_addrkey(u.street), '') AS street_norm,
           staging.hpldn_flat_no(u.saon) AS flat_no, u."UPRN" AS uprn
    FROM staging.ukhpucl u
    WHERE u.county = 'GREATER LONDON' AND u."UPRN" IS NOT NULL AND u.propertytype = 'F'
    UNION ALL
    SELECT staging.hpldn_pcnorm(d.postcode), NULLIF(staging.hpldn_addrkey(d.paon), ''),
           NULLIF(staging.hpldn_addrkey(d.street), ''), staging.hpldn_flat_no(d.saon), uk."UPRN"
    FROM staging.ppd_table p
    JOIN staging.ppd_domestic d ON d.transactionid = p.transactionid
    JOIN staging.address_table a ON a.id = p.address_id
    JOIN staging.ukhcs_table uk ON uk.id = a.uid
    WHERE p.propertytype = 'F' AND d.county = 'GREATER LONDON'
    UNION ALL
    SELECT t.pc, t.paon_norm, t.street_norm, staging.hpldn_flat_no(t.saon), l.uprn
    FROM {ledger} l
    JOIN {pending_all} t ON t.transactionid = l.transactionid
    WHERE t.propertytype = 'F'
) s
WHERE flat_no IS NOT NULL AND paon_norm IS NOT NULL AND street_norm IS NOT NULL
GROUP BY pc, paon_norm, street_norm, flat_no
HAVING count(DISTINCT uprn) = 1
"""

# Known houses: (postcode, street, house number) -> the one London UPRN whose
# address row (0005's convention: postnumber = PAON) or ledger link has it.
_KNOWN_HOUSES_SQL = """
CREATE TABLE {known_houses} AS
SELECT pc, street_norm, num, min(uprn) AS uprn
FROM (
    SELECT staging.hpldn_pcnorm(a.postcode) AS pc,
           NULLIF(staging.hpldn_addrkey(a.poststreet), '') AS street_norm,
           staging.hpldn_house_no(a.postnumber) AS num, uk."UPRN" AS uprn
    FROM staging.address_table a
    JOIN staging.ukhcs_table uk ON uk.id = a.uid
    JOIN staging.nsul_london n ON n.uprn = uk."UPRN"
    UNION ALL
    SELECT t.pc, t.street_norm, staging.hpldn_house_no(t.paon), l.uprn
    FROM {ledger} l
    JOIN {pending_all} t ON t.transactionid = l.transactionid
    WHERE t.is_house
) s
WHERE num IS NOT NULL AND street_norm IS NOT NULL
GROUP BY pc, street_norm, num
HAVING count(DISTINCT uprn) = 1
"""

# flat_sequence: the sale's building (postcode, PAON, street) has at least
# FLAT_SEQUENCE_MIN_FLATS other known flats, every one at UPRN = offset + flat
# number; the sale's flat takes offset + its number.
FLAT_SEQUENCE_SQL = f"""
INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
SELECT t.transactionid, 'flat_sequence', s.off + f.flat_no, 1,
       'flats:' || s.n || ' offset:' || s.off
FROM {{target}} t
{{own_join}}
CROSS JOIN LATERAL (SELECT staging.hpldn_flat_no(t.saon)::bigint AS flat_no) f
CROSS JOIN LATERAL (
    SELECT count(*) AS n, count(DISTINCT k.uprn - k.flat_no) AS n_off, min(k.uprn - k.flat_no) AS off
    FROM {{known_flats}} k
    WHERE k.pc = t.pc AND k.paon_norm = t.paon_norm AND k.street_norm = t.street_norm
      AND k.flat_no <> f.flat_no AND k.uprn IS DISTINCT FROM {{hidden}}
) s
WHERE f.flat_no IS NOT NULL AND t.paon_norm IS NOT NULL AND t.street_norm IS NOT NULL
  AND s.n >= {FLAT_SEQUENCE_MIN_FLATS} AND s.n_off = 1
  AND EXISTS (
      SELECT 1 FROM staging.nsul_london n
      WHERE n.uprn = s.off + f.flat_no AND n.postcode_norm = t.pc
  )
  AND NOT EXISTS (
      SELECT 1 FROM {{assigned}} a
      WHERE a.uprn = s.off + f.flat_no AND a.uprn IS DISTINCT FROM {{hidden}}
  )
ON CONFLICT (transactionid, method) DO NOTHING
"""

# house_bracket: the houses n-2 and n+2 of the sale's (postcode, street) have
# UPRNs at most HOUSE_BRACKET_MAX_GAP apart; the sale takes the one
# unassigned UPRN of the postcode strictly between them (several: ambiguous).
HOUSE_BRACKET_SQL = f"""
INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
SELECT x.transactionid, 'house_bracket',
       CASE WHEN count(*) = 1 THEN min(n.uprn) END, count(*),
       'between:' || min(x.lo) || '-' || min(x.hi)
FROM (
    SELECT t.transactionid, t.pc, {{hidden}} AS hidden,
           least(lo.uprn, hi.uprn) AS lo, greatest(lo.uprn, hi.uprn) AS hi
    FROM {{target}} t
    {{own_join}}
    CROSS JOIN LATERAL (SELECT staging.hpldn_house_no(t.paon) AS num) h
    JOIN {{known_houses}} lo ON lo.pc = t.pc AND lo.street_norm = t.street_norm AND lo.num = h.num - 2
    JOIN {{known_houses}} hi ON hi.pc = t.pc AND hi.street_norm = t.street_norm AND hi.num = h.num + 2
    WHERE t.is_house AND h.num IS NOT NULL
      AND lo.uprn IS DISTINCT FROM {{hidden}} AND hi.uprn IS DISTINCT FROM {{hidden}}
      AND abs(hi.uprn - lo.uprn) <= {HOUSE_BRACKET_MAX_GAP}
) x
JOIN staging.nsul_london n ON n.postcode_norm = x.pc AND n.uprn > x.lo AND n.uprn < x.hi
WHERE NOT EXISTS (
    SELECT 1 FROM {{assigned}} a WHERE a.uprn = n.uprn AND a.uprn IS DISTINCT FROM x.hidden
)
GROUP BY x.transactionid
ON CONFLICT (transactionid, method) DO NOTHING
"""

SEQUENCE_METHODS = ("flat_sequence", "house_bracket")

# One inferred UPRN, one home: a UPRN the L9 rules give to sales of more than
# one address group makes every one of them ambiguous.
SEQUENCE_UNIQUE_SQL = """
WITH shared AS MATERIALIZED (
    SELECT c2.uprn
    FROM {cand} c2
    JOIN {target} t ON t.transactionid = c2.transactionid
    WHERE c2.method IN ('flat_sequence', 'house_bracket') AND c2.n_uprns = 1
    GROUP BY c2.uprn
    HAVING count(DISTINCT t.pc || '|' || t.k_exact) > 1
)
UPDATE {cand} c
SET n_uprns = 2, evidence = coalesce(c.evidence, '') || ' shared-uprn'
FROM shared s
WHERE c.uprn = s.uprn AND c.method IN ('flat_sequence', 'house_bracket') AND c.n_uprns = 1
"""


def run_sequence_tiers(
    connection, target: str, cand: str, tables: Dict[str, str], log=None, hide_true_uprn: bool = False,
) -> Dict[str, int]:
    """
    Run the L9 rules for every sale of `target` into `cand`, then the one-home and duplicate checks.

    `tables` come from `elimination_tables`. The real run passes every
    target: a sale already in the ledger has its own link hidden, so a rule
    that disagrees with it puts the address group in conflict.
    `hide_true_uprn` (the harness) hides ``t.true_uprn`` instead.
    Returns candidate rows per method.
    """
    if hide_true_uprn:
        own_join, hidden = "", "t.true_uprn"
    else:
        own_join, hidden = f"LEFT JOIN {LEDGER_TABLE} own ON own.transactionid = t.transactionid", "own.uprn"
    fmt = dict(tables, cand=cand, target=target, own_join=own_join, hidden=hidden)
    counts: Dict[str, int] = {}
    for method, sql in (("flat_sequence", FLAT_SEQUENCE_SQL), ("house_bracket", HOUSE_BRACKET_SQL)):
        counts[method] = connection.execute(text(sql.format(**fmt))).rowcount
    connection.execute(text(f"ANALYZE {cand}"))
    shared = connection.execute(text(SEQUENCE_UNIQUE_SQL.format(cand=cand, target=target))).rowcount
    duplicates = 0
    for method in SEQUENCE_METHODS:
        duplicates += connection.execute(
            text(DUPLICATE_SQL.format(
                cand=cand, target=target, hide="AND n.uprn <> t.true_uprn" if hide_true_uprn else "",
            )),
            {"method": method},
        ).rowcount
    connection.execute(text(f"ANALYZE {cand}"))
    _emit(log, "sale_linking: L9 -> %s candidate(s) in %s (%d on a UPRN shared by several addresses, "
          "%d on a duplicate address)", counts, cand, shared, duplicates)
    return counts


RESOLVED_DDL = f"""
CREATE TABLE IF NOT EXISTS {RESOLVED_TABLE} (
    certificate_number varchar PRIMARY KEY,
    uprn               bigint NOT NULL,
    method             varchar NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
)
"""


# Stand-ins for the ledger and its sales when a build should see no links.
_NO_LINKS = "(SELECT NULL::varchar AS transactionid, NULL::bigint AS uprn WHERE false)"
_NO_SALES = (
    "(SELECT NULL::varchar AS transactionid, NULL::text AS pc, NULL::text AS paon_norm, "
    "NULL::text AS street_norm, NULL::varchar AS saon, NULL::varchar AS paon, "
    "NULL::varchar AS propertytype, NULL::boolean AS is_house WHERE false)"
)


def build_assigned_uprns(
    connection, assigned: str, ledger: str = LEDGER_TABLE, certified: str = "staging.epc_link_gazetteer",
) -> int:
    """(Re)build `assigned`: every UPRN some source gives an address to (`_ASSIGNED_UPRNS_SQL`)."""
    connection.execute(text(f"DROP TABLE IF EXISTS {assigned}"))
    connection.execute(
        text(_ASSIGNED_UPRNS_SQL.format(assigned=assigned, ledger=ledger, certified=certified))
    )
    connection.execute(text(f"CREATE INDEX ON {assigned} (uprn)"))
    connection.execute(text(f"ANALYZE {assigned}"))
    return connection.execute(text(f"SELECT count(*) FROM {assigned}")).scalar_one()


def build_known_sets(connection, names: Dict[str, str], ledger: str, pending_all: str) -> None:
    """(Re)build the L9 rules' known flats and houses (``names['known_flats']``, ``names['known_houses']``)."""
    fmt = dict(names, ledger=ledger, pending_all=pending_all)
    for role, sql, key in (
        ("known_flats", _KNOWN_FLATS_SQL, "(pc, paon_norm, street_norm)"),
        ("known_houses", _KNOWN_HOUSES_SQL, "(pc, street_norm, num)"),
    ):
        connection.execute(text(f"DROP TABLE IF EXISTS {names[role]}"))
        connection.execute(text(sql.format(**fmt)))
        connection.execute(text(f"CREATE INDEX ON {names[role]} {key}"))
        connection.execute(text(f"ANALYZE {names[role]}"))


def elimination_tables(connection, pending: str, pending_all: str, ledger: str, prefix: str) -> Dict[str, str]:
    """
    Build the L6 and L9 working tables for the unplaced sales of `pending`.

    `pending_all` is the full target table (for the ledger's sale keys);
    `ledger` the links made so far. Returns the table names by role.
    """
    names = {
        "assigned": f"{prefix}_assigned",
        "unplaced": f"{prefix}_unplaced",
        "free": f"{prefix}_free",
        "buildings": f"{prefix}_buildings",
        "known_flats": f"{prefix}_known_flats",
        "known_houses": f"{prefix}_known_houses",
    }
    for name in names.values():
        connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
    connection.execute(text(RESOLVED_DDL))
    fmt = dict(names, pending=pending, pending_all=pending_all, ledger=ledger, resolved=RESOLVED_TABLE)
    build_assigned_uprns(connection, names["assigned"], ledger)
    connection.execute(text(_UNPLACED_ADDRESSES_SQL.format(**fmt)))
    connection.execute(text(_STREET_CANDIDATES_SQL.format(**fmt)))
    connection.execute(text(_BUILDING_TOID_SQL.format(**fmt)))
    connection.execute(text(f"CREATE INDEX ON {names['unplaced']} (pc, street_norm)"))
    connection.execute(text(f"CREATE INDEX ON {names['free']} (pc, street_norm)"))
    connection.execute(text(f"CREATE INDEX ON {names['buildings']} (pc, paon_norm, street_norm)"))
    connection.execute(text(f"CREATE INDEX ON {names['buildings']} (toid)"))
    for name in ("unplaced", "free", "buildings"):
        connection.execute(text(f"ANALYZE {names[name]}"))
    build_known_sets(connection, names, ledger, pending_all)
    return names


# ---------------------------------------------------------------------------
# Resolution: conflicts, ledger, L4, unlinked
# ---------------------------------------------------------------------------

LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    transactionid varchar PRIMARY KEY,
    uprn          bigint NOT NULL,
    method        varchar NOT NULL,
    evidence      text,
    created_at    timestamptz NOT NULL DEFAULT now()
)
"""

UNLINKED_DDL = f"""
CREATE TABLE {UNLINKED_TABLE} (
    transactionid varchar PRIMARY KEY,
    reason        varchar NOT NULL,
    detail        text
)
"""

PRECISION_DDL = f"""
CREATE TABLE IF NOT EXISTS {PRECISION_TABLE} (
    method       varchar NOT NULL,
    ground_truth varchar NOT NULL,
    n            integer NOT NULL,
    agree        integer NOT NULL,
    precision    double precision,
    passed       boolean NOT NULL,
    measured_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (method, ground_truth, measured_at)
)
"""

# Held sales' UPRNs, by transactionid (0027's back-fill).
_HELD_UPRNS_SQL = """
SELECT p.transactionid, uk."UPRN" AS uprn
FROM staging.ppd_table p
JOIN staging.address_table a ON a.id = p.address_id
JOIN staging.ukhcs_table uk ON uk.id = a.uid
WHERE p.transactionid IS NOT NULL
"""

# The address group of every eligible sale (held or not): postcode + the
# normalised saon/paon/street. Sales with an empty key are their own group.
GROUPS_SQL = f"""
CREATE TABLE {{groups}} AS
SELECT p.transactionid, staging.hpldn_pcnorm(p.postcode) AS pc,
       staging.hpldn_addrkey(concat_ws(' ', p.saon, p.paon, p.street)) AS k_exact,
       p.dateoftransfer::date AS dateoftransfer, p.oldnew
FROM staging.ppd_domestic p
WHERE {ELIGIBLE_SALE_PREDICATE}
  AND staging.hpldn_addrkey(concat_ws(' ', p.saon, p.paon, p.street)) <> ''
  AND staging.hpldn_pcnorm(p.postcode) IN (SELECT DISTINCT pc FROM {{target}})
"""


def resolve_candidates(
    connection, target: str, cand: str, groups: str, enabled: Sequence[str], log=None
) -> Dict[str, int]:
    """
    Write the valid, non-conflicting candidates of `enabled` methods into the ledger.

    A group (postcode + address key) is in conflict when the valid candidates
    of its sales and the UPRNs of its held sales name more than one UPRN; none
    of its sales is linked. Otherwise each sale takes its best candidate in
    `METHOD_ORDER`. A candidate for a sale the ledger already links (the L9
    rules run on every target) is not written, but still counts towards its
    group's conflict. Returns links written per method.
    """
    enabled_sql = "ARRAY[" + ",".join(f"'{m}'" for m in enabled) + "]::varchar[]"
    connection.execute(text("DROP TABLE IF EXISTS staging._link_valid"))
    connection.execute(
        text(
            f"""
            CREATE TABLE staging._link_valid AS
            SELECT c.transactionid, c.method, c.uprn, c.evidence
            FROM {cand} c
            JOIN {target} t ON t.transactionid = c.transactionid
            WHERE c.method = ANY({enabled_sql})
              AND {VALID_CANDIDATE_PREDICATE}
            """
        )
    )
    connection.execute(text("CREATE INDEX ON staging._link_valid (transactionid)"))
    connection.execute(text("ANALYZE staging._link_valid"))
    # Accumulates over the stages of one run (link_sales creates it empty).
    connection.execute(text("CREATE TABLE IF NOT EXISTS staging._link_conflict_groups (pc text, k_exact text)"))
    connection.execute(
        text(
            f"""
            INSERT INTO staging._link_conflict_groups (pc, k_exact)
            SELECT g.pc, g.k_exact
            FROM {groups} g
            JOIN (
                SELECT transactionid, uprn FROM staging._link_valid
                UNION ALL
                SELECT transactionid, uprn FROM {LEDGER_TABLE}
                UNION ALL
                {_HELD_UPRNS_SQL}
            ) u ON u.transactionid = g.transactionid
            GROUP BY g.pc, g.k_exact
            HAVING count(DISTINCT u.uprn) > 1
               AND bool_or(g.transactionid IN (SELECT transactionid FROM staging._link_valid))
            """
        )
    )
    connection.execute(text("CREATE INDEX IF NOT EXISTS _link_conflict_groups_key ON staging._link_conflict_groups (pc, k_exact)"))
    connection.execute(text("ANALYZE staging._link_conflict_groups"))
    order_sql = "array_position(ARRAY[" + ",".join(f"'{m}'" for m in METHOD_ORDER) + "]::varchar[], v.method)"
    inserted = connection.execute(
        text(
            f"""
            INSERT INTO {LEDGER_TABLE} (transactionid, uprn, method, evidence)
            SELECT DISTINCT ON (v.transactionid) v.transactionid, v.uprn, v.method, v.evidence
            FROM staging._link_valid v
            JOIN {target} t ON t.transactionid = v.transactionid
            WHERE NOT EXISTS (
                SELECT 1 FROM staging._link_conflict_groups x
                WHERE x.pc = t.pc AND x.k_exact = t.k_exact AND t.k_exact <> ''
            )
              AND NOT EXISTS (SELECT 1 FROM {LEDGER_TABLE} l WHERE l.transactionid = v.transactionid)
            ORDER BY v.transactionid, {order_sql}
            RETURNING method
            """
        )
    ).scalars().all()
    counts: Dict[str, int] = {}
    for method in inserted:
        counts[method] = counts.get(method, 0) + 1
    conflicts = connection.execute(text("SELECT count(*) FROM staging._link_conflict_groups")).scalar_one()
    _emit(log, "sale_linking: ledger += %s; %d address group(s) in conflict", counts, conflicts)
    return counts


# L4: a pending sale takes the one UPRN of its address group's linked and held
# sales, unless a new-build sale of the group follows an earlier sale (the
# address was rebuilt, so the earlier home may be another UPRN).
GROUP_PROPAGATION_SQL = f"""
INSERT INTO {{cand}} (transactionid, method, uprn, n_uprns, evidence)
SELECT t.transactionid, 'group', s.uprn, 1, 'group-of:' || s.source
FROM {{target}} t
JOIN (
    SELECT g.pc, g.k_exact, min(u.uprn) AS uprn, min(u.transactionid) AS source
    FROM {{groups}} g
    JOIN (
        SELECT transactionid, uprn FROM {LEDGER_TABLE} WHERE method <> 'group'
        UNION ALL
        {_HELD_UPRNS_SQL}
    ) u ON u.transactionid = g.transactionid
    GROUP BY g.pc, g.k_exact
    HAVING count(DISTINCT u.uprn) = 1
) s ON s.pc = t.pc AND s.k_exact = t.k_exact
WHERE t.k_exact <> ''
  AND NOT EXISTS (SELECT 1 FROM {LEDGER_TABLE} l WHERE l.transactionid = t.transactionid)
  AND NOT EXISTS (
      SELECT 1 FROM {{groups}} nb
      JOIN {{groups}} earlier
        ON earlier.pc = nb.pc AND earlier.k_exact = nb.k_exact
       AND earlier.dateoftransfer < nb.dateoftransfer
      WHERE nb.pc = t.pc AND nb.k_exact = t.k_exact AND nb.oldnew = 'Y'
  )
ON CONFLICT (transactionid, method) DO NOTHING
"""


def record_unlinked(connection, target: str, cand: str, enabled: Sequence[str], log=None) -> Dict[str, int]:
    """
    Rebuild `UNLINKED_TABLE`: every sale of `target` without a ledger row, with a reason.

    Reasons, first that applies: ``conflict`` (its group's evidence
    disagrees), ``terminated_postcode`` (postcode no longer live, no L8 link),
    ``not_in_open_uprn`` (evidence names a UPRN OS Open UPRN lacks, e.g. a
    demolished home), ``postcode_mismatch`` (the one UPRN is in another NSUL
    postcode), ``ambiguous`` (evidence names several UPRNs),
    ``vetoed`` (its one UPRN has another address), ``below_precision``
    (only a gated-off tier had one), ``no_candidate``.
    """
    enabled_sql = "ARRAY[" + ",".join(f"'{m}'" for m in enabled) + "]::varchar[]"
    connection.execute(text(f"DROP TABLE IF EXISTS {UNLINKED_TABLE}"))
    connection.execute(text(UNLINKED_DDL))
    connection.execute(
        text(
            f"""
            INSERT INTO {UNLINKED_TABLE} (transactionid, reason, detail)
            SELECT t.transactionid,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM staging._link_conflict_groups x
                        WHERE x.pc = t.pc AND x.k_exact = t.k_exact AND t.k_exact <> ''
                    ) THEN 'conflict'
                    WHEN NOT t.is_live_pc THEN 'terminated_postcode'
                    WHEN bool_or(c.method = ANY({enabled_sql}) AND c.n_uprns = 1
                                 AND NOT EXISTS (SELECT 1 FROM staging.ukhcs_table uk WHERE uk."UPRN" = c.uprn))
                        THEN 'not_in_open_uprn'
                    WHEN bool_or(c.method = ANY({enabled_sql}) AND c.n_uprns = 1) THEN 'postcode_mismatch'
                    WHEN bool_or(c.method = ANY({enabled_sql}) AND c.n_uprns > 1) THEN 'ambiguous'
                    WHEN bool_or(c.method = ANY({enabled_sql}) AND c.n_uprns = 0) THEN 'vetoed'
                    WHEN bool_or(c.n_uprns = 1) THEN 'below_precision'
                    ELSE 'no_candidate'
                END,
                string_agg(DISTINCT c.method || ':' || coalesce(c.uprn::text, c.n_uprns || ' uprns'), ' ')
            FROM {target} t
            LEFT JOIN {cand} c ON c.transactionid = t.transactionid
            WHERE NOT EXISTS (SELECT 1 FROM {LEDGER_TABLE} l WHERE l.transactionid = t.transactionid)
            GROUP BY t.transactionid, t.pc, t.k_exact, t.is_live_pc
            """
        )
    )
    rows = connection.execute(
        text(f"SELECT reason, count(*) FROM {UNLINKED_TABLE} GROUP BY 1 ORDER BY 2 DESC")
    ).all()
    counts = {reason: n for reason, n in rows}
    _emit(log, "sale_linking: %s by reason: %s", UNLINKED_TABLE, counts)
    return counts


# ---------------------------------------------------------------------------
# Carry the ledger into staging
# ---------------------------------------------------------------------------

# Address rows for linked UPRNs without one. The text comes from the newest
# linked sale, in 0005's convention for sold homes (postnumber = PAON,
# poststreet = street), so a flat keys to its building as every other row does;
# since 0030 the SAON is its ``unit`` ({unit_column} is empty before then).
MINT_FROM_LEDGER_SQL = f"""
INSERT INTO staging.address_table (uid, postnumber, poststreet, posttown, postcode, postcounty{{unit_column}})
SELECT DISTINCT ON (uk.id)
       uk.id,
       coalesce(NULLIF(btrim(d.paon), ''), NULLIF(btrim(d.saon), ''), 'UNKNOWN'),
       NULLIF(btrim(d.street), ''),
       coalesce(NULLIF(btrim(d.towncity), ''), 'LONDON'),
       coalesce(n.postcode, btrim(d.postcode)),
       'GREATER LONDON'{{unit_value}}
FROM {LEDGER_TABLE} l
JOIN staging.ppd_domestic d ON d.transactionid = l.transactionid
JOIN staging.ukhcs_table uk ON uk."UPRN" = l.uprn
LEFT JOIN staging.nsul_london n ON n.uprn = l.uprn
WHERE NOT EXISTS (SELECT 1 FROM staging.address_table a WHERE a.uid = uk.id)
ORDER BY uk.id, d.dateoftransfer DESC, d.transactionid
ON CONFLICT (uid) DO NOTHING
"""

INSERT_FROM_LEDGER_SQL = f"""
INSERT INTO staging.ppd_table
    (address_id, price, dateoftransfer, propertytype, duration, oldnew, transactionid, link_method)
SELECT a.id, d.price, d.dateoftransfer::date, d.propertytype::bpchar, d.duration::bpchar,
       CASE WHEN d.oldnew IN ('Y', 'N') THEN d.oldnew::bpchar END,
       l.transactionid, l.method
FROM {LEDGER_TABLE} l
JOIN staging.ppd_domestic d ON d.transactionid = l.transactionid
JOIN staging.ukhcs_table uk ON uk."UPRN" = l.uprn
JOIN staging.address_table a ON a.uid = uk.id
WHERE NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.transactionid = l.transactionid)
ON CONFLICT ON CONSTRAINT uq_ppd_table_transaction DO NOTHING
"""


def carry_links_into_staging(engine: Engine, cfg=None, log=None) -> Dict[str, int]:
    """
    Mint address rows for the ledger's new UPRNs, then insert its sales into ``staging.ppd_table``.

    A sale whose (address, date, price) is already held (a duplicate Price
    Paid record of the same sale) is skipped by the unique key and reported.

    Returns
    -------
    dict of str to int
        ``minted`` addresses, ``inserted`` sales, ``duplicates`` skipped.
    """
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        has_unit = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'staging' "
                "AND table_name = 'address_table' AND column_name = 'unit')"
            )
        ).scalar_one()
        minted = connection.execute(
            text(MINT_FROM_LEDGER_SQL.format(
                unit_column=", unit" if has_unit else "",
                unit_value=",\n       NULLIF(upper(btrim(d.saon)), '')" if has_unit else "",
            ))
        ).rowcount
        inserted = connection.execute(text(INSERT_FROM_LEDGER_SQL)).rowcount
        missing = connection.execute(
            text(
                f"""
                SELECT count(*) FROM {LEDGER_TABLE} l
                WHERE NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.transactionid = l.transactionid)
                """
            )
        ).scalar_one()
        connection.execute(text("ANALYZE staging.address_table"))
        connection.execute(text("ANALYZE staging.ppd_table"))
    _emit(
        log,
        "sale_linking: minted %d address row(s); inserted %d sale(s); %d ledger sale(s) not "
        "inserted (their address already holds a sale of that date and price)",
        minted, inserted, missing,
    )
    return {"minted": minted, "inserted": inserted, "duplicates": missing}


# ---------------------------------------------------------------------------
# ODbL separation
# ---------------------------------------------------------------------------

ODBL_DDL = f"""
CREATE TABLE IF NOT EXISTS {ODBL_TABLE} (
    transactionid varchar PRIMARY KEY,
    uprn          bigint NOT NULL,
    method        varchar NOT NULL,
    evidence      text,
    created_at    timestamptz NOT NULL DEFAULT now()
)
"""

# OpenStreetMap-derived ledger rows: its methods, and group links propagated
# from one of them.
ODBL_METHOD_PREDICATE = "(l.method LIKE 'osm\\_%' OR l.method = 'term_osm')"


def record_odbl_links(connection, log=None) -> int:
    """
    Sync `ODBL_TABLE` with the ledger: every OpenStreetMap-derived link, nothing else.

    Returns
    -------
    int
        ODbL-derived links held.
    """
    connection.execute(text(ODBL_DDL))
    connection.execute(
        text(
            f"""
            DELETE FROM {ODBL_TABLE} o
            WHERE NOT EXISTS (
                SELECT 1 FROM {LEDGER_TABLE} l
                WHERE l.transactionid = o.transactionid AND l.uprn = o.uprn AND l.method = o.method
            )
            """
        )
    )
    connection.execute(
        text(
            f"""
            INSERT INTO {ODBL_TABLE} (transactionid, uprn, method, evidence, created_at)
            SELECT l.transactionid, l.uprn, l.method, l.evidence, l.created_at
            FROM {LEDGER_TABLE} l
            WHERE {ODBL_METHOD_PREDICATE}
            ON CONFLICT (transactionid) DO NOTHING
            """
        )
    )
    connection.execute(
        text(
            f"""
            INSERT INTO {ODBL_TABLE} (transactionid, uprn, method, evidence, created_at)
            SELECT l.transactionid, l.uprn, l.method, l.evidence, l.created_at
            FROM {LEDGER_TABLE} l
            JOIN {ODBL_TABLE} src ON src.transactionid = substring(l.evidence FROM '^group-of:(.*)$')
            WHERE l.method = 'group'
            ON CONFLICT (transactionid) DO NOTHING
            """
        )
    )
    rows = connection.execute(text(f"SELECT count(*) FROM {ODBL_TABLE}")).scalar_one()
    _emit(log, "sale_linking: %s holds %d OpenStreetMap-derived link(s) (ODbL)", ODBL_TABLE, rows)
    return rows


# ---------------------------------------------------------------------------
# UPRN-less certificates (0029)
# ---------------------------------------------------------------------------

# Resolution method -> the sale-linking method whose gate it follows.
RESOLUTION_GATES = {
    "cert_text": "group",
    "cert_flat_sequence": "flat_sequence",
    "cert_house_bracket": "house_bracket",
}

# A method's latest gate: every precision row from its latest ground-truth
# measurement on (the cross-checks that followed it) passed.
_LATEST_GATES_SQL = f"""
SELECT p.method, bool_and(p.passed)
FROM {PRECISION_TABLE} p
WHERE p.measured_at >= (
    SELECT max(q.measured_at) FROM {PRECISION_TABLE} q
    WHERE q.method = p.method AND q.ground_truth NOT LIKE 'cross:%'
)
GROUP BY p.method
"""


def gated_resolution_methods(engine: Engine) -> List[str]:
    """The `RESOLUTION_GATES` methods whose sale-linking method last passed its gate."""
    with engine.connect() as connection:
        if connection.execute(text(f"SELECT to_regclass('{PRECISION_TABLE}')")).scalar() is None:
            return []
        gates = dict(connection.execute(text(_LATEST_GATES_SQL)).all())
    return [m for m, gate in RESOLUTION_GATES.items() if gates.get(gate)]


# A certificate is UPRN-less when it has none, or one no London UPRN has
# (471 register UPRNs, such as "1" for FLAT 6, 17 WOODVILLE ROAD, NW11 9TP).
_UPRNLESS_CERTIFICATES_SQL = """
CREATE TABLE {uprnless} AS
SELECT e.certificate_number, staging.hpldn_pcnorm(e.postcode) AS pc, k.k_exact, e.property_type
FROM staging.epc_domestic_history e
CROSS JOIN LATERAL (
    SELECT staging.hpldn_addrkey(concat_ws(' ', e.address1, e.address2, e.address3)) AS k_exact
) k
WHERE e.region = 'E12000007' AND e.certificate_number IS NOT NULL
  AND (e.uprn IS NULL OR NOT EXISTS (SELECT 1 FROM staging.nsul_london n WHERE n.uprn = e.uprn))
  AND k.k_exact <> '' AND NULLIF(staging.hpldn_pcnorm(e.postcode), '') IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM {resolved} r WHERE r.certificate_number = e.certificate_number)
"""

# cert_text: the certificate's address text is a held sale's (postcode +
# normalised SAON/PAON/street), and that address's held sales name one UPRN,
# in the certificate's NSUL postcode. The held addresses are materialised
# first: the planner guesses a few thousand groups for 1.5M and would loop.
_HELD_ADDRESS_KEYS_SQL = """
CREATE TABLE {held_keys} AS
SELECT pc, k_exact, min(uprn) AS uprn
FROM (
    SELECT staging.hpldn_pcnorm(d.postcode) AS pc,
           staging.hpldn_addrkey(concat_ws(' ', d.saon, d.paon, d.street)) AS k_exact,
           uk."UPRN" AS uprn
    FROM staging.ppd_table p
    JOIN staging.ppd_domestic d ON d.transactionid = p.transactionid
    JOIN staging.address_table a ON a.id = p.address_id
    JOIN staging.ukhcs_table uk ON uk.id = a.uid
    WHERE d.county = 'GREATER LONDON'
      AND staging.hpldn_pcnorm(d.postcode) IN (SELECT pc FROM {uprnless})
) h
GROUP BY pc, k_exact
HAVING count(DISTINCT uprn) = 1
"""

_CERT_TEXT_SQL = """
INSERT INTO {found} (certificate_number, pc, k_exact, uprn, method)
SELECT c.certificate_number, c.pc, c.k_exact, s.uprn, 'cert_text'
FROM {uprnless} c
JOIN {held_keys} s ON s.pc = c.pc AND s.k_exact = c.k_exact
WHERE EXISTS (SELECT 1 FROM staging.nsul_london n WHERE n.uprn = s.uprn AND n.postcode_norm = c.pc)
"""

# Known flats for certificates, by building key ("17 WOODVILLE ROAD"): the
# sale-linking known flats, plus the register's own certificates "F <n>
# <building>" whose UPRN is in the certificate's NSUL postcode (a building
# the register has certified flat by flat, never sold).
_CERT_KNOWN_FLATS_SQL = """
CREATE TABLE {cert_flats} AS
SELECT pc, building, flat_no, min(uprn) AS uprn
FROM (
    SELECT pc, paon_norm || ' ' || street_norm AS building, flat_no, uprn
    FROM {known_flats}
    UNION ALL
    SELECT g.postcode_norm, m[2], m[1]::integer, g.uprn
    FROM staging.epc_link_gazetteer g
    JOIN staging.nsul_london n ON n.uprn = g.uprn AND n.postcode_norm = g.postcode_norm
    CROSS JOIN LATERAL regexp_match(g.k_exact, '^F ([0-9]{{1,5}}) (.+)$') m
    WHERE g.resolved_method IS NULL AND m IS NOT NULL
) s
GROUP BY pc, building, flat_no
HAVING count(DISTINCT uprn) = 1
"""

# cert_flat_sequence: "F <n> <building>" in a building whose known flats (at
# least FLAT_SEQUENCE_MIN_FLATS) all sit at UPRN = offset + flat number, and
# whose flat n is not known yet.
_CERT_FLAT_SEQUENCE_SQL = f"""
INSERT INTO {{found}} (certificate_number, pc, k_exact, uprn, method)
SELECT c.certificate_number, c.pc, c.k_exact, b.off + x.flat_no, 'cert_flat_sequence'
FROM {{uprnless}} c
CROSS JOIN LATERAL (
    SELECT (regexp_match(c.k_exact, '^F ([0-9]{{{{1,5}}}}) (.+)$'))[1]::bigint AS flat_no,
           (regexp_match(c.k_exact, '^F ([0-9]{{{{1,5}}}}) (.+)$'))[2] AS building
) x
JOIN (
    SELECT pc, building, min(uprn - flat_no) AS off
    FROM {{cert_flats}}
    GROUP BY pc, building
    HAVING count(*) >= {FLAT_SEQUENCE_MIN_FLATS} AND count(DISTINCT uprn - flat_no) = 1
) b ON b.pc = c.pc AND b.building = x.building
WHERE c.property_type IN ('Flat', 'Maisonette') AND x.flat_no IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM {{cert_flats}} k
      WHERE k.pc = b.pc AND k.building = b.building AND k.flat_no = x.flat_no
  )
  AND EXISTS (
      SELECT 1 FROM staging.nsul_london n WHERE n.uprn = b.off + x.flat_no AND n.postcode_norm = c.pc
  )
  AND NOT EXISTS (SELECT 1 FROM {{assigned}} a WHERE a.uprn = b.off + x.flat_no)
"""

# cert_house_bracket: "<n> <street>" (a house or bungalow, no unit word)
# between known houses n-2 and n+2 of that (postcode, street), n not known.
_CERT_HOUSE_BRACKET_SQL = f"""
INSERT INTO {{found}} (certificate_number, pc, k_exact, uprn, method)
SELECT x.certificate_number, x.pc, x.k_exact, min(n.uprn), 'cert_house_bracket'
FROM (
    SELECT c.certificate_number, c.pc, c.k_exact,
           least(lo.uprn, hi.uprn) AS lo, greatest(lo.uprn, hi.uprn) AS hi
    FROM {{uprnless}} c
    CROSS JOIN LATERAL (
        SELECT (regexp_match(c.k_exact, '^([0-9]{{{{1,4}}}}) (.+)$'))[1]::int AS num,
               (regexp_match(c.k_exact, '^([0-9]{{{{1,4}}}}) (.+)$'))[2] AS street_norm
    ) h
    JOIN {{known_houses}} lo ON lo.pc = c.pc AND lo.street_norm = h.street_norm AND lo.num = h.num - 2
    JOIN {{known_houses}} hi ON hi.pc = c.pc AND hi.street_norm = h.street_norm AND hi.num = h.num + 2
    WHERE c.property_type IN ('House', 'Bungalow') AND NOT staging.hpldn_has_unit(c.k_exact)
      AND abs(hi.uprn - lo.uprn) <= {HOUSE_BRACKET_MAX_GAP}
      AND NOT EXISTS (
          SELECT 1 FROM {{known_houses}} k
          WHERE k.pc = c.pc AND k.street_norm = h.street_norm AND k.num = h.num
      )
) x
JOIN staging.nsul_london n ON n.postcode_norm = x.pc AND n.uprn > x.lo AND n.uprn < x.hi
WHERE NOT EXISTS (SELECT 1 FROM {{assigned}} a WHERE a.uprn = n.uprn)
GROUP BY x.certificate_number, x.pc, x.k_exact
HAVING count(*) = 1
"""

_RESOLUTION_SQL = {
    "cert_text": _CERT_TEXT_SQL,
    "cert_flat_sequence": _CERT_FLAT_SEQUENCE_SQL,
    "cert_house_bracket": _CERT_HOUSE_BRACKET_SQL,
}


def resolve_uprnless_certificates(
    engine: Engine, methods: Sequence[str], cfg=None, log=None
) -> Dict[str, int]:
    """
    Give London certificates lodged without a UPRN one, adding to `RESOLVED_TABLE`.

    `methods` (see `RESOLUTION_GATES`; `gated_resolution_methods` gives the
    ones whose rule passed its gate): ``cert_text`` (a held sale's address
    text), ``cert_flat_sequence`` and ``cert_house_bracket`` (the L9 rules,
    over the certificate's address). An address resolved to several UPRNs,
    or an inferred UPRN claimed by several addresses, is dropped. Rows
    already resolved are kept (their UPRNs now have address rows and
    certificates, so a rebuild could no longer re-derive them); a later
    certificate at a resolved address takes its UPRN.

    Returns
    -------
    dict of str to int
        Certificates resolved per method, in all.
    """
    uprnless = "staging._cert_uprnless"
    found = "staging._cert_resolved_found"
    names = {
        "assigned": "staging._cert_resolve_assigned",
        "known_flats": "staging._cert_resolve_known_flats",
        "known_houses": "staging._cert_resolve_known_houses",
    }
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(LINK_FUNCTIONS_SQL))
        connection.execute(text(RESOLVED_DDL))
        connection.execute(text(f"DROP TABLE IF EXISTS {uprnless}"))
        connection.execute(text(_UPRNLESS_CERTIFICATES_SQL.format(uprnless=uprnless, resolved=RESOLVED_TABLE)))
        connection.execute(text(f"CREATE INDEX ON {uprnless} (pc, k_exact)"))
        connection.execute(text(f"ANALYZE {uprnless}"))
        connection.execute(text(f"DROP TABLE IF EXISTS {found}"))
        connection.execute(
            text(
                f"CREATE TABLE {found} (certificate_number varchar, pc text, k_exact text, "
                f"uprn bigint, method varchar)"
            )
        )
        # Every UPRN the register, a held sale or another source gives an
        # address to, plus those already resolved. Only carried links count:
        # an uncarried ledger may still change.
        build_assigned_uprns(
            connection, names["assigned"], _NO_LINKS,
            certified=(
                f"(SELECT uprn FROM staging.epc_domestic_history WHERE uprn IS NOT NULL "
                f"UNION SELECT uprn FROM {RESOLVED_TABLE})"
            ),
        )
        build_known_sets(connection, names, _NO_LINKS, _NO_SALES)
        fmt = dict(names, uprnless=uprnless, found=found, held_keys="staging._cert_held_keys",
                   cert_flats="staging._cert_resolve_cert_flats")
        connection.execute(text(f"DROP TABLE IF EXISTS {fmt['cert_flats']}"))
        connection.execute(text(_CERT_KNOWN_FLATS_SQL.format(**fmt)))
        connection.execute(text(f"CREATE INDEX ON {fmt['cert_flats']} (pc, building, flat_no)"))
        connection.execute(text(f"ANALYZE {fmt['cert_flats']}"))
        if "cert_text" in methods:
            connection.execute(text(f"DROP TABLE IF EXISTS {fmt['held_keys']}"))
            connection.execute(text(_HELD_ADDRESS_KEYS_SQL.format(**fmt)))
            connection.execute(text(f"CREATE INDEX ON {fmt['held_keys']} (pc, k_exact)"))
            connection.execute(text(f"ANALYZE {fmt['held_keys']}"))
        for method in methods:
            connection.execute(text(_RESOLUTION_SQL[method].format(**fmt)))
        # A certificate lodged since, at an address already resolved.
        connection.execute(
            text(
                f"""
                INSERT INTO {found} (certificate_number, pc, k_exact, uprn, method)
                SELECT c.certificate_number, c.pc, c.k_exact, r.uprn, r.method
                FROM {uprnless} c
                JOIN (
                    SELECT u.pc, u.k_exact, min(r.uprn) AS uprn, min(r.method) AS method
                    FROM {RESOLVED_TABLE} r
                    JOIN staging.epc_domestic_history e ON e.certificate_number = r.certificate_number
                    CROSS JOIN LATERAL (
                        SELECT staging.hpldn_pcnorm(e.postcode) AS pc,
                               staging.hpldn_addrkey(concat_ws(' ', e.address1, e.address2, e.address3)) AS k_exact
                    ) u
                    GROUP BY 1, 2
                    HAVING count(DISTINCT r.uprn) = 1
                ) r ON r.pc = c.pc AND r.k_exact = c.k_exact
                """
            )
        )
        # One address, one UPRN; an inferred UPRN, one address.
        connection.execute(
            text(
                f"""
                DELETE FROM {found} f
                USING (
                    SELECT pc, k_exact FROM {found} GROUP BY 1, 2 HAVING count(DISTINCT uprn) > 1
                ) x
                WHERE x.pc = f.pc AND x.k_exact = f.k_exact
                """
            )
        )
        connection.execute(
            text(
                f"""
                DELETE FROM {found} f
                USING (
                    SELECT uprn FROM {found} WHERE method <> 'cert_text'
                    GROUP BY 1 HAVING count(DISTINCT pc || '|' || k_exact) > 1
                ) x
                WHERE x.uprn = f.uprn
                """
            )
        )
        added = dict(
            connection.execute(
                text(
                    f"""
                    WITH ins AS (
                        INSERT INTO {RESOLVED_TABLE} (certificate_number, uprn, method)
                        SELECT DISTINCT ON (certificate_number) certificate_number, uprn, method
                        FROM {found}
                        ORDER BY certificate_number, method
                        ON CONFLICT (certificate_number) DO NOTHING
                        RETURNING method
                    )
                    SELECT method, count(*) FROM ins GROUP BY 1
                    """
                )
            ).all()
        )
        connection.execute(text(f"ANALYZE {RESOLVED_TABLE}"))
        counts = dict(
            connection.execute(text(f"SELECT method, count(*) FROM {RESOLVED_TABLE} GROUP BY 1")).all()
        )
        total = connection.execute(text(f"SELECT count(*) FROM {uprnless}")).scalar_one()
        for name in (uprnless, found, fmt["held_keys"], fmt["cert_flats"], *names.values()):
            connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
    _emit(log, "sale_linking: resolved %s more of %d unresolved UPRN-less London certificate(s) "
          "(methods %s); %s now hold %s", added, total, list(methods), RESOLVED_TABLE, counts)
    return counts


def refresh_certificate_gazetteer(engine: Engine, cfg=None, log=None) -> Dict[str, int]:
    """
    Resolve UPRN-less certificates by the methods whose rules last passed their gates, then rebuild the gazetteer.

    Returns
    -------
    dict of str to int
        Certificates resolved per method, in all.
    """
    counts = resolve_uprnless_certificates(engine, gated_resolution_methods(engine), cfg, log)
    build_epc_link_gazetteer(engine, cfg, log)
    return counts


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

STAGE_A_METHODS = TIER_METHODS["L1"] + TIER_METHODS["L2"] + TIER_METHODS["L3"] + TIER_METHODS["L5"] + TIER_METHODS["L7"]


def _pending_view(connection, name: str) -> None:
    """(Re)create `name`: the targets without a ledger row."""
    connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
    connection.execute(
        text(
            f"""
            CREATE TABLE {name} AS
            SELECT t.* FROM {TARGET_TABLE} t
            WHERE NOT EXISTS (SELECT 1 FROM {LEDGER_TABLE} l WHERE l.transactionid = t.transactionid)
            """
        )
    )
    connection.execute(text(f"ALTER TABLE {name} ADD PRIMARY KEY (transactionid)"))
    connection.execute(text(f"CREATE INDEX ON {name} (pc, street_norm)"))
    connection.execute(text(f"ANALYZE {name}"))


def _drop_uncarried_conflicts(connection, log=None) -> int:
    """
    Remove not-yet-carried ledger rows of address groups now in conflict.

    A later stage (elimination, group) can disagree with an earlier stage's
    link in the same address group; the whole group then stays unlinked.
    """
    removed = connection.execute(
        text(
            f"""
            DELETE FROM {LEDGER_TABLE} l
            USING {TARGET_TABLE} t, staging._link_conflict_groups x
            WHERE t.transactionid = l.transactionid
              AND x.pc = t.pc AND x.k_exact = t.k_exact AND t.k_exact <> ''
              AND NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.transactionid = l.transactionid)
            """
        )
    ).rowcount
    if removed:
        _emit(log, "sale_linking: removed %d earlier link(s) of address groups now in conflict", removed)
    return removed


def link_sales(engine: Engine, cfg=None, log=None) -> Dict[str, object]:
    """
    Link the sales ``staging.ppd_table`` does not hold, tier by tier, into the ledger.

    Needs every source loaded (``staging.nsul_london``, ``uprn_usrn``,
    ``uprn_toid``, ``usrn_street``, ``osm_address``,
    ``planning_link_gazetteer``, ``hmlr_uprn_lookup``,
    ``postcode_centroid_history``, ``epc_link_gazetteer``) and 0027's
    ``transactionid`` back-fill. Writes `LEDGER_TABLE` (dropping earlier
    links not yet carried into ``ppd_table``) and `UNLINKED_TABLE`; carries
    nothing (`carry_links_into_staging` does).

    Order: ground truth and precision of every method; L1-L3, L5, L7
    (strongest first, conflicts refused per address group); L6 eliminations
    on what is left; L9 flat sequences and house brackets (on every target,
    so they can put an earlier link in conflict); L8 terminated postcodes;
    L4 address-group propagation; unlinked reasons; `ODBL_TABLE`.

    Returns
    -------
    dict
        ``targets``, ``enabled`` methods, ``links`` per method, ``unlinked`` per reason.
    """
    from hpldn.backend.scripts import link_precision as lp

    targets = build_link_targets(engine, cfg, log)
    with engine.begin() as connection:
        connection.execute(text(LEDGER_DDL))
        connection.execute(text(PRECISION_DDL))
        connection.execute(
            text(
                f"""
                DELETE FROM {LEDGER_TABLE} l
                WHERE NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.transactionid = l.transactionid)
                """
            )
        )
        mark_live_postcodes(connection, TARGET_TABLE)
        connection.execute(text("DROP TABLE IF EXISTS staging._link_groups"))
        connection.execute(text(GROUPS_SQL.format(groups="staging._link_groups", target=TARGET_TABLE)))
        connection.execute(text("CREATE INDEX ON staging._link_groups (pc, k_exact)"))
        connection.execute(text("CREATE INDEX ON staging._link_groups (transactionid)"))
        connection.execute(text("ANALYZE staging._link_groups"))
        connection.execute(text("DROP TABLE IF EXISTS staging._link_conflict_groups"))
        connection.execute(text("CREATE TABLE staging._link_conflict_groups (pc text, k_exact text)"))
        build_known_addresses(connection, log)
        # osm_polygon_isolated's "addressed" UPRNs (the ledger holds only
        # carried links here, which address rows already cover).
        build_assigned_uprns(connection, ASSIGNED_TABLE)

    lp.build_ground_truth(engine, cfg, log)
    gated = lp.measure_methods(
        engine, [m for m in STAGE_A_METHODS + TIER_METHODS["L8"]], cfg, log
    )

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(f"DROP TABLE IF EXISTS {CANDIDATE_TABLE}"))
        run_candidate_tiers(connection, TARGET_TABLE, CANDIDATE_TABLE, STAGE_A_METHODS, log)
    # UCL's ground truth (GT1) overlap is too small to gate it: gate it on
    # agreement with the independent exact certificate-address links instead.
    gated["ucl"] = lp.measure_cross(engine, "ucl", "epc_exact", cfg, log)
    # Ground truth is homes UCL linked; the unlinked sales mostly are not. A
    # method must also agree with UCL / exact certificate links on the real
    # sales where both speak.
    lp.cross_check(engine, gated, STAGE_A_METHODS, cfg, log)

    enabled = [m for m in METHOD_ORDER if gated.get(m)]
    links: Dict[str, int] = {}

    def resolve(methods: Sequence[str]) -> None:
        with engine.begin() as connection:
            apply_bulk_session_tuning(connection, cfg, log)
            for method, n in resolve_candidates(
                connection, TARGET_TABLE, CANDIDATE_TABLE, "staging._link_groups",
                [m for m in methods if m in enabled], log,
            ).items():
                links[method] = links.get(method, 0) + n
            _drop_uncarried_conflicts(connection, log)

    resolve(STAGE_A_METHODS)

    # L6: eliminations over what is still pending.
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        _pending_view(connection, "staging._link_pending")
        tables = elimination_tables(
            connection, "staging._link_pending", TARGET_TABLE, LEDGER_TABLE, "staging._link_elim"
        )
    for method, ok in lp.measure_elimination(engine, tables, cfg, log).items():
        gated[method] = ok
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        fmt = dict(tables, cand=CANDIDATE_TABLE, pending="staging._link_pending")
        for method, sql in (("street_elimination", STREET_ELIMINATION_SQL),
                            ("building_elimination", BUILDING_ELIMINATION_SQL)):
            n = connection.execute(text(sql.format(**fmt))).rowcount
            _emit(log, "sale_linking: %s -> %d candidate(s)", method, n)
    lp.cross_check(engine, gated, TIER_METHODS["L6"], cfg, log)
    enabled = [m for m in METHOD_ORDER if gated.get(m)]
    resolve(TIER_METHODS["L6"])

    # L9: flat sequences and house brackets. The working tables are rebuilt
    # so that L6's links count as known and assigned.
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        _pending_view(connection, "staging._link_pending")
        tables = elimination_tables(
            connection, "staging._link_pending", TARGET_TABLE, LEDGER_TABLE, "staging._link_elim"
        )
    for method, ok in lp.measure_sequences(engine, tables, cfg, log).items():
        gated[method] = ok
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        run_sequence_tiers(connection, TARGET_TABLE, CANDIDATE_TABLE, tables, log)
    lp.cross_check(engine, gated, TIER_METHODS["L9"], cfg, log)
    enabled = [m for m in METHOD_ORDER if gated.get(m)]
    resolve(TIER_METHODS["L9"])

    # L8: terminated postcodes.
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        run_candidate_tiers(connection, TARGET_TABLE, CANDIDATE_TABLE, TIER_METHODS["L8"], log)
    lp.cross_check(engine, gated, TIER_METHODS["L8"], cfg, log)
    enabled = [m for m in METHOD_ORDER if gated.get(m)]
    resolve(TIER_METHODS["L8"])

    # L4: address-group propagation, gated on held ground truth.
    gated["group"] = lp.measure_group(engine, "staging._link_groups", cfg, log)
    enabled = [m for m in METHOD_ORDER if gated.get(m)]
    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        n = connection.execute(
            text(GROUP_PROPAGATION_SQL.format(
                cand=CANDIDATE_TABLE, target=TARGET_TABLE, groups="staging._link_groups"
            ))
        ).rowcount
        _emit(log, "sale_linking: group -> %d candidate(s)", n)
    resolve(TIER_METHODS["L4"])

    with engine.begin() as connection:
        unlinked = record_unlinked(connection, TARGET_TABLE, CANDIDATE_TABLE, enabled, log)
        final = dict(
            connection.execute(
                text(
                    f"""
                    SELECT method, count(*) FROM {LEDGER_TABLE} l
                    WHERE NOT EXISTS (SELECT 1 FROM staging.ppd_table p WHERE p.transactionid = l.transactionid)
                    GROUP BY 1
                    """
                )
            ).all()
        )
        record_odbl_links(connection, log)
        for name in ("staging._link_pending", *tables.values()):
            connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
    _emit(log, "sale_linking: %d target sale(s); links by method %s; unlinked %s", targets, final, unlinked)
    return {"targets": targets, "enabled": enabled, "links": final, "unlinked": unlinked}
