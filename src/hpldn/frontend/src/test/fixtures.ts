/**
 * Hand-written API responses for the tests.
 *
 * Every covered area (four with figures tests assert on), one data window,
 * one estimate, and the homes and price for one Croydon postcode.
 */
import type {
  AddressOption,
  MortgageRates,
  AreasResponse,
  AreaStats,
  DataWindow,
  EstimateRequest,
  EstimateResult,
  NearbySchools,
  PriceRequest,
  PriceResult,
} from '../data/types';

/** The data window every response carries. */
export const WINDOW: DataWindow = {
  fromYear: 2023,
  toYear: 2026,
  latestSale: '2026-07-31',
  totalSales: 1923529,
  firstSaleYear: 1995,
};

/** The default area, Battersea, with its figures from hpdbldn (2026 to July; rents for August 2026). */
export const SW11: AreaStats = {
  code: 'SW11',
  name: 'Battersea',
  borough: 'Wandsworth',
  averagePrice: 819883,
  averagePricePerSqm: 9344,
  movement1y: -16.6,
  movement5y: -28.9,
  transportScore: 3,
  walkToStationMins: 9,
  rentalYield: 3.9,
  typicalMonthlyRent: 2632,
  rentMonth: '2026-08-01',
  bedroomRents: { oneBed: 1938, twoBed: 2463, threeBed: 2810, fourPlusBed: 3818 },
  salesCount: 282,
  figuresYear: 2026,
  areaProfilePercent: 32.5,
  epcBand: 'C',
  lowData: false,
};

/** Peckham, the area the fixture estimate is for. */
export const SE15: AreaStats = {
  code: 'SE15',
  name: 'Peckham',
  borough: 'Southwark',
  averagePrice: 560000,
  averagePricePerSqm: 7450,
  movement1y: 2.4,
  movement5y: 6.0,
  transportScore: 4,
  walkToStationMins: 7,
  rentalYield: 4.4,
  typicalMonthlyRent: 2050,
  rentMonth: '2026-08-01',
  bedroomRents: { oneBed: 1550, twoBed: 1950, threeBed: 2250, fourPlusBed: 3000 },
  salesCount: 1801,
  figuresYear: 2026,
  areaProfilePercent: 71,
  epcBand: 'D',
  lowData: false,
};

/** Bromley, an outer London postcode area and the cheapest fixture. */
export const BR: AreaStats = {
  code: 'BR',
  name: 'Bromley',
  borough: 'Bromley',
  averagePrice: 395000,
  averagePricePerSqm: 4910,
  movement1y: 0.8,
  movement5y: 9.2,
  transportScore: 4,
  walkToStationMins: 8,
  rentalYield: 5.4,
  typicalMonthlyRent: 1775,
  rentMonth: '2026-08-01',
  bedroomRents: { oneBed: 1375, twoBed: 1725, threeBed: 2100, fourPlusBed: 3075 },
  salesCount: 2880,
  figuresYear: 2026,
  areaProfilePercent: 58,
  epcBand: 'D',
  lowData: false,
};

/** Kensington, which ranks outside the top 12 by yield. */
export const W8: AreaStats = {
  code: 'W8',
  name: 'Kensington',
  borough: 'Kensington and Chelsea',
  averagePrice: 1650000,
  averagePricePerSqm: 17100,
  movement1y: -1.2,
  movement5y: 2.0,
  transportScore: 5,
  walkToStationMins: 5,
  rentalYield: 2.6,
  typicalMonthlyRent: 3625,
  rentMonth: '2026-08-01',
  bedroomRents: { oneBed: 2600, twoBed: 3375, threeBed: 4000, fourPlusBed: 5550 },
  salesCount: 516,
  figuresYear: 2026,
  areaProfilePercent: 12,
  epcBand: 'D',
  lowData: false,
};

/** Every covered area code and name, in the server's order (`backend/api/market/districts.py`). */
const AREA_NAMES: ReadonlyArray<[string, string]> = [
  ['EC1', 'Barbican, Clerkenwell, Finsbury'],
  ['EC2', 'Moorgate, Liverpool Street'],
  ['EC3', 'Aldgate, Monument, Tower Hill'],
  ["EC4", "Fleet Street, St Paul's"],
  ["WC1", "Bloomsbury, Gray's Inn"],
  ['WC2', 'Covent Garden, Holborn, Strand'],
  ['E1', 'Mile End, Stepney, Whitechapel'],
  ['E2', 'Bethnal Green, Shoreditch'],
  ['E3', 'Bow, Bromley-by-Bow'],
  ['E4', 'Chingford, Highams Park'],
  ['E5', 'Clapton'],
  ['E6', 'East Ham, Beckton'],
  ['E7', 'Forest Gate, Upton Park'],
  ['E8', 'Hackney, Dalston'],
  ['E9', 'Hackney, Homerton'],
  ['E10', 'Leyton'],
  ['E11', 'Leytonstone'],
  ['E12', 'Manor Park'],
  ['E13', 'Plaistow'],
  ['E14', 'Isle of Dogs, Millwall, Poplar'],
  ['E15', 'Stratford, West Ham'],
  ['E16', 'Canning Town, North Woolwich'],
  ['E17', 'Walthamstow'],
  ['E18', 'South Woodford'],
  ['E20', 'Olympic Park, Stratford'],
  ['N1', 'Barnsbury, Canonbury, Islington'],
  ['N2', 'East Finchley'],
  ['N3', 'Finchley Central'],
  ['N4', 'Finsbury Park, Manor House'],
  ['N5', 'Highbury'],
  ['N6', 'Highgate'],
  ['N7', 'Holloway'],
  ['N8', 'Crouch End, Hornsey'],
  ['N9', 'Lower Edmonton'],
  ['N10', 'Muswell Hill'],
  ['N11', 'Friern Barnet, New Southgate'],
  ['N12', 'North Finchley, Woodside Park'],
  ['N13', 'Palmers Green'],
  ['N14', 'Southgate'],
  ['N15', 'Seven Sisters'],
  ['N16', 'Stamford Hill, Stoke Newington'],
  ['N17', 'Tottenham'],
  ['N18', 'Upper Edmonton'],
  ['N19', 'Archway, Tufnell Park'],
  ['N20', 'Totteridge, Whetstone'],
  ['N21', 'Winchmore Hill'],
  ['N22', 'Alexandra Palace, Wood Green'],
  ["NW1", "Camden Town, Regent's Park"],
  ['NW2', 'Cricklewood, Neasden'],
  ['NW3', 'Hampstead, Swiss Cottage'],
  ['NW4', 'Brent Cross, Hendon'],
  ['NW5', 'Kentish Town'],
  ['NW6', 'Kilburn, Queens Park, West Hampstead'],
  ['NW7', 'Mill Hill'],
  ["NW8", "St John's Wood"],
  ['NW9', 'Colindale, Kingsbury'],
  ['NW10', 'Harlesden, Kensal Green, Willesden'],
  ['NW11', 'Golders Green, Hampstead Garden Suburb'],
  ['SE1', 'Bermondsey, Borough, Southwark, Waterloo'],
  ['SE2', 'Abbey Wood'],
  ['SE3', 'Blackheath, Westcombe Park'],
  ['SE4', 'Brockley, Crofton Park, Honor Oak Park'],
  ['SE5', 'Camberwell'],
  ['SE6', 'Bellingham, Catford, Hither Green'],
  ['SE7', 'Charlton'],
  ['SE8', 'Deptford'],
  ['SE9', 'Eltham, Mottingham'],
  ['SE10', 'Greenwich'],
  ['SE11', 'Lambeth'],
  ['SE12', 'Grove Park, Lee'],
  ['SE13', 'Hither Green, Lewisham'],
  ['SE14', 'New Cross, New Cross Gate'],
  ['SE15', 'Nunhead, Peckham'],
  ['SE16', 'Rotherhithe, South Bermondsey, Surrey Docks'],
  ['SE17', 'Elephant & Castle, Walworth'],
  ['SE18', 'Plumstead, Woolwich'],
  ['SE19', 'Crystal Palace, Upper Norwood'],
  ['SE20', 'Anerley, Penge'],
  ['SE21', 'Dulwich'],
  ['SE22', 'East Dulwich'],
  ['SE23', 'Forest Hill'],
  ['SE24', 'Herne Hill'],
  ['SE25', 'South Norwood'],
  ['SE26', 'Sydenham'],
  ['SE27', 'Tulse Hill, West Norwood'],
  ['SE28', 'Thamesmead'],
  ['SW1', 'Belgravia, Pimlico, Westminster'],
  ['SW2', 'Brixton, Streatham Hill'],
  ['SW3', 'Brompton, Chelsea'],
  ['SW4', 'Clapham'],
  ["SW5", "Earl's Court"],
  ["SW6", "Fulham, Parson's Green"],
  ['SW7', 'South Kensington'],
  ['SW8', 'Nine Elms, South Lambeth'],
  ['SW9', 'Brixton, Stockwell'],
  ["SW10", "West Brompton, World's End"],
  ['SW11', 'Battersea, Clapham Junction'],
  ['SW12', 'Balham'],
  ['SW13', 'Barnes, Castelnau'],
  ['SW14', 'East Sheen, Mortlake'],
  ['SW15', 'Putney, Roehampton'],
  ['SW16', 'Norbury, Streatham'],
  ['SW17', 'Tooting'],
  ['SW18', 'Earlsfield, Wandsworth'],
  ['SW19', 'Merton, Wimbledon'],
  ['SW20', 'Raynes Park, South Wimbledon'],
  ['W1', 'Marylebone, Mayfair, Soho'],
  ['W2', 'Bayswater, Paddington'],
  ['W3', 'Acton'],
  ['W4', 'Chiswick'],
  ['W5', 'Ealing'],
  ['W6', 'Hammersmith'],
  ['W7', 'Hanwell'],
  ['W8', 'Kensington'],
  ['W9', 'Maida Vale, Warwick Avenue'],
  ['W10', 'Ladbroke Grove, North Kensington'],
  ['W11', 'Holland Park, Notting Hill'],
  ["W12", "Shepherd's Bush"],
  ['W13', 'West Ealing'],
  ['W14', 'West Kensington'],
  ['BR', 'Bromley'],
  ['CR', 'Croydon'],
  ['DA', 'Dartford'],
  ['EN', 'Enfield'],
  ['HA', 'Harrow'],
  ['IG', 'Ilford'],
  ['KT', 'Kingston'],
  ['RM', 'Romford'],
  ['SM', 'Sutton'],
  ['TW', 'Twickenham'],
  ['UB', 'Uxbridge'],
  ['WD', 'Watford'],
];

/** The areas with hand-written figures, by code. */
const DETAILED: Record<string, AreaStats> = { SW11, SE15, BR, W8 };

/**
 * All 132 areas: the four above as written, the rest with made-up figures that
 * stay out of their way.
 *
 * The rest are dearer than BR and yield less than SW11 and SE15, so those two
 * always make the top 12 by yield.
 */
export const AREAS: AreaStats[] = AREA_NAMES.map(([code, name], index) =>
  DETAILED[code] ?? {
    code,
    name,
    borough: 'Camden',
    averagePrice: 450000 + index * 5000,
    averagePricePerSqm: 7000 + index * 10,
    movement1y: 1.0,
    movement5y: 5.0,
    transportScore: 3,
    walkToStationMins: 10,
    rentalYield: 2.0 + (index % 20) / 10,
    typicalMonthlyRent: 2000,
    rentMonth: '2026-08-01',
    bedroomRents: { oneBed: 1600, twoBed: 1950, threeBed: 2300, fourPlusBed: 3100 },
    salesCount: 900,
    figuresYear: 2026,
    areaProfilePercent: 50,
    epcBand: 'C',
    lowData: false,
  },
);

/** The `GET /areas` response. */
/** The latest mortgage rates, as production.mortgage_rates holds them for July 2026. */
export const MORTGAGE_RATES: MortgageRates = {
  rateDate: '2026-07-31',
  ltv60: 4.61,
  ltv75: 4.79,
  ltv85: 4.89,
  ltv90: 5.07,
  ltv95: 5.49,
};

export const AREAS_RESPONSE: AreasResponse = { window: WINDOW, areas: AREAS, mortgageRates: MORTGAGE_RATES };

/** The fixture estimate, for SE15 4TQ. */
export const ESTIMATE: EstimateResult = {
  request: {
    district: 'SE15',
    postcode: 'SE15 4TQ',
    propertyType: 'terraced',
    bedrooms: 3,
    floorAreaSqm: 92,
  },
  value: 698000,
  low: 649000,
  high: 747000,
  confidence: 'high',
  spreadPercent: 7,
  drivers: [
    {
      id: 'comparables',
      label: 'What similar homes nearby sold for',
      detail: 'Around £830k on nearby streets',
      tone: 'neutral',
    },
    { id: 'momentum', label: 'Prices here are rising', detail: 'Up 2.4% over the last year', tone: 'positive' },
    { id: 'transport', label: 'Very good transport', detail: '7 min walk to the Tube', tone: 'positive' },
    {
      id: 'size',
      label: 'A typical size for the street',
      detail: 'In line with what usually sells in Peckham',
      tone: 'neutral',
    },
  ],
  comparables: [
    {
      id: '7844913',
      street: 'Soames Street',
      district: 'SE15',
      propertyType: 'terraced',
      floorAreaSqm: 92,
      price: 932860,
      soldOn: '2026-06-24',
      distanceKm: 1.2,
      matchType: 'same',
      adjustedPrice: 932860,
    },
    {
      id: '7838893',
      street: 'Peckham Hill Street',
      district: 'SE15',
      propertyType: 'terraced',
      floorAreaSqm: 92,
      price: 766500,
      soldOn: '2026-05-26',
      distanceKm: 1.2,
      matchType: 'same',
      adjustedPrice: 766500,
    },
    {
      id: '7702782',
      street: 'Kimberley Avenue',
      district: 'SE15',
      propertyType: 'flat',
      floorAreaSqm: 88,
      price: 830000,
      soldOn: '2025-03-31',
      distanceKm: 0.6,
      matchType: 'same',
      adjustedPrice: 868000,
    },
  ],
  suggestedMonthlyRent: 2550,
  window: WINDOW,
  basis: 'model',
  projectionUplift: null,
  projectedFrom: null,
  indexArea: null,
};

/**
 * Returns the fixture estimate for whatever request was sent.
 */
export function estimateFor(request: EstimateRequest): EstimateResult {
  return { ...ESTIMATE, request: { ...request } };
}

/** The homes the fixture postcode lists, as the register writes them. */
export const ADDRESSES: AddressOption[] = [
  { postnumber: '16', poststreet: null, posttown: 'CROYDON', postcode: 'CR0 1JN' },
  { postnumber: '18', poststreet: 'BARCLAY ROAD', posttown: 'CROYDON', postcode: 'CR0 1JN' },
  { postnumber: 'Flat 2', poststreet: 'Barclay Court', posttown: 'CROYDON', postcode: 'CR0 1JN' },
];

/** The price of 18 Barclay Road. */
export const PRICE: PriceResult = {
  address: ADDRESSES[1],
  saleDate: '2026-09-15',
  value: 244000,
  low: 189000,
  high: 277000,
  propertyType: 'flat',
  floorAreaSqm: 45,
  energyRating: 'C',
  habitableRooms: 2,
  lastSale: { price: 260000, soldOn: '2026-06-18' },
  evidenceTo: '2026-07-31',
  withinTenPercent: 0.4867,
  projectionUplift: null,
  projectedFrom: null,
  indexArea: null,
  epcSource: 'certificate',
  estimatedFrom: null,
  anchoredOnLastSale: false,
};

/** A home with no energy certificate, priced from comparable homes on its street. */
export const ESTIMATED_PRICE: PriceResult = {
  address: { postnumber: '17', poststreet: 'LICHFIELD ROAD', posttown: 'RICHMOND', postcode: 'TW9 3JR' },
  saleDate: '2026-09-30',
  value: 4667000,
  low: 3500000,
  high: 6100000,
  propertyType: 'detached',
  floorAreaSqm: 331,
  energyRating: null,
  habitableRooms: 9,
  lastSale: { price: 5188000, soldOn: '2026-07-09' },
  evidenceTo: '2026-07-31',
  withinTenPercent: 0.3,
  projectionUplift: 0.021,
  projectedFrom: '2026-08-01',
  indexArea: 'TW9',
  epcSource: 'estimated',
  estimatedFrom: '6 detached houses on this street',
  anchoredOnLastSale: false,
};

/** A home that sold five months ago, priced mainly from that sale. */
export const ANCHORED_PRICE: PriceResult = {
  address: { postnumber: '11D', poststreet: 'QUEENSDALE ROAD', posttown: 'LONDON', postcode: 'W11 4QF' },
  saleDate: '2026-09-27',
  value: 2468000,
  low: 2250000,
  high: 2700000,
  propertyType: 'terraced',
  floorAreaSqm: 107,
  energyRating: 'D',
  habitableRooms: 5,
  lastSale: { price: 2500000, soldOn: '2026-04-27' },
  evidenceTo: '2026-07-31',
  withinTenPercent: 0.62,
  projectionUplift: 0.004,
  projectedFrom: '2026-08-01',
  indexArea: 'W11',
  epcSource: 'certificate',
  estimatedFrom: null,
  anchoredOnLastSale: true,
};

/**
 * Returns the fixture price for whatever home and day was asked for.
 */
export function priceFor(request: PriceRequest): PriceResult {
  const { saleDate, ...address } = request;
  return { ...PRICE, address, saleDate };
}

/** The schools nearest 18 Barclay Road; an all-through school is listed as both. */
export const NEARBY_SCHOOLS: NearbySchools = {
  primary: [
    {
      urn: 101234,
      name: 'St Mary’s CE Primary School',
      phase: 'primary',
      distanceM: 420,
      ratingLabel: 'Good',
      ratingScore: 2,
      gradeBasis: 'overall',
      inspectedOn: '2023-03-07',
      publishedOn: '2023-04-12',
      inheritedFromUrn: null,
      reportUrl: 'https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/101234',
    },
    {
      urn: 140001,
      name: 'Barclay Academy',
      phase: 'all_through',
      distanceM: 1260,
      ratingLabel: 'Expected standard',
      ratingScore: 2,
      gradeBasis: 'report_card',
      inspectedOn: '2026-01-20',
      publishedOn: '2026-03-02',
      inheritedFromUrn: null,
      reportUrl: 'https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/140001',
    },
  ],
  secondary: [
    {
      urn: 140001,
      name: 'Barclay Academy',
      phase: 'all_through',
      distanceM: 1260,
      ratingLabel: 'Expected standard',
      ratingScore: 2,
      gradeBasis: 'report_card',
      inspectedOn: '2026-01-20',
      publishedOn: '2026-03-02',
      inheritedFromUrn: null,
      reportUrl: 'https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/140001',
    },
    {
      urn: 145000,
      name: 'Croydon High',
      phase: 'secondary',
      distanceM: 1830,
      ratingLabel: 'Outstanding',
      ratingScore: 1,
      gradeBasis: 'overall',
      inspectedOn: '2014-05-14',
      publishedOn: '2014-06-20',
      inheritedFromUrn: 101999,
      reportUrl: 'https://reports.ofsted.gov.uk/oxedu_providers/full/(urn)/145000',
    },
  ],
  asOf: '2026-10-02',
  radiusM: 3000,
  source: 'School inspections from Ofsted; school locations from DfE Get Information about Schools.',
};
