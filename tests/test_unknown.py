"""
tests/test_unknown.py
~~~~~~~~~~~~~~~~~~~~~
All sources empty and no cached arrival → pipeline returns “unknown”.
"""

from __future__ import annotations

import pytest
from app import location_service as loc


@pytest.mark.asyncio
async def test_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expect `unknown is True`, confidence 0, reason 'unknown'."""
    monkeypatch.setattr(
        loc, "get_fleet_states", lambda: {"states": [], "errors": []}, raising=True
    )
    monkeypatch.setattr(loc.cal, "get_events", lambda: [], raising=True)

    coords = await loc.current_coords()
    if isinstance(coords, tuple):
        coords = coords[0]

    assert coords["unknown"] is True
    assert coords["confidence"] == 0
    assert coords["reason"] == "unknown"
