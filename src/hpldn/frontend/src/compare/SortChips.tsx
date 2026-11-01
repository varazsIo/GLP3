import React from 'react';
import type { SortMode } from '../data/types';
import styles from './SortChips.module.css';

/**
 * Props for `SortChips`.
 */
export interface SortChipsProps {
  /** The active ranking. */
  value: SortMode;
  /** Called with the chosen ranking. */
  onChange: (mode: SortMode) => void;
}

/** The rankings offered, with their labels; the first is the default. */
export const SORT_MODES: ReadonlyArray<{ value: SortMode; label: string }> = [
  { value: 'cheapest', label: 'Cheapest' },
  { value: 'growing', label: 'Fastest growing' },
  { value: 'connected', label: 'Best connected' },
  { value: 'yield', label: 'Highest yield' },
];

/**
 * Chips for the four ways to rank areas.
 */
export function SortChips({ value, onChange }: SortChipsProps): React.ReactElement {
  return (
    <div className={styles.wrap} role="group" aria-label="Sort areas">
      {SORT_MODES.map((mode) => (
        <button
          key={mode.value}
          type="button"
          className={[styles.chip, mode.value === value ? styles.active : ''].filter(Boolean).join(' ')}
          aria-pressed={mode.value === value}
          onClick={() => onChange(mode.value)}
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}

export default SortChips;
