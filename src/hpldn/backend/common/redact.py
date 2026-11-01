"""
Keep secrets out of the logs.

`RedactFilter` rewrites a log record before any handler formats it:

* every secret `hpldn.backend.common.secrets` has loaded is replaced by
  ``***`` wherever it appears, message and traceback alike;
* the header forms a secret travels in are masked whatever the value:
  ``Bearer <token>``, ``X-API-Key: <key>``, ``X-HPLDN-Pass: <pass>`` (and
  ``=``-separated or quoted variants);
* any run of 32 or more hex or base64url characters is masked: API keys,
  passes, tokens and signatures are all that shape, and no ordinary log
  text is.

`install` puts it on the root logger and every handler already configured,
and wraps the log record factory so records made later, by handlers added
later (gunicorn's, uvicorn's), are redacted too. The API (`api.main`), the
guard (`guard.__main__`) and the provisioning tool (`cloud.oci.provision`,
which also passes every tofu and ssh line through `redact`) install it.
"""

import logging
import re
import threading

from hpldn.backend.common.secrets import loaded_secrets

MASK = "***"

_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_HEADER = re.compile(
    r"(?i)\b(x-api-key|x-hpldn-pass|authorization|cf-access-client-secret|x-auth-key)"
    r"""(["']?\s*[:=]\s*["']?)([^\s"',;}]+)"""
)
# 32+ characters of [A-Za-z0-9_-]: hex digests and base64url tokens. Dots split
# a pass's payload from its signature, and each half is long enough on its own.
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
# A token with base64 padding or '+' and '/' (standard base64).
_LONG_B64 = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=_-])")


def redact(text: str) -> str:
    """Return `text` with every loaded secret, auth header value and long token masked."""
    if not text:
        return text
    for secret in loaded_secrets():
        if secret in text:
            text = text.replace(secret, MASK)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _LONG_TOKEN.sub(MASK, text)
    text = _LONG_B64.sub(MASK, text)
    return text


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact `record`'s message and traceback in place; return it."""
    if getattr(record, "_hpldn_redacted", False):
        return record
    try:
        message = record.getMessage()
    except Exception:  # a malformed %-format: log what there is rather than nothing
        message = str(record.msg)
    record.msg = redact(message)
    record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    record._hpldn_redacted = True
    return record


class RedactFilter(logging.Filter):
    """A `logging.Filter` that redacts each record (`redact_record`) and lets it through."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_install_lock = threading.Lock()
_installed = False
FILTER = RedactFilter()


def _all_handlers():
    loggers = [logging.getLogger()]
    loggers += [lg for lg in logging.root.manager.loggerDict.values() if isinstance(lg, logging.Logger)]
    for logger in loggers:
        yield from logger.handlers


def install() -> None:
    """
    Redact every log record from now on (idempotent).

    The filter goes on the root logger and every existing handler; the record
    factory is wrapped so records for handlers attached later are covered too.
    """
    global _installed
    with _install_lock:
        root = logging.getLogger()
        if FILTER not in root.filters:
            root.addFilter(FILTER)
        for handler in _all_handlers():
            if FILTER not in handler.filters:
                handler.addFilter(FILTER)
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs) -> logging.LogRecord:
            return redact_record(previous(*args, **kwargs))

        logging.setLogRecordFactory(factory)
        _installed = True
