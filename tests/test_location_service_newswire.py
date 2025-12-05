"""
tests/test_location_service_newswire.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration-style test for the GDELT newswire fallback.
"""

import pytest
from app import location_service as loc


@pytest.mark.asyncio
async def test_newswire_branch(monkeypatch):
    """Everything empty → newswire coords are used."""
    # 1️⃣  higher-priority sources gone
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)
    monkeypatch.setattr(loc.cal, "current_event", lambda: None, raising=True)

    async def _no_vip(*_args, **_kwargs) -> list:
        return []

    monkeypatch.setattr(loc, "_vip_json", _no_vip, raising=True)
    monkeypatch.setattr(loc, "load_last", lambda: None, raising=True)

    # 2️⃣  fake GDELT hit
    fake = {"lat": 40.0, "lon": -75.0, "name": "News dateline: Testville"}

    async def _fake_news() -> dict:
        return fake

    monkeypatch.setattr(loc, "get_latest_location", _fake_news, raising=True)

    # 3️⃣  run pipeline
    result = await loc.current_coords(trace=[])
    coords = result[0] if isinstance(result, tuple) else result

    # 4️⃣  assertions
    assert coords["reason"] == "newswire"
    assert coords["confidence"] == 35
    assert coords["lat"] == pytest.approx(40.0)
    assert coords["lon"] == pytest.approx(-75.0)
