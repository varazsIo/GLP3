import React from 'react';
import styles from './TrustBand.module.css';

/** Where the figures come from, in the order a reader would recognise them. */
const SOURCES: ReadonlyArray<string> = [
  'HM Land Registry',
  'Energy certificates',
  'Transport for London',
  'ONS rents',
  'Refreshed monthly',
];

/**
 * A thin strip saying where the numbers come from.
 */
export function TrustBand(): React.ReactElement {
  return (
    <section className={styles.strip} id="sources" aria-labelledby="sources-heading">
      <div className={styles.inner}>
        <h2 className={styles.statement} id="sources-heading">
          Built on public records of what people actually paid, not asking prices.
        </h2>
        <ul className={styles.sources}>
          {SOURCES.map((source) => (
            <li className={styles.source} key={source}>
              {source}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

export default TrustBand;
