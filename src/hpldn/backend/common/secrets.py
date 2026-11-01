"""
Read a secret from systemd's credentials, never from the environment in production.

On the serving host every secret is a ``systemd-creds`` file
(``/etc/credstore.encrypted/hpldn.<name>``, encrypted with the host key) that
the unit loads with ``LoadCredentialEncrypted=<name>:...``. systemd decrypts
it into ``$CREDENTIALS_DIRECTORY/<name>`` (``/run/credentials/<unit>/``,
0400, visible to that unit only), so the value is never in the process's
environment, ``/proc/<pid>/environ``, ``systemctl show`` or a plaintext file
on disk.

`read_secret` reads that file. Off the host (``HPLDN_ENV`` is not
``production``: the laptop, the local stack, the tests) it falls back to the
environment variable, which ``develop.env`` supplies. In production it fails
closed: the environment variable is never read, and one that is set anyway
is an error, because it means a secret was put where every child process and
``/proc`` can see it.

Every value read is remembered (`loaded_secrets`) so `hpldn.backend.common.redact`
can mask it in any log line.
"""

import os
import re
import threading
from pathlib import Path

CREDENTIALS_DIRECTORY_ENV = "CREDENTIALS_DIRECTORY"
ENVIRONMENT_ENV = "HPLDN_ENV"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# Shorter values are not masked by value (they would mask ordinary words); the
# redaction's patterns still cover the header forms.
MIN_REDACTED_LENGTH = 8

_lock = threading.Lock()
_loaded: set[str] = set()


class SecretError(RuntimeError):
    """A secret is missing, or was supplied in a way production refuses."""


def is_production() -> bool:
    """True when ``HPLDN_ENV=production`` (the serving host's units set it)."""
    return os.environ.get(ENVIRONMENT_ENV, "").strip().lower() == "production"


def register_secret(value: str) -> None:
    """Remember `value` as a secret, so the log redaction masks it."""
    if value and len(value) >= MIN_REDACTED_LENGTH:
        with _lock:
            _loaded.add(value)


def loaded_secrets() -> tuple[str, ...]:
    """Every secret value read so far, longest first (so a prefix never masks half of a longer one)."""
    with _lock:
        return tuple(sorted(_loaded, key=len, reverse=True))


def _credential(name: str) -> str | None:
    directory = os.environ.get(CREDENTIALS_DIRECTORY_ENV, "").strip()
    if not directory:
        return None
    path = Path(directory) / name
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    # systemd-creds keeps the bytes as given; a trailing newline from `echo` is not part of the secret.
    return value.rstrip("\r\n")


def read_secret(name: str, env_name: str, *, required: bool = False) -> str:
    """
    Return the secret `name`: ``$CREDENTIALS_DIRECTORY/<name>``, else (off the host) ``$<env_name>``.

    Parameters
    ----------
    name : str
        The credential's name, as in the unit's ``LoadCredentialEncrypted=<name>:``.
    env_name : str
        The environment variable used off the host (``develop.env``, the tests).
    required : bool
        Raise when the secret is missing or empty instead of returning ``""``.

    Raises
    ------
    SecretError
        In production, when `env_name` is set in the environment (a secret
        must never travel there), or when a `required` secret has no
        credential; anywhere, when a `required` secret is missing.
    """
    if not _NAME.match(name):
        raise SecretError(f"not a credential name: {name!r}")
    production = is_production()
    value = _credential(name)
    if production:
        if os.environ.get(env_name):
            raise SecretError(
                f"{env_name} is set in the environment with {ENVIRONMENT_ENV}=production. Secrets on the "
                f"host are systemd credentials only (bootstrap.sh); remove it from the unit and api.env."
            )
    elif value is None:
        value = os.environ.get(env_name)
    value = value or ""
    if required and not value:
        where = (
            f"credential {name!r} (LoadCredentialEncrypted; bootstrap.sh writes /etc/credstore.encrypted/hpldn.{name})"
            if production
            else f"{env_name} (or credential {name!r})"
        )
        raise SecretError(f"missing secret: {where}")
    register_secret(value)
    return value
