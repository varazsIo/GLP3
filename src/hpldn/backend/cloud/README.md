# `backend/cloud` — hosting

One deployment target: a single OCI host behind Cloudflare.

| Directory | Purpose |
| --- | --- |
| [`oci/`](oci/README.md) | OpenTofu (`state/`: compartment, Vault key, buckets, deployer identity, budget, quotas; `main/`: network, A1 instance, Cloudflare, tailnet key) and `python -m hpldn.backend.cloud.oci`, which provisions, deploys and checks it all |
| [`vps/`](vps/README.md) | The serving host (an OCI A1 VM behind Cloudflare): `bootstrap.sh`, `deploy.sh`, nginx, the guard daemon, PostgreSQL holding only the serving tables, systemd units |
| [`local/`](../api/README.md) | The local stack: production's nginx, guard and gunicorn on localhost (`python -m hpldn.backend.cloud.local up`) |

The serving host's `postgres/api_role.sql` and `postgres/grants.sql` are rendered by
`python -m hpldn.backend.db.render_vps_sql` from `src/hpldn/backend/db/sql/fragments/`
(`deploy.sh` runs it); they are not tracked.

The database itself is built locally (`python -m hpldn.backend.scripts.bare_metal`) and
shipped as a signed dump (`python -m hpldn.backend.scripts.export_serving_db --sign`).
