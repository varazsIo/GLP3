"""
The ``fetch_*`` scripts against mocked HTTP: paging, geometry, validation and what they write.

``urllib.request.urlopen`` is replaced by a fake serving canned pages, and
curl downloads by a fake that writes fixture bytes, so nothing leaves the
machine. Row-count expectations are scaled down by patching the modules'
constants.
"""

import io
import json
import subprocess
import zipfile
from datetime import date
from email.utils import format_datetime
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from hpldn.backend.migrations.ofsted import OfstedFile
from hpldn.backend.scripts import (
    fetch_linking_sources,
    fetch_lsoa_boundaries,
    fetch_lsoa_msoa_lookup,
    fetch_ofsted,
    fetch_ptal_centroids,
    fetch_ukhpi,
)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(monkeypatch, module, answer):
    """Route `module`'s urlopen to `answer(url_or_request) -> bytes | dict | Exception`; return the calls."""
    calls = []

    def urlopen(target, timeout=None):
        calls.append(target)
        result = answer(target)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, _Response):
            return result
        return _Response(json.dumps(result).encode() if isinstance(result, (dict, list)) else result)

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    if hasattr(module, "time"):
        monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return calls


def _offset(url: str) -> int:
    return int(parse_qs(urlsplit(url).query)["resultOffset"][0])


# ---------------------------------------------------------------------------
# ArcGIS layers: LSOA boundaries, LSOA->MSOA lookup, PTAL cells
# ---------------------------------------------------------------------------

SQUARE = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]


def test_lsoa_boundaries_pages_converts_to_wkt_and_writes_csv(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_lsoa_boundaries, "PAGE_SIZE", 2)
    monkeypatch.setattr(fetch_lsoa_boundaries, "EXPECTED_ROWS", 3)
    features = [
        {"properties": {"LSOA11CD": "E01000003", "LSOA11NM": "C"}, "geometry": {"type": "Polygon", "coordinates": SQUARE}},
        {"properties": {"LSOA11CD": "E01000001", "LSOA11NM": "A"},
         "geometry": {"type": "MultiPolygon", "coordinates": [SQUARE, SQUARE]}},
        {"properties": {"LSOA11CD": "E01000002", "LSOA11NM": "B"}, "geometry": {"type": "Polygon", "coordinates": SQUARE}},
    ]
    attempts = {"n": 0}

    def answer(url):
        offset = _offset(url)
        if offset == 2 and attempts["n"] == 0:
            attempts["n"] += 1
            return {"error": {"code": 500, "message": "busy"}}  # retried
        return {"features": features[offset:offset + 2]}

    calls = _fake_urlopen(monkeypatch, fetch_lsoa_boundaries, answer)
    out = tmp_path / "lsoa.csv"
    fetch_lsoa_boundaries.main(out)
    lines = out.read_text().splitlines()
    assert lines[0] == "lsoa11cd,lsoa11nm,wkt"
    assert lines[1].startswith('E01000001,A,"MULTIPOLYGON(((0.0 0.0,1.0 0.0')
    assert lines[3] == 'E01000003,C,"POLYGON((0.0 0.0,1.0 0.0,1.0 1.0,0.0 0.0))"'
    assert [_offset(u) for u in calls] == [0, 2, 2]
    assert "outSR=4326" in calls[0]


def test_lsoa_boundaries_refuses_a_short_layer_and_an_unknown_geometry(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_lsoa_boundaries, "EXPECTED_ROWS", 5)
    _fake_urlopen(monkeypatch, fetch_lsoa_boundaries, lambda url: {"features": []})
    with pytest.raises(RuntimeError, match="want 5"):
        fetch_lsoa_boundaries.main(tmp_path / "x.csv")
    with pytest.raises(ValueError, match="Point"):
        fetch_lsoa_boundaries._geometry_to_wkt({"type": "Point", "coordinates": [0, 0]})


def test_a_page_that_keeps_failing_raises_after_its_attempts(monkeypatch):
    calls = _fake_urlopen(monkeypatch, fetch_lsoa_msoa_lookup, lambda url: OSError("down"))
    with pytest.raises(OSError):
        fetch_lsoa_msoa_lookup._fetch_page(0, attempts=3)
    assert len(calls) == 3


def test_lsoa_msoa_lookup_writes_one_row_per_lsoa(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_lsoa_msoa_lookup, "EXPECTED_ROWS", 2)
    rows = [{"attributes": {"LSOA11CD": f"E0100000{i}", "LSOA11NM": f"L{i}", "MSOA11CD": "E02000001", "MSOA11NM": "M"}}
            for i in (2, 1)]
    _fake_urlopen(monkeypatch, fetch_lsoa_msoa_lookup, lambda url: {"features": rows if _offset(url) == 0 else []})
    out = tmp_path / "lu.csv"
    fetch_lsoa_msoa_lookup.main(out)
    assert out.read_text().splitlines() == [
        "lsoa11cd,lsoa11nm,msoa11cd,msoa11nm", "E01000001,L1,E02000001,M", "E01000002,L2,E02000001,M"]


def test_ptal_polygons_need_grid_ids_one_to_n(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_ptal_centroids, "EXPECTED_ROWS", 2)
    features = [{"properties": {"GridID": i}, "geometry": {"type": "Polygon", "coordinates": SQUARE}} for i in (1, 2)]
    _fake_urlopen(monkeypatch, fetch_ptal_centroids, lambda url: {"features": features if _offset(url) == 0 else []})
    out = tmp_path / "polygons.csv"
    fetch_ptal_centroids.main(out, geometry="polygon")
    assert out.read_text().splitlines()[1].startswith('1,"POLYGON((')
    bad = [{"properties": {"GridID": i}, "geometry": {"type": "Polygon", "coordinates": SQUARE}} for i in (1, 3)]
    _fake_urlopen(monkeypatch, fetch_ptal_centroids, lambda url: {"features": bad if _offset(url) == 0 else []})
    with pytest.raises(RuntimeError, match="1..N"):
        fetch_ptal_centroids.main(tmp_path / "bad.csv", geometry="polygon")
    with pytest.raises(ValueError, match="Unknown geometry"):
        fetch_ptal_centroids.main(tmp_path / "x.csv", geometry="hexagon")


def test_ptal_centroids_read_each_projection(monkeypatch):
    def answer(url):
        if _offset(url) > 0:
            return {"features": []}
        sr = parse_qs(urlsplit(url).query)["outSR"][0]
        return {"features": [{"attributes": {"GridID": 1}, "centroid": {"x": float(sr), "y": 1.0}},
                             {"attributes": {"GridID": 2}}]}

    _fake_urlopen(monkeypatch, fetch_ptal_centroids, answer)
    assert fetch_ptal_centroids.fetch_all(27700) == {1: (27700.0, 1.0)}


# ---------------------------------------------------------------------------
# UKHPI
# ---------------------------------------------------------------------------


def _ukhpi_csv(months: int, start_year: int = 1995) -> str:
    rows = [f"{start_year + m // 12}-{m % 12 + 1:02d}-01,London,1" for m in range(months)]
    return "Pivotable date,Name,Index\n" + "\n".join(rows) + "\n"


def test_ukhpi_url_validation_and_write(monkeypatch, tmp_path):
    url = fetch_ukhpi.build_url("1995-01-01", "2025-01-01")
    query = parse_qs(urlsplit(url).query)
    assert query["location"] == [fetch_ukhpi.LOCATION_URI] and query["format"] == ["csv"]
    text = _ukhpi_csv(fetch_ukhpi.MIN_EXPECTED_ROWS)
    _fake_urlopen(monkeypatch, fetch_ukhpi, lambda u: ("﻿" + text).encode("utf-8"))
    out = tmp_path / "ukhpi.csv"
    assert fetch_ukhpi.main(["--out", str(out)]) == 0
    assert out.read_text() == text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Pivotable date,Index\n", "no data rows"),
        ("Date,Index\n2020-01-01,1\n", "no 'Pivotable date'"),
        (_ukhpi_csv(10), "only 10"),
        (_ukhpi_csv(300, start_year=2000), "clamped"),
    ],
)
def test_ukhpi_refuses_a_bad_download(text, message):
    with pytest.raises(RuntimeError, match=message):
        fetch_ukhpi.validate(text, "1995-01-01")


# ---------------------------------------------------------------------------
# Ofsted and GIAS
# ---------------------------------------------------------------------------


def _xlsx(path, rows, sheet="Data"):
    shared = sorted({c for row in rows for c in row if isinstance(c, str)})
    cells = []
    for r, row in enumerate(rows, start=1):
        out = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, str):
                out.append(f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>')
            else:
                out.append(f'<c r="{ref}"><v>{value}</v></c>')
        cells.append(f'<row r="{r}">{"".join(out)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        book.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(cells)}</sheetData></worksheet>')


def test_an_xlsx_sheet_becomes_csv_skipping_empty_rows(tmp_path):
    _xlsx(tmp_path / "book.xlsx", [["URN", "Name"], [100001, "School A"], ["", ""], [100002, "School B"]])
    assert fetch_ofsted.xlsx_sheet_to_csv(tmp_path / "book.xlsx", "Data", tmp_path / "out.csv") == 3
    assert (tmp_path / "out.csv").read_text().splitlines() == ["URN,Name", "100001,School A", "100002,School B"]
    with pytest.raises(LookupError, match="no sheet"):
        fetch_ofsted.xlsx_sheet_to_csv(tmp_path / "book.xlsx", "Other", tmp_path / "x.csv")


def test_resolve_attachment_needs_exactly_one_match(monkeypatch):
    page = {"first_published_at": "2026-06-30T09:30:00Z", "details": {"attachments": [
        {"url": "https://assets/x/state_funded_2025.xlsx", "title": "2025"},
        {"url": "https://assets/x/methodology.pdf", "title": "notes"}]}}
    _fake_urlopen(monkeypatch, fetch_ofsted, lambda url: page)
    found = fetch_ofsted.resolve_attachment("some-slug", r"state_funded.*\.xlsx$")
    assert found == {"url": "https://assets/x/state_funded_2025.xlsx", "title": "2025", "page_published": "2026-06-30"}
    with pytest.raises(LookupError, match="0 attachment"):
        fetch_ofsted.resolve_attachment("some-slug", r"\.zip$")


def test_last_modified_reads_the_header_or_gives_none(monkeypatch):
    stamp = format_datetime(datetime(2026, 5, 1, 12, tzinfo=timezone.utc), usegmt=True)
    _fake_urlopen(monkeypatch, fetch_ofsted, lambda req: _Response(b"", {"Last-Modified": stamp}))
    assert fetch_ofsted._last_modified("https://x") == date(2026, 5, 1)
    _fake_urlopen(monkeypatch, fetch_ofsted, lambda req: OSError("down"))
    assert fetch_ofsted._last_modified("https://x") is None


def test_fetch_one_converts_validates_and_describes_an_inspection_workbook(monkeypatch, tmp_path):
    spec = OfstedFile(key="inyear_2024_25", era="ungraded", out_name="out.csv", coverage_from=date(2024, 9, 1),
                      coverage_to=date(2025, 8, 31), min_rows=1, slug="slug", attachment="x", sheet="Data")
    monkeypatch.setattr(fetch_ofsted, "resolve_attachment",
                        lambda slug, pattern: {"url": "https://assets/book.xlsx", "title": "T", "page_published": "2025-10-01"})
    monkeypatch.setattr(fetch_ofsted, "_download", lambda url, out: _xlsx(out, [["URN"], [1]]))
    monkeypatch.setattr(fetch_ofsted, "_last_modified", lambda url: None)
    monkeypatch.setattr(fetch_ofsted, "validate", lambda path, s: 1)
    entry = fetch_ofsted.fetch_one(spec, tmp_path)
    assert (tmp_path / "out.csv").read_text().splitlines() == ["URN", "1"]
    assert entry["published_date"] == "2025-10-01" and entry["converted_from"] == "book.xlsx"
    assert entry["sha256"] == fetch_ofsted._sha256(tmp_path / "out.csv")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.csv"]


def test_gias_looks_back_for_the_latest_daily_file(monkeypatch, tmp_path):
    spec = OfstedFile(key="gias_links", era="gias", out_name="links.csv", coverage_from=date(2026, 1, 1),
                      coverage_to=date(2026, 10, 1), min_rows=1, kind="gias_links", directory_key="gias_dir")
    tried = []

    def download(url, out):
        tried.append(url)
        if len(tried) < 3:
            raise subprocess.CalledProcessError(22, "curl")
        out.write_text("URN,LinkURN\n1,2\n")

    monkeypatch.setattr(fetch_ofsted, "_download", download)
    monkeypatch.setattr(fetch_ofsted, "validate", lambda path, s: 1)
    entry = fetch_ofsted.fetch_one(spec, tmp_path, today=date(2026, 10, 3))
    assert tried[0].endswith("links_edubasealldata20261003.csv")
    assert entry["published_date"] == "2026-10-01"
    monkeypatch.setattr(fetch_ofsted, "_download", lambda url, out: (_ for _ in ()).throw(subprocess.CalledProcessError(22, "curl")))
    with pytest.raises(RuntimeError, match="last 10 days"):
        fetch_ofsted.fetch_one(spec, tmp_path, today=date(2026, 10, 3))


# ---------------------------------------------------------------------------
# Sale-linking sources
# ---------------------------------------------------------------------------


def test_planning_pages_with_search_after_and_writes_csv(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_linking_sources, "PLANNING_PAGE_SIZE", 2)
    hits = [{"_id": f"app{i}", "sort": [f"app{i}"], "_source": {"uprn": 100 + i, "postcode": " SW1A 2AA ", "lpa_name": None}}
            for i in range(3)]
    bodies = []

    def answer(request):
        body = json.loads(request.data)
        bodies.append(body)
        after = body.get("search_after")
        start = 0 if after is None else int(after[0][3:]) + 1
        return {"hits": {"hits": hits[start:start + 2]}}

    _fake_urlopen(monkeypatch, fetch_linking_sources, answer)
    out = tmp_path / "planning.csv"
    fetch_linking_sources.fetch_planning(out)
    lines = out.read_text().splitlines()
    assert lines[0].split(",")[0] == "id" and len(lines) == 4
    assert lines[1].split(",")[:2] == ["app0", "100"] and "SW1A 2AA" in lines[1]
    assert [b.get("search_after") for b in bodies] == [None, ["app1"], ["app2"]]
    assert bodies[0]["query"] == {"exists": {"field": "uprn"}}


def test_nsul_is_renamed_after_its_london_member(monkeypatch, tmp_path):
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))

    def download(url, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("Data/NSUL_JUN_2026_LN.csv", "UPRN\n1\n")

    monkeypatch.setattr(fetch_linking_sources, "_download", download)
    fetch_linking_sources.fetch_nsul("item")
    assert [p.name for p in (tmp_path / "nsul").iterdir()] == ["NSUL_JUN_2026.zip"]


def test_nsul_without_a_london_member_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))

    def download(url, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("Data/NSUL_JUN_2026_EE.csv", "UPRN\n")

    monkeypatch.setattr(fetch_linking_sources, "_download", download)
    with pytest.raises(RuntimeError, match="_LN.csv"):
        fetch_linking_sources.fetch_nsul("item")


def test_lids_downloads_each_file_of_the_release(monkeypatch, tmp_path):
    monkeypatch.setenv("HPLDN_DATA_DIR", str(tmp_path))
    got = []
    monkeypatch.setattr(fetch_linking_sources, "_download", lambda url, out: got.append((url, out.name)))
    fetch_linking_sources.fetch_lids("2026-09")
    assert len(got) == len(fetch_linking_sources.LIDS_FILES)
    assert all("2026-09" in url and name.startswith("lids-2026-09_csv_") for url, name in got)
