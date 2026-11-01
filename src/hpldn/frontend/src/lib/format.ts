/**
 * Formatting helpers.
 *
 * Hand-rolled rather than using `Intl`, so output is byte-stable across
 * environments for tests and screenshots, and safe for tabular figures.
 */

/**
 * Inserts thousands separators into a string of digits.
 */
function group(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/**
 * Formats whole pounds with separators, e.g. "£520,000".
 */
export function formatGBP(value: number): string {
  const rounded = Math.round(value);
  const sign = rounded < 0 ? '-' : '';
  return sign + '£' + group(String(Math.abs(rounded)));
}

/**
 * Formats pounds compactly for tight spaces, e.g. "£520k" or "£1.34m".
 *
 * Values under £1,000 use the full form.
 */
export function formatGBPCompact(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  if (abs >= 1_000_000) {
    return sign + '£' + (Math.round(abs / 10_000) / 100).toFixed(2) + 'm';
  }
  if (abs >= 1_000) {
    return sign + '£' + Math.round(abs / 1_000) + 'k';
  }
  return formatGBP(value);
}

/**
 * Formats a percentage to one decimal place by default, e.g. "4.0%", with
 * separators in a large one, e.g. "1,200.0%".
 *
 * @param value The percentage.
 * @param options `signed` adds "+" to positive values; `decimals` sets the precision.
 */
export function formatPercent(value: number, options?: { signed?: boolean; decimals?: number }): string {
  const decimals = options?.decimals ?? 1;
  const [whole, fraction] = Math.abs(value).toFixed(decimals).split('.');
  const fixed = group(whole) + (fraction === undefined ? '' : '.' + fraction);
  if (!options?.signed) {
    return (value < 0 ? '-' : '') + fixed + '%';
  }
  if (value > 0) {
    return '+' + fixed + '%';
  }
  if (value < 0) {
    return '-' + fixed + '%';
  }
  return '0' + (decimals > 0 ? '.' + '0'.repeat(decimals) : '') + '%';
}

/**
 * Rounds to `figures` significant figures, halves up, e.g. 32.4959 to 32.5.
 */
export function roundSignificant(value: number, figures = 3): number {
  if (value === 0 || !Number.isFinite(value)) {
    return value;
  }
  const decimals = figures - 1 - Math.floor(Math.log10(Math.abs(value)));
  const scale = 10 ** decimals;
  return (Math.sign(value) * Math.floor(Math.abs(value) * scale + 0.5)) / scale;
}

/**
 * Formats a percentage to `figures` significant figures, keeping trailing
 * zeros, e.g. 32.5 as "32.5%", 9.1 as "9.10%" and 100 as "100%".
 */
export function formatSignificantPercent(value: number, figures = 3): string {
  const rounded = roundSignificant(value, figures);
  if (rounded === 0) {
    return '0%';
  }
  const decimals = Math.max(0, figures - 1 - Math.floor(Math.log10(Math.abs(rounded))));
  return rounded.toFixed(decimals) + '%';
}

/**
 * Formats a whole percentage, e.g. "87%".
 */
export function formatWholePercent(value: number): string {
  return Math.round(value) + '%';
}

/**
 * Formats a floor area, e.g. "72 m²".
 */
export function formatArea(sqm: number): string {
  return group(String(Math.round(sqm))) + ' m²';
}

/**
 * Formats a distance in miles, which reads better to a UK landlord, e.g. "0.4 miles".
 *
 * @param km The distance in kilometres.
 */
export function formatDistance(km: number): string {
  const miles = km * 0.621371;
  if (miles < 0.1) {
    return 'under 0.1 miles';
  }
  return miles.toFixed(1) + ' miles';
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

/**
 * Formats an ISO date as month and year, e.g. "March 2025".
 *
 * Returns the input unchanged when it cannot be parsed.
 */
export function formatSoldDate(iso: string): string {
  const parts = iso.split('-');
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  if (!year || !month || month < 1 || month > 12) {
    return iso;
  }
  return MONTHS[month - 1] + ' ' + year;
}

/**
 * Formats an ISO date as its short month and year, e.g. "Mar 2023".
 */
export function formatShortMonth(iso: string): string {
  const parts = iso.split('-');
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  if (!year || !month || month < 1 || month > 12) {
    return iso;
  }
  return MONTHS[month - 1].slice(0, 3) + ' ' + year;
}

/**
 * Formats an ISO date as day, month and year, e.g. "15 September 2026".
 *
 * Returns the input unchanged when it cannot be parsed.
 */
export function formatLongDate(iso: string): string {
  const parts = iso.split('-');
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  const day = Number(parts[2]);
  if (!year || !month || month < 1 || month > 12 || !day || day < 1 || day > 31) {
    return iso;
  }
  return day + ' ' + MONTHS[month - 1] + ' ' + year;
}

/**
 * Formats a whole number with separators, e.g. "1,240".
 */
export function formatCount(value: number): string {
  return group(String(Math.round(value)));
}

/**
 * Keeps only the digits of what was typed, e.g. "£425,000" becomes "425000".
 */
export function stripNonDigits(text: string): string {
  return text.replace(/\D/g, '');
}

/**
 * Formats a typed whole number with separators as the landlord types, e.g. "4250" becomes "4,250".
 *
 * Leading zeros go; an empty or digit-free input stays empty.
 */
export function groupDigits(text: string): string {
  const digits = stripNonDigits(text).replace(/^0+(?=\d)/, '');
  return digits ? group(digits) : '';
}

/**
 * Formats a bedroom count, e.g. "3 bedrooms" or "1 bedroom".
 */
export function formatBedrooms(count: number): string {
  return count + (count === 1 ? ' bedroom' : ' bedrooms');
}
