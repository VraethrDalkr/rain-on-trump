"""
tests/integration/test_nominatim_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for Nominatim Geocoding API.

IMPORTANT: Nominatim has a strict 1 request/second rate limit.
These tests use explicit delays to respect this limit.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_nominatim_api.py -v
"""

from __future__ import annotations

import time

import pytest
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter


class TestNominatimAPI:
    """Tests for Nominatim Geocoding API integration."""

    @pytest.fixture
    def geocoder(self) -> RateLimiter:
        """Get rate-limited geocoder instance."""
        return RateLimiter(
            Nominatim(user_agent="rain-on-trump-test").geocode,
            min_delay_seconds=1
        )

    def test_known_location_geocodes_correctly(self, geocoder: RateLimiter) -> None:
        """White House should geocode to Washington, DC area."""
        result = geocoder("White House, Washington DC", timeout=10)

        assert result is not None, "White House should be geocodable"
        assert 38.0 < result.latitude < 40.0, f"Latitude out of range: {result.latitude}"
        assert -78.0 < result.longitude < -76.0, f"Longitude out of range: {result.longitude}"

    def test_mar_a_lago_geocodes_correctly(self, geocoder: RateLimiter) -> None:
        """Mar-a-Lago should geocode to Palm Beach, FL area."""
        time.sleep(1.1)  # Rate limit between tests
        result = geocoder("Mar-a-Lago, Palm Beach, Florida", timeout=10)

        assert result is not None, "Mar-a-Lago should be geocodable"
        assert 26.0 < result.latitude < 27.0, f"Latitude out of range: {result.latitude}"
        assert -81.0 < result.longitude < -79.0, f"Longitude out of range: {result.longitude}"

    def test_unknown_location_returns_none(self, geocoder: RateLimiter) -> None:
        """Nonsense location should return None gracefully."""
        time.sleep(1.1)  # Rate limit between tests
        result = geocoder("xyzzy12345nonexistent", timeout=10)

        assert result is None, "Nonsense location should return None"

    def test_airport_code_geocodes(self, geocoder: RateLimiter) -> None:
        """Airport code like 'JFK Airport' should geocode."""
        time.sleep(1.1)  # Rate limit between tests
        result = geocoder("JFK Airport, New York", timeout=10)

        assert result is not None, "JFK Airport should be geocodable"
        # JFK is in Queens, NY
        assert 40.0 < result.latitude < 41.0, f"Latitude out of range: {result.latitude}"
        assert -74.0 < result.longitude < -73.0, f"Longitude out of range: {result.longitude}"

    def test_address_returns_coordinates(self, geocoder: RateLimiter) -> None:
        """Street address should return valid coordinates."""
        time.sleep(1.1)  # Rate limit between tests
        result = geocoder("1600 Pennsylvania Avenue, Washington DC", timeout=10)

        assert result is not None, "Street address should be geocodable"
        assert isinstance(result.latitude, float), "Latitude should be float"
        assert isinstance(result.longitude, float), "Longitude should be float"
        assert -90 <= result.latitude <= 90, "Latitude out of valid range"
        assert -180 <= result.longitude <= 180, "Longitude out of valid range"

    def test_rate_limiter_respects_delay(self, geocoder: RateLimiter) -> None:
        """Rate limiter should enforce minimum delay between requests."""
        time.sleep(1.1)  # Rate limit before test

        start = time.time()

        # Make two consecutive requests
        geocoder("New York", timeout=10)
        geocoder("Los Angeles", timeout=10)

        elapsed = time.time() - start

        # Should have taken at least 1 second (rate limit delay)
        assert elapsed >= 1.0, f"Rate limiting not enforced: {elapsed:.2f}s"
