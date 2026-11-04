"""Regression tests for location_service audit findings #0-#3, #5."""

from __future__ import annotations

import datetime as dt
import threading
from types import SimpleNamespace

import pytest
from dateutil import tz

from app import location_service as loc

UTC = tz.UTC


def _plane(ts):
    return {
        "callsign": "N757AF",
        "lat": 10.0,
        "lon": 20.0,
        "altitude": 0.0,
        "on_ground": True,
        "ts": ts,
        "status": "grounded",
        "tracker_url": "",
    }


def _isolate(monkeypatch, event, plane=None, geocode=None):
    """Stub the sources; *geocode* is the cached candidate for the location."""
    snap = {"states": [plane] if plane else [], "errors": []}
    monkeypatch.setattr(loc, "get_fleet_states", lambda: snap, raising=True)
    monkeypatch.setattr(loc.cal, "get_events", lambda: [event], raising=True)
    monkeypatch.setattr(loc.cal, "get_overnight_base", lambda *a, **k: None)

    async def _no_news():
        return None

    monkeypatch.setattr(loc, "get_latest_location", _no_news, raising=True)
    loc._geocode_store().pop(loc._clean(event["location"]), None)
    if geocode is not None:
        loc._put_candidates(
            event["location"],
            [{"lat": geocode.latitude, "lon": geocode.longitude, "name": "X"}],
        )


async def _coords():
    c = await loc.current_coords()
    return c[0] if isinstance(c, tuple) else c


def _loc(lat=40.0, lon=-75.0):
    return SimpleNamespace(latitude=lat, longitude=lon, address="X")


@pytest.mark.asyncio
async def test_geocode_never_runs_in_request_path(monkeypatch):
    """#0: the request path only reads cached geocodes (never Nominatim)."""

    def _raw(*a, **k):
        raise AssertionError("Nominatim called from current_coords")

    monkeypatch.setattr(loc, "_geocode_raw", _raw)
    ev = {
        "location": "Nowhere Arena",
        "summary": "Rally",
        "dtstart_utc": dt.datetime.now(UTC) - dt.timedelta(hours=1),
    }
    _isolate(monkeypatch, ev)
    c = await _coords()
    assert c["unknown"] is True  # uncached venue: no claim until geocoded


def test_pregeocode_fills_the_cache_for_the_request_path(monkeypatch):
    """#0: the background loop geocodes window venues into the cache."""
    seen = {}

    def _raw(query, **k):
        seen["thread"] = threading.current_thread()
        return [_loc(35.0, -80.0)]

    monkeypatch.setattr(loc, "_geocode_raw", _raw)
    monkeypatch.setattr(loc, "add_geocode_entry", lambda **k: None)
    ev = {
        "location": "Somewhere Arena",
        "summary": "Rally",
        "dtstart_utc": dt.datetime.now(UTC) - dt.timedelta(hours=1),
    }
    monkeypatch.setattr(loc.cal, "get_events", lambda: [ev])
    loc._geocode_store().pop(loc._clean("Somewhere Arena"), None)
    assert loc.pregeocode_events() == 1
    assert loc.cached_candidates("Somewhere Arena")[0]["lat"] == 35.0
    assert loc.pregeocode_events() == 0  # cached now


def test_failed_geocode_is_negative_cached(monkeypatch):
    """#1: an empty result is not retried within the negative TTL."""
    calls = {"n": 0}

    def _raw(*a, **k):
        calls["n"] += 1
        return None

    loc._geocode_cache.clear()
    loc._geocode_neg_cache.clear()
    monkeypatch.setattr(loc, "_geocode_raw", _raw, raising=False)
    monkeypatch.setattr(loc, "add_geocode_entry", lambda **k: None)
    monkeypatch.setattr(loc, "emit_geocode_failure", lambda **k: None)
    assert loc._smart_geocode("Zzyzx Qwerty Hall") is None
    first = calls["n"]
    assert first >= 1
    assert loc._smart_geocode("Zzyzx Qwerty Hall") is None
    assert calls["n"] == first
    # expires after the TTL
    key = loc._clean("Zzyzx Qwerty Hall")
    loc._geocode_neg_cache[key] -= dt.timedelta(seconds=loc.GEOCODE_NEG_TTL_S + 1)
    loc._smart_geocode("Zzyzx Qwerty Hall")
    assert calls["n"] > first
    loc._geocode_neg_cache.clear()


@pytest.mark.asyncio
async def test_future_event_confidence_capped(monkeypatch):
    """#2: an upcoming event never exceeds CAL_BASE_CONF."""
    ev = {
        "location": "The White House",
        "summary": "Meeting",
        "dtstart_utc": dt.datetime.now(UTC) + dt.timedelta(hours=24),
    }
    _isolate(monkeypatch, ev)
    c = await _coords()
    assert c["reason"] == "calendar_alias"
    assert c["confidence"] <= loc.CAL_BASE_CONF


@pytest.mark.asyncio
async def test_grounded_plane_beats_future_event(monkeypatch):
    """#3: a not-yet-started event does not suppress a fresh grounded plane."""
    now = dt.datetime.now(UTC)
    ev = {
        "location": "The White House",
        "summary": "Meeting",
        "dtstart_utc": now + dt.timedelta(hours=5),
    }
    _isolate(monkeypatch, ev, plane=_plane(now - dt.timedelta(minutes=2)))
    c = await _coords()
    assert c["reason"] == "plane_ground"


@pytest.mark.asyncio
async def test_location_geocode_before_summary_alias(monkeypatch):
    """#5: explicit non-alias location is geocoded before summary alias."""
    ev = {
        "location": "Zzyzx Qwerty Airport",
        "summary": "Departs the White House for Zzyzx",
        "dtstart_utc": dt.datetime.now(UTC) - dt.timedelta(minutes=10),
    }
    _isolate(monkeypatch, ev, geocode=_loc(38.9, -77.4))
    c = await _coords()
    assert c["reason"] == "calendar_geocode"

    # An ungeocoded location yields no claim: the summary alias is only used
    # when the location is empty (it brought Bug B back: "departs Salina
    # Regional Airport for Joint Base Andrews" read as JBA)
    _isolate(monkeypatch, ev)
    c = await _coords()
    assert c["unknown"] is True


@pytest.mark.asyncio
async def test_summary_alias_ignores_the_destination(monkeypatch):
    """With an empty location, the summary alias skips "for / en route Y"."""
    ev = {
        "location": "",
        "summary": "The President departs Joint Base Andrews for the White House",
        "dtstart_utc": dt.datetime.now(UTC) - dt.timedelta(minutes=10),
    }
    _isolate(monkeypatch, ev)
    c = await _coords()
    assert c["reason"] == "calendar_summary"
    assert "Joint Base Andrews" in c["name"]
