"""
Ofsted state-funded school inspections and DfE school locations, for migration 0033.

`FILES` is the registry both ``scripts/fetch_ofsted.py`` (which downloads
them into ``data/ofsted/`` and writes ``manifest.json``) and migration 0033
(which loads them into ``staging``) read. Ofsted changed its inspection
framework four times between September 2005 and August 2026 and its files
changed shape with it, so each file belongs to an era:

``pre2015``
    Section 5 inspections, September 2005 - August 2015 (one-off MI file,
    XLSX converted to CSV at fetch time). Dates are Excel serial numbers.
``cif_2015``
    Common Inspection Framework, September 2015 - August 2019 (one-off MI
    file): graded inspections, short (section 8) inspections and monitoring.
``eif_2019``
    Education Inspection Framework, September 2019 - August 2024 (the
    in-year CSV of each "as at 31 August" release).
``eif_2024_no_oe``
    September 2024 - August 2025: the same framework without an overall
    effectiveness grade, so only the four key judgements.
``report_card_2025``
    Report cards from November 2025: six evaluation areas on a five-point
    scale and safeguarding met or not met.
``cross_check``
    The "latest inspections as at 31 August 2026" MI file: one row per open
    school with its latest report card, graded and ungraded inspections.
    It fills inspections a year's file missed (published after the release).

`COLUMN_MAPS` maps each era's headers, normalised by `normalise_header`, to
the common names in `RAW_COLUMNS`; everything stays text until 0033's SQL
cleans it. All files are Open Government Licence v3.0.
"""

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

SOURCE = (
    "Ofsted state-funded school inspections 2005-2026 and DfE Get Information about Schools; "
    "Open Government Licence v3.0"
)

ATTRIBUTION = (
    "School inspections from Ofsted; school locations from DfE Get Information about Schools. "
    "Contains public sector information licensed under the Open Government Licence v3.0."
)

MANIFEST_NAME = "manifest.json"

# Directories under data/ the files are fetched into, by `OfstedFile.directory_key`.
DEFAULT_DIRECTORIES = {"ofsted_dir": "ofsted", "gias_dir": "gias"}

# Ofsted's statistics collection pages (GOV.UK Content API slugs).
MI_SLUG = "government/statistical-data-sets/monthly-management-information-ofsteds-school-inspections-outcomes"
RELEASE_SLUG = "government/statistics/state-funded-schools-inspections-and-outcomes-as-at-31-august-{year}"

# Inspections before or after these never enter production.ofsted.
FIRST_INSPECTION = date(2005, 9, 1)
LAST_INSPECTION = date(2026, 8, 31)

# Framework changes: the first day of each era, by inspection start date.
ERA_STARTS = (
    ("pre2015", date(2005, 9, 1)),
    ("cif_2015", date(2015, 9, 1)),
    ("eif_2019", date(2019, 9, 1)),
    ("eif_2024_no_oe", date(2024, 9, 1)),
    ("report_card_2025", date(2025, 11, 10)),
)
# "Satisfactory" became "Requires improvement" for inspections from this date.
REQUIRES_IMPROVEMENT_FROM = date(2012, 9, 1)

# A missing publication date is imputed as the inspection's last day plus this.
IMPUTED_PUBLICATION_LAG_DAYS = 35

# DfE local authority codes of the 33 London authorities (City of London 201,
# inner 202-213, outer 301-320).
LONDON_LA_CODES = tuple([201] + list(range(202, 214)) + list(range(301, 321)))
# Schools this far outside the GLA boundary are kept, so homes near it see them.
BOUNDARY_BUFFER_M = 3000


@dataclass(frozen=True)
class OfstedFile:
    """One source file: where GOV.UK publishes it and what 0033 expects of it."""

    key: str
    era: str
    out_name: str
    coverage_from: date
    coverage_to: date
    min_rows: int
    # GOV.UK page and a pattern its attachment's file name matches; None for GIAS.
    slug: Optional[str] = None
    attachment: Optional[str] = None
    # Sheet to convert to CSV when the attachment is a workbook.
    sheet: Optional[str] = None
    kind: str = "inspections"  # inspections | gias_establishments | gias_links
    # Configuration key (``data:`` block of staging.yaml) of the directory it lives in.
    directory_key: str = "ofsted_dir"


def _in_year(year: int, era: str, min_rows: int) -> OfstedFile:
    return OfstedFile(
        key=f"inyear_{year - 1}_{year % 100:02d}",
        era=era,
        out_name=f"ofsted_inspections_{year - 1}_{year % 100:02d}.csv",
        coverage_from=date(year - 1, 9, 1),
        coverage_to=date(year, 8, 31),
        min_rows=min_rows,
        slug=RELEASE_SLUG.format(year=year),
        attachment=rf"between_+1_September_{year - 1}_and_31_August_{year}\.csv$",
    )


FILES: Tuple[OfstedFile, ...] = (
    OfstedFile(
        key="mi_2005_2015",
        era="pre2015",
        out_name="ofsted_inspections_2005_2015.csv",
        coverage_from=date(2005, 9, 1),
        coverage_to=date(2015, 8, 31),
        min_rows=60_000,
        slug=MI_SLUG,
        attachment=r"1_Sept_2005_to_31_August_2015\.xlsx$",
        sheet="2005-2015 Inspections",
    ),
    OfstedFile(
        key="mi_2015_2019",
        era="cif_2015",
        out_name="ofsted_inspections_2015_2019.csv",
        coverage_from=date(2015, 9, 1),
        coverage_to=date(2019, 8, 31),
        min_rows=20_000,
        slug=MI_SLUG,
        attachment=r"1_September_2015_to_31_August_2019\.csv$",
    ),
    _in_year(2020, "eif_2019", 3_000),
    _in_year(2021, "eif_2019", 1_200),
    _in_year(2022, "eif_2019", 4_000),
    _in_year(2023, "eif_2019", 6_500),
    _in_year(2024, "eif_2019", 6_000),
    _in_year(2025, "eif_2024_no_oe", 5_500),
    OfstedFile(
        key="mi_2025_26",
        era="report_card_2025",
        out_name="ofsted_inspections_2025_26.csv",
        coverage_from=date(2025, 9, 1),
        coverage_to=date(2026, 8, 31),
        min_rows=2_000,
        slug=MI_SLUG,
        attachment=r"all_inspections_-_year_to_date_published_by_31_August_2026\.csv$",
    ),
    OfstedFile(
        key="mi_latest_2026_08",
        era="cross_check",
        out_name="ofsted_latest_inspections_2026_08_31.csv",
        coverage_from=date(2005, 9, 1),
        coverage_to=date(2026, 8, 31),
        min_rows=20_000,
        slug=MI_SLUG,
        attachment=r"latest_inspections_as_at_31_August_2026\.csv$",
    ),
    OfstedFile(
        key="gias_establishments",
        era="gias",
        out_name="gias_establishments.csv",
        coverage_from=date(1900, 1, 1),
        coverage_to=date(2026, 8, 31),
        min_rows=50_000,
        kind="gias_establishments",
        directory_key="gias_dir",
    ),
    OfstedFile(
        key="gias_links",
        era="gias",
        out_name="gias_links.csv",
        coverage_from=date(1900, 1, 1),
        coverage_to=date(2026, 8, 31),
        min_rows=30_000,
        kind="gias_links",
        directory_key="gias_dir",
    ),
)

FILES_BY_KEY = {f.key: f for f in FILES}
INSPECTION_FILES = tuple(f for f in FILES if f.kind == "inspections")

# Every staging.ofsted_inspections_raw value column, in order.
RAW_COLUMNS = (
    "urn",
    "laestab",
    "school_name",
    "ofsted_phase",
    "type_of_education",
    "local_authority",
    "postcode",
    "predecessor_urn",
    "inspected_urn",
    "inspection_number",
    "inspection_type",
    "inspection_type_grouping",
    "event_type_grouping",
    "inspection_start_date",
    "inspection_end_date",
    "publication_date",
    "overall_effectiveness",
    "category_of_concern",
    "quality_of_education",
    "behaviour_and_attitudes",
    "personal_development",
    "leadership_and_management",
    "safeguarding",
    "converted_to_graded",
    "ungraded_outcome",
    "rc_inclusion",
    "rc_curriculum_and_teaching",
    "rc_achievement",
    "rc_attendance_and_behaviour",
    "rc_personal_development",
    "rc_leadership_and_governance",
    "prev_inspection_number",
    "prev_inspection_start_date",
    "prev_inspection_end_date",
    "prev_publication_date",
    "prev_urn",
    "prev_overall_effectiveness",
    "prev_category_of_concern",
    "prev_quality_of_education",
    "prev_behaviour_and_attitudes",
    "prev_personal_development",
    "prev_leadership_and_management",
    "oeif_inspection_number",
    "oeif_inspection_type",
    "oeif_event_type_grouping",
    "oeif_inspection_start_date",
    "oeif_publication_date",
    "oeif_urn",
    "oeif_overall_effectiveness",
    "oeif_category_of_concern",
    "oeif_quality_of_education",
    "oeif_behaviour_and_attitudes",
    "oeif_personal_development",
    "oeif_leadership_and_management",
    "oeif_safeguarding",
    "ug_inspection_number",
    "ug_inspection_start_date",
    "ug_publication_date",
    "ug_urn",
    "ug_outcome",
)


def normalise_header(header: str) -> str:
    """Return a header lower-cased, single-spaced, without "(opens in new window)" or a trailing "?"."""
    value = re.sub(r"\(opens in new window\)", "", str(header), flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip().rstrip("?").strip().lower()
    return value


# Headers every in-year file (2015-2026) shares.
_IN_YEAR_COMMON = {
    "urn": "urn",
    "laestab": "laestab",
    "school name": "school_name",
    "ofsted phase": "ofsted_phase",
    "type of education": "type_of_education",
    "local authority": "local_authority",
    "postcode": "postcode",
    "inspection number": "inspection_number",
    "inspection type": "inspection_type",
    "inspection type grouping": "inspection_type_grouping",
    "event type grouping": "event_type_grouping",
    "inspection start date": "inspection_start_date",
    "inspection end date": "inspection_end_date",
    "publication date": "publication_date",
    "category of concern": "category_of_concern",
    "previous inspection number": "prev_inspection_number",
    "previous inspection start date": "prev_inspection_start_date",
    "previous inspection end date": "prev_inspection_end_date",
    "previous publication date": "prev_publication_date",
    "urn at time of previous inspection": "prev_urn",
    "previous overall effectiveness": "prev_overall_effectiveness",
    "previous category of concern": "prev_category_of_concern",
    "previous effectiveness of leadership and management": "prev_leadership_and_management",
}

_EIF_JUDGEMENTS = {
    "quality of education": "quality_of_education",
    "behaviour and attitudes": "behaviour_and_attitudes",
    "personal development": "personal_development",
    "effectiveness of leadership and management": "leadership_and_management",
    "previous quality of education": "prev_quality_of_education",
    "previous behaviour and attitudes": "prev_behaviour_and_attitudes",
    "previous personal development": "prev_personal_development",
}

_REPORT_CARD_AREAS = {
    "inclusion": "rc_inclusion",
    "curriculum and teaching": "rc_curriculum_and_teaching",
    "achievement": "rc_achievement",
    "attendance and behaviour": "rc_attendance_and_behaviour",
    "personal development and wellbeing": "rc_personal_development",
    "leadership and governance": "rc_leadership_and_governance",
}

COLUMN_MAPS: Dict[str, Dict[str, str]] = {
    "pre2015": {
        "urn": "urn",
        "laestab": "laestab",
        "school name": "school_name",
        "post code": "postcode",
        "local authority": "local_authority",
        "type of school": "type_of_education",
        "ofsted phase": "ofsted_phase",
        "predecessor urn of schools converting to academy": "predecessor_urn",
        "inspection number": "inspection_number",
        "inspection type": "inspection_type",
        "inspection start date": "inspection_start_date",
        "inspection end date": "inspection_end_date",
        "first published date": "publication_date",
        "overall effectiveness": "overall_effectiveness",
        "category": "category_of_concern",
        "leadership and management": "leadership_and_management",
    },
    "cif_2015": {
        **_IN_YEAR_COMMON,
        "overall effectiveness": "overall_effectiveness",
        "effectiveness of leadership and management": "leadership_and_management",
        "is safeguarding effective": "safeguarding",
        "did the latest s8 inspection convert to a full inspection": "converted_to_graded",
        "outcomes for s8 inspections that did not convert": "ungraded_outcome",
    },
    "eif_2019": {
        **_IN_YEAR_COMMON,
        **_EIF_JUDGEMENTS,
        "overall effectiveness": "overall_effectiveness",
        "safeguarding is effective": "safeguarding",
        "did the latest section 8 inspection of a good or non-exempt outstanding school "
        "convert to a full inspection": "converted_to_graded",
        "did the latest section 8 inspection of a good or outstanding school convert to a "
        "full inspection": "converted_to_graded",
        "did the latest ungraded inspection convert to a graded inspection": "converted_to_graded",
        "outcomes for section 8 inspections": "ungraded_outcome",
        "outcomes for ungraded and monitoring inspections": "ungraded_outcome",
    },
    "eif_2024_no_oe": {
        **_IN_YEAR_COMMON,
        **_EIF_JUDGEMENTS,
        "safeguarding is effective": "safeguarding",
        "did the latest ungraded inspection convert to a graded inspection": "converted_to_graded",
        "outcomes for ungraded and monitoring inspections": "ungraded_outcome",
    },
    "report_card_2025": {
        **_IN_YEAR_COMMON,
        **_REPORT_CARD_AREAS,
        "safeguarding standards": "safeguarding",
        "outcomes for legacy monitoring inspections": "ungraded_outcome",
    },
    "cross_check": {
        "urn": "urn",
        "laestab": "laestab",
        "school name": "school_name",
        "ofsted phase": "ofsted_phase",
        "type of education": "type_of_education",
        "local authority": "local_authority",
        "postcode": "postcode",
        "inspection number of latest full inspection": "inspection_number",
        "inspection type": "inspection_type",
        "inspection type grouping": "inspection_type_grouping",
        "event type grouping": "event_type_grouping",
        "inspection start date": "inspection_start_date",
        "publication date": "publication_date",
        "urn at time of latest full inspection": "inspected_urn",
        "category of concern": "category_of_concern",
        "safeguarding standards": "safeguarding",
        **_REPORT_CARD_AREAS,
        "inspection number of latest oeif graded inspection": "oeif_inspection_number",
        "inspection type of latest oeif graded inspection": "oeif_inspection_type",
        "event type grouping of latest oeif graded inspection": "oeif_event_type_grouping",
        "inspection start date of latest oeif graded inspection": "oeif_inspection_start_date",
        "publication date of latest oeif graded inspection": "oeif_publication_date",
        "urn at time of latest oeif graded inspection": "oeif_urn",
        "latest oeif category of concern": "oeif_category_of_concern",
        "latest oeif overall effectiveness": "oeif_overall_effectiveness",
        "latest oeif quality of education": "oeif_quality_of_education",
        "latest oeif behaviour and attitudes": "oeif_behaviour_and_attitudes",
        "latest oeif personal development": "oeif_personal_development",
        "latest oeif effectiveness of leadership and management": "oeif_leadership_and_management",
        "latest oeif safeguarding is effective": "oeif_safeguarding",
        "latest ungraded inspection number": "ug_inspection_number",
        "date of latest ungraded inspection": "ug_inspection_start_date",
        "ungraded inspection publication date": "ug_publication_date",
        "urn at time of the ungraded inspection": "ug_urn",
        "ungraded inspection overall outcome": "ug_outcome",
    },
}

# Common names each era's file must provide; `validate_columns` checks them.
REQUIRED_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "pre2015": ("urn", "inspection_number", "inspection_start_date", "publication_date", "overall_effectiveness"),
    "cif_2015": (
        "urn", "inspection_number", "inspection_type", "inspection_start_date", "publication_date",
        "overall_effectiveness", "ungraded_outcome", "prev_inspection_number",
    ),
    "eif_2019": (
        "urn", "inspection_number", "inspection_type", "inspection_start_date", "publication_date",
        "overall_effectiveness", "quality_of_education", "leadership_and_management", "ungraded_outcome",
        "prev_inspection_number",
    ),
    "eif_2024_no_oe": (
        "urn", "inspection_number", "inspection_type", "inspection_start_date", "publication_date",
        "quality_of_education", "behaviour_and_attitudes", "personal_development",
        "leadership_and_management", "ungraded_outcome",
    ),
    "report_card_2025": (
        "urn", "inspection_number", "inspection_type", "inspection_start_date", "publication_date",
        "safeguarding", *_REPORT_CARD_AREAS.values(),
    ),
    "cross_check": (
        "urn", "inspection_number", "inspection_start_date", "publication_date", "inspected_urn",
        "oeif_inspection_number", "oeif_overall_effectiveness", "oeif_publication_date",
        "ug_inspection_number", "ug_outcome",
    ),
}

GIAS_COLUMNS = {
    "URN": "urn",
    "LA (code)": "la_code",
    "LA (name)": "la_name",
    "EstablishmentName": "school_name",
    "TypeOfEstablishment (name)": "establishment_type",
    "EstablishmentTypeGroup (name)": "establishment_group",
    "EstablishmentStatus (name)": "status",
    "OpenDate": "open_date",
    "CloseDate": "close_date",
    "PhaseOfEducation (name)": "phase_of_education",
    "StatutoryLowAge": "statutory_low_age",
    "StatutoryHighAge": "statutory_high_age",
    "Postcode": "postcode",
    "Easting": "easting",
    "Northing": "northing",
}

GIAS_LINK_COLUMNS = {
    "URN": "urn",
    "LinkURN": "link_urn",
    "LinkName": "link_name",
    "LinkType": "link_type",
    "LinkEstablishedDate": "link_established_date",
}


def decode(raw: bytes) -> Tuple[str, str]:
    """Return a file's text and the encoding it decoded with (UTF-8, else Windows-1252)."""
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def _header_row(rows: List[List[str]]) -> int:
    """Return the index of the first row with a cell that is exactly ``URN`` (title rows precede it)."""
    for index, row in enumerate(rows[:20]):
        if any(cell.strip() == "URN" for cell in row):
            return index
    raise ValueError("no header row with a URN column in the first 20 rows")


def read_table(path: Path) -> pd.DataFrame:
    """
    Read one source CSV as text, title rows skipped and fully empty rows dropped.

    Returns
    -------
    pandas.DataFrame
        Every column as str (empty for missing) plus ``source_row``, the
        1-based data-row number in the file.
    """
    text, _ = decode(Path(path).read_bytes())
    rows = list(csv.reader(io.StringIO(text)))
    start = _header_row(rows)
    header = rows[start]
    body = [r + [""] * (len(header) - len(r)) for r in rows[start + 1:]]
    frame = pd.DataFrame([r[: len(header)] for r in body], columns=header, dtype=str)
    frame["source_row"] = range(1, len(frame) + 1)
    return frame[frame["URN"].str.strip() != ""].reset_index(drop=True)


def map_columns(frame: pd.DataFrame, era: str) -> pd.DataFrame:
    """
    Return `frame` renamed to `RAW_COLUMNS` through the era's map.

    Columns the era's file does not carry are empty; unmapped source columns
    are dropped.
    """
    column_map = COLUMN_MAPS[era]
    renamed = {}
    for column in frame.columns:
        target = column_map.get(normalise_header(column))
        if target is not None and target not in renamed.values():
            renamed[column] = target
    out = frame.rename(columns=renamed)
    for column in RAW_COLUMNS:
        if column not in out.columns:
            out[column] = None
    out = out[list(RAW_COLUMNS) + ["source_row"]]
    return out.apply(lambda s: s.str.strip() if s.dtype == object else s)


def missing_columns(frame: pd.DataFrame, era: str) -> List[str]:
    """Return the common names `REQUIRED_COLUMNS` asks of `era` that `frame`'s headers do not map to."""
    present = {COLUMN_MAPS[era].get(normalise_header(c)) for c in frame.columns}
    return [c for c in REQUIRED_COLUMNS[era] if c not in present]


def read_inspections(path: Path, era: str) -> pd.DataFrame:
    """
    Read one Ofsted file into `RAW_COLUMNS` (text) plus ``source_row``.

    Raises
    ------
    ValueError
        When a column `REQUIRED_COLUMNS` names is absent.
    """
    frame = read_table(path)
    missing = missing_columns(frame, era)
    if missing:
        raise ValueError(f"{path.name} ({era}) lacks column(s) {missing}")
    return map_columns(frame, era)


def read_gias(path: Path, columns: Dict[str, str]) -> pd.DataFrame:
    """Read a GIAS CSV (Windows-1252), keeping and renaming `columns`, every value text."""
    text, _ = decode(Path(path).read_bytes())
    frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{path.name} lacks column(s) {missing}")
    return frame[list(columns)].rename(columns=columns)


@dataclass
class Manifest:
    """``data/ofsted/manifest.json``: what the fetch wrote, one entry per `FILES` key."""

    entries: Dict[str, dict] = field(default_factory=dict)

    @classmethod
    def read(cls, directory: Path) -> "Manifest":
        """Read the manifest in `directory`; FileNotFoundError when there is none."""
        payload = json.loads((Path(directory) / MANIFEST_NAME).read_text(encoding="utf-8"))
        return cls({entry["key"]: entry for entry in payload["files"]})

    def write(self, directory: Path) -> Path:
        """Write the manifest into `directory` and return its path."""
        path = Path(directory) / MANIFEST_NAME
        files = [self.entries[f.key] for f in FILES if f.key in self.entries]
        path.write_text(json.dumps({"source": SOURCE, "files": files}, indent=2) + "\n", encoding="utf-8")
        return path


def era_of(day: date) -> str:
    """Return the inspection era `day` falls in."""
    era = ERA_STARTS[0][0]
    for name, start in ERA_STARTS:
        if day >= start:
            era = name
    return era


def files_for(keys: Optional[Sequence[str]] = None) -> List[OfstedFile]:
    """Return `FILES`, or the named ones in registry order."""
    if keys is None:
        return list(FILES)
    unknown = set(keys) - set(FILES_BY_KEY)
    if unknown:
        raise KeyError(f"unknown Ofsted file key(s): {sorted(unknown)}")
    return [f for f in FILES if f.key in set(keys)]
