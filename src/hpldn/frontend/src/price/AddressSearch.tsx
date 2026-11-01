import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { AddressOption } from '../data/types';
import { ADDRESS_SUGGESTION_LIMIT, ApiError, MAX_SEARCH_LENGTH, searchKey, searchQueryFor } from '../lib/api';
import { normalisePostcode } from '../lib/estimate';
import { addressKey, describeAddress, describeFullAddress, hasHouseNumber, isFullPostcode } from '../lib/price';
import Combobox from '../ui/Combobox';
import styles from './AddressSearch.module.css';

/** How long typing must pause before a search is sent, in milliseconds. */
export const SEARCH_DEBOUNCE_MS = 300;
/** The shortest time between two searches leaving the page, in milliseconds. */
export const MIN_SEARCH_INTERVAL_MS = 400;
/** The fewest characters worth searching for. */
export const MIN_QUERY_LENGTH = 3;
/** The longest a 429's wait is honoured before searching again, in milliseconds. */
export const MAX_RETRY_AFTER_MS = 10_000;

const NO_HOMES = 'No homes found. Try the postcode';
const NARROW_DOWN = 'Add the house number or postcode to narrow the list';
const FALLBACK_ERROR = 'Something went wrong on our side. Try again in a moment.';
const BUSY = 'One moment…';

type SearchStatus = 'idle' | 'loading' | 'ready' | 'busy' | 'error';

/** No suggestions, as one array, so the list does not see new options on every render. */
const NO_OPTIONS: AddressOption[] = [];

/** The last search that was answered: what it was for, and what came back. */
interface Answer {
  key: string;
  options: AddressOption[];
}

/**
 * Props for `AddressSearch`.
 */
export interface AddressSearchProps {
  /** The input's id. */
  id: string;
  /** Lists the homes in a full postcode (`lib/api` `getAddresses`). */
  findAddresses: (postcode: string, signal?: AbortSignal) => Promise<AddressOption[]>;
  /** Suggests homes for a partial address (`lib/api` `searchAddresses`). */
  searchAddresses: (q: string, signal?: AbortSignal) => Promise<AddressOption[]>;
  /** Called with the chosen home, or null once the text is edited. */
  onChange: (address: AddressOption | null) => void;
  /** Error text for the field. */
  error?: string;
  /** The input element, for focusing it from outside. */
  inputRef?: React.Ref<HTMLInputElement>;
}

/**
 * The address field: type part of an address or a postcode, then pick the home.
 *
 * One pause in typing sends at most one request. A search waits for typing to
 * pause for `SEARCH_DEBOUNCE_MS`, leaves at least `MIN_SEARCH_INTERVAL_MS`
 * after the one before, needs `MIN_QUERY_LENGTH` characters, and is not sent
 * again for text the last answer already covers ("Barclay Rd," after "barclay
 * rd"). Only one is ever in flight, and it is never cancelled while the
 * landlord types: newer text waits for it and goes next (only the latest
 * text is kept), and an answer for text that has since changed is remembered
 * but not shown. Cancelling would not spare the server the work -- it would
 * only leave a cancelled request in the browser's network panel.
 *
 * A full postcode lists every home in it (`findAddresses`); anything else is one
 * `searchAddresses` call. A failure the landlord can do something about shows
 * with a retry; the server asking the page to slow down (429) shows "One
 * moment…" and searches again once, after the wait it asked for.
 */
export function AddressSearch({
  id,
  findAddresses,
  searchAddresses,
  onChange,
  error,
  inputRef,
}: AddressSearchProps): React.ReactElement {
  const [text, setText] = useState('');
  const [options, setOptions] = useState<AddressOption[]>([]);
  const [status, setStatus] = useState<SearchStatus>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [canRetry, setCanRetry] = useState(false);
  const [chosenKey, setChosenKey] = useState<string | null>(null);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flight = useRef<AbortController | null>(null);
  const lastSentAt = useRef(-Infinity);
  const lastAnswer = useRef<Answer | null>(null);
  const pendingQuery = useRef<string | null>(null);
  /** Text that was due while a search was in flight; sent once that one is answered. */
  const queued = useRef<string | null>(null);
  const dispatchRef = useRef<(query: string) => void>(() => undefined);

  /** Stops a search that is waiting to be sent; one already in flight runs on. */
  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    queued.current = null;
  }, []);

  // Leaving the page is the one time a search in flight is cancelled.
  useEffect(
    () => () => {
      clearTimer();
      flight.current?.abort();
      flight.current = null;
    },
    [clearTimer],
  );

  const run = useCallback(
    async (query: string) => {
      const controller = new AbortController();
      flight.current = controller;
      lastSentAt.current = Date.now();
      const byPostcode = isFullPostcode(query);
      const key = byPostcode ? 'POSTCODE ' + normalisePostcode(query) : searchKey(query);
      // The text has changed (or a home was chosen) since this was sent.
      const stale = (): boolean => pendingQuery.current !== query;
      try {
        const found = byPostcode
          ? await findAddresses(normalisePostcode(query), controller.signal)
          : await searchAddresses(query, controller.signal);
        const shown = found.slice(0, ADDRESS_SUGGESTION_LIMIT);
        lastAnswer.current = { key, options: shown };
        if (stale()) {
          return;
        }
        setOptions(shown);
        setStatus('ready');
      } catch (caught) {
        if (controller.signal.aborted || stale()) {
          return;
        }
        const apiError = caught instanceof ApiError ? caught : null;
        setOptions([]);
        if (apiError?.rateLimited) {
          // Not a failure to show: wait as asked, then try this text once more.
          setStatus('busy');
          const waitMs = Math.min(MAX_RETRY_AFTER_MS, Math.max(MIN_SEARCH_INTERVAL_MS, (apiError.retryAfterS ?? 1) * 1000));
          timer.current = setTimeout(() => {
            timer.current = null;
            if (pendingQuery.current === query) {
              void run(query);
            }
          }, waitMs);
          return;
        }
        // A postcode the server knows but cannot price is an answer, not a failure.
        if (apiError?.status === 404 && !apiError.missingRoute) {
          lastAnswer.current = null;
          setErrorMessage(apiError.userMessage);
          setCanRetry(false);
          setStatus('error');
          return;
        }
        setErrorMessage(apiError?.userMessage ?? FALLBACK_ERROR);
        setCanRetry(apiError === null || apiError.retryable);
        setStatus('error');
      } finally {
        if (flight.current === controller) {
          flight.current = null;
        }
        const next = queued.current;
        queued.current = null;
        if (next !== null && next === pendingQuery.current && !controller.signal.aborted) {
          dispatchRef.current(next);
        }
      }
    },
    [findAddresses, searchAddresses],
  );

  /**
   * Sends `query` now, or as soon as the minimum interval since the last
   * search allows; while a search is in flight it is queued behind it instead.
   */
  const dispatch = useCallback(
    (query: string) => {
      if (flight.current !== null) {
        queued.current = query;
        return;
      }
      const wait = lastSentAt.current + MIN_SEARCH_INTERVAL_MS - Date.now();
      if (wait > 0) {
        timer.current = setTimeout(() => {
          timer.current = null;
          void run(query);
        }, wait);
        return;
      }
      void run(query);
    },
    [run],
  );
  dispatchRef.current = dispatch;

  function handleInputChange(next: string): void {
    setText(next);
    if (chosenKey !== null) {
      setChosenKey(null);
      onChange(null);
    }
    clearTimer();
    setErrorMessage(null);
    setCanRetry(false);

    const query = searchQueryFor(next);
    pendingQuery.current = query;
    if (query === null) {
      pendingQuery.current = null;
      setOptions([]);
      setStatus('idle');
      return;
    }

    // The last answer already covers this text: show it again, send nothing.
    const key = isFullPostcode(query) ? 'POSTCODE ' + normalisePostcode(query) : searchKey(query);
    if (lastAnswer.current?.key === key) {
      setOptions(lastAnswer.current.options);
      setStatus('ready');
      return;
    }

    setStatus('loading');
    timer.current = setTimeout(() => {
      timer.current = null;
      dispatch(query);
    }, SEARCH_DEBOUNCE_MS);
  }

  function handleRetry(): void {
    const query = pendingQuery.current;
    if (query === null) {
      return;
    }
    clearTimer();
    setErrorMessage(null);
    setCanRetry(false);
    setStatus('loading');
    dispatch(query);
  }

  function handleSelect(address: AddressOption): void {
    // A search still in flight is left to finish; its answer is not shown.
    clearTimer();
    pendingQuery.current = null;
    setText(describeFullAddress(address));
    setChosenKey(addressKey(address));
    setOptions([]);
    setStatus('idle');
    onChange(address);
  }

  let message: string | null = null;
  if (status === 'loading') {
    message = 'Searching…';
  } else if (status === 'busy') {
    message = BUSY;
  } else if (status === 'error') {
    message = errorMessage;
  } else if (status === 'ready' && !options.length) {
    message = NO_HOMES;
  } else if (status === 'ready' && options.length >= ADDRESS_SUGGESTION_LIMIT && !hasHouseNumber(text)) {
    message = NARROW_DOWN;
  }

  return (
    <Combobox<AddressOption>
      id={id}
      label="Address"
      name={id}
      placeholder="Start typing an address or postcode"
      inputValue={text}
      onInputChange={handleInputChange}
      options={status === 'ready' ? options : NO_OPTIONS}
      getOptionKey={addressKey}
      getOptionLabel={describeFullAddress}
      renderOption={(address) => (
        <>
          <strong className={styles.line}>{describeAddress(address)}</strong>
          <span className={styles.postcode}>{address.postcode}</span>
        </>
      )}
      onSelect={handleSelect}
      message={message}
      selectedKey={chosenKey}
      error={error}
      inputRef={inputRef}
      busy={status === 'loading' || status === 'busy'}
      onRetry={status === 'error' && canRetry ? handleRetry : undefined}
      retryLabel="Search again"
      maxLength={MAX_SEARCH_LENGTH}
    />
  );
}

export default AddressSearch;
