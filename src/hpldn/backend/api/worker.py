"""
The gunicorn worker class the serving host runs the API with.

    gunicorn hpldn.backend.api.main:app -k hpldn.backend.api.worker.HardenedUvicornWorker

uvicorn's own worker, with three of its defaults turned off:

* ``server_header``: no ``server: uvicorn`` on any answer, so a response
  names no software or version (nginx's ``server_tokens off`` covers the
  edge);
* ``date_header``: nginx adds its own ``Date``;
* ``proxy_headers``: uvicorn would otherwise rewrite the client address
  from ``X-Forwarded-For`` for the addresses in ``forwarded_allow_ips``. The
  API takes the client from nginx's ``X-Real-IP`` itself, and only from the
  trusted proxy (`hpldn.backend.api.ratelimit.client_key`), so nothing a
  client sends can choose its own address.
"""

from uvicorn.workers import UvicornWorker


class HardenedUvicornWorker(UvicornWorker):
    """`UvicornWorker` without the ``server`` and ``date`` headers or proxy-header handling."""

    CONFIG_KWARGS = {
        **UvicornWorker.CONFIG_KWARGS,
        "server_header": False,
        "date_header": False,
        "proxy_headers": False,
    }
