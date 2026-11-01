import React from 'react';
import { hrefFor } from '../lib/routes';
import Reveal from '../ui/Reveal';
import styles from './HowItWorks.module.css';

/** The three steps from address to price. */
const STEPS: ReadonlyArray<{ title: string; body: string }> = [
  {
    title: 'Search the address',
    body: 'Type the street or the postcode and pick the home from the list.',
  },
  {
    title: 'Pick the sale date',
    body:
      'Any day from tomorrow. Prices follow the market as of the latest recorded sales; we do not forecast it, so the date only changes what will be true of the home by then.',
  },
  {
    title: 'See the price, the area and the rental sums',
    body: 'The likely price and its range, then how the area compares and whether it could pay as a rental.',
  },
];

/**
 * The "How it works" section: three short steps and a link to the full method.
 */
export function HowItWorks(): React.ReactElement {
  return (
    <div>
      <ol className={styles.steps}>
        {STEPS.map((step, index) => (
          <Reveal as="li" key={step.title} delay={index * 80}>
            <div className={styles.step}>
              <span className={styles.number}>
                <span className={styles.disc} aria-hidden="true">
                  {index + 1}
                </span>
                <span className={styles.visuallyHidden}>Step {index + 1}</span>
              </span>
              <h3 className={styles.stepTitle}>{step.title}</h3>
              <p className={styles.stepBody}>{step.body}</p>
            </div>
          </Reveal>
        ))}
      </ol>

      <p className={styles.more}>
        <a className={styles.moreLink} href={hrefFor('avm')}>
          How our prices are worked out <span aria-hidden="true">→</span>
        </a>
      </p>
    </div>
  );
}

export default HowItWorks;
