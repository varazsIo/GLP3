/**
 * Shared frontend types and the read API's wire format.
 *
 * The market and pricing routes (`backend/api/routers/market.py`,
 * `backend/api/routers/pricing.py`) answer in exactly these
 * shapes, and a backend test diffs the two. Field docs use landlord-facing
 * language: nothing the home page renders names a model, table or column.
 */

/** The kinds of home the form offers. */
export type PropertyType =
  | 'flat'
  | 'maisonette'
  | 'terraced'
  | 'semi-detached'
  | 'detached';

/** How a property type reads in a sentence, e.g. "Flat, 72 m²". */
export const PROPERTY_TYPE_NAMES: Record<PropertyType, string> = {
  flat: 'Flat',
  maisonette: 'Maisonette',
  terraced: 'Terraced house',
  'semi-detached': 'Semi-detached house',
  detached: 'Detached house',
};

/**
 * The form's property type options, in order, so the first is the default.
 *
 * Labels are sentence case, as they read in a sentence.
 */
export const PROPERTY_TYPES: ReadonlyArray<{ value: PropertyType; label: string }> = (
  ['flat', 'maisonette', 'terraced', 'semi-detached', 'detached'] as PropertyType[]
).map((value) => ({ value, label: PROPERTY_TYPE_NAMES[value] }));

/** What the landlord tells us in the hero form. */
export interface EstimateRequest {
  /**
   * Outward postcode district, upper case, e.g. "SW11". Derived client-side
   * for display; the server works it out again from the postcode.
   */
  district: string;
  /** Exactly what they typed, kept for display. */
  postcode: string;
  propertyType: PropertyType;
  bedrooms: number;
  floorAreaSqm: number;
  /** The day the value is asked for, ISO YYYY-MM-DD; omitted means today. */
  saleDate?: string | null;
}

/** Whether a reason pushes the value up, is neutral, or holds it back. */
export type DriverTone = 'positive' | 'neutral' | 'caution';

/** One plain-English reason behind the number. */
export interface Driver {
  id: string;
  label: string;
  detail: string;
  tone: DriverTone;
}

/**
 * How a nearby sale relates to the home asked about: the same type, or the
 * same broad kind (flat or maisonette, or any house) used because too few of
 * that type sold nearby.
 */
export type MatchType = 'same' | 'broader';

/** One nearby sale. */
export interface Comparable {
  id: string;
  /** Street or building name, never a house number. */
  street: string;
  district: string;
  propertyType: PropertyType;
  floorAreaSqm: number;
  price: number;
  /** ISO date, YYYY-MM-DD. */
  soldOn: string;
  distanceKm: number;
  matchType: MatchType;
  /**
   * The price paid, scaled to the requested floor area and moved to the day
   * being valued by the UK HPI. This is the figure the estimate is built from,
   * so the list and the headline reconcile.
   */
  adjustedPrice: number | null;
}

/** How much evidence stands behind an estimate, which sets the range width. */
export type Confidence = 'high' | 'medium' | 'fair';

/** Which estimator produced a figure. */
export type EstimateBasis = 'model' | 'area';

/** What the page shows after a submission. */
export interface EstimateResult {
  request: EstimateRequest;
  /** The headline figure. */
  value: number;
  /** The likely range around it: lower and upper bounds. */
  low: number;
  high: number;
  confidence: Confidence;
  /** Percentage the range spans either side of the value. */
  spreadPercent: number;
  drivers: Driver[];
  comparables: Comparable[];
  /**
   * The borough's mean monthly rent for homes with the requested bedrooms (4+
   * for four or more), else its overall mean; null where no rent is published.
   */
  suggestedMonthlyRent: number | null;
  /** The data the figures were computed from. */
  window: DataWindow;
  /** 'model' when the trained model priced it, 'area' when the area formula did. */
  basis: EstimateBasis;
  /** How much the price index moved the model price from `projectedFrom` to the sale date; null when none. */
  projectionUplift: number | null;
  /** The day the model priced the home as of (after the latest sale held), ISO date; null when not moved. */
  projectedFrom: string | null;
  /** Whose price index moved it: a postcode district ("SW11"), a borough, or "London"; null when not moved. */
  indexArea: string | null;
}

/** One home in a postcode, as the property register writes its address. */
export interface AddressOption {
  /** House number or name, e.g. "18" or "Flat 61". */
  postnumber: string;
  /** Null where the register holds no street for the home. */
  poststreet: string | null;
  posttown: string;
  /** Full postcode in canonical form, e.g. "CR0 1JN". */
  postcode: string;
  /**
   * The flat within the building, e.g. "FLAT 6" (the register's SAON); null or
   * absent for a house or where no record names one. Sent back to `/price` and
   * `/nearby` so they price that flat rather than the whole building.
   */
  unit?: string | null;
}

/** A home suggested for a partial address (`GET /addresses/search`). */
export interface AddressSuggestion extends AddressOption {
  /**
   * The street the home is searched under when the register holds none for it
   * (its postcode's only street); for the label only, never sent to `/price`.
   */
  streetHint?: string | null;
}

/** A specific home and the day it would sell. */
export interface PriceRequest {
  postnumber: string;
  poststreet: string | null;
  posttown: string;
  postcode: string;
  /** The flat, as the address option named it; omitted for a whole house. */
  unit?: string | null;
  /** ISO date, YYYY-MM-DD, after today. */
  saleDate: string;
}

/** The home's latest recorded sale. */
export interface LastSale {
  price: number;
  /** ISO date, YYYY-MM-DD. */
  soldOn: string;
}

/** What the page shows for a specific home on a chosen day. */
export interface PriceResult {
  /** The address as the register holds it. */
  address: AddressOption;
  /** ISO date, YYYY-MM-DD. */
  saleDate: string;
  /** The price, to the nearest £1,000. */
  value: number;
  /** The middle of recent pricing errors around the value; null when not measured. */
  low: number | null;
  high: number | null;
  propertyType: PropertyType;
  /** Square metres; estimated when `epcSource` is 'estimated', null when it could not be. */
  floorAreaSqm: number | null;
  /** The energy rating on the certificate in force; null when not known or estimated. */
  energyRating: string | null;
  /** Habitable rooms on the same certificate as the energy rating; null when not known. */
  habitableRooms: number | null;
  lastSale: LastSale | null;
  /** The latest sale the evidence behind the price includes, ISO date. */
  evidenceTo: string;
  /** Share of recent sales priced within 10% of the price paid, 0 to 1; null when not measured. */
  withinTenPercent: number | null;
  /**
   * How much the price index moved the price from `projectedFrom` -- the day
   * the model priced the home as of, after the latest sale held -- to the sale
   * date. Both are null when the index did not move it.
   */
  projectionUplift: number | null;
  projectedFrom: string | null;
  /**
   * Whose price index moved it, for this type of home: the postcode district
   * ("SW11"), the borough ("Wandsworth") where the district has too few repeat
   * sales, or "London".
   */
  indexArea: string | null;
  /**
   * 'certificate' when size and rooms come from the home's energy certificate;
   * 'estimated' when it has none and they were estimated from comparable homes nearby.
   */
  epcSource: EpcSource;
  /** What an estimate came from, e.g. "12 terraced houses on this street"; null otherwise. */
  estimatedFrom: string | null;
  /**
   * True when the home's own recent sale (`lastSale`, within two years and not
   * renovated since) carries most of the price rather than the model. An API
   * that predates the field leaves it out, which reads as false.
   */
  anchoredOnLastSale?: boolean;
  /**
   * For an estimated home (`epcSource` 'estimated'), the most common energy
   * rating of the homes nearby; null for a home with a certificate. An API
   * that predates the field leaves it out, which reads as not known.
   */
  estimatedEnergyRating?: string | null;
}

/**
 * One recent sale near a priced home (`POST /nearby`), with the figures
 * `production.comps_features` and `production.neighbourhood_features` hold for it.
 */
export interface NearbySale {
  address: AddressOption;
  propertyType: PropertyType;
  /** The price paid, in pounds. */
  price: number;
  /** ISO date, YYYY-MM-DD. */
  soldOn: string;
  /** Straight-line distance from the priced home, in metres. */
  distanceM: number;
  /** Median price of the nearest same-type sales in the 12 months before it; null when none. */
  compMedianPrice: number | null;
  /** Their median price per square metre; null when none had a floor area. */
  compMedianPricePerSqm: number | null;
  /** How many sales those medians cover. */
  compTxnCount: number | null;
  /** Median price of its type within 800 m in the 6 months before it; null when none. */
  localTypeMedian: number | null;
}

/** The sales nearest a priced home in the 12 months to `windowTo`, nearest first. */
export interface NearbySales {
  sales: NearbySale[];
  /** ISO dates, YYYY-MM-DD. */
  windowFrom: string;
  windowTo: string;
}

/** A school's phase as Ofsted's ratings are listed by (`POST /schools`). */
export type SchoolPhase = 'primary' | 'secondary' | 'all_through';

/**
 * What a school's rating rests on: the overall effectiveness grade (to August
 * 2024), the mean of the key judgements (September 2024 to October 2025), a
 * report card (from November 2025), or `carried` when an ungraded inspection
 * confirmed the rating before it.
 */
export type GradeBasis = 'overall' | 'key_judgements' | 'report_card' | 'carried';

/** A state-funded school near a priced home, with the Ofsted rating in force on `asOf`. */
export interface NearbySchool {
  urn: number;
  name: string;
  phase: SchoolPhase;
  /** Straight-line distance from the home, in metres; not a catchment. */
  distanceM: number;
  /** The rating in its era's words, e.g. "Good" or "Expected standard". */
  ratingLabel: string;
  /** Harmonised score, 1 (best) to 4. */
  ratingScore: number;
  gradeBasis: GradeBasis;
  /** ISO dates, YYYY-MM-DD: the inspection that set or confirmed the rating, and its report. */
  inspectedOn: string;
  publishedOn: string;
  /** The predecessor whose rating a converted school (e.g. an academy) carries; null otherwise. */
  inheritedFromUrn: number | null;
  /** The school's page on reports.ofsted.gov.uk. */
  reportUrl: string;
}

/** The nearest primary and secondary schools to a priced home, nearest first. */
export interface NearbySchools {
  primary: NearbySchool[];
  secondary: NearbySchool[];
  /** ISO date the ratings are in force on. */
  asOf: string;
  /** How far the search reaches, in metres. */
  radiusM: number;
  /** Attribution for the data (Ofsted, DfE; Open Government Licence). */
  source: string;
}

/** One month of the CPIH index, 2015 averaging 100. */
export interface InflationMonth {
  /** ISO date of the month's first day, YYYY-MM-01. */
  month: string;
  index: number;
}

/**
 * UK consumer price inflation (`GET /inflation`): ONS's CPIH 12-month rate
 * chained into a monthly index, oldest first. A figure moves from one month's
 * pounds to another's by the ratio of their indexes; past `latestMonth` the
 * index is carried forward at `latestAnnualRate` (percent a year).
 */
export interface InflationSeries {
  source: string;
  /** ISO date, YYYY-MM-01: the last month ONS has published. */
  latestMonth: string;
  latestAnnualRate: number;
  months: InflationMonth[];
}

/** Where a priced home's size and rooms came from. */
export type EpcSource = 'certificate' | 'estimated';

/**
 * The data behind the page's figures. Area figures describe `toYear`, which
 * runs only to `latestSale`; nearby sales draw on `fromYear` onwards.
 */
export interface DataWindow {
  /** First calendar year of the window. */
  fromYear: number;
  /** Last calendar year of the window. */
  toYear: number;
  /** The most recent sale held, ISO date. */
  latestSale: string;
  /** Every sale held, not just the window's. */
  totalSales: number;
  /** The year of the earliest sale held. */
  firstSaleYear: number;
}

/** GET /areas: every district the page covers, and the latest mortgage rates. */
export interface AreasResponse {
  window: DataWindow;
  areas: AreaStats[];
  /** Null when no rates are held. */
  mortgageRates: MortgageRates | null;
}

/**
 * The latest month's two-year fixed mortgage rates, in percent, by
 * loan-to-value band. A band is null when no rate was published for it.
 */
export interface MortgageRates {
  /** The month the rates describe, ISO date. */
  rateDate: string;
  ltv60: number | null;
  ltv75: number | null;
  ltv85: number | null;
  ltv90: number | null;
  ltv95: number | null;
}

/** A borough's mean monthly private rent by bedrooms, in pounds; null where ONS suppressed it. */
export interface BedroomRents {
  oneBed: number | null;
  twoBed: number | null;
  threeBed: number | null;
  /** Four or more bedrooms. */
  fourPlusBed: number | null;
}

/**
 * One postcode district as the page talks about it, for one calendar year:
 * `figuresYear`, the year of the latest sale, which may be a part year.
 *
 * A figure the data cannot support is null: show "Not enough sales" or "Not
 * known", never a number standing in for it.
 */
export interface AreaStats {
  code: string;
  name: string;
  borough: string;
  /** Mean price of the year's sales, in pounds. */
  averagePrice: number;
  /** The mean price over the year's mean floor area; null without floor areas. */
  averagePricePerSqm: number | null;
  /** Change in the mean price from the year before, in percent; null without sales that year. */
  movement1y: number | null;
  /** Change in the mean price from five years before, in percent; null without sales that year. */
  movement5y: number | null;
  /** 1 to 5, where 5 is best connected; null when not known. */
  transportScore: number | null;
  /** Null when the station distance is not known. */
  walkToStationMins: number | null;
  /** Gross rental yield against the mean price, in percent; null where no rent is published. */
  rentalYield: number | null;
  /** Mean monthly private rent in the area's borough, in pounds; null where none is published. */
  typicalMonthlyRent: number | null;
  /** The month that rent describes, ISO date; null with no rent. */
  rentMonth: string | null;
  /** The same month's mean rent by bedrooms; null where no rent is published. */
  bedroomRents: BedroomRents | null;
  /** Sales in `figuresYear`. */
  salesCount: number;
  /** The calendar year the figures describe. */
  figuresYear: number;
  /**
   * Where the district sits in London for deprivation (income, jobs,
   * education, health, crime, housing and environment together): the mean
   * London percentile of the year's sales, to three significant figures.
   * 32.5 means more deprived than 32.5% of London neighbourhoods. Null when
   * not known.
   */
  areaProfilePercent: number | null;
  /** The most common EPC rating among the area's sales. */
  epcBand: string;
  /** True when too few homes sold here in the year to lean on its figures. */
  lowData: boolean;
}

/** One postcode district shape on the map (data/londonGeo.ts, generated). */
export interface DistrictShape {
  /** The area code, e.g. "SW1" or "BR"; an uncovered leftover's letters, e.g. "TN". */
  code: string;
  /** The area's name, or the code again for an uncovered leftover. */
  label: string;
  /** SVG path data in MAP_VIEW_BOX units. */
  path: string;
  /** A point inside the shape's largest part, for its tooltip and for zooming to it. */
  labelX: number;
  labelY: number;
}

/** How the ranked area list orders areas. */
export type SortMode = 'cheapest' | 'growing' | 'connected' | 'yield';
