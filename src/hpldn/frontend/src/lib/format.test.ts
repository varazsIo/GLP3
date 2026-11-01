import {
  formatArea,
  formatBedrooms,
  formatCount,
  formatDistance,
  formatSignificantPercent,
  roundSignificant,
  formatGBP,
  formatGBPCompact,
  formatLongDate,
  formatPercent,
  formatShortMonth,
  formatSoldDate,
  groupDigits,
  stripNonDigits,
} from './format';

describe('formatGBP', () => {
  it('groups thousands and drops pence', () => {
    expect(formatGBP(520000)).toBe('£520,000');
    expect(formatGBP(1340000)).toBe('£1,340,000');
    expect(formatGBP(999)).toBe('£999');
    expect(formatGBP(1234.6)).toBe('£1,235');
  });

  it('keeps the sign outside the symbol', () => {
    expect(formatGBP(-480)).toBe('-£480');
  });

  it('is stable for the figures the page actually shows', () => {
    expect(formatGBP(0)).toBe('£0');
    expect(formatGBP(85)).toBe('£85');
  });
});

describe('formatGBPCompact', () => {
  it('shortens thousands and millions', () => {
    expect(formatGBPCompact(520000)).toBe('£520k');
    expect(formatGBPCompact(1340000)).toBe('£1.34m');
    expect(formatGBPCompact(450000)).toBe('£450k');
  });

  it('falls back to the full form under a thousand', () => {
    expect(formatGBPCompact(825)).toBe('£825');
  });
});

describe('formatPercent', () => {
  it('defaults to one decimal and no sign', () => {
    expect(formatPercent(4)).toBe('4.0%');
    expect(formatPercent(-1.14)).toBe('-1.1%');
    expect(formatPercent(1200000000)).toBe('1,200,000,000.0%');
    expect(formatPercent(-12345.67, { decimals: 0 })).toBe('-12,346%');
  });

  it('adds an explicit sign when asked', () => {
    expect(formatPercent(3.8, { signed: true })).toBe('+3.8%');
    expect(formatPercent(-0.8, { signed: true })).toBe('-0.8%');
    expect(formatPercent(0, { signed: true })).toBe('0.0%');
  });

  it('honours a decimal count', () => {
    expect(formatPercent(7, { decimals: 0 })).toBe('7%');
  });
});

describe('the smaller formatters', () => {
  it('formats area, counts and bedrooms', () => {
    expect(formatArea(72)).toBe('72 m²');
    expect(formatArea(1200)).toBe('1,200 m²');
    expect(formatCount(1240)).toBe('1,240');
    expect(formatBedrooms(1)).toBe('1 bedroom');
    expect(formatBedrooms(3)).toBe('3 bedrooms');
  });

  it('converts distance to miles', () => {
    expect(formatDistance(1)).toBe('0.6 miles');
    expect(formatDistance(0.05)).toBe('under 0.1 miles');
  });

  it('reads sale dates as month and year', () => {
    expect(formatSoldDate('2025-03-14')).toBe('March 2025');
    expect(formatSoldDate('not-a-date')).toBe('not-a-date');
    expect(formatShortMonth('2023-03-07')).toBe('Mar 2023');
    expect(formatShortMonth('2026-09-01')).toBe('Sep 2026');
    expect(formatShortMonth('not-a-date')).toBe('not-a-date');
  });
});

describe('formatLongDate', () => {
  it('writes the day, month and year', () => {
    expect(formatLongDate('2026-09-15')).toBe('15 September 2026');
    expect(formatLongDate('2027-01-01')).toBe('1 January 2027');
  });

  it('returns anything unparseable unchanged', () => {
    expect(formatLongDate('soon')).toBe('soon');
    expect(formatLongDate('2026-13-01')).toBe('2026-13-01');
  });
});

describe('typed whole numbers', () => {
  it('keeps only the digits of what was typed', () => {
    expect(stripNonDigits('£425,000')).toBe('425000');
    expect(stripNonDigits('1 750 pcm')).toBe('1750');
    expect(stripNonDigits('abc')).toBe('');
  });

  it('groups digits as they are typed, dropping leading zeros', () => {
    expect(groupDigits('4250')).toBe('4,250');
    expect(groupDigits('£1,234,567')).toBe('1,234,567');
    expect(groupDigits('007')).toBe('7');
    expect(groupDigits('0')).toBe('0');
    expect(groupDigits('')).toBe('');
  });
});

describe('significant figures', () => {
  it('rounds to three significant figures, halves up', () => {
    expect(roundSignificant(32.4958846)).toBe(32.5);
    expect(roundSignificant(19.7878538)).toBe(19.8);
    expect(roundSignificant(9.0987)).toBe(9.1);
    expect(roundSignificant(67.45)).toBe(67.5);
    expect(roundSignificant(0)).toBe(0);
  });

  it('formats a percentage with its trailing zeros', () => {
    expect(formatSignificantPercent(32.5)).toBe('32.5%');
    expect(formatSignificantPercent(100 - 32.5)).toBe('67.5%');
    expect(formatSignificantPercent(100 - 19.8)).toBe('80.2%');
    expect(formatSignificantPercent(9.1)).toBe('9.10%');
    expect(formatSignificantPercent(100)).toBe('100%');
    expect(formatSignificantPercent(0)).toBe('0%');
  });
});
