/**
 * Google reCAPTCHA v3 and the pass the API hands out for it.
 *
 * Most data routes want a pass in `X-HPLDN-Pass`, proof that a person is using
 * the page (`api.ts` adds it). One invisible reCAPTCHA check buys a pass that
 * lasts about half an hour: `ensurePass` runs `grecaptcha.execute` and trades
 * the token at `POST /api/session`. The pass is held in memory and mirrored to
 * `sessionStorage`, so a reload in the same tab does not ask Google again.
 *
 * Google's script is not loaded with the page, which keeps first paint and
 * search crawlers unaffected: `primeCaptcha` loads it on the first interaction
 * with a form (or the first call that needs a pass), and `warmPass` goes on to
 * get the pass itself, so the call that needs it does not wait. The site key
 * comes from `GET /api/session/config` at runtime, so it is not baked into the
 * bundle.
 */

import { loggedFetch } from './callLog';

const API_BASE = process.env.REACT_APP_API_BASE ?? '/api';

/** How long before its expiry a pass is replaced, in milliseconds. */
export const PASS_MARGIN_MS = 60_000;
/** How long Google's script may take to load and get ready, in milliseconds. */
export const SCRIPT_TIMEOUT_MS = 10_000;

const STORAGE_KEY = 'hpldn.pass';
const SCRIPT_URL = 'https://www.google.com/recaptcha/api.js?render=';

const MESSAGES = {
  unreachable: 'We could not reach our data just now. Check your connection and try again.',
  script:
    'We could not load Google’s check that you’re a person. Allow www.google.com/recaptcha in your browser and reload the page.',
  refused: 'We couldn’t confirm this is a person. Reload the page and try again.',
};

/** What `GET /api/session/config` answers. */
export interface CaptchaConfig {
  enabled: boolean;
  siteKey: string;
  action: string;
}

interface HeldPass {
  pass: string;
  /** Unix milliseconds. */
  expiresAt: number;
}

interface Grecaptcha {
  ready(callback: () => void): void;
  execute(siteKey: string, options: { action: string }): Promise<string>;
}

declare global {
  interface Window {
    grecaptcha?: Grecaptcha;
  }
}

/**
 * Getting a pass failed. `status` is the server's (403 for a visitor Google
 * scored as a bot, 429, 5xx) or 0 when nothing answered; `userMessage` is safe
 * to show a landlord.
 */
export class CaptchaError extends Error {
  readonly status: number;
  readonly userMessage: string;
  readonly retryAfterS: number | null;
  /** For a 403 from `POST /session`, the server's `X-HPLDN-Captcha` (`low_score`, `browser_error`). */
  readonly captcha: string | null;

  constructor(
    status: number,
    userMessage: string,
    message?: string,
    retryAfterS: number | null = null,
    captcha: string | null = null,
  ) {
    super(message ?? userMessage);
    this.name = 'CaptchaError';
    this.status = status;
    this.userMessage = userMessage;
    this.retryAfterS = retryAfterS;
    this.captcha = captcha;
  }
}

let configPromise: Promise<CaptchaConfig | null> | null = null;
let scriptPromise: Promise<Grecaptcha> | null = null;
let held: HeldPass | null = null;
let inflight: Promise<string | null> | null = null;
let warmed = false;

function readStored(): HeldPass | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const value = JSON.parse(raw) as Partial<HeldPass>;
    return typeof value.pass === 'string' && typeof value.expiresAt === 'number'
      ? { pass: value.pass, expiresAt: value.expiresAt }
      : null;
  } catch {
    return null;
  }
}

function writeStored(value: HeldPass | null): void {
  try {
    if (value) {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } else {
      window.sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Private windows and blocked storage: the pass lives in memory only.
  }
}

function fresh(value: HeldPass | null): value is HeldPass {
  return value !== null && value.expiresAt - PASS_MARGIN_MS > Date.now();
}

/**
 * Fetches the reCAPTCHA settings once. Null when the gate is off, or when the
 * server has no such route (an API from before the gate). A failure is not
 * cached, so the next call asks again.
 */
export function loadCaptchaConfig(): Promise<CaptchaConfig | null> {
  if (!configPromise) {
    configPromise = (async () => {
      let response: Response;
      try {
        response = await loggedFetch(API_BASE + '/session/config', { headers: { Accept: 'application/json' } });
      } catch {
        throw new CaptchaError(0, MESSAGES.unreachable, 'GET /session/config failed');
      }
      if (response.status === 404) {
        return null;
      }
      if (!response.ok) {
        throw new CaptchaError(response.status, MESSAGES.unreachable, 'GET /session/config returned ' + response.status);
      }
      const body = (await response.json()) as Partial<CaptchaConfig>;
      if (!body.enabled || !body.siteKey) {
        return null;
      }
      return { enabled: true, siteKey: body.siteKey, action: body.action || 'session' };
    })();
    configPromise.catch(() => {
      configPromise = null;
    });
  }
  return configPromise;
}

/**
 * Adds Google's script to the page once and resolves when `grecaptcha` is ready.
 */
function loadScript(siteKey: string): Promise<Grecaptcha> {
  if (!scriptPromise) {
    scriptPromise = new Promise<Grecaptcha>((resolve, reject) => {
      const fail = (): void => reject(new CaptchaError(0, MESSAGES.script, 'reCAPTCHA did not load'));
      const timer = window.setTimeout(fail, SCRIPT_TIMEOUT_MS);
      const whenReady = (): void => {
        const grecaptcha = window.grecaptcha;
        if (!grecaptcha) {
          window.clearTimeout(timer);
          fail();
          return;
        }
        grecaptcha.ready(() => {
          window.clearTimeout(timer);
          resolve(grecaptcha);
        });
      };
      if (window.grecaptcha) {
        whenReady();
        return;
      }
      const script = document.createElement('script');
      script.src = SCRIPT_URL + encodeURIComponent(siteKey);
      script.async = true;
      script.defer = true;
      script.onload = whenReady;
      script.onerror = () => {
        window.clearTimeout(timer);
        fail();
      };
      document.head.appendChild(script);
    });
    scriptPromise.catch(() => {
      scriptPromise = null;
    });
  }
  return scriptPromise;
}

/**
 * Starts loading reCAPTCHA so the first pass is quick: call on the first
 * interaction with a form. Does nothing while the gate is off; never throws.
 */
export function primeCaptcha(): void {
  if (typeof fetch !== 'function' || fresh(held)) {
    return;
  }
  loadCaptchaConfig()
    .then((config) => (config ? loadScript(config.siteKey) : undefined))
    .catch(() => {
      // Reported by ensurePass when a call actually needs the pass.
    });
}

/**
 * Gets a pass in the background, once per page load: call on the first
 * interaction with a form whose calls need one. Address search needs none, so
 * the pass arrives while the landlord types and picks a home, and the price
 * does not wait for Google. Does nothing while the gate is off; never throws.
 */
export function warmPass(): void {
  if (warmed || typeof fetch !== 'function') {
    return;
  }
  warmed = true;
  ensurePass().catch(() => {
    // Reported by ensurePass when a call actually needs the pass.
  });
}

function retryAfterOf(response: Response): number | null {
  const value = response.headers?.get?.('Retry-After');
  return value && /^\d+$/.test(value.trim()) ? Number(value.trim()) : null;
}

/**
 * Runs reCAPTCHA and trades the token for a pass. When the server answers 403
 * `browser_error` (`execute` failed in the browser and gave an error token),
 * runs it once more, as Google's troubleshooting guide advises.
 */
async function requestPass(config: CaptchaConfig): Promise<HeldPass> {
  const grecaptcha = await loadScript(config.siteKey);
  try {
    return await tradeToken(grecaptcha, config);
  } catch (error) {
    if (error instanceof CaptchaError && error.captcha === 'browser_error') {
      return tradeToken(grecaptcha, config);
    }
    throw error;
  }
}

async function tradeToken(grecaptcha: Grecaptcha, config: CaptchaConfig): Promise<HeldPass> {
  let token: string;
  try {
    token = await grecaptcha.execute(config.siteKey, { action: config.action });
  } catch {
    throw new CaptchaError(0, MESSAGES.script, 'grecaptcha.execute failed');
  }
  let response: Response;
  try {
    response = await loggedFetch(API_BASE + '/session', {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
  } catch {
    throw new CaptchaError(0, MESSAGES.unreachable, 'POST /session failed');
  }
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      detail = undefined;
    }
    const message =
      typeof detail === 'string' && detail ? detail : response.status === 403 ? MESSAGES.refused : MESSAGES.unreachable;
    throw new CaptchaError(
      response.status,
      message,
      'POST /session returned ' + response.status,
      retryAfterOf(response),
      response.status === 403 ? response.headers?.get?.('X-HPLDN-Captcha') ?? null : null,
    );
  }
  const body = (await response.json()) as { pass?: unknown; expiresIn?: unknown };
  if (typeof body.pass !== 'string' || typeof body.expiresIn !== 'number') {
    throw new CaptchaError(0, MESSAGES.unreachable, 'POST /session answered an unexpected body');
  }
  return { pass: body.pass, expiresAt: Date.now() + body.expiresIn * 1000 };
}

/**
 * Returns a pass for `X-HPLDN-Pass`, getting a new one when none is held or it
 * expires within `PASS_MARGIN_MS`. Callers at the same moment share one check.
 *
 * @returns The pass, or null while the gate is off.
 * @throws {CaptchaError} When Google's script cannot load, the server refuses
 *     the visitor, or nothing answers.
 */
export function ensurePass(): Promise<string | null> {
  if (!held) {
    held = readStored();
  }
  if (fresh(held)) {
    return Promise.resolve(held.pass);
  }
  if (!inflight) {
    inflight = (async () => {
      try {
        const config = await loadCaptchaConfig();
        if (!config) {
          return null;
        }
        const next = await requestPass(config);
        held = next;
        writeStored(next);
        return next.pass;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

/** Forgets the pass held, after the server refused it. */
export function dropPass(): void {
  held = null;
  writeStored(null);
}

/** Forgets everything, as on a fresh page load. For tests. */
export function resetCaptchaForTests(): void {
  configPromise = null;
  scriptPromise = null;
  held = null;
  inflight = null;
  warmed = false;
  writeStored(null);
}
