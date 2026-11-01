import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from './App';
import { describeComparable } from './estimate/ComparableRow';
import { ApiError, getAddresses, getAreas, requestEstimate, requestPrice, searchAddresses } from './lib/api';
import { addDays, earliestSaleDate, formatUkDate } from './lib/price';
import { formatGBP, groupDigits } from './lib/format';
import { homeMonthlyRent } from './yield/assumptions';
import {
  ADDRESSES,
  AREAS,
  AREAS_RESPONSE,
  ESTIMATE,
  PRICE,
  SW11,
  WINDOW,
  estimateFor,
  priceFor,
} from './test/fixtures';

jest.mock('./lib/api', () => {
  const actual = jest.requireActual('./lib/api');
  return {
    ...actual,
    getAreas: jest.fn(),
    requestEstimate: jest.fn(),
    getAddresses: jest.fn(),
    searchAddresses: jest.fn(),
    requestPrice: jest.fn(),
  };
});

const mockGetAreas = getAreas as jest.MockedFunction<typeof getAreas>;
const mockRequestEstimate = requestEstimate as jest.MockedFunction<typeof requestEstimate>;
const mockGetAddresses = getAddresses as jest.MockedFunction<typeof getAddresses>;
const mockSearchAddresses = searchAddresses as jest.MockedFunction<typeof searchAddresses>;
const mockRequestPrice = requestPrice as jest.MockedFunction<typeof requestPrice>;

const HOME = '18 Barclay Road, Croydon, CR0 1JN';

/** Any heading the result slot can show. */
const RESULT_HEADINGS = /Working out|We could not|CR0 1JN|SE15 4TQ/;

/**
 * A covered district's shape on the map, which is a button named for the area.
 */
function mapShape(code: string, name: string): HTMLElement {
  return screen.getByRole('button', { name: new RegExp('^' + code + ' · ' + name + ', average price') });
}

afterEach(() => {
  // Page changes push history entries; start every test back on the home page.
  window.history.replaceState(null, '', '/');
});

beforeEach(() => {
  mockGetAreas.mockResolvedValue(AREAS_RESPONSE);
  mockRequestEstimate.mockImplementation(async (request) => estimateFor(request));
  mockGetAddresses.mockResolvedValue(ADDRESSES);
  mockSearchAddresses.mockResolvedValue(ADDRESSES.slice(1));
  mockRequestPrice.mockImplementation(async (request) => priceFor(request));
});

/**
 * Renders a page and waits until the area figures have arrived.
 *
 * @param path The pathname to render; defaults to the home page.
 */
async function renderReady(path?: string): Promise<ReturnType<typeof render>> {
  const view = render(<App path={path} />);
  await screen.findByRole('combobox', { name: 'Area' });
  return view;
}

/**
 * Clicks a link and says whether the page left it to the browser to follow.
 *
 * Whatever the page decided, the browser's own navigation is then stopped,
 * which jsdom cannot do.
 */
function clickLeavesItToBrowser(link: HTMLElement, init?: MouseEventInit): boolean {
  let leftToBrowser = false;
  const after = (event: Event): void => {
    leftToBrowser = !event.defaultPrevented;
    event.preventDefault();
  };
  window.addEventListener('click', after);
  fireEvent.click(link, init);
  window.removeEventListener('click', after);
  return leftToBrowser;
}

/** The address search. */
function addressInput(): HTMLElement {
  return screen.getByRole('combobox', { name: 'Address' });
}

/** The heading of whatever the result slot shows. */
function resultHeading(name: string | RegExp): Promise<HTMLElement> {
  return screen.findByRole('heading', { level: 2, name });
}

/**
 * Searches for the fixture home and picks it from the suggestions.
 */
async function chooseHome(query = 'cr0 1jn'): Promise<void> {
  fireEvent.change(addressInput(), { target: { value: query } });
  fireEvent.click(await screen.findByRole('option', { name: HOME }));
}

/** Clicks the price form's button. */
function priceIt(): void {
  fireEvent.click(screen.getByRole('button', { name: 'Price this home' }));
}

/** Opens the details estimate under the price form. */
function openDetails(): void {
  const toggle = screen.getByRole('button', { name: 'Can’t find the address? Estimate from details' });
  if (toggle.getAttribute('aria-expanded') !== 'true') {
    fireEvent.click(toggle);
  }
}

/**
 * Opens the details form and types the given values into it.
 */
function fillForm(values: { postcode?: string; floorArea?: string; bedrooms?: string }): void {
  openDetails();
  if (values.postcode !== undefined) {
    fireEvent.change(screen.getByLabelText('Postcode'), { target: { value: values.postcode } });
  }
  if (values.floorArea !== undefined) {
    fireEvent.change(screen.getByLabelText('Floor area'), { target: { value: values.floorArea } });
  }
  if (values.bedrooms !== undefined) {
    fireEvent.change(screen.getByLabelText('Bedrooms'), { target: { value: values.bedrooms } });
  }
}

/** Clicks the details form's button. */
function submit(): void {
  fireEvent.click(screen.getByRole('button', { name: 'Estimate value' }));
}

/** The Areas section. */
function areasSection(): HTMLElement {
  return screen.getByRole('region', { name: 'Explore an area' });
}

/** The Yield section. */
function yieldSection(): HTMLElement {
  return screen.getByRole('region', { name: 'Could it pay as a rental?' });
}

describe('the page before anything is submitted', () => {
  it('leads with the question and one search: address, sale date and a button', async () => {
    await renderReady();
    expect(screen.getByRole('heading', { level: 1, name: 'What’s a London home worth?' })).toBeInTheDocument();
    expect(screen.queryByText(/in today’s market/)).not.toBeInTheDocument();
    expect(addressInput()).toHaveAttribute('placeholder', 'Start typing an address or postcode');
    expect(screen.getByLabelText('Sale date')).toHaveValue(formatUkDate(earliestSaleDate(new Date())));
    expect(screen.getByRole('button', { name: 'Price this home' })).toBeInTheDocument();

    expect(screen.queryByRole('button', { name: /find homes/i })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/find the homes/i);
    expect(screen.queryByText('Screen London Property Prices')).not.toBeInTheDocument();
  });

  it('asks the API for the district figures once', async () => {
    await renderReady();
    expect(mockGetAreas).toHaveBeenCalledTimes(1);
  });

  it('shows no result section, placeholder or footnote before anything is asked for', async () => {
    await renderReady();
    expect(screen.queryByRole('heading', { level: 2, name: RESULT_HEADINGS })).not.toBeInTheDocument();
    expect(screen.queryByText(/will appear here/i)).not.toBeInTheDocument();
    // Superscript note marks; m² is a unit, not a note.
    expect(document.body.textContent).not.toMatch(/¹|³|⁴|⁵|(?<!m)²/);
    expect(mockRequestEstimate).not.toHaveBeenCalled();
    expect(mockRequestPrice).not.toHaveBeenCalled();
  });

  it('folds the details estimate away, and offers sentence-case property types once opened', async () => {
    await renderReady();
    const toggle = screen.getByRole('button', { name: 'Can’t find the address? Estimate from details' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Estimate value' })).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('Property type')).toHaveValue('flat');
    ['Flat', 'Maisonette', 'Terraced house', 'Semi-detached house', 'Detached house'].forEach((name) => {
      expect(screen.getByRole('option', { name })).toBeInTheDocument();
    });
    expect(screen.queryByText('Find it on the listing or energy certificate')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ESTIMATE VALUE' })).not.toBeInTheDocument();
  });

  it('keeps the header to four links and a "Price a home" button that focuses the address search', async () => {
    await renderReady();
    const header = screen.getByRole('banner');
    const nav = within(header).getByRole('navigation', { name: 'Sections' });
    expect(within(nav).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Price a home',
      'Areas',
      'Yield',
      'How it works',
    ]);
    expect(within(header).queryByRole('link', { name: 'AVM' })).not.toBeInTheDocument();
    expect(within(header).queryByRole('link', { name: 'Legal' })).not.toBeInTheDocument();

    fireEvent.click(within(header).getByRole('button', { name: 'Price a home' }));
    expect(addressInput()).toHaveFocus();
  });

  it('says "not a valuation" once, in the footer', async () => {
    await renderReady();
    const mentions = screen.getAllByText(/not a valuation/i);
    expect(mentions).toHaveLength(1);
    expect(screen.getByRole('contentinfo')).toContainElement(mentions[0]);
  });
});

describe('while the district figures load, or fail to', () => {
  it('shows a loading state in the area sections', () => {
    mockGetAreas.mockReturnValue(new Promise(() => {}));
    render(<App />);
    const notices = screen.getAllByRole('status', { busy: true });
    expect(notices.length).toBeGreaterThan(0);
    expect(notices[0]).toHaveTextContent('Loading the latest area figures…');
    expect(screen.queryByRole('combobox', { name: 'Area' })).not.toBeInTheDocument();
    // The price search is usable straight away.
    expect(screen.getByRole('button', { name: 'Price this home' })).toBeInTheDocument();
  });

  it('says so when they cannot be loaded, and loads them again on retry', async () => {
    mockGetAreas.mockRejectedValueOnce(
      new ApiError(0, 'We could not reach our data just now. Check your connection and try again.'),
    );
    render(<App />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('We could not reach our data just now.');
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('combobox', { name: 'Area' })).toBeInTheDocument();
    expect(mockGetAreas).toHaveBeenCalledTimes(2);
  });
});

describe('pricing a specific home on a chosen day', () => {
  it('finds the homes at a postcode, and prices the one chosen for tomorrow by default', async () => {
    await renderReady();
    await chooseHome();
    expect(mockGetAddresses).toHaveBeenCalledWith('CR0 1JN', expect.anything());
    expect(mockSearchAddresses).not.toHaveBeenCalled();
    expect(addressInput()).toHaveValue(HOME);

    priceIt();
    const heading = await resultHeading(HOME);
    expect(mockRequestPrice).toHaveBeenCalledWith(
      {
        postnumber: '18',
        poststreet: 'BARCLAY ROAD',
        posttown: 'CROYDON',
        postcode: 'CR0 1JN',
        saleDate: earliestSaleDate(new Date()),
      },
      expect.anything(),
    );

    const result = screen.getByRole('region', { name: HOME });
    expect(result).toContainElement(heading);
    expect(within(result).getByText('£244,000')).toBeInTheDocument();
    const values = within(result).getAllByRole('definition').map((definition) => definition.textContent);
    expect(within(result).getAllByRole('term').map((term, index) => term.textContent + ': ' + values[index])).toEqual([
      'Type: Flat',
      'Floor area: 45 m²',
      'Rooms: 2',
      'Energy rating: C',
    ]);
    expect(within(result).getByText(/£260,000 in June 2026/)).toBeInTheDocument();
    expect(within(result).getByText('Based on sales recorded up to July 2026')).toBeInTheDocument();
    expect(within(result).getByText('£189,000')).toBeInTheDocument();
    expect(within(result).getByText('£277,000')).toBeInTheDocument();
    expect(result).not.toHaveTextContent(/within 10%/);
    expect(result).not.toHaveTextContent(/screening figure/i);
  });

  it('searches by street when the text is not a full postcode', async () => {
    await renderReady();
    await chooseHome('barclay road');
    expect(mockSearchAddresses).toHaveBeenCalledWith('barclay road', expect.anything());
    expect(mockGetAddresses).not.toHaveBeenCalled();
  });

  it('prices the day typed as DD/MM/YYYY, and names it in the price', async () => {
    await renderReady();
    await chooseHome();
    const day = addDays(new Date(), 30);
    fireEvent.change(screen.getByLabelText('Sale date'), { target: { value: formatUkDate(day) } });
    priceIt();
    await resultHeading(HOME);

    expect(mockRequestPrice.mock.calls[0][0].saleDate).toBe(day);
    const [year, month, date] = day.split('-').map(Number);
    const monthName = [
      'January', 'February', 'March', 'April', 'May', 'June',
      'July', 'August', 'September', 'October', 'November', 'December',
    ][month - 1];
    const label = 'Price if sold on ' + date + ' ' + monthName + ' ' + year;
    expect(screen.getByText((_, element) => element?.tagName === 'SPAN' && element.textContent === label)).toBeInTheDocument();
    // One request per price, and no sentence comparing the day with the earliest one.
    expect(mockRequestPrice).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/than a sale on|Same as a sale on/)).not.toBeInTheDocument();
  });

  it('will not price today or a day in the past, without asking the server', async () => {
    await renderReady();
    await chooseHome();
    fireEvent.change(screen.getByLabelText('Sale date'), { target: { value: formatUkDate(addDays(new Date(), 0)) } });
    priceIt();
    expect(screen.getByText('Choose a date from ' + formatUkDate(earliestSaleDate(new Date())) + '.')).toBeInTheDocument();
    expect(screen.getByLabelText('Sale date')).toHaveFocus();
    expect(mockRequestPrice).not.toHaveBeenCalled();
  });

  it('asks for a home from the suggestions, including after the chosen address is edited', async () => {
    await renderReady();
    priceIt();
    expect(screen.getByText('Choose a home from the suggestions.')).toBeInTheDocument();
    expect(addressInput()).toHaveFocus();

    await chooseHome();
    expect(screen.queryByText('Choose a home from the suggestions.')).not.toBeInTheDocument();
    fireEvent.change(addressInput(), { target: { value: '18 Barclay Road' } });
    priceIt();
    expect(screen.getByText('Choose a home from the suggestions.')).toBeInTheDocument();
    expect(mockRequestPrice).not.toHaveBeenCalled();
  });

  it('shows a postcode with no homes to price in the suggestions', async () => {
    mockGetAddresses.mockRejectedValueOnce(new ApiError(404, 'We could not find any homes we can price in ZZ1 1ZZ'));
    await renderReady();
    fireEvent.change(addressInput(), { target: { value: 'ZZ1 1ZZ' } });
    expect(await screen.findByText('We could not find any homes we can price in ZZ1 1ZZ')).toBeInTheDocument();
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });

  it('says why a home could not be priced and tries the same request again', async () => {
    mockRequestPrice.mockRejectedValueOnce(
      new ApiError(422, 'We have no location for this home: there are no recorded sales in or near its postcode to compare it with, so we cannot price it.'),
    );
    await renderReady();
    await chooseHome();
    priceIt();
    expect(await resultHeading('We could not price this home')).toHaveFocus();
    expect(screen.getByText(/We have no location for this home/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await resultHeading(HOME)).toBeInTheDocument();
    expect(mockRequestPrice).toHaveBeenCalledTimes(2);
    expect(mockRequestPrice.mock.calls[1][0]).toEqual(mockRequestPrice.mock.calls[0][0]);
  });

  it('leaves out the range, the rooms and the last sale when the records do not hold them', async () => {
    mockRequestPrice.mockImplementationOnce(async (request) => ({
      ...priceFor(request),
      low: null,
      high: null,
      habitableRooms: null,
      lastSale: null,
    }));
    await renderReady();
    await chooseHome();
    priceIt();
    await resultHeading(HOME);
    const result = screen.getByRole('region', { name: HOME });
    expect(within(result).queryByText('Price range')).not.toBeInTheDocument();
    expect(within(result).queryByText('Rooms')).not.toBeInTheDocument();
    expect(within(result).getByText('No earlier sale on record')).toBeInTheDocument();
    expect(within(result).queryByText('Last sold')).not.toBeInTheDocument();
  });

  it('marks a home without an energy certificate as estimated', async () => {
    mockRequestPrice.mockImplementationOnce(async (request) => ({
      ...priceFor(request),
      epcSource: 'estimated',
      estimatedFrom: '6 flats on this street',
      energyRating: null,
    }));
    await renderReady();
    await chooseHome();
    priceIt();
    await resultHeading(HOME);
    const result = screen.getByRole('region', { name: HOME });
    expect(within(result).getByText('Floor area (est.)')).toBeInTheDocument();
    expect(within(result).queryByText('Energy rating')).not.toBeInTheDocument();
    expect(
      within(result).getByText(
        'No energy certificate on record — size estimated from 6 flats on this street; the range is wider.',
      ),
    ).toBeInTheDocument();
  });

  it('moves Areas to the home’s district', async () => {
    await renderReady();
    await chooseHome();
    priceIt();
    await resultHeading(HOME);

    expect(within(areasSection()).getByRole('heading', { level: 3, name: 'CR · Croydon' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'See the area' })).not.toBeInTheDocument();
  });

  it('starts the rental sums from this home when asked', async () => {
    await renderReady();
    await chooseHome();
    priceIt();
    await resultHeading(HOME);

    fireEvent.click(screen.getByRole('button', { name: 'Work out rental yield' }));
    const section = yieldSection();
    await waitFor(() => expect(within(section).getByRole('heading', { level: 2 })).toHaveFocus());
    expect(within(section).getByRole('button', { name: 'This home' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(section).getByLabelText('Purchase price')).toHaveValue('244,000');
    // The borough's August 2026 rent for a home of this size (two habitable
    // rooms: a living room and one bedroom), not the borough's mean (£2,000).
    const croydon = AREAS.find((area) => area.code === 'CR')!;
    const rent = homeMonthlyRent(croydon, PRICE.habitableRooms)!;
    expect(rent).toBe(croydon.bedroomRents!.oneBed);
    expect(rent).not.toBe(2000);
    expect(within(section).getByLabelText('Monthly rent')).toHaveValue(groupDigits(String(rent)));
    expect(
      within(section).getByText(
        /Based on your price for 18 Barclay Road.*, at the Camden August 2026 rent for a 1-bed home\./,
      ),
    ).toBeInTheDocument();
  });

  it('shows only the latest result: a new estimate replaces a price', async () => {
    await renderReady();
    await chooseHome();
    priceIt();
    await resultHeading(HOME);

    fillForm({ postcode: 'SE15 4TQ', floorArea: '92' });
    submit();
    expect(await resultHeading(/SE15 4TQ in Peckham/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: HOME })).not.toBeInTheDocument();
  });

  it('keeps backend vocabulary off the price card', async () => {
    const { container } = await renderReady();
    await chooseHome();
    priceIt();
    await resultHeading(HOME);
    const text = (container.textContent ?? '').toLowerCase();
    ['catboost', 'model', 'ptal', 'msoa', 'lsoa', 'shap', 'rmse', 'mape', 'postgres', 'migration', 'smearing', 'deflat'].forEach(
      (term) => {
        expect(text).not.toContain(term);
      },
    );
  });
});

describe('estimating from details', () => {
  it('sends the request and shows the value, the top three reasons and the nearby sales', async () => {
    await renderReady();
    fillForm({ postcode: 'se15 4tq', floorArea: '92', bedrooms: '3' });
    submit();

    expect(await resultHeading(/SE15 4TQ in Peckham/)).toBeInTheDocument();
    expect(mockRequestEstimate).toHaveBeenCalledWith(
      {
        district: 'SE15',
        postcode: 'SE15 4TQ',
        propertyType: 'flat',
        bedrooms: 3,
        floorAreaSqm: 92,
        saleDate: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      },
      expect.anything(),
    );
    expect(screen.getByText(formatGBP(ESTIMATE.value))).toBeInTheDocument();
    expect(screen.getByText('Estimated value')).toHaveTextContent(/^Estimated value$/);

    const reasons = screen.getByRole('list', { name: 'Why this price' });
    const labels = within(reasons)
      .getAllByRole('listitem')
      .map((item) => item.textContent ?? '');
    expect(labels).toHaveLength(3);
    expect(labels[0]).toMatch(/what similar homes nearby sold for/i);
    expect(labels[1]).toMatch(/prices here/i);
    expect(labels[2]).toMatch(/connected|transport/i);

    const sales = screen.getByRole('list', { name: 'Nearby sales' });
    ESTIMATE.comparables.forEach((comparable) => {
      expect(within(sales).getByText(comparable.street)).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /^Show all/ })).not.toBeInTheDocument();
  });

  it('shows three nearby sales, and the rest on request', async () => {
    const extra = [0, 1].map((n) => ({ ...ESTIMATE.comparables[0], id: 'extra-' + n, street: 'Extra Street ' + n }));
    mockRequestEstimate.mockImplementationOnce(async (request) => ({
      ...estimateFor(request),
      comparables: [...ESTIMATE.comparables, ...extra],
    }));
    await renderReady();
    fillForm({ postcode: 'SE15 4TQ', floorArea: '92' });
    submit();
    await resultHeading(/SE15 4TQ in Peckham/);

    const sales = screen.getByRole('list', { name: 'Nearby sales' });
    expect(within(sales).getAllByRole('listitem')).toHaveLength(3);
    const toggle = screen.getByRole('button', { name: 'Show all 5' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(within(sales).getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByRole('button', { name: 'Show fewer' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('says it is working while the estimate is on its way', async () => {
    await renderReady();
    mockRequestEstimate.mockReturnValue(new Promise(() => {}));
    fillForm({ postcode: 'se15 4tq', floorArea: '92' });
    submit();

    const heading = await resultHeading('Working out your estimate…');
    expect(screen.getByRole('status', { busy: true })).toContainElement(heading);
  });

  it('describes each nearby sale as its property type and floor area', async () => {
    await renderReady();
    fillForm({ postcode: 'se15 4tq', floorArea: '92' });
    submit();
    await resultHeading(/SE15 4TQ in Peckham/);

    const allowed = /^(Flat|Maisonette|Terraced house|Semi-detached house|Detached house), \d[\d,]* m²$/;
    ESTIMATE.comparables.forEach((comparable) => {
      const text = describeComparable(comparable);
      expect(text).toMatch(allowed);
      expect(screen.getAllByText(text).length).toBeGreaterThan(0);
    });
  });

  it('moves Areas to the submitted district, outer London by postcode area', async () => {
    await renderReady();
    fillForm({ postcode: 'BR3 1AA', floorArea: '80' });
    submit();

    expect(await within(areasSection()).findByRole('heading', { level: 3, name: 'BR · Bromley' })).toBeInTheDocument();
    expect(mockRequestEstimate.mock.calls[0][0].district).toBe('BR');
    expect(screen.getByRole('combobox', { name: 'Area' })).toHaveValue('BR · Bromley');
  });

  it('folds a sub-district into its district', async () => {
    await renderReady();
    fillForm({ postcode: 'SW1V 4AB', floorArea: '60' });
    submit();

    expect(await screen.findByRole('heading', { level: 3, name: /^SW1 · / })).toBeInTheDocument();
    expect(mockRequestEstimate.mock.calls[0][0].district).toBe('SW1');
  });

  it('shows the range as a minimum and a maximum', async () => {
    await renderReady();
    fillForm({ postcode: 'se15 4tq', floorArea: '92' });
    submit();
    await resultHeading(/SE15 4TQ in Peckham/);

    expect(screen.getByText('Price range')).toBeInTheDocument();
    expect(screen.getByText('Minimum')).toBeInTheDocument();
    expect(screen.getByText('Maximum')).toBeInTheDocument();
    expect(screen.getByText(formatGBP(ESTIMATE.low))).toBeInTheDocument();
    expect(screen.getByText(formatGBP(ESTIMATE.high))).toBeInTheDocument();
  });

  it('prefills the yield calculator from the estimate, with the same next steps as a price', async () => {
    await renderReady();
    expect(within(yieldSection()).getByLabelText('Purchase price')).toHaveValue('819,883');
    fillForm({ postcode: 'SW11 2AB', floorArea: '70' });
    submit();
    await resultHeading(/SW11 2AB in Battersea/);

    expect(screen.getByRole('button', { name: 'See the area' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Work out rental yield' })).toBeInTheDocument();
    await waitFor(() => expect(within(yieldSection()).getByLabelText('Purchase price')).toHaveValue('698,000'));
    expect(within(yieldSection()).getByLabelText('Monthly rent')).toHaveValue('2,550');
    expect(
      within(yieldSection()).getByText(/Based on your estimate for SW11 2AB, at the Wandsworth August 2026 rent for a \d\+?-bed home\./),
    ).toBeInTheDocument();
  });

  it('moves focus to the result so a keyboard user lands on it', async () => {
    await renderReady();
    fillForm({ postcode: 'SW11 8AA', floorArea: '65' });
    submit();

    expect(await resultHeading(/SW11 8AA in Battersea/)).toHaveFocus();
  });

  it('cancels an estimate still in flight when a new one is submitted', async () => {
    await renderReady();
    const signals: AbortSignal[] = [];
    mockRequestEstimate.mockImplementation((request, signal) => {
      signals.push(signal as AbortSignal);
      return signals.length === 1 ? new Promise(() => {}) : Promise.resolve(estimateFor(request));
    });

    fillForm({ postcode: 'SW11 2AB', floorArea: '70' });
    submit();
    fillForm({ postcode: 'SE15 4TQ', floorArea: '92' });
    submit();

    expect(await resultHeading(/SE15 4TQ in Peckham/)).toBeInTheDocument();
    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
  });

  it('shows the problem and tries the same request again', async () => {
    await renderReady();
    mockRequestEstimate.mockRejectedValueOnce(new ApiError(503, 'Something went wrong on our side. Try again in a moment.'));
    fillForm({ postcode: 'se15 4tq', floorArea: '92' });
    submit();

    expect(await resultHeading('We could not get your estimate')).toHaveFocus();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Something went wrong on our side. Try again in a moment.');
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await resultHeading(/SE15 4TQ in Peckham/)).toBeInTheDocument();
    expect(mockRequestEstimate).toHaveBeenCalledTimes(2);
    expect(mockRequestEstimate.mock.calls[1][0]).toEqual(mockRequestEstimate.mock.calls[0][0]);
  });

  it('puts a district the server does not cover on the postcode field, with no result section', async () => {
    await renderReady();
    mockRequestEstimate.mockRejectedValueOnce(new ApiError(404, 'We do not cover SE15 yet'));
    fillForm({ postcode: 'SE15 4TQ', floorArea: '92' });
    submit();

    expect(await screen.findByText('We do not cover SE15 yet')).toBeInTheDocument();
    expect(screen.getByLabelText('Postcode')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Postcode')).toHaveFocus();
    expect(screen.queryByRole('heading', { level: 2, name: RESULT_HEADINGS })).not.toBeInTheDocument();

    // The toggle still folds the details away over the server's message, and opens them again.
    const toggle = screen.getByRole('button', { name: 'Can’t find the address? Estimate from details' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Estimate value' })).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Estimate value' })).toBeInTheDocument();
  });

  it('blocks an empty form and says which fields need attention', async () => {
    await renderReady();
    openDetails();
    submit();

    expect(screen.getByText('Enter a London postcode.')).toBeInTheDocument();
    expect(screen.getByText('Floor area must be at least 15 m².')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: RESULT_HEADINGS })).not.toBeInTheDocument();
    expect(mockRequestEstimate).not.toHaveBeenCalled();
  });

  it('rejects a postcode that is not a postcode, in plain words', async () => {
    await renderReady();
    fillForm({ postcode: 'CR', floorArea: '80' });
    submit();
    expect(screen.getByText('Invalid UK Postcode')).toBeInTheDocument();
    expect(mockRequestEstimate).not.toHaveBeenCalled();
  });

  it('says so when the postcode is outside the districts we cover, without asking the server', async () => {
    await renderReady();
    fillForm({ postcode: 'M1 1AE', floorArea: '80' });
    submit();
    expect(screen.getByText(/we do not cover M1 yet/i)).toBeInTheDocument();
    expect(mockRequestEstimate).not.toHaveBeenCalled();
  });

  it('does not cover a London postcode in a postcode area based outside London', async () => {
    await renderReady();
    fillForm({ postcode: 'TN16 3AA', floorArea: '80' });
    submit();
    expect(screen.getByText(/we do not cover TN16 yet/i)).toBeInTheDocument();
    expect(mockRequestEstimate).not.toHaveBeenCalled();
  });

  it('rejects an implausible floor area, keeps the focus there, and marks only that field invalid', async () => {
    await renderReady();
    fillForm({ postcode: 'SW11 2AB', floorArea: '4' });
    submit();

    expect(screen.getByText('Floor area must be at least 15 m².')).toBeInTheDocument();
    expect(screen.getByLabelText('Floor area')).toHaveFocus();
    expect(screen.getByLabelText('Floor area')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Postcode')).toHaveAttribute('aria-invalid', 'false');
  });
});

describe('exploring an area', () => {
  it('puts the area search and the Map | Ranked toggle above the figures, with the map first', async () => {
    await renderReady();
    const section = areasSection();
    expect(within(section).getByRole('combobox', { name: 'Area' })).toHaveValue('SW11 · Battersea');
    expect(within(section).getByRole('button', { name: 'Map' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(section).getByRole('button', { name: 'Ranked' })).toHaveAttribute('aria-pressed', 'false');
    expect(mapShape('W8', 'Kensington')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /compare/i })).not.toBeInTheDocument();
  });

  it('finds an area by code or by name, and shows its figures', async () => {
    await renderReady();
    const input = screen.getByRole('combobox', { name: 'Area' });
    act(() => input.focus());
    fireEvent.change(input, { target: { value: 'kensing' } });
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'SW7 · South Kensington',
      'W8 · Kensington',
      'W10 · Ladbroke Grove, North Kensington',
      'W14 · West Kensington',
    ]);

    fireEvent.change(input, { target: { value: 'br' } });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(within(areasSection()).getByRole('heading', { level: 3, name: 'BR · Bromley' })).toBeInTheDocument();
    expect(input).toHaveValue('BR · Bromley');
  });

  it('ranks areas cheapest first by default, ten at a time, in sentence-case chips', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: 'Ranked' }));

    const chips = screen.getByRole('group', { name: 'Sort areas' });
    expect(within(chips).getAllByRole('button').map((chip) => chip.textContent)).toEqual([
      'Cheapest',
      'Fastest growing',
      'Best connected',
      'Highest yield',
    ]);
    expect(within(chips).getByRole('button', { name: 'Cheapest' })).toHaveAttribute('aria-pressed', 'true');

    const list = screen.getByRole('list', { name: 'Areas, ranked' });
    const rows = within(list).getAllByRole('button');
    const cheapest = AREAS.slice().sort((a, b) => a.averagePrice - b.averagePrice)[0];
    expect(rows[0]).toHaveTextContent(cheapest.code + ' · ' + cheapest.name);
    // Ten, and the selected SW11 below them.
    expect(rows).toHaveLength(11);
    expect(rows[10]).toHaveAttribute('aria-current', 'true');

    fireEvent.click(screen.getByRole('button', { name: 'Show all 132 areas' }));
    expect(within(list).getAllByRole('listitem')).toHaveLength(132);
    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(within(list).getAllByRole('listitem')).toHaveLength(11);
  });

  it('scrolls the full list in its own box on a large screen, keeping the section its size', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: 'Ranked' }));
    expect(screen.queryByRole('region', { name: /areas, scrollable/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show all 132 areas' }));
    const scroller = screen.getByRole('region', { name: 'All 132 areas, scrollable' });
    expect(scroller).toHaveAttribute('tabindex', '0');
    expect(within(scroller).getAllByRole('listitem')).toHaveLength(132);
    expect(screen.queryByRole('navigation', { name: 'Pages of areas' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(screen.queryByRole('region', { name: /areas, scrollable/ })).not.toBeInTheDocument();
  });

  describe('on a small screen', () => {
    const original = window.matchMedia;

    beforeEach(() => {
      window.matchMedia = ((query: string) => ({
        matches: query === '(max-width: 640px)',
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      })) as unknown as typeof window.matchMedia;
    });

    afterEach(() => {
      window.matchMedia = original;
    });

    it('pages through the full list ten at a time', async () => {
      await renderReady();
      fireEvent.click(screen.getByRole('button', { name: 'Ranked' }));
      fireEvent.click(screen.getByRole('button', { name: 'Show all 132 areas' }));

      const list = screen.getByRole('list', { name: 'Areas, ranked' });
      const pager = screen.getByRole('navigation', { name: 'Pages of areas' });
      expect(screen.queryByRole('region', { name: /areas, scrollable/ })).not.toBeInTheDocument();
      expect(within(list).getAllByRole('listitem')).toHaveLength(10);
      expect(within(pager).getByText('Page 1 of 14')).toBeInTheDocument();
      expect(within(pager).getByRole('button', { name: 'Previous' })).toHaveAttribute('aria-disabled', 'true');
      expect(within(list).getAllByRole('button')[0]).toHaveTextContent(/^1/);

      const next = within(pager).getByRole('button', { name: 'Next' });
      fireEvent.click(next);
      expect(within(pager).getByText('Page 2 of 14')).toBeInTheDocument();
      expect(within(list).getAllByRole('button')[0]).toHaveTextContent(/^11/);

      for (let i = 0; i < 20; i += 1) {
        fireEvent.click(next);
      }
      expect(within(pager).getByText('Page 14 of 14')).toBeInTheDocument();
      expect(next).toHaveAttribute('aria-disabled', 'true');
      expect(within(list).getAllByRole('listitem')).toHaveLength(2);

      // A new ranking starts again from the first page.
      fireEvent.click(screen.getByRole('button', { name: 'Highest yield' }));
      expect(within(pager).getByText('Page 1 of 14')).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
      expect(screen.queryByRole('navigation', { name: 'Pages of areas' })).not.toBeInTheDocument();
    });
  });

  it('re-ranks the list by yield', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: 'Ranked' }));
    fireEvent.click(screen.getByRole('button', { name: 'Highest yield' }));
    const rows = within(screen.getByRole('list', { name: 'Areas, ranked' })).getAllByRole('button');
    const best = AREAS.slice().sort((a, b) => (b.rentalYield ?? 0) - (a.rentalYield ?? 0))[0];
    expect(rows[0]).toHaveTextContent(best.code);
    expect(rows[0]).toHaveTextContent('yield');
  });

  it('picks a ranked row in place, without moving the page or the focus', async () => {
    await renderReady();
    const scroll = jest.fn();
    Element.prototype.scrollIntoView = scroll;
    fireEvent.click(screen.getByRole('button', { name: 'Ranked' }));
    const row = within(screen.getByRole('list', { name: 'Areas, ranked' })).getByRole('button', { name: /BR · Bromley/ });
    row.focus();
    fireEvent.click(row);

    expect(within(areasSection()).getByRole('heading', { level: 3, name: 'BR · Bromley' })).toBeInTheDocument();
    expect(row).toHaveAttribute('aria-current', 'true');
    expect(row).toHaveFocus();
    expect(scroll).not.toHaveBeenCalled();
    delete (Element.prototype as Partial<Element>).scrollIntoView;
  });

  it('picks a district on the map', async () => {
    await renderReady();
    const w8 = mapShape('W8', 'Kensington');
    fireEvent.click(w8);

    expect(within(areasSection()).getByRole('heading', { level: 3, name: 'W8 · Kensington' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Area' })).toHaveValue('W8 · Kensington');
    expect(w8).toHaveAttribute('aria-pressed', 'true');
  });

  it('draws districts we do not cover, but not as something to pick', async () => {
    const { container } = await renderReady();
    // An uncovered shape has no role or name to find it by.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const tn = container.querySelector('path[data-code="TN"]') as SVGPathElement;
    expect(tn).not.toBeNull();
    expect(tn).not.toHaveAttribute('role');
    expect(tn).not.toHaveAttribute('tabindex');

    fireEvent.mouseEnter(tn);
    expect(screen.getByText('TN · not covered')).toBeInTheDocument();
    fireEvent.click(tn);
    expect(screen.getByRole('combobox', { name: 'Area' })).toHaveValue('SW11 · Battersea');
  });

  it('shows four tiles, the sales behind them, and the deprivation score as a card', async () => {
    await renderReady();
    const section = areasSection();
    // SW11's figures from hpdbldn: the mean of its 282 sales from January to July 2026.
    expect(within(section).getByText('282 sales, January to July 2026')).toBeInTheDocument();
    expect(within(section).getByText('Average price')).toBeInTheDocument();
    expect(within(section).getByText('£819,883')).toBeInTheDocument();
    expect(within(section).getByText('-16.6%')).toBeInTheDocument();
    expect(within(section).getByText('vs 2025')).toBeInTheDocument();
    expect(within(section).getByText('Average price, 2026')).toBeInTheDocument();
    expect(within(section).getByText('5-year change')).toBeInTheDocument();
    expect(within(section).getByText('-28.9%')).toBeInTheDocument();
    expect(within(section).getByText('Average price, 2026 vs 2021')).toBeInTheDocument();
    expect(within(section).getByText('Transport')).toBeInTheDocument();
    expect(within(section).getByText('9 min walk to a station')).toBeInTheDocument();
    expect(within(section).queryByText('Area profile')).not.toBeInTheDocument();

    expect(within(section).getByText('Deprivation score')).toBeInTheDocument();
    expect(within(section).getByText('32.5%')).toBeInTheDocument();
    expect(within(section).queryByText(/More deprived than/)).not.toBeInTheDocument();
    expect(within(section).queryByText('Price among London areas')).not.toBeInTheDocument();
    expect(within(section).queryByText('Deprivation')).not.toBeInTheDocument();
    expect(within(section).queryByText(/typically rate/i)).not.toBeInTheDocument();
  });

  it('shows the picked area\'s deprivation score', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: 'Ranked' }));
    fireEvent.click(screen.getByRole('button', { name: 'Show all 132 areas' }));
    fireEvent.click(screen.getByRole('button', { name: /W8 · Kensington/ }));
    expect(within(areasSection()).getByText('12.0%')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /SE15 · Peckham/ }));
    expect(within(areasSection()).getByText('71.0%')).toBeInTheDocument();
  });

  it('shows missing figures in words, never as a stand-in number (MAJ-7)', async () => {
    const thin = {
      ...SW11,
      movement1y: null,
      movement5y: null,
      transportScore: null,
      walkToStationMins: null,
      areaProfilePercent: null,
      rentalYield: null,
      typicalMonthlyRent: null,
      rentMonth: null,
      bedroomRents: null,
      lowData: true,
    };
    mockGetAreas.mockResolvedValue({ ...AREAS_RESPONSE, areas: AREAS.map((a) => (a.code === 'SW11' ? thin : a)) });
    await renderReady();
    const section = areasSection();
    expect(within(section).getByText('No sales in 2025 to compare with')).toBeInTheDocument();
    expect(within(section).getByText('No sales in 2021')).toBeInTheDocument();
    expect(within(section).getByText('Walk to a station not known')).toBeInTheDocument();
    expect(within(section).getByText('Not known')).toBeInTheDocument();
    expect(within(section).getByText('Not available here')).toBeInTheDocument();
    expect(within(section).getByText('Few sales: treat with care')).toBeInTheDocument();
    expect(section.textContent).not.toMatch(/\b1 min walk|\+0\.0%|\b0%/);
    expect(within(yieldSection()).getByLabelText('Monthly rent')).toHaveValue('');
  });

  it('picks an area with a tap and leaves no tooltip behind (MAJ-2)', async () => {
    await renderReady();
    const w8 = mapShape('W8', 'Kensington');
    const touch = (type: string): Event =>
      Object.assign(new MouseEvent(type, { bubbles: true, clientX: 10, clientY: 10 }), {
        pointerId: 7,
        pointerType: 'touch',
      });
    fireEvent(w8, touch('pointerdown'));
    fireEvent.mouseEnter(w8);
    fireEvent(w8, touch('pointerup'));
    fireEvent.click(w8);

    expect(screen.getByRole('heading', { level: 3, name: 'W8 · Kensington' })).toBeInTheDocument();
    expect(within(areasSection()).queryByText(/typical$/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset map view' })).toBeInTheDocument();
  });
});

describe('the rental sums', () => {
  it('asks the question, and says where the figures come from with a three-way toggle', async () => {
    await renderReady();
    const section = yieldSection();
    const sources = within(section).getByRole('group', { name: 'Figures from' });
    expect(within(sources).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'This home',
      'Area typical',
      'My own figures',
    ]);
    expect(within(sources).getByRole('button', { name: 'This home' })).toBeDisabled();
    expect(within(sources).getByRole('button', { name: 'Area typical' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(section).getByText(/Based on the average 2026 price in SW11/)).toBeInTheDocument();
    expect(within(section).getByLabelText('Purchase price')).toHaveValue('819,883');
    expect(within(section).getByLabelText('Monthly rent')).toHaveValue('2,632');
  });

  it('groups typed digits, switches to the landlord’s own figures, and keeps them when the area changes (MAJ-4)', async () => {
    await renderReady();
    const section = yieldSection();
    const price = within(section).getByLabelText('Purchase price');
    fireEvent.change(price, { target: { value: '£425000' } });
    expect(price).toHaveValue('425,000');
    expect(within(section).getByRole('button', { name: 'My own figures' })).toHaveAttribute('aria-pressed', 'true');

    const input = screen.getByRole('combobox', { name: 'Area' });
    fireEvent.change(input, { target: { value: 'BR' } });
    fireEvent.click(screen.getByRole('option', { name: 'BR · Bromley' }));
    expect(screen.getByRole('heading', { level: 3, name: 'BR · Bromley' })).toBeInTheDocument();
    expect(price).toHaveValue('425,000');

    fireEvent.click(within(section).getByRole('button', { name: 'Area typical' }));
    expect(price).toHaveValue('395,000');
    expect(within(section).getByLabelText('Monthly rent')).toHaveValue('1,775');
  });

  it('leads with the monthly position, explains each figure in a line, and keeps the costs in a fold', async () => {
    await renderReady();
    const section = yieldSection();
    expect(within(section).getByText(/^Monthly (shortfall|surplus)$|^Each month$/)).toBeInTheDocument();
    expect(within(section).getByText('Rent against the price you pay')).toBeInTheDocument();
    expect(within(section).getByText('After letting, upkeep and insurance')).toBeInTheDocument();
    expect(section.textContent).not.toMatch(/[³⁴⁵]/);

    expect(within(section).getByText('What’s included')).toBeInTheDocument();
    expect(within(section).getByText('Assumed landlord insurance: £480 a year')).toBeInTheDocument();
    // A 25% deposit borrows 75%: the published 75% LTV rate for July 2026.
    expect(
      within(section).getByText(/interest only at 4\.79%, the Bank of England two-year fixed rate at 75% loan to value for July 2026/),
    ).toBeInTheDocument();
    expect(within(section).getByRole('link', { name: 'How the sums are worked out' })).toHaveAttribute(
      'href',
      '/avm#yield-calculator',
    );
  });

  it('lets the deposit run from 10% to 100% in single steps, a cash purchase paying no interest', async () => {
    await renderReady();
    const section = yieldSection();
    const deposit = within(section).getByLabelText('Deposit');
    expect(deposit).toHaveAttribute('min', '10');
    expect(deposit).toHaveAttribute('max', '100');
    expect(deposit).toHaveAttribute('step', '1');

    fireEvent.change(within(section).getByLabelText('Purchase price'), { target: { value: '200000' } });
    fireEvent.change(within(section).getByLabelText('Monthly rent'), { target: { value: '1000' } });
    fireEvent.change(deposit, { target: { value: '100' } });
    expect(within(section).getByText('100%')).toBeInTheDocument();
    // Rent less letting (10%), upkeep (1% a year) and insurance (£480 a year), with no mortgage.
    expect(within(section).getByText(formatGBP(1000 - 100 - 2000 / 12 - 40))).toBeInTheDocument();

    fireEvent.change(deposit, { target: { value: '37' } });
    expect(within(section).getByText('37%')).toBeInTheDocument();
  });

  it('asks the landlord to check figures that give an implausible yield', async () => {
    // £10,000 at £2,625 a month is a 315% gross yield.
    await renderReady();
    const section = yieldSection();
    fireEvent.change(within(section).getByLabelText('Purchase price'), { target: { value: '10000' } });
    fireEvent.change(within(section).getByLabelText('Monthly rent'), { target: { value: '2625' } });
    expect(within(section).getByRole('status')).toHaveTextContent('Check the price and rent.');
    fireEvent.change(within(section).getByLabelText('Purchase price'), { target: { value: '800000' } });
    expect(within(section).queryByText(/Check the price and rent/)).not.toBeInTheDocument();
  });

  it('keeps the price between £10,000 and £100,000,000 and the rent between £1 and £1,000,000', async () => {
    await renderReady();
    const section = yieldSection();
    const price = within(section).getByLabelText('Purchase price');
    const rent = within(section).getByLabelText('Monthly rent');

    fireEvent.change(price, { target: { value: '9999' } });
    expect(within(section).getByText('Enter a purchase price from £10,000 to £100,000,000.')).toBeInTheDocument();
    expect(price).toHaveAttribute('aria-invalid', 'true');
    // The monthly position and both yields wait for figures in bounds.
    expect(within(section).getAllByText('—')).toHaveLength(3);
    expect(within(section).getByText('Enter a purchase price and monthly rent to see the sums')).toBeInTheDocument();

    fireEvent.change(price, { target: { value: '100000001' } });
    expect(price).toHaveValue('100,000,001');
    expect(within(section).getByText('Enter a purchase price from £10,000 to £100,000,000.')).toBeInTheDocument();

    fireEvent.change(price, { target: { value: '100000000' } });
    expect(price).toHaveAttribute('aria-invalid', 'false');
    fireEvent.change(rent, { target: { value: '1000001' } });
    expect(within(section).getByText('Enter a monthly rent from £1 to £1,000,000.')).toBeInTheDocument();
    fireEvent.change(rent, { target: { value: '1000000' } });
    expect(within(section).queryByText(/Enter a monthly rent/)).not.toBeInTheDocument();

    // Typing stops at twelve digits, however much is pasted.
    fireEvent.change(price, { target: { value: '9'.repeat(40) } });
    expect(price).toHaveValue('999,999,999,999');

    // An empty field says nothing until it is left.
    fireEvent.change(rent, { target: { value: '' } });
    expect(within(section).queryByText(/Enter a monthly rent/)).not.toBeInTheDocument();
    fireEvent.blur(rent);
    expect(within(section).getByText('Enter a monthly rent from £1 to £1,000,000.')).toBeInTheDocument();
  });

  it('keeps a loss and its minus sign in one figure, sized to fit (£100m price, £100,000 rent)', async () => {
    await renderReady();
    const section = yieldSection();
    fireEvent.change(within(section).getByLabelText('Purchase price'), { target: { value: '100000000' } });
    fireEvent.change(within(section).getByLabelText('Monthly rent'), { target: { value: '100000' } });

    // Interest at the published 4.79% on £75m borrowed.
    const figure = within(section).getByText('-£292,748');
    expect(figure.textContent).toBe('-£292,748');
    expect(figure.style.getPropertyValue('--chars')).toBe('9');
    expect(within(section).getByText('What it costs you each month, after costs and mortgage interest')).toBeInTheDocument();
  });
});

describe('the rest of the page', () => {
  it('explains the address flow in three steps, with the method one link away', async () => {
    await renderReady();
    const how = screen.getByRole('region', { name: 'How it works' });
    expect(within(how).getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual([
      'Search the address',
      'Pick the sale date',
      'See the price, the area and the rental sums',
    ]);
    expect(within(how).queryByText('What we look at')).not.toBeInTheDocument();
    expect(within(how).getByRole('link', { name: /How our prices are worked out/ })).toHaveAttribute('href', '/avm');
  });

  it('names the sources in a thin strip, without a disclaimer', async () => {
    await renderReady();
    const strip = screen.getByRole('region', {
      name: 'Built on public records of what people actually paid, not asking prices.',
    });
    expect(within(strip).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'HM Land Registry',
      'Energy certificates',
      'Transport for London',
      'ONS rents',
      'Refreshed monthly',
    ]);
    expect(strip).not.toHaveTextContent(/valuation|screening/i);
  });

  it('asks six common questions, all closed, in plain words', async () => {
    await renderReady();
    const faq = screen.getByRole('region', { name: 'Common questions' });
    const triggers = within(faq).getAllByRole('button');
    expect(triggers).toHaveLength(6);
    triggers.forEach((trigger) => expect(trigger).toHaveAttribute('aria-expanded', 'false'));
    expect(faq).not.toHaveTextContent(/\bPPD\b/);

    fireEvent.click(triggers[0]);
    expect(triggers[0]).toHaveAttribute('aria-expanded', 'true');
    const accuracy = screen.getByRole('region', { name: /how accurate are the prices/i });
    expect(within(accuracy).getByRole('link', { name: 'see how accurate they are' })).toHaveAttribute('href', '/avm#accuracy');
    fireEvent.click(triggers[0]);
    expect(triggers[0]).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(within(faq).getByRole('button', { name: /how often does the data update\?/i }));
    expect(
      screen.getByText('Every month. HM Land Registry publishes sales monthly, about two months behind, so the most recent weeks are always missing.'),
    ).toBeInTheDocument();
  });

  it('keeps backend vocabulary off the home page', async () => {
    const { container } = await renderReady();
    fillForm({ postcode: 'se15 4tq', floorArea: '92' });
    submit();
    await resultHeading(/SE15 4TQ in Peckham/);

    const text = (container.textContent ?? '').toLowerCase();
    ['catboost', 'ptal', 'msoa', 'lsoa', 'shap', 'rmse', 'mape', 'postgres', 'migration'].forEach((term) => {
      expect(text).not.toContain(term);
    });
  });

  it('groups the footer links, and carries one disclaimer linking to the terms', async () => {
    await renderReady();
    const footer = screen.getByRole('contentinfo');
    expect(within(footer).getAllByRole('heading').map((heading) => heading.textContent)).toEqual(['Tools', 'About']);
    expect(within(footer).getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')])).toEqual([
      ['Price a home', '/#top'],
      ['Areas', '/#areas'],
      ['Yield', '/#yield'],
      ['How estimates work', '/avm'],
      ['Terms', '/terms-and-conditions'],
      ['Privacy', '/privacy-policy'],
      ['terms', '/terms-and-conditions#legal-advice'],
    ]);
    expect(footer).toHaveTextContent('Prices and estimates are a guide, not a valuation, a survey or financial advice.');
    expect(footer.textContent).not.toMatch(/[¹²]/);
    expect(screen.queryByRole('heading', { name: 'Terms and conditions' })).not.toBeInTheDocument();
  });

  it('closes the mobile menu on Esc and returns focus to the Menu button (MAJ-3)', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }));
    const nav = screen.getAllByRole('navigation', { name: 'Sections' })[1];
    expect(within(nav).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Price a home',
      'Areas',
      'Yield',
      'How it works',
    ]);
    expect(within(nav).getAllByRole('link')[0]).toHaveFocus();
    expect(within(nav).getByRole('button', { name: 'Price a home' })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getAllByRole('navigation', { name: 'Sections' })).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Menu' })).toHaveFocus();
  });
});

describe('the standalone pages', () => {
  it('serves the terms and conditions at their own address, without loading figures', () => {
    render(<App path="/terms-and-conditions" />);
    expect(screen.getByRole('heading', { level: 1, name: 'Terms and conditions' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /we are not financial advisors/i })).toBeInTheDocument();
    expect(screen.getByText(/we do not guarantee the accuracy, completeness/i)).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Address' })).not.toBeInTheDocument();
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('legal-advice')).not.toBeNull();
    expect(mockGetAreas).not.toHaveBeenCalled();
  });

  it('serves the privacy policy at its own address', () => {
    render(<App path="/privacy-policy/" />);
    expect(screen.getByRole('heading', { level: 1, name: 'Privacy policy' })).toBeInTheDocument();
    expect(screen.getByText(/article 77/i)).toBeInTheDocument();
    expect(screen.getAllByText(/uk general data protection regulation/i).length).toBeGreaterThan(0);
    expect(screen.queryByRole('combobox', { name: 'Address' })).not.toBeInTheDocument();
  });

  it('explains how an estimate is made and the yield maths on the AVM page, with the live window', async () => {
    render(<App path="/avm" />);
    expect(screen.getByRole('heading', { level: 1, name: 'How our estimates work' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '2. How an estimate is made' })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/catboost/i);
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('accuracy')).not.toHaveTextContent(/\d+(,\d{3})* sales|About \d+%/);
    expect(screen.getByText(/a screening tool, not a valuation/i)).toBeInTheDocument();
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('yield-calculator')).toHaveTextContent('for the loan-to-value band your deposit falls in');

    expect(await screen.findByText(/1,923,529 Greater London sales recorded since 1995/)).toBeInTheDocument();
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('area-figures')).toHaveTextContent(
      'sales dated ' + WINDOW.fromYear + ' to ' + WINDOW.toYear + ' inclusive, up to the latest sale we hold (July 2026)',
    );
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('area-figures')).toHaveTextContent(
      'every sale in 2026 we hold, up to the latest (July 2026), so the current year is a part year, and compared with the same figure for 2025 and 2021',
    );
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('yield-calculator')).toHaveTextContent('(4.79% at 75% loan to value for July 2026');
  });

  it('writes the AVM page without figures rather than stale ones while they load', () => {
    mockGetAreas.mockReturnValue(new Promise(() => {}));
    render(<App path="/avm" />);
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('data')).toHaveTextContent('We hold every Greater London sale in the Land Registry record');
    // eslint-disable-next-line testing-library/no-node-access -- the id is a link target under test
    expect(document.getElementById('area-figures')).not.toHaveTextContent(/\d{4} to \d{4}/);
  });

  it('keeps the disclaimer in the footer without note numbers, and the header button goes to the price search', async () => {
    render(<App path="/avm" />);
    await screen.findByText(/1,923,529 Greater London sales/);
    const footer = screen.getByRole('contentinfo');
    expect(within(footer).getByText(/not a valuation/i)).toBeInTheDocument();
    expect(footer.textContent).not.toMatch(/[¹²]/);
    expect(within(screen.getAllByRole('banner')[0]).getByRole('button', { name: 'Price a home' })).toBeInTheDocument();
  });

  it('opens another page in place, without reloading, and comes back to the figures already loaded', async () => {
    await renderReady();
    expect(mockGetAreas).toHaveBeenCalledTimes(1);
    const footer = screen.getByRole('contentinfo');
    const terms = within(footer).getByRole('link', { name: 'Terms' });

    expect(clickLeavesItToBrowser(terms)).toBe(false);
    expect(screen.getByRole('heading', { level: 1, name: 'Terms and conditions' })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/terms-and-conditions');
    expect(document.title).toBe('Terms and conditions — HPLDN');
    expect(screen.getByRole('main')).toHaveFocus();

    act(() => {
      window.history.back();
    });
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(await screen.findByRole('heading', { level: 1, name: 'What’s a London home worth?' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Area' })).toHaveValue('SW11 · Battersea');
    expect(mockGetAreas).toHaveBeenCalledTimes(1);
  });

  it('follows a link to a section of another page, and leaves new-tab clicks to the browser', async () => {
    render(<App path="/avm" />);
    await screen.findByText(/1,923,529 Greater London sales/);
    const disclaimer = within(screen.getByRole('contentinfo')).getByRole('link', { name: 'terms' });

    expect(clickLeavesItToBrowser(disclaimer, { ctrlKey: true })).toBe(true);
    expect(screen.getByRole('heading', { level: 1, name: 'How our estimates work' })).toBeInTheDocument();

    expect(clickLeavesItToBrowser(disclaimer)).toBe(false);
    expect(window.location.pathname + window.location.hash).toBe('/terms-and-conditions#legal-advice');
    expect(screen.getByRole('heading', { level: 1, name: 'Terms and conditions' })).toBeInTheDocument();
  });

  it('leaves a link to a section of the page already showing to the browser', async () => {
    await renderReady();
    const areas = within(screen.getByRole('contentinfo')).getByRole('link', { name: 'Areas' });
    expect(clickLeavesItToBrowser(areas)).toBe(true);
    expect(window.location.pathname).toBe('/');
  });

  it('takes the header’s "Price a home" from another page to the address search, focused', async () => {
    render(<App path="/terms-and-conditions" />);
    fireEvent.click(within(screen.getAllByRole('banner')[0]).getByRole('button', { name: 'Price a home' }));
    expect(await screen.findByRole('combobox', { name: 'Address' })).toHaveFocus();
    expect(window.location.pathname + window.location.hash).toBe('/#top');
  });

  it('falls back to the home page for an unknown address', async () => {
    await renderReady('/no-such-page');
    expect(screen.getByRole('heading', { level: 1, name: 'What’s a London home worth?' })).toBeInTheDocument();
  });
});
