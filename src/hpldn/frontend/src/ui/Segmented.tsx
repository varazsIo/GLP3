import React from 'react';
import styles from './Segmented.module.css';

/**
 * Props for `Segmented`.
 */
export interface SegmentedProps<V extends string> {
  /** Names the group for assistive technology, e.g. "Show areas as". */
  label: string;
  /** The choices, in order. A disabled choice cannot be pressed. */
  options: ReadonlyArray<{ value: V; label: string; disabled?: boolean }>;
  /** The chosen value. */
  value: V;
  /** Called with the value pressed. */
  onChange: (value: V) => void;
  className?: string;
}

/**
 * A small set of mutually exclusive views as joined toggle buttons, e.g. Map | Ranked.
 */
export function Segmented<V extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: SegmentedProps<V>): React.ReactElement {
  return (
    <div className={[styles.group, className].filter(Boolean).join(' ')} role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={[styles.segment, option.value === value ? styles.active : ''].filter(Boolean).join(' ')}
          aria-pressed={option.value === value}
          disabled={option.disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export default Segmented;
