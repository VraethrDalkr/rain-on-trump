// The strip under the iOS status bar (installed app) and Safari's bars take
// their colour from the root background, so the sky gradient lives there, with
// its top colour as the plain background colour (as in v2). theme-color
// follows the same top colour.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { SKY_TOP } from '../../frontend/js/sky.js';

const css = readFileSync(new URL('../../frontend/css/app.css', import.meta.url), 'utf8');
const skies = [
  ...css.matchAll(
    /^html:has\(\.sky-([a-z-]+)\) \{ --sky-top: (#[0-9a-f]{6}); --sky: linear-gradient\(180deg, (#[0-9a-f]{6}) 0%/gm,
  ),
];

test('every sky paints the root, with its top colour as the plain colour', () => {
  assert.equal(skies.length, 7, `found ${skies.length} skies`);
  for (const [, name, plain, top] of skies) assert.equal(plain, top, `.sky-${name}`);
  assert.match(css, /^html \{[^}]*background: var\(--sky-top\) var\(--sky\);/m);
});

test('the sky layer has no background of its own (it would hide the root)', () => {
  assert.doesNotMatch(css, /^\.sky(-[a-z-]+)? \{[^}]*background/m);
});

test('theme-color follows the top of each sky', () => {
  const tops = Object.fromEntries(skies.map(([, name, , top]) => [name, top]));
  assert.deepEqual(SKY_TOP, tops);
});

test('the sun has a visible style (the view model shows it on dry days)', () => {
  const rule = css.match(/^\.sun \{([^}]*)\}/m);
  assert.ok(rule, '.sun rule missing: the element renders as an empty div');
  assert.match(rule[1], /width: \d+px/);
  assert.match(rule[1], /radial-gradient/);
});
