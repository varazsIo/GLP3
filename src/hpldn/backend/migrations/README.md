# `hpldn.backend.migrations` — the alembic seeding chain

The database *is* the pipeline state. Every load, clean, fusion and feature build is a
numbered alembic revision, and `alembic upgrade head` on an empty PostgreSQL database
produces `production.london_houses`. The root README's
[Migrations and seeding](../../../../README.md#migrations-and-seeding) section covers
operating the chain; this file covers how it is built and why.

## Layout

```
migrations/
├── env.py                  # alembic environment: URL resolution, pending-input check, one transaction per revision
├── inputs.yaml             # manifest of every input file: path, revisions, sha256, source, licence, fetch
├── inputs.py               # the only resolver: resolve(), check(), paths, pull/push of Tier A to OCI
├── script.py.mako          # template for new revisions
├── chain.py                # CHAIN_HEAD, BASELINE_HEAD, NO_DOWNGRADE, no_downgrade(), load_revision()
├── landing_ddl.py          # raw landing-table DDL and the staging.hpldn_* functions (0002)
├── seed_registry.py        # SeedSource registry, pre-flight and loaders (0003)
├── epc_fusion_sql.py       # EPC → London-address derivation and fusion inserts (0006)
├── london_houses_view.py   # the definitions of production.london_houses (0016/0020, 0025, 0034)
├── london_rents.py         # ONS private rents CSV → the ons_pipr_london_rents_csv_fp input (0026)
├── cpih.py                 # ONS CPIH 12-month rates → a chained monthly index (0032)
├── ofsted.py               # Ofsted / GIAS file registry, per-era column maps and readers (0033)
├── address_search.py       # production.address_search, the suggestion index (0021–0024, 0031)
├── sale_linking.py         # Price Paid sale → UPRN tiers, ledger, ODbL table, UPRN-less certificates (0027–0029)
├── link_carry.py           # carry a linking run into staging/production and rebuild downstream (0028, 0029)
├── address_parse_sql.py    # certificate address parser and production.hpldn_unit_key (0030)
└── versions/               # 0001 … 0034, loaded by path (not a package)
```

Every file the chain reads is listed in `inputs.yaml` (path relative to
`$HPLDN_DATA_DIR`, the revisions that read it, tier, size, SHA-256, source, licence and
the fetch command where there is one); a `data:` entry in `staging.yaml` overrides one
path. Four small hand-derived files are Tier A inputs under
`$HPLDN_DATA_DIR/migration_inputs/` (`ppd_uprn_linkage_tierA.csv`, the 54,137
hand-produced PPD/UPRN links; `ons_pipr_borough_rents.csv`, 0023;
`ons_pipr_london_rents.csv`, 0026; `ons_cpih_annual_rate.csv`, 0032), pinned by sha256
in `inputs.yaml` and backed up to the `$HPLDN_INPUTS_BUCKET` OCI bucket
(`python -m hpldn.backend.migrations.inputs pull|push`). `inputs check [--hash]` reports
every missing or changed input at once.

Configuration is `src/hpldn/backend/alembic.ini` (`script_location = migrations`). Run
from the repository root:

```bash
alembic -c src/hpldn/backend/alembic.ini upgrade head
alembic -c src/hpldn/backend/alembic.ini current
```

## Target database

`env.py` resolves the URL through `common.db.get_migration_db_url`:

1. `HPLDN_DB_URL`, set by `bare_metal` so every subprocess hits the same database;
2. `POSTGRES_DEVELOP_*` — `hpdbldn`, the from-empty target;
3. `POSTGRES_TESTING_*` — a fallback.

A database populated before this chain is not rebuilt: it is stamped at
`chain.BASELINE_HEAD` (`0016`), since pointing 0001–0016 at it would drop
`production.london_houses`; 0017 onwards run on top of a built database.
`chain.CHAIN_HEAD` is `0034`.

Revisions obtain their resources from `scripts/migration_helpers.py`:
`get_migration_engine()`, `get_migration_cfg()` and `get_migration_logger()`.

## The chain

| Rev | Builds |
| --- | --- |
| `0001` | The 10 `AppBase` tables in `staging`, via `models.staging.migration_metadata()` |
| — | `staging.ukhpucl` bulk load by `StagingCopy`, out of band (`bare_metal --load-raw`) |
| `0002` | Landing-table DDL and the four IMMUTABLE `staging.hpldn_*` functions |
| `0003` | Every raw source through `seed_registry.run_sources` |
| `0004` | `staging.ukhpucl` indexes |
| `0005` | London staging spine: `london_houses`, `address_table`, `ppd_table`, EPC tables, categories, INSPIRE parcels |
| `0006` | EPC/PPD fusion for sales after `ukhpucl` ends, plus verification |
| `0007` | Data-quality fixes on staging, then the promote to `production` + `uprn_lookup` |
| `0008` | `floor_level`, EPC backfills, domain bounds, age-band normalisation, `oldnew` |
| `0009` | `boe_rate`, `london_stations`, `ukhpi`, `mortgage_rates` |
| `0010` | `production.address_coordinates` |
| `0011` | Point-in-LSOA, `lsoa_msoa_features`, Indices of Deprivation |
| `0012` | `staging.ptal_london`, `production.ptal_features` |
| `0013` | Station and river proximity, green-space reference tables |
| `0014` | `comps_features`, `neighbourhood_features` |
| `0015` | `prev_sale_features`, `lsoa_price_trend_features` |
| `0016` | `production.london_houses`, its five indexes and the parity guards |
| `0017` | Back-fill of the May–Oct 2024 sales the UCL extract was cut before (`staging.ppd_uprn_linkage_backfill`) |
| `0018` | Post-fusion refresh: London EPC addresses, `poststreet` from PPD and EPC, parcels for every sold UPRN |
| `0019` | Propagation to production, history certificates for new addresses, EPC `property_type` |
| `0020` | Rebuild of every per-sale feature (0010–0015) and the view, with content guards |
| `0021` | `production.address_search`, the relation `GET /addresses/search` reads (`pg_trgm`) |
| `0022` | `address_search` widened to homes without an energy certificate |
| `0023` | `production.borough_rents`: ONS mean rent per borough (dropped by 0026) |
| `0024` | `address_search` street hints: a home held without a street is found under its postcode's street |
| `0025` | 1995–2023 Price Paid history (`pp-complete.csv`), the view's later-certificate fallback, `street_key` and `epc_after_sale`, IoD back-fill; features, view and `address_search` rebuilt |
| `0026` | `production.london_rents`: ONS Price Index of Private Rents for London and its 32 boroughs from January 2015, 9 measures (overall, 1–4+ bed, 4 property types); drops `borough_rents` |
| `0027` | `ppd_table.transactionid` and `link_method` (sale provenance), NSUL and the HMLR PPD→UPRN lookup, HMLR audit |
| `0028` | Link unheld London sales to real UPRNs (`sale_linking`), carry the ledger, rebuild features, view and `address_search` |
| `0029` | More links: `osm_polygon_isolated`, `flat_sequence`, `house_bracket`; UPRN-less certificates resolved into the gazetteer; OSM links copied to `staging.ppd_uprn_link_odbl` |
| `0030` | `address_table.unit` (the SAON), the certificate address parser, address rows for the certified London UPRNs without one |
| `0031` | `production.address_search` keyed on the unit: one suggestion per flat, `unit`/`unit_key` columns |
| `0032` | `production.cpih`: ONS CPIH 12-month rate (L55O) from January 1989, with an index chained from it (2015 = 100) for the price card's inflation-corrected figures |
| `0033` | Ofsted state-funded school inspections Sep 2005–Aug 2026 and DfE GIAS (`scripts/fetch_ofsted.py` → `data/ofsted/`, `data/gias/`): `staging.ofsted_releases`, `ofsted_inspections_raw`, `gias_establishments_raw`, `gias_links_raw`; `production.ofsted` (harmonised 1–4 `rating_score` + `grade_basis`) and `production.ofsted_rating_spells` (rating in force over time, academy successors inheriting) |
| `0034` | `production.ofsted_features`: per-sale nearest primary/secondary rating, distance and age, good+/outstanding counts, IDW scores (`scripts/ofsted_features.py`), leakage guard; view rebuilt with the 12 `ofsted_*` columns (`CREATE_VIEW_SQL`, 107 columns) |

Every feature builder reads `production.ppd_table`, never the view, so the dependency
graph is acyclic and the view is built last.

## Rules every revision follows

**Re-runnable.** Online mode uses `transaction_per_migration=True`, and alembic only
stamps a revision once it completes, so re-running after a failure is the normal
recovery path. Loads truncate first, fusion inserts use `ON CONFLICT DO NOTHING` or
`WHERE NOT EXISTS`, builders drop and recreate, and normalisation statements are guarded
on the value they write.

**Re-running an earlier step.** `versions/` is not a package, so a later revision that
re-runs an earlier revision's helper loads it with `chain.load_revision("0011")` rather
than copying its SQL (0017–0020 do this for the EPC pick, 0008's bounds, and the
0011/0014/0015 builders and guards).

**No downgrades from 0002.** Every revision from 0002 calls `chain.no_downgrade()`,
which raises `NotImplementedError` with the two ways back: restore a `pg_dump`, or
rebuild from empty (`db_init.sql`, `bare_metal --load-raw`,
`db_grants_post_migration.sql`). A downgrade past a data-building revision would destroy
data with nothing to restore it from. Only 0001 is reversible, because its tables are
still empty.

**Fail early and loudly.** 0001 checks both schemas exist before hours of loading. 0003
refuses an empty `staging.ukhpucl` and pre-flights every file. Build revisions check
their outputs are non-empty and, where it matters, match `production.ppd_table` row for
row. 0016 asserts exact counts: a from-empty build that differs from the reference counts
is a finding, not a rounding error. If a refreshed source legitimately moves a count,
update the constant in `london_houses_view.py` in the same commit.

**Heavy SQL runs tuned and outside alembic's transaction.** Builders open their own
connection with `common.tuning.apply_bulk_session_tuning`, use `CREATE TABLE AS` into an
UNLOGGED build table (PostgreSQL will not parallelise a SELECT feeding an INSERT), and
create indexes after loading.

**Promote-safe columns.** 0007 rebuilds production tables with
`LIKE staging.<t> INCLUDING ALL`, so a column must exist on the staging model to survive
a re-promote. `EPC.floor_level` and `PPD.oldnew` are declared there and backfilled on
both schemas by 0008.

## Raw sources (`landing_ddl.py`, `seed_registry.py`)

0002 creates every landing table whose load is a straight `\copy` or `read_csv`. Tables
whose loader is a `pipeline_steps` function (PTAL, rivers, INSPIRE, LSOA boundaries, IoD,
green spaces) create themselves, because their shape depends on the loader;
`landing_ddl.STEP_OWNED_LANDING_TABLES` lists them. The `hpldn_*` functions are in 0002
because `staging.codepoint_open` has a functional index on `hpldn_pcnorm`.

`seed_registry.SOURCES` is an ordered tuple of `SeedSource` records. Each has a target,
a `cfg_key` (an `inputs.yaml` key), and a mechanism:

| Mechanism | Used for |
| --- | --- |
| `PSQL_COPY` | Large CSVs, `\copy` through a psql subprocess (password in `PGPASSWORD`, never argv); optionally one process per file |
| `PANDAS` | Small ONS lookups via `read_csv` → `to_sql` |
| `STEP` | Sources needing transformation: GML via `ogr2ogr`, PTAL band ranks, IoD column map, UPRN refresh |

`check_sources` resolves every path and raises one `DataSourceMissing` listing all
missing files. `run_sources` calls it, then loads in registry order and logs each row
count against `live_rows`.

Two load-order rules are encoded in the registry and must not change:

1. **`staging.ukhcs_table` is two loads.** A full copy of the OS Open UPRN 202604
   extract, then an insert-only append of UPRNs minted in the 202608 extract. The append
   deliberately skips ~192,053 coordinate revisions; applying them would move every
   spatial feature. `staging._ukhcs_refresh_marker` records what was appended.
2. **`epc_domestic` and `epc_domestic_history` read the same directory with different
   scopes.** 2024–2026 national feeds `production.epc_table`; 2008–2026 London-filtered
   feeds the `epc_addrkey` gazetteer. Most recent sales have a pre-2024 certificate,
   which the gazetteer needs and `epc_table` must not take. The history load runs up to
   six years in parallel, each in its own UNLOGGED scratch table.

Loaded outside the registry (`EXTERNAL_SOURCES`): `staging.ukhpucl` (23.3 GB, by
`StagingCopy` before 0002), `staging.iod_releases` (in-code literals), the four 0009
reference CSVs, and INSPIRE parcels (0005, because the lot-area join needs
`london_houses_condensed`).

## EPC / Price Paid fusion (0006, `epc_fusion_sql.py`)

`staging.ukhpucl` ends in December 2024. Later sales are fused from `epc_domestic` and
`ppd_domestic`, on address, because Price Paid has no UPRN:

1. derive London EPC addresses (premise line: first address line starting with a digit,
   else line 2 when line 1 is a flat/floor descriptor) and resolve them to `address_table`;
2. insert one certificate per (address, inspection date);
3. load the Tier A fixture's transactions, minting addresses for fixture-only UPRNs and
   resolving each link through its stable `uprn` (its frozen `address_id` column does
   not match ids minted on a from-empty build);
4. build `epc_addrkey`, whose tier keys come from `hpldn_addrkey` (normalises flat words
   and 22 street abbreviations), `hpldn_tokenset` and `hpldn_numsig`;
5. match with four tiers of decreasing strictness, each rejected if the two postcodes'
   Code-Point Open centroids are further apart than `PPD_EPC_GEO_THRESHOLD_M`;
6. mint addresses for matched UPRNs, insert fused transactions, and re-derive
   certificates for the newly minted addresses;
7. verify the link rate against the fixture's own rate.

The fixture is loaded before the matcher so hand-verified rows win conflicts. 0006 logs
how many fixture keys the matcher does not reproduce; **retire the fixture only when that
number is zero**.

## The serving view (0016, `london_houses_view.py`)

`london_houses_view.py` holds every definition of `production.london_houses`, because a
from-empty replay builds each in turn: `CREATE_VIEW_SQL_0020` (0016/0020; 93 columns, 18
joins, one row per sale where `a.postcode IS NOT NULL AND p.price >= 10000`, with
`is_maisonette` falling back to the as-of certificate's `property_type`),
`CREATE_VIEW_SQL_0025` (the later-certificate fallback, `street_key`, `epc_after_sale`)
and `CREATE_VIEW_SQL` (0034: plus the 12 `ofsted_*` columns, 107 in all).

- The EPC join is an as-of `LEFT JOIN LATERAL` (newest certificate inspected on or before
  the sale). `epc_matched` is true where that certificate has a floor area and energy
  rating; 0016 asserts `EPC_MATCHED_ROWS_EXPECTED = 891_187` of them.
- UKHPI and mortgage rates join on `published_date <= dateoftransfer`, and IoD on
  publication plus an embargo, so no sale sees data published after it.
- `UKHPUCL_UPRN_PRICE_INDEX_SQL` creates the composite ukhpucl index the per-row lateral
  needs; without it the build plans at ~180M cost and runs for hours.
- `VIEW_COLUMNS` is parsed from the SQL and checked against `VIEW_COLUMN_COUNT` at import.
  `hpp_rf.FEATURE_COLUMNS` and the data-quality tests use subsets of it, in order.

0016's guards: row count `1,923,529`, the `epc_matched` count, column names and order, no
NULL `build_form_propertytype`, every `construction_age_band` matching
`^[0-9]{4}(-[0-9]{4})?$`, and ≥ 99 % fusion coverage.

After 0016, refresh the view with `REFRESH MATERIALIZED VIEW CONCURRENTLY
production.london_houses`; do not re-run the revision. Then rebuild the address search
relation (0021) from the same data, `python -m hpldn.backend.migrations.address_search`,
and run `src/hpldn/backend/db/sql/db_grants_post_migration.sql`, `bare_metal --finalize`
and `src/hpldn/backend/db/sql/db_verify_init.sql`. A search relation left stale silently stops
suggesting newly loaded homes.

## Data repairs (0017–0020)

0001–0016 reproduce the reference row counts but leave gaps in the content. These four
revisions repair them:

- **0017, cut-over gap.** `staging.ukhpucl` ends on 2024-10-31 but lacks the late
  registrations of its last months (Oct 2024 kept 14 % of raw London PPD). The 0006
  matcher is re-run from the first month whose kept share drops below 80 % of the
  first three months' share, and never later than six months before the fusion window,
  for PPD sales not already in `ppd_table` by (postcode, date, price). Links go to
  `ppd_uprn_linkage_backfill`, because `verify_epc_ppd_fusion_v2` requires the main
  linkage to stay inside the window. Guard: every month from May 2024 keeps ≥ 60 % of
  raw PPD (measured 92–94 % after the back-fill, 28,277 sales added).
- **0018, post-fusion refresh.** The street and parcel enrichment runs in 0005, before
  fusion mints addresses. 0018 mints an address for every London EPC UPRN with none
  (359,288), re-runs
  `add_poststreet_to_address_table`, fills a missing `poststreet` from the newest linked
  PPD sale's street and then from EPC `address2`, and computes lot areas for every sold
  UPRN without a parcel (89,163). Guard: ≤ 5 % of sold addresses without a street.
- **0019, propagation and `property_type`.** Appends the new addresses and sales
  to production by id (never a re-promote, which would delete 0008's history
  certificates), re-promotes `london_parcels`, rebuilds `uprn_lookup`, backfills history
  certificates and 0008's clean-up, then adds `epc_table.property_type` from
  `epc_domestic_history` (falling back to ukhpucl). Guard: ≤ 2 % of sold addresses'
  certificates without a type.
- **0020, rebuild.** Re-runs the 0010–0015 builders and rebuilds the view, whose
  `is_maisonette` is now `COALESCE(ukhpucl flag, as-of certificate is a Maisonette)`.
  Guards: 0016's counts as floors, the monthly completeness guard on production,
  per-year NULL-rate ceilings from 2024 (`poststreet`, `lot_area_sqm` ≤ 5 %, `latitude`
  ≤ 1 %) and ≤ 10 % unknown `is_maisonette` among flats sold in the latest 12 months.

0017–0019 each ran in minutes against `hpdbldn` in a rolled-back transaction. 0020 takes
hours, and the API has no view between 0019 and the end of 0020. Afterwards run
`src/hpldn/backend/db/sql/db_grants_post_migration.sql`, restart the API, retrain both models and
re-run `build_london_map` and `backtest_heuristic`.

## Address search (0021, 0022, 0024, 0031)

- **0021** installs `pg_trgm` (through `public.deploy_pg_trgm_extension()` from
  `src/hpldn/backend/db/sql/db_init.sql`), creates `production.hpldn_searchnorm` and builds
  `production.address_search` with `migrations/address_search.rebuild`: one row per
  priceable home, a trigram GIN index and two `text_pattern_ops` btrees.
- **0022** widens "priceable" (`address_search._PRICEABLE_PREDICATE`, shared with
  `api.market.pricing.ADDRESSES_SQL`) from "has a certificate with floor area and rating"
  to that **or** a residential sale of its own in `london_houses` **or** a numbered
  register row in a postcode with ≥ `MIN_EPC_NEIGHBOURS` (5) usable certificates or ≥ 1
  modelled sale (`residential_postcodes` CTE). Building-level rows with no number stay
  out. It rebuilds the relation (~17 s), re-runs 0021's shape, row-floor, `/addresses`
  parity and plan checks (via `chain.load_revision("0021")`), holds the row count to
  1,290,000 (measured 1,301,395 on hpdbldn, up from 1,283,559) and checks 500 sampled sold
  homes without a certificate are searchable. `hpp_inference.estimate_epc` prices them.
  Restart the API afterwards: `SearchCache` keeps suggestions for ten minutes.
- **0024** gives a home the register holds without a street its postcode's street as a
  search hint, so it is found by number and street.
- **0031** keys the relation on the unit (0030's `address_table.unit`): one suggestion
  per flat.

## Feature notes

- **Comparables (0014).** Five nearest same-type sales within 2000 m in the prior 12
  months. The radius makes `comp_txn_count` a real sparsity signal, and the type match
  stops flats borrowing detached-house prices. With `btree_gist`, per-type partial
  indexes on `(geom, dateoftransfer)` put the date window in the index.
- **Previous sale (0015).** The price floor is applied before the LAG, so a £1 related-party
  transfer never becomes the previous price of a genuine sale.
- **Momentum (0015).** Computed per MSOA (~7,200 households) because LSOAs are too thin;
  the table keeps its `lsoa` name. The guard compares the NULL rate after the fusion
  cut-over with the overall rate to detect stale coverage.
- **LSOA (0011).** Real point-in-polygon assignment with a 150 m fallback. Guards require
  row parity with `ppd_table` and at least 4,000 distinct LSOAs, catching a
  borough-level proxy.
- **Bounds (0008).** `total_floor_area` outside [15, 2000] m² and
  `number_habitable_rooms` outside [1, 30] become NULL; `construction_age_band` is
  normalised to `YYYY` or `YYYY-YYYY`.

## Adding a revision

1. `alembic -c src/hpldn/backend/alembic.ini revision --rev-id 0035 -m "<slug>"`.
2. Use `get_migration_engine/cfg/logger`; run heavy SQL on a tuned connection of its own.
3. Make `upgrade()` re-runnable and end it with a guard on what it built. Add any new
   input file to `inputs.yaml` and resolve it with `inputs.resolve`.
4. Call `no_downgrade()` in `downgrade()`, update `chain.CHAIN_HEAD`, and update the
   models in `../models` to match.
5. Keep new scratch tables in `staging`; anything in `production` is readable by the API
   role.
