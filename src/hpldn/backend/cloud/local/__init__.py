"""
The local stack: production's nginx, guard daemon and gunicorn on localhost.

``python -m hpldn.backend.cloud.local up`` renders the production
``hpldn.conf`` and ``guard.yaml`` into ``tmp/local-stack/`` (`render`), then
runs nginx on 127.0.0.1:8080, the API under gunicorn on 127.0.0.1:8000 and the
``hpldn-guard`` daemon, so a request on localhost meets every limit, ban and
header it would meet in production. See ``api/README.md`` ("Local stack").
"""
