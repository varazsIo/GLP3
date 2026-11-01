import React, { useMemo, useRef, useState } from 'react';
import SortChips from '../compare/SortChips';
import type { AreaStats, SortMode } from '../data/types';
import { formatGBP, formatGBPCompact, formatPercent } from '../lib/format';
import useMediaQuery from '../ui/useMediaQuery';
import TransportMeter from './TransportMeter';
import styles from './RankedAreas.module.css';

/**
 * Props for `RankedAreas`.
 */
export interface RankedAreasProps {
  /** Every covered area. */
  areas: ReadonlyArray<AreaStats>;
  /** Code of the area the page is focused on. */
  selectedDistrict: string;
  /** Called with an area code when a row is chosen. */
  onSelectDistrict: (code: string) => void;
}

/** Rows shown before "Show all". */
export const SHORTLIST = 10;

/**
 * Orders two figures highest first, with unknown figures last whatever the sort.
 */
function highestFirst(a: number | null, b: number | null): number {
  if (a === null || b === null) {
    return (a === null ? 1 : 0) - (b === null ? 1 : 0);
  }
  return b - a;
}

/** Sort order for each ranking. */
const COMPARATORS: Record<SortMode, (a: AreaStats, b: AreaStats) => number> = {
  cheapest: (a, b) => a.averagePrice - b.averagePrice,
  growing: (a, b) => highestFirst(a.movement1y, b.movement1y),
  connected: (a, b) => highestFirst(a.transportScore, b.transportScore) || b.averagePrice - a.averagePrice,
  yield: (a, b) => highestFirst(a.rentalYield, b.rentalYield),
};

/**
 * The figure a ranking sorts by, as the row shows it.
 */
function metricFor(mode: SortMode, area: AreaStats): React.ReactNode {
  switch (mode) {
    case 'cheapest':
      return area.averagePricePerSqm === null ? 'Size not known' : formatGBP(area.averagePricePerSqm) + ' per m²';
    case 'growing':
      return area.movement1y === null ? 'Not enough sales' : formatPercent(area.movement1y, { signed: true }) + ' in a year';
    case 'connected':
      return <TransportMeter score={area.transportScore} />;
    case 'yield':
      return area.rentalYield === null ? 'Yield not known' : formatPercent(area.rentalYield) + ' yield';
    default:
      return null;
  }
}

/** Rows on each page of the full list on a small screen. */
export const PAGE_SIZE = 10;

/** Screens this narrow page through the full list instead of scrolling it. */
export const PAGED_QUERY = '(max-width: 640px)';

/**
 * Areas ranked by price, growth, transport or yield: the top ten, with every
 * area a click away.
 *
 * "Show all" keeps the section a fixed size: on a large screen the full list
 * scrolls inside its own box, and on a small one (a phone or a watch) it
 * pages through ten at a time. Choosing a row only changes the selection, so
 * the page stays where it is. The selected area always has a row in the
 * shortlist, even outside the top ten, pinned under it with its rank.
 */
export function RankedAreas({ areas, selectedDistrict, onSelectDistrict }: RankedAreasProps): React.ReactElement {
  const [mode, setMode] = useState<SortMode>('cheapest');
  const [showAll, setShowAll] = useState(false);
  const [page, setPage] = useState(0);
  const paged = useMediaQuery(PAGED_QUERY);
  const scroller = useRef<HTMLDivElement>(null);

  const ordered = useMemo(() => areas.slice().sort(COMPARATORS[mode]), [areas, mode]);
  const pageCount = Math.max(1, Math.ceil(ordered.length / PAGE_SIZE));
  const scrolling = showAll && !paged;
  const currentPage = Math.min(page, pageCount - 1);

  const shown = useMemo(() => {
    if (showAll) {
      return paged ? ordered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE) : ordered;
    }
    const top = ordered.slice(0, SHORTLIST);
    const selected = ordered.find((area) => area.code === selectedDistrict);
    return selected && top.indexOf(selected) === -1 ? top.concat(selected) : top;
  }, [ordered, showAll, paged, currentPage, selectedDistrict]);

  /** Back to the first row, as a new ranking or a new view starts from the top. */
  const toTop = (): void => {
    setPage(0);
    if (scroller.current) {
      scroller.current.scrollTop = 0;
    }
  };

  const chooseMode = (next: SortMode): void => {
    setMode(next);
    toTop();
  };

  const goToPage = (next: number): void => {
    if (next >= 0 && next < pageCount) {
      setPage(next);
    }
  };

  // The selected area, when it only shows because it is pinned under the top ten.
  const pinned = !showAll && shown.length > SHORTLIST ? shown[shown.length - 1] : null;

  const list = (
    <ol className={styles.list} id="ranked-areas" aria-label="Areas, ranked">
      {shown.map((area) => {
        const selected = area.code === selectedDistrict;
        const rank = ordered.indexOf(area) + 1;
        return (
          <li key={area.code} className={area === pinned ? styles.pinned : undefined}>
            {area === pinned ? (
              <span className={styles.pinLabel}>
                Your area, ranked {rank} of {ordered.length}
              </span>
            ) : null}
            <button
              type="button"
              className={[styles.row, selected ? styles.selected : ''].filter(Boolean).join(' ')}
              aria-current={selected}
              onClick={() => onSelectDistrict(area.code)}
            >
              <span className={styles.rank}>{rank}</span>
              <span className={styles.name}>
                {area.code} · {area.name}
              </span>
              <span className={styles.price}>{formatGBPCompact(area.averagePrice)}</span>
              <span className={styles.metric}>{metricFor(mode, area)}</span>
              <span className={styles.chevron} aria-hidden="true" />
            </button>
          </li>
        );
      })}
    </ol>
  );

  return (
    <div>
      <SortChips value={mode} onChange={chooseMode} />
      {/* One wrapper either way, so the list is not rebuilt when it starts
          scrolling. While it scrolls it takes focus, so the keyboard can scroll it. */}
      <div
        className={scrolling ? styles.scroller : undefined}
        ref={scroller}
        role={scrolling ? 'region' : undefined}
        aria-label={scrolling ? 'All ' + ordered.length + ' areas, scrollable' : undefined}
        tabIndex={scrolling ? 0 : undefined}
      >
        {list}
      </div>

      {showAll && paged ? (
        <nav className={styles.pager} aria-label="Pages of areas">
          <button
            type="button"
            className={styles.pageButton}
            aria-disabled={currentPage === 0}
            onClick={() => goToPage(currentPage - 1)}
          >
            Previous
          </button>
          <span className={styles.pageStatus} aria-live="polite">
            Page {currentPage + 1} of {pageCount}
          </span>
          <button
            type="button"
            className={styles.pageButton}
            aria-disabled={currentPage === pageCount - 1}
            onClick={() => goToPage(currentPage + 1)}
          >
            Next
          </button>
        </nav>
      ) : null}

      {ordered.length > SHORTLIST ? (
        <button
          type="button"
          className={styles.more}
          aria-expanded={showAll}
          aria-controls="ranked-areas"
          onClick={() => {
            setShowAll((all) => !all);
            toTop();
          }}
        >
          {showAll ? 'Show fewer' : 'Show all ' + ordered.length + ' areas'}
        </button>
      ) : null}
    </div>
  );
}

export default RankedAreas;
