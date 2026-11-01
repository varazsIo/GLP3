"""
Runtime configuration for the HPLDN read API.

Deployed, everything comes from the environment the systemd unit sets
(``/etc/hpldn/api.env`` on the serving host). Locally (``HPLDN_DIR_FP`` set),
``develop.env`` is loaded, with values already in the environment winning. See ``api/README.md`` for the
``POSTGRES_API_*`` variables.

Secrets (``HPLDN_API_KEY``, ``RECAPTCHA_SECRET_KEY``, ``HPLDN_PASS_SECRET``
and, off the host, ``POSTGRES_API_PASSWORD``) are read with
`hpldn.backend.common.secrets.read_secret`: on the host from the unit's
systemd credentials (``api_key``, ``recaptcha_secret_key``, ``pass_secret``),
never from the environment; off it from the environment. On the host the
database is reached over its unix socket with peer authentication, so there
is no database password at all: an empty one is accepted whenever
``POSTGRES_API_HOST`` is a socket directory (starts with ``/``).

The reCAPTCHA keys and the pass-signing secret (``RECAPTCHA_SITE_KEY``,
``RECAPTCHA_SECRET_KEY``, ``HPLDN_PASS_SECRET``) are required while
``guard.yaml``'s ``captcha.enabled`` is on, unless ``HPLDN_CAPTCHA_DISABLED=1``
turns the gate off (the pytest suites only). ``HPLDN_RECAPTCHA_VERIFY_URL``
points the token check at a fake Google on the loopback (the local stack's
``--fake-google``, for Playwright); any other host is refused at startup.
"""

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import URL

from hpldn.backend.common.secrets import read_secret
from hpldn.backend.guard.captcha import SITEVERIFY_URL

ENV_FILE = "develop.env"

CAPTCHA_DISABLED_ENV = "HPLDN_CAPTCHA_DISABLED"

VERIFY_URL_ENV = "HPLDN_RECAPTCHA_VERIFY_URL"

# Serve /docs, /redoc and /openapi.json (api.main): the local stack and the
# Playwright setup only, never the serving host.
DOCS_ENV = "HPLDN_API_DOCS"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

log = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class ApiSettings:
    """
    Connection and authentication settings for one worker process.

    Attributes
    ----------
    db_host, db_port, db_name, db_user, db_password : str or int
        PostgreSQL connection details.
    api_key : str
        The expected ``X-API-Key`` value.
    db_sslmode : str
        libpq ``sslmode``. ``"require"`` over TCP; ``"prefer"`` or
        ``"disable"`` for a local server without a certificate or a unix socket.
    captcha_enabled : bool
        Whether the gated routes want a reCAPTCHA pass: ``guard.yaml``'s
        ``captcha.enabled``, unless ``HPLDN_CAPTCHA_DISABLED=1``.
    recaptcha_site_key, recaptcha_secret_key : str
        The reCAPTCHA v3 key pair. The site key is public; the page fetches it
        from ``GET /session/config``.
    pass_secret : str
        Signs the passes ``POST /session`` hands out; the same in every worker.
    recaptcha_verify_url : str
        Where reCAPTCHA tokens are checked: Google's siteverify, unless
        ``HPLDN_RECAPTCHA_VERIFY_URL`` names a fake Google on the loopback.
    """

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    api_key: str = field(repr=False)
    db_sslmode: str = "require"
    captcha_enabled: bool = False
    recaptcha_site_key: str = ""
    recaptcha_secret_key: str = field(default="", repr=False)
    pass_secret: str = field(default="", repr=False)
    recaptcha_verify_url: str = SITEVERIFY_URL

    @property
    def database_url(self) -> URL:
        """Async SQLAlchemy URL, built with ``URL.create`` so passwords are escaped."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.db_user,
            # None, not "": peer authentication over the socket sends no password.
            password=self.db_password or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"sslmode": self.db_sslmode},
        )

    @property
    def peer_auth(self) -> bool:
        """True when the database is reached over a unix socket directory (peer authentication, no password)."""
        return self.db_host.startswith("/")

    @property
    def safe_target(self) -> str:
        """``user@host:port/db`` for logs, never including the password."""
        return f"{self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    """
    Resolve the API settings once per worker process.

    Returns
    -------
    ApiSettings
        The resolved settings.

    Raises
    ------
    RuntimeError
        If ``POSTGRES_API_DATABASE`` is missing, ``POSTGRES_API_PASSWORD`` is
        missing for a TCP host, or the captcha is on and a reCAPTCHA key or
        ``HPLDN_PASS_SECRET`` is missing.
    SecretError
        In production, when a secret is in the environment instead of the
        unit's credentials (`hpldn.backend.common.secrets.read_secret`).

    Notes
    -----
    Read at startup, not per request, so a rotated password or API key is
    only picked up when the workers restart.
    """
    if os.environ.get("HPLDN_DIR_FP"):
        from hpldn.backend.common.utils import load_env_vars

        load_env_vars()

    settings = ApiSettings(
        db_host=os.environ.get("POSTGRES_API_HOST", "127.0.0.1"),
        db_port=int(os.environ.get("POSTGRES_API_PORT", "6432")),
        db_name=os.environ.get("POSTGRES_API_DATABASE", ""),
        db_user=os.environ.get("POSTGRES_API_USERNAME", "hpldn_api"),
        db_password=read_secret("postgres_api_password", "POSTGRES_API_PASSWORD"),
        api_key=read_secret("api_key", "HPLDN_API_KEY"),
        db_sslmode=os.environ.get("POSTGRES_API_SSLMODE", "require"),
        captcha_enabled=_captcha_enabled(),
        recaptcha_site_key=os.environ.get("RECAPTCHA_SITE_KEY", ""),
        recaptcha_secret_key=read_secret("recaptcha_secret_key", "RECAPTCHA_SECRET_KEY"),
        pass_secret=read_secret("pass_secret", "HPLDN_PASS_SECRET"),
        recaptcha_verify_url=_recaptcha_verify_url(),
    )

    missing = []
    if not settings.db_password and not settings.peer_auth:
        missing.append("POSTGRES_API_PASSWORD")
    if not settings.db_name:
        missing.append("POSTGRES_API_DATABASE")
    if missing:
        raise RuntimeError(
            f"Read API database settings missing: {', '.join(missing)}. Set "
            f"them in the environment or in {ENV_FILE} (with HPLDN_DIR_FP "
            "pointing at the repository root); "
            "src/hpldn/backend/db/sql/db_enable_api_login.sh prints the POSTGRES_API_* "
            "lines for a local server."
        )
    if settings.captcha_enabled:
        missing = [
            name
            for name, value in (
                ("RECAPTCHA_SITE_KEY", settings.recaptcha_site_key),
                ("RECAPTCHA_SECRET_KEY", settings.recaptcha_secret_key),
                ("HPLDN_PASS_SECRET", settings.pass_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"The reCAPTCHA gate is on (guard.yaml captcha.enabled) but "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. "
                "On the host run bootstrap.sh --set-recaptcha-keys (the pass secret "
                f"is generated by bootstrap.sh); locally set them in {ENV_FILE}, or "
                f"{CAPTCHA_DISABLED_ENV}=1 to run without the gate."
            )
        if len(settings.pass_secret) < 32:
            raise RuntimeError("HPLDN_PASS_SECRET must be at least 32 characters (openssl rand -hex 32)")
    return settings


def _captcha_enabled() -> bool:
    """``guard.yaml``'s ``captcha.enabled``, unless ``HPLDN_CAPTCHA_DISABLED=1`` (logged loudly)."""
    from hpldn.backend.guard.config import load_guard_config

    if os.environ.get(CAPTCHA_DISABLED_ENV) == "1":
        log.warning(
            "%s=1: the reCAPTCHA gate is OFF and every route is open to scripts. "
            "Never set this on the public host.",
            CAPTCHA_DISABLED_ENV,
        )
        return False
    return load_guard_config().captcha.enabled


def _recaptcha_verify_url() -> str:
    """
    Google's siteverify, or ``HPLDN_RECAPTCHA_VERIFY_URL`` (a fake Google on the loopback; logged loudly).

    Raises
    ------
    RuntimeError
        When the override's host is not ``127.0.0.1``, ``::1`` or ``localhost``.
    """
    url = os.environ.get(VERIFY_URL_ENV, "").strip()
    if not url:
        return SITEVERIFY_URL
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"{VERIFY_URL_ENV}={url!r}: only a fake Google on the loopback "
            "(127.0.0.1, ::1 or localhost) may stand in for reCAPTCHA's siteverify."
        )
    log.warning(
        "%s=%s: reCAPTCHA tokens are checked by a FAKE Google, not Google. "
        "Never set this on the public host.",
        VERIFY_URL_ENV,
        url,
    )
    return url
