import React from 'react';
import { useAreas } from '../data/AreasProvider';
import type { DataWindow, MortgageRates } from '../data/types';
import { formatCount, formatGBP, formatPercent, formatSoldDate } from '../lib/format';
import { hrefFor } from '../lib/routes';
import styles from '../ui/Prose.module.css';
import { INSURANCE_A_YEAR, LETTING_FEE, MAINTENANCE, mortgageRateFor } from '../yield/assumptions';
import DocumentPage from './DocumentPage';

/** A labelled point within a clause. */
interface Point {
  label: string;
  text: string;
}

/** One numbered clause of the explainer. */
interface Clause {
  id: string;
  heading: string;
  body?: React.ReactNode;
  points?: ReadonlyArray<Point>;
  after?: React.ReactNode;
}

/**
 * Formats a share as a percentage to one decimal place, e.g. 0.379 becomes "37.9%".
 */
function percent(share: number): string {
  return Math.round(share * 1000) / 10 + '%';
}

/**
 * Says which mortgage rate the calculator uses, quoting the live 75% rate as an example.
 */
function mortgageSentence(rates: MortgageRates | null): string {
  const example = mortgageRateFor(rates, 25);
  const base =
    'Borrowing is the price less your deposit, on interest-only terms at the Bank of England’s published two-year fixed rate for the loan-to-value band your deposit falls in';
  if (!example) {
    return base + '.';
  }
  return (
    base +
    ' (' +
    formatPercent(example.ratePercent, { decimals: 2 }) +
    ' at 75% loan to value for ' +
    formatSoldDate(example.rateDate) +
    ', the latest month held).'
  );
}

/**
 * Builds the explainer's clauses with figures from the live data window and rates.
 *
 * Before the window loads, or if it cannot, the sentences omit the numbers
 * rather than showing stale ones.
 */
function clausesFor(dataWindow: DataWindow | null, mortgageRates: MortgageRates | null): ReadonlyArray<Clause> {
  const salesHeld = dataWindow
    ? formatCount(dataWindow.totalSales) + ' Greater London sales recorded since ' + dataWindow.firstSaleYear
    : 'every Greater London sale in the Land Registry record';
  const windowYears = dataWindow ? (
    <>
      sales dated {dataWindow.fromYear} to {dataWindow.toYear} inclusive, up to the latest sale we hold (
      {formatSoldDate(dataWindow.latestSale)})
    </>
  ) : (
    <>sales from the latest three full years and the current one, up to the latest sale we hold</>
  );
  const year = dataWindow ? String(dataWindow.toYear) : 'the latest year';
  const previousYear = dataWindow ? String(dataWindow.toYear - 1) : 'the year before';
  const fiveYearsBefore = dataWindow ? String(dataWindow.toYear - 5) : 'five years before';
  const figuresPeriod = dataWindow ? (
    <>
      every sale in {year} we hold, up to the latest ({formatSoldDate(dataWindow.latestSale)}), so the current year is
      a part year
    </>
  ) : (
    <>every sale in the latest calendar year we hold, which is usually a part year</>
  );

  return [
    {
      id: 'what-it-is',
      heading: '1. What an automated valuation model is',
      body: 'An automated valuation model, or AVM, is software that estimates what a property would sell for from records of what comparable properties actually sold for. Nobody visits the property. That is what makes it fast enough to screen a shortlist in seconds, and it is also why an estimate is a starting point for your own research and never a valuation.',
    },
    {
      id: 'method',
      heading: '2. How an estimate is made',
      body: 'An estimate starts from the median price per square metre of recent sales in the postcode district, multiplied by the floor area you enter. That figure is then adjusted for the type of home, for the number of bedrooms against what a home of that size usually has, and for how well served the district is by public transport, and rounded to the nearest £1,000.',
      points: [
        {
          label: 'Price per square metre',
          text: 'The median across the district’s sales with a recorded floor area, over the same years as the nearby sales described below.',
        },
        {
          label: 'Type of home',
          text: 'Against a terraced house of the same size, a flat is placed 6% lower, a maisonette 3% lower, a semi-detached house 6% higher and a detached house 18% higher.',
        },
        {
          label: 'Bedrooms against size',
          text: '1.5% higher or lower for each bedroom more or fewer than a home of that floor area usually has, by no more than 6% either way.',
        },
        {
          label: 'Transport',
          text: 'Up to 3.6% higher or lower for public transport access in the district, measured from the middle of a five-point scale.',
        },
      ],
      after: (
        <p className={styles.body}>
          The range shown with an estimate spans 7%, 10% or 13% either side of it. It widens where we hold fewer recent
          sales of the same type and a similar floor area nearby (in the district, or in outer London in the postcode’s
          own district such as BR3), so a wide range means the estimate rests on
          less evidence. The estimate does not see the property itself: its condition, age, lease, energy rating and
          what it last sold for all go unmeasured.
        </p>
      ),
    },
    {
      id: 'data',
      heading: '3. The data behind it',
      points: [
        {
          label: 'HM Land Registry Price Paid Data',
          text:
            'The price actually paid for every residential sale in England and Wales, with the date, the address and the type of home. We hold ' +
            salesHeld +
            '. Published monthly under the Open Government Licence.',
        },
        {
          label: 'Energy performance certificates',
          text: 'The floor area recorded for each sale address, which turns prices into prices per square metre, and tells flats from maisonettes. The typical energy rating in the area profile comes from them too.',
        },
        {
          label: 'Transport for London access data',
          text: 'How well served each 100 m square of London is by public transport, and the walk to the nearest station.',
        },
        {
          label: 'English Indices of Deprivation',
          text: 'Income, employment, education, health, crime, barriers to housing and living environment, published for small neighbourhoods of around 1,500 people. Used for the area profile only.',
        },
      ],
    },
    {
      id: 'features',
      heading: '4. What an estimate uses',
      points: [
        {
          label: 'The home you describe',
          text: 'Its postcode district, type, floor area and number of bedrooms. A full postcode is used only to measure how far away the listed nearby sales are.',
        },
        {
          label: 'The district’s recent sales',
          text: 'Their median price per square metre, and how many sales of the same type and a similar size there were, which sets the width of the range.',
        },
        { label: 'Transport', text: 'The district’s typical public transport access.' },
        {
          label: 'Shown alongside, not used in the figure',
          text: 'The nearby sales, how prices have moved over one and five years, the area profile and the typical energy rating are there to help you judge an estimate. They do not change it.',
        },
      ],
    },
    {
      id: 'accuracy',
      heading: '5. How accurate it is',
      body: 'Accuracy is weakest where sales are rare or unusual: very expensive homes, very cheap ones, conversions, homes with short leases, and anything in unusual condition. A price for a specific home shows, beside it, the share of recent sales our model priced within 10% of what was actually paid. Treat an estimate as a first screen, never as a price.',
    },
    {
      id: 'area-figures',
      heading: '6. The area figures',
      body: (
        <>
          Every district figure on the home page is computed from {figuresPeriod}, and compared with the same
          figure for {previousYear} and {fiveYearsBefore}. The nearby sales listed under an estimate are drawn from{' '}
          {windowYears}.
        </>
      ),
      points: [
        { label: 'Average price', text: `The mean price of the district’s ${year} sales.` },
        {
          label: 'Price per square metre',
          text: `The ${year} average price divided by the mean floor area of the same sales.`,
        },
        {
          label: `Against ${previousYear}`,
          text: `The change in the average price from ${previousYear} to ${year}.`,
        },
        {
          label: 'Over five years',
          text: `The change in the average price from ${fiveYearsBefore} to ${year}.`,
        },
        {
          label: 'Transport access',
          text: 'Public transport access on a five-point scale, and the typical walk to the nearest station.',
        },
        {
          label: 'Area profile',
          text: `Where the district sits in London on the Index of Multiple Deprivation: the average, over its ${year} sales, of each home’s London percentile, to three significant figures. 32.5% means more deprived than 32.5% of London neighbourhoods and less deprived than 67.5%; a lower figure is better.`,
        },
        {
          label: 'Postcode district data from Doogal',
          text: 'The district outlines on the map, generalised for display. Contains public sector information licensed under the Open Government Licence.',
        },
        {
          label: 'Addresses from OpenStreetMap',
          text: 'Some older sales are matched to their home using building addresses from OpenStreetMap. © OpenStreetMap contributors, available under the Open Database Licence (ODbL).',
        },
        {
          label: 'Schools from Ofsted',
          text: 'School inspections from Ofsted; school locations from DfE Get Information about Schools. Contains public sector information licensed under the Open Government Licence v3.0.',
        },
      ],
    },
    {
      id: 'yield-calculator',
      heading: '7. The yield calculator',
      body: 'The calculator starts from the estimate, or from the district’s average price before you have one, and from the ONS average private rent for the borough, by bedrooms where known. You can replace either with your own figures. It then works out three numbers.',
      points: [
        {
          label: 'Gross yield',
          text: 'A year’s rent as a percentage of the purchase price: monthly rent × 12 ÷ price.',
        },
        {
          label: 'Net yield',
          text: `A year’s rent less running costs, as a percentage of the price. Running costs are assumptions, not data: letting and management at ${percent(
            LETTING_FEE,
          )} of rent, upkeep at ${percent(MAINTENANCE)} of the price a year, and ${formatGBP(
            INSURANCE_A_YEAR,
          )} a year of insurance.`,
        },
        {
          label: 'Monthly position',
          text: `Monthly rent less a twelfth of those running costs, less interest on the amount borrowed. ${mortgageSentence(
            mortgageRates,
          )} A negative figure is what the property costs you each month.`,
        },
      ],
      after: (
        <p className={styles.body}>
          Not included: stamp duty, legal and survey fees, void periods between tenants, service charges and ground
          rent, repairs beyond routine upkeep, and tax. Rents are the average private rent in each district’s borough, not a
          rental valuation; the City of London has none published. Treat every output as a first screen, and check it against real lettings evidence and your own
          costs.
        </p>
      ),
    },
    {
      id: 'updates',
      heading: '8. How often it updates',
      body: 'Monthly. PPD is updated on the 20th of every month, data for recent months will be incomplete or unavailable as PPD data has a time lag of around 2 months.',
    },
  ];
}

/**
 * The AVM explainer page: how an estimate is made, how far to trust it, and how the yield maths works.
 */
export function AvmPage(): React.ReactElement {
  const { window: dataWindow, mortgageRates } = useAreas();
  const clauses = clausesFor(dataWindow, mortgageRates);

  return (
    <DocumentPage
      eyebrow="AVM"
      heading="How our estimates work"
      lead="How every estimate is worked out, the data behind it, how accurate it is, and the maths in the yield calculator."
      related={[
        { page: 'terms', label: 'Terms and conditions' },
        { page: 'privacy', label: 'Privacy policy' },
      ]}
    >
      <div className={styles.prose}>
        <div className={styles.callout}>
          <h2 className={styles.calloutHead}>A screening tool, not a valuation</h2>
          <p className={styles.calloutBody}>
            An estimate is not a valuation, a survey or financial advice, and it is no replacement for an evaluation by
            a qualified expert. We are not financial advisors. Use it to decide what is worth a closer look, then read
            the{' '}
            <a className={styles.link} href={hrefFor('terms', 'legal-advice')}>
              terms and conditions
            </a>{' '}
            before you act on one.
          </p>
        </div>

        {clauses.map((clause) => (
          <section className={styles.clause} id={clause.id} key={clause.id} aria-labelledby={clause.id + '-heading'}>
            <h2 className={styles.clauseHead} id={clause.id + '-heading'}>
              {clause.heading}
            </h2>
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
            {clause.after}
          </section>
        ))}
      </div>
    </DocumentPage>
  );
}

export default AvmPage;
