import React from 'react';
import type { Driver } from '../data/types';
import styles from './DriverChip.module.css';

/**
 * Props for `DriverChip`.
 */
export interface DriverChipProps {
  /** The reason to show. */
  driver: Driver;
}

/** Screen-reader wording for each tone, which nothing on screen conveys by colour alone. */
const TONE_WORDS: Record<Driver['tone'], string> = {
  positive: 'Helps the value',
  neutral: 'Neutral for the value',
  caution: 'Holds the value back',
};

/**
 * One plain-English reason behind the estimate: a label over a detail line.
 */
export function DriverChip({ driver }: DriverChipProps): React.ReactElement {
  return (
    <li className={styles.chip}>
      <span className={styles.body}>
        <span className="srOnly">{TONE_WORDS[driver.tone]}. </span>
        <span className={styles.label}>{driver.label}</span>
        <span className={styles.detail}>{driver.detail}</span>
      </span>
    </li>
  );
}

export default DriverChip;
