# `hpldn.backend.api` — the read API

A read-only FastAPI service over the `production` schema. It serves two audiences:

- **the frontend**, through `/session`, `/session/config`, `/areas`, `/areas/{code}`,
  `/areas/{code}/comparables`, `/inflation`, `/estimate`, `/addresses/search`,
  `/addresses`, `/price`, `/nearby` and `/schools` — the only routes nginx exposes to the
  browser, under `/api`, with the API key added by nginx;
- **operators and scripts**, through `/properties`, which stays on the loopback.

The root [README → Read API](../../../../README.md#read-api) has the route overview.
This file documents the code.

## Layout

```
api/
├── __init__.py      # pins DB_SCHEMA=production before any model import
├── main.py          # the FastAPI app, lifespan startup check, router wiring
├── config.py        # ApiSettings and get_settings (environment, develop.env, systemd credentials)
├── deps.py          # async engine, session dependency, X-API-Key guard, session-pass check
├── access_log.py    # one [ACCESS] line per request: client, parameter names, status, request id
├── ratelimit.py     # per-client token bucket and in-flight cap (429 + Retry-After)
├── schemas.py       # Pydantic response models: the wire contract
├── worker.py        # HardenedUvicornWorker: no server/date headers, no proxy-header rewrite
├── routers/
│   ├── health.py      # GET /health, GET /ready (no auth)
│   ├── session.py     # GET /session/config, POST /session (reCAPTCHA token -> pass)
│   ├── properties.py  # GET /properties[/{uprn}[/transactions]]
│   ├── market.py      # GET /areas[/{code}[/comparables]], GET /inflation, POST /estimate
│   └── pricing.py     # GET /addresses/search, GET /addresses, POST /price, POST /nearby, POST /schools
└── market/
    ├── districts.py   # the 132 areas and postcode helpers
    ├── queries.py     # read-only SQL, one text() constant per use
    ├── repository.py  # runs that SQL and maps rows onto schemas
    ├── cache.py       # per-worker TTL cache of the window and area figures
    ├── estimator.py   # Estimator protocol, ModelEstimator, HeuristicEstimator, drivers
    ├── price_index.py # UK HPI levels + projection, for time-adjusting nearby sales
    ├── pricing.py     # the loaded price model, address options, PriceResult mapping
    ├── nearby.py      # the nearest sales to a home, for POST /nearby
    ├── schools.py     # the nearest rated schools, for POST /schools
    ├── inflation.py   # the CPIH series (production.cpih), for GET /inflation
    ├── search_query.py    # what /addresses/search accepts: bounds, charset, normal form
    └── address_search.py  # the prefix and trigram SQL, per-worker cache and metrics
```

## Running

```bash
uv pip install -e ".[dev,api]"                  # editable, so --reload serves your edits
uvicorn hpldn.backend.api.main:app --reload      # local, with POSTGRES_API_* in develop.env
gunicorn hpldn.backend.api.main:app -k uvicorn.workers.UvicornWorker   # production
```

At startup `main.check_database` reads `production.london_houses` once and logs the
connection target, row count, latest sale and the `hpldn` install path. Any failure
stops the server. `HPLDN_API_SKIP_STARTUP_CHECK=1` skips it (the hermetic tests do).

## Local stack

`python -m hpldn.backend.cloud.local` runs on localhost what the production host runs:
nginx with `cloud/vps/nginx/hpldn.conf` and `guard.yaml`'s limits, the `hpldn-guard`
daemon, and this API under gunicorn exactly as `hpldn-api.service` starts it (2
`UvicornWorker`s on `127.0.0.1:8000`, `--timeout 360`), with the reCAPTCHA gate on.

```bash
uv run python -m hpldn.backend.cloud.local up [--fake-google] [--build] [--foreground]
uv run python -m hpldn.backend.cloud.local status          # processes, ports, the scope's memory, bans
uv run python -m hpldn.backend.cloud.local unban <ip> | --all [--forget]
uv run python -m hpldn.backend.cloud.local down
```

| Port | What |
| --- | --- |
| `127.0.0.1:8080` | nginx: the built site (`frontend/build`, with the CSP) and `/api/*`; what Playwright tests |
| `127.0.0.1:3000` | `npm start` (HMR); its `setupProxy.js` forwards `/api/*` to `:8080` with `CF-Connecting-IP` set to the browser, as Cloudflare does |

`up` renders everything into `tmp/local-stack/` (`cloud/local/render.py`: production
paths swapped for local ones, plain HTTP, `CF-Connecting-IP` trusted from the loopback),
runs `nginx -t`, builds the frontend with `--build` or when `build/` is missing, and
writes logs to `tmp/local-stack/logs/`. Without nginx on `PATH` it unpacks Ubuntu's
package with `cloud/local/fetch_nginx.sh` (no sudo). It refuses to start with
`HPLDN_CAPTCHA_DISABLED=1` or without the reCAPTCHA keys.

The stack runs on a machine the size of the production host: `up` re-executes itself in
a systemd user scope with `AllowedCPUs` set to 4 CPUs (the VPS's 4 OCPU) and
`MemoryMax=10G`, and every process inherits it. Nothing is tuned inside: numpy's
OpenBLAS and CatBoost size their thread pools to the 4 CPUs they see, as on the VPS.
The local price index's ~800 small linear solves run on one BLAS thread
(`scripts/local_index._solve`): with OpenBLAS's own pool, two workers' spin-waiting
threads on the same 4 cores turned 0.6 s of solves into ~4 minutes at 100 % CPU, here and
on the VPS alike. Both workers are now ready ~11 s after gunicorn starts. Once warm they
hold ~6 GB and sit idle.

`tmp/local-stack/logs/api.log` holds every API request: the API's own timestamped
`[ACCESS]` line (client address from nginx's `X-Real-IP`, status, time, `rid=`) and
nginx's `[EDGE]` line for each `/api` request it refused itself (429, 403 banned, 404),
which the API never sees. The page sends an `X-Request-ID` per call and, under `npm start`
(or with `localStorage['hpldn.apiLog'] = '1'` in a build), prints a console line with the
same `rid`.

`--fake-google` points the API at a fake siteverify on `127.0.0.1:18999`
(`cloud/local/fake_google.py`), which answers by token (`e2e-ok`, `e2e-low`,
`e2e-browser-error`, `e2e-down`); everything else in the gate runs for real. Playwright
starts the stack this way. Without it tokens go to Google, and the production site key
rejects `localhost`: for manual browsing with real Google, create a separate dev
reCAPTCHA v3 key that accepts `localhost` (domain verification off for a GCP key, or
`localhost` in the classic admin console) and put it in `develop.env`. The production key
stays domain-restricted.

Bans behave as in production (`cloud/vps/guard.yaml`): 20 answers of 429 in a minute
ban the address for 10 minutes, then 1 hour, 24 hours, 7 days, and the fifth strike within
30 days is a permanent blacklist; a banned client that keeps sending (60 requests a
minute) moves straight to the next step. Reloading the page in a loop is enough to ban
yourself, so `unban --all --forget` lifts them (and the strikes), and `status` lists them.

## Configuration

`config.get_settings` resolves once per worker process:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HPLDN_DIR_FP` | unset | Load `develop.env` from this directory (the environment still wins) |
| `POSTGRES_API_USERNAME` | `hpldn_api` | Database role |
| `POSTGRES_API_PASSWORD` | required over TCP | Role password; not needed when `POSTGRES_API_HOST` is a socket directory (peer authentication, as on the serving host) |
| `POSTGRES_API_HOST` | `127.0.0.1` | Host, or a unix socket directory (starts with `/`) |
| `POSTGRES_API_PORT` | `6432` | `5432` for a default local server |
| `POSTGRES_API_DATABASE` | required | |
| `POSTGRES_API_SSLMODE` | `require` | `prefer` for a local server without a certificate |
| `HPLDN_API_KEY` | required | Expected `X-API-Key` value (a systemd credential on the host) |
| `HPLDN_MARKET_CACHE_TTL_S` | `3600` | Lifetime of the market cache |
| `HPLDN_PRICE_MODEL_FP` | `models.catboost_bundle` in `staging.yaml`, else `<repo>/tmp/models/model` | The model bundle `POST /price` loads (`hpldn.backend.models.artifact`: `model.cbm`, `meta.json`, `calibration.json` for the price range, ...). Verified at warm-up; there is no fallback to another model and no Random Forest, so a missing or failing bundle makes the pricing routes answer 503 |
| `HPLDN_ALLOW_UNSIGNED_MODEL` | unset | `=1` loads a bundle without a trusted signature, for local development; refused when `HPLDN_ENV=production` |
| `HPLDN_API_DOCS` | unset | Serve `/docs`, `/redoc` and `/openapi.json`: the local stack and Playwright only, never the serving host |
| `RECAPTCHA_SITE_KEY`, `RECAPTCHA_SECRET_KEY` | required while the captcha is on | The reCAPTCHA v3 key pair (the secret is the `recaptcha_secret_key` credential on the host). The site key is served to the page by `GET /session/config` |
| `HPLDN_PASS_SECRET` | required while the captcha is on | Signs the passes `POST /session` hands out (the `pass_secret` credential on the host); at least 32 characters, the same in every worker |
| `HPLDN_CAPTCHA_DISABLED` | unset | `1` turns the reCAPTCHA gate off, with a loud warning: the pytest suites only (the local stack refuses it) |
| `HPLDN_RECAPTCHA_VERIFY_URL` | Google's siteverify | A fake Google on the loopback (`127.0.0.1`, `::1` or `localhost`; any other host fails startup), logged loudly: the local stack's `--fake-google`. `bootstrap.sh` strips it from the host's `api.env` |
| `HPLDN_GUARD_CONFIG` | `/etc/hpldn/guard.yaml`, else the repo's `cloud/vps/guard.yaml` | The guard rails' config: the per-worker rate limits (`rate_limits.app`) and the captcha (`captcha`), see `hpldn.backend.guard.config` |

Secrets are read with `common.secrets.read_secret`: on the host from the unit's systemd
credentials, never from the environment; elsewhere from the environment. Because
settings are cached per worker, rotating a secret needs a worker restart. The engine
disables server-side prepared statements (`prepare_threshold=None`).

## Authentication

`deps.require_api_key` compares `X-API-Key` with the configured key in constant time on
UTF-8 bytes (so non-ASCII input is a 401, not a 500). A missing server-side key fails
closed. Health routes are unauthenticated; every other router is mounted with the guard.

On top of the key, `deps.require_pass` wants a reCAPTCHA pass in `X-HPLDN-Pass` on the
routes in `guard.yaml`'s `captcha.gated_routes` (`/addresses`, `/price`, `/estimate`,
`/nearby`, `/schools`). `/areas*` and `/inflation` stay open, and so does
`/addresses/search`, so a first search never waits for reCAPTCHA; its own rate limits
hold it instead.
The page gets one from `POST /session` (`routers/session.py`), which checks a reCAPTCHA
v3 token with Google and returns an HMAC-signed pass bound to the client's address
(`hpldn.backend.guard.captcha`). Without a valid pass the answer is 403 with
`X-HPLDN-Captcha: required`; a visitor Google scores below `captcha.min_score` gets 403
with `X-HPLDN-Captcha: low_score` from `POST /session`. Both also carry
`X-HPLDN-Reject: captcha`, and the limiters' 429s `X-HPLDN-Reject: rate`, which nginx
logs for the `hpldn-guard` daemon and hides from the client.

## Schemas

`schemas.py` is deliberately separate from the ORM, so a new column cannot change a
response. The `/properties` models are snake_case. The market models inherit
`CamelModel` and are camelCase on the wire, field for field with
`frontend/src/data/types.ts`; a backend test diffs the two, so change both together.

## The market domain

### Areas (`districts.py`)

An area is one of 132 codes. `production.london_houses.postcode_district` stores outward
codes, and `area_of` groups them:

1. a sub-district folds into its parent: SW1V → SW1, EC2M → EC2;
2. an outer London code becomes its postcode area: BR3 → BR (the 12 `OUTER_AREAS`);
3. anything else is unchanged, so TN16 or E22 are simply not covered (a 404).

`district_members(code)` expands an area into every stored outward code (SW1, SW1A–SW1Z;
BR0–BR99) for an index-friendly `= ANY(...)` filter. `evidence_members(outward, area)`
chooses the scope for an estimate's evidence: the whole area in inner London, the
postcode's own outward code in outer London.

Rents come from `production.london_rents` (migration 0026): London's slice of the ONS
Price Index of Private Rents monthly price statistics (release of 16 September 2026),
January 2015 to August 2026, one row per area, month and measure (all homes, one to
four-or-more bedrooms, four property types). ONS publishes no City of London figure, so
the City has no row and its areas are served without a rent or yield. An area's
`typicalMonthlyRent` is its modal borough's overall mean rent for the latest month with
one (`rentMonth`), to the £1, its `bedroomRents` that month's means by bedrooms, and its
yield the overall mean against the area's average price.

### Queries and repository

`queries.py` holds each statement as a `text()` constant; `repository.py` executes them
and builds schemas.

| Statement | Cost | Used for |
| --- | --- | --- |
| `DATA_WINDOW_SQL` | aggregate, cached | Latest and first sale, total sales. The window is the latest sale's year and the three full years before it |
| `DISTRICT_STATS_SQL` | aggregate, cached | One row per area over the window: sales (coverage), median £/m² of sized sales (the estimate's area formula, which is blended with the nearby same-type sales at a 60 % weight), PTAL, station distance, modal EPC band, modal borough, centre |
| `AREA_YEAR_STATS_SQL` | aggregate, cached | One row per area: sales and mean price in the latest sale's calendar year Y, Y − 1 and Y − 5, and for Y the mean floor area and mean `iod_imd_pct_london`. The figures the page shows |
| `LONDON_RENTS_SQL` | cached | Each borough's latest month in `production.london_rents`: its overall mean rent and its 1, 2, 3 and 4+ bed rents |
| `MORTGAGE_RATES_SQL` | cached | The latest month of `production.mortgage_rates` (two-year fixed, 60/75/85/90/95 % LTV), served as `mortgageRates` on `GET /areas` |
| `COMPARABLES_SQL` | per request | Candidate nearby sales of the requested type or its PPD group (flats with maisonettes, or houses), sold in the last 24 months within 2 km (inner) or 5 km (outer) of the origin; same type first, then nearest floor area, then most recent; over-fetched and de-duplicated by street in Python |
| `EVIDENCE_COUNTS_SQL` | per request | The evidence scope's sales, and its same-type sales within ±20 % of the floor area |
| `POSTCODE_CENTROID_SQL` | per request | Centre of an exact postcode, the first-choice distance origin |
| `OUTWARD_CENTROID_SQL` | per request | Median position of an outward code's sales, the origin when the postcode is not held |

The area figures describe calendar year Y, the latest sale's (a part year: 2026 runs to
31 July). `averagePrice` is the mean `price` of the area's Y sales;
`averagePricePerSqm` is that mean over their mean `total_floor_area`; `movement1y` and
`movement5y` are the change in the mean price from Y − 1 and Y − 5; `areaProfilePercent`
is the mean London IMD percentile of the Y sales, to three significant figures; and
`salesCount` counts the Y sales. The borough is the modal LSOA name with its trailing
code stripped. `estimatorPricePerSqm` (the window's median £/m², rounded to £10) is kept
on the model for `HeuristicEstimator` and never serialised.

Rounding: prices and £/m² to £1, movements and yields to 0.1, the profile to three
significant figures.

**Missing data is served as unknown, never as a stand-in figure.** `movement1y` and
`movement5y` are null when Y − 1 or Y − 5 had no sales (E20 has none in 2021);
`averagePricePerSqm` without floor areas; `walkToStationMins`, `transportScore` and
`areaProfilePercent` without a station distance, PTAL or IMD; `typicalMonthlyRent` and
`rentalYield` for a borough without a published rent. An area with fewer than 50 sales in
Y is flagged `lowData` (its estimates are always `fair`); one with fewer than 10 sales in
the window, no sized sale or no sale in Y is not covered (404; WD in 2026). Each cache load logs which areas were dropped and
which figures were served as unknown.

**Nearby sales.** The origin is the full postcode's centre, else the outward code's own
centre, else the area centre only when the request names the whole area; an outer
outward code with no sales has no origin and so no nearby sales. Same-type sales come
first; sales of the broader group are added, marked `matchType: "broader"`, only while
fewer than three same-type sales were found. Fewer rows are returned rather than
unrelated ones.

### Cache (`cache.py`)

`MarketCache` holds one `MarketSnapshot` (window, all area figures, mortgage rates, the
price index and CPIH) per worker. The app's lifespan loads it (`warm`) before the worker
accepts a request, and it is reloaded in the background every `HPLDN_MARKET_CACHE_TTL_S`;
a stale snapshot is served while its replacement loads, and a failed reload keeps the old
one, so no request waits for the 2–7 s load. Only when warming failed does the first
request load it, under a lock that concurrent requests share. The `/areas` and
`/inflation` bodies are serialised once per load, with an ETag (304 on `If-None-Match`)
and `Cache-Control: public, max-age=60, s-maxage=300, stale-while-revalidate=3600`, so
nginx (`cloud/vps/nginx/api_cache.conf`) and Cloudflare keep a copy. Tests inject a fake
clock and call `clear()`.

### Estimator (`estimator.py`)

`Estimator` is a protocol; `get_estimator` is the FastAPI dependency that returns one.

**`ModelEstimator` is what it returns when a model is loaded.** It prices the details form
with the same CatBoost model `POST /price` uses, through
`hpp_inference.PricingService.price_details`: the postcode's centroid and template sale
place the home, the type is pinned to the one chosen, and the floor area and room count
typed replace the surrogate certificate's guess at them. Its range is the model's own
measured error for that price band, type and evidence thickness, so the two forms no
longer value the same home by two different methods. `basis` on the response says which
ran. `HeuristicEstimator` remains behind it, for a postcode with no sales near it, a
missing model, or any failure inside the pricing path -- each logged, because silently
answering by a different method is how the two numbers drifted apart.

`HeuristicEstimator` computes:

```
value = area £/m² × floor area × type factor × bedroom factor × transport factor
```

rounded half-up to £1,000 (JavaScript `Math.round` semantics, kept from the original
browser formula). The range is ±7 %, ±10 % or ±13 % for `high`, `medium` and `fair`
confidence:

| Confidence | Rule |
| --- | --- |
| `high` | `similar_count ≥ 150` and evidence-scope `sales_count ≥ 750` |
| `medium` | `similar_count ≥ 25` |
| `fair` | otherwise, and always when there are no nearby sales or the area is `lowData` |

Both counts come from the same evidence scope (BR3 for a BR3 postcode, not all of BR).

The thresholds come from `python -m hpldn.backend.scripts.backtest_heuristic`, which
treats every sized sale in the window as a request. On the latest 12 months of sales the
similar-count percentiles were P10 25, P50 135, P90 380; the median area had 760 sales.
That keeps thin central groups (EC2–EC4, WC2, E20, WD) out of `high`, and splits the
latest year about 43 % / 49 % / 8 %.

`build_drivers` returns the four plain-English reasons (nearby sales, momentum,
transport, size), worded so nothing on screen names a model, table or column. Without
nearby sales the first reads "Not enough recent similar sales nearby to compare" (never
"Around £0"); with only broader sales it says so; unknown movement or transport says it is
not known.

The nearby-sales reason quotes the **range and the middle** of the same-type sales, each
already scaled to the requested floor area and moved to the day being valued
(`repository.adjusted_price`, `price_index.PriceIndex`), not the plain median of the
prices paid, which would be neither the figure driving the estimate nor one a reader
could derive from the sales listed beside it. Each row also shows its adjusted
figure, so the list and the headline reconcile on screen. `suggested_monthly_rent` is the
borough's mean rent for homes with the requested bedrooms (5–6 take the 4+ figure),
falling back to the overall mean when ONS suppressed that count, or null for the City.

### Model prices (`pricing.py`, `routers/pricing.py`)

`POST /price` prices one home for a sale on a day after today, with the CatBoost model
trained by `hpp_cb.py`. The request is the register's four address fields
(`postnumber`, `poststreet`, `posttown`, `postcode`) and `saleDate`; the work is done by
`hpldn.backend.scripts.hpp_inference` (see `scripts/README.md`), which rebuilds the
`production.london_houses` row that sale would have and scores it with the model's own
saved preprocessing. Nothing is trained per request.

- **Model bundle.** `resolve_model_path()` tries `HPLDN_PRICE_MODEL_FP`, then
  `models.catboost_bundle` in `configuration/staging.yaml` (relative paths from the
  repository root; a host without the file uses the default), then `tmp/models/model`. Only
  that directory is served, and only if it holds a `manifest.json`: there is no fallback to
  another model and no Random Forest (a pickle). At warm-up `SavedModel` runs
  `models.artifact.load_bundle`: signature against `models/trusted_keys/`, file allowlist
  and hashes, JSON schemas, then the loaded model's summary and probe predictions. Any
  failure is logged and the routes answer 503. `get_pricer` is `lru_cache`d, so **restart
  uvicorn after changing the model**.
- **Loading.** `get_pricer` finds the model file once per worker; `ModelPricer` loads the
  model and about six years of sales (the `EvidenceStore`) once, under a lock, on a
  synchronous engine built from the same `ApiSettings` (the evidence read lifts
  `statement_timeout` for its own transaction). The lifespan starts that load on a
  background thread, so the first request does not wait for it; a failed load is logged
  and remembered, and every request then answers 503. After warm-up a price takes about
  50–70 ms and runs in the threadpool.
- **Errors.** An address not in the register is a 404; a sale date that is not after today
  (Europe/London) or more than five years out, and a home with no location (no sale in or
  near its postcode) or no way to tell its type, are 422s. Each carries a sentence the
  frontend shows as it is. A home without a certificate is priced, not refused.
- **Response.** `PriceResult`: the value rounded half-up to the pound (it moves every day
  with the sale date), corrected for the model's measured median bias in the home's
  district (or borough) and type; `low`/`high`, to the £1,000, the middle 80 % of its
  errors on sales like this one -- the same district and type where enough were measured,
  else borough, else price band, type and evidence thickness -- widened by the price index
  projection's own error at this horizon (from `hpp_inference evaluate --write-calibration`,
  `fit-projection` and `fit-local-index`), or null; the property type, floor area,
  habitable rooms and energy rating it was priced as; the last sale; `evidenceTo`, the
  latest sale held (market evidence stops there however far ahead the date is);
  `withinTenPercent`; and `projectionUplift`/`projectedFrom`/`indexArea`: the model prices
  the home as of `projectedFrom` (the day after the latest sale held) and the local price
  index for its postcode district and type -- or its borough's, or London's, named by
  `indexArea` -- moves it by `projectionUplift` to the sale date. Nearby sales on
  `POST /estimate` and `GET /comparables` are moved along the same index once the model
  has loaded. `epcSource` is `certificate`, or `estimated` for a home without one:
  its floor area and rooms were estimated from comparable homes (`estimatedFrom`, e.g.
  "6 detached houses on this street"), `energyRating` is null, `low`/`high` come from the
  calibration's `estimated` block (`evaluate --mask-epc`), and `floorAreaSqm` is null when
  no pool of comparable homes was big enough.
- **`GET /addresses?postcode=`** lists one option per home in a full postcode that the
  model can price: `ADDRESSES_SQL` uses `migrations.address_search._PRICEABLE_PREDICATE`
  (a usable certificate, a sale of its own, or a house number in a postcode with ≥ 5
  certificates or a modelled sale), the same test the search relation is built with. The register often holds a
  home twice, with and without its street; `collapse_addresses` folds the blank-street row
  into the named one when there is exactly one. A postcode with nothing to price is a 404.
- **`GET /addresses/search?q=&limit=`** suggests up to `limit` (1–10, default 8) of the
  same priceable homes for a partial address, best match first, as `AddressOption`s
  `POST /price` accepts unchanged; `200 []` when nothing matches. It reads
  `production.address_search` (migration 0021, widened to homes without a certificate by
  0022), never `address_table`:
  - *Validation.* `q` is NFKC-normalised, trimmed and collapsed, 3–64 characters,
    letters, digits, spaces and `, . / ' -` only. Anything else is a 422 whose sentence
    never repeats `q` (so `q` and `limit` are validated in the route, not by FastAPI,
    whose own 422 echoes the input). `%`, `_` and `\` are refused, and escaped anyway in
    every `LIKE`.
  - *Query.* `parse_query` splits the text into house number, street prefix and postcode
    prefix. The prefix statement reads the `text_pattern_ops` btrees (~1 ms); only when it
    returns fewer than `limit` homes, and no typed house number matched, does `search_text %> :q` fill the rest from the
    trigram GIN index (word similarity ≥ 0.7, ~10 ms). Both run under a 250 ms
    `SET LOCAL statement_timeout`; a timed-out trigram statement answers with the prefix
    homes (not cached) rather than a 500.
  - *Guard rails.* A per-worker LRU (4,096 entries, 10 minutes) on the normalised query,
    `Cache-Control: public, max-age=60`, and `ratelimit.ClientLimiter`: 2 searches/s
    sustained, 20 burst and 2 in flight per client (keyed on `X-Real-IP` only from the
    loopback proxy), answered 429 with `Retry-After`. nginx has its own, separate zone for
    this route. A database failure is a 503 with a sentence. `POST /estimate` and
    `GET /addresses` have their own bucket (1/s, 10 burst, 2 in flight); `POST /price` has
    0.5/s, 6 burst, 2 in flight per client, plus a cap of one model call per core across
    all clients, answered 503 with `Retry-After` when full.
  - *Street-less homes.* A home the register holds with no street is searched under its
    postcode's street when that postcode has only one (migration 0024), and the
    suggestion carries it as a display-only `streetHint`; `POST /price` ignores it.
  - *Observability.* One log line per search with the query's length, the result count,
    the duration and the path taken (`prefix`, `trigram`, `prefix+trigram`, `timeout`,
    `cache`) — never the query text, which is personal data.
    `GET /addresses/search/metrics` (key-protected, not proxied) reports this worker's
    p50/p95/p99, empty-result and cache-hit ratios, 429s, timeouts and errors. Alert on
    p99 above 250 ms and on any 5xx; a rising empty-result ratio means the relation is
    stale against `production.london_houses`.

## Tests

`src/hpldn/backend/tests/api/`: `test_market_logic.py` (pure helpers),
`test_market_routes.py` and `test_properties.py` (routes with a fake session),
`test_pricing_routes.py` (the pricing routes with a fake model, error mapping, address
folding and the `types.ts` contract), `test_address_search.py` (the search route's
bounds, 422 bodies, cache, 429s and parsing, with a fake search), `test_auth.py`,
`test_session.py` (the reCAPTCHA pass), `test_hardening.py` (no published contract, a
generic 500, no server header, the unix-socket peer), `test_access_log.py`,
`test_price_index.py`, and `test_market_postgres.py` and
`test_address_search_postgres.py` (read-only against the build database: the address
SQL, and the search plans on their indexes within 50 ms). `tests/load/address_search.js`
is the k6 load test, run by hand before a release and after any index change.
