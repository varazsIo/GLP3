"""Shared constants and normalised code mappings for the staging pipeline."""

LONDON_DISTRICTS = (
    "BARKING AND DAGENHAM", "BARNET", "BEXLEY", "BRENT", "BROMLEY", "CAMDEN",
    "CITY OF LONDON", "CROYDON", "EALING", "ENFIELD", "GREENWICH", "HACKNEY",
    "HAMMERSMITH AND FULHAM", "HARINGEY", "HARROW", "HAVERING", "HILLINGDON",
    "HOUNSLOW", "ISLINGTON", "KENSINGTON AND CHELSEA", "KINGSTON UPON THAMES",
    "LAMBETH", "LEWISHAM", "MERTON", "NEWHAM", "REDBRIDGE", "RICHMOND UPON THAMES",
    "SOUTHWARK", "SUTTON", "TOWER HAMLETS", "WALTHAM FOREST", "WANDSWORTH",
    "CITY OF WESTMINSTER",
)

LONDON_DISTRICTS_SQL = ", ".join(f"'{d}'" for d in LONDON_DISTRICTS)

MIN_PLAUSIBLE_PRICE = 10_000

# The fewest comparable certificates a home without its own certificate is
# estimated from (hpp_inference.estimate_epc), and the fewest usable
# certificates that make a never-sold, never-certified numbered register row's
# postcode residential enough to list (migrations/address_search.py).
MIN_EPC_NEIGHBOURS = 5

MIN_PLAUSIBLE_FLOOR_AREA_SQM = 15.0

MIN_HABITABLE_ROOMS = 0

MIN_PLAUSIBLE_ANNUAL_COST = 0.0

TENURE_MAPPING = {
    "NO DATA!": None,
    "Not defined": None,
    "Not defined - use in the case of a new dwelling for which the intended "
    "tenure in not known. It is not to be used for an existing dwelling": None,
    "owner-occupied": "O",
    "Owner-occupied": "O",
    "rental (private)": "P",
    "rental (social)": "S",
    "Rented (private)": "P",
    "Rented (social)": "S",
    "unknown": None,
    "None": None,
}

BUILD_FORM_MAPPING = {
    "Detached": 1,
    "Enclosed End-Terrace": 2,
    "Enclosed Mid-Terrace": 3,
    "End-Terrace": 4,
    "Mid-Terrace": 5,
    "Semi-Detached": 6,
    "NO DATA!": None,
    "None": None,
}

TRANSACTION_TYPE_MAPPING = {
    "assessment for green deal": 1,
    "ECO assessment": 2,
    "FiT application": 3,
    "following green deal": 4,
    "marketed sale": 5,
    "new dwelling": 6,
    "NO DATA!": None,
    "non marketed sale": 7,
    "none of the above": None,
    "not recorded": None,
    "not recorded - this is for backwards compatibility only and should not be used": None,
    "not sale or rental": None,
    "rental": 8,
    "rental (private)": 9,
    "rental (private) - this is for backwards compatibility only and should not be used": 9,
    "rental (social)": 10,
    "rental (social) - this is for backwards compatibility only and should not be used": 10,
    "RHI application": 11,
    "Stock condition survey": 12,
    "Stock Condition Survey": 12,
    "unknown": None,
    "None": None,
}

ENERGY_TARIFF_MAPPING = {
    "24 hour": 1,
    "dual": 2,
    "dual (24 hour)": 3,
    "off-peak 10 hour": 4,
    "off-peak 18 hour": 5,
    "off-peak 7 hour": 6,
    "Single": 7,
    "standard tariff": 8,
    "Unknown": 9,
    "None": None,
}

GLAZED_TYPE_MAPPING = {
    "double glazing installed before 2002": 1,
    "double glazing installed during or after 2002": 2,
    "double glazing, unknown install date": 3,
    "double, known data": 4,
    "INVALID!": 5,
    "not defined": 6,
    "secondary glazing": 7,
    "single glazing": 8,
    "triple glazing": 9,
    "triple, known data": 10,
    "None": None,
}

RATING_CATEGORY_MAPPING = {
    "Very Poor": 1,
    "Poor": 2,
    "Average": 3,
    "Good": 4,
    "Very Good": 5,
}

API_ROLE_WORK_MEM = "16MB"

FLOOR_LEVEL_BASEMENT = "BASEMENT"
FLOOR_LEVEL_GROUND = "GROUND"
FLOOR_LEVEL_TOP_BAND = "21+"

FLOOR_LEVEL_MAPPING = {
    "-1": FLOOR_LEVEL_BASEMENT,
    "Basement": FLOOR_LEVEL_BASEMENT,
    "basement": FLOOR_LEVEL_BASEMENT,
    "0": FLOOR_LEVEL_GROUND,
    "00": FLOOR_LEVEL_GROUND,
    "Ground": FLOOR_LEVEL_GROUND,
    "ground": FLOOR_LEVEL_GROUND,
    "ground floor": FLOOR_LEVEL_GROUND,
    "Ground floor": FLOOR_LEVEL_GROUND,
    "20+": FLOOR_LEVEL_TOP_BAND,
    "21st or above": FLOOR_LEVEL_TOP_BAND,
    "top floor": None,
    "mid floor": None,
}

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}
for _floor in range(1, 21):
    _suffix = "th" if 11 <= _floor <= 13 else _ORDINAL_SUFFIXES.get(_floor % 10, "th")
    _canonical = f"{_floor:02d}"
    FLOOR_LEVEL_MAPPING[str(_floor)] = _canonical
    FLOOR_LEVEL_MAPPING[_canonical] = _canonical
    FLOOR_LEVEL_MAPPING[f"{_floor}{_suffix}"] = _canonical
del _floor, _suffix, _canonical, _ORDINAL_SUFFIXES

FLOOR_LEVEL_CANONICAL = (
    (FLOOR_LEVEL_BASEMENT, FLOOR_LEVEL_GROUND)
    + tuple(f"{n:02d}" for n in range(1, 21))
    + (FLOOR_LEVEL_TOP_BAND,)
)
