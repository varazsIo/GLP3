"""
Unit tests for the revisions after the baseline (0017-0034) that need no database.

Covers `chain.load_revision`, 0017's choice of the first back-filled month and
its monthly completeness guard, the view's ``is_maisonette`` fallback, and
the Ofsted registry, readers and column maps (0033) and view columns (0034).
"""

from datetime import date
import logging

import pytest

from hpldn.backend.migrations import chain
from hpldn.backend.migrations.london_houses_view import (
    CREATE_VIEW_SQL,
    CREATE_VIEW_SQL_0020,
    CREATE_VIEW_SQL_0025,
    OFSTED_VIEW_COLUMNS,
    VIEW_COLUMNS,
    VIEW_COLUMNS_0020,
    VIEW_COLUMNS_0025,
)

LOG = logging.getLogger("test_data_repair_revisions")


def _gap():
    return chain.load_revision("0017")


def test_load_revision_returns_module_without_running_upgrade():
    """Check `load_revision` imports a revision by id and exposes its helpers."""
    module = chain.load_revision("0011")
    assert module.revision == "0011"
    assert callable(module._rebuild_lsoa_msoa_features)


def test_load_revision_rejects_unknown_id():
    """Check an id with no file raises `LookupError`."""
    with pytest.raises(LookupError):
        chain.load_revision("9999")


def test_chain_is_linear_to_head():
    """Check 0017-0034 chain onto 0016 and CHAIN_HEAD names the last one."""
    previous = chain.BASELINE_HEAD
    for revision_id in (
        "0017", "0018", "0019", "0020", "0021", "0022", "0023", "0024", "0025", "0026",
        "0027", "0028", "0029", "0030", "0031", "0032", "0033", "0034",
    ):
        module = chain.load_revision(revision_id)
        assert module.down_revision == previous
        previous = module.revision
    assert chain.CHAIN_HEAD == previous


def test_0022_widens_the_search_universe_it_shares_with_the_addresses_route():
    """Check 0022 reuses 0021's checks and /addresses reads the same predicate as the build."""
    from hpldn.backend.api.market import pricing
    from hpldn.backend.migrations import address_search

    module = chain.load_revision("0022")
    previous = chain.load_revision("0021")
    assert module.ADDRESS_SEARCH_NO_EPC_ROWS_EXPECTED > previous.ADDRESS_SEARCH_ROWS_EXPECTED
    predicate = address_search._PRICEABLE_PREDICATE.strip()
    assert predicate in address_search.CREATE_TABLE_SQL
    assert predicate in pricing.ADDRESSES_SQL.text
    assert "residential_postcodes AS" in pricing.ADDRESSES_SQL.text
    assert "AND a.postcode = :postcode" in pricing.ADDRESSES_SQL.text
    assert "production.london_houses" in predicate and "a.postnumber ~" in predicate


@pytest.mark.tier_a
def test_0023_seeds_every_borough_ons_publishes_and_no_stand_in_for_the_city():
    """Check the rents fixture holds one July 2026 rent per borough, none for the City."""
    module = chain.load_revision("0023")
    rows = module.read_fixture()
    boroughs = {row["local_authority"] for row in rows}
    assert len(boroughs) == len(rows) == module.EXPECTED_BOROUGHS == 32
    assert "City of London" not in boroughs
    assert {"Wandsworth", "Westminster", "Kingston upon Thames"} <= boroughs
    assert all(row["mean_monthly_rent"] % 25 == 0 and 1000 < row["mean_monthly_rent"] < 5000 for row in rows)
    assert {row["month"] for row in rows} == {date(2026, 7, 1)}


@pytest.mark.tier_a
def test_0026_fixture_covers_london_to_aug_2026():
    """Check the London rents fixture: 33 areas, no City, Jan 2015-Aug 2026, 9 measures, complete Aug 2026."""
    from hpldn.backend.migrations import london_rents

    module = chain.load_revision("0026")
    rows = london_rents.read_fixture()
    codes = {row["area_code"] for row in rows}
    assert len(codes) == module.EXPECTED_AREA_CODES == 33
    assert "E12000007" in codes and module.CITY_OF_LONDON not in codes
    assert "City of London" not in {row["area_name"] for row in rows}
    months = sorted({row["month"] for row in rows})
    assert (months[0], months[-1], len(months)) == (date(2015, 1, 1), module.LATEST_MONTH, 140)
    assert {row["measure"] for row in rows} == set(london_rents.MEASURES)
    assert len(rows) == 33 * 140 * 9
    latest = {
        (row["area_name"], row["measure"]): row["rental_price"]
        for row in rows
        if row["month"] == module.LATEST_MONTH and row["area_code"].startswith("E09")
    }
    boroughs = {name for name, _ in latest}
    assert len(boroughs) == module.EXPECTED_BOROUGHS == 32
    for borough in boroughs:
        for measure in module.REQUIRED_MEASURES:
            assert latest[(borough, measure)] is not None, (borough, measure)
    assert latest[("Wandsworth", "all")] == 2632
    assert [latest[("Wandsworth", m)] for m in london_rents.BEDROOM_MEASURES.values()] == [1938, 2463, 2810, 3818]
    assert {row["source"] for row in rows} == {"ONS Price Index of Private Rents (release 2026-09-16)"}


@pytest.mark.tier_a
def test_0032_fixture_covers_cpih_from_1989_to_aug_2026():
    """Check the CPIH fixture: every month Jan 1989-Aug 2026, ONS's published rates."""
    from hpldn.backend.migrations import cpih

    module = chain.load_revision("0032")
    rates = cpih.read_rates()
    months = list(rates)
    assert (months[0], months[-1], len(months)) == (module.FIRST_MONTH, module.LATEST_MONTH, module.EXPECTED_MONTHS)
    assert (rates[date(1989, 1, 1)], rates[date(2017, 8, 1)], rates[date(2026, 8, 1)]) == (5.7, 2.7, 3.3)
    rows = module.rows()
    assert len(rows) == 452 and {row["source"] for row in rows} == {cpih.SOURCE}


@pytest.mark.tier_a
def test_cpih_index_chains_the_annual_rates_and_averages_100_in_2015():
    """A ratio whole years apart is the product of the rates; 2015 averages 100."""
    from hpldn.backend.migrations import cpih

    rates = cpih.read_rates()
    index = cpih.build_index(rates)
    assert list(index) == list(rates)
    base = [value for month, value in index.items() if month.year == 2015]
    assert sum(base) / 12 == pytest.approx(100)
    chained = 1.0
    for year in range(2018, 2027):
        chained *= 1 + rates[date(year, 8, 1)] / 100
    assert index[date(2026, 8, 1)] / index[date(2017, 8, 1)] == pytest.approx(chained)
    # The guessed 1988 start leaves no seam between calendar months: 1989 rises month by month.
    year_1989 = [index[date(1989, month, 1)] for month in range(1, 13)]
    assert year_1989 == sorted(year_1989)


def test_cpih_rates_must_be_consecutive(tmp_path):
    """A missing month is refused rather than chained across."""
    from hpldn.backend.migrations import cpih

    gappy = tmp_path / "cpih.csv"
    gappy.write_text('year_month,percentage\n"1989 JAN","5.7"\n"1989 MAR","5.9"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no rate between"):
        cpih.read_rates(gappy)
    assert cpih.parse_month("2017 aug") == date(2017, 8, 1)


def test_london_rents_extract_skips_the_preamble_and_keeps_london(tmp_path):
    """Check `extract` drops the title rows and other regions, blanks [x] and parses ``Aug-26``."""
    from hpldn.backend.migrations import london_rents

    columns = ["Time period", "Area code", "Area name", "Region or country name"]
    for suffix in london_rents.MEASURES.values():
        columns += [f"Index{suffix}", f"Monthly change{suffix}", f"Annual change{suffix}", f"Rental price{suffix}"]
    figures = ",".join(["120.5,0.4,[x],2000.0"] + ["121.0,[x],3.1,[x]"] * 8)
    source = tmp_path / "private_rents_monthly_prices.csv"
    source.write_text(
        "\n".join(
            [
                '"Price Index of Private Rents, UK: monthly price statistics",,,',
                "This worksheet contains one table.,,,",
                ",".join(columns),
                f"Aug-26,E09000032,Wandsworth,London,{figures}",
                f"Aug-26,E12000007,London,[z],{figures}",
                f"Aug-26,E06000001,Hartlepool,North East,{figures}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "rents.csv"
    assert london_rents.extract(source, fixture, release=date(2026, 9, 16)) == 2 * 9
    rows = london_rents.read_fixture(fixture)
    assert {row["area_name"] for row in rows} == {"Wandsworth", "London"}
    assert {row["month"] for row in rows} == {date(2026, 8, 1)}
    overall = next(row for row in rows if row["area_name"] == "Wandsworth" and row["measure"] == "all")
    assert (overall["price_index"], overall["monthly_change"], overall["annual_change"]) == (120.5, 0.4, None)
    assert overall["rental_price"] == 2000
    one_bed = next(row for row in rows if row["area_name"] == "Wandsworth" and row["measure"] == "one_bed")
    assert (one_bed["monthly_change"], one_bed["annual_change"], one_bed["rental_price"]) == (None, 3.1, None)
    assert overall["source"] == "ONS Price Index of Private Rents (release 2026-09-16)"


def test_add_months_crosses_year_boundaries():
    """Check month arithmetic in both directions."""
    gap = _gap()
    assert gap._add_months(date(2024, 11, 1), -6) == date(2024, 5, 1)
    assert gap._add_months(date(2024, 2, 15), -3) == date(2023, 11, 1)
    assert gap._add_months(date(2024, 12, 1), 1) == date(2025, 1, 1)


def _completeness(shares, start=date(2024, 1, 1), raw=10_000):
    gap = _gap()
    return {
        gap._add_months(start, i): (int(share * raw), raw)
        for i, share in enumerate(shares)
    }


def test_choose_backfill_start_is_clamped_to_guard_window():
    """Check a late drop still back-fills the six months before the cut-over."""
    gap = _gap()
    # Jan-Oct 2024 shares from hpdbldn: the drop below 80 % of baseline is June.
    shares = [0.56, 0.52, 0.49, 0.50, 0.48, 0.40, 0.34, 0.32, 0.31, 0.14, 0.93]
    start = gap.choose_backfill_start(_completeness(shares), date(2024, 11, 1))
    assert start == date(2024, 5, 1)


def test_choose_backfill_start_picks_an_earlier_drop():
    """Check a drop earlier than the guard window moves the start earlier."""
    gap = _gap()
    shares = [0.55, 0.55, 0.55, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.93]
    start = gap.choose_backfill_start(_completeness(shares), date(2024, 11, 1))
    assert start == date(2024, 4, 1)


def test_check_monthly_completeness_names_failing_months():
    """Check the guard ignores months before its start and lists the failures."""
    gap = _gap()
    completeness = _completeness([0.10, 0.93, 0.59, 0.93])
    gap.check_monthly_completeness(completeness, date(2024, 4, 1), "t", LOG)
    with pytest.raises(RuntimeError, match="2024-03"):
        gap.check_monthly_completeness(completeness, date(2024, 2, 1), "t", LOG)


def test_view_falls_back_to_certificate_property_type_for_maisonettes():
    """Check ``is_maisonette`` reads the certificate and keeps its column position."""
    for sql, columns, source in (
        (CREATE_VIEW_SQL_0020, VIEW_COLUMNS_0020, "e"),
        (CREATE_VIEW_SQL_0025, VIEW_COLUMNS_0025, "t"),
        (CREATE_VIEW_SQL, VIEW_COLUMNS, "t"),
    ):
        assert "ec.property_type," in sql
        line = next(l for l in sql.splitlines() if l.strip().endswith("AS is_maisonette,"))
        assert "COALESCE(mai.is_maisonette" in line
        assert f"{source}.property_type = 'Maisonette'" in line
        assert "p.propertytype = 'F'" in line
        assert columns.index("is_maisonette") == 41


def test_0025_view_appends_its_columns_and_keeps_the_rating_as_of_the_sale():
    """Only the time-invariant fields fall back to a later certificate."""
    assert VIEW_COLUMNS_0025[: len(VIEW_COLUMNS_0020)] == VIEW_COLUMNS_0020
    assert VIEW_COLUMNS_0025[len(VIEW_COLUMNS_0020):] == ("street_key", "epc_after_sale")
    selected = {l.strip().rstrip(",") for l in CREATE_VIEW_SQL_0025.split("FROM production.ppd_table p")[0].splitlines()}
    assert "e.current_energy_rating" in selected
    assert "e.tenure" in selected
    assert "t.total_floor_area" in selected
    assert "(e.total_floor_area IS NOT NULL AND e.current_energy_rating IS NOT NULL) AS epc_matched" in selected
    assert "ec.inspection_date > p.dateoftransfer" in CREATE_VIEW_SQL_0025
    assert "ec.inspection_date > p.dateoftransfer" not in CREATE_VIEW_SQL_0020


def test_0034_view_appends_the_ofsted_columns_and_earlier_revisions_keep_theirs():
    """0034's view is 0025's plus the twelve ofsted_* columns; 0025/0028/0029 never join ofsted_features."""
    from hpldn.backend.migrations import link_carry
    from hpldn.backend.scripts import ofsted_features

    assert VIEW_COLUMNS == VIEW_COLUMNS_0025 + OFSTED_VIEW_COLUMNS
    assert list(OFSTED_VIEW_COLUMNS) == ofsted_features.OFSTED_COLUMNS
    assert len(VIEW_COLUMNS) == 107
    assert "production.ofsted_features ofs ON ofs.ppd_id = p.id" in CREATE_VIEW_SQL
    assert "ofsted" not in CREATE_VIEW_SQL_0025
    defaults = link_carry.rebuild_production.__defaults__
    assert CREATE_VIEW_SQL_0025 in defaults and VIEW_COLUMNS_0025 in defaults
    revision_0025 = chain.load_revision("0025")
    assert revision_0025.CREATE_VIEW_SQL_0025 is CREATE_VIEW_SQL_0025


def test_ofsted_files_cover_every_inspection_year_from_2005_to_2026():
    """The inspection files tile Sep 2005-Aug 2026 without a gap; each era has a column map and requirements."""
    from hpldn.backend.migrations import ofsted

    timeline = sorted(
        (f.coverage_from, f.coverage_to) for f in ofsted.INSPECTION_FILES if f.era != "cross_check"
    )
    assert timeline[0][0] == ofsted.FIRST_INSPECTION and timeline[-1][1] == ofsted.LAST_INSPECTION
    for (_, end), (start, _) in zip(timeline, timeline[1:]):
        assert (start - end).days == 1, (end, start)
    for spec in ofsted.INSPECTION_FILES:
        assert spec.slug and spec.attachment and spec.directory_key == "ofsted_dir"
        assert set(ofsted.REQUIRED_COLUMNS[spec.era]) <= set(ofsted.COLUMN_MAPS[spec.era].values())
        assert set(ofsted.COLUMN_MAPS[spec.era].values()) <= set(ofsted.RAW_COLUMNS)
    assert {f.directory_key for f in ofsted.FILES if f.kind != "inspections"} == {"gias_dir"}
    assert len(ofsted.LONDON_LA_CODES) == 33
    assert [ofsted.era_of(d) for d in (date(2008, 1, 1), date(2016, 1, 1), date(2023, 1, 1),
                                        date(2025, 1, 1), date(2025, 11, 9), date(2026, 1, 1))] == [
        "pre2015", "cif_2015", "eif_2019", "eif_2024_no_oe", "eif_2024_no_oe", "report_card_2025"]


def test_ofsted_reader_skips_title_rows_and_maps_headers(tmp_path):
    """A title row above the header, cp1252 text and header variants all read into RAW_COLUMNS."""
    from hpldn.backend.migrations import ofsted

    path = tmp_path / "f.csv"
    path.write_bytes(
        "Title row for 2024/25,,,,\n"
        "Web Link (opens in new window),URN,School name,Inspection number,Inspection type,"
        "Inspection start date,Publication date,Quality of education,Behaviour and attitudes,"
        "Personal development,Effectiveness of leadership and management,Safeguarding is effective?,"
        "Outcomes for ungraded and monitoring inspections\n"
        "http://x,100000,St Mary\u2019s,10344003,S5 Inspection,24/09/2024,10/11/2024,2,2,1,2,Yes,\n"
        ",,,,,,,,,,,,\n".encode("cp1252")
    )
    frame = ofsted.read_inspections(path, "eif_2024_no_oe")
    assert len(frame) == 1
    row = frame.iloc[0]
    assert (row["urn"], row["school_name"], row["personal_development"], row["safeguarding"]) == (
        "100000", "St Mary\u2019s", "1", "Yes")
    assert list(frame.columns) == list(ofsted.RAW_COLUMNS) + ["source_row"]
    assert row["overall_effectiveness"] is None
    with pytest.raises(ValueError, match="overall_effectiveness"):
        ofsted.read_inspections(path, "eif_2019")


def test_fetch_ofsted_converts_a_workbook_sheet_to_csv(tmp_path):
    """The 2005-2015 file is a workbook; the stdlib converter keeps shared strings, numbers and gaps."""
    import csv
    import zipfile

    from hpldn.backend.scripts.fetch_ofsted import xlsx_sheet_to_csv

    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    book = tmp_path / "b.xlsx"
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets>'
                   '<sheet name="Cover" sheetId="1" r:id="rId1"/>'
                   '<sheet name="2005-2015 Inspections" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="/xl/worksheets/sheet9.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}><si><t>URN</t></si><si><t>Overall effectiveness</t></si>'
                   '<si><r><t>Beer </t></r><r><t>Primary</t></r></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData/></worksheet>')
        z.writestr("xl/worksheets/sheet9.xml", f'<worksheet {ns}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
                   '<row r="2"/>'
                   '<row r="3"><c r="A3"><v>113424</v></c><c r="B3" t="s"><v>2</v></c><c r="C3"><v>2</v></c>'
                   '<c r="D3" t="inlineStr"><is><t>38607</t></is></c></row>'
                   '</sheetData></worksheet>')
    out = tmp_path / "o.csv"
    assert xlsx_sheet_to_csv(book, "2005-2015 Inspections", out) == 2
    with open(out, newline="", encoding="utf-8") as handle:
        assert list(csv.reader(handle)) == [["URN", "", "Overall effectiveness"], ["113424", "Beer Primary", "2", "38607"]]
    with pytest.raises(LookupError):
        xlsx_sheet_to_csv(book, "Missing", out)


def test_0024_searches_street_less_homes_under_their_postcodes_one_street():
    """Check the hint feeds search_text and name_text only, never the returned poststreet."""
    from hpldn.backend.migrations import address_search

    module = chain.load_revision("0024")
    sql = address_search.CREATE_TABLE_SQL
    # Streets compared by their key since 0025, so "13 The Gardens" is not a second street.
    assert "HAVING count(DISTINCT production.hpldn_street_key(poststreet)) = 1" in sql
    assert "coalesce(nullif(h.street_norm, ''), t.street)" in sql
    assert "coalesce(nullif(h.street_norm, ''), t.street_norm, h.num_norm) AS name_text" in sql
    assert "       h.poststreet,\n" in sql  # the register's street, untouched
    assert "t.street AS street_hint" in sql
    assert module.EXPECTED_COLUMNS[:-1] == chain.load_revision("0021").EXPECTED_COLUMNS
    assert module.STREET_HINTS_EXPECTED > 90_000
