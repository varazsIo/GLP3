/**
 * `AddressSearch` (the price form's address box): `GET /api/addresses/search`
 * for a partial address, `GET /api/addresses?postcode=` for a full postcode.
 * Only the postcode lookup wants the reCAPTCHA pass: a first search must not
 * wait for Google (guard.yaml `captcha.gated_routes`).
 *
 * The rate-limit and ban checks live here because the search is what a
 * scraper would hammer: nginx's search zone, then the guard's ban.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '../fixtures';
import type { APIResponse, Page, Route } from '@playwright/test';
import {
  ADDRESS_POSTCODE,
  ADDRESS_QUERY,
  HOSTILE_STRINGS,
  REPO_ROOT,
  VPS_DIR,
  XSS_PAYLOADS,
  expectMatchesOpenApi,
  expectNoEcho,
  expectNoScriptRan,
  expectSafeRefusal,
  humanVisitor,
  newVisitor,
  priceForm,
  readable,
  searchFor,
  tags,
  watchForScripts,
} from '../support/api';

interface Suggestion {
  postnumber: string;
  poststreet: string | null;
  posttown: string;
  postcode: string;
  unit?: string | null;
  streetHint?: string | null;
}

/** `lib/price.describeFullAddress`, which each option is labelled with. */
function describeFullAddress(address: Suggestion): string {
  const street = address.poststreet ?? address.streetHint ?? null;
  const first = [address.postnumber, street].filter(Boolean).map((part) => readable(String(part)));
  const line = (address.unit ? readable(address.unit) + ', ' : '') + first.join(' ');
  return (address.posttown ? line + ', ' + readable(address.posttown) : line) + ', ' + address.postcode;
}

/** The page shows at most 8 suggestions (`ADDRESS_SUGGESTION_LIMIT`). */
const SHOWN = 8;
const SEARCH = '/api/addresses/search?q=18+Barclay';
const BAN_TEXT = 'Access from your network is temporarily restricted because of unusual traffic. It lifts automatically.\n';
/** guard.yaml's `bans.rules.throttled.count`: the 429s that earn a ban. */
const THROTTLED_COUNT = Number(
  /throttled:\s*\{\s*count:\s*(\d+)/.exec(readFileSync(path.join(VPS_DIR, 'guard.yaml'), 'utf8'))?.[1] ?? NaN,
);

function addressBox(page: Page) {
  return priceForm(page).getByRole('combobox', { name: 'Address' });
}

/** Whether a 429 is nginx's own page rather than the API's JSON. */
function fromNginx(response: APIResponse): boolean {
  return (response.headers()['content-type'] ?? '').startsWith('text/html');
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
});

test.describe('valid', () => {
  test(
    'typing an address sends one search without a pass, and lists what the API answered',
    tags('valid', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      const sent = page.waitForRequest((request) => new URL(request.url()).pathname === '/api/addresses/search');
      const response = await searchFor(page, ADDRESS_QUERY);
      const request = await sent;

      const url = new URL(request.url());
      expect(request.method()).toBe('GET');
      expect(url.searchParams.get('q')).toBe(ADDRESS_QUERY);
      expect(url.searchParams.get('limit')).toBe(String(SHOWN));
      expect(await request.headerValue('x-hpldn-pass')).toBeNull();

      expect(response.status()).toBe(200);
      const body = (await response.json()) as Suggestion[];
      expectMatchesOpenApi('GET', '/addresses/search', body);
      expect(body.length).toBeGreaterThan(0);
      expect(body.length).toBeLessThanOrEqual(SHOWN);

      const options = page.getByRole('option');
      await expect(options).toHaveCount(body.length);
      for (let i = 0; i < body.length; i += 1) {
        await expect(options.nth(i)).toHaveAccessibleName(describeFullAddress(body[i]));
      }

      // Picking one fills the box with it and sends nothing more.
      let later = 0;
      page.on('request', (r) => {
        if (new URL(r.url()).pathname.startsWith('/api/addresses')) {
          later += 1;
        }
      });
      await options.first().click();
      await expect(addressBox(page)).toHaveValue(describeFullAddress(body[0]));
      await page.waitForTimeout(800);
      expect(later).toBe(0);
    },
  );

  test(
    'typing a full postcode lists the homes in it from /api/addresses',
    tags('valid', ['/api/addresses'], { ui: true }),
    async ({ page }) => {
      const sent = page.waitForRequest((request) => new URL(request.url()).pathname === '/api/addresses');
      const response = await searchFor(page, ADDRESS_POSTCODE.toLowerCase().replace(' ', ''));
      const request = await sent;

      expect(new URL(request.url()).searchParams.get('postcode')).toBe(ADDRESS_POSTCODE);
      expect(await request.headerValue('x-hpldn-pass')).toBeTruthy();
      expect(response.status()).toBe(200);
      const body = (await response.json()) as Suggestion[];
      expectMatchesOpenApi('GET', '/addresses', body);
      expect(body.length).toBeGreaterThan(0);
      for (const home of body) {
        expect(home.postcode).toBe(ADDRESS_POSTCODE);
      }

      const options = page.getByRole('option');
      await expect(options).toHaveCount(Math.min(SHOWN, body.length));
      await expect(options.first()).toHaveAccessibleName(describeFullAddress(body[0]));
    },
  );
});

test.describe('invalid', () => {
  test(
    'fewer than 3 characters, or only punctuation, send nothing',
    tags('invalid', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      let searches = 0;
      page.on('request', (request) => {
        if (new URL(request.url()).pathname.startsWith('/api/addresses')) {
          searches += 1;
        }
      });
      for (const text of ['1', '18', ' a ', '!!!', '<>&']) {
        await addressBox(page).fill(text);
        await page.waitForTimeout(700);
      }
      expect(searches).toBe(0);
      await expect(page.getByRole('option')).toHaveCount(0);
    },
  );

  test(
    'an address that matches nothing says so',
    tags('invalid', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      const response = await searchFor(page, 'Zzqxv Nowhere Lane');
      expect(response.status()).toBe(200);
      expect(await response.json()).toEqual([]);
      await expect(priceForm(page).getByText('No homes found. Try the postcode')).toBeVisible();
      await expect(page.getByRole('option')).toHaveCount(0);
    },
  );

  test(
    'a well-formed postcode with no homes we price shows the API’s sentence, without a retry',
    tags('invalid', ['/api/addresses'], { ui: true }),
    async ({ page }) => {
      const response = await searchFor(page, 'ZZ9 9ZZ');
      expect(response.status()).toBe(404);
      await expect(priceForm(page).getByText('We could not find any homes we can price in ZZ9 9ZZ')).toBeVisible();
      await expect(priceForm(page).getByRole('button', { name: 'Search again' })).toHaveCount(0);
    },
  );

  for (const failure of [
    { name: '500', status: 500, json: { detail: 'boom' }, message: 'Something went wrong on our side. Try again in a moment.' },
    { name: '503 with a sentence', status: 503, json: { detail: 'Search is resting.' }, message: 'Search is resting.' },
  ]) {
    test(
      `a ${failure.name} from the search shows the message with "Search again", which searches again`,
      tags('invalid', ['/api/addresses/search'], { ui: true }),
      async ({ page }) => {
        let calls = 0;
        await page.route(
          (url) => url.pathname === '/api/addresses/search',
          (route: Route) => {
            calls += 1;
            return calls === 1 ? route.fulfill({ status: failure.status, json: failure.json }) : route.fallback();
          },
        );
        await searchFor(page, ADDRESS_QUERY);
        await expect(priceForm(page).getByText(failure.message)).toBeVisible();
        await expect(page.getByText('boom')).toHaveCount(0);

        const again = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/addresses/search');
        await priceForm(page).getByRole('button', { name: 'Search again' }).click();
        expect((await again).status()).toBe(200);
        await expect(page.getByRole('option').first()).toBeVisible();
      },
    );
  }

  test(
    'a 429 shows "One moment…" and searches again by itself after Retry-After',
    tags('invalid', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      let calls = 0;
      await page.route(
        (url) => url.pathname === '/api/addresses/search',
        (route: Route) => {
          calls += 1;
          return calls === 1
            ? route.fulfill({ status: 429, headers: { 'Retry-After': '1' }, json: { detail: 'Too many searches' } })
            : route.fallback();
        },
      );
      await searchFor(page, ADDRESS_QUERY);
      await expect(priceForm(page).getByText('One moment…')).toBeVisible();
      await expect(page.getByRole('option').first()).toBeVisible({ timeout: 5_000 });
      expect(calls).toBe(2);
    },
  );

  test(
    'a search that never answers times out with a message and "Search again"',
    tags('invalid', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      const held: Route[] = [];
      await page.clock.install();
      await page.reload();
      await page.route(
        (url) => url.pathname === '/api/addresses/search',
        (route: Route) => {
          held.push(route);
        },
      );
      await addressBox(page).fill(ADDRESS_QUERY);
      await expect.poll(async () => {
        await page.clock.runFor(500);
        return held.length;
      }).toBe(1);
      await page.clock.fastForward(11_000);
      await expect(priceForm(page).getByText('This is taking longer than it should. Try again in a moment.')).toBeVisible();
      await expect(priceForm(page).getByRole('button', { name: 'Search again' })).toBeVisible();
      await Promise.all(held.map((route) => route.abort().catch(() => undefined)));
    },
  );

  test(
    'direct calls with a pass and bad parameters are 422s that do not repeat them',
    tags('invalid', ['/api/addresses/search', '/api/addresses']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const headers = { 'X-HPLDN-Pass': pass };
      const cases: Array<[string, string[]]> = [
        ['/api/addresses/search', []],
        ['/api/addresses/search?q=ab', []],
        [`/api/addresses/search?q=${'barclay '.repeat(9)}`, ['barclay barclay barclay']],
        ['/api/addresses/search?q=barclay&limit=0', []],
        ['/api/addresses/search?q=barclay&limit=11', []],
        ['/api/addresses/search?q=barclay&limit=eight', ['eight']],
        ['/api/addresses', []],
        ['/api/addresses?postcode=CR0', []],
        ['/api/addresses?postcode=SW9%209Z', ['SW9 9Z']],
        ['/api/addresses?postcode=NOTAPOSTCODE', ['NOTAPOSTCODE']],
      ];
      for (const [url, sent] of cases) {
        const response = await api.get(url, { headers });
        expect(response.status(), url).toBe(422);
        await expectNoEcho(response, sent);
      }
      await api.dispose();
    },
  );

  test(
    'the postcode lookup refuses a caller without a pass, telling the page to get one; the search does not ask for one',
    tags('invalid', ['/api/addresses/search', '/api/addresses']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const lookup = `/api/addresses?postcode=${encodeURIComponent(ADDRESS_POSTCODE)}`;
      const refused = await api.get(lookup);
      expect(refused.status(), lookup).toBe(403);
      expect(refused.headers()['x-hpldn-captcha'], lookup).toBe('required');
      expect(refused.headers()['x-hpldn-reject'], lookup).toBeUndefined();

      const search = await api.get(SEARCH);
      expect(search.status(), SEARCH).toBe(200);
      expect(search.headers()['x-hpldn-captcha'], SEARCH).toBeUndefined();
      await api.dispose();
    },
  );
});

test.describe('red team', () => {
  test(
    'SQL meta-characters, LIKE wildcards, unicode and NUL are refused or matched literally, never a 500 or an echo',
    tags('redteam', ['/api/addresses/search', '/api/addresses']),
    async ({ playwright, baseURL }) => {
      for (const value of HOSTILE_STRINGS) {
        // A visitor per value: each sends three calls, well inside the zones.
        const { api, pass } = await humanVisitor(playwright, baseURL);
        const headers = { 'X-HPLDN-Pass': pass };
        for (const url of [
          `/api/addresses/search?q=${encodeURIComponent(value)}`,
          `/api/addresses/search?q=${encodeURIComponent('18 Barclay ' + value)}`,
          `/api/addresses?postcode=${encodeURIComponent(value)}`,
        ]) {
          const response = await api.get(url, { headers });
          expect([200, 404, 422], `${url} answered ${response.status()}`).toContain(response.status());
          if (response.status() === 200) {
            // A wildcard never widens the search: at most what "18 Barclay" alone would find.
            const body = (await response.json()) as Suggestion[];
            expectMatchesOpenApi('GET', new URL(url, 'http://x').pathname.slice(4), body);
            expect(body.length).toBeLessThanOrEqual(SHOWN);
          } else {
            await expectSafeRefusal(response, [value.trim()]);
          }
        }
        await api.dispose();
      }
    },
  );

  test(
    'the search answers at most 10 homes, whatever limit is asked',
    tags('redteam', ['/api/addresses/search']),
    async ({ playwright, baseURL }) => {
      const { api, pass } = await humanVisitor(playwright, baseURL);
      const headers = { 'X-HPLDN-Pass': pass };
      const ten = await api.get('/api/addresses/search?q=Barclay+Road&limit=10', { headers });
      expect(ten.status()).toBe(200);
      expect(((await ten.json()) as unknown[]).length).toBeLessThanOrEqual(10);
      for (const limit of ['11', '100', '1e3', '-1']) {
        expect((await api.get(`/api/addresses/search?q=Barclay+Road&limit=${limit}`, { headers })).status(), limit).toBe(422);
      }
      await api.dispose();
    },
  );

  test(
    'script typed in the box stays text, is never sent as markup, and runs nothing',
    tags('redteam', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      const watch = await watchForScripts(page);
      await page.reload();
      const queries: string[] = [];
      page.on('request', (request) => {
        const url = new URL(request.url());
        if (url.pathname === '/api/addresses/search') {
          queries.push(url.searchParams.get('q') ?? '');
        }
      });
      for (const payload of XSS_PAYLOADS) {
        await addressBox(page).fill(payload);
        await expect(addressBox(page)).toHaveValue(payload);
        await page.waitForTimeout(700);
      }
      for (const q of queries) {
        expect(q).not.toMatch(/[<>"()]/);
      }
      await expectNoScriptRan(page, watch);
    },
  );

  test(
    'suggestions carrying script are listed as text and run nothing',
    tags('redteam', ['/api/addresses/search'], { ui: true }),
    async ({ page }) => {
      const watch = await watchForScripts(page);
      await page.reload();
      const hostile: Suggestion[] = XSS_PAYLOADS.map((payload, i) => ({
        postnumber: String(i + 1),
        poststreet: payload,
        posttown: payload,
        postcode: 'CR0 1JN',
        unit: i % 2 ? payload : null,
        streetHint: null,
      }));
      await page.route(
        (url) => url.pathname === '/api/addresses/search',
        (route: Route) => route.fulfill({ status: 200, json: hostile }),
      );
      await searchFor(page, ADDRESS_QUERY);
      const options = page.getByRole('option');
      await expect(options).toHaveCount(hostile.length);
      for (let i = 0; i < hostile.length; i += 1) {
        await expect(options.nth(i)).toHaveAccessibleName(describeFullAddress(hostile[i]));
        await expect(options.nth(i)).toContainText(XSS_PAYLOADS[i]);
      }
      await options.first().click();
      await expect(addressBox(page)).toHaveValue(describeFullAddress(hostile[0]));
      await expectNoScriptRan(page, watch);
    },
  );

  test(
    'methods other than GET are refused',
    tags('redteam', ['/api/addresses/search', '/api/addresses']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      for (const url of [SEARCH, '/api/addresses?postcode=CR0+1JN']) {
        for (const method of ['POST', 'PUT', 'DELETE']) {
          expect((await api.fetch(url, { method, data: {} })).status(), `${method} ${url}`).toBe(403);
        }
      }
      await api.dispose();
    },
  );

  test(
    'a rapid burst of searches meets nginx’s 429s, with the API’s reason hidden',
    tags('redteam', ['/api/addresses/search']),
    async ({ playwright, baseURL }) => {
      const { api } = await newVisitor(playwright, baseURL);
      const responses: APIResponse[] = [];
      for (let i = 0; i < 25; i += 1) {
        responses.push(await api.get(SEARCH));
      }
      const limited = responses.filter((r) => r.status() === 429);
      // guard.yaml rate_limits.nginx: the search zone's burst of 10, then 20 a minute.
      expect(limited.length).toBeGreaterThanOrEqual(10);
      expect(limited.every(fromNginx)).toBe(true);
      for (const response of responses) {
        expect(response.headers()['x-hpldn-reject']).toBeUndefined();
      }
      await api.dispose();
    },
  );

  test.describe('bans', () => {
    test.afterAll(() => {
      execFileSync('uv', ['run', 'python', '-m', 'hpldn.backend.cloud.local', 'unban', '--all', '--forget'], {
        cwd: REPO_ROOT,
        stdio: 'inherit',
      });
    });

    test(
      'a visitor who keeps hitting the limits is banned within seconds, and nobody else is',
      tags('redteam', ['/api/addresses/search', '/api/areas', '/api/session/config']),
      async ({ playwright, baseURL }) => {
        test.slow();
        expect(THROTTLED_COUNT).toBeGreaterThan(0);
        const abuser = await newVisitor(playwright, baseURL);
        const bystander = await newVisitor(playwright, baseURL);

        let limited = 0;
        for (let i = 0; i < THROTTLED_COUNT * 3 && limited < THROTTLED_COUNT + 5; i += 1) {
          if ((await abuser.api.get(SEARCH)).status() === 429) {
            limited += 1;
          }
        }
        expect(limited).toBeGreaterThanOrEqual(THROTTLED_COUNT);

        // The guard bans on its next tick, then the reload watcher reloads nginx.
        await expect
          .poll(
            async () => {
              const response = await abuser.api.get('/api/areas');
              return [response.status(), await response.text()];
            },
            { timeout: 15_000, intervals: [500] },
          )
          .toEqual([403, BAN_TEXT]);

        // Everything from that address gets the ban text, the page included.
        for (const url of ['/', '/api/session/config', SEARCH]) {
          const response = await abuser.api.get(url);
          expect(response.status(), url).toBe(403);
          expect(await response.text(), url).toBe(BAN_TEXT);
        }
        expect((await bystander.api.get('/api/areas')).status()).toBe(200);

        // Lifted by the operator's unban, the address is served again.
        execFileSync('uv', ['run', 'python', '-m', 'hpldn.backend.cloud.local', 'unban', abuser.ip, '--forget'], {
          cwd: REPO_ROOT,
          stdio: 'inherit',
        });
        await expect
          .poll(async () => (await abuser.api.get('/api/areas')).status(), { timeout: 15_000, intervals: [500] })
          .toBe(200);
        await abuser.api.dispose();
        await bystander.api.dispose();
      },
    );
  });
});
