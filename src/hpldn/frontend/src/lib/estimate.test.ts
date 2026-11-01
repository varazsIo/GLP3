import { areaOf, districtOf, isCoveredPostcode, isPostcodeShaped, normalisePostcode } from './estimate';

describe('postcode handling', () => {
  it('pulls the area out of a full postcode', () => {
    expect(districtOf('SW11 2AB')).toBe('SW11');
    expect(districtOf('sw112ab')).toBe('SW11');
    expect(districtOf('e1 6an')).toBe('E1');
    expect(districtOf('EC1')).toBe('EC1');
    expect(districtOf('ec1v 9aa')).toBe('EC1');
    expect(districtOf('SW1V 4AB')).toBe('SW1');
    expect(districtOf('br3 1aa')).toBe('BR');
    expect(districtOf('TN16 3AA')).toBe('TN16');
  });

  it('groups outward codes into areas the way the server does', () => {
    expect(areaOf('SW1V')).toBe('SW1');
    expect(areaOf('EC2M')).toBe('EC2');
    expect(areaOf('W1G')).toBe('W1');
    expect(areaOf('E1W')).toBe('E1');
    expect(areaOf('N1C')).toBe('N1');
    expect(areaOf('SE1P')).toBe('SE1');
    expect(areaOf('BR3')).toBe('BR');
    expect(areaOf('CR0')).toBe('CR');
    expect(areaOf('SW11')).toBe('SW11');
    expect(areaOf('TN16')).toBe('TN16');
    expect(areaOf('E22')).toBe('E22');
  });

  it('shows a postcode back in its canonical form', () => {
    expect(normalisePostcode('sw112ab')).toBe('SW11 2AB');
    expect(normalisePostcode(' se15 4tq ')).toBe('SE15 4TQ');
    expect(normalisePostcode('n1')).toBe('N1');
  });

  it('accepts full postcodes and bare districts, rejects nonsense', () => {
    expect(isPostcodeShaped('SW11 2AB')).toBe(true);
    expect(isPostcodeShaped('SW11')).toBe(true);
    expect(isPostcodeShaped('banana')).toBe(false);
    expect(isPostcodeShaped('12345')).toBe(false);
  });

  it('checks coverage against the areas it is given', () => {
    const covered = new Set(['SE15', 'SW11', 'SW1', 'BR']);
    expect(isCoveredPostcode('SE15 4TQ', covered)).toBe(true);
    expect(isCoveredPostcode('sw11', covered)).toBe(true);
    expect(isCoveredPostcode('SW1V 4AB', covered)).toBe(true);
    expect(isCoveredPostcode('BR3 1AA', covered)).toBe(true);
    expect(isCoveredPostcode('TN16 3AA', covered)).toBe(false);
    expect(isCoveredPostcode('M1 1AE', covered)).toBe(false);
    expect(isCoveredPostcode('SE15 4TQ', new Set())).toBe(false);
  });
});
