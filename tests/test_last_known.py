"""
tests/test_last_known.py
~~~~~~~~~~~~~~~~~~~~~~~~
Verify the pipeline falls back to the *last-known arrival* when every
live source is missing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app import arrival_cache as cache
from app import location_service as loc


@pytest.mark.asyncio
async def test_last_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expect `reason == "last_known"` and confidence with decay."""
    # 1️⃣  seed cache with a 4-day-old arrival (past the 72 h landing anchor)
    ts_old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)
    cache.save(40.0, -75.0, ts=ts_old)

    # 2️⃣  stub out all live sources
    monkeypatch.setattr(
        loc, "get_fleet_states", lambda: {"states": [], "errors": []}, raising=True
    )
    monkeypatch.setattr(loc.cal, "get_events", lambda: [], raising=True)

    # 3️⃣  run pipeline
    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # trace variant
        coords = coords[0]

    # 4️⃣  assertions
    assert coords["reason"] == "last_known"
    assert coords["stale"] is True
    # 4 days old, unattributed jet (R 0.4) × stay(96 h) 0.4025 → 16
    assert coords["confidence"] == 16
    assert coords["lat"] == pytest.approx(40.0)
    assert coords["lon"] == pytest.approx(-75.0)
