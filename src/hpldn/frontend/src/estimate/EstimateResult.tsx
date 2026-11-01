import React, { useEffect, useRef, useState } from 'react';
import type { EstimateResult as Estimate } from '../data/types';
import { isFullPostcode } from '../lib/estimate';
import { formatArea, formatBedrooms } from '../lib/format';
import NextSteps from '../price/NextSteps';
import Button from '../ui/Button';
import ComparableRow from './ComparableRow';
import DriverChip from './DriverChip';
import RangeBand from './RangeBand';
import ResultSkeleton from './ResultSkeleton';
import ValueHeadline from './ValueHeadline';
import styles from './EstimateResult.module.css';

/** Where the latest estimate request has got to. */
export type EstimateStatus = 'idle' | 'loading' | 'ready' | 'error';

/** Reasons shown: the most important first. */
const REASONS_SHOWN = 3;
/** Nearby sales shown before "Show all". */
const SALES_SHOWN = 3;

/**
 * Props for `EstimateResult`.
 */
export interface EstimateResultProps {
  /** The latest estimate, or null before one arrives. */
  result: Estimate | null;
  /** The area's display name, used in the card's heading. */
  areaName: string;
  /** Where the latest request is. Defaults to `ready` with a result, else `idle`. */
  status?: EstimateStatus;
  /** Shown when status is 'error'. */
  errorMessage?: string | null;
  /** Re-submits the last request. */
  onRetry?: () => void;
  /** Selects the estimate's area in Areas and moves there. */
  onSeeArea?: () => void;
  /** Starts the yield calculator from this estimate and moves there. */
  onWorkOutYield?: () => void;
  /** Shown beside "Try again" after a failure the same request will not fix; moves to the details form. */
  onEdit?: () => void;
}

/**
 * The estimate card: value and range, the main reasons, then nearby sales.
 *
 * Renders nothing until a request is made. Every answer or failure moves focus
 * to the card's heading, including a repeat of the same request, so the
 * landlord always sees the form did something.
 */
export function EstimateResult({
  result,
  areaName,
  status = result ? 'ready' : 'idle',
  errorMessage,
  onRetry,
  onSeeArea,
  onWorkOutYield,
  onEdit,
}: EstimateResultProps): React.ReactElement | null {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [showAllSales, setShowAllSales] = useState(false);
  const shown = status === 'ready' ? result : null;
  const failed = status === 'error';

  useEffect(() => {
    setShowAllSales(false);
  }, [shown]);

  useEffect(() => {
    if ((!shown && !failed) || !headingRef.current) {
      return;
    }
    const heading = headingRef.current;
    heading.focus({ preventScroll: true });
    if (typeof heading.scrollIntoView === 'function') {
      heading.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [shown, failed, errorMessage]);

  if (status === 'idle') {
    return null;
  }

  if (status === 'loading') {
    return <ResultSkeleton heading="Working out your estimate…" />;
  }

  if (failed || !shown) {
    return (
      <div className={styles.notice} role="alert">
        <h2 className={styles.heading} id="result-heading" ref={headingRef} tabIndex={-1}>
          We could not get your estimate
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
              Change the details
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  const { request } = shown;
  const sales = showAllSales ? shown.comparables : shown.comparables.slice(0, SALES_SHOWN);

  return (
    <div className={styles.card} aria-live="polite">
      <h2 className={styles.heading} id="result-heading" ref={headingRef} tabIndex={-1}>
        {request.postcode} in {areaName}
        {isFullPostcode(request.postcode) ? null : <span className={styles.scope}> (district-wide)</span>}
      </h2>

      <div className={styles.top}>
        <ValueHeadline
          value={shown.value}
          label="Estimated value"
          note={formatArea(request.floorAreaSqm) + ', ' + formatBedrooms(request.bedrooms)}
        />
        <RangeBand low={shown.low} value={shown.value} high={shown.high} />
      </div>

      <h3 className={styles.subhead} id="drivers-heading">
        Why this price
      </h3>
      <ul className={styles.drivers} aria-labelledby="drivers-heading">
        {shown.drivers.slice(0, REASONS_SHOWN).map((driver) => (
          <DriverChip driver={driver} key={driver.id} />
        ))}
      </ul>

      <div className={styles.comps}>
        <h3 className={styles.subhead} id="sales-heading">
          Nearby sales
        </h3>
        {shown.comparables.length ? (
          <>
            <ul className={styles.compsList} id="nearby-sales" aria-labelledby="sales-heading">
              {sales.map((comparable) => (
                <ComparableRow comparable={comparable} key={comparable.id} />
              ))}
            </ul>
            {shown.comparables.length > SALES_SHOWN ? (
              <button
                type="button"
                className={styles.more}
                aria-expanded={showAllSales}
                aria-controls="nearby-sales"
                onClick={() => setShowAllSales((all) => !all)}
              >
                {showAllSales ? 'Show fewer' : 'Show all ' + shown.comparables.length}
              </button>
            ) : null}
          </>
        ) : (
          <p className={styles.compsEmpty}>
            Not enough recent sales of similar homes nearby to show. The estimate leans on the area&rsquo;s typical
            prices instead, so treat it with more care.
          </p>
        )}
      </div>

      <NextSteps onSeeArea={onSeeArea} onWorkOutYield={onWorkOutYield} />
    </div>
  );
}

export default EstimateResult;
