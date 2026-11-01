# `hpldn.backend.scripts` — pipeline, provisioning, modelling and data tools

Everything here is either called by the alembic revisions or run as a module:

```bash
python -m hpldn.backend.scripts.<name> --help
```

The root README's [Migrations and seeding](../../../../README.md#migrations-and-seeding)
and [Model training and serving](../../../../README.md#model-training-and-serving)
sections give the operator's view; this file explains how the modules fit together.

## Layout

| Group | Module | Role |
| --- | --- | --- |
| Pipeline | `pipeline_steps.py` | Every load, derivation and feature build the migrations call |
| | `migration_helpers.py` | Engine, cfg and logger for revisions; data-path resolution |
| | `run_pipeline_step.py` | Run one named step by hand |
| Provisioning | `bare_metal.py` | Provision and seed a local PostgreSQL |
| | `export_serving_db.py` | Dump the runtime `production` tables for the single-host deployment ([`cloud/vps`](../cloud/vps/README.md)) |
| Source fetchers | `fetch_ptal_centroids.py` | TfL PTAL cell centroids or polygons |
| | `fetch_lsoa_boundaries.py` | ONS LSOA 2011 boundary polygons as WKT |
| | `fetch_lsoa_msoa_lookup.py` | ONS LSOA11 → MSOA11 lookup |
| | `fetch_ukhpi.py` | London UK House Price Index from 1995 |
| | `fetch_linking_sources.py` | The open sources 0028 links sales with: NSUL, LIDS, OS Open Roads, the OSM extract, GLA planning applications |
| | `fetch_ofsted.py` | Ofsted inspection files and DfE GIAS school locations (0033) |
| Sale linking | `link_precision.py` | Measures each sale-linking method's precision against ground truth, and gates it (`migrations/sale_linking.py`) |
| Modelling | `hpp_rf.py` | Random Forest model and the shared feature/split/metric code |
| | `hpp_cb.py` | CatBoost model |
| | `feature_cache.py` | Parquet cache of `hpp_cb`'s engineered training frame (`tmp/cache/features`) |
| | `cb_fit_gate.py` | Gate (scoped MdAPE within +0.1 pp) and sweep for faster `hpp_cb` fit strategies |
| | `hpp_inference.py` | Prices a home on a future day with a saved `hpp_cb` or `hpp_rf` model |
| | `hpi_projection.py` | Projects the UK HPI forward, so a sale date beyond the published index moves the price |
| | `local_index.py` | Local house price index (postcode district × property type) from repeat sales |
| | `ofsted_features.py` | Ofsted school features of a home on a date (0034 and inference) |
| | `model_ablation.py` | Feature ablation sweeps over either model |
| | `data_visualization.py` | SHAP importance, correlation heatmap, price-response plots |
| Frontend data | `build_london_map.py` | Generates `frontend/src/data/londonGeo.ts` |
| | `backtest_heuristic.py` | Measures the read API's heuristic estimator against real sales |

## Pipeline steps (`pipeline_steps.py`)

Every public step has the signature `(engine, cfg=None, log=None)`, so migrations and
`run_pipeline_step` call them uniformly; `log=None` prints timestamped progress instead.
The conventions they share:

- **Tuned connections.** Heavy statements run inside
  `common.tuning.apply_bulk_session_tuning`, on connections the step opens itself.
- **Rebuild, don't append.** Build steps `DROP ... IF EXISTS` then `CREATE`, so re-running
  after a failure produces the same result. Idempotent updates use
  `IS DISTINCT FROM` guards; inserts use `ON CONFLICT DO NOTHING`.
- **Parallelism PostgreSQL will not give.** A SELECT feeding an INSERT is never
  parallelised, so expensive derivations use `CREATE TABLE AS` into UNLOGGED build
  tables, and per-row spatial joins are split client-side into `mod(id, jobs)` chunks
  across `get_spatial_join_jobs()` connections.
- **Leakage safety.** Every per-sale feature uses only data dated strictly before the sale
  (comparables, momentum, previous sale) or published before it (UKHPI, IoD with an
  embargo).
- **Spatial work in metres.** Points are projected from EPSG:4326 to EPSG:27700 so KNN
  (`<->`) and `ST_DWithin` use true distances.

Steps, grouped by the revision that calls them:

| Revision | Steps |
| --- | --- |
| 0003 (through `seed_registry`) | `refresh_ukhcs_table_from_extract`, `create_ptal_2023_table`, `create_ptal_coords_table`, `create_ptal_cell_polygons_table`, `create_staging_rivers_table`, `create_lsoa_boundaries_table`, `create_lsoa_msoa_lookup_table`, `create_iod_lsoa_raw_table`, `create_london_green_spaces_tables` |
| 0004 | `create_ukhpucl_indexes` |
| 0005 | `create_london_houses_table`, `create_address_table`, `add_poststreet_to_address_table`, `create_london_houses_condensed_table`, `load_ppd_table`, `load_epc_table_condensed`, `create_epc_fused_staging_table`, `insert_epc_data_into_db`, `insert_inspire_polygon_data` |
| 0006 | `create_epc_addrkey_table`, `create_ppd_uprn_linkage_table`, `mint_address_rows_for_unresolved_uprns`, `insert_fused_ppd_rows`, `verify_epc_ppd_fusion_v2` |
| 0007 | `apply_data_quality_fixes`, `promote_staging_to_production`, `promote_additional_serving_tables_to_production` |
| 0008 | `add_floor_level_to_epc_table`, `backfill_epc_table_for_fusion_gap`, `backfill_epc_table_from_london_history` |
| 0009 | `create_boe_rate_table`, `create_london_stations_table`, `create_ukhpi_table`, `create_mortgage_rates_table` |
| 0010 | `create_address_coordinates_table` |
| 0011 | `create_property_lsoa_table`, `create_iod_releases_table`, `create_iod_lsoa_spine_table`, `create_iod_features_table` |
| 0012 | `create_ptal_london_view`, `create_ptal_features_table` |
| 0013 | `create_station_proximity_features_table`, `create_river_proximity_features_table`, `create_london_green_spaces_tables` |
| 0014 | `create_comps_features_table`, `create_neighbourhood_features_table` |
| 0015 | `create_lsoa_price_trend_features_table` |
| 0016 | `verify_epc_ppd_fusion_v2` (again, now the view exists), `verify_epc_ppd_fusion_view_coverage` |
| 0017–0034 | Re-run the builders above through `chain.load_revision` and `migrations/link_carry.py`, plus their own steps in `migrations/` (`sale_linking`, `address_search`, `ofsted`, `cpih`) |
| Not called by the chain | `drop_all_tables`, `populate_ukhcs_table`, `create_london_houses_materialized_view`, `create_api_readonly_role`, `append_new_staging_rows_to_production`, `rebuild_production_uprn_lookup` |

The first `staging.ukhcs_table` load in 0003 is a registry `\copy`; `populate_ukhcs_table`
is its standalone equivalent.

### Running a single step

```bash
python -m hpldn.backend.scripts.run_pipeline_step --list
python -m hpldn.backend.scripts.run_pipeline_step create_comps_features_table
```

Only the steps registered in `run_pipeline_step.STEPS` are exposed. Many are slow and
destructive; prefer `alembic upgrade`, which records what has run.

### Data paths (`migration_helpers.py`)

`resolve_data_path(cfg, key)` returns `hpldn.backend.migrations.inputs.resolve(key)`:
the key's path in `migrations/inputs.yaml` under `HPLDN_DATA_DIR`, unless `staging.yaml`
overrides it under `data:` (the `default_relative` argument revisions still pass is
ignored); `require_data_path` also checks it exists and raises
`DataSourceMissing`, naming the key, the path, what reads it and, where the input has
one, the `fetch:` command from `inputs.yaml` (`FETCH_COMMANDS`):

| Input key | Produced by |
| --- | --- |
| `ptal_centroids_csv_fp`, `ptal_polygons_csv_fp` | `fetch_ptal_centroids --geometry centroid\|polygon` |
| `lsoa_msoa_lookup_csv_fp` | `fetch_lsoa_msoa_lookup` |
| `lsoa_boundaries_csv_fp` | `fetch_lsoa_boundaries` |
| `ukhpi_csv_fp` | `fetch_ukhpi --from 1995-01-01 --to <month>` |
| `nsul_zip_fp`, `lids_dir`, `open_roads_gpkg_fp`, `osm_pbf_fp`, `planning_applications_csv_fp` | `fetch_linking_sources nsul\|lids\|open-roads\|osm\|planning` |
| `ofsted_dir`, `gias_dir` | `fetch_ofsted` |
| `ons_pipr_london_rents_csv_fp` | `python -m hpldn.backend.migrations.london_rents extract <ONS CSV>` |

The fetchers are standalone so `alembic upgrade` never needs the network. They validate
what they download (row counts, id ranges, date coverage) before writing, and convert
GeoJSON to WKT with string formatting, so no GIS Python library is required.
`fetch_ukhpi` overwrites the configured UKHPI file in place; revision 0009 loads it.

## Provisioning

| Script | Target | What it does |
| --- | --- | --- |
| `bare_metal` | Local PostgreSQL | Resolves the URL like `migrations/env.py`, creates the database and schema, checks `db_init.sql` prerequisites and every dataset (`--check`), optionally bulk-loads ukhpucl (`--load-raw`), runs the migrations, then `--finalize`s (LOGGED tables, synchronous_commit check, parallel ANALYZE) |
| `export_serving_db` | `tmp/serving/<date>/` | Dumps the 13 tables the API and `hpp_inference` read (`SERVING_TABLES`; `epc_table` pruned to the 15 columns the pricing SQL reads, the `london_houses` matview as a plain table) from one REPEATABLE READ snapshot as unqualified DDL + zstd binary COPY + post-load indexes, with a manifest and checksums; `--plan` writes nothing. `tests/test_serving_tables.py` fails when runtime code reads a table the dump does not ship |

`bare_metal` pins `HPLDN_DB_URL` so every subprocess targets the same database, passes
passwords through `PGPASSWORD` rather than argv, and resumes at the revision that failed
when re-run. See [`../cloud/README.md`](../cloud/README.md) for the hosts.

## Models

### Shared code (`hpp_rf.py`)

`hpp_rf` holds the pieces both models use, and `hpp_cb`, `model_ablation` and
`data_visualization` import them rather than copying them:

| Stage | Functions |
| --- | --- |
| Load | `load_dataset` (pandas, or cuDF with `use_gpu`), `dataset_sql`, `--population epc_matched|all`; columns coerced to their declared view types (`VIEW_COLUMN_KINDS`, `_coerce_chunk_dtypes`, shared with `hpp_cb`) |
| Features | `engineer_features` (dates, ratios, interactions, distance to Charing Cross, "UNKNOWN" fills for every raw categorical, a three-state `is_maisonette`, causal rolling and same-street statistics) |
| Data contract | `check_core_feature_availability`: per-split missing rates of `CORE_FEATURES`, failing above 50 % |
| Split | `split_dataset` → `time_series_split`, `grouped_time_series_split`, `purged_embargo_time_series_split`, `time_series_cv_folds`, `spatial_holdout_split`, `recency_windowed_time_series_split` |
| Train-only statistics | `add_postcode_price_stats`, `add_postcode_sector_price_stats` (expanding-window out-of-fold for train rows) |
| Encode | `impute_and_encode` → matrices with `FEATURE_MATRIX_COLUMNS` (`FEATURE_COLUMNS` plus `MISSING_FLAG_GROUPS` indicators) |
| Metrics | `compute_metrics`, `compute_metrics_by_price_band`, accuracy within 10 % by band and property type, `residual_slice_report` |
| Evaluation | `backtest_expanding_window`, `evaluate_spatial_generalisation`, `evaluate_two_stage_mixture` |
| Weights | `sample_weight_for_training` (`--sample-weight none|sqrt|full`, default `none`) |

Leakage rules: split first, then fit every statistic, median and vocabulary on training
rows only; features computed before the split must be causal (strictly earlier sales).

**Population.** `production.london_houses` keeps every sale, but EPC fields are NULL on
~53 % of rows, almost all before 2009. Both scripts therefore load `WHERE epc_matched` by
default (`--population epc_matched`).
`CORE_FEATURES` is `EPC_CORE_FEATURES` (total floor area, habitable rooms, energy rating)
plus `LOCATION_CORE_FEATURES` (property type, district, latitude, longitude). With
`--population all` the EPC core features are **optional per training row but mandatory in
the saved model**: `check_core_feature_availability(..., population="all")` reports their
missingness on the `train` split instead of failing, while `val`/`test` (and every
location core feature) keep the 50 % ceiling. A cross-validation or backtest fold
(`is_final_fit=False`) is exempt on its validation block too, since fold 1 of a
walk-forward split can predate certificates entirely.

The model scripts read the database from `common.db.get_model_db_url`: `HPLDN_DB_URL`,
then `POSTGRES_DEVELOP_*` (hpdbldn, which the read API serves), then `POSTGRES_TESTING_*`.
It logs the database chosen, `hpp_cb` names it in its "Loaded N rows" line and stores it
in the sidecar as `database`.

**`is_maisonette`** is 1 or 0 for a flat with a known flag, 0 for a house, and NaN for a
flat whose flag is unknown, with a `maisonette_missing` indicator, so filling unknowns
with False cannot turn every post-2024 maisonette into a flat and hide the shift from the
guards.

`python -m hpldn.backend.scripts.hpp_rf --limit 20000 --n-estimators 5` is the smoke run.

On `--device gpu`, the Random Forest path keeps data in cuDF/cupy and trains with cuML.
cuML serialises the forest to host memory, so an unset `--max-depth` becomes
`GPU_MAX_DEPTH_DEFAULT` there.

### CatBoost (`hpp_cb.py`)

Uses `hpp_rf`'s splits and feature engineering but its own `prepare_features`, which keeps
categoricals as strings and adds mortgage-rate and IoD features. Differences worth
knowing:

- Two-pass fit: early stopping on a tail carved from **train** (`_carve_eval_mask`), then a
  refit on all of train at the scaled best iteration with the realised learning rate
  pinned. Validation and test are never used for fitting.
- Guards drop features whose missingness shifts between train and scored rows
  (`--availability-shift-limit`) and missing flags that merely encode sale year. A guard
  that would drop one of `CORE_FEATURES` raises instead, and every dropped feature is
  listed in the results under `dropped_features` with its reason (and, for the
  availability guard, its train and scored missing rates).
- Under `--population all` (`prepare_features(population="all", is_final_fit=...)`): the
  availability guard skips `EPC_CORE_FEATURES`; an `epc_missing` indicator (no floor area
  or rating) is added and exempt from the sale-year-proxy and availability-shift flag
  guards; a CV fold whose train rows carry no EPC core feature drops it for that fold
  with reason `unavailable_in_fold_train` (logged per fold), while the final fit still
  raises. `--nan-mode` defaults to `native` there (`resolve_nan_mode`; `impute` otherwise)
  and the resolved value is saved in the sidecar. The final fit's availability guard
  still drops non-core features whose missingness shifts (on hpdbldn: 57, including the
  EPC-derived, IoD and mortgage-rate columns), which is why that model scores worse than
  an `epc_matched` one.
- `--target price|log_price|deflated`; `deflated` divides by a UKHPI deflator so trees can
  extrapolate. Predictions get per-price-band Duan smearing.
- Training rows above a plausible price per m² are always dropped.
- `--model-out` names a bundle directory (`models.artifact`, see `models/README.md`): the
  canonical `model.cbm`, `meta.json` carrying the preprocessing state, the smearing
  factors and the high-value threshold, `expert.cbm` when a `--two-stage` expert was
  fitted, probe rows and a manifest, signed with the local key; `--model-in` alone scores
  with `run_prediction`, which applies both through the same `predict_with_adjustments`
  the training run reports from, and refuses a model whose expert file is missing; both
  flags together warm-start.
- `--two-stage` adds a high-value expert to any protocol (`--backtest`,
  `--spatial-generalisation` or a single split).
- The sidecar also records `population`, `train_period` (first and last training sale)
  and `database`, which `hpp_inference` reads.

#### Training speed

- **Resource plan.** `common.hardware.plan_resources` derives CatBoost's `task_type`,
  `devices` (every usable GPU), `gpu_ram_part`, `pinned_memory_size`, `thread_count`
  and CPU `used_ram_limit`, the fold concurrency and the CuPy chunk size from what the
  process may use (affinity, cgroup limits, free VRAM). Nothing is hard-coded;
  `--devices`, `--gpu-ram-part`, `--pinned-memory-size`, `--thread-count`,
  `--used-ram-limit`, `--fold-concurrency` and `--chunk-rows` override it, and
  `HPLDN_DISABLE_GPU=1` turns `--device auto` into CPU so one command runs on a GPU box,
  a CPU-only box and the A1.Flex shape. `python -m hpldn.backend.common.hardware`
  prints the plan.
- **Profiling.** `--profile-json PATH` writes every stage's wall time, peak RSS and
  peak VRAM (`common.profiling.stage`: load, each feature step, each fold's pass 1 /
  pass 2 / predict, the side indices), the plan, the headline metrics and the
  engineered frame's fingerprint (a content hash: equal hashes mean an identical frame).
  `--results-json PATH` saves the results.
- **Vectorised features.** The rolling and same-street statistics run in polars
  (`rolling_quantile_by(...).over(group)`, bit for bit the old per-group pandas loop);
  the size-matched comparables run per (type, month) block through
  `common.accel.StreamingExecutor` (a KD-tree on the CPU, a tiled radius search on the
  GPU, then the size band, radius rule, local-index move and median as arrays); the local
  index's ~1,600 small solves are batched (`local_index._solve_batch`, on the GPU in
  training, NumPy on the API host). The pre-vectorisation implementations live in
  `tests/ml/_training_speed_reference.py`, and `tests/ml/test_training_speed_parity.py`
  holds the vectorised code to them.
- **Work done once.** The engineered frame is cached as parquet in `tmp/cache/features/`
  (`feature_cache`, keyed by alembic revision, row count, max `ppd_id`, the feature code's
  hash and the parameters that shape the frame; `--feature-cache on|off|refresh`).
  Categoricals are cast to CatBoost's strings once (`add_canonical_categoricals`, the
  `__cat__<col>` columns), and each fit builds one `Pool` whose pass-1 rows and eval
  carve are `Pool.slice`s (`--quantize-once` also shares the borders; gated).
- **Gated fit strategies.** `--fold-refit final-only` (folds score the early-stopped
  pass-1 model; only the final holdout model is refitted), `--quantize-once` and the
  `--max-ctr-complexity`/`--one-hot-max-size` sweep are adopted only if
  `python -m hpldn.backend.scripts.cb_fit_gate` passes them (scoped MdAPE within +0.1
  pp of the baseline backtest). Folds run `fold_concurrency` at a time (one per GPU, or
  one per 8 CPU threads, capped by RAM); CatBoost's results do not depend on its thread
  count, so concurrent folds score as sequential ones do.

`hpp_rf --model-out` saves one joblib artefact: the forest, encoder, train medians,
postcode and sector lookups with their train-wide fallbacks (`price_stat_globals`),
`log_target`, `population` and `train_period`. `hpp_rf.load_model` reads it and
`predict_with_saved_model` scores rows through `build_feature_matrix`, the same matrix
layout `impute_and_encode` builds for training.

### Inference (`hpp_inference.py`)

Neither training script can price a sale that has not happened: every
`production.london_houses` column is computed per sale, mostly from the sales before it.
`hpp_inference` builds that row for a given address and sale date, then scores it with the
saved model's own preprocessing (`hpp_cb.prepare_features_from_saved_model(...,
engineered=True)` or `hpp_rf.predict_with_saved_model`). Nothing is refitted.

```bash
python -m hpldn.backend.scripts.hpp_inference price --model tmp/models/model \
    --postnumber 18 --poststreet "BARCLAY ROAD" --posttown CROYDON --postcode "CR0 1JN" --date YYYY-MM-DD
python -m hpldn.backend.scripts.hpp_inference fit-projection --model tmp/models/model --write
python -m hpldn.backend.scripts.hpp_inference fit-local-index --model tmp/models/model --write
python -m hpldn.backend.scripts.hpp_inference evaluate --model tmp/models/model \
    [--from 2025-08-01] --write-calibration --mask-epc [--rolling-parity]
```

`hpp_cb --model-out` writes `projection.json` and `local_index.json` into the bundle itself;
the two `fit-*` commands are for a model saved before that, or to refit after the index or
the view is refreshed. `evaluate` defaults `--from` to the day after the model's
`train_period` and refuses an earlier start (`--allow-in-sample` overrides), and prices
every sale in the window (`--sample 0`) so the district strata are populated.

Both read the database `common.db.get_model_db_url` resolves, like the training scripts.
A `.rfm` or `.joblib` model is read as a Random Forest, which is a pickle: it loads only with
`--trust-pickle`, offline, and the API never serves one.

| Step | What it does |
| --- | --- |
| Address | `match_address`: same postcode and house number or name (case and punctuation ignored); rows whose street matches **or is blank** are one home; the town does not narrow the match |
| Date | `validate_sale_date`: after today in Europe/London, at most `MAX_HORIZON_DAYS` (five years) ahead |
| Static features | From the home's latest sale (coordinates, lot area, station, river, PTAL, LSOA/MSOA, IoD with `iod_release_age_years` aged to the sale date); a home that never sold takes them from the latest sale in its postcode (else the sector's sale nearest its coordinates), its own coordinates, and a property type from `resolve_property_type`: its certificate, then its address text (`FLAT`/`APARTMENT`/`UNIT`, or a number in a named building), then the postcode's modal sold type |
| As of the sale date | The certificate in force, `is_maisonette` from it, the previous sale (price floor applied), Bank Rate, the UK HPI published by then, mortgage rates (in `hpp_cb`) |
| No usable certificate | `estimate_epc`: certificates in force on the sale date of at least `MIN_EPC_NEIGHBOURS` (5) same-type homes from the tightest of the same building, street + postcode, postcode, sector, and district within `COMPS_RADIUS_M`; median floor area and rooms, modal rating, age band, build form, tenure (and floor level for a flat). The row gets `epc_matched=False`, `epc_age_at_sale_days=NaN`, `epc_missing=1`; with no big-enough pool the EPC fields stay NaN (`level="none"`). `PricePrediction` carries `epc_source`, `estimate_level`, `estimate_neighbours` and `estimated_from`. `NotEnoughData` only when there is no location or no way to tell the type |
| As of the evidence date | Comparables, neighbourhood, MSOA momentum (the SQL definitions in `pipeline_steps`), rolling area comparables and same-street sales (`hpp_rf`'s definitions, over the model's population) |

The **evidence date** is the sale date, or the day after the latest sale held when the sale
date is later. A window ending after the last recorded sale is partly empty, which is not
what the model was trained on. `iod_release_age_years` and `years_since_prev_sale` are
measured to the evidence date too: they are the only features that would otherwise grow
without bound as the horizon extends, and a tree asked for a value past its training range
answers with the edge of it.

**A future sale date is priced as a nowcast moved along a local index.** The row is built,
and the model scored, as of the evidence date -- where every feature is in its training
range and where `evaluate` measures it. The price is then moved to the sale date along the
home's **local price index** (`local_index`): a repeat-sales index per postcode district
and property type, estimated as an offset from its borough (itself an offset from the
London UK HPI for the type), shrunk month to month so a district with few sales follows its
borough, interpolated between months so the level moves every day, and projected past the
last published month by `hpi_projection`'s damped blend fitted to that series -- or its
borough's or London's, whichever is the first in the chain that beat carrying forward.
Asking the model for the future date directly does not work: its `rel_*` features divide
evidence-date comparables by the sale-date index, and cancelled any projected growth.
`PricePrediction.projection_uplift` is the move and `index_area` whose index made it.

A home with its own previous sale then has that sale, moved to the sale date along the same
index, blended in log space with the model where `evaluate` measured it helps
(`repeat_sale_blend` in the calibration: a weight per bucket of months since the sale,
cross-fitted, and enabled only when it cuts MdAPE on repeat-sold homes by at least 2 %).

The Bank Rate and mortgage rates are carried forward from their last published values, and
the UK HPI the model deflates by is projected to the evidence date by the bundle's `projection.json`.
A model without that sidecar has it fitted from the live index at load, with a warning.

`EvidenceStore` loads the last 73 months of sales once, in a few seconds, and computes all
of that in memory per request. Distances use a local WGS84 plane scaled to the National
Grid, within about 0.1 % of `ST_Distance` in EPSG:27700 over the 2 km searches.

`evaluate` rebuilds sampled real sales from address and date alone (evidence date = sale
date), compares every rebuilt feature with the stored view row (`parity`), and prices
them (`median_absolute_percentage_error`, `within_10_percent`, `within_20_percent`,
`log_ratio_quantiles`). It also writes `strata`: the same quantiles measured per price
band, property type and evidence thickness, and for each of those keys' parents, whenever
at least `MIN_STRATUM_SALES` (120) evaluation sales fall in them. `--write-calibration`
saves those figures to the bundle's `calibration.json` (re-hashing and re-signing it), where
pricing reads its range and its accuracy line.

Before those, `evaluate` writes **area strata**: `district:<code>|<type>` and
`borough:<name>|<type>` (the borough read from the LSOA name), from at least 20 sales each,
their quantiles shrunk toward the parent's (district toward its borough, borough toward the
type) as `(n q + k q_parent) / (n + k)`, with `k` chosen by cross-fitted quantile loss
(`area_prior_sales`). The same model reads SW11 terraces 3.7 % high at the median and HA0
terraces 3.6 % low, so the area's own bias recentres the price and its own margins set the
range.

A price takes the narrowest stratum measured for it -- its district and type, its borough
and type, then band+type+evidence, band+type, band, type, and finally the global block --
so a 34 m² flat and a £2m detached house need not get the same band. The quantiles are re-expressed around `p50`, which corrects the model's standing
bias (the served model reads about 5.5 % high at the median), and widened in quadrature by
the index projection's cross-validated error at the price's horizon -- the home's own
district or borough series where that was projected, London's otherwise.

`fit-local-index` builds the local index from every repeat-sale pair in
`production.london_houses` (same home, same type, held at least six months, first sale not
a new build), chooses the month-to-month shrinkage per level by held-out pairs, fits the
damped blend with its gate to every borough and district series, and `--write` stores it
in the bundle's `local_index.json`. The levels are rebuilt from the live table whenever the
evidence loads (a few seconds); only the hyperparameters come from the sidecar. Without it
the local levels are still used, but growth beyond the index is London's.

`fit-projection` fits the UK HPI forward projection a future sale date is priced with, and
`--write` stores it in the bundle's `projection.json`. Per property type, in log space, it
blends the OLS slope of the recent index with the long-run CAGR, the first decaying into
the second, the whole path scaled by a shrinkage factor. The four hyperparameters are
grid-searched against a blocked cross-validation of the index: the origin timeline is cut
into five contiguous blocks and, for each, the parameters are chosen on the origins outside
it (embargoed by the horizon, so no training origin's outcome reaches into the test block)
and scored on the block itself. A single held-out tail would be one overlapping five-year
window -- barely one independent observation at the longest horizon.

A type is projected **only where it beat carrying the index forward at every horizon**;
otherwise it carries forward, as everything did before. On the London index all four types
pass, by a widening margin: at five years the mean absolute log error is 0.147 against
0.187 for flats and 0.130 against 0.228 for detached houses. That same per-horizon error
table is what widens the price range with the forecast horizon.

The uplift is modest for flats and larger for houses, because that is what the index says:
London flats stood at 431,036 in June 2026 against 428,549 ten years earlier. Over five
years the projection adds about 4.5 % for a flat and 13.6 % for a detached house.

`--mask-epc` (`evaluate_masked`) also prices recent certified sales with their own
certificates removed, once from the surrogate and once with the EPC fields missing, and
stores the result as the calibration's `estimated` block: the `strategy` used for homes
without a certificate (`surrogate` unless missing inputs beat it by more than 5 % MdAPE),
its `log_ratio_quantiles` and `within_10_percent` (the wider range an estimated home
shows), the pool levels used, and both measurements under `comparison`. Measured on 3,000 sales after the training period: the rolling,
same-street, previous-sale, certificate, MSOA and market features match the view
exactly; comparables match within 1 % for about 96 % of sales and the neighbourhood
columns for 99 %. The gap is ties among flats that share one building's coordinates,
where PostGIS's `LIMIT` picks arbitrarily too.

### Feature ablation (`model_ablation.py`)

Measures hold-out error with features present versus absent:

| `--strategy` | Runs |
| --- | --- |
| `forward` (default) | Top `--baseline-features` SHAP features, then one run per next-ranked feature up to `--max-ablation-features` |
| `loo` | All features, then one run per feature removed |
| `greedy` | From the same baseline, each step adds whichever remaining feature lowers validation RMSE most |

Preprocessing runs once (`prepare_splits`); each run fits only the model on a column slice
of the real matrix, so it matches what the model script trains. CatBoost hyperparameters
use a `--cb-` prefix.

Each run writes a log (`baseline_ablation.log`, `ablation_run_<n>.log`, `loo_*`,
`greedy_*`; CatBoost logs are prefixed `catboost_`) with key/value header lines and
`[features]`, `[metrics.<split>]`, `[price_bands.<split>]` and, for non-baseline runs,
`[delta_vs_baseline]` sections. Deltas are **baseline minus run**, computed from the
parsed files, so positive means the run has lower error. Select features on the `val`
deltas; read `test` once, at the end. `test_model_ablation.py` round-trips the writer and
parser.

The ranking CSV (`--importance-csv`) comes from `data_visualization`; use the CatBoost
ranking for `--model catboost`, because the two models' columns differ.

### Diagnostics (`data_visualization.py`)

Draws three plots over each model's prepared training matrix, not the raw view: SHAP
importance (native CatBoost TreeSHAP, or `shap.TreeExplainer` for the forest, imported
lazily), a clustered Spearman correlation heatmap with near-constant columns excluded,
and binned median price-response panels chosen by price correlation with near-duplicates
skipped. It prints a JSON report of the top features and redundant pairs.

## Frontend data

- **`build_london_map`** (read-only, hpdbldn) builds one SVG path per covered area from
  Doogal's inner-district KML and Voronoi cells of outer postcodes, clipped to the union
  of London's LSOA polygons, cleaned into an exact coverage with `ST_CoverageClean`,
  simplified together at about 40 m, and fitted to a 640-wide viewBox, plus the Thames. It
  fails without writing if a covered area has no shape or the file would exceed 400 KB.
  Re-run it rather than editing `londonGeo.ts`.
- **`backtest_heuristic`** (read-only, through the API's `POSTGRES_API_*` settings) prices
  every sized sale of the latest months with `HeuristicEstimator`. It reports the share
  within 10 % and 20 % of the price paid, and the similar-count distribution the API's
  confidence thresholds are calibrated on (`api/market/estimator.py`). The figures are
  in-sample.

## Tests

`tests/ml/` covers the models (`test_hp_rf.py`, `test_hp_cb.py`, `test_hpp_inference.py`,
`test_hpi_projection.py`, `test_local_index.py`, `test_model_ablation.py`,
`test_model_artifact.py`, `test_data_visualization.py`, `test_ptal.py`,
`test_data_quality.py`, `test_epc_floor_level_migration.py`,
`test_training_speed_parity.py`, `test_price_golden.py`); `tests/infra/` covers
provisioning (`test_bare_metal.py`), the fetchers (`test_fetchers.py`), sale linking
(`test_sale_linking.py`) and the serving dump (`test_export_serving_db.py`).
