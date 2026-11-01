"""
``hpldn-guard``: the guard daemon and its admin commands.

    python -m hpldn.backend.guard run                  # the daemon (hpldn-guard.service)
    python -m hpldn.backend.guard refresh-crawlers     # fetch the crawler ranges (daily timer)
    python -m hpldn.backend.guard ban 203.0.113.7 --for 24h
    python -m hpldn.backend.guard unban 203.0.113.7 [--forget]
    python -m hpldn.backend.guard list
    python -m hpldn.backend.guard cloudflare-setup     # create the IP list, print the WAF rule

On the host, ``/usr/local/sbin/hpldn-guard`` runs these as the
``hpldn-guard`` user in a transient unit that loads the Cloudflare token as
a systemd credential (never an environment variable). ``ban`` and ``unban``
only change the database; the running daemon applies them to nginx and
Cloudflare within seconds.
"""

import argparse
import logging
import sys
import time

from hpldn.backend.common import redact
from hpldn.backend.guard import cloudflare, daemon
from hpldn.backend.guard.config import load_guard_config, parse_duration
from hpldn.backend.guard.detector import Detector, Protection, ban_key, key_network
from hpldn.backend.guard.store import Store

log = logging.getLogger("hpldn.guard")


def _detector(config, paths: daemon.Paths) -> Detector:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return Detector(config.bans, Store(paths.database))


def _key(value: str, ipv6_prefix: int) -> str:
    key = ban_key(value, ipv6_prefix)
    if key is None:
        try:
            key = str(key_network(value))
        except ValueError:
            sys.exit(f"hpldn-guard: {value!r} is not an address or CIDR")
    return key


def cmd_run(config, paths: daemon.Paths, _args) -> int:
    daemon.run(config, paths)
    return 0


def cmd_refresh_crawlers(config, paths: daemon.Paths, _args) -> int:
    ranges = paths.crawler_ranges().refresh(config.crawlers.range_urls)
    print(f"{len(ranges)} crawler ranges in {paths.crawlers_conf}")
    return 0 if ranges else 1


def cmd_ban(config, paths: daemon.Paths, args) -> int:
    key = _key(args.address, config.bans.ipv6_prefix)
    protection = Protection(
        allowlist=config.bans.allowlist_cidrs,
        crawlers=paths.crawler_ranges().ranges(config.crawlers.range_urls),
        cloudflare=paths.cloudflare_ranges(),
    )
    covered = protection.covering(key)
    if covered and not args.force:
        print(f"hpldn-guard: {key} is in the {covered} ranges, which are never banned (--force to ban anyway)", file=sys.stderr)
        return 1
    duration = parse_duration(args.duration) if args.duration else None
    ban = _detector(config, paths).ban(key, "manual", duration=duration)
    print(f"banned {ban.key} until {ban.until()}")
    return 0


def cmd_unban(config, paths: daemon.Paths, args) -> int:
    key = _key(args.address, config.bans.ipv6_prefix)
    detector = _detector(config, paths)
    removed = detector.unban(key)
    if args.forget:
        detector.store.forget_strikes(key)
    print(f"{'unbanned' if removed else 'no active ban on'} {key}{'; strikes forgotten' if args.forget else ''}")
    return 0 if removed or args.forget else 1


def cmd_list(config, paths: daemon.Paths, _args) -> int:
    store = Store(paths.database)
    now = time.time()
    bans = store.active_bans(now)
    for ban in bans:
        print(f"{ban.key:<45} {ban.rule:<10} level {ban.level}  {ban.left(now):>11}")
    print(f"{len(bans)} active bans", file=sys.stderr)
    return 0


def cmd_cloudflare_setup(config, _paths, _args) -> int:
    settings = config.cloudflare
    client = cloudflare.CloudflareClient(cloudflare.api_token(), settings.account_id)
    found = cloudflare.setup(client, settings.list_name)
    print(f"IP list {settings.list_name}: id {found.get('id')}, {found.get('num_items', 0)} items")
    print()
    print("The WAF custom rule that blocks it is OpenTofu's (cloud/oci/main/cloudflare.tf); by hand it is:")
    print(f"  Expression:  {cloudflare.WAF_EXPRESSION.format(settings.list_name)}")
    print("  Action:      Block")
    if not settings.enabled:
        print()
        print("cloudflare.enabled is false: set it (and account_id) in guard.yaml and restart hpldn-guard.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hpldn-guard", description=__doc__.split("\n\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the daemon")
    commands.add_parser("refresh-crawlers", help="fetch the crawlers' published ranges")
    ban = commands.add_parser("ban", help="ban an address or CIDR")
    ban.add_argument("address")
    ban.add_argument("--for", dest="duration", help="e.g. 10m, 24h, 7d (default: the next escalation step)")
    ban.add_argument("--force", action="store_true", help="ban even inside the protected ranges")
    unban = commands.add_parser("unban", help="lift a ban")
    unban.add_argument("address")
    unban.add_argument("--forget", action="store_true", help="also forget its strikes, so the next ban starts short")
    commands.add_parser("list", help="list the active bans")
    commands.add_parser("cloudflare-setup", help="create the Cloudflare IP list and print the WAF rule")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s" if args.command == "run" else "%(message)s",
    )
    # One line per request is noise; failures are logged by the guard itself.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The Cloudflare token (and anything token-shaped) never reaches the journal.
    redact.install()
    try:
        config = load_guard_config()
    except RuntimeError as exc:
        print(f"hpldn-guard: {exc}", file=sys.stderr)
        return 2
    handler = {
        "run": cmd_run,
        "refresh-crawlers": cmd_refresh_crawlers,
        "ban": cmd_ban,
        "unban": cmd_unban,
        "list": cmd_list,
        "cloudflare-setup": cmd_cloudflare_setup,
    }[args.command]
    try:
        return handler(config, daemon.Paths.from_env(), args)
    except cloudflare.CloudflareError as exc:
        print(f"hpldn-guard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
