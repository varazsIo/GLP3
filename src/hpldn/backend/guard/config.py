"""
The guard rails' one configuration file, ``guard.yaml``.

The file is looked for at ``$HPLDN_GUARD_CONFIG``, then
``/etc/hpldn/guard.yaml`` (where ``bootstrap.sh`` installs it), then the
repository copy at ``src/hpldn/backend/cloud/vps/guard.yaml`` (found through
``HPLDN_DIR_FP`` or relative to this package). A ``guard.local.yaml`` beside
the file found is deep-merged on top: host-only overrides that a deploy never
overwrites. Without any file the model's defaults, which equal the repository
copy, apply.

Read once per process (`load_guard_config`); a bad value raises at startup
rather than surfacing as a wrong limit later.
"""

import ipaddress
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

CONFIG_ENV = "HPLDN_GUARD_CONFIG"
SYSTEM_CONFIG = Path("/etc/hpldn/guard.yaml")
REPO_RELATIVE = Path("src/hpldn/backend/cloud/vps/guard.yaml")
PACKAGE_COPY = Path(__file__).resolve().parents[1] / "cloud" / "vps" / "guard.yaml"
LOCAL_NAME = "guard.local.yaml"

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any) -> float:
    """
    Return a duration in seconds from a number or a string such as ``10m``.

    Raises
    ------
    ValueError
        For anything else, or a duration that is not positive.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _DURATION.match(str(value))
        if not match:
            raise ValueError(f"not a duration: {value!r} (use e.g. 30, 30s, 10m, 24h, 7d)")
        seconds = float(match.group(1)) * _UNITS[match.group(2)]
    if seconds <= 0:
        raise ValueError(f"a duration must be positive, not {value!r}")
    return seconds


Duration = Annotated[float, BeforeValidator(parse_duration)]

# The last step of ``bans.durations`` may be this: a ban that never lifts.
PERMANENT = "permanent"


def parse_ban_duration(value: Any) -> float:
    """Return a ban's length in seconds, or ``math.inf`` for ``permanent``; see `parse_duration`."""
    if isinstance(value, str) and value.strip().lower() == PERMANENT:
        return math.inf
    return parse_duration(value)


BanDuration = Annotated[float, BeforeValidator(parse_ban_duration)]


class _Model(BaseModel):
    """Unknown keys are an error, so a misspelt setting is not silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientLimit(_Model):
    """One `ClientLimiter`: a token bucket and an in-flight cap per client."""

    rate_per_s: float = Field(gt=0)
    burst: int = Field(ge=1)
    max_concurrent: int = Field(ge=1)


class AppLimits(_Model):
    """The API's own per-worker limits (``api/ratelimit.py``)."""

    search: ClientLimit = ClientLimit(rate_per_s=1.0, burst=15, max_concurrent=2)
    price: ClientLimit = ClientLimit(rate_per_s=0.5, burst=6, max_concurrent=2)
    form: ClientLimit = ClientLimit(rate_per_s=0.5, burst=10, max_concurrent=4)
    session: ClientLimit = ClientLimit(rate_per_s=0.1, burst=5, max_concurrent=2)
    read: ClientLimit = ClientLimit(rate_per_s=0.2, burst=10, max_concurrent=4)
    price_cap: Literal["auto"] | Annotated[int, Field(ge=1)] = "auto"

    def price_cap_limit(self) -> int:
        """The model-pricing cap as a number: one per core, at least 2, for ``auto``."""
        if self.price_cap == "auto":
            return max(2, os.cpu_count() or 2)
        return int(self.price_cap)


_RATE = re.compile(r"^\d+r/[sm]$")
_SIZE = re.compile(r"^\d+[km]$")


class NginxZone(_Model):
    """A ``limit_req_zone``."""

    rate: str
    size: str = "10m"
    burst: int | None = Field(default=None, ge=0)

    @field_validator("rate")
    @classmethod
    def _rate(cls, value: str) -> str:
        if not _RATE.match(value):
            raise ValueError(f"nginx rate must look like 60r/m or 10r/s, not {value!r}")
        return value

    @field_validator("size")
    @classmethod
    def _size(cls, value: str) -> str:
        if not _SIZE.match(value):
            raise ValueError(f"nginx zone size must look like 10m, not {value!r}")
        return value


class NginxRoute(_Model):
    """The ``limit_req`` of one nginx location."""

    location: str
    zone: str
    burst: int = Field(ge=0)
    search_conn: bool = False


class NginxTimeouts(_Model):
    """Server-level timeouts against slow clients."""

    client_header: Duration = 10
    client_body: Duration = 10
    send: Duration = 10
    keepalive: Duration = 30


def _default_zones() -> dict[str, NginxZone]:
    return {
        "api": NginxZone(rate="30r/m"),
        "page": NginxZone(rate="20r/m"),
        "search": NginxZone(rate="60r/m"),
        "session": NginxZone(rate="10r/m"),
        "site": NginxZone(rate="60r/m"),
        "static": NginxZone(rate="5r/s"),
        "crawler": NginxZone(rate="5r/s", burst=20),
    }


def _default_routes() -> dict[str, NginxRoute]:
    api = {"zone": "api", "burst": 15}
    page = {"zone": "page", "burst": 12}
    site = {"zone": "site", "burst": 30}
    return {
        "areas": NginxRoute(location="= /api/areas", **page),
        "area": NginxRoute(location="~ ^/api/areas/[A-Za-z0-9]+(/comparables)?$", **api),
        "estimate": NginxRoute(location="= /api/estimate", **api),
        "address_search": NginxRoute(location="= /api/addresses/search", zone="search", burst=20, search_conn=True),
        "addresses": NginxRoute(location="= /api/addresses", **api),
        "price": NginxRoute(location="= /api/price", **api),
        "nearby": NginxRoute(location="= /api/nearby", **api),
        "schools": NginxRoute(location="= /api/schools", **api),
        "inflation": NginxRoute(location="= /api/inflation", **api),
        "session": NginxRoute(location="= /api/session", zone="session", burst=5),
        "session_config": NginxRoute(location="= /api/session/config", **page),
        "site": NginxRoute(location="/", **site),
        "index": NginxRoute(location="= /index.html", **site),
        "static": NginxRoute(location="/static/", zone="static", burst=60),
    }


class NginxLimits(_Model):
    """nginx's zones, per-location limits and timeouts (``render_guard_nginx.py``)."""

    zones: dict[str, NginxZone] = Field(default_factory=_default_zones)
    routes: dict[str, NginxRoute] = Field(default_factory=_default_routes)
    conn_per_client: int = Field(default=20, ge=1)
    search_conn_per_client: int = Field(default=4, ge=1)
    timeouts: NginxTimeouts = NginxTimeouts()

    @model_validator(mode="after")
    def _check(self):
        if "crawler" not in self.zones:
            raise ValueError("rate_limits.nginx.zones needs a 'crawler' zone")
        for name, route in self.routes.items():
            if route.zone not in self.zones or route.zone == "crawler":
                raise ValueError(f"rate_limits.nginx.routes.{name}: unknown zone {route.zone!r}")
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise ValueError(f"rate_limits.nginx.routes: {name!r} is not a usable file name")
        return self


class RateLimits(_Model):
    app: AppLimits = AppLimits()
    nginx: NginxLimits = NginxLimits()


class BanRule(_Model):
    """
    A rule: `count` matching requests from one client within `window` is a strike.

    A request matches when every condition given matches: its status, the
    API's ``X-HPLDN-Reject`` reason, or one of the `paths` regexes on its URI.
    `min_level` starts the ban at that step of ``bans.durations``.

    A `while_banned` rule counts only the requests a client keeps sending
    while it is banned (and every other rule counts only the requests of
    clients that are not): its strike moves the ban on to the next step at
    once, so a client that ignores its ban climbs to the blacklist.
    """

    count: int = Field(ge=1)
    window: Duration
    status: int | None = None
    reject: str | None = None
    paths: tuple[str, ...] = ()
    min_level: int = Field(default=0, ge=0)
    while_banned: bool = False

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value


def _default_rules() -> dict[str, BanRule]:
    return {
        "throttled": BanRule(count=20, window=60, status=429),
        "flood": BanRule(count=300, window=60),
        "evading": BanRule(count=60, window=60, while_banned=True),
        "no_pass": BanRule(count=40, window=300, reject="captcha"),
        "scanner": BanRule(
            count=5,
            window=600,
            paths=("wp-", r"\.env", r"\.git", "phpmyadmin", "xmlrpc", r"\.php$", "cgi-bin"),
            min_level=2,
        ),
    }


def _cidr(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


class Bans(_Model):
    rules: dict[str, BanRule] = Field(default_factory=_default_rules)
    durations: tuple[BanDuration, ...] = (600, 3600, 86400, 604800, math.inf)
    strike_memory: Duration = 30 * 86400
    ipv6_prefix: int = Field(default=64, ge=32, le=128)
    max_bans: int = Field(default=5000, ge=1)
    allowlist_cidrs: tuple[str, ...] = ()

    @field_validator("durations")
    @classmethod
    def _durations(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value:
            raise ValueError("bans.durations needs at least one duration")
        if any(math.isinf(step) for step in value[:-1]):
            raise ValueError(f"only the last of bans.durations may be {PERMANENT!r}")
        return value

    @field_validator("allowlist_cidrs")
    @classmethod
    def _allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_cidr(item) for item in value)

    @model_validator(mode="after")
    def _check(self):
        for name, rule in self.rules.items():
            if rule.min_level >= len(self.durations):
                raise ValueError(f"bans.rules.{name}.min_level is past the last of bans.durations")
        return self


class Crawlers(_Model):
    range_urls: tuple[str, ...] = (
        "https://developers.google.com/static/crawling/ipranges/common-crawlers.json",
        "https://developers.google.com/static/crawling/ipranges/special-crawlers.json",
        "https://developers.google.com/static/crawling/ipranges/user-triggered-fetchers.json",
        "https://developers.google.com/static/crawling/ipranges/user-triggered-fetchers-google.json",
        "https://www.bing.com/toolkit/bingbot.json",
    )
    rdns_suffixes: tuple[str, ...] = ("googlebot.com", "google.com", "googleusercontent.com", "search.msn.com")
    user_agent_pattern: str = (
        "(?i)googlebot|bingbot|google-inspectiontool|googleother|adsbot-google"
        "|mediapartners-google|storebot-google|feedfetcher-google"
    )
    refresh_interval: Duration = 86400

    @field_validator("user_agent_pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        re.compile(value)
        return value


# /addresses/search is not gated: a first search must not wait for reCAPTCHA (guard.yaml).
DEFAULT_GATED_ROUTES = ("/addresses", "/price", "/estimate", "/nearby", "/schools")


class Captcha(_Model):
    enabled: bool = True
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    action: str = Field(default="session", pattern=r"^[A-Za-z0-9_/]+$")
    allowed_hostnames: tuple[str, ...] = ()
    pass_ttl: Duration = 1800
    fail_open: bool = True
    fail_open_ttl: Duration = 300
    gated_routes: tuple[str, ...] = DEFAULT_GATED_ROUTES


class UserAgents(_Model):
    deny_on_api: tuple[str, ...] = (
        "",
        "sqlmap",
        "nikto",
        "masscan",
        "zgrab",
        "python-requests",
        "curl",
        "Go-http-client",
        "scrapy",
    )


class Cloudflare(_Model):
    enabled: bool = False
    account_id: str = Field(default="", pattern=r"^[0-9a-f]{0,32}$")
    list_name: str = Field(default="hpldn_banned", pattern=r"^[a-z0-9_]{1,50}$")
    sync_debounce: Duration = 15
    dry_run: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.enabled and not self.account_id:
            raise ValueError("cloudflare.enabled needs cloudflare.account_id")
        return self


class GuardConfig(_Model):
    """The whole of ``guard.yaml``."""

    rate_limits: RateLimits = RateLimits()
    bans: Bans = Bans()
    crawlers: Crawlers = Crawlers()
    captcha: Captcha = Captcha()
    user_agents: UserAgents = UserAgents()
    cloudflare: Cloudflare = Cloudflare()
    # Where it was read from, for logs; not a setting.
    source: tuple[str, ...] = ()


def deep_merge(base: dict, override: dict) -> dict:
    """Return `base` with `override` merged in: mappings recursively, anything else replaced."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_path() -> Path | None:
    """Return the ``guard.yaml`` to read, or None when there is none."""
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise RuntimeError(f"{CONFIG_ENV}={explicit} is not a file")
        return path
    candidates = [SYSTEM_CONFIG]
    root = os.environ.get("HPLDN_DIR_FP")
    if root:
        candidates.append(Path(root) / REPO_RELATIVE)
    candidates.append(PACKAGE_COPY)
    return next((path for path in candidates if path.is_file()), None)


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must hold a mapping at the top level")
    return data


def read_guard_config(path: Path | None) -> GuardConfig:
    """
    Read `path` and the ``guard.local.yaml`` beside it into a `GuardConfig`.

    Raises
    ------
    RuntimeError
        Naming the file, when a value is invalid.
    """
    data: dict = {}
    sources: list[str] = []
    if path is not None:
        data = _read(path)
        sources.append(str(path))
        local = path.with_name(LOCAL_NAME)
        if local.is_file():
            data = deep_merge(data, _read(local))
            sources.append(str(local))
    try:
        return GuardConfig.model_validate({**data, "source": tuple(sources)})
    except ValueError as exc:
        raise RuntimeError(f"Invalid guard configuration in {' + '.join(sources) or 'defaults'}: {exc}") from exc


@lru_cache(maxsize=1)
def load_guard_config() -> GuardConfig:
    """Return this process's guard configuration, read once (see the module docstring)."""
    return read_guard_config(config_path())
