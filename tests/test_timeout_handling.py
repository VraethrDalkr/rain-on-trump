"""
tests/test_timeout_handling.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test timeout and error handling for external API calls.

These tests verify that the application handles network errors
gracefully without crashing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


class TestWeatherApiTimeouts:
    """Tests for weather API timeout and error handling.

    DOCUMENTATION: The weather service currently does NOT catch timeout or
    connection exceptions - they propagate to the caller. This is different
    from TFR/GDELT services which return graceful fallbacks.

    For HTTP errors (4xx/5xx), the service returns a structured error dict.
    """

    async def test_weather_api_timeout_propagates(
        self, httpx_mock: pytest.fixture
    ) -> None:
        """Document: Weather API timeout exceptions propagate to caller.

        Unlike TFR and GDELT services, weather_service does NOT catch
        timeout exceptions. The caller (main.py) should handle this.
        """
        from app.weather_service import get_precip

        get_precip.cache_clear()
        httpx_mock.add_exception(httpx.TimeoutException("timeout"))

        # Current behavior: exception propagates
        with pytest.raises(httpx.TimeoutException):
            await get_precip(38.8977, -77.0365)

    async def test_weather_api_connection_error_propagates(
        self, httpx_mock: pytest.fixture
    ) -> None:
        """Document: Weather API connection errors propagate to caller."""
        from app.weather_service import get_precip

        get_precip.cache_clear()
        httpx_mock.add_exception(httpx.ConnectError("connection failed"))

        with pytest.raises(httpx.ConnectError):
            await get_precip(38.8977, -77.0365)

    async def test_weather_api_500_error_returns_error_structure(
        self, httpx_mock: pytest.fixture
    ) -> None:
        """Weather API 500 error returns structured error dict.

        Note: The function returns a tuple (result, trace) when called
        without trace parameter because internal trace list is created.
        """
        from app.weather_service import get_precip

        get_precip.cache_clear()
        httpx_mock.add_response(
            status_code=500,
            json={"reason": "Internal Server Error"}
        )

        result = await get_precip(38.8977, -77.0365)

        # Function returns (result, trace) tuple
        if isinstance(result, tuple):
            result_dict, trace = result
        else:
            result_dict = result

        assert isinstance(result_dict, dict)
        assert result_dict.get("error") is True
        assert result_dict.get("precipitating") is None


class TestFlightServiceTimeouts:
    """Tests for flight service timeout and error handling."""

    def test_opensky_timeout_falls_back_to_adsbfi(self) -> None:
        """OpenSky timeout should trigger adsb.fi fallback."""
        from app.flight_service import _cache, get_plane_state

        # Clear cache
        _cache.clear()

        with patch("app.flight_service._opensky_state") as mock_opensky:
            mock_opensky.side_effect = httpx.TimeoutException("timeout")

            with patch("app.flight_service.get_plane_state_adsb") as mock_adsb:
                mock_adsb.return_value = None

                # Call the wrapped function directly
                result = get_plane_state.__wrapped__()

        # Should have tried adsb.fi fallback
        mock_adsb.assert_called_once()
        # Both failed, so expect error envelope
        assert "errors" in result

    def test_both_feeds_timeout_returns_error_envelope(self) -> None:
        """Both OpenSky and adsb.fi timeout should return error envelope."""
        from app.flight_service import _cache, get_plane_state

        _cache.clear()

        with patch("app.flight_service._opensky_state") as mock_opensky:
            mock_opensky.side_effect = httpx.TimeoutException("opensky timeout")

            with patch("app.flight_service.get_plane_state_adsb") as mock_adsb:
                mock_adsb.side_effect = httpx.TimeoutException("adsb timeout")

                result = get_plane_state.__wrapped__()

        assert "state" in result
        assert result["state"] is None
        assert "errors" in result
        assert len(result["errors"]) == 2


class TestTfrApiTimeouts:
    """Tests for FAA TFR API timeout handling."""

    async def test_tfr_timeout_returns_empty_list(self) -> None:
        """TFR API timeout should return empty list, not raise."""
        from app.location_service import _cached, _vip_json

        # Clear cache
        _cached.clear()

        with patch("app.location_service.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.TimeoutException("timeout")
            )
            # The logged_request_async is used, so mock that
            mock_client.return_value = mock_instance

            # Need to mock the logged_request_async function
            with patch("app.location_service.logged_request_async") as mock_logged:
                mock_logged.side_effect = httpx.TimeoutException("timeout")

                result = await _vip_json.__wrapped__()

        # Should return empty list, not raise
        assert result == []


class TestGdeltApiTimeouts:
    """Tests for GDELT API timeout handling."""

    async def test_gdelt_timeout_returns_none(self) -> None:
        """GDELT API timeout should return None, not raise."""
        from app.gdelt_service import _cache, get_latest_location

        _cache.clear()

        with patch("app.gdelt_service.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await get_latest_location.__wrapped__()

        # Should return None on both narrow and fallback failure
        assert result is None
