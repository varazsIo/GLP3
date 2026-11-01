/**
 * Shared helpers for the per-component specs in `e2e/api/`.
 *
 * Every test there is tagged with exactly one of `@valid`, `@invalid` or
 * `@redteam`, plus one `@endpoint:<path>` per `/api` route it exercises (a
 * concrete path such as `/api/areas/SW11/comparables`). `e2e/coverage.spec.ts`
 * matches those paths against `src/setupProxy.js`'s `EXPOSED` routes and fails
 * when an exposed route lacks any of the three kinds. `@ui` marks a test that
 * drives the page, which the 390 px mobile project runs as well.
 *
 * Responses are checked against the API's own `openapi.json`, fetched from the
 * loopback (:8000) by `global-setup.ts` into `OPENAPI_CACHE`, with ajv's JSON
 * Schema 2020-12 validator (OpenAPI 3.1's dialect).
 */
import { createHash, createHmac, randomBytes } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import Ajv2020 from 'ajv8/dist/2020';
import type { ValidateFunction } from 'ajv8';
import { expect } from '@playwright/test';
import type { APIRequestContext, APIResponse, Page, PlaywrightWorkerArgs, Response, Route } from '@playwright/test';

export const FRONTEND_DIR = path.resolve(__dirname, '../..');
export const REPO_ROOT = path.resolve(FRONTEND_DIR, '../../..');
export const VPS_DIR = path.join(REPO_ROOT, 'src/hpldn/backend/cloud/vps');
/** gunicorn's `[ACCESS]` lines and nginx's `[EDGE]` lines (`cloud/local/render.py`). */
export const API_LOG = path.join(REPO_ROOT, 'tmp/local-stack/logs/api.log');
/** Where `global-setup.ts` saves the API's `openapi.json`. */
export const OPENAPI_CACHE = path.join(FRONTEND_DIR, 'node_modules/.cache/hpldn-e2e/openapi.json');

/** What a test checks: the happy path, a rejected input, or an attack. */
export type Kind = 'valid' | 'invalid' | 'redteam';

/** The prefix of the tag naming an `/api` path a test calls. */
export const ENDPOINT_TAG = '@endpoint:';

/**
 * The `details` of a test: its kind, the `/api` paths it calls and, with
 * `ui`, the tag that also runs it in the mobile project.
 */
export function tags(kind: Kind, endpoints: string[], options: { ui?: boolean } = {}): { tag: string[] } {
  return {
    tag: ['@' + kind, ...endpoints.map((endpoint) => ENDPOINT_TAG + endpoint), ...(options.ui ? ['@ui'] : [])],
  };
}

// ---------------------------------------------------------------------------
// The response contract: openapi.json
// ---------------------------------------------------------------------------

const OPENAPI_ID = 'https://hpldn.local/openapi.json';
let ajv: Ajv2020 | null = null;
const validators = new Map<string, ValidateFunction>();

function openApiAjv(): Ajv2020 {
  if (ajv) {
    return ajv;
  }
  if (!existsSync(OPENAPI_CACHE)) {
    throw new Error(`${OPENAPI_CACHE} is missing: global-setup.ts saves it from the API on :8000`);
  }
  const document = JSON.parse(readFileSync(OPENAPI_CACHE, 'utf8')) as Record<string, unknown>;
  // OpenAPI's own fields (info, paths, ...) are not JSON Schema keywords: not strict.
  ajv = new Ajv2020({ strict: false, allErrors: true });
  // The formats FastAPI emits, checked as the API writes them.
  ajv.addFormat('date', /^\d{4}-\d{2}-\d{2}$/);
  ajv.addFormat('date-time', /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  ajv.addFormat('uri', /^https?:\/\/\S+$/);
  ajv.addSchema({ ...document, $id: OPENAPI_ID });
  return ajv;
}

/** JSON Pointer escaping for one segment (RFC 6901). */
function pointer(segment: string): string {
  return segment.replace(/~/g, '~0').replace(/\//g, '~1');
}

/**
 * Checks `body` against what `openapi.json` says `method apiPath` answers with `status`.
 *
 * @param apiPath The API's own path template, without `/api`, e.g. `/areas/{code}`.
 */
export function expectMatchesOpenApi(method: string, apiPath: string, body: unknown, status = 200): void {
  const key = `${method.toLowerCase()} ${apiPath} ${status}`;
  let validate = validators.get(key);
  if (!validate) {
    const ref =
      `${OPENAPI_ID}#/paths/${pointer(apiPath)}/${method.toLowerCase()}` +
      `/responses/${status}/content/${pointer('application/json')}/schema`;
    validate = openApiAjv().compile({ $ref: ref });
    validators.set(key, validate);
  }
  const valid = validate(body);
  expect(valid ? [] : validate.errors, `${method} ${apiPath} answered a body openapi.json does not allow`).toEqual([]);
}

// ---------------------------------------------------------------------------
// Visitors and passes
// ---------------------------------------------------------------------------

/**
 * A random host of 198.18.0.0/15 (RFC 2544's benchmarking block): no real
 * visitor has one, and 131,072 of them make a reuse within the guard's windows
 * unlikely, so each API-level visitor starts with clean limits and no strikes.
 */
export function benchmarkIp(): string {
  const [a, b] = randomBytes(2);
  return `198.${18 + (a & 1)}.${b}.${1 + Math.floor(Math.random() * 254)}`;
}

/** A user agent nginx's `bad_ua` list lets through, as a browser's is. */
export const BROWSER_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36';

/** An API client that is a new visitor at `ip` (a random benchmark host by default). */
export async function newVisitor(
  playwright: PlaywrightWorkerArgs['playwright'],
  baseURL: string | undefined,
  options: { ip?: string; userAgent?: string; headers?: Record<string, string> } = {},
): Promise<{ api: APIRequestContext; ip: string }> {
  const ip = options.ip ?? benchmarkIp();
  const api = await playwright.request.newContext({
    baseURL,
    userAgent: options.userAgent ?? BROWSER_UA,
    extraHTTPHeaders: { 'CF-Connecting-IP': ip, ...(options.headers ?? {}) },
  });
  return { api, ip };
}

/** Trades the fake Google's `e2e-ok` for a real pass, as the page does. */
export async function earnPass(api: APIRequestContext): Promise<string> {
  const response = await api.post('/api/session', { data: { token: 'e2e-ok' } });
  expect(response.status(), 'POST /api/session with e2e-ok').toBe(200);
  const body = (await response.json()) as { pass: string };
  return body.pass;
}

/** A new visitor who has passed the check: its client, address and pass. */
export async function humanVisitor(
  playwright: PlaywrightWorkerArgs['playwright'],
  baseURL: string | undefined,
  options: { ip?: string } = {},
): Promise<{ api: APIRequestContext; ip: string; pass: string }> {
  const visitor = await newVisitor(playwright, baseURL, options);
  return { ...visitor, pass: await earnPass(visitor.api) };
}

/** `develop.env`'s lines, `KEY=value`, comments and blanks skipped. */
function readEnvFile(file: string): Record<string, string> {
  if (!existsSync(file)) {
    return {};
  }
  const values: Record<string, string> = {};
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    const match = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (match && !line.trim().startsWith('#')) {
      values[match[1]] = match[2].replace(/^(['"])(.*)\1$/, '$2');
    }
  }
  return values;
}

/**
 * A setting of the local stack: the environment's, else `develop.env`'s, as
 * `cloud.local up` reads them. Fails when neither has it.
 */
export function stackSetting(key: 'HPLDN_PASS_SECRET' | 'HPLDN_API_KEY'): string {
  const value = process.env[key] ?? readEnvFile(path.join(REPO_ROOT, 'develop.env'))[key];
  if (!value) {
    throw new Error(`${key} is set neither in the environment nor in develop.env`);
  }
  return value;
}

/** The secret the local stack signs passes with. */
export function passSecret(): string {
  return stackSetting('HPLDN_PASS_SECRET');
}

function b64url(data: Buffer | string): string {
  return Buffer.from(data).toString('base64url');
}

/** `guard/captcha.address_hash` for an IPv4 address: a truncated SHA-256. */
function addressHash(ip: string): string {
  return createHash('sha256').update(ip).digest('hex').slice(0, 16);
}

/**
 * A pass signed the way `guard/captcha.issue_pass` signs one, with any expiry:
 * a test that holds the secret can mint an expired one.
 */
export function signPass(ip: string, expiresAtS: number, secret = passSecret()): string {
  const payload = b64url(JSON.stringify({ v: 1, exp: Math.floor(expiresAtS), iph: addressHash(ip) }));
  const signature = createHmac('sha256', secret).update(payload).digest('base64url');
  return `${payload}.${signature}`;
}

// ---------------------------------------------------------------------------
// Hostile input
// ---------------------------------------------------------------------------

/** SQL meta-characters, LIKE wildcards, unicode tricks and NUL, for every text field. */
export const HOSTILE_STRINGS: ReadonlyArray<string> = [
  "' OR '1'='1",
  "18'; DROP TABLE production.london_houses;--",
  '%',
  '_',
  '%_%',
  '\\',
  '18\u0000ROAD',
  '‮18 ROAD‭',
  '18 ŔOAD \u{1F3E0}',
  '﻿​',
];

/** Script payloads for every field: each must be shown as text and never run. */
export const XSS_PAYLOADS: ReadonlyArray<string> = [
  '<img src=x onerror=alert(1)>',
  '"><script>alert(1)</script>',
  '<svg onload=alert(1)>',
  "javascript:alert('x')",
];

/**
 * Collects what an injected script would set off: dialogs (dismissed so the
 * page never blocks) and Content-Security-Policy violations.
 *
 * Call before `page.goto`.
 */
export async function watchForScripts(page: Page): Promise<{ dialogs: string[]; violations: () => Promise<string[]> }> {
  const dialogs: string[] = [];
  page.on('dialog', (dialog) => {
    dialogs.push(`${dialog.type()}: ${dialog.message()}`);
    void dialog.dismiss().catch(() => undefined);
  });
  await page.addInitScript(() => {
    const seen: string[] = [];
    (window as unknown as { __cspViolations: string[] }).__cspViolations = seen;
    document.addEventListener('securitypolicyviolation', (event) => {
      seen.push(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  return {
    dialogs,
    violations: () =>
      page.evaluate(() => (window as unknown as { __cspViolations?: string[] }).__cspViolations ?? []),
  };
}

/** Asserts nothing injected ran: no dialog, no CSP violation, no element a payload would add. */
export async function expectNoScriptRan(
  page: Page,
  watch: { dialogs: string[]; violations: () => Promise<string[]> },
): Promise<void> {
  expect(watch.dialogs, 'a payload opened a dialog').toEqual([]);
  expect(await watch.violations(), 'the page broke its Content-Security-Policy').toEqual([]);
  expect(await page.locator('img[src="x"], script:not([src]):text-matches("alert"), svg[onload]').count()).toBe(0);
}

/**
 * Asserts a 422 names the problem without repeating what was sent: none of
 * `sent` appears in the body, and its error list holds only type, location and
 * message (`api/main.validation_error`).
 */
export async function expectNoEcho(response: APIResponse, sent: ReadonlyArray<string>): Promise<void> {
  const text = await response.text();
  for (const value of sent) {
    if (value.length >= 3) {
      expect(text, `the ${response.status()} repeats ${JSON.stringify(value)}`).not.toContain(value);
    }
  }
  if (response.status() === 422) {
    const detail = (JSON.parse(text) as { detail: unknown }).detail;
    if (Array.isArray(detail)) {
      for (const error of detail) {
        expect(Object.keys(error as object).sort()).toEqual(['loc', 'msg', 'type']);
      }
    } else {
      expect(typeof detail).toBe('string');
    }
  }
}

/** What a server error page or a leaked exception would show. */
const INTERNALS = /Traceback|psycopg|sqlalchemy|asyncpg|File "\/|line \d+, in |Internal Server Error/;

/**
 * Asserts a refusal of hostile input is a safe one: a 422 never repeats the
 * input (`expectNoEcho`); any other answer is JSON with a string `detail` and
 * no internals. A 404 may name the home it could not find: that sentence is
 * JSON, and the page shows it as text (the XSS checks).
 */
export async function expectSafeRefusal(response: APIResponse, sent: ReadonlyArray<string>): Promise<void> {
  expect(response.status(), 'a server error').toBeLessThan(500);
  const text = await response.text();
  expect(text).not.toMatch(INTERNALS);
  if (response.status() === 422) {
    await expectNoEcho(response, sent);
    return;
  }
  expect(response.headers()['content-type'] ?? '').toContain('application/json');
  expect(typeof (JSON.parse(text) as { detail?: unknown }).detail).toBe('string');
}

/**
 * Forwards a routed `/api` call to the stack as the test's visitor and returns
 * the answer: `route.fetch()` alone skips the context route that adds the
 * visitor's `CF-Connecting-IP`, so a gated call would carry a pass for another address.
 */
export function fetchAsVisitor(route: Route, visitorIp: string) {
  return route.fetch({ headers: { ...route.request().headers(), 'cf-connecting-ip': visitorIp } });
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

/** Waits for the next answer to `method /api<path>` (the query is ignored). */
export function apiResponse(page: Page, method: string, apiPath: string): Promise<Response> {
  return page.waitForResponse(
    (response) => response.request().method() === method && new URL(response.url()).pathname === '/api' + apiPath,
  );
}

/** Routes `/api<path>` (any query) to a fixed answer for this page. */
export async function stub(
  page: Page,
  apiPath: string,
  answer: { status: number; json?: unknown; body?: string; headers?: Record<string, string> },
): Promise<void> {
  await page.route(
    (url) => url.pathname === '/api' + apiPath,
    (route: Route) =>
      route.fulfill({
        status: answer.status,
        headers: answer.headers,
        ...(answer.json !== undefined ? { json: answer.json } : { body: answer.body ?? '' }),
      }),
  );
}

/** Formats a day as the date fields show it, DD/MM/YYYY. */
export function ukDate(day: Date): string {
  const dd = String(day.getDate()).padStart(2, '0');
  const mm = String(day.getMonth() + 1).padStart(2, '0');
  return dd + '/' + mm + '/' + day.getFullYear();
}

/** An ISO day, YYYY-MM-DD, in local time. */
export function isoDay(day: Date): string {
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
}

/** Today in the browser's clock, and the first day the forms accept (tomorrow). */
export async function saleDayFromToday(page: Page): Promise<{ today: Date; saleDay: Date }> {
  const [y, m, d] = await page.evaluate(() => {
    const now = new Date();
    return [now.getFullYear(), now.getMonth(), now.getDate()];
  });
  return { today: new Date(y, m, d), saleDay: new Date(y, m, d + 1) };
}

/** Tomorrow in London, as the API's sale dates want it (YYYY-MM-DD). */
export function tomorrowIso(): string {
  const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Europe/London' }));
  return isoDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
}

/** `lib/format.formatGBP`: whole pounds with separators, e.g. "£520,000". */
export function formatGBP(value: number): string {
  const rounded = Math.round(value);
  return (rounded < 0 ? '-' : '') + '£' + Math.abs(rounded).toLocaleString('en-GB');
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

/** `lib/format.formatSoldDate`: "2026-08-01" reads "August 2026". */
export function formatSoldDate(iso: string): string {
  const [year, month] = iso.split('-').map(Number);
  return MONTHS[month - 1] + ' ' + year;
}

/** `lib/format.formatLongDate`: "2026-10-05" reads "5 October 2026". */
export function formatLongDate(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number);
  return day + ' ' + MONTHS[month - 1] + ' ' + year;
}

/** `lib/price.readable`: an all-capitals register value in title case. */
export function readable(value: string): string {
  if (value !== value.toUpperCase()) {
    return value;
  }
  return value.toLowerCase().replace(/(^|[\s,(-])([a-z])/g, (_, gap: string, letter: string) => gap + letter.toUpperCase());
}

/** The address form the page searches with by default (`E2E_ADDRESS`). */
export const ADDRESS_QUERY = process.env.E2E_ADDRESS ?? '18 Barclay Road';
/** Its postcode, which lists every home in it (`E2E_POSTCODE`). */
export const ADDRESS_POSTCODE = process.env.E2E_POSTCODE ?? 'CR0 1JN';
/** A home the API can price, as `POST /price` and `/nearby` take it. */
export const HOME = {
  postnumber: '18',
  poststreet: 'BARCLAY ROAD',
  posttown: 'CROYDON',
  postcode: 'CR0 1JN',
  unit: 'FLAT 1',
};

/** The price form on the home page. */
export function priceForm(page: Page) {
  return page.getByRole('form', { name: 'Home to price' });
}

/** Types `text` in the address search and waits for its answer. */
export async function searchFor(page: Page, text: string): Promise<Response> {
  const answer = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return pathname === '/api/addresses/search' || pathname === '/api/addresses';
  });
  await priceForm(page).getByRole('combobox', { name: 'Address' }).fill(text);
  return answer;
}

/**
 * Searches for `ADDRESS_QUERY`, picks the first suggestion and sets tomorrow's
 * date; the form is then ready for "Price this home".
 */
export async function chooseHome(page: Page): Promise<{ saleDay: Date }> {
  const search = await searchFor(page, ADDRESS_QUERY);
  expect(search.status()).toBe(200);
  await page.getByRole('option').first().click();
  const { saleDay } = await saleDayFromToday(page);
  const dateInput = priceForm(page).getByRole('textbox', { name: 'Sale date' });
  await dateInput.fill(ukDate(saleDay));
  await expect(dateInput).toHaveValue(ukDate(saleDay));
  return { saleDay };
}

/** Presses "Price this home" and returns the answer to `POST /api/price`. */
export async function submitPrice(page: Page): Promise<Response> {
  const price = apiResponse(page, 'POST', '/price');
  await priceForm(page).getByRole('button', { name: 'Price this home' }).click();
  return price;
}

/** The estimate form, opened from the hero. */
export async function openEstimateForm(page: Page) {
  await page.getByRole('button', { name: /Estimate from details/ }).click();
  const form = page.getByRole('form', { name: 'Property details' });
  await expect(form).toBeVisible();
  return form;
}

// ---------------------------------------------------------------------------
// Below the browser: raw sockets and the API's log
// ---------------------------------------------------------------------------

/**
 * Sends one GET to the local nginx from `localAddress` (another loopback
 * host, which nginx does not trust as a proxy), with any headers.
 */
export function rawGet(
  baseURL: string | undefined,
  pathname: string,
  headers: Record<string, string>,
  localAddress?: string,
): Promise<{ status: number; body: string }> {
  const target = new URL(pathname, baseURL ?? 'http://localhost:8080');
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: '127.0.0.1',
        port: Number(target.port || 80),
        path: target.pathname + target.search,
        method: 'GET',
        headers: { 'User-Agent': BROWSER_UA, ...headers },
        localAddress,
      },
      (response) => {
        let body = '';
        response.setEncoding('utf8');
        response.on('data', (chunk: string) => {
          body += chunk;
        });
        response.on('end', () => resolve({ status: response.statusCode ?? 0, body }));
      },
    );
    request.on('error', reject);
    request.end();
  });
}

/** A request id nginx accepts as the page's own (`[A-Za-z0-9-]{8,64}`). */
export function requestId(label: string): string {
  return `e2e-${label}-${randomBytes(6).toString('hex')}`;
}

/** The `api.log` line (`[ACCESS]` or `[EDGE]`) for request id `rid`, waiting for it to be written. */
export async function apiLogLine(rid: string, timeoutMs = 5_000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const line = existsSync(API_LOG)
      ? readFileSync(API_LOG, 'utf8')
          .split('\n')
          .find((candidate) => candidate.includes(`rid=${rid}`))
      : undefined;
    if (line) {
      return line;
    }
    if (Date.now() > deadline) {
      throw new Error(`no line for rid=${rid} in ${API_LOG}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

/** The client address an `api.log` line names. */
export function loggedAddress(line: string): string {
  const match = /\[(?:ACCESS|EDGE)\] (\S+) "/.exec(line);
  if (!match) {
    throw new Error(`not an access line: ${line}`);
  }
  return match[1];
}
