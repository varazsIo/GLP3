# `backend/cloud/vps` — single-host deployment behind Cloudflare

The host side of the deployment: plain files that run the whole public site on one VM,
an OCI Always Free Ampere A1.Flex (4 OCPU, 24 GB, 200 GB, aarch64, about £0 a month), or
the same scripts on any 24 GB Ubuntu 24.04 VPS. The VM, its network and everything in
front of it (Cloudflare, the tailnet key) are made by OpenTofu in
[`../oci/`](../oci/README.md), and `python -m hpldn.backend.cloud.oci up` drives these
scripts; they also run on their own. nginx keeps a route whitelist, adds the server-side
API key and limits each client. PostgreSQL runs on the same host and holds only the
tables the API reads.

```
browser ──HTTPS──▶ Cloudflare (DNS proxy, TLS, WAF: banned-IP list, methods, docs; rate-limit rule; cache)
                        │  OCI NSG + host firewall: tcp/443 from Cloudflare's ranges only
                        │  TLS client certificate from OUR origin-pull CA (Authenticated Origin Pulls, mandatory)
                        ▼
      VM (Ubuntu 24.04)
      nginx :443 ── no AOP certificate → handshake refused · banned IP → 403 · scripted user agent on /api → 403
                 ├─ /                     → /var/www/hpldn (minified React build, no source maps)
                 └─ whitelisted /api/*    → /run/hpldn-api/api.sock (X-API-Key added, limit_req per real IP)
                 └─ guard log ── unix socket ──▶ hpldn-guard: strikes → bans → banned.conf + Cloudflare list
      gunicorn (socket-activated): 2 hardened UvicornWorkers, sourceless .pyc ── unix socket, peer auth ──▶ PostgreSQL 18
                (reCAPTCHA v3 pass on the gated routes)                         (no TCP listener) db hpldn_serve
      secrets: systemd credentials (LoadCredentialEncrypted), never in the environment
      admin: SSH over the tailnet only, host keys pinned on the laptop (22 never public; 5432 never listens)
```

## Files

| File | Installed as | Purpose |
| --- | --- | --- |
| `bootstrap.sh` | run as root; a copy lives in `/opt/hpldn/vps/` | Packages (PGDG PostgreSQL 18, nginx, uv + Python 3.14, Tailscale with its apt key pinned, python3-yaml), apt over HTTPS, `hpldn` and `hpldn-guard` users, directories, database, owner and API roles, secrets as systemd credentials, firewall, units, sysctl, journald, unattended upgrades, sshd hardening. `--configs-only` for every deploy; `--set-tls`, `--set-recaptcha-keys`, `--set-cloudflare-token`, `--set-cloudflare-account`, `--rotate-pass-secret` and `--rotate-api-key` read their secret on stdin; `--close-public-ssh` (break-glass only) |
| `deploy.sh` | run on the laptop | Builds the frontend (no source maps) and the backend wheel, ships them, the model and optionally a signed data dump, installs the package sourceless, then restarts and smoke-tests the host. `--bootstrap` for the first run. SSH trusts only the pinned host keys in `~/.config/hpldn/known_hosts` |
| `restore_serving_db.sh` | `/usr/local/sbin/hpldn-restore-serving-db` | Checks a dump's signature, loads it into `production_next` as `hpldn_owner` (never the superuser), swaps schemas in one transaction, restarts the API and swaps back if it does not come up |
| `verify_dump_signature.sh` | `/usr/local/sbin/hpldn-verify-dump` | Checks a dump's Ed25519 `SIGNATURE` with `openssl` against `trusted_keys/` |
| `trusted_keys/*.pem` | `/usr/local/share/hpldn/trusted_keys/` | The public keys serving dumps are signed with (`export_serving_db --keygen`; commit the `.pem`) |
| `firewall.sh` | `/usr/local/sbin/hpldn-firewall` | Inbound policy and Cloudflare trust: 443 from Cloudflare only, Tailscale, break-glass 22; outbound, the metadata service is root-only; writes nginx's `set_real_ip_from` list |
| `guard.yaml` | `/etc/hpldn/guard.yaml` | Every rate limit, ban rule, crawler range, captcha setting and denied user agent ([Bot protection](#bot-protection)) |
| `render_guard_nginx.py` | run by `bootstrap.sh` | Renders `guard.yaml` into `/etc/nginx/hpldn/{limits_http,server_limits,bad_ua}.conf` and `limits/<route>.conf` |
| `hpldn-guard.sh` | `/usr/local/sbin/hpldn-guard` | The guard's commands (`list`, `ban`, `unban`, `cloudflare-setup`, `refresh-crawlers`), run as `hpldn-guard` |
| `nginx/hpldn.conf` | `/etc/nginx/conf.d/hpldn.conf` | The site. `@SERVER_NAME@` is replaced with the domain |
| `nginx/api_proxy.conf`, `nginx/security_headers.conf` | `/etc/nginx/hpldn/` | Shared by the `/api` locations; the response headers and the CSP |
| `systemd/hpldn-api.socket` | `/etc/systemd/system/` | `/run/hpldn-api/api.sock`, hpldn:www-data 0660: the API's only listener |
| `systemd/hpldn-api.service` | `/etc/systemd/system/` | gunicorn (socket-activated, `HardenedUvicornWorker`), 2 workers, `--timeout 360`, its three secrets as credentials, sandboxed (`IPAddressDeny` of every private range and the metadata service, no `@privileged`/`@resources` syscalls; no `MemoryDenyWriteExecute`, because the aarch64 CatBoost `.so` has text relocations), `MemoryMax=14G` |
| `systemd/hpldn-firewall*.{service,timer}` | `/etc/systemd/system/` | Apply the firewall at boot, refresh Cloudflare's ranges weekly |
| `systemd/hpldn-guard.service` | `/etc/systemd/system/` | The guard daemon (`python -m hpldn.backend.guard run`), sandboxed like the API |
| `systemd/hpldn-guard-crawlers.{service,timer}` | `/etc/systemd/system/` | Refresh Google's and Bing's crawler ranges daily |
| `systemd/hpldn-nginx-reload.{path,service}` | `/etc/systemd/system/` | Reload nginx (after `nginx -t`) when the guard rewrites `banned.conf` or `crawlers.conf` |
| `postgres/10-hpldn.conf` | `/etc/postgresql/18/main/conf.d/` | `listen_addresses = ''`, no statement or parameter logging, memory from `common/tuning.py` for a 16 GB share |
| `postgres/pg_hba.conf`, `postgres/pg_ident.conf` | `/etc/postgresql/18/main/` | `postgres` by peer, `hpldn_api` by peer (OS user `hpldn` only) into `hpldn_serve`, nobody else; no passwords exist |
| `postgres/api_role.sql`, `postgres/grants.sql` | `/usr/local/share/hpldn/` | Untracked build output: `deploy.sh` renders them (`python -m hpldn.backend.db.render_vps_sql`) from `src/hpldn/backend/db/sql/fragments/`, the same role settings and grants `db_enable_api_login.sh` and `db_grants_post_migration.sql` apply locally |

On the host:

| Path | Owner / mode | Holds |
| --- | --- | --- |
| `/opt/hpldn/vps` | root 0700 | This bundle, as last deployed (`REVISION` names its commit); the stdin modes run from here |
| `/opt/hpldn/venv` → `venv-a` or `venv-b` | root:hpldn 0750 | The running virtualenv, with the `hpldn` package compiled to `.pyc` and no `.py` (`REVISION` names its commit); `deploy.sh` builds the other slot, then flips the link |
| `/opt/hpldn/python` | root 0755 | uv-managed Python 3.14 |
| `/opt/hpldn/models` | root:hpldn 0750, files 0640 | The model and its sidecars |
| `/opt/hpldn/serving/<date>` | root 0700 | The newest two data dumps |
| `/var/www/hpldn` | root 0755 | The React build, the only thing nginx serves from disk |
| `/etc/hpldn` | root 0751 | Traversable, not listable: the API and the guard read `guard.yaml` by name |
| `/etc/hpldn/api.env` | root 0600 | Settings only: `POSTGRES_API_*` (the socket, the role; no password), `HPLDN_PRICE_MODEL_FP`, `RECAPTCHA_SITE_KEY` (public) |
| `/etc/credstore.encrypted/hpldn.{api_key,pass_secret,recaptcha_secret_key,cloudflare_api_token}` | root 0600 | The secrets, sealed with `systemd-creds --with-key=host` (`/var/lib/systemd/credential.secret`, root 0400; A1 has no TPM). Each unit sees only its own, decrypted in `/run/credentials/<unit>/` |
| `/etc/hpldn/guard.yaml`, `guard.local.yaml` | root 0644 | The bundle's `guard.yaml` (replaced on every deploy) and the host-only overrides merged on top (written once, never overwritten) |
| `/var/lib/hpldn-guard` | hpldn-guard 0755 | `guard.db` (bans, 30 days of strikes), `crawlers.json`, `nginx/banned.conf` and `nginx/crawlers.conf` |
| `/etc/nginx/hpldn/api_key.conf` | root 0600 | `proxy_set_header X-API-Key "…";` |
| `/etc/hpldn/tls/origin.{crt,key}`, `aop_ca.pem` | root 0600 (key) | The Cloudflare Origin CA certificate (ECDSA P-256) and the CA our origin-pull client certificate is issued from (`--set-tls`) |

## What is deployed

`export_serving_db.py --sign` ([`../../scripts`](../../scripts/README.md)) dumps only the tables
the runtime reads (`SERVING_TABLES`). `epc_table` keeps 15 of its 51 columns, and the
`london_houses` matview becomes a plain table. A dump is about 1 GB compressed and
restores to about 10 GB with indexes. The rest of the build database (all of `staging`,
the feature tables, `uprn_lookup`, ~75 GB) never leaves the build machine.
`tests/test_serving_tables.py` fails if runtime code starts reading a table, or an
`epc_table` column, that the dump does not ship.

## First deploy

Use [`../oci/README.md`](../oci/README.md): `python -m hpldn.backend.cloud.oci up` creates
the VM, its network and the Cloudflare zone settings with OpenTofu, pins the host keys,
waits for the host on the tailnet, runs `deploy.sh --bootstrap`, pushes every secret over
SSH stdin, deploys and smoke-tests. By hand (any VPS, or to see each step):

1. **VM**: Ubuntu 24.04, joined to the tailnet as `hpldn` with the tag `tag:hpldn`
   (`tailscale up --auth-key=file:<0600 file> --advertise-tags=tag:hpldn --hostname=hpldn`),
   its SSH host keys added to `~/.config/hpldn/known_hosts` from the provider's console
   (never trust on first use). Port 22 is never opened to the internet; every session is
   over the tailnet.
2. **Bootstrap**: `src/hpldn/backend/cloud/vps/deploy.sh --bootstrap --domain hpldn.example.co.uk`.
3. **Secrets**, each on stdin (`ssh ubuntu@hpldn sudo bash /opt/hpldn/vps/bootstrap.sh <mode>`):
   `--set-tls` (a tar of `origin.crt`, `origin.key`, `aop_ca.pem`), `--set-recaptcha-keys`
   (two lines), `--set-cloudflare-token`, `--set-cloudflare-account`. Until `--set-tls`, nginx
   serves a self-signed placeholder and trusts a CA whose key was thrown away: the site
   stays shut.
4. **Export and deploy** (on the laptop, with everything committed):

   ```bash
   python -m hpldn.backend.scripts.export_serving_db --sign      # tmp/serving/<today>/
   src/hpldn/backend/cloud/vps/deploy.sh --dump tmp/serving/<today>
   ```

   The first run ships the app, the frontend, the model bundle and the dump. Then it
   restores, restarts and waits (up to 15 min) until both workers log
   `Model pricing ready`.

## Updating the deployed app

After the first deploy, `deploy.sh` (no `--bootstrap`) is the only way changes reach the
VPS. It ships three independent things, and each has its own source on the laptop. The
site matches the laptop when all three do:

| Part | Comes from | Shipped by | Needed when you changed |
| --- | --- | --- | --- |
| Code + frontend | the **committed** `HEAD`, as a wheel (plus `uv.lock`, and a fresh `npm run build:prod`) | default; `--skip-app` / `--skip-frontend` leave it out | anything under `src/hpldn/backend` or `src/hpldn/frontend`, dependencies, or this bundle's nginx / systemd / PostgreSQL files |
| Model | the signed bundle `tmp/models/<name>/` (`models.artifact`) | `--model tmp/models/<name>` (default `tmp/models/model_anchored`, `MODEL` in `deploy.sh`); `--skip-model` leaves it out | retrained or swapped the served model, or its calibration / local index / projection |
| Data | a signed dump from `export_serving_db.py --sign` | `--dump tmp/serving/<date>`; without it the database stays as it is | tables or columns the API reads, migrations, refreshed Price Paid / EPC / rents / area figures |

1. **Test and commit.** The deploy ships a wheel built from `git archive HEAD`, not the working tree.
   Untracked files are never sent (`deploy.sh` warns; `git add` and commit them), and it
   refuses uncommitted changes to tracked files unless you pass `--allow-dirty` (which
   labels the revision `<sha>-dirty`; avoid it for a real update).

   ```bash
   uv run pytest src/hpldn/backend/tests          # includes test_serving_tables.py
   git status --short                             # should be clean
   ```

2. **Decide which parts changed** (table above), then run the matching command from the
   repo root, with Tailscale up:

   | You changed | Command |
   | --- | --- |
   | Frontend only | `deploy.sh --skip-app --skip-model` (it still restarts the API, which is harmless but slow) |
   | Backend only | `deploy.sh --skip-model --skip-frontend` |
   | Frontend + backend | `deploy.sh --skip-model` |
   | Model | `deploy.sh --skip-app --skip-frontend --model tmp/models/<name>`, plus `--dump` for the dump it was fitted against |
   | Data, or any code that reads a new table or column | `python -m hpldn.backend.scripts.export_serving_db --sign`, then `deploy.sh --skip-model --dump tmp/serving/<date>`. Ship the code and the dump in the one run so the API never runs against a schema it does not expect |
   | Everything (the safe default when unsure) | `deploy.sh --dump tmp/serving/<date> --model tmp/models/<name>` |

   Pass `--model` with the same bundle your local API serves (`models.catboost_bundle` in
   the config, or `HPLDN_PRICE_MODEL_FP`). The default, `tmp/models/model_anchored`, is
   only right if that is the one you run locally. The bundle must be signed with a key whose public
   half is committed under `src/hpldn/backend/models/trusted_keys/` and already deployed
   with the app (`deploy.sh` checks both); the host verifies it as the `hpldn` user before
   `HPLDN_PRICE_MODEL_FP` moves.

3. **What the run does.** It checks the dump's signature and digests and verifies the
   model bundle locally, then on the host installs the locked dependencies and the wheel
   into the idle virtualenv slot, compiles the `hpldn` package to `.pyc` and deletes its
   sources, runs the import and sandbox smoke tests against that, flips
   `/opt/hpldn/venv`, replaces `/opt/hpldn/vps`, re-installs the nginx, PostgreSQL and
   systemd files from it (`bootstrap.sh --configs-only`), rsyncs the frontend (never a
   `*.map`), restarts the API and waits for both workers to log `Model pricing ready`
   (up to 15 min: `/api/*` is unavailable while the workers restart and load the model).
   It ends with a smoke test on the API's socket, a check that nginx refuses a client
   without the origin-pull certificate, and `/` through Cloudflare within 1.5 s. A failed dump restore rolls back to the previous
   schema by itself. Nothing here touches the credentials, the Cloudflare settings, the
   Tailscale join or `/etc/hpldn/tls`.

4. **Check the site matches the laptop.**

   ```bash
   git rev-parse HEAD; ssh ubuntu@hpldn cat /opt/hpldn/venv/REVISION      # same sha, no -dirty
   ssh ubuntu@hpldn 'sudo sha256sum /opt/hpldn/models/<name>/manifest.json; ls /opt/hpldn/serving'
   sha256sum tmp/models/<name>/manifest.json                               # same manifest, so same files
   cat tmp/serving/<date>/manifest.json | head                             # newest dump on the host is this one
   ```

   Then load `https://<domain>/` (hard refresh, or purge the Cloudflare cache if `index.html`
   looks stale), search an address and compare one estimate with the laptop's.

5. **Roll back.** The previous slot (`venv-a`/`venv-b`) is kept until the next deploy:
   `ssh ubuntu@hpldn 'cd /opt/hpldn && sudo ln -sfn <other slot> venv && sudo systemctl restart hpldn-api'`.
   Or redeploy the previous commit:
   `git worktree add ../hpldn-prev <sha>` (keep `uv.lock` and the `tmp/` model and dump
   in place, or copy them in), run that checkout's `deploy.sh` with the same flags, then
   `git worktree remove ../hpldn-prev`. A bad dump: `sudo hpldn-restore-serving-db
   /opt/hpldn/serving/<previous date>` (the newest two dumps are kept). A bad model:
   `deploy.sh --skip-app --skip-frontend --model <previous file>`, since the earlier model
   files stay in `/opt/hpldn/models`.

Things `deploy.sh` does not carry over, so do them by hand when the laptop changes:

- **Settings that live in `/etc/hpldn/api.env`** (new environment variables the code reads).
  Edit the file as root and `systemctl restart hpldn-api`. `bootstrap.sh --configs-only`
  keeps existing values. A new secret is never an environment variable: add a
  `LoadCredentialEncrypted=` line to the unit, a `bootstrap.sh` stdin mode, and read it
  with `common.secrets.read_secret`.
- **Alembic migrations.** The host has no `hpdbldn`, only the exported tables. Run the
  migration locally, then re-export and ship the dump.
- **Gitignored inputs** (`configuration/`, `tmp/`, `.env`). Only `uv.lock`, the model and
  the dump are sent from them. If the API needs another such file, add it to
  `deploy.sh` first.
- **A new public route or a changed nginx rule.** It is in the bundle, so it deploys with
  the code, but the route whitelist and rate limits are deliberately narrow: update
  `nginx/hpldn.conf`, its route in `guard.yaml` (`rate_limits.nginx.routes`, and
  `captcha.gated_routes` if it needs a pass), the frontend's `setupProxy.js` and the
  "Data protection" section together.
- **Host-only guard settings** (`/etc/hpldn/guard.local.yaml`): edit as root, then
  `bootstrap.sh --configs-only` (re-renders nginx and restarts the guard) and restart
  `hpldn-api` if a `captcha` or `rate_limits.app` value changed.

## Routine operations

| Task | Command (laptop, over Tailscale) |
| --- | --- |
| Code change | commit, then `deploy.sh --skip-model` (add `--skip-frontend` for backend-only); see [Updating the deployed app](#updating-the-deployed-app) |
| Data refresh | `export_serving_db.py`, then `deploy.sh --skip-app --skip-frontend --skip-model --dump tmp/serving/<date>` |
| Model refresh | `deploy.sh --skip-app --skip-frontend --model tmp/models/<name>`. It verifies the bundle locally, ships it, runs `python -m hpldn.backend.models.artifact verify` on the host as `hpldn` (signature, hashes, schemas, sandboxed CatBoost round-trip, summary, probe) and only then points `HPLDN_PRICE_MODEL_FP` at it. Ship a model with the dump it was fitted against (`deploy.sh` warns when they differ) |
| Rotate the API key | `ssh ubuntu@hpldn sudo bash /opt/hpldn/vps/bootstrap.sh --rotate-api-key` (restarts the API, reloads nginx; the browser never held the key) |
| Rotate the reCAPTCHA pass secret | `ssh ubuntu@hpldn sudo bash /opt/hpldn/vps/bootstrap.sh --rotate-pass-secret` (restarts the API; open pages get a new pass on their next call) |
| New reCAPTCHA keys | `printf '%s\n%s\n' "$SITE" "$SECRET" \| ssh ubuntu@hpldn sudo bash /opt/hpldn/vps/bootstrap.sh --set-recaptcha-keys` (or `ssh -t` and type them; no rebuild: the page reads the site key from `/api/session/config`) |
| Bans | `sudo hpldn-guard list`, `sudo hpldn-guard ban <ip> --for 24h`, `sudo hpldn-guard unban <ip> [--forget]` |
| Origin certificate, AOP CA, guard token | made by OpenTofu; re-issue with `python -m hpldn.backend.cloud.oci up` ([`../oci/README.md`](../oci/README.md)), which pushes them again |
| Security exposure of the units | `sudo systemd-analyze security hpldn-api` (target ≤ 2.0); `python -m hpldn.backend.cloud.oci status` reports it and scans the logs for leaked secrets |
| Re-restore a kept dump | `sudo hpldn-restore-serving-db /opt/hpldn/serving/<date>` |
| Logs | `journalctl -u hpldn-api -f` (one timestamped `[ACCESS]` line per API request, with the client's address and `rid=`), `/var/log/nginx/hpldn-api.log` (the `/api` requests nginx refused itself: 429, 403 banned, 404), `journalctl -u hpldn-guard -f` (bans; the fifth strike in 30 days is a permanent blacklist, lifted only by `hpldn-guard unban`), `/var/log/nginx/access.log`, `journalctl -u postgresql@18-main` |
| Security updates | Ubuntu and PGDG security updates install automatically; reboot by hand when `/var/run/reboot-required` exists (warm-up takes a few minutes) |

`restore_serving_db.sh` needs about twice the data on disk while the old and new schemas
both exist (~20 GB). A failed start after the swap puts the old data back automatically,
and keeps the new schema as `production_next` for inspection.

## Fallback: an x86 VPS

If A1 capacity cannot be obtained, or the account is reclaimed, use the same scripts on
any Ubuntu 24.04 x86_64 VPS with 24 GB (OVH, Contabo, Hetzner; about £12–16 a month).
`bootstrap.sh` picks the right uv build. Every dependency in `uv.lock` has both
`x86_64` and `aarch64` wheels. There is no NSG there: `hpldn-firewall`
alone enforces 443-from-Cloudflare (with nginx's origin-pull check behind it), and the
provider's own firewall (if any) should allow tcp/443 only. Join the tailnet from the
provider's console; never open 22 to the internet.

## Data protection

The dump holds Land Registry Price Paid, EPC and address data. The aim is that the only
way to it is the public routes, one small answer at a time, to a person.

| Threat | Control |
| --- | --- |
| Direct database access | PostgreSQL has no TCP listener (`listen_addresses = ''`), so 5432 is closed whatever the firewall says. `pg_hba.conf` admits two local roles only, both by peer authentication: no password exists. `hpldn_api` (OS user `hpldn` only) can read the `production` tables and nothing else: SELECT only, read-only transactions, `search_path=production`, `statement_timeout = 5s`, no CREATE or TEMP anywhere. The data is owned by `hpldn_owner` (NOLOGIN), and the dump's SQL runs as that role, never as the superuser |
| Downloading raw files | Only the runtime tables are on the host, and none of them are under the web root. nginx serves `/var/www/hpldn` (the minified React build; `*.map` and `asset-manifest.json` are 404 even if present) and proxies the whitelisted routes. It cannot traverse `/opt/hpldn` (0750 root:hpldn), where the dumps (`serving/`, 0700 root) and the model live. Dumps are 0600 on the laptop too. Nothing is in object storage |
| Calling internal routes | nginx proxies the method-restricted routes of `guard.yaml`'s `rate_limits.nginx.routes`. `/properties`, `/addresses/search/metrics`, `/docs`, `/openapi.json` and every other `/api/*` path are 404. `/docs`, `/openapi.json`, `/health` and `/ready` at the top level are 404 too, not the app's index page, and the API itself serves no docs or `openapi.json` (only with `HPLDN_API_DOCS=1`, never set on the host); Cloudflare blocks `/api/docs` at the edge. An unhandled error answers `{"detail": "internal error", "rid": ...}`; no error echoes the request. The API key is added by nginx (overriding any client header) and never reaches the browser. Requests for any other host name are refused at the TLS handshake |
| Bulk scraping through the public routes | nginx `limit_req` per **real** client IP (60/min burst 20; address search 20/min burst 10, 4 open at once; 20 connections per client; all in `guard.yaml`). The real IP comes from `CF-Connecting-IP`, believed from Cloudflare's ranges only, and it is replaced (not appended to) in `X-Real-IP`/`X-Forwarded-For`, so `api/ratelimit.py`'s token buckets key on the same address. Behind that sit the reCAPTCHA v3 pass on the costly routes and the WAF rate-limit rule (100 `/api/` requests per client per 10 s). The origin accepts 443 from Cloudflare's ranges only (the NSG and the host firewall), and nginx answers only the client certificate our own origin-pull CA issued to this zone, so none of this can be bypassed by calling the origin IP, nor from another Cloudflare zone |
| Scripts calling the data routes | Every route but `/areas*` and `/inflation` wants a reCAPTCHA v3 pass (`X-HPLDN-Pass`): one invisible check per visit, a 30-minute HMAC-signed pass bound to the client's address. Known script and scanner user agents (`curl`, `python-requests`, `sqlmap`, an empty one, ...) are refused on `/api` by nginx ([Bot protection](#bot-protection)) |
| Clients that keep at it | `hpldn-guard` bans a client that keeps hitting 429s, floods, keeps calling gated routes without a pass, or probes for `.env`/`wp-`/`.php` paths: 10 min, then 1 h, 24 h, 7 days (probes start at 24 h). nginx answers a banned address 403 at once, and the Cloudflare IP list blocks it at the edge. Verified search crawlers, Cloudflare's ranges and the allowlist are never banned |
| Admin compromise | No public SSH ever: SSH over the tailnet only, key auth, `AllowUsers ubuntu`, no forwarding, host keys pinned on the laptop. Unattended security upgrades (Ubuntu, PGDG, Tailscale) over HTTPS. Secrets are systemd credentials (never in an environment or `/proc/<pid>/environ`) plus root-only `api_key.conf` and the TLS key; logs are redacted (`common/redact.py`), and access logs hold query parameter names and lengths, not values. The API runs as `hpldn` under a systemd sandbox: read-only filesystem, no capabilities, no private-range or metadata-service network, `/opt/hpldn/serving` and the TLS/nginx secrets invisible. The metadata service is root-only (`hpldn-firewall`), IMDSv1 is off and the instance has no OCI identity |
| Distributed floods | Cloudflare's always-on DDoS protection; switch the zone to **Under Attack** mode by hand while one lasts |

Response sizes are bounded in the code. Every public answer is an
aggregate or a small, capped list, so the routes cannot re-expose address-level EPC or
Price Paid rows in bulk:

| Route | At most |
| --- | --- |
| `GET /api/areas`, `GET /api/areas/{code}`, `POST /api/estimate` | area aggregates only |
| `GET /api/areas/{code}/comparables` | 10 rows (`MAX_COMPARABLES`, default 3): street, district, type, floor area, price |
| `POST /api/nearby` | 5 sales (`NEARBY_LIMIT`): address, type, price, date, distance |
| `GET /api/addresses/search` | 10 suggestions (`MAX_LIMIT`), address fields only |
| `GET /api/addresses?postcode=` | the priceable homes in one full postcode, address fields only (no EPC or sale fields). Not capped in code, but bounded by the postcode: 495 at most in the 2026-09 data, 40 at the 99th percentile |
| `POST /api/price` | one home's estimate |
| `GET /api/inflation` | the public ONS CPIH series (452 months), no property data |

Enumerating every London postcode through `/api/addresses` at the nginx limit takes about
50 hours per client IP, before Cloudflare's rules, the reCAPTCHA pass and the guard's bans.
It yields addresses only.

## Bot protection

The site has no login, so the guard rails are per client address and per visit. All of
their numbers are in [`guard.yaml`](guard.yaml); host-only changes go in
`/etc/hpldn/guard.local.yaml`.

| Layer | What it does |
| --- | --- |
| Cloudflare edge | Browser Integrity Check, the rate-limit rule, always-on DDoS protection, and the WAF rules (the `hpldn_banned` IP list, methods other than GET/POST/HEAD, the API docs), all made by OpenTofu (`../oci/main/cloudflare.tf`). Bot Fight Mode is off: it tarpitted fresh iOS Safari visitors, the CSP blocks its JS detection, and nothing can be exempted from it |
| nginx | `geo $hpldn_banned` → 403; `limit_req`/`limit_conn` per client (crawlers on their own zone); `curl`, `python-requests`, scanners and an empty user agent → 403 on `/api`; 10 s header/body/send timeouts; the guard log to `/run/hpldn-guard/log.sock` |
| API | Per-worker token buckets (`rate_limits.app`); `POST /session` checks a reCAPTCHA v3 token with Google and hands out a pass; the gated routes answer 403 without one |
| `hpldn-guard` | Turns the guard log into strikes and escalating bans, and writes them to `banned.conf` (nginx reloads within ~10 s) and the Cloudflare list (within ~15 s) |

1. **reCAPTCHA v3 keys.** In the Google Cloud console (Security → reCAPTCHA, or the
   reCAPTCHA admin console), create a **score-based (v3)** key restricted to the site's
   domain. `python -m hpldn.backend.cloud.oci up` pushes it from `RECAPTCHA_SITE_KEY` and
   `RECAPTCHA_SECRET_KEY`; by hand:

   ```bash
   printf '%s\n%s\n' "$RECAPTCHA_SITE_KEY" "$RECAPTCHA_SECRET_KEY" | ssh ubuntu@hpldn sudo bash /opt/hpldn/vps/bootstrap.sh --set-recaptcha-keys
   ```

   (before the first deploy, run it from `~/hpldn-vps/` instead). The site key goes to
   `/etc/hpldn/api.env`, the secret key to the `recaptcha_secret_key` credential, and the
   API restarts. The pass secret is generated by `bootstrap.sh`. Google's free tier is
   10,000 assessments a month; one assessment per visit (a pass lasts 30 minutes) keeps
   the site well inside it. If Google cannot be reached, the API hands out a 5-minute pass
   instead (`captcha.fail_open`), so an outage there cannot take the site down; if Google
   refuses our secret, the check fails closed (503, no pass) and logs an error.
   `captcha.allowed_hostnames` in `guard.local.yaml` starts as the domain.
2. **Cloudflare IP list, WAF rules and the guard's token** are OpenTofu's
   (`../oci/main/cloudflare.tf`): the `hpldn_banned` list (its items are the guard's),
   the block rule `(ip.src in $hpldn_banned and not cf.client.bot)`, and an account token
   with only **Account Rule Lists: Write**, a year long. `up` pushes the token
   (`--set-cloudflare-token`) and the account id (`--set-cloudflare-account`, which sets
   `cloudflare.enabled` and `account_id` in `guard.local.yaml`). The token that edits the
   list cannot change the zone's rules. To try it first, set `cloudflare.dry_run: true` in
   `guard.local.yaml`: the guard then logs each list update instead of sending it.
3. **Keep** Browser Integrity Check and the rate-limit rule on; leave Bot Fight Mode off
   (it tarpitted fresh iOS Safari visitors, and the CSP blocks its JS detection).
   For a distributed flood that the per-address bans cannot catch, switch the zone to
   **Under Attack** mode by hand until it passes.

Commands, on the host:

| Command | Does |
| --- | --- |
| `sudo hpldn-guard list` | The active bans, their rule and time left |
| `sudo hpldn-guard ban 203.0.113.7 --for 24h` | Ban by hand (an IPv6 address bans its /64); refuses the crawler, Cloudflare and allowlist ranges without `--force` |
| `sudo hpldn-guard unban 203.0.113.7 [--forget]` | Lift a ban; `--forget` also clears its strikes, so a next ban starts at 10 minutes |
| `sudo hpldn-guard refresh-crawlers` | Fetch the crawler ranges now (the timer does it daily) |
| `journalctl -u hpldn-guard -f` | Every ban, expiry and sync |

Notes:

- Verified search crawlers are never banned or slowed by the visitor zones: Google's and
  Bing's published ranges (`crawlers.range_urls`) get their own `crawler` zone, and a
  client claiming to be Googlebot or Bingbot from elsewhere is checked by reverse DNS
  plus forward confirmation before any ban. Googlebot never calls a gated route; it
  renders the home page with `/api/areas`, which stays open.
- Do not add a `robots.txt` block on `/api/`: Googlebot needs `/api/areas` to render
  the page.
- IP addresses are kept in `guard.db` for at most 30 days (`bans.strike_memory`), as the
  privacy policy says.
