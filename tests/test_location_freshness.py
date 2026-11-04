"""
tests/test_location_freshness.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Verify the “plane vs calendar” *freshness* rule:

* If the grounded jet’s timestamp is **newer** than the calendar event,
  plane wins (`reason == "plane_ground"`).

* If the jet landed **before** the calendar event, pipeline falls through
  to the calendar branch (`reason` starts with `"calendar_"`).

The jet flies as Air Force One (corroborated) and is parked near the venue,
as an uncorroborated or far-away grounded jet never outranks the calendar.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict

import pytest
from dateutil import tz

from app import location_service as loc

UTC = tz.UTC


def _stub_plane(ts: dt.datetime) -> Dict[str, Any]:
    """Return minimal grounded PlaneState for stubbing."""
    return {
        "callsign": "N757AF",
        "lat": 40.7994,  # Morristown Municipal Airport, ~25 km from the venue
        "lon": -74.4149,
        "altitude": 0.0,
        "on_ground": True,
        "ts": ts,
        "status": "grounded",
        "tracker_url": "",
        "flight_id": "AF1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plane_delta_h,newer_expected",
    [
        (-2, False),  # plane 2 h older than event → calendar wins
        (+1, True),  # plane 1 h newer than event → plane wins
    ],
)
async def test_freshness_rule(
    monkeypatch: pytest.MonkeyPatch, plane_delta_h: int, newer_expected: bool
) -> None:
    """Parametrised check for the freshness comparator."""
    now = dt.datetime.now(UTC)
    plane_ts = now + dt.timedelta(hours=plane_delta_h)
    event_ts = now

    # 1️⃣  Plane state stub (a live state stamped in the future is clamped)
    snap = {"states": [_stub_plane(plane_ts)], "errors": []}
    monkeypatch.setattr(loc, "get_fleet_states", lambda: snap, raising=True)

    # 2️⃣  Calendar event stub
    event = {
        "location": "Bedminster, NJ",
        "summary": "Private fundraiser",
        "dtstart_utc": event_ts,
    }
    monkeypatch.setattr(loc.cal, "get_events", lambda: [event], raising=True)

    # 3️⃣  "Bedminster, NJ" has no place alias → a cached geocode
    loc._put_candidates(
        "Bedminster, NJ", [{"lat": 40.68, "lon": -74.65, "name": "Bedminster, NJ"}]
    )

    coords = await loc.current_coords()
    if isinstance(coords, tuple):
        coords = coords[0]

    if newer_expected:
        assert coords["reason"] == "plane_ground"
    else:
        assert coords["reason"].startswith("calendar_")
