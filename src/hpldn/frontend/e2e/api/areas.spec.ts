/**
 * `AreasProvider` (the home page's "Explore an area" and the yield
 * calculator's area figures): `GET /api/areas`, and the open per-area routes
 * nginx exposes beside it, `GET /api/areas/{code}` and
 * `GET /api/areas/{code}/comparables`, which no page calls today but anyone can.
 *
 * Also home to the edge checks every `/api` route shares: method tampering,
 * paths around the whitelist, scripted user agents, a client's own
 * `X-API-Key`, and the security headers.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '../fixtures';
import type { Page, Route } from '@playwright/test';
import {
  BROWSER_UA,
  HOSTILE_STRINGS,
  VPS_DIR,
  XSS_PAYLOADS,
  apiResponse,
  benchmarkIp,
  expectMatchesOpenApi,
  expectNoEcho,
  expectNoScriptRan,
  fetchAsVisitor,
  formatGBP,
  newVisitor,
  rawGet,
  tags,
  watchForScripts,
} from '../support/api';

interface Area {
  code: string;
  name: string;
  averagePrice: number;
  salesCount: number;
}

const DEFAULT_AREA = 'SW11';
const COMPARABLES = '/api/areas/SW11/comparables?propertyType=flat&floorAreaSqm=70';

/** The Content-Security-Policy production's security_headers.conf sends. */
const PRODUCTION_CSP = /Content-Security-Policy "([^"]+)"/.exec(
  readFileSync(path.join(VPS_DIR, 'nginx/security_headers.conf'), 'utf8'),
)?.[1];

/** The "Explore an area" section. */
function areasSection(page: Page) {
  return page.locator('section#areas');
}

test.describe('valid', () => {
  test(
    'the page asks for /api/areas once, without a pass, and shows the default area as the API gave it',
    tags('valid', ['/api/areas'], { ui: true }),
    async ({ page }) => {
      const calls: string[] = [];
      page.on('request', (request) => {
        if (new URL(request.url()).pathname === '/api/areas') {
          calls.push(request.method());
        }
      });
      const areasRequest = page.waitForRequest((request) => new URL(request.url()).pathname === '/api/areas');
      const areas = apiResponse(page, 'GET', '/areas');
      await page.goto('/');

      const request = await areasRequest;
      expect(request.method()).toBe('GET');
      expect(new URL(request.url()).search).toBe('');
      expect(request.postData()).toBeNull();
      expect(await request.headerValue('x-hpldn-pass')).toBeNull();

      const response = await areas;
      expect(response.status()).toBe(200);
      const body = (await response.json()) as { areas: Area[]; window: { latestSale: string } };
      expectMatchesOpenApi('GET', '/areas', body);
      expect(body.areas.length).toBeGreaterThan(100);

      const area = body.areas.find((candidate) => candidate.code === DEFAULT_AREA);
      expect(area, `${DEFAULT_AREA} in /api/areas`).toBeTruthy();
      const section = areasSection(page);
      await expect(section.getByRole('heading', { level: 3, name: `${area!.code} · ${area!.name}` })).toBeVisible();
      await expect(section.getByText(formatGBP(area!.averagePrice), { exact: true })).toBeVisible();
      await expect(section.getByText(new RegExp(`^${area!.salesCount.toLocaleString('en-GB')} sales?, `))).toBeVisible();

      // Choosing another area shows its figures from the same answer: no second call.
      const other = body.areas.find((candidate) => candidate.code === 'SE15') ?? body.areas[1];
      const search = section.getByRole('combobox', { name: 'Area' });
      await search.fill(other.code);
      await section.getByRole('option', { name: new RegExp(`^${other.code} · `) }).first().click();
      await expect(section.getByRole('heading', { level: 3, name: `${other.code} · ${other.name}` })).toBeVisible();
      await expect(section.getByText(formatGBP(other.averagePrice), { exact: true })).toBeVisible();
      expect(calls).toEqual(['GET']);
    },
  );

  test(
    'GET /api/areas/{code} answers one area, in either case, equal to its /api/areas entry',
    tags('valid', ['/api/areas/sw11', '/api/areas']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const all = (await (await api.get('/api/areas')).json()) as { areas: Area[] };
      const response = await api.get('/api/areas/sw11');
      expect(response.status()).toBe(200);
      const body = await response.json();
      expectMatchesOpenApi('GET', '/areas/{code}', body);
      expect(body).toEqual(all.areas.find((area) => area.code === DEFAULT_AREA));
      await api.dispose();
    },
  );

  test(
    'GET /api/areas/{code}/comparables answers at most `limit` sales of the area',
    tags('valid', ['/api/areas/SW11/comparables']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      for (const limit of [1, 3, 10]) {
        const response = await api.get(`${COMPARABLES}&limit=${limit}`);
        expect(response.status()).toBe(200);
        const body = (await response.json()) as Array<{ district: string; floorAreaSqm: number }>;
        expectMatchesOpenApi('GET', '/areas/{code}/comparables', body);
        expect(body.length).toBeGreaterThan(0);
        expect(body.length).toBeLessThanOrEqual(limit);
        expect(new Set(body.map((sale) => sale.district))).toEqual(new Set([DEFAULT_AREA]));
      }
      await api.dispose();
    },
  );
});

test.describe('invalid', () => {
  // A 5xx is tried once more by itself (lib/api getWithRetry), so it fails
  // twice before the visitor sees it; a 429 is not tried again.
  for (const failure of [
    { name: '500', status: 500, json: { detail: 'boom' }, message: 'Something went wrong on our side. Try again in a moment.', fails: 2 },
    { name: '503 with a sentence', status: 503, json: { detail: 'Area figures are being refreshed.' }, message: 'Area figures are being refreshed.', fails: 2 },
    { name: '429', status: 429, json: { detail: 'slow down' }, headers: { 'Retry-After': '5' }, message: 'One moment…', fails: 1 },
  ]) {
    test(
      `a ${failure.name} from /api/areas shows the error with a retry, and the retry loads the figures`,
      tags('invalid', ['/api/areas'], { ui: true }),
      async ({ page }) => {
        let calls = 0;
        await page.route(
          (url) => url.pathname === '/api/areas',
          (route: Route) => {
            calls += 1;
            return calls <= failure.fails
              ? route.fulfill({ status: failure.status, json: failure.json, headers: failure.headers })
              : route.fallback();
          },
        );
        await page.goto('/');
        const notice = areasSection(page).getByRole('alert');
        await expect(notice).toContainText(failure.message);
        // The raw server text of a 500 is never shown.
        await expect(page.getByText('boom')).toHaveCount(0);

        const retried = apiResponse(page, 'GET', '/areas');
        await notice.getByRole('button', { name: 'Try again' }).click();
        expect((await retried).status()).toBe(200);
        await expect(areasSection(page).getByRole('heading', { level: 3, name: /^SW11 · / })).toBeVisible();
      },
    );
  }

  test(
    'one 502 from /api/areas (a restart) is tried again without the visitor seeing an error',
    tags('invalid', ['/api/areas'], { ui: true }),
    async ({ page }) => {
      let calls = 0;
      await page.route(
        (url) => url.pathname === '/api/areas',
        (route: Route) => {
          calls += 1;
          return calls === 1 ? route.fulfill({ status: 502, body: '' }) : route.fallback();
        },
      );
      await page.goto('/');
      await expect(areasSection(page).getByRole('heading', { level: 3, name: /^SW11 · / })).toBeVisible();
      await expect(areasSection(page).getByRole('alert')).toHaveCount(0);
      expect(calls).toBe(2);
    },
  );

  test(
    'a /api/areas that never answers times out with a message and a retry',
    tags('invalid', ['/api/areas'], { ui: true }),
    async ({ page }) => {
      const held: Route[] = [];
      await page.clock.install();
      await page.route(
        (url) => url.pathname === '/api/areas',
        (route: Route) => {
          held.push(route);
        },
      );
      await page.goto('/');
      await expect.poll(() => held.length).toBe(1);
      // lib/api: an open GET gets two attempts of OPEN_GET_ATTEMPT_MS (5 s),
      // REQUEST_TIMEOUT_MS (10 s) in all, before the message.
      await page.clock.fastForward(6_000);
      await expect.poll(() => held.length).toBe(2);
      await expect(areasSection(page).getByRole('alert')).toHaveCount(0);
      await page.clock.fastForward(6_000);
      await expect(areasSection(page).getByRole('alert')).toContainText('This is taking longer than it should.');
      await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
    },
  );

  test(
    'an area the API does not cover is a 404 with a fixed sentence that echoes nothing',
    tags('invalid', ['/api/areas/ZZ99', '/api/areas/ZZ99/comparables']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const area = await api.get('/api/areas/ZZ99');
      expect(area.status()).toBe(404);
      expect(await area.json()).toEqual({ detail: 'We do not cover this area yet' });
      const comparables = await api.get('/api/areas/ZZ99/comparables?propertyType=flat&floorAreaSqm=70');
      expect(comparables.status()).toBe(404);
      await api.dispose();
    },
  );

  test(
    'comparables with a bad limit, type or floor area are 422s that do not repeat the input',
    tags('invalid', ['/api/areas/SW11/comparables']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const cases: Array<[string, string[]]> = [
        ['/api/areas/SW11/comparables?propertyType=flat&floorAreaSqm=70&limit=0', []],
        ['/api/areas/SW11/comparables?propertyType=flat&floorAreaSqm=70&limit=11', []],
        ['/api/areas/SW11/comparables?propertyType=flat&floorAreaSqm=70&limit=eleventy', ['eleventy']],
        ['/api/areas/SW11/comparables?propertyType=castle&floorAreaSqm=70', ['castle']],
        ['/api/areas/SW11/comparables?propertyType=flat&floorAreaSqm=4', []],
        ['/api/areas/SW11/comparables?propertyType=flat&floorAreaSqm=99999', ['99999']],
        ['/api/areas/SW11/comparables?floorAreaSqm=70', []],
      ];
      for (const [url, sent] of cases) {
        const response = await api.get(url);
        expect(response.status(), url).toBe(422);
        await expectNoEcho(response, sent);
      }
      await api.dispose();
    },
  );
});

test.describe('red team', () => {
  test(
    'hostile area codes and query values are refused, never a 500, never echoed',
    tags('redteam', ['/api/areas/SW11', '/api/areas/SW11/comparables']),
    async ({ baseURL }) => {
      for (const value of [...HOSTILE_STRINGS, ...XSS_PAYLOADS]) {
        // A visitor per value keeps each inside the area zone's burst.
        const ip = benchmarkIp();
        const encoded = encodeURIComponent(value);
        // nginx admits only [A-Za-z0-9]+ codes: anything else is its own 404 (a NUL, its 400).
        const code = await rawGet(baseURL, `/api/areas/${encoded}`, { 'CF-Connecting-IP': ip });
        expect([400, 404], `code ${JSON.stringify(value)}`).toContain(code.status);
        for (const query of [
          `propertyType=${encoded}&floorAreaSqm=70`,
          `propertyType=flat&floorAreaSqm=${encoded}`,
          `propertyType=flat&floorAreaSqm=70&postcode=${encoded}`,
          `propertyType=flat&floorAreaSqm=70&limit=${encoded}`,
        ]) {
          const response = await rawGet(baseURL, `/api/areas/SW11/comparables?${query}`, { 'CF-Connecting-IP': ip });
          expect([200, 404, 422], `${query} answered ${response.status}`).toContain(response.status);
          if (value.length >= 3 && response.status !== 200) {
            expect(response.body, query).not.toContain(value);
          }
        }
      }
    },
  );

  test(
    'comparables never answer more than 10 sales, however many are asked for',
    tags('redteam', ['/api/areas/SW11/comparables']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const ten = await api.get(`${COMPARABLES}&limit=10`);
      expect(ten.status()).toBe(200);
      expect(((await ten.json()) as unknown[]).length).toBeLessThanOrEqual(10);
      for (const limit of ['11', '1000', '-1', '1e9']) {
        expect((await api.get(`${COMPARABLES}&limit=${limit}`)).status(), limit).toBe(422);
      }
      await api.dispose();
    },
  );

  test(
    'every method but GET is refused on the open routes',
    tags('redteam', ['/api/areas', '/api/areas/SW11', '/api/areas/SW11/comparables']),
    async ({ playwright, baseURL }) => {
      for (const url of ['/api/areas', '/api/areas/SW11', COMPARABLES]) {
        // nginx counts refused methods against the zone too: a visitor per route.
        const { api } = await newVisitor(playwright, baseURL);
        for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
          const response = await api.fetch(url, { method, data: method === 'OPTIONS' ? undefined : {} });
          expect(response.status(), `${method} ${url}`).toBe(403);
        }
        expect((await api.fetch(url, { method: 'TRACE' })).status(), `TRACE ${url}`).toBe(405);
        // nginx lets HEAD through with GET; the API has no HEAD routes. nginx
        // answers HEAD /api/areas itself, from its copy of the GET (api_cache.conf).
        const head = await api.fetch(url, { method: 'HEAD' });
        expect(head.status(), `HEAD ${url}`).toBe(url === '/api/areas' ? 200 : 405);
        await api.dispose();
      }
    },
  );

  test(
    'paths around the whitelist reach nothing: dot segments, the API’s own pages, unlisted routes',
    tags('redteam', ['/api/areas']),
    async ({ baseURL }) => {
      const headers = { 'CF-Connecting-IP': benchmarkIp() };
      for (const probe of [
        '/api/../docs',
        '/api/../openapi.json',
        '/api/../properties',
        '/api/%2e%2e/docs',
        '/api/..%2fproperties',
        '/api/areas/../../properties',
        '/api/areas/SW11/../../../openapi.json',
        '/api/properties',
        '/api/properties/100023336956',
        '/api/docs',
        '/api/redoc',
        '/api/openapi.json',
        '/api/health',
        '/api/ready',
        '/api/addresses/search/metrics',
        '/api/x',
        '/docs',
        '/redoc',
        '/openapi.json',
        '/properties',
        '/health',
      ]) {
        const response = await rawGet(baseURL, probe, headers);
        expect(response.status, probe).toBe(404);
        expect(response.body, probe).not.toContain('swagger');
        expect(response.body, probe).not.toContain('"openapi"');
      }
      // A dot segment that resolves back onto an exposed route is that route, nothing more.
      expect((await rawGet(baseURL, '/api/../api/areas', headers)).status).toBe(200);
    },
  );

  test(
    'scripts’ user agents are refused on /api; the same visitor with a browser’s is served',
    tags('redteam', ['/api/areas']),
    async ({ playwright, baseURL }) => {
      for (const userAgent of ['curl/8.5.0', 'python-requests/2.32.3', 'Go-http-client/1.1', 'sqlmap/1.8', 'Scrapy/2.11']) {
        const { api, ip } = await newVisitor(playwright, baseURL, { userAgent });
        expect((await api.get('/api/areas')).status(), userAgent).toBe(403);
        await api.dispose();
        const browser = await newVisitor(playwright, baseURL, { ip, userAgent: BROWSER_UA });
        expect((await browser.api.get('/api/areas')).status(), `${userAgent} then a browser`).toBe(200);
        await browser.api.dispose();
      }
      // No user agent at all.
      expect((await rawGet(baseURL, '/api/areas', { 'CF-Connecting-IP': benchmarkIp(), 'User-Agent': '' })).status).toBe(403);
    },
  );

  test(
    'a client’s own X-API-Key changes nothing: nginx replaces it',
    tags('redteam', ['/api/areas', '/api/properties']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL, { headers: { 'X-API-Key': 'not-the-key' } });
      expect((await api.get('/api/areas')).status()).toBe(200);
      // Nor does a key open the routes nginx keeps on the loopback.
      expect((await api.get('/api/properties')).status()).toBe(404);
      await api.dispose();
    },
  );

  test(
    'the page and /api answers carry production’s security headers',
    tags('redteam', ['/api/areas']),
    async ({ playwright, baseURL }) => {
      expect(PRODUCTION_CSP).toBeTruthy();
      const { api } = await newVisitor(playwright, baseURL);
      for (const url of ['/', '/api/areas', '/api/x']) {
        const response = await api.get(url);
        const headers = response.headers();
        expect(headers['content-security-policy'], url).toBe(PRODUCTION_CSP);
        expect(headers['x-frame-options'], url).toBe('DENY');
        expect(headers['x-content-type-options'], url).toBe('nosniff');
        expect(headers['referrer-policy'], url).toBe('strict-origin-when-cross-origin');
        expect(headers['strict-transport-security'], url).toMatch(/^max-age=\d+/);
        expect(headers['permissions-policy'], url).toContain('camera=()');
        // No version in the server header (server_tokens off).
        expect(headers['server'], url).toBe('nginx');
      }
      // The area figures are the same for everyone: nginx and Cloudflare keep a
      // copy. Every other /api answer is no-store.
      const areas = await api.get('/api/areas');
      expect(areas.headers()['cache-control']).toBe('public, max-age=60, s-maxage=300, stale-while-revalidate=3600');
      expect(areas.headers()['cache-control']).not.toContain('no-store');
      expect(areas.headers()['etag']).toBeTruthy();
      expect(areas.headers()['x-request-id']).toMatch(/^[A-Za-z0-9-]{8,64}$/);
      expect(areas.headers()['x-hpldn-reject']).toBeUndefined();
      const config = await api.get('/api/session/config');
      expect(config.headers()['cache-control']).toBe('no-store');
      await api.dispose();
    },
  );

  test(
    'an area name carrying script is shown as text and runs nothing',
    tags('redteam', ['/api/areas'], { ui: true }),
    async ({ page, visitorIp }) => {
      const watch = await watchForScripts(page);
      const payload = XSS_PAYLOADS.join(' ');
      await page.route(
        (url) => url.pathname === '/api/areas',
        async (route: Route) => {
          const response = await fetchAsVisitor(route, visitorIp);
          const body = (await response.json()) as { areas: Area[] };
          for (const area of body.areas) {
            if (area.code === DEFAULT_AREA) {
              area.name = payload;
            }
          }
          await route.fulfill({ response, json: body });
        },
      );
      await page.goto('/');
      await expect(areasSection(page).getByRole('heading', { level: 3, name: `${DEFAULT_AREA} · ${payload}` })).toBeVisible();
      await expectNoScriptRan(page, watch);
    },
  );
});
