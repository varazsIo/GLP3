import React, { useState } from 'react';
import { hrefFor } from '../lib/routes';
import styles from './FaqAccordion.module.css';

/** One question and its answer. */
export interface FaqItem {
  question: string;
  answer: React.ReactNode;
}

/**
 * Props for `FaqAccordion`.
 */
export interface FaqAccordionProps {
  /** The questions to show; defaults to the common questions. */
  items?: FaqItem[];
}

/** The common questions, all in plain words. */
const DEFAULT_ITEMS: FaqItem[] = [
  {
    question: 'How accurate are the prices, and can I rely on one?',
    answer: (
      <>
        Most land close to what homes really sell for, but we cannot see condition, lease length or what the
        neighbours are like, so use a price to compare homes and ask a surveyor before you commit;{' '}
        <a className={styles.link} href={hrefFor('avm', 'accuracy')}>
          see how accurate they are
        </a>
        .
      </>
    ),
  },
  {
    question: 'Which areas do you cover?',
    answer:
      'All of Greater London. Inner London goes by postcode district, from EC1 to W14, and outer London by postcode area, such as BR for Bromley. A few London postcodes that belong to areas based outside London, such as TN16 near Biggin Hill, are not covered yet.',
  },
  {
    question: 'Where does the rental figure come from?',
    answer:
      'From the average private rent in the borough, which the Office for National Statistics publishes. You can swap in your own price and rent in the rental sums.',
  },
  {
    question: 'How often does the data update?',
    answer:
      'Every month. HM Land Registry publishes sales monthly, about two months behind, so the most recent weeks are always missing.',
  },
  {
    question: 'Does the sale date change the price?',
    answer:
      'Only through the home itself, such as its energy certificate or the time since it last sold. We do not forecast the market: every date is priced on the market as of the latest recorded sales, so a date years ahead is not a prediction of prices then.',
  },
  {
    question: 'What if my home is unusual?',
    answer:
      'Conversions, very large homes and anything with a short lease are harder to price. We widen the range rather than claim a precision we do not have.',
  },
];

/**
 * A keyboard-accessible disclosure list, every item closed to start.
 *
 * Enter and Space toggle an item.
 */
export function FaqAccordion({ items = DEFAULT_ITEMS }: FaqAccordionProps): React.ReactElement {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  return (
    <div className={styles.list}>
      {items.map((item, index) => {
        const open = openIndex === index;
        const panelId = 'faq-panel-' + index;
        const triggerId = 'faq-trigger-' + index;
        return (
          <div className={styles.item} key={item.question}>
            <h3>
              <button
                type="button"
                id={triggerId}
                className={[styles.trigger, open ? styles.open : ''].filter(Boolean).join(' ')}
                aria-expanded={open}
                aria-controls={panelId}
                onClick={() => setOpenIndex(open ? null : index)}
              >
                {item.question}
                <span className={styles.sign} aria-hidden="true" />
              </button>
            </h3>
            <div id={panelId} role="region" aria-labelledby={triggerId} hidden={!open}>
              <p className={styles.panel}>{item.answer}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default FaqAccordion;
