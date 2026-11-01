import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AreaSnapshot from '../area/AreaSnapshot';
import FaqAccordion from '../content/FaqAccordion';
import HowItWorks from '../content/HowItWorks';
import TrustBand from '../content/TrustBand';
import { DEFAULT_DISTRICT, useAreas } from '../data/AreasProvider';
import type {
  EstimateRequest,
  EstimateResult as Estimate,
  PriceRequest,
  PriceResult,
} from '../data/types';
import EstimateResult from '../estimate/EstimateResult';
import Hero from '../hero/Hero';
import PageShell from '../layout/PageShell';
import Section from '../layout/Section';
import {
  ApiError,
  getAddresses,
  getInflation,
  getNearbySales,
  getNearbySchools,
  requestEstimate,
  requestPrice,
  searchAddresses,
} from '../lib/api';
import { districtOf } from '../lib/estimate';
import { addressKey, describeFullAddress } from '../lib/price';
import { ADDRESS_INPUT_ID } from '../price/PriceForm';
import type { HomeRent } from '../price/DetailedAnalysis';
import PriceResultCard from '../price/PriceResultCard';
import DataNotice from '../ui/DataNotice';
import useReducedMotion from '../ui/useReducedMotion';
import YieldCalculator from '../yield/YieldCalculator';
import type { YieldFigures } from '../yield/YieldCalculator';
import { bedroomsFromRooms, describeHomeRent, homeMonthlyRent, rentBasis } from '../yield/assumptions';
import styles from './HomePage.module.css';

/** Loading text for sections waiting on the area figures. */
const AREAS_LOADING = 'Loading the latest area figures…';

const FALLBACK_ERROR = 'Something went wrong on our side. Try again in a moment.';

/** Which tool the latest request came from, with its answer once there is one. */
type Latest =
  | { kind: 'price'; request: PriceRequest; result: PriceResult | null }
  | { kind: 'estimate'; request: EstimateRequest; result: Estimate | null };

type ResultStatus = 'idle' | 'loading' | 'ready' | 'error';

/**
 * Props for `HomePage`.
 */
export interface HomePageProps {
  /** The day sale dates are measured from; tests pass one in. */
  today?: Date;
}

/**
 * The home page: the whole tool on one scrolling page.
 *
 * Holds the latest price or estimate (only the newest request's answer shows)
 * and the focused area; area figures come from `AreasProvider`. A new request
 * cancels one in flight, and an estimate's 404 is shown on its postcode field.
 * Only the first section waiting on figures offers a retry.
 */
export function HomePage({ today }: HomePageProps = {}): React.ReactElement {
  const {
    status: areasStatus,
    areas,
    areaByCode,
    window: dataWindow,
    mortgageRates,
    errorMessage,
    retry,
  } = useAreas();

  const [latest, setLatest] = useState<Latest | null>(null);
  const [status, setStatus] = useState<ResultStatus>('idle');
  const [resultError, setResultError] = useState<string | null>(null);
  // True when the last failure was about the request itself, which sending it again will not fix.
  const [requestRejected, setRequestRejected] = useState(false);
  const [postcodeError, setPostcodeError] = useState<string | null>(null);
  const [district, setDistrict] = useState<string>(DEFAULT_DISTRICT);
  const [areaFocusRequest, setAreaFocusRequest] = useState(0);
  const [yieldRequest, setYieldRequest] = useState(0);
  const reducedMotion = useReducedMotion();

  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => () => inFlight.current?.abort(), []);

  const area = areaByCode[district] ?? areaByCode[DEFAULT_DISTRICT] ?? areas[0];

  const coveredDistricts = useMemo(
    () => (areasStatus === 'ready' ? new Set(areas.map((candidate) => candidate.code)) : undefined),
    [areasStatus, areas],
  );

  /**
   * Sends a price or estimate request, cancelling any other still in flight.
   */
  const send = useCallback(async (next: Latest) => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    setLatest({ ...next, result: null } as Latest);
    setStatus('loading');
    setResultError(null);
    setRequestRejected(false);
    setPostcodeError(null);

    try {
      if (next.kind === 'price') {
        const result = await requestPrice(next.request, controller.signal);
        setLatest({ kind: 'price', request: next.request, result });
        setDistrict(districtOf(result.address.postcode));
      } else {
        const result = await requestEstimate(next.request, controller.signal);
        setLatest({ kind: 'estimate', request: next.request, result });
        setDistrict(result.request.district);
      }
      setStatus('ready');
    } catch (error) {
      // Only this call's own cancellation is silent; a timeout, even one that
      // fires while the body is read, is an error with a retry.
      if (controller.signal.aborted) {
        return;
      }
      const apiError = error instanceof ApiError ? error : null;
      if (next.kind === 'estimate' && apiError?.status === 404) {
        setPostcodeError(apiError.userMessage);
        setLatest(null);
        setStatus('idle');
        return;
      }
      setResultError(apiError?.userMessage ?? FALLBACK_ERROR);
      setRequestRejected(Boolean(apiError && !apiError.retryable && !apiError.rateLimited));
      setStatus('error');
    } finally {
      if (inFlight.current === controller) {
        inFlight.current = null;
      }
    }
  }, []);

  const handlePrice = useCallback((request: PriceRequest) => void send({ kind: 'price', request, result: null }), [send]);
  const handleEstimate = useCallback(
    (request: EstimateRequest) => void send({ kind: 'estimate', request, result: null }),
    [send],
  );

  const handleRetry = useCallback(() => {
    if (latest) {
      void send(latest);
    }
  }, [latest, send]);

  /**
   * Scrolls a section's heading into view and focuses it.
   */
  const moveTo = useCallback(
    (headingId: string) => {
      const heading = document.getElementById(headingId);
      if (!heading) {
        return;
      }
      if (typeof heading.scrollIntoView === 'function') {
        heading.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
      }
      heading.focus({ preventScroll: true });
    },
    [reducedMotion],
  );

  useEffect(() => {
    if (areaFocusRequest) {
      moveTo('areas-heading');
    }
    // Run once per request; the motion preference is read at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [areaFocusRequest]);

  useEffect(() => {
    if (yieldRequest) {
      moveTo('yield-heading');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [yieldRequest]);

  const shownPrice = latest?.kind === 'price' ? latest.result : null;
  const shownEstimate = latest?.kind === 'estimate' ? latest.result : null;
  const resultDistrict = shownPrice
    ? districtOf(shownPrice.address.postcode)
    : latest?.kind === 'estimate'
      ? shownEstimate?.request.district ?? latest.request.district
      : null;

  const handleSeeArea = useCallback(() => {
    if (resultDistrict && areaByCode[resultDistrict]) {
      setDistrict(resultDistrict);
    }
    setAreaFocusRequest((n) => n + 1);
  }, [resultDistrict, areaByCode]);

  const handleWorkOutYield = useCallback(() => setYieldRequest((n) => n + 1), []);

  const handleGetEstimate = useCallback(() => {
    const input = document.getElementById(ADDRESS_INPUT_ID);
    if (input && typeof input.scrollIntoView === 'function') {
      input.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'center' });
    }
    if (input instanceof HTMLInputElement) {
      input.focus({ preventScroll: true });
    }
  }, [reducedMotion]);

  const handleEditDetails = useCallback(() => {
    const input = document.getElementById('postcode');
    if (input && typeof input.scrollIntoView === 'function') {
      input.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'center' });
    }
    input?.focus({ preventScroll: true });
  }, [reducedMotion]);

  const homeFigures = useMemo<YieldFigures | null>(() => {
    if (shownPrice) {
      const homeArea = areaByCode[districtOf(shownPrice.address.postcode)];
      // An API that predates habitableRooms leaves it out, which reads as not known.
      const rooms = shownPrice.habitableRooms ?? null;
      return {
        key: 'price:' + addressKey(shownPrice.address) + ':' + shownPrice.saleDate + ':' + shownPrice.value,
        description:
          'your price for ' +
          describeFullAddress(shownPrice.address) +
          describeHomeRent(homeArea, bedroomsFromRooms(rooms)),
        purchasePrice: shownPrice.value,
        monthlyRent: homeMonthlyRent(homeArea, rooms),
      };
    }
    if (shownEstimate) {
      const estimateArea = areaByCode[shownEstimate.request.district];
      return {
        key: 'estimate:' + shownEstimate.request.postcode + ':' + shownEstimate.value,
        description:
          'your estimate for ' +
          shownEstimate.request.postcode +
          (shownEstimate.suggestedMonthlyRent === null
            ? ''
            : describeHomeRent(estimateArea, shownEstimate.request.bedrooms)),
        purchasePrice: shownEstimate.value,
        monthlyRent: shownEstimate.suggestedMonthlyRent,
      };
    }
    return null;
  }, [shownPrice, shownEstimate, areaByCode]);

  // The same borough rent the yield calculator starts from, for the detailed analysis.
  const homeRent = useMemo<HomeRent | null>(() => {
    if (!shownPrice) {
      return null;
    }
    const homeArea = areaByCode[districtOf(shownPrice.address.postcode)];
    const rooms = shownPrice.habitableRooms ?? null;
    const monthly = homeMonthlyRent(homeArea, rooms);
    if (!homeArea || monthly === null) {
      return null;
    }
    return { monthly, month: homeArea.rentMonth, basis: rentBasis(homeArea, bedroomsFromRooms(rooms)) };
  }, [shownPrice, areaByCode]);

  const areaFigures = useMemo<YieldFigures | null>(
    () =>
      area
        ? {
            key: 'area:' + area.code,
            description: 'the average ' + area.figuresYear + ' price in ' + area.code + ', ' + area.name,
            purchasePrice: area.averagePrice,
            monthlyRent: area.typicalMonthlyRent,
          }
        : null,
    [area],
  );

  const areasNotice = (withRetry: boolean): React.ReactNode =>
    areasStatus === 'ready' ? null : (
      <DataNotice
        status={areasStatus}
        loadingText={AREAS_LOADING}
        errorText={errorMessage}
        onRetry={withRetry ? retry : undefined}
      />
    );

  return (
    <PageShell skipTo={{ href: '#top', label: 'Skip to the price search' }} onGetEstimate={handleGetEstimate}>
      <Hero
        findAddresses={getAddresses}
        searchAddresses={searchAddresses}
        onPrice={handlePrice}
        onEstimate={handleEstimate}
        coveredDistricts={coveredDistricts}
        postcodeError={postcodeError}
        today={today}
        pending={status === 'loading' && latest ? latest.kind : null}
      />

      {latest && status !== 'idle' ? (
        <section className={styles.result} id="result" aria-labelledby="result-heading">
          <div className={styles.resultInner}>
            {latest.kind === 'price' ? (
              <PriceResultCard
                result={latest.result}
                loadNearby={getNearbySales}
                loadInflation={getInflation}
                loadSchools={getNearbySchools}
                rent={homeRent}
                status={status}
                errorMessage={resultError}
                onRetry={handleRetry}
                onEdit={requestRejected ? handleGetEstimate : undefined}
                onWorkOutYield={handleWorkOutYield}
              />
            ) : (
              <EstimateResult
                result={latest.result}
                areaName={areaByCode[resultDistrict ?? '']?.name ?? resultDistrict ?? ''}
                status={status}
                errorMessage={resultError}
                onRetry={handleRetry}
                onEdit={requestRejected ? handleEditDetails : undefined}
                onSeeArea={handleSeeArea}
                onWorkOutYield={handleWorkOutYield}
              />
            )}
          </div>
        </section>
      ) : null}

      <Section id="areas" heading="Explore an area" tone="sunk">
        {area && dataWindow ? (
          <AreaSnapshot area={area} areas={areas} dataWindow={dataWindow} onSelectDistrict={setDistrict} />
        ) : (
          areasNotice(true)
        )}
      </Section>

      <Section id="yield" heading="Could it pay as a rental?">
        {homeFigures || areaFigures ? (
          <YieldCalculator
            home={homeFigures}
            area={areaFigures}
            homeRequest={yieldRequest}
            mortgageRates={mortgageRates}
          />
        ) : (
          areasNotice(false)
        )}
      </Section>

      <Section id="how" heading="How it works" tone="sunk">
        <HowItWorks />
      </Section>

      <TrustBand />

      <Section id="faq" heading="Common questions">
        <FaqAccordion />
      </Section>
    </PageShell>
  );
}

export default HomePage;
