"""
Fetch the Ofsted inspection files and DfE school locations migration 0033 loads.

Standalone so ``alembic upgrade`` stays offline. Every file in
`hpldn.backend.migrations.ofsted.FILES` is written under a stable name to
``data/ofsted/`` (Ofsted) or ``data/gias/`` (DfE), the ``ofsted_dir`` and
``gias_dir`` keys of ``configuration/staging.yaml``, and
``data/ofsted/manifest.json`` records each one's URL, sha256, published date,
coverage and row count:

- Ofsted's files are resolved through the GOV.UK Content API
  (``https://www.gov.uk/api/content/<slug>``): the attachment whose file name
  matches the registry's pattern, so a re-published file is picked up
  without editing a URL. The published date is the asset's Last-Modified.
- The 2005-2015 file is published only as a workbook; its inspections sheet
  is converted to UTF-8 CSV here (`xlsx_sheet_to_csv`, standard library
  only), so the migration reads CSV alone.
- DfE Get Information about Schools publishes ``edubasealldataYYYYMMDD.csv``
  (every establishment, closed ones included) and
  ``links_edubasealldataYYYYMMDD.csv`` (predecessor and successor URNs)
  daily; the latest of the last `GIAS_LOOKBACK_DAYS` days is taken.

Each download is checked by `validate` (required columns, minimum rows)
before it replaces the file on disk. All sources are Open Government Licence
v3.0.

Usage::

    python -m hpldn.backend.scripts.fetch_ofsted
    python -m hpldn.backend.scripts.fetch_ofsted --only gias_establishments gias_links
"""

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from xml.etree import ElementTree

from hpldn.backend.migrations import inputs, ofsted


CONTENT_API = "https://www.gov.uk/api/content/{slug}"
GIAS_URL = "https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/{name}{day:%Y%m%d}.csv"
GIAS_NAMES = {"gias_establishments": "edubasealldata", "gias_links": "links_edubasealldata"}
GIAS_LOOKBACK_DAYS = 10

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _download(url: str, out_fp: Path) -> None:
    """Download `url` to `out_fp` with curl (retries, follows redirects)."""
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    subprocess.run(["curl", "-sSfL", "--retry", "5", "-o", str(out_fp), url], check=True)


def _last_modified(url: str) -> Optional[date]:
    """Return the HTTP Last-Modified date of `url`, or None when the server sends none."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            header = response.headers.get("Last-Modified")
    except OSError:
        return None
    return parsedate_to_datetime(header).date() if header else None


def resolve_attachment(slug: str, pattern: str) -> Dict[str, object]:
    """
    Return the GOV.UK attachment on page `slug` whose file name matches `pattern`.

    Raises
    ------
    LookupError
        When no attachment, or more than one, matches.
    """
    with urllib.request.urlopen(CONTENT_API.format(slug=slug), timeout=120) as response:
        page = json.load(response)
    matches = [
        a for a in page.get("details", {}).get("attachments", [])
        if re.search(pattern, a.get("url", "") or "", flags=re.IGNORECASE)
    ]
    if len(matches) != 1:
        raise LookupError(f"{slug}: {len(matches)} attachment(s) match {pattern!r}, expected 1")
    return {
        "url": matches[0]["url"],
        "title": matches[0].get("title"),
        "page_published": (page.get("first_published_at") or "")[:10] or None,
    }


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number - 1


def xlsx_sheet_to_csv(xlsx_fp: Path, sheet: str, csv_fp: Path) -> int:
    """
    Write one worksheet of an .xlsx workbook to a UTF-8 CSV, streaming.

    Cells are written as stored: shared and inline strings as text, numbers
    (dates among them, as Excel serials) as their raw value. Empty rows are
    skipped.

    Returns
    -------
    int
        Rows written, header and title rows included.
    """
    with zipfile.ZipFile(xlsx_fp) as book:
        workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
        sheets = {s.get("name"): s.get(_REL_NS + "id") for s in workbook.iter(_SHEET_NS + "sheet")}
        if sheet not in sheets:
            raise LookupError(f"{xlsx_fp.name} has no sheet {sheet!r} (has {sorted(sheets)})")
        rels = ElementTree.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == sheets[sheet])
        target = target.lstrip("/").removeprefix("xl/")
        shared: List[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            for _, element in ElementTree.iterparse(book.open("xl/sharedStrings.xml")):
                if element.tag == _SHEET_NS + "si":
                    shared.append("".join(t.text or "" for t in element.iter(_SHEET_NS + "t")))
                    element.clear()
        written = 0
        with open(csv_fp, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for _, element in ElementTree.iterparse(book.open(f"xl/{target}")):
                if element.tag != _SHEET_NS + "row":
                    continue
                cells = {}
                for cell in element.iter(_SHEET_NS + "c"):
                    kind = cell.get("t")
                    value = cell.find(_SHEET_NS + "v")
                    if kind == "s":
                        text = shared[int(value.text)] if value is not None else ""
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(_SHEET_NS + "t"))
                    else:
                        text = value.text if value is not None and value.text is not None else ""
                    cells[_col_index(cell.get("r"))] = text
                element.clear()
                if any(v.strip() for v in cells.values()):
                    writer.writerow([cells.get(i, "") for i in range(max(cells) + 1)])
                    written += 1
    return written


def validate(path: Path, spec: ofsted.OfstedFile) -> int:
    """
    Check a downloaded file has its era's columns and at least `spec.min_rows` rows.

    Returns
    -------
    int
        Data rows with a URN.

    Raises
    ------
    RuntimeError
        On a missing column or too few rows.
    """
    try:
        if spec.kind == "inspections":
            frame = ofsted.read_table(path)
            missing = ofsted.missing_columns(frame, spec.era)
            if missing:
                raise RuntimeError(f"{path.name} lacks column(s) {missing} for era {spec.era}")
        else:
            columns = ofsted.GIAS_COLUMNS if spec.kind == "gias_establishments" else ofsted.GIAS_LINK_COLUMNS
            frame = ofsted.read_gias(path, columns)
    except ValueError as exc:
        raise RuntimeError(f"{path.name}: {exc}") from exc
    rows = len(frame)
    if rows < spec.min_rows:
        raise RuntimeError(f"{path.name} has {rows} row(s), expected at least {spec.min_rows}")
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_one(spec: ofsted.OfstedFile, out_dir: Path, today: Optional[date] = None) -> dict:
    """Download, convert if needed, validate and place one file; return its manifest entry."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_fp = out_dir / spec.out_name
    with tempfile.TemporaryDirectory(dir=out_dir) as scratch:
        scratch = Path(scratch)
        if spec.kind == "inspections":
            attachment = resolve_attachment(spec.slug, spec.attachment)
            url = str(attachment["url"])
            original = scratch / url.rsplit("/", 1)[-1]
            _download(url, original)
            published = _last_modified(url) or attachment["page_published"]
            entry_extra = {"title": attachment["title"]}
        else:
            today = today or date.today()
            for back in range(GIAS_LOOKBACK_DAYS):
                day = today - timedelta(days=back)
                url = GIAS_URL.format(name=GIAS_NAMES[spec.key], day=day)
                original = scratch / url.rsplit("/", 1)[-1]
                try:
                    _download(url, original)
                    break
                except subprocess.CalledProcessError:
                    continue
            else:
                raise RuntimeError(f"no {GIAS_NAMES[spec.key]} file in the last {GIAS_LOOKBACK_DAYS} days")
            published = day
            entry_extra = {"title": f"Get Information about Schools {GIAS_NAMES[spec.key]} {day:%Y-%m-%d}"}
        staged = scratch / spec.out_name
        if spec.sheet:
            entry_extra["converted_from"] = original.name
            entry_extra["converted_sha256"] = _sha256(original)
            written = xlsx_sheet_to_csv(original, spec.sheet, staged)
            print(f"  converted sheet {spec.sheet!r} to CSV ({written} rows)")
        else:
            original.replace(staged)
        rows = validate(staged, spec)
        sha = _sha256(staged)
        staged.replace(out_fp)
    print(f"  wrote {out_fp} ({rows} rows)")
    return {
        "key": spec.key,
        "file": spec.out_name,
        "url": url,
        "sha256": sha,
        "published_date": str(published) if published else None,
        "coverage_from": spec.coverage_from.isoformat(),
        "coverage_to": spec.coverage_to.isoformat(),
        "era": spec.era,
        "rows": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **entry_extra,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Fetch the files, update the manifest and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--out", dest="out_dir", type=Path, default=None,
                        help="Ofsted directory (holds manifest.json); the ofsted_dir input by default")
    parser.add_argument("--gias-out", dest="gias_dir", type=Path, default=None,
                        help="GIAS directory; the gias_dir input by default")
    parser.add_argument(
        "--only", nargs="+", choices=[f.key for f in ofsted.FILES], help="Fetch only these files"
    )
    args = parser.parse_args(argv)
    args.out_dir = args.out_dir or inputs.resolve("ofsted_dir")
    args.gias_dir = args.gias_dir or inputs.resolve("gias_dir")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest = ofsted.Manifest.read(args.out_dir)
    except FileNotFoundError:
        manifest = ofsted.Manifest()
    for spec in ofsted.files_for(args.only):
        target = args.gias_dir if spec.directory_key == "gias_dir" else args.out_dir
        manifest.entries[spec.key] = fetch_one(spec, target)
        manifest.write(args.out_dir)
    missing = [f.key for f in ofsted.FILES if f.key not in manifest.entries]
    path = manifest.write(args.out_dir)
    print(f"Manifest: {path} ({len(manifest.entries)} of {len(ofsted.FILES)} files)")
    if missing:
        print(f"Still to fetch: {missing}")
        return 1
    print("Next: alembic -c src/hpldn/backend/alembic.ini upgrade head (migration 0033 loads them).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
