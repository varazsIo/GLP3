/**
 * The yearly costs the yield calculator assumes for any property, and the
 * mortgage rate and rent it reads from the live data.
 *
 * The calculator uses these and the AVM page prints them, so the two always
 * agree. The three costs are stated assumptions, not data; the mortgage rate
 * comes from the Bank of England rates the API serves with the area figures.
 */
import type { AreaStats, MortgageRates } from '../data/types';
import { formatSoldDate } from '../lib/format';

/** Letting and management, as a share of rent. */
export const LETTING_FEE = 0.1;
/** Upkeep, as a share of the purchase price, each year. */
export const MAINTENANCE = 0.01;
/** Landlord insurance, in pounds a year. */
export const INSURANCE_A_YEAR = 480;

/** The loan-to-value bands rates are published for, lowest first. */
export const LTV_BANDS = [60, 75, 85, 90, 95] as const;

/** One published loan-to-value band. */
export type LtvBand = (typeof LTV_BANDS)[number];

/** The rate that applies to a deposit, and the band it was read from. */
export interface MortgageRateChoice {
  /** Two-year fixed rate, in percent a year. */
  ratePercent: number;
  /** The loan-to-value band, e.g. 75 for a 25% deposit. */
  band: LtvBand;
  /** The month the rate describes, ISO date. */
  rateDate: string;
}

/**
 * Picks the published rate for a deposit: the lowest loan-to-value band that
 * covers the borrowing, e.g. a 25% deposit borrows 75% and takes the 75% rate,
 * a 20% deposit the 85% rate.
 *
 * @param rates The latest rates, or null when none are held.
 * @param depositPercent The deposit, 0 to 100.
 * @returns The rate, or null when nothing is borrowed, no rates are held, or
 *   the covering band has no published rate.
 */
export function mortgageRateFor(rates: MortgageRates | null, depositPercent: number): MortgageRateChoice | null {
  const ltv = 100 - depositPercent;
  if (!rates || ltv <= 0) {
    return null;
  }
  const band = LTV_BANDS.find((candidate) => candidate >= ltv);
  if (band === undefined) {
    return null;
  }
  const ratePercent = rates[('ltv' + band) as keyof Omit<MortgageRates, 'rateDate'>];
  return ratePercent === null || ratePercent === undefined ? null : { ratePercent, band, rateDate: rates.rateDate };
}

/** The bedroom counts ONS publishes a rent for; 4 stands for four or more. */
const BEDROOM_RENT_KEYS = { 1: 'oneBed', 2: 'twoBed', 3: 'threeBed', 4: 'fourPlusBed' } as const;

/**
 * The bedroom bucket ONS publishes a rent for: 1 to 4, where 4 is four or more.
 *
 * @param bedrooms Bedrooms, any whole number.
 */
export function rentBedrooms(bedrooms: number): 1 | 2 | 3 | 4 {
  return Math.min(Math.max(Math.round(bedrooms), 1), 4) as 1 | 2 | 3 | 4;
}

/**
 * The bedrooms a certificate's habitable rooms imply: one room is the living
 * room, so a home's bedrooms are its rooms less one, within 1 to 4 (4+).
 *
 * @param habitableRooms Habitable rooms on the certificate; null when not known.
 * @returns The bucket, or null when the rooms are not known.
 */
export function bedroomsFromRooms(habitableRooms: number | null): 1 | 2 | 3 | 4 | null {
  if (habitableRooms === null || !Number.isFinite(habitableRooms) || habitableRooms < 1) {
    return null;
  }
  return rentBedrooms(habitableRooms - 1);
}

/**
 * The borough's mean monthly rent for a home with these bedrooms.
 *
 * @returns The rent in pounds, or null when the area has none for the bucket.
 */
export function bedroomRent(area: AreaStats | null | undefined, bedrooms: number): number | null {
  return area?.bedroomRents?.[BEDROOM_RENT_KEYS[rentBedrooms(bedrooms)]] ?? null;
}

/**
 * The monthly rent the yield calculator pairs with a priced home: its
 * borough's mean rent for the bedrooms its habitable rooms imply, as the
 * server's `suggested_monthly_rent` picks one for an estimate.
 *
 * @param area The home's area; null or undefined when not covered.
 * @param habitableRooms Habitable rooms on its certificate; null when not known.
 * @returns The bedroom rent, else the borough's overall mean when the rooms or
 *   that bucket are not known, else null (no published rent, e.g. the City).
 */
export function homeMonthlyRent(area: AreaStats | null | undefined, habitableRooms: number | null): number | null {
  if (!area) {
    return null;
  }
  const bedrooms = bedroomsFromRooms(habitableRooms);
  return (bedrooms === null ? null : bedroomRent(area, bedrooms)) ?? area.typicalMonthlyRent;
}

/**
 * Where a home's rent comes from, for the calculator's "Based on …" line:
 * ", at the Wandsworth August 2026 rent for a 2-bed home", or "… average rent"
 * when the bedroom figure is not used.
 *
 * @param area The home's area.
 * @param bedrooms The bedrooms the rent was picked for; null when not known.
 * @returns The phrase, starting with a comma, or '' when the area has no rent.
 */
export function describeHomeRent(area: AreaStats | null | undefined, bedrooms: number | null): string {
  if (!area || area.typicalMonthlyRent === null) {
    return '';
  }
  const when = area.rentMonth ? ' ' + formatSoldDate(area.rentMonth) : '';
  const source = ', at the ' + area.borough + when;
  if (bedrooms !== null && bedroomRent(area, bedrooms) !== null) {
    const bucket = rentBedrooms(bedrooms);
    return source + ' rent for a ' + (bucket === 4 ? '4+' : String(bucket)) + '-bed home';
  }
  return source + ' average rent';
}

/**
 * Where a priced home's rent comes from, for the detailed analysis: "ONS mean
 * rent for 2-bed homes in Wandsworth, August 2026", or "ONS mean rent in
 * Wandsworth, August 2026" when the bedroom figure is not used.
 *
 * @param area The home's area.
 * @param bedrooms The bedrooms the rent was picked for; null when not known.
 */
export function rentBasis(area: AreaStats, bedrooms: number | null): string {
  const when = area.rentMonth ? ', ' + formatSoldDate(area.rentMonth) : '';
  if (bedrooms !== null && bedroomRent(area, bedrooms) !== null) {
    const bucket = rentBedrooms(bedrooms);
    return 'ONS mean rent for ' + (bucket === 4 ? '4+' : String(bucket)) + '-bed homes in ' + area.borough + when;
  }
  return 'ONS mean rent in ' + area.borough + when;
}
