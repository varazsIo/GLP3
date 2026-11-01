import { describeFiguresPeriod } from './AreaSnapshot';
import { BAND_LABELS, PRICE_BREAKS, RAMP, rampIndexFor } from './MapLegend';
import { MORTGAGE_RATES } from '../test/fixtures';
import { mortgageRateFor } from '../yield/assumptions';

describe('map price bands', () => {
  it('shades by the fixed bands, labelled with each band\'s upper edge', () => {
    expect(PRICE_BREAKS).toHaveLength(RAMP.length - 1);
    expect(BAND_LABELS).toHaveLength(RAMP.length);
    expect(rampIndexFor(449_999, PRICE_BREAKS)).toBe(0);
    expect(rampIndexFor(450_000, PRICE_BREAKS)).toBe(1);
    expect(rampIndexFor(599_999, PRICE_BREAKS)).toBe(1);
    expect(rampIndexFor(600_000, PRICE_BREAKS)).toBe(2);
    expect(rampIndexFor(799_999, PRICE_BREAKS)).toBe(2);
    expect(rampIndexFor(800_000, PRICE_BREAKS)).toBe(3);
    expect(rampIndexFor(999_999, PRICE_BREAKS)).toBe(3);
    expect(rampIndexFor(1_000_000, PRICE_BREAKS)).toBe(4);
    expect(rampIndexFor(1_199_999, PRICE_BREAKS)).toBe(4);
    expect(rampIndexFor(1_200_000, PRICE_BREAKS)).toBe(RAMP.length - 1);
    expect(BAND_LABELS).toEqual(['<£450K', '£600K', '£800K', '£1M', '£1.2M', '£1.2M+']);
  });
});

describe('the figures period', () => {
  it('names the part year the latest sale falls in', () => {
    expect(describeFiguresPeriod(2026, '2026-07-31')).toBe('January to July 2026');
    expect(describeFiguresPeriod(2026, '2026-01-20')).toBe('January 2026');
    expect(describeFiguresPeriod(2026, '2026-12-31')).toBe('2026');
    expect(describeFiguresPeriod(2025, '2026-02-01')).toBe('2025');
  });
});

describe('the mortgage rate for a deposit', () => {
  it('takes the lowest loan-to-value band that covers the borrowing', () => {
    expect(mortgageRateFor(MORTGAGE_RATES, 25)).toEqual({ ratePercent: 4.79, band: 75, rateDate: '2026-07-31' });
    expect(mortgageRateFor(MORTGAGE_RATES, 20)?.band).toBe(85);
    expect(mortgageRateFor(MORTGAGE_RATES, 10)?.ratePercent).toBe(5.07);
    expect(mortgageRateFor(MORTGAGE_RATES, 40)?.ratePercent).toBe(4.61);
    expect(mortgageRateFor(MORTGAGE_RATES, 90)?.band).toBe(60);
  });

  it('has no rate for a cash purchase, without rates, or for an unpublished band', () => {
    expect(mortgageRateFor(MORTGAGE_RATES, 100)).toBeNull();
    expect(mortgageRateFor(null, 25)).toBeNull();
    expect(mortgageRateFor({ ...MORTGAGE_RATES, ltv75: null }, 25)).toBeNull();
  });
});
