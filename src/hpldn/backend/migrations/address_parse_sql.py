"""
Split a certificate's address into the register's fields, and key a flat's unit (migration 0030).

``production.address_table`` holds a home's address the way HM Land Registry's
Price Paid register writes it (0005's convention): ``postnumber`` is the PAON
(a house number, a building name, or "BUILDING, NUMBER"), ``poststreet`` the
street, and since 0030 ``unit`` the SAON ("FLAT 3"). EPC certificates write
the same address as up to three free-text lines ("Flat 161 Faunce House" /
"Doddington Grove"). 0006's parser took the first line starting with a digit
as number + street, which agreed with the register on 78 % of the 1.88M
London UPRNs that have both; `PARSE_EPC_ADDRESS_SQL` agrees on
`MIN_PARSER_AGREEMENT` or more.

`UNIT_KEY_SQL` keys a unit so "Flat 6", "Apartment 6" and "6" are one home:
the flat words are those ``staging.hpldn_unit_markers`` collapses to ``F``
(`sale_linking.LINK_FUNCTIONS_SQL`), and what remains keeps its order, as
``staging.hpldn_numseq`` does. It lives in ``production`` and calls nothing in
``staging``, because the serving database (`scripts.export_serving_db`) has
no staging schema; `unit_key` is the same rule in Python, for the API.
"""

import re
from typing import Optional

# Agreement of the parse with the address rows of UPRNs that have both
# (postnumber and street, compared by staging.hpldn_addrkey).
MIN_PARSER_AGREEMENT = 0.95

# The words that name a flat: staging.hpldn_unit_markers' "F" vocabulary
# (FLAT APARTMENT APT MAISONETTE PENTHOUSE STUDIO, and F itself, which is
# hpldn_addrkey's form of FLAT/UNIT/FLT), plus UNIT, FLT and NO.
UNIT_WORDS = ("FLAT", "FLT", "F", "APARTMENT", "APT", "UNIT", "MAISONETTE", "PENTHOUSE", "STUDIO", "NO")

# Words ending a numbered line that name a building, not a street: "22 EXMOOR
# HOUSE" before "CLYDESDALE WAY" is flat 22 of Exmoor House.
BUILDING_WORDS = (
    "HOUSE", "COURT", "APARTMENTS", "MANSIONS", "LODGE", "BUILDING", "TOWER", "POINT", "HALL",
    "WHARF", "HEIGHTS", "CHAMBERS", "BLOCK", "CENTRE", "STUDIOS", "WORKS", "FLATS", "LOFTS",
)

_UNIT_ALT = "FLAT|FLT|APARTMENT|APT|UNIT|MAISONETTE|STUDIO|PENTHOUSE|ROOM"

# Words that make a line a street rather than a locality ("GLYN ROAD", not
# "ENFIELD"), when no line starts with a house number.
# ("COURT" is left out: after a flat, "FARNSWORTH COURT" is the building.)
STREET_WORDS = (
    "ROAD", "STREET", "AVENUE", "LANE", "GARDENS", "CLOSE", "DRIVE", "WAY", "PLACE", "GROVE",
    "CRESCENT", "TERRACE", "SQUARE", "HILL", "PARK", "RISE", "WALK", "MEWS", "GREEN", "VALE", "ROW",
    "PARADE", "BOULEVARD", "EMBANKMENT", "QUAY", "APPROACH", "YARD", "GATE", "END", "VIEW", "PATH",
    "STRAND", "CIRCUS", "HIGHWAY", "BROADWAY", "VILLAS", "PASSAGE", "ALLEY",
    "ST", "RD", "AVE", "GDNS", "CL", "DR", "PL", "SQ", "TER", "LN", "CRES", "GR",
)

PARSE_EPC_ADDRESS_SQL = rf"""
CREATE OR REPLACE FUNCTION staging.hpldn_parse_epc_address(
    a1 text, a2 text, a3 text,
    OUT unit text, OUT postnumber text, OUT poststreet text
)
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $fn$
DECLARE
    parts text[] := '{{}}';
    p text;
    m text[];
    i integer;
    s integer := 0;
    num text;
    bldg text[] := '{{}}';
    units text[] := '{{}}';
BEGIN
    -- 1. One part per comma-separated piece of the three lines, upper-cased;
    --    a leading unit ("FLAT 161 FAUNCE HOUSE") is a part of its own.
    --    Apostrophes and full stops go, as the register writes "ST JOHNS HILL".
    FOREACH p IN ARRAY string_to_array(concat_ws(',', a1, a2, a3), ',') LOOP
        p := btrim(regexp_replace(translate(upper(p), $q$'’`.$q$, ''), '[[:space:]]+', ' ', 'g'), ' ;');
        CONTINUE WHEN p = '' OR p = 'LONDON' OR p = parts[cardinality(parts)];
        -- "77 B" is 77B.
        p := regexp_replace(p, '^([0-9]+) ([A-Z])$', '\1\2');
        m := regexp_match(p, '^(({_UNIT_ALT}) ?([A-Z]?[0-9]+[A-Z]?(/[A-Z0-9]+)?|[A-Z])) (.+)$');
        IF m IS NOT NULL THEN
            parts := array_append(parts, m[1]);
            p := m[5];
        END IF;
        -- "1A 9 CUMBERLAND STREET": flat 1A of number 9.
        m := regexp_match(p, '^([0-9]+[A-Z]?) ([0-9]+[A-Z]? [A-Z].*)$');
        IF m IS NOT NULL THEN
            parts := array_append(parts, m[1]);
            p := m[2];
        END IF;
        parts := array_append(parts, p);
    END LOOP;

    -- 2. A bare number joins the words after it: "60", "KENILWORTH GARDENS".
    i := 1;
    WHILE i < cardinality(parts) LOOP
        IF parts[i] ~ '^[0-9]+[A-Z]?( ?- ?[0-9]+[A-Z]?)?$' AND parts[i + 1] ~ '^[A-Z]' THEN
            parts[i] := parts[i] || ' ' || parts[i + 1];
            parts := parts[1:i] || parts[i + 2:cardinality(parts)];
        END IF;
        i := i + 1;
    END LOOP;

    -- 3. The street: the last part starting with a house number, unless a
    --    part follows it and the words after the number name a building, or
    --    name no street while a later part does ("7 CARBERRY", "LITTLE STRAND").
    FOR i IN REVERSE cardinality(parts)..1 LOOP
        m := regexp_match(parts[i], '^([0-9]+[A-Z]?( ?- ?[0-9]+[A-Z]?)?) ([A-Z].*)$');
        CONTINUE WHEN m IS NULL;
        CONTINUE WHEN i < cardinality(parts)
                  AND (m[3] ~ ' ({"|".join(BUILDING_WORDS)})$'
                       OR (m[3] !~ '\m({"|".join(STREET_WORDS)})\M'
                           AND array_to_string(parts[i + 1:cardinality(parts)], ',')
                               ~ '\m({"|".join(STREET_WORDS)})\M'));
        s := i;
        num := regexp_replace(m[1], ' ', '', 'g');
        poststreet := m[3];
        EXIT;
    END LOOP;
    -- No numbered street: the last of several parts is the street, or the
    -- one before it when only that one names a street ("GLYN ROAD", "ENFIELD").
    IF s = 0 AND cardinality(parts) >= 2 THEN
        s := cardinality(parts);
        IF s >= 3 AND parts[s] !~ '\m({"|".join(STREET_WORDS)})\M'
                  AND parts[s - 1] ~ '\m({"|".join(STREET_WORDS)})\M' THEN
            s := s - 1;
        END IF;
        poststreet := parts[s];
    END IF;
    IF s = 0 THEN
        s := cardinality(parts) + 1;
    END IF;

    -- 4. Before the street: units (flat words, floors, a bare number) and the
    --    building ("309 CITY PAVILION" is flat 309 of City Pavilion).
    FOR i IN 1..s - 1 LOOP
        p := parts[i];
        IF p ~ '\m({_UNIT_ALT}|FLOOR|FLOORS|BASEMENT)\M' OR p ~ '^[0-9]+[A-Z]?$' THEN
            units := array_append(units, p);
        ELSE
            m := regexp_match(p, '^([0-9]+[A-Z]?) ([A-Z].*)$');
            IF m IS NOT NULL THEN
                units := array_append(units, m[1]);
                bldg := array_append(bldg, m[2]);
            ELSE
                bldg := array_append(bldg, p);
            END IF;
        END IF;
    END LOOP;

    unit := NULLIF(array_to_string(units, ' '), '');
    postnumber := CASE
        WHEN cardinality(bldg) > 0 AND num IS NOT NULL THEN array_to_string(bldg, ' ') || ', ' || num
        WHEN num IS NOT NULL THEN num
        ELSE NULLIF(array_to_string(bldg, ' '), '')
    END;
    poststreet := NULLIF(poststreet, '');
END
$fn$
"""

UNIT_KEY_SQL = rf"""
CREATE OR REPLACE FUNCTION production.hpldn_unit_key(unit text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    -- "Flat 6" = "Apartment 6" = "6"; "Flat F" = "F"; floors keep their words.
    -- A flat word goes unless it is the last word (the letter of "FLAT F").
    SELECT NULLIF(array_to_string(array(
        SELECT u.t
        FROM unnest(x.w) WITH ORDINALITY AS u(t, i)
        WHERE u.t <> ALL (ARRAY[{", ".join(f"'{w}'" for w in UNIT_WORDS)}]) OR u.i = cardinality(x.w)
        ORDER BY u.i
    ), ' '), '')
    FROM (
        SELECT string_to_array(
            NULLIF(btrim(regexp_replace(upper(coalesce(unit, '')), '[^A-Z0-9]+', ' ', 'g')), ''), ' '
        ) AS w
    ) x
$fn$
"""

UNIT_KEY_FUNCTION_NAME = "production.hpldn_unit_key"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def unit_key(unit: Optional[str]) -> Optional[str]:
    """
    Return `UNIT_KEY_SQL`'s key for `unit` ("Flat 6" -> "6"), or None for no unit.

    >>> unit_key("Apartment 6"), unit_key("FLAT F"), unit_key("Ground Floor Flat"), unit_key(" ")
    ('6', 'F', 'GROUND FLOOR FLAT', None)
    """
    if not isinstance(unit, str):
        return None
    tokens = _NON_ALNUM.sub(" ", unit.upper()).split()
    kept = [t for i, t in enumerate(tokens) if t not in UNIT_WORDS or i == len(tokens) - 1]
    return " ".join(kept) or None


# The parse against the register: the address rows whose text is a held
# sale's own PAON and street (0005's rows, not those an earlier parser minted
# from a certificate), for the London UPRNs that also have a certificate, each
# compared with its newest certificate. Apostrophes and full stops are ignored
# on both sides ("ST MARY'S LANE" is "ST MARYS LANE"). 0006's parser agreed on
# 76.8 % of a 1-in-50 sample; this one on 96.3 %.
PARSER_AGREEMENT_SQL = """
WITH register AS (
    SELECT DISTINCT a.id, uk."UPRN" AS uprn, a.postnumber, a.poststreet
    FROM staging.ppd_table p
    JOIN staging.ppd_domestic d ON d.transactionid = p.transactionid
    JOIN staging.address_table a ON a.id = p.address_id
    JOIN staging.ukhcs_table uk ON uk.id = a.uid
    WHERE staging.hpldn_addrkey(d.paon) = staging.hpldn_addrkey(a.postnumber)
      AND staging.hpldn_addrkey(d.street) IS NOT DISTINCT FROM staging.hpldn_addrkey(a.poststreet)
), newest AS (
    SELECT DISTINCT ON (h.uprn) h.uprn, h.address1, h.address2, h.address3
    FROM staging.epc_domestic_history h
    WHERE h.region = 'E12000007' AND h.uprn IN (SELECT uprn FROM register)
    ORDER BY h.uprn, h.lodgement_datetime DESC NULLS LAST, h.certificate_number DESC
)
SELECT count(*) AS compared,
       count(*) FILTER (
           WHERE staging.hpldn_addrkey(translate(x.postnumber, chr(39) || '.', ''))
                 = staging.hpldn_addrkey(translate(r.postnumber, chr(39) || '.', ''))
             AND staging.hpldn_addrkey(translate(x.poststreet, chr(39) || '.', ''))
                 IS NOT DISTINCT FROM staging.hpldn_addrkey(translate(r.poststreet, chr(39) || '.', ''))
       ) AS agree
FROM newest e
JOIN register r ON r.uprn = e.uprn
CROSS JOIN LATERAL staging.hpldn_parse_epc_address(e.address1, e.address2, e.address3) x
"""
