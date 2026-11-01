import React from 'react';
import styles from './TransportMeter.module.css';

/**
 * Props for `TransportMeter`.
 */
export interface TransportMeterProps {
  /** 1 to 5, where 5 is best connected; `null` when not known. */
  score: number | null;
  /** Shows the word beside the dots; defaults to true. */
  showText?: boolean;
}

/** The word for each score, from 1 to 5. */
const WORDS = ['Limited', 'Modest', 'Good', 'Very good', 'Excellent'];

/**
 * A transport score as five dots and a word, so it never relies on colour alone.
 *
 * An unknown score shows "Not known" and no dots, never a low score.
 */
export function TransportMeter({ score, showText = true }: TransportMeterProps): React.ReactElement {
  if (score === null) {
    return (
      <span className={styles.wrap}>
        <span className={styles.text}>Not known</span>
      </span>
    );
  }
  const clamped = Math.max(1, Math.min(5, Math.round(score)));
  const word = WORDS[clamped - 1];

  return (
    <span className={styles.wrap}>
      <span className={styles.dots} role="img" aria-label={'Transport access ' + clamped + ' out of 5, ' + word.toLowerCase()}>
        {[1, 2, 3, 4, 5].map((step) => (
          <span
            key={step}
            className={[styles.dot, step <= clamped ? styles.on : ''].filter(Boolean).join(' ')}
          />
        ))}
      </span>
      {showText ? <span className={styles.text}>{word}</span> : null}
    </span>
  );
}

export default TransportMeter;
