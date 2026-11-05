// Push notifications: permission flow, subscribe / unsubscribe, preferences.
// Endpoints, payloads and localStorage keys are unchanged from v2, so existing
// subscribers keep working. The module touches the DOM only when its exported
// functions run, so its pure helpers can be unit-tested in Node.

import { apiBaseFor } from './api.js';
import { showToast } from './toast.js';
import { trapTab } from './dialog.js';

// Rotating this key invalidates every existing subscription: never change it
// without rotating the backend's VAPID keys too.
const VAPID_PUBLIC = 'BO67EcPl8JyHtvhvm04cdf_jMgkXVQBT1uRX9BiljiIxTBH4PpYH0BxsU3BEglva1_iqnqH3T9bVVIj9MI3ZQIE';
const DEFAULT_PREFS = { rain_start: true, rain_stop: true, thunderstorm: true };
const SUBSCRIBE_TIMEOUT_MS = 10000;
const FIRST_LAUNCH_DELAY_MS = 2000;

// ── Pure decisions (unit-tested) ───────────────────────────────────────

/**
 * What a tap on "Tell me when it rains" should do.
 * @param {{permission: 'default'|'granted'|'denied'|'unsupported', iosBrowser: boolean, subscribed: boolean}} env
 * @returns {'install-prompt'|'request'|'subscribe'|'denied-help'|'unsupported'|'settings'}
 */
export function permissionPlan(env) {
  if (env.subscribed) return 'settings';
  if (env.iosBrowser) return 'install-prompt';
  if (env.permission === 'unsupported') return 'unsupported';
  if (env.permission === 'denied') return 'denied-help';
  if (env.permission === 'granted') return 'subscribe';
  return 'request';
}

/**
 * The "Get rain alerts" popup: first visit only, never over another dialog or
 * a permission prompt in progress, never for an already-blocked permission.
 */
export function shouldShowFirstLaunch({
  hasVisited, subscribed, pushSupported, dialogOpen, asking = false, permission = 'default',
}) {
  return !hasVisited && !subscribed && pushSupported && !dialogOpen && !asking && permission !== 'denied';
}

/** The browser's own subscription decides; localStorage is only a first guess. */
export function reconcileSubscribed(storedFlag, browserHasSubscription) {
  return Boolean(browserHasSubscription);
}

/** sw.js posts this when the browser rotated the push endpoint. */
export function isSubChangeMessage(data) {
  return Boolean(data) && typeof data === 'object' && data.type === 'PUSH_SUB_CHANGED';
}

/** Thunderstorm alerts ride on rain alerts: one of them must be on. */
export function thunderstormAllowed(prefs) {
  return Boolean(prefs.rain_start || prefs.rain_stop);
}

// ── Browser state ─────────────────────────────────────────────────────

let subscribed = false;
let currentSubscription = null;
/** True from a notify tap until its permission / subscribe flow settles. */
let asking = false;

const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // private mode / storage blocked: state lives for this session only
    }
  },
  remove(key) {
    try {
      localStorage.removeItem(key);
    } catch {
      // ignore
    }
  },
};

const base = () => apiBaseFor(location);
const pushSupported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent);
const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;

function env() {
  return {
    permission: pushSupported() ? Notification.permission : 'unsupported',
    iosBrowser: isIOS() && !isStandalone(),
    subscribed,
  };
}

function announce() {
  document.dispatchEvent(new CustomEvent('subscription-change', { detail: { subscribed } }));
}

export function isSubscribed() {
  return subscribed;
}

export function getPrefs() {
  try {
    const stored = store.get('notificationPrefs');
    return stored ? { ...DEFAULT_PREFS, ...JSON.parse(stored) } : { ...DEFAULT_PREFS };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

function savePrefs(prefs) {
  store.set('notificationPrefs', JSON.stringify(prefs));
  document.dispatchEvent(new CustomEvent('prefs-change', { detail: prefs }));
}

function urlBase64ToUint8Array(b64) {
  const padding = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function browserSubscription() {
  if (currentSubscription) return currentSubscription;
  try {
    const reg = await navigator.serviceWorker.ready;
    currentSubscription = await reg.pushManager.getSubscription();
  } catch {
    currentSubscription = null;
  }
  return currentSubscription;
}

/** Subscribe with the push service and the backend (permission already granted). */
async function subscribeGranted() {
  try {
    const reg = await navigator.serviceWorker.ready;
    // Chrome sometimes hangs in subscribe(): give up after a while
    const sub = await Promise.race([
      reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC),
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error('Subscribe timeout')), SUBSCRIBE_TIMEOUT_MS)),
    ]);
    currentSubscription = sub;
    const res = await fetch(`${base()}/subscribe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    if (!res.ok) {
      // Don't keep a browser subscription the server doesn't know about
      try {
        await sub.unsubscribe();
      } catch {
        // ignore
      }
      currentSubscription = null;
      throw new Error(`Server rejected subscription (${res.status})`);
    }
    const data = await res.json().catch(() => null);
    if (data && data.preferences) savePrefs({ ...DEFAULT_PREFS, ...data.preferences });
    subscribed = true;
    store.set('pushSubscribed', 'true');
    announce();
    showToast('Notifications on. We\'ll tell you when it rains on him.');
    return true;
  } catch (err) {
    console.error('Subscription failed:', err);
    showToast('Could not turn on notifications. Try again in a moment.');
    return false;
  }
}

/**
 * Handle a tap on any "turn on notifications" control. Must be called
 * directly from the click handler: when the plan is to ask, the permission
 * request is the first thing that happens, so iOS still sees the user's tap.
 */
export function onNotifyTap() {
  const plan = permissionPlan(env());
  if (plan === 'request') {
    const asked = Notification.requestPermission();
    asking = true;
    return asked
      .then((permission) => {
        if (permission === 'granted') return subscribeGranted();
        if (permission === 'denied') showDeniedHelp();
        return false;
      })
      .finally(() => {
        asking = false;
      });
  }
  if (plan === 'subscribe') {
    asking = true;
    return subscribeGranted().finally(() => {
      asking = false;
    });
  }
  if (plan === 'install-prompt') {
    openPopup('iosInstallPopup');
    return Promise.resolve(false);
  }
  if (plan === 'denied-help') {
    showDeniedHelp();
    return Promise.resolve(false);
  }
  if (plan === 'unsupported') {
    showToast("This browser can't receive notifications.");
    return Promise.resolve(false);
  }
  document.dispatchEvent(new CustomEvent('open-settings'));
  return Promise.resolve(true);
}

/**
 * How to unblock notifications. Never names the app: iOS lists it under the
 * name it got when it was added to the Home Screen, which the user can edit.
 */
export function deniedHelpText(ios) {
  return ios
    ? "Notifications are blocked. Turn them on in iOS Settings → Notifications, under this app's name."
    : "Notifications are blocked. Allow them for this site in your browser's settings.";
}

function showDeniedHelp() {
  showToast(deniedHelpText(isIOS()), 7000);
}

export async function unsubscribe() {
  try {
    const sub = await browserSubscription();
    if (sub) {
      // Tell the server first; only drop the browser subscription once that worked
      const res = await fetch(`${base()}/subscribe`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      if (!res.ok && res.status !== 404) throw new Error(`Server refused unsubscribe (${res.status})`);
      await sub.unsubscribe();
    }
    currentSubscription = null;
    subscribed = false;
    store.set('pushSubscribed', 'false');
    store.remove('notificationPrefs');
    announce();
    showToast('Notifications off');
    return true;
  } catch (err) {
    console.error('Unsubscribe failed:', err);
    showToast('Could not turn off notifications. Try again in a moment.');
    return false;
  }
}

/** Change one preference: optimistic locally, reverted if the server refuses. */
export async function setPref(key, value) {
  const prefs = getPrefs();
  const previous = prefs[key];
  savePrefs({ ...prefs, [key]: value });
  const revert = (msg) => {
    savePrefs({ ...getPrefs(), [key]: previous });
    showToast(msg);
  };
  const sub = await browserSubscription();
  if (!sub) {
    revert('Not subscribed on this device');
    return;
  }
  try {
    const res = await fetch(`${base()}/preferences`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: sub.endpoint, preferences: { [key]: value } }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json().catch(() => null);
    if (data && data.preferences) savePrefs({ ...DEFAULT_PREFS, ...data.preferences });
  } catch (err) {
    console.error('Failed to save preference:', err);
    revert('Could not save that setting');
  }
}

// ── Popups ────────────────────────────────────────────────────────────

let popupReturnFocus = null;

export function anyDialogOpen() {
  return ['sheet', 'settingsSheet', 'firstLaunchPopup', 'iosInstallPopup'].some((id) => {
    const el = document.getElementById(id);
    return el && !el.hidden;
  });
}

function openPopup(id) {
  const el = document.getElementById(id);
  popupReturnFocus = document.activeElement;
  el.hidden = false;
  const first = el.querySelector('button');
  if (first) first.focus();
}

function closePopup(id) {
  document.getElementById(id).hidden = true;
  if (popupReturnFocus && popupReturnFocus.focus) popupReturnFocus.focus();
  popupReturnFocus = null;
}

/** Re-send the subscription so the server knows it (and return its prefs). */
async function resync(sub) {
  try {
    const res = await fetch(`${base()}/subscribe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    if (!res.ok) return;
    const data = await res.json().catch(() => null);
    if (data && data.preferences) savePrefs({ ...DEFAULT_PREFS, ...data.preferences });
  } catch {
    // offline: try again next launch
  }
}

function maybeFirstLaunch() {
  const hasVisited = store.get('hasVisited') === 'true';
  store.set('hasVisited', 'true');
  if (hasVisited) return;
  setTimeout(() => {
    const iosBrowser = env().iosBrowser;
    const show = shouldShowFirstLaunch({
      hasVisited: false,
      subscribed,
      // An iOS Safari tab has no push until installed: it gets the install popup
      pushSupported: pushSupported() || iosBrowser,
      dialogOpen: anyDialogOpen(),
      asking,
      permission: env().permission,
    });
    if (!show) return;
    if (iosBrowser) openPopup('iosInstallPopup');
    else openPopup('firstLaunchPopup');
  }, FIRST_LAUNCH_DELAY_MS);
}

/** Register the service worker, restore subscription state, wire popups. */
export function initNotify() {
  subscribed = store.get('pushSubscribed') === 'true';
  announce();

  document.getElementById('popupEnableBtn').addEventListener('click', () => {
    closePopup('firstLaunchPopup');
    onNotifyTap();
  });
  document.getElementById('popupDismissBtn').addEventListener('click', () => closePopup('firstLaunchPopup'));
  document.getElementById('iosInstallDismissBtn').addEventListener('click', () => closePopup('iosInstallPopup'));
  for (const id of ['firstLaunchPopup', 'iosInstallPopup']) {
    const popup = document.getElementById(id);
    // A tap on the dimmed backdrop (not the card) closes it, like Escape
    popup.addEventListener('click', (e) => {
      if (e.target === popup) closePopup(id);
    });
    popup.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closePopup(id);
      else trapTab(popup, e);
    });
  }

  // Independent of the service worker: it checks `subscribed` when it fires,
  // by which time the ready() handler below has usually corrected it.
  maybeFirstLaunch();
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('sw.js').catch((err) => console.warn('Service worker registration failed:', err));
  if (!pushSupported()) return;
  navigator.serviceWorker.ready.then(async (reg) => {
    try {
      const sub = await reg.pushManager.getSubscription();
      currentSubscription = sub;
      subscribed = reconcileSubscribed(subscribed, Boolean(sub));
      store.set('pushSubscribed', String(subscribed));
      if (sub) resync(sub);
    } catch {
      // keep the localStorage state
    }
    announce();
  });
  // The browser rotated our push endpoint (sw.js pushsubscriptionchange):
  // send the new one now, or the server keeps pushing to a dead endpoint.
  navigator.serviceWorker.addEventListener('message', async (e) => {
    if (!isSubChangeMessage(e.data)) return;
    currentSubscription = null;
    const sub = await browserSubscription();
    if (sub) resync(sub);
  });
}
