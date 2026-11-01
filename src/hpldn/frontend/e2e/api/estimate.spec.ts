/**
 * `EstimateForm` and its result card: `POST /api/estimate` from a postcode,
 * property type, bedrooms, floor area and date, with the reCAPTCHA pass.
 */
import { expect, test } from '../fixtures';
import type { Page, Route } from '@playwright/test';
import {
  HOSTILE_STRINGS,
  XSS_PAYLOADS,
  expectMatchesOpenApi,
  expectNoEcho,
  expectNoScriptRan,
  expectSafeRefusal,
  fetchAsVisitor,
  formatGBP,
  humanVisitor,
  isoDay,
  newVisitor,
  openEstimateForm,
  saleDayFromToday,
  tags,
  ukDate,
  watchForScripts,
} from '../support/api';

const POSTCODE = process.env.E2E_POSTCODE ?? 'CR0 1JN';
const VALID = { postcode: 'SW11 1AA', propertyType: 'flat', bedrooms: 2, floorAreaSqm: 72 };
/** The card shows the first 3 reasons and 3 sales (`EstimateResult`). */
const SHOWN = 3;

interface Estimate {
  request: { postcode: string; district: string; bedrooms: number; floorAreaSqm: number };
  value: number;
  drivers: Array<{ id: string; label: string; detail: string }>;
  comparables: Array<{ id: string; street: string; price: number }>;
}

type Form = Awaited<ReturnType<typeof openEstimateForm>>;

/** Fills the details form; `floorArea` and `bedrooms` default to 72 m² and 2. */
async function fillDetails(form: Form, postcode: string, options: { floorArea?: string; bedrooms?: string } = {}) {
  await form.getByRole('textbox', { name: 'Postcode' }).fill(postcode);
  await form.getByRole('combobox', { name: 'Property type' }).selectOption('flat');
  await form.getByRole('combobox', { name: 'Bedrooms' }).selectOption(options.bedrooms ?? '2');
  await form.getByRole('spinbutton', { name: 'Floor area' }).fill(options.floorArea ?? '72');
}

function estimateResponse(page: Page) {
  return page.waitForResponse((r) => new URL(r.url()).pathname === '/api/estimate');
}

function resultCard(page: Page) {
  return page.locator('section#result');
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
});

test.describe('valid', () => {
  test(
    'the details are sent with the pass, and the card shows the API’s value, reasons and sales',
    tags('valid', ['/api/estimate'], { ui: true }),
    async ({ page }) => {
      const form = await openEstimateForm(page);
      await fillDetails(form, POSTCODE.toLowerCase());
      const { saleDay } = await saleDayFromToday(page);

      const sent = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/estimate');
      const answer = estimateResponse(page);
      await form.getByRole('button', { name: 'Estimate value' }).click();
      const request = await sent;
      expect(request.method()).toBe('POST');
      expect(await request.headerValue('x-hpldn-pass')).toBeTruthy();
      expect(request.postDataJSON()).toEqual({
        postcode: POSTCODE,
        propertyType: 'flat',
        bedrooms: 2,
        floorAreaSqm: 72,
        saleDate: isoDay(saleDay),
      });

      const response = await answer;
      expect(response.status()).toBe(200);
      const body = (await response.json()) as Estimate;
      expectMatchesOpenApi('POST', '/estimate', body);

      const card = resultCard(page);
      await expect(card.locator('#result-heading')).toContainText(`${POSTCODE} in `);
      await expect(card.getByText('Estimated value')).toBeVisible();
      await expect(card.locator('strong').first()).toHaveText(formatGBP(body.value));
      await expect(card.getByText('72 m², 2 bedrooms')).toBeVisible();
      const reasons = card.getByRole('list', { name: 'Why this price' }).getByRole('listitem');
      await expect(reasons).toHaveCount(Math.min(SHOWN, body.drivers.length));
      await expect(reasons.first()).toContainText(body.drivers[0].label);
      if (body.comparables.length) {
        const sales = card.getByRole('list', { name: 'Nearby sales' }).getByRole('listitem');
        await expect(sales).toHaveCount(Math.min(SHOWN, body.comparables.length));
        await expect(sales.first()).toContainText(body.comparables[0].street);
        await expect(sales.first()).toContainText(formatGBP(body.comparables[0].price));
      }
    },
  );

  test(
    'POST /api/estimate works out the district from a full postcode or a bare district',
    tags('valid', ['/api/estimate']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      for (const postcode of ['sw111aa', 'SW11']) {
        const response = await api.post('/api/estimate', {
          data: { ...VALID, postcode, district: 'E1' },
          headers: { 'X-HPLDN-Pass': pass },
        });
        expect(response.status(), postcode).toBe(200);
        const body = (await response.json()) as Estimate;
        expectMatchesOpenApi('POST', '/estimate', body);
        // A district sent by the client is ignored.
        expect(body.request.district).toBe('SW11');
        expect(body.value).toBeGreaterThan(0);
      }
      await api.dispose();
    },
  );
});

test.describe('invalid', () => {
  test(
    'a malformed, unknown or uncovered postcode, a floor area out of range and a past date are refused before sending',
    tags('invalid', ['/api/estimate'], { ui: true }),
    async ({ page }) => {
      let estimates = 0;
      page.on('request', (r) => {
        if (new URL(r.url()).pathname === '/api/estimate') {
          estimates += 1;
        }
      });
      // The coverage check needs the area list.
      await expect(page.locator('section#areas').getByRole('heading', { level: 3 })).toBeVisible();
      const form = await openEstimateForm(page);
      const submit = form.getByRole('button', { name: 'Estimate value' });
      const { today, saleDay } = await saleDayFromToday(page);

      for (const [postcode, options, message] of [
        ['', {}, 'Enter a London postcode.'],
        ['NOTAPOSTCODE', {}, 'Invalid UK Postcode'],
        ['SW11 1AAA', {}, 'Invalid UK Postcode'],
        ['TN16 1AA', {}, 'We do not cover TN16 yet. Try a London postcode.'],
        ['SW11 1AA', { floorArea: '14' }, 'Floor area must be at least 15 m².'],
        ['SW11 1AA', { floorArea: '1001' }, 'Floor area must be under 1,000 m².'],
        ['SW11 1AA', { floorArea: '40', bedrooms: '6' }, 'A home with 6 bedrooms needs at least 65 m². Check the floor area or the bedrooms.'],
      ] as Array<[string, { floorArea?: string; bedrooms?: string }, string]>) {
        await fillDetails(form, postcode, options);
        await submit.click();
        await expect(form.getByText(message), `${postcode} ${JSON.stringify(options)}`).toBeVisible();
      }

      await fillDetails(form, 'SW11 1AA');
      const dateInput = form.getByRole('textbox', { name: 'Value as of' });
      await dateInput.fill(ukDate(today));
      await submit.click();
      await expect(form.getByText(`Choose a date from ${ukDate(saleDay)}.`)).toBeVisible();
      expect(estimates).toBe(0);
    },
  );

  test(
    'a postcode the API does not know is shown on the postcode field',
    tags('invalid', ['/api/estimate'], { ui: true }),
    async ({ page }) => {
      await page.route(
        (url) => url.pathname === '/api/estimate',
        (route: Route) => route.fulfill({ status: 404, json: { detail: 'We do not cover SW11 9ZZ yet' } }),
      );
      const form = await openEstimateForm(page);
      await fillDetails(form, 'SW11 9ZZ');
      const answer = estimateResponse(page);
      await form.getByRole('button', { name: 'Estimate value' }).click();
      expect((await answer).status()).toBe(404);
      await expect(form.getByText('We do not cover SW11 9ZZ yet')).toBeVisible();
      await expect(form.getByRole('textbox', { name: 'Postcode' })).toBeFocused();
      await expect(resultCard(page)).toHaveCount(0);
    },
  );

  for (const failure of [
    { name: '500', status: 500, json: { detail: 'KeyError: district' }, message: 'Something went wrong on our side. Try again in a moment.', edit: false },
    { name: '503 with a sentence', status: 503, json: { detail: 'Estimates are paused.' }, message: 'Estimates are paused.', edit: false },
    { name: '422 with a sentence', status: 422, json: { detail: 'A home with 2 bedrooms needs at least 25 m².' }, message: 'A home with 2 bedrooms needs at least 25 m².', edit: true },
    { name: '429', status: 429, json: { detail: 'Too many' }, headers: { 'Retry-After': '3' }, message: 'One moment…', edit: false },
  ]) {
    test(
      `a ${failure.name} from /api/estimate shows "We could not get your estimate" with the right actions`,
      tags('invalid', ['/api/estimate'], { ui: true }),
      async ({ page }) => {
        let calls = 0;
        await page.route(
          (url) => url.pathname === '/api/estimate',
          (route: Route) => {
            calls += 1;
            return calls === 1
              ? route.fulfill({ status: failure.status, json: failure.json, headers: failure.headers })
              : route.fallback();
          },
        );
        const form = await openEstimateForm(page);
        await fillDetails(form, VALID.postcode);
        const first = estimateResponse(page);
        await form.getByRole('button', { name: 'Estimate value' }).click();
        expect((await first).status()).toBe(failure.status);

        const card = resultCard(page);
        await expect(card.getByRole('heading', { name: 'We could not get your estimate' })).toBeVisible();
        await expect(card.getByText(failure.message)).toBeVisible();
        await expect(page.getByText('KeyError')).toHaveCount(0);
        await expect(card.getByRole('button', { name: 'Change the details' })).toHaveCount(failure.edit ? 1 : 0);

        const again = estimateResponse(page);
        await card.getByRole('button', { name: 'Try again' }).click();
        expect((await again).status()).toBe(200);
        await expect(card.getByText('Estimated value')).toBeVisible();
      },
    );
  }

  test(
    'an estimate that never answers times out with a message and a retry',
    tags('invalid', ['/api/estimate'], { ui: true }),
    async ({ page }) => {
      const held: Route[] = [];
      await page.clock.install();
      await page.reload();
      await page.route(
        (url) => url.pathname === '/api/estimate',
        (route: Route) => {
          held.push(route);
        },
      );
      const form = await openEstimateForm(page);
      await fillDetails(form, VALID.postcode);
      await form.getByRole('button', { name: 'Estimate value' }).click();
      await expect.poll(() => held.length).toBe(1);
      await page.clock.fastForward(11_000);
      await expect(resultCard(page).getByText('This is taking longer than it should. Try again in a moment.')).toBeVisible();
      await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
    },
  );

  test(
    'direct calls with a pass and bad bodies are 422s that do not repeat them',
    tags('invalid', ['/api/estimate']),
    async ({ playwright, baseURL }) => {
      const cases: Array<[string, unknown, string[]]> = [
        ['no bedrooms', { ...VALID, bedrooms: undefined }, []],
        ['0 bedrooms', { ...VALID, bedrooms: 0 }, []],
        ['7 bedrooms', { ...VALID, bedrooms: 7 }, []],
        ['words for bedrooms', { ...VALID, bedrooms: 'two' }, ['two']],
        ['tiny', { ...VALID, floorAreaSqm: 5 }, []],
        ['huge', { ...VALID, floorAreaSqm: 50_000 }, ['50000']],
        ['castle', { ...VALID, propertyType: 'castle' }, ['castle']],
        ['bad postcode', { ...VALID, postcode: 'NOTAPOSTCODE' }, ['NOTAPOSTCODE']],
        ['too small for the bedrooms', { ...VALID, bedrooms: 6, floorAreaSqm: 30 }, []],
        ['not a date', { ...VALID, saleDate: 'soonish' }, ['soonish']],
      ];
      // The form limiter's burst is 10 a visitor: a new visitor every 5 calls.
      for (let start = 0; start < cases.length; start += 5) {
        const { api, pass } = await humanVisitor(playwright, baseURL);
        for (const [name, body, sent] of cases.slice(start, start + 5)) {
          const response = await api.post('/api/estimate', { data: body, headers: { 'X-HPLDN-Pass': pass } });
          expect(response.status(), name).toBe(422);
          await expectNoEcho(response, sent);
        }
        await api.dispose();
      }
    },
  );

  test(
    'POST /api/estimate without a pass is refused by the API’s gate',
    tags('invalid', ['/api/estimate']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const response = await api.post('/api/estimate', { data: VALID });
      expect(response.status()).toBe(403);
      expect(response.headers()['x-hpldn-captcha']).toBe('required');
      await api.dispose();
    },
  );
});

test.describe('red team', () => {
  test(
    'SQL meta-characters, wildcards, unicode, NUL and script in the text fields: refused, never a 500 or an echo',
    tags('redteam', ['/api/estimate']),
    async ({ playwright, baseURL }) => {
      for (const value of [...HOSTILE_STRINGS, ...XSS_PAYLOADS]) {
        const { api, pass } = await humanVisitor(playwright, baseURL);
        for (const field of ['postcode', 'propertyType', 'bedrooms', 'floorAreaSqm', 'saleDate']) {
          const response = await api.post('/api/estimate', {
            data: { ...VALID, [field]: value },
            headers: { 'X-HPLDN-Pass': pass },
          });
          const where = `${field}=${JSON.stringify(value)}`;
          expect([404, 422], `${where} answered ${response.status()}`).toContain(response.status());
          await expectSafeRefusal(response, [value.trim()]);
        }
        await api.dispose();
      }
    },
  );

  test(
    'an estimate lists at most 10 comparable sales',
    tags('redteam', ['/api/estimate']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      for (const body of [VALID, { ...VALID, postcode: 'SW11', floorAreaSqm: 999 }, { ...VALID, postcode: 'E14', propertyType: 'detached', bedrooms: 6, floorAreaSqm: 400 }]) {
        const response = await api.post('/api/estimate', { data: body, headers: { 'X-HPLDN-Pass': pass } });
        expect(response.status()).toBe(200);
        expect(((await response.json()) as Estimate).comparables.length).toBeLessThanOrEqual(10);
      }
      await api.dispose();
    },
  );

  test(
    'a body over 4 kB is a 413 from nginx; methods other than POST are refused',
    tags('redteam', ['/api/estimate']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const big = await api.post('/api/estimate', {
        data: { ...VALID, postcode: 'S'.repeat(5_000) },
        headers: { 'X-HPLDN-Pass': pass },
      });
      expect(big.status()).toBe(413);
      for (const method of ['GET', 'PUT', 'DELETE']) {
        expect((await api.fetch('/api/estimate', { method })).status(), method).toBe(403);
      }
      await api.dispose();
    },
  );

  test(
    'an estimate carrying script in its reasons and sales is shown as text and runs nothing',
    tags('redteam', ['/api/estimate'], { ui: true }),
    async ({ page, visitorIp }) => {
      const watch = await watchForScripts(page);
      await page.reload();
      await page.route(
        (url) => url.pathname === '/api/estimate',
        async (route: Route) => {
          const response = await fetchAsVisitor(route, visitorIp);
          const body = (await response.json()) as Estimate;
          body.drivers = body.drivers.map((driver, i) => ({ ...driver, label: XSS_PAYLOADS[i % XSS_PAYLOADS.length], detail: XSS_PAYLOADS[0] }));
          body.comparables = body.comparables.map((sale, i) => ({ ...sale, street: XSS_PAYLOADS[i % XSS_PAYLOADS.length] }));
          await route.fulfill({ response, json: body });
        },
      );
      const form = await openEstimateForm(page);
      await fillDetails(form, VALID.postcode);
      const answer = estimateResponse(page);
      await form.getByRole('button', { name: 'Estimate value' }).click();
      expect((await answer).status()).toBe(200);
      const card = resultCard(page);
      await expect(card.getByRole('list', { name: 'Why this price' })).toContainText(XSS_PAYLOADS[0]);
      await expectNoScriptRan(page, watch);
    },
  );
});
