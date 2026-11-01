/**
 * k6 load test for GET /addresses/search: 50 people typing London addresses.
 *
 * Not part of the default suite. Run it against a local API before a release
 * and after any change to production.address_search or its indexes:
 *
 *   uvicorn hpldn.backend.api.main:app --port 8000 --workers 4
 *   HPLDN_API_KEY=dev HPLDN_PASS_SECRET=<the API's> k6 run src/hpldn/backend/tests/load/address_search.js
 *
 * /addresses/search is behind the reCAPTCHA gate, so each user sends the
 * X-HPLDN-Pass a page earns from POST /session, signed here with the API's
 * HPLDN_PASS_SECRET exactly as hpldn.backend.guard.captcha.issue_pass does
 * and bound to that user's X-Real-IP. Without the secret the run stops:
 * every search would be a 403.
 *
 * Each virtual user types one address a character at a time, 120-260 ms per
 * keystroke with the odd longer pause, and sends a search exactly when the page
 * would (src/price/AddressSearch.tsx): 300 ms after the last keystroke, at least
 * 400 ms after the previous search, never for fewer than 3 characters, never
 * the same text twice. It calls the API directly on the loopback with its own
 * X-Real-IP, which the API trusts from the loopback only, so each user gets its
 * own rate-limit bucket as it would behind nginx.
 *
 * Passes when p99 < 150 ms and there are no 5xx. A 429 fails the run too: at
 * typing speed one person must never reach the per-client limit.
 */
import http from 'k6/http';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE = __ENV.HPLDN_API_URL || 'http://127.0.0.1:8000';
const KEY = __ENV.HPLDN_API_KEY || 'dev';
const PASS_SECRET = __ENV.HPLDN_PASS_SECRET || '';
// Long enough for any run; the API checks expiry, so keep it finite.
const PASS_TTL_S = 6 * 3600;

if (!PASS_SECRET) {
  throw new Error('set HPLDN_PASS_SECRET to the API\'s: /addresses/search wants a pass in X-HPLDN-Pass');
}

/**
 * A pass for `ip`, as guard.captcha.issue_pass makes it: base64url (no padding)
 * of {"v":1,"exp":<unix s>,"iph":<first 16 hex of sha256(ip)>}, a dot, and the
 * payload's HMAC-SHA256 under the secret, base64url without padding.
 */
function humanPass(ip) {
  const body = { v: 1, exp: Math.floor(Date.now() / 1000) + PASS_TTL_S, iph: crypto.sha256(ip, 'hex').slice(0, 16) };
  const payload = encoding.b64encode(JSON.stringify(body), 'rawurl');
  return payload + '.' + crypto.hmac('sha256', PASS_SECRET, payload, 'base64rawurl');
}

const DEBOUNCE_MS = 300;
const MIN_INTERVAL_MS = 400;
const MIN_QUERY_LENGTH = 3;

const ADDRESSES = [
  '17 barclay road croydon',
  '18 barclay road cr0 1jn',
  'flat 3 high street',
  '24 church road',
  'battersea park road sw11',
  'henry house',
  "12 st mary's road",
  '5 cairo road e17',
  'barcley road',
  'sw11 1ad',
  '101 lavender hill',
  'flat 18 henry house ringers road',
  'e17 3ae',
  '40 northcote road',
  'zqxwv street',
];

const searchDuration = new Trend('search_duration', true);
const serverErrors = new Rate('search_5xx');
const rateLimited = new Counter('search_429');
const searchesPerAddress = new Trend('searches_per_address');

export const options = {
  scenarios: {
    typists: {
      executor: 'constant-vus',
      vus: 50,
      duration: __ENV.DURATION || '2m',
    },
  },
  thresholds: {
    search_duration: ['p(99)<150'],
    search_5xx: ['rate==0'],
    search_429: ['count==0'],
    checks: ['rate==1'],
  },
};

function search(text) {
  const ip = '10.77.0.' + __VU;
  const response = http.get(
    BASE + '/addresses/search?q=' + encodeURIComponent(text) + '&limit=8',
    {
      headers: { 'X-API-Key': KEY, 'X-Real-IP': ip, 'X-HPLDN-Pass': humanPass(ip) },
      tags: { name: 'GET /addresses/search' },
    },
  );
  searchDuration.add(response.timings.duration);
  serverErrors.add(response.status >= 500);
  if (response.status === 429) {
    rateLimited.add(1);
  }
  check(response, {
    'status is 200': (r) => r.status === 200,
    'at most 8 homes': (r) => r.status !== 200 || r.json().length <= 8,
  });
}

export default function typeOneAddress() {
  const address = ADDRESSES[Math.floor(Math.random() * ADDRESSES.length)];
  let lastSentAt = -Infinity;
  let lastSent = '';
  let sent = 0;
  let clock = 0;

  // Walk the keystrokes on a virtual clock and send where the page would.
  for (let i = 1; i <= address.length; i += 1) {
    const typed = address.slice(0, i);
    const gap = Math.random() < 0.1 ? 350 + Math.random() * 500 : 120 + Math.random() * 140;
    const isLast = i === address.length;
    const pauseAfter = isLast ? DEBOUNCE_MS : gap;
    const query = typed.trim().replace(/\s+/g, ' ');

    if (pauseAfter >= DEBOUNCE_MS && query.length >= MIN_QUERY_LENGTH && query !== lastSent) {
      const due = clock + DEBOUNCE_MS;
      const at = Math.max(due, lastSentAt + MIN_INTERVAL_MS);
      sleep((at - clock) / 1000);
      search(query);
      lastSentAt = at;
      lastSent = query;
      sent += 1;
      clock = at;
      sleep(Math.max(0, pauseAfter - DEBOUNCE_MS) / 1000);
      clock += Math.max(0, pauseAfter - DEBOUNCE_MS);
    } else {
      sleep(pauseAfter / 1000);
      clock += pauseAfter;
    }
  }
  searchesPerAddress.add(sent);
  sleep(1 + Math.random() * 2);
}
