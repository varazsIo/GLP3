import React from 'react';
import { formatPercent } from '../lib/format';
import styles from './StatTile.module.css';

/**
 * Props for `StatTile`.
 */
export interface StatTileProps {
  label: string;
  /** The large figure. */
  value: React.ReactNode;
  /**
   * A signed percentage, shown with an arrow and sign, never colour alone.
   * `null` means the data cannot support one, and `deltaMissing` is shown.
   */
  delta?: number | null;
  /** What to say in place of a `null` delta. */
  deltaMissing?: string;
  /** Text after the delta, e.g. "last year". */
  deltaLabel?: string;
  /** Small print under the value. */
  note?: string;
  /**
   * When the value itself is a signed change, e.g. a 5-year change: it is
   * then coloured and arrowed as the delta is, so a fall reads the same
   * everywhere.
   */
  trend?: number | null;
}

/**
 * A figure tile: label, large value, and an optional note and delta.
 *
 * A change is coloured one way everywhere: a rise in the up colour with ▲, a
 * fall in the down colour with ▼.
 */
export function StatTile({
  label,
  value,
  delta,
  deltaLabel,
  deltaMissing = 'Not enough sales to show a change',
  note,
  trend,
}: StatTileProps): React.ReactElement {
  const hasDelta = typeof delta === 'number';
  const rising = hasDelta && (delta as number) >= 0;
  const hasTrend = typeof trend === 'number';
  const trendUp = hasTrend && (trend as number) >= 0;

  return (
    <div className={styles.tile}>
      <span className={styles.label}>{label}</span>
      {hasTrend ? (
        <span className={[styles.value, styles.trendValue, trendUp ? styles.up : styles.down].join(' ')}>
          <span className={styles.trendArrow} aria-hidden="true">
            {trendUp ? '▲' : '▼'}
          </span>
          {value}
        </span>
      ) : (
        <span className={styles.value}>{value}</span>
      )}
      {note ? <span className={styles.note}>{note}</span> : null}
      {hasDelta ? (
        <span className={styles.footer}>
          <span className={[styles.delta, rising ? styles.up : styles.down].join(' ')}>
            <span aria-hidden="true">{rising ? '▲' : '▼'}</span>
            {formatPercent(delta as number, { signed: true })}
            {deltaLabel ? <span className={styles.note}>{deltaLabel}</span> : null}
          </span>
        </span>
      ) : null}
      {delta === null ? (
        <span className={styles.footer}>
          <span className={styles.note}>{deltaMissing}</span>
        </span>
      ) : null}
    </div>
  );
}

export default StatTile;
