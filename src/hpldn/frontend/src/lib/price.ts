/**
 * Helpers for pricing a specific home on a chosen day.
 *
 * The price itself comes from the read API (`POST /price`). These helpers
 * decide whether the form's input is worth sending and how an address reads.
 * The server checks the date again against London's calendar.
 */
import type { AddressOption, AddressSuggestion } from '../data/types';

/** How far ahead a sale date may be, in days; mirrors the server's limit. */
export const MAX_HORIZON_DAYS = 5 * 365;

const UK_POSTCODE = /^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$/;

/**
 * Checks for a full UK postcode, in any case and spacing, e.g. "cr0 1jn".
 */
export function isFullPostcode(input: string): boolean {
  return UK_POSTCODE.test(input.toUpperCase().replace(/\s+/g, ''));
}

/** Words that name the flat before its number, e.g. "Flat 3". */
const FLAT_WORDS = new Set(['FLAT', 'FLT', 'APARTMENT', 'APT', 'UNIT', 'NO', 'ROOM', 'STUDIO']);

/** An outward code with, perhaps, the start of the inward one: "CR0", "SW11 1A". */
const POSTCODE_PREFIX = /^[A-Z]{1,2}\d[A-Z\d]?( \d[A-Z]{0,2})?$/;

/**
 * Checks whether typed address text starts with a house number, the way the
 * server's `parse_query` reads it: "18 barclay road" and "flat 3 high street"
 * do; "barclay road cr0" and "SW11 1A" do not, although both contain digits.
 */
export function hasHouseNumber(text: string): boolean {
  const normal = text
    .toUpperCase()
    .replace(/['\u2019`]/g, '')
    .replace(/[^A-Z0-9]+/g, ' ')
    .trim();
  if (!normal || POSTCODE_PREFIX.test(normal) || isFullPostcode(normal)) {
    return false;
  }
  const tokens = normal.split(' ');
  let index = 0;
  while (index < tokens.length && FLAT_WORDS.has(tokens[index])) {
    index += 1;
  }
  return index < tokens.length && /\d/.test(tokens[index]);
}

/**
 * Formats a date as an ISO day in local time, e.g. "2026-09-15".
 */
export function isoDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return date.getFullYear() + '-' + month + '-' + day;
}

/**
 * Returns the ISO day `days` after `from`.
 */
export function addDays(from: Date, days: number): string {
  const next = new Date(from.getFullYear(), from.getMonth(), from.getDate() + days);
  return isoDate(next);
}

/**
 * Returns the earliest sale date that can be priced: tomorrow.
 */
export function earliestSaleDate(today: Date = new Date()): string {
  return addDays(today, 1);
}

/**
 * Returns the latest sale date that can be priced.
 */
export function latestSaleDate(today: Date = new Date()): string {
  return addDays(today, MAX_HORIZON_DAYS);
}

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
const UK_DAY = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/;

/**
 * Checks that a day, month and year name a real calendar day, e.g. not 31 February.
 */
function isRealDay(year: number, month: number, day: number): boolean {
  if (year < 1000 || month < 1 || month > 12 || day < 1) {
    return false;
  }
  return day <= new Date(year, month, 0).getDate();
}

/**
 * Formats an ISO day the UK way, e.g. "2026-09-15" becomes "15/09/2026".
 *
 * @returns An empty string when the input is not a real ISO day.
 */
export function formatUkDate(iso: string): string {
  const match = ISO_DAY.exec(iso);
  if (!match || !isRealDay(Number(match[1]), Number(match[2]), Number(match[3]))) {
    return '';
  }
  return match[3] + '/' + match[2] + '/' + match[1];
}

/**
 * Reads a UK date as typed, e.g. "15/10/2026" or "5/1/2027", as an ISO day.
 *
 * @returns The ISO day, or null when the text is not a whole, real date.
 */
export function parseUkDate(text: string): string | null {
  const match = UK_DAY.exec(text.trim());
  if (!match) {
    return null;
  }
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = Number(match[3]);
  if (!isRealDay(year, month, day)) {
    return null;
  }
  return match[3] + '-' + String(month).padStart(2, '0') + '-' + String(day).padStart(2, '0');
}

/**
 * Masks typing in a date field to DD/MM/YYYY, adding the slashes as digits arrive.
 *
 * A pasted ISO day is turned round, so "2026-10-15" reads "15/10/2026".
 */
export function maskUkDate(text: string): string {
  const iso = formatUkDate(text.trim());
  if (iso) {
    return iso;
  }
  const digits = text.replace(/\D/g, '').slice(0, 8);
  if (digits.length <= 2) {
    return digits;
  }
  if (digits.length <= 4) {
    return digits.slice(0, 2) + '/' + digits.slice(2);
  }
  return digits.slice(0, 2) + '/' + digits.slice(2, 4) + '/' + digits.slice(4);
}

/**
 * Checks a sale date from the form.
 *
 * ISO days compare correctly as strings, so no time zone enters the check.
 *
 * @param value The field's ISO day, YYYY-MM-DD, or empty while what is typed is not a date.
 * @param today The day to measure from.
 * @param typed What the field shows, so a well-formed day that does not exist,
 *   such as 31/02/2027, is named as such rather than as a format mistake.
 * @returns A sentence for the field, or undefined when the date is fine.
 */
export function saleDateError(value: string, today: Date = new Date(), typed?: string): string | undefined {
  if (!formatUkDate(value)) {
    const shown = (typed ?? '').trim();
    if (UK_DAY.test(shown)) {
      return 'There is no ' + shown + '. Check the day and month.';
    }
    return 'Enter the date as DD/MM/YYYY.';
  }
  const earliest = earliestSaleDate(today);
  if (value < earliest) {
    return 'Choose a date from ' + formatUkDate(earliest) + '.';
  }
  const latest = latestSaleDate(today);
  if (value > latest) {
    return 'Choose a date up to ' + formatUkDate(latest) + '.';
  }
  return undefined;
}

/**
 * Title-cases an all-capitals register value, leaving mixed case alone.
 *
 * "BARCLAY ROAD" reads "Barclay Road"; "Flat 61" stays as it is.
 */
export function readable(value: string): string {
  if (value !== value.toUpperCase()) {
    return value;
  }
  return value.toLowerCase().replace(/(^|[\s,(-])([a-z])/g, (_, gap: string, letter: string) => gap + letter.toUpperCase());
}

/**
 * Describes a home on one line, without its postcode, e.g. "18 Barclay Road, Croydon".
 *
 * A flat leads with its unit, e.g. "Flat 6, 17 Woodville Road, London". A
 * suggestion the register holds no street for shows the street it was found
 * under, e.g. "232a Battersea Bridge Road, London".
 */
export function describeAddress(address: AddressOption | AddressSuggestion): string {
  const street = address.poststreet ?? ('streetHint' in address ? address.streetHint : null);
  const first = [address.postnumber, street].filter(Boolean).map((part) => readable(String(part)));
  const line = (address.unit ? readable(address.unit) + ', ' : '') + first.join(' ');
  return address.posttown ? line + ', ' + readable(address.posttown) : line;
}

/**
 * Describes a home on one line with its postcode, e.g. "18 Barclay Road, Croydon, CR0 1JN".
 */
export function describeFullAddress(address: AddressOption): string {
  return describeAddress(address) + ', ' + address.postcode;
}

/**
 * A stable key for an address option, used as its suggestion's key; each flat of a building has its own.
 */
export function addressKey(address: AddressOption): string {
  return [address.unit ?? '', address.postnumber, address.poststreet ?? '', address.posttown, address.postcode].join('|');
}
