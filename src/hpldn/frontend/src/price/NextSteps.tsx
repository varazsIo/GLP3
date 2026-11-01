import React from 'react';
import Button from '../ui/Button';
import styles from './NextSteps.module.css';

/**
 * Props for `NextSteps`.
 */
export interface NextStepsProps {
  /** Further actions, shown first in the row. */
  children?: React.ReactNode;
  /** Selects the home's area in Areas and moves there. */
  onSeeArea?: () => void;
  /** Starts the yield calculator from this home's figures and moves there. */
  onWorkOutYield?: () => void;
}

/**
 * The actions under a price or estimate: on to the area, or to the rental sums.
 */
export function NextSteps({ children, onSeeArea, onWorkOutYield }: NextStepsProps): React.ReactElement | null {
  if (!children && !onSeeArea && !onWorkOutYield) {
    return null;
  }
  return (
    <div className={styles.row}>
      {children}
      {onSeeArea ? (
        <Button variant="secondary" onClick={onSeeArea}>
          See the area
        </Button>
      ) : null}
      {onWorkOutYield ? (
        <Button variant="ghost" className={styles.ghost} onClick={onWorkOutYield}>
          Work out rental yield
        </Button>
      ) : null}
    </div>
  );
}

export default NextSteps;
