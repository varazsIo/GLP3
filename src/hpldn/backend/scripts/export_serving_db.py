"""
Export the tables the read API serves from into a portable serving dump.

The read API and `hpp_inference` read about a dozen ``production`` tables;
everything else in the database (all of ``staging``, the feature tables the
matview is built from, ~75 GB) is build-time only. This writes just the
runtime set, from one REPEATABLE READ snapshot, as:

* ``schema.sql``: the two ``production`` SQL functions and one CREATE TABLE
  per table, unqualified, so the restore places them in whichever schema its
  ``search_path`` names (``production_next`` for a blue/green swap);
* ``data/<table>.copy.zst``: ``COPY ... TO STDOUT (FORMAT binary)``, zstd
  compressed;
* ``post.sql``: primary keys, unique and check constraints and indexes, run
  after the load (duplicate indexes and ones over dropped columns are left
  out);
* ``manifest.json`` (row counts, alembic revision, model files, digests),
  ``SHA256SUMS`` for the dump files and ``models.sha256`` for the model
  bundle's files (``<bundle>/<file>``, relative to the bundle's parent);
* with ``--sign``, ``SIGNATURE``: ``ed25519 <key id> <base64>`` over
  `signed_payload` (the SHA-256 of ``manifest.json`` and of ``SHA256SUMS``),
  made with ``~/.config/hpldn/serving-signing.key`` (a key of its own, not
  the model's). ``restore_serving_db.sh`` checks it with ``openssl`` against
  the public keys committed under ``cloud/vps/trusted_keys/`` BEFORE it
  trusts ``SHA256SUMS`` or runs any of the dump's SQL.

``production.london_houses`` is a materialized view; ``pg_dump`` would emit a
REFRESH that needs every source table, so it is exported as a plain table
with the view's indexes. The server only reads it and never refreshes it.
``production.epc_table`` keeps only the columns the pricing SQL reads.

No scratch database is needed (the migration role has no CREATEDB). Restore
with ``cloud/vps/restore_serving_db.sh``.

Usage::

    python -m hpldn.backend.scripts.export_serving_db --plan     # what would be exported
    python -m hpldn.backend.scripts.export_serving_db            # tmp/serving/<date>/
    python -m hpldn.backend.scripts.export_serving_db --out-dir tmp/serving/test --force
    python -m hpldn.backend.scripts.export_serving_db --sign     # ... and sign it
    python -m hpldn.backend.scripts.export_serving_db --sign-only tmp/serving/<date>
    python -m hpldn.backend.scripts.export_serving_db --verify tmp/serving/<date>
    python -m hpldn.backend.scripts.export_serving_db --keygen   # once: the signing key pair
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg
from compression import zstd

from hpldn.backend.common.config import get_repo_root
from hpldn.backend.common.db import get_migration_db_url
from hpldn.backend.scripts.bare_metal import get_bare_metal_logger, get_cfg

SCHEMA = "production"

# Every production relation the runtime reads: api/**, scripts/hpp_inference.py
# and the modules it calls at serve time (hpp_cb, hpp_rf, local_index,
# hpi_projection). tests/test_serving_tables.py fails when the code starts
# reading a table missing from here (or from its NOT_SERVED set).
SERVING_TABLES: Tuple[str, ...] = (
    "london_houses",
    "epc_table",
    "address_table",
    "address_coordinates",
    "ppd_table",
    "neighbourhood_features",
    "address_search",
    "comps_features",
    "london_rents",
    "cpih",
    "ofsted",
    "ofsted_rating_spells",
    "mortgage_rates",
    "ukhpi",
    "boe_rate",
    "build_form_category_table",
)

# Called by name from the API's SQL (hpldn_searchnorm) and kept beside it
# (hpldn_street_key, and hpldn_unit_key, which keyed address_search's
# unit_key since 0031) so the serving schema matches the one the chain builds.
SERVING_FUNCTIONS: Tuple[str, ...] = ("hpldn_searchnorm", "hpldn_street_key", "hpldn_unit_key")

# Tables exported with fewer columns than they have. epc_table's columns are
# those `hpp_inference._EPC_SQL` and `_NEIGHBOUR_EPC_SQL` select, filter or
# order by; the other ~35 are the certificate's cost and efficiency detail,
# which nothing at runtime reads. /properties (not proxied publicly) maps the
# full ORM model and does not work against the pruned table.
SERVING_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "epc_table": (
        "id",
        "address_id",
        "current_energy_rating",
        "build_form",
        "inspection_date",
        "total_floor_area",
        "mains_gas_flag",
        "multi_glaze_proportion",
        "extension_count",
        "number_habitable_rooms",
        "construction_age_band",
        "lodgement_datetime",
        "tenure",
        "floor_level",
        "property_type",
    ),
    # The Ofsted tables without their PostGIS ``geom``: the serving host has no
    # PostGIS, and POST /schools and the pricing code read lat/lon.
    "ofsted_rating_spells": (
        "id", "urn", "ofsted_id", "phase", "school_name", "la_code", "lat", "lon", "valid_from",
        "valid_to", "rating_score", "grade_basis", "rating_label", "inspection_date",
        "publication_date", "inherited_from_urn", "source",
    ),
    "ofsted": (
        "id", "urn", "inspection_number", "inspection_type", "inspection_type_raw", "era",
        "inspection_start_date", "inspection_end_date", "publication_date", "publication_date_imputed",
        "overall_effectiveness", "quality_of_education", "behaviour_and_attitudes", "personal_development",
        "leadership_and_management", "rc_inclusion", "rc_curriculum_and_teaching", "rc_achievement",
        "rc_attendance_and_behaviour", "rc_personal_development", "rc_leadership_and_governance",
        "safeguarding_met", "category_of_concern", "ungraded_outcome", "ungraded_outcome_raw",
        "rating_score", "grade_basis", "rating_label", "school_name", "phase", "establishment_type",
        "la_code", "la_name", "postcode", "open_date", "close_date", "statutory_low_age",
        "statutory_high_age", "location_source", "latitude", "longitude", "origin", "source_file",
        "source_row", "source",
    ),
}

# Dump signatures (`sign_dump`, `verify_dump`; restore_serving_db.sh checks
# them on the host with openssl). The private key never leaves the laptop;
# its public half is committed under cloud/vps/trusted_keys/<key id>.pem.
SIGNATURE_FILE = "SIGNATURE"
SIGNATURE_DOMAIN = "hpldn-serving-dump-v1"
SIGNING_KEY_ENV = "HPLDN_SERVING_SIGNING_KEY"
DEFAULT_SIGNING_KEY = Path("~/.config/hpldn/serving-signing.key")
TRUSTED_KEYS_DIR = Path(__file__).resolve().parents[1] / "cloud" / "vps" / "trusted_keys"

DEFAULT_MODEL_RELATIVE = Path("tmp") / "models" / "model_anchored"
DEFAULT_OUT_ROOT_RELATIVE = Path("tmp") / "serving"
MANIFEST_FORMAT = 1
DEFAULT_ZSTD_LEVEL = 6
HASH_CHUNK = 8 * 1024 * 1024

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_FUNCTION_HEAD = re.compile(rf"^(CREATE OR REPLACE FUNCTION ){SCHEMA}\.")
_INDEX_HEAD = re.compile(rf"^(CREATE (?:UNIQUE )?INDEX \S+ ON (?:ONLY )?){SCHEMA}\.")
_INDEX_NAME = re.compile(r"^CREATE (UNIQUE )?INDEX \S+ ON ")


@dataclass
class Column:
    """One exported column: name, SQL type, NOT NULL, and a non-default collation."""

    name: str
    sql_type: str
    not_null: bool
    collation: Optional[str] = None

    def ddl(self) -> str:
        """Return the column's line in CREATE TABLE."""
        parts = [quote_ident(self.name), self.sql_type]
        if self.collation:
            parts.append(f"COLLATE {self.collation}")
        if self.not_null:
            parts.append("NOT NULL")
        return " ".join(parts)


@dataclass
class TablePlan:
    """What is exported of one relation."""

    name: str
    relkind: str
    columns: List[Column]
    source_columns: int
    constraints: List[str] = field(default_factory=list)
    indexes: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def kind(self) -> str:
        """``"matview"`` or ``"table"``."""
        return "matview" if self.relkind == "m" else "table"

    @property
    def file(self) -> str:
        """The data file, relative to the dump directory."""
        return f"data/{self.name}.copy.zst"

    def create_sql(self) -> str:
        """Return an unqualified CREATE TABLE for the exported columns."""
        body = ",\n    ".join(c.ddl() for c in self.columns)
        return f"CREATE TABLE {quote_ident(self.name)} (\n    {body}\n);"

    def copy_sql(self) -> str:
        """Return the COPY that streams the exported columns in binary."""
        cols = ", ".join(quote_ident(c.name) for c in self.columns)
        return (
            f"COPY (SELECT {cols} FROM {SCHEMA}.{quote_ident(self.name)}) "
            "TO STDOUT (FORMAT binary)"
        )


def quote_ident(name: str) -> str:
    """Return `name` bare when it is a plain lower-case identifier, else double-quoted."""
    if _IDENTIFIER.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def unqualify_function(ddl: str) -> str:
    """Strip the schema from ``pg_get_functiondef``'s ``CREATE OR REPLACE FUNCTION production.x``."""
    out, n = _FUNCTION_HEAD.subn(r"\1", ddl.strip(), count=1)
    if n != 1:
        raise ValueError(f"unexpected function definition head: {ddl[:80]!r}")
    return out


def unqualify_index(indexdef: str) -> str:
    """Strip the schema from ``pg_get_indexdef``'s ``ON production.table``."""
    out, n = _INDEX_HEAD.subn(r"\1", indexdef, count=1)
    if n != 1:
        raise ValueError(f"unexpected index definition: {indexdef!r}")
    return out


def index_signature(indexdef: str) -> str:
    """Return an index definition without its name, to spot duplicate indexes."""
    return _INDEX_NAME.sub(r"CREATE \1INDEX ON ", indexdef, count=1)


def model_files(model_fp: Path) -> List[Path]:
    """Return every file of the model bundle at `model_fp`, sorted."""
    return sorted(path for path in model_fp.iterdir() if path.is_file())


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(repo_root: Path) -> Optional[str]:
    """Return ``HEAD``'s commit, suffixed ``-dirty`` for a modified tree; None outside git."""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{head}-dirty" if dirty else head


def connect(log: logging.Logger) -> psycopg.Connection:
    """
    Open a read-only REPEATABLE READ connection to the migration database.

    Every table is read from the same snapshot, so the dump is consistent
    even if a pipeline step writes to production while it runs.
    """
    url = get_migration_db_url(log)
    conn = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database,
        application_name="hpldn-export-serving-db",
    )
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    with conn.cursor() as cur:
        # pg_get_* print production objects qualified or not depending on the
        # search path; the rewrites below expect it to be exactly this.
        cur.execute(f"SET LOCAL search_path = {SCHEMA}, pg_catalog")
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
    log.info(f"Exporting from {url.database} on {url.host}:{url.port or 5432}")
    return conn


def fetch_functions(cur: psycopg.Cursor, names: Sequence[str]) -> List[str]:
    """Return an unqualified CREATE OR REPLACE FUNCTION for each of `names`."""
    cur.execute(
        """
        SELECT p.proname, pg_get_functiondef(p.oid)
        FROM pg_proc p
        WHERE p.pronamespace = %s::regnamespace AND p.proname = ANY(%s)
        ORDER BY p.proname
        """,
        (SCHEMA, list(names)),
    )
    rows = cur.fetchall()
    found = [r[0] for r in rows]
    missing = sorted(set(names) - set(found))
    if missing:
        raise RuntimeError(f"{SCHEMA} has no function(s) {missing}")
    if len(found) != len(set(found)):
        raise RuntimeError(f"{SCHEMA} has overloaded serving functions: {found}")
    return [unqualify_function(ddl) + ";" for _, ddl in rows]


def plan_table(cur: psycopg.Cursor, name: str, log: logging.Logger) -> TablePlan:
    """Read one relation's columns, constraints and indexes from the catalog."""
    qualified = f"{SCHEMA}.{name}"
    cur.execute(
        "SELECT c.relkind, pg_total_relation_size(c.oid) FROM pg_class c WHERE c.oid = to_regclass(%s)",
        (qualified,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"{qualified} does not exist")
    relkind, size_bytes = row
    if relkind not in ("r", "m"):
        raise RuntimeError(f"{qualified} is relkind {relkind!r}, not a table or matview")

    cur.execute(
        """
        SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation
                    THEN a.attcollation::regcollation::text END
        FROM pg_attribute a
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (qualified,),
    )
    attrs = cur.fetchall()
    keep = SERVING_COLUMNS.get(name)
    if keep is not None:
        missing = sorted(set(keep) - {a[1] for a in attrs})
        if missing:
            raise RuntimeError(f"{qualified} has no column(s) {missing}")
    kept = [a for a in attrs if keep is None or a[1] in keep]
    kept_attnums = {a[0] for a in kept}
    plan = TablePlan(
        name=name,
        relkind=relkind,
        columns=[Column(a[1], a[2], a[3], a[4]) for a in kept],
        source_columns=len(attrs),
        size_bytes=size_bytes,
    )

    # Constraints first: an index a kept constraint already creates is a
    # duplicate. Primary key, then unique, then check; NOT NULL is in the
    # column list and a matview has none of these.
    cur.execute(
        """
        SELECT c.conname, c.contype, pg_get_constraintdef(c.oid), c.conkey, c.conindid::regclass::text
        FROM pg_constraint c
        WHERE c.conrelid = %s::regclass AND c.contype IN ('p', 'u', 'c')
        ORDER BY array_position(ARRAY['p', 'u', 'c']::"char"[], c.contype), c.conname
        """,
        (qualified,),
    )
    seen_constraints = set()
    signatures = set()
    constraint_indexes = {}
    for conname, contype, condef, conkey, conindex in cur.fetchall():
        if not set(conkey or ()) <= kept_attnums:
            plan.skipped.append(f"constraint {conname} (dropped column)")
            continue
        if (contype, condef) in seen_constraints:
            plan.skipped.append(f"constraint {conname} (duplicate)")
            continue
        seen_constraints.add((contype, condef))
        plan.constraints.append(
            f"ALTER TABLE {quote_ident(name)} ADD CONSTRAINT {quote_ident(conname)} {condef};"
        )
        if contype in ("p", "u"):
            constraint_indexes[conindex] = conname

    cur.execute(
        """
        SELECT i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid), i.indkey::int2[],
               i.indexprs IS NOT NULL OR i.indpred IS NOT NULL
        FROM pg_index i
        WHERE i.indrelid = %s::regclass
        ORDER BY i.indisunique DESC, i.indexrelid::regclass::text
        """,
        (qualified,),
    )
    indexes = cur.fetchall()
    # Signatures of the constraint-backed indexes, so a plain index that
    # repeats one is dropped.
    for index_name, indexdef, _, _ in indexes:
        if index_name in constraint_indexes:
            signatures.add(index_signature(unqualify_index(indexdef)))
    for index_name, indexdef, indkey, has_expr in indexes:
        if index_name in constraint_indexes:
            continue
        # An expression or predicate may name a dropped column; only a pruned
        # table has to care, and none of its kept indexes use one.
        if keep is not None and (has_expr or not set(indkey) <= kept_attnums):
            plan.skipped.append(f"index {index_name} (dropped column)")
            continue
        ddl = unqualify_index(indexdef)
        sig = index_signature(ddl)
        if sig in signatures:
            plan.skipped.append(f"index {index_name} (duplicate)")
            continue
        signatures.add(sig)
        plan.indexes.append(ddl + ";")

    log.info(
        f"{qualified}: {plan.kind}, {len(plan.columns)}/{plan.source_columns} columns, "
        f"{len(plan.constraints)} constraints, {len(plan.indexes)} indexes, "
        f"{size_bytes / 1024**3:.2f} GB in the source"
        + (f"; skipping {', '.join(plan.skipped)}" if plan.skipped else "")
    )
    return plan


def check_unqualified(sql: str, what: str) -> None:
    """Refuse DDL (comments aside) that still names the source schema; the restore targets another."""
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    if re.search(rf"\b{SCHEMA}\.", code):
        raise RuntimeError(f"{what} still references {SCHEMA}.*; the rewrite missed a form")


def render_schema_sql(functions: List[str], plans: List[TablePlan], header: str) -> str:
    """Return schema.sql: the functions, then every CREATE TABLE."""
    parts = [
        header,
        "-- Run with: psql -v ON_ERROR_STOP=1 -v schema=<target schema> -f schema.sql",
        "-- Nothing below is schema-qualified; the target is whatever :schema names.",
        'SET search_path TO :"schema", pg_catalog;',
        "",
        *functions,
        "",
    ]
    for plan in plans:
        parts += [f"-- {SCHEMA}.{plan.name} ({plan.kind})", plan.create_sql(), ""]
    sql = "\n".join(parts)
    check_unqualified(sql, "schema.sql")
    return sql


def render_post_sql(plans: List[TablePlan], header: str) -> str:
    """Return post.sql: constraints and indexes, run after the data is loaded."""
    parts = [
        header,
        "-- Run after every data file has been loaded, with the same -v schema.",
        'SET search_path TO :"schema", pg_catalog;',
        "",
    ]
    for plan in plans:
        if plan.constraints or plan.indexes:
            parts += [f"-- {plan.name}", *plan.constraints, *plan.indexes, ""]
    sql = "\n".join(parts)
    check_unqualified(sql, "post.sql")
    return sql


def zstd_options(level: int) -> dict:
    """Return compressor options: `level`, on every core when libzstd is multithreaded."""
    options = {zstd.CompressionParameter.compression_level: level}
    _, max_workers = zstd.CompressionParameter.nb_workers.bounds()
    if max_workers > 0:
        options[zstd.CompressionParameter.nb_workers] = min(os.cpu_count() or 1, max_workers)
    return options


def export_table(
    cur: psycopg.Cursor, plan: TablePlan, out_dir: Path, level: int, log: logging.Logger
) -> Dict:
    """Stream one table to ``data/<table>.copy.zst`` and return its manifest entry."""
    path = out_dir / plan.file
    started = time.monotonic()
    raw_bytes = 0
    with zstd.ZstdFile(path, "wb", options=zstd_options(level)) as fh:
        with cur.copy(plan.copy_sql()) as copy:
            for block in copy:
                fh.write(block)
                raw_bytes += len(block)
    rows = cur.rowcount
    if rows is None or rows < 0:
        raise RuntimeError(f"COPY of {plan.name} did not report a row count")
    os.chmod(path, 0o600)
    size = path.stat().st_size
    log.info(
        f"{plan.name}: {rows:,} rows, {raw_bytes / 1024**2:,.0f} MB raw -> "
        f"{size / 1024**2:,.0f} MB in {time.monotonic() - started:,.0f}s"
    )
    return {
        "name": plan.name,
        "kind": plan.kind,
        "rows": rows,
        "columns": [c.name for c in plan.columns],
        "source_columns": plan.source_columns,
        "file": plan.file,
        "bytes": size,
        "raw_bytes": raw_bytes,
        "sha256": sha256_file(path),
    }


def source_facts(cur: psycopg.Cursor) -> Dict:
    """Return the source database, server version and alembic revision."""
    cur.execute("SELECT current_database(), current_setting('server_version')")
    database, version = cur.fetchone()
    cur.execute("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    revision = None
    if cur.fetchone()[0]:
        cur.execute("SELECT version_num FROM public.alembic_version")
        row = cur.fetchone()
        revision = row[0] if row else None
    return {"database": database, "server_version": version, "alembic_revision": revision}


def describe_model(model_fp: Path, log: logging.Logger) -> Dict:
    """
    Return the model bundle's files with sizes and digests (the bundle itself is rsynced by deploy.sh).

    The bundle is verified first (signature, file hashes, schemas), so a dump
    never records a model the serving host would refuse.
    """
    from hpldn.backend.models import artifact

    if not artifact.is_bundle(model_fp):
        raise FileNotFoundError(f"model bundle {model_fp} does not exist (pass --model)")
    verified = artifact.verify(model_fp, deep=False, logger=log)
    entries = []
    for path in model_files(model_fp):
        log.info(f"Hashing {path.name} ({path.stat().st_size / 1024**2:,.0f} MB)")
        entries.append(
            {"name": f"{model_fp.name}/{path.name}", "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    return {"bundle": model_fp.name, "key_id": verified.key_id, "files": entries}


def write_text(path: Path, text: str) -> None:
    """Write `text` to `path`, readable by the owner only."""
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def export(
    out_dir: Path,
    model_fp: Path,
    level: int,
    log: logging.Logger,
    plan_only: bool = False,
) -> Optional[Dict]:
    """
    Export the serving tables to `out_dir` and return the manifest.

    The dump is written to ``<out_dir>.partial`` and renamed into place only
    once every file and digest is written, so an interrupted export is never
    mistaken for a complete one.
    """
    repo_root = get_repo_root()
    with connect(log) as conn, conn.cursor() as cur:
        facts = source_facts(cur)
        log.info(f"Source: {facts}")
        functions = fetch_functions(cur, SERVING_FUNCTIONS)
        plans = [plan_table(cur, name, log) for name in SERVING_TABLES]
        total = sum(p.size_bytes for p in plans)
        log.info(f"{len(plans)} relations, {total / 1024**3:.1f} GB with indexes in the source")
        if plan_only:
            return None

        model = describe_model(model_fp, log)
        partial = out_dir.with_name(out_dir.name + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        (partial / "data").mkdir(parents=True)
        os.chmod(partial, 0o700)
        os.chmod(partial / "data", 0o700)

        created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        header = (
            f"-- HPLDN serving schema, exported {created} from {facts['database']} "
            f"(alembic {facts['alembic_revision']}) by export_serving_db.py"
        )
        write_text(partial / "schema.sql", render_schema_sql(functions, plans, header))
        write_text(partial / "post.sql", render_post_sql(plans, header))
        tables = [export_table(cur, plan, partial, level, log) for plan in plans]

    files = {name: sha256_file(partial / name) for name in ("schema.sql", "post.sql")}
    manifest = {
        "format": MANIFEST_FORMAT,
        "created_at": created,
        "schema": SCHEMA,
        "source": facts,
        "git_commit": git_revision(repo_root),
        "functions": list(SERVING_FUNCTIONS),
        "tables": tables,
        "files": files,
        "model": model,
    }
    write_text(partial / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    sums = [f"{digest}  {name}" for name, digest in files.items()]
    sums += [f"{t['sha256']}  {t['file']}" for t in tables]
    write_text(partial / "SHA256SUMS", "\n".join(sums) + "\n")
    write_text(
        partial / "models.sha256",
        "\n".join(f"{f['sha256']}  {f['name']}" for f in model["files"]) + "\n",
    )

    if out_dir.exists():
        shutil.rmtree(out_dir)
    partial.rename(out_dir)
    dump_bytes = sum(t["bytes"] for t in tables)
    log.info(
        f"Serving dump written to {out_dir}: {dump_bytes / 1024**3:.2f} GB, "
        f"{sum(t['rows'] for t in tables):,} rows in {len(tables)} tables"
    )
    return manifest


def default_signing_key() -> Path:
    """``HPLDN_SERVING_SIGNING_KEY`` or ``~/.config/hpldn/serving-signing.key``."""
    return Path(os.environ.get(SIGNING_KEY_ENV) or DEFAULT_SIGNING_KEY).expanduser()


def signed_payload(dump_dir: Path) -> bytes:
    """
    The bytes a dump's ``SIGNATURE`` signs.

    ``hpldn-serving-dump-v1`` and then ``sha256sum manifest.json SHA256SUMS``'s
    own output, so the host rebuilds it with coreutils alone::

        { echo hpldn-serving-dump-v1; sha256sum manifest.json SHA256SUMS; }
    """
    lines = [SIGNATURE_DOMAIN]
    for name in ("manifest.json", "SHA256SUMS"):
        lines.append(f"{sha256_file(dump_dir / name)}  {name}")
    return ("\n".join(lines) + "\n").encode("ascii")


def sign_dump(dump_dir: Path, key_path: Optional[Path] = None) -> str:
    """Write `dump_dir`'s ``SIGNATURE`` with the serving signing key; return the key id."""
    import base64

    from hpldn.backend.models import artifact

    private = artifact.load_signing_key(Path(key_path or default_signing_key()))
    kid = artifact.key_id(private.public_key())
    signature = base64.b64encode(private.sign(signed_payload(dump_dir))).decode("ascii")
    write_text(dump_dir / SIGNATURE_FILE, f"{artifact.SIGNATURE_SCHEME} {kid} {signature}\n")
    return kid


def verify_dump(dump_dir: Path, trusted_dir: Optional[Path] = None) -> str:
    """
    Check `dump_dir`'s ``SIGNATURE`` against the trusted keys, then its ``SHA256SUMS``; return the key id.

    Raises
    ------
    ValueError
        On a missing or malformed signature, an untrusted key, a signature
        that does not match, or a file whose digest is not the one listed.
    """
    import base64

    from cryptography.exceptions import InvalidSignature

    from hpldn.backend.models import artifact

    dump_dir = Path(dump_dir)
    trusted_dir = Path(trusted_dir or TRUSTED_KEYS_DIR)
    sig_path = dump_dir / SIGNATURE_FILE
    if not sig_path.is_file() or sig_path.is_symlink():
        raise ValueError(f"{dump_dir} is not signed (no {SIGNATURE_FILE}); export it with --sign")
    parts = sig_path.read_text(encoding="ascii", errors="replace").split()
    if len(parts) != 3 or parts[0] != artifact.SIGNATURE_SCHEME:
        raise ValueError(f"{sig_path} is not '{artifact.SIGNATURE_SCHEME} <key id> <signature>'")
    _, kid, encoded = parts
    keys = artifact.trusted_keys(trusted_dir)
    if kid not in keys:
        raise ValueError(f"{dump_dir} is signed with key {kid}, which is not in {trusted_dir}")
    try:
        keys[kid].verify(base64.b64decode(encoded, validate=True), signed_payload(dump_dir))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError(f"{sig_path} does not match manifest.json and SHA256SUMS for key {kid}") from exc
    for line in (dump_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        target = (dump_dir / name).resolve()
        if dump_dir.resolve() not in target.parents:
            raise ValueError(f"SHA256SUMS names a file outside the dump: {name}")
        if sha256_file(target) != digest:
            raise ValueError(f"{name} does not match SHA256SUMS")
    return kid


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Export the production tables the read API serves from into tmp/serving/<date>/."
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Dump directory (default: <repo>/tmp/serving/<today>)",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help=f"Served model bundle, recorded in the manifest (default: <repo>/{DEFAULT_MODEL_RELATIVE})",
    )
    parser.add_argument(
        "--level",
        type=int,
        default=DEFAULT_ZSTD_LEVEL,
        help=f"zstd compression level (default {DEFAULT_ZSTD_LEVEL})",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print what would be exported (columns, constraints, indexes, sizes) and write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing dump directory",
    )
    parser.add_argument(
        "--sign",
        action="store_true",
        help=f"Sign the dump (SIGNATURE) with the serving signing key ({DEFAULT_SIGNING_KEY})",
    )
    parser.add_argument("--signing-key", type=Path, help="The serving signing key (default: $%s or %s)" % (SIGNING_KEY_ENV, DEFAULT_SIGNING_KEY))
    parser.add_argument("--sign-only", type=Path, metavar="DIR", help="Sign an existing dump and export nothing")
    parser.add_argument("--verify", type=Path, metavar="DIR", help="Verify a dump's signature and checksums, and export nothing")
    parser.add_argument(
        "--keygen",
        action="store_true",
        help="Create the serving signing key and put its public key under cloud/vps/trusted_keys/ (commit it)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> Optional[Dict]:
    """Parse arguments and run the export."""
    args = build_arg_parser().parse_args(argv)
    if args.keygen:
        from hpldn.backend.models import artifact

        kid = artifact.keygen(args.signing_key or default_signing_key(), TRUSTED_KEYS_DIR)
        print(f"serving signing key {kid}: commit {TRUSTED_KEYS_DIR / (kid + '.pem')}")
        return None
    if args.verify:
        kid = verify_dump(args.verify)
        print(f"{args.verify}: signed by {kid}, checksums match")
        return None
    if args.sign_only:
        kid = sign_dump(args.sign_only, args.signing_key)
        print(f"{args.sign_only}: signed by {kid}")
        return None
    if args.sign:
        # Fail before the hour-long export, not after it.
        from hpldn.backend.models import artifact

        artifact.load_signing_key(args.signing_key or default_signing_key())
    log = get_bare_metal_logger(get_cfg())
    repo_root = get_repo_root()
    out_dir = args.out_dir or repo_root / DEFAULT_OUT_ROOT_RELATIVE / dt.date.today().isoformat()
    out_dir = out_dir if out_dir.is_absolute() else repo_root / out_dir
    model_fp = args.model or repo_root / DEFAULT_MODEL_RELATIVE
    model_fp = model_fp if model_fp.is_absolute() else repo_root / model_fp
    if not args.plan and out_dir.exists() and not args.force:
        raise SystemExit(f"{out_dir} already exists; pass --force to replace it")
    manifest = export(out_dir, model_fp, args.level, log, plan_only=args.plan)
    if manifest is not None and args.sign:
        kid = sign_dump(out_dir, args.signing_key)
        log.info(f"Serving dump signed by {kid} ({out_dir / SIGNATURE_FILE})")
    return manifest


if __name__ == "__main__":
    main()
