# `hpldn.backend.common` — shared helpers

Configuration loading, database URLs and engines, PostgreSQL tuning, GPU-optional array
maths and the code mappings used by the staging pipeline. Everything else in the backend
(migrations, scripts, tests, the API's local configuration) imports from here; nothing
here imports from `scripts` or `api`.

## Layout

| Module | Responsibility |
| --- | --- |
| `config.py` | Find, load and `${VAR}`-interpolate `configuration/staging.yaml` |
| `utils.py` | `load_env_vars` (`develop.env`), `load_yaml_safe`, `setup_logger`, 68-95-99.7 distribution checks |
| `db.py` | Database URLs (`get_db_url`, `get_migration_db_url`, `get_model_db_url`), psql URL/env helpers, `create_db`/`drop_db`, column statistics |
| `hardware.py` | CPUs (affinity, cgroup `cpu.max`), RAM (cgroup `memory.max`, `/proc/meminfo`) and GPUs (CuPy, `nvidia-smi`) available to this process |
| `tuning.py` | Derived PostgreSQL settings (cluster, seeding and bulk-session) from `hardware` |
| `accel.py` | CuPy-or-numpy array module selection (`get_array_module`, `asarray`, `to_host`) |
| `constants.py` | London boroughs, plausibility floors, EPC/PPD code mappings, canonical floor levels |
| `profiling.py` | Stage timings for training runs: wall time, peak RSS and peak VRAM per stage |
| `secrets.py` | `read_secret`: systemd credentials on the serving host, the environment elsewhere |
| `redact.py` | `RedactFilter`: masks loaded secrets, auth headers and long tokens in log records |

`__init__.py` re-exports the most used names: `create_db`, `drop_db`, `get_db_url`,
`get_migration_db_url`, `get_psql_env`, `get_psql_url`, `get_sql_dev_engine`,
`load_env_vars`, `load_yaml_safe`, `setup_logger` and `check_none_variants`.

## Configuration (`config.py`, `utils.py`)

`staging.yaml` is resolved in one place, `config.get_cfg_fp`:

1. `HPLDN_CONFIG_FP`, if set;
2. otherwise `<repo root>/configuration/staging.yaml`, where the repo root is
   `HPLDN_DIR_FP` or four directories above `config.py` (source checkouts only).

`get_cfg` loads it and runs `interpolate_cfg` over every string leaf. Two forms are
supported, and a literal `$` is otherwise left alone:

| Form | Result |
| --- | --- |
| `${VAR}` | `os.environ["VAR"]`; `KeyError` if unset |
| `${VAR:-default}` | `VAR`, or `default`, which may itself contain `${...}` |

Expansion repeats until the value stops changing, up to `MAX_INTERPOLATION_PASSES` (8),
so a self-referential variable raises `ValueError` instead of looping.

`utils.load_env_vars` reads `$HPLDN_DIR_FP/develop.env`. Values already in the
environment win; set `HPLDN_ENV_OVERRIDE=1` to let the file win.

## Database URLs (`db.py`)

| Function | Resolves to |
| --- | --- |
| `get_db_url` | `HPLDN_DB_URL`, else `POSTGRES_TESTING_*` |
| `get_migration_db_url` | `HPLDN_DB_URL`, else `POSTGRES_DEVELOP_*` (`hpdbldn`, the from-empty target), else `POSTGRES_TESTING_*` |
| `get_model_db_url` | The same order as `get_migration_db_url`: the database the model scripts train and score on |

The alembic chain rebuilds from empty, so it prefers the build database and never
defaults to a separately populated one (see the root README's
[Migrations and seeding](../../../../README.md#migrations-and-seeding)).

psql subprocesses never receive a password in argv, where any local user could read it
from the process list. `get_psql_url` renders a password-less `postgresql://` URL and
`get_psql_env` puts the password in `PGPASSWORD`.

`get_stat_array` and `distribution_assertions` back the EDA and data-quality tests:
per-column min/max/mean/null counts, and domain checks for `propertytype`, `oldnew`,
`duration`, `CER` and `tenure` that raise `ValueError` (not `assert`, so `python -O`
cannot strip them).

## Tuning (`tuning.py`, `hardware.py`)

Nothing hardcodes `work_mem` or parallel workers; they are derived from the resources
the process may use.

### Host detection

`detect_host_resources` (cached per process) tries, in order:

1. `HPLDN_HOST_VCPUS` and `HPLDN_HOST_MEMORY_MIB` (plus optional `HPLDN_HOST_GPU_COUNT`,
   `HPLDN_HOST_GPU_VENDOR`) — explicit overrides;
2. `hardware.detect()`: CPUs from the affinity mask lowered to the tightest cgroup v2
   `cpu.max` on the process's path; RAM from the tightest `memory.max` when below
   `/proc/meminfo` (macOS `sysctl` otherwise, 4 GiB if unreadable); GPUs from CuPy
   (`getDeviceCount`, `memGetInfo`, compute capability), then `nvidia-smi`, then none.
   A systemd scope or container limit is therefore what tuning sizes for, not the host.
   `python -m hpldn.backend.common.hardware` prints the result.

`resolve_host_resources(connection)` uses that for a local database, but for a remote one
infers the server's resources from its own `shared_buffers` and `max_worker_processes`,
so server memory is never sized from a laptop's RAM.

### Formulas

`compute_postgres_settings(resources, policy, data_volume_gb)` returns the cluster
settings. `TuningPolicy` (overridable through a `tuning:` block in `staging.yaml`)
supplies `memory_fraction` (0.90), `concurrent_queries` (2), `memory_nodes_per_query` (2)
and `max_workers_per_gather_cap` (16).

| Setting | Rule | Why |
| --- | --- | --- |
| `shared_buffers` | 25 % of RAM, 128 MB–64 GB | PostgreSQL's recommendation; more double-caches with the OS |
| `effective_cache_size` | 70 % of RAM | Planner hint only |
| `maintenance_work_mem` | RAM / 16, 256 MB–8 GB | |
| `autovacuum_work_mem` | RAM / 32, 128 MB–2 GB | |
| `autovacuum_max_workers` | vCPU / 4 + 1, 1–8 | Small hosts stay below one worker per core |
| `max_parallel_workers_per_gather` | vCPU / 2, capped at 16 (0 on one vCPU) | One parallel scan leaves half the box free |
| `max_parallel_maintenance_workers` | vCPU / 2, 1–8 | |
| `work_mem` | see below | |
| `max_wal_size` | RAM / 2, at most 10 % of the data volume, 2–32 GB | |

`work_mem` is a per-node limit, so total use is roughly
`work_mem × nodes × (workers + 1) × sessions`. The formula takes what is left of the
memory budget after `shared_buffers`, `maintenance_work_mem` and the autovacuum
reservations, divides it by exactly that product, then halves it and rounds down to a
16 MB boundary (`_half_and_align_mb`) for headroom.

### Runtime profiles

| Function | Use |
| --- | --- |
| `get_seeding_settings` | A temporary `conf.d` drop-in while seeding: `synchronous_commit=off`, `autovacuum=off`, long checkpoints, doubled WAL |
| `apply_bulk_session_tuning(connection, cfg)` | `SET LOCAL` for one bulk statement: cluster `work_mem × concurrent_queries`, zeroed parallel costs, `synchronous_commit=off` |
| `get_bulk_copy_env` | `PGOPTIONS` for `psql \copy` subprocesses, which have no SQLAlchemy connection |
| `get_copy_jobs`, `get_index_jobs`, `get_vacuum_jobs` | Concurrency for chunked COPY, CREATE INDEX and `vacuumdb` |

`apply_bulk_session_tuning` uses `SET LOCAL` by default; a plain `SET` would leak to
whichever request next checks the pooled connection out.

## GPU acceleration (`accel.py`)

PostgreSQL has no GPU support, but the element-wise statistics around it (EDA stats,
68-95-99.7 checks, outlier bounds) run on CuPy when a CUDA GPU is present:

```python
from hpldn.backend.common.accel import get_array_module, to_host

xp = get_array_module()
mean = float(to_host(xp.mean(xp.asarray(values, dtype=xp.float64))))
```

- `to_host` must wrap anything leaving the layer (comparisons, f-strings, SQL).
- `HPLDN_DISABLE_GPU=1` forces numpy; `reset_backend()` clears the cached choice in tests.
- AMD GPUs, which CuPy cannot drive, stay on numpy.
- Install CuPy with the `gpu` extra (`pip install 'hpldn[gpu]'`).

## Constants (`constants.py`)

- `LONDON_DISTRICTS` / `LONDON_DISTRICTS_SQL` — the 33 London local authorities as PPD
  spells them.
- `MIN_PLAUSIBLE_PRICE`, `MIN_PLAUSIBLE_FLOOR_AREA_SQM`, `MIN_HABITABLE_ROOMS`,
  `MIN_PLAUSIBLE_ANNUAL_COST` — data-quality floors used by the cleaning steps.
- `TENURE_MAPPING`, `BUILD_FORM_MAPPING`, `TRANSACTION_TYPE_MAPPING`,
  `ENERGY_TARIFF_MAPPING`, `GLAZED_TYPE_MAPPING`, `RATING_CATEGORY_MAPPING` — raw EPC
  strings to normalised codes (None for "no data" variants).
- `FLOOR_LEVEL_MAPPING` / `FLOOR_LEVEL_CANONICAL` — raw EPC floor levels to `BASEMENT`,
  `GROUND`, `01`–`20` and `21+`.
- `API_ROLE_WORK_MEM` — `work_mem` for the read API's database role.

## Tests

`tests/common/test_utils.py`, `tests/common/test_tuning.py`,
`tests/common/test_resource_plan.py` and `tests/common/test_redact_secrets.py` run
without a database.
