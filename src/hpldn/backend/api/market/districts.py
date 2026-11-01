"""
The areas the page covers and the pure helpers shared by the market queries.

An area is every inner London postcode district, with its sub-districts folded
in, and one area per outer London postcode area. `area_of` maps a stored
outward code onto its area and `district_members` lists the outward codes an
area's SQL filters on.
"""

import math
import re

# Postcode areas that are one area each (BR3 and BR6 are both "BR").
OUTER_AREAS: tuple[str, ...] = (
    "BR", "CR", "DA", "EN", "HA", "IG", "KT", "RM", "SM", "TW", "UB", "WD",
)

# Code -> display name for all 132 covered areas, in page order. See `area_of`.
DISTRICTS: dict[str, str] = {
    # EC and WC grouped by line
    "EC1": "Barbican, Clerkenwell, Finsbury",
    "EC2": "Moorgate, Liverpool Street",
    "EC3": "Aldgate, Monument, Tower Hill",
    "EC4": "Fleet Street, St Paul's",
    "WC1": "Bloomsbury, Gray's Inn",
    "WC2": "Covent Garden, Holborn, Strand",

    "E1": "Mile End, Stepney, Whitechapel",
    "E2": "Bethnal Green, Shoreditch",
    "E3": "Bow, Bromley-by-Bow",
    "E4": "Chingford, Highams Park",
    "E5": "Clapton",
    "E6": "East Ham, Beckton",
    "E7": "Forest Gate, Upton Park",
    "E8": "Hackney, Dalston",
    "E9": "Hackney, Homerton",
    "E10": "Leyton",
    "E11": "Leytonstone",
    "E12": "Manor Park",
    "E13": "Plaistow",
    "E14": "Isle of Dogs, Millwall, Poplar",
    "E15": "Stratford, West Ham",
    "E16": "Canning Town, North Woolwich",
    "E17": "Walthamstow",
    "E18": "South Woodford",
    "E20": "Olympic Park, Stratford",

    "N1": "Barnsbury, Canonbury, Islington",
    "N2": "East Finchley",
    "N3": "Finchley Central",
    "N4": "Finsbury Park, Manor House",
    "N5": "Highbury",
    "N6": "Highgate",
    "N7": "Holloway",
    "N8": "Crouch End, Hornsey",
    "N9": "Lower Edmonton",
    "N10": "Muswell Hill",
    "N11": "Friern Barnet, New Southgate",
    "N12": "North Finchley, Woodside Park",
    "N13": "Palmers Green",
    "N14": "Southgate",
    "N15": "Seven Sisters",
    "N16": "Stamford Hill, Stoke Newington",
    "N17": "Tottenham",
    "N18": "Upper Edmonton",
    "N19": "Archway, Tufnell Park",
    "N20": "Totteridge, Whetstone",
    "N21": "Winchmore Hill",
    "N22": "Alexandra Palace, Wood Green",

    "NW1": "Camden Town, Regent's Park",
    "NW2": "Cricklewood, Neasden",
    "NW3": "Hampstead, Swiss Cottage",
    "NW4": "Brent Cross, Hendon",
    "NW5": "Kentish Town",
    "NW6": "Kilburn, Queens Park, West Hampstead",
    "NW7": "Mill Hill",
    "NW8": "St John's Wood",
    "NW9": "Colindale, Kingsbury",
    "NW10": "Harlesden, Kensal Green, Willesden",
    "NW11": "Golders Green, Hampstead Garden Suburb",

    "SE1": "Bermondsey, Borough, Southwark, Waterloo",
    "SE2": "Abbey Wood",
    "SE3": "Blackheath, Westcombe Park",
    "SE4": "Brockley, Crofton Park, Honor Oak Park",
    "SE5": "Camberwell",
    "SE6": "Bellingham, Catford, Hither Green",
    "SE7": "Charlton",
    "SE8": "Deptford",
    "SE9": "Eltham, Mottingham",
    "SE10": "Greenwich",
    "SE11": "Lambeth",
    "SE12": "Grove Park, Lee",
    "SE13": "Hither Green, Lewisham",
    "SE14": "New Cross, New Cross Gate",
    "SE15": "Nunhead, Peckham",
    "SE16": "Rotherhithe, South Bermondsey, Surrey Docks",
    "SE17": "Elephant & Castle, Walworth",
    "SE18": "Plumstead, Woolwich",
    "SE19": "Crystal Palace, Upper Norwood",
    "SE20": "Anerley, Penge",
    "SE21": "Dulwich",
    "SE22": "East Dulwich",
    "SE23": "Forest Hill",
    "SE24": "Herne Hill",
    "SE25": "South Norwood",
    "SE26": "Sydenham",
    "SE27": "Tulse Hill, West Norwood",
    "SE28": "Thamesmead",

    "SW1": "Belgravia, Pimlico, Westminster",
    "SW2": "Brixton, Streatham Hill",
    "SW3": "Brompton, Chelsea",
    "SW4": "Clapham",
    "SW5": "Earl's Court",
    "SW6": "Fulham, Parson's Green",
    "SW7": "South Kensington",
    "SW8": "Nine Elms, South Lambeth",
    "SW9": "Brixton, Stockwell",
    "SW10": "West Brompton, World's End",
    "SW11": "Battersea, Clapham Junction",
    "SW12": "Balham",
    "SW13": "Barnes, Castelnau",
    "SW14": "East Sheen, Mortlake",
    "SW15": "Putney, Roehampton",
    "SW16": "Norbury, Streatham",
    "SW17": "Tooting",
    "SW18": "Earlsfield, Wandsworth",
    "SW19": "Merton, Wimbledon",
    "SW20": "Raynes Park, South Wimbledon",

    "W1": "Marylebone, Mayfair, Soho",
    "W2": "Bayswater, Paddington",
    "W3": "Acton",
    "W4": "Chiswick",
    "W5": "Ealing",
    "W6": "Hammersmith",
    "W7": "Hanwell",
    "W8": "Kensington",
    "W9": "Maida Vale, Warwick Avenue",
    "W10": "Ladbroke Grove, North Kensington",
    "W11": "Holland Park, Notting Hill",
    "W12": "Shepherd's Bush",
    "W13": "West Ealing",
    "W14": "West Kensington",

    # Outer London: one area per postcode area
    "BR": "Bromley",
    "CR": "Croydon",
    "DA": "Dartford",
    "EN": "Enfield",
    "HA": "Harrow",
    "IG": "Ilford",
    "KT": "Kingston",
    "RM": "Romford",
    "SM": "Sutton",
    "TW": "Twickenham",
    "UB": "Uxbridge",
    "WD": "Watford",
}

# PPD property type codes other than F (flat or maisonette, split below).
TYPE_KEYS = {"D": "detached", "S": "semi-detached", "T": "terraced"}

# A floor area outside these bounds is an EPC data-entry error, not a home.
MIN_FLOOR_AREA = 15
MAX_FLOOR_AREA = 1000

# Walking pace used to turn a distance to the nearest station into minutes.
WALK_KMH = 4.8

# Furthest a "nearby" sale may be from the origin. Outer areas are a borough
# or more, and their homes further apart.
INNER_COMPARABLE_RADIUS_KM = 2.0
OUTER_COMPARABLE_RADIUS_KM = 5.0


# The smallest home that can hold a given number of bedrooms: a 15 m² studio
# for one, and 10 m² more for each further bedroom (a small double plus its
# share of hall and bathroom).
def min_floor_area_for(bedrooms: int) -> int:
    """Return the fewest m² a home with `bedrooms` bedrooms can have."""
    return MIN_FLOOR_AREA + 10 * (max(1, bedrooms) - 1)


def comparable_radius_km(area: str) -> float:
    """Return how far from the origin a nearby sale in `area` may be."""
    return OUTER_COMPARABLE_RADIUS_KM if area in OUTER_AREAS else INNER_COMPARABLE_RADIUS_KM

# The frontend's regexes (src/lib/estimate.ts), applied to compacted input.
_UK_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$")
_UK_DISTRICT = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
_DISTRICT_OF_POSTCODE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\d[A-Z]{2}$")

# A sub-district (SW1V, EC2M, W1G, E1W, N1C): letters, one digit, one letter.
_SUB_DISTRICT = re.compile(r"^([A-Z]{1,2}\d)[A-Z]$")
_SINGLE_DIGIT_DISTRICT = re.compile(r"^[A-Z]{1,2}\d$")
_LETTERS = re.compile(r"^[A-Z]+")


def property_type_key(propertytype: str, is_maisonette: bool | None) -> str:
    """
    Return the frontend property type for a sale.

    Parameters
    ----------
    propertytype : str
        PPD property type code: ``D``, ``S``, ``T`` or ``F``.
    is_maisonette : bool or None
        EPC-derived flag splitting PPD's single flat/maisonette code ``F``.
        An unknown flag (no EPC match) is treated as a flat.

    Returns
    -------
    str
        One of the `PropertyType` literals.
    """
    if propertytype == "F":
        return "maisonette" if is_maisonette else "flat"
    return TYPE_KEYS[propertytype]


def type_group(property_type: str) -> str:
    """
    Return the PPD group a frontend property type belongs to.

    ``"F"`` for flats and maisonettes, which PPD records under one code, and
    ``"house"`` for the rest. Comparables widen to the group when too few of
    the exact type sold nearby.
    """
    return "F" if property_type in ("flat", "maisonette") else "house"


def transport_score(ptal_band_rank: float | None) -> int | None:
    """
    Map PTAL band ranks 0-8 (0, 1a, 1b, 2, 3, 4, 5, 6a, 6b) onto a 1-5 score.

    Returns None without a rank: unknown access is not "limited" access.
    """
    if ptal_band_rank is None or math.isnan(ptal_band_rank):
        return None
    rank = round(ptal_band_rank)
    if rank <= 1:
        return 1
    if rank <= 3:
        return 2
    if rank == 4:
        return 3
    if rank <= 6:
        return 4
    return 5


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def street_name(raw: str | None) -> str:
    """
    Return the street or building name alone.

    Tokens carrying a digit (``"17,"``, ``"246-252"``) are dropped, and PPD's
    upper-case streets are title-cased to match the EPC-matched ones.
    """
    if not raw:
        return ""
    tokens = [t.strip(",-") for t in raw.replace(",", " ").split()]
    street = " ".join(t for t in tokens if t and not any(c.isdigit() for c in t))
    return street.title() if street.isupper() else street


def _compact(value: str) -> str:
    """Upper-case `value` and strip all whitespace."""
    return re.sub(r"\s+", "", value.upper())


def is_postcode_shaped(value: str) -> bool:
    """Return True for a full postcode or a bare district."""
    cleaned = _compact(value)
    return bool(_UK_POSTCODE.match(cleaned) or _UK_DISTRICT.match(cleaned))


def is_full_postcode(value: str) -> bool:
    """Return True for a full postcode only."""
    return bool(_UK_POSTCODE.match(_compact(value)))


def normalise_postcode(value: str) -> str:
    """
    Return the canonical postcode form.

    ``"sw112ab"`` becomes ``"SW11 2AB"``; a bare district is returned
    compacted and upper-cased.
    """
    cleaned = _compact(value)
    if _UK_POSTCODE.match(cleaned):
        return cleaned[:-3] + " " + cleaned[-3:]
    return cleaned


def area_of(outward: str) -> str:
    """
    Return the area an outward code belongs to.

    The rules apply in order:

    1. a sub-district folds into its parent: SW1V -> SW1, EC2M -> EC2;
    2. an outer London code is its letters: BR3 -> BR, CR0 -> CR;
    3. anything else is returned unchanged, so TN16 and E22 stay themselves
       and, not being in `DISTRICTS`, are not covered.
    """
    code = outward.upper()
    sub = _SUB_DISTRICT.match(code)
    if sub:
        return sub.group(1)
    letters = _LETTERS.match(code)
    if letters and letters.group(0) in OUTER_AREAS:
        return letters.group(0)
    return code


def district_members(code: str) -> list[str]:
    """
    Return every outward code ``production.london_houses`` may store for an area.

    Used in an ``= ANY(...)`` filter, which still uses the postcode_district
    index. A single-digit inner district also holds its sub-districts (SW1 is
    SW1 and SW1A..SW1Z, never SW11); an outer area holds BR0..BR99.
    """
    if code in OUTER_AREAS:
        return [f"{code}{n}" for n in range(100)]
    if _SINGLE_DIGIT_DISTRICT.match(code):
        return [code] + [code + chr(letter) for letter in range(ord("A"), ord("Z") + 1)]
    return [code]


def evidence_members(outward: str, area: str) -> list[str]:
    """
    Return the outward codes an estimate's evidence is drawn from.

    An outer area spans a borough or more, so BR3 uses BR3 alone (a bare
    ``"BR"`` still uses all of BR); an inner sub-district is too thin on its
    own, so SW1V uses all of SW1.
    """
    if area in OUTER_AREAS and outward != area:
        return [outward]
    return district_members(area)


def outward_of(value: str) -> str:
    """Return the outward code as typed, e.g. ``"SW1V"`` from ``"SW1V 4AB"`` or ``"sw1v"``."""
    cleaned = _compact(value)
    if _UK_DISTRICT.match(cleaned):
        return cleaned
    match = _DISTRICT_OF_POSTCODE.match(cleaned)
    return match.group(1) if match else cleaned


def district_of(value: str) -> str:
    """Return the area a postcode or outward code falls in, e.g. ``"SW1"`` from ``"sw1v 4ab"``."""
    return area_of(outward_of(value))


def round_to(value: float, step: int) -> int:
    """Round `value` to the nearest `step` with Python's banker's rounding."""
    return int(round(value / step) * step)


def round_significant(value: float, figures: int = 3) -> float:
    """
    Round `value` to `figures` significant figures, halves up.

    ``32.4959`` becomes ``32.5`` and ``9.0987`` becomes ``9.10``; zero stays zero.
    """
    if value == 0:
        return 0.0
    decimals = figures - 1 - math.floor(math.log10(abs(value)))
    scale = 10 ** decimals
    return math.floor(abs(value) * scale + 0.5) / scale * (1 if value > 0 else -1)


def round_half_up(value: float) -> int:
    """Round halves up, matching JavaScript's ``Math.round``."""
    return math.floor(value + 0.5)


def round_to_half_up(value: float, step: int) -> int:
    """Round `value` to the nearest `step`, halves up."""
    return int(round_half_up(value / step) * step)
