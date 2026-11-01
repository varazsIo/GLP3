#!/usr/bin/env node
/**
 * Strip everything that maps the production bundle back to the source. Run
 * after `react-scripts build` (`npm run build:prod`).
 *
 *     node scripts/strip-build.mjs                    # build/
 *     node scripts/strip-build.mjs --build some/dir   # another build directory
 *     node scripts/strip-build.mjs --check            # only check build/ ships nothing it must not
 *
 * What it does, in order:
 *   1. `asset-manifest.json` and every `*.map` are deleted, and every
 *      `sourceMappingURL` comment is stripped from the JS and CSS;
 *   2. the check: no `*.map`, no `sourceMappingURL` and no
 *      `asset-manifest.json` anywhere in the build, or the build fails
 *      (deploy.sh checks the same again before it ships).
 *
 * The bundle is not obfuscated. javascript-obfuscator made it x2.3 the
 * minified size gzipped (458 KB against 198 KB),
 * seconds of download on a phone, and it could never hide the API contract,
 * which DevTools shows anyway. The boundary is the server (cloud/oci/README.md).
 */
import { readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SOURCE_MAP_COMMENT = /\n?\/[/*][#@]\s*sourceMappingURL=[^\n]*?(\*\/)?(?=\n|$)/g;

function parseArgs(argv) {
  const args = { build: path.join(FRONTEND_DIR, 'build'), check: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--build') {
      args.build = path.resolve(argv[(i += 1)]);
    } else if (arg === '--check') {
      args.check = true;
    } else {
      throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

/** Every file under `dir`, recursively. */
function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? walk(full) : [full];
  });
}

/** What must never ship: source maps, the asset manifest, sourceMappingURL comments. */
export function leaks(buildDir) {
  const found = [];
  for (const file of walk(buildDir)) {
    const name = path.basename(file);
    if (name.endsWith('.map') || name === 'asset-manifest.json') {
      found.push(path.relative(buildDir, file));
    } else if (/\.(js|css|html)$/.test(name) && readFileSync(file, 'utf8').includes('sourceMappingURL')) {
      found.push(`${path.relative(buildDir, file)} (sourceMappingURL)`);
    }
  }
  return found;
}

/** Deletes the maps and the asset manifest, and strips sourceMappingURL comments. */
function strip(buildDir) {
  const jsDir = path.join(buildDir, 'static', 'js');
  if (!statSync(jsDir, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error(`${jsDir} does not exist: run react-scripts build first`);
  }
  for (const file of walk(buildDir)) {
    const name = path.basename(file);
    if (name.endsWith('.map') || name === 'asset-manifest.json') {
      rmSync(file);
    } else if (/\.(js|css)$/.test(name)) {
      const text = readFileSync(file, 'utf8');
      const stripped = text.replace(SOURCE_MAP_COMMENT, '');
      if (stripped !== text) {
        writeFileSync(file, stripped);
      }
    }
  }
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.check) {
    strip(args.build);
  }
  const found = leaks(args.build);
  if (found.length) {
    throw new Error(`the build would ship what maps it back to the source: ${found.join(', ')}`);
  }
  console.log(`strip-build: ${args.build}: no source maps, no sourceMappingURL, no asset manifest`);
}

try {
  main();
} catch (error) {
  console.error(`strip-build: ${error.message}`);
  process.exit(1);
}
