"""
`guard.captcha`: the siteverify check against a mocked Google, and the signed pass.

``verify_token`` is driven through ``httpx.MockTransport`` for every error
code Google documents, its success shapes, and the ways the call itself can
fail. The pass is checked for issue, expiry, tampering and address binding.
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from hpldn.backend.guard import captcha
from hpldn.backend.guard.captcha import Verdict
from hpldn.backend.guard.config import Captcha

SECRET = "s" * 40
NOW = 1_790_000_000.0
CONFIG = Captcha(min_score=0.5, action="session", allowed_hostnames=("hpldn.example",))


def _ts(seconds_ago: float = 5) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok_body(**changes) -> dict:
    body = {"success": True, "score": 0.9, "action": "session", "hostname": "hpldn.example", "challenge_ts": _ts()}
    body.update(changes)
    return body


def _verify(handler, token="tok", ip="203.0.113.7", config=CONFIG):
    seen = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    result = asyncio.run(
        captcha.verify_token(
            token, ip, secret=SECRET, config=config, transport=httpx.MockTransport(recording), clock=lambda: NOW
        )
    )
    return result, seen


def _answer(body, status=200):
    return lambda request: httpx.Response(status, json=body)


# Google's documented error codes and what each must conclude.
@pytest.mark.parametrize(
    ("code", "verdict"),
    [
        # Our own secret is wrong: fail closed (never a fail-open pass).
        ("missing-input-secret", Verdict.CONFIG_ERROR),
        ("invalid-input-secret", Verdict.CONFIG_ERROR),
        # About what the client sent: refused like a bot.
        ("bad-request", Verdict.LOW_SCORE),
        ("missing-input-response", Verdict.LOW_SCORE),
        ("invalid-input-response", Verdict.LOW_SCORE),
        ("timeout-or-duplicate", Verdict.LOW_SCORE),
        ("browser-error", Verdict.BROWSER_ERROR),
    ],
)
def test_every_google_error_code_has_its_verdict(code, verdict):
    result, _ = _verify(_answer({"success": False, "error-codes": [code]}))
    assert result.verdict is verdict
    assert code in result.reason


def test_a_config_error_wins_over_a_token_error():
    result, _ = _verify(_answer({"success": False, "error-codes": ["timeout-or-duplicate", "invalid-input-secret"]}))
    assert result.verdict is Verdict.CONFIG_ERROR


def test_a_good_token_is_ok_and_the_request_carries_secret_token_and_ip():
    result, seen = _verify(_answer(_ok_body()), token="the-token", ip="198.51.100.4")
    assert (result.verdict, result.score) == (Verdict.OK, 0.9)
    [request] = seen
    assert str(request.url) == captcha.SITEVERIFY_URL
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {"secret": SECRET, "response": "the-token", "remoteip": "198.51.100.4"}


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"score": 0.2}, "score"),
        ({"score": None}, "score"),
        ({"action": "login"}, "action"),
        ({"hostname": "evil.example"}, "hostname"),
        ({"challenge_ts": _ts(seconds_ago=captcha.MAX_TOKEN_AGE_S + 5)}, "challenge_ts"),
        ({"challenge_ts": _ts(seconds_ago=-(captcha.MAX_TOKEN_AGE_S + 5))}, "challenge_ts"),
        ({"challenge_ts": "yesterday"}, "challenge_ts"),
        ({"challenge_ts": "2026-09-01T10:00:00"}, "challenge_ts"),
    ],
)
def test_a_successful_answer_is_still_refused_when_it_is_for_the_wrong_thing(changes, fragment):
    result, _ = _verify(_answer(_ok_body(**changes)))
    assert result.verdict is Verdict.LOW_SCORE
    assert fragment in result.reason


def test_any_hostname_is_accepted_without_an_allowlist():
    result, _ = _verify(_answer(_ok_body(hostname="anything")), config=Captcha(min_score=0.5))
    assert result.verdict is Verdict.OK


@pytest.mark.parametrize(
    "handler",
    [
        _answer({"success": True}, status=500),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(200, json=["a", "list"]),
    ],
)
def test_a_broken_answer_is_unavailable(handler):
    result, _ = _verify(handler)
    assert result.verdict is Verdict.UNAVAILABLE


@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_an_unreachable_google_is_unavailable_and_never_raises(exc):
    def fail(request):
        raise exc

    result, _ = _verify(fail)
    assert result.verdict is Verdict.UNAVAILABLE
    assert type(exc).__name__ in result.reason


# The pass.


def test_a_pass_is_good_for_the_address_it_was_issued_to_until_it_expires():
    token = captcha.issue_pass("203.0.113.7", 60, secret=SECRET, clock=lambda: NOW)
    assert captcha.check_pass(token, "203.0.113.7", secret=SECRET, clock=lambda: NOW + 59)
    assert not captcha.check_pass(token, "203.0.113.7", secret=SECRET, clock=lambda: NOW + 60)
    assert not captcha.check_pass(token, "203.0.113.8", secret=SECRET, clock=lambda: NOW)


def test_a_pass_binds_an_ipv6_client_to_its_64():
    token = captcha.issue_pass("2001:db8:1:2::10", 60, secret=SECRET, clock=lambda: NOW)
    assert captcha.check_pass(token, "2001:db8:1:2:abcd::99", secret=SECRET, clock=lambda: NOW)
    assert not captcha.check_pass(token, "2001:db8:1:3::10", secret=SECRET, clock=lambda: NOW)


def test_an_ipv4_mapped_address_is_its_ipv4():
    assert captcha.address_hash("::ffff:203.0.113.7") == captcha.address_hash("203.0.113.7")
    assert captcha.address_hash(" 203.0.113.7 ") == captcha.address_hash("203.0.113.7")
    assert captcha.address_hash("unknown") != captcha.address_hash("203.0.113.7")


def test_a_pass_holds_nothing_but_its_version_expiry_and_address_hash():
    token = captcha.issue_pass("203.0.113.7", 60, secret=SECRET, clock=lambda: NOW)
    payload = json.loads(captcha._unb64(token.split(".")[0]))
    assert payload == {"v": 1, "exp": int(NOW + 60), "iph": captcha.address_hash("203.0.113.7")}
    assert "203.0.113.7" not in token


def _forge(token: str, **changes) -> str:
    payload, _, signature = token.partition(".")
    body = json.loads(captcha._unb64(payload))
    body.update(changes)
    return captcha._b64(json.dumps(body).encode()) + "." + signature


@pytest.mark.parametrize(
    "tamper",
    [
        lambda t: _forge(t, exp=int(NOW + 10**6)),
        lambda t: _forge(t, iph=captcha.address_hash("198.51.100.1")),
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),
        lambda t: t.split(".")[0],
        lambda t: "." + t.split(".")[1],
        lambda t: t + "x" * 600,
        lambda t: "",
        lambda t: None,
        lambda t: "é." + t.split(".")[1],
    ],
)
def test_a_tampered_pass_is_refused(tamper):
    token = captcha.issue_pass("203.0.113.7", 60, secret=SECRET, clock=lambda: NOW)
    assert not captcha.check_pass(tamper(token), "203.0.113.7", secret=SECRET, clock=lambda: NOW)


def test_a_pass_signed_with_another_secret_or_without_one_is_refused():
    token = captcha.issue_pass("203.0.113.7", 60, secret="other" * 10, clock=lambda: NOW)
    assert not captcha.check_pass(token, "203.0.113.7", secret=SECRET, clock=lambda: NOW)
    assert not captcha.check_pass(token, "203.0.113.7", secret="", clock=lambda: NOW)


def test_a_pass_of_another_version_or_shape_is_refused():
    for body in ({"v": 2, "exp": int(NOW + 60), "iph": captcha.address_hash("203.0.113.7")}, ["not", "a", "dict"],
                 {"v": 1, "exp": "later", "iph": captcha.address_hash("203.0.113.7")}):
        payload = captcha._b64(json.dumps(body).encode())
        token = f"{payload}.{captcha._sign(payload, SECRET)}"
        assert not captcha.check_pass(token, "203.0.113.7", secret=SECRET, clock=lambda: NOW)


def test_the_default_clock_is_wall_time():
    token = captcha.issue_pass("203.0.113.7", 60, secret=SECRET)
    assert captcha.check_pass(token, "203.0.113.7", secret=SECRET)
    assert int(json.loads(captcha._unb64(token.split(".")[0]))["exp"]) <= time.time() + 61
