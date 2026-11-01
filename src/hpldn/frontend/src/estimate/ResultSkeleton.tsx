import React from 'react';
import styles from './ResultSkeleton.module.css';

/**
 * Props for `ResultSkeleton`.
 */
export interface ResultSkeletonProps {
  /** What is being worked out, e.g. "Working out the price…"; the card's heading. */
  heading: string;
}

/**
 * The result card while its answer is on the way: the heading says what is
 * happening, and shimmering blocks stand where the price, its range and the
 * home's details will land, so the card does not jump when they arrive.
 */
export function ResultSkeleton({ heading }: ResultSkeletonProps): React.ReactElement {
  return (
    <div className={styles.card} role="status" aria-busy="true">
      <h2 className={styles.heading} id="result-heading" tabIndex={-1}>
        {heading}
      </h2>
      <div className={styles.top} aria-hidden="true">
        <div className={styles.stack}>
          <span className={[styles.bone, styles.label].join(' ')} />
          <span className={[styles.bone, styles.value].join(' ')} />
        </div>
        <div className={styles.stack}>
          <span className={[styles.bone, styles.label, styles.centred].join(' ')} />
          <span className={[styles.bone, styles.band].join(' ')} />
          <span className={styles.ends}>
            <span className={[styles.bone, styles.end].join(' ')} />
            <span className={[styles.bone, styles.end].join(' ')} />
          </span>
        </div>
      </div>
      <span className={[styles.bone, styles.row].join(' ')} aria-hidden="true" />
    </div>
  );
}

export default ResultSkeleton;
