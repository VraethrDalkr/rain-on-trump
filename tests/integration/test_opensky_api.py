"""
tests/integration/test_opensky_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for OpenSky Network API.

Note: OpenSky has a 100 req/day limit for anonymous access.
These tests should be run sparingly.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_opensky_api.py -v
"""

from __future__ import annotations

import httpx
import pytest

from app.constants import USER_AGENT
from app.fleet import FLEET


class TestOpenSkyAPI:
    """Tests for OpenSky Network API integration."""

    @pytest.fixture
    def icao_codes(self) -> str:
        """Get comma-separated ICAO codes for fleet aircraft."""
        return ",".join(aircraft["icao"] for aircraft in FLEET.values())

    async def test_api_responds_with_valid_json(self, icao_codes: str) -> None:
        """OpenSky API should respond with valid JSON for fleet query."""
        url = f"https://opensky-network.org/api/states/all?icao24={icao_codes}"

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        # API may return 200 (with or without states) or be rate-limited
        assert resp.status_code in (200, 429, 503), f"Unexpected status: {resp.status_code}"

        if resp.status_code == 200:
            data = resp.json()
            # Should have expected structure
            assert "time" in data, "Response missing 'time' field"
            # states may be null if no aircraft found
            assert "states" in data, "Response missing 'states' field"

    async def test_api_handles_invalid_icao(self) -> None:
        """OpenSky API should handle invalid ICAO code gracefully."""
        url = "https://opensky-network.org/api/states/all?icao24=invalid123"

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        # Should still return 200 with empty states
        if resp.status_code == 200:
            data = resp.json()
            states = data.get("states")
            assert states is None or states == [], "Invalid ICAO should return no states"

    async def test_api_timeout_handling(self) -> None:
        """API should respect timeout settings."""
        url = "https://opensky-network.org/api/states/all"

        # Use very short timeout to test timeout handling
        async with httpx.AsyncClient(
            timeout=0.001, headers={"User-Agent": USER_AGENT}
        ) as client:
            with pytest.raises((httpx.TimeoutException, httpx.ConnectError)):
                await client.get(url)

    async def test_response_schema_when_aircraft_found(self, icao_codes: str) -> None:
        """When aircraft are found, response should have expected schema."""
        url = f"https://opensky-network.org/api/states/all?icao24={icao_codes}"

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            pytest.skip("OpenSky API not available or rate limited")

        data = resp.json()
        states = data.get("states")

        if states:
            # Verify state vector structure (17 elements)
            state = states[0]
            assert len(state) >= 9, f"State vector too short: {len(state)} elements"

            # Check key fields exist
            icao24 = state[0]
            callsign = state[1]
            assert isinstance(icao24, str), "ICAO24 should be string"
            assert callsign is None or isinstance(callsign, str), "Callsign should be string or None"
