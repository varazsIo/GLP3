import React, { useState } from 'react';
import type { AddressOption, EstimateRequest, PriceRequest } from '../data/types';
import PriceForm from '../price/PriceForm';
import EstimateForm from './EstimateForm';
import Terraces from './Terraces';
import styles from './Hero.module.css';

/**
 * Props for `Hero`.
 */
export interface HeroProps {
  /** Lists the homes in a full postcode. */
  findAddresses: (postcode: string, signal?: AbortSignal) => Promise<AddressOption[]>;
  /** Suggests homes for a partial address. */
  searchAddresses: (q: string, signal?: AbortSignal) => Promise<AddressOption[]>;
  /** Called with the price form's validated request. */
  onPrice: (request: PriceRequest) => void;
  /** Called with the details form's validated request. */
  onEstimate: (request: EstimateRequest) => void;
  /** Covered area codes, once they have loaded. */
  coveredDistricts?: ReadonlySet<string>;
  /** A postcode problem the server reported for the details form, e.g. a district it does not cover. */
  postcodeError?: string | null;
  /** The day sale dates are measured from; tests pass one in. */
  today?: Date;
  /** Which form's request is being worked out, if either. */
  pending?: 'price' | 'estimate' | null;
}

/**
 * The home page's opening screen: the question, the address search, and the
 * details estimate for a home the search cannot find.
 *
 * The details form stays folded away until asked for, or until the server
 * reports a problem with its postcode.
 */
export function Hero({
  findAddresses,
  searchAddresses,
  onPrice,
  onEstimate,
  coveredDistricts,
  postcodeError,
  today,
  pending = null,
}: HeroProps): React.ReactElement {
  const [detailsOpen, setDetailsOpen] = useState(false);
  // The server's postcode problem the details were closed on, so the toggle can close them over it.
  const [closedOnError, setClosedOnError] = useState<string | null>(null);
  const showDetails = detailsOpen || (Boolean(postcodeError) && postcodeError !== closedOnError);

  return (
    <section className={styles.hero} id="top" aria-labelledby="hero-heading">
      <div className={styles.backdrop} aria-hidden="true">
        <div className={styles.sky} />
        <Terraces className={styles.terraces} />
      </div>
      <div className={styles.scrim} aria-hidden="true" />

      <div className={styles.inner}>
        <h1 className={styles.headline} id="hero-heading">
          What’s a London home worth?
        </h1>

        <div className={styles.panel}>
          <PriceForm
            findAddresses={findAddresses}
            searchAddresses={searchAddresses}
            onSubmit={onPrice}
            today={today}
            pending={pending === 'price'}
          />

          <button
            type="button"
            className={styles.detailsToggle}
            aria-expanded={showDetails}
            aria-controls="estimate-details"
            onClick={() => {
              setDetailsOpen(!showDetails);
              setClosedOnError(showDetails ? postcodeError ?? null : null);
            }}
          >
            Can’t find the address? Estimate from details
          </button>

          <div id="estimate-details" className={styles.details} hidden={!showDetails}>
            <EstimateForm
              onSubmit={onEstimate}
              coveredDistricts={coveredDistricts}
              serverPostcodeError={postcodeError}
              pending={pending === 'estimate'}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

export default Hero;
