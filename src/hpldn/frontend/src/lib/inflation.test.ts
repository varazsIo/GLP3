import type { InflationSeries } from '../data/types';
import { cpihIndexAt, inflate, isProjectedMonth } from './inflation';

// 1 % a month: 12.68250301 % a year.
const SERIES: InflationSeries = {
  source: 'ONS CPIH',
  latestMonth: '2026-08-01',
  latestAnnualRate: (Math.pow(1.01, 12) - 1) * 100,
  months: [
    { month: '2017-08-01', index: 52 },
    { month: '2026-06-01', index: 102 },
    { month: '2026-07-01', index: 103 },
    { month: '2026-08-01', index: 104 },
  ],
};

describe('cpihIndexAt', () => {
  it('reads a published month whatever the day', () => {
    expect(cpihIndexAt(SERIES, '2026-06-18')).toBe(102);
    expect(cpihIndexAt(SERIES, '2026-08-01')).toBe(104);
  });

  it('carries the latest month forward at its annual rate', () => {
    expect(cpihIndexAt(SERIES, '2026-09-15')).toBeCloseTo(105.04, 6);
    expect(cpihIndexAt(SERIES, '2027-08-01')).toBeCloseTo(104 * Math.pow(1.01, 12), 6);
    expect(isProjectedMonth(SERIES, '2026-09-15')).toBe(true);
    expect(isProjectedMonth(SERIES, '2026-08-31')).toBe(false);
  });

  it('has no figure before the series or for a month it lacks', () => {
    expect(cpihIndexAt(SERIES, '1988-12-01')).toBeNull();
    expect(cpihIndexAt(SERIES, '2020-01-01')).toBeNull();
    expect(cpihIndexAt(SERIES, 'not a date')).toBeNull();
    expect(cpihIndexAt({ ...SERIES, months: [] }, '2026-08-01')).toBeNull();
  });
});

describe('inflate', () => {
  it('moves pounds by the ratio of the two months', () => {
    expect(inflate(SERIES, 523000, '2017-08-10', '2026-08-01')).toBe(1046000);
    expect(inflate(SERIES, 260000, '2026-06-18', '2026-09-15')).toBeCloseTo((260000 * 105.04) / 102, 6);
  });

  it('is null when a month has no index', () => {
    expect(inflate(SERIES, 100, '2020-01-01', '2026-08-01')).toBeNull();
  });
});
