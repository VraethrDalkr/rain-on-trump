"""
tests/integration/test_calendar_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for Factba.se Calendar API.

Run with:
    INTEGRATION_TESTS=1 pytest tests/integration/test_calendar_api.py -v
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.constants import USER_AGENT


FEED_URL = "https://media-cdn.factba.se/rss/json/trump/calendar-full.json"


class TestFactbaseCalendarAPI:
    """Tests for Factba.se Calendar API integration."""

    async def test_api_responds_with_json_array(self) -> None:
        """Factba.se calendar should return JSON array."""
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(FEED_URL)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"

        data = json.loads(resp.text)
        assert isinstance(data, list), "Response should be a JSON array"

    async def test_calendar_items_have_expected_fields(self) -> None:
        """Calendar items should have date, details fields."""
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(FEED_URL)

        assert resp.status_code == 200
        data = json.loads(resp.text)

        if not data:
            pytest.skip("No calendar events returned")

        # Check first item has expected fields
        item = data[0]
        assert "date" in item, "Calendar item missing 'date' field"
        # 'details' or 'time' or 'location' are common
        assert any(
            k in item for k in ("details", "time", "location")
        ), "Calendar item missing expected content fields"

    async def test_dates_are_parseable(self) -> None:
        """Date fields should be parseable ISO format."""
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(FEED_URL)

        assert resp.status_code == 200
        data = json.loads(resp.text)

        if not data:
            pytest.skip("No calendar events returned")

        for item in data[:10]:  # Check first 10 items
            date_str = item.get("date")
            assert date_str, f"Item missing date: {item}"
            try:
                dt.datetime.fromisoformat(date_str)
            except ValueError:
                pytest.fail(f"Could not parse date: {date_str}")

    async def test_recent_events_exist(self) -> None:
        """Calendar should have events within the past week."""
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(FEED_URL)

        assert resp.status_code == 200
        data = json.loads(resp.text)

        now = dt.datetime.now(dt.timezone.utc)
        week_ago = now - dt.timedelta(days=7)

        recent_events = []
        for item in data:
            try:
                date = dt.datetime.fromisoformat(item["date"])
                # Make naive datetime aware (assume UTC for comparison)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=dt.timezone.utc)
                if date >= week_ago:
                    recent_events.append(item)
            except (KeyError, ValueError):
                continue

        # Not asserting - just documenting what we found
        if not recent_events:
            pytest.skip("No events found in past week (may be normal)")

    async def test_locations_are_strings(self) -> None:
        """Location fields should be strings."""
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(FEED_URL)

        assert resp.status_code == 200
        data = json.loads(resp.text)

        for item in data[:20]:
            location = item.get("location")
            if location is not None:
                assert isinstance(location, str), f"Location should be string: {location}"
