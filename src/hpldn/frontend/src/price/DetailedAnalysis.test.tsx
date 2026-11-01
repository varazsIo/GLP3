import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { InflationSeries, NearbySales } from '../data/types';
import { ESTIMATED_PRICE, NEARBY_SCHOOLS, PRICE } from '../test/fixtures';
import type { HomeRent } from './DetailedAnalysis';
import { averagePrice, pricePerSqm } from './DetailedAnalysis';
import { inspectedText, schoolDetails, schoolRows } from './NearbySchools';
import PriceResultCard from './PriceResultCard';

// 1 % a month, so the figures are easy to check by hand.
const INFLATION: InflationSeries = {
  source: 'ONS CPIH',
  latestMonth: '2026-08-01',
  latestAnnualRate: (Math.pow(1.01, 12) - 1) * 100,
  months: [
    { month: '2026-04-01', index: 100 },
    { month: '2026-05-01', index: 101 },
    { month: '2026-06-01', index: 102 },
    { month: '2026-07-01', index: 103 },
    { month: '2026-08-01', index: 104 },
  ],
};

const NEARBY: NearbySales = {
  windowFrom: '2025-07-31',
  windowTo: '2026-07-31',
  sales: [
    {
      address: { postnumber: '4', poststreet: 'BARCLAY ROAD', posttown: 'CROYDON', postcode: 'CR0 1JN' },
      propertyType: 'flat',
      price: 300000,
      soldOn: '2026-04-20',
      distanceM: 40,
      compMedianPrice: null,
      compMedianPricePerSqm: null,
      compTxnCount: null,
      localTypeMedian: null,
    },
    {
      address: { postnumber: '9', poststreet: 'BARCLAY ROAD', posttown: 'CROYDON', postcode: 'CR0 1JN' },
      propertyType: 'flat',
      price: 400000,
      soldOn: '2026-05-02',
      distanceM: 90,
      compMedianPrice: null,
      compMedianPricePerSqm: null,
      compTxnCount: null,
      localTypeMedian: null,
    },
  ],
};

const RENT: HomeRent = { monthly: 2000, month: '2026-08-01', basis: 'ONS mean rent for 1-bed homes in Croydon, August 2026' };

/** The analysis's figures as "label: value" lines, notes left out. */
function figures(): string[] {
  const definitions = screen.getAllByRole('definition');
  return screen.getAllByRole('term').map((term, i) => {
    // The analysis's value is the definition's first span (a note may follow
    // it); other lists on the card hold the value directly.
    const [value = definitions[i]] = within(definitions[i]).queryAllByText((_, element) => element?.tagName === 'SPAN');
    return term.textContent + ': ' + value.textContent;
  });
}

/** Wait until no figure or section of the analysis is still loading. */
async function settled(): Promise<void> {
  await waitFor(() => {
    for (const element of [...screen.queryAllByRole('definition'), ...screen.queryAllByRole('region')]) {
      expect(element).not.toHaveAttribute('aria-busy');
    }
  });
}

async function openAnalysis(props: Partial<React.ComponentProps<typeof PriceResultCard>> = {}) {
  const loadNearby = jest.fn().mockResolvedValue(NEARBY);
  const loadInflation = jest.fn().mockResolvedValue(INFLATION);
  const loadSchools = jest.fn().mockResolvedValue(NEARBY_SCHOOLS);
  const view = render(
    <PriceResultCard
      result={PRICE}
      status="ready"
      loadNearby={loadNearby}
      loadInflation={loadInflation}
      loadSchools={loadSchools}
      rent={RENT}
      {...props}
    />,
  );
  fireEvent.click(screen.getByRole('button', { name: 'Detailed Analysis' }));
    await settled();
  return { ...view, loadNearby, loadInflation, loadSchools };
}

describe('Detailed Analysis', () => {
  it('turns the card to the analysis, with a back button in focus', async () => {
    await openAnalysis();
    const back = screen.getByRole('button', { name: 'Back to the price' });
    expect(back).toHaveFocus();
    expect(screen.getByText('Detailed Analysis')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '18 Barclay Road, Croydon, CR0 1JN' })).toBeInTheDocument();
    // The price face is gone.
    expect(screen.queryByRole('button', { name: 'Detailed Analysis' })).not.toBeInTheDocument();
  });

  it('lists the price, last sale, specs, price per m² and nearby figures, inflation corrected to the sale date', async () => {
    await openAnalysis();
    expect(figures()).toEqual([
      'Price if sold on 15 September 2026: £244,000',
      'Last sold: £260,000 in June 2026',
      // 260,000 x 105.04 / 102 (September is August carried on 1 %).
      'Last sold, inflation corrected: £267,749',
      'Type: Flat',
      'Floor area: 45 m²',
      'Rooms: 2',
      'Energy rating: C',
      'Price per m² (based on estimated price): £5,422',
      'Price per m² (based on last sold price): £5,778',
      'Average price of nearby properties: £350,000',
      // (300,000 x 1.0504 + 400,000 x 105.04 / 101) / 2 = 365,560.
      'Average price of nearby properties, inflation corrected: £366,000',
      'Average rent of nearby properties: £2,000 a month',
      'Average rent of nearby properties, inflation corrected: £2,020 a month',
    ]);
    expect(screen.getByText('Mean of the 2 nearest sales, July 2025 to July 2026')).toBeInTheDocument();
    expect(screen.getByText('ONS mean rent for 1-bed homes in Croydon, August 2026')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Inflation corrected figures are in September 2026 pounds, using the ONS consumer prices index including ' +
          'housing costs (CPIH), published to August 2026; later months assume its latest annual rate, 12.7% a year.',
      ),
    ).toBeInTheDocument();
  });

  it('turns back to the price and returns focus to the button that opened it', async () => {
    await openAnalysis();
    fireEvent.click(screen.getByRole('button', { name: 'Back to the price' }));
    const button = screen.getByRole('button', { name: 'Detailed Analysis' });
    expect(button).toHaveFocus();
    expect(screen.getByText('Based on sales recorded up to July 2026')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Back to the price' })).not.toBeInTheDocument();
  });

  it('ends with each nearby sale and its comparable-sales figures', async () => {
    const loadNearby = jest.fn().mockResolvedValue({
      ...NEARBY,
      sales: [
        {
          ...NEARBY.sales[0],
          distanceM: 82,
          compMedianPrice: 736101,
          compMedianPricePerSqm: 9231,
          compTxnCount: 5,
          localTypeMedian: 510000,
        },
        NEARBY.sales[1],
      ],
    });
    await openAnalysis({ loadNearby });
    const nearby = screen.getByRole('region', { name: 'Nearby sales' });
    const rows = within(nearby).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    // The sales follow the last nearby figure and the schools, at the foot.
    const lastFigure = screen.getByText('Average rent of nearby properties, inflation corrected');
    expect(lastFigure.compareDocumentPosition(rows[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const schools = screen.getByRole('region', { name: 'Schools' });
    expect(schools.compareDocumentPosition(nearby) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(nearby).getByText('What the nearest homes sold for, July 2025 to July 2026')).toBeInTheDocument();
    expect(within(rows[0]).getByText('4 Barclay Road, Croydon, CR0 1JN')).toBeInTheDocument();
    expect(within(rows[0]).getByText('£300,000')).toBeInTheDocument();
    expect(within(rows[0]).getByText('Flat · 80 m away · Sold April 2026')).toBeInTheDocument();
    expect(within(rows[0]).getByText('£736,101 median of 5')).toBeInTheDocument();
    expect(within(rows[0]).getByText('£9,231')).toBeInTheDocument();
    expect(within(rows[0]).getByText('£510,000')).toBeInTheDocument();
  });

  it('says when there are no sales nearby', async () => {
    await openAnalysis({ loadNearby: jest.fn().mockResolvedValue({ ...NEARBY, sales: [] }), loadSchools: undefined });
    expect(screen.getByText('No sales near this home in the 12 months to July 2026.')).toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  it('fetches nearby sales, schools and CPIH once, however often the card turns', async () => {
    const { loadNearby, loadInflation, loadSchools } = await openAnalysis();
    fireEvent.click(screen.getByRole('button', { name: 'Back to the price' }));
    fireEvent.click(screen.getByRole('button', { name: 'Detailed Analysis' }));
      await settled();
    expect(screen.getByText('£300,000')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Croydon High' })).toBeInTheDocument();
    expect(loadNearby).toHaveBeenCalledTimes(1);
    expect(loadInflation).toHaveBeenCalledTimes(1);
    expect(loadSchools).toHaveBeenCalledTimes(1);
    expect(loadSchools).toHaveBeenCalledWith(PRICE.address, expect.anything());
  });

  it('lists the nearest schools with their Ofsted rating, each linking to its report', async () => {
    await openAnalysis();
    const schools = screen.getByRole('region', { name: 'Schools' });
    const rows = within(schools).getAllByRole('listitem');
    // The all-through school is listed once, among the primaries.
    expect(rows.map((row) => row.textContent)).toEqual([
      'Primary · St Mary’s CE Primary School · Good · inspected Mar 2023 · 420 m away',
      'All-through · Barclay Academy · Expected standard · inspected Jan 2026 · 1.3 km away',
      'Secondary · Croydon High · Outstanding · inspected May 2014 (predecessor school) · 1.8 km away',
    ]);
    const link = within(rows[0]).getByRole('link', { name: 'St Mary’s CE Primary School' });
    expect(link).toHaveAttribute('href', 'https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/101234');
    expect(link).toHaveAttribute('target', '_blank');
    expect(
      within(schools).getByText(
        'Ratings since September 2024 come from key judgements or report cards rather than a single grade. ' +
          'Distance is a straight line and does not mean a catchment place.',
      ),
    ).toBeInTheDocument();
  });

  it('says when no rated school is in range, and leaves the section out without a loader', async () => {
    const { unmount } = await openAnalysis({
      loadSchools: jest.fn().mockResolvedValue({ ...NEARBY_SCHOOLS, primary: [], secondary: [] }),
    });
    expect(screen.getByText('No rated state schools within 3 km.')).toBeInTheDocument();
    unmount();
    await openAnalysis({ loadSchools: undefined });
    expect(screen.queryByRole('region', { name: 'Schools' })).not.toBeInTheDocument();
  });

  it('reports a failed schools fetch and tries again', async () => {
    const loadSchools = jest.fn().mockRejectedValueOnce(new Error('down')).mockResolvedValue(NEARBY_SCHOOLS);
    await openAnalysis({ loadSchools });
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong on our side. Try again in a moment.');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
      await settled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Croydon High' })).toBeInTheDocument();
  });

  it('shows an estimated home’s specs, including its neighbourhood’s energy rating, as estimates', async () => {
    await openAnalysis({ result: { ...ESTIMATED_PRICE, estimatedEnergyRating: 'D' } });
    const specs = within(screen.getByLabelText('About this home'));
    expect(specs.getAllByRole('term').map((term) => term.textContent)).toEqual([
      'Type',
      'Floor area (est.)',
      'Rooms (est.)',
      'Energy rating (est.)',
    ]);
    expect(specs.getByText('D')).toBeInTheDocument();
    expect(
      screen.getByText('No energy certificate on record, so these are estimated from 6 detached houses on this street.'),
    ).toBeInTheDocument();
    expect(screen.getAllByText('Floor area estimated')).toHaveLength(2);
  });

  it('says what it cannot know: no last sale, floor area, rent or CPIH', async () => {
    await openAnalysis({
      result: { ...PRICE, lastSale: null, floorAreaSqm: null },
      rent: null,
      loadInflation: undefined,
    });
    expect(figures()).toEqual([
      'Price if sold on 15 September 2026: £244,000',
      'Last sold: No earlier sale on record',
      'Type: Flat',
      'Rooms: 2',
      'Energy rating: C',
      'Price per m² (based on estimated price): Not known',
      'Average price of nearby properties: £350,000',
      'Average price of nearby properties, inflation corrected: Not known',
      'Average rent of nearby properties: Not known',
    ]);
  });

  it('reports a failed fetch and tries again', async () => {
    const loadNearby = jest.fn().mockRejectedValueOnce(new Error('down')).mockResolvedValue(NEARBY);
    await openAnalysis({ loadNearby });
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong on our side. Try again in a moment.');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
      await settled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(figures()).toContain('Average price of nearby properties: £350,000');
  });

  it('opens on the price again for a new answer', async () => {
    const { rerender } = await openAnalysis();
    rerender(<PriceResultCard result={{ ...PRICE, value: 250000 }} status="ready" />);
    expect(screen.getByRole('button', { name: 'Detailed Analysis' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Back to the price' })).not.toBeInTheDocument();
  });
});

describe('analysis helpers', () => {
  it('works out price per m² and the mean nearby price', () => {
    expect(pricePerSqm(507783, 85)).toBe(5974);
    expect(pricePerSqm(507783, null)).toBeNull();
    expect(pricePerSqm(507783, 0)).toBeNull();
    expect(averagePrice(NEARBY.sales)).toBe(350000);
    expect(averagePrice([])).toBeNull();
  });
});


describe('school helpers', () => {
  it('reads a school as rating, inspection month and distance, and lists each school once', () => {
    const [primary] = NEARBY_SCHOOLS.primary;
    expect(schoolDetails(primary)).toBe('Good · inspected Mar 2023 · 420 m away');
    expect(inspectedText(NEARBY_SCHOOLS.secondary[1])).toBe('inspected May 2014 (predecessor school)');
    expect(schoolRows(NEARBY_SCHOOLS).map((school) => school.urn)).toEqual([101234, 140001, 145000]);
  });
});
