"""
The Cloudflare account-level IP list the guard's bans are mirrored into.

A WAF custom rule, ``(ip.src in $hpldn_banned and not cf.client.bot)`` →
Block, then refuses a banned client at Cloudflare's edge, before it costs this
host anything. Every sync replaces the list's whole contents with the current
bans (``PUT .../items``), so a missed or failed sync heals on the next one.

The API token needs only *Account → Account Rule Lists → Write*, and is read
from ``CLOUDFLARE_API_TOKEN`` (``/etc/hpldn/guard.env``), never from
``guard.yaml``.
"""

import logging
from collections.abc import Iterable

import httpx

log = logging.getLogger("hpldn.guard")

API_BASE = "https://api.cloudflare.com/client/v4"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
# The systemd credential holding it on the host (LoadCredentialEncrypted in
# hpldn-guard.service; bootstrap.sh --set-cloudflare-token writes it).
TOKEN_CREDENTIAL = "cloudflare_api_token"


def api_token() -> str:
    """The guard's Cloudflare token: the unit's credential on the host, ``CLOUDFLARE_API_TOKEN`` off it."""
    from hpldn.backend.common.secrets import read_secret

    return read_secret(TOKEN_CREDENTIAL, TOKEN_ENV)
TIMEOUT_S = 20.0
WAF_EXPRESSION = "(ip.src in ${} and not cf.client.bot)"


class CloudflareError(RuntimeError):
    """A Cloudflare API call failed; the message carries Cloudflare's own errors."""


class CloudflareClient:
    """
    The few Lists API calls the guard makes.

    Parameters
    ----------
    token : str
        An API token with Account Rule Lists: Write.
    account_id : str
        The account that holds the list.
    transport : httpx.BaseTransport, optional
        For tests (``httpx.MockTransport``).
    """

    def __init__(self, token: str, account_id: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not token:
            raise CloudflareError(f"{TOKEN_ENV} is not set")
        if not account_id:
            raise CloudflareError("cloudflare.account_id is not set in guard.yaml")
        self.account_id = account_id
        # The base stops at /rules: httpx ends a base URL with "/", and
        # Cloudflare answers .../rules/lists/ (with the slash) 404.
        self._client = httpx.Client(
            base_url=f"{API_BASE}/accounts/{account_id}/rules",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_S,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, **kwargs) -> object:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400 or not isinstance(body, dict) or not body.get("success"):
            errors = body.get("errors") if isinstance(body, dict) else None
            raise CloudflareError(f"{method} {path} answered {response.status_code}: {errors or response.text[:200]}")
        return body.get("result")

    def find_list(self, name: str) -> dict | None:
        """Return the account's list called `name`, or None."""
        result = self._call("GET", "lists")
        for item in result or []:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return None

    def create_list(self, name: str, description: str) -> dict:
        """Create an IP list called `name`."""
        result = self._call("POST", "lists", json={"name": name, "kind": "ip", "description": description})
        if not isinstance(result, dict) or "id" not in result:
            raise CloudflareError(f"creating list {name} returned {result!r}")
        return result

    def replace_items(self, list_id: str, items: list[dict]) -> str | None:
        """Replace every item of list `list_id`; returns the bulk operation's id."""
        result = self._call("PUT", f"lists/{list_id}/items", json=items)
        return result.get("operation_id") if isinstance(result, dict) else None

    def operation_status(self, operation_id: str) -> dict:
        """The state of a bulk operation: ``pending``, ``running``, ``completed`` or ``failed``."""
        result = self._call("GET", f"lists/bulk_operations/{operation_id}")
        return result if isinstance(result, dict) else {}


def list_items(bans: Iterable) -> list[dict]:
    """The list items for `bans`: the address or prefix, with the rule and expiry as the comment."""
    items = []
    for ban in bans:
        items.append({"ip": ban.key, "comment": f"hpldn-guard {ban.rule} until {ban.until(utc=True)}"})
    return items


def setup(client: CloudflareClient, list_name: str) -> dict:
    """
    Create the IP list `list_name` unless it exists (idempotent); returns it.

    The WAF rule that uses it is made by hand (``README.md``), so a token that
    can edit lists cannot also change the zone's firewall.
    """
    existing = client.find_list(list_name)
    if existing is not None:
        if existing.get("kind") != "ip":
            raise CloudflareError(f"list {list_name} exists but is a {existing.get('kind')} list, not an ip list")
        return existing
    created = client.create_list(list_name, "Clients banned by hpldn-guard; synced automatically, do not edit")
    log.info("created Cloudflare IP list %s (%s)", list_name, created.get("id"))
    return created
