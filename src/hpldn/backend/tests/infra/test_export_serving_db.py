"""
`scripts.export_serving_db`: its DDL rewrites, and a full export and restore on the ephemeral cluster.

The cluster test (marker ``pg``) builds a stand-in ``production`` schema with
every serving table (``london_houses`` as a materialized view, the pruned
tables with an extra column the dump must leave out), the serving functions
and an index, exports it exactly as the real script does, then restores the
dump into ``production_next`` the way ``cloud/vps/restore_serving_db.sh``
does, and applies the rendered ``api_role.sql``/``grants.sql``.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

import psycopg
import pytest
from compression import zstd
from sqlalchemy import text

from hpldn.backend.db import render_vps_sql
from hpldn.backend.scripts import export_serving_db as esd

log = logging.getLogger("test_export_serving_db")


def test_quote_ident():
    assert esd.quote_ident("london_houses") == "london_houses"
    assert esd.quote_ident("UPRN") == '"UPRN"'
    assert esd.quote_ident('a"b') == '"a""b"'


def test_function_and_index_definitions_lose_their_schema():
    ddl = "CREATE OR REPLACE FUNCTION production.hpldn_searchnorm(t text)\n RETURNS text"
    assert esd.unqualify_function(ddl).startswith("CREATE OR REPLACE FUNCTION hpldn_searchnorm(")
    with pytest.raises(ValueError):
        esd.unqualify_function("CREATE FUNCTION public.x()")
    index = "CREATE UNIQUE INDEX ix ON production.london_houses USING btree (ppd_id)"
    assert esd.unqualify_index(index) == "CREATE UNIQUE INDEX ix ON london_houses USING btree (ppd_id)"
    assert esd.index_signature(index) == "CREATE UNIQUE INDEX ON production.london_houses USING btree (ppd_id)"
    with pytest.raises(ValueError):
        esd.unqualify_index("CREATE INDEX ix ON staging.t (a)")


def test_rendered_sql_refuses_a_qualified_leftover():
    plan = esd.TablePlan(name="t", relkind="r", columns=[esd.Column("a", "integer", True)], source_columns=1,
                         indexes=["CREATE INDEX ix ON production.t (a);"])
    with pytest.raises(RuntimeError, match="post.sql"):
        esd.render_post_sql([plan], "-- h")
    schema = esd.render_schema_sql([], [esd.TablePlan(name="t", relkind="m", columns=[esd.Column("a b", "text", False)],
                                                     source_columns=2)], "-- h")
    assert 'CREATE TABLE t (\n    "a b" text\n);' in schema and "(matview)" in schema


def test_the_plan_names_the_copy_and_file():
    plan = esd.TablePlan(name="epc_table", relkind="r", columns=[esd.Column("id", "bigint", True)], source_columns=50)
    assert plan.file == "data/epc_table.copy.zst"
    assert plan.copy_sql() == "COPY (SELECT id FROM production.epc_table) TO STDOUT (FORMAT binary)"


# ---------------------------------------------------------------------------
# A real export and restore on the ephemeral cluster.
# ---------------------------------------------------------------------------


def _stand_in_schema(engine) -> None:
    """Create every serving relation and function, two rows each, owned by the migration role."""
    statements = ["CREATE TABLE IF NOT EXISTS public.alembic_version (version_num varchar(32) PRIMARY KEY)",
                  # The cluster is session-scoped: another test may have stamped
                  # a revision already, and the export reads the one row.
                  "DELETE FROM public.alembic_version",
                  "INSERT INTO public.alembic_version VALUES ('0034')"]
    for name in esd.SERVING_TABLES:
        columns = [c for c in esd.SERVING_COLUMNS.get(name, ("id", "value")) if c != "id"]
        extra = ", dropped_by_the_export text" if name in esd.SERVING_COLUMNS else ""
        body = ", ".join(f"{c} text" for c in columns)
        source = "_london_houses_source" if name == "london_houses" else name
        statements.append(f"CREATE TABLE production.{source} (id bigint PRIMARY KEY, {body}{extra})")
        statements.append(
            f"INSERT INTO production.{source} (id, {', '.join(columns)}) "
            f"SELECT g, {', '.join(repr('v') + '||g' for _ in columns)} FROM generate_series(1, 2) g"
        )
    statements += [
        "CREATE MATERIALIZED VIEW production.london_houses AS SELECT * FROM production._london_houses_source",
        "CREATE UNIQUE INDEX london_houses_id ON production.london_houses (id)",
        "CREATE INDEX cpih_value ON production.cpih (value)",
    ]
    for fn in esd.SERVING_FUNCTIONS:
        statements.append(
            f"CREATE FUNCTION production.{fn}(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT lower(t) $$"
        )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _model(tmp_path: Path) -> Path:
    from hpldn.backend.models import artifact
    from hpldn.backend.tests._bundles import tiny_bundle

    model = tmp_path / "models" / "model_anchored"
    if not artifact.is_bundle(model):
        model.parent.mkdir(exist_ok=True)
        tiny_bundle(model)
    return model


def test_describe_model_records_a_verified_bundle_and_refuses_a_tampered_one(tmp_path):
    from hpldn.backend.models import artifact

    model = _model(tmp_path)
    described = esd.describe_model(model, log)
    assert described["bundle"] == "model_anchored" and described["key_id"]
    names = {f["name"] for f in described["files"]}
    assert {"model_anchored/model.cbm", "model_anchored/meta.json", "model_anchored/manifest.sig"} <= names
    for entry in described["files"]:
        assert hashlib.sha256((model.parent / entry["name"]).read_bytes()).hexdigest() == entry["sha256"]
    (model / "meta.json").write_text("{}")
    with pytest.raises(artifact.BundleError):
        esd.describe_model(model, log)


def test_export_then_restore_round_trips_every_serving_table(pg_cluster, tmp_path):
    _stand_in_schema(pg_cluster.migration_engine)
    out = tmp_path / "dump"
    with pg_cluster.pinned_db_url():
        manifest = esd.export(out, _model(tmp_path), level=3, log=log)

    assert sorted(p.name for p in out.iterdir()) == ["SHA256SUMS", "data", "manifest.json", "models.sha256", "post.sql", "schema.sql"]
    assert not out.with_name("dump.partial").exists()
    assert json.loads((out / "manifest.json").read_text()) == manifest
    assert manifest["source"]["alembic_revision"] == "0034"
    tables = {t["name"]: t for t in manifest["tables"]}
    assert set(tables) == set(esd.SERVING_TABLES)
    assert all(t["rows"] == 2 for t in tables.values())
    assert tables["london_houses"]["kind"] == "matview"
    for name, columns in esd.SERVING_COLUMNS.items():
        assert tables[name]["columns"] == list(columns)
        assert tables[name]["source_columns"] == len(columns) + 1
    for line in (out / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert "CREATE UNIQUE INDEX london_houses_id ON london_houses" in (out / "post.sql").read_text()

    # The serving host's role, owner and grants, rendered from db/sql/fragments
    # and applied first, as restore_serving_db.sh does.
    superuser = pg_cluster.superuser
    render_vps_sql.write(tmp_path)
    pg_cluster.psql("-v", f"api_role={pg_cluster.api_role}", "-v", f"db={pg_cluster.database}",
                    "-f", str(tmp_path / "api_role.sql"))
    try:
        # Restore as restore_serving_db.sh does, into production_next, as the
        # NOLOGIN owner (never the superuser): PGOPTIONS -c role=hpldn_owner.
        as_owner = {**os.environ, "PGOPTIONS": "-c role=hpldn_owner"}
        pg_cluster.psql("-c", "CREATE SCHEMA production_next", env=as_owner)
        pg_cluster.psql("-v", "schema=production_next", "-f", str(out / "schema.sql"), env=as_owner)
        dsn = pg_cluster.url(superuser).render_as_string(hide_password=False).replace("+psycopg2", "")
        with psycopg.connect(dsn, options="-c role=hpldn_owner") as conn:
            for name, table in tables.items():
                with conn.cursor() as cur, cur.copy(f"COPY production_next.{name} FROM STDIN (FORMAT binary)") as copy:
                    copy.write(zstd.decompress((out / table["file"]).read_bytes()))
            conn.commit()
            # What a tampered dump might try: the owner role cannot.
            for attack in ("COPY production_next.cpih TO PROGRAM 'true'", "SELECT pg_read_file('/etc/passwd')",
                           "ALTER ROLE hpldn_owner SUPERUSER"):
                with pytest.raises(psycopg.Error, match="permission denied|must be superuser|privilege"):
                    conn.execute(attack)
                conn.rollback()
        pg_cluster.psql("-v", "schema=production_next", "-f", str(out / "post.sql"), env=as_owner)
        with pg_cluster.superuser_engine.connect() as connection:
            for name in tables:
                assert connection.execute(text(f"SELECT count(*) FROM production_next.{name}")).scalar() == 2
            assert connection.execute(text("SELECT production_next.hpldn_searchnorm('ABC')")).scalar() == "abc"
            assert connection.execute(text("SELECT to_regclass('production_next.london_houses_id')")).scalar()
            owners = connection.execute(text(
                "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE relnamespace = 'production_next'::regnamespace")).scalars().all()
            assert owners == ["hpldn_owner"]

        pg_cluster.psql("-v", f"api_role={pg_cluster.api_role}", "-v", "schema=production_next",
                        "-f", str(tmp_path / "grants.sql"))
        with pg_cluster.api_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM production_next.cpih")).scalar() == 2
            assert connection.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
            with pytest.raises(Exception, match="read-only|permission denied"):
                connection.execute(text("INSERT INTO production_next.cpih (id) VALUES (99)"))
            connection.rollback()
            # Refused twice over: read-only transactions, and no TEMP on the database.
            with pytest.raises(Exception, match="read-only|permission denied"):
                connection.execute(text("CREATE TEMP TABLE x (i int)"))
            connection.rollback()
            privileges = connection.execute(text(
                "SELECT has_database_privilege(current_user, current_database(), 'TEMP'), "
                "has_database_privilege(current_user, current_database(), 'CREATE')")).one()
            assert tuple(privileges) == (False, False)
    finally:
        # The cluster is shared by the session: hand everything back.
        pg_cluster.psql("-c", f'REASSIGN OWNED BY hpldn_owner TO "{pg_cluster.migration_role}"',
                        "-c", "DROP OWNED BY hpldn_owner",
                        "-c", f'ALTER ROLE "{pg_cluster.api_role}" RESET search_path',
                        "-c", "DROP SCHEMA IF EXISTS production_next CASCADE",
                        "-c", "DROP ROLE hpldn_owner")


def test_a_missing_serving_function_stops_the_export(pg_cluster, tmp_path):
    with pg_cluster.migration_engine.begin() as connection:
        connection.execute(text("DROP FUNCTION IF EXISTS production.hpldn_unit_key(text)"))
    try:
        with pg_cluster.pinned_db_url(), pytest.raises(RuntimeError, match="hpldn_unit_key"):
            esd.export(tmp_path / "dump2", _model(tmp_path), level=3, log=log, plan_only=True)
    finally:
        with pg_cluster.migration_engine.begin() as connection:
            connection.execute(text(
                "CREATE OR REPLACE FUNCTION production.hpldn_unit_key(t text) RETURNS text LANGUAGE sql "
                "IMMUTABLE AS $$ SELECT lower(t) $$"))


def _signed_dump(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A minimal dump directory, signed with a throwaway key trusted in its own directory."""
    from hpldn.backend.models import artifact

    dump = tmp_path / "dump"
    (dump / "data").mkdir(parents=True)
    (dump / "schema.sql").write_text("-- schema\n")
    (dump / "data" / "cpih.copy.zst").write_bytes(b"rows")
    (dump / "manifest.json").write_text('{"format": 1}\n')
    sums = [f"{hashlib.sha256((dump / n).read_bytes()).hexdigest()}  {n}" for n in ("schema.sql", "data/cpih.copy.zst")]
    (dump / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    key, trusted = tmp_path / "serving-signing.key", tmp_path / "trusted"
    artifact.keygen(key, trusted)
    esd.sign_dump(dump, key)
    return dump, key, trusted


def test_a_signed_dump_verifies_and_any_change_is_refused(tmp_path):
    dump, _, trusted = _signed_dump(tmp_path)
    kid = esd.verify_dump(dump, trusted)
    assert (dump / esd.SIGNATURE_FILE).read_text().startswith(f"ed25519 {kid} ")
    assert esd.signed_payload(dump).decode().startswith("hpldn-serving-dump-v1\n")

    (dump / "data" / "cpih.copy.zst").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match SHA256SUMS"):
        esd.verify_dump(dump, trusted)
    (dump / "data" / "cpih.copy.zst").write_bytes(b"rows")
    (dump / "manifest.json").write_text('{"format": 2}\n')
    with pytest.raises(ValueError, match="does not match manifest.json"):
        esd.verify_dump(dump, trusted)


def test_a_dump_signed_by_an_untrusted_key_or_unsigned_is_refused(tmp_path):
    dump, _, _ = _signed_dump(tmp_path)
    with pytest.raises(ValueError, match="not in"):
        esd.verify_dump(dump, tmp_path / "nobody")
    (dump / esd.SIGNATURE_FILE).unlink()
    with pytest.raises(ValueError, match="not signed"):
        esd.verify_dump(dump, tmp_path / "trusted")


def test_the_host_verifies_the_signature_with_openssl_alone(tmp_path):
    """cloud/vps/verify_dump_signature.sh: what restore_serving_db.sh runs before anything else."""
    import shutil
    import subprocess

    if not shutil.which("openssl"):
        pytest.fail("missing prerequisite: openssl", pytrace=False)
    script = Path(__file__).resolve().parents[2] / "cloud" / "vps" / "verify_dump_signature.sh"
    dump, _, trusted = _signed_dump(tmp_path)
    ok = subprocess.run(["bash", str(script), str(dump), str(trusted)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    (dump / "SHA256SUMS").write_text((dump / "SHA256SUMS").read_text() + "\n")
    bad = subprocess.run(["bash", str(script), str(dump), str(trusted)], capture_output=True, text=True)
    assert bad.returncode != 0 and "signature" in bad.stderr
