/**
 * One console line per API call, under the id the server logs it with.
 *
 * Every call the page makes (`./api`, and `./captcha`'s session calls)
 * carries a fresh `X-Request-ID`; nginx and the API write it in their logs as
 * `rid=`. In development (and in tests with `localStorage['hpldn.apiLog']`
 * set to `1`) each call also prints
 * `[api] 12:16:41.123 GET /api/areas -> 200 in 7 ms (rid …)`, so the browser
 * console and `api.log` read side by side. A production build has none of
 * it: `apiLogEnabled` is a constant `false` there, so the bundler drops the
 * logging and the `localStorage` switch altogether.
 */

/** The header naming one call in the browser console, nginx's log and the API's log. */
export const REQUEST_ID_HEADER = 'X-Request-ID';

/** The `localStorage` key that turns the console's call log on outside development (never in production). */
export const API_LOG_KEY = 'hpldn.apiLog';

/**
 * Returns a new id for one call: 16 hex characters, as the API makes its own.
 */
export function newRequestId(): string {
  const bytes = new Uint8Array(8);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * Checks whether each call is logged to the console: always in development,
 * never in a production build, and in tests when `localStorage['hpldn.apiLog']`
 * is `1`.
 */
export function apiLogEnabled(): boolean {
  // `process.env.NODE_ENV` is a constant in the build: this branch makes the
  // rest of the function, and every console line below, dead code there.
  if (process.env.NODE_ENV === 'production') {
    return false;
  }
  if (process.env.NODE_ENV === 'development') {
    return true;
  }
  try {
    return typeof window !== 'undefined' && window.localStorage?.getItem(API_LOG_KEY) === '1';
  } catch {
    return false;
  }
}

/** The local time of day with milliseconds, e.g. `12:16:41.123`, to set beside `api.log`. */
function clockTime(at: Date): string {
  const pad = (n: number, width = 2): string => String(n).padStart(width, '0');
  return pad(at.getHours()) + ':' + pad(at.getMinutes()) + ':' + pad(at.getSeconds()) + '.' + pad(at.getMilliseconds(), 3);
}

/**
 * Prints one call's console line when `apiLogEnabled`.
 *
 * @param outcome The HTTP status, or what happened instead (`cancelled`, `timed out`, `network error`).
 */
export function logCall(method: string, url: string, outcome: number | string, startedAt: Date, requestId: string): void {
  if (!apiLogEnabled()) {
    return;
  }
  const ms = Math.round(Date.now() - startedAt.getTime());
  const line =
    '[api] ' + clockTime(startedAt) + ' ' + method + ' ' + url + ' -> ' + outcome + ' in ' + ms + ' ms (rid ' + requestId + ')';
  const failed = typeof outcome !== 'number' || outcome >= 400;
  // eslint-disable-next-line no-console
  (failed ? console.warn : console.info)(line);
}

/**
 * `fetch` with a fresh `X-Request-ID`, printing the call's console line.
 *
 * For the calls made outside `./api`'s `request` (the reCAPTCHA session).
 */
export async function loggedFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const requestId = newRequestId();
  const headers = { ...(init.headers as Record<string, string> | undefined), [REQUEST_ID_HEADER]: requestId };
  const method = (init.method ?? 'GET').toUpperCase();
  const startedAt = new Date();
  try {
    const response = await fetch(url, { ...init, headers });
    logCall(method, url, response.status, startedAt, requestId);
    return response;
  } catch (error) {
    const aborted = typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'AbortError';
    logCall(method, url, aborted ? 'cancelled' : 'network error', startedAt, requestId);
    throw error;
  }
}
