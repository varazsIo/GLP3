# `hpldn.backend.models` — SQLAlchemy models and price-model bundles

The package holds two unrelated things: the SQLAlchemy models below, and
[`artifact.py`](#price-model-bundles-artifactpy), the signed, data-only format the price
model is saved, shipped and served in.


Declarative models for the two PostgreSQL schemas. They are the readable description of
what each table holds and the query surface for Python code. **They do not build the
database**: the alembic chain in [`../migrations`](../migrations/README.md) owns every
`CREATE`, and only one `create_all` exists in the whole project (see below).

The root README's [Migrations and seeding](../../../../README.md#migrations-and-seeding)
section covers building the database; this file explains the code.

## Layout

| Module | Base | Schema binding | Contents |
| --- | --- | --- | --- |
| `staging.py` | `AppBase` | `DB_SCHEMA` env var, default `staging` | The 10-table ORM spine promoted to `production` |
| `staging.py` | `StagingBase` | literal `staging` | The staging-only relations of 0002–0016: landing tables, the London spine build, fusion intermediates, geography and IoD, `ptal_london` |
| `production.py` | `ProductionBase` | literal `production` | The relations the runtime reads in `production`: the promoted spine, lookups, reference tables, per-transaction features, Ofsted, and the `london_houses` view |

Not every relation has a model. The staging tables of 0017 onwards (the sale-linking
ledger and its sources, the Ofsted raw tables, the back-fill linkage) and three
`production` relations (`address_search`, `london_rents`, `cpih`) are defined only by
their migrations and read with SQL.

`__init__.py` re-exports only the spine (`app_base_meta`, `AppBase`, `UKHPUCL`,
`Address`, `PPD`, `EPC`) plus the two modules. Import anything else from its module:

```python
from hpldn.backend.models.production import LondonHouses   # the 107-column serving view
from hpldn.backend.models.staging import LondonHouses      # the London slice of ukhpucl
```

The two `LondonHouses` classes share nothing but a name.

## Three bases, three bindings

### `AppBase` — the spine

`UKHPUCL`, `UKHC`, `Address`, `PPD`, `EPC` and the five category tables
(`RatingCategory`, `BuildFormCategory`, `TransactionTypeCategory`,
`EnergyTariffCategory`, `GlazedTypeCategory`). Their `MetaData` schema is
`os.getenv("DB_SCHEMA", "staging")`, read at import time.

`DB_SCHEMA` exists for the **read path only**. It must never decide where migrations
build: `migration_metadata()` copies every `AppBase` table into a fresh `MetaData` pinned
to `MIGRATION_SCHEMA` (`"staging"`), and migration 0001 calls
`migration_metadata().create_all()`. That is the only `create_all` in the chain. A shell
that exports `DB_SCHEMA=production` therefore cannot redirect the build.

### `StagingBase` — staging-only relations

Pinned to the literal `staging`, because none of these is promoted. They are created by
raw DDL, in `migrations/landing_ddl.py` or in the pipeline step that also fills them,
because several are UNLOGGED, one is a materialized view and some take PostGIS column
types from the loader. `staging_meta.create_all()` would build the right shape as the
wrong kind of relation, so nothing calls it.

Grouped as in the module:

| Group | Built by | Classes |
| --- | --- | --- |
| Raw landing tables | 0002 / 0003 | `EpcDomestic`, `EpcDomesticHistory`, `PpdDomestic`, `CodepointOpen`, `PpdUprnLinkageTierA`, `Lsoa`, `Msoa`, `LsoaBoundaries`, `LsoaMsoaLookup`, `Ptal2023`, `PtalCoords`, `PtalCellPolygons`, `Rivers`, `InspirePolygons`, `IodLsoaRaw`, `LondonGreenSpaces`, `LondonGreenSpaceAccessPoints` |
| London spine | 0005 | `LondonHouses`, `LondonHousesCondensed`, `EpcTableCondensed`, `EpcFusedStaging` (all UNLOGGED), `LondonParcels`, `PpdTableErrors` |
| EPC / PPD fusion | 0006 | `EpcLondonAddr`, `EpcLondonAddrId`, `EpcLondonPick`, `EpcAddrKey`, `EpcAddrKeyUprnOk`, `PpdTarget`, `PpdUprnLinkage` |
| Geography and IoD | 0011 | `PropertyLsoa`, `IodLsoaSpine`, `IodReleases` |
| Views and housekeeping | 0012 / 0003 | `PtalLondon` (materialized view), `UkhcsRefreshMarker` |

`EpcRegisterColumns` is a mixin holding the 93 columns of the DLUHC EPC register CSV,
shared by `EpcDomestic`, `EpcDomesticHistory` and `EpcLondonPick`. `Geometry` is a
minimal PostGIS `UserDefinedType` so spatial columns carry their real type and SRID
without a GeoAlchemy2 dependency; all spatial work is server-side SQL.

UNLOGGED tables are rebuilt from their inputs by a single revision. They do not survive a
crash and are not replicated: re-run the revision instead of restoring them. Tables whose
names start with `_` are build intermediates read only by the migration that creates them.

### `ProductionBase` — the serving contract

Pinned to the literal `production`, so the read API reads it whatever `DB_SCHEMA` says.

| Group | Built by | Classes |
| --- | --- | --- |
| Category tables | 0007 | `RatingCategory`, `BuildFormCategory`, `TransactionTypeCategory`, `EnergyTariffCategory`, `GlazedTypeCategory` |
| Promoted spine | 0007 | `Address`, `PPD`, `EPC` |
| Lookups and geography | 0005 / 0007 / 0010 | `UprnLookup`, `AddressCoordinates`, `LondonParcels` |
| Reference tables | 0009 | `BoeRate`, `LondonStations`, `Ukhpi`, `MortgageRates` |
| Per-transaction features | 0011–0015, 0034 | `CompsFeatures`, `NeighbourhoodFeatures`, `StationProximityFeatures`, `RiverProximityFeatures`, `PtalFeatures`, `LsoaMsoaFeatures`, `LsoaPriceTrendFeatures`, `PrevSaleFeatures`, `IodFeatures`, `OfstedFeatures` |
| Schools | 0033 | `Ofsted`, `OfstedRatingSpells` |
| Serving view | 0016, rebuilt by 0020, 0025, 0028, 0029, 0034 | `LondonHouses` |

Migration 0007 promotes with `CREATE TABLE production.<t> (LIKE staging.<t> INCLUDING ALL)`.
That copies constraints and indexes but **not foreign keys**, so the production
relationships spell out `primaryjoin`/`foreign_keys` and are `viewonly=True`. It also
copies both of staging's UNIQUE constraints on each category label, which is why each
production category table declares two (`..._key` and `..._key1`).

Because each promote rebuilds production from staging, a column must be declared on the
staging model to survive. `PPD.oldnew` and `EPC.floor_level` are declared there for that
reason, even though they are filled later (migration 0008).

## Relations without a primary key

Relations created with `CREATE TABLE AS` or `CREATE MATERIALIZED VIEW` carry no primary
key; each got a UNIQUE index instead. They are mapped with
`__mapper_args__ = {"primary_key": (<unique column>,)}`, which gives the ORM an identity
without declaring a key the database does not have:

- production: the nine feature tables (`ppd_id`), `UprnLookup` (`uprn`),
  `AddressCoordinates` (`address_id`) and `LondonHouses` (`ppd_id`);
- staging: seventeen of the `StagingBase` relations.

A few have no unique key at all (`EpcAddrKey` is one row per certificate,
`PpdTableErrors` is a log). Their mapper key is a convenience only: ORM entity queries can
collapse distinct rows, so select from `Model.__table__` with Core instead.

## Why scratch tables stay out of `production`

`src/hpldn/backend/db/sql/db_init.sql` runs `ALTER DEFAULT PRIVILEGES ... IN SCHEMA production GRANT
SELECT ON TABLES TO hpldn_api`. Every table created in `production` is automatically
readable by the read-only API role, so anything that is not part of the serving contract
(including unused reference data such as `LondonGreenSpaces`) belongs in `staging`.

## Refreshing the view

`LondonHouses` is descriptive; its only definition is
`migrations/london_houses_view.CREATE_VIEW_SQL`. The unique `ppd_id` index enables the
supported refresh, which does not block readers:

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY production.london_houses;
```

Do not re-run migration 0016 to refresh it. Rebuild `production.address_search` straight
after, from the same register and certificate tables:

```bash
python -m hpldn.backend.migrations.address_search
```

## Changing a model

1. Write the migration that changes the database (see `migrations/README.md`).
2. Update the model here so it matches the live column, constraint and index state.
3. For a spine column, change the `staging.py` model so the next promote keeps it, and
   mirror it in `production.py`.

## Price-model bundles (`artifact.py`)

A trained price model is a directory, `tmp/models/<name>/`, of data only:

| File | Holds |
| --- | --- |
| `model.cbm`, `expert.cbm` | CatBoost FlatBuffers in canonical form: CatBoost's metadata map (whose JSON it re-orders on every save) is moved to the manifest, so `load_model` -> `save_model` reproduces the file byte for byte |
| `meta.json` | the preprocessing state `hpp_cb` scores with; the two stats frames as records with their dtypes |
| `calibration.json`, `projection.json`, `local_index.json` | written by `hpp_inference evaluate --write-calibration`, `fit-projection --write`, `fit-local-index --write` (and the last two by `hpp_cb --model-out`) |
| `probe.json` | 64 fixed feature rows drawn from the model's own borders, and each model's raw predictions |
| `manifest.json` | schema version, git sha, CatBoost version, training args, dataset fingerprint (database, alembic revision, rows, date range), sha256 and size of every file, each model's summary (trees, depth, features, categoricals, CTR types, split counts) |
| `manifest.sig` | `ed25519 <key id> <base64 signature of manifest.json>` |

`python -m hpldn.backend.models.artifact verify <bundle>` checks, in order: the signature
against the public keys in [`trusted_keys/`](trusted_keys/) (tracked); the file allowlist
and every hash (a `.joblib`/`.pkl` anywhere fails); strict JSON and schemas; each `.cbm`'s
magic and canonical round-trip in a child process (`_cbm_sandbox.py`: `python -I`, empty
environment, CPU/memory/file-size limits, no writable files, a network namespace where the
host allows one, else no sockets); the summaries; the probe predictions, exactly. The API
runs the first three at warm-up and the last two on the model it loaded; the serving host
runs all six (as `hpldn`) before `deploy.sh` switches models.

| Command | Does |
| --- | --- |
| `keygen` | writes the signing key to `~/.config/hpldn/model-signing.key` (0600; `HPLDN_MODEL_SIGNING_KEY` overrides) and its public key to `trusted_keys/<key id>.pem`: commit that |
| `sign <bundle>` | re-signs after a manual change (the writers re-hash and re-sign themselves) |
| `verify <bundle>` | the six checks above; exit 1 naming the failed check |
| `convert <model>.cbm <bundle>` | one-time, **trusted** conversion of a pre-bundle model and its `.meta.joblib` pickle (the only pickle load left) |
| `export-json <bundle> --out DIR` | CatBoost's JSON export, for reading; never shipped |

A bundle written without a key is unsigned: the API refuses it unless
`HPLDN_ALLOW_UNSIGNED_MODEL=1`, which is itself refused when `HPLDN_ENV=production`.
`hpp_rf`'s `.rfm` stays a pickle, loaded only offline with `--trust-pickle`.
