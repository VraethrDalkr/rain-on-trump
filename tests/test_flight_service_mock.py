"""
tests/test_flight_service_mock.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Validate the *airborne* and *grounded* branches inside
app.location_service.current_coords().

Key change → we now expect the tracker URL under ``source_url`` instead of
the removed ``tracker`` key.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import location_service as loc
from app.flight_service import PlaneState


# --------------------------------------------------------------------------- #
# Helper                                                                      #
# --------------------------------------------------------------------------- #
def _patch_plane(monkeypatch: pytest.MonkeyPatch, fake: PlaneState) -> None:
    """
    Replace *location_service.get_fleet_states* with a lambda returning
    ``fake`` as the only fleet state, and stub the calendar and the
    newswire so no real HTTP request is made.
    """
    monkeypatch.setattr(
        loc, "get_fleet_states", lambda: {"states": [fake], "errors": []}
    )
    monkeypatch.setattr(loc.cal, "get_events", lambda: [])

    # Stub out the GDELT newswire (just in case we fall through)
    async def _no_news() -> dict | None:
        return None

    monkeypatch.setattr(loc, "get_latest_location", _no_news, raising=True)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_current_coords_in_flight(monkeypatch):
    """
    Simulate airborne Trump jet → `in_flight` True plus tracker URL under
    ``source_url``.  Also ensures no “coroutine was never awaited” warnings.
    """
    fake_state: PlaneState = {
        "callsign": "N757AF",
        "lat": 38.897676,
        "lon": -77.036529,
        "altitude": 10_000.0,
        "on_ground": False,
        "ts": dt.datetime.now(dt.timezone.utc),
        "status": "airborne",
        "tracker_url": "https://globe.adsbexchange.com/?icao=N757AF",
    }
    _patch_plane(monkeypatch, fake_state)

    result, _trace = await loc.current_coords(trace=[])

    assert result["in_flight"] is True
    assert "In flight" in result["name"]
    assert result["source_url"] == fake_state["tracker_url"]


@pytest.mark.asyncio
async def test_current_coords_grounded(monkeypatch):
    """
    Simulate grounded jet → coordinates match the fake position.  Also
    ensures we never launch an un-awaited coroutine.
    """
    fake_state: PlaneState = {
        "callsign": "N757AF",
        "lat": 26.6758,
        "lon": -80.0364,
        "altitude": 0.0,
        "on_ground": True,
        "ts": dt.datetime.now(dt.timezone.utc),
        "status": "grounded",
        "tracker_url": "https://globe.adsbexchange.com/?icao=N757AF",
    }
    _patch_plane(monkeypatch, fake_state)

    result, _trace = await loc.current_coords(trace=[])

    # For a grounded jet, `in_flight` is omitted (or None)
    assert result.get("in_flight") is None
    assert pytest.approx(26.6758) == result["lat"]
    assert pytest.approx(-80.0364) == result["lon"]
    assert "parked" in result["name"]
