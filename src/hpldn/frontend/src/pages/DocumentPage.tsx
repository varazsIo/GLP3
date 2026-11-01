import React from 'react';
import PageShell from '../layout/PageShell';
import { hrefFor } from '../lib/routes';
import type { PageKey } from '../lib/routes';
import styles from './DocumentPage.module.css';

/**
 * Props for `DocumentPage`.
 */
export interface DocumentPageProps {
  /** Small label above the heading. */
  eyebrow: string;
  heading: string;
  /** Introductory paragraph. */
  lead: string;
  /** Other long-form pages worth pointing at from this one. */
  related: ReadonlyArray<{ page: PageKey; label: string }>;
  children: React.ReactNode;
}

/**
 * A standalone long-form page, used for the terms, privacy policy and AVM explainer.
 */
export function DocumentPage({
  eyebrow,
  heading,
  lead,
  related,
  children,
}: DocumentPageProps): React.ReactElement {
  return (
    <PageShell skipTo={{ href: '#document', label: 'Skip to content' }}>
      <article className={styles.page} id="document" aria-labelledby="document-heading">
        <div className={styles.inner}>
          <a className={styles.back} href={hrefFor('home')}>
            ← Back to HPLDN
          </a>
          <header className={styles.head}>
            <span className={styles.eyebrow}>{eyebrow}</span>
            <h1 className={styles.heading} id="document-heading">
              {heading}
            </h1>
            <p className={styles.lead}>{lead}</p>
          </header>

          {children}

          <nav className={styles.related} aria-label="Related pages">
            {related.map((item) => (
              <a className={styles.relatedLink} href={hrefFor(item.page)} key={item.page}>
                {item.label}
              </a>
            ))}
          </nav>
        </div>
      </article>
    </PageShell>
  );
}

export default DocumentPage;
