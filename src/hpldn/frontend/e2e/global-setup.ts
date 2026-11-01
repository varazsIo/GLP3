/**
 * Fails the run at once unless it is against the local stack with the gate on and fake Google wired.
 *
 * The specs assume production's layers: `GET /api/session/config` must say the
 * reCAPTCHA gate is on, and `POST /api/session` with the fake token `e2e-ok`
 * must earn a pass, which only the fake siteverify grants. With
 * `E2E_LIVE_GOOGLE=1` (a stack started without `--fake-google`, for the live
 * Google specs) only the first check is made.
 *
 * It also saves the API's `openapi.json`, which `e2e/api/` validates every
 * answer against. nginx never serves it, and the API serves it only with
 * `HPLDN_API_DOCS=1` (which the local stack sets, and the serving host never
 * does), so it is read from gunicorn's unix socket (`E2E_API_SOCKET`, default
 * tmp/local-stack/run/api.sock), or from `E2E_OPENAPI_URL` when that is set.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { request } from '@playwright/test';
import type { FullConfig } from '@playwright/test';
import { OPENAPI_CACHE, REPO_ROOT } from './support/api';

const START = 'uv run python -m hpldn.backend.cloud.local up --fake-google';
const API_SOCKET = process.env.E2E_API_SOCKET ?? path.join(REPO_ROOT, 'tmp/local-stack/run/api.sock');
const OPENAPI_URL = process.env.E2E_OPENAPI_URL;

/** GET /openapi.json from gunicorn's unix socket: the status and the body. */
function getOverSocket(socketPath: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = http.get({ socketPath, path: '/openapi.json', headers: { 'User-Agent': 'hpldn-e2e-setup' } }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk: string) => {
        body += chunk;
      });
      res.on('end', () => resolve({ status: res.statusCode ?? 0, body }));
    });
    req.setTimeout(10_000, () => req.destroy(new Error(`timed out reading ${socketPath}`)));
    req.on('error', reject);
  });
}

/** Saves the API's OpenAPI document for the response checks. */
async function saveOpenApi(): Promise<void> {
  let where: string;
  let status: number;
  let text: string;
  if (OPENAPI_URL) {
    where = OPENAPI_URL;
    const api = await request.newContext();
    try {
      const response = await api.get(OPENAPI_URL);
      status = response.status();
      text = await response.text();
    } finally {
      await api.dispose();
    }
  } else {
    where = `${API_SOCKET} /openapi.json`;
    ({ status, body: text } = await getOverSocket(API_SOCKET));
  }
  if (status !== 200) {
    throw new Error(
      `GET ${where} answered ${status}: the specs validate answers against it ` +
        '(the API serves it only with HPLDN_API_DOCS=1, which the local stack sets)',
    );
  }
  const document = JSON.parse(text) as { openapi?: string; paths?: Record<string, unknown> };
  if (!document.openapi?.startsWith('3.1') || !document.paths) {
    throw new Error(`${where} is not an OpenAPI 3.1 document (openapi: ${document.openapi})`);
  }
  mkdirSync(path.dirname(OPENAPI_CACHE), { recursive: true });
  writeFileSync(OPENAPI_CACHE, JSON.stringify(document));
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  await saveOpenApi();
  const baseURL = config.projects[0].use.baseURL;
  // A TEST-NET-1 visitor of its own, apart from the specs' addresses.
  const ip = `192.0.2.${1 + Math.floor(Math.random() * 254)}`;
  const api = await request.newContext({ baseURL, extraHTTPHeaders: { 'CF-Connecting-IP': ip } });
  try {
    const configResponse = await api.get('/api/session/config');
    if (configResponse.status() !== 200) {
      throw new Error(`GET ${baseURL}/api/session/config answered ${configResponse.status()}: is the local stack up? ${START}`);
    }
    const gate = (await configResponse.json()) as { enabled: boolean };
    if (!gate.enabled) {
      throw new Error(`The API at ${baseURL} runs with the reCAPTCHA gate off; the specs need it on. ${START}`);
    }
    if (process.env.E2E_LIVE_GOOGLE === '1') {
      return;
    }
    const session = await api.post('/api/session', { data: { token: 'e2e-ok' } });
    if (session.status() !== 200) {
      throw new Error(
        `POST /api/session with the fake token e2e-ok answered ${session.status()} ` +
          `(${session.headers()['x-hpldn-captcha'] ?? 'no reason'}): the API is not checking tokens with the fake ` +
          `Google. Restart the stack with: ${START}`,
      );
    }
  } finally {
    await api.dispose();
  }
}
