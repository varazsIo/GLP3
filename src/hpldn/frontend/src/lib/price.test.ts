import {
  MAX_HORIZON_DAYS,
  addDays,
  addressKey,
  describeAddress,
  describeFullAddress,
  earliestSaleDate,
  formatUkDate,
  hasHouseNumber,
  isFullPostcode,
  latestSaleDate,
  maskUkDate,
  parseUkDate,
  readable,
  saleDateError,
} from './price';

const TODAY = new Date(2026, 8, 14);

describe('sale dates', () => {
  it('starts tomorrow and ends five years out', () => {
    expect(earliestSaleDate(TODAY)).toBe('2026-09-15');
    expect(latestSaleDate(TODAY)).toBe(addDays(TODAY, MAX_HORIZON_DAYS));
  });

  it('crosses month and year ends', () => {
    expect(addDays(new Date(2026, 11, 31), 1)).toBe('2027-01-01');
  });

  it('accepts tomorrow to five years out, and refuses today, the past, a blank and the far future in DD/MM/YYYY', () => {
    expect(saleDateError('2026-09-15', TODAY)).toBeUndefined();
    expect(saleDateError(latestSaleDate(TODAY), TODAY)).toBeUndefined();
    expect(saleDateError('2026-09-14', TODAY)).toBe('Choose a date from 15/09/2026.');
    expect(saleDateError('2020-01-01', TODAY)).toBe('Choose a date from 15/09/2026.');
    expect(saleDateError('', TODAY)).toBe('Enter the date as DD/MM/YYYY.');
    expect(saleDateError('2027-02-31', TODAY)).toBe('Enter the date as DD/MM/YYYY.');
    expect(saleDateError('', TODAY, '31/02/2027')).toBe('There is no 31/02/2027. Check the day and month.');
    expect(saleDateError('', TODAY, '31/02/20')).toBe('Enter the date as DD/MM/YYYY.');
    expect(saleDateError(addDays(TODAY, MAX_HORIZON_DAYS + 1), TODAY)).toBe(
      'Choose a date up to ' + formatUkDate(latestSaleDate(TODAY)) + '.',
    );
  });
});

describe('UK dates', () => {
  it('formats an ISO day as DD/MM/YYYY, and nothing for an invalid one', () => {
    expect(formatUkDate('2026-09-15')).toBe('15/09/2026');
    expect(formatUkDate('2028-02-29')).toBe('29/02/2028');
    expect(formatUkDate('2027-02-29')).toBe('');
    expect(formatUkDate('2026-13-01')).toBe('');
    expect(formatUkDate('15/09/2026')).toBe('');
    expect(formatUkDate('')).toBe('');
  });

  it('reads a typed UK date as an ISO day, with or without leading zeros', () => {
    expect(parseUkDate('15/10/2026')).toBe('2026-10-15');
    expect(parseUkDate(' 5/1/2027 ')).toBe('2027-01-05');
    expect(parseUkDate('29/02/2028')).toBe('2028-02-29');
  });

  it('reads nothing from a partial, impossible or US-ordered date', () => {
    expect(parseUkDate('15/10')).toBeNull();
    expect(parseUkDate('31/02/2027')).toBeNull();
    expect(parseUkDate('00/10/2026')).toBeNull();
    expect(parseUkDate('10/32/2026')).toBeNull();
    expect(parseUkDate('12/13/2026')).toBeNull();
    expect(parseUkDate('2026-10-15')).toBeNull();
    expect(parseUkDate('')).toBeNull();
  });

  it('round-trips every day across the sale window bounds', () => {
    [earliestSaleDate(TODAY), latestSaleDate(TODAY), '2026-12-31', '2027-01-01'].forEach((iso) => {
      expect(parseUkDate(formatUkDate(iso))).toBe(iso);
    });
  });

  it('masks typing to DD/MM/YYYY and turns a pasted ISO day round', () => {
    expect(maskUkDate('1')).toBe('1');
    expect(maskUkDate('15')).toBe('15');
    expect(maskUkDate('151')).toBe('15/1');
    expect(maskUkDate('15/10/')).toBe('15/10');
    expect(maskUkDate('151020261')).toBe('15/10/2026');
    expect(maskUkDate('15.10.2026')).toBe('15/10/2026');
    expect(maskUkDate('2026-10-15')).toBe('15/10/2026');
  });
});

describe('addresses', () => {
  it('finds a house number only where the address starts with one', () => {
    expect(hasHouseNumber('18 barclay road')).toBe(true);
    expect(hasHouseNumber('flat 3 high street')).toBe(true);
    expect(hasHouseNumber('Flat 3B, High St')).toBe(true);
    expect(hasHouseNumber('barclay road cr0')).toBe(false);
    expect(hasHouseNumber('barclay road cr0 1jn')).toBe(false);
    expect(hasHouseNumber('CR0')).toBe(false);
    expect(hasHouseNumber('sw11 1a')).toBe(false);
    expect(hasHouseNumber('flat')).toBe(false);
    expect(hasHouseNumber('')).toBe(false);
  });

  it('recognises only a full postcode', () => {
    expect(isFullPostcode('cr0 1jn')).toBe(true);
    expect(isFullPostcode('CR01JN')).toBe(true);
    expect(isFullPostcode('CR0')).toBe(false);
  });

  it('reads register capitals in title case and leaves mixed case alone', () => {
    expect(readable('BARCLAY ROAD')).toBe('Barclay Road');
    expect(readable('ST. CRISPIN HOUSE, 2')).toBe('St. Crispin House, 2');
    expect(readable('Flat 61')).toBe('Flat 61');
  });

  it('describes a home on one line, with or without a street', () => {
    const named = { postnumber: '18', poststreet: 'BARCLAY ROAD', posttown: 'CROYDON', postcode: 'CR0 1JN' };
    expect(describeAddress(named)).toBe('18 Barclay Road, Croydon');
    expect(describeAddress({ ...named, poststreet: null })).toBe('18, Croydon');
    expect(describeFullAddress(named)).toBe('18 Barclay Road, Croydon, CR0 1JN');
    expect(addressKey(named)).not.toBe(addressKey({ ...named, poststreet: null }));
  });
});

describe('flats', () => {
  it('leads with the unit and keys each flat of a building apart', () => {
    const flat = { unit: 'FLAT 6', postnumber: '17', poststreet: 'WOODVILLE ROAD', posttown: 'LONDON', postcode: 'NW11 9TP' };
    expect(describeAddress(flat)).toBe('Flat 6, 17 Woodville Road, London');
    expect(describeFullAddress(flat)).toBe('Flat 6, 17 Woodville Road, London, NW11 9TP');
    expect(describeAddress({ ...flat, unit: null })).toBe('17 Woodville Road, London');
    expect(addressKey(flat)).not.toBe(addressKey({ ...flat, unit: 'FLAT 7' }));
    expect(addressKey(flat)).not.toBe(addressKey({ ...flat, unit: null }));
  });
});

describe('street-less suggestions', () => {
  it('shows the street a register row without one was found under', () => {
    // 232A, SW11 3AA is on Battersea Bridge Road, which the register omits.
    const home = { postnumber: '232A', poststreet: null, posttown: 'LONDON', postcode: 'SW11 3AA' };
    expect(describeAddress({ ...home, streetHint: 'BATTERSEA BRIDGE ROAD' })).toBe('232a Battersea Bridge Road, London');
    expect(describeAddress(home)).toBe('232a, London');
    expect(describeAddress({ ...home, poststreet: 'ALBERT ROAD', streetHint: 'BATTERSEA BRIDGE ROAD' })).toBe(
      '232a Albert Road, London',
    );
  });
});
