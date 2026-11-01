import React, { useEffect, useState } from 'react';
import type { AddressOption, InflationSeries, NearbySale, NearbySales, NearbySchools as NearbySchoolsData, PriceResult } from '../data/types';
import { ApiError, isAbort } from '../lib/api';
import { primeCaptcha } from '../lib/captcha';
import { formatGBP, formatLongDate, formatPercent, formatSoldDate } from '../lib/format';
import { inflate, isProjectedMonth } from '../lib/inflation';
import Button from '../ui/Button';
import styles from './DetailedAnalysis.module.css';
import NearbyProperties from './NearbyProperties';
import NearbySchools from './NearbySchools';
import { homeSpecs, isEstimated } from './PriceResultCard';

/** The rent a priced home would let for, as the yield calculator takes it. */
export interface HomeRent {
  /** Mean monthly rent, in pounds. */
  monthly: number;
  /** ISO date of the month the rent is for; null when not known. */
  month: string | null;
  /** Where it comes from, e.g. "ONS mean rent for 2-bed homes in Wandsworth, August 2026". */
  basis: string;
}

/**
 * Props for `DetailedAnalysis`.
 */
export interface DetailedAnalysisProps {
  result: PriceResult;
  /** Fetches the sales nearest the home; without it the nearby figures are not known. */
  loadNearby?: (address: AddressOption, signal?: AbortSignal) => Promise<NearbySales>;
  /** Fetches CPIH; without it no figure is inflation corrected. */
  loadInflation?: (signal?: AbortSignal) => Promise<InflationSeries>;
  /** Fetches the schools nearest the home with their Ofsted ratings; without it there is no schools section. */
  loadSchools?: (address: AddressOption, signal?: AbortSignal) => Promise<NearbySchoolsData>;
  /** The borough rent for this home; null or absent where none is published. */
  rent?: HomeRent | null;
}

/** A figure the analysis lists: a label, its value and, where it helps, what it rests on. */
export interface AnalysisFigure {
  label: string;
  value: string;
  note?: string;
}

type Load<T> = { kind: 'loading' } | { kind: 'ready'; data: T } | { kind: 'error'; message: string } | { kind: 'none' };

const FALLBACK_ERROR = 'Something went wrong on our side. Try again in a moment.';
const NOT_KNOWN = 'Not known';
const WORKING = 'Working out…';

/** The nearby averages are of a handful of sales, so they are given to the £1,000. */
function toThousand(value: number): number {
  return Math.round(value / 1000) * 1000;
}

/**
 * A price over a floor area, to the pound; null when either is missing or the area is not positive.
 */
export function pricePerSqm(price: number | null | undefined, floorAreaSqm: number | null | undefined): number | null {
  if (price === null || price === undefined || floorAreaSqm === null || floorAreaSqm === undefined || floorAreaSqm <= 0) {
    return null;
  }
  return Math.round(price / floorAreaSqm);
}

/** The mean price of the nearby sales; null when there are none. */
export function averagePrice(sales: NearbySale[]): number | null {
  if (sales.length === 0) {
    return null;
  }
  return sales.reduce((sum, sale) => sum + sale.price, 0) / sales.length;
}

/**
 * The mean of the nearby sales, each first moved from the month it sold in to
 * the pounds of `toIso`; null when there are none or a month has no CPIH.
 */
export function averageInflatedPrice(series: InflationSeries, sales: NearbySale[], toIso: string): number | null {
  if (sales.length === 0) {
    return null;
  }
  let sum = 0;
  for (const sale of sales) {
    const moved = inflate(series, sale.price, sale.soldOn, toIso);
    if (moved === null) {
      return null;
    }
    sum += moved;
  }
  return sum / sales.length;
}

/**
 * The home's type, floor area, rooms and energy rating. A home without a
 * certificate shows its neighbourhood's most common rating, marked as an
 * estimate like its size and rooms.
 */
export function analysisSpecs(result: PriceResult): Array<{ label: string; value: string }> {
  const specs = homeSpecs(result);
  if (isEstimated(result) && result.estimatedEnergyRating) {
    specs.push({ label: 'Energy rating (est.)', value: result.estimatedEnergyRating });
  }
  return specs;
}

/** "in October 2026 pounds", the note under an inflation-corrected figure. */
function inPoundsOf(iso: string): string {
  return 'In ' + formatSoldDate(iso) + ' pounds';
}

/**
 * The price, last sale and per-m² figures; the inflation-corrected last sale
 * waits on `inflation`.
 */
export function priceFigures(result: PriceResult, inflation: Load<InflationSeries>): AnalysisFigure[] {
  const figures: AnalysisFigure[] = [
    { label: 'Price if sold on ' + formatLongDate(result.saleDate), value: formatGBP(result.value) },
  ];
  const sale = result.lastSale;
  if (!sale) {
    figures.push({ label: 'Last sold', value: 'No earlier sale on record' });
  } else {
    figures.push({ label: 'Last sold', value: formatGBP(sale.price) + ' in ' + formatSoldDate(sale.soldOn) });
    const moved = inflation.kind === 'ready' ? inflate(inflation.data, sale.price, sale.soldOn, result.saleDate) : null;
    figures.push({
      label: 'Last sold, inflation corrected',
      value: inflation.kind === 'loading' ? WORKING : moved === null ? NOT_KNOWN : formatGBP(moved),
      note: moved === null ? undefined : inPoundsOf(result.saleDate),
    });
  }
  return figures;
}

/** Price per m² at the estimated price and at the last sale. */
export function perSqmFigures(result: PriceResult): AnalysisFigure[] {
  const area = result.floorAreaSqm ?? null;
  const areaNote = area === null ? 'Floor area not known' : isEstimated(result) ? 'Floor area estimated' : undefined;
  const atPrice = pricePerSqm(result.value, area);
  const figures: AnalysisFigure[] = [
    {
      label: 'Price per m² (based on estimated price)',
      value: atPrice === null ? NOT_KNOWN : formatGBP(atPrice),
      note: areaNote,
    },
  ];
  if (result.lastSale) {
    const atSale = pricePerSqm(result.lastSale.price, area);
    figures.push({
      label: 'Price per m² (based on last sold price)',
      value: atSale === null ? NOT_KNOWN : formatGBP(atSale),
      note: areaNote,
    });
  }
  return figures;
}

/** The nearby price and rent, each as it stands and inflation corrected to the sale date. */
export function nearbyFigures(
  result: PriceResult,
  nearby: Load<NearbySales>,
  inflation: Load<InflationSeries>,
  rent: HomeRent | null,
): AnalysisFigure[] {
  const series = inflation.kind === 'ready' ? inflation.data : null;
  const figures: AnalysisFigure[] = [];

  if (nearby.kind === 'ready') {
    const { sales, windowFrom, windowTo } = nearby.data;
    const average = averagePrice(sales);
    const span = formatSoldDate(windowFrom) + ' to ' + formatSoldDate(windowTo);
    figures.push({
      label: 'Average price of nearby properties',
      value: average === null ? NOT_KNOWN : formatGBP(toThousand(average)),
      note:
        average === null
          ? 'No sales nearby, ' + span
          : 'Mean of the ' + (sales.length === 1 ? 'nearest sale' : sales.length + ' nearest sales') + ', ' + span,
    });
    const moved = series === null ? null : averageInflatedPrice(series, sales, result.saleDate);
    figures.push({
      label: 'Average price of nearby properties, inflation corrected',
      value: average !== null && inflation.kind === 'loading' ? WORKING : moved === null ? NOT_KNOWN : formatGBP(toThousand(moved)),
      note: moved === null ? undefined : inPoundsOf(result.saleDate) + ', each sale from the month it sold',
    });
  } else {
    const value = nearby.kind === 'loading' ? WORKING : NOT_KNOWN;
    figures.push({ label: 'Average price of nearby properties', value });
    figures.push({ label: 'Average price of nearby properties, inflation corrected', value });
  }

  if (rent === null) {
    figures.push({ label: 'Average rent of nearby properties', value: NOT_KNOWN, note: 'No published rent for this borough' });
  } else {
    figures.push({ label: 'Average rent of nearby properties', value: formatGBP(rent.monthly) + ' a month', note: rent.basis });
    const moved = series === null || rent.month === null ? null : inflate(series, rent.monthly, rent.month, result.saleDate);
    figures.push({
      label: 'Average rent of nearby properties, inflation corrected',
      value:
        inflation.kind === 'loading' && rent.month !== null
          ? WORKING
          : moved === null
            ? NOT_KNOWN
            : formatGBP(moved) + ' a month',
      note: moved === null ? undefined : inPoundsOf(result.saleDate),
    });
  }
  return figures;
}

/** The sentence under the figures saying what "inflation corrected" means here. */
export function inflationNote(series: InflationSeries, saleDate: string): string {
  const base =
    'Inflation corrected figures are in ' +
    formatSoldDate(saleDate) +
    ' pounds, using the ONS consumer prices index including housing costs (CPIH), published to ' +
    formatSoldDate(series.latestMonth);
  if (!isProjectedMonth(series, saleDate)) {
    return base + '.';
  }
  return base + '; later months assume its latest annual rate, ' + formatPercent(series.latestAnnualRate) + ' a year.';
}

/** Runs `load` once per `key` and `attempt`, as a `Load` state; `none` without a loader. */
function useLoad<T>(load: ((signal: AbortSignal) => Promise<T>) | null, key: unknown, attempt: number): Load<T> {
  const [state, setState] = useState<Load<T>>(load ? { kind: 'loading' } : { kind: 'none' });
  useEffect(() => {
    if (!load) {
      setState({ kind: 'none' });
      return undefined;
    }
    const controller = new AbortController();
    setState({ kind: 'loading' });
    load(controller.signal).then(
      (data) => setState({ kind: 'ready', data }),
      (error: unknown) => {
        if (!isAbort(error)) {
          setState({ kind: 'error', message: error instanceof ApiError ? error.userMessage : FALLBACK_ERROR });
        }
      },
    );
    return () => controller.abort();
    // `load` is rebuilt every render; `key` and `attempt` say when to run it again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt, load === null]);
  return state;
}

function FigureList({ figures, label }: { figures: AnalysisFigure[]; label: string }): React.ReactElement {
  return (
    <dl className={styles.figures} aria-label={label}>
      {figures.map((figure) => (
        <div className={styles.figure} key={figure.label}>
          <dt>{figure.label}</dt>
          <dd aria-busy={figure.value === WORKING || undefined}>
            <span className={styles.value}>{figure.value}</span>
            {figure.note ? <span className={styles.note}>{figure.note}</span> : null}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * The price card's detailed analysis: the price and last sale (as paid and in
 * today's pounds), the home's specs, price per m², what nearby homes sold
 * and let for, the nearest schools with their Ofsted ratings, and then each
 * of the nearest sales. The nearby sales, schools and CPIH are fetched as it
 * opens.
 */
export function DetailedAnalysis({
  result,
  loadNearby,
  loadInflation,
  loadSchools,
  rent = null,
}: DetailedAnalysisProps): React.ReactElement {
  const [attempt, setAttempt] = useState(0);
  // Nearby sales and schools need a reCAPTCHA pass; start loading it now.
  useEffect(() => primeCaptcha(), []);
  const nearby = useLoad<NearbySales>(
    loadNearby ? (signal) => loadNearby(result.address, signal) : null,
    result.address,
    attempt,
  );
  const inflation = useLoad<InflationSeries>(loadInflation ? (signal) => loadInflation(signal) : null, result, attempt);
  const schools = useLoad<NearbySchoolsData>(
    loadSchools ? (signal) => loadSchools(result.address, signal) : null,
    result.address,
    attempt,
  );
  const failure =
    nearby.kind === 'error'
      ? nearby.message
      : inflation.kind === 'error'
        ? inflation.message
        : schools.kind === 'error'
          ? schools.message
          : null;

  return (
    <div className={styles.analysis}>
      <section className={styles.group} aria-labelledby="analysis-price">
        <h3 className={styles.groupHeading} id="analysis-price">
          Price
        </h3>
        <FigureList figures={priceFigures(result, inflation)} label="Price and last sale" />
      </section>

      <section className={styles.group} aria-labelledby="analysis-home">
        <h3 className={styles.groupHeading} id="analysis-home">
          This home
        </h3>
        <dl className={styles.specs} aria-label="About this home">
          {analysisSpecs(result).map((spec) => (
            <div className={styles.spec} key={spec.label}>
              <dt>{spec.label}</dt>
              <dd>{spec.value}</dd>
            </div>
          ))}
        </dl>
        {isEstimated(result) ? (
          <p className={styles.footnote}>
            No energy certificate on record, so these are estimated
            {result.estimatedFrom ? ' from ' + result.estimatedFrom : ''}.
          </p>
        ) : null}
        <FigureList figures={perSqmFigures(result)} label="Price per square metre" />
      </section>

      <section className={styles.group} aria-labelledby="analysis-nearby">
        <h3 className={styles.groupHeading} id="analysis-nearby">
          Nearby
        </h3>
        <FigureList figures={nearbyFigures(result, nearby, inflation, rent)} label="Nearby prices and rents" />
      </section>

      {schools.kind !== 'none' ? (
        <section className={styles.group} aria-labelledby="analysis-schools" aria-busy={schools.kind === 'loading' || undefined}>
          <h3 className={styles.groupHeading} id="analysis-schools">
            Schools
          </h3>
          {schools.kind === 'ready' ? (
            <NearbySchools data={schools.data} />
          ) : schools.kind === 'loading' ? (
            <p className={styles.footnote}>{WORKING}</p>
          ) : (
            <p className={styles.footnote}>{NOT_KNOWN}</p>
          )}
        </section>
      ) : null}

      {nearby.kind === 'ready' ? (
        <section className={styles.group} aria-label="Nearby sales">
          <NearbyProperties data={nearby.data} />
        </section>
      ) : null}

      {failure ? (
        <div className={styles.error} role="alert">
          <p>{failure}</p>
          <Button variant="ghost" size="sm" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </Button>
        </div>
      ) : null}
      {inflation.kind === 'ready' ? <p className={styles.footnote}>{inflationNote(inflation.data, result.saleDate)}</p> : null}
    </div>
  );
}

export default DetailedAnalysis;
