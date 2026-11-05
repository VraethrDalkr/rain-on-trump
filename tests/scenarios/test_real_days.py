"""
tests/scenarios/test_real_days.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Replay real Factba.se schedule days (fixtures extracted from the production
feed) hour by hour, with no ADS-B data and a fixed geocoding table.

Ground truth for each hourly sample is the venue of the latest timed, located
item before that time, except that:

* between a departure for a destination more than 150 km away and the
  matching "arrives" item (or the ETA at 800 km/h + 30 min when none is
  listed) he is in flight;
* 45 minutes after a shorter "departs X for/en route to Y" with no later item
  listed, he is at Y.

The answer must be within 60 km of the ground truth (or in flight).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import calendar_service as cal
from app.location_fusion import DEPARTS_RE, clean, haversine_km
from app.place_aliases import find_alias

from .harness import NYC, World, describe, et

FIXTURES = Path(__file__).parent / "fixtures"
#: Known feed errors, left out of the ground truth: a duplicate "arrives at
#: McGhee Tyson Airport" stamped 02:28 ET while he was at the White House (the
#: fusion flags it as a suspicious geocode and keeps the White House).
BOGUS_ITEMS = {("2026-09-26T02:28", "arrives at McGhee Tyson Airport")}
RADIUS_KM = 60.0
LONG_LEG_KM = 150.0

#: Geocoder stub: venues of the fixture days that have no place alias
GEO: dict[str, tuple[float, float, str]] = {
    # Bedminster weekend
    "garden city, new york": (40.7268, -73.6343, "us"),
    "david s. mack center for training and intelligence, garden city, ny": (
        40.7300,
        -73.6200,
        "us",
    ),
    "bedminster, new jersey": (40.6807, -74.6454, "us"),
    "mitchel county park, uniondale, ny": (40.7260, -73.5900, "us"),
    # G7
    "geneva airport": (46.2381, 6.1090, "ch"),
    "hôtel royal, evian-les-baines, france": (46.3985, 6.5980, "fr"),
    "versailles, france": (48.8049, 2.1204, "fr"),
    "palace of versailles, france": (48.8049, 2.1204, "fr"),
    "paris orly airport": (48.7262, 2.3652, "fr"),
    # Knoxville
    "the national archives": (38.8928, -77.0230, "us"),
    "national archives": (38.8928, -77.0230, "us"),
    "knoxville, tennessee": (35.9606, -83.9207, "us"),
    "neyland stadium, knoxville, tn": (35.9550, -83.9250, "us"),
    "mcghee tyson airport, alcoa, tn": (35.8110, -83.9940, "us"),
    "medinah, illinois": (41.9785, -88.0420, "us"),
    "medinah country club, medinah, il": (41.9710, -88.0500, "us"),
    "o'hare international airport, chicago": (41.9742, -87.9073, "us"),
    # LA fires, Las Vegas
    "asheville regional airport": (35.4362, -82.5418, "us"),
    "asheville regional airport, fletcher, nc": (35.4362, -82.5418, "us"),
    "swannanoa, nc": (35.6018, -82.3990, "us"),
    "los angeles international airport": (33.9416, -118.4085, "us"),
    "pacific palisades, ca": (34.0481, -118.5265, "us"),
    "circa resort and casino": (36.1717, -115.1458, "us"),
    "circa resort and casino, las vegas": (36.1717, -115.1458, "us"),
    "miami": (25.7617, -80.1918, "us"),
    # Mar-a-Lago weekend
    "miami, florida": (25.7617, -80.1918, "us"),
    "palm beach, florida": (26.7056, -80.0364, "us"),
    "miami beach": (25.7907, -80.1300, "us"),
    "faena hotel & forum, miami beach, fl": (25.8121, -80.1226, "us"),
    # Week of 2026-10-26
    "corinth, mississippi": (34.9343, -88.5223, "us"),
    "crossroads arena, corinth, ms": (34.9290, -88.5270, "us"),
    "frederick w. smith international airport, memphis, tn": (
        35.0424,
        -89.9767,
        "us",
    ),
    "roscoe turner airport, corinth, ms": (34.9150, -88.6035, "us"),
    "corpus christi, texas": (27.8006, -97.3964, "us"),
    "port of corpus christi, texas": (27.8130, -97.4000, "us"),
    "lubbock, texas": (33.5779, -101.8552, "us"),
    "lubbock memorial civic center, texas": (33.5900, -101.8460, "us"),
    "lubbock preston smith international airport, lubbock, tx": (
        33.6636,
        -101.8228,
        "us",
    ),
    "chillicothe, ohio": (39.3331, -82.9824, "us"),
    "shoemaker center, chillicothe, oh": (39.3400, -82.9830, "us"),
    "rickenbacker international airport, columbus, oh": (39.8138, -82.9278, "us"),
    "yoctangee park, chillicothe, oh": (39.3370, -82.9930, "us"),
    "rock hill, south carolina": (34.9249, -81.0251, "us"),
    "rock hill sports & event center, rock hill, sc": (34.9330, -81.0170, "us"),
    "charlotte douglas international airport": (35.2144, -80.9473, "us"),
    "daytona beach, florida": (29.2108, -81.0228, "us"),
    "ocean center, daytona beach, fl": (29.2270, -81.0090, "us"),
    "daytona beach international airport": (29.1799, -81.0581, "us"),
    "dubuque, iowa": (42.5006, -90.6646, "us"),
    "five flags center, dubuque, ia": (42.5000, -90.6680, "us"),
    "salina, kansas": (38.8403, -97.6114, "us"),
    "salina regional airport, ks": (38.7910, -97.6522, "us"),
    "dubuque regional airport, ia": (42.4020, -90.7095, "us"),
    "the lincoln memorial reflecting pool": (38.8893, -77.0450, "us"),
    "lincoln memorial reflecting pool": (38.8893, -77.0450, "us"),
}


def _load(name: str, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Normalise a fixture with the production feed parser."""
    items = json.loads((FIXTURES / name).read_text())
    resp = SimpleNamespace(text=json.dumps(items), raise_for_status=lambda: None)
    monkeypatch.setattr(cal.httpx, "get", lambda *a, **k: resp)
    return cal._download_events.__wrapped__()  # type: ignore[attr-defined]


def _resolve(text: str) -> tuple[float, float] | None:
    key = clean(text or "")
    if not key:
        return None
    alias = find_alias(key)
    if alias:
        return float(alias["lat"]), float(alias["lon"])
    hit = GEO.get(key)
    return (hit[0], hit[1]) if hit else None


def _bogus(ev: dict) -> bool:
    stamp = ev["dtstart_utc"].astimezone(NYC).strftime("%Y-%m-%dT%H:%M")
    return any(stamp == t and text in ev["summary"] for t, text in BOGUS_ITEMS)


def ground_truth(events: list[dict], when: dt.datetime) -> tuple[str, object]:
    """("flight", dest) or ("at", (lat, lon)) per the module docstring."""
    timed = [
        e for e in events if e["has_time"] and _resolve(e["location"]) and not _bogus(e)
    ]
    timed.sort(key=lambda e: e["dtstart_utc"])
    for i, ev in enumerate(timed):
        m = DEPARTS_RE.search(ev["summary"])
        if not m or ev["dtstart_utc"] > when:
            continue
        origin, dest = _resolve(ev["location"]), _resolve(m.group("dest"))
        if not (origin and dest) or haversine_km(*origin, *dest) <= LONG_LEG_KM:
            continue
        arrival = next(
            (
                e
                for e in timed[i + 1 :]
                if "arrive" in e["summary"].lower()
                and haversine_km(*_resolve(e["location"]), *dest) <= RADIUS_KM
            ),
            None,
        )
        eta = dt.timedelta(hours=haversine_km(*origin, *dest) / 800 + 0.5)
        end = arrival["dtstart_utc"] if arrival else ev["dtstart_utc"] + eta
        if when < end:
            return "flight", dest
    before = [e for e in timed if e["dtstart_utc"] <= when]
    if not before:
        return "none", None
    last = before[-1]
    m = DEPARTS_RE.search(last["summary"])
    if m and when - last["dtstart_utc"] >= dt.timedelta(minutes=45):
        dest = _resolve(m.group("dest"))
        if dest:
            return "at", dest
    return "at", _resolve(last["location"])


def output_errors(c: dict) -> list[str]:
    """Contract violations in one fusion output (see the location_fusion output)."""
    errs = []
    if c.get("unknown") and ("lat" in c or "lon" in c):
        errs.append("unknown with lat/lon")
    if not 0 <= c.get("confidence", 0) <= 95:
        errs.append(f"confidence {c.get('confidence')}")
    ev = c.get("evidence")
    if ev is not None:
        ts = [e["t"] for e in ev]
        if len(ev) > 4:
            errs.append("more than 4 evidence steps")
        if ts != sorted(ts):
            errs.append(f"evidence not oldest first: {ts}")
        if [e["current"] for e in ev].count(True) != 1 or not ev[-1]["current"]:
            errs.append("evidence: the pick must be the one current step, last")
        if ev[-1].get("since", "") > ev[-1]["t"]:
            errs.append("evidence: since after t")
        keys = [(e["kind"], e["place"]) for e in ev]
        if len(set(keys)) != len(keys):
            errs.append(f"evidence: repeated (kind, place) {keys}")
    return errs


def _replay(world: World, monkeypatch, fixture: str, first: str, last: str) -> None:
    events = _load(fixture, monkeypatch)
    world.events[:] = events
    for key, (lat, lon, cc) in GEO.items():
        world.geo[key] = [(lat, lon, cc)]
    t = et(first)
    end = et(last)
    misses: list[str] = []
    while t <= end:
        local = t.astimezone(NYC).strftime("%Y-%m-%dT%H:%M")
        coords = world.at(local)
        kind, truth = ground_truth(events, t)
        if kind == "flight":
            ok = coords.get("in_flight") is True
        elif kind == "at":
            ok = (
                not coords.get("in_flight")
                and "lat" in coords
                and haversine_km(coords["lat"], coords["lon"], *truth) <= RADIUS_KM
            )
        else:
            ok = True
        if not ok:
            misses.append(f"{local} ET expected {kind} {truth}: {describe(coords)}")
        if errs := output_errors(coords):
            misses.append(f"{local} ET output contract: {errs}: {describe(coords)}")
        t += dt.timedelta(hours=1)
    assert not misses, "\n".join(misses)


def test_g7_trip(world: World, monkeypatch) -> None:
    """DC → Geneva → Evian → Versailles → home (foreign venues)."""
    _replay(
        world, monkeypatch, "g7_2026_06.json", "2026-06-14T00:00", "2026-06-17T23:00"
    )


def test_knoxville_day_trip(world: World, monkeypatch) -> None:
    _replay(
        world,
        monkeypatch,
        "knoxville_2026_09_26.json",
        "2026-09-26T00:00",
        "2026-09-26T23:00",
    )


def test_la_fires_then_las_vegas(world: World, monkeypatch) -> None:
    _replay(
        world,
        monkeypatch,
        "la_vegas_2025_01.json",
        "2025-01-24T00:00",
        "2025-01-25T23:00",
    )


def test_quiet_mar_a_lago_weekend(world: World, monkeypatch) -> None:
    _replay(
        world,
        monkeypatch,
        "mar_a_lago_2026_03.json",
        "2026-03-28T00:00",
        "2026-03-29T23:00",
    )


def test_bedminster_weekend(world: World, monkeypatch) -> None:
    _replay(
        world,
        monkeypatch,
        "bedminster_2026_08.json",
        "2026-08-14T12:00",
        "2026-08-16T23:00",
    )


def test_campaign_week(world: World, monkeypatch) -> None:
    """2026-10-28 → 11-04, the week covered by production debug snapshots."""
    _replay(
        world,
        monkeypatch,
        "week_2026_10_26.json",
        "2026-10-28T00:00",
        "2026-11-04T08:00",
    )


def test_chillicothe_rally_is_not_overnight_dc(world: World, monkeypatch) -> None:
    """Bug A (2026-10-28): the old code said overnight_dc at 21:03 ET while he
    was at (or leaving) the Chillicothe rally."""
    world.events[:] = _load("week_2026_10_26.json", monkeypatch)
    for key, (lat, lon, cc) in GEO.items():
        world.geo[key] = [(lat, lon, cc)]
    c = world.at("2026-10-28T21:03")
    assert not c["reason"].startswith("overnight"), describe(c)
    assert haversine_km(c["lat"], c["lon"], 39.3400, -82.9830) <= RADIUS_KM


def test_salina_departure_is_in_flight(world: World, monkeypatch) -> None:
    """Bug B (2026-11-01): "departs Salina Regional Airport, KS for Joint Base
    Andrews" was reported as JBA at once (calendar_summary), ~2.5 h early."""
    world.events[:] = _load("week_2026_10_26.json", monkeypatch)
    for key, (lat, lon, cc) in GEO.items():
        world.geo[key] = [(lat, lon, cc)]
    for t in ("2026-11-01T23:45", "2026-11-02T01:00"):
        c = world.at(t)
        assert c.get("in_flight") is True and c["reason"] == "en_route", describe(c)
        assert c["name"] == "In flight to Joint Base Andrews, MD", describe(c)
    for t in ("2026-11-02T01:35", "2026-11-02T03:00"):
        c = world.at(t)
        assert not c.get("in_flight"), describe(c)
        assert haversine_km(c["lat"], c["lon"], 38.8977, -77.0365) <= 30, describe(c)


def test_salina_departure_without_geocode_is_in_flight(
    world: World, monkeypatch
) -> None:
    """Bug B again, via the summary alias: with "Salina Regional Airport, KS"
    not geocoded yet, the summary "… for Joint Base Andrews" must not place
    him at JBA (the summary alias is only used for empty locations, and never
    on the destination part)."""
    world.events[:] = _load("week_2026_10_26.json", monkeypatch)
    for key, (lat, lon, cc) in GEO.items():
        if "salina" not in key:
            world.geo[key] = [(lat, lon, cc)]
    for t in ("2026-11-01T23:10", "2026-11-01T23:45", "2026-11-02T00:30"):
        c = world.at(t)
        # A departure from an airport that can't be placed yet is still a
        # flight to the (resolvable) destination, until the listed arrival
        assert c.get("in_flight") is True and c["reason"] == "en_route", describe(c)
