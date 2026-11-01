"""
Pydantic response models, the read API's contract.

Kept separate from the ORM models so a new table column cannot silently change
a response. The ``/properties`` schemas are snake_case; the market schemas are
camelCase on the wire and match ``frontend/src/data/types.ts`` field for field.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from hpldn.backend.api.market.districts import (
    MAX_FLOOR_AREA,
    MIN_FLOOR_AREA,
    is_full_postcode,
    min_floor_area_for,
    is_postcode_shaped,
    normalise_postcode,
)
from hpldn.backend.api.market.search_query import DEFAULT_LIMIT, MAX_LIMIT, validate_query


class PropertySummary(BaseModel):
    """An address and its UPRN."""

    uprn: int
    postnumber: str
    poststreet: str | None = None
    posttown: str
    postcode: str
    postcounty: str
    unit: str | None = None


class TransactionRead(BaseModel):
    """One Price Paid transaction."""

    model_config = ConfigDict(from_attributes=True)

    price: int
    dateoftransfer: date
    propertytype: str
    duration: str


class EpcSummary(BaseModel):
    """The headline fields of one energy performance certificate."""

    model_config = ConfigDict(from_attributes=True)

    current_energy_rating: str | None = None
    potential_energy_rating: str | None = None
    current_energy_efficiency: int | None = None
    potential_energy_efficiency: int | None = None
    total_floor_area: float | None = None
    tenure: str | None = None
    construction_age_band: str | None = None
    inspection_date: date | None = None
    lodgement_datetime: datetime | None = None


class PropertyDetail(PropertySummary):
    """An address with its latest EPC and transaction history."""

    epc: EpcSummary | None = None
    transactions: list[TransactionRead] = []


PropertyType = Literal["flat", "maisonette", "terraced", "semi-detached", "detached"]
Confidence = Literal["high", "medium", "fair"]
DriverTone = Literal["positive", "neutral", "caution"]
MatchType = Literal["same", "broader"]


class CamelModel(BaseModel):
    """Base model serialised with camelCase aliases."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DataWindow(CamelModel):
    """
    The data behind the page's figures.

    The area figures describe the calendar year of the latest sale
    (``to_year``, which may be a part year: it runs to ``latest_sale``) and
    compare it with the year before and five years before. Nearby sales and
    the estimate's evidence still draw on ``from_year`` onwards.

    Attributes
    ----------
    from_year, to_year : int
        First and last calendar year of the window.
    latest_sale : date
        Date of the most recent sale held.
    total_sales : int
        Every sale in ``production.london_houses``, not just the window's.
    first_sale_year : int
        Year of the earliest sale held.
    """

    from_year: int
    to_year: int
    latest_sale: date
    total_sales: int
    first_sale_year: int


class BedroomRents(CamelModel):
    """
    A borough's mean monthly private rent by bedrooms, for the month in
    `AreaStats.rent_month`; a figure ONS suppressed is None.
    """

    one_bed: int | None
    two_bed: int | None
    three_bed: int | None
    four_plus_bed: int | None


class AreaStats(CamelModel):
    """
    The figures shown for one area, for the calendar year of the latest sale.

    ``movement1y`` and ``movement5y`` carry explicit aliases because
    `to_camel` would spell them ``movement1Y`` and ``movement5Y``.

    Every figure comes from ``production`` tables; see
    `repository.area_stats_from_rows`. A figure the data cannot support is
    None ("not enough sales", "not known"), never a stand-in number: a
    movement without sales in the year compared with, the walk without a
    station distance, the transport score without PTAL, the profile without
    deprivation data, and the rent and yield for a borough without a
    published rent. ``low_data`` marks an area with fewer than
    `repository.LOW_DATA_SALES` sales in the year.

    The rents come from ``production.london_rents`` (migration 0026), the ONS
    Price Index of Private Rents: ``typical_monthly_rent`` is the borough's
    mean monthly rent for ``rent_month`` (its latest published month), to the
    £1, and ``rental_yield`` twelve of it over ``average_price``;
    ``bedroom_rents`` is the same month's mean by bedrooms. The City of London
    has no published rent, so all four are None there.

    ``estimator_price_per_sqm`` is the median price per m² of the window's
    sized sales, which the estimate formula uses; it is never sent.
    """

    code: str
    name: str
    borough: str
    average_price: int
    average_price_per_sqm: int | None
    movement1y: float | None = Field(alias="movement1y")
    movement5y: float | None = Field(alias="movement5y")
    transport_score: int | None
    walk_to_station_mins: int | None
    rental_yield: float | None
    typical_monthly_rent: int | None
    rent_month: date | None = None
    bedroom_rents: BedroomRents | None = None
    sales_count: int
    figures_year: int
    area_profile_percent: float | None
    epc_band: str
    low_data: bool = False
    estimator_price_per_sqm: int | None = Field(default=None, exclude=True)


class MortgageRates(CamelModel):
    """
    The latest month's two-year fixed mortgage rates, in percent, by
    loan-to-value band (``production.mortgage_rates``).
    """

    rate_date: date
    ltv60: float | None
    ltv75: float | None
    ltv85: float | None
    ltv90: float | None
    ltv95: float | None


class InflationMonth(CamelModel):
    """One month of the CPIH index (2015 = 100)."""

    month: date
    index: float


class InflationSeries(CamelModel):
    """
    UK consumer price inflation, month by month (``production.cpih``, migration 0032).

    ``months`` is ONS's CPIH 12-month rate for all items chained into an
    index, 2015 averaging 100, oldest first: a figure moves from one month's
    pounds to another's by the ratio of their indexes. ``latest_annual_rate``
    (percent) is the 12-month rate to ``latest_month``, the last month
    published; a day after it is reached by carrying that rate forward.
    """

    source: str
    latest_month: date
    latest_annual_rate: float
    months: list[InflationMonth]


class AreasResponse(CamelModel):
    """The data window, every covered area and the latest mortgage rates."""

    window: DataWindow
    areas: list[AreaStats]
    mortgage_rates: MortgageRates | None = None


class Comparable(CamelModel):
    """
    A nearby sale shown beneath an estimate.

    ``match_type`` is ``"same"`` for the requested type and ``"broader"`` for
    a sale of the same broad kind (flat or maisonette, or any house) used
    because too few of the requested type sold nearby.

    ``price`` is what was paid; ``adjusted_price`` is that price at the
    requested floor area and the valuation date, which is what the estimate is
    built from.
    """

    id: str
    street: str
    district: str
    property_type: PropertyType
    floor_area_sqm: int
    price: int
    sold_on: date
    distance_km: float
    match_type: MatchType = "same"
    # The price paid, scaled to the requested floor area and moved to the
    # valuation date by the UK HPI. This is the figure the estimate actually
    # uses, so the list and the headline reconcile on screen.
    adjusted_price: int | None = None


class EstimateRequest(CamelModel):
    """
    What the landlord typed.

    The district is derived from the postcode server-side; a ``district`` sent
    by the client is ignored. ``sale_date`` is the day the value is asked for,
    as ``POST /price`` takes one; it may be omitted.
    """

    postcode: str
    property_type: PropertyType
    bedrooms: int = Field(ge=1, le=6)
    floor_area_sqm: float = Field(ge=MIN_FLOOR_AREA, le=MAX_FLOOR_AREA)
    # Optional so an older client still estimates; the route fills it with the
    # earliest date it can price when it is absent.
    sale_date: date | None = None

    @field_validator("postcode")
    @classmethod
    def _postcode_shaped(cls, value: str) -> str:
        """Reject values that are not postcode-shaped and normalise the rest."""
        if not is_postcode_shaped(value):
            raise ValueError("Invalid UK Postcode")
        return normalise_postcode(value)

    @model_validator(mode="after")
    def _room_for_the_bedrooms(self) -> "EstimateRequest":
        """Reject more bedrooms than the floor area can hold (`min_floor_area_for`)."""
        needed = min_floor_area_for(self.bedrooms)
        if self.floor_area_sqm < needed:
            raise ValueError(
                f"A home with {self.bedrooms} bedrooms needs at least {needed} m². "
                "Check the floor area or the bedrooms."
            )
        return self


class ResolvedEstimateRequest(EstimateRequest):
    """The request as the estimate was made for it, district included."""

    district: str


class Driver(CamelModel):
    """One reason behind an estimate."""

    id: str
    label: str
    detail: str
    tone: DriverTone


class EstimateResult(CamelModel):
    """
    An estimate, its range, reasons, nearby sales and implied rent.

    ``suggested_monthly_rent`` is the borough's mean monthly rent for homes
    with the requested bedrooms (four or more for 4-6), else its overall mean,
    and None when the area has no published rent.
    """

    request: ResolvedEstimateRequest
    value: int
    low: int
    high: int
    confidence: Confidence
    spread_percent: int
    drivers: list[Driver]
    comparables: list[Comparable]
    suggested_monthly_rent: int | None
    window: DataWindow
    # How the figure was reached: "model" when the CatBoost model priced it,
    # "area" when it fell back to the area formula.
    basis: Literal["model", "area"] = "area"
    projection_uplift: float | None = None
    projected_from: date | None = None
    index_area: str | None = None


class AddressOption(CamelModel):
    """
    One home in a postcode, as the property register writes its address.

    ``poststreet`` is None where the register holds no street for it.
    ``unit`` is the flat within the building ("FLAT 6", the register's SAON),
    None for a house or where no record names one.
    """

    postnumber: str
    poststreet: str | None = None
    posttown: str
    postcode: str
    unit: str | None = None


class AddressSuggestion(AddressOption):
    """
    One home suggested for a partial address.

    ``street_hint`` is the street the home is searched under when the register
    holds none for it (its postcode's only street), for the label only: ``POST
    /price`` takes the four address fields and ignores it. None otherwise.
    """

    street_hint: str | None = None


class AddressSearchParams(BaseModel):
    """
    The query string of ``GET /addresses/search``.

    ``q`` is checked and normalised by `search_query.validate_query`: 3-64
    characters once trimmed and collapsed, letters, digits, spaces and
    ``, . / ' -`` only, upper-cased with apostrophes dropped. ``limit`` is 1-10.
    The route turns a violation into a 422 whose body never repeats ``q``.
    """

    model_config = ConfigDict(frozen=True)

    q: str
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, strict=False)

    @field_validator("q", mode="before")
    @classmethod
    def _query(cls, value: object) -> str:
        """Reject a query outside the bounds and return the normalised form."""
        if not isinstance(value, str):
            raise ValueError("Type at least 3 characters of the address")
        return validate_query(value)


class HomeRequest(CamelModel):
    """
    A specific home, by the address fields of ``production.address_table``.

    ``unit`` ("FLAT 6") narrows the home to that flat of the building; without
    it every register row of the number and street is the home, as before
    units were kept.
    """

    postnumber: str = Field(min_length=1, max_length=100)
    poststreet: str | None = Field(default=None, max_length=100)
    unit: str | None = Field(default=None, max_length=100)

    @field_validator("poststreet", "unit")
    @classmethod
    def _blank_street(cls, value: str | None) -> str | None:
        """Treat a blank street or unit as none."""
        return value or None

    posttown: str = Field(min_length=1, max_length=60)
    postcode: str

    @field_validator("postcode")
    @classmethod
    def _full_postcode(cls, value: str) -> str:
        """Reject anything but a full postcode and normalise it."""
        if not is_full_postcode(value):
            raise ValueError("Enter the full postcode, e.g. CR0 1JN")
        return normalise_postcode(value)

    @field_validator("postnumber", "poststreet", "posttown", "unit", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        """
        Trim whitespace before the length checks, turning a blank street into None.

        A blank house number or town stays ``""`` so ``min_length`` rejects it
        with a 422, rather than reaching the register as a missing field.
        """
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value


class PriceRequest(HomeRequest):
    """
    A specific home and the day it would sell.

    The sale date must be after today (London time), which the route checks.
    """

    sale_date: date


class LastSale(CamelModel):
    """The home's latest recorded sale."""

    price: int
    sold_on: date


class PriceResult(CamelModel):
    """
    A model price for one home on one day.

    ``low`` and ``high`` span the middle 80 % of the model's errors on recent
    sales of homes like this one -- its price band, type and how many nearby
    sales back it -- and are None when that has not been measured. They widen
    with the forecast horizon. ``evidence_to`` is the latest sale held: the
    market evidence behind the price ends there, however far ahead
    ``sale_date`` is. ``within_ten_percent`` is the share of recent sales the
    model priced within 10 % of the price paid, when measured.

    ``value`` is to the pound (it moves every day with the sale date); ``low``
    and ``high`` are to the £1,000. The model prices the home as of
    ``projected_from``, the day after the latest sale held, and
    ``projection_uplift`` is how much the price index then moved it to
    ``sale_date``. ``index_area`` is whose index that was: the home's postcode
    district (e.g. "SW11"), its borough, or "London", for its property type.
    All three are None when the index did not move the price.

    ``epc_source`` is ``"certificate"`` when the floor area and rooms come from
    the home's energy certificate, and ``"estimated"`` when it has none and
    they were estimated from comparable homes nearby (``estimated_from``, e.g.
    "12 terraced houses on this street"). An estimated home has no
    ``energy_rating``, a wider range, and a null ``floor_area_sqm`` when no
    pool of comparable homes was big enough; ``estimated_energy_rating`` is
    then the most common rating of the homes nearby, None for a certificate.

    ``anchored_on_last_sale`` is True when the home's own recent sale
    (``last_sale``, within two years and with no sign of renovation since)
    carries most of the price, rather than the model.
    """

    address: AddressOption
    sale_date: date
    value: int
    low: int | None
    high: int | None
    property_type: PropertyType
    floor_area_sqm: int | None
    energy_rating: str | None
    habitable_rooms: float | None
    last_sale: LastSale | None
    estimated_energy_rating: str | None = None
    evidence_to: date
    within_ten_percent: float | None
    projection_uplift: float | None = None
    projected_from: date | None = None
    index_area: str | None = None
    epc_source: Literal["certificate", "estimated"]
    estimated_from: str | None
    anchored_on_last_sale: bool = False


class NearbySale(CamelModel):
    """
    One recent sale near a home, with the neighbourhood figures held for it.

    ``price`` and ``sold_on`` are the Price Paid record. The ``comp_*`` fields
    are its row in ``production.comps_features`` (the median price and price
    per m² of the nearest same-type sales within 2 km in the 12 months before
    it, and how many there were); ``local_type_median`` is
    ``proptype_local_median_800m_6m`` from ``production.neighbourhood_features``.
    Any of them is None where the table holds none. ``distance_m`` is the
    straight-line distance from the home, in metres.
    """

    address: AddressOption
    property_type: PropertyType
    price: int
    sold_on: date
    distance_m: int
    comp_median_price: int | None
    comp_median_price_per_sqm: int | None
    comp_txn_count: int | None
    local_type_median: int | None


class NearbySales(CamelModel):
    """The sales nearest a home in the 12 months to ``window_to``, nearest first."""

    sales: list[NearbySale]
    window_from: date
    window_to: date


SchoolPhase = Literal["primary", "secondary", "all_through"]
GradeBasis = Literal["overall", "key_judgements", "report_card", "carried"]


class NearbySchool(CamelModel):
    """
    A state-funded school near a home, with the Ofsted rating in force on ``as_of``.

    From the ``ofsted_rating_spells`` table (migration 0033). ``rating_label``
    is the rating in its era's words ("Good", "Requires improvement",
    "Expected standard"...); ``rating_score`` the harmonised 1 (best) to 4
    score; ``grade_basis`` what it rests on: ``overall`` effectiveness (to
    August 2024), the mean of the ``key_judgements`` (September 2024 -
    October 2025), a ``report_card`` (from November 2025), or ``carried``
    when an ungraded inspection confirmed the previous rating.
    ``inspected_on`` is that inspection's first day, ``published_on`` its
    report's. A school that converted (an academy, say) and has no report of
    its own yet carries its predecessor's, ``inherited_from_urn``.
    ``distance_m`` is straight-line, not a catchment.
    """

    urn: int
    name: str
    phase: SchoolPhase
    distance_m: int
    rating_label: str
    rating_score: float
    grade_basis: GradeBasis
    inspected_on: date
    published_on: date
    inherited_from_urn: int | None
    report_url: str


class NearbySchools(CamelModel):
    """The nearest primary and secondary schools to a home, nearest first, within ``radius_m``."""

    primary: list[NearbySchool]
    secondary: list[NearbySchool]
    as_of: date
    radius_m: int
    source: str


class SessionConfig(CamelModel):
    """What the page needs to run reCAPTCHA: served at runtime so the key can rotate without a rebuild."""

    enabled: bool
    site_key: str = Field(description="The reCAPTCHA v3 site key; empty while the gate is off")
    action: str


class SessionRequest(BaseModel):
    """A reCAPTCHA v3 token to trade for a pass."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=8192)


class SessionPass(CamelModel):
    """A pass for ``X-HPLDN-Pass``, and how many seconds it lasts."""

    pass_: str = Field(alias="pass")
    expires_in: int
