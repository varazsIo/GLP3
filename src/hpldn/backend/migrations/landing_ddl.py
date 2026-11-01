"""
DDL for the raw landing tables and the four ``staging.hpldn_*`` functions.

Migration 0002 executes everything here and 0003 fills the tables, so no load
targets a table that does not yet exist. Landing tables whose loader is a
``pipeline_steps`` step create themselves (`STEP_OWNED_LANDING_TABLES`).
"""

from hpldn.backend.scripts.pipeline_steps import (
    _CODEPOINT_OPEN_DDL,
    _EPC_DOMESTIC_COLUMNS_SQL,
    _EPC_DOMESTIC_HISTORY_DDL,
    _HPLDN_ADDRKEY_FUNCTIONS_SQL,
)

# The IMMUTABLE normalisation functions. Created in 0002 because
# staging.codepoint_open has a functional index on hpldn_pcnorm.
HPLDN_FUNCTIONS_SQL = _HPLDN_ADDRKEY_FUNCTIONS_SQL

HPLDN_FUNCTION_NAMES = (
    "hpldn_pcnorm",
    "hpldn_addrkey",
    "hpldn_tokenset",
    "hpldn_numsig",
)

# epc_domestic and epc_domestic_history share one 93-column shape and differ
# only in the scope seed_registry loads.
EPC_DOMESTIC_DDL = "CREATE TABLE staging.epc_domestic " + _EPC_DOMESTIC_COLUMNS_SQL

EPC_DOMESTIC_HISTORY_DDL = _EPC_DOMESTIC_HISTORY_DDL

# The 16-column PPD "complete" layout. The files have no header, so the order
# must match seed_registry.PPD_DOMESTIC_COLUMNS.
PPD_DOMESTIC_DDL = """
CREATE TABLE staging.ppd_domestic (
    transactionid  varchar,
    price          bigint,
    dateoftransfer timestamp without time zone,
    postcode       varchar,
    propertytype   varchar,
    oldnew         varchar,
    duration       varchar,
    paon           varchar,
    saon           varchar,
    street         varchar,
    locality       varchar,
    towncity       varchar,
    district       varchar,
    county         varchar,
    categorytype   varchar,
    recordstatus   varchar
)
"""

CODEPOINT_OPEN_DDL = _CODEPOINT_OPEN_DDL

# The 54,137-row hand-produced Tier A fixture, loaded by migration 0006.
PPD_UPRN_LINKAGE_TIERA_DDL = """
CREATE TABLE staging._ppd_uprn_linkage_tiera (
    transactionid  varchar,
    uprn           bigint,
    address_id     bigint,
    dateoftransfer date,
    price          bigint,
    propertytype   varchar,
    duration       varchar
)
"""

LSOA_DDL = """
CREATE TABLE staging.lsoa (
    lsoa11cd  varchar,
    lsoa11nm  varchar,
    lsoa21cd  varchar,
    lsoa21nm  varchar,
    lsoa21nmw varchar,
    lad22cd   varchar,
    lad22nm   varchar,
    lad22nmw  varchar,
    object_id integer,
    id        uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY
)
"""

MSOA_DDL = """
CREATE TABLE staging.msoa (
    msoa11cd  varchar,
    msoa11nm  varchar,
    chngind   varchar,
    msoa21cd  varchar,
    msoa21nm  varchar,
    lad22cd   varchar,
    lad22nm   varchar,
    lad22nmw  varchar,
    object_id integer,
    id        uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY
)
"""

# Execution order: tables first, then the functions the loads depend on.
LANDING_TABLES = (
    ("staging.epc_domestic", EPC_DOMESTIC_DDL),
    ("staging.epc_domestic_history", EPC_DOMESTIC_HISTORY_DDL),
    ("staging.ppd_domestic", PPD_DOMESTIC_DDL),
    ("staging.codepoint_open", CODEPOINT_OPEN_DDL),
    ("staging._ppd_uprn_linkage_tiera", PPD_UPRN_LINKAGE_TIERA_DDL),
    ("staging.lsoa", LSOA_DDL),
    ("staging.msoa", MSOA_DDL),
)

# Landing tables whose DDL belongs to their loader.
STEP_OWNED_LANDING_TABLES = (
    "staging.ptal_2023",
    "staging.ptal_coords",
    "staging.ptal_cell_polygons",
    "staging.rivers",
    "staging.inspire_polygons",
    "staging.lsoa_boundaries",
    "staging.lsoa_msoa_lu",
    "staging.iod_lsoa_raw",
    "staging.london_green_spaces",
    "staging.london_green_space_access_points",
)
