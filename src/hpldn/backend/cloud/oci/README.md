# `backend/cloud/oci` — the OCI deployment, with OpenTofu

A reproducible, Always Free deployment of HPLDN: one Ampere A1.Flex VM (Ubuntu 24.04,
4 OCPU, 24 GB) behind Cloudflare, administered over Tailscale, with its OpenTofu state
encrypted. The host itself is set up by the scripts in [`../vps/`](../vps/README.md);
this directory makes everything around it and drives those scripts.

| Path | What |
| --- | --- |
| `state/` | Run **once, as the tenancy administrator**, with a local (encrypted) state: the `hpldn` compartment, the Vault and its customer-managed key (CMK), the `hpldn-tfstate` and `hpldn-inputs` buckets, the `hpldn-tfstate` S3 key, the `hpldn-deployer` identity, the budget and its alerts, and the quota that makes paid resources impossible |
| `main/` | Run as **hpldn-deployer**, state in `hpldn-tfstate` (S3 API, encrypted): the VCN, the NSG, the A1 instance, Cloudflare (DNS, zone settings, origin certificate, Authenticated Origin Pulls with our own CA, WAF, rate limiting, the guard's token) and the single-use tailnet key |
| `provision.py` | `python -m hpldn.backend.cloud.oci check \| state \| up \| bootstrap \| deploy \| close-ssh \| status \| destroy` |
| `oci.example.toml` | The non-secret settings; copy to `~/.config/hpldn/oci.toml` |
| `*/terraform.example.tfvars` | Variable templates for running a module by hand with `tofu -chdir=<module>`; `provision.py` passes the same values itself |
| `*/tests/*.tftest.hcl` | `tofu test` with mock providers (run by `scripts/ci/run_all.sh lint`) |

OpenTofu is **1.12.x** (`scripts/install_tofu.sh` installs 1.12.7, checking the release's
`SHA256SUMS` with cosign against OpenTofu's release workflow, then the tarball against it
and against a pinned digest). Providers are pinned to a minor (`oracle/oci ~> 9.8.0`,
`cloudflare/cloudflare ~> 5.27.0`, `tailscale/tailscale ~> 0.29.0`, `hashicorp/tls ~> 4.4`,
`hashicorp/random ~> 3.9`) and locked for `linux_amd64` and `linux_arm64` in the committed
`.terraform.lock.hcl` files.

## Architecture and trust boundaries

```
 operator laptop ─────────────────────────────────────────────────────────────────────────────┐
 │ password manager ──env──▶ provision.py ──TF_VAR/env──▶ tofu ──▶ OCI API (hpldn-deployer:    │
 │   (passphrase, CF token,     │  redacts all output        │      compartment hpldn only)   │
 │    TS OAuth, reCAPTCHA)      │                            ├──▶ Cloudflare API (tofu token)  │
 │                              │                            └──▶ Tailscale API (OAuth, keys)  │
 │ state: AES-GCM (pbkdf2) ─────┼──S3 key (state bucket only)──▶ hpldn-tfstate (CMK, versioned)│
 │ ~/.config/hpldn/known_hosts ◀┘ host keys read from the instance's serial console            │
 └──────────────┬──────────────────────────────────────────────────────────────────────────────┘
                │ SSH over the tailnet only (StrictHostKeyChecking=yes); secrets on stdin
                ▼
 ┌─ OCI compartment hpldn (quota: A1 only) ─────────────────────────────────────────────────────┐
 │ NSG: 443 ← Cloudflare ranges · udp/41641 Tailscale · ICMP 3/4 · (22 ← admin_cidr, break-glass)│
 │ egress: 443, 80, Tailscale UDP/STUN, DNS+NTP to 169.254.169.254 only                        │
 │ default security list: no rules                                                              │
 │ ┌─ VM: IMDSv2 only · no Run Command/management agent · boot volume on the CMK ─────────────┐│
 │ │ nginx :443 ─ requires OUR origin-pull client certificate (AOP) ─▶ /run/hpldn-api/api.sock ││
 │ │ gunicorn (hpldn, sourceless .pyc, no private-range/IMDS network) ─peer─▶ PostgreSQL (no TCP)││
 │ │ secrets: systemd credentials (host key; no TPM on A1) · IMDS root-only (iptables owner)  ││
 │ └────────────────────────────────────────────────────────────────────────────────────────┘│
 └───────────────────────────────────────────────────────────────────────────────────────────┘
                ▲ HTTPS, client cert from our CA
 visitor ──▶ Cloudflare (proxied A record; Full (strict); WAF: banned list, methods, docs; rate limit; Bot Fight Mode off)
```

The boundaries, outside in:

1. **Cloudflare.** The only public way in. The zone validates the origin's Origin CA
   certificate (Full (strict)) and presents a client certificate issued by **our** CA
   (Authenticated Origin Pulls at zone level; Cloudflare's shared origin-pull CA is not
   trusted, because every Cloudflare zone presents it).
2. **The NSG and the host firewall.** tcp/443 from Cloudflare's published IPv4 ranges
   only; Tailscale's UDP; nothing on 22 unless break-glass is on. The VCN's default
   security list (which opens 22 to the world by default) is managed with no rules.
3. **nginx.** No origin-pull certificate, no TLS session. Then the route whitelist, the
   API key it adds, the limits, the guard's bans.
4. **The API.** A unix socket only nginx's group can open; secrets from systemd
   credentials; no docs; fixed error messages; no route to the metadata service, the
   VCN or the tailnet.
5. **PostgreSQL.** No TCP listener; peer authentication for the OS user `hpldn` only;
   a read-only role on data a NOLOGIN owner holds.
6. **Administration.** SSH over the tailnet, as `ubuntu`, key only, host keys pinned
   from the serial console. The instance has **no OCI IAM identity** (no dynamic group,
   no instance principal): a compromised host cannot call the OCI API.

## Secret inventory

| Secret | Where it lives | Who can read it | Rotate |
| --- | --- | --- | --- |
| State passphrase | The operator's password manager → `HPLDN_TOFU_PASSPHRASE` → `TF_VAR_state_passphrase` in tofu's environment | The operator | Add the old one as a `fallback` (see `state/versions.tf`), apply both modules, remove it |
| OpenTofu state (`state/` and `main/`) | `state/terraform.tfstate` (local) and `hpldn-tfstate/main/terraform.tfstate`; AES-GCM before it leaves the laptop, then the bucket's CMK | Anyone with the passphrase *and* the file | With the passphrase |
| hpldn-tfstate S3 key | `state/` output, read in memory by provision.py into `AWS_*` for tofu only | The operator | `tofu -chdir=state apply -replace=oci_identity_customer_secret_key.tfstate` |
| hpldn-deployer API key | Private half `~/.oci/` (0600); public half in `state/` | The operator | New key pair, `deployer_public_key` in `oci.toml`, `provision.py state` |
| Cloudflare tofu token | Password manager → `CLOUDFLARE_API_TOKEN` (tofu's environment only) | The operator | Cloudflare dashboard |
| Tailscale OAuth client | Password manager → `TAILSCALE_OAUTH_CLIENT_ID/_SECRET` | The operator | Tailscale admin console |
| Tailnet auth key | `main/` state; the instance's `user_data`; `/run/hpldn/ts.key` until shredded at first boot | Root on the host (via IMDS) — but single-use, pre-authorised, 1 h, already consumed | A new one per instance |
| Origin certificate key | `main/` state → `--set-tls` on stdin → `/etc/hpldn/tls/origin.key` (root 0600) | The nginx master | `provision.py up --renew-certs` (before `origin_cert_expires_on`, which `status` prints) |
| Origin-pull CA key | `main/` state only (never on the host: nginx needs only the CA certificate) | The operator | `-replace=tls_private_key.aop_ca`, apply, `up` |
| Origin-pull client key | `main/` state and Cloudflare (zone AOP certificate) | Cloudflare's edge | `-replace=tls_private_key.aop_client` |
| Guard's Cloudflare token | `main/` state → `--set-cloudflare-token` → credential `hpldn.cloudflare_api_token` | `hpldn-guard.service` only | `provision.py up --rotate-guard-token` (a year by default) |
| reCAPTCHA secret key | Password manager → `--set-recaptcha-keys` → credential `hpldn.recaptcha_secret_key` | `hpldn-api.service` only | New key in the Google console, `up` (or the stdin mode) |
| reCAPTCHA pass secret | Generated on the host → credential `hpldn.pass_secret` | `hpldn-api.service` only | `bootstrap.sh --rotate-pass-secret` |
| API key (nginx → API) | Generated on the host → credential `hpldn.api_key`, and `/etc/nginx/hpldn/api_key.conf` (root 0600) | `hpldn-api.service`; the nginx master | `bootstrap.sh --rotate-api-key` |
| Host credential key | `/var/lib/systemd/credential.secret` (root 0400) | root | Re-encrypt every credential (re-run the `--set-*` modes) |
| Database password | **None**: peer authentication | — | — |
| Model signing key | `~/.config/hpldn/model-signing.key` (0600); public half in `models/trusted_keys/` | The operator | `python -m hpldn.backend.models.artifact keygen`, commit, re-sign |
| Dump signing key | `~/.config/hpldn/serving-signing.key` (0600); public half in `vps/trusted_keys/` | The operator | `export_serving_db --keygen`, commit, deploy, re-sign |
| age identity (inputs backups) | `~/.config/hpldn/age.key` (0600); recipient `age.pub` | The operator | New key, re-push |

Nothing secret is in an environment variable on the host, on a command line, in
`user_data` (bar the consumed tailnet key) or in a log: `common/redact.py` masks every
loaded secret and token-shaped string in the API's and the guard's logs, provision.py
passes every tofu and ssh line through the same filter and refuses to run with `TF_LOG`
set, and `provision.py status` scans the host's journal and nginx logs for SHA-256
prefixes of the secrets it pushed (the values never leave the laptop).

## Tailscale ACL

Tofu does not own the tailnet policy (so it can never clobber it); add this to it once:

```jsonc
{
  "tagOwners": {
    // Only admins can mint keys for, or move a node into, tag:hpldn.
    "tag:hpldn": ["autogroup:admin"]
  },
  "acls": [
    // Admins may SSH to the host. tag:hpldn itself reaches nothing.
    { "action": "accept", "src": ["autogroup:admin"], "dst": ["tag:hpldn:22"] }
  ]
}
```

The OAuth client (Settings → OAuth clients) needs only the **`auth_keys`** scope (write),
with the tag `tag:hpldn`. Keys it makes are single-use, pre-authorised and expire in an
hour (`main/tailscale.tf`).

## Cloudflare token for OpenTofu (minimal)

A custom API token, for the one account and the one zone:

| Scope | Permission |
| --- | --- |
| Zone → DNS | Edit |
| Zone → Zone Settings | Edit (TLS, HSTS; HTTP/3 off, as iOS Safari stalled over it) |
| Zone → SSL and Certificates | Edit (Origin CA certificate, Authenticated Origin Pulls) |
| Zone → Bot Management | Edit (keeps Bot Fight Mode off) |
| Zone → Zone WAF | Edit (custom rules and the rate-limiting rule) |
| Zone → Single Redirect | Edit (www to the apex) |
| Zone → Cache Rules | Edit (/api/areas and /api/inflation cached at the edge) |
| Zone → Zone | Read |
| Account → Account Rule Lists | Write (the `hpldn_banned` list) |
| Account → Account API Tokens | Edit (the guard's token) |

Restrict it to the zone and the account, and give it an expiry. The guard's own token,
which tofu creates, has **Account Rule Lists: Write** only, on the account, for a year.

## Cost and the free tier

| Resource | Always Free | Here |
| --- | --- | --- |
| A1.Flex | 4 OCPU, 24 GB per tenancy | 4 OCPU, 24 GB (validation refuses more) |
| Block volume | 200 GB per tenancy | 100 GB boot by default (50–200 allowed) |
| Vault | Virtual (DEFAULT) vaults, 20 key versions | 1 vault, 1 HSM key; rotate yearly by hand (`oci kms management key rotate`) |
| Object Storage | 20 GB | The state (KBs) and the age-encrypted inputs |
| Network | VCN, IGW, public IP, 10 TB egress | One of each |
| Cloudflare | Free plan | 3 custom rules (of 5), 1 rate-limiting rule (of 1); Bot Fight Mode off (it tarpitted fresh iOS Safari visitors; the CSP blocks its JS detection) |
| Tailscale | Personal | One tagged node |

The account should be **Pay-As-You-Go**: it still bills nothing inside the Always Free
limits, but idle Always Free instances are not reclaimed and A1 capacity is easier to get.
Paid resources are made impossible instead by the quota on compartment `hpldn` (every
compute core and memory family zeroed except A1's 4 cores and 24 GB, 200 GB of block
storage, no load balancers, no databases), and the budget alerts at £1 actual and £15
forecast tell you if anything still costs.

Not available on A1: shielded instances (Secure Boot, measured boot, vTPM). That is why
the host's credentials are sealed with the host key (`systemd-creds --with-key=host`)
rather than a TPM.

## What the code protects, and what it cannot

**Browser code cannot be made impossible to reverse engineer.** The frontend ships
minified, with no source maps and no asset manifest, and the backend ships as `.pyc` only.
It is not obfuscated: javascript-obfuscator made the bundle x2.3 the size gzipped (seconds
on a phone), and the API contract is visible to anyone with DevTools open anyway. The real boundary is the server: nothing but nginx is
reachable; the API is read-only, allowlisted route by route, and gives small answers; and
reCAPTCHA passes, rate limits at three layers and the guard's bans apply to every client.

## Manual steps (once)

1. **OCI account**: create it with the home region you want (e.g. UK South, London) and
   upgrade it to **Pay-As-You-Go** (see above).
2. **Domain**: buy it and move its name servers to Cloudflare (add the zone, free plan).
3. **reCAPTCHA v3**: a score-based key restricted to the domain (there is no Terraform
   resource for classic v3 keys).
4. **Tailscale**: the OAuth client (`auth_keys`, tag `tag:hpldn`) and the ACL above.
5. **Cloudflare**: the tofu token above.
6. **Keys on the laptop**:

   ```bash
   # The deployer's OCI API key pair (the public half goes in oci.toml).
   openssl genrsa -out ~/.oci/hpldn-deployer.pem 4096 && chmod 600 ~/.oci/hpldn-deployer.pem
   openssl rsa -pubout -in ~/.oci/hpldn-deployer.pem -out ~/.oci/hpldn-deployer_public.pem
   # The age identity the inputs backups are encrypted to.
   age-keygen -o ~/.config/hpldn/age.key && chmod 600 ~/.config/hpldn/age.key
   age-keygen -y ~/.config/hpldn/age.key > ~/.config/hpldn/age.pub
   # The dump signing key (commit the .pem it writes under cloud/vps/trusted_keys/).
   uv run python -m hpldn.backend.scripts.export_serving_db --keygen
   ```

## Running it

```bash
scripts/install_tofu.sh --gitleaks                 # once: tofu 1.12.7 (+ cosign, gitleaks)
cp src/hpldn/backend/cloud/oci/oci.example.toml ~/.config/hpldn/oci.toml   # and fill it in

# Secrets from the password manager, e.g. with pass:
export HPLDN_TOFU_PASSPHRASE="$(pass hpldn/tofu-passphrase)"
export CLOUDFLARE_API_TOKEN="$(pass hpldn/cloudflare-tofu)" \
       TAILSCALE_OAUTH_CLIENT_ID="$(pass hpldn/ts-client-id)" TAILSCALE_OAUTH_CLIENT_SECRET="$(pass hpldn/ts-client-secret)" \
       RECAPTCHA_SITE_KEY="$(pass hpldn/recaptcha-site)" RECAPTCHA_SECRET_KEY="$(pass hpldn/recaptcha-secret)"

uv run python -m hpldn.backend.cloud.oci state     # once, with the admin profile; then add the
                                                   # [hpldn-deployer] profile it prints to ~/.oci/config
uv run python -m hpldn.backend.cloud.oci check     # every pre-flight check, changes nothing
uv run python -m hpldn.backend.cloud.oci up --dump tmp/serving/<date>
uv run python -m hpldn.backend.cloud.oci status    # drift, instance, stray origin records, exposure scores, leak scan
uv run python -m hpldn.backend.cloud.oci deploy --export   # later: new dump, app, frontend, model
```

`up`, step by step: pre-flight; `tofu apply` (moving to the next availability domain on
"Out of host capacity"); the host keys read from the serial console's
`HPLDN-HOST-KEYS` block into `~/.config/hpldn/known_hosts`; wait for `hpldn` on the
tailnet; a check that the zone has no A/AAAA record for the site (apex or `www`) besides
the instance's address (a dead one made Cloudflare wait ~19 s on every uncached request;
`up` stops and names it, it never deletes it); `deploy.sh --bootstrap`; the secrets on SSH stdin (`--set-tls`,
`--set-recaptcha-keys`, `--set-cloudflare-token`, `--set-cloudflare-account`); public SSH
confirmed closed (`bootstrap_ssh=false`, `--close-public-ssh`); the deploy; and a smoke
test through the domain (`/` and `/api/areas` 200; `/api/docs` refused at the edge or 404;
`*.map` 404; no script injected by Cloudflare in `/`; `/` answering within 1.5 s, best of
three; a direct TLS connection to the
origin's address refused). `up` includes the deploy: run `deploy` afterwards only for later
app, frontend, model or dump changes.

If `up` stops after the instance exists (bootstrap, secrets or deploy failed), resume with
`bootstrap [--dump DIR]`: the same steps from the tailnet wait onwards, without `tofu apply`
(a re-run of `up` is safe too: an existing instance keeps its availability domain). Never
point `deploy.sh --host` at the public address: port 22 is closed there by design.

**SSH by hand.** sshd admits only `ubuntu`, and the pinned host keys live in
`~/.config/hpldn/known_hosts`, not `~/.ssh/known_hosts`; add to `~/.ssh/config`:

```
Host hpldn
    User ubuntu
    UserKnownHostsFile ~/.config/hpldn/known_hosts
    StrictHostKeyChecking yes
```

then `ssh hpldn`.

**Break-glass.** If the tailnet is unreachable, set `bootstrap_ssh = true` and your
address in `admin_cidr` in `oci.toml`, `up` (the NSG opens 22 to that one address), get in
with `ssh -o UserKnownHostsFile=~/.config/hpldn/known_hosts ubuntu@<public ip>`, then
`close-ssh` (NSG rule removed, `bootstrap.sh --close-public-ssh` on the host).

**State locking.** OpenTofu's S3 lockfile (`use_lockfile`) depends on conditional writes,
which OCI's S3-compatible API must honour; leave `use_lockfile = false` in `oci.toml`
until that is confirmed for your region (one `tofu plan` from two shells at once: the
second must fail on the lock). provision.py always holds a local `flock`
(`~/.config/hpldn/tofu.lock`), so with one operator and one laptop runs never overlap.
With more operators, confirm the lockfile first.

**destroy** asks for the domain, then destroys `main/` only. `state/` (the vault, the key,
the buckets, the budget and the quota) stays; its vault, key and buckets are
`prevent_destroy`.
