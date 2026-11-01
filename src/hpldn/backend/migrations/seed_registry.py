r"""
The declarative registry of every raw source the database is seeded from.

Migration 0003 runs `run_sources`, which loads the ordered `SOURCES` top to
bottom; ``bare_metal --check`` and 0003 both call `check_sources` first so
every missing file is reported at once. Paths resolve through
``migration_helpers.require_data_path``, which reads ``migrations/inputs.yaml``.
Sources load by ``\copy`` over psql
(`PSQL_COPY`), pandas ``to_sql`` for small lookups (`PANDAS`), or a
``pipeline_steps`` loader (`STEP`). See ``migrations/README.md`` for the two
load-order rules encoded here.
"""

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Engine, text

from hpldn.backend.common.tuning import get_bulk_copy_env, get_copy_jobs
from hpldn.backend.scripts.migration_helpers import DataSourceMissing, require_data_path

PSQL_COPY = "psql_copy"
PANDAS = "pandas"
STEP = "step"


@dataclass(frozen=True)
class SeedSource:
    r"""
    One raw source: where it comes from, where it lands and how it loads.

    Attributes
    ----------
    name : str
        Stable identifier, used by ``run_sources(names=...)`` and in logs.
    target : str
        Relation it lands in, ``schema.table``.
    mechanism : str
        `PSQL_COPY`, `PANDAS` or `STEP`.
    cfg_key : str
        Key in ``migrations/inputs.yaml``, which holds the path; a
        ``staging.yaml`` ``data:`` entry of the same name overrides it.
    description : str
        What reads it, quoted in the missing-file error.
    live_rows : int, optional
        Row count of a reference build; informational only.
    is_dir : bool
        Whether ``cfg_key`` names a directory.
    filenames : tuple of str, optional
        Files to load from a directory source, in order; every ``*.csv``
        sorted when None.
    columns : tuple of str, optional
        Explicit ``\copy`` column list, required for headerless files.
    header : bool
        Whether the files carry a header row.
    parallel : bool
        Load files concurrently, one psql process each; only for append-only
        targets and disjoint files.
    truncate_first : bool
        TRUNCATE the target first, which makes a re-run idempotent.
    truncate_cascade : bool
        Use ``TRUNCATE ... CASCADE``. Only ``ukhcs_table`` needs it, because
        tables referencing it are still empty (or reloaded later) when it is
        the first source loaded.
    post_load : str, optional
        SQL run once after all files are in.
    step : str, optional
        Attribute name on ``pipeline_steps`` for `STEP` sources.
    drop_before_step : tuple of str, optional
        Relations to drop before ``step`` runs. ``ptal_2023`` needs it so a
        re-run can drop the table ``ptal_cell_polygons`` references.
    pandas_loader : callable, optional
        Custom loader for `PANDAS` sources.
    notes : str
        Free-text notes.
    """

    name: str
    target: str
    mechanism: str
    cfg_key: str
    description: str
    live_rows: Optional[int] = None
    is_dir: bool = False
    filenames: Optional[Tuple[str, ...]] = None
    columns: Optional[Tuple[str, ...]] = None
    header: bool = True
    parallel: bool = False
    truncate_first: bool = True
    truncate_cascade: bool = False
    post_load: Optional[str] = None
    step: Optional[str] = None
    drop_before_step: Optional[Tuple[str, ...]] = None
    pandas_loader: Optional[Callable] = None
    notes: str = ""


# Column lists for headerless sources. PPD "complete" files are positional,
# so this must match staging.ppd_domestic's DDL and the file layout.
PPD_DOMESTIC_COLUMNS = (
    "transactionid", "price", "dateoftransfer", "postcode", "propertytype",
    "oldnew", "duration", "paon", "saon", "street", "locality", "towncity",
    "district", "county", "categorytype", "recordstatus",
)

# OS Code-Point Open: 10 columns, no header, one CSV per postcode area.
CODEPOINT_OPEN_COLUMNS = (
    "postcode", "positional_quality", "eastings", "northings", "country_code",
    "nhs_regional_ha_code", "nhs_ha_code", "admin_county_code",
    "admin_district_code", "admin_ward_code",
)

# The five OS Open UPRN columns kept, quoted because the ORM declares them upper-case.
UKHCS_COLUMNS = ('"UPRN"', '"X_COORDINATE"', '"Y_COORDINATE"', '"LATITUDE"', '"LONGITUDE"')

# staging.epc_domestic's scope: the fusion-window years only.
EPC_DOMESTIC_YEARS = (2024, 2025, 2026)
EPC_DOMESTIC_FILENAMES = tuple(f"certificates-{y}.csv" for y in EPC_DOMESTIC_YEARS)

PPD_DOMESTIC_FILENAMES = ("pp-2024.csv", "pp-2025.csv", "pp-2026.csv")

# staging.epc_domestic_history's scope, mirroring pipeline_steps; listed so the
# pre-flight names certificate files rather than globbing recommendations CSVs.
EPC_HISTORY_YEARS = tuple(range(2008, 2027))
EPC_HISTORY_FILENAMES = tuple(f"certificates-{y}.csv" for y in EPC_HISTORY_YEARS)


# The registry, in load order.
SOURCES: Tuple[SeedSource, ...] = (
    # UPRN coordinates: a full load, then an insert-only append (rule 1).
    SeedSource(
        name="ukhcs_table",
        target="staging.ukhcs_table",
        mechanism=PSQL_COPY,
        cfg_key="osopenuprn_csv_fp",
        description="OS Open UPRN 202604 extract -> staging.ukhcs_table",
        live_rows=41_633_944,
        columns=UKHCS_COLUMNS,
        header=True,
        truncate_cascade=True,
        notes=(
            "Phase 1 of 2. Phase 2 is ukhcs_table_refresh, below. "
            "truncate_cascade=True: address_table and london_parcels both "
            "FK to this table, so a plain TRUNCATE is rejected by "
            "Postgres. Safe to cascade -- both are still empty this early "
            "in the run, or get reloaded later in the same run."
        ),
    ),
    SeedSource(
        name="ukhcs_table_refresh",
        target="staging.ukhcs_table",
        mechanism=STEP,
        step="refresh_ukhcs_table_from_extract",
        cfg_key="osopenuprn_refresh_csv_fp",
        description="OS Open UPRN 202608 extract, INSERT-ONLY append",
        truncate_first=False,
        notes=(
            "Phase 2 of 2, and the load-bearing half. Adds only UPRNs absent "
            "from phase 1 and deliberately does NOT apply the ~192k "
            "coordinate revisions to existing rows. Applying them would "
            "move every spatial feature."
        ),
    ),
    # EPC: two tables, same directory, different scopes (rule 2).
    SeedSource(
        name="epc_domestic",
        target="staging.epc_domestic",
        mechanism=PSQL_COPY,
        cfg_key="epc_domestic_dir",
        description="EPC certificates 2024-2026, national -> production.epc_table",
        live_rows=4_508_685,
        is_dir=True,
        filenames=EPC_DOMESTIC_FILENAMES,
        header=True,
        parallel=True,
        notes=(
            "One CSV per year, read from the epc_domestic_dir directory "
            "resolved through inputs.yaml."
        ),
    ),
    SeedSource(
        name="epc_domestic_history",
        target="staging.epc_domestic_history",
        mechanism=PSQL_COPY,
        cfg_key="epc_domestic_dir",
        description=(
            "EPC certificates 2008-2026, London-filtered -> the epc_addrkey "
            "gazetteer"
        ),
        live_rows=4_594_106,
        is_dir=True,
        filenames=EPC_HISTORY_FILENAMES,
        header=True,
        parallel=True,
        notes=(
            "~27 GB across 19 files. Loaded through N per-worker UNLOGGED "
            "scratch tables and one filtered INSERT ... SELECT each, rather "
            "than migration 0065's single scratch with a TRUNCATE between "
            "years. Peak extra disk is N national years, not 27 GB."
        ),
    ),
    # Price Paid
    SeedSource(
        name="ppd_domestic",
        target="staging.ppd_domestic",
        mechanism=PSQL_COPY,
        cfg_key="ppd_domestic_dir",
        description="HM Land Registry Price Paid 2024-2026, national",
        live_rows=2_208_116,
        is_dir=True,
        filenames=PPD_DOMESTIC_FILENAMES,
        columns=PPD_DOMESTIC_COLUMNS,
        header=False,
        parallel=True,
    ),
    # Postcode centroids
    SeedSource(
        name="codepoint_open",
        target="staging.codepoint_open",
        mechanism=PSQL_COPY,
        cfg_key="codepoint_open_dir",
        description=(
            "OS Code-Point Open postcode centroids -> the ppd_uprn_linkage "
            "geometric-plausibility guard"
        ),
        live_rows=1_749_109,
        is_dir=True,
        filenames=None,
        columns=CODEPOINT_OPEN_COLUMNS,
        header=False,
        parallel=True,
        post_load=(
            "CREATE INDEX IF NOT EXISTS idx_codepoint_open_pcnorm "
            "ON staging.codepoint_open (staging.hpldn_pcnorm(postcode))"
        ),
        notes=(
            "The post_load index is a functional index on "
            "staging.hpldn_pcnorm, which is why migration 0002 -- not 0006 "
            "-- creates the four staging.hpldn_* IMMUTABLE functions."
        ),
    ),
    # Tier A input (HPLDN_DATA_DIR/migration_inputs, backed up to OCI)
    SeedSource(
        name="ppd_uprn_linkage_tiera",
        target="staging._ppd_uprn_linkage_tiera",
        mechanism=PSQL_COPY,
        cfg_key="ppd_uprn_linkage_tiera_csv_fp",
        description="hand-produced Tier A PPD/UPRN linkage fixture",
        live_rows=54_137,
        header=True,
        notes=(
            "The one genuinely irreproducible input in the chain. Kept until "
            "a set-difference on (address_id, dateoftransfer, price) against "
            "staging.ppd_uprn_linkage comes back empty -- migration 0006 "
            "runs and reports exactly that comparison."
        ),
    ),
    # Geography lookups (pandas)
    SeedSource(
        name="lsoa",
        target="staging.lsoa",
        mechanism=PANDAS,
        cfg_key="lsoa_lookup_csv_fp",
        description="LSOA11 -> LSOA21 -> LAD22 best-fit lookup",
        live_rows=34_753,
        notes=(
            "Path resolved through inputs.yaml (lsoa_lookup_csv_fp), under "
            "$HPLDN_DATA_DIR."
        ),
    ),
    SeedSource(
        name="msoa",
        target="staging.msoa",
        mechanism=PANDAS,
        cfg_key="msoa_lookup_csv_fp",
        description="MSOA11 -> MSOA21 -> LAD22 best-fit lookup",
        live_rows=7_201,
    ),
    SeedSource(
        name="lsoa_msoa_lu",
        target="staging.lsoa_msoa_lu",
        mechanism=STEP,
        step="create_lsoa_msoa_lookup_table",
        cfg_key="lsoa_msoa_lookup_csv_fp",
        description="LSOA11 -> MSOA11 lookup (real codes, not 0050's LAD fudge)",
        live_rows=34_753,
    ),
    SeedSource(
        name="lsoa_boundaries",
        target="staging.lsoa_boundaries",
        mechanism=STEP,
        step="create_lsoa_boundaries_table",
        cfg_key="lsoa_boundaries_csv_fp",
        description="LSOA 2011 boundary WKT, London-filtered on load",
        live_rows=4_835,
        notes="WKT -> geometry(MultiPolygon,4326) plus a GIST index.",
    ),
    SeedSource(
        name="iod_lsoa_raw",
        target="staging.iod_lsoa_raw",
        mechanism=STEP,
        step="create_iod_lsoa_raw_table",
        cfg_key="iod_csv_dir",
        description="English Indices of Deprivation 2010/2015/2019/2025",
        live_rows=131_925,
        is_dir=True,
        notes=(
            "Four releases with four different column layouts, concatenated "
            "through _IOD_COLUMN_MAP and checked against "
            "_IOD_EXPECTED_ROW_COUNTS."
        ),
    ),
    # PTAL grid
    SeedSource(
        name="ptal_2023",
        target="staging.ptal_2023",
        mechanism=STEP,
        step="create_ptal_2023_table",
        cfg_key="ptal_csv_fp",
        description="TfL PTAL 2023 100m grid, accessibility bands",
        live_rows=159_451,
        drop_before_step=("staging.ptal_cell_polygons",),
        notes=(
            "drop_before_step covers a table create_ptal_2023_table itself "
            "does not drop: staging.ptal_cell_polygons FKs to ptal_2023.grid_id "
            "and, on a re-run after a prior run got that far, blocks "
            "create_ptal_2023_table's own DROP TABLE staging.ptal_2023 with "
            "'cannot drop table ... because other objects depend on it'. "
            "Safe to drop here -- ptal_cell_polygons, below, recreates it."
        ),
    ),
    SeedSource(
        name="ptal_coords",
        target="staging.ptal_coords",
        mechanism=STEP,
        step="create_ptal_coords_table",
        cfg_key="ptal_centroids_csv_fp",
        description="PTAL grid cell centroids (FETCHED, not downloaded)",
        live_rows=159_451,
    ),
    SeedSource(
        name="ptal_cell_polygons",
        target="staging.ptal_cell_polygons",
        mechanism=STEP,
        step="create_ptal_cell_polygons_table",
        cfg_key="ptal_polygons_csv_fp",
        description="PTAL grid cell polygons (FETCHED, not downloaded)",
        live_rows=159_451,
    ),
    # GML via ogr2ogr (needs GDAL and PostGIS). INSPIRE parcels load in 0005
    # because they join staging.london_houses_condensed; see EXTERNAL_SOURCES.
    SeedSource(
        name="rivers",
        target="staging.rivers",
        mechanism=STEP,
        step="create_staging_rivers_table",
        cfg_key="open_rivers_gml_fp",
        description="OS Open Rivers WatercourseLink, clipped to Greater London",
        live_rows=1_613,
    ),
    SeedSource(
        name="london_green_spaces",
        target="staging.london_green_spaces",
        mechanism=STEP,
        step="create_london_green_spaces_tables",
        cfg_key="open_green_spaces_gml_fp",
        description=(
            "OS Open Greenspace TQ tile -> staging.london_green_spaces "
            "+ staging.london_green_space_access_points"
        ),
        live_rows=21_992,
        notes=(
            "Path resolved through inputs.yaml (open_green_spaces_gml_fp); "
            "the GML must exist on every replay."
        ),
    ),
)

SOURCES_BY_NAME = {source.name: source for source in SOURCES}

# Sources loaded outside this registry (staging.ukhpucl by StagingCopy,
# iod_releases literals, 0009's reference CSVs, 0005's INSPIRE parcels),
# listed so the inventory is complete. See migrations/README.md.
EXTERNAL_SOURCES = (
    "staging.ukhpucl (StagingCopy, between 0001 and 0002)",
    "staging.iod_releases (in-code literals, migration 0011)",
    "production.boe_rate / london_stations / ukhpi / mortgage_rates "
    "(migration 0009)",
    "staging.inspire_polygons / staging.london_parcels (migration 0005)",
)


# Path resolution and pre-flight.
def source_path(source: SeedSource, cfg: Optional[Dict]) -> Path:
    """
    Return the resolved path for one source, checked for existence.

    Raises
    ------
    DataSourceMissing
        If it is not on disk.
    """
    return require_data_path(
        cfg,
        source.cfg_key,
        description=source.description,
        must_be_dir=source.is_dir,
    )


def source_files(source: SeedSource, cfg: Optional[Dict]) -> List[Path]:
    """
    Return the files a `PSQL_COPY` source loads, in load order.

    A directory source yields its declared ``filenames`` or, when None, every
    ``*.csv`` under it, sorted.
    """
    root = source_path(source, cfg)
    if not source.is_dir:
        return [root]
    if source.filenames is not None:
        files = [root / name for name in source.filenames]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise DataSourceMissing(
                f"{source.name}: {len(missing)} expected file(s) missing under "
                f"{root} (input {source.cfg_key}):\n  " + "\n  ".join(missing)
            )
        return files
    files = sorted(root.glob("*.csv"))
    if not files:
        raise DataSourceMissing(
            f"{source.name}: no *.csv files under {root} "
            f"(input {source.cfg_key}) — {source.description}"
        )
    return files


def check_sources(
    cfg: Optional[Dict],
    names: Optional[Sequence[str]] = None,
    log: Optional[logging.Logger] = None,
) -> None:
    """
    Check every selected source is on disk, reporting all failures at once.

    Parameters
    ----------
    cfg : dict or None
        ``staging.yaml``; resolved from disk when None.
    names : sequence of str, optional
        Sources to check; all by default.
    log : logging.Logger, optional
        Logger for progress messages.

    Raises
    ------
    DataSourceMissing
        Listing every missing source.
    """
    selected = _select(names)
    failures: List[str] = []
    for source in selected:
        try:
            if source.mechanism == PSQL_COPY:
                source_files(source, cfg)
            else:
                source_path(source, cfg)
        except (DataSourceMissing, KeyError) as err:
            failures.append(f"[{source.name}] {err}")
    if failures:
        raise DataSourceMissing(
            f"{len(failures)} of {len(selected)} seed source(s) are missing on "
            "this machine:\n\n" + "\n\n".join(failures)
        )
    if log is not None:
        log.info(
            "All %d registered seed sources are present on this machine",
            len(selected),
        )


def _select(names: Optional[Sequence[str]]) -> List[SeedSource]:
    """
    Return the named sources in registry order, or all of them.

    Raises
    ------
    KeyError
        If a name is not registered.
    """
    if names is None:
        return list(SOURCES)
    unknown = [n for n in names if n not in SOURCES_BY_NAME]
    if unknown:
        raise KeyError(
            f"unknown seed source(s) {unknown}; known: "
            f"{sorted(SOURCES_BY_NAME)}"
        )
    return [s for s in SOURCES if s.name in set(names)]


# Executors.
def _psql_copy_command(db_url: str, copy_sql: str) -> List[str]:
    r"""
    Return a psql argv running one ``\copy`` with ``ON_ERROR_STOP=1``.

    The password is never in argv; it travels in ``PGPASSWORD``.
    """
    return ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-c", copy_sql]


def _copy_sql(target: str, csv_fp: Path, columns, header: bool) -> str:
    r"""Return a CSV ``\copy`` command for `csv_fp` into `target`."""
    column_sql = f" ({', '.join(columns)})" if columns else ""
    header_sql = "TRUE" if header else "FALSE"
    return (
        f"\\copy {target}{column_sql} FROM '{csv_fp}' "
        f"WITH (FORMAT CSV, HEADER {header_sql})"
    )


def _max_jobs(cfg: Optional[Dict], log: Optional[logging.Logger]) -> int:
    """Return how many psql processes a parallel source runs: `get_copy_jobs`, capped at 6."""
    override = ((cfg or {}).get("seed") or {}).get("max_parallel_copy_jobs")
    if override:
        return max(1, int(override))
    try:
        return max(1, min(6, int(get_copy_jobs(log=log))))
    except Exception:  # pragma: no cover - tuning is best-effort
        return 4


def _copy_via_psql(
    engine: Engine,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
    source: SeedSource,
) -> int:
    r"""
    ``\copy`` every file of `source` into its target table.

    Truncates first when ``truncate_first``. ``parallel`` sources run one psql
    process per file, valid only for append-only targets and disjoint files.

    Returns
    -------
    int
        Rows in the target after the load.
    """
    from hpldn.backend.scripts.migration_helpers import get_psql_env, get_psql_url

    files = source_files(source, cfg)
    db_url = get_psql_url(engine)
    env = get_psql_env(engine, get_bulk_copy_env(log))

    if source.truncate_first:
        cascade_sql = " CASCADE" if source.truncate_cascade else ""
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE TABLE {source.target}{cascade_sql}"))

    def _one(csv_fp: Path) -> None:
        """Copy one file; ``pool.map`` is drained so worker errors propagate."""
        sql = _copy_sql(source.target, csv_fp, source.columns, source.header)
        if log is not None:
            log.info("0003: %s <- %s", source.target, csv_fp.name)
        subprocess.run(_psql_copy_command(db_url, sql), check=True, env=env)

    if source.parallel and len(files) > 1:
        jobs = min(_max_jobs(cfg, log), len(files))
        if log is not None:
            log.info(
                "0003: loading %d file(s) into %s across %d parallel psql "
                "job(s)", len(files), source.target, jobs,
            )
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(_one, files))
    else:
        for csv_fp in files:
            _one(csv_fp)

    with engine.begin() as connection:
        if source.post_load:
            connection.execute(text(source.post_load))
        connection.execute(text(f"ANALYZE {source.target}"))
        rows = connection.execute(
            text(f"SELECT count(*) FROM {source.target}")
        ).scalar_one()
    _report(log, source, rows)
    return rows


def _load_via_pandas(
    engine: Engine,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
    source: SeedSource,
) -> int:
    """
    Load a small reference CSV with pandas ``read_csv`` and ``to_sql``.

    Column names are lower-cased, ``ObjectId`` becomes ``object_id`` and the
    ONS export's BOM is stripped, matching the 0002 DDL.
    """
    import pandas as pd

    csv_fp = source_path(source, cfg)
    frame = pd.read_csv(csv_fp, encoding="utf-8-sig")
    frame.columns = [
        "object_id" if column == "ObjectId" else column.lower()
        for column in frame.columns
    ]
    schema, table = source.target.split(".", 1)
    with engine.begin() as connection:
        if source.truncate_first:
            connection.execute(text(f"TRUNCATE TABLE {source.target}"))
        frame.to_sql(
            table, connection, schema=schema, if_exists="append", index=False
        )
        connection.execute(text(f"ANALYZE {source.target}"))
        rows = connection.execute(
            text(f"SELECT count(*) FROM {source.target}")
        ).scalar_one()
    _report(log, source, rows)
    return rows


def _load_via_step(
    engine: Engine,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
    source: SeedSource,
) -> int:
    """
    Dispatch to the ``pipeline_steps`` loader named by ``source.step``.

    For sources whose landing is not a straight copy: GML clipping, PTAL band
    ranks and geometry, the IoD column map and the insert-only UPRN refresh.
    The path has already been resolved and checked.
    """
    from hpldn.backend.scripts import pipeline_steps as steps

    func = getattr(steps, source.step)
    if source.drop_before_step:
        with engine.begin() as connection:
            for relation in source.drop_before_step:
                if log is not None:
                    log.info(
                        "0003: dropping %s before %s (dependent FK would "
                        "otherwise block the step's own DROP TABLE on a "
                        "re-run)", relation, source.step,
                    )
                connection.execute(text(f"DROP TABLE IF EXISTS {relation}"))
    if log is not None:
        log.info(
            "0003: %s <- pipeline_steps.%s (%s)",
            source.target, source.step, source.cfg_key,
        )
    func(engine, cfg, log)

    with engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT count(*) FROM {source.target}")
        ).scalar_one()
    _report(log, source, rows)
    return rows


def _copy_epc_history(
    engine: Engine,
    cfg: Optional[Dict],
    log: Optional[logging.Logger],
    source: SeedSource,
) -> int:
    """
    Load the 2008-2026 London EPC history, several years at a time.

    Each worker copies one year into its own UNLOGGED scratch table, inserts
    the London rows (region E12000007) and truncates the scratch table, so
    peak extra disk stays at a few years rather than all ~27 GB. Workers are
    capped by `_max_jobs`.

    Returns
    -------
    int
        Rows inserted.
    """
    from hpldn.backend.scripts import pipeline_steps as steps
    from hpldn.backend.scripts.migration_helpers import get_psql_env, get_psql_url

    root = source_path(source, cfg)
    years = list(
        range(
            steps.EPC_DOMESTIC_HISTORY_YEAR_START,
            steps.EPC_DOMESTIC_HISTORY_YEAR_END + 1,
        )
    )
    files = [(year, root / f"certificates-{year}.csv") for year in years]
    missing = [str(fp) for _, fp in files if not fp.is_file()]
    if missing:
        raise DataSourceMissing(
            f"epc_domestic_history: {len(missing)} yearly certificate file(s) "
            f"missing under {root} (input {source.cfg_key}):\n  "
            + "\n  ".join(missing)
        )

    db_url = get_psql_url(engine)
    env = get_psql_env(engine, get_bulk_copy_env(log))
    jobs = min(_max_jobs(cfg, log), len(files))
    select_sql = steps._epc_domestic_history_select_sql()

    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {source.target}"))
        for slot in range(jobs):
            scratch = f"staging._epc_history_scratch_{slot}"
            connection.execute(text(f"DROP TABLE IF EXISTS {scratch}"))
            connection.execute(
                text(
                    steps._EPC_DOMESTIC_HISTORY_SCRATCH_DDL.replace(
                        "staging._epc_domestic_history_scratch", scratch
                    )
                )
            )

    if log is not None:
        log.info(
            "0003: loading %d EPC certificate year(s) into %s across %d "
            "parallel job(s), one UNLOGGED scratch table each",
            len(files), source.target, jobs,
        )

    def _one(indexed) -> int:
        """Load one ``(slot, (year, path))``; slot ``s`` owns scratch table ``s``."""
        slot, (year, csv_fp) = indexed
        scratch = f"staging._epc_history_scratch_{slot % jobs}"
        copy_sql = _copy_sql(scratch, csv_fp, None, True)
        subprocess.run(_psql_copy_command(db_url, copy_sql), check=True, env=env)
        with engine.begin() as connection:
            inserted = connection.execute(
                text(
                    f"INSERT INTO {source.target} SELECT\n    "
                    + select_sql
                    + f"\nFROM {scratch} WHERE region = 'E12000007'"
                )
            ).rowcount
            connection.execute(text(f"TRUNCATE TABLE {scratch}"))
        if log is not None:
            log.info(
                "0003: %d contributed %d London EPC certificate row(s)",
                year, inserted,
            )
        return inserted

    total = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for start in range(0, len(files), jobs):
            block = list(enumerate(files[start:start + jobs]))
            total += sum(pool.map(_one, block))

    with engine.begin() as connection:
        for slot in range(jobs):
            connection.execute(
                text(f"DROP TABLE IF EXISTS staging._epc_history_scratch_{slot}")
            )
        connection.execute(text(f"ANALYZE {source.target}"))
        rows = connection.execute(
            text(f"SELECT count(*) FROM {source.target}")
        ).scalar_one()
    _report(log, source, rows)
    return rows


_SPECIAL_EXECUTORS = {"epc_domestic_history": _copy_epc_history}

_EXECUTORS = {
    PSQL_COPY: _copy_via_psql,
    PANDAS: _load_via_pandas,
    STEP: _load_via_step,
}


def _report(log, source: SeedSource, rows: int) -> None:
    """Log a source's loaded row count and its drift from ``live_rows``."""
    if log is None:
        return
    if source.live_rows:
        drift = rows - source.live_rows
        log.info(
            "0003: %s loaded with %d row(s) (reference: %d, delta %+d)",
            source.target, rows, source.live_rows, drift,
        )
    else:
        log.info("0003: %s loaded with %d row(s)", source.target, rows)


def run_sources(
    engine: Engine,
    cfg: Optional[Dict] = None,
    log: Optional[logging.Logger] = None,
    names: Optional[Sequence[str]] = None,
) -> Dict[str, int]:
    """
    Load every selected source in registry order.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Target database.
    cfg : dict, optional
        ``staging.yaml``; resolved from disk when None.
    log : logging.Logger, optional
        Logger for progress messages.
    names : sequence of str, optional
        Sources to load; all by default.

    Returns
    -------
    dict of str to int
        Row count in each source's target after its load.
    """
    selected = _select(names)
    check_sources(cfg, [s.name for s in selected], log)

    results: Dict[str, int] = {}
    for source in selected:
        executor = _SPECIAL_EXECUTORS.get(source.name) or _EXECUTORS[source.mechanism]
        results[source.name] = executor(engine, cfg, log, source)
    return results
