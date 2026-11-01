"""
Provision, deploy and check the HPLDN serving host on OCI, end to end.

    python -m hpldn.backend.cloud.oci check                 # pre-flight; changes nothing
    python -m hpldn.backend.cloud.oci state                 # once, as the tenancy admin: cloud/oci/state
    python -m hpldn.backend.cloud.oci up [--dump DIR]       # tofu apply, pin host keys, bootstrap, secrets, deploy, smoke test
    python -m hpldn.backend.cloud.oci bootstrap [--dump DIR] # up without tofu apply: resume on the existing instance
    python -m hpldn.backend.cloud.oci deploy [--export] [--dump DIR] [--skip-model] ...
    python -m hpldn.backend.cloud.oci close-ssh             # break-glass over: shut 22 in the NSG and on the host
    python -m hpldn.backend.cloud.oci status                # drift, instance, tailnet, unit exposure, leak scan
    python -m hpldn.backend.cloud.oci destroy               # the host and its network (cloud/oci/state stays)

Every command is idempotent. Settings that are not secret come from
``~/.config/hpldn/oci.toml`` (``HPLDN_OCI_CONFIG``; see ``oci.example.toml``).
Secrets come only from the environment, injected by the operator's password
manager (``pass``, ``op run``, ...), and never from a file:

    HPLDN_TOFU_PASSPHRASE           the state encryption passphrase (>= 32 characters)
    CLOUDFLARE_API_TOKEN            the tofu token (README.md lists its permissions)
    TAILSCALE_OAUTH_CLIENT_ID/_SECRET   an OAuth client with the auth_keys scope
    RECAPTCHA_SITE_KEY/_SECRET_KEY  the reCAPTCHA v3 key pair

OCI credentials are the ``~/.oci/config`` profiles (the deployer's for
``main/``, the administrator's for ``state/``).

Secret handling: secrets never go on a command line; every child process gets
an allowlisted environment holding only what it needs; every line of tofu and
ssh output passes through `hpldn.backend.common.redact` (with each secret
registered); ``TF_LOG`` (which logs provider requests, credentials included)
is refused; ``tofu output -json`` is parsed in memory; plan files live in a
0700 temporary directory and are deleted after the apply; secrets reach the
host only on SSH stdin, to ``bootstrap.sh``'s ``--set-*`` modes.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hpldn.backend.common.redact import redact
from hpldn.backend.common.secrets import register_secret

OCI_DIR = Path(__file__).resolve().parent
STATE_DIR = OCI_DIR / "state"
MAIN_DIR = OCI_DIR / "main"
BACKEND_DIR = OCI_DIR.parents[1]
REPO_ROOT = BACKEND_DIR.parents[2]
VPS_REL = Path("src/hpldn/backend/cloud/vps")
DEPLOY_SH = BACKEND_DIR / "cloud" / "vps" / "deploy.sh"
INSTALL_TOFU = REPO_ROOT / "scripts" / "install_tofu.sh"
MODEL_KEYS = BACKEND_DIR / "models" / "trusted_keys"
DUMP_KEYS = BACKEND_DIR / "cloud" / "vps" / "trusted_keys"

CONFIG_ENV = "HPLDN_OCI_CONFIG"
DEFAULT_CONFIG = Path("~/.config/hpldn/oci.toml")
HPLDN_CONFIG_DIR = Path("~/.config/hpldn")
KNOWN_HOSTS = HPLDN_CONFIG_DIR / "known_hosts"
LOCK_FILE = HPLDN_CONFIG_DIR / "tofu.lock"
SERVING_SIGNING_KEY = HPLDN_CONFIG_DIR / "serving-signing.key"
MODEL_SIGNING_KEY = HPLDN_CONFIG_DIR / "model-signing.key"

PASSPHRASE_ENV = "HPLDN_TOFU_PASSPHRASE"
MIN_PASSPHRASE = 32
SECRET_ENVS = (
    PASSPHRASE_ENV,
    "CLOUDFLARE_API_TOKEN",
    "TAILSCALE_OAUTH_CLIENT_ID",
    "TAILSCALE_OAUTH_CLIENT_SECRET",
    "RECAPTCHA_SECRET_KEY",
)
# What every child process may inherit from ours; anything else is dropped.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR")
# Variables that make tofu log or rewrite what it runs.
REFUSED_ENV = ("TF_LOG", "TF_LOG_CORE", "TF_LOG_PROVIDER", "TF_LOG_PATH", "TF_CLI_ARGS")

HOST_KEYS_BEGIN = "HPLDN-HOST-KEYS-BEGIN"
HOST_KEYS_END = "HPLDN-HOST-KEYS-END"
_HOST_KEY_LINE = re.compile(r"HPLDN-HOST-KEY (ssh-ed25519|ecdsa-sha2-nistp256) ([A-Za-z0-9+/]+={0,2})(?=\s|$)")
OUT_OF_CAPACITY = re.compile(r"out of (host )?capacity", re.IGNORECASE)
TAILNET_HOST = "hpldn"
# `up --renew-certs`: a new origin key and Origin CA certificate (and so a new --set-tls).
RENEW_CERTS = ("tls_private_key.origin", "tls_cert_request.origin", "cloudflare_origin_ca_certificate.origin")
# `up --rotate-guard-token`: a new year-long guard token.
ROTATE_GUARD_TOKEN = ("terraform_data.guard_token_issued", "cloudflare_account_token.guard")
SSH_USER = "ubuntu"
HOST_VPS = "/opt/hpldn/vps/bootstrap.sh"


class ProvisionError(RuntimeError):
    """A step failed; the message says which and what to do."""


# ---------------------------------------------------------------------------
# Output and processes
# ---------------------------------------------------------------------------
def say(message: str) -> None:
    print(f"[oci] {redact(message)}", file=sys.stderr, flush=True)


def secret(value: str | None) -> str:
    """Remember `value` as a secret (masked in every line printed from now on); return it."""
    if value:
        register_secret(value)
    return value or ""


def refuse_logging_env(environ: Mapping[str, str] | None = None) -> None:
    """
    Refuse to run with tofu's request logging (or its argument injection) switched on.

    Raises
    ------
    ProvisionError
        When ``TF_LOG`` (or a sibling) is set: its provider logs carry credentials.
    """
    environ = os.environ if environ is None else environ
    found = [name for name in environ if name in REFUSED_ENV or name.startswith("TF_CLI_ARGS")]
    if found:
        raise ProvisionError(f"{', '.join(sorted(found))} set: tofu would log or rewrite requests that carry credentials. Unset it.")


def child_env(extra: Mapping[str, str] | None = None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """
    The environment for one child process: `BASE_ENV` from ours, plus exactly `extra`.

    Nothing else of ours is passed on: a secret reaches only the child that needs it.
    """
    environ = os.environ if environ is None else environ
    env = {name: environ[name] for name in BASE_ENV if name in environ}
    env["TF_IN_AUTOMATION"] = "1"
    env["CHECKPOINT_DISABLE"] = "1"
    if extra:
        env.update(extra)
    return env


def check_argv(argv: Sequence[str]) -> None:
    """
    Refuse a command line that holds a secret value.

    Raises
    ------
    ProvisionError
        When any registered secret appears in `argv` (ps and /proc would show it).
    """
    from hpldn.backend.common.secrets import loaded_secrets

    joined = "\0".join(argv)
    for value in loaded_secrets():
        if value in joined:
            raise ProvisionError(f"refusing to run {argv[0]}: a secret is on its command line")


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    capture: bool = False,
    check: bool = True,
    echo: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """
    Run `argv` with `env` (from `child_env`), every output line redacted.

    With `capture`, stdout is returned (not printed) and stderr is printed;
    otherwise both are printed as they come. Stdin is `stdin` (bytes, e.g. a
    secret for the host) or nothing.
    """
    check_argv(argv)
    env = dict(env if env is not None else child_env())
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture else subprocess.STDOUT,
    )
    if stdin is not None:
        try:
            proc.stdin.write(stdin)
        except BrokenPipeError:
            pass
        proc.stdin.close()
    if capture:
        out, err = proc.communicate(timeout=timeout)
        if echo:
            for line in err.decode("utf-8", "replace").splitlines():
                print(redact(line), file=sys.stderr)
        stdout = out.decode("utf-8", "replace")
        captured_err = err.decode("utf-8", "replace")
    else:
        collected = []
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            collected.append(line)
            if echo:
                print(redact(line), file=sys.stderr, flush=True)
        proc.wait(timeout=timeout)
        stdout = "\n".join(collected)
        captured_err = ""
    done = subprocess.CompletedProcess(list(argv), proc.returncode, stdout, captured_err)
    if check and proc.returncode != 0:
        tail = redact((captured_err or stdout)[-2000:])
        raise ProvisionError(f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''} failed ({proc.returncode}): {tail}")
    return done


# ---------------------------------------------------------------------------
# Settings and secrets
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    """``oci.toml``: everything that is not a secret."""

    region: str
    tenancy_ocid: str
    domain: str
    zone_name: str
    cloudflare_account_id: str
    alert_email: str
    ssh_public_key_file: Path
    deployer_public_key_file: Path
    admin_profile: str = "DEFAULT"
    deployer_profile: str = "hpldn-deployer"
    oci_config_file: Path = Path("~/.oci/config")
    use_lockfile: bool = False
    bootstrap_ssh: bool = False
    admin_cidr: str = "192.0.2.1/32"
    ocpus: int = 4
    memory_gb: int = 24
    boot_gb: int = 100
    model: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG).expanduser()
        if not path.is_file():
            raise ProvisionError(f"no settings at {path}: copy {OCI_DIR / 'oci.example.toml'} there and fill it in")
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        oci, site, ssh, tofu = (data.get(k, {}) for k in ("oci", "site", "ssh", "tofu"))
        try:
            settings = cls(
                region=oci["region"],
                tenancy_ocid=oci["tenancy_ocid"],
                admin_profile=oci.get("admin_profile", "DEFAULT"),
                deployer_profile=oci.get("deployer_profile", "hpldn-deployer"),
                oci_config_file=Path(oci.get("config_file", "~/.oci/config")).expanduser(),
                deployer_public_key_file=Path(oci["deployer_public_key"]).expanduser(),
                alert_email=oci["alert_email"],
                domain=site["domain"],
                zone_name=site["zone_name"],
                cloudflare_account_id=site["cloudflare_account_id"],
                ssh_public_key_file=Path(ssh["public_key"]).expanduser(),
                use_lockfile=bool(tofu.get("use_lockfile", False)),
                bootstrap_ssh=bool(tofu.get("bootstrap_ssh", False)),
                admin_cidr=tofu.get("admin_cidr", "192.0.2.1/32"),
                ocpus=int(tofu.get("ocpus", 4)),
                memory_gb=int(tofu.get("memory_gb", 24)),
                boot_gb=int(tofu.get("boot_gb", 100)),
                model=Path(site["model"]).expanduser() if site.get("model") else None,
            )
        except KeyError as exc:
            raise ProvisionError(f"{path} is missing {exc.args[0]!r} (see oci.example.toml)") from None
        return settings


@dataclass
class Secrets:
    """The secrets, from the environment only, each registered for redaction."""

    passphrase: str
    cloudflare_token: str = ""
    tailscale_client_id: str = ""
    tailscale_client_secret: str = ""
    recaptcha_site_key: str = ""
    recaptcha_secret_key: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, need_cloud: bool = True, need_recaptcha: bool = False) -> "Secrets":
        environ = os.environ if environ is None else environ
        passphrase = secret(environ.get(PASSPHRASE_ENV, ""))
        if len(passphrase) < MIN_PASSPHRASE:
            raise ProvisionError(f"{PASSPHRASE_ENV} must be set and at least {MIN_PASSPHRASE} characters (from your password manager)")
        found = cls(
            passphrase=passphrase,
            cloudflare_token=secret(environ.get("CLOUDFLARE_API_TOKEN")),
            tailscale_client_id=secret(environ.get("TAILSCALE_OAUTH_CLIENT_ID")),
            tailscale_client_secret=secret(environ.get("TAILSCALE_OAUTH_CLIENT_SECRET")),
            # Public, but kept beside its secret; not registered (it is no secret).
            recaptcha_site_key=environ.get("RECAPTCHA_SITE_KEY", ""),
            recaptcha_secret_key=secret(environ.get("RECAPTCHA_SECRET_KEY")),
        )
        missing = []
        if need_cloud:
            missing += [n for n, v in (("CLOUDFLARE_API_TOKEN", found.cloudflare_token),
                                       ("TAILSCALE_OAUTH_CLIENT_ID", found.tailscale_client_id),
                                       ("TAILSCALE_OAUTH_CLIENT_SECRET", found.tailscale_client_secret)) if not v]
        if need_recaptcha:
            missing += [n for n, v in (("RECAPTCHA_SITE_KEY", found.recaptcha_site_key),
                                       ("RECAPTCHA_SECRET_KEY", found.recaptcha_secret_key)) if not v]
        if missing:
            raise ProvisionError(f"{', '.join(missing)} not set (inject them from your password manager)")
        return found


# ---------------------------------------------------------------------------
# tofu
# ---------------------------------------------------------------------------
def tofu_binary() -> str:
    found = shutil.which("tofu") or str(Path("~/.local/bin/tofu").expanduser())
    if not Path(found).is_file():
        raise ProvisionError("tofu is not installed: scripts/install_tofu.sh")
    return found


def pinned_tofu_version() -> str:
    match = re.search(r"^TOFU_VERSION=([0-9.]+)$", INSTALL_TOFU.read_text(), re.MULTILINE)
    if not match:
        raise ProvisionError(f"no TOFU_VERSION in {INSTALL_TOFU}")
    return match.group(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def operator_lock() -> Iterator[None]:
    """One tofu run at a time from this laptop (the S3 backend's lock is not relied on)."""
    path = LOCK_FILE.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProvisionError(f"another provision run holds {path}") from None
        yield


@contextlib.contextmanager
def private_tmpdir() -> Iterator[Path]:
    """A 0700 directory for plan and backend files, removed (and its files overwritten) afterwards."""
    path = Path(tempfile.mkdtemp(prefix="hpldn-tofu-"))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        for item in path.rglob("*"):
            if item.is_file():
                with contextlib.suppress(OSError):
                    item.write_bytes(b"\0" * item.stat().st_size)
        shutil.rmtree(path, ignore_errors=True)


class Tofu:
    """tofu in one module directory, with its environment and secrets."""

    def __init__(self, workdir: Path, env: Mapping[str, str]) -> None:
        self.workdir = workdir
        self.env = dict(env)
        self.binary = tofu_binary()

    def __call__(self, *args: str, capture: bool = False, check: bool = True, echo: bool = True) -> subprocess.CompletedProcess:
        return run([self.binary, f"-chdir={self.workdir}", *args], env=self.env, capture=capture, check=check, echo=echo)

    def outputs(self) -> dict:
        """``tofu output -json``, parsed in memory; every sensitive value is registered for redaction."""
        done = self("output", "-json", capture=True, echo=False)
        outputs = json.loads(done.stdout or "{}")
        values = {}
        for name, entry in outputs.items():
            value = entry.get("value")
            if entry.get("sensitive") and isinstance(value, str):
                secret(value)
            values[name] = value
        return values


def tf_vars(values: Mapping[str, object]) -> dict[str, str]:
    """``TF_VAR_*`` for each value (strings as-is, the rest as HCL/JSON)."""
    out = {}
    for name, value in values.items():
        out[f"TF_VAR_{name}"] = value if isinstance(value, str) else json.dumps(value)
    return out


def state_tofu(settings: Settings, secrets: Secrets) -> Tofu:
    env = child_env({
        "TF_VAR_state_passphrase": secrets.passphrase,
        "OCI_CLI_CONFIG_FILE": str(settings.oci_config_file),
        **tf_vars({
            "oci_profile": settings.admin_profile,
            "tenancy_ocid": settings.tenancy_ocid,
            "region": settings.region,
            "alert_email": settings.alert_email,
            "deployer_public_key_pem": settings.deployer_public_key_file.read_text() if settings.deployer_public_key_file.is_file() else "",
        }),
    })
    return Tofu(STATE_DIR, env)


def entrypoint_ids(settings: Settings, secrets: Secrets) -> dict[str, str]:
    """The zone's existing custom-firewall, redirect and cache rulesets (a zone has only one per phase), "" where it has none.

    A token without Zone -> Single Redirect: Edit (or Cache Rules: Edit) cannot read that phase; that is only a
    warning here, so `bootstrap` and `status` still run, and `up` then fails at Cloudflare naming the missing
    permission.
    """
    import httpx

    headers = {"Authorization": f"Bearer {secrets.cloudflare_token}"}
    ids = {}
    with httpx.Client(base_url="https://api.cloudflare.com/client/v4", headers=headers, timeout=20) as client:
        zones = client.get("/zones", params={"name": settings.zone_name})
        zones.raise_for_status()
        zone_id = zones.json()["result"][0]["id"]
        optional = {
            "http_request_dynamic_redirect": "redirect rules (add Zone -> Single Redirect: Edit, README.md)",
            "http_request_cache_settings": "cache rules (add Zone -> Cache Rules: Edit, README.md)",
        }
        for var, phase in (("firewall_entrypoint_id", "http_request_firewall_custom"),
                           ("redirect_entrypoint_id", "http_request_dynamic_redirect"),
                           ("cache_entrypoint_id", "http_request_cache_settings")):
            entry = client.get(f"/zones/{zone_id}/rulesets/phases/{phase}/entrypoint")
            if entry.status_code == 404:
                ids[var] = ""
                continue
            if entry.status_code == 403 and phase in optional:
                say(f"WARNING: the Cloudflare token cannot read {optional[phase]}")
                ids[var] = ""
                continue
            entry.raise_for_status()
            ids[var] = entry.json()["result"]["id"]
    return ids


def stray_origin_records(settings: Settings, secrets: Secrets, public_ip: str) -> list[dict]:
    """The zone's A/AAAA records for the site (apex and www) that point anywhere but `public_ip`.

    Cloudflare spreads a proxied name over all of its addresses: one dead
    address (for example a stale record made by hand) makes every
    request Cloudflare cannot answer from its cache wait ~19 s while it
    times out connecting there.
    """
    import httpx

    headers = {"Authorization": f"Bearer {secrets.cloudflare_token}"}
    stray = []
    with httpx.Client(base_url="https://api.cloudflare.com/client/v4", headers=headers, timeout=20) as client:
        zones = client.get("/zones", params={"name": settings.zone_name})
        zones.raise_for_status()
        zone_id = zones.json()["result"][0]["id"]
        for name in (settings.domain, f"www.{settings.domain}"):
            records = client.get(f"/zones/{zone_id}/dns_records", params={"name": name, "per_page": 100})
            records.raise_for_status()
            stray += [r for r in records.json()["result"] if r.get("type") in ("A", "AAAA") and r.get("content") != public_ip]
    return stray


def refuse_stray_origin_records(settings: Settings, secrets: Secrets, public_ip: str) -> None:
    """Stop when the site's name has an origin address besides the instance's (never deleted for you)."""
    stray = stray_origin_records(settings, secrets, public_ip)
    if stray:
        listed = "; ".join(f"{r['type']} {r['name']} -> {r['content']} (id {r['id']})" for r in stray)
        raise ProvisionError(f"the zone has origin records besides the instance's {public_ip}: {listed}. Cloudflare "
                             "spreads requests over them and waits out a dead one; delete them (dashboard -> DNS -> "
                             "Records, or DELETE /zones/<zone>/dns_records/<id>) and run this again")


def main_tofu(settings: Settings, secrets: Secrets, state: Mapping[str, object], **overrides: object) -> Tofu:
    env = child_env({
        "TF_VAR_state_passphrase": secrets.passphrase,
        "CLOUDFLARE_API_TOKEN": secrets.cloudflare_token,
        "TAILSCALE_OAUTH_CLIENT_ID": secrets.tailscale_client_id,
        "TAILSCALE_OAUTH_CLIENT_SECRET": secrets.tailscale_client_secret,
        # The state backend's S3 key, from cloud/oci/state's outputs: env only.
        "AWS_ACCESS_KEY_ID": str(state["s3_access_key_id"]),
        "AWS_SECRET_ACCESS_KEY": str(state["s3_secret_key"]),
        "OCI_CLI_CONFIG_FILE": str(settings.oci_config_file),
        **tf_vars({
            "oci_profile": settings.deployer_profile,
            "region": settings.region,
            "tenancy_ocid": settings.tenancy_ocid,
            "compartment_id": state["compartment_id"],
            "kms_key_id": state["kms_key_id"],
            "domain": settings.domain,
            "zone_name": settings.zone_name,
            "cloudflare_account_id": settings.cloudflare_account_id,
            "ssh_public_key": settings.ssh_public_key_file.read_text().strip(),
            "bootstrap_ssh": settings.bootstrap_ssh,
            "admin_cidr": settings.admin_cidr,
            "ocpus": settings.ocpus,
            "memory_gb": settings.memory_gb,
            "boot_gb": settings.boot_gb,
            **entrypoint_ids(settings, secrets),
            **overrides,
        }),
    })
    return Tofu(MAIN_DIR, env)


def backend_config(state: Mapping[str, object], settings: Settings) -> str:
    """The partial S3 backend configuration for main/ (no credentials in it)."""
    return (
        f'region = "{settings.region}"\n'
        f'endpoints = {{ s3 = "{state["s3_endpoint"]}" }}\n'
        f"use_lockfile = {'true' if settings.use_lockfile else 'false'}\n"
    )


def read_state_outputs(settings: Settings, secrets: Secrets) -> dict:
    tofu = state_tofu(settings, secrets)
    if not (STATE_DIR / "terraform.tfstate").is_file():
        raise ProvisionError("cloud/oci/state has no state yet: run `python -m hpldn.backend.cloud.oci state` as the tenancy admin")
    if not (STATE_DIR / ".terraform").is_dir():
        tofu("init", "-input=false", "-backend=false", echo=False)
    return tofu.outputs()


def init_main(tofu: Tofu, state: Mapping[str, object], settings: Settings, tmp: Path) -> None:
    backend = tmp / "backend.tfbackend"
    fd = os.open(backend, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(backend_config(state, settings))
    tofu("init", "-input=false", "-reconfigure", f"-backend-config={backend}")
    backend.unlink()


def apply_main(tofu: Tofu, tmp: Path, *extra: str) -> subprocess.CompletedProcess:
    """Plan into the private directory, apply that plan, delete it."""
    plan = tmp / "main.tfplan"
    tofu("plan", "-input=false", f"-out={plan}", *extra)
    try:
        return tofu("apply", "-input=false", str(plan), check=False)
    finally:
        plan.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The host: pinned keys, SSH, secrets on stdin
# ---------------------------------------------------------------------------
def parse_host_keys(console: str) -> list[tuple[str, str]]:
    """The ``(type, base64)`` host keys between the HPLDN-HOST-KEYS markers of a console log, deduplicated."""
    keys: list[tuple[str, str]] = []
    inside = False
    for line in console.splitlines():
        if HOST_KEYS_BEGIN in line:
            inside = True
            continue
        if HOST_KEYS_END in line:
            inside = False
            continue
        if inside:
            match = _HOST_KEY_LINE.search(line)
            if match and match.groups() not in keys:
                keys.append(match.groups())
    return keys


def known_hosts_lines(keys: Sequence[tuple[str, str]], names: Sequence[str]) -> str:
    """known_hosts lines for each key under `names` (comma-joined, unhashed so they can be read)."""
    if not keys:
        raise ProvisionError("no host keys to pin")
    host = ",".join(names)
    return "".join(f"{host} {kind} {blob}\n" for kind, blob in keys)


def write_known_hosts(text: str) -> Path:
    path = KNOWN_HOSTS.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)
    return path


def ssh_options() -> list[str]:
    return [
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS.expanduser()}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ForwardAgent=no",
    ]


def ssh(command: Sequence[str], *, stdin: bytes | None = None, capture: bool = False, check: bool = True, host: str = TAILNET_HOST) -> subprocess.CompletedProcess:
    """One command on the host over SSH (pinned host keys), `stdin` piped in."""
    return run(["ssh", *ssh_options(), f"{SSH_USER}@{host}", *command], stdin=stdin, capture=capture, check=check)


def tls_bundle(origin_cert: str, origin_key: str, aop_ca: str) -> bytes:
    """The tar stream ``bootstrap.sh --set-tls`` reads: origin.crt, origin.key, aop_ca.pem (plain names, root, 0600)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, text in (("origin.crt", origin_cert), ("origin.key", origin_key), ("aop_ca.pem", aop_ca)):
            data = text.encode("ascii")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def push_secrets(main_out: Mapping[str, object], secrets: Secrets) -> None:
    """Every host secret, on SSH stdin to bootstrap.sh's non-interactive modes."""
    say("pushing the origin certificate and the origin-pull CA (--set-tls)")
    ssh(["sudo", "bash", HOST_VPS, "--set-tls"],
        stdin=tls_bundle(str(main_out["origin_cert_pem"]), str(main_out["origin_key_pem"]), str(main_out["aop_ca_pem"])))
    say("pushing the reCAPTCHA keys (--set-recaptcha-keys)")
    ssh(["sudo", "bash", HOST_VPS, "--set-recaptcha-keys"],
        stdin=f"{secrets.recaptcha_site_key}\n{secrets.recaptcha_secret_key}\n".encode())
    say("pushing the guard's Cloudflare token (--set-cloudflare-token)")
    ssh(["sudo", "bash", HOST_VPS, "--set-cloudflare-token"], stdin=f"{main_out['guard_token']}\n".encode())
    say("setting the guard's Cloudflare account (--set-cloudflare-account)")
    ssh(["sudo", "bash", HOST_VPS, "--set-cloudflare-account"], stdin=f"{main_out['cloudflare_account_id']}\n".encode())


def leak_hashes(values: Sequence[str], prefix: int = 16) -> list[str]:
    """SHA-256 prefixes of each secret value: what the host's leak scan looks for, never the values."""
    return sorted({hashlib.sha256(v.encode()).hexdigest()[:prefix] for v in values if v})


# Runs on the host (python3 -I, as root). It hashes every token-shaped run in
# the journal and nginx's logs of the last 30 days and reports any whose
# SHA-256 prefix is one of the pushed secrets'. Only hashes cross the wire.
LEAK_SCAN = r"""
import hashlib, json, re, subprocess, sys, glob
wanted = set(json.load(sys.stdin))
token = re.compile(rb"[A-Za-z0-9_+/=.-]{16,}")
def scan(blob, where):
    hits = 0
    for match in token.finditer(blob):
        for candidate in {match.group(0), match.group(0).strip(b"=.")}:
            if hashlib.sha256(candidate).hexdigest()[:16] in wanted:
                hits += 1
    if hits:
        print(f"LEAK {where}: {hits} occurrence(s)")
    return hits
total = scan(subprocess.run(["journalctl", "--since", "-30d", "-o", "cat", "--no-pager"], capture_output=True).stdout, "journal")
for path in glob.glob("/var/log/nginx/*.log") + glob.glob("/var/log/postgresql/*.log"):
    try:
        with open(path, "rb") as handle:
            total += scan(handle.read(), path)
    except OSError as exc:
        print(f"skipped {path}: {exc.strerror}")
print(f"leak scan: {total} occurrence(s)")
sys.exit(1 if total else 0)
"""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _profile(config_file: Path, profile: str) -> dict[str, str]:
    import configparser

    parser = configparser.ConfigParser()
    if not parser.read(config_file):
        raise ProvisionError(f"no OCI config at {config_file}")
    if profile not in parser and profile != "DEFAULT":
        raise ProvisionError(f"no profile [{profile}] in {config_file}")
    return dict(parser[profile])


def check_tofu() -> Check:
    try:
        binary = Path(tofu_binary())
        version = run([str(binary), "version", "-json"], capture=True, echo=False)
        found = json.loads(version.stdout).get("terraform_version", "")
        pinned = pinned_tofu_version()
        recorded = binary.with_name(f".tofu-{pinned}.sha256")
        if found != pinned:
            return Check("tofu", False, f"{binary} is {found}; scripts/install_tofu.sh pins {pinned}")
        if not recorded.is_file() or recorded.read_text().strip() != sha256_file(binary):
            return Check("tofu", False, f"{binary} does not match the digest install_tofu.sh recorded after verifying it; re-run it")
        return Check("tofu", True, f"{found}, verified binary")
    except (ProvisionError, ValueError) as exc:
        return Check("tofu", False, str(exc))


def check_oci_profile(settings: Settings, profile: str) -> Check:
    try:
        entries = _profile(settings.oci_config_file, profile)
        key = Path(entries.get("key_file", "")).expanduser()
        if not key.is_file():
            return Check(f"oci [{profile}]", False, f"key_file {key} is missing")
        for path in (key, settings.oci_config_file):
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                return Check(f"oci [{profile}]", False, f"{path} has mode {mode:o}; chmod 600")
        done = run(["oci", "os", "ns", "get", "--profile", profile, "--config-file", str(settings.oci_config_file)],
                   env=child_env({"OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING": "False"}), capture=True, check=False, echo=False)
        if done.returncode != 0:
            return Check(f"oci [{profile}]", False, "the profile cannot authenticate (oci os ns get failed)")
        return Check(f"oci [{profile}]", True, "authenticates; key and config 0600")
    except (ProvisionError, OSError) as exc:
        return Check(f"oci [{profile}]", False, str(exc))


def check_cloudflare(settings: Settings, secrets: Secrets) -> Check:
    import httpx

    headers = {"Authorization": f"Bearer {secrets.cloudflare_token}"}
    try:
        with httpx.Client(base_url="https://api.cloudflare.com/client/v4", headers=headers, timeout=20) as client:
            verify = client.get(f"/accounts/{settings.cloudflare_account_id}/tokens/verify")
            if verify.status_code != 200:
                verify = client.get("/user/tokens/verify")
            if verify.status_code != 200 or verify.json().get("result", {}).get("status") != "active":
                return Check("cloudflare token", False, f"not active ({verify.status_code})")
            zones = client.get("/zones", params={"name": settings.zone_name})
            if zones.status_code != 200 or len(zones.json().get("result", [])) != 1:
                return Check("cloudflare token", False, f"cannot read zone {settings.zone_name} (needs Zone:Read and the zone in its resources)")
            groups = client.get(f"/accounts/{settings.cloudflare_account_id}/tokens/permission_groups")
            if groups.status_code != 200:
                return Check("cloudflare token", False, "cannot list permission groups (needs Account API Tokens: Edit)")
    except httpx.HTTPError as exc:
        return Check("cloudflare token", False, f"Cloudflare unreachable: {type(exc).__name__}")
    return Check("cloudflare token", True, "active; reads the zone and the account's token permission groups")


def check_tailscale(secrets: Secrets) -> Check:
    import httpx

    try:
        response = httpx.post(
            "https://api.tailscale.com/api/v2/oauth/token",
            data={"client_id": secrets.tailscale_client_id, "client_secret": secrets.tailscale_client_secret,
                  "grant_type": "client_credentials", "scope": "auth_keys"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        return Check("tailscale oauth", False, f"Tailscale unreachable: {type(exc).__name__}")
    if response.status_code != 200:
        return Check("tailscale oauth", False, f"the OAuth client was refused ({response.status_code})")
    token = response.json().get("access_token", "")
    secret(token)
    scopes = response.json().get("scope", "")
    if scopes and set(scopes.split()) - {"auth_keys", "auth_keys:read"}:
        return Check("tailscale oauth", False, f"the OAuth client has more than auth_keys: {scopes}")
    return Check("tailscale oauth", True, "client credentials work; scope auth_keys")


def check_recaptcha(secrets: Secrets) -> Check:
    pattern = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
    if not (pattern.match(secrets.recaptcha_site_key) and pattern.match(secrets.recaptcha_secret_key)):
        return Check("recaptcha", False, "RECAPTCHA_SITE_KEY/RECAPTCHA_SECRET_KEY missing or malformed")
    if secrets.recaptcha_site_key == secrets.recaptcha_secret_key:
        return Check("recaptcha", False, "the site key and the secret key are the same")
    return Check("recaptcha", True, "both keys present")


def _tracked(path: Path) -> bool:
    done = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(path)],
                          capture_output=True, env=child_env())
    return done.returncode == 0


def check_signing_keys() -> Check:
    model_keys = [p for p in MODEL_KEYS.glob("*.pem") if _tracked(p)]
    dump_keys = [p for p in DUMP_KEYS.glob("*.pem") if _tracked(p)]
    serving = SERVING_SIGNING_KEY.expanduser()
    problems = []
    if not model_keys:
        problems.append("no committed model public key (models/trusted_keys/)")
    if not dump_keys:
        problems.append("no committed dump public key (cloud/vps/trusted_keys/; export_serving_db --keygen)")
    if not serving.is_file():
        problems.append(f"no serving signing key at {serving}")
    elif serving.stat().st_mode & 0o077:
        problems.append(f"{serving} is not 0600")
    else:
        from hpldn.backend.models import artifact

        kid = artifact.key_id(artifact.load_signing_key(serving).public_key())
        if kid not in {p.stem for p in dump_keys}:
            problems.append(f"the serving signing key {kid} has no committed public key in cloud/vps/trusted_keys/")
    if problems:
        return Check("signing keys", False, "; ".join(problems))
    return Check("signing keys", True, f"{len(model_keys)} model key(s), {len(dump_keys)} dump key(s) committed; serving key present")


def check_state_decrypts(settings: Settings, secrets: Secrets) -> Check:
    try:
        outputs = read_state_outputs(settings, secrets)
    except ProvisionError as exc:
        return Check("state", False, str(exc))
    needed = {"compartment_id", "kms_key_id", "s3_access_key_id", "s3_secret_key", "s3_endpoint"}
    missing = needed - set(outputs)
    if missing:
        return Check("state", False, f"cloud/oci/state outputs lack {sorted(missing)}")
    return Check("state", True, "cloud/oci/state decrypts with the passphrase")


def run_checks(settings: Settings, secrets: Secrets) -> list[Check]:
    checks = [
        check_tofu(),
        Check("passphrase", len(secrets.passphrase) >= MIN_PASSPHRASE, f">= {MIN_PASSPHRASE} characters"),
        check_oci_profile(settings, settings.deployer_profile),
        check_cloudflare(settings, secrets),
        check_tailscale(secrets),
        check_tailnet_client(),
        check_recaptcha(secrets),
        check_signing_keys(),
        Check("ssh key", settings.ssh_public_key_file.is_file() and settings.ssh_public_key_file.read_text().startswith("ssh-ed25519 "),
              f"{settings.ssh_public_key_file} (ed25519)"),
        check_state_decrypts(settings, secrets),
    ]
    for item in checks:
        print(f"  {'ok ' if item.ok else 'FAIL'} {item.name:<18} {redact(item.detail)}", file=sys.stderr)
    return checks


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_check(settings: Settings, _args) -> int:
    secrets = Secrets.from_env(need_cloud=True, need_recaptcha=True)
    failed = [c for c in run_checks(settings, secrets) if not c.ok]
    return 1 if failed else 0


def cmd_state(settings: Settings, args) -> int:
    """cloud/oci/state, as the tenancy admin (local, encrypted state), then its backup in the bucket."""
    secrets = Secrets.from_env(need_cloud=False)
    tofu = state_tofu(settings, secrets)
    with operator_lock(), private_tmpdir() as tmp:
        tofu("init", "-input=false")
        plan = tmp / "state.tfplan"
        tofu("plan", "-input=false", f"-out={plan}")
        if not args.yes and input("apply this plan? type 'yes': ").strip() != "yes":
            say("not applied")
            return 1
        tofu("apply", "-input=false", str(plan))
    outputs = tofu.outputs()
    # The local state file is already encrypted (enforced); keep a copy in the bucket.
    run(["oci", "os", "object", "put", "--profile", settings.admin_profile, "--config-file", str(settings.oci_config_file),
         "--namespace", str(outputs["namespace"]), "--bucket-name", str(outputs["tfstate_bucket"]),
         "--name", "bootstrap/terraform.tfstate", "--file", str(STATE_DIR / "terraform.tfstate"), "--force"],
        env=child_env())
    say(f"state applied; add the deployer profile to {settings.oci_config_file}: user={outputs['deployer_user_ocid']} "
        f"fingerprint={outputs['deployer_key_fingerprint']} tenancy={settings.tenancy_ocid} region={settings.region}")
    return 0


# The OCI CLI wraps every answer in {"data": ...}.
OCI_PAYLOAD = "data"


def oci_payload(stdout: str):
    """The payload of an ``oci`` CLI JSON answer."""
    return json.loads(stdout or "{}").get(OCI_PAYLOAD)


def _availability_domains(settings: Settings, state: Mapping[str, object]) -> list[str]:
    done = run(["oci", "iam", "availability-domain", "list", "--profile", settings.deployer_profile,
                "--config-file", str(settings.oci_config_file), "--compartment-id", str(state["compartment_id"])],
               env=child_env(), capture=True, echo=False)
    return [str(ad["name"]) for ad in oci_payload(done.stdout) or []]


def existing_ad_index(outputs: Mapping[str, object], names: Sequence[str]) -> int | None:
    """The index (ad_index) of the AD the instance in the state already runs in, or None if there is none yet."""
    current = outputs.get("availability_domain") if outputs.get("instance_id") else None
    return list(names).index(current) if current in names else None


def apply_with_capacity_retry(tofu_for: Callable[..., Tofu], settings: Settings, state: Mapping[str, object], tmp: Path,
                              extra: Sequence[str] = (), rounds: int = 3) -> None:
    """Apply main/, moving to the next availability domain on "Out of host capacity" (A1 is often full).

    An instance that already exists keeps its availability domain: a
    different ad_index would plan to replace it, so only that one is applied.
    """
    names = _availability_domains(settings, state)
    domains = max(1, len(names))
    try:
        current = existing_ad_index(tofu_for().outputs(), names)
    except ProvisionError:
        current = None
    if current is not None:
        say(f"the instance exists in {names[current]}; applying there only")
        if apply_main(tofu_for(ad_index=current), tmp, *extra).returncode != 0:
            raise ProvisionError("tofu apply failed (see above)")
        return
    for attempt in range(rounds * domains):
        ad = attempt % domains
        tofu = tofu_for(ad_index=ad)
        done = apply_main(tofu, tmp, *extra)
        if done.returncode == 0:
            return
        if not OUT_OF_CAPACITY.search(done.stdout or ""):
            raise ProvisionError("tofu apply failed (see above)")
        say(f"availability domain {ad} is out of A1 capacity; trying the next")
        time.sleep(30 if ad == domains - 1 else 5)
    raise ProvisionError(f"no availability domain had A1 capacity after {rounds} rounds; try again later")


def pin_host_keys(settings: Settings, instance_id: str, public_ip: str, timeout_s: float = 900) -> Path:
    """Read the host keys cloud-init printed on the serial console and pin them in ~/.config/hpldn/known_hosts."""
    base = ["oci", "--profile", settings.deployer_profile, "--config-file", str(settings.oci_config_file)]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        capture = run([*base, "compute", "console-history", "capture", "--instance-id", instance_id],
                      env=child_env(), capture=True, echo=False)
        history = oci_payload(capture.stdout)["id"]
        for _ in range(30):
            state = oci_payload(run([*base, "compute", "console-history", "get", "--instance-console-history-id", history],
                                   env=child_env(), capture=True, echo=False).stdout)["lifecycle-state"]
            if state in ("SUCCEEDED", "FAILED"):
                break
            time.sleep(5)
        with private_tmpdir() as tmp:
            out = tmp / "console.txt"
            run([*base, "compute", "console-history", "get-content", "--instance-console-history-id", history,
                 "--length", "10000000", "--file", str(out)], env=child_env(), capture=True, echo=False)
            console = out.read_text(errors="replace")
        keys = parse_host_keys(console)
        if keys:
            path = write_known_hosts(known_hosts_lines(keys, [TAILNET_HOST, public_ip]))
            say(f"pinned {len(keys)} host key(s) in {path}")
            return path
        say("host keys not on the console yet (cloud-init still running); waiting")
        time.sleep(30)
    raise ProvisionError("cloud-init never printed the host keys on the console; check the console history in the OCI console")


def tailnet_client_problem(status: Mapping[str, object] | None = None) -> str | None:
    """Why this laptop cannot SSH to the host over the tailnet, or None.

    The ACL (README.md) lets only autogroup:admin reach tag:hpldn:22, and the
    tag itself reaches nothing: a laptop that joined as a tagged node (for
    instance with the host's `tailscale up --advertise-tags=tag:hpldn`) loses
    its user identity, sees no peers and cannot resolve or reach the host.
    """
    if status is None:
        done = run(["tailscale", "status", "--json"], env=child_env(), capture=True, check=False, echo=False)
        if done.returncode != 0 and not done.stdout:
            return "tailscale is not running on this laptop (`tailscale up`)"
        status = json.loads(done.stdout or "{}")
    if status.get("BackendState") != "Running":
        return f"tailscale on this laptop is {status.get('BackendState') or 'not running'} (`tailscale up`)"
    me = status.get("Self") or {}
    if me.get("Tags"):
        return (f"this laptop is on the tailnet as a tagged node ({', '.join(me['Tags'])}, hostname "
                f"{me.get('HostName')!r}), so the ACL gives it nothing; log it back in as yourself: "
                "`sudo tailscale up --reset --force-reauth`")
    if me.get("HostName") == TAILNET_HOST:
        return f"this laptop's tailnet hostname is {TAILNET_HOST!r}, the host's name (`sudo tailscale set --hostname=$(hostname)`)"
    return None


def check_tailnet_client() -> Check:
    problem = tailnet_client_problem()
    return Check("tailnet (laptop)", problem is None, problem or "this laptop is on the tailnet as a user, untagged")


def wait_for_tailnet(timeout_s: float = 900) -> None:
    problem = tailnet_client_problem()
    if problem:
        raise ProvisionError(problem)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        done = run(["tailscale", "status", "--json"], env=child_env(), capture=True, check=False, echo=False)
        if done.returncode == 0:
            peers = json.loads(done.stdout or "{}").get("Peer") or {}
            if any(p.get("HostName") == TAILNET_HOST and p.get("Online") for p in peers.values()):
                if ssh(["true"], check=False).returncode == 0:
                    say(f"{TAILNET_HOST} is on the tailnet and SSH (pinned key) works")
                    return
        time.sleep(15)
    raise ProvisionError(f"{TAILNET_HOST} did not appear on the tailnet; is this laptop on it, and is the ACL in README.md applied?")


def deploy_sh(args: Sequence[str]) -> None:
    problem = tailnet_client_problem()
    if problem:
        raise ProvisionError(problem)
    env = child_env({"HPLDN_KNOWN_HOSTS": str(KNOWN_HOSTS.expanduser())})
    run(["bash", str(DEPLOY_SH), "--host", TAILNET_HOST, *args], env=env, cwd=REPO_ROOT)


# The most the page may take to answer through Cloudflare (smoke_test).
ORIGIN_TTFB_LIMIT_S = 1.5

# Cloudflare's own answers when it cannot reach the origin over TLS (SSL mode strict).
ORIGIN_TLS_ERRORS = {
    525: "the TLS handshake with the origin failed (host name not nginx's server_name, or the AOP client certificate refused)",
    526: "the origin's certificate is not a valid one (still bootstrap.sh's placeholder? `bootstrap` pushes --set-tls)",
}


def smoke_test(domain: str, public_ip: str) -> None:
    """Through Cloudflare: the site and /api/areas answer; docs and maps do not; the origin refuses a direct client."""
    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (hpldn provision smoke test)"}
    with httpx.Client(base_url=f"https://{domain}", headers=headers, timeout=30, follow_redirects=False) as client:
        for path, allowed in (("/", {200}), ("/api/areas", {200}), ("/api/docs", {403, 404}),
                              ("/api/openapi.json", {403, 404}), ("/static/js/main.js.map", {404}), ("/api/nope", {404})):
            response = client.get(path)
            status = response.status_code
            if status in ORIGIN_TLS_ERRORS:
                raise ProvisionError(f"GET https://{domain}{path} answered Cloudflare {status}: {ORIGIN_TLS_ERRORS[status]}")
            if status not in allowed:
                raise ProvisionError(f"GET https://{domain}{path} answered {status}, expected {sorted(allowed)}")
            # Anything Cloudflare writes into the page (Bot Fight Mode's JS
            # detections, Rocket Loader, email obfuscation) is a script the
            # CSP refuses; JS detections failing for every visitor got fresh
            # iOS Safari visitors tarpitted at the edge (cloudflare.tf).
            if path == "/" and ("/cdn-cgi/" in response.text or "__CF$cv$params" in response.text):
                raise ProvisionError(f"GET https://{domain}/: Cloudflare injects a script into the page that the CSP "
                                     "blocks (Bot Fight Mode / JS detections?)")
            say(f"GET {path}: {status}")
        # The page itself is never cached at the edge (no-cache), so its
        # answer times Cloudflare's way to the origin: best of three.
        times = []
        for attempt in range(3):
            started = time.perf_counter()
            client.get("/", params={"smoke": str(attempt)})
            times.append(time.perf_counter() - started)
        answer_s = min(times)
        if answer_s > ORIGIN_TTFB_LIMIT_S:
            raise ProvisionError(f"GET https://{domain}/ took {answer_s:.1f} s to answer through Cloudflare "
                                 f"(limit {ORIGIN_TTFB_LIMIT_S} s): a stray origin record, or the origin is slow")
        say(f"GET /: answered in {answer_s * 1000:.0f} ms")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((public_ip, 443), timeout=10) as raw, context.wrap_socket(raw, server_hostname=domain) as tls:
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode())
            answer = tls.recv(64)
        if answer:
            raise ProvisionError(f"the origin {public_ip} answered a direct client without the origin-pull certificate")
    except (OSError, ssl.SSLError):
        say(f"direct https://{public_ip} (not via Cloudflare): refused, as it must be")


def _main_with_state(settings: Settings, secrets: Secrets):
    state = read_state_outputs(settings, secrets)
    return state, (lambda **overrides: main_tofu(settings, secrets, state, **overrides))


def cmd_up(settings: Settings, args) -> int:
    secrets = Secrets.from_env(need_cloud=True, need_recaptcha=True)
    refuse_logging_env()
    say("pre-flight")
    if [c for c in run_checks(settings, secrets) if not c.ok]:
        raise ProvisionError("pre-flight failed; fix the FAIL lines above")
    state, tofu_for = _main_with_state(settings, secrets)
    extra = []
    if args.renew_certs:
        extra += [f"-replace={r}" for r in RENEW_CERTS]
    if args.rotate_guard_token:
        extra += [f"-replace={r}" for r in ROTATE_GUARD_TOKEN]
    with operator_lock(), private_tmpdir() as tmp:
        init_main(tofu_for(), state, settings, tmp)
        apply_with_capacity_retry(tofu_for, settings, state, tmp, extra)
        out = tofu_for().outputs()
    pin_host_keys(settings, str(out["instance_id"]), str(out["public_ip"]))
    _bootstrap_and_deploy(settings, secrets, out, args, deploy=not args.skip_deploy)
    say(f"up: https://{settings.domain} (origin {out['public_ip']}, tailnet {TAILNET_HOST})")
    if not args.skip_deploy:
        say("up: deployed; run `deploy` only for later app/frontend/model/dump changes")
    return 0


def _bootstrap_and_deploy(settings: Settings, secrets: Secrets, out: Mapping[str, object], args, *, deploy: bool) -> None:
    """Everything `up` does once the instance exists and its host keys are pinned."""
    refuse_stray_origin_records(settings, secrets, str(out["public_ip"]))
    wait_for_tailnet()
    deploy_sh(["--bootstrap", "--domain", settings.domain])
    push_secrets(out, secrets)
    if settings.bootstrap_ssh:
        cmd_close_ssh(settings, args, secrets=secrets)
    else:
        ssh(["sudo", "bash", HOST_VPS, "--close-public-ssh"])
    if deploy:
        deploy_args = []
        if args.dump:
            deploy_args += ["--dump", str(args.dump)]
        if args.model or settings.model:
            deploy_args += ["--model", str(args.model or settings.model)]
        deploy_sh(deploy_args)
        smoke_test(settings.domain, str(out["public_ip"]))


def cmd_bootstrap(settings: Settings, args) -> int:
    """`up` without tofu apply: bootstrap, secrets and (with --dump/--model) a deploy on the existing instance."""
    secrets = Secrets.from_env(need_cloud=True, need_recaptcha=True)
    refuse_logging_env()
    state, tofu_for = _main_with_state(settings, secrets)
    with operator_lock(), private_tmpdir() as tmp:
        init_main(tofu_for(), state, settings, tmp)
        out = tofu_for().outputs()
    if not out.get("instance_id"):
        raise ProvisionError("no instance in the main state; run `up` first")
    known = KNOWN_HOSTS.expanduser()
    if not (known.is_file() and known.stat().st_size):
        pin_host_keys(settings, str(out["instance_id"]), str(out["public_ip"]))
    _bootstrap_and_deploy(settings, secrets, out, args, deploy=bool(args.dump or args.model))
    say(f"bootstrap: done on {TAILNET_HOST} (origin {out['public_ip']})")
    return 0


def cmd_deploy(settings: Settings, args) -> int:
    refuse_logging_env()
    dump = args.dump
    if args.export:
        say("exporting and signing a serving dump")
        run([sys.executable, "-m", "hpldn.backend.scripts.export_serving_db", "--sign"], env=child_env(dict(
            (k, os.environ[k]) for k in ("HPLDN_DIR_FP", "HPLDN_CONFIG_FP", "HPLDN_DB_URL", "HPLDN_SERVING_SIGNING_KEY") if k in os.environ)), cwd=REPO_ROOT)
        dump = sorted((REPO_ROOT / "tmp" / "serving").glob("20*"))[-1]
    if dump:
        say(f"verifying the dump {dump}")
        run([sys.executable, "-m", "hpldn.backend.scripts.export_serving_db", "--verify", str(dump)], env=child_env(), cwd=REPO_ROOT)
    model = args.model or settings.model
    if not args.skip_model and model:
        say(f"verifying the model {model}")
        run([sys.executable, "-m", "hpldn.backend.models.artifact", "verify", str(model)], env=child_env(), cwd=REPO_ROOT)
    deploy_args = []
    if dump:
        deploy_args += ["--dump", str(dump)]
    if model:
        deploy_args += ["--model", str(model)]
    for flag in ("skip_app", "skip_frontend", "skip_model", "allow_dirty"):
        if getattr(args, flag):
            deploy_args.append("--" + flag.replace("_", "-"))
    deploy_sh(deploy_args)
    return 0


def cmd_close_ssh(settings: Settings, _args, secrets: Secrets | None = None) -> int:
    secrets = secrets or Secrets.from_env(need_cloud=True)
    refuse_logging_env()
    state, tofu_for = _main_with_state(settings, secrets)
    with operator_lock(), private_tmpdir() as tmp:
        tofu = tofu_for(bootstrap_ssh=False)
        init_main(tofu, state, settings, tmp)
        if apply_main(tofu, tmp).returncode != 0:
            raise ProvisionError("tofu apply (bootstrap_ssh=false) failed")
    ssh(["sudo", "bash", HOST_VPS, "--close-public-ssh"])
    say("public SSH closed in the NSG and on the host")
    return 0


def cmd_status(settings: Settings, _args) -> int:
    secrets = Secrets.from_env(need_cloud=True, need_recaptcha=False)
    refuse_logging_env()
    state, tofu_for = _main_with_state(settings, secrets)
    problems = []
    with operator_lock(), private_tmpdir() as tmp:
        tofu = tofu_for()
        init_main(tofu, state, settings, tmp)
        drift = tofu("plan", "-input=false", "-detailed-exitcode", "-lock=false", f"-out={tmp / 'status.tfplan'}", check=False)
        (tmp / "status.tfplan").unlink(missing_ok=True)
        if drift.returncode == 2:
            problems.append("drift: tofu plan has changes")
        elif drift.returncode != 0:
            problems.append("tofu plan failed")
        out = tofu.outputs()
    inst = run(["oci", "--profile", settings.deployer_profile, "--config-file", str(settings.oci_config_file), "compute", "instance", "get",
                "--instance-id", str(out["instance_id"])], env=child_env(), capture=True, echo=False, check=False)
    lifecycle = oci_payload(inst.stdout)["lifecycle-state"] if inst.returncode == 0 else "unknown"
    say(f"instance: {lifecycle}; origin cert expires {out.get('origin_cert_expires_on')}; guard token expires {out.get('guard_token_expires_on')}")
    if lifecycle != "RUNNING":
        problems.append(f"instance {lifecycle}")
    for record in stray_origin_records(settings, secrets, str(out["public_ip"])):
        problems.append(f"stray origin record {record['type']} {record['name']} -> {record['content']} (id {record['id']}): delete it")
    if ssh(["sudo", "test", "-e", "/etc/hpldn/tls/placeholder"], check=False).returncode == 0:
        problems.append("placeholder TLS installed: Cloudflare answers 526 until `bootstrap` pushes --set-tls")
    security = ssh(["sudo", "systemd-analyze", "security", "--no-pager", "hpldn-api.service", "hpldn-guard.service"], capture=True, check=False)
    for line in security.stdout.splitlines():
        say(f"exposure: {line}")
        match = re.search(r"hpldn-api\.service\s+([0-9.]+)", line)
        if match and float(match.group(1)) > 2.0:
            problems.append(f"hpldn-api exposure {match.group(1)} > 2.0")
    pushed = [str(out[k]) for k in ("origin_key_pem", "guard_token") if out.get(k)] + [secrets.recaptcha_secret_key]
    pushed += [line for line in str(out.get("origin_key_pem", "")).splitlines() if len(line) >= 16 and "PRIVATE KEY" not in line]
    scan = ssh(["sudo", "python3", "-I", "-c", LEAK_SCAN], stdin=json.dumps(leak_hashes(pushed)).encode(), capture=True, check=False)
    for line in scan.stdout.splitlines():
        say(line)
    if scan.returncode != 0:
        problems.append("leak scan found secrets in the host's logs")
    for problem in problems:
        say(f"PROBLEM: {problem}")
    return 1 if problems else 0


def cmd_destroy(settings: Settings, args) -> int:
    secrets = Secrets.from_env(need_cloud=True)
    refuse_logging_env()
    typed = input(f"This destroys the host, its network and its Cloudflare records. Type the domain ({settings.domain}) to confirm: ").strip()
    if typed != settings.domain:
        say("not confirmed; nothing destroyed")
        return 1
    state, tofu_for = _main_with_state(settings, secrets)
    with operator_lock(), private_tmpdir() as tmp:
        tofu = tofu_for()
        init_main(tofu, state, settings, tmp)
        plan = tmp / "destroy.tfplan"
        tofu("plan", "-destroy", "-input=false", f"-out={plan}")
        tofu("apply", "-input=false", str(plan))
    say("destroyed; cloud/oci/state (vault, buckets, budget, quota) is kept")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hpldn.backend.cloud.oci", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, help=f"settings file (default: ${CONFIG_ENV} or {DEFAULT_CONFIG})")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="pre-flight checks; changes nothing")
    state = commands.add_parser("state", help="apply cloud/oci/state as the tenancy administrator (once)")
    state.add_argument("--yes", action="store_true", help="apply without asking")
    up = commands.add_parser("up", help="provision, bootstrap, push secrets, deploy and smoke-test")
    up.add_argument("--dump", type=Path, help="a signed serving dump to restore")
    up.add_argument("--model", type=Path, help="the model bundle (default: oci.toml site.model, else deploy.sh's)")
    up.add_argument("--skip-deploy", action="store_true", help="stop after the secrets (no app, data or smoke test)")
    up.add_argument("--renew-certs", action="store_true", help="re-issue the origin key and certificate")
    up.add_argument("--rotate-guard-token", action="store_true", help="issue the guard a new Cloudflare token")
    bootstrap = commands.add_parser("bootstrap", help="up without tofu apply: resume on the existing instance")
    bootstrap.add_argument("--dump", type=Path, help="also deploy, restoring this signed serving dump")
    bootstrap.add_argument("--model", type=Path, help="also deploy this model bundle (default: oci.toml site.model)")
    deploy = commands.add_parser("deploy", help="deploy the app, frontend, model and (optionally) data")
    deploy.add_argument("--export", action="store_true", help="export_serving_db --sign first")
    deploy.add_argument("--dump", type=Path)
    deploy.add_argument("--model", type=Path)
    for flag in ("--skip-app", "--skip-frontend", "--skip-model", "--allow-dirty"):
        deploy.add_argument(flag, action="store_true")
    commands.add_parser("close-ssh", help="remove break-glass SSH from the NSG and the host")
    commands.add_parser("status", help="drift, instance, unit exposure and the leak scan")
    commands.add_parser("destroy", help="destroy main/ (asks for the domain)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        refuse_logging_env()
        settings = Settings.load(args.config)
        handler = {
            "check": cmd_check,
            "state": cmd_state,
            "up": cmd_up,
            "bootstrap": cmd_bootstrap,
            "deploy": cmd_deploy,
            "close-ssh": cmd_close_ssh,
            "status": cmd_status,
            "destroy": cmd_destroy,
        }[args.command]
        return handler(settings, args)
    except ProvisionError as exc:
        say(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        say("interrupted")
        return 130

