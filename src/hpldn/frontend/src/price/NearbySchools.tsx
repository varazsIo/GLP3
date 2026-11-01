import React from 'react';
import type { NearbySchool, NearbySchools as NearbySchoolsData, SchoolPhase } from '../data/types';
import { formatShortMonth } from '../lib/format';
import { formatMetres } from './NearbyProperties';
import styles from './NearbySchools.module.css';

/** How each phase reads at the start of a school's row. */
export const SCHOOL_PHASE_NAMES: Record<SchoolPhase, string> = {
  primary: 'Primary',
  secondary: 'Secondary',
  all_through: 'All-through',
};

/** The sentence under the list: what the ratings rest on, and that distance is not a place. */
export const SCHOOLS_FOOTNOTE =
  'Ratings since September 2024 come from key judgements or report cards rather than a single grade. ' +
  'Distance is a straight line and does not mean a catchment place.';

/** "inspected Mar 2023", or "rated Mar 2023 (predecessor school)" for a converted school's inherited rating. */
export function inspectedText(school: NearbySchool): string {
  const when = formatShortMonth(school.inspectedOn);
  return school.inheritedFromUrn !== null ? 'inspected ' + when + ' (predecessor school)' : 'inspected ' + when;
}

/** A school's row after its name, e.g. "Good · inspected Mar 2023 · 420 m away". */
export function schoolDetails(school: NearbySchool): string {
  return [school.ratingLabel, inspectedText(school), formatMetres(school.distanceM)].join(' · ');
}

/** The primaries, then the secondaries; an all-through school listed in both appears once. */
export function schoolRows(data: NearbySchoolsData): NearbySchool[] {
  const seen = new Set<number>();
  const rows: NearbySchool[] = [];
  for (const school of [...data.primary, ...data.secondary]) {
    if (!seen.has(school.urn)) {
      seen.add(school.urn);
      rows.push(school);
    }
  }
  return rows;
}

/**
 * Props for `NearbySchools`.
 */
export interface NearbySchoolsProps {
  /** The schools nearest the priced home. */
  data: NearbySchoolsData;
}

/**
 * The nearest primary and secondary schools with their Ofsted rating today,
 * each linking to its Ofsted report, in the detailed analysis.
 */
export function NearbySchools({ data }: NearbySchoolsProps): React.ReactElement {
  const rows = schoolRows(data);
  if (rows.length === 0) {
    return <p className={styles.note}>No rated state schools within {Math.round(data.radiusM / 100) / 10} km.</p>;
  }
  return (
    <>
      <ul className={styles.list} aria-label="Nearby schools">
        {rows.map((school) => (
          <li className={styles.school} key={school.urn}>
            <span className={styles.phase}>{SCHOOL_PHASE_NAMES[school.phase]}</span>
            <span aria-hidden="true"> · </span>
            <a className={styles.name} href={school.reportUrl} target="_blank" rel="noopener noreferrer">
              {school.name}
            </a>
            <span aria-hidden="true"> · </span>
            <span className={styles.details}>{schoolDetails(school)}</span>
          </li>
        ))}
      </ul>
      <p className={styles.note}>{SCHOOLS_FOOTNOTE}</p>
    </>
  );
}

export default NearbySchools;
