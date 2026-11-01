/**
 * The end-to-end tests' `test`: every test is a fresh visitor, and only Google is fake.
 *
 * Two automatic fixtures:
 *  - `visitorIp`: a TEST-NET address of the test's own (198.51.100.x,
 *    203.0.113.x), sent as `CF-Connecting-IP` on the page's same-origin `/api`
 *    calls, as Cloudflare does in production. The local nginx trusts it from
 *    the loopback, so nginx's and the API's limits, the guard's bans and the
 *    pass's address binding all see a separate visitor per test, and never the
 *    developer's 127.0.0.1.
 *  - `googleStub`: Google's reCAPTCHA script replaced by a stub `grecaptcha`
 *    whose `execute` returns tokens the local stack's fake siteverify
 *    understands (`hpldn.backend.cloud.local.fake_google`): `e2e-ok` by default,
 *    `e2e-low`, `e2e-browser-error` or `e2e-down` when a test asks. Calls are
 *    recorded on `window.__executed`. `test.use({ liveGoogle: true })` keeps
 *    Google's real script.
 */
import { test as base } from '@playwright/test';
import type { APIRequestContext, Page, PlaywrightWorkerArgs } from '@playwright/test';

export { expect } from '@playwright/test';

export const RECAPTCHA_SCRIPT = /^https:\/\/www\.google\.com\/recaptcha\/api\.js\?render=/;

/** The tokens the fake siteverify answers (`fake_google.py`). */
export type FakeToken = 'e2e-ok' | 'e2e-low' | 'e2e-browser-error' | 'e2e-down';

export interface GoogleStub {
  /** The tokens `execute` returns in turn, the last one from then on. Set before the page loads the script. */
  tokens(...tokens: FakeToken[]): void;
  /** The `execute` calls the page made so far. */
  executed(page: Page): Promise<Array<{ siteKey: string; action: string }>>;
}

/** TEST-NET-2 and TEST-NET-3 hosts: documentation addresses no real visitor has. */
const VISITOR_POOL = [
  ...Array.from({ length: 254 }, (_, i) => `198.51.100.${i + 1}`),
  ...Array.from({ length: 254 }, (_, i) => `203.0.113.${i + 1}`),
];
// A random start per run, so a re-run within the guard's windows meets no
// strikes or limits left by the last one.
let nextVisitor = Math.floor(Math.random() * VISITOR_POOL.length);

/** An address no other test of this run has used. */
export function nextVisitorIp(): string {
  const ip = VISITOR_POOL[nextVisitor % VISITOR_POOL.length];
  nextVisitor += 1;
  return ip;
}

/** An API client that is the visitor at `ip` (and, optionally, a given user agent). */
export async function visitorRequest(
  playwright: PlaywrightWorkerArgs['playwright'],
  baseURL: string | undefined,
  ip: string,
  userAgent?: string,
): Promise<APIRequestContext> {
  return playwright.request.newContext({
    baseURL,
    extraHTTPHeaders: { 'CF-Connecting-IP': ip },
    ...(userAgent ? { userAgent } : {}),
  });
}

function stubScript(tokens: string[]): string {
  return `
    window.__executed = [];
    (function () {
      var tokens = ${JSON.stringify(tokens)};
      window.grecaptcha = {
        ready: function (callback) { callback(); },
        execute: function (siteKey, options) {
          window.__executed.push({ siteKey: siteKey, action: options && options.action });
          return Promise.resolve(tokens.length > 1 ? tokens.shift() : tokens[0]);
        },
      };
    })();
  `;
}

export const test = base.extend<{ liveGoogle: boolean; visitorIp: string; googleStub: GoogleStub }>({
  liveGoogle: [false, { option: true }],

  visitorIp: [
    async ({ context, baseURL }, use) => {
      const ip = nextVisitorIp();
      const origin = new URL(baseURL ?? 'http://localhost:8080').origin;
      await context.route(
        (url) => url.origin === origin && url.pathname.startsWith('/api/'),
        (route) => route.continue({ headers: { ...route.request().headers(), 'cf-connecting-ip': ip } }),
      );
      await use(ip);
    },
    { auto: true },
  ],

  googleStub: [
    async ({ context, liveGoogle }, use) => {
      let tokens: FakeToken[] = ['e2e-ok'];
      if (!liveGoogle) {
        await context.route(RECAPTCHA_SCRIPT, (route) =>
          route.fulfill({ contentType: 'application/javascript', body: stubScript(tokens) }),
        );
      }
      await use({
        tokens(...next: FakeToken[]) {
          tokens = next;
        },
        executed(page: Page) {
          return page.evaluate(
            () => (window as unknown as { __executed?: Array<{ siteKey: string; action: string }> }).__executed ?? [],
          );
        },
      });
    },
    { auto: true },
  ],
});
