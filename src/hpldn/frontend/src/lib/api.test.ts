import type { EstimateRequest } from '../data/types';
import { ADDRESSES, AREAS_RESPONSE, ESTIMATE, ESTIMATED_PRICE, PRICE } from '../test/fixtures';
import {
  ApiError,
  PRICE_TIMEOUT_MS,
  OPEN_GET_ATTEMPT_MS,
  REQUEST_TIMEOUT_MS,
  SEARCH_ATTEMPT_MS,
  getAddresses,
  getAreas,
  getInflation,
  getNearbySales,
  getNearbySchools,
  isAbort,
  isGated,
  request,
  requestEstimate,
  requestPrice,
  searchAddresses,
  searchKey,
  searchQueryFor,
} from './api';
import { CaptchaError, dropPass, ensurePass } from './captcha';

// The pass itself is captcha.test.ts's business; here it is whatever each test says.
jest.mock('./captcha', () => ({
  ...jest.requireActual('./captcha'),
  ensurePass: jest.fn(),
  dropPass: jest.fn(),
}));
const mockEnsurePass = ensurePass as jest.MockedFunction<typeof ensurePass>;
const mockDropPass = dropPass as jest.MockedFunction<typeof dropPass>;

const REQUEST: EstimateRequest = {
  district: 'SE15',
  postcode: 'SE15 4TQ',
  propertyType: 'terraced',
  bedrooms: 3,
  floorAreaSqm: 92,
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const mockFetch = jest.fn();

beforeEach(() => {
  mockFetch.mockReset();
  (global as unknown as { fetch: typeof fetch }).fetch = mockFetch as unknown as typeof fetch;
  // The gate is off unless a test turns it on.
  mockEnsurePass.mockReset();
  mockEnsurePass.mockResolvedValue(null);
  mockDropPass.mockReset();
});

/** Lets pending promise callbacks run, for tests on fake timers. */
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await Promise.resolve();
  }
}

afterEach(() => {
  jest.useRealTimers();
});

function headersOfCall(index: number): Record<string, string> {
  return (mockFetch.mock.calls[index][1] as RequestInit).headers as Record<string, string>;
}

describe('the success path', () => {
  it('loads the areas from same-origin /api', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, AREAS_RESPONSE));
    await expect(getAreas()).resolves.toEqual(AREAS_RESPONSE);
    expect(mockFetch).toHaveBeenCalledWith('/api/areas', expect.objectContaining({ signal: expect.anything() }));
    expect(headersOfCall(0)).toEqual({ Accept: 'application/json', 'X-Request-ID': expect.stringMatching(/^[0-9a-f]{16}$/) });
  });

  it('posts an estimate request as JSON, leaving the district for the server to derive', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, ESTIMATE));
    await expect(requestEstimate(REQUEST)).resolves.toEqual(ESTIMATE);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/estimate');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      postcode: 'SE15 4TQ',
      propertyType: 'terraced',
      bedrooms: 3,
      floorAreaSqm: 92,
      // Omitted by this caller; the server then values it today.
      saleDate: null,
    });
    expect(headersOfCall(0)).toEqual({
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-Request-ID': expect.stringMatching(/^[0-9a-f]{16}$/),
    });
  });
});

describe('failures', () => {
  it('turns a 404 into an ApiError carrying the server’s own words', async () => {
    mockFetch.mockResolvedValue(jsonResponse(404, { detail: 'We do not cover ZZ9 yet' }));
    const error = await requestEstimate(REQUEST).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).userMessage).toBe('We do not cover ZZ9 yet');
  });

  it('tells a route that does not exist from one that found nothing', async () => {
    const missing = async (status: number, body: unknown): Promise<boolean> => {
      mockFetch.mockResolvedValueOnce(jsonResponse(status, body));
      const error = (await getAddresses('CR0 1JN').catch((e: unknown) => e)) as ApiError;
      return error.missingRoute;
    };
    // FastAPI's and the dev proxy's own 404, nginx's HTML page, and a 405.
    await expect(missing(404, { detail: 'Not Found' })).resolves.toBe(true);
    await expect(missing(404, undefined)).resolves.toBe(true);
    await expect(missing(405, { detail: 'Method Not Allowed' })).resolves.toBe(true);
    // A route that exists and says why.
    await expect(missing(404, { detail: 'We could not find any homes we can price in ZZ1 1ZZ' })).resolves.toBe(false);
    await expect(missing(500, { detail: 'boom' })).resolves.toBe(false);
  });

  it('never shows a validation payload or a server error verbatim', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(422, { detail: [{ loc: ['body', 'postcode'], msg: 'bad' }] }));
    const invalid = (await getAreas().catch((e: unknown) => e)) as ApiError;
    expect(invalid.status).toBe(422);
    expect(invalid.userMessage).toBe('Some of those details did not look right. Check them and try again.');

    // An open GET tries a 5xx once more, so both attempts fail here.
    const broken = { ok: false, status: 500, json: async () => { throw new Error('not json'); } };
    mockFetch.mockResolvedValueOnce(broken).mockResolvedValueOnce(broken);
    const server = (await getAreas().catch((e: unknown) => e)) as ApiError;
    expect(server.status).toBe(500);
    expect(server.userMessage).toBe('Something went wrong on our side. Try again in a moment.');
  });

  it('turns a network failure into an ApiError', async () => {
    mockFetch.mockRejectedValue(new TypeError('Failed to fetch'));
    const error = (await getAreas().catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.userMessage).toMatch(/could not reach our data/);
  });

  it('gives up after the timeout and aborts the call', async () => {
    jest.useFakeTimers();
    let signal: AbortSignal | undefined;
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          signal = init.signal ?? undefined;
          signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );

    const pending = request('/areas').catch((e: unknown) => e);
    jest.advanceTimersByTime(REQUEST_TIMEOUT_MS - 1);
    expect(signal?.aborted).toBe(false);
    jest.advanceTimersByTime(1);

    const error = (await pending) as ApiError;
    expect(signal?.aborted).toBe(true);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.userMessage).toBe('This is taking longer than it should. Try again in a moment.');
  });

  it('times out while the body is still arriving, as an ApiError rather than a silent abort', async () => {
    jest.useFakeTimers();
    mockFetch.mockResolvedValue({ ok: true, status: 200, json: () => new Promise(() => {}) } as Response);

    const pending = request('/areas').catch((e: unknown) => e);
    await Promise.resolve();
    await Promise.resolve();
    jest.advanceTimersByTime(REQUEST_TIMEOUT_MS);

    const error = (await pending) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(isAbort(error)).toBe(false);
    expect(error.userMessage).toBe('This is taking longer than it should. Try again in a moment.');
  });

  it('turns a body that is not JSON into an ApiError', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError('Unexpected token <');
      },
    } as unknown as Response);
    const error = (await request('/areas').catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.userMessage).toMatch(/something went wrong on our side/i);
  });

  it('lets a caller cancel a call without it counting as an error', async () => {
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );
    const controller = new AbortController();
    const pending = getAreas(controller.signal).catch((e: unknown) => e);
    controller.abort();
    const error = await pending;
    expect(error).not.toBeInstanceOf(ApiError);
    expect(isAbort(error)).toBe(true);
  });
});

describe('the API key', () => {
  it('is never sent from the browser, on any call', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, ESTIMATE));
    await getAreas();
    await requestEstimate(REQUEST);
    await request('/areas', { headers: { 'X-API-Key': 'smuggled' } });

    mockFetch.mock.calls.forEach((_call, index) => {
      const names = Object.keys(headersOfCall(index)).map((name) => name.toLowerCase());
      expect(names).not.toContain('x-api-key');
    });
  });
});

describe('pricing a specific home', () => {
  it('asks for the homes at a postcode with the postcode in the query string', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, ADDRESSES));
    await expect(getAddresses(' CR0 1JN ')).resolves.toEqual(ADDRESSES);
    expect(mockFetch.mock.calls[0][0]).toBe('/api/addresses?postcode=CR0%201JN');
    expect(headersOfCall(0)).toEqual({ Accept: 'application/json', 'X-Request-ID': expect.stringMatching(/^[0-9a-f]{16}$/) });
  });

  it('searches addresses with the text and a limit of eight in the query string', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, ADDRESSES));
    await expect(searchAddresses(' 18 barclay rd ')).resolves.toEqual(ADDRESSES);
    expect(mockFetch.mock.calls[0][0]).toBe('/api/addresses/search?q=18%20barclay%20rd&limit=8');
    expect(headersOfCall(0)).toEqual({ Accept: 'application/json', 'X-Request-ID': expect.stringMatching(/^[0-9a-f]{16}$/) });
  });

  it('never sends an address search outside the server’s bounds', async () => {
    for (const q of ['', 'zz', '  ab  ', "a'b", '((( )))', 'a'.repeat(65)]) {
      const error = (await searchAddresses(q).catch((e: unknown) => e)) as ApiError;
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(422);
    }
    expect(mockFetch).not.toHaveBeenCalled();

    mockFetch.mockResolvedValue(jsonResponse(200, []));
    await searchAddresses('a'.repeat(64));
    await searchAddresses('abc');
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('sends characters the server refuses as spaces, so typing never earns a 422', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, ADDRESSES));
    await searchAddresses('Flat A (Ground Floor), 100% Smith_&_Sons\\ Road');
    expect(mockFetch.mock.calls[0][0]).toBe(
      '/api/addresses/search?q=' + encodeURIComponent('Flat A Ground Floor , 100 Smith Sons Road') + '&limit=8',
    );
  });

  it('reads the query bounds and normal form the server uses', () => {
    expect(searchQueryFor('  18   Barclay Rd, ')).toBe('18 Barclay Rd,');
    expect(searchQueryFor('ab')).toBeNull();
    expect(searchKey("St Mary's Rd,")).toBe('ST MARYS RD');
    expect(searchKey('st marys rd')).toBe(searchKey("St Mary\u2019s  Rd."));
  });

  it('turns a 429 into a quiet ApiError carrying Retry-After', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 429,
      headers: new Headers({ 'Retry-After': '3' }),
      json: async () => ({ detail: 'You are searching faster than we can keep up.' }),
    } as Response);
    const error = (await searchAddresses('barclay').catch((e: unknown) => e)) as ApiError;
    expect(error.rateLimited).toBe(true);
    expect(error.retryAfterS).toBe(3);
    expect(error.userMessage).toBe('One moment…');
    expect(error.retryable).toBe(false);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('says which failures are worth trying again', () => {
    expect(new ApiError(0, 'x').retryable).toBe(true);
    expect(new ApiError(503, 'x').retryable).toBe(true);
    expect(new ApiError(404, 'x', undefined, true).retryable).toBe(true);
    expect(new ApiError(404, 'We could not find any homes').retryable).toBe(false);
    expect(new ApiError(422, 'x').retryable).toBe(false);
  });

  it('sends an address search without waiting for a pass', async () => {
    mockEnsurePass.mockReturnValue(new Promise(() => {}));
    mockFetch.mockResolvedValue(jsonResponse(200, ADDRESSES));
    await expect(searchAddresses('barclay')).resolves.toEqual(ADDRESSES);
    expect(mockEnsurePass).not.toHaveBeenCalled();
    expect(headersOfCall(0)).not.toHaveProperty('X-HPLDN-Pass');
  });

  it('sends a stalled address search once more, within the overall timeout', async () => {
    jest.useFakeTimers();
    const signals: AbortSignal[] = [];
    mockFetch
      .mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            signals.push(init.signal as AbortSignal);
            init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
          }),
      )
      .mockResolvedValueOnce(jsonResponse(200, ADDRESSES));

    const pending = searchAddresses('barclay');
    jest.advanceTimersByTime(SEARCH_ATTEMPT_MS);
    await expect(pending).resolves.toEqual(ADDRESSES);
    expect(signals[0].aborted).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(2 * SEARCH_ATTEMPT_MS).toBeLessThanOrEqual(REQUEST_TIMEOUT_MS);
  });

  it('does not send an address search again after an answer, even a 5xx', async () => {
    for (const status of [429, 500, 503]) {
      mockFetch.mockReset();
      mockFetch.mockResolvedValue(jsonResponse(status, { detail: 'not now' }));
      expect(((await searchAddresses('barclay').catch((e: unknown) => e)) as ApiError).status).toBe(status);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    }
  });

  it('lets an address search be cancelled without it counting as an error', async () => {
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );
    const controller = new AbortController();
    const pending = searchAddresses('barclay', controller.signal).catch((e: unknown) => e);
    controller.abort();
    expect(isAbort(await pending)).toBe(true);
  });

  it('passes an estimated home’s price through unchanged', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, ESTIMATED_PRICE));
    const result = await requestPrice({ ...ESTIMATED_PRICE.address, saleDate: '2026-09-30' });
    expect(result).toEqual(ESTIMATED_PRICE);
    expect(result.epcSource).toBe('estimated');
    expect(result.estimatedFrom).toBe('6 detached houses on this street');
  });

  it('posts the address and sale date as JSON', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, PRICE));
    const req = { ...PRICE.address, saleDate: '2026-09-15' };
    await expect(requestPrice(req)).resolves.toEqual(PRICE);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/price');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      postnumber: '18',
      poststreet: 'BARCLAY ROAD',
      posttown: 'CROYDON',
      postcode: 'CR0 1JN',
      saleDate: '2026-09-15',
    });
    expect(headersOfCall(0)).not.toHaveProperty('X-API-Key');
  });

  it('sends a flat’s unit so the price is that flat’s, and leaves it out for a house', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, PRICE));
    await requestPrice({ ...PRICE.address, unit: 'FLAT 6', saleDate: '2026-09-15' });
    await requestPrice({ ...PRICE.address, unit: null, saleDate: '2026-09-15' });
    const bodies = mockFetch.mock.calls.map(([, init]) => JSON.parse((init as RequestInit).body as string));
    expect(bodies[0]).toMatchObject({ postnumber: '18', unit: 'FLAT 6', saleDate: '2026-09-15' });
    expect(bodies[1]).not.toHaveProperty('unit');
  });

  it('asks for the sales near a flat by its unit', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { sales: [], windowFrom: '2025-09-01', windowTo: '2026-09-01' }));
    await getNearbySales({ ...PRICE.address, unit: 'FLAT 6' });
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/nearby');
    expect(JSON.parse(init.body as string)).toEqual({
      postnumber: '18',
      poststreet: 'BARCLAY ROAD',
      posttown: 'CROYDON',
      postcode: 'CR0 1JN',
      unit: 'FLAT 6',
    });
  });

  it('asks for the schools near a home by its address, with the unit for a flat', async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(200, { primary: [], secondary: [], asOf: '2026-10-02', radiusM: 3000, source: 'Ofsted' }),
    );
    const schools = await getNearbySchools({ ...PRICE.address, unit: 'FLAT 6' });
    await getNearbySchools(PRICE.address);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/schools');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      postnumber: '18',
      poststreet: 'BARCLAY ROAD',
      posttown: 'CROYDON',
      postcode: 'CR0 1JN',
      unit: 'FLAT 6',
    });
    expect(JSON.parse((mockFetch.mock.calls[1][1] as RequestInit).body as string)).not.toHaveProperty('unit');
    expect(schools.radiusM).toBe(3000);
  });

  it('carries the server’s sentence for a date or record problem, but not a validation payload', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(422, { detail: 'Choose a sale date from 15/09/2026 onwards: we can only price a future sale.' }),
    );
    const dated = (await requestPrice({ ...PRICE.address, saleDate: '2026-09-14' }).catch((e: unknown) => e)) as ApiError;
    expect(dated.status).toBe(422);
    expect(dated.userMessage).toBe('Choose a sale date from 15/09/2026 onwards: we can only price a future sale.');

    mockFetch.mockResolvedValueOnce(jsonResponse(503, { detail: 'Pricing a specific home is not available right now. Try again later.' }));
    const down = (await requestPrice({ ...PRICE.address, saleDate: '2026-09-15' }).catch((e: unknown) => e)) as ApiError;
    expect(down.userMessage).toBe('Pricing a specific home is not available right now. Try again later.');
  });

  it('waits longer for a price than for other calls', async () => {
    jest.useFakeTimers();
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );
    const pending = requestPrice({ ...PRICE.address, saleDate: '2026-09-15' }).catch((e: unknown) => e);
    // The price waits for its pass before it is sent.
    await flush();
    jest.advanceTimersByTime(REQUEST_TIMEOUT_MS + 1);
    expect(mockFetch.mock.calls[0][1].signal.aborted).toBe(false);
    jest.advanceTimersByTime(PRICE_TIMEOUT_MS);
    const error = (await pending) as ApiError;
    expect(error.userMessage).toBe('This is taking longer than it should. Try again in a moment.');
  });
});

describe('the open GETs', () => {
  it('send one request for callers that ask at once, and one caller giving up cancels nothing', async () => {
    let answer: (response: Response) => void = () => undefined;
    mockFetch.mockReturnValue(
      new Promise<Response>((resolve) => {
        answer = resolve;
      }),
    );
    const leaving = new AbortController();
    const first = getInflation(leaving.signal);
    const second = getInflation();
    leaving.abort();
    answer(jsonResponse(200, { months: [] }));

    await expect(first).rejects.toHaveProperty('name', 'AbortError');
    await expect(second).resolves.toEqual({ months: [] });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect((mockFetch.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(false);
  });

  it('try once more after a failure that may pass, within the usual wait', async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: false, status: 502, json: async () => ({}) } as Response)
      .mockResolvedValueOnce(jsonResponse(200, AREAS_RESPONSE));
    await expect(getAreas()).resolves.toEqual(AREAS_RESPONSE);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('try once more after the first attempt times out, and give up at the usual timeout', async () => {
    jest.useFakeTimers();
    mockFetch.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );
    const pending = getAreas().catch((e: unknown) => e);
    jest.advanceTimersByTime(OPEN_GET_ATTEMPT_MS);
    await flush();
    expect(mockFetch).toHaveBeenCalledTimes(2);
    jest.advanceTimersByTime(OPEN_GET_ATTEMPT_MS);
    const error = (await pending) as ApiError;
    expect(2 * OPEN_GET_ATTEMPT_MS).toBe(REQUEST_TIMEOUT_MS);
    expect(error.userMessage).toBe('This is taking longer than it should. Try again in a moment.');
  });

  it('do not try again an answer that will not change', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(403, {}));
    await getAreas().catch(() => undefined);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('send afresh once the last answer is in', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { months: [] }));
    await getInflation();
    await getInflation();
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});

describe('the reCAPTCHA pass', () => {
  function captchaResponse(status: number, captcha: string | null, detail: string): Response {
    const headers = new Headers();
    if (captcha) {
      headers.set('X-HPLDN-Captcha', captcha);
    }
    return { ok: false, status, headers, json: async () => ({ detail }) } as Response;
  }

  it('knows which routes are gated, with or without a query', () => {
    for (const path of ['/addresses?postcode=CR0', '/price', '/estimate', '/nearby', '/schools']) {
      expect(isGated(path)).toBe(true);
    }
    // Address search is open, so a first search never waits for Google.
    for (const path of ['/addresses/search?q=abc&limit=8', '/areas', '/areas/SW11', '/areas/SW11/comparables?propertyType=flat', '/inflation', '/session']) {
      expect(isGated(path)).toBe(false);
    }
  });

  it('is sent on the gated routes only', async () => {
    mockEnsurePass.mockResolvedValue('pass-1');
    mockFetch.mockResolvedValue(jsonResponse(200, PRICE));
    await requestPrice({ ...PRICE.address, saleDate: '2026-09-15' });
    await getAreas();
    await getInflation();

    expect(headersOfCall(0)['X-HPLDN-Pass']).toBe('pass-1');
    expect(headersOfCall(1)).not.toHaveProperty('X-HPLDN-Pass');
    expect(headersOfCall(2)).not.toHaveProperty('X-HPLDN-Pass');
    expect(mockEnsurePass).toHaveBeenCalledTimes(1);
  });

  it('is dropped and fetched again once when the server says it is required', async () => {
    mockEnsurePass.mockResolvedValueOnce('stale').mockResolvedValueOnce('fresh');
    mockFetch
      .mockResolvedValueOnce(captchaResponse(403, 'required', 'Reload the page to keep using this.'))
      .mockResolvedValueOnce(jsonResponse(200, ADDRESSES));

    await expect(getAddresses('CR0 1JN')).resolves.toEqual(ADDRESSES);
    expect(mockDropPass).toHaveBeenCalledTimes(1);
    expect(headersOfCall(0)['X-HPLDN-Pass']).toBe('stale');
    expect(headersOfCall(1)['X-HPLDN-Pass']).toBe('fresh');
  });

  it('is not retried more than once', async () => {
    mockEnsurePass.mockResolvedValue('refused');
    mockFetch.mockResolvedValue(captchaResponse(403, 'required', 'Reload the page to keep using this.'));

    const error = (await requestEstimate(REQUEST).catch((e: unknown) => e)) as ApiError;
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(error.status).toBe(403);
    expect(error.captcha).toBe('required');
    expect(error.userMessage).toBe('Reload the page to keep using this.');
  });

  it('turns a visitor reCAPTCHA refuses into the server’s sentence, without calling the route', async () => {
    mockEnsurePass.mockRejectedValue(
      new CaptchaError(403, "We couldn't confirm this is a person. Reload the page and try again."),
    );
    const error = (await getAddresses('CR0 1JN').catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
    expect(error.captcha).toBe('low_score');
    expect(error.userMessage).toBe("We couldn't confirm this is a person. Reload the page and try again.");
    expect(error.retryable).toBe(false);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('treats a pass that cannot be had because nothing answered as worth retrying', async () => {
    mockEnsurePass.mockRejectedValue(new CaptchaError(0, 'We could not reach our data just now.'));
    const error = (await getAddresses('CR0 1JN').catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.retryable).toBe(true);
  });

  it('lets a call waiting for its pass be cancelled', async () => {
    mockEnsurePass.mockReturnValue(new Promise(() => {}));
    const controller = new AbortController();
    const pending = getAddresses('CR0 1JN', controller.signal).catch((e: unknown) => e);
    controller.abort();
    expect(isAbort(await pending)).toBe(true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('explains a plain 403, which is a blocked network', async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 403, json: async () => { throw new SyntaxError('text'); } });
    const error = (await getAreas().catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(403);
    expect(error.captcha).toBeNull();
    expect(error.userMessage).toMatch(/temporarily restricted because of unusual traffic/);
  });
});
