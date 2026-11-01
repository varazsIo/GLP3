/**
 * `PriceForm` and its result card: `POST /api/price` for the chosen home and
 * sale date, with the reCAPTCHA pass.
 */
import { expect, test } from '../fixtures';
import type { Page, Route } from '@playwright/test';
import {
  HOME,
  HOSTILE_STRINGS,
  XSS_PAYLOADS,
  chooseHome,
  expectMatchesOpenApi,
  expectNoEcho,
  expectNoScriptRan,
  expectSafeRefusal,
  fetchAsVisitor,
  formatGBP,
  formatLongDate,
  formatSoldDate,
  humanVisitor,
  isoDay,
  newVisitor,
  priceForm,
  readable,
  saleDayFromToday,
  submitPrice,
  tags,
  tomorrowIso,
  ukDate,
  watchForScripts,
} from '../support/api';

interface Address {
  postnumber: string;
  poststreet: string | null;
  posttown: string;
  postcode: string;
  unit?: string | null;
}

interface PriceResult {
  address: Address;
  saleDate: string;
  value: number;
  lastSale: { price: number; soldOn: string } | null;
  evidenceTo: string;
}

/** `lib/price.describeFullAddress`. */
function describeFullAddress(address: Address): string {
  const first = [address.postnumber, address.poststreet].filter(Boolean).map((part) => readable(String(part)));
  const line = (address.unit ? readable(address.unit) + ', ' : '') + first.join(' ');
  return line + ', ' + readable(address.posttown) + ', ' + address.postcode;
}

/** The result card: the section under the hero. */
function resultCard(page: Page) {
  return page.locator('section#result');
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
});

test.describe('valid', () => {
  test(
    'pricing a chosen home sends it and the date with the pass, and the card shows the API’s figures',
    tags('valid', ['/api/price'], { ui: true }),
    async ({ page }) => {
      const suggestions = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/addresses/search');
      const { saleDay } = await chooseHome(page);
      const chosen = ((await (await suggestions).json()) as Array<Address & { streetHint?: string | null }>)[0];

      const sent = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/price');
      const response = await submitPrice(page);
      const request = await sent;

      expect(request.method()).toBe('POST');
      expect(await request.headerValue('content-type')).toBe('application/json');
      expect(await request.headerValue('x-hpldn-pass')).toBeTruthy();
      const { streetHint: _hint, unit, ...rest } = chosen;
      expect(request.postDataJSON()).toEqual({ ...rest, ...(unit ? { unit } : {}), saleDate: isoDay(saleDay) });

      expect(response.status()).toBe(200);
      const body = (await response.json()) as PriceResult;
      expectMatchesOpenApi('POST', '/price', body);
      expect(body.saleDate).toBe(isoDay(saleDay));

      const card = resultCard(page);
      await expect(card.locator('#result-heading')).toHaveText(describeFullAddress(body.address));
      await expect(card.getByText('Price if sold on')).toContainText(formatLongDate(body.saleDate));
      await expect(card.locator('strong').first()).toHaveText(formatGBP(body.value));
      await expect(card.getByText(`Based on sales recorded up to ${formatSoldDate(body.evidenceTo)}`)).toBeVisible();
      if (body.lastSale) {
        await expect(card.getByText('Last sold').first()).toBeVisible();
        await expect(card.getByText(`${formatGBP(body.lastSale.price)} in ${formatSoldDate(body.lastSale.soldOn)}`)).toBeVisible();
      } else {
        await expect(card.getByText('No earlier sale on record')).toBeVisible();
      }
    },
  );

  test(
    'POST /api/price with a pass prices a known home for tomorrow, as openapi.json says',
    tags('valid', ['/api/price']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const response = await api.post('/api/price', { data: { ...HOME, saleDate: tomorrowIso() }, headers: { 'X-HPLDN-Pass': pass } });
      expect(response.status()).toBe(200);
      const body = (await response.json()) as PriceResult;
      expectMatchesOpenApi('POST', '/price', body);
      expect(body.address.postcode).toBe(HOME.postcode);
      expect(body.saleDate).toBe(tomorrowIso());
      expect(body.value).toBeGreaterThan(0);
      await api.dispose();
    },
  );
});

test.describe('invalid', () => {
  test(
    'without a chosen home the form asks for one and sends nothing',
    tags('invalid', ['/api/price'], { ui: true }),
    async ({ page }) => {
      let prices = 0;
      page.on('request', (r) => {
        if (new URL(r.url()).pathname === '/api/price') {
          prices += 1;
        }
      });
      await priceForm(page).getByRole('button', { name: 'Price this home' }).click();
      await expect(priceForm(page).getByText('Choose a home from the suggestions.')).toBeVisible();
      await expect(priceForm(page).getByText('Check 1 field above.')).toBeVisible();
      // Typed text that was never picked is not a home either.
      await priceForm(page).getByRole('combobox', { name: 'Address' }).fill('18 Barclay Road');
      await priceForm(page).getByRole('button', { name: 'Price this home' }).click();
      await expect(priceForm(page).getByText('Choose a home from the suggestions.')).toBeVisible();
      expect(prices).toBe(0);
    },
  );

  test(
    'a sale date of today or earlier, past the horizon, unreal or half typed is refused before sending',
    tags('invalid', ['/api/price'], { ui: true }),
    async ({ page }) => {
      await chooseHome(page);
      let prices = 0;
      page.on('request', (r) => {
        if (new URL(r.url()).pathname === '/api/price') {
          prices += 1;
        }
      });
      const { today, saleDay } = await saleDayFromToday(page);
      const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
      const beyond = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 5 * 365 + 1);
      const latest = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 5 * 365);
      const dateInput = priceForm(page).getByRole('textbox', { name: 'Sale date' });
      for (const [typed, message] of [
        [ukDate(today), `Choose a date from ${ukDate(saleDay)}.`],
        [ukDate(yesterday), `Choose a date from ${ukDate(saleDay)}.`],
        [ukDate(beyond), `Choose a date up to ${ukDate(latest)}.`],
        ['31/02/2027', 'There is no 31/02/2027. Check the day and month.'],
        ['12/1', 'Enter the date as DD/MM/YYYY.'],
      ]) {
        await dateInput.fill(typed);
        await priceForm(page).getByRole('button', { name: 'Price this home' }).click();
        await expect(priceForm(page).getByText(message), typed).toBeVisible();
      }
      expect(prices).toBe(0);
    },
  );

  for (const failure of [
    { name: '500', status: 500, json: { detail: 'Traceback (most recent call last)' }, message: 'Something went wrong on our side. Try again in a moment.', edit: false },
    { name: '503 with a sentence', status: 503, json: { detail: 'Pricing is busy. Try again in a minute.' }, message: 'Pricing is busy. Try again in a minute.', edit: false },
    { name: '422 with a sentence', status: 422, json: { detail: 'We do not hold enough records to price this home.' }, message: 'We do not hold enough records to price this home.', edit: true },
    { name: '429', status: 429, json: { detail: 'Too many' }, headers: { 'Retry-After': '2' }, message: 'One moment…', edit: false },
  ]) {
    test(
      `a ${failure.name} from /api/price shows "We could not price this home" with the right actions`,
      tags('invalid', ['/api/price'], { ui: true }),
      async ({ page }) => {
        let calls = 0;
        await page.route(
          (url) => url.pathname === '/api/price',
          (route: Route) => {
            calls += 1;
            return calls === 1
              ? route.fulfill({ status: failure.status, json: failure.json, headers: failure.headers })
              : route.fallback();
          },
        );
        await chooseHome(page);
        expect((await submitPrice(page)).status()).toBe(failure.status);
        const card = resultCard(page);
        await expect(card.getByRole('heading', { name: 'We could not price this home' })).toBeVisible();
        await expect(card.getByText(failure.message)).toBeVisible();
        await expect(page.getByText('Traceback')).toHaveCount(0);
        await expect(card.getByRole('button', { name: 'Change the search' })).toHaveCount(failure.edit ? 1 : 0);

        // "Try again" sends the same request, which the real API now answers.
        const again = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/price');
        await card.getByRole('button', { name: 'Try again' }).click();
        expect((await again).status()).toBe(200);
        await expect(card.getByText('Price if sold on')).toBeVisible();
      },
    );
  }

  test(
    'a price that never answers times out after 30 s with a message and a retry',
    tags('invalid', ['/api/price'], { ui: true }),
    async ({ page }) => {
      const held: Route[] = [];
      await page.clock.install();
      await page.reload();
      await page.route(
        (url) => url.pathname === '/api/price',
        (route: Route) => {
          held.push(route);
        },
      );
      await chooseHome(page);
      await priceForm(page).getByRole('button', { name: 'Price this home' }).click();
      await expect.poll(() => held.length).toBe(1);
      // lib/api PRICE_TIMEOUT_MS is 30 s.
      await page.clock.fastForward(31_000);
      const card = resultCard(page);
      await expect(card.getByText('This is taking longer than it should. Try again in a moment.')).toBeVisible();
      await expect(card.getByRole('button', { name: 'Try again' })).toBeVisible();
      await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
    },
  );

  test(
    'direct calls with a pass and bad bodies are 422s that do not repeat them; an unknown home is a 404',
    tags('invalid', ['/api/price']),
    async ({ playwright, baseURL }) => {
      const cases: Array<[string, unknown, string[]]> = [
        ['no sale date', { ...HOME }, []],
        ['today', { ...HOME, saleDate: isoDay(new Date()) }, []],
        ['not a date', { ...HOME, saleDate: 'next tuesday' }, ['next tuesday']],
        ['bad postcode', { ...HOME, postcode: 'NOTAPOSTCODE', saleDate: tomorrowIso() }, ['NOTAPOSTCODE']],
        ['blank number', { ...HOME, postnumber: '   ', saleDate: tomorrowIso() }, []],
        ['long number', { ...HOME, postnumber: '9'.repeat(101), saleDate: tomorrowIso() }, ['9'.repeat(101)]],
        ['long town', { ...HOME, posttown: 'Q'.repeat(61), saleDate: tomorrowIso() }, ['Q'.repeat(61)]],
        ['wrong types', { ...HOME, postnumber: ['18'], saleDate: 20271231 }, []],
        ['an array', [HOME], []],
      ];
      // The price limiter's burst is 6 a visitor: a new visitor every 5 calls.
      for (let start = 0; start < cases.length; start += 5) {
        const { api, pass } = await humanVisitor(playwright, baseURL);
        for (const [name, body, sent] of cases.slice(start, start + 5)) {
          const response = await api.post('/api/price', { data: body, headers: { 'X-HPLDN-Pass': pass } });
          expect(response.status(), name).toBe(422);
          await expectNoEcho(response, sent);
        }
        await api.dispose();
      }
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const notJson = await api.post('/api/price', {
        headers: { 'X-HPLDN-Pass': pass, 'Content-Type': 'application/json' },
        data: '{"postnumber": "18", <b>',
      });
      expect(notJson.status()).toBe(422);
      await expectNoEcho(notJson, ['<b>']);
      const unknown = await api.post('/api/price', {
        data: { ...HOME, postnumber: '9999', unit: null, saleDate: tomorrowIso() },
        headers: { 'X-HPLDN-Pass': pass },
      });
      expect(unknown.status()).toBe(404);
      await expectSafeRefusal(unknown, ['9999']);
      await api.dispose();
    },
  );

  test(
    'POST /api/price without a pass is refused by the API’s gate',
    tags('invalid', ['/api/price']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const response = await api.post('/api/price', { data: { ...HOME, saleDate: tomorrowIso() } });
      expect(response.status()).toBe(403);
      expect(response.headers()['x-hpldn-captcha']).toBe('required');
      expect(response.headers()['x-hpldn-reject']).toBeUndefined();
      await api.dispose();
    },
  );
});

test.describe('red team', () => {
  test(
    'SQL meta-characters, wildcards, unicode, NUL and script in every field: refused or not found, never a 500 or an echo',
    tags('redteam', ['/api/price']),
    async ({ playwright, baseURL }) => {
      test.slow();
      const fields = ['postnumber', 'poststreet', 'posttown', 'unit', 'postcode'] as const;
      for (const value of [...HOSTILE_STRINGS, ...XSS_PAYLOADS]) {
        // Five calls a visitor, inside the price limiter's burst of 6.
        const { api, pass } = await humanVisitor(playwright, baseURL);
        for (const field of fields) {
          const response = await api.post('/api/price', {
            data: { ...HOME, [field]: value, saleDate: tomorrowIso() },
            headers: { 'X-HPLDN-Pass': pass },
          });
          const where = `${field}=${JSON.stringify(value)}`;
          expect([200, 404, 422], `${where} answered ${response.status()}`).toContain(response.status());
          if (response.status() === 200) {
            // Matched after normalisation: the home priced is the register's, not the input.
            const body = (await response.json()) as PriceResult;
            expectMatchesOpenApi('POST', '/price', body);
            expect(JSON.stringify(body.address), where).not.toContain(value.trim() || '\u0000');
          } else {
            await expectSafeRefusal(response, [value.trim()]);
          }
        }
        await api.dispose();
      }
    },
  );

  test(
    'a body over 4 kB is refused by nginx with 413 before the API sees it',
    tags('redteam', ['/api/price']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const big = await api.post('/api/price', {
        data: { ...HOME, saleDate: tomorrowIso(), postnumber: 'x'.repeat(5_000) },
        headers: { 'X-HPLDN-Pass': pass },
      });
      expect(big.status()).toBe(413);
      expect(await big.text()).not.toContain('xxxx');
      // Just under the limit reaches the API, which refuses the field.
      const under = await api.post('/api/price', {
        data: { ...HOME, saleDate: tomorrowIso(), postnumber: 'x'.repeat(3_900) },
        headers: { 'X-HPLDN-Pass': pass },
      });
      expect(under.status()).toBe(422);
      await api.dispose();
    },
  );

  test(
    'methods other than POST, and bodies that are not JSON, are refused without a 500',
    tags('redteam', ['/api/price']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      for (const method of ['GET', 'PUT', 'PATCH', 'DELETE']) {
        expect((await api.fetch('/api/price', { method })).status(), method).toBe(403);
      }
      const form = await api.post('/api/price', {
        form: { postnumber: '18', saleDate: tomorrowIso() },
        headers: { 'X-HPLDN-Pass': pass },
      });
      expect(form.status()).toBe(422);
      const plain = await api.post('/api/price', {
        data: JSON.stringify({ ...HOME, saleDate: tomorrowIso() }),
        headers: { 'X-HPLDN-Pass': pass, 'Content-Type': 'text/plain' },
      });
      expect([200, 415, 422]).toContain(plain.status());
      await api.dispose();
    },
  );

  test(
    'a price answer carrying script is shown as text and runs nothing',
    tags('redteam', ['/api/price'], { ui: true }),
    async ({ page, visitorIp }) => {
      const watch = await watchForScripts(page);
      await page.reload();
      const payload = XSS_PAYLOADS.join(' ');
      await page.route(
        (url) => url.pathname === '/api/price',
        async (route: Route) => {
          const response = await fetchAsVisitor(route, visitorIp);
          const body = (await response.json()) as PriceResult & { estimatedFrom: string | null };
          body.address = { ...body.address, poststreet: payload, posttown: payload };
          await route.fulfill({ response, json: body });
        },
      );
      await chooseHome(page);
      expect((await submitPrice(page)).status()).toBe(200);
      await expect(resultCard(page).locator('#result-heading')).toContainText(XSS_PAYLOADS[0]);
      await expectNoScriptRan(page, watch);
    },
  );
});
