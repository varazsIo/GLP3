import React from 'react';
import styles from './MapLegend.module.css';

/** Fill colour for each band, from the design tokens; one more than the breaks. */
export const RAMP: string[] = [
  'var(--c-ramp-1)',
  'var(--c-ramp-2)',
  'var(--c-ramp-3)',
  'var(--c-ramp-4)',
  'var(--c-ramp-5)',
  'var(--c-ramp-6)',
];

/**
 * The fixed band edges the map is shaded by: <£450K, £450K-£600K,
 * £600K-£800K, £800K-£1M, £1M-£1.2M and £1.2M+. One fewer than `RAMP`.
 */
export const PRICE_BREAKS: ReadonlyArray<number> = [450_000, 600_000, 800_000, 1_000_000, 1_200_000];

/**
 * The legend label for each band: its upper edge, and the top band as open-ended,
 * so six labels fit under the swatches.
 */
export const BAND_LABELS: ReadonlyArray<string> = ['<£450K', '£600K', '£800K', '£1M', '£1.2M', '£1.2M+'];

/**
 * Returns the ramp index a price falls into, given the breaks.
 */
export function rampIndexFor(price: number, breaks: ReadonlyArray<number>): number {
  for (let i = 0; i < breaks.length; i += 1) {
    if (price < breaks[i]) {
      return i;
    }
  }
  return breaks.length;
}

/**
 * Props for `MapLegend`.
 */
export interface MapLegendProps {
  /** The year the prices describe, e.g. 2026. */
  year: number;
}

/**
 * The price legend under the map: one swatch per band, labelled with the
 * band's upper edge.
 */
export function MapLegend({ year }: MapLegendProps): React.ReactElement {
  return (
    <div className={styles.legend}>
      <span className={styles.caption}>Average price, {year}</span>
      <span className={styles.scale}>
        {RAMP.map((colour, index) => (
          <span className={styles.step} key={colour}>
            <span className={styles.swatch} style={{ background: colour }} />
            <span className={styles.tick}>{BAND_LABELS[index]}</span>
          </span>
        ))}
      </span>
    </div>
  );
}

export default MapLegend;
