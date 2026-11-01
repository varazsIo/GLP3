"""
The search crawlers' published address ranges, for nginx and the detector.

Google and Bing publish their crawlers' ranges as JSON
(``{"prefixes": [{"ipv4Prefix": ...}, {"ipv6Prefix": ...}]}``). Each URL in
``crawlers.range_urls`` is fetched on its own; a failed or implausible download
keeps that URL's cached ranges, as ``firewall.sh`` does for Cloudflare's, so a
bad day at Google never turns its crawler into an ordinary client.

The cache is ``crawlers.json`` in the guard's state directory, and the result
is also written as ``nginx/crawlers.conf`` (``<cidr> 1;`` lines) for the
``geo $hpldn_crawler`` block, which gives crawlers their own rate-limit zone.
"""

import ipaddress
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

import httpx

log = logging.getLogger("hpldn.guard")

FETCH_TIMEOUT_S = 20.0


def parse_ranges(body: object) -> list[str]:
    """
    Return the CIDRs in one published range file.

    Raises
    ------
    ValueError
        When the body is not the published shape, holds an invalid range or
        holds none at all.
    """
    if not isinstance(body, dict) or not isinstance(body.get("prefixes"), list):
        raise ValueError("no 'prefixes' list")
    ranges = []
    for entry in body["prefixes"]:
        if not isinstance(entry, dict):
            raise ValueError(f"unexpected entry {entry!r}")
        value = entry.get("ipv4Prefix") or entry.get("ipv6Prefix")
        if not isinstance(value, str):
            raise ValueError(f"unexpected entry {entry!r}")
        ranges.append(str(ipaddress.ip_network(value, strict=False)))
    if not ranges:
        raise ValueError("no ranges")
    return ranges


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Write `text` to `path` through a temporary file and a rename, so readers never see half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def geo_lines(ranges: Iterable[str], header: str) -> str:
    """An nginx ``geo`` include: one ``<cidr> 1;`` line per range."""
    return "".join([f"# {header}\n", *(f"{cidr} 1;\n" for cidr in ranges)])


class CrawlerRanges:
    """The cached ranges in `cache_path`, refreshed from the published URLs."""

    def __init__(self, cache_path: Path, conf_path: Path) -> None:
        self.cache_path = Path(cache_path)
        self.conf_path = Path(conf_path)

    def _read_cache(self) -> dict:
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("cannot read %s (%s); starting without cached crawler ranges", self.cache_path, exc)
            return {}
        sources = data.get("sources") if isinstance(data, dict) else None
        return sources if isinstance(sources, dict) else {}

    def ranges(self, urls: Iterable[str] | None = None) -> list[str]:
        """The cached ranges of `urls` (every cached URL by default), sorted and de-duplicated."""
        sources = self._read_cache()
        wanted = sources.keys() if urls is None else [u for u in urls if u in sources]
        found = {cidr for url in wanted for cidr in sources[url].get("prefixes", [])}
        return sorted(found, key=lambda c: (ipaddress.ip_network(c).version, ipaddress.ip_network(c)))

    def age(self) -> float | None:
        """Seconds since the cache was last written, or None without one."""
        try:
            return time.time() - self.cache_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def refresh(self, urls: Iterable[str], *, transport: httpx.BaseTransport | None = None) -> list[str]:
        """
        Fetch every URL, keep the cached ranges of any that fail, and rewrite the cache and ``crawlers.conf``.

        Returns the ranges now in force.
        """
        urls = list(urls)
        sources = self._read_cache()
        with httpx.Client(timeout=FETCH_TIMEOUT_S, transport=transport, follow_redirects=True) as client:
            for url in urls:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    prefixes = parse_ranges(response.json())
                except (httpx.HTTPError, ValueError) as exc:
                    kept = len(sources.get(url, {}).get("prefixes", []))
                    log.warning("could not refresh %s (%s); keeping %d cached ranges", url, exc, kept)
                    continue
                sources[url] = {"fetched": time.time(), "prefixes": prefixes}
                log.info("%d ranges from %s", len(prefixes), url)
        # URLs no longer configured are dropped.
        sources = {url: sources[url] for url in urls if url in sources}
        write_atomic(self.cache_path, json.dumps({"sources": sources}, indent=1) + "\n")
        ranges = self.ranges(urls)
        self.write_conf(ranges)
        return ranges

    def write_conf(self, ranges: Iterable[str]) -> None:
        """Write ``crawlers.conf`` for nginx if it differs from what is there."""
        text = geo_lines(ranges, "Search crawler ranges, written by hpldn-guard refresh-crawlers")
        try:
            if self.conf_path.read_text(encoding="utf-8") == text:
                return
        except FileNotFoundError:
            pass
        write_atomic(self.conf_path, text)
