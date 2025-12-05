"""
tests/integration/test_gdelt_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for GDELT Doc API.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_gdelt_api.py -v
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.constants import USER_AGENT
from dateutil import tz


UTC = tz.UTC


class TestGdeltAPI:
    """Tests for GDELT Doc API integration."""

    @pytest.fixture
    def base_url(self) -> str:
        """Base GDELT API URL."""
        return "https://api.gdeltproject.org/api/v2/doc/doc"

    async def test_api_responds_with_json(self, base_url: str) -> None:
        """GDELT API should respond with valid JSON."""
        url = (
            f"{base_url}"
            "?query=Donald%20Trump"
            "&filter=sourceCountry:US"
            "&mode=ArtList"
            "&format=json"
            "&maxrecords=5"
            "&timespan=7d"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"

        data = resp.json()
        assert isinstance(data, dict), "Response should be JSON object"

    async def test_response_has_articles_array(self, base_url: str) -> None:
        """Response should contain articles array."""
        url = (
            f"{base_url}"
            "?query=Donald%20Trump"
            "&filter=sourceCountry:US"
            "&mode=ArtList"
            "&format=json"
            "&maxrecords=10"
            "&timespan=7d"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200
        data = resp.json()

        # articles may be empty but should be a list
        articles = data.get("articles", [])
        assert isinstance(articles, list), "articles should be a list"

    async def test_can_extract_spatial_data(self, base_url: str) -> None:
        """Should be able to extract spatial data from articles."""
        url = (
            f"{base_url}"
            "?query=Donald%20Trump"
            "&filter=sourceCountry:US"
            "&mode=ArtList"
            "&format=json"
            "&maxrecords=50"
            "&timespan=7d"
            "&include=locations"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200
        data = resp.json()

        articles = data.get("articles", [])
        if not articles:
            pytest.skip("No articles found for spatial extraction test")

        # Check if any article has spatial data
        has_spatial = False
        for art in articles:
            spatial = art.get("spatial", [])
            if spatial:
                has_spatial = True
                # Verify spatial structure
                loc = spatial[0]
                assert "lat" in loc or "latitude" in loc, "Spatial missing lat"
                assert "lon" in loc or "longitude" in loc, "Spatial missing lon"
                break

        if not has_spatial:
            pytest.skip("No articles with spatial data found")

    async def test_narrow_timewindow_query(self, base_url: str) -> None:
        """Should be able to query with startdatetime parameter."""
        # Query for last 2 hours
        since = (dt.datetime.now(UTC) - dt.timedelta(hours=2)).strftime("%Y%m%d%H%M%S")

        url = (
            f"{base_url}"
            "?query=Donald%20Trump"
            "&filter=sourceCountry:US"
            "&mode=ArtList"
            "&format=json"
            "&maxrecords=10"
            f"&startdatetime={since}"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        # API should accept the query even if no results
        assert resp.status_code == 200, f"Narrow query failed: {resp.status_code}"

        data = resp.json()
        # Result structure should still be valid
        assert isinstance(data.get("articles", []), list)

    async def test_fallback_7day_window(self, base_url: str) -> None:
        """7-day timespan query should work as fallback."""
        url = (
            f"{base_url}"
            "?query=Donald%20Trump"
            "&filter=sourceCountry:US"
            "&mode=ArtList"
            "&format=json"
            "&maxrecords=10"
            "&timespan=7d"
        )

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)

        assert resp.status_code == 200
        data = resp.json()

        # 7-day window should usually have results for Donald Trump
        articles = data.get("articles", [])
        # Don't assert count - just verify structure is valid
        assert isinstance(articles, list)
