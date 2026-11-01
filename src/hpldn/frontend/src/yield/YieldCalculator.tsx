import React, { useEffect, useRef, useState } from 'react';
import type { MortgageRates } from '../data/types';
import { formatGBP, formatPercent, formatSoldDate, groupDigits, stripNonDigits } from '../lib/format';
import { hrefFor } from '../lib/routes';
import Field from '../ui/Field';
import Segmented from '../ui/Segmented';
import { INSURANCE_A_YEAR, LETTING_FEE, MAINTENANCE, mortgageRateFor } from './assumptions';
import styles from './YieldCalculator.module.css';

/**
 * Figures the calculator can start from, and what they describe.
 */
export interface YieldFigures {
  /** Changes whenever the figures describe something else, e.g. a new price. */
  key: string;
  /** Completes "Based on …", e.g. "your price for 18 Barclay Road, Croydon, CR0 1JN". */
  description: string;
  purchasePrice: number;
  /** Null where no rent is published; the rent field is then left for the landlord. */
  monthlyRent: number | null;
}

/**
 * Props for `YieldCalculator`.
 */
export interface YieldCalculatorProps {
  /** The latest price or estimate's figures, once there is one. */
  home: YieldFigures | null;
  /** The focused area's typical figures, once they have loaded. */
  area: YieldFigures | null;
  /** Changes each time the landlord asks to work out this home's yield, which switches back to it. */
  homeRequest?: number;
  /** The latest published mortgage rates; null until loaded or when none are held. */
  mortgageRates?: MortgageRates | null;
}

/** Where the figures in the fields came from. */
export type YieldSource = 'home' | 'area' | 'own';

/** Deposit slider bounds, in percent: from a typical minimum to buying outright. */
export const DEPOSIT_MIN = 10;
export const DEPOSIT_MAX = 100;

/** Purchase price bounds, in pounds: nothing in London sells for less than the floor. */
export const PRICE_MIN = 10_000;
export const PRICE_MAX = 100_000_000;

/** A gross yield above this, in percent, is far outside London's and most likely a typo. */
export const IMPLAUSIBLE_YIELD = 20;

/** Monthly rent bounds, in pounds. */
export const RENT_MIN = 1;
export const RENT_MAX = 1_000_000;

/** The most digits a money field keeps: enough to show that a figure is too big, not enough to break the layout. */
const MAX_DIGITS = 12;

/** What the figures show while a field is empty or out of bounds. */
const NO_FIGURE = '—';

/**
 * Checks a money field against its bounds.
 *
 * @param text What the field holds, e.g. "425,000".
 * @param label How the error names the figure, e.g. "a purchase price".
 * @param touched Whether the landlord has left the field, so an empty one is worth mentioning.
 * @returns The amount in pounds, or null with the error to show, if any.
 */
export function readMoney(
  text: string,
  bounds: { min: number; max: number },
  label: string,
  touched: boolean,
): { value: number | null; error?: string } {
  const digits = stripNonDigits(text);
  const range = 'Enter ' + label + ' from ' + formatGBP(bounds.min) + ' to ' + formatGBP(bounds.max) + '.';
  if (!digits) {
    return { value: null, error: touched ? range : undefined };
  }
  const value = Number(digits);
  if (value < bounds.min || value > bounds.max) {
    return { value: null, error: range };
  }
  return { value };
}

/**
 * Where the caret belongs once a money field's text is regrouped: after the
 * same digits it followed as typed, so deleting the "2" from "2,632" leaves
 * the caret at the start of "632".
 *
 * @param typed The field's text as typed, e.g. ",632".
 * @param caret The caret's index in `typed`.
 * @param grouped The regrouped text, e.g. "632".
 * @returns The caret's index in `grouped`.
 */
export function caretFor(typed: string, caret: number, grouped: string): number {
  const digits = stripNonDigits(typed).slice(0, MAX_DIGITS);
  const leadingZeros = digits.length - digits.replace(/^0+(?=\d)/, '').length;
  let wanted = Math.min(stripNonDigits(typed.slice(0, caret)).length, digits.length) - leadingZeros;
  if (wanted <= 0) {
    return 0;
  }
  for (let at = 0; at < grouped.length; at++) {
    if (/\d/.test(grouped[at]) && --wanted === 0) {
      return at + 1;
    }
  }
  return grouped.length;
}

/**
 * Keeps a number figure on one line, sized to fit its box: the CSS reads
 * `--chars` to shrink long figures such as -£318,373.
 */
function fitStyle(text: string): React.CSSProperties {
  return { '--chars': Math.max(text.length, 6) } as React.CSSProperties;
}

/**
 * Yield calculator: price, rent and deposit, with the monthly position, gross
 * yield and net yield recomputed as the landlord types.
 *
 * A toggle says where the figures come from: this home, the area's typical
 * figures, or the landlord's own. Typing switches to their own, and nothing
 * new overwrites those until they choose another source or ask to work out a
 * home's yield. The price must be £10,000 to £100,000,000 and the rent £1 to
 * £1,000,000; until both are, the figures show a dash. A gross yield above
 * `IMPLAUSIBLE_YIELD` asks the landlord to check the figures. Costs come from
 * `./assumptions`; the mortgage rate is the published rate for the deposit's
 * loan-to-value band, and the figures show a dash while none is known and
 * anything is borrowed.
 */
export function YieldCalculator({
  home,
  area,
  homeRequest = 0,
  mortgageRates = null,
}: YieldCalculatorProps): React.ReactElement {
  const initial = home ?? area;
  const [source, setSource] = useState<YieldSource>(home ? 'home' : area ? 'area' : 'own');
  const [price, setPrice] = useState(() => (initial ? groupDigits(String(initial.purchasePrice)) : ''));
  const [rent, setRent] = useState(() =>
    initial && initial.monthlyRent !== null ? groupDigits(String(initial.monthlyRent)) : '',
  );
  const [depositPercent, setDepositPercent] = useState(25);
  const [touched, setTouched] = useState({ price: false, rent: false });

  const lastHomeKey = useRef(home?.key);
  const lastHomeRequest = useRef(homeRequest);

  const apply = (figures: YieldFigures): void => {
    setPrice(groupDigits(String(figures.purchasePrice)));
    setRent(figures.monthlyRent === null ? '' : groupDigits(String(figures.monthlyRent)));
  };

  useEffect(() => {
    const homeChanged = home?.key !== lastHomeKey.current;
    const requested = homeRequest !== lastHomeRequest.current;
    lastHomeKey.current = home?.key;
    lastHomeRequest.current = homeRequest;

    let next = source;
    if (home && (requested || (homeChanged && source !== 'own'))) {
      next = 'home';
    } else if (next === 'home' && !home) {
      next = area ? 'area' : 'own';
    }
    if (next !== source) {
      setSource(next);
    }
    const figures = next === 'home' ? home : next === 'area' ? area : null;
    if (figures) {
      apply(figures);
    }
    // Only new figures or a new request apply them; typing must not re-run this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [home?.key, home?.purchasePrice, home?.monthlyRent, area?.key, area?.purchasePrice, area?.monthlyRent, homeRequest]);

  const choose = (next: YieldSource): void => {
    setSource(next);
    const figures = next === 'home' ? home : next === 'area' ? area : null;
    if (figures) {
      apply(figures);
    }
  };

  const active = source === 'home' ? home : source === 'area' ? area : null;

  const priceCheck = readMoney(price, { min: PRICE_MIN, max: PRICE_MAX }, 'a purchase price', touched.price);
  const rentCheck = readMoney(rent, { min: RENT_MIN, max: RENT_MAX }, 'a monthly rent', touched.rent);
  const priceValue = priceCheck.value;
  const rentValue = rentCheck.value;

  const mortgage = mortgageRateFor(mortgageRates, depositPercent);
  const borrowing = depositPercent < 100;
  const rateMissing = borrowing && mortgage === null;

  let sums: { monthlyCash: number; grossYield: number; netYield: number } | null = null;
  if (priceValue !== null && rentValue !== null && !rateMissing) {
    const annualRent = rentValue * 12;
    const annualCosts = annualRent * LETTING_FEE + priceValue * MAINTENANCE + INSURANCE_A_YEAR;
    const borrowed = priceValue * (1 - depositPercent / 100);
    const monthlyInterest = mortgage ? (borrowed * mortgage.ratePercent) / 100 / 12 : 0;
    sums = {
      monthlyCash: rentValue - annualCosts / 12 - monthlyInterest,
      grossYield: (annualRent / priceValue) * 100,
      netYield: ((annualRent - annualCosts) / priceValue) * 100,
    };
  }

  const monthlyText = sums ? formatGBP(sums.monthlyCash) : NO_FIGURE;
  const grossText = sums ? formatPercent(sums.grossYield) : NO_FIGURE;
  const netText = sums ? formatPercent(sums.netYield) : NO_FIGURE;
  const losing = sums !== null && sums.monthlyCash < 0;

  const moneyField = (
    id: string,
    label: string,
    value: string,
    setValue: (next: string) => void,
    error: string | undefined,
    onBlur: () => void,
  ): React.ReactElement => (
    <Field id={id} label={label} error={error}>
      {({ className, ...props }) => (
        <div className={styles.money}>
          <span className={styles.currency} aria-hidden="true">
            £
          </span>
          <input
            {...props}
            className={[className, styles.moneyInput].join(' ')}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={value}
            onChange={(event) => {
              const input = event.target;
              let typed = input.value;
              let caret = input.selectionStart ?? typed.length;
              // Backspace that only took a separator also takes the digit before it,
              // or regrouping would put the separator straight back.
              if (
                (event.nativeEvent as InputEvent).inputType === 'deleteContentBackward' &&
                stripNonDigits(typed) === stripNonDigits(value)
              ) {
                const digit = typed.slice(0, caret).search(/\d(?=\D*$)/);
                if (digit >= 0) {
                  typed = typed.slice(0, digit) + typed.slice(digit + 1);
                  caret = digit;
                }
              }
              const next = groupDigits(stripNonDigits(typed).slice(0, MAX_DIGITS));
              const at = caretFor(typed, caret, next);
              // Write the grouped text and caret now: left to React, a value that
              // gains or loses a separator puts the caret at the end.
              input.value = next;
              input.setSelectionRange(at, at);
              setValue(next);
              setSource('own');
            }}
            onBlur={onBlur}
          />
        </div>
      )}
    </Field>
  );

  return (
    <div className={styles.strip}>
      <div className={styles.inputs}>
        <Segmented
          label="Figures from"
          className={styles.sources}
          value={source}
          onChange={choose}
          options={[
            { value: 'home', label: 'This home', disabled: !home },
            { value: 'area', label: 'Area typical', disabled: !area },
            { value: 'own', label: 'My own figures' },
          ]}
        />
        <p className={styles.source} aria-live="polite">
          {active ? 'Based on ' + active.description + '.' : 'Your own figures.'}
          {active && active.monthlyRent === null ? ' No typical rent is published here; enter your own.' : ''}
        </p>

        {moneyField('yield-price', 'Purchase price', price, setPrice, priceCheck.error, () =>
          setTouched((was) => ({ ...was, price: true })),
        )}
        {moneyField('yield-rent', 'Monthly rent', rent, setRent, rentCheck.error, () =>
          setTouched((was) => ({ ...was, rent: true })),
        )}

        <Field id="yield-deposit" label="Deposit">
          {(props) => (
            <span className={styles.depositRow}>
              <span
                className={styles.depositTrack}
                style={{ '--at': (depositPercent - DEPOSIT_MIN) / (DEPOSIT_MAX - DEPOSIT_MIN) } as React.CSSProperties}
              >
                <span className={styles.depositValue}>
                  <span>{depositPercent}%</span>
                  {priceValue !== null ? <span> · {formatGBP((priceValue * depositPercent) / 100)}</span> : null}
                </span>
                <input
                  {...props}
                  className={styles.range}
                  type="range"
                  min={DEPOSIT_MIN}
                  max={DEPOSIT_MAX}
                  step={1}
                  value={depositPercent}
                  aria-valuetext={depositPercent + '%'}
                  onChange={(event) => setDepositPercent(Number(event.target.value))}
                />
              </span>
              <span className={styles.depositBounds} aria-hidden="true">
                <span>{DEPOSIT_MIN}% minimum</span>
                <span>{DEPOSIT_MAX}% cash</span>
              </span>
            </span>
          )}
        </Field>
      </div>

      <div className={styles.outputs}>
        <div className={styles.headline}>
          <span className={styles.figureLabel}>
            {!sums ? 'Each month' : losing ? 'Monthly shortfall' : 'Monthly surplus'}
          </span>
          <span
            className={[styles.headlineValue, losing ? styles.negative : ''].filter(Boolean).join(' ')}
            style={fitStyle(monthlyText)}
          >
            {monthlyText}
          </span>
          <span className={styles.helper}>
            {!sums
              ? rateMissing && priceValue !== null && rentValue !== null
                ? 'No published mortgage rate for this deposit yet'
                : 'Enter a purchase price and monthly rent to see the sums'
              : losing
                ? 'What it costs you each month, after costs and mortgage interest'
                : 'What lands in your account each month, after costs and mortgage interest'}
          </span>
        </div>

        <div className={styles.figures}>
          <div className={styles.figure}>
            <span className={styles.figureLabel}>Gross yield</span>
            <span className={styles.figureValue} style={fitStyle(grossText)}>
              {grossText}
            </span>
            <span className={styles.helper}>Rent against the price you pay</span>
          </div>
          <div className={styles.figure}>
            <span className={styles.figureLabel}>Net yield</span>
            <span
              className={[styles.figureValue, sums && sums.netYield < 0 ? styles.negative : ''].filter(Boolean).join(' ')}
              style={fitStyle(netText)}
            >
              {netText}
            </span>
            <span className={styles.helper}>After letting, upkeep and insurance</span>
          </div>
        </div>

        {sums && sums.grossYield > IMPLAUSIBLE_YIELD ? (
          <p className={styles.check} role="status">
            A gross yield over {IMPLAUSIBLE_YIELD}% is far above any London area’s. Check the price and rent.
          </p>
        ) : null}

        <details className={styles.included}>
          <summary>What’s included</summary>
          <ul className={styles.includedList}>
            <li>Assumed letting and management: {Math.round(LETTING_FEE * 100)}% of the rent</li>
            <li>Assumed upkeep: {Math.round(MAINTENANCE * 100)}% of the price each year</li>
            <li>Assumed landlord insurance: {formatGBP(INSURANCE_A_YEAR)} a year</li>
            <li>
              {!borrowing
                ? 'Mortgage: none, as the deposit is the whole price'
                : mortgage
                  ? 'Mortgage: interest only at ' +
                    formatPercent(mortgage.ratePercent, { decimals: 2 }) +
                    ', the Bank of England two-year fixed rate at ' +
                    mortgage.band +
                    '% loan to value for ' +
                    formatSoldDate(mortgage.rateDate) +
                    ', on everything but the deposit'
                  : 'Mortgage: no published rate is held for this deposit'}
            </li>
          </ul>
          <a className={styles.link} href={hrefFor('avm', 'yield-calculator')}>
            How the sums are worked out
          </a>
        </details>
      </div>
    </div>
  );
}

export default YieldCalculator;
