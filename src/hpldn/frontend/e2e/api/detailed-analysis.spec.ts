/**
 * `DetailedAnalysis` (the price card turned over): `POST /api/nearby` and
 * `POST /api/schools` for the priced home, with the pass, and the open
 * `GET /api/inflation` (CPIH) for the inflation-corrected figures.
 */
import { expect, test } from '../fixtures';
import type { Page, Route } from '@playwright/test';
import {
  HOME,
  HOSTILE_STRINGS,
  XSS_PAYLOADS,
  apiResponse,
  chooseHome,
  expectMatchesOpenApi,
  expectNoEcho,
  expectNoScriptRan,
  expectSafeRefusal,
  formatGBP,
  formatSoldDate,
  humanVisitor,
  newVisitor,
  readable,
  submitPrice,
  tags,
  watchForScripts,
} from '../support/api';

interface Address {
  postnumber: string;
  poststreet: string | null;
  posttown: string;
  postcode: string;
  unit?: string | null;
}

interface NearbySales {
  sales: Array<{ address: Address; price: number; soldOn: string; distanceM: number }>;
  windowFrom: string;
  windowTo: string;
}

interface Schools {
  primary: Array<{ name: string; distanceM: number }>;
  secondary: Array<{ name: string; distanceM: number }>;
  radiusM: number;
}

interface Inflation {
  latestMonth: string;
  months: Array<{ month: string; index: number }>;
}

const { unit: _unit, ...BUILDING } = HOME;
/** The most nearby sales the API lists (`market/nearby.NEARBY_LIMIT`). */
const NEARBY_LIMIT = 5;

/** `lib/price.describeFullAddress`. */
function describeFullAddress(address: Address): string {
  const first = [address.postnumber, address.poststreet].filter(Boolean).map((part) => readable(String(part)));
  const line = (address.unit ? readable(address.unit) + ', ' : '') + first.join(' ');
  return line + ', ' + readable(address.posttown) + ', ' + address.postcode;
}

/** Prices the default home and turns the card to the analysis; returns the answers it fetched. */
async function openAnalysis(page: Page) {
  await chooseHome(page);
  const price = await submitPrice(page);
  expect(price.status()).toBe(200);
  const nearby = apiResponse(page, 'POST', '/nearby');
  const schools = apiResponse(page, 'POST', '/schools');
  const inflation = apiResponse(page, 'GET', '/inflation');
  await page.getByRole('button', { name: 'Detailed Analysis' }).click();
  return { price, nearby, schools, inflation };
}

function analysis(page: Page) {
  return page.locator('section#result');
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
});

test.describe('valid', () => {
  test(
    'opening the analysis fetches nearby sales, schools and CPIH, and shows exactly what they hold',
    tags('valid', ['/api/nearby', '/api/schools', '/api/inflation'], { ui: true }),
    async ({ page }) => {
      const requests = new Map<string, { pass: string | null; body: unknown }>();
      page.on('request', (request) => {
        const pathname = new URL(request.url()).pathname;
        if (['/api/nearby', '/api/schools', '/api/inflation'].includes(pathname)) {
          void request.headerValue('x-hpldn-pass').then((pass) => requests.set(pathname, { pass, body: request.postDataJSON() }));
        }
      });
      const { price, nearby, schools, inflation } = await openAnalysis(page);
      const home = ((await price.json()) as { address: Address }).address;
      const [nearbyResponse, schoolsResponse, inflationResponse] = await Promise.all([nearby, schools, inflation]);

      for (const response of [nearbyResponse, schoolsResponse, inflationResponse]) {
        expect(response.status(), response.url()).toBe(200);
      }
      // The home as the price answer named it; `unit` only for a flat (`lib/api` getNearbySales).
      const { unit, ...building } = home;
      const expectedBody = unit ? { ...building, unit } : building;
      expect(requests.get('/api/nearby')?.body).toEqual(expectedBody);
      expect(requests.get('/api/schools')?.body).toEqual(expectedBody);
      expect(requests.get('/api/nearby')?.pass).toBeTruthy();
      expect(requests.get('/api/schools')?.pass).toBeTruthy();
      expect(requests.get('/api/inflation')?.pass).toBeNull();

      const nearbyBody = (await nearbyResponse.json()) as NearbySales;
      const schoolsBody = (await schoolsResponse.json()) as Schools;
      const inflationBody = (await inflationResponse.json()) as Inflation;
      expectMatchesOpenApi('POST', '/nearby', nearbyBody);
      expectMatchesOpenApi('POST', '/schools', schoolsBody);
      expectMatchesOpenApi('GET', '/inflation', inflationBody);

      const card = analysis(page);
      await expect(card.getByText('Detailed Analysis', { exact: true })).toBeVisible();
      await expect(card.getByRole('alert')).toHaveCount(0);

      // Nearby sales: one row per sale, its address and price.
      const sales = card.getByRole('region', { name: 'Nearby sales' });
      if (nearbyBody.sales.length) {
        await expect(sales.getByText(`What the nearest homes sold for, ${formatSoldDate(nearbyBody.windowFrom)} to ${formatSoldDate(nearbyBody.windowTo)}`)).toBeVisible();
        const rows = sales.getByRole('listitem');
        await expect(rows).toHaveCount(nearbyBody.sales.length);
        for (let i = 0; i < nearbyBody.sales.length; i += 1) {
          await expect(rows.nth(i)).toContainText(describeFullAddress(nearbyBody.sales[i].address));
          await expect(rows.nth(i)).toContainText(formatGBP(nearbyBody.sales[i].price));
        }
        const mean = nearbyBody.sales.reduce((sum, sale) => sum + sale.price, 0) / nearbyBody.sales.length;
        const figures = card.getByRole('definition').filter({ hasText: formatGBP(Math.round(mean / 1000) * 1000) });
        await expect(figures.first()).toBeVisible();
      } else {
        await expect(sales.getByText(/^No sales near this home/)).toBeVisible();
      }

      // Schools: every primary and secondary school, by name.
      const schoolList = card.getByRole('list', { name: 'Nearby schools' });
      const names = [...schoolsBody.primary, ...schoolsBody.secondary].map((school) => school.name);
      await expect(schoolList.getByRole('listitem')).toHaveCount(names.length);
      for (const name of names) {
        await expect(schoolList.getByRole('link', { name })).toBeVisible();
      }

      // CPIH: the footnote names the latest month the API holds.
      await expect(card.getByText(new RegExp(`published to ${formatSoldDate(inflationBody.latestMonth)}`))).toBeVisible();
    },
  );

  test(
    'the routes answer directly as openapi.json says: nearest sales first, schools within the radius, CPIH by month',
    tags('valid', ['/api/nearby', '/api/schools', '/api/inflation']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const headers = { 'X-HPLDN-Pass': pass };
      const nearby = await api.post('/api/nearby', { data: BUILDING, headers });
      expect(nearby.status()).toBe(200);
      const sales = (await nearby.json()) as NearbySales;
      expectMatchesOpenApi('POST', '/nearby', sales);
      const distances = sales.sales.map((sale) => sale.distanceM);
      expect(distances).toEqual([...distances].sort((a, b) => a - b));

      const schools = await api.post('/api/schools', { data: BUILDING, headers });
      expect(schools.status()).toBe(200);
      const schoolsBody = (await schools.json()) as Schools;
      expectMatchesOpenApi('POST', '/schools', schoolsBody);
      for (const school of [...schoolsBody.primary, ...schoolsBody.secondary]) {
        expect(school.distanceM).toBeLessThanOrEqual(schoolsBody.radiusM);
      }

      const inflation = await api.get('/api/inflation');
      expect(inflation.status()).toBe(200);
      const series = (await inflation.json()) as Inflation;
      expectMatchesOpenApi('GET', '/inflation', series);
      expect(series.months.at(-1)?.month).toBe(series.latestMonth);
      await api.dispose();
    },
  );
});

test.describe('invalid', () => {
  for (const failure of [
    { route: '/api/nearby', status: 500, json: { detail: 'relation does not exist' }, message: 'Something went wrong on our side. Try again in a moment.' },
    { route: '/api/schools', status: 503, json: { detail: 'School ratings are being refreshed.' }, message: 'School ratings are being refreshed.' },
    // An open GET tries a 5xx once more by itself (lib/api getWithRetry), so it fails twice here.
    { route: '/api/inflation', status: 500, json: { detail: 'relation does not exist' }, message: 'Something went wrong on our side. Try again in a moment.', fails: 2 },
    { route: '/api/nearby', status: 429, json: { detail: 'Too many' }, headers: { 'Retry-After': '2' }, message: 'One moment…' },
  ]) {
    test(
      `a ${failure.status} from ${failure.route} shows the message with "Try again", which loads it`,
      tags('invalid', [failure.route], { ui: true }),
      async ({ page }) => {
        let calls = 0;
        await page.route(
          (url) => url.pathname === failure.route,
          (route: Route) => {
            calls += 1;
            return calls <= (failure.fails ?? 1)
              ? route.fulfill({ status: failure.status, json: failure.json, headers: failure.headers })
              : route.fallback();
          },
        );
        await openAnalysis(page);
        const alert = analysis(page).getByRole('alert');
        await expect(alert).toContainText(failure.message);
        await expect(page.getByText('relation does not exist')).toHaveCount(0);

        const again = page.waitForResponse((r) => new URL(r.url()).pathname === failure.route);
        await alert.getByRole('button', { name: 'Try again' }).click();
        expect((await again).status()).toBe(200);
        await expect(analysis(page).getByRole('alert')).toHaveCount(0);
      },
    );
  }

  test(
    'nearby sales that never answer time out with a message and a retry',
    tags('invalid', ['/api/nearby'], { ui: true }),
    async ({ page }) => {
      const held: Route[] = [];
      await page.clock.install();
      await page.reload();
      await page.route(
        (url) => url.pathname === '/api/nearby',
        (route: Route) => {
          held.push(route);
        },
      );
      await chooseHome(page);
      expect((await submitPrice(page)).status()).toBe(200);
      await page.getByRole('button', { name: 'Detailed Analysis' }).click();
      await expect.poll(() => held.length).toBe(1);
      await page.clock.fastForward(11_000);
      await expect(analysis(page).getByRole('alert')).toContainText('This is taking longer than it should.');
      await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
    },
  );

  test(
    'direct calls with a pass and bad bodies are 422s that do not repeat them; an unknown home is a 404',
    tags('invalid', ['/api/nearby', '/api/schools']),
    async ({ playwright, baseURL }) => {
      const cases: Array<[string, unknown, string[]]> = [
        ['no postcode', { ...BUILDING, postcode: undefined }, []],
        ['bad postcode', { ...BUILDING, postcode: 'NOTAPOSTCODE' }, ['NOTAPOSTCODE']],
        ['blank town', { ...BUILDING, posttown: ' ' }, []],
        ['long street', { ...BUILDING, poststreet: 'Z'.repeat(101) }, ['Z'.repeat(101)]],
      ];
      for (const route of ['/api/nearby', '/api/schools']) {
        const { api, pass } = await humanVisitor(playwright, baseURL);
        for (const [name, body, sent] of cases) {
          const response = await api.post(route, { data: body, headers: { 'X-HPLDN-Pass': pass } });
          expect(response.status(), `${route} ${name}`).toBe(422);
          await expectNoEcho(response, sent);
        }
        const unknown = await api.post(route, { data: { ...BUILDING, postnumber: '9999' }, headers: { 'X-HPLDN-Pass': pass } });
        expect(unknown.status(), `${route} unknown home`).toBe(404);
        await expectSafeRefusal(unknown, []);
        await api.dispose();
      }
    },
  );

  test(
    'nearby and schools refuse a caller without a pass; inflation needs none and ignores a query',
    tags('invalid', ['/api/nearby', '/api/schools', '/api/inflation']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      for (const route of ['/api/nearby', '/api/schools']) {
        const response = await api.post(route, { data: BUILDING });
        expect(response.status(), route).toBe(403);
        expect(response.headers()['x-hpldn-captcha'], route).toBe('required');
      }
      const plain = (await (await api.get('/api/inflation')).json()) as Inflation;
      const queried = await api.get('/api/inflation?from=1900-01-01&months=-1');
      expect(queried.status()).toBe(200);
      expect(await queried.json()).toEqual(plain);
      await api.dispose();
    },
  );
});

test.describe('red team', () => {
  test(
    'SQL meta-characters, wildcards, unicode, NUL and script in every field: refused or not found, never a 500',
    tags('redteam', ['/api/nearby', '/api/schools']),
    async ({ playwright, baseURL }) => {
      test.slow();
      const fields = ['postnumber', 'poststreet', 'posttown', 'unit', 'postcode'] as const;
      for (const value of [...HOSTILE_STRINGS, ...XSS_PAYLOADS]) {
        for (const route of ['/api/nearby', '/api/schools']) {
          // Five calls a visitor, inside the form limiter's burst.
          const { api, pass } = await humanVisitor(playwright, baseURL);
          for (const field of fields) {
            const response = await api.post(route, { data: { ...BUILDING, [field]: value }, headers: { 'X-HPLDN-Pass': pass } });
            const where = `${route} ${field}=${JSON.stringify(value)}`;
            expect([200, 404, 422], `${where} answered ${response.status()}`).toContain(response.status());
            if (response.status() !== 200) {
              await expectSafeRefusal(response, [value.trim()]);
            }
          }
          await api.dispose();
        }
      }
    },
  );

  test(
    'nearby lists at most 5 sales, for a house, a flat and a crowded postcode',
    tags('redteam', ['/api/nearby']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      for (const home of [BUILDING, HOME]) {
        const response = await api.post('/api/nearby', { data: home, headers: { 'X-HPLDN-Pass': pass } });
        expect(response.status()).toBe(200);
        expect(((await response.json()) as NearbySales).sales.length).toBeLessThanOrEqual(NEARBY_LIMIT);
      }
      await api.dispose();
    },
  );

  test(
    'a body over 4 kB is a 413; methods other than the route’s own are refused',
    tags('redteam', ['/api/nearby', '/api/schools', '/api/inflation']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      for (const route of ['/api/nearby', '/api/schools']) {
        const big = await api.post(route, { data: { ...BUILDING, poststreet: 'B'.repeat(5_000) }, headers: { 'X-HPLDN-Pass': pass } });
        expect(big.status(), route).toBe(413);
        for (const method of ['GET', 'PUT', 'DELETE']) {
          expect((await api.fetch(route, { method })).status(), `${method} ${route}`).toBe(403);
        }
      }
      for (const method of ['POST', 'PUT', 'DELETE']) {
        expect((await api.fetch('/api/inflation', { method, data: {} })).status(), `${method} /api/inflation`).toBe(403);
      }
      await api.dispose();
    },
  );

  test(
    'nearby sales and schools carrying script are shown as text and run nothing',
    tags('redteam', ['/api/nearby', '/api/schools'], { ui: true }),
    async ({ page }) => {
      const watch = await watchForScripts(page);
      await page.reload();
      const hostileSales: NearbySales = {
        windowFrom: '2025-07-31',
        windowTo: '2026-07-31',
        sales: XSS_PAYLOADS.map((payload, i) => ({
          address: { postnumber: String(i + 1), poststreet: payload, posttown: payload, postcode: 'CR0 1JN', unit: null },
          propertyType: 'flat',
          price: 250_000 + i,
          soldOn: '2026-05-01',
          distanceM: 10 * (i + 1),
          compMedianPrice: null,
          compMedianPricePerSqm: null,
          compTxnCount: null,
          localTypeMedian: null,
        })) as unknown as NearbySales['sales'],
      };
      const hostileSchool = (payload: string, i: number) => ({
        urn: 100_000 + i,
        name: payload,
        phase: 'primary',
        distanceM: 100 * (i + 1),
        ratingLabel: payload,
        ratingScore: 2,
        gradeBasis: 'overall',
        inspectedOn: '2024-01-10',
        publishedOn: '2024-02-01',
        inheritedFromUrn: null,
        reportUrl: 'https://reports.ofsted.gov.uk/provider/21/' + (100_000 + i),
      });
      await page.route(
        (url) => url.pathname === '/api/nearby',
        (route: Route) => route.fulfill({ status: 200, json: hostileSales }),
      );
      await page.route(
        (url) => url.pathname === '/api/schools',
        (route: Route) =>
          route.fulfill({
            status: 200,
            json: {
              primary: XSS_PAYLOADS.map(hostileSchool),
              secondary: [],
              asOf: '2026-10-01',
              radiusM: 3000,
              source: XSS_PAYLOADS[0],
            },
          }),
      );
      await chooseHome(page);
      expect((await submitPrice(page)).status()).toBe(200);
      await page.getByRole('button', { name: 'Detailed Analysis' }).click();
      const card = analysis(page);
      await expect(card.getByRole('region', { name: 'Nearby sales' }).getByRole('listitem')).toHaveCount(XSS_PAYLOADS.length);
      await expect(card.getByRole('list', { name: 'Nearby schools' }).getByRole('link', { name: XSS_PAYLOADS[0] })).toBeVisible();
      await expect(card.getByRole('region', { name: 'Nearby sales' })).toContainText(XSS_PAYLOADS[1]);
      await expectNoScriptRan(page, watch);
    },
  );
});
