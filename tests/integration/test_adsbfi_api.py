"""
tests/integration/test_adsbfi_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for adsb.fi API.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_adsbfi_api.py -v
"""

from __future__ import annotations

import httpx
import pytest

from app.constants import USER_AGENT
from app.fleet import FLEET


class TestAdsbFiAPI:
    """Tests for adsb.fi API integration."""

    @pytest.fixture
    def sample_icao(self) -> str:
        """Get a sample ICAO code from fleet."""
        return next(iter(FLEET.values()))["icao"]

    async def test_api_responds_with_valid_json(self, sample_icao: str) -> None:
        """adsb.fi API should respond with valid JSON for aircraft query."""
        url = f"https://api.adsb.fi/v1/aircraft/{sample_icao}"

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        # 404 is expected if aircraft not currently tracked
        assert resp.status_code in (200, 404), f"Unexpected status: {resp.status_code}"

        if resp.status_code == 200:
            data = resp.json()
            # Should have expected structure
            assert isinstance(data, dict), "Response should be a JSON object"

    async def test_404_for_nonexistent_aircraft(self) -> None:
        """adsb.fi should return 404 for non-existent ICAO codes."""
        url = "https://api.adsb.fi/v1/aircraft/000000"

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        # Non-existent ICAO should return 404
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    async def test_response_schema_when_aircraft_found(self, sample_icao: str) -> None:
        """When aircraft found, response should have expected fields."""
        url = f"https://api.adsb.fi/v1/aircraft/{sample_icao}"

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            pytest.skip("Aircraft not currently tracked by adsb.fi")

        data = resp.json()
        aircraft = data.get("aircraft")

        if aircraft:
            # Verify expected fields
            expected_fields = {"lat", "lon", "seen_pos"}
            missing = expected_fields - set(aircraft.keys())
            assert not missing, f"Missing expected fields: {missing}"

            # Verify types
            assert isinstance(aircraft["lat"], (int, float)), "lat should be numeric"
            assert isinstance(aircraft["lon"], (int, float)), "lon should be numeric"
            assert isinstance(aircraft["seen_pos"], (int, float)), "seen_pos should be numeric"

    async def test_multiple_fleet_aircraft(self) -> None:
        """Query multiple fleet aircraft in sequence."""
        headers = {"User-Agent": USER_AGENT}
        found_count = 0
        not_found_count = 0

        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            for callsign, meta in FLEET.items():
                icao = meta["icao"]
                url = f"https://api.adsb.fi/v1/aircraft/{icao}"
                resp = await client.get(url)

                if resp.status_code == 200:
                    found_count += 1
                elif resp.status_code == 404:
                    not_found_count += 1
                else:
                    pytest.fail(f"Unexpected status {resp.status_code} for {callsign}")

        # At least verify we got valid responses for all aircraft
        total = found_count + not_found_count
        assert total == len(FLEET), f"Expected {len(FLEET)} responses, got {total}"
