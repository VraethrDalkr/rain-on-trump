"""
tests/test_location_freshness.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Verify the “plane vs calendar” *freshness* rule:

* If the grounded jet’s timestamp is **newer** than the calendar event,
  plane wins (`reason == "plane_ground"`).

* If the jet landed **before** the calendar event, pipeline falls through
  to the calendar branch (`reason` starts with `"calendar_"`).
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
        "lat": 10.0,
        "lon": 20.0,
        "altitude": 0.0,
        "on_ground": True,
        "ts": ts,
        "status": "grounded",
        "tracker_url": "",
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

    # 1️⃣  Plane state stub
    monkeypatch.setattr(
        loc, "get_plane_state", lambda: _stub_plane(plane_ts), raising=True
    )

    # 2️⃣  Calendar event stub
    monkeypatch.setattr(
        loc.cal,
        "current_event",
        lambda: {
            "location": "Bedminster, NJ",
            "summary": "Private fundraiser",
            "dtstart_utc": event_ts,
        },
        raising=True,
    )

    # 3️⃣  Empty VIP/TFR + last-known + newswire to isolate branch
    async def _no_vip(*_a, **_k):
        return []

    monkeypatch.setattr(loc, "_vip_json", _no_vip, raising=True)
    monkeypatch.setattr(loc, "load_last", lambda: None, raising=True)

    async def _no_news():
        return None

    monkeypatch.setattr(loc, "get_latest_location", _no_news, raising=True)

    coords = await loc.current_coords()
    if isinstance(coords, tuple):
        coords = coords[0]

    if newer_expected:
        assert coords["reason"] == "plane_ground"
    else:
        assert coords["reason"].startswith("calendar_")
