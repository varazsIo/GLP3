import React, { useEffect, useRef, useState } from 'react';
import { PROPERTY_TYPES } from '../data/types';
import type { EstimateRequest, PropertyType } from '../data/types';
import { warmPass } from '../lib/captcha';
import { districtOf, isCoveredPostcode, isPostcodeShaped, minFloorAreaFor, normalisePostcode } from '../lib/estimate';
import { earliestSaleDate, latestSaleDate, saleDateError } from '../lib/price';
import Button from '../ui/Button';
import DateField from '../ui/DateField';
import Field from '../ui/Field';
import RecaptchaNotice from '../ui/RecaptchaNotice';
import styles from './EstimateForm.module.css';

/**
 * Props for `EstimateForm`.
 */
export interface EstimateFormProps {
  /** Called with a validated request. */
  onSubmit: (request: EstimateRequest) => void;
  /**
   * Covered area codes, for the "We do not cover X yet" message. Until they
   * load, the check is left to the server.
   */
  coveredDistricts?: ReadonlySet<string>;
  /** A postcode problem the server reported for the last submission. */
  serverPostcodeError?: string | null;
  /** True while the estimate it asked for is being worked out. */
  pending?: boolean;
  /** The day to measure the sale date against; injectable for tests. */
  today?: Date;
}

interface Errors {
  postcode?: string;
  floorArea?: string;
  saleDate?: string;
}

const BEDROOM_CHOICES = [1, 2, 3, 4, 5, 6];

/**
 * The estimate from details: postcode, property type, bedrooms and floor area,
 * for a home the address search cannot find.
 *
 * Validates postcode shape and coverage and a 15-1,000 m² floor area, focusing
 * the first invalid field, and shows a server postcode error on the same field.
 * A field's error clears as soon as it is edited, and after the first submit
 * each field is checked again as it is left, so no message outlives its cause.
 */
export function EstimateForm({
  onSubmit,
  coveredDistricts,
  serverPostcodeError,
  pending = false,
  today = new Date(),
}: EstimateFormProps): React.ReactElement {
  const [postcode, setPostcode] = useState('');
  const [propertyType, setPropertyType] = useState<PropertyType>('flat');
  const [bedrooms, setBedrooms] = useState(2);
  const [floorArea, setFloorArea] = useState('');
  // The same default the price form uses, so both flows value the same day.
  const [saleDate, setSaleDate] = useState(() => earliestSaleDate(today));
  const [errors, setErrors] = useState<Errors>({});
  // Set by the first submit: from then on a field is re-checked as it is left.
  const [submitted, setSubmitted] = useState(false);
  // The server error the postcode has been edited since, which then stops showing.
  const [editedOnServerError, setEditedOnServerError] = useState<string | null>(null);

  const postcodeRef = useRef<HTMLInputElement>(null);
  const floorAreaRef = useRef<HTMLInputElement>(null);
  const dateRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (serverPostcodeError) {
      postcodeRef.current?.focus();
    }
  }, [serverPostcodeError]);

  function validate(): Errors {
    const next: Errors = {};

    const trimmed = postcode.trim();
    if (!trimmed) {
      next.postcode = 'Enter a London postcode.';
    } else if (!isPostcodeShaped(trimmed)) {
      next.postcode = 'Invalid UK Postcode';
    } else if (coveredDistricts && !isCoveredPostcode(trimmed, coveredDistricts)) {
      next.postcode = 'We do not cover ' + districtOf(trimmed) + ' yet. Try a London postcode.';
    }

    const area = Number(floorArea);
    if (!floorArea.trim() || !Number.isFinite(area) || area < 15) {
      next.floorArea = 'Floor area must be at least 15 m².';
    } else if (area > 1000) {
      next.floorArea = 'Floor area must be under 1,000 m².';
    } else if (area < minFloorAreaFor(bedrooms)) {
      next.floorArea =
        'A home with ' + bedrooms + ' bedrooms needs at least ' + minFloorAreaFor(bedrooms) + ' m². Check the floor area or the bedrooms.';
    }

    const dateMessage = saleDateError(saleDate, today);
    if (dateMessage) {
      next.saleDate = dateMessage;
    }

    return next;
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const next = validate();
    setErrors(next);
    setSubmitted(true);
    setEditedOnServerError(null);

    if (next.postcode) {
      postcodeRef.current?.focus();
      return;
    }
    if (next.floorArea) {
      floorAreaRef.current?.focus();
      return;
    }
    if (next.saleDate) {
      dateRef.current?.focus();
      return;
    }

    onSubmit({
      district: districtOf(postcode),
      postcode: normalisePostcode(postcode),
      propertyType,
      bedrooms,
      floorAreaSqm: Number(floorArea),
      saleDate,
    });
  }

  /** Clears a field's error as it is edited, so a fixed field never shows a stale message. */
  function clearError(field: keyof Errors): void {
    setErrors((was) => {
      if (!was[field]) {
        return was;
      }
      const next = { ...was };
      delete next[field];
      return next;
    });
  }

  /** Re-checks a field as it is left, once the form has been submitted. */
  function recheck(field: keyof Errors): void {
    if (!submitted) {
      return;
    }
    const message = validate()[field];
    setErrors((was) => {
      const next = { ...was };
      if (message) {
        next[field] = message;
      } else {
        delete next[field];
      }
      return next;
    });
  }

  const serverError = serverPostcodeError && serverPostcodeError !== editedOnServerError ? serverPostcodeError : null;
  const postcodeError = errors.postcode ?? serverError ?? undefined;
  const errorCount = (errors.floorArea ? 1 : 0) + (postcodeError ? 1 : 0) + (errors.saleDate ? 1 : 0);

  return (
    <form
      className={styles.card}
      onSubmit={handleSubmit}
      // Gets the reCAPTCHA pass on the first interaction, so the first price is not kept waiting.
      onFocusCapture={warmPass}
      onInputCapture={warmPass}
      noValidate
      aria-label="Property details"
    >
      <div className={styles.grid}>
        <Field id="postcode" label="Postcode" error={postcodeError}>
          {(props) => (
            <input
              {...props}
              ref={postcodeRef}
              type="text"
              name="postcode"
              autoComplete="postal-code"
              placeholder="SW11 2AB"
              value={postcode}
              onChange={(event) => {
                setPostcode(event.target.value);
                clearError('postcode');
                setEditedOnServerError(serverPostcodeError ?? null);
              }}
              onBlur={() => recheck('postcode')}
            />
          )}
        </Field>

        <Field id="propertyType" label="Property type">
          {(props) => (
            <select
              {...props}
              name="propertyType"
              value={propertyType}
              onChange={(event) => setPropertyType(event.target.value as PropertyType)}
            >
              {PROPERTY_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field id="bedrooms" label="Bedrooms">
          {(props) => (
            <select
              {...props}
              name="bedrooms"
              value={bedrooms}
              onChange={(event) => {
                setBedrooms(Number(event.target.value));
                // The floor-area check depends on the bedrooms; it is re-run on the next submit.
                clearError('floorArea');
              }}
            >
              {BEDROOM_CHOICES.map((count) => (
                <option key={count} value={count}>
                  {count === 6 ? '6 or more' : count}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field id="floorArea" label="Floor area" error={errors.floorArea}>
          {({ className, ...props }) => (
            <div className={styles.measure}>
              <input
                {...props}
                className={[className, styles.measureInput].join(' ')}
                ref={floorAreaRef}
                type="number"
                name="floorArea"
                inputMode="numeric"
                min={15}
                max={1000}
                placeholder="72"
                value={floorArea}
                onChange={(event) => {
                  setFloorArea(event.target.value);
                  clearError('floorArea');
                }}
                onBlur={() => recheck('floorArea')}
              />
              <span className={styles.unit} aria-hidden="true">
                m²
              </span>
            </div>
          )}
        </Field>

        <DateField
          id="estimate-date"
          name="estimate-date"
          label="Value as of"
          value={saleDate}
          onChange={(next) => {
            setSaleDate(next);
            clearError('saleDate');
          }}
          min={earliestSaleDate(today)}
          max={latestSaleDate(today)}
          error={errors.saleDate}
          inputRef={dateRef}
        />

        <div className={styles.submitCell}>
          <Button type="submit" size="field" shape="rect" fullWidth loading={pending}>
            Estimate value
          </Button>
        </div>
      </div>

      <p className={styles.summary} role="status">
        {errorCount > 0
          ? 'Check ' + errorCount + (errorCount === 1 ? ' field' : ' fields') + ' above.'
          : ''}
      </p>
      <RecaptchaNotice />
    </form>
  );
}

export default EstimateForm;
