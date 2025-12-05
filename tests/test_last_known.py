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
    # 1️⃣  seed cache with yesterday's arrival
    ts_yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    cache.save(40.0, -75.0, ts=ts_yesterday)

    # 2️⃣  stub out all live sources
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)
    monkeypatch.setattr(loc.cal, "current_event", lambda: None, raising=True)

    async def _no_vip(*_args, **_kwargs) -> list:  # accepts any sig
        return []

    monkeypatch.setattr(loc, "_vip_json", _no_vip, raising=True)

    # 3️⃣  run pipeline
    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # trace variant
        coords = coords[0]

    # 4️⃣  assertions
    assert coords["reason"] == "last_known"
    # 1 day old → confidence = 30 - (1 * 3) = 27
    assert coords["confidence"] == 27
    assert coords["lat"] == pytest.approx(40.0)
    assert coords["lon"] == pytest.approx(-75.0)
