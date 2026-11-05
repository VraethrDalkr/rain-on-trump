// View model: turns an /is_it_raining.json payload into everything the screen
// shows. Pure: no DOM, no fetch, no clock (``now`` is passed in), so it can be
// unit-tested with `node --test tests/frontend/*.test.mjs`.

const IMPERIAL_REGIONS = new Set(['US', 'LR', 'MM']);
const HEAVY_RAIN_MMH = 2.5;
const DUSK_MS = 45 * 60e3;
const FACTBASE_CALENDAR = 'https://rollcall.com/factbase/trump/topic/calendar/';

export const MAX_DROPS = 110;
export const MAX_NEAR_DROPS = 28;
export const FLAKES = 64;

const SUBLINES = {
  dry: 'Dry skies over Trump right now.',
  stormOnly: 'A thunderstorm, but not a drop on him yet.',
  flight: "He's above the clouds. Nothing falls up there.",
  unknown: "We've lost track of him. No schedule, no plane, no clue.",
  weatherError: 'Weather data unavailable right now.',
  offline: "Can't reach the server right now.",
  freshNote: 'Based on the newest trustworthy report.',
  staleNote: 'This report is getting old: treat it as a best guess.',
};

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** Rain particle counts for a rain rate in mm/h (log scale, clamped). */
export function dropsFor(mmh) {
  const r = num(mmh) ?? 0;
  if (r <= 0) return { drops: 0, nearDrops: 0 };
  const drops = clamp(Math.round(30 + 22 * Math.log(1 + r)), 0, MAX_DROPS);
  return { drops, nearDrops: clamp(Math.round(drops / 4), 0, MAX_NEAR_DROPS) };
}

/** 'metric' | 'imperial' from the user's choice and the device locale. */
export function resolveUnits(pref, locale) {
  if (pref === 'metric' || pref === 'imperial') return pref;
  let region = '';
  try {
    region = new Intl.Locale(locale || 'en').maximize().region || '';
  } catch {
    region = '';
  }
  // maximize() guesses a region for bare languages ("en" → US); only trust an
  // explicit one.
  const explicit = /[-_]([A-Za-z]{2}|\d{3})\b/.test(locale || '');
  return explicit && IMPERIAL_REGIONS.has(region) ? 'imperial' : 'metric';
}

/** Units after a tap on a Rain/Snow tile: the other system, stored explicitly. */
export function nextUnits(pref, locale) {
  return resolveUnits(pref, locale) === 'metric' ? 'imperial' : 'metric';
}

/** Toast after a tile tap. */
export function unitsToast(units) {
  return units === 'imperial'
    ? 'Showing inches · tap again for millimetres'
    : 'Showing millimetres · tap again for inches';
}

/** "3.1 mm/h", "0.12 in/h", "0.4 cm/h"; tiny non-zero values as "<0.1". */
export function fmtRate(value, kind, units) {
  const v = num(value) ?? 0;
  let amount = v;
  let unit = kind === 'snow' ? 'cm/h' : 'mm/h';
  let decimals = 1;
  if (units === 'imperial') {
    amount = kind === 'snow' ? v / 2.54 : v / 25.4;
    unit = 'in/h';
    decimals = kind === 'snow' ? 1 : 2;
  }
  if (v <= 0) return `0 ${unit}`;
  const floor = 10 ** -decimals;
  if (amount < floor) return `<${floor.toFixed(decimals)} ${unit}`;
  return `${amount.toFixed(decimals)} ${unit}`;
}

function sunMs(utcIso, localIso, offsetS) {
  if (utcIso) return Date.parse(utcIso);
  if (!localIso || num(offsetS) === null) return NaN;
  if (/(Z|[+-]\d\d:?\d\d)$/.test(localIso)) return Date.parse(localIso);
  return Date.parse(`${localIso}Z`) - offsetS * 1000;
}

/** 'day' | 'dusk' | 'night' at the location (dusk = ±45 min of sunrise/set). */
export function timeOfDay(now, data) {
  const d = data || {};
  const t = now.getTime();
  const rise = sunMs(d.sunrise_utc, d.sunrise, d.utc_offset_seconds);
  const set = sunMs(d.sunset_utc, d.sunset, d.utc_offset_seconds);
  if (Number.isFinite(rise) && Number.isFinite(set)) {
    // The sun times are for the location's current date; shift by whole days
    // so a "now" just past local midnight still compares correctly.
    const day = 864e5;
    const shift = Math.round((t - (rise + set) / 2) / day) * day;
    const r = rise + shift;
    const s = set + shift;
    if (Math.abs(t - r) <= DUSK_MS || Math.abs(t - s) <= DUSK_MS) return 'dusk';
    return t > r && t < s ? 'day' : 'night';
  }
  const offset = num(d.utc_offset_seconds);
  if (offset !== null) {
    const hour = new Date(t + offset * 1000).getUTCHours();
    return hour >= 6 && hour < 18 ? 'day' : 'night';
  }
  return 'night';
}

function ageText(hours) {
  const h = num(hours);
  if (h === null) return '';
  if (h < 1) return 'just now';
  if (h < 24) return `${Math.floor(h)} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

function minutesAgo(fromMs, nowMs) {
  const m = Math.max(0, Math.round((nowMs - fromMs) / 60e3));
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  return ageText(m / 60);
}

function clock(iso, prefs) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  return new Intl.DateTimeFormat(prefs.locale || 'en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: prefs.timeZone,
  }).format(new Date(ms));
}

const DAY_MS = 864e5;

function dayKey(ms, prefs) {
  // YYYY-MM-DD in the display time zone
  return new Intl.DateTimeFormat('en-CA', { timeZone: prefs.timeZone }).format(new Date(ms));
}

/** "7:12 PM" today, "Sat 7:12 PM" within the last week, else "Oct 25, 7:12 PM". */
function when(iso, now, prefs) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  const opts = { hour: 'numeric', minute: '2-digit', timeZone: prefs.timeZone };
  if (dayKey(ms, prefs) !== dayKey(now.getTime(), prefs)) {
    if (now.getTime() - ms < 6 * DAY_MS) opts.weekday = 'short';
    else Object.assign(opts, { month: 'short', day: 'numeric' });
  }
  return new Intl.DateTimeFormat(prefs.locale || 'en-US', opts).format(new Date(ms)).replace(' at ', ', ');
}

/**
 * What a Factba.se schedule item says happened, shortened for a timeline:
 * "The President departs X for Y [6:11 PM Local]" → "Departs X for Y".
 */
export function eventText(summary) {
  if (typeof summary !== 'string') return '';
  const text = summary
    .replace(/\s*\[[^\]]*\]/g, '')
    .replace(/^The President( and The First Lady)?\s+/, '')
    .trim();
  return text ? text[0].toUpperCase() + text.slice(1) : '';
}

function windClass(kmh) {
  const v = num(kmh);
  if (v === null || v < 15) return 'calm';
  return v < 35 ? 'breezy' : 'gusty';
}

function stormLevel(state) {
  return state === 'moderate' || state === 'severe' ? state : 'none';
}

function weatherSubline(rainMmh, snow, storm) {
  const parts = [];
  if (rainMmh > 0) parts.push(rainMmh >= HEAVY_RAIN_MMH ? 'heavy rain' : 'light rain');
  if (snow > 0) parts.push('snow');
  if (!parts.length) return storm === 'none' ? SUBLINES.dry : SUBLINES.stormOnly;
  const what = parts.join(' and ');
  const stormText = {
    none: '',
    moderate: ', with thunder',
    severe: ', with a severe thunderstorm',
  }[storm];
  return `${what[0].toUpperCase()}${what.slice(1)}${stormText}.`;
}

function sinceLabel(coords) {
  if (!coords.in_flight) return 'Since';
  // A scheduled flight's time is its departure; an ADS-B one is the last fix
  return coords.reason === 'en_route' && coords.eta ? 'Departed' : 'Last seen';
}

function timelineTitle(e) {
  if (e.kind === 'landing' || e.kind === 'landing_inferred') return `Landed at ${e.place}`;
  if (e.kind && e.kind.startsWith('overnight')) return `Overnight at ${e.place}`;
  return e.place;
}

const httpsOnly = (url) => (typeof url === 'string' && /^https:\/\//i.test(url) ? url : null);

/** Where the location came from: link + a label saying what it is. */
function sourceLink(coords) {
  const url = httpsOnly(coords && coords.source_url);
  if (!url) return { source: FACTBASE_CALENDAR, sourceLabel: 'Schedule' };
  const reason = String((coords && coords.reason) || '');
  if (reason.startsWith('plane') || reason.startsWith('landing') || coords.airframe) {
    return { source: url, sourceLabel: 'Tracker' };
  }
  if (reason === 'newswire') return { source: url, sourceLabel: 'Article' };
  return { source: url, sourceLabel: 'Schedule' };
}

function links(coords) {
  const lat = num(coords && coords.lat);
  const lon = num(coords && coords.lon);
  const src = sourceLink(coords);
  if (lat === null || lon === null) return { radar: null, map: null, ...src };
  const ll = `${lat.toFixed(4)},${lon.toFixed(4)}`;
  return {
    radar: `https://www.rainviewer.com/map.html?loc=${ll},8`,
    map: `https://www.openstreetmap.org/?mlat=${lat.toFixed(4)}&mlon=${lon.toFixed(4)}#map=12/${lat.toFixed(4)}/${lon.toFixed(4)}`,
    ...src,
  };
}

function baseSky(time) {
  return {
    time,
    wet: false,
    kind: 'clear',
    drops: 0,
    nearDrops: 0,
    flakes: 0,
    storm: 'none',
    wind: 'calm',
    stars: time === 'night',
    sun: time === 'day',
  };
}

function offlineModel(now, prefs) {
  return {
    state: 'offline',
    units: resolveUnits(prefs.units, prefs.locale),
    answer: '???',
    subline: SUBLINES.offline,
    sky: { ...baseSky('night'), kind: 'fog', stars: false },
    readings: [],
    location: { title: 'No connection', meta: '', confidence: null, stale: true, icon: 'clock' },
    status: { tone: 'offline', text: 'Offline' },
    timeline: [],
    links: links(null),
  };
}

/**
 * Build the view model.
 *
 * @param {object|null} data  /is_it_raining.json payload, or null when the
 *   request failed.
 * @param {Date} now
 * @param {{units: string, locale: string, timeZone?: string}} prefs
 * @param {{data: object, at: Date}=} last  last good payload (offline mode)
 */
export function viewModel(data, now, prefs, last) {
  if (!data) {
    if (!last || !last.data) return offlineModel(now, prefs);
    const vm = viewModel(last.data, now, prefs);
    return {
      ...vm,
      state: 'offline',
      status: {
        tone: 'offline',
        text: `Offline · ${minutesAgo(last.at.getTime(), now.getTime())}`,
      },
    };
  }

  const units = resolveUnits(prefs.units, prefs.locale);
  const coords = data.coords || {};
  const time = timeOfDay(now, data);
  const evidence = Array.isArray(coords.evidence) ? coords.evidence : [];
  const timeline = evidence.map((e) => ({
    title: timelineTitle(e),
    meta: [when(e.t, now, prefs), eventText(e.summary) || e.source].filter(Boolean).join(' · '),
    current: Boolean(e.current),
  }));
  const current = evidence.find((e) => e.current);
  const confidence = num(coords.confidence);
  const liveStatus = {
    tone: 'live',
    text: `Live · ${clock(data.timestamp, prefs) || clock(now.toISOString(), prefs)}`,
  };

  if (coords.unknown) {
    const lk = coords.last_known;
    return {
      state: 'unknown',
      units,
      answer: '???',
      subline: SUBLINES.unknown,
      sky: { ...baseSky(time), kind: 'fog', wet: true, stars: false, sun: false },
      readings: [],
      location: {
        title: lk && lk.name ? `Last seen: ${lk.name}` : 'No recent sighting',
        meta: [lk ? ageText(lk.age_h) : '', 'too old to trust'].filter(Boolean).join(' · '),
        confidence: null,
        stale: true,
        icon: 'clock',
      },
      status: { tone: 'searching', text: `Searching · ${clock(data.timestamp, prefs)}` },
      timeline: [],
      links: links(null),
    };
  }

  const since = clock(coords.observed_at, prefs);
  // A stay that started before its newest report: "Since Sun 1:52 AM · last report 10:30 AM"
  const stayDetail = current && current.since && Date.parse(current.since) < Date.parse(current.t)
    ? `Since ${when(current.since, now, prefs)} · last report ${when(current.t, now, prefs)}`
    : '';
  const location = {
    title: coords.name || 'Unknown place',
    meta: [coords.source_display, confidence !== null ? `${confidence}% sure` : '']
      .filter(Boolean)
      .join(' · '),
    detail: (!coords.in_flight && stayDetail)
      || [since ? `${sinceLabel(coords)} ${since}` : '', coords.source_display].filter(Boolean).join(' · '),
    note: coords.stale ? SUBLINES.staleNote : SUBLINES.freshNote,
    confidence,
    stale: Boolean(coords.stale),
    icon: 'pin',
  };

  if (coords.in_flight) {
    const readings = [];
    if (coords.eta) readings.push({ kind: 'lands', switchable: false, label: 'Lands', value: `~${clock(coords.eta, prefs)}` });
    if (coords.airframe) readings.push({ kind: 'aircraft', switchable: false, label: 'Aircraft', value: coords.airframe });
    return {
      state: 'in_flight',
      units,
      answer: 'NO',
      subline: SUBLINES.flight,
      sky: { ...baseSky(time), kind: 'flight', stars: false, sun: false },
      readings,
      location: {
        ...location,
        title: coords.destination ? `In flight to ${coords.destination}` : location.title,
        icon: 'plane',
      },
      status: liveStatus,
      timeline,
      links: links(coords),
    };
  }

  const storm = stormLevel(data.thunderstorm_state);
  const known = typeof data.precipitating === 'boolean';
  const rain = known ? num(data.mmh) ?? 0 : 0;
  const snow = known ? num(data.snow) ?? 0 : 0;
  const { drops, nearDrops } = dropsFor(rain);
  const kind = rain > 0 && snow > 0 ? 'mixed'
    : rain > 0 ? 'rain'
      : snow > 0 ? 'snow'
        : storm !== 'none' ? 'storm' : 'clear';
  const wet = kind !== 'clear';
  const answer = !known ? '???' : data.precipitating ? 'YES' : 'NO';

  return {
    state: 'answer',
    units,
    answer,
    subline: known ? weatherSubline(rain, snow, storm) : SUBLINES.weatherError,
    sky: {
      time,
      wet,
      kind,
      drops,
      nearDrops,
      flakes: snow > 0 ? FLAKES : 0,
      storm,
      wind: windClass(data.wind_kmh),
      stars: time === 'night' && !wet,
      sun: time === 'day' && !wet,
    },
    readings: known
      ? [
        { kind: 'rain', switchable: true, label: 'Rain', value: fmtRate(rain, 'rain', units) },
        { kind: 'snow', switchable: true, label: 'Snow', value: fmtRate(snow, 'snow', units) },
        {
          kind: 'storm',
          switchable: false,
          label: 'Storm',
          value: { none: 'None', moderate: 'Thunder', severe: 'Severe' }[storm],
        },
      ]
      : [],
    location,
    status: liveStatus,
    timeline,
    links: links(coords),
  };
}
