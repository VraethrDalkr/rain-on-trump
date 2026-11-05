// Debug effects panel (dev PWA only): host gating and presets. Each preset is
// a fake payload run through the real view model, so every sky effect can be
// checked on a phone without waiting for the weather.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isDebugHost, PRESETS, presetPayload } from '../../frontend/js/debug.js';
import { viewModel } from '../../frontend/js/model.js';

const PREFS = { units: 'metric', locale: 'en-US', timeZone: 'America/New_York' };
const sky = (id, now) => viewModel(presetPayload(id, now), now, PREFS).sky;

test('only the dev PWA and local hosts get the panel', () => {
  for (const h of ['rain-on-trump-dev.pages.dev', 'a67ae249.rain-on-trump-dev.pages.dev', 'localhost', '127.0.0.1', '192.168.1.20', '10.0.0.5']) {
    assert.equal(isDebugHost(h), true, h);
  }
  for (const h of ['rain-on-trump.pages.dev', 'abc.rain-on-trump.pages.dev', 'example.com', '']) {
    assert.equal(isDebugHost(h), false, h);
  }
});

test('presets have unique ids and labels', () => {
  const ids = PRESETS.map((p) => p.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const p of PRESETS) assert.ok(p.label, p.id);
});

// Several times of day, so the presets don't depend on when the test runs
for (const iso of ['2026-11-05T03:10:00Z', '2026-11-05T11:45:00Z', '2026-11-05T19:30:00Z', '2026-11-05T23:59:00Z']) {
  const now = new Date(iso);
  test(`every preset shows its effect (${iso})`, () => {
    const s = (id) => sky(id, now);
    assert.ok(s('day-clear').sun && s('day-clear').time === 'day');
    assert.ok(s('night-clear').stars && s('night-clear').time === 'night');
    assert.ok(s('day-rain').drops > 0 && s('day-rain').time === 'day');
    assert.equal(s('day-storm').storm, 'moderate');
    assert.equal(s('day-storm').drops, 0);
    assert.ok(s('dusk-snow').flakes > 0 && s('dusk-snow').time === 'dusk');
    assert.equal(s('night-severe').storm, 'severe');
    assert.ok(s('night-severe').drops > 0);
    assert.ok(s('night-mixed').drops > 0 && s('night-mixed').flakes > 0);
    assert.equal(s('gusty-rain').wind, 'gusty');
    assert.ok(s('flight-day').kind === 'flight' && s('flight-day').time === 'day');
    assert.ok(s('flight-night').kind === 'flight' && s('flight-night').time === 'night');
    assert.equal(s('unknown').kind, 'fog');
  });
}
