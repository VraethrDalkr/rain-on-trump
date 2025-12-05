"""
tests/integration/test_weather_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for Open-Meteo Weather API.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_weather_api.py -v
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.constants import USER_AGENT


class TestOpenMeteoAPI:
    """Tests for Open-Meteo Weather API integration."""

    @pytest.fixture
    def washington_dc(self) -> tuple[float, float]:
        """Coordinates for Washington, DC (known location)."""
        return (38.8977, -77.0365)

    @pytest.fixture
    def palm_beach(self) -> tuple[float, float]:
        """Coordinates for Palm Beach, FL (Mar-a-Lago)."""
        return (26.6765, -80.0369)

    async def test_api_responds_with_valid_json(
        self, washington_dc: tuple[float, float]
    ) -> None:
        """Open-Meteo API should respond with valid JSON."""
        lat, lon = washington_dc
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=rain,snowfall"
            "&timezone=UTC"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"

        data = resp.json()
        assert "hourly" in data, "Response missing 'hourly' field"
        assert "time" in data["hourly"], "Response missing 'hourly.time'"
        assert "rain" in data["hourly"], "Response missing 'hourly.rain'"
        assert "snowfall" in data["hourly"], "Response missing 'hourly.snowfall'"

    async def test_response_contains_current_hour(
        self, washington_dc: tuple[float, float]
    ) -> None:
        """Response should contain data for current hour."""
        lat, lon = washington_dc
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=rain,snowfall"
            "&timezone=UTC"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200
        data = resp.json()

        # Get current hour in UTC
        now_hour = dt.datetime.now(dt.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

        times = data["hourly"]["time"]
        assert lookup in times, f"Current hour {lookup} not in response"

    async def test_precipitation_values_are_numeric(
        self, palm_beach: tuple[float, float]
    ) -> None:
        """Precipitation values should be numeric (float)."""
        lat, lon = palm_beach
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=rain,snowfall"
            "&timezone=UTC"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200
        data = resp.json()

        rain_values = data["hourly"]["rain"]
        snow_values = data["hourly"]["snowfall"]

        # Check first few values are numeric
        for i in range(min(5, len(rain_values))):
            assert isinstance(rain_values[i], (int, float)), f"Rain value {i} not numeric"
            assert isinstance(snow_values[i], (int, float)), f"Snow value {i} not numeric"
            assert rain_values[i] >= 0, f"Rain value {i} is negative"
            assert snow_values[i] >= 0, f"Snow value {i} is negative"

    async def test_invalid_coordinates_handled(self) -> None:
        """API should handle invalid coordinates gracefully."""
        # Latitude out of range
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=999&longitude=-77"
            "&hourly=rain"
            "&timezone=UTC"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        # Open-Meteo returns 400 for invalid coordinates
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"

    async def test_timeout_handling(self) -> None:
        """API should respect timeout settings."""
        url = "https://api.open-meteo.com/v1/forecast?latitude=38&longitude=-77&hourly=rain"

        # Use very short timeout to test timeout handling
        async with httpx.AsyncClient(
            timeout=0.001, headers={"User-Agent": USER_AGENT}
        ) as client:
            with pytest.raises((httpx.TimeoutException, httpx.ConnectError)):
                await client.get(url)
