import React from 'react';
import Button from './Button';
import styles from './DataNotice.module.css';

/**
 * Props for `DataNotice`.
 */
export interface DataNoticeProps {
  /** Whether the figures are still loading or failed to load. */
  status: 'loading' | 'error';
  /** What is loading, e.g. "Loading the area figures…". */
  loadingText: string;
  /** Shown when status is 'error'. */
  errorText?: string | null;
  /** Shows a "Try again" button when given. */
  onRetry?: () => void;
}

/**
 * Stands in for a section whose figures are still loading or failed to arrive.
 */
export function DataNotice({ status, loadingText, errorText, onRetry }: DataNoticeProps): React.ReactElement {
  if (status === 'loading') {
    return (
      <div className={styles.notice} role="status" aria-busy="true">
        <p className={styles.text}>{loadingText}</p>
      </div>
    );
  }
  return (
    <div className={styles.notice} role={onRetry ? 'alert' : 'status'}>
      <p className={styles.text}>{errorText ?? 'We could not load these figures just now.'}</p>
      {onRetry ? (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export default DataNotice;
