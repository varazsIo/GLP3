import React, { useMemo, useState } from 'react';
import type { AreaStats, DataWindow } from '../data/types';
import { formatCount, formatGBP, formatPercent, formatSignificantPercent, formatSoldDate } from '../lib/format';
import Combobox from '../ui/Combobox';
import Segmented from '../ui/Segmented';
import LondonMap from './LondonMap';
import MapLegend, { PRICE_BREAKS } from './MapLegend';
import RankedAreas from './RankedAreas';
import StatTile from './StatTile';
import TransportMeter from './TransportMeter';
import styles from './AreaSnapshot.module.css';
import tileStyles from './StatTile.module.css';

/**
 * Props for `AreaSnapshot`.
 */
export interface AreaSnapshotProps {
  /** The area the page is focused on. */
  area: AreaStats;
  /** Every covered area. */
  areas: ReadonlyArray<AreaStats>;
  /** The live data window, whose years and latest sale date the snapshot quotes. */
  dataWindow: DataWindow;
  /** Called with an area code when the landlord picks an area. */
  onSelectDistrict: (code: string) => void;
}

type AreaView = 'map' | 'ranked';

const VIEWS: ReadonlyArray<{ value: AreaView; label: string }> = [
  { value: 'map', label: 'Map' },
  { value: 'ranked', label: 'Ranked' },
];

/** The most matches the area search lists while the landlord types. */
const MATCH_LIMIT = 8;

/**
 * Maps each area code to its average price, for the map's shading.
 */
function districtPrices(areas: ReadonlyArray<AreaStats>): Record<string, number> {
  const out: Record<string, number> = {};
  areas.forEach((area) => {
    out[area.code] = area.averagePrice;
  });
  return out;
}

/**
 * Says which part of the year the figures cover, e.g. "January to July 2026",
 * or the whole year once the latest sale is in a later one.
 */
export function describeFiguresPeriod(figuresYear: number, latestSale: string): string {
  const [year, month] = latestSale.split('-').map(Number);
  if (year !== figuresYear || !month || month === 12) {
    return String(figuresYear);
  }
  return month === 1 ? 'January ' + figuresYear : 'January to ' + formatSoldDate(latestSale);
}

/**
 * How an area reads in the search field and its list, e.g. "SW11 · Battersea".
 */
export function areaLabel(area: AreaStats): string {
  return area.code + ' · ' + area.name;
}

/**
 * Finds the areas matching what was typed, by code or name, best match first.
 *
 * An exact code comes first, then codes starting with the text, then names
 * containing it, each group in the server's order.
 */
export function matchAreas(areas: ReadonlyArray<AreaStats>, query: string): AreaStats[] {
  const text = query.trim().toLowerCase();
  if (!text) {
    return areas.slice();
  }
  const code = text.replace(/\s+/g, '').toUpperCase();
  const scored: Array<{ area: AreaStats; score: number; index: number }> = [];
  areas.forEach((area, index) => {
    let score = -1;
    if (area.code === code) {
      score = 0;
    } else if (area.code.startsWith(code)) {
      score = 1;
    } else if (area.name.toLowerCase().includes(text) || areaLabel(area).toLowerCase().includes(text)) {
      score = 2;
    }
    if (score >= 0) {
      scored.push({ area, score, index });
    }
  });
  return scored.sort((a, b) => a.score - b.score || a.index - b.index).map((entry) => entry.area);
}

/**
 * Explore an area: search for one, see it on the map or in a ranked list, and
 * read its figures beneath it.
 *
 * Picking an area anywhere (the search, the map, a ranked row) only changes the
 * figures, so the page never jumps. A figure the data cannot support is said in
 * words, not as a number.
 */
export function AreaSnapshot({ area, areas, dataWindow, onSelectDistrict }: AreaSnapshotProps): React.ReactElement {
  const prices = useMemo(() => districtPrices(areas), [areas]);
  const period = describeFiguresPeriod(area.figuresYear, dataWindow.latestSale);
  const [view, setView] = useState<AreaView>('map');
  // What the landlord is typing; null shows the selected area, however it was picked.
  const [query, setQuery] = useState<string | null>(null);

  const matches = useMemo(
    () => (query === null ? areas.slice() : matchAreas(areas, query).slice(0, MATCH_LIMIT)),
    [areas, query],
  );

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.search}>
          <Combobox<AreaStats>
            id="area-search"
            label="Area"
            name="area-search"
            placeholder="Search by code or name, e.g. SW11 or Battersea"
            inputValue={query ?? areaLabel(area)}
            onInputChange={setQuery}
            options={matches}
            getOptionKey={(candidate) => candidate.code}
            getOptionLabel={areaLabel}
            onSelect={(candidate) => {
              setQuery(null);
              onSelectDistrict(candidate.code);
            }}
            message={query !== null && !matches.length ? 'No areas match. Try a postcode district, e.g. SE15' : null}
            selectedKey={area.code}
            openOnFocus
            selectOnFocus
            onBlur={() => setQuery(null)}
          />
        </div>
        <Segmented label="Show areas as" options={VIEWS} value={view} onChange={setView} />
      </div>

      <div className={styles.layout}>
        <div className={styles.viewSide}>
          {view === 'map' ? (
            <LondonMap
              districtPrices={prices}
              priceBreaks={PRICE_BREAKS}
              selectedDistrict={area.code}
              onSelectDistrict={onSelectDistrict}
              legend={<MapLegend year={area.figuresYear} />}
            />
          ) : (
            <RankedAreas areas={areas} selectedDistrict={area.code} onSelectDistrict={onSelectDistrict} />
          )}
        </div>

        <div className={styles.statsSide}>
          <div className={styles.areaHead} aria-live="polite">
            <span className={styles.eyebrow}>
              {view === 'map' ? 'Selected on the map' : 'Selected in the list'}
            </span>
            <h3 className={styles.district}>{areaLabel(area)}</h3>
            <span className={styles.sales}>
              {formatCount(area.salesCount) + (area.salesCount === 1 ? ' sale, ' : ' sales, ') + period}
            </span>
            {area.lowData ? <span className={styles.lowData}>Few sales: treat with care</span> : null}
          </div>

          <div className={styles.tiles}>
            <StatTile
              label="Average price"
              value={formatGBP(area.averagePrice)}
              delta={area.movement1y}
              deltaLabel={'vs ' + (area.figuresYear - 1)}
              deltaMissing={'No sales in ' + (area.figuresYear - 1) + ' to compare with'}
            />
            <StatTile
              label="5-year change"
              value={
                area.movement5y === null ? (
                  <span className={tileStyles.missing}>No sales in {area.figuresYear - 5}</span>
                ) : (
                  formatPercent(area.movement5y, { signed: true })
                )
              }
              note={'Average price, ' + area.figuresYear + ' vs ' + (area.figuresYear - 5)}
              trend={area.movement5y}
            />
            <StatTile
              label="Transport"
              value={<TransportMeter score={area.transportScore} />}
              note={
                area.walkToStationMins === null
                  ? 'Walk to a station not known'
                  : area.walkToStationMins + ' min walk to a station'
              }
            />
            <StatTile
              label="Deprivation score"
              value={
                area.areaProfilePercent === null ? (
                  <span className={tileStyles.missing}>Not available here</span>
                ) : (
                  formatSignificantPercent(area.areaProfilePercent)
                )
              }
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default AreaSnapshot;
