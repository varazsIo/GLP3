# `hpldn.backend.db` — database bootstrap and grants SQL

Everything a PostgreSQL database needs that the alembic chain cannot create itself, and
the SQL the serving host's database is locked down with. The files ship in the wheel
(`[tool.setuptools.package-data]`).

## Layout

| Path | Run by | What it does |
| --- | --- | --- |
| `sql/db_init.sql` | the superuser, once, before any migration | Creates the migration role (`LOGIN` only, no password), the database, `postgis`/`btree_gist`/`pg_trgm`, the `staging` and `production` schemas, default privileges, the `deploy_*` `SECURITY DEFINER` helpers and a `NOLOGIN` stub for the API role. Idempotent |
| `sql/db_grants_post_migration.sql` | the migration role, after every full build | Read grants on `production` tables that already existed, or were created by another role, when the default privileges were set |
| `sql/db_verify_init.sql` | anyone, read-only | One assertion per expected property; fails with a non-zero exit under `ON_ERROR_STOP` |
| `sql/db_enable_api_login.sh` | the superuser, on a local server | Gives the API role a generated password and its settings without logging the password, checks it can read `production.london_houses`, and prints the `POSTGRES_API_*` lines for `develop.env` |
| `sql/fragments/api_grants.sql`, `api_role_settings.sql` | included by the scripts above | The single source for the API role's read grants and settings (`statement_timeout`, `work_mem`) |
| `sql/fragments/serving_owner.sql`, `serving_db_lockdown.sql` | the serving host | Who owns the serving data, and that only the API role may connect |
| `render_vps_sql.py` | `cloud/vps/deploy.sh` | Renders the fragments into the serving host's self-contained `cloud/vps/postgres/api_role.sql` and `grants.sql` (gitignored build output); `--check` exits 1 when they are stale |

## Order

```
db_init.sql                         (superuser, once)
  └─ alembic upgrade 0001
     └─ bare_metal --load-raw       (StagingCopy: staging.ukhpucl)
        └─ alembic upgrade head
           ├─ db_grants_post_migration.sql
           ├─ bare_metal --finalize (SET LOGGED, durability, ANALYZE)
           └─ db_verify_init.sql
db_enable_api_login.sh              (superuser, local API only)
```

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres -f src/hpldn/backend/db/sql/db_init.sql
psql -v ON_ERROR_STOP=1 -d hpdbldn -f src/hpldn/backend/db/sql/db_grants_post_migration.sql
psql -v ON_ERROR_STOP=1 -d hpdbldn -f src/hpldn/backend/db/sql/db_verify_init.sql
sudo -u postgres bash src/hpldn/backend/db/sql/db_enable_api_login.sh [--db hpdbldn] [--role hpldn_api]
```

`db_init.sql` takes `-v migration_role=… -v db_name=… -v api_role=…` (defaults `smith`,
`hpdbldn`, `hpldn_api`). No file here holds a password.

## Why the migration role has no `CREATE` on the database

It is deliberate. Withholding `CREATE` is what makes the `deploy_*` helpers necessary:
the role can create schemas and extensions only through them, with a pinned
`search_path`. `CREATE SCHEMA IF NOT EXISTS` cannot replace the existence check in
`pipeline_steps._ensure_production_schema`, because PostgreSQL checks the `CREATE`
privilege before `IF NOT EXISTS` short-circuits.

## psql variables inside `DO` blocks

psql's `:'var'` substitution happens client-side and does not reach into dollar-quoted
strings. The scripts therefore copy `:'migration_role'` and `:'api_role'` into
session-level settings (`set_config('hpldn.api_role', …)`) and every `DO $$ … $$` block
reads them back with `current_setting()`.

## Tests

`tests/infra/test_db_init.py` (idempotency, a failing verify on a broken database, the
role and privilege shape, on an ephemeral cluster) and `tests/infra/test_shell_scripts.py`
(shellcheck and behaviour of `db_enable_api_login.sh`).
