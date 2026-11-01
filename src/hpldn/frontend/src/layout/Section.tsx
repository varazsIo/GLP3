import React from 'react';
import Reveal from '../ui/Reveal';
import styles from './Section.module.css';

/**
 * Props for `Section`.
 */
export interface SectionProps {
  /** The section's anchor id; the heading id derives from it. */
  id: string;
  /** Small label above the heading. */
  eyebrow?: string;
  heading?: string;
  /** Introductory sentence under the heading. */
  lead?: string;
  /** Sets the lead full width on one line where it fits, wrapping at a readable size where it does not. */
  leadOneLine?: boolean;
  /** Background treatment; defaults to `default`. */
  tone?: 'default' | 'sunk' | 'dark';
  children: React.ReactNode;
}

/**
 * A page band with consistent padding, maximum width and heading.
 */
export function Section({
  id,
  eyebrow,
  heading,
  lead,
  leadOneLine = false,
  tone = 'default',
  children,
}: SectionProps): React.ReactElement {
  const toneClass = tone === 'sunk' ? styles.sunk : tone === 'dark' ? styles.dark : '';
  const headingId = heading ? id + '-heading' : undefined;

  return (
    <section
      id={id}
      className={[styles.section, toneClass].filter(Boolean).join(' ')}
      aria-labelledby={headingId}
    >
      <div className={styles.inner}>
        {heading ? (
          <Reveal className={[styles.head, leadOneLine ? styles.headWide : ''].filter(Boolean).join(' ')}>
            {eyebrow ? <span className={styles.eyebrow}>{eyebrow}</span> : null}
            <h2 className={styles.heading} id={headingId} tabIndex={-1}>
              {heading}
            </h2>
            {lead ? (
              <p className={[styles.lead, leadOneLine ? styles.leadOneLine : ''].filter(Boolean).join(' ')}>
                {lead}
              </p>
            ) : null}
          </Reveal>
        ) : null}
        {children}
      </div>
    </section>
  );
}

export default Section;
