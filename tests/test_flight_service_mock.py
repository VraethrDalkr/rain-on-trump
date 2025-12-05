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
    Replace *location_service.get_plane_state* with a lambda returning
    ``fake`` and wipe its memo-cache so earlier tests don’t leak state.
    Also stub out _vip_json and get_latest_location to prevent real HTTP.
    """
    # Clear any cached plane_state
    loc._cached.pop("get_plane_state", None)  # type: ignore[attr-defined]
    monkeypatch.setattr(loc, "get_plane_state", lambda: fake, raising=True)

    # Stub out FAA‐TFR JSON so no real HTTP request is ever made:
    async def _no_vip(*_args, **_kwargs) -> list:
        return []

    monkeypatch.setattr(loc, "_vip_json", _no_vip, raising=True)

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
