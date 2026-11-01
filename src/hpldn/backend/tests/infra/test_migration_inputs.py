"""
Tests for the migration inputs manifest (`migrations.inputs`, ``inputs.yaml``).

Offline apart from the Tier A checks, which read the four ex-fixture files
from ``HPLDN_DATA_DIR`` (a missing file fails the run; restore it with
``python -m hpldn.backend.migrations.inputs pull``). No database is touched.
"""

import ast
import hashlib
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from hpldn.backend.migrations import inputs
from hpldn.backend.scripts import migration_helpers

BACKEND = Path(inputs.__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parents[2]
VERSIONS = BACKEND / "migrations" / "versions"

# Calls whose argument at this position is a manifest key.
KEY_CALLS = {"resolve": 0, "require_data_path": 1, "resolve_data_path": 1}

# Manifest inputs read by a literal path rather than through the resolver,
# with the file that reads each and the literal it must contain.
READ_BY_PATH = {
    "osm_addresses_ini": (VERSIONS / "0028_link_unheld_sales.py", '"osm_addresses.ini"'),
}

# Files that pass a key through a variable: the pass-through helpers, the
# registry (its keys are the SeedSource cfg_key literals) and 0033 (the keys
# of ofsted.DEFAULT_DIRECTORIES). All three sources of keys are scanned.
DYNAMIC_SITES = {"migration_helpers.py", "seed_registry.py", "0033_ofsted.py"}

TIER_A_FILES = {
    "ppd_uprn_linkage_tiera_csv_fp": "ppd_uprn_linkage_tierA.csv",
    "ons_pipr_borough_rents_csv_fp": "ons_pipr_borough_rents.csv",
    "ons_pipr_london_rents_csv_fp": "ons_pipr_london_rents.csv",
    "ons_cpih_annual_rate_csv_fp": "ons_cpih_annual_rate.csv",
}


def _source_files():
    for path in sorted(BACKEND.rglob("*.py")):
        if "tests" in path.relative_to(BACKEND).parts or path.name == "inputs.py":
            continue
        yield path


def _scan():
    """Return ``(keys, dynamic_files, data_lookups)`` found in the backend source."""
    keys, dynamic, data_lookups = set(), set(), []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("INPUT_KEY"):
                        constants[target.id] = node.value.value
                        keys.add(node.value.value)
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                    and any(isinstance(t, ast.Name) and t.id == "DEFAULT_DIRECTORIES" for t in node.targets)):
                keys.update(k.value for k in node.value.keys if isinstance(k, ast.Constant))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "SeedSource":
                keys.update(kw.value.value for kw in node.keywords
                            if kw.arg == "cfg_key" and isinstance(kw.value, ast.Constant))
            if name == "get" and node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "data":
                data_lookups.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
            if name not in KEY_CALLS or len(node.args) <= KEY_CALLS[name]:
                continue
            arg = node.args[KEY_CALLS[name]]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in constants:
                keys.add(constants[arg.id])
            elif name != "resolve" or isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "inputs":
                dynamic.add(path.name)
    return keys, dynamic, data_lookups


def test_every_key_the_code_resolves_is_in_the_manifest_and_vice_versa():
    """Drift: the keys the backend passes to the resolver match ``inputs.yaml`` exactly."""
    keys, dynamic, _ = _scan()
    manifest = set(inputs.load_manifest())
    assert keys - manifest == set(), "used in code but missing from inputs.yaml"
    assert manifest - keys - set(READ_BY_PATH) == set(), "in inputs.yaml but never resolved"
    assert dynamic <= DYNAMIC_SITES, f"keys passed as variables in {dynamic - DYNAMIC_SITES}"
    for key, (reader, literal) in READ_BY_PATH.items():
        assert literal in reader.read_text(encoding="utf-8"), f"{reader.name} no longer reads {key}"


def test_no_code_reads_paths_from_the_staging_yaml_data_block_directly():
    """Only the resolver reads ``cfg["data"]``; everything else goes through it."""
    _, _, data_lookups = _scan()
    assert data_lookups == []


def test_the_fixtures_package_is_gone():
    """The ex-fixtures live under ``HPLDN_DATA_DIR``; neither the tree nor the wheel ships them."""
    assert not (BACKEND / "migrations" / "fixtures").exists()
    tracked = subprocess.run(
        ["git", "ls-files", "src/hpldn/backend/migrations/fixtures"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert tracked == ""
    package_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    shipped = package_data["hpldn.backend.migrations"]
    assert not any("fixtures" in pattern for pattern in shipped)
    assert {"inputs.yaml", "versions/*.py"} <= set(shipped)


@pytest.mark.tier_a
def test_tier_a_files_match_their_manifest_sha256():
    """The four moved files are in ``migration_inputs/`` and match the pinned size and sha256."""
    manifest = inputs.load_manifest()
    assert {key for key, entry in manifest.items() if entry.tier == "A"} == set(TIER_A_FILES)
    for key, name in TIER_A_FILES.items():
        entry = manifest[key]
        assert entry.path == f"migration_inputs/{name}" and entry.sha256 and entry.size
        assert inputs.problems(entry, hash=True) == [], key


def test_manifest_entries_are_well_formed():
    """Every entry names real revisions, and every fixed file is pinned."""
    revisions = {path.name[:4] for path in VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")}
    for key, entry in inputs.load_manifest().items():
        assert set(entry.required_by) <= revisions | {inputs.LOAD_RAW}, key
        if entry.sha256:
            assert len(entry.sha256) == 64 and entry.size, key
        if entry.tier == "A":
            assert entry.sha256 and entry.path, key
        assert entry.source_url or entry.licence, key


def test_check_reports_every_missing_input_at_once(tmp_path, monkeypatch):
    """With an empty data dir, one `check` names every input the revision reads."""
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    expected = [entry.key for entry in inputs.select("0028") if entry.path]
    assert len(expected) > 1
    with pytest.raises(inputs.DataSourceMissing) as caught:
        inputs.check("0028")
    message = str(caught.value)
    assert f"{len(expected)} of {len(inputs.select('0028'))}" in message
    for key in expected:
        assert f"[{key}]" in message
    assert "osm_addresses_ini" not in message  # tracked in the repository


def test_resolve_honours_data_dir_override_and_glob(tmp_path, monkeypatch):
    """``HPLDN_DATA_DIR`` moves every path; a ``data:`` override moves one; a glob resolves to its directory."""
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    assert inputs.resolve("ppd_complete_csv_fp") == tmp_path / "pp-complete.csv"
    assert inputs.resolve("osm_addresses_ini") == inputs.get_repo_root() / "configuration" / "osm_addresses.ini"
    assert inputs.resolve("hmlr_uprn_lookup_dir") == tmp_path
    elsewhere = tmp_path / "x" / "pp.csv"
    assert inputs.resolve("ppd_complete_csv_fp", {"data": {"ppd_complete_csv_fp": str(elsewhere)}}) == elsewhere
    monkeypatch.delenv("HPLDN_DATA_DIR")
    assert inputs.data_dir() == inputs.get_repo_root() / "data"
    with pytest.raises(KeyError, match="not a migration input"):
        inputs.resolve("no_such_key")


def test_problems_checks_dirs_globs_and_sizes(tmp_path, monkeypatch):
    """A dir needs its listed files and glob matches; a pinned file needs its size."""
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    manifest = inputs.load_manifest()
    iod = manifest["iod_csv_dir"]
    (tmp_path / "indices_of_deprivation").mkdir()
    (tmp_path / "indices_of_deprivation" / "2010.csv").write_text("x")
    assert "3 file(s) missing" in inputs.problems(iod)[0]
    assert inputs.problems(manifest["hmlr_uprn_lookup_dir"]) == [f"no file matches {tmp_path / 'pp-uprn-lookup-*.csv'}"]
    (tmp_path / "pp-uprn-lookup-jul-2026.csv").write_text("x")
    assert inputs.problems(manifest["hmlr_uprn_lookup_dir"]) == []
    (tmp_path / "BoE_MR_2007-2026.csv").write_text("short")
    assert inputs.problems(manifest["mortgage_rates_csv_fp"])[0].startswith("size 5 != manifest")


def test_manifest_rejects_malformed_entries(tmp_path):
    """Duplicate keys, two locations and unknown kinds fail when the manifest loads."""
    fp = tmp_path / "inputs.yaml"
    base = "  - {key: a, path: a.csv, kind: file, tier: B, required_by: ['0003']}\n"
    for body, match in ((base + base, "twice"),
                        ("  - {key: a, path: a, repo: a, kind: file, tier: B, required_by: ['0003']}\n", "exactly one"),
                        ("  - {key: a, path: a, kind: link, tier: B, required_by: ['0003']}\n", "kind"),
                        ("  - {key: a, path: a, kind: dir, tier: B, required_by: ['0003'], sha256: x}\n", "only a file")):
        fp.write_text("inputs:\n" + body)
        with pytest.raises(ValueError, match=match):
            inputs.load_manifest(fp)


def test_fetch_commands_come_from_the_manifest():
    """`migration_helpers.FETCH_COMMANDS` is the manifest's ``fetch`` column, UKHPI included."""
    assert migration_helpers.FETCH_COMMANDS == inputs.fetch_commands()
    assert migration_helpers.FETCH_COMMANDS["ukhpi_csv_fp"].startswith("python -m hpldn.backend.scripts.fetch_ukhpi")


def test_require_data_path_names_the_manifest_key(tmp_path, monkeypatch):
    """A missing input raises `DataSourceMissing` naming the key and its fetch command."""
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    assert migration_helpers.DataSourceMissing is inputs.DataSourceMissing
    with pytest.raises(inputs.DataSourceMissing, match="fetch_ofsted"):
        migration_helpers.require_data_path(None, "ofsted_dir", "data/ofsted", must_be_dir=True)


def test_cli_paths_and_check(tmp_path, monkeypatch, capsys):
    """``paths`` prints each required path; ``check`` exits 1 listing what is missing."""
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(inputs, "cfg_or_none", lambda: None)
    assert inputs.main(["paths", "--revision", "0025"]) == 0
    assert capsys.readouterr().out == f"ppd_complete_csv_fp\t{tmp_path / 'pp-complete.csv'}\n"
    assert inputs.main(["check", "--revision", "0025"]) == 1
    assert "[ppd_complete_csv_fp]" in capsys.readouterr().err


def _fake_oci(tmp_path, monkeypatch):
    """Record oci CLI calls; ``get`` writes the bytes in `served` to ``--file``."""
    calls, served = [], {}

    def run(cmd, check):
        calls.append(cmd)
        if cmd[3] == "get":
            name = cmd[cmd.index("--name") + 1]
            Path(cmd[cmd.index("--file") + 1]).write_bytes(served[name])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(inputs, "_oci_cli", lambda: "oci")
    monkeypatch.setattr(inputs.subprocess, "run", run)
    # age stands in as a reversible transform, so a plaintext object would fail.
    monkeypatch.setattr(inputs, "_age_encrypt", lambda src, dst: Path(dst).write_bytes(b"AGE:" + Path(src).read_bytes()))

    def decrypt(src, dst):
        data = Path(src).read_bytes()
        if not data.startswith(b"AGE:"):
            raise subprocess.CalledProcessError(1, ["age", "--decrypt"])
        Path(dst).write_bytes(data[4:])

    monkeypatch.setattr(inputs, "_age_decrypt", decrypt)
    monkeypatch.setenv(inputs.BUCKET_ENV, "hpldn-inputs")
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    return calls, served


def test_pull_downloads_missing_tier_a_and_verifies_it(tmp_path, monkeypatch):
    """`pull` fetches each missing Tier A file and renames it into place only when its sha256 matches."""
    calls, served = _fake_oci(tmp_path, monkeypatch)
    (tmp_path / "migration_inputs").mkdir()
    entry = inputs.load_manifest()["ons_cpih_annual_rate_csv_fp"]
    good = b"good"
    monkeypatch.setattr(inputs, "_tier_a", lambda: [inputs.Input(**{**entry.__dict__, "sha256": hashlib.sha256(good).hexdigest(), "size": 4})])
    served[entry.path + ".age"] = b"AGE:" + good
    assert inputs.pull() == [entry.key]
    assert calls[0][:4] == ["oci", "os", "object", "get"] and "hpldn-inputs" in calls[0]
    assert calls[0][calls[0].index("--name") + 1] == entry.path + ".age"
    assert (tmp_path / entry.path).read_bytes() == good
    assert not list((tmp_path / entry.path).parent.glob("*.part*"))
    assert inputs.pull() == []  # already present and verified
    (tmp_path / entry.path).unlink()
    served[entry.path + ".age"] = b"AGE:tampered"
    with pytest.raises(inputs.DataSourceMissing, match="does not match"):
        inputs.pull()
    assert not (tmp_path / entry.path).exists()


def test_push_refuses_files_that_differ_from_the_manifest(tmp_path, monkeypatch):
    """`push` uploads nothing unless every Tier A file matches its pin."""
    calls, _ = _fake_oci(tmp_path, monkeypatch)
    with pytest.raises(inputs.DataSourceMissing, match="refusing to push"):
        inputs.push()
    assert calls == []
    monkeypatch.setattr(inputs, "problems", lambda entry, cfg, hash: [])
    assert sorted(inputs.push(dry_run=True)) == sorted(TIER_A_FILES)
    assert calls == []


def test_push_uploads_only_age_ciphertext(tmp_path, monkeypatch):
    """Every object leaves the laptop encrypted, under ``<path>.age``."""
    calls, _ = _fake_oci(tmp_path, monkeypatch)
    entry = inputs.load_manifest()["ons_cpih_annual_rate_csv_fp"]
    target = tmp_path / entry.path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"plain")
    monkeypatch.setattr(inputs, "_tier_a", lambda: [entry])
    monkeypatch.setattr(inputs, "problems", lambda entry, cfg, hash: [])
    uploaded = {}

    def run(cmd, check):
        calls.append(cmd)
        uploaded[cmd[cmd.index("--name") + 1]] = Path(cmd[cmd.index("--file") + 1]).read_bytes()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(inputs.subprocess, "run", run)
    assert inputs.push() == [entry.key]
    assert uploaded == {entry.path + ".age": b"AGE:plain"}


def test_bucket_is_required(monkeypatch):
    """Without a bucket, pull and push stop before calling oci."""
    monkeypatch.setattr(inputs, "_oci_cli", lambda: "oci")
    monkeypatch.delenv(inputs.BUCKET_ENV, raising=False)
    with pytest.raises(RuntimeError, match=inputs.BUCKET_ENV):
        inputs.pull()


def test_data_dir_follows_the_environment():
    """The suite runs with develop.env's HPLDN_DATA_DIR, or the repository's data/ link."""
    expected = os.environ.get("HPLDN_DATA_DIR")
    assert inputs.data_dir() == (Path(expected) if expected else REPO_ROOT / "data")
