import { BR, SW11 } from '../test/fixtures';
import { bedroomsFromRooms, describeHomeRent, homeMonthlyRent } from './assumptions';

describe('a home’s monthly rent', () => {
  it('takes the borough’s rent for the bedrooms its habitable rooms imply', () => {
    // One habitable room is the living room: three rooms are a 2-bed home.
    expect(homeMonthlyRent(SW11, 3)).toBe(2463);
    // A single room is still a 1-bed home, and seven rooms fall in 4+.
    expect(homeMonthlyRent(SW11, 1)).toBe(1938);
    expect(homeMonthlyRent(SW11, 7)).toBe(3818);
  });

  it('falls back to the borough’s mean when the rooms or the bucket are not known', () => {
    expect(homeMonthlyRent(SW11, null)).toBe(2632);
    expect(homeMonthlyRent({ ...SW11, bedroomRents: null }, 3)).toBe(2632);
    const suppressed = { ...SW11, bedroomRents: { ...SW11.bedroomRents!, twoBed: null } };
    expect(homeMonthlyRent(suppressed, 3)).toBe(2632);
  });

  it('is null without an area or a published rent', () => {
    expect(homeMonthlyRent(null, 3)).toBeNull();
    expect(homeMonthlyRent(undefined, 3)).toBeNull();
    const city = { ...SW11, borough: 'City of London', typicalMonthlyRent: null, rentMonth: null, bedroomRents: null };
    expect(homeMonthlyRent(city, 3)).toBeNull();
  });

  it('maps rooms to bedroom buckets', () => {
    expect([null, 0, 1, 2, 3, 4, 5, 9].map(bedroomsFromRooms)).toEqual([null, null, 1, 1, 2, 3, 4, 4]);
  });
});

describe('where a home’s rent comes from', () => {
  it('names the borough, month and bedrooms, or the average on the fallback', () => {
    expect(describeHomeRent(SW11, 2)).toBe(', at the Wandsworth August 2026 rent for a 2-bed home');
    expect(describeHomeRent(SW11, 6)).toBe(', at the Wandsworth August 2026 rent for a 4+-bed home');
    expect(describeHomeRent(SW11, null)).toBe(', at the Wandsworth August 2026 average rent');
    expect(describeHomeRent({ ...BR, bedroomRents: null }, 2)).toBe(', at the Bromley August 2026 average rent');
  });

  it('says nothing where no rent is published', () => {
    expect(describeHomeRent(null, 2)).toBe('');
    expect(describeHomeRent({ ...SW11, typicalMonthlyRent: null, bedroomRents: null }, 2)).toBe('');
  });
});
