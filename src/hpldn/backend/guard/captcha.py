"""
reCAPTCHA v3 token checks and the signed "human pass" the API hands out for one.

A visitor's page runs reCAPTCHA once and trades the token for a pass with
``POST /session``; the gated routes then want the pass in ``X-HPLDN-Pass``.
One Google check per visit, not per request, keeps the site inside
reCAPTCHA's free 10,000 assessments a month.

A pass is ``<payload>.<signature>``, both base64url without padding: the
payload is the JSON ``{"v": 1, "exp": <unix seconds>, "iph": <hash>}``, where
``iph`` is a truncated SHA-256 of the client's address (its /64 for IPv6, which
changes host part under privacy extensions), and the signature is the payload's
HMAC-SHA256 under ``HPLDN_PASS_SECRET``. Every worker holds the same secret, so
a pass is good on any of them. It cannot be moved to another address, and it
holds nothing personal.
"""

import base64
import enum
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from hpldn.backend.guard.config import Captcha

SITEVERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
SITEVERIFY_TIMEOUT_S = 5.0
# A token older than this was not made for this page load.
MAX_TOKEN_AGE_S = 120
PASS_VERSION = 1
# Google's error codes that mean our own secret is wrong, not the token: the
# check fails CLOSED (no fail-open pass), because a pass handed out while the
# secret is broken would be a pass for anyone. Every other error code
# (bad-request, invalid-input-response, timeout-or-duplicate, ...) is about
# what the client sent, and is refused like a bot score.
_CONFIG_ERRORS = frozenset({"missing-input-secret", "invalid-input-secret"})

log = logging.getLogger("uvicorn.error")


class Verdict(enum.StrEnum):
    """What a token check concluded."""

    OK = "ok"
    # A bot score, a bad, reused or stale token, or one made for another action or site.
    LOW_SCORE = "low_score"
    # The page's ``execute`` failed, so it sent an error token: a network hiccup (worth one
    # more ``execute``) or a site key whose domain list lacks the page's host.
    BROWSER_ERROR = "browser_error"
    # Google could not be asked (network error, timeout, 5xx): may fail open.
    UNAVAILABLE = "unavailable"
    # Google refused our secret: a configuration error, never failed open.
    CONFIG_ERROR = "config_error"


@dataclass(frozen=True)
class Verification:
    """A token check's verdict, the score Google gave and why, for the log."""

    verdict: Verdict
    reason: str
    score: float | None = None


def _challenge_age(challenge_ts: object, now: float) -> float | None:
    """Seconds since Google's ``challenge_ts`` (ISO 8601), or None when unreadable."""
    if not isinstance(challenge_ts, str):
        return None
    try:
        issued = datetime.fromisoformat(challenge_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if issued.tzinfo is None:
        return None
    return now - issued.timestamp()


def judge(body: object, config: Captcha, now: float) -> Verification:
    """
    Decide on Google's siteverify answer `body`.

    Checks ``success``, the action, the hostname (when ``allowed_hostnames``
    names any), that the challenge is under `MAX_TOKEN_AGE_S` old, and the score.
    """
    if not isinstance(body, dict):
        return Verification(
            Verdict.UNAVAILABLE, "siteverify answered something other than an object"
        )
    if not body.get("success"):
        codes = body.get("error-codes") or []
        if any(code in _CONFIG_ERRORS for code in codes):
            return Verification(
                Verdict.CONFIG_ERROR, f"siteverify refused our secret: {codes}"
            )
        if "browser-error" in codes:
            return Verification(
                Verdict.BROWSER_ERROR,
                f"token rejected: {codes} (grecaptcha.execute failed in the browser: a network "
                "failure, or the site key's domains lack the page's host, localhost included)",
            )
        return Verification(Verdict.LOW_SCORE, f"token rejected: {codes}")
    score = body.get("score")
    score = float(score) if isinstance(score, (int, float)) else None
    if body.get("action") != config.action:
        return Verification(Verdict.LOW_SCORE, f"action {body.get('action')!r}", score)
    hostname = body.get("hostname")
    if config.allowed_hostnames and hostname not in config.allowed_hostnames:
        return Verification(Verdict.LOW_SCORE, f"hostname {hostname!r}", score)
    age = _challenge_age(body.get("challenge_ts"), now)
    if age is None or age > MAX_TOKEN_AGE_S or age < -MAX_TOKEN_AGE_S:
        return Verification(
            Verdict.LOW_SCORE, f"challenge_ts {body.get('challenge_ts')!r}", score
        )
    if score is None or score < config.min_score:
        return Verification(Verdict.LOW_SCORE, f"score {score}", score)
    return Verification(Verdict.OK, "ok", score)


async def verify_token(
    token: str,
    ip: str,
    *,
    secret: str,
    config: Captcha,
    url: str = SITEVERIFY_URL,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> Verification:
    """
    Ask Google's siteverify about a reCAPTCHA v3 `token` made by the client at `ip`.

    Parameters
    ----------
    token : str
        What ``grecaptcha.execute`` gave the page.
    ip : str
        The client's address, sent as ``remoteip``.
    secret : str
        The reCAPTCHA secret key.
    config : Captcha
        ``guard.yaml``'s ``captcha`` section.
    url : str, optional
        The siteverify endpoint; a fake Google on the loopback for local runs
        (``ApiSettings.recaptcha_verify_url``).
    transport : httpx.AsyncBaseTransport, optional
        For tests (``httpx.MockTransport``).
    clock : callable, optional
        Wall clock in Unix seconds, for tests.

    Returns
    -------
    Verification
        ``UNAVAILABLE`` when Google cannot be reached in `SITEVERIFY_TIMEOUT_S`,
        answers an error, or refuses our secret; never raises for those.
    """
    try:
        async with httpx.AsyncClient(
            timeout=SITEVERIFY_TIMEOUT_S, transport=transport
        ) as client:
            response = await client.post(
                url,
                data={"secret": secret, "response": token, "remoteip": ip},
            )
        if response.status_code != 200:
            return Verification(
                Verdict.UNAVAILABLE, f"siteverify answered {response.status_code}"
            )
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return Verification(
            Verdict.UNAVAILABLE, f"siteverify failed: {type(exc).__name__}"
        )
    return judge(body, config, clock())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def address_hash(ip: str, ipv6_prefix: int = 64) -> str:
    """
    Return the truncated SHA-256 a pass is bound to: of `ip`, or of its /`ipv6_prefix` for IPv6.

    Anything that is not an address (``unknown``) is hashed as given.
    """
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        key = ip.strip()
    else:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is None:
            key = str(ipaddress.ip_network(f"{address}/{ipv6_prefix}", strict=False))
        else:
            # An IPv4-mapped IPv6 address is bound as its IPv4 form.
            key = str(getattr(address, "ipv4_mapped", None) or address)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _sign(payload: str, secret: str) -> str:
    return _b64(
        hmac.new(
            secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).digest()
    )


def issue_pass(
    ip: str, ttl: float, *, secret: str, clock: Callable[[], float] = time.time
) -> str:
    """Return a pass for the client at `ip`, good for `ttl` seconds."""
    body = {"v": PASS_VERSION, "exp": int(clock() + ttl), "iph": address_hash(ip)}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign(payload, secret)}"


def check_pass(
    token: str | None, ip: str, *, secret: str, clock: Callable[[], float] = time.time
) -> bool:
    """
    Return True when `token` is a pass this server signed, unexpired, for the client at `ip`.

    The signature is compared in constant time before the payload is read, so
    a forged payload is never parsed.
    """
    if not token or not secret or len(token) > 512:
        return False
    payload, _, signature = token.partition(".")
    if not payload or not signature:
        return False
    try:
        expected = _sign(payload, secret)
    except UnicodeEncodeError:
        return False
    if not secrets.compare_digest(
        signature.encode("ascii", "replace"), expected.encode("ascii")
    ):
        return False
    try:
        body = json.loads(_unb64(payload))
    except ValueError:
        return False
    if not isinstance(body, dict) or body.get("v") != PASS_VERSION:
        return False
    exp = body.get("exp")
    if not isinstance(exp, int) or exp <= clock():
        return False
    iph = body.get("iph")
    return isinstance(iph, str) and secrets.compare_digest(iph, address_hash(ip))
