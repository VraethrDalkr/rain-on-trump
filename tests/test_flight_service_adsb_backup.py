"""
tests/test_flight_service_adsb_backup.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
`flight_service.get_plane_state()`: feed order, fallback and confidence.
Feeds are stubbed at the whole-fleet fetchers (``get_fleet_adsb`` / ``_adsblol``).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import flight_service as fs
from app.adsbfi_service import PlaneState

UTC = dt.timezone.utc


def _state(age_s: float = 0, status: str = "airborne", src: str = "x") -> PlaneState:
    return {
        "callsign": "N757AF",
        "lat": 39.0,
        "lon": -75.0,
        "altitude": 9000.0,
        "on_ground": status == "grounded",
        "ts": dt.datetime.now(UTC) - dt.timedelta(seconds=age_s),
        "status": status,  # type: ignore[typeddict-item]
        "tracker_url": "https://globe.adsbexchange.com/?icao=aa3410",
        "source": src,
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    fs._cache.clear()


def test_adsbfi_wins_and_gets_confidence(monkeypatch):
    monkeypatch.setattr(fs, "get_fleet_adsb", lambda: [_state(src="adsb.fi")])
    monkeypatch.setattr(fs, "get_fleet_adsblol", lambda: pytest.fail("not needed"))

    result = fs.get_plane_state()
    assert result["source"] == "adsb.fi"
    assert result["confidence"] == fs.AIRBORNE_CONFIDENCE_BASE


def test_falls_back_to_adsblol_when_adsbfi_sees_nothing(monkeypatch):
    monkeypatch.setattr(fs, "get_fleet_adsb", lambda: [])
    monkeypatch.setattr(fs, "get_fleet_adsblol", lambda: [_state(src="adsb.lol")])

    result = fs.get_plane_state()
    assert result["source"] == "adsb.lol"
    assert "errors" not in result


def test_error_is_reported_alongside_fallback_state(monkeypatch):
    def boom():
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(fs, "get_fleet_adsb", boom)
    monkeypatch.setattr(fs, "get_fleet_adsblol", lambda: [_state(src="adsb.lol")])

    result = fs.get_plane_state()
    assert result["source"] == "adsb.lol"
    assert result["errors"] == ["adsb.fi error: HTTP 503"]


def test_stale_snapshot_is_dropped(monkeypatch):
    """An airborne fix older than the 10-min window is not a usable state."""
    monkeypatch.setattr(fs, "get_fleet_adsb", lambda: [_state(age_s=900)])
    monkeypatch.setattr(fs, "get_fleet_adsblol", lambda: [])

    assert fs.get_plane_state() == {"state": None, "errors": []}


def test_fleet_states_lists_every_aircraft(monkeypatch):
    a = {**_state(src="adsb.fi"), "callsign": "82-8000"}
    b = {**_state(age_s=60, src="adsb.fi"), "callsign": "92-9000"}
    monkeypatch.setattr(fs, "get_fleet_adsb", lambda: [a, b])

    snap = fs.get_fleet_states()
    assert [s["callsign"] for s in snap["states"]] == ["82-8000", "92-9000"]
    assert snap["errors"] == []


def test_plane_state_prefers_primary_airframe(monkeypatch):
    from app import arrival_cache

    newest = {**_state(src="adsb.fi"), "callsign": "92-9000"}
    primary = {**_state(age_s=60, src="adsb.fi"), "callsign": "N7478D"}
    monkeypatch.setattr(fs, "get_fleet_adsb", lambda: [newest, primary])

    assert fs.get_plane_state()["callsign"] == "92-9000"
    fs._cache.clear()
    arrival_cache.set_primary_airframe("vc25b-bridge")
    assert fs.get_plane_state()["callsign"] == "N7478D"
