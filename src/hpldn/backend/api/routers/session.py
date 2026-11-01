"""
The page's reCAPTCHA v3 check and the pass it earns.

``GET /session/config`` tells the page whether the gate is on and which site
key to run reCAPTCHA with; ``POST /session`` trades the token for a signed
pass (`hpldn.backend.guard.captcha`) that the gated routes want in
``X-HPLDN-Pass``. Both are exposed under ``/api`` by nginx and the dev proxy.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from hpldn.backend.api import ratelimit
from hpldn.backend.api.config import ApiSettings, get_settings
from hpldn.backend.api.schemas import SessionConfig, SessionPass, SessionRequest
from hpldn.backend.guard import captcha
from hpldn.backend.guard.config import Captcha, load_guard_config

router = APIRouter(prefix="/session", tags=["session"])

log = logging.getLogger("uvicorn.error")

NOT_A_PERSON = "We couldn't confirm this is a person. Reload the page and try again."

CHECK_UNAVAILABLE = "We can't check visitors right now. Try again in a few minutes."


def get_captcha_config() -> Captcha:
    """Return ``guard.yaml``'s ``captcha`` section (FastAPI dependency)."""
    return load_guard_config().captcha


@router.get("/config", response_model=SessionConfig, dependencies=[Depends(ratelimit.read_limit)])
async def session_config(
    settings: ApiSettings = Depends(get_settings),
    config: Captcha = Depends(get_captcha_config),
) -> SessionConfig:
    """Whether the gated routes want a pass, and the reCAPTCHA site key and action to get one with."""
    return SessionConfig(
        enabled=settings.captcha_enabled,
        site_key=settings.recaptcha_site_key if settings.captcha_enabled else "",
        action=config.action,
    )


@router.post("", response_model=SessionPass)
async def open_session(
    request: Request,
    body: SessionRequest,
    settings: ApiSettings = Depends(get_settings),
    config: Captcha = Depends(get_captcha_config),
    limiter: ratelimit.ClientLimiter = Depends(ratelimit.get_session_limiter),
) -> SessionPass:
    """
    Check a reCAPTCHA v3 token with Google and hand out a pass for this client.

    403 with ``X-HPLDN-Captcha: low_score`` when Google scores the visitor as a
    bot or the token is bad, stale or for another action or site, and with
    ``browser_error`` when the page's ``grecaptcha.execute`` failed (the page
    runs it once more, as Google advises). When Google
    cannot be reached, a short pass (``captcha.fail_open_ttl``) is handed out if
    ``captcha.fail_open``, else 503. When Google refuses our own secret the
    check fails closed: 503, never a pass, and an error in the log. 404 while
    the gate is off; 429 for a client asking faster than any page would.
    """
    if not settings.captcha_enabled:
        raise HTTPException(status_code=404, detail="Visitor checks are off")
    ip = ratelimit.client_key(request)
    async with limiter.acquire(ip):
        result = await captcha.verify_token(
            body.token,
            ip,
            secret=settings.recaptcha_secret_key,
            config=config,
            url=settings.recaptcha_verify_url,
        )
    if result.verdict is captcha.Verdict.OK:
        ttl = config.pass_ttl
    elif result.verdict is captcha.Verdict.CONFIG_ERROR:
        log.error("captcha outcome=config_error reason=%s: fix the reCAPTCHA secret key (bootstrap.sh --set-recaptcha-keys)", result.reason)
        raise HTTPException(status_code=503, detail=CHECK_UNAVAILABLE, headers={"Retry-After": "60"})
    elif result.verdict is captcha.Verdict.UNAVAILABLE:
        log.warning("captcha outcome=unavailable reason=%s fail_open=%s", result.reason, config.fail_open)
        if not config.fail_open:
            raise HTTPException(status_code=503, detail=CHECK_UNAVAILABLE, headers={"Retry-After": "60"})
        ttl = config.fail_open_ttl
    else:
        log.info("captcha outcome=refused reason=%s", result.reason)
        raise HTTPException(
            status_code=403,
            detail=NOT_A_PERSON,
            headers={"X-HPLDN-Captcha": str(result.verdict), "X-HPLDN-Reject": "captcha"},
        )
    log.info("captcha outcome=%s score=%s", result.verdict, result.score)
    return SessionPass(
        pass_=captcha.issue_pass(ip, ttl, secret=settings.pass_secret),
        expires_in=int(ttl),
    )
