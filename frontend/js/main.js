// Boot: refresh loop, render the main screen, wire the buttons.

import { viewModel, nextUnits, unitsToast } from './model.js';
import { renderSky } from './sky.js';
import { fetchAnswer, lastAnswer } from './api.js';
import { showToast } from './toast.js';
import { initNotify, onNotifyTap } from './notify.js';
import { initSheet } from './sheet.js';
import { initSettings } from './settings.js';

const REFRESH_MS = 60000;
const MIN_REFRESH_GAP_MS = 5000;
const UNITS_KEY = 'units';
const SHARE_URL = 'https://rain-on-trump.pages.dev/';

const ICONS = {
  pin: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffd27a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"></path><circle cx="12" cy="9.5" r="2.5"></circle></svg>',
  plane: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#7dd3fc" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 13l8-1 5-7h2l-3 7 6-.5c1.2 0 1.2 2 0 2l-6-.5 3 7h-2l-5-7-8-1z"></path></svg>',
  clock: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#c9ced8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>',
};

const $ = (id) => document.getElementById(id);

export const app = {
  /** Latest successful payload (null when the last request failed). */
  data: null,
  /** False until the first request has finished (success or not). */
  answered: false,
  /** Latest view model, for the details sheet. */
  vm: null,
};

export function prefs() {
  let units = unitsOverride || 'auto';
  try {
    units = localStorage.getItem(UNITS_KEY) || units;
  } catch {
    // keep the in-memory choice
  }
  return { units, locale: navigator.language || 'en-US' };
}

let unitsOverride = null;

export function setUnits(value) {
  unitsOverride = value;
  try {
    localStorage.setItem(UNITS_KEY, value);
  } catch {
    // not persisted; unitsOverride keeps it for this session
  }
  render();
  document.dispatchEvent(new CustomEvent('units-applied', { detail: value }));
}

/** Rain/Snow tile tap: switch to the other unit system and say so. */
function switchUnits() {
  const p = prefs();
  const next = nextUnits(p.units, p.locale);
  setUnits(next);
  showToast(unitsToast(next));
}

function renderReadings(readings, units) {
  const box = $('readings');
  const other = units === 'imperial' ? 'millimetres' : 'inches';
  box.replaceChildren(
    ...readings.map((r) => {
      const tile = document.createElement(r.switchable ? 'button' : 'div');
      tile.className = 'reading';
      if (r.switchable) {
        tile.type = 'button';
        tile.setAttribute('aria-label', `${r.label} ${r.value}. Show ${other}`);
        tile.addEventListener('click', switchUnits);
      }
      const label = document.createElement('span');
      label.className = 'reading-label';
      label.textContent = r.label;
      const value = document.createElement('span');
      value.className = 'reading-value';
      value.textContent = r.value;
      tile.append(label, value);
      return tile;
    }),
  );
}

function setText(id, text) {
  const el = $(id);
  if (el.textContent !== text) el.textContent = text;
}

/** Recompute the view model from the latest data and paint the screen. */
export function render() {
  const now = new Date();
  const last = lastAnswer();
  let vm;
  if (!app.answered) {
    if (!last) return; // keep the static "Loading…" markup
    vm = viewModel(last.data, now, prefs());
    vm = { ...vm, status: { tone: 'searching', text: 'Updating…' } };
  } else {
    vm = viewModel(app.data, now, prefs(), app.data ? undefined : last || undefined);
  }
  app.vm = vm;

  renderSky($('sky'), vm.sky);
  $('status').dataset.tone = vm.status.tone;
  setText('statusText', vm.status.text);
  setText('answer', vm.answer);
  setText('subline', vm.subline);
  renderReadings(vm.readings, vm.units);

  const card = $('locationCard');
  card.dataset.state = vm.state;
  card.dataset.stale = String(vm.location.stale);
  card.disabled = vm.state === 'unknown' || (vm.state === 'offline' && !last);
  $('locationIcon').innerHTML = ICONS[vm.location.icon] || ICONS.pin;
  setText('locationTitle', vm.location.title);
  setText('locationMeta', vm.location.meta);
  document.dispatchEvent(new CustomEvent('vm-change', { detail: vm }));
}

let lastRefresh = 0;

async function refresh() {
  lastRefresh = Date.now();
  const res = await fetchAnswer();
  if (res.superseded) return;
  app.data = res.data || null;
  app.answered = true;
  render();
}

function refreshOnReturn() {
  if (Date.now() - lastRefresh >= MIN_REFRESH_GAP_MS) refresh();
}

async function copyLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    showToast('Link copied');
  } catch {
    showToast(url, 6000);
  }
}

async function share() {
  const data = {
    title: 'Is It Raining on Trump?',
    text: 'Is it raining on Trump right now?',
    url: SHARE_URL,
  };
  // Web Share only on phones (desktop support is inconsistent)
  if (/Android|iPhone|iPad|iPod/i.test(navigator.userAgent) && navigator.share) {
    try {
      await navigator.share(data);
    } catch (err) {
      if (err.name !== 'AbortError') await copyLink(SHARE_URL);
    }
  } else {
    await copyLink(SHARE_URL);
  }
}

function renderNotifyButton(on) {
  const btn = $('notifyBtn');
  btn.dataset.on = String(on);
  setText('notifyText', on ? 'Notifications on' : 'Tell me when it rains');
}

function boot() {
  $('shareBtn').addEventListener('click', share);
  // onNotifyTap must run synchronously inside the tap (iOS permission rule)
  $('notifyBtn').addEventListener('click', () => onNotifyTap());
  document.addEventListener('subscription-change', (e) => renderNotifyButton(e.detail.subscribed));
  initSheet();
  initSettings({ units: () => prefs().units });
  document.addEventListener('units-change', (e) => setUnits(e.detail));
  initNotify();
  render(); // last good answer (or the offline screen) until the first response
  refresh();
  setInterval(refresh, REFRESH_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshOnReturn();
  });
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) refreshOnReturn();
  });
  window.addEventListener('focus', refreshOnReturn);
}

boot();
