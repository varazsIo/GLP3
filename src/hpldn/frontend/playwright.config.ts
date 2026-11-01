/**
 * Playwright end-to-end tests, run against the local stack: production's nginx
 * (`hpldn.conf`, `guard.yaml`'s limits, bans and headers), the guard daemon,
 * and the API under gunicorn with the reCAPTCHA gate ON.
 *
 * Unless one already answers on `E2E_BASE_URL` (default
 * `http://localhost:8080`), the stack is started here with
 *
 *     uv run python -m hpldn.backend.cloud.local up --fake-google --build --foreground
 *
 * Only Google is replaced: the page gets a stub `grecaptcha` (`e2e/fixtures.ts`)
 * whose tokens a fake siteverify on 127.0.0.1:18999 answers, so the API's own
 * check, its pass and every limit run for real. `e2e/global-setup.ts` fails fast
 * when the stack is not like that. Each test is a fresh visitor with its own
 * `CF-Connecting-IP`, so limits and bans never carry over between tests or reach
 * the developer's own address. The specs that need Google's real script run
 * only with `E2E_LIVE_GOOGLE=1`, against a stack started without `--fake-google`.
 *
 * Two projects: desktop Chromium runs everything; `mobile` (Chromium at a
 * 390 px wide phone viewport) runs the `e2e/api/` tests tagged `@ui`, the ones
 * that drive the page. `e2e/coverage.spec.ts` checks every exposed `/api`
 * route has `@valid`, `@invalid` and `@redteam` tests.
 */
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:8080';

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  // One stack, whose per-worker limits and model pricing are shared: one spec at a time.
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    timezoneId: 'Europe/London',
    locale: 'en-GB',
    reducedMotion: 'reduce',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    {
      name: 'mobile',
      testMatch: /api\/.*\.spec\.ts$/,
      grep: /@ui\b/,
      use: { ...devices['Pixel 5'], viewport: { width: 390, height: 844 } },
    },
  ],
  webServer: {
    command: 'cd ../../../.. && uv run python -m hpldn.backend.cloud.local up --fake-google --build --foreground',
    url: new URL('/api/session/config', baseURL).toString(),
    reuseExistingServer: true,
    // The build, then each worker's start.
    timeout: 600_000,
    // SIGTERM lets the launcher stop nginx, gunicorn and the guard in order.
    gracefulShutdown: { signal: 'SIGTERM', timeout: 60_000 },
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
