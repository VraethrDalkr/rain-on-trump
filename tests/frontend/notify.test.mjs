// Unit tests for the pure decisions in frontend/js/notify.js
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  permissionPlan, shouldShowFirstLaunch, thunderstormAllowed, reconcileSubscribed, isSubChangeMessage, deniedHelpText,
} from '../../frontend/js/notify.js';

const env = (o) => ({ permission: 'default', iosBrowser: false, subscribed: false, ...o });

test('iOS Safari tab is told to install the app', () => {
  assert.equal(permissionPlan(env({ iosBrowser: true })), 'install-prompt');
  assert.equal(permissionPlan(env({ iosBrowser: true, permission: 'unsupported' })), 'install-prompt');
});

test('first time asks for permission', () => {
  assert.equal(permissionPlan(env({})), 'request');
});

test('granted but not subscribed subscribes without asking again', () => {
  assert.equal(permissionPlan(env({ permission: 'granted' })), 'subscribe');
});

test('denied explains how to unblock', () => {
  assert.equal(permissionPlan(env({ permission: 'denied' })), 'denied-help');
});

test('no push support at all', () => {
  assert.equal(permissionPlan(env({ permission: 'unsupported' })), 'unsupported');
});

test('already subscribed opens settings', () => {
  assert.equal(permissionPlan(env({ subscribed: true, permission: 'granted' })), 'settings');
});

test('first-launch popup only for a fresh visit with push and nothing else open', () => {
  const base = { hasVisited: false, subscribed: false, pushSupported: true, dialogOpen: false };
  assert.equal(shouldShowFirstLaunch(base), true);
  assert.equal(shouldShowFirstLaunch({ ...base, hasVisited: true }), false);
  assert.equal(shouldShowFirstLaunch({ ...base, subscribed: true }), false);
  assert.equal(shouldShowFirstLaunch({ ...base, pushSupported: false }), false);
  assert.equal(shouldShowFirstLaunch({ ...base, dialogOpen: true }), false);
});

test('thunderstorm alerts need rain start or stop on', () => {
  assert.equal(thunderstormAllowed({ rain_start: true, rain_stop: false }), true);
  assert.equal(thunderstormAllowed({ rain_start: false, rain_stop: true }), true);
  assert.equal(thunderstormAllowed({ rain_start: false, rain_stop: false }), false);
});

test('first-launch popup never stacks on a permission prompt or offers a blocked feature', () => {
  const base = { hasVisited: false, subscribed: false, pushSupported: true, dialogOpen: false };
  assert.equal(shouldShowFirstLaunch({ ...base, asking: true }), false);
  assert.equal(shouldShowFirstLaunch({ ...base, permission: 'denied' }), false);
  assert.equal(shouldShowFirstLaunch({ ...base, permission: 'default', asking: false }), true);
});

test('the browser subscription is the truth for the subscribed flag', () => {
  assert.equal(reconcileSubscribed(true, true), true);
  assert.equal(reconcileSubscribed(false, true), true);
  assert.equal(reconcileSubscribed(true, false), false);
  assert.equal(reconcileSubscribed(false, false), false);
});

test('service worker endpoint-rotation message is recognised', () => {
  assert.equal(isSubChangeMessage({ type: 'PUSH_SUB_CHANGED' }), true);
  assert.equal(isSubChangeMessage({ type: 'SKIP_WAITING' }), false);
  assert.equal(isSubChangeMessage(null), false);
  assert.equal(isSubChangeMessage('PUSH_SUB_CHANGED'), false);
});

test('denied help never guesses the installed app name', () => {
  assert.equal(
    deniedHelpText(true),
    "Notifications are blocked. Turn them on in iOS Settings → Notifications, under this app's name.",
  );
  assert.equal(
    deniedHelpText(false),
    "Notifications are blocked. Allow them for this site in your browser's settings.",
  );
});
