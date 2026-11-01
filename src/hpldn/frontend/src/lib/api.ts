/**
 * Client for the HPLDN read API.
 *
 * Every call goes to same-origin `/api`, or `REACT_APP_API_BASE`. A proxy adds
 * the API key on the way through (`src/setupProxy.js` in development, nginx in
 * production), so the bundle never holds a key. The routes in `GATED_PATHS`
 * (everything but the open GETs and address search) also carry a reCAPTCHA
 * pass (`./captcha`) in `X-HPLDN-Pass`.
 *
 * Every call carries a fresh `X-Request-ID` and prints a console line with
 * it (`./callLog`), matching nginx's and the API's log lines.
 */
import type {
  AddressOption,
  AddressSuggestion,
  AreasResponse,
  EstimateRequest,
  EstimateResult,
  InflationSeries,
  NearbySales,
  NearbySchools,
  PriceRequest,
  PriceResult,
} from '../data/types';
import { logCall, newRequestId, REQUEST_ID_HEADER } from './callLog';
import { CaptchaError, dropPass, ensurePass } from './captcha';

const API_BASE = process.env.REACT_APP_API_BASE ?? '/api';

/** How long a call may take before the page stops waiting, in milliseconds. */
export const REQUEST_TIMEOUT_MS = 10_000;

const MESSAGES = {
  network: 'We could not reach our data just now. Check your connection and try again.',
  timeout: 'This is taking longer than it should. Try again in a moment.',
  invalid: 'Some of those details did not look right. Check them and try again.',
  server: 'Something went wrong on our side. Try again in a moment.',
  busy: 'One moment…',
  forbidden:
    'Access from your network is temporarily restricted because of unusual traffic. It lifts automatically.',
};

/**
 * The API paths that want a reCAPTCHA pass (`guard.yaml` `captcha.gated_routes`).
 * `/areas` and `/inflation` stay open for the home page and search crawlers,
 * and `/addresses/search` so that a first search never waits for Google.
 */
export const GATED_PATHS: ReadonlySet<string> = new Set([
  '/addresses',
  '/price',
  '/estimate',
  '/nearby',
  '/schools',
]);

/** The header a gated call carries its pass in. */
export const PASS_HEADER = 'X-HPLDN-Pass';

/**
 * Checks whether a call to `path` (with or without a query) needs a pass.
 */
export function isGated(path: string): boolean {
  return GATED_PATHS.has(path.split('?')[0]);
}

/**
 * A failed call, carrying a sentence that is safe to show a landlord.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly userMessage: string;
  /**
   * True when the server has no such route at all (a 404 or 405 without a
   * sentence of its own), as opposed to a route that found nothing.
   */
  readonly missingRoute: boolean;
  /** For a 429, how long the server asked the page to wait, in seconds. */
  readonly retryAfterS: number | null;
  /**
   * For a 403 about the reCAPTCHA pass, the server's `X-HPLDN-Captcha`:
   * `required` (no pass, or a stale one) or `low_score` (Google judged a bot).
   */
  readonly captcha: string | null;

  constructor(
    status: number,
    userMessage: string,
    message?: string,
    missingRoute = false,
    retryAfterS: number | null = null,
    captcha: string | null = null,
  ) {
    super(message ?? userMessage);
    this.name = 'ApiError';
    this.status = status;
    this.userMessage = userMessage;
    this.missingRoute = missingRoute;
    this.retryAfterS = retryAfterS;
    this.captcha = captcha;
  }

  /** True when the server is asking the page to slow down (429), which is not a failure to show. */
  get rateLimited(): boolean {
    return this.status === 429;
  }

  /**
   * True when trying the same call again may succeed: no answer at all, a
   * missing route (a deploy in progress), or a server error.
   */
  get retryable(): boolean {
    return this.status === 0 || this.missingRoute || this.status >= 500;
  }
}

/**
 * Reads a `Retry-After` header given in seconds; an HTTP date or nonsense is ignored.
 */
function retryAfterOf(response: Response): number | null {
  const value = response.headers?.get?.('Retry-After');
  if (!value || !/^\d+$/.test(value.trim())) {
    return null;
  }
  return Number(value.trim());
}

/**
 * Checks whether a failed response means the route does not exist: FastAPI's
 * own "Not Found", a 405, or a 404 page with no JSON sentence, such as nginx's.
 */
function isMissingRoute(status: number, detail: unknown): boolean {
  return status === 405 || (status === 404 && (typeof detail !== 'string' || detail === 'Not Found'));
}

/**
 * Checks whether an error is a cancellation the caller requested.
 *
 * Compares the name because `DOMException` is not an `Error` subclass in every
 * environment.
 *
 * @param error The thrown value.
 * @returns True for an `AbortError`, which is not an error to show.
 */
export function isAbort(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'AbortError';
}

/**
 * Reads a response body as JSON, rejecting with an `AbortError` as soon as
 * `signal` aborts.
 *
 * `fetch` already ties the body stream to the signal, but a stalled
 * `response.json()` is not guaranteed to settle in every engine or test
 * double, so the abort is raced here as well.
 */
function readJson(response: Response, signal: AbortSignal): Promise<unknown> {
  if (signal.aborted) {
    return Promise.reject(new DOMException('Aborted', 'AbortError'));
  }
  return new Promise((resolve, reject) => {
    const onAbort = (): void => reject(new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort, { once: true });
    response.json().then(
      (body) => {
        signal.removeEventListener('abort', onAbort);
        resolve(body);
      },
      (error) => {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      },
    );
  });
}

/**
 * Reads the `detail` field of an error response, if it has one.
 */
async function detailOf(response: Response, signal: AbortSignal): Promise<unknown> {
  try {
    const body = await readJson(response, signal);
    return body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Picks the landlord-facing message for a failed response.
 *
 * A 403, 404, 422 or 503 whose `detail` is a sentence carries the server's
 * own words, such as "We do not cover ZZ9 yet" or "Choose a sale date from
 * 15/09/2026 onwards". FastAPI's own validation errors carry a list, not a
 * sentence, and get the generic message. A 403 without one is nginx refusing
 * a banned network.
 */
function userMessageFor(status: number, detail: unknown): string {
  if (status === 429) {
    return MESSAGES.busy;
  }
  if ((status === 403 || status === 404 || status === 422 || status === 503) && typeof detail === 'string' && detail) {
    return detail;
  }
  if (status === 403) {
    return MESSAGES.forbidden;
  }
  if (status === 422 || status === 400) {
    return MESSAGES.invalid;
  }
  return MESSAGES.server;
}

/**
 * Waits for a pass, giving up when `signal` aborts or `timeoutMs` passes.
 *
 * @throws {ApiError} When no pass can be had (`CaptchaError`'s sentence) or the wait times out.
 * @throws An `AbortError` when `signal` aborts.
 */
function passFor(path: string, signal: AbortSignal | null | undefined, timeoutMs: number): Promise<string | null> {
  return new Promise((resolve, reject) => {
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      reject(new ApiError(0, MESSAGES.timeout, 'Getting a pass for ' + path + ' timed out'));
    }, timeoutMs);
    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener('abort', onAbort, { once: true });
    ensurePass().then(
      (pass) => {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        resolve(pass);
      },
      (error: unknown) => {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        if (error instanceof CaptchaError) {
          reject(
            new ApiError(
              error.status,
              error.status === 429 ? MESSAGES.busy : error.userMessage,
              error.message + ' (for ' + path + ')',
              false,
              error.status === 429 ? error.retryAfterS : null,
              error.status === 403 ? error.captcha ?? 'low_score' : null,
            ),
          );
        } else {
          reject(new ApiError(0, MESSAGES.network, 'Getting a pass for ' + path + ' failed'));
        }
      },
    );
  });
}

/**
 * Calls the API with JSON headers and a timeout, adding a reCAPTCHA pass on
 * the gated routes.
 *
 * A gated call the server refuses with `X-HPLDN-Captcha: required` (the pass
 * expired, or the visitor's address changed) drops the pass and is sent once
 * more with a new one.
 *
 * @param path The path below the API base, e.g. `/areas`.
 * @param init Fetch options; `signal` cancels the call.
 * @param timeoutMs How long to wait, in milliseconds; defaults to `REQUEST_TIMEOUT_MS`.
 * @returns The parsed JSON body.
 * @throws {ApiError} For a non-2xx response, a network failure, a malformed
 *     body or a timeout, including one that fires while the body is read, and
 *     for a visitor reCAPTCHA refuses (403, `captcha` `low_score`, with the
 *     server's sentence). Only a cancellation through `init.signal` rethrows an
 *     `AbortError`.
 */
export async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  if (!isGated(path)) {
    return send<T>(path, init, timeoutMs, null);
  }
  for (let attempt = 0; ; attempt += 1) {
    const pass = await passFor(path, init.signal, timeoutMs);
    try {
      return await send<T>(path, init, timeoutMs, pass);
    } catch (error) {
      if (attempt === 0 && error instanceof ApiError && error.captcha === 'required') {
        dropPass();
        continue;
      }
      throw error;
    }
  }
}

/**
 * Sends one call; `request` without the pass handling.
 */
async function send<T>(path: string, init: RequestInit, timeoutMs: number, pass: string | null): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const outer = init.signal;
  const forwardAbort = (): void => controller.abort();
  if (outer) {
    if (outer.aborted) {
      controller.abort();
    } else {
      outer.addEventListener('abort', forwardAbort);
    }
  }

  const requestId = newRequestId();
  const headers: Record<string, string> = { Accept: 'application/json', [REQUEST_ID_HEADER]: requestId };
  if (init.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (pass) {
    headers[PASS_HEADER] = pass;
  }

  const failure = (error: unknown, fallback: string): unknown => {
    if (timedOut) {
      return new ApiError(0, MESSAGES.timeout, 'Request to ' + path + ' timed out');
    }
    if (isAbort(error)) {
      return outer?.aborted ? error : new ApiError(0, MESSAGES.network, 'Request to ' + path + ' was cut off');
    }
    return error instanceof ApiError ? error : new ApiError(0, fallback, 'Request to ' + path + ' failed');
  };

  const method = (init.method ?? 'GET').toUpperCase();
  const startedAt = new Date();
  const logFailure = (error: unknown): void => {
    const outcome = timedOut ? 'timed out' : isAbort(error) ? 'cancelled' : 'network error';
    logCall(method, API_BASE + path, outcome, startedAt, requestId);
  };

  try {
    let response: Response;
    try {
      response = await fetch(API_BASE + path, { ...init, headers, signal: controller.signal });
    } catch (error) {
      logFailure(error);
      throw failure(error, MESSAGES.network);
    }
    logCall(method, API_BASE + path, response.status, startedAt, requestId);

    // The timer is still running while the body arrives, so a timeout or a
    // cancellation can land here too, and must map the same way.
    try {
      if (!response.ok) {
        const detail = await detailOf(response, controller.signal);
        if (timedOut || outer?.aborted) {
          throw failure(new DOMException('Aborted', 'AbortError'), MESSAGES.server);
        }
        throw new ApiError(
          response.status,
          userMessageFor(response.status, detail),
          'Request to ' + path + ' returned ' + response.status,
          isMissingRoute(response.status, detail),
          response.status === 429 ? retryAfterOf(response) : null,
          response.status === 403 ? response.headers?.get?.('X-HPLDN-Captcha') ?? null : null,
        );
      }
      return (await readJson(response, controller.signal)) as T;
    } catch (error) {
      throw failure(error, MESSAGES.server);
    }
  } finally {
    clearTimeout(timer);
    outer?.removeEventListener('abort', forwardAbort);
  }
}

/** The open GETs in flight, by path, that a second caller joins instead of sending its own. */
const inFlight = new Map<string, Promise<unknown>>();

/**
 * How long each of an open GET's two attempts may take, in milliseconds: both
 * fit in `REQUEST_TIMEOUT_MS`, so a retry never extends a visitor's wait.
 */
export const OPEN_GET_ATTEMPT_MS = REQUEST_TIMEOUT_MS / 2;

/**
 * Sends `GET path`, and once more when the first attempt fails in a way that
 * may pass: no answer in time, a dropped connection, a 5xx or a missing
 * route (a deploy or a restart in progress). The figures behind the open GETs
 * are the same for everyone, so one blip should not show the visitor an error.
 */
async function getWithRetry<T>(path: string): Promise<T> {
  try {
    return await request<T>(path, {}, OPEN_GET_ATTEMPT_MS);
  } catch (error) {
    if (error instanceof ApiError && error.retryable) {
      return request<T>(path, {}, OPEN_GET_ATTEMPT_MS);
    }
    throw error;
  }
}

/**
 * Resolves as `promise` does, or rejects with an `AbortError` as soon as `signal` aborts.
 */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (!signal) {
    return promise;
  }
  if (signal.aborted) {
    return Promise.reject(new DOMException('Aborted', 'AbortError'));
  }
  return new Promise((resolve, reject) => {
    const onAbort = (): void => reject(new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener('abort', onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      },
    );
  });
}

/**
 * Sends `GET path`, or joins the same call already in flight.
 *
 * The call is never cancelled by a caller: `signal` only stops that caller
 * waiting (an `AbortError`), and the others still get the answer. So React's
 * StrictMode running an effect twice, or two parts of the page wanting the
 * same figures, send one request and leave no cancelled one behind. A failure
 * that may pass is tried once more (`getWithRetry`); both attempts together are
 * bounded by `REQUEST_TIMEOUT_MS`. Once settled, the next call sends afresh.
 */
function sharedGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  let promise = inFlight.get(path) as Promise<T> | undefined;
  if (!promise) {
    const sent = getWithRetry<T>(path);
    const forget = (): void => {
      if (inFlight.get(path) === sent) {
        inFlight.delete(path);
      }
    };
    sent.then(forget, forget);
    inFlight.set(path, sent);
    promise = sent;
  }
  return untilAborted(promise, signal);
}

/** Forgets the calls in flight, so the next `sharedGet` sends afresh (for tests). */
export function forgetSharedGets(): void {
  inFlight.clear();
}

/**
 * Fetches every covered district and the data window their figures come from.
 *
 * @param signal Stops waiting for the answer; a call another part of the page
 *     still waits for is not cancelled (`sharedGet`).
 */
export function getAreas(signal?: AbortSignal): Promise<AreasResponse> {
  return sharedGet<AreasResponse>('/areas', signal);
}

/**
 * Requests an estimate for one property. The server derives the district.
 *
 * @param req The details the landlord entered.
 * @param signal Cancels the call.
 */
export function requestEstimate(req: EstimateRequest, signal?: AbortSignal): Promise<EstimateResult> {
  const { postcode, propertyType, bedrooms, floorAreaSqm, saleDate } = req;
  return request<EstimateResult>('/estimate', {
    method: 'POST',
    body: JSON.stringify({ postcode, propertyType, bedrooms, floorAreaSqm, saleDate: saleDate ?? null }),
    signal,
  });
}

/**
 * How long a price may take, in milliseconds. Longer than other calls: the
 * first price after the server starts can wait for its figures to load.
 */
export const PRICE_TIMEOUT_MS = 30_000;

/**
 * Lists the homes in a full postcode that can be priced.
 *
 * @param postcode The full postcode, in any case and spacing.
 * @param signal Cancels the call.
 * @throws {ApiError} A 404 carries "We could not find any homes we can price in …".
 */
export function getAddresses(postcode: string, signal?: AbortSignal): Promise<AddressOption[]> {
  return request<AddressOption[]>('/addresses?postcode=' + encodeURIComponent(postcode.trim()), { signal });
}

/**
 * How long each of an address search's two attempts may take, in
 * milliseconds. The API answers in a few; an attempt this slow is a stalled
 * connection, which a second one gets past, and both fit in
 * `REQUEST_TIMEOUT_MS`.
 */
export const SEARCH_ATTEMPT_MS = 4_000;

/** The most suggestions an address search asks for. */
export const ADDRESS_SUGGESTION_LIMIT = 8;
/** The fewest characters an address search sends; the server refuses fewer. */
export const MIN_SEARCH_LENGTH = 3;
/** The most characters an address search sends; the server refuses more. */
export const MAX_SEARCH_LENGTH = 64;

/** Characters the server accepts in an address search (`api/market/search_query.py`). */
const NOT_SEARCHABLE = /[^A-Za-z0-9 '\u2019\-,./]+/g;

/**
 * Turns what was typed into the text an address search sends, or null when it
 * should not be sent at all.
 *
 * Mirrors the server's bounds so ordinary typing never earns a 422: characters
 * the server refuses ("Flat A (Ground Floor)", "Smith & Sons") become spaces --
 * the server's normal form treats them as spaces anyway -- whitespace is
 * collapsed and trimmed, and the result must be 3 to 64 characters long.
 *
 * @param text The field's text.
 * @returns The query to send, or null.
 */
export function searchQueryFor(text: string): string | null {
  const query = text.normalize('NFKC').replace(NOT_SEARCHABLE, ' ').replace(/\s+/g, ' ').trim();
  if (query.length < MIN_SEARCH_LENGTH || query.length > MAX_SEARCH_LENGTH) {
    return null;
  }
  return searchKey(query).length < MIN_SEARCH_LENGTH ? null : query;
}

/**
 * The server's normal form of a query (`normalise_query`): upper case, no
 * apostrophes, every other run of punctuation or space as one space.
 *
 * Two texts with the same key get the same suggestions, so the page need not
 * ask again for "Barclay Rd," after "barclay rd".
 */
export function searchKey(query: string): string {
  return query
    .normalize('NFKC')
    .toUpperCase()
    .replace(/['\u2019`]/g, '')
    .replace(/[^A-Z0-9]+/g, ' ')
    .trim();
}

/**
 * Suggests homes that can be priced for a partial address, best match first.
 *
 * Needs no reCAPTCHA pass. An attempt that got no answer (none within
 * `SEARCH_ATTEMPT_MS`, or a dropped connection) is sent once more; a
 * cancellation through `signal` is not, and nor is an answer, even a 5xx,
 * which the landlord sees with "Search again".
 *
 * @param q What the landlord typed.
 * @param signal Cancels the call.
 * @returns Up to `ADDRESS_SUGGESTION_LIMIT` homes; an empty list when nothing matches.
 * @throws {ApiError} Without sending anything when `q` is outside the bounds
 *     `searchQueryFor` applies (status 422); a 429 has `rateLimited` and
 *     `retryAfterS`.
 */
export function searchAddresses(q: string, signal?: AbortSignal): Promise<AddressSuggestion[]> {
  const query = searchQueryFor(q);
  if (query === null) {
    return Promise.reject(new ApiError(422, MESSAGES.invalid, 'Address search needs 3 to 64 characters'));
  }
  const path = '/addresses/search?q=' + encodeURIComponent(query) + '&limit=' + ADDRESS_SUGGESTION_LIMIT;
  const attempt = (): Promise<AddressSuggestion[]> => request<AddressSuggestion[]>(path, { signal }, SEARCH_ATTEMPT_MS);
  return attempt().catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 0 && !signal?.aborted) {
      return attempt();
    }
    throw error;
  });
}

/**
 * Requests the price of one home for a sale on a chosen day.
 *
 * @param req The home's address, as `getAddresses` listed it, and the sale date.
 * @param signal Cancels the call.
 * @throws {ApiError} A 404 (address not found) or 422 (date not in the future,
 *     or the home lacks the records a price needs) carries the server's sentence.
 */
export function requestPrice(req: PriceRequest, signal?: AbortSignal): Promise<PriceResult> {
  const { postnumber, poststreet, posttown, postcode, unit, saleDate } = req;
  return request<PriceResult>(
    '/price',
    {
      method: 'POST',
      body: JSON.stringify({ postnumber, poststreet, posttown, postcode, ...(unit ? { unit } : {}), saleDate }),
      signal,
    },
    PRICE_TIMEOUT_MS,
  );
}

/**
 * Lists what the homes nearest one home sold for in the latest 12 months.
 *
 * @param address The home, as its price result names it.
 * @param signal Cancels the call.
 * @throws {ApiError} A 404 (address not found) carries the server's sentence.
 */
export function getNearbySales(address: AddressOption, signal?: AbortSignal): Promise<NearbySales> {
  const { postnumber, poststreet, posttown, postcode, unit } = address;
  return request<NearbySales>('/nearby', {
    method: 'POST',
    body: JSON.stringify({ postnumber, poststreet, posttown, postcode, ...(unit ? { unit } : {}) }),
    signal,
  });
}

/**
 * Lists the primary and secondary schools nearest one home, with their Ofsted ratings today.
 *
 * @param address The home, as its price result names it.
 * @param signal Cancels the call.
 * @throws {ApiError} A 404 (address not found) carries the server's sentence.
 */
export function getNearbySchools(address: AddressOption, signal?: AbortSignal): Promise<NearbySchools> {
  const { postnumber, poststreet, posttown, postcode, unit } = address;
  return request<NearbySchools>('/schools', {
    method: 'POST',
    body: JSON.stringify({ postnumber, poststreet, posttown, postcode, ...(unit ? { unit } : {}) }),
    signal,
  });
}

/**
 * Fetches UK consumer price inflation (CPIH) month by month.
 *
 * @param signal Stops waiting for the answer, as for `getAreas`.
 * @throws {ApiError} A 503 while the series is not loaded.
 */
export function getInflation(signal?: AbortSignal): Promise<InflationSeries> {
  return sharedGet<InflationSeries>('/inflation', signal);
}
