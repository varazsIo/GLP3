import React from 'react';
import styles from '../ui/Prose.module.css';

/** One numbered clause of the document. */
interface Clause {
  id: string;
  heading: string;
  body?: string;
  points?: Array<{ label: string; text: string }>;
}

const CLAUSES: ReadonlyArray<Clause> = [
  {
    id: 'privacy-controller',
    heading: '1. Who is responsible',
    body:
      'HPLDN is the controller of the personal data described here, as that term is used in the UK General Data Protection Regulation. Write to privacy@hpldn.example for anything in this policy, including a request to exercise your rights. If we ever appoint a data protection officer, their details will appear here.',
  },
  {
    id: 'privacy-data',
    heading: '2. What we collect',
    points: [
      {
        label: 'What you type into the estimate form',
        text:
          'A postcode or postcode district, the type of home, the number of bedrooms, and the floor area. A full postcode can identify a household, so we treat it as personal data.',
      },
      {
        label: 'How you use the site',
        text:
          'Pages viewed, estimates requested, and the approximate region your request came from, together with technical data such as browser type and IP address.',
      },
      {
        label: 'What you send us',
        text: 'Your email address and the content of your message, if you contact us.',
      },
    ],
  },
  {
    id: 'privacy-basis',
    heading: '3. Why we are allowed to use it',
    points: [
      {
        label: 'Legitimate interests, Article 6(1)(f)',
        text:
          'To produce the estimate you asked for, to keep the site secure, and to understand in aggregate how it is used. We have weighed this against your interests and use the least data that will do the job.',
      },
      {
        label: 'Consent, Article 6(1)(a)',
        text:
          'For any analytics or marketing cookie that is not strictly necessary, and for any marketing email. You can withdraw consent at any time under Article 7(3), and we stop.',
      },
      {
        label: 'Contract, Article 6(1)(b)',
        text: 'To provide and support any account or paid service you take out with us.',
      },
      {
        label: 'Legal obligation, Article 6(1)(c)',
        text: 'Where we must keep records, for example for tax or to answer a lawful request.',
      },
    ],
  },
  {
    id: 'privacy-retention',
    heading: '4. How long we keep it',
    body:
      'Estimate requests are kept in identifiable form for 12 months, then aggregated so no individual property or person can be picked out. Correspondence is kept for 24 months after the matter closes. Security logs, which hold the IP address of each request, are kept for up to 30 days. We keep nothing for longer than the purpose needs, as Article 5(1)(e) requires.',
  },
  {
    id: 'privacy-sharing',
    heading: '5. Who else sees it',
    body:
      'Our hosting, error-monitoring and analytics providers process data on our instructions under written terms that meet Article 28. We do not sell personal data and we do not share it with estate agents, brokers or lenders. Where a provider processes data outside the UK, we rely on the UK international data transfer agreement or an adequacy decision, and we tell you which on request.',
  },
  {
    id: 'privacy-security',
    heading: '6. Keeping out automated traffic',
    points: [
      {
        label: 'Google reCAPTCHA',
        text:
          'Before you price a home, search for an address or ask for an estimate, Google reCAPTCHA v3 checks in the background that a person is using the site. Google’s script loads only once you start using a form, and it collects hardware and software information, such as device and application data, which it sends to Google to make the check. Google’s Privacy Policy (policies.google.com/privacy) and Terms of Service (policies.google.com/terms) apply to that. We receive only a score, and keep a signed note that your browser passed, bound to your IP address, for 30 minutes.',
      },
      {
        label: 'Security logs',
        text:
          'Our servers record the IP address, time, address requested and browser type of each request, and use them to block networks that send automated or abusive traffic. A block lifts automatically, after between 10 minutes and 7 days. Those records, and any block, are kept for up to 30 days, on the legitimate interest of keeping the site available and its data from being copied in bulk. Blocked addresses are also passed to our network provider, Cloudflare, which refuses them before they reach us.',
      },
    ],
  },
  {
    id: 'privacy-automated',
    heading: '7. Automated processing',
    body:
      'Our estimates are produced by software, but they are generic market figures about a property, not decisions about a person. We do not carry out automated decision-making or profiling that produces legal effects for you, of the kind Article 22 restricts, and we do not use your data to decide whether you can have credit, insurance or a tenancy.',
  },
  {
    id: 'privacy-rights',
    heading: '8. Your rights',
    points: [
      {
        label: 'Access, Article 15',
        text: 'A copy of the personal data we hold about you, and an explanation of how we use it.',
      },
      {
        label: 'Rectification and erasure, Articles 16 and 17',
        text: 'Correction of anything inaccurate, and deletion where we no longer need it.',
      },
      {
        label: 'Restriction and objection, Articles 18 and 21',
        text:
          'A pause on our use of your data while a dispute is resolved, and an objection to processing we base on legitimate interests or use for direct marketing.',
      },
      {
        label: 'Portability, Article 20',
        text: 'The data you gave us, in a structured, commonly used, machine-readable form.',
      },
    ],
  },
  {
    id: 'privacy-cookies',
    heading: '9. Cookies',
    body:
      'We set the cookies the site needs to work. Anything beyond that is set only if you agree, as the Privacy and Electronic Communications Regulations require, and you can change your mind from the cookie settings at any time.',
  },
  {
    id: 'privacy-complaints',
    heading: '10. Complaints',
    body:
      'Tell us first and we will put it right if we can. You also have the right under Article 77 to complain to the Information Commissioner’s Office, the UK supervisory authority, at ico.org.uk or on 0303 123 1113.',
  },
];

/**
 * The privacy policy, written to the transparency duties in Articles 13 and 14.
 */
export function PrivacyPolicy(): React.ReactElement {
  return (
    <div className={styles.prose}>
      <p className={styles.updated}>Last updated 2 October 2026</p>

      <p className={styles.body}>
        This policy explains what we do with personal data when you use HPLDN, and it is
        written to the transparency duties in Articles 13 and 14 of the UK General Data
        Protection Regulation. The Regulation itself is published at{' '}
        <a
          className={styles.link}
          href="https://www.legislation.gov.uk/eur/2016/679/contents"
          rel="noreferrer noopener"
          target="_blank"
        >
          legislation.gov.uk
        </a>
        .
      </p>

      {CLAUSES.map((clause) => (
        <div className={styles.clause} id={clause.id} key={clause.id}>
          <h3 className={styles.clauseHead}>{clause.heading}</h3>
          {clause.body ? <p className={styles.body}>{clause.body}</p> : null}
          {clause.points ? (
            <div className={styles.list}>
              {clause.points.map((point) => (
                <p className={styles.item} key={point.label}>
                  <span>
                    <span className={styles.itemLabel}>{point.label}. </span>
                    {point.text}
                  </span>
                </p>
              ))}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export default PrivacyPolicy;
