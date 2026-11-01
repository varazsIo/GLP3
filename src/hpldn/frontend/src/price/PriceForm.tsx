import React, { useRef, useState } from 'react';
import type { AddressOption, PriceRequest } from '../data/types';
import { warmPass } from '../lib/captcha';
import { earliestSaleDate, latestSaleDate, saleDateError } from '../lib/price';
import Button from '../ui/Button';
import DateField from '../ui/DateField';
import RecaptchaNotice from '../ui/RecaptchaNotice';
import AddressSearch from './AddressSearch';
import styles from './PriceForm.module.css';

/** The address input's id, which the header's "Price a home" button focuses. */
export const ADDRESS_INPUT_ID = 'price-address';

/**
 * Props for `PriceForm`.
 */
export interface PriceFormProps {
  /** Lists the homes in a full postcode (`lib/api` `getAddresses`). */
  findAddresses: (postcode: string, signal?: AbortSignal) => Promise<AddressOption[]>;
  /** Suggests homes for a partial address (`lib/api` `searchAddresses`). */
  searchAddresses: (q: string, signal?: AbortSignal) => Promise<AddressOption[]>;
  /** Called with a validated request. */
  onSubmit: (request: PriceRequest) => void;
  /** The day the sale date is measured from; tests pass one in. */
  today?: Date;
  /** True while the price it asked for is being worked out. */
  pending?: boolean;
}

interface Errors {
  address?: string;
  saleDate?: string;
}

/**
 * The form for pricing a specific home: its address, the sale date, and the button.
 *
 * The sale date defaults to tomorrow and cannot be today or earlier. Focus
 * moves to the first invalid field.
 */
export function PriceForm({
  findAddresses,
  searchAddresses,
  onSubmit,
  today,
  pending = false,
}: PriceFormProps): React.ReactElement {
  const [address, setAddress] = useState<AddressOption | null>(null);
  const [saleDate, setSaleDate] = useState(() => earliestSaleDate(today));
  const [errors, setErrors] = useState<Errors>({});

  const addressRef = useRef<HTMLInputElement>(null);
  const dateRef = useRef<HTMLInputElement>(null);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const next: Errors = {};
    if (!address) {
      next.address = 'Choose a home from the suggestions.';
    }
    const dateError = saleDateError(saleDate, today, dateRef.current?.value);
    if (dateError) {
      next.saleDate = dateError;
    }
    setErrors(next);

    if (next.address) {
      addressRef.current?.focus();
      return;
    }
    if (next.saleDate) {
      dateRef.current?.focus();
      return;
    }
    if (address) {
      onSubmit({ ...address, saleDate });
    }
  }

  const errorCount = Object.keys(errors).length;

  return (
    <form
      className={styles.card}
      onSubmit={handleSubmit}
      // Gets the reCAPTCHA pass on the first interaction, so the first price is not kept waiting.
      onFocusCapture={warmPass}
      onInputCapture={warmPass}
      noValidate
      aria-label="Home to price"
    >
      <div className={styles.grid}>
        <AddressSearch
          id={ADDRESS_INPUT_ID}
          findAddresses={findAddresses}
          searchAddresses={searchAddresses}
          onChange={(chosen) => {
            setAddress(chosen);
            if (chosen && errors.address) {
              setErrors(({ address: _cleared, ...rest }) => rest);
            }
          }}
          error={errors.address}
          inputRef={addressRef}
        />

        <DateField
          id="price-date"
          name="price-date"
          label="Sale date"
          value={saleDate}
          onChange={setSaleDate}
          min={earliestSaleDate(today)}
          max={latestSaleDate(today)}
          error={errors.saleDate}
          inputRef={dateRef}
        />

        <div className={styles.submitCell}>
          <Button type="submit" size="field" shape="rect" fullWidth loading={pending}>
            Price this home
          </Button>
        </div>
      </div>

      <p className={styles.summary} role="status">
        {errorCount > 0 ? 'Check ' + errorCount + (errorCount === 1 ? ' field' : ' fields') + ' above.' : ''}
      </p>
      <RecaptchaNotice />
    </form>
  );
}

export default PriceForm;
