// Settings sheet: notifications (switches, turn off, or turn on) and units.
// Opened by the gear button, and by "Notifications on". Works before the first
// answer: nothing here depends on the location.

import { createSheet } from './dialog.js';
import { resolveUnits } from './model.js';
import {
  getPrefs, setPref, unsubscribe, isSubscribed, onNotifyTap, thunderstormAllowed,
} from './notify.js';

const $ = (id) => document.getElementById(id);

let sheet = null;
let getUnits = () => 'auto';

function renderPrefs() {
  const on = isSubscribed();
  $('prefsOn').hidden = !on;
  $('prefsOff').hidden = on;
  const prefs = getPrefs();
  for (const key of ['rain_start', 'rain_stop', 'thunderstorm']) {
    $(`pref-${key}`).checked = Boolean(prefs[key]);
  }
  $('pref-thunderstorm').disabled = !thunderstormAllowed(prefs);
}

function renderUnits() {
  const pref = getUnits();
  const auto = resolveUnits('auto', navigator.language || 'en-US') === 'imperial' ? 'in' : 'mm';
  $('unitsAutoLabel').textContent = `Auto (${auto})`;
  for (const input of document.querySelectorAll('input[name="units"]')) {
    input.checked = input.value === pref;
  }
}

function render() {
  renderPrefs();
  renderUnits();
}

export function openSettings(trigger) {
  sheet.open(trigger);
}

/**
 * @param {{units: () => string}} opts  current units preference getter
 */
export function initSettings(opts) {
  getUnits = opts.units;
  sheet = createSheet({
    el: $('settingsSheet'),
    backdrop: $('sheetBackdrop'),
    heading: $('settingsTitle'),
    closeBtn: $('settingsClose'),
    onOpen: () => {
      $('offConfirm').hidden = true;
      $('offBtn').hidden = false;
      render();
    },
  });
  $('settingsBtn').addEventListener('click', (e) => sheet.open(e.currentTarget));

  for (const input of document.querySelectorAll('input[name="units"]')) {
    input.addEventListener('change', () => {
      if (input.checked) document.dispatchEvent(new CustomEvent('units-change', { detail: input.value }));
    });
  }
  for (const key of ['rain_start', 'rain_stop', 'thunderstorm']) {
    $(`pref-${key}`).addEventListener('change', (e) => setPref(key, e.target.checked));
  }
  $('offBtn').addEventListener('click', () => {
    $('offBtn').hidden = true;
    $('offConfirm').hidden = false;
    $('offNo').focus();
  });
  $('offNo').addEventListener('click', () => {
    $('offConfirm').hidden = true;
    $('offBtn').hidden = false;
    $('offBtn').focus();
  });
  $('offYes').addEventListener('click', async () => {
    await unsubscribe();
    $('offConfirm').hidden = true;
    $('offBtn').hidden = false;
    renderPrefs();
    $('settingsTitle').focus();
  });
  // Synchronous call inside the tap (iOS permission rule)
  $('sheetNotifyBtn').addEventListener('click', () => onNotifyTap());

  document.addEventListener('prefs-change', () => sheet.isOpen() && renderPrefs());
  document.addEventListener('subscription-change', () => sheet.isOpen() && renderPrefs());
  document.addEventListener('units-applied', () => sheet.isOpen() && renderUnits());
  // "Notifications on" asks for the settings (notify.js plan 'settings')
  document.addEventListener('open-settings', () => sheet.open($('notifyBtn')));
}
