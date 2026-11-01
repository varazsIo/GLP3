"""
Declarative models for the ``production`` schema, the serving contract.

One class per relation in ``production`` after migration 0016, at the state
the live database carries. The classes describe and query the schema; the
alembic chain builds it, so ``production_meta.create_all()`` is never called.
Relations without a primary key map their unique column through
``__mapper_args__["primary_key"]``. See ``models/README.md``.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List

from sqlalchemy import (
    CheckConstraint,
    Identity,
    Index,
    MetaData,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import (
    CHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)

PRODUCTION_SCHEMA = "production"

production_meta = MetaData(schema=PRODUCTION_SCHEMA)


class ProductionBase(DeclarativeBase):
    """Declarative base for the `production` serving schema."""

    metadata = production_meta


# Category tables. Each carries two identical UNIQUE constraints because the
# promote's LIKE ... INCLUDING ALL copies both staging constraints.


class RatingCategory(ProductionBase):
    """
    Efficiency rating vocabulary, 1..5 ascending.

    Very Poor = 1, Poor = 2, Average = 3, Good = 4, Very Good = 5. Every
    ``*_energy_eff`` and ``*_env_eff`` column on `EPC` holds one of these ids.
    """

    __tablename__ = "rating_category_table"
    __table_args__ = (
        UniqueConstraint(
            "rating_category",
            name="rating_category_table_rating_category_key",
        ),
        UniqueConstraint(
            "rating_category",
            name="rating_category_table_rating_category_key1",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    rating_category: Mapped[str] = mapped_column(String(9), nullable=False)


class BuildFormCategory(ProductionBase):
    """
    Build-form vocabulary, the reverse mapping of `EPC.build_form`.

    Detached = 1, Enclosed End-Terrace = 2, Enclosed Mid-Terrace = 3,
    End-Terrace = 4, Mid-Terrace = 5, Semi-Detached = 6.
    """

    __tablename__ = "build_form_category_table"
    __table_args__ = (
        UniqueConstraint("build_form", name="build_form_category_table_build_form_key"),
        UniqueConstraint(
            "build_form", name="build_form_category_table_build_form_key1"
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    build_form: Mapped[str] = mapped_column(String(20), nullable=False)


class TransactionTypeCategory(ProductionBase):
    """
    EPC transaction-type vocabulary, the reverse mapping of `EPC.transaction_type`.

    'assessment for green deal' = 1 through 'Stock condition survey' = 12.
    """

    __tablename__ = "transaction_type_category_table"
    __table_args__ = (
        UniqueConstraint(
            "transaction_type",
            name="transaction_type_category_table_transaction_type_key",
        ),
        UniqueConstraint(
            "transaction_type",
            name="transaction_type_category_table_transaction_type_key1",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(25), nullable=False)


class EnergyTariffCategory(ProductionBase):
    """Energy-tariff vocabulary: '24 hour' = 1 through 'Unknown' = 9."""

    __tablename__ = "energy_tariff_category_table"
    __table_args__ = (
        UniqueConstraint(
            "energy_tariff",
            name="energy_tariff_category_table_energy_tariff_key",
        ),
        UniqueConstraint(
            "energy_tariff",
            name="energy_tariff_category_table_energy_tariff_key1",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    energy_tariff: Mapped[str] = mapped_column(String(16), nullable=False)


class GlazedTypeCategory(ProductionBase):
    """
    Glazing vocabulary, the reverse mapping of `EPC.glazed_type`.

    'double glazing installed before 2002' = 1 through 'triple, known data' = 10.
    """

    __tablename__ = "glazed_type_category_table"
    __table_args__ = (
        UniqueConstraint(
            "glazed_type",
            name="glazed_type_category_table_glazed_type_key",
        ),
        UniqueConstraint(
            "glazed_type",
            name="glazed_type_category_table_glazed_type_key1",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    glazed_type: Mapped[str] = mapped_column(String(50), nullable=False)


# The promoted spine (migration 0007). LIKE ... INCLUDING ALL drops foreign
# keys, so relationships spell out their joins and are viewonly.


class Address(ProductionBase):
    """
    The normalised postal address every transaction and certificate hangs off.

    ``uid`` references ``staging.ukhcs_table.id`` in staging but is a plain
    unique BIGINT here. Use `UprnLookup` to get from a UPRN to an ``id``, as
    the read API's ``/properties`` routes do.
    """

    __tablename__ = "address_table"
    __table_args__ = (
        UniqueConstraint("uid", name="address_table_uid_key"),
        Index("address_table_uid_idx", "uid", unique=True),
        Index("idx_prod_address_postcode", "postcode"),
        Index("idx_prod_address_posttown", "posttown"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
    )
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    postnumber: Mapped[str] = mapped_column(String, nullable=False)
    posttown: Mapped[str] = mapped_column(String, nullable=False)
    postcode: Mapped[str] = mapped_column(String(8), nullable=False)
    postcounty: Mapped[str] = mapped_column(String, nullable=False)
    poststreet: Mapped[str] = mapped_column(String, nullable=True)
    # The flat within the building ("FLAT 3"), the register's SAON (0030).
    unit: Mapped[str] = mapped_column(String, nullable=True)

    ppd_history: Mapped[List["PPD"]] = relationship(
        back_populates="address",
        primaryjoin=lambda: Address.id == PPD.address_id,
        foreign_keys=lambda: [PPD.address_id],
        viewonly=True,
    )
    epc_history: Mapped[List["EPC"]] = relationship(
        back_populates="address",
        primaryjoin=lambda: Address.id == EPC.address_id,
        foreign_keys=lambda: [EPC.address_id],
        viewonly=True,
    )


class PPD(ProductionBase):
    """
    HM Land Registry Price Paid transactions in London, ~1.92M rows.

    The driving table of the serving schema: every feature table is keyed on
    ``ppd_id``, this table's ``id``.

    Attributes
    ----------
    oldnew : str
        ``'Y'`` for the first sale of a new build, else ``'N'``. Backfilled
        by migration 0008 on (postcode, price, dateoftransfer); a key whose
        source rows disagree is NULL rather than a guess.
    transactionid : str
        HM Land Registry's Price Paid transaction id, unique where set
        (migration 0027).
    link_method : str
        The evidence that placed the sale on its UPRN (0027, 0028;
        ``migrations/sale_linking.py``).
    """

    __tablename__ = "ppd_table"
    __table_args__ = (
        CheckConstraint(
            "(duration = ANY (ARRAY['F'::bpchar, 'L'::bpchar]))",
            name="ck_ppd_duration",
        ),
        CheckConstraint("(price > 0)", name="ck_ppd_price_positive"),
        CheckConstraint(
            "(propertytype = ANY (ARRAY['D'::bpchar, 'S'::bpchar, 'T'::bpchar, 'F'::bpchar]))",
            name="ck_ppd_propertytype",
        ),
        UniqueConstraint(
            "address_id",
            "dateoftransfer",
            "price",
            name="ppd_table_address_id_dateoftransfer_price_key",
        ),
        Index("ppd_table_address_id_idx", "address_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
    )
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=False)
    propertytype: Mapped[str] = mapped_column(CHAR, nullable=False)
    duration: Mapped[str] = mapped_column(CHAR, nullable=False)
    oldnew: Mapped[str] = mapped_column(CHAR, nullable=True)
    transactionid: Mapped[str] = mapped_column(String, nullable=True)
    link_method: Mapped[str] = mapped_column(String, nullable=True)

    address: Mapped["Address"] = relationship(
        back_populates="ppd_history",
        primaryjoin=lambda: Address.id == PPD.address_id,
        foreign_keys=lambda: [PPD.address_id],
        viewonly=True,
    )


class EPC(ProductionBase):
    """
    Energy Performance Certificates, one row per (address_id, inspection_date).

    About 2.30M rows at 0016; 0019 adds the certificates of the addresses 0017 and 0018 mint. Free-text vocabularies are stored as ids into the
    category tables. ``idx_prod_epc_address_inspection`` orders certificates
    newest-first per address, so the view takes the latest certificate
    inspected on or before a sale without a sort.

    Attributes
    ----------
    floor_level : str
        Normalised storey of a flat or maisonette, derived from the register's
        free-text FLOOR_LEVEL by migration 0008.
    property_type : str
        The register's PROPERTY_TYPE (Flat, Maisonette, House, Bungalow, Park
        home), backfilled by migration 0019 and read by the view's
        ``is_maisonette`` fallback.
    """

    __tablename__ = "epc_table"
    __table_args__ = (
        CheckConstraint(
            "((current_energy_rating IS NULL) OR (current_energy_rating = ANY (ARRAY['A'::bpchar, 'B'::bpchar, 'C'::bpchar, 'D'::bpchar, 'E'::bpchar, 'F'::bpchar, 'G'::bpchar])))",
            name="ck_epc_current_energy_rating",
        ),
        CheckConstraint(
            "((mains_gas_flag IS NULL) OR (mains_gas_flag = ANY (ARRAY['Y'::bpchar, 'N'::bpchar])))",
            name="ck_epc_mains_gas_flag",
        ),
        CheckConstraint(
            "((potential_energy_rating IS NULL) OR (potential_energy_rating = ANY (ARRAY['A'::bpchar, 'B'::bpchar, 'C'::bpchar, 'D'::bpchar, 'E'::bpchar, 'F'::bpchar, 'G'::bpchar])))",
            name="ck_epc_potential_energy_rating",
        ),
        CheckConstraint(
            "((tenure IS NULL) OR (tenure = ANY (ARRAY['O'::bpchar, 'P'::bpchar, 'S'::bpchar])))",
            name="ck_epc_tenure",
        ),
        UniqueConstraint(
            "address_id",
            "inspection_date",
            name="epc_table_address_id_inspection_date_key",
            postgresql_nulls_not_distinct=True,
        ),
        Index("epc_table_address_id_idx", "address_id"),
        Index("epc_table_build_form_idx", "build_form"),
        Index("epc_table_energy_tariff_idx", "energy_tariff"),
        Index("epc_table_glazed_type_idx", "glazed_type"),
        Index("epc_table_hot_water_energy_eff_idx", "hot_water_energy_eff"),
        Index("epc_table_hot_water_env_eff_idx", "hot_water_env_eff"),
        Index("epc_table_lighting_energy_eff_idx", "lighting_energy_eff"),
        Index("epc_table_lighting_env_eff_idx", "lighting_env_eff"),
        Index("epc_table_mainheat_energy_eff_idx", "mainheat_energy_eff"),
        Index("epc_table_mainheat_env_eff_idx", "mainheat_env_eff"),
        Index("epc_table_mainheatc_energy_eff_idx", "mainheatc_energy_eff"),
        Index("epc_table_mainheatc_env_eff_idx", "mainheatc_env_eff"),
        Index("epc_table_roof_energy_eff_idx", "roof_energy_eff"),
        Index("epc_table_roof_env_eff_idx", "roof_env_eff"),
        Index("epc_table_transaction_type_idx", "transaction_type"),
        Index("epc_table_walls_energy_eff_idx", "walls_energy_eff"),
        Index("epc_table_walls_env_eff_idx", "walls_env_eff"),
        Index("epc_table_windows_energy_eff_idx", "windows_energy_eff"),
        Index("epc_table_windows_env_eff_idx", "windows_env_eff"),
        Index(
            "idx_prod_epc_address_inspection",
            "address_id",
            text("inspection_date DESC"),
            text("lodgement_datetime DESC NULLS LAST"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
    )
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_energy_rating: Mapped[str] = mapped_column(CHAR, nullable=True)
    potential_energy_rating: Mapped[str] = mapped_column(CHAR, nullable=True)
    current_energy_efficiency: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    potential_energy_efficiency: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    build_form: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, nullable=True)
    transaction_type: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    environment_impact_current: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    environment_impact_potential: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    energy_consumption_current: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    energy_consumption_potential: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    co2_emissions_current: Mapped[float] = mapped_column(Float, nullable=True)
    co2_emiss_curr_per_floor_area: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    co2_emissions_potential: Mapped[float] = mapped_column(Float, nullable=True)
    lighting_cost_current: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    lighting_cost_potential: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    heating_cost_current: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    heating_cost_potential: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    hot_water_cost_current: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    hot_water_cost_potential: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    total_floor_area: Mapped[float] = mapped_column(Float, nullable=True)
    energy_tariff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    mains_gas_flag: Mapped[str] = mapped_column(CHAR, nullable=True)
    flat_storey_count: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    multi_glaze_proportion: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    glazed_type: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    extension_count: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    number_habitable_rooms: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    number_heated_rooms: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    low_energy_lighting: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    hot_water_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    hot_water_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    windows_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    windows_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    walls_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    walls_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    roof_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    roof_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    mainheat_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    mainheat_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    mainheatc_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    mainheatc_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    lighting_energy_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    lighting_env_eff: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    construction_age_band: Mapped[str] = mapped_column(String, nullable=True)
    lodgement_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    tenure: Mapped[str] = mapped_column(CHAR, nullable=True)
    floor_level: Mapped[str] = mapped_column(String, nullable=True)
    property_type: Mapped[str] = mapped_column(String, nullable=True)

    address: Mapped["Address"] = relationship(
        back_populates="epc_history",
        primaryjoin=lambda: Address.id == EPC.address_id,
        foreign_keys=lambda: [EPC.address_id],
        viewonly=True,
    )


# Address geography and lookups (migrations 0007 and 0010).


class UprnLookup(ProductionBase):
    """
    UPRN to `Address.id`, ~11.37M rows, derived by migration 0007.

    The join the read API uses to resolve a UPRN and `LondonHouses` uses to
    attach one to a sale. No primary key; ``uprn`` has a unique index.
    """

    __tablename__ = "uprn_lookup"
    __table_args__ = (Index("idx_prod_uprn_lookup", "uprn", unique=True),)

    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)

    __mapper_args__ = {"primary_key": (uprn,)}


class AddressCoordinates(ProductionBase):
    """
    WGS84 latitude and longitude per `Address.id`, ~11.37M rows, from OS Open UPRN.

    Built by migration 0010 and read by every later spatial feature: comps,
    neighbourhood, station and river proximity, PTAL and LSOA assignment.
    """

    __tablename__ = "address_coordinates"
    __table_args__ = (
        Index("idx_prod_address_coordinates_address_id", "address_id", unique=True),
    )

    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (address_id,)}


class LondonParcels(ProductionBase):
    """
    Lot area in m² per UPRN, from the INSPIRE parcel containing the address.

    INSPIRE covers freehold only, so leasehold UPRNs have no row.
    """

    __tablename__ = "london_parcels"
    __table_args__ = (Index("idx_prod_london_parcels_uprn", "uprn"),)

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
    )
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lot_area_sqm: Mapped[float] = mapped_column(Float, nullable=False)


# Reference tables: small macro sources loaded from CSV by migration 0009.


class BoeRate(ProductionBase):
    """Bank of England Bank Rate history, 258 rows, one per change."""

    __tablename__ = "boe_rate"

    date_changed: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)


class LondonStations(ProductionBase):
    """London rail and Underground stations with fare zone, 659 rows (read by migration 0013)."""

    __tablename__ = "london_stations"
    __table_args__ = (Index("idx_prod_london_stations_zone", "zone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    station: Mapped[str] = mapped_column(Text, nullable=False)
    os_x: Mapped[int] = mapped_column(Integer, nullable=True)
    os_y: Mapped[int] = mapped_column(Integer, nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    zone: Mapped[str] = mapped_column(Text, nullable=True)
    postcode: Mapped[str] = mapped_column(Text, nullable=True)


class Ukhpi(ProductionBase):
    """
    UK House Price Index for London, monthly from 1995, 378 rows.

    Attributes
    ----------
    published_date : date
        When the period's index was published (the third Wednesday of the
        month after next), not the period itself. The view joins on
        ``published_date <= dateoftransfer`` so a sale never sees a future
        index.
    """

    __tablename__ = "ukhpi"

    period: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    avg_price_all_property_types: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    hpi_all_property_types: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    pct_change_yearly_all_property_types: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    sales_volume_all_property_types: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    avg_price_detached: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hpi_detached: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    avg_price_semi_detached: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hpi_semi_detached: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    avg_price_terraced: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hpi_terraced: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    avg_price_flats_maisonettes: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hpi_flats_maisonettes: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    published_date: Mapped[date] = mapped_column(Date, nullable=True)


class MortgageRates(ProductionBase):
    """
    Bank of England quoted mortgage rates by loan-to-value band, 235 rows.

    ``published_date`` carries the same reporting lag as `Ukhpi.published_date`.
    """

    __tablename__ = "mortgage_rates"

    rate_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    rate_2y_95ltv: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    rate_2y_90ltv: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    rate_2y_75ltv: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    rate_2y_60ltv: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    rate_2y_85ltv: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    published_date: Mapped[date] = mapped_column(Date, nullable=True)


# Per-transaction feature tables: one row per PPD.id, unique on ppd_id, no
# primary key. Precomputed so each can be rebuilt without the view's other joins.


class CompsFeatures(ProductionBase):
    """
    Comparable-sale statistics, built by migration 0014.

    The median price and price per m² of the 5 nearest other sales of the same
    type within 2000 m in the 12 months strictly before each sale, so it is
    leakage-safe. ``comp_txn_count`` below 5 signals sparsity.
    """

    __tablename__ = "comps_features"
    __table_args__ = (Index("idx_prod_comps_features_ppd_id", "ppd_id", unique=True),)

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    comp_median_price: Mapped[float] = mapped_column(Float, nullable=True)
    comp_median_price_per_sqm: Mapped[float] = mapped_column(Float, nullable=True)
    comp_txn_count: Mapped[int] = mapped_column(BigInteger, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class NeighbourhoodFeatures(ProductionBase):
    """
    Local price and housing-stock statistics, built by migration 0014.

    Spatial-lag and local-median prices, the property's floor-area percentile
    against its neighbours, and the local EPC band mix.
    """

    __tablename__ = "neighbourhood_features"
    __table_args__ = (
        Index("idx_prod_neighbourhood_features_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    spatial_lag_price: Mapped[float] = mapped_column(Float, nullable=True)
    median_price_per_sqm_20nn_6m: Mapped[float] = mapped_column(Float, nullable=True)
    recency_weighted_price: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    proptype_local_median_800m_6m: Mapped[float] = mapped_column(Float, nullable=True)
    floor_area_percentile_local: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_a: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_b: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_c: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_d: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_e: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_f: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_g: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class StationProximityFeatures(ProductionBase):
    """Nearest station name, fare zone and distance, built by migration 0013."""

    __tablename__ = "station_proximity_features"
    __table_args__ = (
        Index("idx_prod_station_proximity_features_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    nearest_station_name: Mapped[str] = mapped_column(Text, nullable=True)
    nearest_station_zone: Mapped[int] = mapped_column(Integer, nullable=True)
    nearest_station_distance_km: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class RiverProximityFeatures(ProductionBase):
    """Distance in metres to the nearest watercourse within the search radius (migration 0013)."""

    __tablename__ = "river_proximity_features"
    __table_args__ = (
        Index("idx_prod_river_proximity_features_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    distance_to_river_m: Mapped[int] = mapped_column(SmallInteger, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class PtalFeatures(ProductionBase):
    """
    TfL Public Transport Accessibility Level per transaction, built by migration 0012.

    ``ptal_band_rank`` ordinally encodes the bands '0', '1a', '1b', '2'..'6b';
    ``ptal_ai`` is the raw accessibility index.
    """

    __tablename__ = "ptal_features"
    __table_args__ = (Index("idx_prod_ptal_features_ppd_id", "ppd_id", unique=True),)

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    ptal_band_rank: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    ptal_ai: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class LsoaMsoaFeatures(ProductionBase):
    """
    The 2011 LSOA and MSOA a transaction sits in, built by migration 0011.

    Point-in-polygon assignment with a 150 m nearest-boundary fallback.
    """

    __tablename__ = "lsoa_msoa_features"
    __table_args__ = (
        Index("idx_prod_lsoa_msoa_features_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    lsoa_code: Mapped[str] = mapped_column(String, nullable=True)
    lsoa_name: Mapped[str] = mapped_column(String, nullable=True)
    msoa_code: Mapped[str] = mapped_column(String, nullable=True)
    msoa_name: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class LsoaPriceTrendFeatures(ProductionBase):
    """
    MSOA price momentum, built by migration 0015.

    The ratio of an MSOA's median price in the twelve months before a sale to
    its median one or five years earlier; LSOAs are too thin for a stable
    median, but the table keeps its ``lsoa`` name. NULL where the area had no
    qualifying sale.
    """

    __tablename__ = "lsoa_price_trend_features"
    __table_args__ = (
        Index("idx_prod_lsoa_price_trend_features_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    msoa_price_momentum_1y: Mapped[float] = mapped_column(Float, nullable=True)
    msoa_price_momentum_5y: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class PrevSaleFeatures(ProductionBase):
    """
    The same property's previous transaction, built by migration 0015.

    A LAG window partitioned by UPRN, over sales at or above
    ``MIN_PLAUSIBLE_PRICE`` so nominal £1 transfers never become a previous
    sale price.
    """

    __tablename__ = "prev_sale_features"
    __table_args__ = (
        Index("idx_prod_prev_sale_features_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    prev_sale_price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    prev_sale_date: Mapped[date] = mapped_column(Date, nullable=True)
    years_since_prev_sale: Mapped[Decimal] = mapped_column(Numeric, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class IodFeatures(ProductionBase):
    """
    Indices of Deprivation as of the sale date, built by migration 0011.

    Joined on publication date, not release year, so a sale sees only
    releases already published. NULL for sales before the 2010 release.
    """

    __tablename__ = "iod_features"
    __table_args__ = (Index("idx_prod_iod_features_ppd_id", "ppd_id", unique=True),)

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    iod_release_year: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    iod_release_age_years: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_decile_national: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    iod_income_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_income_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_employment_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_education_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_education_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_health_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_crime_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_wider_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_geo_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_living_env_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_indoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_outdoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_outdoors_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_idaci_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_idaopi_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_children_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_adult_skills_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_score_vs_lad: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_contrast_1km: Mapped[float] = mapped_column(Float, nullable=True)
    iod_income_contrast_1km: Mapped[float] = mapped_column(Float, nullable=True)
    iod_prev_release_year: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    iod_imd_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_income_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_education_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_living_env_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_pct_london_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_years_between_releases: Mapped[float] = mapped_column(Float, nullable=True)
    # True where the sale predates every release in force and took the earliest (0025).
    iod_backfilled: Mapped[bool] = mapped_column(Boolean, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class OfstedFeatures(ProductionBase):
    """
    Ofsted ratings of the schools near each sale, as published on its date (migration 0034).

    Definitions in `hpldn.backend.scripts.ofsted_features`; read from
    ``production.ofsted_rating_spells`` (migration 0033).
    """

    __tablename__ = "ofsted_features"
    __table_args__ = (Index("idx_prod_ofsted_features_ppd_id", "ppd_id", unique=True),)

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ofsted_primary_nearest_m: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_primary_nearest_score: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_primary_nearest_age_years: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_nearest_m: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_nearest_score: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_nearest_age_years: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_primary_good_plus_1km: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ofsted_primary_outstanding_1km: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ofsted_secondary_good_plus_2km: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ofsted_primary_idw_score_1500m: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_idw_score_3km: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_nearest_primary_basis: Mapped[str] = mapped_column(String, nullable=True)
    ofsted_primary_spell_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    ofsted_secondary_spell_id: Mapped[int] = mapped_column(BigInteger, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class Ofsted(ProductionBase):
    """
    Ofsted state-funded school inspections 2005-2026 near London (migration 0033).

    One row per inspection with a harmonised ``rating_score`` (1 best to 4)
    and its ``grade_basis``. The PostGIS ``geom`` column (British National
    Grid) is not mapped: nothing reads it through the ORM, and the serving
    dump leaves it out.
    """

    __tablename__ = "ofsted"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    urn: Mapped[int] = mapped_column(Integer, nullable=False)
    inspection_number: Mapped[str] = mapped_column(String, nullable=True)
    inspection_type: Mapped[str] = mapped_column(String, nullable=False)
    inspection_type_raw: Mapped[str] = mapped_column(String, nullable=True)
    era: Mapped[str] = mapped_column(String, nullable=False)
    inspection_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    inspection_end_date: Mapped[date] = mapped_column(Date, nullable=True)
    publication_date: Mapped[date] = mapped_column(Date, nullable=False)
    publication_date_imputed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    overall_effectiveness: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    quality_of_education: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    behaviour_and_attitudes: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    personal_development: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    leadership_and_management: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    rc_inclusion: Mapped[float] = mapped_column(Numeric(2, 1), nullable=True)
    rc_curriculum_and_teaching: Mapped[float] = mapped_column(Numeric(2, 1), nullable=True)
    rc_achievement: Mapped[float] = mapped_column(Numeric(2, 1), nullable=True)
    rc_attendance_and_behaviour: Mapped[float] = mapped_column(Numeric(2, 1), nullable=True)
    rc_personal_development: Mapped[float] = mapped_column(Numeric(2, 1), nullable=True)
    rc_leadership_and_governance: Mapped[float] = mapped_column(Numeric(2, 1), nullable=True)
    safeguarding_met: Mapped[bool] = mapped_column(Boolean, nullable=True)
    category_of_concern: Mapped[str] = mapped_column(String, nullable=True)
    ungraded_outcome: Mapped[str] = mapped_column(String, nullable=True)
    ungraded_outcome_raw: Mapped[str] = mapped_column(String, nullable=True)
    rating_score: Mapped[float] = mapped_column(Numeric(3, 2), nullable=True)
    grade_basis: Mapped[str] = mapped_column(String, nullable=True)
    rating_label: Mapped[str] = mapped_column(String, nullable=True)
    school_name: Mapped[str] = mapped_column(String, nullable=True)
    phase: Mapped[str] = mapped_column(String, nullable=True)
    establishment_type: Mapped[str] = mapped_column(String, nullable=True)
    la_code: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    la_name: Mapped[str] = mapped_column(String, nullable=True)
    postcode: Mapped[str] = mapped_column(String, nullable=True)
    open_date: Mapped[date] = mapped_column(Date, nullable=True)
    close_date: Mapped[date] = mapped_column(Date, nullable=True)
    statutory_low_age: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    statutory_high_age: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    location_source: Mapped[str] = mapped_column(String, nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)
    origin: Mapped[str] = mapped_column(String, nullable=False)
    source_file: Mapped[str] = mapped_column(String, nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class OfstedRatingSpells(ProductionBase):
    """
    The Ofsted rating in force at each school over time (migration 0033).

    From the report's publication date (``valid_from``) to the next report,
    closure or a successor's own first report (``valid_to``, exclusive). The
    PostGIS ``geom`` column is not mapped (see `Ofsted`).
    """

    __tablename__ = "ofsted_rating_spells"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    urn: Mapped[int] = mapped_column(Integer, nullable=False)
    ofsted_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=True)
    school_name: Mapped[str] = mapped_column(String, nullable=True)
    la_code: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=True)
    lon: Mapped[float] = mapped_column(Float, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=True)
    rating_score: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    grade_basis: Mapped[str] = mapped_column(String, nullable=False)
    rating_label: Mapped[str] = mapped_column(String, nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, nullable=False)
    publication_date: Mapped[date] = mapped_column(Date, nullable=False)
    inherited_from_urn: Mapped[int] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)


# The serving view, which both models train on.


class LondonHouses(ProductionBase):
    """
    Materialized view with one row per Price Paid transaction.

    107 columns, built by migration 0034 from
    `hpldn.backend.migrations.london_houses_view.CREATE_VIEW_SQL` (0016 and
    0020 built the 93-column `CREATE_VIEW_SQL_0020`, 0025-0029 the
    95-column `CREATE_VIEW_SQL_0025`). The unique
    ``ppd_id`` index enables the supported refresh::

        REFRESH MATERIALIZED VIEW CONCURRENTLY production.london_houses

    followed by a rebuild of ``production.address_search`` (migration 0021),
    ``python -m hpldn.backend.migrations.address_search``.

    Attributes
    ----------
    epc_matched : bool
        True where the sale has a usable pre-sale certificate. Migration 0016
        asserts exactly 891,187 such rows.
    street_key : str
        The same-street group key, ``production.hpldn_street_key(poststreet)``.
    epc_after_sale : bool
        True where the time-invariant certificate fields came from the
        nearest certificate inspected after the sale (migration 0025).
    ofsted_* : float / str
        The twelve school columns of ``production.ofsted_features`` (0034).
    """

    __tablename__ = "london_houses"
    __table_args__ = (
        Index("idx_prod_london_houses_dateoftransfer", "dateoftransfer"),
        Index("idx_prod_london_houses_epc_matched", "epc_matched"),
        Index("idx_prod_london_houses_iod_release_year", "iod_release_year"),
        Index("idx_prod_london_houses_postcode_district", "postcode_district"),
        Index("idx_prod_london_houses_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=True)
    propertytype: Mapped[str] = mapped_column(CHAR, nullable=True)
    duration: Mapped[str] = mapped_column(CHAR, nullable=True)
    oldnew: Mapped[str] = mapped_column(CHAR, nullable=True)
    postcode_district: Mapped[str] = mapped_column(Text, nullable=True)
    postcode_sector: Mapped[str] = mapped_column(Text, nullable=True)
    poststreet: Mapped[str] = mapped_column(String, nullable=True)
    construction_age_band: Mapped[str] = mapped_column(String, nullable=True)
    number_habitable_rooms: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    current_energy_rating: Mapped[str] = mapped_column(CHAR, nullable=True)
    total_floor_area: Mapped[float] = mapped_column(Float, nullable=True)
    build_form: Mapped[str] = mapped_column(String(20), nullable=True)
    mains_gas_flag: Mapped[str] = mapped_column(CHAR, nullable=True)
    tenure: Mapped[str] = mapped_column(CHAR, nullable=True)
    multi_glaze_proportion: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    extension_count: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    floor_level: Mapped[str] = mapped_column(String, nullable=True)
    epc_age_at_sale_days: Mapped[int] = mapped_column(Integer, nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)
    lot_area_sqm: Mapped[float] = mapped_column(Float, nullable=True)
    comp_median_price: Mapped[float] = mapped_column(Float, nullable=True)
    comp_median_price_per_sqm: Mapped[float] = mapped_column(Float, nullable=True)
    comp_txn_count: Mapped[int] = mapped_column(BigInteger, nullable=True)
    nearest_station_distance_km: Mapped[float] = mapped_column(Float, nullable=True)
    nearest_station_zone: Mapped[int] = mapped_column(Integer, nullable=True)
    boe_rate_at_sale: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    ukhpi_avg_price_at_sale: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    ptal_band_rank: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    ptal_ai: Mapped[float] = mapped_column(Float, nullable=True)
    distance_to_river: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    lsoa_code: Mapped[str] = mapped_column(String, nullable=True)
    lsoa_name: Mapped[str] = mapped_column(String, nullable=True)
    msoa_code: Mapped[str] = mapped_column(String, nullable=True)
    msoa_name: Mapped[str] = mapped_column(String, nullable=True)
    build_form_propertytype: Mapped[str] = mapped_column(Text, nullable=True)
    construction_age_band_propertytype: Mapped[str] = mapped_column(Text, nullable=True)
    is_maisonette: Mapped[bool] = mapped_column(Boolean, nullable=True)
    prev_sale_price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    prev_sale_date: Mapped[date] = mapped_column(Date, nullable=True)
    years_since_prev_sale: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    spatial_lag_price: Mapped[float] = mapped_column(Float, nullable=True)
    median_price_per_sqm_20nn_6m: Mapped[float] = mapped_column(Float, nullable=True)
    recency_weighted_price: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    proptype_local_median_800m_6m: Mapped[float] = mapped_column(Float, nullable=True)
    floor_area_percentile_local: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_a: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_b: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_c: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_d: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_e: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_f: Mapped[float] = mapped_column(Float, nullable=True)
    epc_band_share_g: Mapped[float] = mapped_column(Float, nullable=True)
    msoa_price_momentum_1y: Mapped[float] = mapped_column(Float, nullable=True)
    msoa_price_momentum_5y: Mapped[float] = mapped_column(Float, nullable=True)
    iod_release_year: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    iod_release_age_years: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_decile_national: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    iod_income_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_income_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_employment_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_education_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_education_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_health_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_crime_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_wider_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_geo_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_living_env_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_indoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_outdoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_outdoors_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    iod_idaci_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_idaopi_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_children_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_adult_skills_score: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_score_vs_lad: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_contrast_1km: Mapped[float] = mapped_column(Float, nullable=True)
    iod_income_contrast_1km: Mapped[float] = mapped_column(Float, nullable=True)
    iod_prev_release_year: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    iod_imd_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_income_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_education_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_living_env_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_imd_pct_london_change: Mapped[float] = mapped_column(Float, nullable=True)
    iod_years_between_releases: Mapped[float] = mapped_column(Float, nullable=True)
    epc_matched: Mapped[bool] = mapped_column(Boolean, nullable=True)
    street_key: Mapped[str] = mapped_column(String, nullable=True)
    epc_after_sale: Mapped[bool] = mapped_column(Boolean, nullable=True)
    ofsted_primary_nearest_m: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_primary_nearest_score: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_primary_nearest_age_years: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_nearest_m: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_nearest_score: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_nearest_age_years: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_primary_good_plus_1km: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    ofsted_primary_outstanding_1km: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    ofsted_secondary_good_plus_2km: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    ofsted_primary_idw_score_1500m: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_secondary_idw_score_3km: Mapped[float] = mapped_column(Float, nullable=True)
    ofsted_nearest_primary_basis: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}
