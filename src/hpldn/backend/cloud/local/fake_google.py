"""
A fake reCAPTCHA siteverify on the loopback, for the end-to-end tests.

    python -m hpldn.backend.cloud.local.fake_google [--port 18999]

The API is pointed at it with ``HPLDN_RECAPTCHA_VERIFY_URL`` (loopback only,
`hpldn.backend.api.config`); `up --fake-google` does both. Everything else in
the gate (the verdict, the pass, its address binding, the limits) runs for
real. The answer depends only on the token the page sent:

    e2e-ok             success, score 0.9, action session, hostname localhost
    e2e-low            success, score 0.1 (a bot)
    e2e-browser-error  success false, error-codes [browser-error]
    e2e-down           HTTP 503 (Google unreachable: the fail-open pass)
    anything else      success false, error-codes [invalid-input-response]
"""

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HOST = "127.0.0.1"
PORT = 18999
PATH = "/siteverify"

log = logging.getLogger("hpldn.fake_google")


def answer(token: str, now: datetime | None = None) -> tuple[int, dict | None]:
    """Return the HTTP status and JSON body siteverify would give for `token`."""
    now = now or datetime.now(UTC)
    passed = {
        "success": True,
        "challenge_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": "localhost",
        "action": "session",
    }
    if token == "e2e-ok":
        return 200, {**passed, "score": 0.9}
    if token == "e2e-low":
        return 200, {**passed, "score": 0.1}
    if token == "e2e-browser-error":
        return 200, {"success": False, "error-codes": ["browser-error"]}
    if token == "e2e-down":
        return 503, None
    return 200, {"success": False, "error-codes": ["invalid-input-response"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-google"

    def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
        if self.path.split("?", 1)[0] != PATH:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        token = (form.get("response") or [""])[0]
        status, body = answer(token)
        payload = json.dumps(body).encode("utf-8") if body is not None else b"Service Unavailable"
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if body is not None else "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        log.info("siteverify token=%s -> %s", token[:24], status)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 (the base class's name)
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    log.info("fake Google siteverify on http://%s:%d%s", HOST, args.port, PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
