import React, { useCallback, useEffect, useRef, useState } from 'react';
import { PROPERTY_TYPE_NAMES } from '../data/types';
import type { AddressOption, InflationSeries, NearbySales, NearbySchools, PriceResult } from '../data/types';
import RangeBand from '../estimate/RangeBand';
import ResultSkeleton from '../estimate/ResultSkeleton';
import ValueHeadline from '../estimate/ValueHeadline';
import { formatArea, formatGBP, formatLongDate, formatPercent, formatSoldDate } from '../lib/format';
import { addressKey, describeFullAddress } from '../lib/price';
import Button from '../ui/Button';
import DetailedAnalysis from './DetailedAnalysis';
import type { HomeRent } from './DetailedAnalysis';
import NextSteps from './NextSteps';
import styles from './PriceResultCard.module.css';

/** Where the latest price request has got to. */
export type PriceStatus = 'idle' | 'loading' | 'ready' | 'error';

/**
 * Props for `PriceResultCard`.
 */
export interface PriceResultCardProps {
  /** The latest price, or null before one arrives. */
  result: PriceResult | null;
  status: PriceStatus;
  /** Shown when status is 'error'. */
  errorMessage?: string | null;
  /** Re-sends the last request. */
  onRetry?: () => void;
  /** Starts the yield calculator from this home and moves there. */
  onWorkOutYield?: () => void;
  /** Fetches what the homes nearest this one sold for, for the detailed analysis. */
  loadNearby?: (address: AddressOption, signal?: AbortSignal) => Promise<NearbySales>;
  /** Shown beside "Try again" after a failure the same request will not fix; moves to the search. */
  onEdit?: () => void;
  /** Fetches CPIH for the detailed analysis's inflation-corrected figures. */
  loadInflation?: (signal?: AbortSignal) => Promise<InflationSeries>;
  /** Fetches the schools nearest this home with their Ofsted ratings, for the detailed analysis. */
  loadSchools?: (address: AddressOption, signal?: AbortSignal) => Promise<NearbySchools>;
  /** The borough rent for the priced home, for the detailed analysis; null where none is published. */
  rent?: HomeRent | null;
}

/** Which face of the card shows: the price, or its detailed analysis. */
type View = 'price' | 'analysis';

/** Whether a price rests on size and rooms estimated from nearby homes rather than a certificate. */
export function isEstimated(result: PriceResult): boolean {
  return result.epcSource === 'estimated';
}

/**
 * The home's specs as a listing shows them: type, floor area, rooms and energy
 * rating, leaving out any the records do not hold. Estimated figures are
 * labelled as such, and an estimated home shows no energy rating.
 */
export function homeSpecs(result: PriceResult): Array<{ label: string; value: string }> {
  const estimated = isEstimated(result);
  const suffix = estimated ? ' (est.)' : '';
  const specs = [{ label: 'Type', value: PROPERTY_TYPE_NAMES[result.propertyType] }];
  // An API that predates these fields leaves them out, which reads as not known.
  const area = result.floorAreaSqm ?? null;
  if (area !== null) {
    specs.push({ label: 'Floor area' + suffix, value: formatArea(area) });
  }
  const rooms = result.habitableRooms ?? null;
  if (rooms !== null) {
    specs.push({ label: 'Rooms' + suffix, value: String(rooms) });
  }
  if (result.energyRating && !estimated) {
    specs.push({ label: 'Energy rating', value: result.energyRating });
  }
  return specs;
}

/** The sentence under an estimated home's specs; null for a home with a certificate. */
export function estimateNote(result: PriceResult): string | null {
  if (!isEstimated(result)) {
    return null;
  }
  const source = result.estimatedFrom ? 'size estimated from ' + result.estimatedFrom : 'size could not be estimated';
  return 'No energy certificate on record — ' + source + '; the range is wider.';
}

/**
 * The sentence saying the price rests mainly on the home's own recent sale, or
 * null when the model priced it. A home's recent arm's-length sale is the best
 * evidence of its value, so when there is one it leads the price.
 */
export function anchorNote(result: PriceResult): string | null {
  if (!result.anchoredOnLastSale || !result.lastSale) {
    return null;
  }
  return (
    'Based mainly on its sale for ' +
    formatGBP(result.lastSale.price) +
    ' in ' +
    formatSoldDate(result.lastSale.soldOn) +
    '.'
  );
}

/** The type of home, in the words the projection is fitted per type. */
const PROJECTED_TYPE_WORD: Record<string, string> = {
  flat: 'flat',
  maisonette: 'flat',
  terraced: 'terraced house',
  'semi-detached': 'semi-detached house',
  detached: 'detached house',
};

/**
 * The sentence saying how much of the price is a forecast, or null when none is.
 *
 * The model prices the home as of the day after the latest recorded sale, and
 * the price is then moved to the chosen date along the local price index for
 * this type of home -- the postcode district's own, measured from repeat
 * sales, or its borough's or London's where the district has too few. Saying
 * so, and by how much, is what keeps the date picker honest: the figure moves
 * with the date because a measured index moved it.
 */
export function projectionNote(result: PriceResult): string | null {
  if (result.projectionUplift === null || result.projectedFrom === null) {
    return null;
  }
  const kind = PROJECTED_TYPE_WORD[result.propertyType] ?? 'home';
  const direction = result.projectionUplift >= 0 ? 'growth' : 'decline';
  const area = result.indexArea ?? 'London';
  return (
    'Includes ' +
    formatPercent(Math.abs(result.projectionUplift) * 100) +
    ' projected ' +
    area +
    ' ' +
    kind +
    ' price ' +
    direction +
    ' from ' +
    formatSoldDate(result.projectedFrom) +
    ', after the latest recorded sale.'
  );
}

/**
 * The price card for a specific home: the price on the chosen day and its
 * range, the home's specs, its last sale, how recent the sales behind it are,
 * and where to go next: the detailed analysis or the rental sums.
 *
 * Nothing shows until a request is made. Each answer or failure moves focus to
 * the card's heading and scrolls the whole card into view, and a new answer
 * is briefly ringed in the accent so the change is unmissable.
 *
 * "Detailed Analysis" turns the card to the analysis of the same home (see
 * `DetailedAnalysis`), with a back button in its top left corner that turns
 * it back; a new answer always opens on the price. Nearby sales, schools and
 * CPIH are kept once fetched, so turning back and forth does not fetch them again.
 */
export function PriceResultCard({
  result,
  status,
  errorMessage,
  onRetry,
  onWorkOutYield,
  loadNearby,
  onEdit,
  loadInflation,
  loadSchools,
  rent = null,
}: PriceResultCardProps): React.ReactElement | null {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const analysisButtonRef = useRef<HTMLButtonElement>(null);
  const shown = status === 'ready' ? result : null;
  const failed = status === 'error';
  const [view, setView] = useState<View>('price');
  // Set by a press of "Detailed Analysis" or back, so focus follows only a turn the user made.
  const turned = useRef(false);

  const nearbyCache = useRef(new Map<string, NearbySales>());
  const cachedNearby = useCallback(
    async (address: AddressOption, signal?: AbortSignal): Promise<NearbySales> => {
      const key = addressKey(address);
      const hit = nearbyCache.current.get(key);
      if (hit) {
        return hit;
      }
      if (!loadNearby) {
        throw new Error('no nearby sales loader');
      }
      const data = await loadNearby(address, signal);
      nearbyCache.current.set(key, data);
      return data;
    },
    [loadNearby],
  );

  const schoolsCache = useRef(new Map<string, NearbySchools>());
  const cachedSchools = useCallback(
    async (address: AddressOption, signal?: AbortSignal): Promise<NearbySchools> => {
      const key = addressKey(address);
      const hit = schoolsCache.current.get(key);
      if (hit) {
        return hit;
      }
      if (!loadSchools) {
        throw new Error('no schools loader');
      }
      const data = await loadSchools(address, signal);
      schoolsCache.current.set(key, data);
      return data;
    },
    [loadSchools],
  );

  const inflationCache = useRef<InflationSeries | null>(null);
  const cachedInflation = useCallback(
    async (signal?: AbortSignal): Promise<InflationSeries> => {
      if (inflationCache.current) {
        return inflationCache.current;
      }
      if (!loadInflation) {
        throw new Error('no inflation loader');
      }
      const data = await loadInflation(signal);
      inflationCache.current = data;
      return data;
    },
    [loadInflation],
  );

  // A new answer opens on the price.
  useEffect(() => {
    setView('price');
  }, [shown]);

  useEffect(() => {
    if (!turned.current) {
      return;
    }
    turned.current = false;
    const target = view === 'analysis' ? backRef.current : analysisButtonRef.current;
    target?.focus({ preventScroll: true });
    const box = boxRef.current;
    if (view === 'analysis' && box && typeof box.scrollIntoView === 'function') {
      box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [view]);

  const turnTo = (next: View) => {
    turned.current = true;
    setView(next);
  };

  useEffect(() => {
    if ((!shown && !failed) || !headingRef.current) {
      return;
    }
    const heading = headingRef.current;
    heading.focus({ preventScroll: true });
    // The whole card, from its top edge below the header, so the answer is never below the fold.
    const box = boxRef.current ?? heading;
    if (typeof box.scrollIntoView === 'function') {
      box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [shown, failed, errorMessage]);

  if (status === 'idle') {
    return null;
  }

  if (status === 'loading') {
    return <ResultSkeleton heading="Working out the price…" />;
  }

  if (failed || !shown) {
    return (
      <div className={styles.notice} role="alert" ref={boxRef}>
        <h2 className={styles.heading} id="result-heading" ref={headingRef} tabIndex={-1}>
          We could not price this home
        </h2>
        <p className={styles.errorText}>{errorMessage ?? 'Something went wrong on our side. Try again in a moment.'}</p>
        <div className={styles.actions}>
          {onRetry ? (
            <Button variant="primary" onClick={onRetry}>
              Try again
            </Button>
          ) : null}
          {onEdit ? (
            <Button variant="secondary" onClick={onEdit}>
              Change the search
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  if (view === 'analysis') {
    return (
      <div className={styles.card} ref={boxRef}>
        <div className={styles.analysisBar}>
          <button
            type="button"
            className={styles.back}
            ref={backRef}
            onClick={() => turnTo('price')}
            aria-label="Back to the price"
            title="Back to the price"
          >
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
              <path
                d="M15 5l-7 7 7 7"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          <p className={styles.analysisTitle}>Detailed Analysis</p>
        </div>
        <h2 className={styles.heading} id="result-heading" ref={headingRef} tabIndex={-1}>
          {describeFullAddress(shown.address)}
        </h2>
        <DetailedAnalysis
          result={shown}
          loadNearby={loadNearby ? cachedNearby : undefined}
          loadInflation={loadInflation ? cachedInflation : undefined}
          loadSchools={loadSchools ? cachedSchools : undefined}
          rent={rent}
        />
      </div>
    );
  }

  return (
    <div className={styles.card} aria-live="polite" ref={boxRef}>
      <h2 className={styles.heading} id="result-heading" ref={headingRef} tabIndex={-1}>
        {describeFullAddress(shown.address)}
      </h2>

      <div className={styles.top}>
        <div className={styles.headline}>
          <ValueHeadline
            value={shown.value}
            label={
              <>
                Price if sold on <span className={styles.saleDate}>{formatLongDate(shown.saleDate)}</span>
              </>
            }
          />
        </div>
        {shown.low !== null && shown.high !== null ? (
          <RangeBand
            low={shown.low}
            value={shown.value}
            high={shown.high}
            label={null}
            valueName="a price"
          />
        ) : null}
      </div>
      {anchorNote(shown) ? <p className={styles.anchorNote}>{anchorNote(shown)}</p> : null}
      {projectionNote(shown) ? <p className={styles.projectionNote}>{projectionNote(shown)}</p> : null}

      <dl className={styles.specs} aria-label="About this home">
        {homeSpecs(shown).map((spec) => (
          <div className={styles.spec} key={spec.label}>
            <dt>{spec.label}</dt>
            <dd>{spec.value}</dd>
          </div>
        ))}
      </dl>
      {estimateNote(shown) ? <p className={styles.estimateNote}>{estimateNote(shown)}</p> : null}

      <p className={styles.lastSale}>
        {shown.lastSale ? (
          <>
            <span className={styles.lastSaleLabel}>Last sold</span>{' '}
            {formatGBP(shown.lastSale.price) + ' in ' + formatSoldDate(shown.lastSale.soldOn)}
          </>
        ) : (
          'No earlier sale on record'
        )}
      </p>
      <p className={styles.evidence}>Based on sales recorded up to {formatSoldDate(shown.evidenceTo)}</p>

      <NextSteps onWorkOutYield={onWorkOutYield}>
        <Button variant="primary" ref={analysisButtonRef} onClick={() => turnTo('analysis')}>
          Detailed Analysis
        </Button>
      </NextSteps>
    </div>
  );
}

export default PriceResultCard;
