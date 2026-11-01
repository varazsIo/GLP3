"""
Price a London property on a future day with a saved `hpp_cb` or `hpp_rf` model.

Both training scripts learn from rows of ``production.london_houses``, one per
Price Paid sale, and almost every column of that view is computed per sale from
the sales before it: nearby comparables, neighbourhood prices, MSOA momentum,
the previous sale, the energy certificate in force, the market indices on the
day. A sale that has not happened has no such row, so neither script can price
one. This module builds it.

Given an address (``postnumber``, ``poststreet``, ``posttown``, ``postcode``)
and a sale date strictly after today, it

1. resolves the address to its ``production.address_table`` rows (a property
   is often split across several, one with the street and one without);
2. takes the property's static features (coordinates, lot area, station,
   river, PTAL, LSOA/MSOA, deprivation) from its latest recorded sale, or from
   the latest sale in the same postcode when it has never sold;
3. takes the energy certificate in force on the sale date, the previous sale,
   the Bank Rate and the UK HPI published by then, exactly as the view does.
   A home with no usable certificate gets a *surrogate* one (`estimate_epc`):
   the median floor area and rooms and the modal rating, age band, build form
   and tenure of at least `MIN_EPC_NEIGHBOURS` certified homes of the same
   type, from the tightest of its building, street, postcode, postcode sector
   and district. The row is then marked ``epc_missing`` and its price range is
   taken from the calibration measured on homes priced that way;
4. recomputes every sales-history feature -- comparables, neighbourhood,
   MSOA momentum, rolling area comparables and same-street sales -- with the
   view's own windows and radii, as of the *evidence date*: the sale date, or
   the day after the latest sale held when the sale date is later than that
   (there is no evidence after it, and an empty window is not what the model
   was trained on);
5. runs the result through the saved model's own preprocessing
   (`hpp_cb.prepare_features_from_saved_model`, or
   `hpp_rf.predict_with_saved_model`), so nothing is refitted per request.

The model prices the home **as of the evidence date**, where every feature is
inside the range it was trained and calibrated on, and that price is then
moved to the sale date along the home's own **local price index**
(`local_index`): a repeat-sales index per postcode district and property type,
shrunk toward its borough and London where sales are thin, interpolated to the
day, and projected past the published index with `hpi_projection`'s damped
trend where that beat carrying the index forward under blocked
cross-validation. Asking the model for the future date directly does not work:
its comparable-based features divide evidence-date prices by the sale-date
index, and cancelled any projected growth. `PricePrediction.projection_uplift`
says how much the index moved the price and `index_area` whose index it was.

A home with its own previous sale has that sale, moved along the same index,
blended in where `evaluate` measured it improves accuracy -- and a sale in the
last two years with no sign of renovation since carries at least
`RECENT_SALE_WEIGHT_FLOOR` of it (0.9 up to six months), because a home's own
recent arm's-length sale is the best evidence of its value. The range comes
from the model's measured errors in the home's district (or borough), type and
value tier, shrunk toward the wider area and the tier's band where few sales
were measured; an anchored price takes it from what anchored prices were
measured to miss by (``calibration["recent_sale"]``).

The Bank Rate and quoted mortgage rates are carried forward from their latest
published values, and the UK HPI the model deflates by is projected to the
evidence date by the model bundle's ``projection.json``.

Sales history is read once into an `EvidenceStore` (about six years of sales,
a few seconds) and reused, which is what makes a request take milliseconds.

Usage::

    python -m hpldn.backend.scripts.hpp_inference price --model tmp/models/model \\
        --postnumber 18 --poststreet "BARCLAY ROAD" --posttown CROYDON --postcode "CR0 1JN" \\
        --date 2026-09-15
    python -m hpldn.backend.scripts.hpp_inference price --model tmp/models/model.rfm --trust-pickle ...
    python -m hpldn.backend.scripts.hpp_inference fit-projection --model tmp/models/model --write
    python -m hpldn.backend.scripts.hpp_inference fit-local-index --model tmp/models/model --write
    python -m hpldn.backend.scripts.hpp_inference evaluate --model tmp/models/model \\
        --write-calibration --mask-epc

``--model`` is a model bundle (`models.artifact`), verified before it is
loaded; the ``--write`` options add their JSON to it and re-sign it. An
``hpp_rf`` model is a pickle and loads only with ``--trust-pickle``.
"""

import argparse
import json
import logging
import re
import sys
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import text

from hpldn.backend.common import profiling
from hpldn.backend.common.constants import MIN_EPC_NEIGHBOURS, MIN_PLAUSIBLE_PRICE
from hpldn.backend.migrations.address_parse_sql import unit_key
from hpldn.backend.migrations.london_houses_view import VIEW_COLUMNS
from hpldn.backend.models import artifact
from hpldn.backend.scripts import hpi_projection, hpp_cb, hpp_rf, local_index
from hpldn.backend.scripts import ofsted_features as ofsted
from hpldn.backend.scripts.hpp_rf import (
    DATE_COLUMN,
    POPULATION_EPC_MATCHED,
    ROLLING_COMP_EB_PRIOR_K,
    ROLLING_COMP_GROUP_COLUMNS,
    ROLLING_COMP_WINDOWS_MONTHS,
    SAME_STREET_WINDOW_MONTHS,
    TARGET_COLUMN,
)
from hpldn.backend.scripts.pipeline_steps import (
    COMPS_K,
    COMPS_RADIUS_M,
    COMPS_WINDOW_MONTHS,
    EPC_BAND_SHARE_RADIUS_M,
    EPC_BANDS,
    FLOOR_AREA_PERCENTILE_RADIUS_M,
    FLOOR_AREA_PERCENTILE_WINDOW_MONTHS,
    LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS,
    LSOA_TREND_RECENT_WINDOW_MONTHS,
    LSOA_TREND_YEAR_AGO_OFFSET_MONTHS,
    NEAREST20_K,
    NEAREST20_WINDOW_MONTHS,
    NEIGHBOUR_SEARCH_MAX_K,
    NEIGHBOUR_SEARCH_RADIUS_M,
    NEIGHBOUR_SEARCH_WINDOW_MONTHS,
    PROPTYPE_MEDIAN_RADIUS_M,
    PROPTYPE_MEDIAN_WINDOW_MONTHS,
    RECENCY_DECAY_LAMBDA,
    SPATIAL_LAG_RING_INNER_M,
    SPATIAL_LAG_RING_OUTER_M,
    SPATIAL_LAG_WINDOW_MONTHS,
)

LONDON_TZ = ZoneInfo("Europe/London")

# Enough history for the longest window: MSOA momentum compares the latest 12
# months with the 12 months five years before (72 months), plus a month of slack.
EVIDENCE_LOOKBACK_MONTHS = (
    LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS + LSOA_TREND_RECENT_WINDOW_MONTHS + 1
)

# The furthest ahead a price may be asked for. Every market input is carried
# forward from today, so a price years out says nothing a nearer one does not.
MAX_HORIZON_DAYS = 5 * 365

# `(date - date) / 30.4375` in the feature SQL.
DAYS_PER_MONTH = 30.4375

# WGS84, for local distances in metres. The feature tables measure in the
# OSGB36 National Grid (EPSG:27700), whose point scale over London is ~0.9998.
_WGS84_A = 6378137.0
_WGS84_E2 = 0.00669437999014
_NATIONAL_GRID_SCALE = 0.9998

# The static per-location columns taken from a template sale. The ofsted_*
# columns are not among them: ratings change over time, so they are computed
# for the date priced (`EvidenceStore.ofsted_features`), never copied.
STATIC_COLUMNS = [
    "uprn",
    "postcode_district",
    "postcode_sector",
    "latitude",
    "longitude",
    "lot_area_sqm",
    "nearest_station_distance_km",
    "nearest_station_zone",
    "ptal_band_rank",
    "ptal_ai",
    "distance_to_river",
    "lsoa_code",
    "lsoa_name",
    "msoa_code",
    "msoa_name",
] + [c for c in VIEW_COLUMNS if c.startswith("iod_")]

if any(col.startswith("ofsted_") for col in STATIC_COLUMNS):
    raise AssertionError("STATIC_COLUMNS must not copy the time-varying ofsted_* columns from a template sale")

EPC_COLUMNS = [
    "construction_age_band",
    "number_habitable_rooms",
    "current_energy_rating",
    "total_floor_area",
    "build_form",
    "mains_gas_flag",
    "tenure",
    "multi_glaze_proportion",
    "extension_count",
    "floor_level",
]

COMPS_COLUMNS = ["comp_median_price", "comp_median_price_per_sqm", "comp_txn_count"]
NEIGHBOURHOOD_COLUMNS = [
    "spatial_lag_price",
    "median_price_per_sqm_20nn_6m",
    "recency_weighted_price",
    "proptype_local_median_800m_6m",
    "floor_area_percentile_local",
] + [f"epc_band_share_{band.lower()}" for band in EPC_BANDS]
MSOA_TREND_COLUMNS = ["msoa_price_momentum_1y", "msoa_price_momentum_5y"]

# `hpp_rf --model-out` writes joblib to any path; these suffixes mark one.
RANDOM_FOREST_SUFFIXES = frozenset({".joblib", ".rfm"})

UKHPI_PRICE_COLUMN_BY_TYPE = {
    "D": "avg_price_detached",
    "S": "avg_price_semi_detached",
    "T": "avg_price_terraced",
    "F": "avg_price_flats_maisonettes",
}


class PricingError(Exception):
    """A request that cannot be priced, with a sentence safe to show a landlord."""


class PropertyNotFound(PricingError):
    """No address in the register matches the one given."""


class InvalidSaleDate(PricingError):
    """The sale date is not in the future, or too far into it."""


class NotEnoughData(PricingError):
    """The property lacks something every model price depends on."""


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


def normalise_address_part(value) -> str:
    """Return `value` upper-cased with punctuation dropped and whitespace collapsed."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", str(value).upper())
    return " ".join(cleaned.split())


def normalise_postcode(value: str) -> str:
    """Return a UK postcode in canonical form, e.g. ``"cr01jn"`` -> ``"CR0 1JN"``."""
    compact = re.sub(r"\s+", "", str(value or "").upper())
    if len(compact) < 5:
        return compact
    return compact[:-3] + " " + compact[-3:]


@dataclass(frozen=True)
class PropertyAddress:
    """
    An address as the register writes it.

    ``unit`` is the flat within the building ("FLAT 6"): given, it narrows
    the home to that flat (`match_address`); None keeps every row of the
    number and street.
    """

    postnumber: str
    poststreet: Optional[str]
    posttown: str
    postcode: str
    unit: Optional[str] = None


def _address_label(address: PropertyAddress) -> str:
    """Return the address on one line ("FLAT 6, 17, WOODVILLE ROAD, LONDON, NW11 9TP")."""
    parts = [address.unit, address.postnumber, address.poststreet, address.posttown, address.postcode]
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


# `unit` since 0030.
_CANDIDATES_SQL = text(
    """
    SELECT a.id AS address_id, a.postnumber, a.poststreet, a.posttown, a.postcode, a.unit
    FROM production.address_table a
    WHERE a.postcode = :postcode
    """
)


def match_address(candidates: pd.DataFrame, address: PropertyAddress) -> pd.DataFrame:
    """
    Return the register rows that are the property `address` names.

    The postcode and house number or name must match (after
    `normalise_address_part`). A property is often held as several rows, one
    with its street and one without, so every row whose street matches or is
    blank is the same property; when no row carries the street given, only
    the blank-street rows are. The town does not narrow the match: the
    register spells it inconsistently for one postcode (CROYDON, LONDON).
    A unit, when given, narrows the rows to that flat (by
    `address_parse_sql.unit_key`: "Flat 6" is "Apartment 6" is "6"); with
    none, every flat of the number and street is the property.

    Raises
    ------
    PropertyNotFound
        When no street was given and the number exists on more than one
        street in the postcode.
    """
    if candidates.empty:
        return candidates
    number = normalise_address_part(address.postnumber)
    street = normalise_address_part(address.poststreet)
    numbers = candidates["postnumber"].map(normalise_address_part)
    streets = candidates["poststreet"].map(normalise_address_part)

    keep = numbers == number
    wanted_unit = unit_key(address.unit)
    if wanted_unit is not None:
        units = candidates["unit"] if "unit" in candidates else pd.Series(None, index=candidates.index)
        keep = keep & (units.map(unit_key) == wanted_unit)
    if street:
        if (keep & (streets == street)).any():
            keep = keep & ((streets == street) | (streets == ""))
        else:
            keep = keep & (streets == "")
    matched = candidates[keep.to_numpy()]
    if not street and len(set(streets[matched.index]) - {""}) > 1:
        raise PropertyNotFound(
            f"{address.postnumber} is on more than one street in {normalise_postcode(address.postcode)}. "
            "Add the street to the address."
        )
    return matched


# --------------------------------------------------------------------------
# Evidence: sales history and the market reference tables
# --------------------------------------------------------------------------


_EVIDENCE_SQL = text(
    f"""
    SELECT ppd_id, dateoftransfer, price, propertytype, total_floor_area,
           current_energy_rating, latitude, longitude, msoa_code,
           postcode_district, postcode_sector, poststreet, epc_matched
    FROM production.london_houses
    WHERE dateoftransfer >= :since
      AND price >= {MIN_PLAUSIBLE_PRICE}
    ORDER BY dateoftransfer, ppd_id
    """
)

_LATEST_SALE_SQL = text("SELECT max(dateoftransfer) FROM production.london_houses")

_OFSTED_TABLE_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'production' AND tablename = 'ofsted_rating_spells')"
)

# The school rating spells the per-sale Ofsted features read (migration 0033),
# by lat/lon: the serving host has no PostGIS (`export_serving_db`).
_OFSTED_SPELLS_SQL = text(
    """
    SELECT s.id, s.phase, s.lat, s.lon, s.valid_from, s.valid_to,
           s.rating_score::double precision AS rating_score, s.grade_basis, s.inspection_date
    FROM production.ofsted_rating_spells s
    WHERE s.phase = ANY(:phases) AND s.lat IS NOT NULL AND s.lon IS NOT NULL
    ORDER BY s.id
    """
)

# A spell further than this in latitude or longitude is out of
# `ofsted_features.SEARCH_RADIUS_M` anywhere in London; a cheap pre-filter.
_OFSTED_LAT_WINDOW = 0.03
_OFSTED_LON_WINDOW = 0.05


def _days(value) -> int:
    """Return a date as days since the Unix epoch."""
    return int(pd.Timestamp(value).normalize().value // 86_400_000_000_000)


def _months_before(value, months: int) -> pd.Timestamp:
    """Return `value` minus calendar months, clamped at month ends like PostgreSQL's interval."""
    return pd.Timestamp(value) - pd.DateOffset(months=months)


def _local_metres(lat0: float, lon0: float, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """
    Return the grid distance in metres from one point to many.

    A local tangent plane on the WGS84 ellipsoid scaled to the National Grid:
    within the 2 km the feature searches use it agrees with ``ST_Distance`` in
    EPSG:27700 to about 0.1 %.
    """
    phi = np.radians(lat0)
    denom = 1.0 - _WGS84_E2 * np.sin(phi) ** 2
    meridional = _WGS84_A * (1.0 - _WGS84_E2) / denom**1.5
    normal = _WGS84_A / np.sqrt(denom)
    dy = np.radians(lat - lat0) * meridional
    dx = np.radians(lon - lon0) * normal * np.cos(phi)
    return np.hypot(dx, dy) * _NATIONAL_GRID_SCALE


def _median(values: np.ndarray) -> float:
    """Return the median of the finite `values`, or NaN (``percentile_cont(0.5)``)."""
    values = values[np.isfinite(values)]
    return float(np.median(values)) if values.size else np.nan


def _price_per_sqm(price: np.ndarray, floor_area: np.ndarray) -> np.ndarray:
    """Return ``price / NULLIF(floor_area, 0)``, NaN where the area is missing or zero."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(floor_area > 0, price / floor_area, np.nan)


class EvidenceStore:
    """
    The sales history and market tables every price is computed from.

    Loaded once from ``production.london_houses`` (the last
    `EVIDENCE_LOOKBACK_MONTHS` months of sales) and the reference tables, then
    shared by every request; reload it after the view is refreshed.

    Attributes
    ----------
    latest_sale : pandas.Timestamp
        The most recent sale held: the evidence ends here.
    mortgage_rates, ukhpi, boe_rate : pandas.DataFrame
        The market reference tables.
    """

    def __init__(
        self,
        sales: pd.DataFrame,
        mortgage_rates: pd.DataFrame,
        ukhpi: pd.DataFrame,
        boe_rate: pd.DataFrame,
        latest_sale,
        projection: Optional["hpi_projection.IndexProjection"] = None,
    ) -> None:
        sales = sales.sort_values([DATE_COLUMN, "ppd_id"], kind="stable").reset_index(drop=True)
        self.latest_sale = pd.Timestamp(latest_sale)
        self.mortgage_rates = mortgage_rates
        self.ukhpi = ukhpi.sort_values(["published_date", "period"]).reset_index(drop=True)
        # Carrying the index forward unchanged until `set_projection` says otherwise.
        self.projection = projection or hpi_projection.IndexProjection(self.ukhpi, None)
        # The district-by-type index a price is moved along to its sale date
        # (`local_index`); None until a `PricingService` or `evaluate` attaches
        # one, and then the London index moves it instead.
        self.local_index: Optional["local_index.LocalIndex"] = None
        self.boe_rate = boe_rate.sort_values("date_changed").reset_index(drop=True)
        self.n_sales = len(sales)
        # School rating spells (`set_ofsted_spells`); None leaves the features missing.
        self._ofsted: Optional[Dict[str, np.ndarray]] = None

        self._days = np.array([_days(d) for d in pd.to_datetime(sales[DATE_COLUMN])], dtype=np.int64)
        self._price = sales[TARGET_COLUMN].to_numpy(dtype=np.float64)
        self._ptype = sales["propertytype"].astype("str").to_numpy()
        self._area = pd.to_numeric(sales["total_floor_area"], errors="coerce").to_numpy(dtype=np.float64)
        self._rating = sales["current_energy_rating"].astype("object").where(
            sales["current_energy_rating"].notna(), None
        ).to_numpy()
        self._lat = pd.to_numeric(sales["latitude"], errors="coerce").to_numpy(dtype=np.float64)
        self._lon = pd.to_numeric(sales["longitude"], errors="coerce").to_numpy(dtype=np.float64)
        self._has_location = np.isfinite(self._lat) & np.isfinite(self._lon)
        # Size-matched comparables measure distance as training does (`hpp_rf.planar_metres`).
        self._x, self._y = hpp_rf.planar_metres(self._lat, self._lon)
        with np.errstate(divide="ignore", invalid="ignore"):
            self._log_pps = np.log(self._price / self._area)

        # MSOA momentum reads every sale in the MSOA.
        self._msoa_index = {
            key: np.asarray(idx, dtype=np.int64)
            for key, idx in sales.groupby("msoa_code", sort=False).indices.items()
        }

        # Rolling area comparables and same-street sales are statistics over the
        # training frame, which is the modelled population only.
        matched = sales["epc_matched"].fillna(False).astype(bool).to_numpy()
        self._population_index = {
            POPULATION_EPC_MATCHED: np.flatnonzero(matched),
            hpp_rf.POPULATION_ALL: np.arange(len(sales)),
        }
        self._in_population = {POPULATION_EPC_MATCHED: matched, hpp_rf.POPULATION_ALL: np.ones(len(sales), dtype=bool)}
        self._groups: Dict[Tuple[str, str], Dict[str, np.ndarray]] = {}
        for population, rows in self._population_index.items():
            subset = sales.iloc[rows]
            # Keyed exactly as hpp_rf.add_same_street_features keys its groups.
            street = subset["postcode_district"].astype("str").str.cat(
                hpp_rf.street_key_series(subset), sep="_"
            )
            keys = {
                "propertytype": subset["propertytype"].astype("str"),
                "street": street,
            }
            for group_col in ROLLING_COMP_GROUP_COLUMNS:
                keys[group_col] = subset[group_col].astype("str") + "\x1f" + subset["propertytype"].astype("str")
            for name, values in keys.items():
                self._groups[(population, name)] = {
                    key: rows[np.asarray(idx, dtype=np.int64)]
                    for key, idx in values.groupby(values, sort=False).indices.items()
                    if isinstance(key, str)
                }

    def set_ofsted_spells(self, spells: Optional[pd.DataFrame]) -> None:
        """
        Hold the ``ofsted_rating_spells`` table (migration 0033) for `ofsted_features`.

        `spells` has the columns `_OFSTED_SPELLS_SQL` reads; None (a database
        before migration 0033) leaves every Ofsted feature missing.
        """
        if spells is None:
            self._ofsted = None
            return
        spells = spells.sort_values("id", kind="stable")
        valid_to = pd.to_datetime(spells["valid_to"])
        self._ofsted = {
            "id": spells["id"].to_numpy(dtype=np.int64),
            "phase": spells["phase"].astype("object").to_numpy(),
            "lat": pd.to_numeric(spells["lat"]).to_numpy(dtype=np.float64),
            "lon": pd.to_numeric(spells["lon"]).to_numpy(dtype=np.float64),
            "from": np.array([_days(d) for d in pd.to_datetime(spells["valid_from"])], dtype=np.float64),
            "to": np.array([np.inf if pd.isna(d) else _days(d) for d in valid_to], dtype=np.float64),
            "score": pd.to_numeric(spells["rating_score"]).to_numpy(dtype=np.float64),
            "basis": spells["grade_basis"].astype("object").to_numpy(),
            "inspected": np.array([_days(d) for d in pd.to_datetime(spells["inspection_date"])], dtype=np.float64),
        }

    def ofsted_features(self, as_of, lat: float, lon: float) -> Dict[str, object]:
        """
        Return ``production.ofsted_features`` for a home at (`lat`, `lon`) on `as_of`.

        The spells in force on the date within `ofsted_features.SEARCH_RADIUS_M`,
        measured as the other features are (`_local_metres`), ties broken by
        spell id as 0034's SQL does, through `ofsted_features.compute`. Every
        column is missing without a location or without the spells, as the
        view's LEFT JOIN leaves them.
        """
        out: Dict[str, object] = {col: np.nan for col in ofsted.NUMERIC_COLUMNS}
        out["ofsted_nearest_primary_basis"] = None
        spells = self._ofsted
        if spells is None or not (np.isfinite(lat) and np.isfinite(lon)):
            return out
        day = float(_days(as_of))
        idx = np.flatnonzero(
            (spells["from"] <= day)
            & (day < spells["to"])
            & (np.abs(spells["lat"] - lat) <= _OFSTED_LAT_WINDOW)
            & (np.abs(spells["lon"] - lon) <= _OFSTED_LON_WINDOW)
        )
        dist = _local_metres(lat, lon, spells["lat"][idx], spells["lon"][idx])
        return ofsted.compute(
            dist, spells["phase"][idx], spells["score"][idx], day - spells["inspected"][idx], spells["basis"][idx]
        )

    def set_projection(self, payload: Optional[Mapping]) -> None:
        """
        Fit the index projection this store prices with, from a model's sidecar.

        Called once, when a `PricingService` pairs the evidence with its model.
        A payload of None leaves the index carried forward, which is what every
        price did before `hpi_projection` existed.
        """
        self.projection = hpi_projection.IndexProjection(self.ukhpi, payload)

    @classmethod
    def load(cls, engine, log: Optional[logging.Logger] = None, earliest_anchor=None) -> "EvidenceStore":
        """
        Read the evidence from the database `engine` points at.

        Enough history is read for an evidence date as early as
        `earliest_anchor` (default: the latest sale, which covers every future
        sale date). The read is one statement of a few hundred thousand rows,
        so it lifts ``statement_timeout`` for its own transaction: the read
        API's role carries a 5 s default.
        """
        with engine.connect() as connection:
            with connection.begin():
                connection.execute(text("SET LOCAL statement_timeout = '120s'"))
                latest_sale = connection.execute(_LATEST_SALE_SQL).scalar_one()
                start = latest_sale if earliest_anchor is None else min(pd.Timestamp(earliest_anchor), pd.Timestamp(latest_sale))
                since = _months_before(start, EVIDENCE_LOOKBACK_MONTHS).date()
                sales = pd.read_sql(_EVIDENCE_SQL, connection, params={"since": since})
                mortgage_rates = hpp_cb.load_mortgage_rates(connection)
                ukhpi = pd.read_sql(
                    text(
                        "SELECT period, published_date, avg_price_all_property_types, "
                        + ", ".join(UKHPI_PRICE_COLUMN_BY_TYPE.values())
                        + " FROM production.ukhpi WHERE published_date IS NOT NULL"
                    ),
                    connection,
                )
                boe_rate = pd.read_sql(
                    text("SELECT date_changed, rate FROM production.boe_rate"), connection
                )
                ofsted_spells = (
                    pd.read_sql(_OFSTED_SPELLS_SQL, connection, params={"phases": list(ofsted.FEATURE_PHASES)})
                    if connection.execute(_OFSTED_TABLE_SQL).scalar_one()
                    else None
                )
        for frame, cols in ((ukhpi, ["period", "published_date"]), (boe_rate, ["date_changed"])):
            for col in cols:
                frame[col] = pd.to_datetime(frame[col])
        store = cls(sales, mortgage_rates, ukhpi, boe_rate, latest_sale)
        store.set_ofsted_spells(ofsted_spells)
        if log is not None:
            log.info(
                f"EvidenceStore: {store.n_sales:,} sales from {since} to {store.latest_sale.date()}; "
                + ("no Ofsted spells (before migration 0033)" if ofsted_spells is None
                   else f"{len(ofsted_spells):,} Ofsted rating spells")
            )
        return store

    # -- evidence date ------------------------------------------------------

    def evidence_date(self, sale_date) -> pd.Timestamp:
        """Return the date the sales-history features are computed as of."""
        return min(pd.Timestamp(sale_date), self.latest_sale + pd.Timedelta(days=1))

    def _window(self, start, end) -> slice:
        """Return the positions of sales dated in ``[start, end)``."""
        lo = int(np.searchsorted(self._days, _days(start), side="left"))
        hi = int(np.searchsorted(self._days, _days(end), side="left"))
        return slice(lo, hi)

    # -- market reference ---------------------------------------------------

    def boe_rate_on(self, day) -> float:
        """Return the Bank Rate in force on `day`, or NaN before the first change."""
        rows = self.boe_rate[self.boe_rate["date_changed"] <= pd.Timestamp(day)]
        return float(rows["rate"].iloc[-1]) if len(rows) else np.nan

    def ukhpi_on(self, day, propertytype: str) -> float:
        """
        Return the UK HPI average price for the type, as it stands on `day`.

        The latest figure published on or before `day`, multiplied by
        `hpi_projection.IndexProjection.factor` when `day` lies beyond the
        published index. The model deflates its target by this column, so this
        is the one channel through which a future sale date moves the price.
        """
        rows = self.ukhpi[self.ukhpi["published_date"] <= pd.Timestamp(day)]
        if not len(rows):
            return np.nan
        latest = rows.iloc[-1]
        column = UKHPI_PRICE_COLUMN_BY_TYPE.get(propertytype)
        value = latest[column] if column else np.nan
        if pd.isna(value):
            value = latest["avg_price_all_property_types"]
        if pd.isna(value):
            return np.nan
        return float(value) * self.projection.factor(day, propertytype)

    # -- moving a price through time -----------------------------------------

    def time_factor(self, from_day, to_day, district: Optional[str], propertytype: str) -> float:
        """
        Return what a price on `from_day` is worth on `to_day` for this area and type.

        Read from the local index when one is attached: the district's own
        measured repeat-sale level, projected beyond the published index by the
        first series in its chain that beat carrying forward. Without one, the
        London UK HPI for the type, with its projection.
        """
        if self.local_index is not None:
            return self.local_index.factor(from_day, to_day, district, propertytype)
        was = self.ukhpi_on(from_day, propertytype)
        now = self.ukhpi_on(to_day, propertytype)
        if not np.isfinite(was) or not np.isfinite(now) or was <= 0:
            return 1.0
        return float(now / was)

    def index_sigma(self, day, district: Optional[str], propertytype: str) -> float:
        """Return the index projection's log standard deviation for a price on `day`."""
        if self.local_index is not None:
            return self.local_index.sigma(district, propertytype, self.local_index.horizon_years(day))
        return self.projection.sigma(propertytype, self.projection.horizon_years(day))

    def index_area(self, district: Optional[str], propertytype: str) -> str:
        """Return the area whose price index moves this home through time ("SW11", "Wandsworth", "London")."""
        if self.local_index is not None:
            return self.local_index.area_of(district, propertytype)
        return local_index.LONDON

    # -- sales-history features ---------------------------------------------

    def comps_features(self, anchor, lat: float, lon: float, propertytype: str) -> Dict[str, float]:
        """
        Return ``production.comps_features`` for a sale on `anchor`.

        The `COMPS_K` nearest earlier sales of the same type within
        `COMPS_RADIUS_M` over `COMPS_WINDOW_MONTHS`.
        """
        empty = {"comp_median_price": np.nan, "comp_median_price_per_sqm": np.nan, "comp_txn_count": 0.0}
        if not (np.isfinite(lat) and np.isfinite(lon)):
            return empty
        window = self._window(_months_before(anchor, COMPS_WINDOW_MONTHS), anchor)
        idx = np.arange(window.start, window.stop)
        idx = idx[(self._ptype[idx] == propertytype) & self._has_location[idx]]
        if not idx.size:
            return empty
        dist = _local_metres(lat, lon, self._lat[idx], self._lon[idx])
        near = dist <= COMPS_RADIUS_M
        idx, dist = idx[near], dist[near]
        if not idx.size:
            return empty
        if idx.size > COMPS_K:
            keep = np.argsort(dist, kind="stable")[:COMPS_K]
            idx = idx[keep]
        price = self._price[idx]
        return {
            "comp_median_price": _median(price),
            "comp_median_price_per_sqm": _median(_price_per_sqm(price, self._area[idx])),
            "comp_txn_count": float(idx.size),
        }

    def neighbourhood_features(
        self, anchor, lat: float, lon: float, propertytype: str, floor_area: float
    ) -> Dict[str, float]:
        """
        Return ``production.neighbourhood_features`` for a sale on `anchor`.

        One fetch of up to `NEIGHBOUR_SEARCH_MAX_K` nearest earlier sales
        within `NEIGHBOUR_SEARCH_RADIUS_M` over
        `NEIGHBOUR_SEARCH_WINDOW_MONTHS`, which every column reads, as in
        `pipeline_steps._neighbourhood_select_sql`.
        """
        out = {col: np.nan for col in NEIGHBOURHOOD_COLUMNS}
        if not (np.isfinite(lat) and np.isfinite(lon)):
            return out
        window = self._window(_months_before(anchor, NEIGHBOUR_SEARCH_WINDOW_MONTHS), anchor)
        idx = np.arange(window.start, window.stop)
        idx = idx[self._has_location[idx]]
        if not idx.size:
            return out
        dist = _local_metres(lat, lon, self._lat[idx], self._lon[idx])
        near = dist <= NEIGHBOUR_SEARCH_RADIUS_M
        idx, dist = idx[near], dist[near]
        if not idx.size:
            return out
        order = np.argsort(dist, kind="stable")[:NEIGHBOUR_SEARCH_MAX_K]
        idx, dist = idx[order], dist[order]
        price = self._price[idx]
        area = self._area[idx]
        months_ago = (_days(anchor) - self._days[idx]) / DAYS_PER_MONTH

        ring = (dist >= SPATIAL_LAG_RING_INNER_M) & (dist <= SPATIAL_LAG_RING_OUTER_M)
        lag = ring & (months_ago <= SPATIAL_LAG_WINDOW_MONTHS)
        if lag.any():
            out["spatial_lag_price"] = float(np.sum(price[lag] / dist[lag]) / np.sum(1.0 / dist[lag]))

        decay = np.exp(-RECENCY_DECAY_LAMBDA * months_ago)
        out["recency_weighted_price"] = float(np.sum(price * decay) / np.sum(decay))

        local_type = (
            (dist <= PROPTYPE_MEDIAN_RADIUS_M)
            & (months_ago <= PROPTYPE_MEDIAN_WINDOW_MONTHS)
            & (self._ptype[idx] == propertytype)
        )
        out["proptype_local_median_800m_6m"] = _median(price[local_type])

        if np.isfinite(floor_area):
            sized = (
                (dist <= FLOOR_AREA_PERCENTILE_RADIUS_M)
                & (months_ago <= FLOOR_AREA_PERCENTILE_WINDOW_MONTHS)
                & np.isfinite(area)
            )
            if sized.any():
                out["floor_area_percentile_local"] = float(np.mean(area[sized] <= floor_area))

        rated = (dist <= EPC_BAND_SHARE_RADIUS_M) & np.array([r is not None for r in self._rating[idx]])
        if rated.any():
            ratings = self._rating[idx][rated]
            for band in EPC_BANDS:
                out[f"epc_band_share_{band.lower()}"] = float(np.mean(ratings == band))

        nn20 = np.flatnonzero(ring & (months_ago <= NEAREST20_WINDOW_MONTHS))[:NEAREST20_K]
        if nn20.size:
            out["median_price_per_sqm_20nn_6m"] = _median(_price_per_sqm(price[nn20], area[nn20]))
        return out

    def msoa_trend_features(self, anchor, msoa_code) -> Dict[str, float]:
        """Return ``production.lsoa_price_trend_features``: the MSOA's 1- and 5-year price momentum."""
        out = {col: np.nan for col in MSOA_TREND_COLUMNS}
        rows = self._msoa_index.get(msoa_code) if isinstance(msoa_code, str) else None
        if rows is None or not rows.size:
            return out
        days = self._days[rows]
        anchor_days = _days(anchor)
        recent_start = _days(_months_before(anchor, LSOA_TREND_RECENT_WINDOW_MONTHS))
        year_ago_start = _days(
            _months_before(anchor, LSOA_TREND_RECENT_WINDOW_MONTHS + LSOA_TREND_YEAR_AGO_OFFSET_MONTHS)
        )
        five_recent = _days(_months_before(anchor, LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS))
        five_start = _days(
            _months_before(anchor, LSOA_TREND_FIVE_YEAR_AGO_OFFSET_MONTHS + LSOA_TREND_RECENT_WINDOW_MONTHS)
        )
        in_scope = (days < anchor_days) & (days >= five_start)
        price = self._price[rows]

        def _avg(mask: np.ndarray) -> float:
            mask = mask & in_scope
            return float(price[mask].mean()) if mask.any() else np.nan

        recent = _avg(days >= recent_start)
        year_ago = _avg((days < recent_start) & (days >= year_ago_start))
        five_year_ago = _avg((days < five_recent) & (days >= five_start))
        if np.isfinite(recent) and np.isfinite(year_ago) and year_ago != 0:
            out["msoa_price_momentum_1y"] = recent / year_ago - 1
        if np.isfinite(recent) and np.isfinite(five_year_ago) and five_year_ago != 0:
            out["msoa_price_momentum_5y"] = recent / five_year_ago - 1
        return out

    def _rolling_stats(
        self, population: str, group: str, key, anchor, window_months: int
    ) -> Tuple[float, float, float]:
        """
        Return the median price, median price per m² and sale count in a group's window.

        The window is ``[anchor - round(months * 30.4375) days, anchor)``, the
        left-closed rolling window of `hpp_rf._rolling_causal_group_stats`.
        """
        rows = self._groups[(population, group)].get(key) if isinstance(key, str) else None
        if rows is None or not rows.size:
            return np.nan, np.nan, 0.0
        days = self._days[rows]
        end = _days(anchor)
        start = end - int(round(window_months * DAYS_PER_MONTH))
        lo = int(np.searchsorted(days, start, side="left"))
        hi = int(np.searchsorted(days, end, side="left"))
        if hi <= lo:
            return np.nan, np.nan, 0.0
        chosen = rows[lo:hi]
        price = self._price[chosen]
        return _median(price), _median(_price_per_sqm(price, self._area[chosen])), float(chosen.size)

    def rolling_comp_features(
        self,
        anchor,
        propertytype: str,
        groups: Mapping[str, str],
        population: str = POPULATION_EPC_MATCHED,
    ) -> Dict[str, float]:
        """
        Return `hpp_rf.add_rolling_area_comp_features`' columns for a sale on `anchor`.

        Local medians per postcode sector and district and property type,
        shrunk towards the London-wide median for the type with
        `ROLLING_COMP_EB_PRIOR_K`, over the modelled `population`.
        """
        out: Dict[str, float] = {}
        for window in ROLLING_COMP_WINDOWS_MONTHS:
            city_price, city_pps, _ = self._rolling_stats(population, "propertytype", propertytype, anchor, window)
            for group_col in ROLLING_COMP_GROUP_COLUMNS:
                key = f"{groups[group_col]}\x1f{propertytype}"
                local_price, local_pps, n = self._rolling_stats(population, group_col, key, anchor, window)
                k = ROLLING_COMP_EB_PRIOR_K
                local_price = 0.0 if np.isnan(local_price) else local_price
                local_pps = 0.0 if np.isnan(local_pps) else local_pps
                out[f"{group_col}_{window}m_comp_median_price"] = (n * local_price + k * city_price) / (n + k)
                out[f"{group_col}_{window}m_comp_median_price_per_sqm"] = (n * local_pps + k * city_pps) / (n + k)
                out[f"{group_col}_{window}m_comp_txn_count"] = n
        return out

    def sizeband_comp_features(
        self,
        anchor,
        lat: float,
        lon: float,
        propertytype: str,
        floor_area: float,
        district: Optional[str],
        population: str = POPULATION_EPC_MATCHED,
    ) -> Dict[str, float]:
        """
        Return `hpp_rf.add_sizeband_comp_features`' columns for a sale on `anchor`.

        The modelled `population`'s sales of the type in the window before
        `anchor`, sized within the band around `floor_area` and within the
        outer radius, go to `hpp_rf.sizeband_comp_value`, moved to `anchor`
        along the home's local index when one is attached.
        """
        empty = {"comp_sizeband_pps": np.nan, "comp_sizeband_implied_value": np.nan}
        if not (np.isfinite(lat) and np.isfinite(lon) and np.isfinite(floor_area) and floor_area > 0):
            return empty
        day = _days(anchor)
        lo = int(np.searchsorted(self._days, day - hpp_rf.SIZEBAND_COMP_WINDOW_DAYS, side="left"))
        hi = int(np.searchsorted(self._days, day, side="left"))
        idx = np.arange(lo, hi)
        idx = idx[
            (self._ptype[idx] == propertytype)
            & self._has_location[idx]
            & self._in_population[population][idx]
            & np.isfinite(self._log_pps[idx])
        ]
        idx = idx[hpp_rf.sizeband_candidates(self._area[idx], self._days[idx], floor_area, day)]
        if not idx.size:
            return empty
        (x,), (y,) = (np.atleast_1d(v) for v in hpp_rf.planar_metres([lat], [lon]))
        dist = np.hypot(self._x[idx] - x, self._y[idx] - y)
        inside = dist <= hpp_rf.SIZEBAND_COMP_RADII_M[1]
        idx, dist = idx[inside], dist[inside]
        if not idx.size:
            return empty
        levels = None
        if self.local_index is not None:
            index = self.local_index
            levels = lambda days: index.log_levels(days, district, propertytype)  # noqa: E731
        pps = hpp_rf.sizeband_comp_value(self._days[idx], self._log_pps[idx], dist, day, levels)
        return {"comp_sizeband_pps": pps, "comp_sizeband_implied_value": pps * floor_area}

    def same_street_features(
        self, anchor, postcode_district: str, street_key, population: str = POPULATION_EPC_MATCHED
    ) -> Dict[str, float]:
        """
        Return `hpp_rf.add_same_street_features`' columns for a sale on `anchor`.

        `street_key` is the street's `hpp_rf.normalise_street_key` form, the
        key the view stores as ``street_key``.
        """
        if not isinstance(street_key, str):
            return {"street_sales_count_12m": 0.0, "street_sales_median_price_12m": np.nan}
        median, _, count = self._rolling_stats(
            population, "street", f"{postcode_district}_{street_key}", anchor, SAME_STREET_WINDOW_MONTHS
        )
        return {"street_sales_count_12m": count, "street_sales_median_price_12m": median}


# --------------------------------------------------------------------------
# Building the view row for a sale that has not happened
# --------------------------------------------------------------------------


_OWN_SALES_SQL = text(
    """
    SELECT p.address_id AS source_address_id, lh.*
    FROM production.ppd_table p
    JOIN production.london_houses lh ON lh.ppd_id = p.id
    WHERE p.address_id = ANY(:address_ids)
    ORDER BY lh.dateoftransfer DESC, lh.ppd_id DESC
    """
)

_POSTCODE_SALE_SQL = text(
    """
    SELECT lh.*
    FROM production.address_table a
    JOIN production.ppd_table p ON p.address_id = a.id
    JOIN production.london_houses lh ON lh.ppd_id = p.id
    WHERE a.postcode = :postcode
    ORDER BY lh.dateoftransfer DESC, lh.ppd_id DESC
    LIMIT 1
    """
)

# A postcode with no sale of its own takes its location template from the
# sector's sale nearest the home (or the latest, without coordinates).
# `postcode_district` is indexed on the view; `postcode_sector` is not.
_SECTOR_NEAREST_SALE_SQL = text(
    """
    SELECT lh.*
    FROM production.london_houses lh
    WHERE lh.postcode_district = :district AND lh.postcode_sector = :sector
      AND lh.latitude IS NOT NULL AND lh.longitude IS NOT NULL
    ORDER BY (lh.latitude - :lat) ^ 2 + ((lh.longitude - :lon) * cos(radians(:lat))) ^ 2,
             lh.dateoftransfer DESC, lh.ppd_id DESC
    LIMIT 1
    """
)

_SECTOR_LATEST_SALE_SQL = text(
    """
    SELECT lh.*
    FROM production.london_houses lh
    WHERE lh.postcode_district = :district AND lh.postcode_sector = :sector
    ORDER BY lh.dateoftransfer DESC, lh.ppd_id DESC
    LIMIT 1
    """
)

# The modal Price Paid type among the postcode's modelled sales, for a home
# with neither a sale nor a certificate to read its type from.
_POSTCODE_MODAL_TYPE_SQL = text(
    """
    SELECT lh.propertytype
    FROM production.address_table a
    JOIN production.ppd_table p ON p.address_id = a.id
    JOIN production.london_houses lh ON lh.ppd_id = p.id
    WHERE a.postcode = :postcode
    GROUP BY lh.propertytype
    ORDER BY count(*) DESC, lh.propertytype
    LIMIT 1
    """
)

_EPC_SQL = text(
    """
    SELECT ec.address_id, ec.construction_age_band, ec.number_habitable_rooms,
           ec.current_energy_rating, ec.total_floor_area, bf.build_form,
           ec.mains_gas_flag, ec.tenure, ec.multi_glaze_proportion,
           ec.extension_count, ec.floor_level, ec.property_type,
           ec.inspection_date, ec.lodgement_datetime, ec.id
    FROM production.epc_table ec
    LEFT JOIN production.build_form_category_table bf ON bf.id = ec.build_form
    WHERE ec.address_id = ANY(:address_ids) AND ec.inspection_date IS NOT NULL
    """
)

_COORDINATES_SQL = text(
    """
    SELECT address_id, latitude, longitude
    FROM production.address_coordinates
    WHERE address_id = ANY(:address_ids)
    """
)

# EPC property_type / build_form -> Price Paid property type, for a home with
# no sale of its own to read it from.
_BUILD_FORM_TO_PPD_TYPE = {
    "Detached": "D",
    "Semi-Detached": "S",
    "End-Terrace": "T",
    "Mid-Terrace": "T",
    "Enclosed End-Terrace": "T",
    "Enclosed Mid-Terrace": "T",
}

# Address words that only a flat carries.
_FLAT_ADDRESS_TOKENS = frozenset(
    {"FLAT", "FLATS", "APARTMENT", "APARTMENTS", "APT", "UNIT", "MAISONETTE", "STUDIO"}
)
_HOUSE_NUMBER_RE = re.compile(r"\d+[A-Z]?|[A-Z]")


@dataclass
class PropertyRecord:
    """
    What the register holds about one property, from its address rows.

    ``postcode_template`` is the sale the location columns are copied from
    when the home has none of its own, and ``template_basis`` says where it
    came from (``postcode_sale`` or ``sector_sale``). ``postcode_modal_type``
    is the most common sold type in the postcode.
    """

    address: PropertyAddress
    address_ids: List[int]
    sales: pd.DataFrame
    epcs: pd.DataFrame
    coordinates: Optional[Tuple[float, float]]
    postcode_template: Optional[pd.Series] = None
    template_basis: str = "postcode_sale"
    postcode_modal_type: Optional[str] = None


def load_property(connection, address: PropertyAddress) -> PropertyRecord:
    """
    Resolve `address` and read its sales, certificates and coordinates.

    Raises
    ------
    PropertyNotFound
        When no register row matches.
    """
    postcode = normalise_postcode(address.postcode)
    candidates = pd.read_sql(_CANDIDATES_SQL, connection, params={"postcode": postcode})
    matched = match_address(candidates, address)
    if matched.empty:
        raise PropertyNotFound(
            f"We could not find {_address_label(address)} in the property register. "
            "Check the house number or name and the street against the postcode."
        )
    return load_property_by_ids(connection, [int(i) for i in matched["address_id"]], matched)


_ADDRESS_ROWS_SQL = text(
    "SELECT id AS address_id, postnumber, poststreet, posttown, postcode, unit "
    "FROM production.address_table WHERE id = ANY(:address_ids)"
)

# Rows per `load_properties_by_ids` round trip.
PROPERTY_BATCH_IDS = 1000


def load_property_by_ids(connection, address_ids: Sequence[int], rows: Optional[pd.DataFrame] = None) -> PropertyRecord:
    """Read the sales, certificates and coordinates of the register rows `address_ids`."""
    params = {"address_ids": list(address_ids)}
    sales = pd.read_sql(_OWN_SALES_SQL, connection, params=params)
    epcs = pd.read_sql(_EPC_SQL, connection, params=params)
    coordinates = pd.read_sql(_COORDINATES_SQL, connection, params=params).dropna()
    if rows is None:
        rows = pd.read_sql(_ADDRESS_ROWS_SQL, connection, params=params)
    return _record_from_frames(connection, address_ids, sales, epcs, coordinates, rows)


def load_properties_by_ids(connection, address_ids: Sequence[int], batch: int = PROPERTY_BATCH_IDS) -> Dict[int, PropertyRecord]:
    """
    Return `load_property_by_ids` of each single register row in `address_ids`, keyed by id.

    Four queries per `batch` ids instead of four per id (`evaluate` builds
    thousands of sales). Each id's frames are its slice of the batch's, in
    the query's order (sales newest first), so every record is the one the
    single-id call returns; a home without a sale still reads its postcode
    template one at a time. Ids with no register row are left out.
    """
    records: Dict[int, PropertyRecord] = {}
    unique = list(dict.fromkeys(int(i) for i in address_ids))
    for start in range(0, len(unique), batch):
        ids = unique[start : start + batch]
        params = {"address_ids": ids}
        sales = pd.read_sql(_OWN_SALES_SQL, connection, params=params)
        epcs = pd.read_sql(_EPC_SQL, connection, params=params)
        coordinates = pd.read_sql(_COORDINATES_SQL, connection, params=params).dropna()
        rows = pd.read_sql(_ADDRESS_ROWS_SQL, connection, params=params)
        by_id = {
            name: {key: group.reset_index(drop=True) for key, group in frame.groupby(column, sort=False)}
            for name, frame, column in (
                ("sales", sales, "source_address_id"),
                ("epcs", epcs, "address_id"),
                ("coordinates", coordinates, "address_id"),
                ("rows", rows, "address_id"),
            )
        }
        for address_id in ids:
            own_rows = by_id["rows"].get(address_id)
            if own_rows is None:
                continue
            records[address_id] = _record_from_frames(
                connection,
                [address_id],
                by_id["sales"].get(address_id, sales.iloc[0:0].reset_index(drop=True)),
                by_id["epcs"].get(address_id, epcs.iloc[0:0].reset_index(drop=True)),
                by_id["coordinates"].get(address_id, coordinates.iloc[0:0].reset_index(drop=True)),
                own_rows,
            )
    return records


def _record_from_frames(
    connection,
    address_ids: Sequence[int],
    sales: pd.DataFrame,
    epcs: pd.DataFrame,
    coordinates: pd.DataFrame,
    rows: pd.DataFrame,
) -> PropertyRecord:
    """Build the `PropertyRecord` of `address_ids` from its frames (`load_property_by_ids`)."""
    # The row with the street and a sale names the property best.
    order = rows.assign(
        _street=rows["poststreet"].map(normalise_address_part) != "",
        _sold=rows["address_id"].isin(sales["source_address_id"]) if len(sales) else False,
    ).sort_values(["_sold", "_street"], ascending=False)
    best = order.iloc[0]
    # The unit, when every matched row names the same one.
    units = {unit_key(u) for u in rows["unit"]} if "unit" in rows else {None}
    resolved = PropertyAddress(
        postnumber=str(best["postnumber"] or "").strip(),
        poststreet=(str(best["poststreet"]).strip() if isinstance(best["poststreet"], str) else None),
        posttown=str(best["posttown"] or "").strip(),
        postcode=normalise_postcode(best["postcode"]),
        unit=(str(best["unit"]).strip() if len(units) == 1 and isinstance(best.get("unit"), str) else None),
    )
    point = None
    if len(coordinates):
        point = (float(coordinates["latitude"].iloc[0]), float(coordinates["longitude"].iloc[0]))
    record = PropertyRecord(resolved, list(address_ids), sales, epcs, point)
    if sales.empty:
        template = pd.read_sql(_POSTCODE_SALE_SQL, connection, params={"postcode": resolved.postcode})
        if len(template):
            record.postcode_template = template.iloc[0]
        else:
            district, sector = _district_and_sector(resolved.postcode)
            params = {"district": district, "sector": sector}
            if point is not None:
                template = pd.read_sql(
                    _SECTOR_NEAREST_SALE_SQL, connection, params={**params, "lat": point[0], "lon": point[1]}
                )
            else:
                template = pd.read_sql(_SECTOR_LATEST_SALE_SQL, connection, params=params)
            if len(template):
                record.postcode_template = template.iloc[0]
                record.template_basis = "sector_sale"
        modal = connection.execute(_POSTCODE_MODAL_TYPE_SQL, {"postcode": resolved.postcode}).scalar()
        record.postcode_modal_type = str(modal) if modal is not None else None
    return record


_POSTCODE_CENTROID_SQL = text(
    """
    SELECT avg(ac.latitude) AS latitude, avg(ac.longitude) AS longitude,
           min(a.id) AS address_id
    FROM production.address_table a
    JOIN production.address_coordinates ac ON ac.address_id = a.id
    WHERE a.postcode = :postcode
    """
)


def load_postcode_record(connection, postcode: str) -> PropertyRecord:
    """
    Return a record standing for "a home in this postcode", with no address of its own.

    ``POST /estimate`` is given a postcode, a type, a bedroom count and a floor
    area but no house number, so there is nothing to resolve in the register.
    The location comes from the postcode's centroid and the static columns from
    the same template sale a never-sold address would use, which is the case
    `build_sale_row` was already written for.

    Raises
    ------
    NotEnoughData
        When the postcode has neither coordinates nor a sale to place it by.
    """
    postcode = normalise_postcode(postcode)
    centroid = pd.read_sql(_POSTCODE_CENTROID_SQL, connection, params={"postcode": postcode}).iloc[0]
    point = None
    if pd.notna(centroid["latitude"]) and pd.notna(centroid["longitude"]):
        point = (float(centroid["latitude"]), float(centroid["longitude"]))
    address = PropertyAddress(postnumber="", poststreet=None, posttown="", postcode=postcode)
    empty_sales = pd.read_sql(_OWN_SALES_SQL, connection, params={"address_ids": [-1]})
    empty_epcs = pd.read_sql(_EPC_SQL, connection, params={"address_ids": [-1]})
    address_id = int(centroid["address_id"]) if pd.notna(centroid["address_id"]) else -1
    record = PropertyRecord(address, [address_id], empty_sales, empty_epcs, point)

    template = pd.read_sql(_POSTCODE_SALE_SQL, connection, params={"postcode": postcode})
    if len(template):
        record.postcode_template = template.iloc[0]
    else:
        district, sector = _district_and_sector(postcode)
        params = {"district": district, "sector": sector}
        if point is not None:
            template = pd.read_sql(
                _SECTOR_NEAREST_SALE_SQL, connection, params={**params, "lat": point[0], "lon": point[1]}
            )
        else:
            template = pd.read_sql(_SECTOR_LATEST_SALE_SQL, connection, params=params)
        if len(template):
            record.postcode_template = template.iloc[0]
            record.template_basis = "sector_sale"
    if record.postcode_template is None:
        raise NotEnoughData(
            "We have no recorded sales in or near this postcode to compare with, so we cannot value it."
        )
    modal = connection.execute(_POSTCODE_MODAL_TYPE_SQL, {"postcode": postcode}).scalar()
    record.postcode_modal_type = str(modal) if modal is not None else None
    return record


def _district_and_sector(postcode: str) -> Tuple[str, str]:
    """Return a postcode's district and sector as the view keys them, e.g. ``("TW9", "TW9_3")``."""
    district, _, inward = normalise_postcode(postcode).partition(" ")
    return district, f"{district}_{inward[:1]}"


def _epc_in_force(epcs: pd.DataFrame, day) -> Optional[pd.Series]:
    """Return the certificate the view would join for a sale on `day`: the latest inspected on or before it."""
    if epcs.empty:
        return None
    usable = epcs[pd.to_datetime(epcs["inspection_date"]) <= pd.Timestamp(day)]
    if usable.empty:
        return None
    usable = usable.assign(
        _inspection=pd.to_datetime(usable["inspection_date"]),
        _lodged=pd.to_datetime(usable["lodgement_datetime"]).fillna(pd.Timestamp.min),
    ).sort_values(["_inspection", "_lodged", "id"], ascending=False)
    return usable.iloc[0]


def _ppd_type_from_epc(epc: pd.Series) -> Optional[str]:
    """Return the Price Paid property type an EPC describes, or None when it cannot say."""
    kind = epc.get("property_type")
    if kind in ("Flat", "Maisonette"):
        return "F"
    if kind in ("House", "Bungalow"):
        return _BUILD_FORM_TO_PPD_TYPE.get(epc.get("build_form"))
    return None


def _ppd_type_from_address(address: PropertyAddress) -> Optional[str]:
    """
    Return ``"F"`` when the address itself says the home is a flat, else None.

    A unit ("FLAT 6"), a flat word (FLAT, APARTMENT, UNIT, ...) in the number or street, or a
    number (``3``, ``12A``) inside a named building (``12A ROSE COURT``). A
    bare ``12A`` on a road is not enough: it is as often a house.
    """
    tokens = normalise_address_part(address.postnumber).split()
    street = normalise_address_part(address.poststreet).split()
    if unit_key(address.unit) is not None or _FLAT_ADDRESS_TOKENS & (set(tokens) | set(street)):
        return "F"
    has_number = any(re.fullmatch(r"\d+[A-Z]?", t) for t in tokens)
    words = [t for t in tokens if re.fullmatch(r"[A-Z]{2,}", t)]
    return "F" if has_number and words else None


def _building_token(postnumber) -> str:
    """Return the building name inside a house number, e.g. ``"FLAT 3 CRESSALL HOUSE"`` -> ``"CRESSALL HOUSE"``."""
    tokens = normalise_address_part(postnumber).split()
    return " ".join(
        t for t in tokens if t not in _FLAT_ADDRESS_TOKENS and not _HOUSE_NUMBER_RE.fullmatch(t)
    )


def resolve_property_type(record: PropertyRecord, certificate: Optional[pd.Series] = None) -> Optional[str]:
    """
    Return the Price Paid type of a home, or None when nothing says.

    In order: its latest recorded sale; the certificate (`certificate`, else
    its latest, even one without a floor area); its address text; the modal
    sold type in its postcode.
    """
    if len(record.sales):
        return str(record.sales["propertytype"].iloc[0])
    candidates = [certificate] if certificate is not None else []
    if not record.epcs.empty:
        latest = record.epcs.sort_values(["inspection_date", "id"], ascending=False)
        candidates.extend(row for _, row in latest.iterrows())
    for epc in candidates:
        kind = _ppd_type_from_epc(epc)
        if kind is not None:
            return kind
    return _ppd_type_from_address(record.address) or record.postcode_modal_type


# --------------------------------------------------------------------------
# Homes without a certificate: a surrogate from comparable certified homes
# --------------------------------------------------------------------------

EPC_SOURCE_CERTIFICATE = "certificate"
EPC_SOURCE_ESTIMATED = "estimated"

# Tightest pool first. A pool is used when it holds at least
# MIN_EPC_NEIGHBOURS certified homes of the same type.
ESTIMATE_LEVELS = ("building", "street", "postcode", "sector", "district")
ESTIMATE_LEVEL_NONE = "none"

# How a home without a certificate is scored: from the surrogate certificate,
# or with the EPC fields left missing. `evaluate --mask-epc` measures both and
# records the choice in the calibration file.
ESTIMATE_STRATEGY_SURROGATE = "surrogate"
ESTIMATE_STRATEGY_NAN = "nan"
# NaN inputs replace the surrogate only when they beat it by more than this.
NAN_STRATEGY_MIN_ADVANTAGE = 0.05

_TYPE_NOUNS = {
    "D": "detached houses",
    "S": "semi-detached houses",
    "T": "terraced houses",
    "F": "flats",
}

# Certificates in force on `day` with a floor area and a rating, for every
# register row in `postcodes` except the home's own. One row per register row;
# several rows of one home are folded in Python.
_NEIGHBOUR_EPC_SQL = text(
    """
    SELECT DISTINCT ON (ec.address_id)
           ec.address_id, a.postnumber, a.poststreet, a.postcode,
           ec.construction_age_band, ec.number_habitable_rooms, ec.current_energy_rating,
           ec.total_floor_area, bf.build_form, ec.mains_gas_flag, ec.tenure,
           ec.multi_glaze_proportion, ec.extension_count, ec.floor_level, ec.property_type,
           ec.inspection_date, c.latitude, c.longitude
    FROM production.address_table a
    JOIN production.epc_table ec ON ec.address_id = a.id
    LEFT JOIN production.build_form_category_table bf ON bf.id = ec.build_form
    LEFT JOIN production.address_coordinates c ON c.address_id = a.id
    WHERE a.postcode = ANY(:postcodes)
      AND ec.inspection_date IS NOT NULL
      AND ec.inspection_date <= :day
      AND ec.total_floor_area IS NOT NULL
      AND ec.current_energy_rating IS NOT NULL
      AND NOT (a.id = ANY(:exclude_ids))
    ORDER BY ec.address_id, ec.inspection_date DESC, ec.lodgement_datetime DESC NULLS LAST, ec.id DESC
    """
)

# The postcodes under a sector or district prefix, from address_search's
# text_pattern_ops index (address_table's collated btree cannot serve a prefix).
_POSTCODES_WITH_PREFIX_SQL = text(
    "SELECT DISTINCT postcode FROM production.address_search WHERE postcode LIKE :prefix"
)


@dataclass(frozen=True)
class EpcEstimate:
    """
    Where a home's certificate values came from.

    Attributes
    ----------
    source : {"certificate", "estimated"}
    level : str or None
        The pool used (`ESTIMATE_LEVELS`), ``"none"`` when no pool reached
        `MIN_EPC_NEIGHBOURS` (the EPC fields are then left missing).
    n : int
        Homes in that pool, or in the largest pool tried for ``"none"``.
    floor_area_iqr : tuple of float or None
        The pool's 25th and 75th percentile floor area.
    description : str or None
        E.g. ``"12 terraced houses on this street"``.
    """

    source: str
    level: Optional[str] = None
    n: int = 0
    floor_area_iqr: Optional[Tuple[float, float]] = None
    description: Optional[str] = None


class NeighbourCertificates:
    """
    Certified homes near a home, read from the database.

    `certificates` and `postcodes_with_prefix` are the two reads
    `estimate_epc` makes; tests substitute an in-memory source.
    """

    def __init__(self, connection, day, exclude_ids: Sequence[int]) -> None:
        self.connection = connection
        self.day = pd.Timestamp(day).date()
        self.exclude_ids = [int(i) for i in exclude_ids]

    def certificates(self, postcodes: Sequence[str]) -> pd.DataFrame:
        """Return the certificates in force for every home in `postcodes`."""
        if not postcodes:
            return pd.DataFrame()
        return pd.read_sql(
            _NEIGHBOUR_EPC_SQL,
            self.connection,
            params={"postcodes": list(postcodes), "day": self.day, "exclude_ids": self.exclude_ids},
        )

    def postcodes_with_prefix(self, prefix: str) -> List[str]:
        """Return the postcodes starting with `prefix` (a sector ``"TW9 3"`` or district ``"TW9 "``)."""
        rows = self.connection.execute(_POSTCODES_WITH_PREFIX_SQL, {"prefix": prefix + "%"})
        return sorted(str(r[0]) for r in rows)


def _mode(values: pd.Series):
    """Return the most common non-missing value (ties to the smallest), or NaN."""
    present = [v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))]
    if not present:
        return np.nan
    counts = Counter(present)
    top = max(counts.values())
    return sorted((v for v, c in counts.items() if c == top), key=str)[0]


def _comparable_homes(frame: pd.DataFrame, propertytype: str, address: PropertyAddress) -> pd.DataFrame:
    """
    Return one certificate per comparable home: same type, not the home itself.

    A home held as several register rows keeps its latest certificate (a
    blank-street row folds into the one named row with its number, as
    ``GET /addresses`` folds them); the home's own number in its own postcode
    is dropped even under another register row, so its certificate never
    estimates itself.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["postcode", "_num", "_street", "_building", "total_floor_area"])
    frame = frame.assign(
        _type=[_ppd_type_from_epc(row) for _, row in frame.iterrows()],
        _num=frame["postnumber"].map(normalise_address_part),
        _street=frame["poststreet"].map(normalise_address_part),
        _building=frame["postnumber"].map(_building_token),
    )
    own = (frame["postcode"] == normalise_postcode(address.postcode)) & (
        frame["_num"] == normalise_address_part(address.postnumber)
    )
    frame = frame[(frame["_type"] == propertytype) & ~own]
    frame = frame.sort_values("inspection_date", ascending=False, kind="stable")
    frame = frame.drop_duplicates(["postcode", "_num", "_street"])
    # A blank-street row is the same home as the one named row with its number.
    named = frame[frame["_street"] != ""].groupby(["postcode", "_num"]).size()
    blank = frame["_street"] == ""
    folded = blank & pd.Series(
        [named.get((pc, num), 0) == 1 for pc, num in zip(frame["postcode"], frame["_num"])],
        index=frame.index,
        dtype=bool,
    )
    return frame[~folded].reset_index(drop=True)


def _summarise_pool(
    pool: pd.DataFrame, level: str, propertytype: str, address: PropertyAddress
) -> Tuple[pd.Series, EpcEstimate]:
    """Return the surrogate certificate a pool of comparable homes gives."""
    area = pd.to_numeric(pool["total_floor_area"], errors="coerce")
    rooms = pd.to_numeric(pool["number_habitable_rooms"], errors="coerce")
    glazing = pd.to_numeric(pool["multi_glaze_proportion"], errors="coerce")
    extensions = pd.to_numeric(pool["extension_count"], errors="coerce")
    values = {
        "total_floor_area": float(area.median()),
        "number_habitable_rooms": float(np.round(rooms.median())) if rooms.notna().any() else np.nan,
        "current_energy_rating": _mode(pool["current_energy_rating"]),
        "construction_age_band": _mode(pool["construction_age_band"]),
        "build_form": _mode(pool["build_form"]),
        "tenure": _mode(pool["tenure"]),
        "mains_gas_flag": _mode(pool["mains_gas_flag"]),
        "multi_glaze_proportion": float(glazing.median()) if glazing.notna().any() else np.nan,
        "extension_count": float(np.round(extensions.median())) if extensions.notna().any() else np.nan,
        # A house has no floor level; a flat takes its building's commonest.
        "floor_level": _mode(pool["floor_level"]) if propertytype == "F" else np.nan,
        "property_type": _mode(pool["property_type"]),
    }
    district, _, inward = normalise_postcode(address.postcode).partition(" ")
    noun = _TYPE_NOUNS.get(propertytype, "similar homes")
    n = int(len(pool))
    where = {
        "building": "in this building",
        "street": "on this street",
        "postcode": "in this postcode",
        "sector": f"in the {district} {inward[:1]} postcode sector",
        "district": f"within {COMPS_RADIUS_M / 1000:g} km in {district}",
    }[level]
    estimate = EpcEstimate(
        source=EPC_SOURCE_ESTIMATED,
        level=level,
        n=n,
        floor_area_iqr=(round(float(area.quantile(0.25)), 1), round(float(area.quantile(0.75)), 1)),
        description=f"{n} {noun} {where}",
    )
    return pd.Series(values, dtype=object), estimate


def _missing_certificate() -> pd.Series:
    """Return certificate values that are all missing."""
    return pd.Series({col: np.nan for col in EPC_COLUMNS + ["property_type"]}, dtype=object)


def estimate_epc(
    record: PropertyRecord,
    day,
    store: Optional["EvidenceStore"],
    connection=None,
    propertytype: Optional[str] = None,
    source: Optional[NeighbourCertificates] = None,
) -> Tuple[pd.Series, EpcEstimate]:
    """
    Estimate what an energy certificate would say for a home that has none.

    Pools of certified homes of the same type are tried tightest first --
    the same building (postcode, street and building name), the same street
    and postcode, the postcode, the postcode sector, and the district within
    `COMPS_RADIUS_M` of the home -- and the first with `MIN_EPC_NEIGHBOURS`
    homes gives the median floor area and rooms and the modal rating, age
    band, build form and tenure (and, for a flat, floor level). Only
    certificates in force on `day` are read, so an evaluation on a past sale
    sees nothing from after it.

    Parameters
    ----------
    record : PropertyRecord
    day : date-like
    store : EvidenceStore or None
        Unused; kept so every feature builder takes the same arguments.
    connection : sqlalchemy Connection, optional
        Where certificates are read from, unless `source` is given.
    propertytype : str, optional
        The home's Price Paid type; resolved from the record when omitted.
    source : NeighbourCertificates, optional

    Returns
    -------
    values : pandas.Series
        `EPC_COLUMNS` plus ``property_type``; all missing when no pool is big enough.
    estimate : EpcEstimate
    """
    propertytype = propertytype or resolve_property_type(record)
    if source is None:
        if connection is None:
            return _missing_certificate(), EpcEstimate(EPC_SOURCE_ESTIMATED, ESTIMATE_LEVEL_NONE)
        source = NeighbourCertificates(connection, day, record.address_ids)
    if propertytype is None:
        return _missing_certificate(), EpcEstimate(EPC_SOURCE_ESTIMATED, ESTIMATE_LEVEL_NONE)

    address = record.address
    postcode = normalise_postcode(address.postcode)
    street = normalise_address_part(address.poststreet)
    building = _building_token(address.postnumber)
    district, _, inward = postcode.partition(" ")
    largest = 0

    local = _comparable_homes(source.certificates([postcode]), propertytype, address)
    pools = []
    if building and street:
        pools.append(("building", local[(local["_street"] == street) & (local["_building"] == building)]))
    if street:
        pools.append(("street", local[local["_street"] == street]))
    pools.append(("postcode", local))
    for level, pool in pools:
        largest = max(largest, len(pool))
        if len(pool) >= MIN_EPC_NEIGHBOURS:
            return _summarise_pool(pool, level, propertytype, address)

    if inward:
        sector_codes = source.postcodes_with_prefix(f"{district} {inward[:1]}")
        sector = _comparable_homes(source.certificates(sector_codes), propertytype, address)
        largest = max(largest, len(sector))
        if len(sector) >= MIN_EPC_NEIGHBOURS:
            return _summarise_pool(sector, "sector", propertytype, address)

    district_codes = source.postcodes_with_prefix(f"{district} ")
    nearby = _comparable_homes(source.certificates(district_codes), propertytype, address)
    if record.coordinates is not None and len(nearby):
        lat = pd.to_numeric(nearby["latitude"], errors="coerce").to_numpy(dtype=np.float64)
        lon = pd.to_numeric(nearby["longitude"], errors="coerce").to_numpy(dtype=np.float64)
        distance = _local_metres(record.coordinates[0], record.coordinates[1], lat, lon)
        nearby = nearby[np.isfinite(distance) & (distance <= COMPS_RADIUS_M)]
    largest = max(largest, len(nearby))
    if len(nearby) >= MIN_EPC_NEIGHBOURS:
        return _summarise_pool(nearby, "district", propertytype, address)
    return _missing_certificate(), EpcEstimate(EPC_SOURCE_ESTIMATED, ESTIMATE_LEVEL_NONE, n=largest)


@dataclass
class SaleRow:
    """A built view row and what it was built from."""

    row: Dict[str, object]
    basis: str
    last_sale: Optional[Tuple[pd.Timestamp, float]]
    epc_inspected: Optional[pd.Timestamp]
    epc: EpcEstimate = field(default_factory=lambda: EpcEstimate(EPC_SOURCE_CERTIFICATE))
    # Whether a certificate since the previous sale shows the home renovated
    # (`_renovated_since`), and so whether that sale may anchor its price.
    renovated_since_prev_sale: Optional[bool] = None
    recent_sale_eligible: bool = False


# 1.0 on a built row whose home a certificate since its previous sale shows
# renovated, 0.0 when none does, NaN without a previous sale.
RENOVATED_COLUMN = "renovated_since_prev_sale"


def _is_missing(value) -> bool:
    """Return whether a certificate value is absent."""
    return value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NA


def build_sale_row(
    record: PropertyRecord,
    sale_date,
    store: EvidenceStore,
    population: str = POPULATION_EPC_MATCHED,
    anchor=None,
    connection=None,
    epc_strategy: str = ESTIMATE_STRATEGY_SURROGATE,
    epc_source: Optional[NeighbourCertificates] = None,
    overrides: Optional[Mapping[str, object]] = None,
    force_propertytype: Optional[str] = None,
) -> SaleRow:
    """
    Build the ``production.london_houses`` row a sale of `record` on `sale_date` would have.

    `anchor` overrides the evidence date (evaluation passes the sale date
    itself); by default it is `EvidenceStore.evidence_date`.

    `overrides` replaces columns once the certificate is in place and before
    anything derived from it is computed, which is how ``POST /estimate``
    prices the floor area and room count a landlord types rather than the
    surrogate certificate's guess.

    A home without a certificate carrying a floor area and an energy rating
    is given a surrogate one by `estimate_epc` (read through `connection` or
    `epc_source`), or none when `epc_strategy` is ``"nan"`` or no pool of
    comparable homes is big enough. Either way ``epc_matched`` is False,
    ``epc_age_at_sale_days`` is missing and ``epc_missing`` is 1.

    Raises
    ------
    NotEnoughData
        When there is no sale nearby to take the location from, or no way to
        tell the property type.
    """
    day = pd.Timestamp(sale_date).normalize()
    anchor = store.evidence_date(day) if anchor is None else pd.Timestamp(anchor)
    sales = record.sales
    if len(sales):
        dated = pd.to_datetime(sales[DATE_COLUMN])
        previous = sales[(dated <= day).to_numpy()]
    else:
        previous = sales
    if force_propertytype is not None and len(previous):
        # The landlord's own answer still wins over what the address last sold as.
        previous = previous[(previous["propertytype"].astype("str") == force_propertytype).to_numpy()]
    epc = _epc_in_force(record.epcs, day)
    has_certificate = (
        epc is not None
        and pd.notna(epc.get("total_floor_area"))
        and pd.notna(epc.get("current_energy_rating"))
    )

    row: Dict[str, object] = {col: np.nan for col in VIEW_COLUMNS}
    row[hpp_rf.UKHPI_AT_PREV_SALE_COLUMN] = np.nan
    row[RENOVATED_COLUMN] = np.nan
    row["prev_sale_local_indexed"] = np.nan
    if len(previous):
        template = previous.iloc[0]
        basis = "own_sale"
        propertytype = str(template["propertytype"])
        duration = template["duration"]
    elif record.postcode_template is not None:
        template = record.postcode_template
        basis = record.template_basis
        # A details estimate is told the type; everything else infers it.
        propertytype = force_propertytype or resolve_property_type(record, epc)
        if propertytype is None:
            raise NotEnoughData("We cannot tell what type of home this is from its records, so we cannot price it.")
        duration = "L" if propertytype == "F" else "F"
    else:
        raise NotEnoughData(
            "We have no location for this home: there are no recorded sales in or near its postcode "
            "to compare it with, so we cannot price it."
        )

    if has_certificate:
        certificate = epc
        estimate = EpcEstimate(EPC_SOURCE_CERTIFICATE)
    elif epc_strategy == ESTIMATE_STRATEGY_NAN:
        certificate, estimate = _missing_certificate(), EpcEstimate(EPC_SOURCE_ESTIMATED, ESTIMATE_LEVEL_NONE)
    else:
        certificate, estimate = estimate_epc(record, day, store, connection, propertytype, source=epc_source)

    for col in STATIC_COLUMNS:
        row[col] = template.get(col, np.nan)
    if basis != "own_sale":
        row["uprn"] = np.nan
        # A never-sold home's plot is unknown; a neighbour's would be a guess.
        row["lot_area_sqm"] = np.nan
        if record.coordinates is not None:
            row["latitude"], row["longitude"] = record.coordinates
    if pd.notna(row.get("iod_release_age_years")):
        # Aged to the evidence date, not the sale date. This and
        # `years_since_prev_sale` below are the only features that would grow
        # without bound as the horizon extends, and a tree asked for a value
        # past its training range answers with the edge of it -- which is what
        # made a 2031 price wobble a thousand pounds below a 2030 one instead
        # of sitting above it. Held here, every feature stays in distribution
        # and all movement with the sale date comes from the index projection
        # in `EvidenceStore.ukhpi_on`, which is measured and can be shown.
        elapsed = (anchor - pd.Timestamp(template[DATE_COLUMN])).days / 365.25
        row["iod_release_age_years"] = float(row["iod_release_age_years"]) + elapsed

    row["ppd_id"] = np.nan
    row["address_id"] = record.address_ids[0]
    row[TARGET_COLUMN] = np.nan
    row[DATE_COLUMN] = day
    row["propertytype"] = propertytype
    row["duration"] = duration
    row["oldnew"] = "N"
    if basis == "own_sale":
        # The street exactly as the view spells it for this property's sales,
        # which is what the same-street groups were keyed on.
        row["poststreet"] = template.get("poststreet", np.nan)
        row[hpp_rf.STREET_KEY_COLUMN] = template.get(hpp_rf.STREET_KEY_COLUMN, np.nan)
    else:
        row["poststreet"] = record.address.poststreet
        row[hpp_rf.STREET_KEY_COLUMN] = np.nan
    if not isinstance(row.get(hpp_rf.STREET_KEY_COLUMN), str):
        row[hpp_rf.STREET_KEY_COLUMN] = hpp_rf.normalise_street_key(row["poststreet"])
        district, sector = _district_and_sector(record.address.postcode)
        row["postcode_district"] = district
        row["postcode_sector"] = sector

    for col in EPC_COLUMNS:
        value = certificate.get(col)
        row[col] = np.nan if _is_missing(value) else value
    inspected = pd.Timestamp(epc["inspection_date"]) if has_certificate else None
    row["epc_age_at_sale_days"] = float((day - inspected).days) if inspected is not None else np.nan
    row["build_form_propertytype"] = f"{row['build_form'] if isinstance(row['build_form'], str) else 'UNKNOWN'}_{propertytype}"
    band = row["construction_age_band"]
    row["construction_age_band_propertytype"] = f"{band if isinstance(band, str) else 'UNKNOWN'}_{propertytype}"
    kind = certificate.get("property_type")
    row["is_maisonette"] = (propertytype == "F" and kind == "Maisonette") if isinstance(kind, str) else None
    row["epc_matched"] = bool(has_certificate)
    row[hpp_cb.EPC_MISSING_FLAG] = 0.0 if has_certificate else 1.0

    # What the landlord typed wins over a surrogate certificate's guess. Set
    # before the neighbourhood features below, which read the floor area.
    for column, value in (overrides or {}).items():
        if value is not None:
            row[column] = value

    row["boe_rate_at_sale"] = store.boe_rate_on(day)
    row["ukhpi_avg_price_at_sale"] = store.ukhpi_on(day, propertytype)

    last_sale, renovated, eligible = None, None, False
    if pd.notna(row["uprn"]):
        prior = previous[
            (pd.to_datetime(previous[DATE_COLUMN]) < day).to_numpy()
            & (previous[TARGET_COLUMN].to_numpy(dtype=np.float64) >= MIN_PLAUSIBLE_PRICE)
        ] if len(previous) else previous
        if len(prior):
            prior_date = pd.Timestamp(prior[DATE_COLUMN].iloc[0])
            prior_price = float(prior[TARGET_COLUMN].iloc[0])
            row["prev_sale_price"] = prior_price
            row["prev_sale_date"] = prior_date
            # Measured to the evidence date for the same reason as
            # `iod_release_age_years` above; the sale itself is still reported
            # from `last_sale` exactly as it stands.
            row["years_since_prev_sale"] = max((anchor - prior_date).days, 0) / 365.25
            # The index the previous price is deflated by, with the view's
            # as-of rule (hpp_rf.add_market_relative_features reads it).
            row[hpp_rf.UKHPI_AT_PREV_SALE_COLUMN] = store.ukhpi_on(prior_date, propertytype)
            # Moved to the row's date along the home's own local index, as
            # `hpp_rf.local_indexed_prev_sale` does at training time.
            district_now = row["postcode_district"] if isinstance(row["postcode_district"], str) else None
            row["prev_sale_local_indexed"] = prior_price * store.time_factor(prior_date, day, district_now, propertytype)
            last_sale = (prior_date, prior_price)
            renovated = _renovated_since(record.epcs, prior_date, day)
            row[RENOVATED_COLUMN] = float(renovated)
            eligible = _recent_sale_eligible(record, prior_date, prior_price, day)

    lat, lon = float(row["latitude"]), float(row["longitude"])
    area = float(row["total_floor_area"])
    row.update(store.comps_features(anchor, lat, lon, propertytype))
    row.update(store.neighbourhood_features(anchor, lat, lon, propertytype, area))
    row.update(store.msoa_trend_features(anchor, row["msoa_code"]))
    # As of the evidence date, like the sales features: a rating's age would
    # otherwise grow without bound with the horizon (see iod_release_age_years).
    row.update(store.ofsted_features(anchor, lat, lon))
    row.update(
        store.rolling_comp_features(
            anchor,
            propertytype,
            {"postcode_sector": row["postcode_sector"], "postcode_district": row["postcode_district"]},
            population,
        )
    )
    row.update(
        store.same_street_features(anchor, row["postcode_district"], row[hpp_rf.STREET_KEY_COLUMN], population)
    )
    row.update(
        store.sizeband_comp_features(
            anchor, lat, lon, propertytype, area,
            row["postcode_district"] if isinstance(row["postcode_district"], str) else None,
            population,
        )
    )
    return SaleRow(
        row=row,
        basis=basis,
        last_sale=last_sale,
        epc_inspected=inspected,
        epc=estimate,
        renovated_since_prev_sale=renovated,
        recent_sale_eligible=eligible,
    )


_CAUSAL_COLUMNS = (
    hpp_rf.ROLLING_COMP_STAT_COLUMNS + hpp_rf.SAME_STREET_STAT_COLUMNS
)


def engineer_rows(rows: Sequence[Mapping[str, object]], float_dtype: str) -> pd.DataFrame:
    """
    Return the built rows as an engineered frame, as a training run holds them.

    Columns are coerced the way `hpp_rf.load_dataset` coerces the view, then
    `hpp_rf.engineer_features` adds everything but the causal statistics,
    which the rows already carry from `EvidenceStore`.
    """
    frame = pd.DataFrame(list(rows))
    causal = frame[list(_CAUSAL_COLUMNS)].copy()
    frame = frame.drop(columns=list(_CAUSAL_COLUMNS))
    frame["is_maisonette"] = frame["is_maisonette"].astype("object")
    # A text column that is missing on every row (a home priced without any
    # certificate values) arrives as float NaN; hold it as text like the view.
    for col, kind in hpp_rf.VIEW_COLUMN_KINDS.items():
        if kind == "str" and col in frame.columns and pd.api.types.is_float_dtype(frame[col].dtype):
            frame[col] = frame[col].astype("object")
    hpp_rf._coerce_chunk_dtypes(frame, hpp_rf.resolve_column_kinds(frame), float_dtype)
    frame[DATE_COLUMN] = pd.to_datetime(frame[DATE_COLUMN])
    engineered = hpp_rf.engineer_features(frame, causal_stats=False)
    for col in _CAUSAL_COLUMNS:
        engineered[col] = causal[col].to_numpy(dtype=np.float64)
    return engineered


# --------------------------------------------------------------------------
# Saved models
# --------------------------------------------------------------------------


def calibration_path(model_path) -> Path:
    """Return where `evaluate --write-calibration` stores a model's measured error: its bundle's ``calibration.json``."""
    return Path(model_path) / artifact.CALIBRATION_FILE


def _legacy_sidecar(model_path: Path, suffix: str) -> Optional[dict]:
    """Return a JSON sidecar beside an offline ``hpp_rf`` model file, or None."""
    path = Path(f"{model_path}{suffix}")
    return json.loads(path.read_text()) if path.is_file() else None


class SavedModel:
    """
    A trained model loaded for pricing, with its preprocessing state.

    Attributes
    ----------
    kind : {"catboost", "random_forest"}
        Which script trained it.
    population : str
        The population it was trained on, which the rolling statistics use.
    calibration : dict or None
        The error measured by `evaluate` on sales after its training period.
    projection : dict or None
        The index projection `fit-projection` fitted, which decides how a sale
        date beyond the published index moves the price.
    local_index_params : dict or None
        The local price index `fit-local-index` fitted.

    A CatBoost model is a bundle directory, verified (`models.artifact.load_bundle`)
    before anything in it is used; its JSON is read from the verified bytes.
    A Random Forest is an ``hpp_rf`` pickle, loaded only with `trust_pickle`
    (offline use; the API never serves one).
    """

    def __init__(self, model_path, kind: Optional[str] = None, trust_pickle: bool = False) -> None:
        self.path = Path(model_path)
        self.kind = kind or ("random_forest" if self.path.suffix in RANDOM_FOREST_SUFFIXES else "catboost")
        self.verified = None
        if self.kind == "catboost":
            self.model, self.meta, self.verified = artifact.load_bundle(self.path)
            self.float_dtype = "float32"
            self.calibration = self.verified.payload(artifact.CALIBRATION_FILE)
            # The fitted index projection, written by `fit-projection` (and by
            # `hpp_cb --model-out`). When it is missing, `attach_market_indices`
            # fits one from the live index rather than carrying it forward.
            self.projection = self.verified.payload(artifact.PROJECTION_FILE)
            self.local_index_params = self.verified.payload(artifact.LOCAL_INDEX_FILE)
        elif self.kind == "random_forest":
            self.meta = hpp_rf.load_model(self.path, trust_pickle=trust_pickle)
            self.model = self.meta["model"]
            self.float_dtype = "float64"
            self.calibration = _legacy_sidecar(self.path, ".calibration.json")
            self.projection = _legacy_sidecar(self.path, ".projection.json")
            self.local_index_params = _legacy_sidecar(self.path, ".local_index.json")
        else:
            raise ValueError(f"Unknown model kind {kind!r}, expected catboost or random_forest")
        self.population = self.meta.get("population") or POPULATION_EPC_MATCHED
        self.train_period = self.meta.get("train_period")

    def predict(self, engineered: pd.DataFrame, store: EvidenceStore) -> np.ndarray:
        """Return prices in pounds for an `engineer_rows` frame, in its row order."""
        order = np.argsort(engineered[DATE_COLUMN].to_numpy(), kind="stable")
        ordered = engineered.iloc[order].reset_index(drop=True)
        if self.kind == "random_forest":
            prices = hpp_rf.predict_with_saved_model(self.meta, ordered, engineered=True)
        else:
            X, _, deflator = hpp_cb.prepare_features_from_saved_model(
                ordered, store.mortgage_rates, self.meta, engineered=True
            )
            if list(X.columns) != list(self.meta["feature_columns"]):
                raise RuntimeError("hpp_inference: rebuilt features do not match the model's saved columns")
            target = self.meta.get("target", "price")
            general, mixture = hpp_cb.predict_with_adjustments(
                self.model,
                X,
                self.meta["cat_features"],
                target,
                deflator if target in hpp_cb.DEFLATOR_COLUMN_BY_TARGET else None,
                smearing_factors=self.meta.get("smearing_factors"),
                high_value_model=self.meta.get("high_value_model"),
                high_value_threshold=self.meta.get("high_value_threshold"),
            )
            prices = mixture if mixture is not None else general
        out = np.empty(len(order), dtype=np.float64)
        out[order] = np.asarray(prices, dtype=np.float64)
        return out


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


@dataclass
class PricePrediction:
    """
    One model price and what it rests on.

    ``low`` and ``high`` span the middle 80 % of the model's measured errors
    on recent sales, and are None when the model has not been evaluated. They
    come from the narrowest stratum measured for this price -- its band, type
    and evidence thickness -- widened by the index projection's own error at
    this horizon, and are centred on the bias-corrected value. For a home
    priced from an estimated certificate (``epc_source="estimated"``) they come
    from the errors measured on homes priced that way, which are wider.

    ``evidence_date`` is the date the sales-history features are computed as of
    and ``evidence_to`` the latest sale held. The model prices the home as of
    the evidence date; ``projection_uplift`` is how much the price index then
    moved it from ``projected_from`` (that evidence date) to ``sale_date``, and
    ``index_area`` names whose index that was: the home's postcode district,
    its borough, or London, by type (`local_index`).
    """

    address: PropertyAddress
    sale_date: date
    evidence_date: date
    evidence_to: date
    value: float
    low: Optional[float]
    high: Optional[float]
    propertytype: str
    is_maisonette: Optional[bool]
    floor_area_sqm: Optional[float]
    energy_rating: Optional[str]
    habitable_rooms: Optional[float]
    last_sale_date: Optional[date]
    last_sale_price: Optional[float]
    basis: str
    model_kind: str
    within_10_percent: Optional[float] = None
    # What the index projection added for a sale beyond the published index,
    # and the band the range was measured on. Both are None when nothing was
    # projected or no calibration was available.
    projection_uplift: Optional[float] = None
    projected_from: Optional[date] = None
    index_area: Optional[str] = None
    range_stratum: Optional[str] = None
    epc_source: str = EPC_SOURCE_CERTIFICATE
    estimate_level: Optional[str] = None
    estimate_neighbours: Optional[int] = None
    estimated_from: Optional[str] = None
    # True when the home's own recent sale carries most of the price
    # (`RECENT_SALE_WEIGHT_FLOOR`); the range then comes from `recent_sale:<bucket>`.
    anchored_on_last_sale: bool = False
    extra: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        """Return the prediction as JSON-ready values."""
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, (date, datetime)):
                out[key] = value.isoformat()
        return out


def today_in_london() -> date:
    """Return today's date in London, which decides what "the future" is."""
    return datetime.now(LONDON_TZ).date()


def validate_sale_date(sale_date: date, today: Optional[date] = None) -> date:
    """
    Check a sale date is after today and within `MAX_HORIZON_DAYS`.

    Raises
    ------
    InvalidSaleDate
        With the earliest and latest dates allowed.
    """
    today = today or today_in_london()
    earliest = today + timedelta(days=1)
    latest = today + timedelta(days=MAX_HORIZON_DAYS)
    if sale_date < earliest:
        raise InvalidSaleDate(
            f"Choose a sale date from {earliest.isoformat()} onwards: we can only price a future sale."
        )
    if sale_date > latest:
        raise InvalidSaleDate(f"Choose a sale date no later than {latest.isoformat()}.")
    return sale_date


# The strata the calibration measures error over, most specific first. A price
# takes the narrowest stratum that has enough evaluation sales behind it, and
# falls back through the list to the global block.
# The bands the model's own smearing correction is measured over
# (`hpp_rf.PRICE_BAND_LABELS` / `PRICE_BAND_EDGES`), reused so a price's band
# means the same thing everywhere.
PRICE_BANDS = tuple(
    (label, float(hpp_rf.PRICE_BAND_EDGES[i]), float(hpp_rf.PRICE_BAND_EDGES[i + 1]))
    for i, label in enumerate(hpp_rf.PRICE_BAND_LABELS)
)

# The column evidence thickness is read from, and how much of it counts as
# thin, ordinary or thick. Sales on the home's own street over the last year:
# `comp_txn_count` cannot serve, because it counts the COMPS_K (5) nearest
# sales and so reads 5 for 99.8 % of rows.
EVIDENCE_COLUMN = "street_sales_count_12m"
EVIDENCE_BUCKETS = (("thin", 0.0, 2.0), ("ordinary", 2.0, 7.0), ("thick", 7.0, float("inf")))

# A stratum needs this many evaluation sales before its own quantiles are
# trusted over its parent's.
MIN_STRATUM_SALES = 120

# Area strata (``district:<code>|<type>``, ``borough:<name>|<type>``) are
# looked up before the band strata: the same model misses by different amounts,
# and in different directions, in different places -- in 2026 it read SW11
# terraces 3.7 % high at the median and HA0 terraces 3.6 % low. An area block
# needs `MIN_AREA_STRATUM_SALES` sales, and its quantiles are shrunk toward its
# parent's (the borough's, then the type's) as ``(n q + k q_parent) / (n + k)``,
# with ``k`` chosen from `AREA_PRIOR_GRID` by cross-fitted quantile loss.
MIN_AREA_STRATUM_SALES = 20
AREA_PRIOR_GRID: Tuple[int, ...] = (10, 30, 100, 300, 1000)
DEFAULT_AREA_PRIOR_SALES = 100

QUANTILE_LEVELS: Tuple[Tuple[str, float], ...] = (
    ("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90)
)


def price_band_of(value: float) -> str:
    """Return the price band a value falls in, as `PRICE_BANDS` names them."""
    for name, low, high in PRICE_BANDS:
        if low <= value < high:
            return name
    return PRICE_BANDS[-1][0]


def evidence_bucket_of(street_sales) -> str:
    """Return how thick the same-street evidence is, as `EVIDENCE_BUCKETS` names it."""
    count = 0.0 if street_sales is None or pd.isna(street_sales) else float(street_sales)
    for name, low, high in EVIDENCE_BUCKETS:
        if low <= count < high:
            return name
    return EVIDENCE_BUCKETS[-1][0]


def stratum_keys(value: float, propertytype: str, street_sales) -> List[str]:
    """
    Return the stratum keys to try for a price, narrowest first.

    ``band|type|evidence``, then ``band|type``, then ``band``, then ``type``.
    `_stratified_calibration` writes exactly these keys.
    """
    band = price_band_of(value)
    evidence = evidence_bucket_of(street_sales)
    return [
        f"{band}|{propertytype}|{evidence}",
        f"{band}|{propertytype}",
        band,
        str(propertytype),
    ]


# Area strata are also keyed by the value tier of the price (``district:SW3|F|2m+``):
# a district's error is mostly its cheaper homes' error, and SW3's flats --
# mostly under £2m, read 5.6 % high at the median -- cut a £3m flat by that
# much when flats of its value there read about 2 % high. The tiers follow the
# band strata's edges, so the £1-2m and £2m+ tiers shrink toward exactly the
# ``£1-2m|<type>`` and ``£2m+|<type>`` band blocks.
VALUE_TIERS: Tuple[Tuple[str, float, float], ...] = (
    ("<1m", 0.0, 1_000_000.0),
    ("1-2m", 1_000_000.0, 2_000_000.0),
    ("2m+", 2_000_000.0, float("inf")),
)


def value_tier_of(value: float) -> str:
    """Return the `VALUE_TIERS` label a price falls in."""
    for name, low, high in VALUE_TIERS:
        if low <= value < high:
            return name
    return VALUE_TIERS[-1][0] if value >= VALUE_TIERS[-1][1] else VALUE_TIERS[0][0]


def area_keys(
    propertytype: str,
    district: Optional[str] = None,
    borough: Optional[str] = None,
    tier: Optional[str] = None,
) -> List[str]:
    """
    Return the area stratum keys to try for a home, as `_area_calibration` writes them.

    With a `tier`, the tiered district and borough keys first; the untiered
    ones follow, which only a calibration written before tiers existed holds.
    """
    keys = []
    has_district = isinstance(district, str) and bool(district)
    has_borough = isinstance(borough, str) and bool(borough)
    if tier is not None:
        if has_district:
            keys.append(f"district:{district}|{propertytype}|{tier}")
        if has_borough:
            keys.append(f"borough:{borough}|{propertytype}|{tier}")
    if has_district:
        keys.append(f"district:{district}|{propertytype}")
    if has_borough:
        keys.append(f"borough:{borough}|{propertytype}")
    return keys


def _raw_quantiles(log_ratio: np.ndarray) -> Dict[str, float]:
    """Return the `QUANTILE_LEVELS` of a set of log errors."""
    return {name: float(np.quantile(log_ratio, q)) for name, q in QUANTILE_LEVELS}


def _shrink(raw: Mapping[str, float], n: int, parent: Mapping[str, float], prior: float) -> Dict[str, float]:
    """Return `raw` quantiles from `n` sales shrunk toward `parent` with `prior` pseudo-sales."""
    return {name: (n * raw[name] + prior * parent[name]) / (n + prior) for name, _ in QUANTILE_LEVELS}


def _tier_root_name(kind: str, tier: Optional[str]) -> str:
    """Return the name of the block a tier's area strata shrink toward: its band block where one matches, e.g. ``£2m+|F``."""
    if tier is None:
        return str(kind)
    low = dict((name, lo) for name, lo, _ in VALUE_TIERS)[tier]
    band = price_band_of(low) if low > 0 else None
    if band is not None and dict((b, lo) for b, lo, _ in PRICE_BANDS).get(band) == low:
        return f"{band}|{kind}"
    return f"{tier}|{kind}"


def _area_blocks(
    log_ratio: np.ndarray,
    propertytype: np.ndarray,
    district: np.ndarray,
    borough: np.ndarray,
    prior: float,
    tier: Optional[np.ndarray] = None,
) -> Tuple[Dict[str, Dict[str, object]], Dict[Tuple[str, Optional[str]], Dict[str, float]]]:
    """
    Return the shrunk area blocks for one prior strength, and the root quantiles they shrink toward.

    With `tier` (each sale's `value_tier_of` its predicted price) the blocks
    are ``district:<d>|<type>|<tier>`` and ``borough:<b>|<type>|<tier>``, and
    the root they shrink toward is that type and tier's own quantiles -- for
    the £1-2m and £2m+ tiers exactly the ``£1-2m|<type>`` / ``£2m+|<type>``
    band blocks. Without it they are untiered and the root is the type's.
    A borough block shrinks toward its root; a district block toward its
    borough's block (the borough most of its sales lie in) when that exists,
    else toward the root. Roots are keyed ``(type, tier)``.
    """
    kinds = propertytype.astype("str")
    tiers = np.full(kinds.size, None, dtype=object) if tier is None else np.asarray(tier, dtype=object)
    frame = pd.DataFrame(
        {"lr": log_ratio, "kind": kinds, "tier": tiers, "district": district, "borough": borough}
    )
    frame["tier_key"] = frame["tier"].fillna("")
    roots: Dict[Tuple[str, Optional[str]], Dict[str, float]] = {}
    for (kind, tier_key), group in frame.groupby(["kind", "tier_key"]):
        roots[(str(kind), tier_key or None)] = _raw_quantiles(group["lr"].to_numpy())

    def _key(level: str, area: str, kind: str, tier_key: str) -> str:
        return f"{level}:{area}|{kind}|{tier_key}" if tier_key else f"{level}:{area}|{kind}"

    blocks: Dict[str, Dict[str, object]] = {}
    shrunk: Dict[str, Dict[str, float]] = {}
    for (name, kind, tier_key), group in frame.dropna(subset=["borough"]).groupby(["borough", "kind", "tier_key"]):
        if len(group) < MIN_AREA_STRATUM_SALES:
            continue
        key = _key("borough", name, kind, tier_key)
        root = roots[(str(kind), tier_key or None)]
        shrunk[key] = _shrink(_raw_quantiles(group["lr"].to_numpy()), len(group), root, prior)
        blocks[key] = {"n": int(len(group)), "prior_sales": prior, "parent": _tier_root_name(kind, tier_key or None)}
    home_borough = (
        frame.dropna(subset=["district", "borough"])
        .groupby(["district", "borough"]).size().reset_index(name="n")
        .sort_values(["district", "n", "borough"], ascending=[True, False, True])
        .drop_duplicates("district").set_index("district")["borough"].to_dict()
    )
    for (name, kind, tier_key), group in frame.dropna(subset=["district"]).groupby(["district", "kind", "tier_key"]):
        if len(group) < MIN_AREA_STRATUM_SALES:
            continue
        parent_key = _key("borough", str(home_borough.get(name)), kind, tier_key)
        parent = shrunk.get(parent_key, roots[(str(kind), tier_key or None)])
        key = _key("district", name, kind, tier_key)
        shrunk[key] = _shrink(_raw_quantiles(group["lr"].to_numpy()), len(group), parent, prior)
        blocks[key] = {
            "n": int(len(group)),
            "prior_sales": prior,
            "parent": parent_key if parent_key in shrunk else _tier_root_name(kind, tier_key or None),
        }
    for key, block in blocks.items():
        block["log_ratio_quantiles"] = {name: round(value, 4) for name, value in shrunk[key].items()}
    return blocks, roots


def _pinball(log_ratio: np.ndarray, quantiles: Sequence[Mapping[str, float]]) -> float:
    """Return the mean quantile (pinball) loss of per-sale quantile predictions over `QUANTILE_LEVELS`."""
    total = 0.0
    for name, q in QUANTILE_LEVELS:
        predicted = np.array([block[name] for block in quantiles], dtype=np.float64)
        diff = log_ratio - predicted
        total += float(np.mean(np.maximum(q * diff, (q - 1.0) * diff)))
    return total / len(QUANTILE_LEVELS)


def _area_calibration(
    log_ratio: np.ndarray,
    propertytype: np.ndarray,
    district: np.ndarray,
    borough: np.ndarray,
    random_state: int = 42,
    predicted: Optional[np.ndarray] = None,
) -> Tuple[Dict[str, Dict[str, object]], float, Dict[str, float]]:
    """
    Return the area strata, the prior strength chosen for them, and every prior's cross-fitted loss.

    With `predicted` the strata are keyed by each sale's value tier as well
    (`VALUE_TIERS`, of the predicted price) and shrink toward the tier's band
    block; without it they are the untiered district and borough blocks.

    Sales are split in two; blocks built on one half predict the quantiles of
    the other (a sale with no area block takes its root's: its type and
    tier's), and the prior with the lower pinball loss over both directions is
    used on all the sales.
    """
    kinds = np.asarray(propertytype).astype("str")
    district = np.asarray(district, dtype=object)
    borough = np.asarray(borough, dtype=object)
    tiers = (
        None if predicted is None
        else np.array([value_tier_of(float(v)) for v in np.asarray(predicted, dtype=np.float64)], dtype=object)
    )
    fold = np.random.default_rng(random_state).random(log_ratio.size) < 0.5
    scores: Dict[str, float] = {}
    for prior in AREA_PRIOR_GRID:
        loss, count = 0.0, 0
        for train in (fold, ~fold):
            test = ~train
            if train.sum() == 0 or test.sum() == 0:
                continue
            blocks, roots = _area_blocks(
                log_ratio[train], kinds[train], district[train], borough[train], prior,
                None if tiers is None else tiers[train],
            )
            test_tiers = [None] * int(test.sum()) if tiers is None else tiers[test]
            predictions = []
            for kind, d, b, tier in zip(kinds[test], district[test], borough[test], test_tiers):
                chosen = None
                for key in area_keys(kind, d, b, tier) if tier is not None else area_keys(kind, d, b):
                    if key in blocks:
                        chosen = blocks[key]["log_ratio_quantiles"]
                        break
                predictions.append(
                    chosen or roots.get((kind, tier)) or roots.get((kind, None)) or _raw_quantiles(log_ratio[train])
                )
            loss += _pinball(log_ratio[test], predictions) * int(test.sum())
            count += int(test.sum())
        scores[str(prior)] = round(loss / count, 6) if count else float("inf")
    best = min(AREA_PRIOR_GRID, key=lambda prior: scores[str(prior)]) if scores else DEFAULT_AREA_PRIOR_SALES
    blocks, _ = _area_blocks(log_ratio, kinds, district, borough, best, tiers)
    return blocks, float(best), scores


def _quantile_block(log_ratio: np.ndarray) -> Dict[str, object]:
    """Return the accuracy and log-ratio quantiles one stratum's errors imply."""
    return {
        "n": int(log_ratio.size),
        "log_ratio_quantiles": {
            name: round(float(np.quantile(log_ratio, q)), 4)
            for name, q in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90))
        },
    }


def _stratified_calibration(
    predicted: np.ndarray,
    log_ratio: np.ndarray,
    propertytype: np.ndarray,
    street_sales: np.ndarray,
) -> Dict[str, object]:
    """
    Return the per-stratum quantile blocks `stratum_keys` looks up.

    Every key a price could ask for is written when it has at least
    `MIN_STRATUM_SALES` evaluation sales; the rest are left out so the lookup
    falls back to a stratum that was actually measured.
    """
    keys = [
        stratum_keys(float(value), str(kind), count)
        for value, kind, count in zip(predicted, propertytype, street_sales)
    ]
    blocks: Dict[str, object] = {}
    for depth in range(4):
        labels = np.array([k[depth] for k in keys])
        for label in np.unique(labels):
            if label in blocks:
                continue
            rows = log_ratio[labels == label]
            if rows.size >= MIN_STRATUM_SALES:
                blocks[str(label)] = _quantile_block(rows)
    return blocks


def _calibration_for(calibration: Optional[Mapping], epc_source: str = EPC_SOURCE_CERTIFICATE) -> Optional[Mapping]:
    """
    Return the calibration block that matches how a home was priced.

    ``calibration["estimated"]`` (written by ``evaluate --mask-epc``) for an
    estimated certificate; the top-level block otherwise, and as the fallback
    for a model that has not been measured on estimated homes.
    """
    if not calibration:
        return None
    if epc_source == EPC_SOURCE_ESTIMATED and calibration.get("estimated"):
        return calibration["estimated"]
    return calibration


def _quantiles_for(
    calibration: Optional[Mapping],
    value: float,
    propertytype: str,
    street_sales,
    district: Optional[str] = None,
    borough: Optional[str] = None,
) -> Tuple[Optional[Mapping], Optional[str]]:
    """
    Return the narrowest measured quantile block for a price, and which stratum it came from.

    The home's district and borough blocks for its type and value tier first
    (`area_keys`: ``district:<d>|<type>|<tier>``, ``borough:<b>|<type>|<tier>``,
    then the untiered ones an older calibration holds), then `stratum_keys`,
    then the calibration's own global block, so a model evaluated before area
    or band strata existed still prices as it did.
    """
    if not calibration:
        return None, None
    strata = calibration.get("strata") or {}
    keys = area_keys(propertytype, district, borough, value_tier_of(value))
    for key in keys + stratum_keys(value, propertytype, street_sales):
        block = strata.get(key)
        if block and block.get("log_ratio_quantiles"):
            return block["log_ratio_quantiles"], key
    quantiles = calibration.get("log_ratio_quantiles")
    return (quantiles, "global") if quantiles else (None, None)


# p90 of a standard normal: converts a measured 10-90 spread to a sigma and back.
_Z90 = 1.2815515655446004


def _range_from_calibration(
    value: float,
    calibration: Optional[Mapping],
    epc_source: str = EPC_SOURCE_CERTIFICATE,
    propertytype: str = "F",
    street_sales=None,
    index_sigma: float = 0.0,
    district: Optional[str] = None,
    borough: Optional[str] = None,
    recent_sale: Optional[str] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """
    Return ``(low, high, centre, stratum)`` from the model's measured log errors.

    A price anchored on the home's own recent sale (`recent_sale` is its
    `recent_sale_bucket`) takes its quantiles from ``calibration["recent_sale"]``
    (stratum ``recent_sale:<bucket>``) -- what anchored prices were measured to
    miss by -- before any area or band stratum, and is recentred by that block's
    median alone: an area's bias is the model's, and the anchor is not the
    model. Without such a block it falls back to the strata below.

    Three things happen here.

    First the quantiles come from the narrowest stratum that was measured for
    this price -- its district and type, its borough and type, or else its
    band, type and evidence thickness -- rather than one global block, so two
    different homes need not get the same band, and a home in an area the
    model misreads is recentred by that area's own measured bias. The
    stratum is looked up on `value`, the model's raw output, because that is
    what `_stratified_calibration` binned the evaluation sales by; using the
    corrected centre here would read a band the errors were not measured over.

    Second the point estimate is recentred. ``log_ratio`` is ``log(actual /
    predicted)``, so a non-zero p50 is a standing bias; the served model's is
    -0.0548, meaning it reads about 5.5 % high at the median. The centre is
    corrected by that median and the quantiles re-expressed around it, so the
    marker sits mid-range unless the stratum's error really is skewed.

    Third the band widens with the forecast horizon. `index_sigma` is the
    cross-validated error of the price index projection at this horizon --
    the home's own district or borough series where that was projected
    (`local_index.LocalIndex.sigma`), London's otherwise; it is added to the model's own
    spread in quadrature, because a price years out inherits the projection's
    uncertainty on top of the model's. Both terms are measured, neither assumed.
    """
    full = calibration
    calibration = _calibration_for(calibration, epc_source)
    quantiles, stratum = None, None
    if recent_sale is not None:
        blocks = (calibration or {}).get("recent_sale") or (full or {}).get("recent_sale") or {}
        block = blocks.get(recent_sale) or {}
        if block.get("log_ratio_quantiles"):
            quantiles, stratum = block["log_ratio_quantiles"], f"recent_sale:{recent_sale}"
    if quantiles is None:
        quantiles, stratum = _quantiles_for(calibration, value, propertytype, street_sales, district, borough)
    if not quantiles or "p10" not in quantiles or "p90" not in quantiles:
        return None, None, None, None
    p10, p50, p90 = float(quantiles["p10"]), float(quantiles.get("p50", 0.0)), float(quantiles["p90"])
    centre = value * float(np.exp(p50))
    low_offset, high_offset = p10 - p50, p90 - p50
    if index_sigma > 0:
        model_sigma = max((high_offset - low_offset) / (2 * _Z90), 1e-9)
        widen = float(np.sqrt(model_sigma**2 + index_sigma**2)) / model_sigma
        low_offset, high_offset = low_offset * widen, high_offset * widen
    return (
        centre * float(np.exp(low_offset)),
        centre * float(np.exp(high_offset)),
        centre,
        stratum,
    )


# The home's own previous sale, moved to the sale date along its area's
# index, is blended with the model in log space, ``w * repeat + (1 - w) *
# model``, with a weight per bucket of months since that sale. The weights are
# fitted by `evaluate` on out-of-sample sales and switched on only where the
# cross-fitted blend beat the model alone on repeat-sold homes.
REPEAT_SALE_BUCKET_EDGES_MONTHS: Tuple[float, ...] = (6.0, 12.0, 24.0, 60.0, 120.0)
REPEAT_SALE_WEIGHT_GRID: Tuple[float, ...] = tuple(round(0.05 * i, 2) for i in range(21))
MIN_REPEAT_SALE_BUCKET = 50
# The cross-fitted blend must cut median absolute percentage error on
# repeat-sold homes by at least this share before it is served: a smaller gain
# is within what the choice of weights alone can find in noise.
MIN_REPEAT_SALE_IMPROVEMENT = 0.02

# A home's own recent arm's-length sale is the primary evidence of its value
# (RICS practice), so a sale in the last year sets a floor under the weight it
# gets: 0.9 up to 6 months, 0.8 up to 12, then falling linearly to the fitted
# weight at `RECENT_SALE_TAPER_END_MONTHS`. The fitted weights alone cannot say
# this: the resales within two years in any holdout are mostly refurbish-and-
# resell flips, which beat their indexed purchase price by around 10 %, so
# cross-fitting on them gave a sale four months old a weight of 0.15. The floor
# applies only to a sale `_recent_sale_eligible` accepts -- a home renovated
# since keeps the measured weights, and the model sees its new certificate.
RECENT_SALE_WEIGHT_FLOOR: Tuple[Tuple[float, float], ...] = ((6.0, 0.9), (12.0, 0.8))
RECENT_SALE_TAPER_END_MONTHS = 24.0

# A certificate inspected after the previous sale marks the home renovated when
# its floor area differs from the one in force at that sale by more than this
# share, or its rating is this many bands better.
RENOVATION_FLOOR_AREA_CHANGE = 0.10
RENOVATION_RATING_BANDS = 2
_ENERGY_RATINGS = "ABCDEFG"


def _months_between(earlier, later) -> float:
    """Return the months from `earlier` to `later`, as the feature SQL counts them."""
    return (pd.Timestamp(later) - pd.Timestamp(earlier)).days / DAYS_PER_MONTH


def _repeat_sale_bucket(months: float, edges: Sequence[float] = REPEAT_SALE_BUCKET_EDGES_MONTHS) -> int:
    """Return the bucket a gap between sales falls in."""
    return int(np.searchsorted(np.asarray(edges, dtype=np.float64), months, side="right"))


def _fitted_repeat_sale_weight(calibration: Optional[Mapping], months: float) -> float:
    """Return the weight `evaluate` fitted for a previous sale `months` old, 0 when the blend is off."""
    block = (calibration or {}).get("repeat_sale_blend") or {}
    if not block.get("enabled") or not np.isfinite(months) or months < 0:
        return 0.0
    weights = block.get("weights") or []
    bucket = _repeat_sale_bucket(months, block.get("bucket_edges_months") or REPEAT_SALE_BUCKET_EDGES_MONTHS)
    return float(weights[bucket]) if bucket < len(weights) else 0.0


def recent_sale_floor(months: float, fitted_at_taper_end: float = 0.0) -> float:
    """
    Return the least weight an eligible previous sale `months` old gets (`RECENT_SALE_WEIGHT_FLOOR`).

    `fitted_at_taper_end` is the fitted weight at `RECENT_SALE_TAPER_END_MONTHS`,
    where the floor meets it; beyond that there is no floor.
    """
    if not np.isfinite(months) or months < 0 or months >= RECENT_SALE_TAPER_END_MONTHS:
        return 0.0
    for edge, floor in RECENT_SALE_WEIGHT_FLOOR:
        if months <= edge:
            return floor
    last_edge, last_floor = RECENT_SALE_WEIGHT_FLOOR[-1]
    share = (months - last_edge) / (RECENT_SALE_TAPER_END_MONTHS - last_edge)
    return last_floor + (fitted_at_taper_end - last_floor) * share


def repeat_sale_weight(calibration: Optional[Mapping], months: float, recent_eligible: bool = False) -> float:
    """
    Return the weight the home's own indexed previous sale gets.

    The weight `evaluate` fitted for its age (0 when the blend is off), raised
    to `recent_sale_floor` when the sale is `recent_eligible` -- whether or not
    the fitted blend is on.
    """
    fitted = _fitted_repeat_sale_weight(calibration, months)
    if not recent_eligible:
        return fitted
    taper_end = _fitted_repeat_sale_weight(calibration, RECENT_SALE_TAPER_END_MONTHS)
    return max(fitted, recent_sale_floor(months, taper_end))


# The recent-sale range blocks `evaluate` writes, by months since the sale.
RECENT_SALE_BUCKETS: Tuple[Tuple[str, float, float], ...] = (("≤12", 0.0, 12.0), ("12–24", 12.0, 24.0))
RECENT_SALE_POOLED = "≤24"


def recent_sale_bucket(months: float) -> Optional[str]:
    """Return the `RECENT_SALE_BUCKETS` label for a previous sale `months` old, None past two years."""
    if not np.isfinite(months) or months < 0 or months >= RECENT_SALE_TAPER_END_MONTHS:
        return None
    return RECENT_SALE_BUCKETS[0][0] if months <= RECENT_SALE_BUCKETS[0][2] else RECENT_SALE_BUCKETS[1][0]


def _renovated_since(epcs: pd.DataFrame, prior_date, day=None) -> bool:
    """
    Return whether a certificate after `prior_date` (and by `day`) shows the home renovated.

    Compared with the certificate in force at the previous sale: a floor area
    more than `RENOVATION_FLOOR_AREA_CHANGE` different, or a rating
    `RENOVATION_RATING_BANDS` or more bands better. With no certificate at the
    previous sale there is nothing to compare, and no signal.
    """
    if epcs is None or epcs.empty:
        return False
    before = _epc_in_force(epcs, prior_date)
    if before is None:
        return False
    inspected = pd.to_datetime(epcs["inspection_date"])
    later = inspected > pd.Timestamp(prior_date)
    if day is not None:
        later &= inspected <= pd.Timestamp(day)
    area0 = pd.to_numeric(pd.Series([before.get("total_floor_area")]), errors="coerce").iloc[0]
    rating0 = before.get("current_energy_rating")
    rank0 = _ENERGY_RATINGS.find(rating0) if isinstance(rating0, str) and rating0 else -1
    for _, certificate in epcs[later.to_numpy()].iterrows():
        area = pd.to_numeric(pd.Series([certificate.get("total_floor_area")]), errors="coerce").iloc[0]
        if pd.notna(area) and pd.notna(area0) and area0 > 0 and abs(area / area0 - 1.0) > RENOVATION_FLOOR_AREA_CHANGE:
            return True
        rating = certificate.get("current_energy_rating")
        rank = _ENERGY_RATINGS.find(rating) if isinstance(rating, str) and rating else -1
        if rank0 >= 0 and rank >= 0 and rank0 - rank >= RENOVATION_RATING_BANDS:
            return True
    return False


def _recent_sale_eligible(record: "PropertyRecord", prior_date, prior_price, day=None) -> bool:
    """
    Return whether the home's previous sale may anchor its price (`RECENT_SALE_WEIGHT_FLOOR`).

    The price must be plausible (``MIN_PLAUSIBLE_PRICE``) and nothing since may
    say the home was renovated (`_renovated_since`, certificates to `day`).
    """
    if prior_date is None or prior_price is None or not np.isfinite(prior_price):
        return False
    if prior_price < MIN_PLAUSIBLE_PRICE:
        return False
    return not _renovated_since(record.epcs, prior_date, day)


def _blend(model_value, repeat_value, weight):
    """Return the log-space blend of the model's price and the indexed previous sale."""
    return np.exp(weight * np.log(repeat_value) + (1.0 - weight) * np.log(model_value))


def _fit_repeat_sale_weights(
    actual: np.ndarray, model_value: np.ndarray, repeat_value: np.ndarray, months: np.ndarray
) -> Tuple[List[float], List[int]]:
    """Return the weight per bucket that minimises the median absolute log error, and each bucket's size."""
    buckets = np.array([_repeat_sale_bucket(m) for m in months], dtype=np.int64)
    weights, sizes = [], []
    for bucket in range(len(REPEAT_SALE_BUCKET_EDGES_MONTHS) + 1):
        mask = buckets == bucket
        sizes.append(int(mask.sum()))
        if mask.sum() < MIN_REPEAT_SALE_BUCKET:
            weights.append(0.0)
            continue
        target = np.log(actual[mask])
        errors = [
            float(np.median(np.abs(target - np.log(_blend(model_value[mask], repeat_value[mask], w)))))
            for w in REPEAT_SALE_WEIGHT_GRID
        ]
        weights.append(float(REPEAT_SALE_WEIGHT_GRID[int(np.argmin(errors))]))
    return weights, sizes


def _served_repeat_weights(
    weights: Sequence[float], months: np.ndarray, recent_eligible: np.ndarray
) -> np.ndarray:
    """Return each row's served weight from fitted bucket `weights`, raised to the recent-sale floor where eligible."""
    served = np.array([weights[_repeat_sale_bucket(m)] for m in months], dtype=np.float64)
    taper_end = float(weights[_repeat_sale_bucket(RECENT_SALE_TAPER_END_MONTHS)])
    floors = np.array(
        [recent_sale_floor(m, taper_end) if ok else 0.0 for m, ok in zip(months, recent_eligible)],
        dtype=np.float64,
    )
    return np.maximum(served, floors)


def _repeat_sale_blend(
    actual: np.ndarray,
    predicted: np.ndarray,
    repeat_value: np.ndarray,
    months: np.ndarray,
    random_state: int = 42,
    renovated: Optional[np.ndarray] = None,
    recent_eligible: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, Dict[str, object]]:
    """
    Fit the repeat-sale blend and return the predictions the calibration should measure.

    Weights are fitted only on homes not `renovated` since their previous sale,
    so they measure like-for-like resales; they are cross-fitted over two
    halves of those homes, so the comparison with the model alone is out of
    sample. The fitted blend is enabled only when it lowers median absolute
    percentage error on repeat-sold homes by `MIN_REPEAT_SALE_IMPROVEMENT` or
    more. The returned predictions are what is served: the cross-fitted blend
    where enabled, with each `recent_eligible` home's weight raised to
    `recent_sale_floor` whether or not it is (and the model's own price for
    every other home), so the range is measured on what is served.
    """
    n = actual.size
    renovated = np.zeros(n, dtype=bool) if renovated is None else np.asarray(renovated, dtype=bool)
    recent_eligible = np.zeros(n, dtype=bool) if recent_eligible is None else np.asarray(recent_eligible, dtype=bool)
    has = np.isfinite(repeat_value) & (repeat_value > 0) & np.isfinite(months)
    fittable = has & ~renovated
    zero = [0.0] * (len(REPEAT_SALE_BUCKET_EDGES_MONTHS) + 1)
    block: Dict[str, object] = {
        "enabled": False,
        "bucket_edges_months": list(REPEAT_SALE_BUCKET_EDGES_MONTHS),
        "weights": zero,
        "n_repeat_sales": int(has.sum()),
        "n_renovated_excluded": int((has & renovated).sum()),
        "recent_sale_floor": [list(pair) for pair in RECENT_SALE_WEIGHT_FLOOR],
        "recent_sale_taper_end_months": RECENT_SALE_TAPER_END_MONTHS,
    }
    floored = has & recent_eligible

    def _served(weights: Sequence[float], rows: np.ndarray) -> np.ndarray:
        out = predicted.copy()
        idx = np.flatnonzero(rows)
        w = _served_repeat_weights(weights, months[idx], floored[idx])
        idx, w = idx[w > 0], w[w > 0]
        out[idx] = _blend(predicted[idx], repeat_value[idx], w)
        return out

    if fittable.sum() < 2 * MIN_REPEAT_SALE_BUCKET:
        return _served(zero, floored), block
    idx = np.flatnonzero(fittable)
    fold = np.random.default_rng(random_state).random(idx.size) < 0.5
    # Renovated homes are scored by the weights fitted on the half they were
    # not in the fold of, like everyone else; they are just never fitted on.
    other = np.flatnonzero(has & renovated)
    other_fold = np.random.default_rng(random_state + 1).random(other.size) < 0.5
    crossfit = predicted.copy()
    fold_weights = []
    for train, test in (
        (idx[fold], np.concatenate([idx[~fold], other[~other_fold]])),
        (idx[~fold], np.concatenate([idx[fold], other[other_fold]])),
    ):
        weights, _ = _fit_repeat_sale_weights(actual[train], predicted[train], repeat_value[train], months[train])
        fold_weights.append((weights, test))
        w = np.array([weights[_repeat_sale_bucket(m)] for m in months[test]], dtype=np.float64)
        crossfit[test] = _blend(predicted[test], repeat_value[test], w)
    mdape_model = float(np.median(np.abs(predicted[has] - actual[has]) / actual[has]))
    mdape_blend = float(np.median(np.abs(crossfit[has] - actual[has]) / actual[has]))
    weights, sizes = _fit_repeat_sale_weights(
        actual[fittable], predicted[fittable], repeat_value[fittable], months[fittable]
    )
    enabled = mdape_blend < mdape_model * (1.0 - MIN_REPEAT_SALE_IMPROVEMENT) and any(w > 0 for w in weights)
    block.update(
        {
            "enabled": bool(enabled),
            "weights": weights if enabled else zero,
            "fitted_weights": weights,
            "n": sizes,
            "mdape_model": round(mdape_model, 4),
            "mdape_blend_crossfit": round(mdape_blend, 4),
        }
    )
    # Every repeat-sold home is in one fold's test half.
    served = predicted.copy()
    for fitted, test in fold_weights:
        rows = np.zeros(n, dtype=bool)
        rows[test] = True
        served[test] = _served(fitted if enabled else zero, rows)[test]
    return served, block


def _recent_sale_calibration(
    log_ratio: np.ndarray, months: np.ndarray, anchored: np.ndarray
) -> Dict[str, Dict[str, object]]:
    """
    Return the ``calibration["recent_sale"]`` blocks: the served errors of prices anchored on a recent sale.

    One block per `RECENT_SALE_BUCKETS` bucket of the `anchored` sales (those
    whose weight the recent-sale floor raised), plus the pooled ``≤24`` block.
    A bucket with fewer than `MIN_REPEAT_SALE_BUCKET` sales is shrunk toward the
    pooled block with that many pseudo-sales; with fewer than that pooled,
    nothing is written and anchored prices take the ordinary strata.
    """
    months = np.asarray(months, dtype=np.float64)
    buckets = np.array([recent_sale_bucket(m) if ok else None for m, ok in zip(months, anchored)], dtype=object)
    pooled_mask = buckets != None  # noqa: E711 -- elementwise on an object array
    if pooled_mask.sum() < MIN_REPEAT_SALE_BUCKET:
        return {}
    pooled = _raw_quantiles(log_ratio[pooled_mask])
    out: Dict[str, Dict[str, object]] = {
        RECENT_SALE_POOLED: {
            "n": int(pooled_mask.sum()),
            "log_ratio_quantiles": {name: round(value, 4) for name, value in pooled.items()},
        }
    }
    for label, _, _ in RECENT_SALE_BUCKETS:
        mask = buckets == label
        n = int(mask.sum())
        raw = _raw_quantiles(log_ratio[mask]) if n else pooled
        quantiles = raw if n >= MIN_REPEAT_SALE_BUCKET else _shrink(raw, n, pooled, MIN_REPEAT_SALE_BUCKET)
        out[label] = {
            "n": n,
            "shrunk_toward": None if n >= MIN_REPEAT_SALE_BUCKET else RECENT_SALE_POOLED,
            "log_ratio_quantiles": {name: round(value, 4) for name, value in quantiles.items()},
        }
    return out


def estimate_strategy(calibration: Optional[Mapping]) -> str:
    """Return how a model scores homes without a certificate, as `evaluate --mask-epc` chose."""
    return ((calibration or {}).get("estimated") or {}).get("strategy", ESTIMATE_STRATEGY_SURROGATE)


def price_record(
    record: PropertyRecord,
    sale_date: date,
    model: SavedModel,
    store: EvidenceStore,
    connection=None,
) -> PricePrediction:
    """
    Price one resolved property for a sale on `sale_date` (not validated here).

    `connection` reads the comparable certificates a home without its own
    certificate is estimated from.
    """
    built = build_sale_row(
        record,
        # As a date, so the row's timestamps keep the unit a sale date gives them.
        store.evidence_date(sale_date).date(),
        store,
        population=model.population,
        connection=connection,
        epc_strategy=estimate_strategy(model.calibration),
    )
    return _predict_built(record, built, sale_date, model, store)


def _predict_built(
    record: PropertyRecord,
    built: "SaleRow",
    sale_date: date,
    model: SavedModel,
    store: EvidenceStore,
) -> PricePrediction:
    """
    Score one row built as of the evidence date, move it to `sale_date` and wrap it.

    The model is asked only what it was trained and measured on: the home's
    price on the evidence date, with every feature in its training range. That
    price is then moved to `sale_date` along the home's own area and type index
    (`EvidenceStore.time_factor`), which is what makes the price differ from
    one day to the next and between a flat and a detached house. Asking the
    model directly for a future date could not do that: its strongest
    features divide evidence-date comparables by the sale-date index, and
    cancelled any index growth.

    A home with a previous sale then has that sale, moved to `sale_date` along
    the same index, blended in (`serve_price`). The range and the bias
    correction come last.
    """
    frame = engineer_rows([built.row], model.float_dtype)
    nowcast = float(model.predict(frame, store)[0])
    row = built.row
    propertytype = str(row["propertytype"])
    district = row.get("postcode_district") if isinstance(row.get("postcode_district"), str) else None
    evidence = store.evidence_date(sale_date)
    factor = store.time_factor(evidence, sale_date, district, propertytype)
    served = serve_price(
        nowcast * factor, row, built.last_sale, built.recent_sale_eligible, sale_date,
        model.calibration, built.epc.source, store,
    )
    value, low, high, stratum, weight = served.value, served.low, served.high, served.stratum, served.weight
    uplift = factor - 1.0
    return PricePrediction(
        address=record.address,
        sale_date=pd.Timestamp(sale_date).date(),
        evidence_date=evidence.date(),
        evidence_to=store.latest_sale.date(),
        value=value,
        low=low,
        high=high,
        propertytype=propertytype,
        is_maisonette=row["is_maisonette"],
        floor_area_sqm=None if pd.isna(row["total_floor_area"]) else float(row["total_floor_area"]),
        energy_rating=row["current_energy_rating"] if isinstance(row["current_energy_rating"], str) else None,
        habitable_rooms=None if pd.isna(row["number_habitable_rooms"]) else float(row["number_habitable_rooms"]),
        last_sale_date=built.last_sale[0].date() if built.last_sale else None,
        last_sale_price=built.last_sale[1] if built.last_sale else None,
        basis=built.basis,
        model_kind=model.kind,
        within_10_percent=(_calibration_for(model.calibration, built.epc.source) or {}).get("within_10_percent"),
        projection_uplift=uplift if uplift else None,
        projected_from=None if not uplift else evidence.date(),
        index_area=store.index_area(district, propertytype),
        range_stratum=stratum,
        epc_source=built.epc.source,
        estimate_level=built.epc.level,
        estimate_neighbours=built.epc.n if built.epc.source == EPC_SOURCE_ESTIMATED else None,
        estimated_from=built.epc.description,
        anchored_on_last_sale=served.anchored,
        extra={"nowcast": round(nowcast, 2), "repeat_sale_weight": weight},
    )


# A price is "anchored" on the home's last sale -- and says so -- when that
# sale carries at least this much of the blend.
ANCHORED_MIN_WEIGHT = 0.5


@dataclass
class ServedPrice:
    """The price `serve_price` quotes and how it was reached."""

    value: float
    low: Optional[float]
    high: Optional[float]
    stratum: Optional[str]
    weight: float
    recent_sale: Optional[str]
    anchored: bool


def serve_price(
    value: float,
    row: Mapping[str, object],
    last_sale: Optional[Tuple[pd.Timestamp, float]],
    recent_eligible: bool,
    sale_date,
    calibration: Optional[Mapping],
    epc_source: str,
    store: EvidenceStore,
) -> ServedPrice:
    """
    Turn the model's price on `sale_date` into the price quoted, with its range.

    The home's previous sale, moved to `sale_date` along its local index, is
    blended in with `repeat_sale_weight` -- raised to the recent-sale floor when
    `recent_eligible` (`_recent_sale_eligible`) and the sale is under two years
    old. Such a price takes its range from the ``recent_sale:<bucket>`` block;
    every other one from its area, tier and band strata
    (`_range_from_calibration`), whose median bias recentres the price itself.
    `evaluate` measures its calibration on exactly this.
    """
    propertytype = str(row["propertytype"])
    district = row.get("postcode_district") if isinstance(row.get("postcode_district"), str) else None
    borough = local_index.borough_of(row.get("lsoa_name"))
    weight, bucket = 0.0, None
    if last_sale is not None:
        prior_date, prior_price = last_sale
        months = _months_between(prior_date, sale_date)
        weight = repeat_sale_weight(calibration, months, recent_eligible)
        if recent_eligible:
            bucket = recent_sale_bucket(months)
        if weight > 0:
            repeat = prior_price * store.time_factor(prior_date, sale_date, district, propertytype)
            value = float(_blend(value, repeat, weight))
    low, high, centre, stratum = _range_from_calibration(
        value,
        calibration,
        epc_source,
        propertytype=propertytype,
        street_sales=row.get(EVIDENCE_COLUMN),
        index_sigma=store.index_sigma(sale_date, district, propertytype),
        district=district,
        borough=borough,
        recent_sale=bucket,
    )
    # The bias correction moves the price itself, not only the band around it.
    if centre is not None:
        value = centre
    return ServedPrice(
        value=value,
        low=low,
        high=high,
        stratum=stratum,
        weight=weight,
        recent_sale=bucket,
        anchored=bucket is not None and weight >= ANCHORED_MIN_WEIGHT,
    )


# Price Paid property type for each type the estimate form offers.
PPD_TYPE_BY_ESTIMATE_TYPE = {
    "flat": "F",
    "maisonette": "F",
    "terraced": "T",
    "semi-detached": "S",
    "detached": "D",
}


def price_details(
    record: PropertyRecord,
    sale_date: date,
    propertytype: str,
    floor_area_sqm: float,
    habitable_rooms: Optional[float],
    model: SavedModel,
    store: EvidenceStore,
    connection=None,
) -> PricePrediction:
    """
    Price a home described only by its postcode, type and size.

    The same path `price_record` takes, with the type pinned and the floor area
    and room count replaced by the ones given, so the model prices what was
    typed rather than the surrogate certificate's guess at it.
    """
    built = build_sale_row(
        record,
        # As a date, so the row's timestamps keep the unit a sale date gives them.
        store.evidence_date(sale_date).date(),
        store,
        population=model.population,
        connection=connection,
        epc_strategy=estimate_strategy(model.calibration),
        overrides={"total_floor_area": float(floor_area_sqm), "number_habitable_rooms": habitable_rooms},
        force_propertytype=propertytype,
    )
    return _predict_built(record, built, sale_date, model, store)


def attach_market_indices(
    store: EvidenceStore, engine, model: SavedModel, log: Optional[logging.Logger] = None
) -> EvidenceStore:
    """
    Give `store` the model's UK HPI projection and the local price index, and return it.

    A model without its ``projection.json`` has one fitted here from
    ``production.ukhpi``, with a warning: carrying the index forward silently
    is what froze every future price at the last published month. The local
    index is built from the live repeat sales (`local_index.load`); if that
    fails the store keeps moving prices along the London index, with an error
    logged, rather than refusing to price.
    """
    if model.projection is None:
        (log or logging.getLogger(__name__)).warning(
            "hpp_inference: %s has no %s; fitting the UK HPI projection from the live index. "
            "Write one with: python -m hpldn.backend.scripts.hpp_inference fit-projection --model %s --write",
            model.path,
            hpi_projection.PROJECTION_FILE,
            model.path,
        )
        model.projection = fit_projection(engine, log)
    store.set_projection(model.projection)
    try:
        store.local_index = local_index.load(
            engine, model.path, store.projection, log, params=getattr(model, "local_index_params", None)
        )
    except Exception as exc:  # noqa: BLE001 -- the London index still prices every home
        (log or logging.getLogger(__name__)).error(
            "hpp_inference: local price index unavailable (%s); moving prices along the London index", exc
        )
        store.local_index = None
    return store


class PricingService:
    """
    A model and its evidence, loaded once and shared by concurrent requests.

    `engine` is a synchronous SQLAlchemy engine. The evidence loads on first
    use (or on `warm`), under a lock so concurrent first requests read it once.
    """

    def __init__(self, engine, model: SavedModel, log: Optional[logging.Logger] = None) -> None:
        self.engine = engine
        self.model = model
        self.log = log
        self._store: Optional[EvidenceStore] = None
        self._lock = threading.Lock()

    @property
    def store(self) -> EvidenceStore:
        """Return the evidence, loading it the first time."""
        if self._store is None:
            with self._lock:
                if self._store is None:
                    store = EvidenceStore.load(self.engine, self.log)
                    self._store = attach_market_indices(store, self.engine, self.model, self.log)
        return self._store

    def warm(self) -> None:
        """Load the evidence now rather than on the first request."""
        _ = self.store

    @property
    def local_index(self) -> Optional["local_index.LocalIndex"]:
        """Return the local price index if the evidence has loaded, without loading it."""
        store = self._store
        return None if store is None else store.local_index

    def price(self, address: PropertyAddress, sale_date: date, today: Optional[date] = None) -> PricePrediction:
        """
        Price `address` for a sale on `sale_date`.

        Raises
        ------
        InvalidSaleDate, PropertyNotFound, NotEnoughData
            With a sentence safe to show a landlord.
        """
        validate_sale_date(sale_date, today)
        store = self.store
        with self.engine.connect() as connection:
            record = load_property(connection, address)
            return price_record(record, sale_date, self.model, store, connection)

    def price_details(
        self,
        postcode: str,
        property_type: str,
        floor_area_sqm: float,
        habitable_rooms: Optional[float],
        sale_date: date,
        today: Optional[date] = None,
    ) -> PricePrediction:
        """
        Price a home from its postcode, type and size, with no address.

        What ``POST /estimate`` asks for. `property_type` is the estimate
        form's word for it (`PPD_TYPE_BY_ESTIMATE_TYPE`).

        Raises
        ------
        InvalidSaleDate, NotEnoughData
            As `price` does.
        """
        validate_sale_date(sale_date, today)
        letter = PPD_TYPE_BY_ESTIMATE_TYPE.get(property_type)
        if letter is None:
            raise NotEnoughData(f"We cannot price a {property_type}.")
        store = self.store
        with self.engine.connect() as connection:
            record = load_postcode_record(connection, postcode)
            return price_details(
                record, sale_date, letter, floor_area_sqm, habitable_rooms, self.model, store, connection
            )


# --------------------------------------------------------------------------
# Evaluation: parity with the view, accuracy and calibration
# --------------------------------------------------------------------------

PARITY_COLUMNS = (
    COMPS_COLUMNS
    + NEIGHBOURHOOD_COLUMNS
    + MSOA_TREND_COLUMNS
    + ["prev_sale_price", "years_since_prev_sale", "boe_rate_at_sale", "ukhpi_avg_price_at_sale",
       "total_floor_area", "number_habitable_rooms"]
    # The Ofsted columns computed in numpy against 0034's SQL (the basis is categorical).
    + list(ofsted.NUMERIC_COLUMNS)
)
# Computed in Python on both sides rather than stored on the view, so they are
# checked against `hpp_rf`'s training-time code (``evaluate --rolling-parity``).
LOCAL_PARITY_COLUMNS = hpp_rf.SIZEBAND_COMP_COLUMNS + hpp_rf.LOCAL_REPEAT_SALE_COLUMNS

_EVALUATION_SALES_SQL = text(
    """
    SELECT lh.*
    FROM production.london_houses lh
    WHERE lh.dateoftransfer BETWEEN :date_from AND :date_to
      AND (:population = 'all' OR lh.epc_matched)
    ORDER BY lh.dateoftransfer, lh.ppd_id
    """
)


def _training_causal_stats(
    engine,
    population: str,
    date_from: date,
    date_to: date,
    log: Optional[logging.Logger] = None,
    local_index_=None,
) -> pd.DataFrame:
    """
    Return the rolling, same-street and local-index statistics a training run computes, per ``ppd_id``.

    Runs `hpp_rf`'s own pandas implementations over the modelled population
    from the longest window before `date_from` to `date_to`, which gives every
    sale in the range the same statistics as the full training frame. The
    size-matched comparables and the locally indexed previous sale
    (`LOCAL_PARITY_COLUMNS`) are moved along `local_index_` -- the index the
    evidence was priced with, so only the code is compared, not two indices.
    """
    since = _months_before(date_from, max(ROLLING_COMP_WINDOWS_MONTHS) + 1).date()
    sql = text(
        """
        SELECT ppd_id, dateoftransfer, price, propertytype, total_floor_area,
               postcode_district, postcode_sector, poststreet, latitude, longitude,
               prev_sale_price, prev_sale_date, comp_median_price_per_sqm
        FROM production.london_houses
        WHERE dateoftransfer BETWEEN :since AND :date_to
          AND (:population = 'all' OR epc_matched)
        ORDER BY dateoftransfer, ppd_id
        """
    )
    with engine.connect() as connection:
        frame = pd.read_sql(sql, connection, params={"since": since, "date_to": date_to, "population": population})
    frame[DATE_COLUMN] = pd.to_datetime(frame[DATE_COLUMN])
    for col in ("total_floor_area", "latitude", "longitude", "prev_sale_price", "comp_median_price_per_sqm"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if log is not None:
        log.info(f"evaluate: computing training-time rolling statistics over {len(frame):,} sales")
    frame = hpp_rf.add_same_street_features(frame)
    frame, _ = hpp_rf.add_rolling_area_comp_features(frame)
    frame = hpp_rf.add_sizeband_comp_features(frame, local_index_, log)
    frame = hpp_rf.add_market_relative_features(frame, local_index=local_index_)
    return frame[["ppd_id"] + list(_CAUSAL_COLUMNS) + list(LOCAL_PARITY_COLUMNS)]


def evaluate(
    engine,
    model: SavedModel,
    date_from: date,
    date_to: date,
    sample: int,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
    store: Optional[EvidenceStore] = None,
    rolling_parity: bool = False,
) -> Dict[str, object]:
    """
    Price real sales with the inference path and compare with the view and the price paid.

    Each sampled sale is rebuilt from its address and date alone, with the
    sale date as the evidence date, so the comparison with its stored
    ``production.london_houses`` row checks every recomputed feature, and its
    price checks the model end to end. Sales inside the model's training
    period are in-sample; choose `date_from` after it.

    Returns
    -------
    dict
        ``parity`` (per column: rows compared, share within 1 % or 0.01, and
        the largest difference), ``accuracy`` (median absolute percentage
        error, shares within 10 % and 20 %), ``log_ratio_quantiles`` of
        ``log(price / predicted)`` and the rows that could not be built.
    """
    store = store or attach_market_indices(
        EvidenceStore.load(engine, log, earliest_anchor=date_from), engine, model, log
    )
    with engine.connect() as connection:
        sales = pd.read_sql(
            _EVALUATION_SALES_SQL,
            connection,
            params={"date_from": date_from, "date_to": date_to, "population": model.population},
        )
    if sales.empty:
        raise ValueError(f"evaluate: no sales between {date_from} and {date_to}")
    if sample and len(sales) > sample:
        sales = sales.sample(n=sample, random_state=random_state).sort_values([DATE_COLUMN, "ppd_id"])
    sales = sales.reset_index(drop=True)

    rows, kept, skipped = [], [], {}
    with engine.connect() as connection:
        with profiling.stage("evaluate/load_properties", sales=len(sales)):
            records = load_properties_by_ids(connection, [int(i) for i in sales["address_id"]])
        for position, sale in sales.iterrows():
            try:
                record = records.get(int(sale["address_id"]))
                if record is None:
                    record = load_property_by_ids(connection, [int(sale["address_id"])])
                built = build_sale_row(
                    record,
                    sale[DATE_COLUMN],
                    store,
                    population=model.population,
                    anchor=sale[DATE_COLUMN],
                    connection=connection,
                    epc_strategy=estimate_strategy(model.calibration),
                )
            except PricingError as exc:
                skipped[type(exc).__name__] = skipped.get(type(exc).__name__, 0) + 1
                continue
            rows.append(built.row)
            kept.append(position)
            if log is not None and len(rows) % 500 == 0:
                log.info(f"evaluate: built {len(rows)} of {len(sales)} sales")
    truth = sales.iloc[kept].reset_index(drop=True)
    built_frame = pd.DataFrame(rows)

    expected_frame = truth
    parity_columns = list(PARITY_COLUMNS)
    got_frame = built_frame
    if rolling_parity:
        expected_frame = truth.drop(columns=[c for c in LOCAL_PARITY_COLUMNS if c in truth.columns]).merge(
            _training_causal_stats(engine, model.population, date_from, date_to, log, store.local_index),
            how="left",
            on="ppd_id",
        )
        parity_columns += list(_CAUSAL_COLUMNS) + list(LOCAL_PARITY_COLUMNS)
        # The ratio is derived from the row as a saved model's preprocessing derives it.
        got_frame = hpp_rf.add_market_relative_features(built_frame)
    parity = {}
    for col in parity_columns:
        expected = pd.to_numeric(expected_frame[col], errors="coerce").to_numpy(dtype=np.float64)
        got = pd.to_numeric(got_frame[col], errors="coerce").to_numpy(dtype=np.float64)
        both = np.isfinite(expected) & np.isfinite(got)
        null_mismatch = int((np.isfinite(expected) != np.isfinite(got)).sum())
        diff = np.abs(expected[both] - got[both])
        tolerance = np.maximum(0.01 * np.abs(expected[both]), 0.01)
        parity[col] = {
            "compared": int(both.sum()),
            "null_mismatch": null_mismatch,
            "within_tolerance": round(float(np.mean(diff <= tolerance)), 4) if both.any() else None,
            "max_abs_diff": round(float(diff.max()), 4) if both.any() else None,
        }

    engineered = engineer_rows(rows, model.float_dtype)
    model_only = model.predict(engineered, store)
    actual = truth[TARGET_COLUMN].to_numpy(dtype=np.float64)
    repeat_value, months = _repeat_sale_inputs(built_frame, store)
    renovated, recent_eligible = _renovation_flags(built_frame)
    predicted, blend = _repeat_sale_blend(
        actual, model_only, repeat_value, months, random_state,
        renovated=renovated, recent_eligible=recent_eligible,
    )
    if log is not None:
        log.info(
            f"evaluate: repeat-sale blend {'ENABLED' if blend['enabled'] else 'off'} over "
            f"{blend['n_repeat_sales']:,} repeat-sold homes, {blend['n_renovated_excluded']:,} renovated "
            f"since excluded from the fit (MdAPE model {blend.get('mdape_model')} vs "
            f"cross-fitted blend {blend.get('mdape_blend_crossfit')}; weights {blend.get('fitted_weights')})"
        )
    ape = np.abs(predicted - actual) / actual
    log_ratio = np.log(actual / predicted)
    anchored = recent_eligible & np.isfinite(repeat_value) & (repeat_value > 0)
    recent_sale = _recent_sale_calibration(log_ratio, months, anchored)
    if log is not None:
        log.info(
            "evaluate: recent-sale range blocks "
            + ", ".join(f"{k} n={v['n']} {v['log_ratio_quantiles']}" for k, v in recent_sale.items())
        )
    kinds = built_frame["propertytype"].astype("str").to_numpy()
    strata = _stratified_calibration(
        predicted,
        log_ratio,
        kinds,
        pd.to_numeric(built_frame.get(EVIDENCE_COLUMN), errors="coerce").to_numpy(dtype=np.float64),
    )
    district, borough = _areas_of(built_frame)
    area_strata, area_prior, area_scores = _area_calibration(
        log_ratio, kinds, district, borough, random_state, predicted=predicted
    )
    if log is not None:
        log.info(
            f"evaluate: {len(area_strata)} area strata, prior {area_prior:g} sales "
            f"(cross-fitted quantile loss by prior {area_scores})"
        )
    model_ape = np.abs(model_only - actual) / actual
    result = {
        "model": str(model.path),
        "model_kind": model.kind,
        "train_period": model.train_period,
        "n_estimated_certificates": int(
            sum(1 for r in rows if not r.get("epc_matched", True))
        ),
        "evaluated_from": str(date_from),
        "evaluated_to": str(date_to),
        "n_sales": int(len(sales)),
        "n_priced": int(len(truth)),
        "skipped": skipped,
        "median_absolute_percentage_error": round(float(np.median(ape)), 4),
        "within_10_percent": round(float(np.mean(ape <= 0.10)), 4),
        "within_20_percent": round(float(np.mean(ape <= 0.20)), 4),
        "log_ratio_quantiles": {
            name: round(float(np.quantile(log_ratio, q)), 4)
            for name, q in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90))
        },
        "by_actual_band": _band_report(actual, predicted, actual),
        "by_predicted_band": _band_report(actual, predicted, predicted),
        "strata": {**area_strata, **strata},
        "area_prior_sales": area_prior,
        "area_prior_scores": area_scores,
        "repeat_sale_blend": blend,
        "recent_sale": recent_sale,
        "model_only": {
            "median_absolute_percentage_error": round(float(np.median(model_ape)), 4),
            "within_10_percent": round(float(np.mean(model_ape <= 0.10)), 4),
        },
        "parity": parity,
    }
    return result


# The bands `evaluate` reports accuracy over. Grouped by *predicted* price, as
# the calibration strata are, the served model looks unbiased at the top end;
# grouped by what homes actually sold for it under-prices the dearest ones,
# more the dearer they are. Both groupings are reported so the gap shows.
ACCURACY_BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("<£1m", 0.0, 1_000_000.0),
    ("£1-2m", 1_000_000.0, 2_000_000.0),
    ("£2-3m", 2_000_000.0, 3_000_000.0),
    ("£3-5m", 3_000_000.0, 5_000_000.0),
    ("£5m+", 5_000_000.0, float("inf")),
)


def _band_report(actual: np.ndarray, predicted: np.ndarray, by: np.ndarray) -> Dict[str, Dict[str, object]]:
    """
    Return accuracy per `ACCURACY_BANDS` band of `by` (the actual or the predicted price).

    Each band: its sale count, the median and 10-90 spread of ``log(actual /
    predicted)`` (a positive median means the model prices the band low), and
    the median absolute percentage error.
    """
    report: Dict[str, Dict[str, object]] = {}
    for label, low, high in ACCURACY_BANDS:
        mask = (by >= low) & (by < high)
        if not mask.any():
            report[label] = {"n": 0}
            continue
        log_ratio = np.log(actual[mask] / predicted[mask])
        report[label] = {
            "n": int(mask.sum()),
            "median_log_ratio": round(float(np.median(log_ratio)), 4),
            "p10": round(float(np.quantile(log_ratio, 0.10)), 4),
            "p90": round(float(np.quantile(log_ratio, 0.90)), 4),
            "mdape": round(float(np.median(np.abs(predicted[mask] - actual[mask]) / actual[mask])), 4),
        }
    return report


def _areas_of(frame: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """Return each built row's postcode district and borough (None where unknown)."""
    district = np.array(
        [d if isinstance(d, str) and d else None for d in frame.get("postcode_district", pd.Series(dtype=object))],
        dtype=object,
    )
    borough = np.array([local_index.borough_of(name) for name in frame.get("lsoa_name", pd.Series(dtype=object))], dtype=object)
    if district.size != len(frame):
        district = np.full(len(frame), None, dtype=object)
    if borough.size != len(frame):
        borough = np.full(len(frame), None, dtype=object)
    return district, borough


def _renovation_flags(frame: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """
    Return each built row's renovated-since-its-previous-sale flag, and whether that sale may anchor its price.

    Read from `RENOVATED_COLUMN`, which `build_sale_row` sets: a row without a
    previous sale is neither renovated nor eligible.
    """
    flag = pd.to_numeric(frame.get(RENOVATED_COLUMN), errors="coerce") if RENOVATED_COLUMN in frame else None
    if flag is None:
        return np.zeros(len(frame), dtype=bool), np.zeros(len(frame), dtype=bool)
    values = flag.to_numpy(dtype=np.float64)
    return values == 1.0, values == 0.0


def _repeat_sale_inputs(frame: pd.DataFrame, store: EvidenceStore) -> Tuple[np.ndarray, np.ndarray]:
    """
    Return each built row's previous sale moved to its sale date along its area's index, and the months between.

    NaN where the row has no previous sale. The previous sale is the one the
    row itself carries (``prev_sale_price`` / ``prev_sale_date``), so this is
    exactly what `_predict_built` blends for a home priced today.
    """
    n = len(frame)
    repeat = np.full(n, np.nan)
    months = np.full(n, np.nan)
    prices = pd.to_numeric(frame.get("prev_sale_price"), errors="coerce") if "prev_sale_price" in frame else None
    if prices is None:
        return repeat, months
    for i, (price, prior, day, district, kind) in enumerate(
        zip(prices, frame["prev_sale_date"], frame[DATE_COLUMN], frame["postcode_district"], frame["propertytype"])
    ):
        if not np.isfinite(price) or price < MIN_PLAUSIBLE_PRICE or prior is None or pd.isna(prior):
            continue
        district = district if isinstance(district, str) else None
        repeat[i] = float(price) * store.time_factor(prior, day, district, str(kind))
        months[i] = _months_between(prior, day)
    return repeat, months


_MASKED_SALES_SQL = text(
    """
    SELECT lh.*
    FROM production.london_houses lh
    WHERE lh.dateoftransfer BETWEEN :date_from AND :date_to
      AND lh.epc_matched
    ORDER BY lh.dateoftransfer, lh.ppd_id
    """
)


def _accuracy(
    actual: np.ndarray,
    predicted: np.ndarray,
    propertytype: Optional[np.ndarray] = None,
    street_sales: Optional[np.ndarray] = None,
    district: Optional[np.ndarray] = None,
    borough: Optional[np.ndarray] = None,
) -> Dict[str, object]:
    """
    Return median absolute percentage error, shares within 10 % and 20 %, and log-ratio quantiles.

    With `propertytype` and `street_sales` it also returns the per-stratum
    blocks `_range_from_calibration` looks up, so a home priced from an
    estimated certificate gets a band graded by its size, type and evidence
    rather than one figure for every such home.
    """
    ape = np.abs(predicted - actual) / actual
    log_ratio = np.log(actual / predicted)
    strata = (
        _stratified_calibration(predicted, log_ratio, propertytype, street_sales)
        if propertytype is not None and street_sales is not None
        else {}
    )
    if propertytype is not None and district is not None and borough is not None:
        area_strata, _, _ = _area_calibration(
            log_ratio, np.asarray(propertytype), district, borough, predicted=predicted
        )
        strata = {**area_strata, **strata}
    return {
        "strata": strata,
        "median_absolute_percentage_error": round(float(np.median(ape)), 4),
        "within_10_percent": round(float(np.mean(ape <= 0.10)), 4),
        "within_20_percent": round(float(np.mean(ape <= 0.20)), 4),
        "log_ratio_quantiles": {
            name: round(float(np.quantile(log_ratio, q)), 4)
            for name, q in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90))
        },
    }


def evaluate_masked(
    engine,
    model: SavedModel,
    date_from: date,
    date_to: date,
    sample: int,
    random_state: int = 42,
    log: Optional[logging.Logger] = None,
    store: Optional[EvidenceStore] = None,
    blend: Optional[Mapping] = None,
) -> Dict[str, object]:
    """
    Price recent certified homes as if they had no certificate.

    Each sampled sale with a certificate is rebuilt with its own certificates
    removed, twice: from the surrogate `estimate_epc` gives (certificates in
    force on the sale date, the home's own excluded), and with the EPC fields
    left missing. Both are compared with the price paid.

    Returns
    -------
    dict
        The calibration block for estimated homes: the chosen ``strategy``
        (``"surrogate"`` unless missing inputs beat it on median absolute
        percentage error by more than `NAN_STRATEGY_MIN_ADVANTAGE`), its
        accuracy and ``log_ratio_quantiles``, ``comparison`` of the two, and
        how many homes each pool level priced.

    `blend` is the ``repeat_sale_blend`` block `evaluate` fitted; its served
    weights are applied here too, so the estimated block measures the price a
    home without a certificate is actually quoted.
    """
    store = store or attach_market_indices(
        EvidenceStore.load(engine, log, earliest_anchor=date_from), engine, model, log
    )
    served = {"repeat_sale_blend": blend} if blend else None
    with engine.connect() as connection:
        sales = pd.read_sql(_MASKED_SALES_SQL, connection, params={"date_from": date_from, "date_to": date_to})
    if sales.empty:
        raise ValueError(f"evaluate_masked: no certified sales between {date_from} and {date_to}")
    if sample and len(sales) > sample:
        sales = sales.sample(n=sample, random_state=random_state).sort_values([DATE_COLUMN, "ppd_id"])
    sales = sales.reset_index(drop=True)

    surrogate_rows, nan_rows, kept, levels, skipped = [], [], [], Counter(), Counter()
    with engine.connect() as connection:
        with profiling.stage("evaluate_masked/load_properties", sales=len(sales)):
            records = load_properties_by_ids(connection, [int(i) for i in sales["address_id"]])
        for position, sale in sales.iterrows():
            try:
                record = records.get(int(sale["address_id"]))
                if record is None:
                    record = load_property_by_ids(connection, [int(sale["address_id"])])
                masked = replace(record, epcs=record.epcs.iloc[0:0])
                surrogate = build_sale_row(
                    masked, sale[DATE_COLUMN], store, population=model.population,
                    anchor=sale[DATE_COLUMN], connection=connection,
                )
                missing = build_sale_row(
                    masked, sale[DATE_COLUMN], store, population=model.population,
                    anchor=sale[DATE_COLUMN], epc_strategy=ESTIMATE_STRATEGY_NAN,
                )
            except PricingError as exc:
                skipped[type(exc).__name__] += 1
                continue
            surrogate_rows.append(surrogate.row)
            nan_rows.append(missing.row)
            levels[surrogate.epc.level] += 1
            kept.append(position)
            if log is not None and len(kept) % 500 == 0:
                log.info(f"evaluate --mask-epc: built {len(kept)} of {len(sales)} sales")
    actual = sales.iloc[kept][TARGET_COLUMN].to_numpy(dtype=np.float64)

    def _graded(rows: List[Mapping[str, object]]) -> Dict[str, object]:
        """Score one set of rebuilt rows, stratified the way a price looks its band up."""
        frame = pd.DataFrame(rows)
        predicted = model.predict(engineer_rows(rows, model.float_dtype), store)
        repeat_value, months = _repeat_sale_inputs(frame, store)
        _, recent_eligible = _renovation_flags(frame)
        for i in np.flatnonzero(np.isfinite(repeat_value)):
            weight = repeat_sale_weight(served, months[i], bool(recent_eligible[i]))
            if weight > 0:
                predicted[i] = _blend(predicted[i], repeat_value[i], weight)
        district, borough = _areas_of(frame)
        return _accuracy(
            actual,
            predicted,
            frame["propertytype"].astype("str").to_numpy(),
            pd.to_numeric(frame.get(EVIDENCE_COLUMN), errors="coerce").to_numpy(dtype=np.float64),
            district,
            borough,
        )

    surrogate_accuracy = _graded(surrogate_rows)
    nan_accuracy = _graded(nan_rows)
    surrogate_mdape = surrogate_accuracy["median_absolute_percentage_error"]
    nan_mdape = nan_accuracy["median_absolute_percentage_error"]
    strategy = (
        ESTIMATE_STRATEGY_NAN
        if nan_mdape < surrogate_mdape * (1.0 - NAN_STRATEGY_MIN_ADVANTAGE)
        else ESTIMATE_STRATEGY_SURROGATE
    )
    chosen = nan_accuracy if strategy == ESTIMATE_STRATEGY_NAN else surrogate_accuracy
    result = {
        "strategy": strategy,
        "n_sales": int(len(sales)),
        "n_priced": int(len(kept)),
        "skipped": dict(skipped),
        "levels": dict(levels),
        **chosen,
        "comparison": {
            "surrogate": surrogate_accuracy,
            "nan": nan_accuracy,
            "nan_min_advantage": NAN_STRATEGY_MIN_ADVANTAGE,
        },
    }
    if log is not None:
        log.info(
            f"evaluate --mask-epc: surrogate MdAPE {surrogate_mdape:.1%} vs missing-EPC MdAPE {nan_mdape:.1%}; "
            f"using {strategy}"
        )
    return result


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    """Parse an ISO date for argparse."""
    return date.fromisoformat(value)


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description="Price London properties with a saved hpp_cb or hpp_rf model.")
    sub = parser.add_subparsers(dest="command", required=True)

    price = sub.add_parser("price", help="Price one address for a future sale date")
    price.add_argument(
        "--model", type=Path, required=True,
        help="Model bundle directory (hpp_cb --model-out), or an hpp_rf .rfm/.joblib with --trust-pickle",
    )
    price.add_argument("--postnumber", required=True)
    price.add_argument("--poststreet", default=None)
    price.add_argument("--unit", default=None, help='The flat, e.g. "FLAT 6"; omit for the whole number')
    price.add_argument("--posttown", required=True)
    price.add_argument("--postcode", required=True)
    price.add_argument("--date", type=_parse_date, required=True, help="Sale date, YYYY-MM-DD, after today")

    ev = sub.add_parser("evaluate", help="Rebuild real sales through the inference path and measure it")
    ev.add_argument("--model", type=Path, required=True)
    ev.add_argument(
        "--from",
        dest="date_from",
        type=_parse_date,
        default=None,
        help="First sale date to evaluate (default: the day after the model's training period ends)",
    )
    ev.add_argument("--to", dest="date_to", type=_parse_date, default=date.max)
    ev.add_argument(
        "--allow-in-sample",
        action="store_true",
        help="Evaluate sales inside the model's training period (its errors will read too small)",
    )
    ev.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Sales to sample (default 0 = every sale, which the district strata need)",
    )
    ev.add_argument("--random-state", type=int, default=42)
    ev.add_argument(
        "--rolling-parity",
        action="store_true",
        help="Also compare the rolling and same-street statistics with hpp_rf's training-time ones (slower)",
    )
    ev.add_argument(
        "--write-calibration",
        action="store_true",
        help="Save the measured error beside the model, where pricing reads its range from",
    )
    ev.add_argument(
        "--mask-epc",
        action="store_true",
        help=(
            "Also price certified homes as if they had no certificate (estimated vs missing EPC "
            "inputs) and store the result as the calibration's 'estimated' block"
        ),
    )

    fp = sub.add_parser(
        "fit-projection",
        help="Fit the UK HPI forward projection a future sale date is priced with",
    )
    fp.add_argument("--model", type=Path, required=True, help="The model the projection is stored beside")
    fp.add_argument(
        "--write",
        action="store_true",
        help="Save the fit beside the model, where pricing reads its projection from",
    )

    fl = sub.add_parser(
        "fit-local-index",
        help="Fit the district-by-type repeat-sales price index a sale date is priced along",
    )
    fl.add_argument("--model", type=Path, required=True, help="The model the local index is stored beside")
    fl.add_argument(
        "--write",
        action="store_true",
        help="Save the fit beside the model, where pricing reads its local index from",
    )

    for sp in (price, ev):
        sp.add_argument(
            "--trust-pickle",
            action="store_true",
            help="Load an hpp_rf .rfm/.joblib model: a pickle, which runs code from the file. Only for models you trained",
        )
    for sp in (price, ev, fp, fl):
        sp.add_argument("--verbose", action="store_true")
    return parser


def evaluation_window(
    model: SavedModel, date_from: Optional[date], allow_in_sample: bool = False
) -> date:
    """
    Return the first sale date to evaluate, refusing a window inside the training period.

    Defaults to the day after the model's training period ends. A calibration
    measured on sales the model was trained on reads its errors too small, and
    every range built from it too narrow.

    Raises
    ------
    ValueError
        When no window can be chosen, or the chosen one starts in-sample.
    """
    trained_to = None
    if model.train_period and len(model.train_period) == 2 and model.train_period[1]:
        trained_to = date.fromisoformat(str(model.train_period[1])[:10])
    if date_from is None:
        if trained_to is None:
            raise ValueError(
                f"{model.path} does not record its training period; pass --from with the first "
                "sale date after it (a backtest model trains on everything before its final holdout)"
            )
        return trained_to + timedelta(days=1)
    if trained_to is not None and date_from <= trained_to and not allow_in_sample:
        raise ValueError(
            f"--from {date_from} is inside the training period (to {trained_to}); the errors would be "
            f"in-sample. Use --from {trained_to + timedelta(days=1)} or later, or --allow-in-sample"
        )
    return date_from


def fit_projection(engine, log: Optional[logging.Logger] = None) -> Dict[str, object]:
    """
    Fit the index projection against ``production.ukhpi`` and return the sidecar payload.

    Read separately from the `EvidenceStore` so the fit needs neither a model
    nor the sales history: it is a property of the index alone.
    """
    columns = ", ".join(hpi_projection.PRICE_COLUMN_BY_TYPE.values())
    with engine.connect() as connection:
        frame = pd.read_sql(
            text(
                f"SELECT period, {hpi_projection.ALL_TYPES_COLUMN}, {columns} "
                "FROM production.ukhpi WHERE published_date IS NOT NULL"
            ),
            connection,
        )
    payload = hpi_projection.fit(frame)
    if log is not None:
        for propertytype, fitted in sorted(payload["types"].items()):
            gate = "projected" if fitted["beats_carry_forward"] else "CARRIED FORWARD (did not beat it)"
            errors = " ".join(
                f"{h}y {fitted['holdout_error'][h]:.3f}/{fitted['carry_forward_error'][h]:.3f}"
                for h in sorted(fitted["holdout_error"], key=int)
            )
            log.info(f"fit-projection: {propertytype} {gate}; cv/carry-forward error {errors}")
    return payload


def main(argv: Optional[List[str]] = None) -> Dict:
    """Run a subcommand against `common.db.get_model_db_url`'s database and print JSON."""
    args = build_arg_parser().parse_args(argv)
    log = hpp_cb.get_logger(args.verbose)
    from sqlalchemy import create_engine

    from hpldn.backend.common.db import get_model_db_url

    engine = create_engine(get_model_db_url(log))
    if args.command == "fit-local-index":
        result = local_index.fit(engine, log)
        if args.write:
            artifact.update_json_files(args.model, {artifact.LOCAL_INDEX_FILE: result}, logger=log)
            log.info(f"Wrote local price index to {local_index.sidecar_path(args.model)}")
        summary = {k: v for k, v in result.items() if k != "series"}
        summary["series_fitted"] = len(result["series"])
        summary["series_projected"] = sum(1 for f in result["series"].values() if f["beats_carry_forward"])
        print(json.dumps(summary, indent=2, default=str))
        return result
    if args.command == "fit-projection":
        result = fit_projection(engine, log)
        if args.write:
            artifact.update_json_files(args.model, {artifact.PROJECTION_FILE: result}, logger=log)
            log.info(f"Wrote projection to {hpi_projection.projection_path(args.model)}")
        print(json.dumps(result, indent=2, default=str))
        return result
    model = SavedModel(args.model, trust_pickle=args.trust_pickle)
    if args.command == "price":
        service = PricingService(engine, model, log)
        address = PropertyAddress(args.postnumber, args.poststreet, args.posttown, args.postcode, args.unit)
        try:
            result = service.price(address, args.date).to_dict()
        except PricingError as exc:
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, indent=2))
            sys.exit(2)
    else:
        date_to = args.date_to if args.date_to != date.max else today_in_london()
        try:
            date_from = evaluation_window(model, args.date_from, args.allow_in_sample)
        except ValueError as exc:
            print(json.dumps({"error": "InvalidWindow", "detail": str(exc)}, indent=2))
            sys.exit(2)
        log.info(f"evaluate: sales from {date_from} to {date_to} (model trained on {model.train_period})")
        store = attach_market_indices(
            EvidenceStore.load(engine, log, earliest_anchor=date_from), engine, model, log
        )
        result = evaluate(
            engine, model, date_from, date_to, args.sample, args.random_state, log,
            store=store, rolling_parity=args.rolling_parity,
        )
        if args.mask_epc:
            result["estimated"] = evaluate_masked(
                engine, model, date_from, date_to, args.sample, args.random_state, log, store=store,
                blend=result.get("repeat_sale_blend"),
            )
        if args.write_calibration:
            if model.kind != "catboost":
                raise SystemExit("--write-calibration needs a model bundle; an hpp_rf pickle has none")
            calibration = {k: v for k, v in result.items() if k != "parity"}
            # Re-hashed and re-signed with the calibration in it.
            artifact.update_json_files(args.model, {artifact.CALIBRATION_FILE: calibration}, logger=log)
            log.info(f"Wrote calibration to {calibration_path(args.model)}")
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
