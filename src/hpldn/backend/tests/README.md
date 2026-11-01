# `hpldn.backend.tests` — the backend test suite

pytest, configured in `pyproject.toml` (`[tool.pytest.ini_options]`): strict markers,
strict xfails, and `pytest-env` unsetting `HPLDN_CAPTCHA_DISABLED`, `HPLDN_DB_URL` and
`HPLDN_RUN_REBUILD_MIGRATIONS` so a developer's shell cannot leak into a run. Paths the
suite needs (`HPLDN_DIR_FP`, `HPLDN_LOG_FP`, and the config template when there is no
`configuration/staging.yaml`) are derived from the checkout in `conftest.py`.

```bash
uv run pytest src/hpldn/backend/tests                              # everything allowed without writes
uv run pytest src/hpldn/backend/tests -m "not pg and not hpdbldn and not db_write"   # hermetic only
HPLDN_TEST_DB_WRITES=1 uv run pytest src/hpldn/backend/tests -m pg  # the ephemeral-cluster tests
scripts/ci/run_all.sh unit pg hpdbldn coverage                     # as CI runs them
```

## Rules (`conftest.py`)

- **A skip is a failure.** A skipped test, a non-strict `xfail` or a module skipped at
  collection fails the run and names the missing prerequisite. The one allowed skip is
  the next rule.
- **Database writes are opt-in.** Tests marked `pg` or `db_write` run only with
  `HPLDN_TEST_DB_WRITES=1` (`run_all.sh` sets it for its `pg` and `hpdbldn` stages).
- **The build database is read-only.** `hpdbldn_ro` opens `default_transaction_read_only`
  sessions; `hpdbldn_rollback` is the savepoint-and-rollback harness for checks that must
  write, and is `db_write`.
- **No test signs with a real key.** Model bundles are signed with a throwaway key
  (`_bundles.py`), trusted alongside the tracked `models/trusted_keys/`;
  `HPLDN_ALLOW_UNSIGNED_MODEL` and `HPLDN_ENV` are cleared.

`test_suite_rules.py` proves these rules by running pytest on throwaway test files.

## Markers

| Marker | Needs |
| --- | --- |
| `pg` | The ephemeral PostgreSQL 18 cluster (`_pg.py`: `initdb` into a temporary directory, trust auth on the loopback, `db_init.sql` applied); `HPLDN_TEST_DB_WRITES=1` |
| `hpdbldn` | The migrated build database, read-only |
| `db_write` | Creates, updates or deletes, even inside a rolled-back transaction; `HPLDN_TEST_DB_WRITES=1` |
| `gpu` | A CUDA GPU and CuPy |
| `nginx` | The nginx `cloud/local/fetch_nginx.sh` unpacks, or one on `PATH` |
| `tofu` | The OpenTofu CLI |
| `tier_a` | The Tier A migration inputs under `HPLDN_DATA_DIR` (`python -m hpldn.backend.migrations.inputs pull` restores them from OCI) |

Tests using `hpdbldn_ro`, `hpdbldn_rollback` or `pg_cluster` are marked automatically.

## Layout

| Directory | Covers |
| --- | --- |
| `api/` | Routes, auth, the session pass, rate limits, hardening, the access log and the market logic against fakes or in-memory SQLite; the market and address-search SQL read-only against the build database |
| `cloud/` | OCI provisioning (`provision.py`, with fakes), the nginx hardening, the frontend production build, the sourceless install `deploy.sh` performs |
| `common/` | Tuning formulas, the resource plan, the 68-95-99.7 helpers, log redaction |
| `guard/` | The captcha pass, detector, store, sinks, Cloudflare list, crawler ranges and daemon |
| `infra/` | `bare_metal`, `db_init.sql`, the migration chain (`test_database_upgrade.py`, the data-repair revisions), migration inputs, the fetchers, sale linking, the address parser, the serving dump, the local stack, the fake Google, the guard's nginx rendering and shellcheck over every shell script |
| `ml/` | `hpp_rf`, `hpp_cb`, `hpp_inference`, the HPI projection, the local index, ablation, plots, PTAL wiring, model bundles, the training-speed parity tests, a golden-price test on the served bundle (`HPLDN_GOLDEN_MODEL_FP`), and a data-quality gate over every model feature |
| `load/` | `address_search.js`: a k6 load test of `GET /addresses/search`, run by hand before a release, not by pytest |
| top level | `test_serving_tables.py` (the serving dump holds every table the runtime reads), `test_suite_rules.py` |

Helpers: `_pg.py` (the ephemeral cluster), `_bundles.py` (throwaway signing keys and a
tiny signed bundle), `ml/_training_speed_reference.py` (the pre-vectorisation code the
parity tests compare against).

The from-empty replay of the whole chain is not a pytest test: it takes hours and needs
every dataset, so it is `scripts/ci/replay_chain.sh`, run nightly.

## GitHub CI

`.github/workflows/ci.yml` runs, on free GitHub-hosted runners, every test that needs
no server, no private data and no GPU: `pytest -m "not hpdbldn and not gpu and not
tier_a"` with `HPLDN_TEST_DB_WRITES=1` (the hermetic tests and the `pg` cluster tests,
PostgreSQL 18 + PostGIS from PGDG), held to `check_coverage.py --hosted`'s floors;
Jest; Playwright's `e2e/coverage.spec.ts` (`playwright.static.config.ts`: no stack, no
browser); and `tofu test` with mock providers. It does not run the `hpdbldn` tests (the
build database), `gpu`, `tier_a`, the rest of Playwright (the local stack, with the
serving database and the signed model), the `model` stage or the chain replay.

It writes `scripts/ci/develop.ci.env` (placeholders only) to `develop.env`, and sets
`DB_SCHEMA=staging` (`api/__init__.py` would otherwise default it to `production` at
import) and `HPLDN_PG_BINDIR` (the runner also ships PostgreSQL 16). The same run on a
workstation, from a clean environment:

```bash
scripts/ci/run_all.sh hosted jest e2e_static tofu
```
