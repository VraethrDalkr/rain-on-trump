// The service worker must precache every app file, or offline starts break.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';

const root = new URL('../../frontend/', import.meta.url);
const sw = readFileSync(new URL('sw.js', root), 'utf8');
const shell = sw.slice(sw.indexOf('const STATIC_SHELL'), sw.indexOf('];', sw.indexOf('const STATIC_SHELL')));

test('every js/ and css/ file is in STATIC_SHELL', () => {
  for (const dir of ['js', 'css']) {
    for (const file of readdirSync(new URL(`${dir}/`, root))) {
      assert.ok(shell.includes(`'/${dir}/${file}'`), `/${dir}/${file} missing from STATIC_SHELL in sw.js`);
    }
  }
});

test('every font is precached', () => {
  for (const file of readdirSync(new URL('fonts/', root)).filter((f) => f.endsWith('.woff2'))) {
    assert.ok(sw.includes(`'/fonts/${file}'`), `/fonts/${file} missing from sw.js`);
  }
});
