/**
 * Development proxy, loaded automatically by `npm start`.
 *
 * Forwards the browser's same-origin `/api/*` calls, unchanged, to the local
 * stack's nginx at `HPLDN_EDGE_URL` (default `http://127.0.0.1:8080`, started
 * with `uv run python -m hpldn.backend.cloud.local up`). nginx runs
 * production's `hpldn.conf`: it strips `/api`, adds the API key, and applies
 * every rate limit, ban and user-agent rule before gunicorn sees the call. This
 * proxy only sets `CF-Connecting-IP` to the browser's address, as Cloudflare
 * does in production, which nginx trusts from the loopback. So the page under
 * `npm start` meets every API-side rule; only the page itself (HMR) is unlimited
 * and served without the CSP. At startup it probes `GET /api/areas` once and
 * logs the live data window or why it is unavailable; unreachable calls answer
 * 502 with a JSON `detail`.
 */
const { createProxyMiddleware } = require('http-proxy-middleware');

/** The routes production nginx exposes; any other `/api` path is a 404. */
const EXPOSED = [
  /^\/api\/areas$/,
  /^\/api\/areas\/[A-Za-z0-9]+(\/comparables)?$/,
  /^\/api\/estimate$/,
  /^\/api\/addresses$/,
  /^\/api\/addresses\/search$/,
  /^\/api\/price$/,
  /^\/api\/nearby$/,
  /^\/api\/schools$/,
  /^\/api\/inflation$/,
  /^\/api\/session$/,
  /^\/api\/session\/config$/,
];

/** The user agent of the startup probe; nginx refuses scripts' own (curl, python-requests). */
const PROBE_USER_AGENT = 'hpldn-dev-proxy';

/** How to start what this proxy forwards to. */
const START_HINT = 'Start it with: uv run python -m hpldn.backend.cloud.local up';

/** How long the startup probe waits for the API, in milliseconds. */
const PROBE_TIMEOUT_MS = 10000;

/**
 * Checks whether a request path is one of the exposed API routes.
 *
 * @param {string} pathname The request path.
 * @returns {boolean} True when the proxy should forward it.
 */
function isExposed(pathname) {
  return EXPOSED.some((pattern) => pattern.test(pathname));
}

/**
 * Returns the errno-style code of a failed connection.
 *
 * Looks inside an `AggregateError`, which Node raises when both `::1` and
 * `127.0.0.1` refuse and which may lack its own code.
 *
 * @param {unknown} err The connection error.
 * @returns {string} The code, message or a placeholder.
 */
function errorCode(err) {
  if (!err) {
    return 'unknown error';
  }
  const inner = err.cause || (Array.isArray(err.errors) ? err.errors[0] : undefined);
  return err.code || (inner && inner.code) || err.message || String(err);
}

/**
 * Calls `GET /api/areas` through nginx once and logs whether the page will get live data.
 *
 * @param {string} target The local nginx's base URL.
 */
async function probeApi(target) {
  const url = target.replace(/\/+$/, '') + '/api/areas';
  let response;
  try {
    response = await fetch(url, {
      headers: { 'User-Agent': PROBE_USER_AGENT },
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[setupProxy] cannot reach ' + target + ' (' + errorCode(err) + ') -- is the local stack up? ' + START_HINT);
    return;
  }

  if (!response.ok) {
    // eslint-disable-next-line no-console
    console.warn(
      '[setupProxy] ' + url + ' returned ' + response.status +
        ': nginx is up but the API cannot serve data -- check tmp/local-stack/logs/api.log (database connection?).',
    );
    return;
  }

  try {
    const body = await response.json();
    const dataWindow = body.window;
    // eslint-disable-next-line no-console
    console.log(
      '[setupProxy] live data: ' + Number(dataWindow.totalSales).toLocaleString('en-GB') + ' sales to ' +
        dataWindow.latestSale + ' (' + body.areas.length + ' districts) from ' + target,
    );
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[setupProxy] ' + url + ' answered 200 with an unexpected body (' + errorCode(err) + ').');
  }
}

/**
 * Registers the proxy, the startup probe and the `/api` 404 fallback.
 *
 * @param {import('express').Application} app The development server.
 */
module.exports = function setupProxy(app) {
  const target = process.env.HPLDN_EDGE_URL || 'http://127.0.0.1:8080';

  app.use(
    createProxyMiddleware((pathname) => isExposed(pathname), {
      target,
      changeOrigin: true,
      logLevel: 'warn',
      onProxyReq(proxyReq, req) {
        // Name the browser as Cloudflare does, so nginx's and the API's
        // per-client limits, bans and pass binding apply per browser. nginx
        // sets X-Real-IP and the API key itself.
        proxyReq.setHeader('CF-Connecting-IP', (req.socket && req.socket.remoteAddress) || '127.0.0.1');
      },
      onError(err, req, res) {
        const detail = 'cannot reach ' + target + ' (' + errorCode(err) + ') -- is the local stack up?';
        // eslint-disable-next-line no-console
        console.error('[setupProxy] ' + detail + ' [' + req.method + ' ' + req.url + '] ' + START_HINT);
        if (!res || typeof res.writeHead !== 'function') {
          return;
        }
        if (!res.headersSent) {
          res.writeHead(502, { 'Content-Type': 'application/json' });
        }
        res.end(JSON.stringify({ detail }));
      },
    }),
  );

  // Fire and forget: the dev server starts whatever the probe finds.
  probeApi(target).catch(() => {});

  app.use('/api', (req, res) => {
    res.status(404).json({ detail: 'Not Found' });
  });
};
