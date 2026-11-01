"""
The one resolver for migration input files, driven by ``inputs.yaml``.

Every file the alembic chain reads is an entry in ``inputs.yaml``: its key,
its path relative to ``HPLDN_DATA_DIR`` (or to the repository for tracked
files), the revisions that read it and, for fixed files, its size and
sha256. `resolve` turns a key into a path, `check` reports every missing or
changed input at once, and `pull`/`push` copy Tier A, the inputs that cannot
be fetched again, to and from the OCI bucket.

Every object is encrypted on the laptop with ``age`` before it leaves
(``<path>.age``), to the operator's recipient in ``~/.config/hpldn/age.pub``
(``HPLDN_AGE_RECIPIENTS``), and decrypted on pull with the identity in
``~/.config/hpldn/age.key`` (``HPLDN_AGE_IDENTITY``). The bucket's own
customer-managed key (OCI Vault, ``cloud/oci/state``) sits on top: a leaked
bucket credential, or OCI itself, sees ciphertext only.

A host can move one input by setting ``data: {<key>: <path>}`` in
``staging.yaml``; that override wins over the manifest path.

CLI::

    python -m hpldn.backend.migrations.inputs check [--revision R ...] [--hash]
    python -m hpldn.backend.migrations.inputs paths [--revision R ...]
    python -m hpldn.backend.migrations.inputs pull|push [--dry-run]
"""

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import yaml

from hpldn.backend.common.config import get_cfg, get_repo_root

MANIFEST_FP = Path(__file__).with_name("inputs.yaml")

# The pseudo-revision for ``bare_metal --load-raw`` (StagingCopy runs between 0001 and 0002).
LOAD_RAW = "load-raw"

KINDS = ("file", "dir", "glob")
TIERS = ("A", "B")

# Tier A lives in this OCI Object Storage bucket, under its manifest path.
BUCKET_ENV = "HPLDN_INPUTS_BUCKET"
NAMESPACE_ENV = "HPLDN_INPUTS_NAMESPACE"
AGE_RECIPIENTS_ENV = "HPLDN_AGE_RECIPIENTS"
AGE_IDENTITY_ENV = "HPLDN_AGE_IDENTITY"
DEFAULT_AGE_RECIPIENTS = Path("~/.config/hpldn/age.pub")
DEFAULT_AGE_IDENTITY = Path("~/.config/hpldn/age.key")
AGE_SUFFIX = ".age"

Target = Union[None, str, Iterable[str]]


class DataSourceMissing(FileNotFoundError):
    """
    One or more migration inputs are missing or differ from the manifest.

    `check` collects every failure before raising, so one run names them all.
    """


@dataclass(frozen=True)
class Input:
    """One ``inputs.yaml`` entry; the fields are documented in the manifest's header."""

    key: str
    kind: str
    required_by: Tuple[str, ...]
    tier: str
    path: Optional[str] = None
    repo: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None
    source_url: Optional[str] = None
    licence: Optional[str] = None
    fetch: Optional[str] = None
    contains: Tuple[str, ...] = ()
    glob: Optional[str] = None


def _parse(raw: dict) -> Input:
    """Build an `Input` from one manifest mapping, rejecting malformed entries."""
    key = raw.get("key")
    if not key:
        raise ValueError(f"inputs.yaml entry without a key: {raw!r}")
    if bool(raw.get("path")) == bool(raw.get("repo")):
        raise ValueError(f"inputs.yaml {key}: give exactly one of path or repo")
    if raw.get("kind") not in KINDS:
        raise ValueError(f"inputs.yaml {key}: kind must be one of {KINDS}")
    if raw.get("tier") not in TIERS:
        raise ValueError(f"inputs.yaml {key}: tier must be one of {TIERS}")
    if not raw.get("required_by"):
        raise ValueError(f"inputs.yaml {key}: required_by is empty")
    if raw.get("sha256") is not None and raw["kind"] != "file":
        raise ValueError(f"inputs.yaml {key}: only a file can carry a sha256")
    if (raw.get("contains") or raw.get("glob")) and raw["kind"] != "dir":
        raise ValueError(f"inputs.yaml {key}: contains/glob apply to a dir only")
    return Input(
        key=key,
        kind=raw["kind"],
        required_by=tuple(str(r) for r in raw["required_by"]),
        tier=raw["tier"],
        path=raw.get("path"),
        repo=raw.get("repo"),
        sha256=raw.get("sha256"),
        size=raw.get("size"),
        source_url=raw.get("source_url"),
        licence=raw.get("licence"),
        fetch=raw.get("fetch"),
        contains=tuple(raw.get("contains") or ()),
        glob=raw.get("glob"),
    )


def load_manifest(fp: Path = MANIFEST_FP) -> Dict[str, Input]:
    """Return the manifest's entries by key, in file order."""
    if fp == MANIFEST_FP:
        return dict(_default_manifest())
    return _read_manifest(fp)


@lru_cache(maxsize=1)
def _default_manifest() -> Tuple[Tuple[str, Input], ...]:
    return tuple(_read_manifest(MANIFEST_FP).items())


def _read_manifest(fp: Path) -> Dict[str, Input]:
    document = yaml.safe_load(Path(fp).read_text(encoding="utf-8")) or {}
    entries: Dict[str, Input] = {}
    for raw in document.get("inputs") or ():
        entry = _parse(raw)
        if entry.key in entries:
            raise ValueError(f"inputs.yaml lists {entry.key} twice")
        entries[entry.key] = entry
    return entries


def data_dir() -> Path:
    """Return ``HPLDN_DATA_DIR``, or ``<repo root>/data`` when it is unset."""
    configured = os.environ.get("HPLDN_DATA_DIR")
    return Path(configured).expanduser() if configured else get_repo_root().joinpath("data")


def _override(cfg: Optional[Dict], key: str) -> Optional[Path]:
    configured = ((cfg or {}).get("data") or {}).get(key)
    return Path(str(configured)).expanduser() if configured else None


def _location(entry: Input, cfg: Optional[Dict]) -> Path:
    """Return the entry's path; for a glob, the pattern itself."""
    override = _override(cfg, entry.key)
    if override is not None:
        if entry.kind == "glob":
            return override.joinpath(Path(entry.path).name)
        return override
    if entry.repo:
        return get_repo_root().joinpath(entry.repo)
    return data_dir().joinpath(entry.path)


def resolve(key: str, cfg: Optional[Dict] = None) -> Path:
    """
    Return the path of one input, resolved but not checked.

    Parameters
    ----------
    key : str
        The manifest key, e.g. ``"epc_domestic_dir"``.
    cfg : dict, optional
        Loaded ``staging.yaml``; its ``data:`` block overrides the manifest.

    Returns
    -------
    pathlib.Path
        The file or directory. A ``glob`` input resolves to the directory its
        pattern matches in.

    Raises
    ------
    KeyError
        If `key` is not in ``inputs.yaml``.
    """
    manifest = load_manifest()
    if key not in manifest:
        raise KeyError(f"{key} is not a migration input in {MANIFEST_FP}")
    entry = manifest[key]
    location = _location(entry, cfg)
    return location.parent if entry.kind == "glob" else location


def fetch_commands() -> Dict[str, str]:
    """Return ``{key: command}`` for every input a command produces."""
    return {key: entry.fetch for key, entry in load_manifest().items() if entry.fetch}


def select(target: Target = None) -> List[Input]:
    """
    Return the inputs a target needs, in manifest order.

    `target` is None or ``"all"`` for every input, a revision id such as
    ``"0028"`` (or `LOAD_RAW`), or an iterable of them.
    """
    entries = list(load_manifest().values())
    if target is None or target == "all":
        return entries
    wanted = {target} if isinstance(target, str) else set(target)
    return [entry for entry in entries if wanted.intersection(entry.required_by)]


def sha256_of(path: Path) -> str:
    """Return the hex sha256 of a file, read in 4 MiB blocks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def problems(entry: Input, cfg: Optional[Dict] = None, hash: bool = False) -> List[str]:
    """
    Return what is wrong with one input on this machine; empty when it is fine.

    Size is always compared for a pinned file; sha256 only when `hash` is set.
    """
    location = _location(entry, cfg)
    if entry.kind == "glob":
        if not any(location.parent.glob(location.name)):
            return [f"no file matches {location}"]
        return []
    if entry.kind == "dir":
        if not location.is_dir():
            return [f"directory not found: {location}"]
        found = []
        missing = [name for name in entry.contains if not location.joinpath(name).is_file()]
        if missing:
            found.append(f"{len(missing)} file(s) missing under {location}: {', '.join(missing)}")
        if entry.glob and not any(location.glob(entry.glob)):
            found.append(f"no file matches {location / entry.glob}")
        return found
    if not location.is_file():
        return [f"file not found: {location}"]
    if entry.size is not None and location.stat().st_size != entry.size:
        return [f"size {location.stat().st_size} != manifest {entry.size}: {location}"]
    if hash and entry.sha256 and sha256_of(location) != entry.sha256:
        return [f"sha256 differs from the manifest: {location}"]
    return []


def _describe(entry: Input, issues: Sequence[str]) -> str:
    lines = [f"[{entry.key}] (required by {', '.join(entry.required_by)})"]
    lines.extend(f"  {issue}" for issue in issues)
    if entry.fetch:
        lines.append(f"  produce it with: {entry.fetch}")
    elif entry.tier == "A":
        lines.append("  Tier A: restore it with python -m hpldn.backend.migrations.inputs pull")
    elif entry.source_url:
        lines.append(f"  download it from: {entry.source_url}")
    return "\n".join(lines)


def check(target: Target = None, hash: bool = False, cfg: Optional[Dict] = None,
          log: Optional[logging.Logger] = None) -> List[Input]:
    """
    Check every input `target` needs, reporting all failures at once.

    Parameters
    ----------
    target : None, str or iterable of str
        As for `select`; every input by default.
    hash : bool, optional
        Also compare each pinned file's sha256 (reads every byte).
    cfg : dict, optional
        Loaded ``staging.yaml``, for its ``data:`` overrides.
    log : logging.Logger, optional
        Logger for the summary line.

    Returns
    -------
    list of Input
        The inputs checked.

    Raises
    ------
    DataSourceMissing
        Listing every input that is missing or differs from the manifest.
    """
    selected = select(target)
    failures = []
    for entry in selected:
        issues = problems(entry, cfg, hash)
        if issues:
            failures.append(_describe(entry, issues))
    if failures:
        raise DataSourceMissing(
            f"{len(failures)} of {len(selected)} migration input(s) are missing or "
            f"differ from {MANIFEST_FP.name} (HPLDN_DATA_DIR={data_dir()}):\n\n"
            + "\n\n".join(failures)
        )
    if log is not None:
        log.info("All %d migration input(s) checked are present", len(selected))
    return selected


def required_paths(target: Target = None, cfg: Optional[Dict] = None) -> List[Tuple[str, Path]]:
    """Return ``(key, path)`` for every input `target` needs; a glob gives its pattern."""
    return [(entry.key, _location(entry, cfg)) for entry in select(target)]


def _oci_cli() -> str:
    """Return the ``oci`` CLI: on PATH, or beside this interpreter (the project venv)."""
    found = shutil.which("oci") or shutil.which("oci", path=str(Path(sys.executable).parent))
    if not found:
        raise RuntimeError("the oci CLI is not installed (pip install oci-cli)")
    return found


def _bucket_args(bucket: Optional[str], namespace: Optional[str]) -> List[str]:
    bucket = bucket or os.environ.get(BUCKET_ENV)
    if not bucket:
        raise RuntimeError(f"set {BUCKET_ENV} to the OCI bucket holding Tier A inputs")
    args = ["--bucket-name", bucket]
    namespace = namespace or os.environ.get(NAMESPACE_ENV)
    if namespace:
        args += ["--namespace", namespace]
    return args


def _tier_a() -> List[Input]:
    return [entry for entry in load_manifest().values() if entry.tier == "A" and entry.path]


def _age_cli() -> str:
    found = shutil.which("age")
    if not found:
        raise RuntimeError("age is not installed (https://github.com/FiloSottile/age; apt install age)")
    return found


def _age_file(env: str, default: Path, what: str) -> Path:
    path = Path(os.environ.get(env) or default).expanduser()
    if not path.is_file():
        raise RuntimeError(f"no age {what} at {path} (set {env}); see cloud/oci/README.md")
    return path


def _age_encrypt(source: Path, target: Path) -> None:
    """Encrypt `source` into `target` to the operator's recipients (``age -R``)."""
    recipients = _age_file(AGE_RECIPIENTS_ENV, DEFAULT_AGE_RECIPIENTS, "recipients file")
    subprocess.run([_age_cli(), "--encrypt", "-R", str(recipients), "-o", str(target), str(source)], check=True)


def _age_decrypt(source: Path, target: Path) -> None:
    """Decrypt `source` into `target` with the operator's identity (``age -d -i``)."""
    identity = _age_file(AGE_IDENTITY_ENV, DEFAULT_AGE_IDENTITY, "identity")
    if identity.stat().st_mode & 0o077:
        raise RuntimeError(f"{identity} must be readable by its owner only (chmod 600)")
    subprocess.run([_age_cli(), "--decrypt", "-i", str(identity), "-o", str(target), str(source)], check=True)


def _run(cmd: List[str], dry_run: bool, log: Optional[logging.Logger]) -> None:
    if log is not None:
        log.info("%s", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True)


def pull(bucket: Optional[str] = None, namespace: Optional[str] = None, dry_run: bool = False,
         log: Optional[logging.Logger] = None) -> List[str]:
    """
    Download every Tier A input that is missing or differs from its sha256.

    Each object (``<path>.age``) is downloaded and decrypted beside its
    target, and renamed into place only once its sha256 matches the manifest.

    Returns
    -------
    list of str
        The keys downloaded (or, with `dry_run`, that would be).
    """
    cli, bucket_args = _oci_cli(), _bucket_args(bucket, namespace)
    pulled = []
    for entry in _tier_a():
        target = _location(entry, None)
        if target.is_file() and (entry.sha256 is None or sha256_of(target) == entry.sha256):
            continue
        partial = target.with_name(target.name + ".part")
        sealed = target.with_name(target.name + ".part" + AGE_SUFFIX)
        _run([cli, "os", "object", "get", *bucket_args, "--name", entry.path + AGE_SUFFIX, "--file", str(sealed)],
             dry_run, log)
        pulled.append(entry.key)
        if dry_run:
            continue
        try:
            _age_decrypt(sealed, partial)
        finally:
            sealed.unlink(missing_ok=True)
        if entry.sha256 and sha256_of(partial) != entry.sha256:
            partial.unlink()
            raise DataSourceMissing(f"[{entry.key}] the bucket's copy does not match the manifest sha256")
        partial.replace(target)
    return pulled


def push(bucket: Optional[str] = None, namespace: Optional[str] = None, dry_run: bool = False,
         log: Optional[logging.Logger] = None) -> List[str]:
    """
    Upload every Tier A input, ``age``-encrypted, refusing any whose content differs from the manifest.

    Returns
    -------
    list of str
        The keys uploaded (or, with `dry_run`, that would be).
    """
    cli, bucket_args = _oci_cli(), _bucket_args(bucket, namespace)
    entries = _tier_a()
    failures = [_describe(entry, issues) for entry in entries if (issues := problems(entry, None, hash=True))]
    if failures:
        raise DataSourceMissing("refusing to push Tier A inputs that differ from the manifest:\n\n"
                                + "\n\n".join(failures))
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hpldn-inputs-") as tmp:
        os.chmod(tmp, 0o700)
        for entry in entries:
            sealed = Path(tmp) / (Path(entry.path).name + AGE_SUFFIX)
            if not dry_run:
                _age_encrypt(_location(entry, None), sealed)
            _run([cli, "os", "object", "put", *bucket_args, "--name", entry.path + AGE_SUFFIX,
                  "--file", str(sealed), "--force"], dry_run, log)
            sealed.unlink(missing_ok=True)
    return [entry.key for entry in entries]


def cfg_or_none() -> Optional[Dict]:
    """Return ``staging.yaml`` for its overrides, or None on a host without one."""
    try:
        return get_cfg()
    except (FileNotFoundError, KeyError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hpldn.backend.migrations.inputs",
                                     description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("check", "check the inputs are present"),
                            ("paths", "print every required path")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--revision", action="append", default=None,
                         help=f"only inputs this revision (or {LOAD_RAW}) reads; repeatable")
        if name == "check":
            sub.add_argument("--hash", action="store_true", help="also compare sha256")
    for name in ("pull", "push"):
        sub = commands.add_parser(name, help=f"{name} Tier A inputs from/to ${BUCKET_ENV}")
        sub.add_argument("--bucket", default=None)
        sub.add_argument("--namespace", default=None)
        sub.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = logging.getLogger("hpldn.migrations.inputs")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "paths":
        for key, path in required_paths(args.revision, cfg_or_none()):
            print(f"{key}\t{path}")
        return 0
    if args.command == "check":
        try:
            checked = check(args.revision, hash=args.hash, cfg=cfg_or_none())
        except DataSourceMissing as err:
            print(err, file=sys.stderr)
            return 1
        print(f"{len(checked)} input(s) OK{' (sha256 verified)' if args.hash else ''}")
        return 0
    action = pull if args.command == "pull" else push
    done = action(args.bucket, args.namespace, args.dry_run, log)
    print(f"{args.command}: {', '.join(done) or 'nothing to do'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
