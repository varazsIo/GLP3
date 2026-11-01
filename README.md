# HPLDN — London House Price Data Platform

HPLDN (`hpldn`) is a full-stack application that cleans, fuses and analyses UK housing
data for Greater London, trains a price model on it and serves the results through a
public website.

- **Pipeline.** An Alembic migration chain against PostgreSQL + PostGIS loads, links and
  enriches Land Registry sales, energy certificates and geographic context, and builds
  one model-ready serving view, `production.london_houses` (one row per sale).
- **Models.** A CatBoost price model (`hpp_cb`) and a Random Forest baseline (`hpp_rf`)
  train on that view with leakage-safe time-series splits. The served model is a signed,
  data-only bundle.
- **API.** A read-only FastAPI service serves area figures, estimates, address search
  and model prices for a specific home on a future day.
- **Frontend.** A React + TypeScript single-page app. A landlord describes a London
  property, or picks a home by address, and gets an indicative price, the range around it
  and the reasons behind it.
- **Deployment.** One OCI Always Free VM behind Cloudflare, provisioned with OpenTofu,
  with nginx, a bot-protection guard daemon and a PostgreSQL that holds only the serving
  tables.

The whole pipeline is an **Alembic migration chain**: every load, clean, fusion and
feature build is a numbered, resumable revision, so the database *is* the pipeline state.

> **The data is not part of this repository.** The code expects the datasets described
> in [Data](#data) to be present locally; see that section for why they are not
> published and under which licences they are available.

## Contents

- [Architecture](#architecture)
- [Repository structure](#repository-structure)
- [Data](#data)
  - [Why the data is not included](#why-the-data-is-not-included)
  - [Datasets and licences](#datasets-and-licences)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Environment variables](#environment-variables)
- [Database setup](#database-setup)
- [Migrations and seeding](#migrations-and-seeding)
- [Model training and serving](#model-training-and-serving)
- [Read API](#read-api)
- [Frontend](#frontend)
- [Deployment](#deployment)
- [Testing and CI](#testing-and-ci)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Disclaimer](#disclaimer)
- [License](#license)

## Architecture

```
 data/ (local, not published)
   │  migrations/inputs.yaml: every file, its checksum and licence
   ▼
 PostgreSQL + PostGIS (local build database, "hpdbldn")
   │  alembic upgrade head  (0001 … 0034)
   │    staging.*  : raw loads, cleaning, EPC/PPD fusion, sale linking
   │    production.*: promoted tables, feature tables, london_houses (materialized view)
   ├──────────────► scripts/hpp_cb.py ──► signed model bundle (models/artifact.py)
   │
   ▼  scripts/export_serving_db.py --sign  (serving tables only)
 Serving host (OCI A1 VM, Ubuntu)
   PostgreSQL (serving tables) ◄── gunicorn + FastAPI (api/) ◄── nginx ◄── Cloudflare ◄── browser
                                        ▲                          │
                                        └── guard daemon (guard/) ─┘  (rate limits, bans, reCAPTCHA)
   nginx also serves the React build (frontend/)
```

## Repository structure

Everything server-side lives under `src/hpldn/backend/` and the React client under
`src/hpldn/frontend/`. Only the backend is a Python package; the frontend is a separate
npm project and is excluded from the wheel (`[tool.setuptools.packages.find]` in
`pyproject.toml`).

```
.
├── .github/workflows/ci.yml        # GitHub Actions: the server-free tests
├── pyproject.toml                  # Python dependencies, extras, pytest/coverage/ruff config
├── uv.lock
├── configuration/
│   └── osm_addresses.ini           # GDAL OSM config, a migration input (0028); local
│                                   #   staging.yaml also lives here (gitignored)
├── scripts/
│   ├── ci/                         # run_all.sh (local + hosted CI), replay_chain.sh (nightly), check_coverage.py, develop.ci.env
│   └── install_tofu.sh             # pinned OpenTofu / cosign / gitleaks installer
└── src/hpldn/
    ├── backend/
    │   ├── alembic.ini
    │   ├── api/                    # FastAPI read API                     → api/README.md
    │   ├── cloud/                  # hosting                              → cloud/README.md
    │   │   ├── local/              #   production's stack on localhost
    │   │   ├── oci/                #   OpenTofu + provisioning CLI       → cloud/oci/README.md
    │   │   └── vps/                #   serving host scripts and configs  → cloud/vps/README.md
    │   ├── common/                 # config, DB URLs, tuning, hardware, GPU → common/README.md
    │   ├── config/                 # staging.example.yaml (template for configuration/staging.yaml)
    │   ├── db/                     # bootstrap SQL and the serving host's SQL → db/README.md
    │   ├── guard/                  # bot-protection daemon                → guard/README.md
    │   ├── migrations/             # the Alembic chain and its helpers     → migrations/README.md
    │   ├── models/                 # SQLAlchemy models, signed model bundles → models/README.md
    │   ├── preprocessing/          # StagingCopy: chunked bulk COPY of the UCL extract
    │   ├── scripts/                # CLIs: seeding, fetchers, training, inference → scripts/README.md
    │   └── tests/                  # pytest suite                         → tests/README.md
    └── frontend/                   # React + TypeScript client             → frontend/README.md
        ├── src/                    #   the app, by feature folder
        ├── e2e/                    #   Playwright suite
        ├── public/
        └── scripts/strip-build.mjs
```

Gitignored, machine-local paths the code reads: `data/` (or `$HPLDN_DATA_DIR`),
`configuration/staging.yaml`, `develop.env`, and `tmp/` (logs, caches, model bundles,
serving dumps).

## Data

### Why the data is not included

The code reads every dataset from a local data directory, `$HPLDN_DATA_DIR` (default
`<repo>/data`), and none of it is published here:

1. **Size.** The inputs are far beyond what a Git repository can hold: the UCL linked
   extract alone is 23 GB, and the Price Paid, EPC, OS Open UPRN, OpenStreetMap and
   INSPIRE files add tens of gigabytes more. A full build needs ~100 GB of disk.
2. **Licence terms.** Each dataset has its own licence. Some carry conditions that a
   blanket republication in a code repository would not meet: EPC address data is
   excepted from the Open Government Licence, the INSPIRE parcels and the Bank of
   England series have their own terms, the UCL extract is distributed under the UK
   Data Service's terms, and OpenStreetMap data is share-alike under the ODbL.
   Publishing only the code leaves each dataset under its own licence, obtained from its
   publisher.
3. **Address-level records.** The raw and derived tables hold sales and energy
   certificates for individual addresses. They are public records, but there is no
   reason to redistribute them in bulk alongside the code.
4. **Reproducibility does not need them in Git.** `src/hpldn/backend/migrations/inputs.yaml`
   pins every input: its path under the data directory, the revisions that read it, its
   size and SHA-256, its licence and, where one exists, the command that fetches it.
   `python -m hpldn.backend.migrations.inputs check` verifies a local copy before a long
   build, and `bare_metal --check` reports every missing file at once.

For the same reasons, the built databases, the serving dumps (`tmp/serving/`) and the
trained model bundles (`tmp/models/`) are not published either.

The expected layout under the data directory is exactly the `path` of each entry in
`inputs.yaml`. A few small inputs derived by hand ("Tier A": the PPD→UPRN linkage
fixture, the ONS rents extracts and the CPIH series) live under `migration_inputs/`. A
`data:` block in `configuration/staging.yaml` can override the path of any single
entry.

### Datasets and licences

Licences as recorded in `migrations/inputs.yaml`. Where a dataset is under the Open
Government Licence, its use here is subject to the attribution statement "Contains
public sector information licensed under the Open Government Licence v3.0"; Ordnance
Survey products additionally carry "Contains OS data © Crown copyright and database
right"; Land Registry data "Contains HM Land Registry data © Crown copyright and
database right"; ONS data "Source: Office for National Statistics".

| Dataset | Used for | Licence |
| --- | --- | --- |
| UCL UK House Prices 2024, linked Price Paid + EPC extract (`tranall_link_*.csv`) | the original sales/certificate spine (`staging.ukhpucl`) | UK Data Service ReShare terms (DOI below) |
| HM Land Registry Price Paid Data: yearly files and `pp-complete.csv` | sales from 1995 onward, fused into the spine (0003, 0025) | OGL v3.0 |
| HM Land Registry Price Paid → UPRN lookup | sale provenance and link audit (0027) | OGL v3.0 |
| HM Land Registry INSPIRE Index Polygons | parcel areas (0005) | INSPIRE end user licence (HM Land Registry) |
| Domestic Energy Performance Certificates (`certificates-*.csv`) | floor area, rooms, rating, property type | OGL v3.0, **address data excepted** |
| OS Open UPRN (two releases) | UPRN coordinates | OGL v3.0 |
| OS Code-Point Open | postcode centroids | OGL v3.0 |
| OS Open Rivers, OS Open Greenspace, OS Open Roads | proximity features, sale linking | OGL v3.0 |
| ONS LSOA/MSOA lookups and LSOA 2011 boundaries | geography features | OGL v3.0 |
| ONS National Statistics UPRN Lookup (NSUL) | sale linking (0027) | OGL v3.0 |
| Linked Identifiers Datasets (LIDS) | sale linking (0028) | OGL v3.0 |
| English Indices of Deprivation 2010, 2015, 2019, 2025 | deprivation features | OGL v3.0 |
| TfL PTAL 2023 100 m grid | public transport accessibility | OGL v3.0 (TfL) |
| UK House Price Index (London) | deflator and projection | OGL v3.0 |
| ONS Price Index of Private Rents (borough and London extracts) | rents and yields (0023, 0026) | OGL v3.0 |
| ONS CPIH annual rate | inflation-adjusted figures (0032) | OGL v3.0 |
| GLA Planning London Datahub applications | sale linking (0028) | OGL v3.0 |
| Ofsted state-funded school inspections and DfE GIAS | school features (0033, 0034) | OGL v3.0 |
| Bank of England Bank Rate and quoted mortgage rates | rate features, yield calculator | Bank of England terms of use |
| OpenStreetMap Greater London extract | sale linking (0028); OSM-derived links are kept apart (`staging.ppd_uprn_link_odbl`) | ODbL 1.0, © OpenStreetMap contributors |
| Doogal London stations and London postcodes (CSV, KML) | station proximity, sale linking, the site's map (`scripts/build_london_map.py`) | Doogal, derived from OGL v3.0 sources (ONSPD) |
| Derived Tier A files (`migration_inputs/`) | PPD→UPRN linkage fixture, ONS rents and CPIH extracts | derived from the OGL v3.0 sources above |

`src/hpldn/frontend/src/data/londonGeo.ts`, the map geometry generated from the Doogal
postcode boundaries and ONS LSOA boundaries, is the only data derivative in the
repository.

## Requirements

- **Python 3.14+** and [uv](https://docs.astral.sh/uv/).
- **PostgreSQL** (developed against 18) with **PostGIS**, `btree_gist` and `pg_trgm`.
- **GDAL** (`ogr2ogr`) for the GML/PBF/GeoPackage loads, and **`psql`** on `PATH` for
  the bulk `\copy` loads.
- **Node.js and npm** for the frontend only (developed against Node 22).
- Linux or macOS; ~100 GB of free disk and the datasets above for a full build.

Optional:

- An NVIDIA GPU with CUDA for GPU training and EDA (`.[gpu]`, CatBoost's built-in GPU
  trainer, RAPIDS cuDF/cuML for `hpp_rf --device gpu`).
- **OpenTofu**, an OCI tenancy, a Cloudflare zone and a Tailscale tailnet for the
  deployment (`scripts/install_tofu.sh` installs the pinned tools).

## Installation

```bash
# System prerequisites (Debian/Ubuntu example)
sudo apt install postgresql postgresql-contrib postgresql-<version>-postgis-3 gdal-bin

git clone <repository-url> HPLDN
cd HPLDN
uv venv --python 3.14
source .venv/bin/activate
uv pip install -e ".[dev,api]"
```

Extras (`pyproject.toml` is the only dependency manifest):

| Extra | Contents |
| --- | --- |
| `api` | FastAPI, uvicorn, gunicorn, psycopg 3 (the serving host installs only this) |
| `dev` | pytest, pytest-env, pytest-xdist, pytest-cov, aiosqlite, ruff, shellcheck-py, pip-audit |
| `gpu` | CuPy for CUDA 13 (`common.accel` / `common.hardware`); without it the same code runs on NumPy |

RAPIDS for `hpp_rf --device gpu` ships from NVIDIA's own index and is not an extra:
`pip install --extra-index-url https://pypi.nvidia.com libcuml-cu13 cudf-cu13`.

The frontend has its own dependency tree:

```bash
cd src/hpldn/frontend && npm install
```

## Configuration

Two machine-local files, both gitignored.

**`configuration/staging.yaml`** — copy
[`src/hpldn/backend/config/staging.example.yaml`](src/hpldn/backend/config/staging.example.yaml).
It holds bulk-load chunking, the PostgreSQL tuning policy (`common.tuning` derives the
actual settings from the host's vCPUs and RAM), logging, the served model bundle
(`models.catboost_bundle`) and an optional `data:` block of per-input path overrides.
`${VAR}` and `${VAR:-default}` are expanded from the environment when it is loaded. It
is found through `$HPLDN_CONFIG_FP`, else `<repo>/configuration/staging.yaml`.

**`develop.env`** — in the repository root, loaded by `common.utils.load_env_vars`:

```env
HPLDN_DIR_FP=/path/to/HPLDN
POSTGRES_DEVELOP_USERNAME=<migration role>
POSTGRES_DEVELOP_PASSWORD=<password>
POSTGRES_DEVELOP_HOST=localhost
POSTGRES_DEVELOP_DATABASE=hpdbldn
# Optional fallback database (get_db_url / get_migration_db_url try it last)
POSTGRES_TESTING_USERNAME=...
POSTGRES_TESTING_PASSWORD=...
POSTGRES_TESTING_HOST=localhost
POSTGRES_TESTING_DATABASE=...
# Read API, as the read-only hpldn_api role (see Database setup)
POSTGRES_API_USERNAME=hpldn_api
POSTGRES_API_PASSWORD=<printed by db_enable_api_login.sh>
POSTGRES_API_HOST=localhost
POSTGRES_API_PORT=5432
POSTGRES_API_DATABASE=hpdbldn
POSTGRES_API_SSLMODE=prefer
HPLDN_API_KEY=dev
```

It holds plaintext credentials: `chmod 600 develop.env`. Values already in the process
environment win over the file unless `HPLDN_ENV_OVERRIDE=1`.

## Environment variables

The main ones; each module's README lists its own.

| Variable | Purpose |
| --- | --- |
| `HPLDN_DIR_FP` | Repository root; `develop.env` is found there |
| `HPLDN_CONFIG_FP` | Explicit path to `staging.yaml` |
| `HPLDN_DATA_DIR` | Data directory the `inputs.yaml` paths are relative to (default `<repo>/data`) |
| `HPLDN_DB_URL` | Full SQLAlchemy URL; wins over every `POSTGRES_*` block |
| `POSTGRES_DEVELOP_*`, `POSTGRES_TESTING_*` | Build database, then fallback, for the migrations, scripts and models (`common.db`) |
| `POSTGRES_API_*`, `HPLDN_API_KEY` | The read API's connection and key (`api.config`) |
| `HPLDN_PRICE_MODEL_FP` | Model bundle the API serves; overrides `models.catboost_bundle` |
| `HPLDN_ALLOW_UNSIGNED_MODEL`, `HPLDN_ENV` | Load an unsigned bundle in development; refused when `HPLDN_ENV=production` |
| `HPLDN_MODEL_SIGNING_KEY`, `HPLDN_SERVING_SIGNING_KEY` | Ed25519 keys that sign model bundles and serving dumps (default under `~/.config/hpldn/`) |
| `HPLDN_CAPTCHA_DISABLED`, `HPLDN_PASS_SECRET` | The API's reCAPTCHA gate and session-pass secret (`api/README.md`) |
| `HPLDN_MARKET_CACHE_TTL_S` | How often each API worker reloads the area figures (default 3600) |
| `HPLDN_SKIP_INPUTS_CHECK` | `=1` skips the pre-upgrade input check in `migrations/env.py` |
| `HPLDN_TEST_DB_WRITES` | `=1` runs the tests that write to a database (see [Testing](#testing-and-ci)) |
| `HPLDN_DISABLE_GPU` | `=1` forces the NumPy path even where CuPy is installed |
| `HPLDN_HOST_VCPUS`, `HPLDN_HOST_MEMORY_MIB`, `HPLDN_HOST_GPU_COUNT`, `HPLDN_HOST_GPU_VENDOR` | Override host-resource detection (`common.hardware`) |

Secrets never enter `argv`: `common.db.get_psql_env` passes the password to `psql`
subprocesses in `PGPASSWORD`.

## Database setup

Run once, as the PostgreSQL superuser, before any migration:

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres -f src/hpldn/backend/db/sql/db_init.sql
# non-default names:
sudo -u postgres psql -v ON_ERROR_STOP=1 \
     -v migration_role=alice -v db_name=hpdbldn_dev -v api_role=hpldn_api \
     -d postgres -f src/hpldn/backend/db/sql/db_init.sql
```

It creates the migration role (`LOGIN`, nothing else, no password), the database, the
extensions, the `staging`/`production` schemas, the default privileges and the
`SECURITY DEFINER` helpers the chain relies on, plus a `NOLOGIN` stub for the read-only
API role. The migration role deliberately has no `CREATE` on the database. After a
build:

```bash
psql -v ON_ERROR_STOP=1 -d hpdbldn -f src/hpldn/backend/db/sql/db_grants_post_migration.sql
psql -v ON_ERROR_STOP=1 -d hpdbldn -f src/hpldn/backend/db/sql/db_verify_init.sql
sudo -u postgres bash src/hpldn/backend/db/sql/db_enable_api_login.sh   # gives hpldn_api a password
```

See [`src/hpldn/backend/db/README.md`](src/hpldn/backend/db/README.md) for what each
script does and why.

## Migrations and seeding

`hpldn.backend.scripts.bare_metal` is the supported entry point. It checks connectivity,
the `db_init.sql` prerequisites and every dataset the target revisions read, then drives
the chain:

```bash
python -m hpldn.backend.migrations.inputs check     # every input present (add --hash for SHA-256)
python -m hpldn.backend.scripts.bare_metal --check  # prerequisites + inputs, no seeding
python -m hpldn.backend.scripts.bare_metal --schema-only   # revision 0001 only
python -m hpldn.backend.scripts.bare_metal --load-raw      # StagingCopy: the UCL extract
python -m hpldn.backend.scripts.bare_metal                 # every revision to head
python -m hpldn.backend.scripts.bare_metal --revision 0020 # stop at a revision
python -m hpldn.backend.scripts.bare_metal --finalize      # SET LOGGED, durability, ANALYZE
```

Or with Alembic directly (`alembic -c src/hpldn/backend/alembic.ini upgrade head`). The
chain is 34 revisions, `0001` … `0034` (head). It builds from an empty database, runs
each revision in its own transaction (a failed revision rolls back, and re-running
resumes there) and refuses downgrades from `0002` on. The target is
`common.db.get_migration_db_url`: `HPLDN_DB_URL`, else `POSTGRES_DEVELOP_*`, else
`POSTGRES_TESTING_*`.

The revision-by-revision table, the rules every revision follows and how to add one are
in [`src/hpldn/backend/migrations/README.md`](src/hpldn/backend/migrations/README.md);
the pipeline steps and fetch scripts in
[`src/hpldn/backend/scripts/README.md`](src/hpldn/backend/scripts/README.md); the schema
(three declarative bases, `production.london_houses`) in
[`src/hpldn/backend/models/README.md`](src/hpldn/backend/models/README.md).

## Model training and serving

Both models train on `production.london_houses` and share the dataset load, splits and
feature engineering (`hpp_cb` imports them from `hpp_rf`).

```bash
# Train the served CatBoost model, all but the last 5 % of sales
python -m hpldn.backend.scripts.hpp_cb --device gpu --train-frac 0.95 --val-frac 0.025 \
  --smearing-correction --model-out tmp/models/model

# Measure it on later sales and write the price ranges it reports
python -m hpldn.backend.scripts.hpp_inference evaluate --model tmp/models/model \
  --from YYYY-MM-DD --sample 3000 --write-calibration --mask-epc

# Price one home on a future day
python -m hpldn.backend.scripts.hpp_inference price --model tmp/models/model \
  --postnumber 18 --poststreet "BARCLAY ROAD" --posttown CROYDON --postcode "CR0 1JN" --date YYYY-MM-DD

# Random Forest baseline, feature ablation, SHAP and correlation plots
python -m hpldn.backend.scripts.hpp_rf --limit 20000 --n-estimators 50
python -m hpldn.backend.scripts.model_ablation --model catboost --strategy loo
python -m hpldn.backend.scripts.data_visualization --model both --out-dir tmp/plots
```

A model is a signed directory of data only (`model.cbm`, `meta.json`, JSON sidecars,
probe rows and a manifest), made and checked with `python -m hpldn.backend.models.artifact
keygen|sign|verify|convert`. The API serves a bundle only if it verifies against
`models/trusted_keys/`; there is no fallback. Splitting strategies, evaluation modes,
inference and the HPI projection are documented in
[`src/hpldn/backend/scripts/README.md`](src/hpldn/backend/scripts/README.md) and the
bundle format in [`src/hpldn/backend/models/README.md`](src/hpldn/backend/models/README.md).

## Read API

```bash
uvicorn hpldn.backend.api.main:app --reload          # development, with develop.env
python -m hpldn.backend.cloud.local up               # production's nginx + guard + gunicorn locally
```

| Route | Purpose |
| --- | --- |
| `GET /health`, `GET /ready` | liveness, readiness |
| `GET /session/config`, `POST /session` | reCAPTCHA site key; exchange a token for a session pass |
| `GET /areas`, `GET /areas/{code}`, `GET /areas/{code}/comparables` | area figures for the 132 covered areas |
| `GET /inflation` | the CPIH series |
| `POST /estimate` | estimate from a description of a home |
| `GET /addresses/search`, `GET /addresses` | address suggestions; homes at a postcode |
| `POST /price`, `POST /nearby`, `POST /schools` | model price for a home on a day; nearby sales; nearby schools |
| `GET /properties`, `/properties/{uprn}`, `/properties/{uprn}/transactions` | key-only property lookups, never exposed through nginx |

Authentication, the session pass, rate limiting, caching and the market logic are in
[`src/hpldn/backend/api/README.md`](src/hpldn/backend/api/README.md).

## Frontend

```bash
cd src/hpldn/frontend
npm start                      # dev server on :3000, /api proxied to the local stack's nginx (HPLDN_EDGE_URL)
npm test                       # Jest + React Testing Library
npm run build:prod             # production bundle, stripped by scripts/strip-build.mjs
npm run e2e                    # Playwright, against the local stack
```

The browser only calls same-origin `/api/*` and never holds the API key: nginx adds it.
Pages, components, conventions and tests are in
[`src/hpldn/frontend/README.md`](src/hpldn/frontend/README.md).

## Deployment

The site runs on one OCI Ampere A1 VM behind Cloudflare. OpenTofu in
`src/hpldn/backend/cloud/oci` creates the tenancy-side resources, the instance and the
Cloudflare configuration; `python -m hpldn.backend.cloud.oci check|state|up|bootstrap|deploy|status|destroy`
drives it. `cloud/vps/bootstrap.sh` hardens the host and `cloud/vps/deploy.sh` ships the
wheel, the frontend build, the signed model bundle and a signed serving dump built by
`python -m hpldn.backend.scripts.export_serving_db --sign`. See
[`src/hpldn/backend/cloud/README.md`](src/hpldn/backend/cloud/README.md).

## Testing and CI

```bash
uv run pytest src/hpldn/backend/tests                      # hermetic + read-only tiers
HPLDN_TEST_DB_WRITES=1 uv run pytest src/hpldn/backend/tests -m pg   # ephemeral-cluster tests
scripts/ci/run_all.sh                                      # every stage
scripts/ci/run_all.sh unit jest                            # chosen stages
scripts/ci/replay_chain.sh                                 # nightly: full chain from empty
```

`run_all.sh` stages: `lint` (ruff, eslint, tsc, shellcheck, `tofu fmt/validate/test`,
gitleaks), `unit`, `pg` (an `initdb`'d throwaway cluster), `hpdbldn` (read-only and
rolled-back checks against the build database), `coverage` (per-package floors),
`jest`, `e2e` (Playwright against `cloud.local up --fake-google`), `model`
(`models.artifact verify` on the served bundle) and `audit` (pip-audit, npm audit,
gitleaks).

GitHub Actions (`.github/workflows/ci.yml`) runs the tests that need no server, no
private data and no GPU (`run_all.sh hosted jest e2e_static tofu`); see the
[tests README](src/hpldn/backend/tests/README.md#github-ci).

In the pytest suite **a skip is a failure**: a test either proves something or names
the missing prerequisite. Database writes are opt-in (`HPLDN_TEST_DB_WRITES=1`), and
the build database is only ever read through read-only sessions. See
[`src/hpldn/backend/tests/README.md`](src/hpldn/backend/tests/README.md).

Lint is deliberately narrow: ruff selects `S101`/`B011` only, because `python -O` strips
`assert` and package code must validate with an explicit `raise`. No formatter or type
checker is configured for the backend.

## Troubleshooting

**`HPLDN_DIR_FP is not set, so develop.env cannot be located`** — export it. The test
suite derives it from the checkout (`tests/conftest.py`).

**`HPLDN configuration not found at <path>`** — create `configuration/staging.yaml`
from the template, or point `HPLDN_CONFIG_FP` at one.

**A missing dataset** — run `python -m hpldn.backend.migrations.inputs check` or
`bare_metal --check` first; both report every missing file and, where one exists, the
command that fetches it.

**A migration fails halfway** — fix the cause and re-run; the failed revision rolled
back and was never stamped. Do not wipe the database.

**`function deploy_spatial_extension() does not exist`**, **`permission denied for
schema production`** or **`type "geometry" does not exist`** — the database was never
bootstrapped: run `db_init.sql`, then `db_verify_init.sql`.

**A new `production` table is invisible to the API** — run
`db_grants_post_migration.sql` as the migration role.

**`psycopg2` build failure** — the pin is `psycopg2-binary`, which ships its own libpq.

## Limitations

- **London only.** `common.constants.LONDON_DISTRICTS` restricts the pipeline to Greater
  London.
- **A full build takes hours** and needs every dataset; the from-empty replay runs
  nightly (`scripts/ci/replay_chain.sh`), not in an ordinary `pytest` run.
- **Downgrades refuse** from `0002` on: restore from a dump or rebuild from empty.
- **The models describe, the migrations build.** Only revision `0001` calls
  `create_all`; everything else is DDL in `migrations/` and `scripts/pipeline_steps.py`,
  and nothing enforces that a model edit and its DDL agree.
- **Packaging.** `namespaces = false` keeps `node_modules` out of the wheel but also
  drops `__init__`-less directories (`preprocessing/`, `migrations/versions/`, `tests/`)
  from package discovery; `migrations/versions` ships through `package-data`. Use the
  editable install for development.
- **Migrations are not offline-safe**: only the pure-DDL revisions support
  `alembic upgrade --sql`.
- **Fetch scripts** depend on live ONS/TfL/ArcGIS services and can fail mid-run.

## Disclaimer

Estimates and prices produced by this software are indicative only. They are not a
valuation, a survey or financial advice.

This project uses the UK UCL House Prices 2024 dataset
(https://dx.doi.org/10.5255/UKDA-SN-857911), with earlier versions available at
https://data.london.gov.uk/dataset/house-price-per-square-metre-in-england-and-wales-epo9w/.
Credit for it goes to Bin Chi, Adam Dennett, Thomas Oléron-Evans and Robin Morphet —
thank you.

## License

The code is licensed under the GNU GPLv3 — see [LICENSE](LICENSE). The datasets it
processes are **not** covered by this licence; each remains under its own licence, as
listed in [Datasets and licences](#datasets-and-licences).

If you use this code, credit adaml.dev.team@gmail.com and link to this repository in
your own README so users can find the original.
