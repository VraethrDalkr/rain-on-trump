"""
tests/test_location_service_newswire.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration-style test for the GDELT newswire fallback (disabled by default:
``NEWSWIRE_ENABLED``; a dateline claim is timed by the article's seendate).
"""

import datetime as dt

import pytest
from app import location_service as loc


def _isolate(monkeypatch):
    monkeypatch.setattr(
        loc, "get_fleet_states", lambda: {"states": [], "errors": []}, raising=True
    )
    monkeypatch.setattr(loc.cal, "get_events", lambda: [], raising=True)


@pytest.mark.asyncio
async def test_newswire_disabled_by_default(monkeypatch):
    _isolate(monkeypatch)

    async def _never():
        raise AssertionError("newswire must not be queried")

    monkeypatch.setattr(loc, "get_latest_location", _never, raising=True)
    coords = await loc.current_coords()
    assert coords["unknown"] is True


@pytest.mark.asyncio
async def test_newswire_branch(monkeypatch):
    """Everything empty → newswire coords are used."""
    # 1️⃣  higher-priority sources gone
    _isolate(monkeypatch)
    monkeypatch.setattr(loc, "NEWSWIRE_ENABLED", True)

    # 2️⃣  fake GDELT hit, seen 30 min ago
    fake = {
        "lat": 40.0,
        "lon": -75.0,
        "name": "News dateline: Testville",
        "seendate": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30),
    }

    async def _fake_news() -> dict:
        return fake

    monkeypatch.setattr(loc, "get_latest_location", _fake_news, raising=True)

    # 3️⃣  run pipeline
    result = await loc.current_coords(trace=[])
    coords = result[0] if isinstance(result, tuple) else result

    # 4️⃣  assertions
    assert coords["reason"] == "newswire"
    assert coords["confidence"] == 45  # R 0.45 × stay 1.0
    assert coords["lat"] == pytest.approx(40.0)
    assert coords["lon"] == pytest.approx(-75.0)
