"""
tests/test_flight_service_adsb_backup.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Ensure `flight_service.get_plane_state()` falls back to adsb.fi when
OpenSky has no data.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import flight_service as fs
from app.adsbfi_service import PlaneState


@pytest.mark.asyncio
async def test_adsb_backup(monkeypatch):
    """OpenSky → None, adsb.fi → fresh state → wrapper returns it."""

    # Clear the memoization cache first
    fs._cache.clear()

    # ① stub OpenSky internal helper to force *None*
    monkeypatch.setattr(fs, "_opensky_state", lambda: None, raising=True)

    # ② fake adsb.fi response
    fake: PlaneState = {
        "callsign": "N757AF",
        "lat": 39.0,
        "lon": -75.0,
        "altitude": 9000.0,
        "on_ground": False,
        "ts": dt.datetime.now(dt.timezone.utc),
        "status": "airborne",
        "tracker_url": "https://globe.adsbexchange.com/?icao=N757AF",
    }
    monkeypatch.setattr(fs, "get_plane_state_adsb", lambda: fake, raising=True)

    result = fs.get_plane_state()
    assert result == fake  # exact dict returned
