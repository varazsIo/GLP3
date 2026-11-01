import React from 'react';
import { PROPERTY_TYPE_NAMES } from '../data/types';
import type { NearbySale, NearbySales } from '../data/types';
import { formatCount, formatGBP, formatSoldDate } from '../lib/format';
import { describeFullAddress } from '../lib/price';
import styles from './NearbyProperties.module.css';

/** A distance in metres as it reads beside a sale, e.g. "60 m away" or "1.2 km away". */
export function formatMetres(metres: number): string {
  if (metres < 1000) {
    return formatCount(Math.max(0, Math.round(metres / 10) * 10)) + ' m away';
  }
  return (metres / 1000).toFixed(1) + ' km away';
}

/** The figures a sale's comparable-sales rows hold, leaving out any that are empty. */
export function saleFigures(sale: NearbySale): Array<{ label: string; value: string }> {
  const figures: Array<{ label: string; value: string }> = [];
  if (sale.compMedianPrice !== null) {
    const count = sale.compTxnCount ?? 0;
    figures.push({
      label: 'Similar sales near it',
      value: formatGBP(sale.compMedianPrice) + ' median' + (count > 0 ? ' of ' + count : ''),
    });
  }
  if (sale.compMedianPricePerSqm !== null) {
    figures.push({ label: 'Price per m²', value: formatGBP(sale.compMedianPricePerSqm) });
  }
  if (sale.localTypeMedian !== null) {
    figures.push({
      label: PROPERTY_TYPE_NAMES[sale.propertyType] + ' median within 800 m',
      value: formatGBP(sale.localTypeMedian),
    });
  }
  return figures;
}

/**
 * Props for `NearbyProperties`.
 */
export interface NearbyPropertiesProps {
  /** The sales nearest the priced home. */
  data: NearbySales;
}

/**
 * What the nearest homes sold for in the latest 12 months, each with the
 * comparable-sales figures held for it, at the foot of the detailed analysis.
 */
export function NearbyProperties({ data }: NearbyPropertiesProps): React.ReactElement {
  if (data.sales.length === 0) {
    return <p className={styles.note}>No sales near this home in the 12 months to {formatSoldDate(data.windowTo)}.</p>;
  }
  return (
    <>
      <p className={styles.note}>
        What the nearest homes sold for, {formatSoldDate(data.windowFrom)} to {formatSoldDate(data.windowTo)}
      </p>
      <ul className={styles.list}>
        {data.sales.map((sale) => (
          <li className={styles.sale} key={describeFullAddress(sale.address) + sale.soldOn + sale.price}>
            <div className={styles.saleTop}>
              <span className={styles.address}>{describeFullAddress(sale.address)}</span>
              <span className={styles.price}>{formatGBP(sale.price)}</span>
            </div>
            <p className={styles.meta}>
              {PROPERTY_TYPE_NAMES[sale.propertyType]} · {formatMetres(sale.distanceM)} · Sold {formatSoldDate(sale.soldOn)}
            </p>
            {saleFigures(sale).length > 0 ? (
              <dl className={styles.figures}>
                {saleFigures(sale).map((figure) => (
                  <div className={styles.figure} key={figure.label}>
                    <dt>{figure.label}</dt>
                    <dd>{figure.value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </li>
        ))}
      </ul>
    </>
  );
}

export default NearbyProperties;
