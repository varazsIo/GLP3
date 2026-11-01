"""
Declarative models for the ``staging`` schema.

`AppBase` is the ORM spine: tables promoted to ``production`` by migration
0007, bound to ``DB_SCHEMA`` so the read API can serve the promoted copies,
and created only by `migration_metadata` in migration 0001. `StagingBase` is
every staging-only relation, pinned to the literal ``staging`` and built by
raw DDL, so ``staging_meta.create_all()`` is never called. See
``models/README.md``.
"""

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, List
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, DeclarativeBase, declared_attr
from sqlalchemy.orm import relationship, mapped_column
from sqlalchemy.types import (
    Integer,
    SmallInteger,
    BigInteger,
    Boolean,
    Float,
    Numeric,
    String,
    Text,
    Date,
    DateTime,
    Uuid,
    CHAR,
    UserDefinedType,
)

# DB_SCHEMA only redirects the read path; the migrations always build
# MIGRATION_SCHEMA through migration_metadata().
MIGRATION_SCHEMA = "staging"

app_base_meta = MetaData(schema=os.getenv("DB_SCHEMA", MIGRATION_SCHEMA))


class AppBase(DeclarativeBase):
    """Declarative base for the promoted spine, bound to ``DB_SCHEMA``."""

    metadata = app_base_meta


class UKHPUCL(AppBase):
    """The UCL UK House Prices 2024 linked PPD and EPC dataset, as loaded."""

    __tablename__ = "ukhpucl"
    id: Mapped[Integer] = mapped_column(
        Integer,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
        name="id",
        autoincrement=True,
    )
    postcode: Mapped[String] = mapped_column(String, name="postcode", nullable=True)
    transactionid: Mapped[String] = mapped_column(String, name="transactionid")
    price: Mapped[BigInteger] = mapped_column(BigInteger, name="price", nullable=True)
    dateoftransfer: Mapped[String] = mapped_column(
        String, name="dateoftransfer", nullable=True
    )
    propertytype: Mapped[String] = mapped_column(
        String, name="propertytype", nullable=True
    )
    oldnew: Mapped[String] = mapped_column(String, name="oldnew", nullable=True)
    duration: Mapped[String] = mapped_column(String, name="duration", nullable=True)
    paon: Mapped[String] = mapped_column(String, name="paon", nullable=True)
    saon: Mapped[String] = mapped_column(String, name="saon", nullable=True)
    street: Mapped[String] = mapped_column(String, name="street", nullable=True)
    locality: Mapped[String] = mapped_column(String, name="locality", nullable=True)
    towncity: Mapped[String] = mapped_column(String, name="towncity", nullable=True)
    district: Mapped[String] = mapped_column(String, name="district", nullable=True)
    county: Mapped[String] = mapped_column(String, name="county", nullable=True)
    categorytype: Mapped[String] = mapped_column(
        String, name="categorytype", nullable=True
    )
    recordstatus: Mapped[String] = mapped_column(
        String, name="recordstatus", nullable=True
    )
    lmk_key: Mapped[String] = mapped_column(String, name="LMK_KEY", nullable=True)
    classt: Mapped[BigInteger] = mapped_column(BigInteger, name="classt", nullable=True)
    lad23cd: Mapped[String] = mapped_column(String, name="lad23cd", nullable=True)
    building_reference_number: Mapped[Integer] = mapped_column(
        BigInteger, name="BUILDING_REFERENCE_NUMBER", nullable=True
    )
    current_energy_rating: Mapped[String] = mapped_column(
        String, name="CURRENT_ENERGY_RATING", nullable=True
    )
    potential_energy_rating: Mapped[String] = mapped_column(
        String, name="POTENTIAL_ENERGY_RATING", nullable=True
    )
    current_energy_efficiency: Mapped[Integer] = mapped_column(
        Integer, name="CURRENT_ENERGY_EFFICIENCY", nullable=True
    )
    potential_energy_efficiency: Mapped[Integer] = mapped_column(
        Integer, name="POTENTIAL_ENERGY_EFFICIENCY", nullable=True
    )
    property_type: Mapped[String] = mapped_column(
        String, name="PROPERTY_TYPE", nullable=True
    )
    built_form: Mapped[String] = mapped_column(String, name="BUILT_FORM", nullable=True)
    inspection_date: Mapped[String] = mapped_column(
        String, name="INSPECTION_DATE", nullable=True
    )
    local_authority: Mapped[String] = mapped_column(
        String, name="LOCAL_AUTHORITY", nullable=True
    )
    constituency: Mapped[String] = mapped_column(
        String, name="CONSTITUENCY", nullable=True
    )
    county_epc: Mapped[String] = mapped_column(String, name="COUNTY", nullable=True)
    lodgement_date: Mapped[String] = mapped_column(
        String, name="LODGEMENT_DATE", nullable=True
    )
    transaction_type: Mapped[String] = mapped_column(
        String, name="TRANSACTION_TYPE", nullable=True
    )
    environment_impact_current: Mapped[Integer] = mapped_column(
        Integer, name="ENVIRONMENT_IMPACT_CURRENT", nullable=True
    )
    environment_impact_potential: Mapped[Integer] = mapped_column(
        Integer, name="ENVIRONMENT_IMPACT_POTENTIAL", nullable=True
    )
    energy_consumption_current: Mapped[Integer] = mapped_column(
        Integer, name="ENERGY_CONSUMPTION_CURRENT", nullable=True
    )
    energy_consumption_potential: Mapped[Integer] = mapped_column(
        Integer, name="ENERGY_CONSUMPTION_POTENTIAL", nullable=True
    )
    co2_emissions_current: Mapped[Float] = mapped_column(
        Float, name="CO2_EMISSIONS_CURRENT", nullable=True
    )
    co2_emiss_curr_per_floor_area: Mapped[Integer] = mapped_column(
        Integer, name="CO2_EMISS_CURR_PER_FLOOR_AREA", nullable=True
    )
    co2_emissions_potential: Mapped[Float] = mapped_column(
        Float, name="CO2_EMISSIONS_POTENTIAL", nullable=True
    )
    lighting_cost_current: Mapped[Integer] = mapped_column(
        Integer, name="LIGHTING_COST_CURRENT", nullable=True
    )
    lighting_cost_potential: Mapped[Integer] = mapped_column(
        Integer, name="LIGHTING_COST_POTENTIAL", nullable=True
    )
    heating_cost_current: Mapped[Integer] = mapped_column(
        Integer, name="HEATING_COST_CURRENT", nullable=True
    )
    heating_cost_potential: Mapped[Integer] = mapped_column(
        Integer, name="HEATING_COST_POTENTIAL", nullable=True
    )
    hot_water_cost_current: Mapped[Integer] = mapped_column(
        Integer, name="HOT_WATER_COST_CURRENT", nullable=True
    )
    hot_water_cost_potential: Mapped[Integer] = mapped_column(
        Integer, name="HOT_WATER_COST_POTENTIAL", nullable=True
    )
    total_floor_area: Mapped[Float] = mapped_column(
        Float, name="TOTAL_FLOOR_AREA", nullable=True
    )
    energy_tariff: Mapped[String] = mapped_column(
        String, name="ENERGY_TARIFF", nullable=True
    )
    mains_gas_flag: Mapped[String] = mapped_column(
        String, name="MAINS_GAS_FLAG", nullable=True
    )
    floor_level: Mapped[String] = mapped_column(
        String, name="FLOOR_LEVEL", nullable=True
    )
    flat_top_storey: Mapped[String] = mapped_column(
        String, name="FLAT_TOP_STOREY", nullable=True
    )
    flat_storey_count: Mapped[Integer] = mapped_column(
        Integer, name="FLAT_STOREY_COUNT", nullable=True
    )
    main_heating_controls: Mapped[String] = mapped_column(
        String, name="MAIN_HEATING_CONTROLS", nullable=True
    )
    multi_glaze_proportion: Mapped[Integer] = mapped_column(
        Integer, name="MULTI_GLAZE_PROPORTION", nullable=True
    )
    glazed_type: Mapped[String] = mapped_column(
        String, name="GLAZED_TYPE", nullable=True
    )
    glazed_area: Mapped[String] = mapped_column(
        String, name="GLAZED_AREA", nullable=True
    )
    extension_count: Mapped[Integer] = mapped_column(
        Integer, name="EXTENSION_COUNT", nullable=True
    )
    number_habitable_rooms: Mapped[Integer] = mapped_column(
        Integer, name="NUMBER_HABITABLE_ROOMS", nullable=True
    )
    number_heated_rooms: Mapped[Integer] = mapped_column(
        Integer, name="NUMBER_HEATED_ROOMS", nullable=True
    )
    low_energy_lighting: Mapped[Integer] = mapped_column(
        Integer, name="LOW_ENERGY_LIGHTING", nullable=True
    )
    number_open_fireplaces: Mapped[Integer] = mapped_column(
        Integer, name="NUMBER_OPEN_FIREPLACES", nullable=True
    )
    hotwater_description: Mapped[String] = mapped_column(
        String, name="HOTWATER_DESCRIPTION", nullable=True
    )
    hot_water_energy_eff: Mapped[String] = mapped_column(
        String, name="HOT_WATER_ENERGY_EFF", nullable=True
    )
    hot_water_env_eff: Mapped[String] = mapped_column(
        String, name="HOT_WATER_ENV_EFF", nullable=True
    )
    floor_description: Mapped[String] = mapped_column(
        String, name="FLOOR_DESCRIPTION", nullable=True
    )
    floor_energy_eff: Mapped[String] = mapped_column(
        String, name="FLOOR_ENERGY_EFF", nullable=True
    )
    floor_env_eff: Mapped[String] = mapped_column(
        String, name="FLOOR_ENV_EFF", nullable=True
    )
    windows_description: Mapped[String] = mapped_column(
        String, name="WINDOWS_DESCRIPTION", nullable=True
    )
    windows_energy_eff: Mapped[String] = mapped_column(
        String, name="WINDOWS_ENERGY_EFF", nullable=True
    )
    windows_env_eff: Mapped[String] = mapped_column(
        String, name="WINDOWS_ENV_EFF", nullable=True
    )
    walls_description: Mapped[String] = mapped_column(
        String, name="WALLS_DESCRIPTION", nullable=True
    )
    walls_energy_eff: Mapped[String] = mapped_column(
        String, name="WALLS_ENERGY_EFF", nullable=True
    )
    walls_env_eff: Mapped[String] = mapped_column(
        String, name="WALLS_ENV_EFF", nullable=True
    )
    secondheat_description: Mapped[String] = mapped_column(
        String, name="SECONDHEAT_DESCRIPTION", nullable=True
    )
    sheating_energy_eff: Mapped[String] = mapped_column(
        String, name="SHEATING_ENERGY_EFF", nullable=True
    )
    sheating_env_eff: Mapped[String] = mapped_column(
        String, name="SHEATING_ENV_EFF", nullable=True
    )
    roof_description: Mapped[String] = mapped_column(
        String, name="ROOF_DESCRIPTION", nullable=True
    )
    roof_energy_eff: Mapped[String] = mapped_column(
        String, name="ROOF_ENERGY_EFF", nullable=True
    )
    roof_env_eff: Mapped[String] = mapped_column(
        String, name="ROOF_ENV_EFF", nullable=True
    )
    mainheat_description: Mapped[String] = mapped_column(
        String, name="MAINHEAT_DESCRIPTION", nullable=True
    )
    mainheat_energy_eff: Mapped[String] = mapped_column(
        String, name="MAINHEAT_ENERGY_EFF", nullable=True
    )
    mainheat_env_eff: Mapped[String] = mapped_column(
        String, name="MAINHEAT_ENV_EFF", nullable=True
    )
    mainheatcont_description: Mapped[String] = mapped_column(
        String, name="MAINHEATCONT_DESCRIPTION", nullable=True
    )
    mainheatc_energy_eff: Mapped[String] = mapped_column(
        String, name="MAINHEATC_ENERGY_EFF", nullable=True
    )
    mainheatc_env_eff: Mapped[String] = mapped_column(
        String, name="MAINHEATC_ENV_EFF", nullable=True
    )
    lighting_description: Mapped[String] = mapped_column(
        String, name="LIGHTING_DESCRIPTION", nullable=True
    )
    lighting_energy_eff: Mapped[String] = mapped_column(
        String, name="LIGHTING_ENERGY_EFF", nullable=True
    )
    lighting_env_eff: Mapped[String] = mapped_column(
        String, name="LIGHTING_ENV_EFF", nullable=True
    )
    main_fuel: Mapped[String] = mapped_column(String, name="MAIN_FUEL", nullable=True)
    wind_turbine_count: Mapped[Integer] = mapped_column(
        Integer, name="WIND_TURBINE_COUNT", nullable=True
    )
    heat_loss_corridor: Mapped[String] = mapped_column(
        String, name="HEAT_LOSS_CORRIDOR", nullable=True
    )
    unheated_corridor_length: Mapped[Float] = mapped_column(
        Float, name="UNHEATED_CORRIDOR_LENGTH", nullable=True
    )
    floor_height: Mapped[Float] = mapped_column(
        Float, name="FLOOR_HEIGHT", nullable=True
    )
    photo_supply: Mapped[Integer] = mapped_column(
        Integer, name="PHOTO_SUPPLY", nullable=True
    )
    solar_water_heating_flag: Mapped[String] = mapped_column(
        String, name="SOLAR_WATER_HEATING_FLAG", nullable=True
    )
    mechanical_ventilation: Mapped[String] = mapped_column(
        String, name="MECHANICAL_VENTILATION", nullable=True
    )
    local_authority_label: Mapped[String] = mapped_column(
        String, name="LOCAL_AUTHORITY_LABEL", nullable=True
    )
    constituency_label: Mapped[String] = mapped_column(
        String, name="CONSTITUENCY_LABEL", nullable=True
    )
    posttown: Mapped[String] = mapped_column(String, name="POSTTOWN")
    construction_age_band: Mapped[String] = mapped_column(
        String, name="CONSTRUCTION_AGE_BAND", nullable=True
    )
    lodgement_datetime: Mapped[DateTime] = mapped_column(
        DateTime(timezone=False), name="LODGEMENT_DATETIME", nullable=True
    )
    tenure: Mapped[String] = mapped_column(String, name="TENURE", nullable=True)
    fixed_lighting_outlets_count: Mapped[Integer] = mapped_column(
        Integer, name="FIXED_LIGHTING_OUTLETS_COUNT", nullable=True
    )
    low_energy_fixed_light_count: Mapped[Integer] = mapped_column(
        Integer, name="LOW_ENERGY_FIXED_LIGHT_COUNT", nullable=True
    )
    uprn: Mapped[BigInteger] = mapped_column(BigInteger, name="UPRN", nullable=True)
    uprn_source: Mapped[String] = mapped_column(
        String, name="UPRN_SOURCE", nullable=True
    )
    priceper: Mapped[Float] = mapped_column(Float, name="priceper", nullable=True)


class UKHC(AppBase):
    """UK housing coordinates: OS Open UPRN latitude, longitude and BNG position per UPRN."""

    __tablename__ = "ukhcs_table"
    id: Mapped[BigInteger] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
        name="id",
        autoincrement=True,
    )
    uprn: Mapped[BigInteger] = mapped_column(
        BigInteger, name="UPRN", nullable=False, unique=True
    )
    latitude: Mapped[Float] = mapped_column(Float, nullable=False, name="LATITUDE")
    longitude: Mapped[Float] = mapped_column(Float, nullable=False, name="LONGITUDE")
    x_coordinate: Mapped[Float] = mapped_column(
        Float, nullable=False, name="X_COORDINATE"
    )
    y_coordinate: Mapped[Float] = mapped_column(
        Float, nullable=False, name="Y_COORDINATE"
    )


class Address(AppBase):
    """A normalised UK postal address, one row per UPRN."""

    __tablename__ = "address_table"
    id: Mapped[BigInteger] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
        name="id",
        autoincrement=True,
    )
    uid: Mapped[BigInteger] = mapped_column(
        ForeignKey("ukhcs_table.id"), nullable=False, index=True, unique=True
    )
    postnumber: Mapped[String] = mapped_column(
        String, nullable=False, name="postnumber"
    )
    posttown: Mapped[String] = mapped_column(String, nullable=False, name="posttown")
    postcode: Mapped[String] = mapped_column(String(8), nullable=False, name="postcode")
    postcounty: Mapped[String] = mapped_column(
        String, nullable=False, name="postcounty"
    )
    poststreet: Mapped[String] = mapped_column(String, nullable=True, name="poststreet")
    # The flat within the building ("FLAT 3"), the register's SAON (0030).
    unit: Mapped[String] = mapped_column(String, nullable=True, name="unit")
    ppd_history: Mapped[List["PPD"]] = relationship(
        back_populates="address", cascade="all, delete-orphan"
    )
    epc_history: Mapped[List["EPC"]] = relationship(
        back_populates="address", cascade="all, delete-orphan"
    )


class PPD(AppBase):
    """
    Price Paid transactions, reduced from the original PPD record.

    Attributes
    ----------
    propertytype : str
        D = Detached, S = Semi-Detached, T = Terraced, F = Flat or maisonette.
    transactionid : str
        HM Land Registry's Price Paid transaction id (unique where set),
        back-filled by migration 0027 from the evidence that placed the sale.
    link_method : str
        How the sale was linked to its UPRN (``ucl``, ``epc_t1``..``epc_t4``,
        ``epc_matcher``, and 0028's methods; see
        ``migrations/sale_linking.py``).
    oldnew : str
        ``'Y'`` for the first sale of a new build, else ``'N'``. Backfilled by
        migration 0008 on (postcode, price, dateoftransfer); NULL where source
        rows disagree. Declared on the model, not ALTERed onto production,
        because each promote rebuilds production from this table.
    """

    __tablename__ = "ppd_table"
    __table_args__ = (
        CheckConstraint(
            "propertytype IN ('D','S','T','F')", name="ck_ppd_propertytype"
        ),
        CheckConstraint("duration IN ('F','L')", name="ck_ppd_duration"),
        CheckConstraint("price > 0", name="ck_ppd_price_positive"),
        UniqueConstraint(
            "address_id",
            "dateoftransfer",
            "price",
            name="uq_ppd_table_transaction",
        ),
    )
    id: Mapped[BigInteger] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
        name="id",
        autoincrement=True,
    )
    address_id: Mapped[BigInteger] = mapped_column(
        ForeignKey("address_table.id"), nullable=True, index=True
    )
    price: Mapped[BigInteger] = mapped_column(BigInteger, nullable=False, name="price")
    dateoftransfer: Mapped[Date] = mapped_column(
        Date, nullable=False, name="dateoftransfer"
    )
    propertytype: Mapped[CHAR] = mapped_column(
        CHAR, nullable=False, name="propertytype"
    )
    duration: Mapped[CHAR] = mapped_column(CHAR, nullable=False, name="duration")
    oldnew: Mapped[CHAR] = mapped_column(CHAR, nullable=True, name="oldnew")
    transactionid: Mapped[String] = mapped_column(String, nullable=True, name="transactionid")
    link_method: Mapped[String] = mapped_column(String, nullable=True, name="link_method")
    address: Mapped["Address"] = relationship(back_populates="ppd_history")


class RatingCategory(AppBase):
    """
    Rating category vocabulary.

    Very Poor = 1, Poor = 2, Average = 3, Good = 4, Very Good = 5.
    """

    __tablename__ = "rating_category_table"
    id: Mapped[SmallInteger] = mapped_column(
        SmallInteger, primary_key=True, nullable=False, name="id"
    )
    rating_category: Mapped[String] = mapped_column(
        String(9), nullable=False, unique=True, name="rating_category"
    )


class BuildFormCategory(AppBase):
    """
    Build-form vocabulary, the reverse mapping of `EPC.build_form`.

    Detached = 1, Enclosed End-Terrace = 2, Enclosed Mid-Terrace = 3,
    End-Terrace = 4, Mid-Terrace = 5, Semi-Detached = 6.
    """

    __tablename__ = "build_form_category_table"
    id: Mapped[SmallInteger] = mapped_column(
        SmallInteger, primary_key=True, nullable=False, name="id"
    )
    build_form: Mapped[String] = mapped_column(
        String(20), nullable=False, unique=True, name="build_form"
    )


class TransactionTypeCategory(AppBase):
    """
    Transaction-type vocabulary, the reverse mapping of `EPC.transaction_type`.

    assessment for green deal = 1, ECO assessment = 2, FiT application = 3,
    following green deal = 4, marketed sale = 5, new dwelling = 6, non
    marketed sale = 7, rental = 8, rental (private) = 9, rental (social) = 10,
    RHI application = 11, Stock condition survey = 12.
    """

    __tablename__ = "transaction_type_category_table"
    id: Mapped[SmallInteger] = mapped_column(
        SmallInteger, primary_key=True, nullable=False, name="id"
    )
    transaction_type: Mapped[String] = mapped_column(
        String(25), nullable=False, unique=True, name="transaction_type"
    )


class EnergyTariffCategory(AppBase):
    """
    Energy-tariff vocabulary, the reverse mapping of `EPC.energy_tariff`.

    24 hour = 1, dual = 2, dual (24 hour) = 3, off-peak 10 hour = 4,
    off-peak 18 hour = 5, off-peak 7 hour = 6, Single = 7,
    standard tariff = 8, Unknown = 9.
    """

    __tablename__ = "energy_tariff_category_table"
    id: Mapped[SmallInteger] = mapped_column(
        SmallInteger, primary_key=True, nullable=False, name="id"
    )
    energy_tariff: Mapped[String] = mapped_column(
        String(16), nullable=False, unique=True, name="energy_tariff"
    )


class GlazedTypeCategory(AppBase):
    """
    Glazing vocabulary, the reverse mapping of `EPC.glazed_type`.

    double glazing installed before 2002 = 1, double glazing installed during
    or after 2002 = 2, double glazing, unknown install date = 3, double, known
    data = 4, INVALID! = 5, not defined = 6, secondary glazing = 7, single
    glazing = 8, triple glazing = 9, triple, known data = 10.
    """

    __tablename__ = "glazed_type_category_table"
    id: Mapped[SmallInteger] = mapped_column(
        SmallInteger, primary_key=True, nullable=False, name="id"
    )
    glazed_type: Mapped[String] = mapped_column(
        String(50), nullable=False, unique=True, name="glazed_type"
    )


class EPC(AppBase):
    """
    Energy Performance Certificates, reduced from the original EPC record.

    Attributes
    ----------
    tenure : str
        Owner-occupied (O), rented private (P) or rented social (S).
    floor_level : str
        Normalised storey of a flat or maisonette, filled by
        ``pipeline_steps.add_floor_level_to_epc_table`` from the register's
        free-text FLOOR_LEVEL. Declared on the model so a promote, which
        rebuilds production from this table, keeps it.
    property_type : str
        The register's PROPERTY_TYPE (Flat, Maisonette, House, Bungalow, Park
        home), backfilled by migration 0019. The view's ``is_maisonette``
        falls back to it for sales ukhpucl does not cover.
    """

    __tablename__ = "epc_table"
    __table_args__ = (
        CheckConstraint(
            "tenure IS NULL OR tenure IN ('O','P','S')", name="ck_epc_tenure"
        ),
        CheckConstraint(
            "current_energy_rating IS NULL OR current_energy_rating IN "
            "('A','B','C','D','E','F','G')",
            name="ck_epc_current_energy_rating",
        ),
        CheckConstraint(
            "potential_energy_rating IS NULL OR potential_energy_rating IN "
            "('A','B','C','D','E','F','G')",
            name="ck_epc_potential_energy_rating",
        ),
        CheckConstraint(
            "mains_gas_flag IS NULL OR mains_gas_flag IN ('Y','N')",
            name="ck_epc_mains_gas_flag",
        ),
        UniqueConstraint(
            "address_id",
            "inspection_date",
            name="uq_epc_table_address_inspection",
            postgresql_nulls_not_distinct=True,
        ),
    )
    id: Mapped[BigInteger] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
        name="id",
        autoincrement=True,
    )
    address_id: Mapped[BigInteger] = mapped_column(
        ForeignKey("address_table.id"), nullable=False, index=True
    )
    current_energy_rating: Mapped[CHAR] = mapped_column(
        CHAR, name="current_energy_rating", nullable=True
    )
    potential_energy_rating: Mapped[CHAR] = mapped_column(
        CHAR, name="potential_energy_rating", nullable=True
    )
    current_energy_efficiency: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="current_energy_efficiency", nullable=True
    )
    potential_energy_efficiency: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="potential_energy_efficiency", nullable=True
    )
    build_form: Mapped[SmallInteger] = mapped_column(
        ForeignKey("build_form_category_table.id"),
        name="build_form",
        nullable=True,
        index=True,
    )
    inspection_date: Mapped[Date] = mapped_column(
        Date, name="inspection_date", nullable=True
    )
    transaction_type: Mapped[SmallInteger] = mapped_column(
        ForeignKey("transaction_type_category_table.id"),
        name="transaction_type",
        nullable=True,
        index=True,
    )
    environment_impact_current: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="environment_impact_current", nullable=True
    )
    environment_impact_potential: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="environment_impact_potential", nullable=True
    )
    energy_consumption_current: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="energy_consumption_current", nullable=True
    )
    energy_consumption_potential: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="energy_consumption_potential", nullable=True
    )
    co2_emissions_current: Mapped[Float] = mapped_column(
        Float, name="co2_emissions_current", nullable=True
    )
    co2_emiss_curr_per_floor_area: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="co2_emiss_curr_per_floor_area", nullable=True
    )
    co2_emissions_potential: Mapped[Float] = mapped_column(
        Float, name="co2_emissions_potential", nullable=True
    )
    lighting_cost_current: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="lighting_cost_current", nullable=True
    )
    lighting_cost_potential: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="lighting_cost_potential", nullable=True
    )
    heating_cost_current: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="heating_cost_current", nullable=True
    )
    heating_cost_potential: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="heating_cost_potential", nullable=True
    )
    hot_water_cost_current: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="hot_water_cost_current", nullable=True
    )
    hot_water_cost_potential: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="hot_water_cost_potential", nullable=True
    )
    total_floor_area: Mapped[Float] = mapped_column(
        Float, name="total_floor_area", nullable=True
    )
    energy_tariff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("energy_tariff_category_table.id"),
        name="energy_tariff",
        nullable=True,
        index=True,
    )
    mains_gas_flag: Mapped[CHAR] = mapped_column(
        CHAR, name="mains_gas_flag", nullable=True
    )
    flat_storey_count: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="flat_storey_count", nullable=True
    )
    multi_glaze_proportion: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="multi_glaze_proportion", nullable=True
    )
    glazed_type: Mapped[SmallInteger] = mapped_column(
        ForeignKey("glazed_type_category_table.id"),
        name="glazed_type",
        nullable=True,
        index=True,
    )
    extension_count: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="extension_count", nullable=True
    )
    number_habitable_rooms: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="number_habitable_rooms", nullable=True
    )
    number_heated_rooms: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="number_heated_rooms", nullable=True
    )
    low_energy_lighting: Mapped[SmallInteger] = mapped_column(
        SmallInteger, name="low_energy_lighting", nullable=True
    )
    hot_water_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="hot_water_energy_eff",
        nullable=True,
        index=True,
    )
    hot_water_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="hot_water_env_eff",
        nullable=True,
        index=True,
    )
    windows_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="windows_energy_eff",
        nullable=True,
        index=True,
    )
    windows_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="windows_env_eff",
        nullable=True,
        index=True,
    )
    walls_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="walls_energy_eff",
        nullable=True,
        index=True,
    )
    walls_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="walls_env_eff",
        nullable=True,
        index=True,
    )
    roof_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="roof_energy_eff",
        nullable=True,
        index=True,
    )
    roof_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="roof_env_eff",
        nullable=True,
        index=True,
    )
    mainheat_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="mainheat_energy_eff",
        nullable=True,
        index=True,
    )
    mainheat_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="mainheat_env_eff",
        nullable=True,
        index=True,
    )
    mainheatc_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="mainheatc_energy_eff",
        nullable=True,
        index=True,
    )
    mainheatc_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="mainheatc_env_eff",
        nullable=True,
        index=True,
    )
    lighting_energy_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="lighting_energy_eff",
        nullable=True,
        index=True,
    )
    lighting_env_eff: Mapped[SmallInteger] = mapped_column(
        ForeignKey("rating_category_table.id"),
        name="lighting_env_eff",
        nullable=True,
        index=True,
    )
    construction_age_band: Mapped[String] = mapped_column(
        String, name="construction_age_band", nullable=True
    )
    lodgement_datetime: Mapped[DateTime] = mapped_column(
        DateTime(timezone=False), name="lodgement_datetime", nullable=True
    )
    tenure: Mapped[CHAR] = mapped_column(CHAR, name="tenure", nullable=True)
    floor_level: Mapped[String] = mapped_column(
        String, name="floor_level", nullable=True
    )
    property_type: Mapped[String] = mapped_column(
        String, name="property_type", nullable=True
    )
    address: Mapped["Address"] = relationship(back_populates="epc_history")


def migration_metadata() -> MetaData:
    """
    Return `AppBase` metadata rebound to the literal `MIGRATION_SCHEMA`.

    The chain builds ``staging`` whatever ``DB_SCHEMA`` the shell exports.
    ``Table.to_metadata`` copies each table, rewriting foreign-key targets,
    without mutating the ORM classes the API imports.
    """
    target = MetaData(schema=MIGRATION_SCHEMA)
    for table in AppBase.metadata.sorted_tables:
        table.to_metadata(target, schema=MIGRATION_SCHEMA)
    return target


# Staging-only relations, built by raw DDL in migrations 0002-0012.

staging_meta = MetaData(schema=MIGRATION_SCHEMA)


class StagingBase(DeclarativeBase):
    """
    Declarative base for the staging-only relations.

    Pinned to `MIGRATION_SCHEMA`, never ``DB_SCHEMA``, since none of these
    relations is promoted.
    """

    metadata = staging_meta


class Geometry(UserDefinedType):
    """
    PostGIS ``geometry(<type>,<srid>)`` as a column type the models can name.

    Spatial work is server-side SQL, so no GeoAlchemy2 dependency is needed;
    values round-trip as EWKB hex.

    Parameters
    ----------
    geometry_type : str, optional
        PostGIS geometry type, e.g. ``"MULTIPOLYGON"``.
    srid : int, optional
        Spatial reference id.
    """

    cache_ok = True

    def __init__(self, geometry_type: str = "GEOMETRY", srid: int = 0) -> None:
        self.geometry_type = geometry_type
        self.srid = srid

    def get_col_spec(self, **kw: Any) -> str:
        """Return the DDL type, e.g. ``geometry(POINT,4326)``."""
        return f"geometry({self.geometry_type},{self.srid})"

    def __repr__(self) -> str:
        return f"Geometry({self.geometry_type!r}, {self.srid})"


# Raw landing tables, created by migration 0002 (or their pipeline step) and
# loaded by 0003. Sources as published: nothing is cleaned or typed down.


class EpcRegisterColumns:
    """
    The 93 columns of DLUHC's domestic EPC register CSV, verbatim.

    Shared by `EpcDomestic`, `EpcDomesticHistory` and `EpcLondonPick`. Every
    column is nullable and numerics are mostly NUMERIC, because typing
    happens downstream. Subclass columns sort first, matching the live column
    order. None of the three has a primary key; ``_mapper_pk`` names the
    identity columns and a subclass overrides it when its key differs.
    """

    _mapper_pk: tuple[str, ...] = ("certificate_number",)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, object]:
        """Map the ``_mapper_pk`` columns as the ORM identity."""
        return {
            "primary_key": tuple(cls.__table__.c[n] for n in cls._mapper_pk),
        }

    certificate_number: Mapped[str] = mapped_column(String, nullable=True)
    address1: Mapped[str] = mapped_column(String, nullable=True)
    address2: Mapped[str] = mapped_column(String, nullable=True)
    address3: Mapped[str] = mapped_column(String, nullable=True)
    postcode: Mapped[str] = mapped_column(String, nullable=True)
    posttown: Mapped[str] = mapped_column(String, nullable=True)
    address: Mapped[str] = mapped_column(String, nullable=True)
    constituency: Mapped[str] = mapped_column(String, nullable=True)
    constituency_label: Mapped[str] = mapped_column(String, nullable=True)
    local_authority: Mapped[str] = mapped_column(String, nullable=True)
    local_authority_label: Mapped[str] = mapped_column(String, nullable=True)
    built_form: Mapped[str] = mapped_column(String, nullable=True)
    co2_emiss_curr_per_floor_area: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    co2_emissions_current: Mapped[float] = mapped_column(Float, nullable=True)
    co2_emissions_potential: Mapped[float] = mapped_column(Float, nullable=True)
    construction_age_band: Mapped[str] = mapped_column(String, nullable=True)
    current_energy_efficiency: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    current_energy_rating: Mapped[str] = mapped_column(String, nullable=True)
    energy_consumption_current: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    energy_consumption_potential: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    energy_tariff: Mapped[str] = mapped_column(String, nullable=True)
    environment_impact_current: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    environment_impact_potential: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    extension_count: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    fixed_lighting_outlets_count: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    flat_storey_count: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    flat_top_storey: Mapped[str] = mapped_column(String, nullable=True)
    floor_description: Mapped[str] = mapped_column(String, nullable=True)
    floor_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    floor_height: Mapped[float] = mapped_column(Float, nullable=True)
    floor_level: Mapped[str] = mapped_column(String, nullable=True)
    glazed_area: Mapped[str] = mapped_column(String, nullable=True)
    glazed_type: Mapped[str] = mapped_column(String, nullable=True)
    heat_loss_corridor: Mapped[str] = mapped_column(String, nullable=True)
    heating_cost_current: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    heating_cost_potential: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hot_water_cost_current: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hot_water_cost_potential: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    hot_water_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    hot_water_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    hotwater_description: Mapped[str] = mapped_column(String, nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, nullable=True)
    lighting_cost_current: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    lighting_cost_potential: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    lighting_description: Mapped[str] = mapped_column(String, nullable=True)
    lighting_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    lighting_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    lodgement_date: Mapped[date] = mapped_column(Date, nullable=True)
    lodgement_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    low_energy_lighting: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    low_energy_fixed_lighting_outlets_count: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=True,
    )
    main_fuel: Mapped[str] = mapped_column(String, nullable=True)
    mainheat_description: Mapped[str] = mapped_column(String, nullable=True)
    mainheat_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheat_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheatc_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheatc_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheatcont_description: Mapped[str] = mapped_column(String, nullable=True)
    main_heating_controls: Mapped[str] = mapped_column(String, nullable=True)
    mains_gas_flag: Mapped[str] = mapped_column(String, nullable=True)
    mechanical_ventilation: Mapped[str] = mapped_column(String, nullable=True)
    multi_glaze_proportion: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    number_habitable_rooms: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    number_heated_rooms: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    number_open_fireplaces: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    photo_supply: Mapped[float] = mapped_column(Float, nullable=True)
    potential_energy_efficiency: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    potential_energy_rating: Mapped[str] = mapped_column(String, nullable=True)
    property_type: Mapped[str] = mapped_column(String, nullable=True)
    report_type: Mapped[str] = mapped_column(String, nullable=True)
    roof_description: Mapped[str] = mapped_column(String, nullable=True)
    roof_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    roof_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    secondheat_description: Mapped[str] = mapped_column(String, nullable=True)
    sheating_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    sheating_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    solar_water_heating_flag: Mapped[str] = mapped_column(String, nullable=True)
    tenure: Mapped[str] = mapped_column(String, nullable=True)
    total_floor_area: Mapped[float] = mapped_column(Float, nullable=True)
    transaction_type: Mapped[str] = mapped_column(String, nullable=True)
    unheated_corridor_length: Mapped[float] = mapped_column(Float, nullable=True)
    walls_description: Mapped[str] = mapped_column(String, nullable=True)
    walls_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    walls_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    wind_turbine_count: Mapped[Decimal] = mapped_column(Numeric, nullable=True)
    windows_description: Mapped[str] = mapped_column(String, nullable=True)
    windows_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    windows_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    floor_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    region: Mapped[str] = mapped_column(String, nullable=True)
    country: Mapped[str] = mapped_column(String, nullable=True)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    uprn_source: Mapped[str] = mapped_column(String, nullable=True)


class EpcDomestic(EpcRegisterColumns, StagingBase):
    """
    DLUHC domestic EPC certificates, 2024-2026, national, ~4.51M rows.

    Feeds `EPC` through the fusion in migration 0006. Kept separate from
    `EpcDomesticHistory` because most recent sales have a pre-2024
    certificate that the promoted `EPC` deliberately does not take.
    """

    __tablename__ = "epc_domestic"
    _mapper_pk = ("certificate_number",)


class EpcDomesticHistory(EpcRegisterColumns, StagingBase):
    """
    DLUHC domestic EPC certificates, 2008-2026, London only, ~4.59M rows.

    Feeds the `EpcAddrKey` gazetteer (migration 0006) and the London history
    backfill of ``production.epc_table`` (migration 0008).
    """

    __tablename__ = "epc_domestic_history"
    _mapper_pk = ("certificate_number",)


class PpdDomestic(StagingBase):
    """
    HM Land Registry Price Paid, national, as published, ~2.21M rows.

    The transfers side of the 2024-2026 fusion; it carries no UPRN, so the
    fusion joins on address.
    """

    __tablename__ = "ppd_domestic"

    transactionid: Mapped[str] = mapped_column(String, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    dateoftransfer: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    postcode: Mapped[str] = mapped_column(String, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    oldnew: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)
    paon: Mapped[str] = mapped_column(String, nullable=True)
    saon: Mapped[str] = mapped_column(String, nullable=True)
    street: Mapped[str] = mapped_column(String, nullable=True)
    locality: Mapped[str] = mapped_column(String, nullable=True)
    towncity: Mapped[str] = mapped_column(String, nullable=True)
    district: Mapped[str] = mapped_column(String, nullable=True)
    county: Mapped[str] = mapped_column(String, nullable=True)
    categorytype: Mapped[str] = mapped_column(String, nullable=True)
    recordstatus: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (transactionid,)}


class CodepointOpen(StagingBase):
    """
    Ordnance Survey Code-Point Open: a BNG centroid per postcode, ~1.75M rows.

    The geometric guard on the fusion's match tiers. Its functional index uses
    ``staging.hpldn_pcnorm``, which is why that function is created in 0002.
    """

    __tablename__ = "codepoint_open"
    __table_args__ = (
        Index(
            "idx_codepoint_open_pcnorm",
            text("staging.hpldn_pcnorm((postcode)::text)"),
        ),
    )

    postcode: Mapped[str] = mapped_column(String, nullable=True)
    positional_quality: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    eastings: Mapped[int] = mapped_column(Integer, nullable=True)
    northings: Mapped[int] = mapped_column(Integer, nullable=True)
    country_code: Mapped[str] = mapped_column(String, nullable=True)
    nhs_regional_ha_code: Mapped[str] = mapped_column(String, nullable=True)
    nhs_ha_code: Mapped[str] = mapped_column(String, nullable=True)
    admin_county_code: Mapped[str] = mapped_column(String, nullable=True)
    admin_district_code: Mapped[str] = mapped_column(String, nullable=True)
    admin_ward_code: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (postcode,)}


class PpdUprnLinkageTierA(StagingBase):
    """
    The Tier A linkage fixture: 54,137 hand-produced (transaction, UPRN) links.

    The one irreproducible input in the chain. Migration 0006 loads it before
    the four-tier matcher so hand-verified rows win, and reports the set
    difference against the matcher's output; retire it when that is empty.
    """

    __tablename__ = "_ppd_uprn_linkage_tiera"

    transactionid: Mapped[str] = mapped_column(String, nullable=True)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (transactionid,)}


class Lsoa(StagingBase):
    """ONS LSOA 2011 to LSOA 2021 to LAD 2022 lookup, 34,753 rows."""

    __tablename__ = "lsoa"

    lsoa11cd: Mapped[str] = mapped_column(String, nullable=True)
    lsoa11nm: Mapped[str] = mapped_column(String, nullable=True)
    lsoa21cd: Mapped[str] = mapped_column(String, nullable=True)
    lsoa21nm: Mapped[str] = mapped_column(String, nullable=True)
    lsoa21nmw: Mapped[str] = mapped_column(String, nullable=True)
    lad22cd: Mapped[str] = mapped_column(String, nullable=True)
    lad22nm: Mapped[str] = mapped_column(String, nullable=True)
    lad22nmw: Mapped[str] = mapped_column(String, nullable=True)
    object_id: Mapped[int] = mapped_column(Integer, nullable=True)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, nullable=False)


class Msoa(StagingBase):
    """ONS MSOA 2011 to MSOA 2021 to LAD 2022 lookup, 7,201 rows."""

    __tablename__ = "msoa"

    msoa11cd: Mapped[str] = mapped_column(String, nullable=True)
    msoa11nm: Mapped[str] = mapped_column(String, nullable=True)
    chngind: Mapped[str] = mapped_column(String, nullable=True)
    msoa21cd: Mapped[str] = mapped_column(String, nullable=True)
    msoa21nm: Mapped[str] = mapped_column(String, nullable=True)
    lad22cd: Mapped[str] = mapped_column(String, nullable=True)
    lad22nm: Mapped[str] = mapped_column(String, nullable=True)
    lad22nmw: Mapped[str] = mapped_column(String, nullable=True)
    object_id: Mapped[int] = mapped_column(Integer, nullable=True)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, nullable=False)


class LsoaBoundaries(StagingBase):
    """LSOA 2011 boundary polygons for London, 4,835 rows, behind `production.lsoa_msoa_features`."""

    __tablename__ = "lsoa_boundaries"
    __table_args__ = (
        Index("idx_staging_lsoa_boundaries_geom", "geom", postgresql_using="gist"),
    )

    lsoa11cd: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    lsoa11nm: Mapped[str] = mapped_column(String, nullable=True)
    wkt: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[str] = mapped_column(Geometry("MultiPolygon", 4326), nullable=False)


class LsoaMsoaLookup(StagingBase):
    """LSOA 2011 to MSOA 2011 lookup, 34,753 rows."""

    __tablename__ = "lsoa_msoa_lu"
    __table_args__ = (Index("idx_staging_lsoa_msoa_lu_msoa11cd", "msoa11cd"),)

    lsoa11cd: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    lsoa11nm: Mapped[str] = mapped_column(String, nullable=True)
    msoa11cd: Mapped[str] = mapped_column(String, nullable=False)
    msoa11nm: Mapped[str] = mapped_column(String, nullable=True)


class Ptal2023(StagingBase):
    """
    TfL's 2023 PTAL grid: 159,451 100 m cells.

    Per-mode accessibility indices, their total ``ai`` and the published band.
    ``grid_id`` is TfL's own id, so it is declared without autoincrement.
    """

    __tablename__ = "ptal_2023"
    __table_args__ = (
        CheckConstraint(
            "(ai >= (0)::double precision)",
            name="ck_ptal_2023_ai_non_negative",
        ),
        CheckConstraint(
            "(ptal_band = ANY (ARRAY['0'::text, '1a'::text, '1b'::text, '2'::text, '3'::text, '4'::text, '5'::text, '6a'::text, '6b'::text]))",
            name="ck_ptal_2023_band",
        ),
        Index("idx_staging_ptal_2023_band", "ptal_band"),
    )

    grid_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, autoincrement=False
    )
    bus: Mapped[float] = mapped_column(Float, nullable=False)
    lul: Mapped[float] = mapped_column(Float, nullable=False)
    rail: Mapped[float] = mapped_column(Float, nullable=False)
    tram: Mapped[float] = mapped_column(Float, nullable=False)
    ai: Mapped[float] = mapped_column(Float, nullable=False)
    ptal_band: Mapped[str] = mapped_column(Text, nullable=False)
    shape_leng: Mapped[float] = mapped_column(Float, nullable=True)
    shape_area: Mapped[float] = mapped_column(Float, nullable=True)


class PtalCoords(StagingBase):
    """The centroid of every PTAL cell, geocoded from TfL's centroid file."""

    __tablename__ = "ptal_coords"
    __table_args__ = (
        CheckConstraint(
            "((latitude >= ('-90'::integer)::double precision) AND (latitude <= (90)::double precision))",
            name="ck_ptal_coords_latitude",
        ),
        CheckConstraint(
            "((longitude >= ('-180'::integer)::double precision) AND (longitude <= (180)::double precision))",
            name="ck_ptal_coords_longitude",
        ),
        Index("idx_staging_ptal_coords_geom", "geom", postgresql_using="gist"),
        Index("idx_staging_ptal_coords_lat_lon", "latitude", "longitude"),
    )

    grid_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("ptal_2023.grid_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    easting: Mapped[float] = mapped_column(Float, nullable=False)
    northing: Mapped[float] = mapped_column(Float, nullable=False)
    geom: Mapped[str] = mapped_column(Geometry("Point", 4326), nullable=True)


class PtalCellPolygons(StagingBase):
    """
    The boundary polygon of every PTAL cell.

    Migration 0012 joins properties to cells with ``ST_Contains``; `PtalCoords`
    supplies only the bounded fallback for points in no cell.
    """

    __tablename__ = "ptal_cell_polygons"
    __table_args__ = (
        Index("idx_staging_ptal_cell_polygons_geom", "geom", postgresql_using="gist"),
    )

    grid_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("ptal_2023.grid_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    wkt: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[str] = mapped_column(Geometry("MultiPolygon", 4326), nullable=False)


class Rivers(StagingBase):
    """OS Open Rivers watercourse centrelines for London, 1,613 rows (read by migration 0013)."""

    __tablename__ = "rivers"
    __table_args__ = (
        Index("idx_staging_rivers_geom", "geom", postgresql_using="gist"),
        Index("idx_staging_rivers_name_lower", text("lower(watercourse_name)")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    watercourse_name: Mapped[str] = mapped_column(Text, nullable=True)
    watercourse_name_alternative: Mapped[str] = mapped_column(Text, nullable=True)
    form: Mapped[str] = mapped_column(Text, nullable=True)
    flow_direction: Mapped[str] = mapped_column(Text, nullable=True)
    fictitious: Mapped[bool] = mapped_column(Boolean, nullable=True)
    length_m: Mapped[int] = mapped_column(Integer, nullable=True)
    geom: Mapped[str] = mapped_column(Geometry("LineString", 4326), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)


class InspirePolygons(StagingBase):
    """
    INSPIRE cadastral parcels, ~2.06M rows, loaded from 33 borough GMLs via ogr2ogr.

    `LondonParcels` takes the parcel containing each address. INSPIRE covers
    freehold only, so leasehold UPRNs get no parcel; migration 0005 logs them.
    """

    __tablename__ = "inspire_polygons"
    __table_args__ = (
        Index("idx_inspire_polygons_geom", "wkb_geometry", postgresql_using="gist"),
    )

    ogc_fid: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    gml_id: Mapped[str] = mapped_column(String, nullable=False)
    inspireid: Mapped[int] = mapped_column(Integer, nullable=True)
    label: Mapped[int] = mapped_column(Integer, nullable=True)
    nationalcadastralreference: Mapped[int] = mapped_column(Integer, nullable=True)
    validfrom: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    beginlifespanversion: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    wkb_geometry: Mapped[str] = mapped_column(
        Geometry("MultiPolygon", 27700),
        nullable=True,
    )


class IodLsoaRaw(StagingBase):
    """
    Indices of Deprivation as published, one row per (release, LSOA), 131,925 rows.

    Covers the 2010, 2015, 2019 and 2025 releases, whose layouts and LSOA
    vintages differ; migration 0011 reconciles them and checks row counts.
    """

    __tablename__ = "iod_lsoa_raw"

    release_year: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
    )
    lsoa_code_raw: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    lad_code_raw: Mapped[str] = mapped_column(String, nullable=True)
    imd_score: Mapped[float] = mapped_column(Float, nullable=True)
    imd_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    income_score: Mapped[float] = mapped_column(Float, nullable=True)
    income_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    employment_score: Mapped[float] = mapped_column(Float, nullable=True)
    employment_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    education_score: Mapped[float] = mapped_column(Float, nullable=True)
    education_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    health_score: Mapped[float] = mapped_column(Float, nullable=True)
    health_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    crime_score: Mapped[float] = mapped_column(Float, nullable=True)
    crime_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    barriers_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    living_env_score: Mapped[float] = mapped_column(Float, nullable=True)
    living_env_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    idaci_score: Mapped[float] = mapped_column(Float, nullable=True)
    idaci_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    idaopi_score: Mapped[float] = mapped_column(Float, nullable=True)
    idaopi_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    children_score: Mapped[float] = mapped_column(Float, nullable=True)
    adult_skills_score: Mapped[float] = mapped_column(Float, nullable=True)
    geo_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    wider_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    indoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    outdoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    total_population: Mapped[int] = mapped_column(Integer, nullable=True)


class LondonGreenSpaces(StagingBase):
    """
    OS Open Greenspace sites for London, 21,992 rows.

    Loaded for a planned distance-to-greenspace feature; nothing reads it yet.
    It stays in staging so it is not exposed to the read-only API role.
    """

    __tablename__ = "london_green_spaces"
    __table_args__ = (
        Index("idx_prod_green_spaces_function", "space_function"),
        Index("idx_prod_green_spaces_geom", "geom", postgresql_using="gist"),
        Index("idx_prod_green_spaces_name1_lower", text("lower(distinctive_name_1)")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    space_function: Mapped[str] = mapped_column(Text, nullable=False)
    distinctive_name_1: Mapped[str] = mapped_column(Text, nullable=True)
    distinctive_name_2: Mapped[str] = mapped_column(Text, nullable=True)
    geom: Mapped[str] = mapped_column(Geometry("MultiPolygon", 4326), nullable=False)
    area_m2: Mapped[float] = mapped_column(Float, nullable=True)
    area_ha: Mapped[float] = mapped_column(Float, nullable=True)


class LondonGreenSpaceAccessPoints(StagingBase):
    """Public access points into `LondonGreenSpaces`, 46,442 rows, with a cascading foreign key."""

    __tablename__ = "london_green_space_access_points"
    __table_args__ = (
        Index(
            "idx_prod_green_space_access_points_geom",
            "geom",
            postgresql_using="gist",
        ),
        Index("idx_prod_green_space_access_points_site_ref", "ref_to_greenspace_site"),
        Index("idx_prod_green_space_access_points_type", "access_type"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    access_type: Mapped[str] = mapped_column(Text, nullable=False)
    ref_to_greenspace_site: Mapped[str] = mapped_column(
        Text,
        ForeignKey("london_green_spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    geom: Mapped[str] = mapped_column(Geometry("Point", 4326), nullable=False)


# The London spine, built by migration 0005. The UNLOGGED tables are rebuilt
# by one revision: after a crash, re-run it rather than restoring them.


class LondonHouses(StagingBase):
    """
    The London slice of `UKHPUCL`, ~2.81M rows, UNLOGGED.

    Built as ``SELECT * FROM staging.ukhpucl WHERE ...``, so ``id`` is unique
    though undeclared. Unrelated to the ``production.london_houses`` view.
    """

    __tablename__ = "london_houses"
    __table_args__ = (
        Index("idx_lh_uprn", "UPRN"),
        {"prefixes": ["UNLOGGED"]},
    )

    id: Mapped[int] = mapped_column(Integer, nullable=True)
    postcode: Mapped[str] = mapped_column(String, nullable=True)
    transactionid: Mapped[str] = mapped_column(String, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    dateoftransfer: Mapped[str] = mapped_column(String, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    oldnew: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)
    paon: Mapped[str] = mapped_column(String, nullable=True)
    saon: Mapped[str] = mapped_column(String, nullable=True)
    street: Mapped[str] = mapped_column(String, nullable=True)
    locality: Mapped[str] = mapped_column(String, nullable=True)
    towncity: Mapped[str] = mapped_column(String, nullable=True)
    district: Mapped[str] = mapped_column(String, nullable=True)
    county: Mapped[str] = mapped_column(String, nullable=True)
    categorytype: Mapped[str] = mapped_column(String, nullable=True)
    recordstatus: Mapped[str] = mapped_column(String, nullable=True)
    lmk_key: Mapped[str] = mapped_column(String, nullable=True, name="LMK_KEY")
    classt: Mapped[int] = mapped_column(BigInteger, nullable=True)
    lad23cd: Mapped[str] = mapped_column(String, nullable=True)
    building_reference_number: Mapped[int] = mapped_column(
        BigInteger,
        nullable=True,
        name="BUILDING_REFERENCE_NUMBER",
    )
    current_energy_rating: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="CURRENT_ENERGY_RATING",
    )
    potential_energy_rating: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="POTENTIAL_ENERGY_RATING",
    )
    current_energy_efficiency: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="CURRENT_ENERGY_EFFICIENCY",
    )
    potential_energy_efficiency: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="POTENTIAL_ENERGY_EFFICIENCY",
    )
    property_type: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="PROPERTY_TYPE",
    )
    built_form: Mapped[str] = mapped_column(String, nullable=True, name="BUILT_FORM")
    inspection_date: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="INSPECTION_DATE",
    )
    local_authority: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="LOCAL_AUTHORITY",
    )
    constituency: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="CONSTITUENCY",
    )
    county_epc: Mapped[str] = mapped_column(String, nullable=True, name="COUNTY")
    lodgement_date: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="LODGEMENT_DATE",
    )
    transaction_type: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="TRANSACTION_TYPE",
    )
    environment_impact_current: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="ENVIRONMENT_IMPACT_CURRENT",
    )
    environment_impact_potential: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="ENVIRONMENT_IMPACT_POTENTIAL",
    )
    energy_consumption_current: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="ENERGY_CONSUMPTION_CURRENT",
    )
    energy_consumption_potential: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="ENERGY_CONSUMPTION_POTENTIAL",
    )
    co2_emissions_current: Mapped[float] = mapped_column(
        Float,
        nullable=True,
        name="CO2_EMISSIONS_CURRENT",
    )
    co2_emiss_curr_per_floor_area: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="CO2_EMISS_CURR_PER_FLOOR_AREA",
    )
    co2_emissions_potential: Mapped[float] = mapped_column(
        Float,
        nullable=True,
        name="CO2_EMISSIONS_POTENTIAL",
    )
    lighting_cost_current: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="LIGHTING_COST_CURRENT",
    )
    lighting_cost_potential: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="LIGHTING_COST_POTENTIAL",
    )
    heating_cost_current: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="HEATING_COST_CURRENT",
    )
    heating_cost_potential: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="HEATING_COST_POTENTIAL",
    )
    hot_water_cost_current: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="HOT_WATER_COST_CURRENT",
    )
    hot_water_cost_potential: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="HOT_WATER_COST_POTENTIAL",
    )
    total_floor_area: Mapped[float] = mapped_column(
        Float,
        nullable=True,
        name="TOTAL_FLOOR_AREA",
    )
    energy_tariff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="ENERGY_TARIFF",
    )
    mains_gas_flag: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINS_GAS_FLAG",
    )
    floor_level: Mapped[str] = mapped_column(String, nullable=True, name="FLOOR_LEVEL")
    flat_top_storey: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="FLAT_TOP_STOREY",
    )
    flat_storey_count: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="FLAT_STOREY_COUNT",
    )
    main_heating_controls: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAIN_HEATING_CONTROLS",
    )
    multi_glaze_proportion: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="MULTI_GLAZE_PROPORTION",
    )
    glazed_type: Mapped[str] = mapped_column(String, nullable=True, name="GLAZED_TYPE")
    glazed_area: Mapped[str] = mapped_column(String, nullable=True, name="GLAZED_AREA")
    extension_count: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="EXTENSION_COUNT",
    )
    number_habitable_rooms: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="NUMBER_HABITABLE_ROOMS",
    )
    number_heated_rooms: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="NUMBER_HEATED_ROOMS",
    )
    low_energy_lighting: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="LOW_ENERGY_LIGHTING",
    )
    number_open_fireplaces: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="NUMBER_OPEN_FIREPLACES",
    )
    hotwater_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="HOTWATER_DESCRIPTION",
    )
    hot_water_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="HOT_WATER_ENERGY_EFF",
    )
    hot_water_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="HOT_WATER_ENV_EFF",
    )
    floor_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="FLOOR_DESCRIPTION",
    )
    floor_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="FLOOR_ENERGY_EFF",
    )
    floor_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="FLOOR_ENV_EFF",
    )
    windows_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="WINDOWS_DESCRIPTION",
    )
    windows_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="WINDOWS_ENERGY_EFF",
    )
    windows_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="WINDOWS_ENV_EFF",
    )
    walls_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="WALLS_DESCRIPTION",
    )
    walls_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="WALLS_ENERGY_EFF",
    )
    walls_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="WALLS_ENV_EFF",
    )
    secondheat_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="SECONDHEAT_DESCRIPTION",
    )
    sheating_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="SHEATING_ENERGY_EFF",
    )
    sheating_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="SHEATING_ENV_EFF",
    )
    roof_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="ROOF_DESCRIPTION",
    )
    roof_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="ROOF_ENERGY_EFF",
    )
    roof_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="ROOF_ENV_EFF",
    )
    mainheat_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINHEAT_DESCRIPTION",
    )
    mainheat_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINHEAT_ENERGY_EFF",
    )
    mainheat_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINHEAT_ENV_EFF",
    )
    mainheatcont_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINHEATCONT_DESCRIPTION",
    )
    mainheatc_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINHEATC_ENERGY_EFF",
    )
    mainheatc_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MAINHEATC_ENV_EFF",
    )
    lighting_description: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="LIGHTING_DESCRIPTION",
    )
    lighting_energy_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="LIGHTING_ENERGY_EFF",
    )
    lighting_env_eff: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="LIGHTING_ENV_EFF",
    )
    main_fuel: Mapped[str] = mapped_column(String, nullable=True, name="MAIN_FUEL")
    wind_turbine_count: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="WIND_TURBINE_COUNT",
    )
    heat_loss_corridor: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="HEAT_LOSS_CORRIDOR",
    )
    unheated_corridor_length: Mapped[float] = mapped_column(
        Float,
        nullable=True,
        name="UNHEATED_CORRIDOR_LENGTH",
    )
    floor_height: Mapped[float] = mapped_column(
        Float,
        nullable=True,
        name="FLOOR_HEIGHT",
    )
    photo_supply: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="PHOTO_SUPPLY",
    )
    solar_water_heating_flag: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="SOLAR_WATER_HEATING_FLAG",
    )
    mechanical_ventilation: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="MECHANICAL_VENTILATION",
    )
    local_authority_label: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="LOCAL_AUTHORITY_LABEL",
    )
    constituency_label: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="CONSTITUENCY_LABEL",
    )
    posttown: Mapped[str] = mapped_column(String, nullable=True, name="POSTTOWN")
    construction_age_band: Mapped[str] = mapped_column(
        String,
        nullable=True,
        name="CONSTRUCTION_AGE_BAND",
    )
    lodgement_datetime: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=True,
        name="LODGEMENT_DATETIME",
    )
    tenure: Mapped[str] = mapped_column(String, nullable=True, name="TENURE")
    fixed_lighting_outlets_count: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="FIXED_LIGHTING_OUTLETS_COUNT",
    )
    low_energy_fixed_light_count: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        name="LOW_ENERGY_FIXED_LIGHT_COUNT",
    )
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True, name="UPRN")
    uprn_source: Mapped[str] = mapped_column(String, nullable=True, name="UPRN_SOURCE")
    priceper: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (id,)}


class LondonHousesCondensed(StagingBase):
    """
    One row per (UPRN, inspection_date) from `LondonHouses`, ~1.81M rows, UNLOGGED.

    Carries only the fields the address and transaction derivations need.
    """

    __tablename__ = "london_houses_condensed"
    __table_args__ = (
        Index("idx_lhc_uprn", "uprn"),
        Index("idx_lhc_uprn_inspection", "uprn", "inspection_date"),
        {"prefixes": ["UNLOGGED"]},
    )

    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    postcode: Mapped[str] = mapped_column(String, nullable=True)
    total_floor_area: Mapped[float] = mapped_column(Float, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)
    current_energy_rating: Mapped[str] = mapped_column(String, nullable=True)
    potential_energy_rating: Mapped[str] = mapped_column(String, nullable=True)
    construction_age_band: Mapped[str] = mapped_column(Text, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, nullable=True)
    flat_storey_count: Mapped[int] = mapped_column(Integer, nullable=True)
    lighting_cost_current: Mapped[int] = mapped_column(Integer, nullable=True)
    lighting_cost_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    heating_cost_current: Mapped[int] = mapped_column(Integer, nullable=True)
    heating_cost_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    number_habitable_rooms: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_cost_current: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_cost_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    extension_count: Mapped[int] = mapped_column(Integer, nullable=True)
    tenure: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (uprn, inspection_date, date)}


class EpcTableCondensed(StagingBase):
    """
    One row per (UPRN, inspection_date) certificate, ~1.22M rows, UNLOGGED.

    The as-of join's input: migration 0005 takes the newest certificate
    inspected on or before a sale, so no post-sale measurement leaks in.
    """

    __tablename__ = "epc_table_condensed"
    __table_args__ = (
        Index("idx_epc_condensed_uprn_inspection", "uprn", "inspection_date"),
        {"prefixes": ["UNLOGGED"]},
    )

    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    current_energy_efficiency: Mapped[int] = mapped_column(Integer, nullable=True)
    potential_energy_efficiency: Mapped[int] = mapped_column(Integer, nullable=True)
    transaction_type: Mapped[str] = mapped_column(String, nullable=True)
    environment_impact_current: Mapped[int] = mapped_column(Integer, nullable=True)
    environment_impact_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    energy_consumption_current: Mapped[int] = mapped_column(Integer, nullable=True)
    energy_consumption_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    co2_emissions_current: Mapped[float] = mapped_column(Float, nullable=True)
    co2_emiss_curr_per_floor_area: Mapped[int] = mapped_column(Integer, nullable=True)
    co2_emissions_potential: Mapped[float] = mapped_column(Float, nullable=True)
    energy_tariff: Mapped[str] = mapped_column(String, nullable=True)
    mains_gas_flag: Mapped[str] = mapped_column(String, nullable=True)
    multi_glaze_proportion: Mapped[int] = mapped_column(Integer, nullable=True)
    extension_count: Mapped[int] = mapped_column(Integer, nullable=True)
    number_habitable_rooms: Mapped[int] = mapped_column(Integer, nullable=True)
    number_heated_rooms: Mapped[int] = mapped_column(Integer, nullable=True)
    low_energy_lighting: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    hot_water_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    windows_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    windows_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    walls_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    walls_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    roof_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    roof_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheat_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheat_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheatc_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    mainheatc_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    lighting_energy_eff: Mapped[str] = mapped_column(String, nullable=True)
    lighting_env_eff: Mapped[str] = mapped_column(String, nullable=True)
    lodgement_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    glazed_type: Mapped[str] = mapped_column(String, nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, nullable=True)
    build_form: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (uprn, inspection_date)}


class EpcFusedStaging(StagingBase):
    """
    Certificates joined to addresses and transactions, ~939k rows, UNLOGGED.

    Register vocabularies are already resolved to category ids; the direct
    input to `EPC`.
    """

    __tablename__ = "epc_fused_staging_table"
    __table_args__ = (
        Index("idx_fused_address", "address_id"),
        Index("idx_fused_uprn", "uprn"),
        {"prefixes": ["UNLOGGED"]},
    )

    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    postcode: Mapped[str] = mapped_column(String, nullable=True)
    total_floor_area: Mapped[float] = mapped_column(Float, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)
    potential_energy_rating: Mapped[str] = mapped_column(String, nullable=True)
    current_energy_rating: Mapped[str] = mapped_column(String, nullable=True)
    construction_age_band: Mapped[str] = mapped_column(Text, nullable=True)
    flat_storey_count: Mapped[int] = mapped_column(Integer, nullable=True)
    lighting_cost_current: Mapped[int] = mapped_column(Integer, nullable=True)
    lighting_cost_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    heating_cost_current: Mapped[int] = mapped_column(Integer, nullable=True)
    heating_cost_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_cost_current: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_cost_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    tenure: Mapped[str] = mapped_column(Text, nullable=True)
    number_habitable_rooms: Mapped[int] = mapped_column(Integer, nullable=True)
    extension_count: Mapped[int] = mapped_column(Integer, nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, nullable=True)
    build_form: Mapped[int] = mapped_column(Integer, nullable=True)
    current_energy_efficiency: Mapped[int] = mapped_column(Integer, nullable=True)
    potential_energy_efficiency: Mapped[int] = mapped_column(Integer, nullable=True)
    transaction_type: Mapped[int] = mapped_column(Integer, nullable=True)
    environment_impact_current: Mapped[int] = mapped_column(Integer, nullable=True)
    environment_impact_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    energy_consumption_current: Mapped[int] = mapped_column(Integer, nullable=True)
    energy_consumption_potential: Mapped[int] = mapped_column(Integer, nullable=True)
    co2_emissions_current: Mapped[float] = mapped_column(Float, nullable=True)
    co2_emiss_curr_per_floor_area: Mapped[int] = mapped_column(Integer, nullable=True)
    co2_emissions_potential: Mapped[float] = mapped_column(Float, nullable=True)
    energy_tariff: Mapped[int] = mapped_column(Integer, nullable=True)
    mains_gas_flag: Mapped[str] = mapped_column(String, nullable=True)
    multi_glaze_proportion: Mapped[int] = mapped_column(Integer, nullable=True)
    glazed_type: Mapped[int] = mapped_column(Integer, nullable=True)
    number_heated_rooms: Mapped[int] = mapped_column(Integer, nullable=True)
    low_energy_lighting: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    hot_water_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    windows_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    windows_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    walls_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    walls_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    roof_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    roof_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    mainheat_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    mainheat_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    mainheatc_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    mainheatc_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    lighting_energy_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    lighting_env_eff: Mapped[int] = mapped_column(Integer, nullable=True)
    lodgement_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    __mapper_args__ = {"primary_key": (address_id, inspection_date)}


class LondonParcels(StagingBase):
    """
    Lot area in m² per UPRN from the containing INSPIRE parcel, 922,286 rows.

    Promoted to ``production.london_parcels`` by migration 0007. The database
    foreign key to ``staging.ukhcs_table."UPRN"`` is created by migration 0005
    and not declared here, since it would cross two MetaData objects.
    """

    __tablename__ = "london_parcels"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(start=1, cycle=False),
        primary_key=True,
        nullable=False,
    )
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lot_area_sqm: Mapped[float] = mapped_column(Float, nullable=False)


class PpdTableErrors(StagingBase):
    """
    Transactions rejected while building `PPD`, with the reason, 124 rows.

    A log with no key; the mapper primary key is only an ORM convenience.
    """

    __tablename__ = "ppd_table_errors"

    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(CHAR, nullable=True)
    error_reason: Mapped[str] = mapped_column(Text, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    __mapper_args__ = {"primary_key": (address_id, dateoftransfer, price, logged_at)}


# EPC / Price Paid fusion (migration 0006) for sales after staging.ukhpucl ends.
# The `_`-prefixed relations are build intermediates read only by 0006.


class EpcLondonAddr(StagingBase):
    """One row per London EPC UPRN with the address split into parts, 620,761 rows (intermediate)."""

    __tablename__ = "_epc_london_addr"
    __table_args__ = (Index("_epc_london_addr_uprn_idx", "uprn", unique=True),)

    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    postnumber: Mapped[str] = mapped_column(Text, nullable=True)
    poststreet: Mapped[str] = mapped_column(Text, nullable=True)
    posttown: Mapped[str] = mapped_column(Text, nullable=True)
    postcode: Mapped[str] = mapped_column(Text, nullable=True)
    postcounty: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {"primary_key": (uprn,)}


class EpcLondonAddrId(StagingBase):
    """London EPC UPRNs joined through `UKHC` to an `Address.id`, 256,546 rows (intermediate)."""

    __tablename__ = "_epc_london_addrid"
    __table_args__ = (Index("_epc_london_addrid_uprn_idx", "uprn", unique=True),)

    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)

    __mapper_args__ = {"primary_key": (uprn,)}


class EpcLondonPick(EpcRegisterColumns, StagingBase):
    """
    One certificate per (address_id, inspection_date), 261,624 rows (intermediate).

    In the register's column shape; the row source for inserts into `EPC` and
    `EpcTableCondensed`.
    """

    __tablename__ = "_epc_london_pick"
    _mapper_pk = ("address_id", "inspection_date")

    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)


class EpcAddrKey(StagingBase):
    """
    The address gazetteer: one row per London-UPRN certificate, ~3.08M rows.

    Carries the four tier keys precomputed by the ``staging.hpldn_*``
    functions from migration 0002: ``hpldn_addrkey`` normalises flat words and
    street abbreviations, ``hpldn_tokenset`` is its order-insensitive form and
    ``hpldn_numsig`` its digits-only signature. Nothing is unique, so prefer
    Core selects against ``__table__``.
    """

    __tablename__ = "epc_addrkey"
    __table_args__ = (
        Index("epc_addrkey_postcode_norm_k_exact_idx", "postcode_norm", "k_exact"),
        Index("epc_addrkey_postcode_norm_k_numsig_idx", "postcode_norm", "k_numsig"),
        Index(
            "epc_addrkey_postcode_norm_k_tokenset_idx", "postcode_norm", "k_tokenset"
        ),
    )

    postcode_norm: Mapped[str] = mapped_column(Text, nullable=True)
    k_exact: Mapped[str] = mapped_column(Text, nullable=True)
    k_tokenset: Mapped[str] = mapped_column(Text, nullable=True)
    k_numsig: Mapped[str] = mapped_column(Text, nullable=True)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)

    __mapper_args__ = {"primary_key": (postcode_norm, k_exact, uprn)}


class EpcAddrKeyUprnOk(StagingBase):
    """(postcode, UPRN) gazetteer pairs passing the Code-Point Open guard, ~2.69M rows (intermediate)."""

    __tablename__ = "_epc_addrkey_uprn_ok"
    __table_args__ = (
        Index(
            "_epc_addrkey_uprn_ok_postcode_norm_uprn_idx",
            "postcode_norm",
            "uprn",
            unique=True,
        ),
    )

    postcode_norm: Mapped[str] = mapped_column(Text, nullable=True)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=True)

    __mapper_args__ = {"primary_key": (postcode_norm, uprn)}


class PpdTarget(StagingBase):
    """
    Unmatched Price Paid transactions the matcher places, 126,326 rows (intermediate).

    Keyed the same four ways as the gazetteer, so each tier is one equi-join.
    """

    __tablename__ = "_ppd_target"
    __table_args__ = (
        Index("_ppd_target_postcode_norm_k_exact_idx", "postcode_norm", "k_exact"),
        Index("_ppd_target_postcode_norm_k_numsig_idx", "postcode_norm", "k_numsig"),
        Index(
            "_ppd_target_postcode_norm_k_tokenset_idx", "postcode_norm", "k_tokenset"
        ),
    )

    transactionid: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)
    postcode_norm: Mapped[str] = mapped_column(Text, nullable=True)
    k_exact: Mapped[str] = mapped_column(Text, nullable=True)
    k_tokenset: Mapped[str] = mapped_column(Text, nullable=True)
    k_numsig: Mapped[str] = mapped_column(Text, nullable=True)


class PpdUprnLinkage(StagingBase):
    """
    The matcher's output: transaction to UPRN with the producing tier, 117,485 rows.

    Four tiers of decreasing strictness, each guarded against Code-Point Open.
    Provenance for the fusion, so it is not promoted.
    """

    __tablename__ = "ppd_uprn_linkage"

    transactionid: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=False)
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)


class PpdUprnLinkageBackfill(StagingBase):
    """
    The same matcher run over May-October 2024, 28,327 rows (migration 0017).

    Sales ukhpucl was cut before they were registered. A table of its own
    because the fusion verification requires ``ppd_uprn_linkage`` to stay
    inside the fusion window.
    """

    __tablename__ = "ppd_uprn_linkage_backfill"

    transactionid: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    uprn: Mapped[int] = mapped_column(BigInteger, nullable=False)
    address_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dateoftransfer: Mapped[date] = mapped_column(Date, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=True)
    propertytype: Mapped[str] = mapped_column(String, nullable=True)
    duration: Mapped[str] = mapped_column(String, nullable=True)


# Geography and Indices of Deprivation (migration 0011); only the derived
# per-transaction feature tables are promoted.


class PropertyLsoa(StagingBase):
    """
    The LSOA 2011 each transaction's coordinate falls in, 1,923,533 rows.

    ``assignment_method`` distinguishes point-in-polygon hits from the 150 m
    nearest-boundary fallback, whose distance is ``assignment_dist_m``.
    """

    __tablename__ = "property_lsoa"
    __table_args__ = (
        Index("idx_prod_property_lsoa_lsoa11cd", "lsoa11cd"),
        Index("idx_prod_property_lsoa_ppd_id", "ppd_id", unique=True),
    )

    ppd_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    lsoa11cd: Mapped[str] = mapped_column(String, nullable=True)
    assignment_method: Mapped[str] = mapped_column(Text, nullable=True)
    assignment_dist_m: Mapped[float] = mapped_column(Float, nullable=True)

    __mapper_args__ = {"primary_key": (ppd_id,)}


class IodLsoaSpine(StagingBase):
    """
    `IodLsoaRaw` reconciled onto LSOA 2011 across all releases, 130,204 rows.

    London-relative percentiles and neighbourhood contrasts are precomputed;
    ``mapping_method`` records how each release's LSOA vintage was mapped.
    """

    __tablename__ = "iod_lsoa_spine"
    __table_args__ = (
        Index(
            "idx_prod_iod_lsoa_spine_release_lsoa",
            "release_year",
            "lsoa11cd",
            unique=True,
        ),
    )

    release_year: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
    )
    lsoa11cd: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    lad_code_raw: Mapped[str] = mapped_column(String, nullable=True)
    imd_score: Mapped[float] = mapped_column(Float, nullable=True)
    imd_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    income_score: Mapped[float] = mapped_column(Float, nullable=True)
    income_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    employment_score: Mapped[float] = mapped_column(Float, nullable=True)
    employment_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    education_score: Mapped[float] = mapped_column(Float, nullable=True)
    education_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    health_score: Mapped[float] = mapped_column(Float, nullable=True)
    health_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    crime_score: Mapped[float] = mapped_column(Float, nullable=True)
    crime_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    barriers_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    living_env_score: Mapped[float] = mapped_column(Float, nullable=True)
    living_env_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    idaci_score: Mapped[float] = mapped_column(Float, nullable=True)
    idaci_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    idaopi_score: Mapped[float] = mapped_column(Float, nullable=True)
    idaopi_rank: Mapped[int] = mapped_column(Integer, nullable=True)
    children_score: Mapped[float] = mapped_column(Float, nullable=True)
    adult_skills_score: Mapped[float] = mapped_column(Float, nullable=True)
    geo_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    wider_barriers_score: Mapped[float] = mapped_column(Float, nullable=True)
    indoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    outdoors_score: Mapped[float] = mapped_column(Float, nullable=True)
    total_population: Mapped[int] = mapped_column(Integer, nullable=True)
    imd_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    income_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    education_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    outdoors_pct_london: Mapped[float] = mapped_column(Float, nullable=True)
    imd_decile_national: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    imd_score_vs_lad: Mapped[float] = mapped_column(Float, nullable=True)
    imd_score_nbr_1km: Mapped[float] = mapped_column(Float, nullable=True)
    imd_contrast_1km: Mapped[float] = mapped_column(Float, nullable=True)
    income_contrast_1km: Mapped[float] = mapped_column(Float, nullable=True)
    mapping_method: Mapped[str] = mapped_column(Text, nullable=True)


class IodReleases(StagingBase):
    """
    The four IoD releases: year, publication date, LSOA vintage and source file.

    Written by migration 0011 itself. ``published_date`` drives the embargo
    join, so a sale sees only releases published before it.
    """

    __tablename__ = "iod_releases"

    release_year: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
        autoincrement=False,
    )
    published_date: Mapped[date] = mapped_column(Date, nullable=False)
    lsoa_vintage: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reference_year: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    source_filename: Mapped[str] = mapped_column(Text, nullable=False)


# Derived views and housekeeping.


class PtalLondon(StagingBase):
    """
    Materialized view of `Ptal2023` joined to `PtalCoords` within London, 159,451 rows.

    Built by migration 0012. ``ptal_band_rank`` ordinally encodes the band
    labels, added here so the landing table stays as TfL published it.
    """

    __tablename__ = "ptal_london"
    __table_args__ = (
        Index("idx_staging_ptal_london_band_rank", "ptal_band_rank"),
        Index("idx_staging_ptal_london_geom", "geom", postgresql_using="gist"),
        Index("idx_staging_ptal_london_grid_id", "grid_id", unique=True),
    )

    grid_id: Mapped[int] = mapped_column(Integer, nullable=True)
    bus: Mapped[float] = mapped_column(Float, nullable=True)
    lul: Mapped[float] = mapped_column(Float, nullable=True)
    rail: Mapped[float] = mapped_column(Float, nullable=True)
    tram: Mapped[float] = mapped_column(Float, nullable=True)
    ai: Mapped[float] = mapped_column(Float, nullable=True)
    ptal_band: Mapped[str] = mapped_column(Text, nullable=True)
    ptal_band_rank: Mapped[int] = mapped_column(SmallInteger, nullable=True)
    shape_leng: Mapped[float] = mapped_column(Float, nullable=True)
    shape_area: Mapped[float] = mapped_column(Float, nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=True)
    easting: Mapped[float] = mapped_column(Float, nullable=True)
    northing: Mapped[float] = mapped_column(Float, nullable=True)
    geom: Mapped[str] = mapped_column(Geometry("Point", 4326), nullable=True)

    __mapper_args__ = {"primary_key": (grid_id,)}


class UkhcsRefreshMarker(StagingBase):
    r"""
    Records which OS Open UPRN extracts have been appended to `UKHC`.

    Migration 0003 does a full ``\copy`` of the 202604 extract, then an
    insert-only append of UPRNs minted since. The append deliberately skips
    ~192,053 coordinate revisions, which would otherwise move every spatial
    feature.
    """

    __tablename__ = "_ukhcs_refresh_marker"

    revision: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    extract_fp: Mapped[str] = mapped_column(Text, nullable=False)
    max_id_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
