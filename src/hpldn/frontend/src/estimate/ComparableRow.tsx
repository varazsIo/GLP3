import React from 'react';
import { PROPERTY_TYPE_NAMES } from '../data/types';
import type { Comparable } from '../data/types';
import { formatArea, formatDistance, formatGBP, formatSoldDate } from '../lib/format';
import styles from './ComparableRow.module.css';

/**
 * Props for `ComparableRow`.
 */
export interface ComparableRowProps {
  /** The nearby sale to show. */
  comparable: Comparable;
}

/**
 * Describes a sale as its property type then floor area, e.g. "Flat, 130 m²",
 * adding "other type" when it is not the type asked about.
 */
export function describeComparable(comparable: Comparable): string {
  return (
    PROPERTY_TYPE_NAMES[comparable.propertyType] +
    ', ' +
    formatArea(comparable.floorAreaSqm) +
    (comparable.matchType === 'broader' ? ' (other type)' : '')
  );
}

/**
 * One nearby sale that informed the estimate.
 *
 * Shows the price paid and, when it differs, what that sale is worth at the
 * size and on the day being valued -- the figure the estimate is actually
 * built from. Without it the headline could not be derived from the list: a
 * reader averaging three prices paid gets a different number from the one
 * quoted above them.
 */
export function ComparableRow({ comparable }: ComparableRowProps): React.ReactElement {
  const description = describeComparable(comparable);
  const adjusted = comparable.adjustedPrice;
  // Within a percent is the same number once rounded, so say nothing.
  const shifted = adjusted !== null && Math.abs(adjusted - comparable.price) / comparable.price > 0.01;
  return (
    <li className={styles.row}>
      <span className={styles.street}>{comparable.street}</span>
      <span className={styles.meta}>{description}</span>
      <span className={styles.meta}>Sold {formatSoldDate(comparable.soldOn)}</span>
      <span className={styles.price}>
        {formatGBP(comparable.price)}
        {shifted ? (
          <span className={styles.adjusted}>{formatGBP(adjusted as number)} at this size today</span>
        ) : null}
      </span>
      <span className={styles.distance}>{formatDistance(comparable.distanceKm)}</span>
    </li>
  );
}

export default ComparableRow;
