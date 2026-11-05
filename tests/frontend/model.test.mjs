// Unit tests for frontend/js/model.js (run: node --test tests/frontend/)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  viewModel, resolveUnits, fmtRate, timeOfDay, dropsFor, nextUnits, unitsToast, eventText,
} from '../../frontend/js/model.js';

const fx = (name) =>
  JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url)));
const PREFS = { units: 'metric', locale: 'en-US', timeZone: 'America/New_York' };
const at = (data) => new Date(data.timestamp);
const vmOf = (name, prefs = PREFS) => {
  const data = fx(name);
  return viewModel(data, at(data), prefs);
};

test('answer YES with heavy rain, storm at night', () => {
  const vm = vmOf('rain-night-storm');
  assert.equal(vm.state, 'answer');
  assert.equal(vm.answer, 'YES');
  assert.equal(vm.sky.time, 'night');
  assert.equal(vm.sky.stars, false);
  assert.equal(vm.sky.storm, 'severe');
  assert.equal(vm.sky.wind, 'gusty');
  assert.ok(vm.sky.drops >= 70, `drops ${vm.sky.drops}`);
  assert.equal(vm.subline, 'Heavy rain, with a severe thunderstorm.');
  assert.deepEqual(vm.readings.map((r) => r.label), ['Rain', 'Snow', 'Storm']);
  assert.equal(vm.readings[0].value, '6.2 mm/h');
  assert.equal(vm.readings[2].value, 'Severe');
});

test('dry day shows sun, no particles', () => {
  const vm = vmOf('dry-day');
  assert.equal(vm.answer, 'NO');
  assert.equal(vm.subline, 'Dry skies over Trump right now.');
  assert.equal(vm.sky.time, 'day');
  assert.equal(vm.sky.sun, true);
  assert.equal(vm.sky.wet, false);
  assert.equal(vm.sky.kind, 'clear');
  assert.equal(vm.sky.drops + vm.sky.nearDrops + vm.sky.flakes, 0);
});

test('snow at dusk', () => {
  const vm = vmOf('snow-dusk');
  assert.equal(vm.answer, 'YES');
  assert.equal(vm.sky.time, 'dusk');
  assert.equal(vm.sky.kind, 'snow');
  assert.equal(vm.sky.flakes, 64);
  assert.equal(vm.sky.drops, 0);
  assert.equal(vm.subline, 'Snow.');
  assert.equal(vm.readings[1].value, '0.4 cm/h');
});

test('mixed rain and snow', () => {
  const vm = vmOf('mixed');
  assert.equal(vm.sky.kind, 'mixed');
  assert.equal(vm.sky.flakes, 64);
  assert.ok(vm.sky.drops > 0);
  assert.equal(vm.subline, 'Light rain and snow.');
});

test('storm without rain', () => {
  const vm = vmOf('storm-only');
  assert.equal(vm.answer, 'NO');
  assert.equal(vm.subline, 'A thunderstorm, but not a drop on him yet.');
  assert.equal(vm.sky.kind, 'storm');
  assert.equal(vm.sky.wet, true);
  assert.equal(vm.sky.storm, 'moderate');
});

test('in flight scheduled', () => {
  const vm = vmOf('in-flight-scheduled');
  assert.equal(vm.state, 'in_flight');
  assert.equal(vm.answer, 'NO');
  assert.equal(vm.subline, "He's above the clouds. Nothing falls up there.");
  assert.equal(vm.sky.kind, 'flight');
  assert.equal(vm.location.title, 'In flight to Palm Beach, FL');
  assert.equal(vm.location.icon, 'plane');
  const lands = vm.readings.find((r) => r.label === 'Lands');
  assert.equal(lands.value, '~4:10 PM');
});

test('in flight adsb has no Lands reading', () => {
  const vm = vmOf('in-flight-adsb');
  assert.equal(vm.state, 'in_flight');
  assert.equal(vm.readings.find((r) => r.label === 'Lands'), undefined);
  assert.equal(vm.location.title, 'Air Force One (airborne)');
});

test('unknown', () => {
  const vm = vmOf('unknown');
  assert.equal(vm.state, 'unknown');
  assert.equal(vm.answer, '???');
  assert.equal(vm.subline, "We've lost track of him. No schedule, no plane, no clue.");
  assert.equal(vm.sky.kind, 'fog');
  assert.equal(vm.location.icon, 'clock');
  assert.ok(vm.location.title.startsWith('Last seen: '), vm.location.title);
  assert.ok(vm.location.meta.endsWith('too old to trust'), vm.location.meta);
  assert.equal(vm.status.tone, 'searching');
  assert.equal(vm.links.radar, null);
});

test('unknown without any last sighting', () => {
  const data = fx('unknown');
  delete data.coords.last_known;
  const vm = viewModel(data, at(data), PREFS);
  assert.equal(vm.location.title, 'No recent sighting');
});

test('weather error gives ??? but keeps the location', () => {
  const data = fx('dry-day');
  Object.assign(data, { precipitating: null, mmh: null, error: 'Weather API error' });
  const vm = viewModel(data, at(data), PREFS);
  assert.equal(vm.state, 'answer');
  assert.equal(vm.answer, '???');
  assert.equal(vm.subline, 'Weather data unavailable right now.');
  assert.equal(vm.location.title, data.coords.name);
});

test('viewModel tolerates a v2 backend payload', () => {
  const vm = vmOf('v2-backend');
  assert.deepEqual(vm.timeline, []);
  assert.equal(vm.sky.wind, 'calm');
  assert.equal(vm.state, 'answer');
});

test('location card shows source and confidence', () => {
  const vm = vmOf('dry-day');
  assert.equal(vm.location.title, 'The White House');
  assert.equal(vm.location.confidence, 81);
  assert.equal(vm.location.icon, 'pin');
  assert.ok(vm.location.meta.includes('Factba.se schedule'));
  assert.ok(vm.location.meta.endsWith('81% sure'), vm.location.meta);
});

test('timeline from evidence, oldest first, last is current', () => {
  const vm = vmOf('in-flight-scheduled');
  assert.equal(vm.timeline.length, 2);
  assert.equal(vm.timeline[0].title, 'The White House');
  assert.equal(vm.timeline[0].meta, '1:20 PM · Factba.se schedule');
  assert.equal(vm.timeline[1].current, true);
});

test('timeline titles for landings and overnight bases', () => {
  const data = fx('dry-day');
  data.coords.evidence = [
    { t: '2026-11-04T15:00:00+00:00', place: 'Palm Beach Intl.', kind: 'landing', source: 'ADS-B', current: false },
    { t: '2026-11-04T16:00:00+00:00', place: 'Mar-a-Lago', kind: 'overnight_fl', source: 'Schedule', current: true },
  ];
  const vm = viewModel(data, at(data), PREFS);
  assert.equal(vm.timeline[0].title, 'Landed at Palm Beach Intl.');
  assert.equal(vm.timeline[1].title, 'Overnight at Mar-a-Lago');
});

test('offline view keeps last answer', () => {
  const data = fx('rain-night-storm');
  const last = { data, at: new Date(Date.parse(data.timestamp) - 5 * 60e3) };
  const vm = viewModel(null, at(data), PREFS, last);
  assert.equal(vm.state, 'offline');
  assert.equal(vm.answer, 'YES');
  assert.equal(vm.status.tone, 'offline');
  assert.equal(vm.status.text, 'Offline · 5 min ago');
});

test('offline with nothing cached', () => {
  const vm = viewModel(null, new Date('2026-11-04T20:00:00Z'), PREFS);
  assert.equal(vm.state, 'offline');
  assert.equal(vm.answer, '???');
  assert.equal(vm.subline, "Can't reach the server right now.");
});

test('live status shows the answer time', () => {
  const vm = vmOf('dry-day');
  assert.equal(vm.status.tone, 'live');
  assert.equal(vm.status.text, 'Live · 12:00 PM');
});

test('drops clamp at extremes', () => {
  assert.ok(dropsFor(0.05).drops >= 30);
  assert.deepEqual(dropsFor(40), { drops: 110, nearDrops: 28 });
  assert.deepEqual(dropsFor(0), { drops: 0, nearDrops: 0 });
  assert.ok(dropsFor(1).drops < dropsFor(5).drops);
});

test('timeOfDay: sun times, dusk window and fallbacks', () => {
  const d = fx('dry-day');
  assert.equal(timeOfDay(new Date('2026-11-04T17:00:00Z'), d), 'day');
  assert.equal(timeOfDay(new Date('2026-11-04T22:30:00Z'), d), 'dusk');
  assert.equal(timeOfDay(new Date('2026-11-04T11:00:00Z'), d), 'dusk');
  assert.equal(timeOfDay(new Date('2026-11-05T03:00:00Z'), d), 'night');
  // Naive local strings + offset (no *_utc fields)
  const local = { sunrise: '2026-11-04T06:38', sunset: '2026-11-04T17:04', utc_offset_seconds: -18000 };
  assert.equal(timeOfDay(new Date('2026-11-04T17:00:00Z'), local), 'day');
  // Offset only: 06:00-18:00 local is day
  assert.equal(timeOfDay(new Date('2026-11-04T17:00:00Z'), { utc_offset_seconds: -18000 }), 'day');
  assert.equal(timeOfDay(new Date('2026-11-05T04:00:00Z'), { utc_offset_seconds: -18000 }), 'night');
  // Nothing at all
  assert.equal(timeOfDay(new Date('2026-11-04T17:00:00Z'), {}), 'night');
});

test('resolveUnits', () => {
  assert.equal(resolveUnits('auto', 'en-US'), 'imperial');
  assert.equal(resolveUnits('auto', 'fr-CA'), 'metric');
  assert.equal(resolveUnits('auto', 'my-MM'), 'imperial');
  assert.equal(resolveUnits('auto', 'en'), 'metric');
  assert.equal(resolveUnits('metric', 'en-US'), 'metric');
  assert.equal(resolveUnits('imperial', 'fr-FR'), 'imperial');
});

test('fmtRate', () => {
  assert.equal(fmtRate(3.1, 'rain', 'metric'), '3.1 mm/h');
  assert.equal(fmtRate(3.1, 'rain', 'imperial'), '0.12 in/h');
  assert.equal(fmtRate(0.4, 'snow', 'metric'), '0.4 cm/h');
  assert.equal(fmtRate(0.4, 'snow', 'imperial'), '0.2 in/h');
  assert.equal(fmtRate(0, 'rain', 'metric'), '0 mm/h');
  assert.equal(fmtRate(0.05, 'rain', 'metric'), '<0.1 mm/h');
  assert.equal(fmtRate(0.1, 'rain', 'imperial'), '<0.01 in/h');
});

test('imperial readings when units resolve to imperial', () => {
  const vm = vmOf('rain-night-storm', { ...PREFS, units: 'auto' });
  assert.equal(vm.readings[0].value, '0.24 in/h');
  assert.equal(vm.units, 'imperial');
});

test('location detail and confidence note for the sheet', () => {
  const vm = vmOf('dry-day');
  assert.equal(vm.location.detail, 'Since 3:30 PM · Factba.se schedule, 3 h ago');
  assert.equal(vm.location.note, 'Based on the newest trustworthy report.');
  const data = fx('dry-day');
  data.coords.stale = true;
  const stale = viewModel(data, at(data), PREFS);
  assert.equal(stale.location.note, 'This report is getting old: treat it as a best guess.');
});

test('in flight detail uses the last fix time', () => {
  const vm = vmOf('in-flight-adsb');
  assert.equal(vm.location.detail, 'Last seen 2:29 PM · ADS-B (adsb.fi), just now');
});

test('scheduled flight detail says when it departed', () => {
  const vm = vmOf('in-flight-scheduled');
  assert.equal(vm.location.detail, 'Departed 2:00 PM · Factba.se schedule (flight), just now');
});

test('source link is labelled by what it points to and must be https', () => {
  const vm = vmOf('dry-day');
  assert.equal(vm.links.sourceLabel, 'Schedule');
  assert.ok(vm.links.source.startsWith('https://'));

  const plane = vmOf('in-flight-adsb');
  assert.equal(plane.links.sourceLabel, 'Tracker');
  assert.equal(plane.links.source, 'https://globe.adsbexchange.com/?icao=af83f3');

  const data = fx('dry-day');
  data.coords.reason = 'newswire';
  data.coords.source_url = 'https://example.org/story';
  assert.equal(viewModel(data, at(data), PREFS).links.sourceLabel, 'Article');

  data.coords.source_url = 'javascript:alert(1)';
  const bad = viewModel(data, at(data), PREFS);
  assert.equal(bad.links.source, 'https://rollcall.com/factbase/trump/topic/calendar/');
  assert.equal(bad.links.sourceLabel, 'Schedule');
});

test('rain and snow tiles switch units, storm does not', () => {
  const vm = vmOf('rain-night-storm');
  assert.deepEqual(vm.readings.map((r) => [r.kind, r.switchable]), [
    ['rain', true], ['snow', true], ['storm', false],
  ]);
});

test('a tile tap flips to the other unit system as an explicit choice', () => {
  assert.equal(nextUnits('auto', 'en-US'), 'metric');
  assert.equal(nextUnits('auto', 'fr-CA'), 'imperial');
  assert.equal(nextUnits('metric', 'en-US'), 'imperial');
  assert.equal(nextUnits('imperial', 'fr-CA'), 'metric');
});

test('units toast says what changed and how to undo it', () => {
  assert.equal(unitsToast('imperial'), 'Showing inches · tap again for millimetres');
  assert.equal(unitsToast('metric'), 'Showing millimetres · tap again for inches');
});

test('eventText shortens a Factba.se item to what happened', () => {
  assert.equal(
    eventText('The President departs Dubuque Regional Airport, IA for Salina Regional Airport, KS [6:11 PM Local]'),
    'Departs Dubuque Regional Airport, IA for Salina Regional Airport, KS',
  );
  assert.equal(eventText('The President and The First Lady arrive at Mar-a-Lago'), 'Arrive at Mar-a-Lago');
  assert.equal(eventText('Sean Hannity interviews The President on the telephone'), 'Sean Hannity interviews The President on the telephone');
  assert.equal(eventText(''), '');
  assert.equal(eventText(undefined), '');
});

function multiDay() {
  const data = fx('dry-day');  // timestamp: Wed 2026-11-04 12:00 PM ET
  data.coords.evidence = [
    { t: '2026-11-02T00:12:00+00:00', place: 'Dubuque Regional Airport, IA', kind: 'calendar_geocode', source: 'Factba.se schedule (geocoded)', summary: 'The President departs Dubuque Regional Airport, IA for Salina Regional Airport, KS [6:11 PM Local]', current: false },
    { t: '2026-11-02T06:45:00+00:00', place: 'Joint Base Andrews, MD', kind: 'calendar_alias', source: 'Factba.se schedule', summary: 'The President departs Joint Base Andrews en route to The White House', current: false },
    { t: '2026-11-04T15:30:00+00:00', place: 'The White House', kind: 'calendar_alias', source: 'Factba.se schedule', summary: 'The President participates in a Policy Meeting', current: true, since: '2026-11-02T06:52:00+00:00' },
  ];
  data.coords.observed_at = '2026-11-04T15:30:00+00:00';
  return data;
}

test('timeline steps show the weekday when not today, and what happened', () => {
  const data = multiDay();
  const vm = viewModel(data, at(data), PREFS);
  assert.deepEqual(vm.timeline.map((s) => s.meta), [
    'Sun 7:12 PM · Departs Dubuque Regional Airport, IA for Salina Regional Airport, KS',
    'Mon 1:45 AM · Departs Joint Base Andrews en route to The White House',
    '10:30 AM · Participates in a Policy Meeting',
  ]);
});

test('sheet header gives the start of the stay and the latest report', () => {
  const data = multiDay();
  const vm = viewModel(data, at(data), PREFS);
  assert.equal(vm.location.detail, 'Since Mon 1:52 AM · last report 10:30 AM');
});

test('a step more than a week old shows its date', () => {
  const data = multiDay();
  data.coords.evidence[0].t = '2026-10-25T23:12:00+00:00';
  const vm = viewModel(data, at(data), PREFS);
  assert.ok(vm.timeline[0].meta.startsWith('Oct 25, 7:12 PM · '), vm.timeline[0].meta);
});

test('in flight (no sun times, no offset): the sky follows solar time at the plane', () => {
  // 19:30 UTC over South Carolina (lon -79.9) is about 14:10 solar time
  const d = fx('in-flight-adsb');
  assert.equal(timeOfDay(at(d), d), 'day');
  assert.equal(vmOf('in-flight-adsb').sky.time, 'day');
  // 04:00 UTC there is night
  assert.equal(timeOfDay(new Date('2026-11-05T04:00:00Z'), d), 'night');
  // Unknown answer: the last known position gives the solar time
  const u = fx('unknown');
  assert.equal(timeOfDay(new Date('2026-11-04T17:00:00Z'), u), 'day');
});
