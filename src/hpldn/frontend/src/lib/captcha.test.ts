import {
  CaptchaError,
  dropPass,
  ensurePass,
  loadCaptchaConfig,
  primeCaptcha,
  resetCaptchaForTests,
  warmPass,
} from './captcha';

// setupTests.ts stubs primeCaptcha for the component tests; these test the real one (hoisted by babel-jest).
jest.unmock('./captcha');

const SITE_KEY = '6LcTestSiteKey-for-the-unit-tests_000000';
const CONFIG = { enabled: true, siteKey: SITE_KEY, action: 'session' };

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return { ok: status >= 200 && status < 300, status, headers: new Headers(headers), json: async () => body } as Response;
}

const REFUSED = { detail: "We couldn't confirm this is a person. Reload the page and try again." };

const mockFetch = jest.fn();
const execute = jest.fn();

/** Answers the config and session calls as the API would. */
function serve(config: unknown = CONFIG, session: Response = jsonResponse(200, { pass: 'pass-1', expiresIn: 1800 })): void {
  mockFetch.mockImplementation(async (url: string) => {
    if (url === '/api/session/config') {
      return jsonResponse(200, config);
    }
    if (url === '/api/session') {
      return session;
    }
    throw new Error('unexpected call to ' + url);
  });
}

function installGrecaptcha(): void {
  window.grecaptcha = { ready: (callback: () => void) => callback(), execute };
}

function scripts(): HTMLScriptElement[] {
  return Array.from(document.querySelectorAll<HTMLScriptElement>('script[src*="recaptcha"]'));
}

function callsTo(url: string): unknown[][] {
  return mockFetch.mock.calls.filter(([called]) => called === url);
}

/** Lets pending promise callbacks run. */
async function flush(): Promise<void> {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
}

beforeEach(() => {
  resetCaptchaForTests();
  scripts().forEach((script) => script.remove());
  delete window.grecaptcha;
  mockFetch.mockReset();
  execute.mockReset();
  execute.mockResolvedValue('google-token');
  (global as unknown as { fetch: typeof fetch }).fetch = mockFetch as unknown as typeof fetch;
});

describe('while the gate is off', () => {
  it('has no pass to give and loads nothing from Google', async () => {
    serve({ enabled: false, siteKey: '', action: 'session' });
    primeCaptcha();
    await expect(ensurePass()).resolves.toBeNull();
    expect(scripts()).toHaveLength(0);
    expect(callsTo('/api/session')).toHaveLength(0);
  });

  it('treats an API without the route as off', async () => {
    mockFetch.mockResolvedValue(jsonResponse(404, { detail: 'Not Found' }));
    await expect(loadCaptchaConfig()).resolves.toBeNull();
  });
});

describe('loading Google’s script', () => {
  it('waits for the first interaction, then loads it once with the site key from the API', async () => {
    serve();
    expect(scripts()).toHaveLength(0);

    primeCaptcha();
    primeCaptcha();
    await flush();
    expect(scripts()).toHaveLength(1);
    expect(scripts()[0].src).toBe('https://www.google.com/recaptcha/api.js?render=' + SITE_KEY);
    expect(callsTo('/api/session/config')).toHaveLength(1);

    // The page asks for a pass while the script is still arriving.
    const pending = ensurePass();
    installGrecaptcha();
    scripts()[0].dispatchEvent(new Event('load'));
    await expect(pending).resolves.toBe('pass-1');
    expect(scripts()).toHaveLength(1);
  });

  it('says what to do when the script is blocked, and tries again next time', async () => {
    serve();
    const pending = ensurePass().catch((e: unknown) => e);
    await flush();
    scripts()[0].dispatchEvent(new Event('error'));
    const error = (await pending) as CaptchaError;
    expect(error).toBeInstanceOf(CaptchaError);
    expect(error.status).toBe(0);
    expect(error.userMessage).toMatch(/could not load Google’s check/);

    installGrecaptcha();
    await expect(ensurePass()).resolves.toBe('pass-1');
  });
});

describe('warming the pass', () => {
  it('gets the pass on the first interaction, so the call that needs it does not wait', async () => {
    serve();
    installGrecaptcha();
    warmPass();
    warmPass();
    await flush();
    expect(callsTo('/api/session')).toHaveLength(1);
    await expect(ensurePass()).resolves.toBe('pass-1');
    expect(callsTo('/api/session')).toHaveLength(1);
  });

  it('swallows a refusal, tries only once per page, and leaves the real call to report it', async () => {
    serve(CONFIG, jsonResponse(403, REFUSED, { 'X-HPLDN-Captcha': 'low_score' }));
    installGrecaptcha();
    warmPass();
    await flush();
    warmPass();
    await flush();
    expect(callsTo('/api/session')).toHaveLength(1);
    await expect(ensurePass()).rejects.toBeInstanceOf(CaptchaError);
  });
});

describe('the pass', () => {
  beforeEach(installGrecaptcha);

  it('is bought with one reCAPTCHA token and then reused', async () => {
    serve();
    await expect(ensurePass()).resolves.toBe('pass-1');
    await expect(ensurePass()).resolves.toBe('pass-1');

    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith(SITE_KEY, { action: 'session' });
    const [[, init]] = callsTo('/api/session') as [[string, RequestInit]];
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ token: 'google-token' });
  });

  it('is shared by callers that ask at the same moment', async () => {
    serve();
    const passes = await Promise.all([ensurePass(), ensurePass(), ensurePass()]);
    expect(passes).toEqual(['pass-1', 'pass-1', 'pass-1']);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(callsTo('/api/session')).toHaveLength(1);
  });

  it('is kept in sessionStorage, so a reload in the same tab does not ask Google again', async () => {
    serve();
    await ensurePass();
    const stored = JSON.parse(window.sessionStorage.getItem('hpldn.pass') ?? 'null');
    expect(stored.pass).toBe('pass-1');

    // A reload: memory is gone, the tab's storage is not.
    const saved = window.sessionStorage.getItem('hpldn.pass');
    resetCaptchaForTests();
    window.sessionStorage.setItem('hpldn.pass', saved ?? '');
    await expect(ensurePass()).resolves.toBe('pass-1');
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it('is replaced a minute before it expires', async () => {
    serve(CONFIG, jsonResponse(200, { pass: 'short', expiresIn: 30 }));
    await expect(ensurePass()).resolves.toBe('short');
    await ensurePass();
    expect(execute).toHaveBeenCalledTimes(2);
  });

  it('is replaced after the server refuses it', async () => {
    serve();
    await ensurePass();
    dropPass();
    await ensurePass();
    expect(execute).toHaveBeenCalledTimes(2);
    expect(window.sessionStorage.getItem('hpldn.pass')).not.toBeNull();
  });

  it('is refused for a visitor Google scores as a bot, with the server’s sentence', async () => {
    serve(
      CONFIG,
      jsonResponse(403, { detail: "We couldn't confirm this is a person. Reload the page and try again." }),
    );
    const error = (await ensurePass().catch((e: unknown) => e)) as CaptchaError;
    expect(error).toBeInstanceOf(CaptchaError);
    expect(error.status).toBe(403);
    expect(error.userMessage).toBe("We couldn't confirm this is a person. Reload the page and try again.");
    expect(window.sessionStorage.getItem('hpldn.pass')).toBeNull();
  });

  it('runs reCAPTCHA once more when the browser gave an error token, as Google advises', async () => {
    const pass = jsonResponse(200, { pass: 'pass-1', expiresIn: 1800 });
    const browserError = jsonResponse(403, REFUSED, { 'X-HPLDN-Captcha': 'browser_error' });
    mockFetch.mockImplementation(async (url: string) =>
      url === '/api/session/config' ? jsonResponse(200, CONFIG) : callsTo('/api/session').length === 1 ? browserError : pass,
    );
    await expect(ensurePass()).resolves.toBe('pass-1');
    expect(execute).toHaveBeenCalledTimes(2);
    expect(callsTo('/api/session')).toHaveLength(2);
  });

  it('gives up after the second error token', async () => {
    serve(CONFIG, jsonResponse(403, REFUSED, { 'X-HPLDN-Captcha': 'browser_error' }));
    const error = (await ensurePass().catch((e: unknown) => e)) as CaptchaError;
    expect(error.status).toBe(403);
    expect(error.captcha).toBe('browser_error');
    expect(execute).toHaveBeenCalledTimes(2);
  });

  it('does not run reCAPTCHA again for a bot score', async () => {
    serve(CONFIG, jsonResponse(403, REFUSED, { 'X-HPLDN-Captcha': 'low_score' }));
    const error = (await ensurePass().catch((e: unknown) => e)) as CaptchaError;
    expect(error.captcha).toBe('low_score');
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it('asks for the settings again after a failed attempt', async () => {
    mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const error = (await ensurePass().catch((e: unknown) => e)) as CaptchaError;
    expect(error.status).toBe(0);

    serve();
    await expect(ensurePass()).resolves.toBe('pass-1');
    expect(callsTo('/api/session/config')).toHaveLength(2);
  });
});
