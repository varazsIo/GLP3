/**
 * Every `/api` route the site exposes has a valid, an invalid and a red-team test.
 *
 * The routes are `src/setupProxy.js`'s `EXPOSED` patterns (the same whitelist
 * as production nginx's locations). The tests are Playwright's own listing of
 * `e2e/api/` (`playwright test --list --reporter=json`, which runs neither the
 * global setup nor the stack), read through their tags (`support/api.ts`
 * `tags`): one of `@valid`, `@invalid`, `@redteam`, and `@endpoint:<path>` for
 * each path called. A route added to `EXPOSED` without the three kinds of test
 * fails this spec, and so CI.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { ENDPOINT_TAG, FRONTEND_DIR } from './support/api';
import type { Kind } from './support/api';

const KINDS: Kind[] = ['valid', 'invalid', 'redteam'];

interface ListedTest {
  file: string;
  title: string;
  tags: string[];
}

interface JsonSuite {
  file?: string;
  title: string;
  specs?: Array<{ title: string; file: string; tags: string[] }>;
  suites?: JsonSuite[];
}

/** `EXPOSED` in `src/setupProxy.js`: one regular expression literal per line of the array. */
function exposedRoutes(source: string): RegExp[] {
  const block = /const EXPOSED = \[([\s\S]*?)\];/.exec(source);
  if (!block) {
    throw new Error('setupProxy.js has no `const EXPOSED = [...]`');
  }
  return block[1]
    .split('\n')
    .map((line) => /^\s*\/(.+)\/,?\s*$/.exec(line)?.[1])
    .filter((body): body is string => Boolean(body))
    .map((body) => new RegExp(body));
}

/** Every test of `e2e/api/` as Playwright lists it, with its tags (without the `@`). */
function listedApiTests(): ListedTest[] {
  // The runner's own variables (a JSON output file, worker ids) must not reach the listing.
  const env = Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !/^(PLAYWRIGHT_|PW_|TEST_)/.test(key)),
  ) as NodeJS.ProcessEnv;
  const dir = mkdtempSync(path.join(tmpdir(), 'hpldn-e2e-list-'));
  const file = path.join(dir, 'list.json');
  let report: { suites: JsonSuite[]; errors?: Array<{ message: string }> };
  try {
    execFileSync('npx', ['playwright', 'test', '--list', '--reporter=json', '--project=chromium', 'e2e/api/'], {
      cwd: FRONTEND_DIR,
      stdio: ['ignore', 'ignore', 'pipe'],
      env: { ...env, FORCE_COLOR: '0', PLAYWRIGHT_JSON_OUTPUT_NAME: file },
    });
    report = JSON.parse(readFileSync(file, 'utf8'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
  expect(report.errors ?? [], 'playwright --list reported errors').toEqual([]);
  const tests: ListedTest[] = [];
  const walk = (suite: JsonSuite, titles: string[]): void => {
    for (const spec of suite.specs ?? []) {
      tests.push({ file: spec.file, title: [...titles, spec.title].join(' › '), tags: spec.tags });
    }
    for (const child of suite.suites ?? []) {
      walk(child, [...titles, child.title]);
    }
  };
  for (const suite of report.suites) {
    walk(suite, []);
  }
  return tests;
}

/** The endpoint paths a test's tags name, without their query. */
function endpointsOf(listed: ListedTest): string[] {
  const prefix = ENDPOINT_TAG.slice(1);
  return listed.tags.filter((tag) => tag.startsWith(prefix)).map((tag) => tag.slice(prefix.length).split('?')[0]);
}

/** For each exposed route, the kinds of test none of `tests` covers. */
function missingKinds(routes: RegExp[], tests: ListedTest[]): Record<string, Kind[]> {
  const missing: Record<string, Kind[]> = {};
  for (const route of routes) {
    const lacking = KINDS.filter(
      (kind) => !tests.some((listed) => listed.tags.includes(kind) && endpointsOf(listed).some((endpoint) => route.test(endpoint))),
    );
    if (lacking.length) {
      missing[route.source] = lacking;
    }
  }
  return missing;
}

const ROUTES = exposedRoutes(readFileSync(path.join(FRONTEND_DIR, 'src/setupProxy.js'), 'utf8'));

test.describe('API test coverage', () => {
  let tests: ListedTest[] = [];

  test.beforeAll(() => {
    tests = listedApiTests();
  });

  test('setupProxy.js exposes the routes production nginx proxies', () => {
    expect(ROUTES.length).toBeGreaterThanOrEqual(11);
    for (const route of ['/api/areas', '/api/price', '/api/session', '/api/session/config']) {
      expect(ROUTES.some((pattern) => pattern.test(route)), route).toBe(true);
    }
  });

  test('every test in e2e/api has exactly one kind and names the routes it calls', () => {
    expect(tests.length).toBeGreaterThan(50);
    const untagged = tests.filter(
      (listed) => KINDS.filter((kind) => listed.tags.includes(kind)).length !== 1 || endpointsOf(listed).length === 0,
    );
    expect(untagged.map((listed) => `${listed.file}: ${listed.title} [${listed.tags.join(' ')}]`)).toEqual([]);
  });

  test('every exposed /api route has a valid, an invalid and a red-team test', () => {
    const missing = missingKinds(ROUTES, tests);
    expect(missing, 'routes in setupProxy.js EXPOSED without these kinds of e2e/api test').toEqual({});
  });

  test('a tag naming a route nginx does not expose is a red-team probe, not a typo', () => {
    const strays = tests.flatMap((listed) =>
      endpointsOf(listed)
        .filter((endpoint) => !ROUTES.some((route) => route.test(endpoint)))
        .filter(() => !listed.tags.includes('redteam'))
        .map((endpoint) => `${listed.title}: ${endpoint}`),
    );
    expect(strays).toEqual([]);
  });

  test('a route added to EXPOSED without tests is reported for all three kinds', () => {
    const added = /^\/api\/valuations$/;
    expect(missingKinds([...ROUTES, added], tests)).toEqual({ [added.source]: KINDS });
  });
});
