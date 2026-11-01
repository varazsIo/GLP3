"""``cloud/local/fake_google``: the loopback siteverify the end-to-end tests point the API at."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest

from hpldn.backend.cloud.local import fake_google
from hpldn.backend.guard.captcha import Verdict, judge
from hpldn.backend.guard.config import Captcha

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("token", "status", "verdict"),
    [
        ("e2e-ok", 200, Verdict.OK),
        ("e2e-low", 200, Verdict.LOW_SCORE),
        ("e2e-browser-error", 200, Verdict.BROWSER_ERROR),
        ("anything", 200, Verdict.LOW_SCORE),
    ],
)
def test_each_token_gets_the_answer_the_real_gate_turns_into_its_verdict(token, status, verdict):
    code, body = fake_google.answer(token, now=NOW)
    assert code == status
    assert judge(body, Captcha(), NOW.timestamp()).verdict is verdict


def test_e2e_down_is_google_unreachable():
    assert fake_google.answer("e2e-down") == (503, None)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer((fake_google.HOST, 0), fake_google.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://{fake_google.HOST}:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(url, token):
    data = urllib.parse.urlencode({"secret": "s", "response": token, "remoteip": "127.0.0.1"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_the_server_answers_siteverify_over_http(server):
    status, body = _post(server + fake_google.PATH, "e2e-ok")
    assert status == 200 and json.loads(body)["score"] == 0.9
    assert _post(server + fake_google.PATH, "e2e-down")[0] == 503
    assert _post(server + "/elsewhere", "e2e-ok")[0] == 404
