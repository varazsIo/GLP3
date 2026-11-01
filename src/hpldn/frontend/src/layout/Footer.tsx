import React from 'react';
import { hrefFor } from '../lib/routes';
import styles from './Footer.module.css';

/**
 * Props for `Footer`.
 */
export interface FooterProps {
  /** Copyright year; defaults to 2026. */
  year?: number;
}

/** The footer's link groups. */
const GROUPS: ReadonlyArray<{ heading: string; links: ReadonlyArray<{ href: string; label: string }> }> = [
  {
    heading: 'Tools',
    links: [
      { href: hrefFor('home', 'top'), label: 'Price a home' },
      { href: hrefFor('home', 'areas'), label: 'Areas' },
      { href: hrefFor('home', 'yield'), label: 'Yield' },
    ],
  },
  {
    heading: 'About',
    links: [
      { href: hrefFor('avm'), label: 'How estimates work' },
      { href: hrefFor('terms'), label: 'Terms' },
      { href: hrefFor('privacy'), label: 'Privacy' },
    ],
  },
];

/**
 * Site footer: wordmark, two link groups, and the one notice that prices are
 * not a valuation.
 */
export function Footer({ year = 2026 }: FooterProps): React.ReactElement {
  return (
    <footer className={styles.footer}>
      <div className={styles.inner}>
        <div className={styles.top}>
          <span className={styles.wordmark}>HPLDN</span>
          <nav className={styles.groups} aria-label="Footer">
            {GROUPS.map((group) => (
              <div className={styles.group} key={group.heading}>
                <h2 className={styles.groupHeading}>{group.heading}</h2>
                <ul className={styles.links}>
                  {group.links.map((link) => (
                    <li key={link.href}>
                      <a href={link.href}>{link.label}</a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
        </div>

        <p className={styles.disclaimer}>
          Prices and estimates are a guide, not a valuation, a survey or financial advice. Read our{' '}
          <a className={styles.disclaimerLink} href={hrefFor('terms', 'legal-advice')}>
            terms
          </a>{' '}
          before you act on one.
        </p>
        <p className={styles.copyright}>© {year} HPLDN. Built on public London housing data.</p>
      </div>
    </footer>
  );
}

export default Footer;
