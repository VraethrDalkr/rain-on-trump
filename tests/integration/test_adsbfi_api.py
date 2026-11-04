"""
tests/integration/test_adsbfi_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for the adsb.fi open-data v2 API.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_adsbfi_api.py -v
"""

from __future__ import annotations

import httpx
import pytest

from app import adsbfi_service as svc
from app.constants import USER_AGENT
from app.fleet import FLEET


def _get(url: str) -> httpx.Response:
    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        return client.get(url)


class TestAdsbFiAPI:
    """Tests for adsb.fi API integration."""

    def test_fleet_query_returns_v2_shape(self) -> None:
        """The fleet query returns the ADSBx-v2 shape: an 'ac' list and 'now' (ms)."""
        url = svc.ADSBFI_URL.format(hexes=",".join(m["icao"] for m in FLEET.values()))
        resp = _get(url)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
        data = resp.json()
        assert isinstance(data.get("ac"), list), f"No 'ac' list: {data}"
        assert isinstance(data.get("now"), (int, float))

    def test_military_traffic_is_included(self) -> None:
        """The /v2/mil feed lists military aircraft (proves no military filter)."""
        resp = _get("https://opendata.adsb.fi/api/v2/mil")

        assert resp.status_code == 200
        assert resp.json().get("ac"), "No military aircraft visible right now"

    def test_visible_fleet_aircraft_parse(self) -> None:
        """Any fleet aircraft currently visible must parse without errors."""
        url = svc.ADSBFI_URL.format(hexes=",".join(m["icao"] for m in FLEET.values()))
        aircraft = svc._aircraft_list(_get(url).json())
        if not aircraft:
            pytest.skip("No fleet aircraft visible right now")

        for ac in aircraft:
            # seen_pos must be relative seconds (readsb style), not epoch time
            assert isinstance(ac.get("seen_pos", 0), (int, float))
            assert ac.get("seen_pos", 0) < 1e9
            print(
                f"{ac.get('hex')} flight={ac.get('flight')!r} alt={ac.get('alt_baro')}"
            )
