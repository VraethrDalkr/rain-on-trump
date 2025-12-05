"""tests/test_location_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Regression tests for the **alias-shortcut** branch inside
app.location_service.current_coords().
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import calendar_service as cal
from app import location_service as loc


@pytest.mark.asyncio
async def test_alias_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Calendar event ‘The White House’ → alias coordinates are returned
    (no geocoding, no plane lookup).
    """
    # 1️⃣ Plane: force *no data* so pipeline proceeds to calendar branch
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)

    # 2️⃣ Calendar: fake a pool-call event at the White House.
    now_utc = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        cal,
        "current_event",
        lambda *_: {
            "location": "The White House",
            "summary": "Pool call",
            "dtstart_utc": now_utc,
        },
        raising=True,
    )

    # 3️⃣ Flush memo-cache between tests
    loc._cached.clear()  # type: ignore[attr-defined]

    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # when trace is returned
        coords = coords[0]

    assert "white house" in coords["name"].lower()
    assert coords["lat"] == pytest.approx(38.897676)
    assert coords["lon"] == pytest.approx(-77.036529)


@pytest.mark.asyncio
async def test_in_town_pool_call_time_resolves_to_white_house(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    'In-Town Pool Call Time' in summary (without location) → White House.

    This tests strategy 2b (alias on summary) when the location field is empty.
    'In-Town Pool Call Time' means the press pool must report to the White House.
    """
    # 1️⃣ Plane: force *no data* so pipeline proceeds to calendar branch
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)

    # 2️⃣ Calendar: event with empty location but implicit WH indicator in summary
    now_utc = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        cal,
        "current_event",
        lambda *_: {
            "location": "",  # Empty location field
            "summary": "In-Town Pool Call Time",
            "dtstart_utc": now_utc,
        },
        raising=True,
    )

    # 3️⃣ Flush memo-cache between tests
    loc._cached.clear()  # type: ignore[attr-defined]

    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # when trace is returned
        coords = coords[0]

    assert "white house" in coords["name"].lower()
    assert coords["lat"] == pytest.approx(38.897676)
    assert coords["lon"] == pytest.approx(-77.036529)
    assert coords["reason"] == "calendar_summary"  # via summary, not location


@pytest.mark.asyncio
async def test_overnight_inference_returns_white_house(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    At 11PM after DC event, with DC morning event → returns WH via overnight inference.

    Tests that overnight base inference takes precedence over stale calendar events.
    """
    # 1️⃣ Plane: force *no data* so pipeline proceeds to overnight/calendar branch
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)

    # 2️⃣ Mock overnight base to return WH (simulating DC evening + DC morning pattern)
    monkeypatch.setattr(
        cal,
        "get_overnight_base",
        lambda now=None: {"lat": 38.897676, "lon": -77.036529, "name": "The White House"},
    )

    # 3️⃣ Calendar returns old Ellipse event (would normally win without overnight inference)
    now_utc = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        cal,
        "current_event",
        lambda *_: {
            "location": "The Ellipse",
            "summary": "Christmas Tree Lighting",
            "dtstart_utc": now_utc - dt.timedelta(hours=5),
        },
        raising=True,
    )

    # 4️⃣ Flush memo-cache between tests
    loc._cached.clear()  # type: ignore[attr-defined]

    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # when trace is returned
        coords = coords[0]

    # Should return White House from overnight inference, not Ellipse from calendar
    assert "white house" in coords["name"].lower()
    assert coords["lat"] == pytest.approx(38.897676)
    assert coords["lon"] == pytest.approx(-77.036529)
    assert coords["reason"] == "overnight_dc"
    assert coords["confidence"] == 58
