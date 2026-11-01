/**
 * The Playwright specs that need no stack: `e2e/coverage.spec.ts` alone.
 *
 * It reads `src/setupProxy.js` and Playwright's own listing of `e2e/api/`
 * (`playwright test --list`, which runs neither the global setup nor the
 * web server), so it runs anywhere `npm ci` has run: no local stack, no
 * database, no model and no browser. GitHub Actions runs it this way
 * (`npm run e2e:static`, `scripts/ci/run_all.sh e2e_static`); the full
 * suite, against the local stack, is `playwright.config.ts`.
 */
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: /coverage\.spec\.ts$/,
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  reporter: 'list',
});
