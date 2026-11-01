/**
 * Postcode helpers for the estimate form.
 *
 * The estimate itself comes from the read API. These helpers decide whether
 * input is worth sending, and mirror the server's postcode rules
 * (`backend/api/market/districts.py`) so both sides agree on a district.
 */

/** Outer London postcode areas covered as one area each (BR3 and BR6 are both BR). */
export const OUTER_AREAS: ReadonlyArray<string> = [
  'BR', 'CR', 'DA', 'EN', 'HA', 'IG', 'KT', 'RM', 'SM', 'TW', 'UB', 'WD',
];

const UK_POSTCODE = /^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$/;
const UK_DISTRICT = /^[A-Z]{1,2}\d[A-Z\d]?$/;

/**
 * Strips spacing and case, so "sw11 2ab" and "SW112AB" compare equal.
 */
function compact(input: string): string {
  return input.toUpperCase().replace(/\s+/g, '');
}

/**
 * Formats a postcode for display, e.g. "sw112ab" becomes "SW11 2AB".
 *
 * A bare district is returned compacted and upper-cased.
 */
export function normalisePostcode(input: string): string {
  const cleaned = compact(input);
  if (UK_POSTCODE.test(cleaned)) {
    return cleaned.slice(0, cleaned.length - 3) + ' ' + cleaned.slice(-3);
  }
  return cleaned;
}

/**
 * Returns the area an outward code belongs to.
 *
 * A sub-district folds into its parent (SW1V to SW1), an outer London code
 * becomes its letters (BR3 to BR), and anything else is unchanged, so TN16
 * stays TN16 and is not covered.
 */
export function areaOf(outward: string): string {
  const code = outward.toUpperCase();
  const sub = code.match(/^([A-Z]{1,2}\d)[A-Z]$/);
  if (sub) {
    return sub[1];
  }
  const letters = code.match(/^[A-Z]+/);
  if (letters && OUTER_AREAS.indexOf(letters[0]) !== -1) {
    return letters[0];
  }
  return code;
}

/**
 * Returns the area a postcode or district falls in.
 *
 * For example "SW11" from "SW11 2AB", "SW1" from "sw1v 4ab", "BR" from "BR3 1AA".
 */
export function districtOf(input: string): string {
  const cleaned = compact(input);
  if (UK_DISTRICT.test(cleaned)) {
    return areaOf(cleaned);
  }
  const match = cleaned.match(/^([A-Z]{1,2}\d[A-Z\d]?)\d[A-Z]{2}$/);
  return areaOf(match ? match[1] : cleaned);
}

/**
 * Checks whether input is a full postcode or a bare district.
 */
export function isPostcodeShaped(input: string): boolean {
  const cleaned = compact(input);
  return UK_POSTCODE.test(cleaned) || UK_DISTRICT.test(cleaned);
}

/**
 * Checks whether input is a full postcode, not just a district such as "SW11".
 */
export function isFullPostcode(input: string): boolean {
  return UK_POSTCODE.test(compact(input));
}

/**
 * The fewest m² a home with this many bedrooms can have: a 15 m² studio for
 * one and 10 m² more for each further bedroom. Mirrors the server's
 * `min_floor_area_for`, which rejects anything smaller.
 */
export function minFloorAreaFor(bedrooms: number): number {
  return 15 + 10 * (Math.max(1, bedrooms) - 1);
}

/**
 * Checks whether a postcode's area is one of the covered areas.
 *
 * @param input The postcode or district as typed.
 * @param covered The area codes the API returned.
 */
export function isCoveredPostcode(input: string, covered: ReadonlySet<string>): boolean {
  return covered.has(districtOf(input));
}
