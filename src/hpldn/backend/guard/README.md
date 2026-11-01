# `hpldn.backend.guard` — bot protection and abuse guard rails

Two things live here. The API imports `config` and `captcha` for its reCAPTCHA gate and
per-worker rate limits. The rest is the `hpldn-guard` daemon, which reads nginx's guard
log, turns abusive clients into escalating bans and enforces them in nginx and at
Cloudflare's edge.

## Layout

| Module | Role |
| --- | --- |
| `config.py` | Loads `guard.yaml`: `$HPLDN_GUARD_CONFIG`, then `/etc/hpldn/guard.yaml`, then the repository's `cloud/vps/guard.yaml` |
| `captcha.py` | reCAPTCHA v3 token checks (Google's siteverify, or a loopback fake in the local stack) and the signed pass `POST /session` hands out |
| `detector.py` | Turns guard-log lines into strikes, and strikes into escalating bans |
| `store.py` | Active bans and strike history in SQLite (`/var/lib/hpldn-guard/guard.db` on the host) |
| `sinks.py` | Where bans are enforced: nginx's `banned.conf` (reloaded through a systemd path unit) and the Cloudflare IP list |
| `cloudflare.py` | The account-level Cloudflare IP list (`hpldn_banned`) the bans are mirrored into |
| `crawlers.py` | Google's and Bing's published crawler ranges, so verified search crawlers are never banned |
| `daemon.py` | The daemon loop: reads nginx's JSON guard log from a unix socket, runs the detector and keeps the sinks in step |
| `__main__.py` | The CLI |

## Configuration

Everything tunable is in [`../cloud/vps/guard.yaml`](../cloud/vps/guard.yaml):

| Block | Controls |
| --- | --- |
| `rate_limits` | nginx's `limit_req`/`limit_conn` zones (rendered by `cloud/vps/render_guard_nginx.py`) and the API's per-worker token buckets (`rate_limits.app`) |
| `bans` | What counts as a strike, the escalation ladder (10 min, 1 h, 24 h, 7 days, then permanent) and the allowlist |
| `crawlers` | The crawler range URLs and reverse-DNS suffixes |
| `captcha` | Whether the gate is on, which routes need a pass, the score threshold and the pass lifetime |
| `user_agents` | User agents nginx refuses on `/api` |
| `cloudflare` | The account id and list name the bans are mirrored to |

A host can override any of it in `/etc/hpldn/guard.local.yaml`.

## CLI

```bash
python -m hpldn.backend.guard run                  # the daemon (hpldn-guard.service)
python -m hpldn.backend.guard refresh-crawlers     # fetch the crawler ranges (daily timer)
python -m hpldn.backend.guard ban 203.0.113.7 --for 24h
python -m hpldn.backend.guard unban 203.0.113.7 [--forget]
python -m hpldn.backend.guard list
python -m hpldn.backend.guard cloudflare-setup     # create the IP list, print the WAF rule
```

On localhost, `python -m hpldn.backend.cloud.local up` runs the daemon beside nginx and
the API, and `python -m hpldn.backend.cloud.local unban --all --forget` clears bans. How
the layers fit together on the serving host is in
[`../cloud/vps/README.md`](../cloud/vps/README.md#bot-protection).

## Tests

`tests/guard/` covers the captcha and pass (`test_captcha.py`), the detector, store and
sinks (`test_detector_store_sinks.py`), and the Cloudflare list, crawler ranges and
daemon (`test_cloudflare_crawlers_daemon.py`). `tests/infra/test_render_guard_nginx.py`
checks the nginx configuration rendered from `guard.yaml`.
