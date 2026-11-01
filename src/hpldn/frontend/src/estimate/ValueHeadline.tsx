import React, { useEffect, useState } from 'react';
import { formatGBP } from '../lib/format';
import useReducedMotion from '../ui/useReducedMotion';
import styles from './ValueHeadline.module.css';

/**
 * Props for `ValueHeadline`.
 */
export interface ValueHeadlineProps {
  /** The figure, in pounds. */
  value: number;
  /** Label above the figure; defaults to "Estimated value". */
  label?: React.ReactNode;
  /** Short line under the figure, e.g. "72 m², 2 bedrooms". */
  note?: string;
}

/** Length of the count-up animation, in milliseconds. */
const DURATION = 600;

/**
 * The large estimated figure, counting up once.
 *
 * The final value is in the DOM from the first render, so screen readers,
 * tests and early screenshots see the real number; reduced motion skips the
 * animation.
 */
export function ValueHeadline({
  value,
  label = 'Estimated value',
  note,
}: ValueHeadlineProps): React.ReactElement {
  const reducedMotion = useReducedMotion();
  const [shown, setShown] = useState(value);

  useEffect(() => {
    if (reducedMotion || typeof window.requestAnimationFrame !== 'function') {
      setShown(value);
      return undefined;
    }

    let frame = 0;
    const start = performance.now();
    const from = 0;

    const step = (now: number): void => {
      const t = Math.min(1, (now - start) / DURATION);
      const eased = 1 - Math.pow(1 - t, 3);
      setShown(Math.round((from + (value - from) * eased) / 1000) * 1000);
      if (t < 1) {
        frame = window.requestAnimationFrame(step);
      } else {
        setShown(value);
      }
    };

    frame = window.requestAnimationFrame(step);
    return () => window.cancelAnimationFrame(frame);
  }, [value, reducedMotion]);

  return (
    <div className={styles.wrap}>
      <span className={styles.label}>{label}</span>
      <strong className={styles.value}>{formatGBP(shown)}</strong>
      {note ? <span className={styles.note}>{note}</span> : null}
    </div>
  );
}

export default ValueHeadline;
