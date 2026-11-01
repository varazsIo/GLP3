/**
 * End-to-end checks that reCAPTCHA v3 is triggered, and what the page does with it.
 *
 * Run against the local stack (`playwright.config.ts`): the gate is on, and
 * `fixtures.ts` replaces Google's script with a stub whose tokens the fake
 * siteverify answers. Address search needs no pass (a first search must not
 * wait for Google), but the first touch of the address box gets one in the
 * background; the postcode lookup, `GET /api/addresses`, is the gated call
 * the UI specs watch. Three groups:
 *  - "wiring" specs also replace `POST /api/session` (and the address calls) with
 *    stubs, so they pin down what our page sends and how it reacts to each
 *    answer, whatever the server would say.
 *  - "end to end" specs stub nothing of ours: the token goes through nginx to
 *    the API, which asks the fake Google, and the pass it hands out must open
 *    the gated routes. Only Google's verdict is chosen by the test.
 *  - "live" specs use Google's real script and the real site key. They run only
 *    with `E2E_LIVE_GOOGLE=1`, against a stack started without `--fake-google`
 *    and with a site key that accepts localhost; the last one also needs Google
 *    to score the browser as a person, and says which of the two failed.
 *  - "API" specs call `/api/session/config` and `/api/session` directly: the
 *    contract, refused bodies, every way of faking a pass, Google's verdicts,
 *    a client's own API key and address headers, and the session zone.
 */
import { expect, RECAPTCHA_SCRIPT, test } from '../fixtures';
import {
  BROWSER_UA,
  apiLogLine,
  benchmarkIp,
  earnPass,
  expectMatchesOpenApi,
  expectNoEcho,
  loggedAddress,
  newVisitor,
  rawGet,
  requestId,
  ADDRESS_POSTCODE,
  signPass,
  stackSetting,
  tags,
} from '../support/api';
import type { Page, Request, Response } from '@playwright/test';

const STUB_PASS = 'stub-pass.stub-signature';
const LIVE_GOOGLE = process.env.E2E_LIVE_GOOGLE === '1';
// The API's own 403 detail (an ASCII apostrophe) or the page's fallback (a typographic one).
const REFUSED = /We couldn[’']t confirm this is a person/;

/** Reads the gate's settings the way the page does, as this test's visitor. */
async function captchaConfig(page: Page, visitorIp: string): Promise<{ enabled: boolean; siteKey: string; action: string }> {
  const response = await page.request.get('/api/session/config', { headers: { 'CF-Connecting-IP': visitorIp } });
  expect(response.status()).toBe(200);
  return response.json();
}

/** Types in the address box, the first interaction that makes the page load reCAPTCHA. */
async function touchAddressSearch(page: Page, text = '18 Barclay'): Promise<void> {
  const form = page.getByRole('form', { name: 'Home to price' });
  await form.getByRole('combobox', { name: 'Address' }).fill(text);
}

/** Waits for the page's `POST /api/session`. */
function sessionRequest(page: Page): Promise<Request> {
  return page.waitForRequest(
    (request) => request.method() === 'POST' && new URL(request.url()).pathname === '/api/session',
  );
}

/** Waits for the answer to the page's `POST /api/session`. */
function sessionResponse(page: Page): Promise<Response> {
  return page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/session',
  );
}

/** Waits for the answer to the page's next address search. */
function searchResponse(page: Page): Promise<Response> {
  return page.waitForResponse((response) => new URL(response.url()).pathname === '/api/addresses/search');
}

/** True for the page's postcode lookup, `GET /api/addresses`, the gated call these specs watch. */
function isLookup(url: string | URL): boolean {
  return new URL(url).pathname === '/api/addresses';
}

/** Waits for the answer to the page's next postcode lookup. */
function lookupResponse(page: Page): Promise<Response> {
  return page.waitForResponse((response) => isLookup(response.url()));
}

test.describe('wiring, with our session and search stubbed', () => {
  test.skip(LIVE_GOOGLE, 'E2E_LIVE_GOOGLE=1 runs the live specs only');

  test('opening the page loads no reCAPTCHA and asks for no pass', tags('valid', ['/api/session', '/api/session/config'], { ui: true }), async ({ page }) => {
    const googleRequests: string[] = [];
    const sessionCalls: string[] = [];
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.hostname.endsWith('google.com') && url.pathname.startsWith('/recaptcha')) {
        googleRequests.push(request.url());
      }
      if (url.pathname === '/api/session') {
        sessionCalls.push(request.method());
      }
    });
    const areas = page.waitForRequest((request) => new URL(request.url()).pathname === '/api/areas');
    await page.goto('/');
    const areasRequest = await areas;

    expect(await areasRequest.headerValue('x-hpldn-pass')).toBeNull();
    await page.waitForLoadState('networkidle');
    expect(googleRequests).toEqual([]);
    expect(sessionCalls).toEqual([]);
    await expect(page.locator('.grecaptcha-badge')).toHaveCount(0);
  });

  test('touching the address search runs execute with the site key and action', tags('valid', ['/api/session', '/api/session/config'], { ui: true }), async ({
    page,
    visitorIp,
    googleStub,
  }) => {
    await page.route('**/api/session', (route) =>
      route.fulfill({ status: 200, json: { pass: STUB_PASS, expiresIn: 1800 } }),
    );
    await page.route('**/api/addresses/search*', (route) => route.fulfill({ status: 200, json: [] }));
    const config = await captchaConfig(page, visitorIp);
    await page.goto('/');

    const script = page.waitForRequest(RECAPTCHA_SCRIPT);
    const session = sessionRequest(page);
    await touchAddressSearch(page);

    const scriptUrl = new URL((await script).url());
    expect(scriptUrl.searchParams.get('render')).toBe(config.siteKey);
    const request = await session;
    expect(request.postDataJSON()).toEqual({ token: 'e2e-ok' });
    expect(await googleStub.executed(page)).toEqual([{ siteKey: config.siteKey, action: config.action }]);
  });

  test('a search carries no pass, a gated call carries it, and one check serves several calls', tags('valid', ['/api/session', '/api/addresses/search', '/api/addresses'], { ui: true }), async ({ page }) => {
    let sessionCalls = 0;
    await page.route('**/api/session', (route) => {
      sessionCalls += 1;
      return route.fulfill({ status: 200, json: { pass: STUB_PASS, expiresIn: 1800 } });
    });
    await page.route('**/api/addresses/search*', (route) => route.fulfill({ status: 200, json: [] }));
    await page.route(isLookup, (route) => route.fulfill({ status: 200, json: [] }));
    await page.goto('/');

    const search = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/addresses/search');
    await touchAddressSearch(page, '18 Barclay');
    expect(await (await search).headerValue('x-hpldn-pass')).toBeNull();

    for (const postcode of [ADDRESS_POSTCODE, 'SW11 1AD']) {
      const lookup = page.waitForRequest((r) => isLookup(r.url()) && new URL(r.url()).searchParams.get('postcode') === postcode);
      await touchAddressSearch(page, postcode);
      expect(await (await lookup).headerValue('x-hpldn-pass'), postcode).toBe(STUB_PASS);
    }
    expect(sessionCalls).toBe(1);
  });

  test('a visitor Google scores as a bot can still search, but a gated call is refused with a message', tags('invalid', ['/api/session', '/api/addresses/search'], { ui: true }), async ({ page }) => {
    await page.route('**/api/session', (route) =>
      route.fulfill({
        status: 403,
        headers: { 'X-HPLDN-Captcha': 'low_score' },
        json: { detail: 'We couldn’t confirm this is a person. Reload the page and try again.' },
      }),
    );
    await page.route('**/api/addresses/search*', (route) => route.fulfill({ status: 200, json: [] }));
    let lookups = 0;
    await page.route(isLookup, (route) => {
      lookups += 1;
      return route.fulfill({ status: 200, json: [] });
    });
    await page.goto('/');

    const session = sessionRequest(page);
    const search = searchResponse(page);
    await touchAddressSearch(page);
    await session;
    expect((await search).status()).toBe(200);
    // The background check's refusal is not shown: nothing has needed the pass yet.
    await expect(page.getByText(REFUSED)).toHaveCount(0);

    await touchAddressSearch(page, ADDRESS_POSTCODE);
    await expect(page.getByText(REFUSED)).toBeVisible();
    expect(lookups).toBe(0);
  });

  test('a browser_error refusal runs execute a second time', tags('invalid', ['/api/session'], { ui: true }), async ({ page, googleStub }) => {
    let sessionCalls = 0;
    await page.route('**/api/session', (route) => {
      sessionCalls += 1;
      return sessionCalls === 1
        ? route.fulfill({
            status: 403,
            headers: { 'X-HPLDN-Captcha': 'browser_error' },
            json: { detail: 'We couldn’t confirm this is a person. Reload the page and try again.' },
          })
        : route.fulfill({ status: 200, json: { pass: STUB_PASS, expiresIn: 1800 } });
    });
    await page.route(isLookup, (route) => route.fulfill({ status: 200, json: [] }));
    await page.goto('/');

    const lookup = page.waitForRequest((r) => isLookup(r.url()));
    await touchAddressSearch(page, ADDRESS_POSTCODE);
    expect(await (await lookup).headerValue('x-hpldn-pass')).toBe(STUB_PASS);
    expect(sessionCalls).toBe(2);
    expect(await googleStub.executed(page)).toHaveLength(2);
  });

  test('a pass the server refuses as stale is dropped and a new check runs', tags('invalid', ['/api/session', '/api/addresses'], { ui: true }), async ({ page }) => {
    let sessionCalls = 0;
    await page.route('**/api/session', (route) => {
      sessionCalls += 1;
      return route.fulfill({ status: 200, json: { pass: 'pass-' + sessionCalls, expiresIn: 1800 } });
    });
    const passesSeen: Array<string | null> = [];
    await page.route(isLookup, (route) => {
      const pass = route.request().headers()['x-hpldn-pass'] ?? null;
      passesSeen.push(pass);
      return pass === 'pass-1'
        ? route.fulfill({
            status: 403,
            headers: { 'X-HPLDN-Captcha': 'required' },
            json: { detail: 'Pass required' },
          })
        : route.fulfill({ status: 200, json: [] });
    });
    await page.goto('/');

    await touchAddressSearch(page, ADDRESS_POSTCODE);
    await expect.poll(() => passesSeen).toEqual(['pass-1', 'pass-2']);
    expect(sessionCalls).toBe(2);
  });
});

test.describe('end to end, through nginx and the API to the fake Google', () => {
  test.skip(LIVE_GOOGLE, 'E2E_LIVE_GOOGLE=1 runs the live specs only');

  test('a visitor the fake Google scores as a bot can search, but is shown the message and sends no gated call', tags('redteam', ['/api/session', '/api/addresses/search'], { ui: true }), async ({
    page,
    googleStub,
  }) => {
    googleStub.tokens('e2e-low');
    const lookups: string[] = [];
    page.on('request', (request) => {
      if (isLookup(request.url())) {
        lookups.push(request.url());
      }
    });
    await page.goto('/');

    const session = sessionResponse(page);
    const search = searchResponse(page);
    await touchAddressSearch(page);
    const response = await session;
    expect(response.status()).toBe(403);
    expect(response.headers()['x-hpldn-captcha']).toBe('low_score');
    expect(response.headers()['x-hpldn-reject']).toBeUndefined();
    expect((await search).status()).toBe(200);

    await touchAddressSearch(page, ADDRESS_POSTCODE);
    await expect(page.getByText(REFUSED)).toBeVisible();
    expect(lookups).toEqual([]);
  });

  test('a first search is answered without waiting for the reCAPTCHA check', tags('valid', ['/api/addresses/search'], { ui: true }), async ({ page }) => {
    // The check never finishes: the search must not wait for it.
    await page.route('**/api/session', () => new Promise<void>(() => {}));
    await page.goto('/');

    const search = searchResponse(page);
    await touchAddressSearch(page);
    const answer = await search;
    expect(answer.status()).toBe(200);
    expect(await answer.request().headerValue('x-hpldn-pass')).toBeNull();
    expect(((await answer.json()) as unknown[]).length).toBeGreaterThan(0);
  });

  test('a browser_error verdict runs execute once more, and the second token earns the pass', tags('redteam', ['/api/session', '/api/addresses'], { ui: true }), async ({
    page,
    googleStub,
  }) => {
    googleStub.tokens('e2e-browser-error', 'e2e-ok');
    const statuses: number[] = [];
    page.on('response', (response) => {
      if (response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/session') {
        statuses.push(response.status());
      }
    });
    await page.goto('/');

    const lookup = lookupResponse(page);
    await touchAddressSearch(page, ADDRESS_POSTCODE);
    const response = await lookup;

    expect(response.status()).toBe(200);
    expect(await response.request().headerValue('x-hpldn-pass')).toBeTruthy();
    expect(statuses).toEqual([403, 200]);
    expect(await googleStub.executed(page)).toHaveLength(2);
  });

  test('a gated call carries the pass the API signed, and the API accepts it', tags('valid', ['/api/session', '/api/addresses'], { ui: true }), async ({ page }) => {
    await page.goto('/');

    const session = sessionResponse(page);
    const lookup = lookupResponse(page);
    await touchAddressSearch(page, ADDRESS_POSTCODE);
    const sessionAnswer = await session;
    expect(sessionAnswer.status()).toBe(200);
    const body = (await sessionAnswer.json()) as { pass: string; expiresIn: number };
    expect(body.expiresIn).toBe(1800);

    const lookupAnswer = await lookup;
    expect(await lookupAnswer.request().headerValue('x-hpldn-pass')).toBe(body.pass);
    expect(lookupAnswer.status()).toBe(200);
  });

  test('when Google cannot be reached, the visitor gets the short fail-open pass', tags('redteam', ['/api/session', '/api/addresses'], { ui: true }), async ({ page, googleStub }) => {
    googleStub.tokens('e2e-down');
    await page.goto('/');

    const session = sessionResponse(page);
    const lookup = lookupResponse(page);
    await touchAddressSearch(page, ADDRESS_POSTCODE);
    const sessionAnswer = await session;
    expect(sessionAnswer.status()).toBe(200);
    // guard.yaml captcha.fail_open_ttl: 5m.
    expect(((await sessionAnswer.json()) as { expiresIn: number }).expiresIn).toBe(300);
    expect((await lookup).status()).toBe(200);
  });
});

test.describe('live, with Google’s real script', () => {
  test.skip(!LIVE_GOOGLE, 'Set E2E_LIVE_GOOGLE=1 and start the stack without --fake-google');
  test.use({ liveGoogle: true });

  test('touching the address search loads Google’s script for the site key and shows its badge', tags('valid', ['/api/session/config'], { ui: true }), async ({
    page,
    visitorIp,
  }) => {
    const config = await captchaConfig(page, visitorIp);
    expect(config.siteKey).toMatch(/^6L[\w-]{38}$/);
    await page.goto('/');
    await expect(page.locator('.grecaptcha-badge')).toHaveCount(0);

    const script = page.waitForResponse(RECAPTCHA_SCRIPT);
    await touchAddressSearch(page);
    const response = await script;

    expect(response.status()).toBe(200);
    expect(new URL(response.url()).searchParams.get('render')).toBe(config.siteKey);
    await expect(page.locator('.grecaptcha-badge')).toBeAttached();
    expect(await page.evaluate(() => typeof window.grecaptcha?.execute)).toBe('function');
    expect(await page.evaluate(() => Boolean(document.querySelector('iframe[src*="recaptcha/api2/anchor"]')))).toBe(
      true,
    );
  });

  test('execute runs and its token is posted to /api/session', tags('valid', ['/api/session'], { ui: true }), async ({ page }) => {
    await page.goto('/');
    const session = sessionRequest(page);
    await touchAddressSearch(page);
    const request = await session;

    const { token } = request.postDataJSON() as { token: string };
    expect(typeof token).toBe('string');
    // A real token is a long opaque string, not empty.
    expect(token.length).toBeGreaterThan(20);
  });

  test('Google’s answer lets a gated call through on localhost', tags('valid', ['/api/session', '/api/addresses'], { ui: true }), async ({ page }) => {
    await page.goto('/');
    const session = sessionResponse(page);
    const lookup = lookupResponse(page);
    await touchAddressSearch(page, ADDRESS_POSTCODE);
    const response = await session;

    const reason = response.headers()['x-hpldn-captcha'];
    expect(
      response.status(),
      reason === 'browser_error'
        ? 'browser_error: grecaptcha.execute failed, the site key’s domain list lacks this host (localhost)'
        : reason === 'low_score'
          ? 'low_score: Google scored this headless browser as a bot, or the token was refused'
          : 'POST /api/session did not answer 200',
    ).toBe(200);
    const body = await response.json();
    expect(typeof body.pass).toBe('string');
    expect(body.expiresIn).toBeGreaterThan(0);
    expect((await lookup).status()).toBe(200);
  });
});

test.describe('API, called directly', () => {
  test.skip(LIVE_GOOGLE, 'E2E_LIVE_GOOGLE=1 runs the live specs only');

  // The gated GET these specs open with a pass (the search itself needs none).
  const GATED_GET = `/api/addresses?postcode=${encodeURIComponent(ADDRESS_POSTCODE)}`;
  const SEARCH = '/api/addresses/search?q=18+Barclay';

  test(
    'an address search needs no pass',
    tags('valid', ['/api/addresses/search']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const response = await api.get(SEARCH);
      expect(response.status()).toBe(200);
      expect(response.headers()['x-hpldn-captcha']).toBeUndefined();
      await api.dispose();
    },
  );

  test(
    'the config names the site key and action, and e2e-ok earns a 30-minute pass the gated routes accept',
    tags('valid', ['/api/session/config', '/api/session', '/api/addresses']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const config = await api.get('/api/session/config');
      expect(config.status()).toBe(200);
      const configBody = await config.json();
      expectMatchesOpenApi('GET', '/session/config', configBody);
      expect(configBody).toEqual({ enabled: true, siteKey: expect.stringMatching(/^6L[\w-]{38}$/), action: 'session' });

      const session = await api.post('/api/session', { data: { token: 'e2e-ok' } });
      expect(session.status()).toBe(200);
      const body = (await session.json()) as { pass: string; expiresIn: number };
      expectMatchesOpenApi('POST', '/session', body);
      // guard.yaml captcha.pass_ttl: 30m.
      expect(body.expiresIn).toBe(1800);
      expect(body.pass).toMatch(/^[\w-]+\.[\w-]+$/);
      const payload = JSON.parse(Buffer.from(body.pass.split('.')[0], 'base64url').toString('utf8'));
      expect(Object.keys(payload).sort()).toEqual(['exp', 'iph', 'v']);
      expect(payload.exp - Date.now() / 1000).toBeGreaterThan(1700);

      // Good for several calls from the same address.
      for (let i = 0; i < 3; i += 1) {
        expect((await api.get(GATED_GET, { headers: { 'X-HPLDN-Pass': body.pass } })).status()).toBe(200);
      }
      await api.dispose();
    },
  );

  test(
    'session bodies that are not one short token are 422s that do not repeat them',
    tags('invalid', ['/api/session']),
    async ({ playwright, baseURL }) => {
      const cases: Array<[string, unknown, string[]]> = [
        ['empty object', {}, []],
        ['empty token', { token: '' }, []],
        ['a number', { token: 12345678 }, ['12345678']],
        ['an extra field', { token: 'e2e-ok', admin: 'yes-please' }, ['yes-please']],
        ['too long', { token: 'T'.repeat(8_193) }, ['TTTTTTTTTTTT']],
      ];
      // The session zone allows a burst of 5 a visitor: a new one every 4 calls.
      for (let start = 0; start < cases.length; start += 4) {
        const { api } = await newVisitor(playwright, baseURL);
        for (const [name, body, sent] of cases.slice(start, start + 4)) {
          const response = await api.post('/api/session', { data: body });
          expect(response.status(), name).toBe(422);
          await expectNoEcho(response, sent);
        }
        await api.dispose();
      }
    },
  );

  test(
    'when /api/session/config or /api/session fails, search still works, and a gated call says it cannot reach the data and is not sent',
    tags('invalid', ['/api/session/config', '/api/session', '/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      let lookups = 0;
      page.on('request', (request) => {
        if (isLookup(request.url())) {
          lookups += 1;
        }
      });
      await page.route('**/api/session/config', (route) => route.fulfill({ status: 500, json: { detail: 'boom' } }));
      await page.goto('/');
      const search = searchResponse(page);
      await touchAddressSearch(page);
      expect((await search).status()).toBe(200);
      await touchAddressSearch(page, ADDRESS_POSTCODE);
      await expect(page.getByText('We could not reach our data just now. Check your connection and try again.')).toBeVisible();

      await page.unroute('**/api/session/config');
      await page.route('**/api/session', (route) => route.fulfill({ status: 503, json: { detail: 'Checks are paused.' } }));
      await page.reload();
      await touchAddressSearch(page, ADDRESS_POSTCODE);
      await expect(page.getByText('Checks are paused.')).toBeVisible();
      await expect(page.getByText('boom')).toHaveCount(0);
      expect(lookups).toBe(0);
    },
  );

  test(
    'a forged, tampered, expired, oversized or other-address pass is refused with "required"',
    tags('redteam', ['/api/session', '/api/addresses', '/api/price']),
    async ({ playwright, baseURL }) => {
      const owner = await newVisitor(playwright, baseURL);
      const pass = await earnPass(owner.api);
      const [payload, signature] = pass.split('.');
      const claims = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
      const reencode = (value: object) => Buffer.from(JSON.stringify(value)).toString('base64url');
      const now = Date.now() / 1000;

      // The helper signs as the API does: a fresh one is accepted, so the refusals below are about the pass.
      expect((await owner.api.get(GATED_GET, { headers: { 'X-HPLDN-Pass': signPass(owner.ip, now + 600) } })).status()).toBe(200);

      const forged: Array<[string, string]> = [
        ['random', `${reencode({ v: 1, exp: now + 600, iph: '0000000000000000' })}.${'A'.repeat(43)}`],
        ['no signature', payload],
        ['empty signature', `${payload}.`],
        ['tampered payload', `${reencode({ ...claims, exp: claims.exp + 86_400 })}.${signature}`],
        ['tampered signature', `${payload}.${signature.slice(0, -2)}${signature.endsWith('AA') ? 'BB' : 'AA'}`],
        ['expired', signPass(owner.ip, now - 5)],
        ['other version', `${reencode({ ...claims, v: 2 })}.${signature}`],
        ['oversized', `${payload}.${signature}${'x'.repeat(600)}`],
        ['not ascii', `${payload}.${signature.slice(0, -1)}é`],
      ];
      for (const [name, forgedPass] of forged) {
        const response = await owner.api.get(GATED_GET, { headers: { 'X-HPLDN-Pass': forgedPass } });
        expect(response.status(), name).toBe(403);
        expect(response.headers()['x-hpldn-captcha'], name).toBe('required');
      }

      // Replayed from another address: refused there, still good for its owner.
      const thief = await newVisitor(playwright, baseURL);
      for (const [method, url, data] of [['GET', GATED_GET, undefined], ['POST', '/api/price', { postnumber: '18' }]] as const) {
        const response = await thief.api.fetch(url, { method, data, headers: { 'X-HPLDN-Pass': pass } });
        expect(response.status(), `${method} ${url} from another address`).toBe(403);
        expect(response.headers()['x-hpldn-captcha']).toBe('required');
      }
      expect((await owner.api.get(GATED_GET, { headers: { 'X-HPLDN-Pass': pass } })).status()).toBe(200);
      await owner.api.dispose();
      await thief.api.dispose();
    },
  );

  test(
    'Google’s verdicts: e2e-low and unknown tokens are refused, e2e-browser-error asks for one more try, e2e-down fails open for 5 minutes',
    tags('redteam', ['/api/session']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      for (const [token, status, verdict] of [
        ['e2e-low', 403, 'low_score'],
        ['e2e-browser-error', 403, 'browser_error'],
        ['a-token-google-never-issued', 403, 'low_score'],
      ] as const) {
        const response = await api.post('/api/session', { data: { token } });
        expect(response.status(), token).toBe(status);
        expect(response.headers()['x-hpldn-captcha'], token).toBe(verdict);
        expect(response.headers()['x-hpldn-reject'], token).toBeUndefined();
        expect(await response.text(), token).not.toContain('pass');
      }
      const down = await api.post('/api/session', { data: { token: 'e2e-down' } });
      expect(down.status()).toBe(200);
      // guard.yaml captcha.fail_open_ttl: 5m.
      expect(((await down.json()) as { expiresIn: number }).expiresIn).toBe(300);
      await api.dispose();
    },
  );

  test(
    'a client’s X-API-Key, even the real one, opens nothing: no pass, no hidden routes',
    tags('redteam', ['/api/addresses', '/api/properties', '/api/session']),
    async ({ playwright, baseURL }) => {
      for (const key of [stackSetting('HPLDN_API_KEY'), 'not-the-key']) {
        const { api } = await newVisitor(playwright, baseURL, { headers: { 'X-API-Key': key } });
        const gated = await api.get(GATED_GET);
        expect(gated.status()).toBe(403);
        expect(gated.headers()['x-hpldn-captcha']).toBe('required');
        expect((await api.get('/api/properties')).status()).toBe(404);
        expect((await api.get('/api/addresses/search/metrics')).status()).toBe(404);
        // With a pass, a wrong key still works: nginx sends its own.
        const pass = await earnPass(api);
        expect((await api.get(GATED_GET, { headers: { 'X-HPLDN-Pass': pass } })).status()).toBe(200);
        await api.dispose();
      }
    },
  );

  test(
    'CF-Connecting-IP is believed only from the trusted hop; X-Real-IP and X-Forwarded-For never',
    tags('redteam', ['/api/session/config', '/api/addresses']),
    async ({ baseURL, playwright }) => {
      // From 127.0.0.2, which the local nginx does not trust (production trusts only
      // Cloudflare's ranges): the header is ignored and the peer is the client.
      const spoofed = benchmarkIp();
      const untrusted = requestId('untrusted');
      const answer = await rawGet(baseURL, '/api/session/config', { 'CF-Connecting-IP': spoofed, 'X-Request-ID': untrusted }, '127.0.0.2');
      expect(answer.status).toBe(200);
      expect(loggedAddress(await apiLogLine(untrusted))).toBe('127.0.0.2');

      // From the trusted hop, the visitor is CF-Connecting-IP, whatever else the client adds.
      const visitor = benchmarkIp();
      const trusted = requestId('trusted');
      await rawGet(baseURL, '/api/session/config', {
        'CF-Connecting-IP': visitor,
        'X-Real-IP': '203.0.113.9',
        'X-Forwarded-For': '203.0.113.9, 10.0.0.1',
        'True-Client-IP': '203.0.113.9',
        'X-Request-ID': trusted,
      });
      expect(loggedAddress(await apiLogLine(trusted))).toBe(visitor);

      // So a pass cannot follow its owner's address through a forged header from elsewhere.
      const owner = await newVisitor(playwright, baseURL);
      const pass = await earnPass(owner.api);
      const stolen = await rawGet(baseURL, GATED_GET, { 'CF-Connecting-IP': owner.ip, 'X-Real-IP': owner.ip, 'X-HPLDN-Pass': pass }, '127.0.0.2');
      expect(stolen.status).toBe(403);
      await owner.api.dispose();
    },
  );

  test(
    'GET or other methods on /api/session, and POST on the config, are refused',
    tags('redteam', ['/api/session', '/api/session/config']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      for (const method of ['GET', 'PUT', 'DELETE']) {
        expect((await api.fetch('/api/session', { method })).status(), `${method} /api/session`).toBe(403);
      }
      for (const method of ['POST', 'PUT', 'DELETE']) {
        expect((await api.fetch('/api/session/config', { method, data: {} })).status(), `${method} config`).toBe(403);
      }
      await api.dispose();
    },
  );

  test(
    'a body over 16 kB is a 413, and a burst of checks meets the session zone’s 429',
    tags('redteam', ['/api/session']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL, { userAgent: BROWSER_UA });
      expect((await api.post('/api/session', { data: { token: 'T'.repeat(17_000) } })).status()).toBe(413);
      const statuses: number[] = [];
      for (let i = 0; i < 9; i += 1) {
        statuses.push((await api.post('/api/session', { data: { token: 'e2e-ok' } })).status());
      }
      // guard.yaml rate_limits.nginx: the session zone's burst of 5, then 10 a minute.
      expect(statuses.filter((status) => status === 200).length).toBeGreaterThanOrEqual(4);
      expect(statuses).toContain(429);
      await api.dispose();
    },
  );
});
