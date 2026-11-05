// Debug effects panel, dev PWA and local hosts only. Five quick taps on the
// status line open it; a preset replaces the live answer with a fake payload
// that goes through the real view model and sky, so every effect (sun, moon,
// rain, snow, storm, fog, plane) can be checked on a phone at any time.

const TAPS = 5;
const TAP_WINDOW_MS = 3000;
const H = 3600e3;
const WH = { lat: 38.897676, lon: -77.036529, name: 'The White House' };

/** The panel exists only on the dev PWA (any deploy URL) and local hosts. */
export function isDebugHost(hostname) {
  return /(^|\.)rain-on-trump-dev\.pages\.dev$|^(localhost|127\.|10\.|192\.168\.)/.test(hostname || '');
}

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

/** Sun times that make *now* fall in day, dusk or night. */
function sunTimes(time, now) {
  const t = now.getTime();
  const [rise, set] = {
    day: [t - 5 * H, t + 5 * H],
    dusk: [t - 10 * H, t + 5 * 60e3],
    night: [t + 7 * H, t - 7 * H],
  }[time];
  return { sunrise_utc: iso(rise), sunset_utc: iso(set), sunrise: null, sunset: null, utc_offset_seconds: null };
}

/** Longitude where the solar time is *hour* (in flight there are no sun times). */
function lonAt(hour, now) {
  const utc = now.getUTCHours() + now.getUTCMinutes() / 60;
  return ((((hour - utc) * 15 + 540) % 360) + 360) % 360 - 180;
}

function weather(time, now, { mmh = 0, snow = 0, storm = 'none', wind = 8 } = {}) {
  const wet = mmh > 0 || snow > 0;
  return {
    precipitating: wet,
    mmh,
    snow,
    precipitation_type: mmh > 0 && snow > 0 ? 'both' : mmh > 0 ? 'rain' : snow > 0 ? 'snow' : 'none',
    thunderstorm: storm !== 'none',
    thunderstorm_state: storm,
    wind_kmh: wind,
    ...sunTimes(time, now),
    coords: { ...WH, reason: 'calendar_alias', confidence: 82, age_h: 1, stale: false, source_display: 'Debug preset' },
  };
}

function flight(hour, now) {
  return {
    precipitating: false, mmh: 0, snow: 0, precipitation_type: 'none',
    thunderstorm: false, thunderstorm_state: 'none', wind_kmh: null,
    sunrise: null, sunset: null, sunrise_utc: null, sunset_utc: null, utc_offset_seconds: null,
    coords: {
      lat: 33.1, lon: lonAt(hour, now), name: 'Air Force One (airborne)', reason: 'plane_air',
      in_flight: true, confidence: 95, age_h: 0, stale: false, airframe: '92-9000', source_display: 'Debug preset',
    },
  };
}

/** Presets shown in the panel, in order. */
export const PRESETS = [
  { id: 'day-clear', label: 'Day, clear (sun)', make: (n) => weather('day', n) },
  { id: 'night-clear', label: 'Night, clear (moon, stars)', make: (n) => weather('night', n) },
  { id: 'day-rain', label: 'Day, rain', make: (n) => weather('day', n, { mmh: 2.5 }) },
  { id: 'day-storm', label: 'Day, thunder, no rain', make: (n) => weather('day', n, { storm: 'moderate', wind: 25 }) },
  { id: 'dusk-snow', label: 'Dusk, snow', make: (n) => weather('dusk', n, { snow: 0.6, wind: 20 }) },
  { id: 'night-severe', label: 'Night, heavy rain, severe storm', make: (n) => weather('night', n, { mmh: 9, storm: 'severe', wind: 30 }) },
  { id: 'night-mixed', label: 'Night, rain and snow', make: (n) => weather('night', n, { mmh: 1.2, snow: 0.4 }) },
  { id: 'gusty-rain', label: 'Day, gusty rain', make: (n) => weather('day', n, { mmh: 4, wind: 55 }) },
  { id: 'flight-day', label: 'In flight, day', make: (n) => flight(13, n) },
  { id: 'flight-night', label: 'In flight, night', make: (n) => flight(1, n) },
  {
    id: 'unknown',
    label: 'Unknown (fog)',
    make: () => ({
      precipitating: null, mmh: null, snow: null, precipitation_type: null,
      thunderstorm: null, thunderstorm_state: null, wind_kmh: null,
      coords: { unknown: true, confidence: 0, reason: 'unknown', last_known: { ...WH, kind: 'calendar_alias', age_h: 96 } },
    }),
  },
];

/** Fake /is_it_raining.json payload for a preset at *now*. */
export function presetPayload(id, now) {
  const p = PRESETS.find((x) => x.id === id);
  if (!p) return null;
  return { ...p.make(now), timestamp: now.toISOString() };
}

export function presetLabel(id) {
  return (PRESETS.find((x) => x.id === id) || {}).label || id;
}

/**
 * Wire the hidden trigger and panel. *onPick(id|null)*: a preset id, or null
 * for live data. No-op outside debug hosts.
 */
export function initDebug(onPick) {
  if (!isDebugHost(location.hostname)) return;
  const status = document.getElementById('status');
  let taps = [];
  status.addEventListener('click', () => {
    const t = Date.now();
    taps = [...taps.filter((x) => t - x < TAP_WINDOW_MS), t];
    if (taps.length >= TAPS) {
      taps = [];
      openPanel(onPick);
    }
  });
}

function openPanel(onPick) {
  document.querySelector('.debug-panel')?.remove();
  const panel = document.createElement('div');
  panel.className = 'debug-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Debug effects');
  const close = () => {
    panel.remove();
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
  };
  const button = (label, fn, cls = '') => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    if (cls) b.className = cls;
    b.addEventListener('click', fn);
    return b;
  };
  const title = document.createElement('div');
  title.className = 'debug-title';
  title.textContent = 'Debug effects';
  const grid = document.createElement('div');
  grid.className = 'debug-grid';
  for (const p of PRESETS) {
    grid.append(button(p.label, () => {
      onPick(p.id);
      close();
    }));
  }
  const row = document.createElement('div');
  row.className = 'debug-row';
  row.append(
    button('Live data', () => {
      onPick(null);
      close();
    }, 'debug-live'),
    button('Close', close),
  );
  panel.append(title, grid, row);
  document.body.append(panel);
  document.addEventListener('keydown', onKey);
  panel.querySelector('button').focus();
}
