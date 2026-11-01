import type { InflationSeries } from '../data/types';

/**
 * Moving pounds between months along UK consumer price inflation (CPIH).
 *
 * The series is monthly, so a figure is placed in the month of its date. A
 * month after the last one ONS has published is reached by carrying the latest
 * month forward at its 12-month rate; a month before the series starts has no
 * figure.
 */

/** The YYYY-MM of an ISO date. */
function monthOf(iso: string): string {
  return iso.slice(0, 7);
}

/** Whole months from `from` to `to`, both YYYY-MM. */
function monthsBetween(from: string, to: string): number {
  const [fromYear, fromMonth] = from.split('-').map(Number);
  const [toYear, toMonth] = to.split('-').map(Number);
  return (toYear - fromYear) * 12 + (toMonth - fromMonth);
}

/**
 * The CPIH index for the month of a date.
 *
 * @param series The CPIH series.
 * @param iso Any ISO date, YYYY-MM-DD.
 * @returns The month's index, projected past `latestMonth`; null before the series starts.
 */
export function cpihIndexAt(series: InflationSeries, iso: string): number | null {
  const month = monthOf(iso);
  if (!/^\d{4}-\d{2}$/.test(month) || series.months.length === 0) {
    return null;
  }
  const latest = monthOf(series.latestMonth);
  if (month > latest) {
    const last = series.months[series.months.length - 1];
    return last.index * Math.pow(1 + series.latestAnnualRate / 100, monthsBetween(latest, month) / 12);
  }
  return series.months.find((entry) => monthOf(entry.month) === month)?.index ?? null;
}

/** Whether a date's month is past the last one ONS has published, so its index is projected. */
export function isProjectedMonth(series: InflationSeries, iso: string): boolean {
  return monthOf(iso) > monthOf(series.latestMonth);
}

/**
 * A sum of pounds from one date, in the pounds of another: `amount` times the
 * rise in CPIH between their months.
 *
 * @returns The sum, or null when either month has no index.
 */
export function inflate(series: InflationSeries, amount: number, fromIso: string, toIso: string): number | null {
  const from = cpihIndexAt(series, fromIso);
  const to = cpihIndexAt(series, toIso);
  if (from === null || to === null || from <= 0) {
    return null;
  }
  return (amount * to) / from;
}
