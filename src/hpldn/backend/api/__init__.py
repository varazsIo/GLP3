"""
HPLDN read API (FastAPI), serving the production schema read-only.

``DB_SCHEMA`` defaults to ``production`` here, before any model import, because
`hpldn.backend.models.staging` binds its schema from it at import time. The
routes themselves read `hpldn.backend.models.production` and name
``production.*`` explicitly, so an exported ``DB_SCHEMA=staging`` redirects none
of them.
"""

import os

os.environ.setdefault("DB_SCHEMA", "production")
