// Unit tests for frontend/js/api.js (pure parts only)
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { usablePayload, apiBaseFor } from '../../frontend/js/api.js';

test('usablePayload accepts a normal answer', () => {
  assert.equal(usablePayload(true, { precipitating: false, coords: {} }), true);
});

test('usablePayload accepts a 503 that still carries the location', () => {
  assert.equal(usablePayload(false, { precipitating: null, error: 'Weather API error', coords: {} }), true);
});

test('usablePayload rejects errors without an answer shape', () => {
  assert.equal(usablePayload(false, { detail: 'Rate limit exceeded' }), false);
  assert.equal(usablePayload(true, null), false);
  assert.equal(usablePayload(true, 'nope'), false);
  assert.equal(usablePayload(false, { precipitating: false }), false);
});

test('apiBaseFor mirrors v2 host detection', () => {
  assert.equal(apiBaseFor({ hostname: 'rain-on-trump.pages.dev', protocol: 'https:', origin: 'x' }), 'https://rain-api.fly.dev');
  assert.equal(apiBaseFor({ hostname: '127.0.0.1', protocol: 'http:', origin: 'x' }), 'http://127.0.0.1:8000');
  assert.equal(apiBaseFor({ hostname: '192.168.1.20', protocol: 'http:', origin: 'x' }), 'http://192.168.1.20:8000');
  assert.equal(apiBaseFor({ hostname: 'rain-api.fly.dev', protocol: 'https:', origin: 'https://rain-api.fly.dev' }), 'https://rain-api.fly.dev');
});
