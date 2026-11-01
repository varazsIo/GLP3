import React from 'react';
import Footer from './Footer';
import Header from './Header';
import styles from './PageShell.module.css';

/**
 * Props for `PageShell`.
 */
export interface PageShellProps {
  /** Where the skip link lands, and what it says. */
  skipTo: { href: string; label: string };
  /** Home only: moves to the address search. Other pages link back to it instead. */
  onGetEstimate?: () => void;
  children: React.ReactNode;
}

/**
 * The frame every page shares: skip link, header, main content and footer.
 */
export function PageShell({
  skipTo,
  onGetEstimate,
  children,
}: PageShellProps): React.ReactElement {
  return (
    <>
      <a className={styles.skipLink} href={skipTo.href}>
        {skipTo.label}
      </a>
      <Header onGetEstimate={onGetEstimate} overHero={Boolean(onGetEstimate)} />
      <main className={styles.main} id="main" tabIndex={-1}>
        {children}
      </main>
      <Footer />
    </>
  );
}

export default PageShell;
