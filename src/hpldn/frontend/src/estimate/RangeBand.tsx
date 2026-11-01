import React from 'react';
import { formatGBP, formatGBPCompact } from '../lib/format';
import styles from './RangeBand.module.css';

/**
 * Props for `RangeBand`.
 */
export interface RangeBandProps {
  /** Lower end of the range, in pounds. */
  low: number;
  /** The estimate, in pounds. */
  value: number;
  /** Upper end of the range, in pounds. */
  high: number;
  /** The label's id, unique on the page; defaults to "range-label". */
  labelId?: string;
  /** What the value is called in the band's accessible name; defaults to "an estimate". */
  valueName?: string;
  /** The heading shown above the band; defaults to "Price range", and null shows none. */
  label?: string | null;
}

/**
 * Where the value sits along the range, from 0 to 100; the middle when the
 * range has no width.
 */
export function markerPercent(low: number, value: number, high: number): number {
  if (!(high > low)) {
    return 50;
  }
  return Math.min(100, Math.max(0, ((value - low) / (high - low)) * 100));
}

/**
 * The price range as a static interval between its two ends, with a tick and a short
 * label where the value sits, and both ends labelled underneath.
 */
export function RangeBand({
  low,
  value,
  high,
  labelId = 'range-label',
  valueName = 'an estimate',
  label = 'Price range',
}: RangeBandProps): React.ReactElement {
  const percent = markerPercent(low, value, high);
  // The label shifts by its own width as the marker does, so it stays over the
  // band at either end: left-aligned at 0%, centred at 50%, right-aligned at 100%.
  const placed = { left: percent + '%', transform: 'translateX(-' + percent + '%)' };

  return (
    <div className={styles.wrap}>
      {label ? (
        <span className={styles.label} id={labelId}>
          {label}
        </span>
      ) : null}
      <div
        className={styles.band}
        role="img"
        aria-label={
          'Price range from ' + formatGBP(low) + ' to ' + formatGBP(high) +
          ', with ' + valueName + ' of ' + formatGBP(value)
        }
      >
        <span className={styles.valueLabel} style={placed} aria-hidden="true">
          {formatGBPCompact(value)}
        </span>
        <span className={styles.track} aria-hidden="true">
          <span className={styles.marker} style={{ left: percent + '%' }} />
        </span>
      </div>
      <div className={styles.ends}>
        <span className={styles.end}>
          <span className={styles.endLabel}>Minimum</span>
          <span className={styles.endValue}>{formatGBP(low)}</span>
        </span>
        <span className={[styles.end, styles.endHigh].join(' ')}>
          <span className={styles.endLabel}>Maximum</span>
          <span className={styles.endValue}>{formatGBP(high)}</span>
        </span>
      </div>
    </div>
  );
}

export default RangeBand;
