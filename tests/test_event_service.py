"""
tests/test_event_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests for Discord webhook event emitter.
"""

import datetime as dt

import pytest

from app import event_service as es


class MockResponse:
    """Mock httpx response."""

    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class MockAsyncClient:
    """Mock httpx.AsyncClient that records POST calls."""

    def __init__(self, response: MockResponse | None = None):
        self.posts: list[dict] = []
        self.response = response or MockResponse(204)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url: str, json: dict) -> MockResponse:
        self.posts.append({"url": url, "json": json})
        return self.response


@pytest.fixture
def mock_webhook_url(monkeypatch):
    """Set a mock webhook URL."""
    url = "https://discord.com/api/webhooks/test/mock"
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", url)
    return url


@pytest.fixture
def reset_api_error_state(monkeypatch):
    """Reset API error deduplication state."""
    monkeypatch.setattr(es, "_last_api_error", {})


def test_is_configured_false_when_empty(monkeypatch):
    """_is_configured returns False when webhook URL is empty."""
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "")
    assert es._is_configured() is False


def test_is_configured_false_when_whitespace(monkeypatch):
    """_is_configured returns False when webhook URL is whitespace."""
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "   ")
    assert es._is_configured() is False


def test_is_configured_true_when_set(mock_webhook_url):
    """_is_configured returns True when webhook URL is set."""
    assert es._is_configured() is True


@pytest.mark.asyncio
async def test_post_webhook_skips_when_not_configured(monkeypatch):
    """_post_webhook does nothing when webhook is not configured."""
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "")

    # Should not raise, just return
    await es._post_webhook({"title": "Test"})


@pytest.mark.asyncio
async def test_post_webhook_sends_embed(mock_webhook_url, monkeypatch):
    """_post_webhook sends embed to Discord webhook."""
    mock_client = MockAsyncClient()

    # Patch httpx.AsyncClient to use our mock
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock_client)

    embed = {"title": "Test Event", "color": 0x3498DB}
    await es._post_webhook(embed)

    assert len(mock_client.posts) == 1
    assert mock_client.posts[0]["url"] == mock_webhook_url
    assert mock_client.posts[0]["json"] == {"embeds": [embed]}


@pytest.mark.asyncio
async def test_post_webhook_handles_error_gracefully(mock_webhook_url, monkeypatch):
    """_post_webhook logs error but doesn't raise on failure."""
    mock_client = MockAsyncClient(MockResponse(500, "Internal Server Error"))
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock_client)

    # Should not raise
    await es._post_webhook({"title": "Test"})


def test_emit_flight_detected_logs_event(mock_webhook_url, caplog):
    """emit_flight_detected logs the event."""
    import logging

    caplog.set_level(logging.INFO)

    es.emit_flight_detected(
        callsign="AF1",
        lat=38.8977,
        lon=-77.0365,
        altitude=35000,
        source="OpenSky",
    )

    assert "[event] flight_detected" in caplog.text
    assert "AF1" in caplog.text


def test_emit_landing_detected_logs_event(mock_webhook_url, caplog):
    """emit_landing_detected logs the event."""
    import logging

    caplog.set_level(logging.INFO)

    es.emit_landing_detected(
        callsign="AF1",
        lat=26.6857,
        lon=-80.0998,
        location_name="Palm Beach International",
    )

    assert "[event] landing_detected" in caplog.text
    assert "Palm Beach" in caplog.text


def test_emit_location_changed_logs_event(mock_webhook_url, caplog):
    """emit_location_changed logs the event."""
    import logging

    caplog.set_level(logging.INFO)

    es.emit_location_changed(
        from_reason="calendar_alias",
        to_reason="newswire",
        location_name="Mar-a-Lago",
        confidence=35,
        lat=26.6776,
        lon=-80.0370,
    )

    assert "[event] location_changed" in caplog.text
    assert "calendar_alias" in caplog.text
    assert "newswire" in caplog.text


def test_emit_rain_state_changed_logs_event(mock_webhook_url, caplog):
    """emit_rain_state_changed logs the event."""
    import logging

    caplog.set_level(logging.INFO)

    es.emit_rain_state_changed(
        was="none",
        now="rain",
        location="Palm Beach",
        rain_mmh=2.5,
    )

    assert "[event] rain_state_changed" in caplog.text
    assert "none" in caplog.text
    assert "rain" in caplog.text


def test_emit_api_error_logs_event(mock_webhook_url, reset_api_error_state, caplog):
    """emit_api_error logs the event."""
    import logging

    caplog.set_level(logging.INFO)

    es.emit_api_error("OpenSky", "Connection timeout")

    assert "[event] api_error" in caplog.text
    assert "OpenSky" in caplog.text


def test_emit_api_error_deduplicates(mock_webhook_url, reset_api_error_state, caplog):
    """emit_api_error suppresses duplicate errors within cooldown period."""
    import logging

    caplog.set_level(logging.INFO)

    # First call should log
    es.emit_api_error("OpenSky", "Error 1")
    assert caplog.text.count("[event] api_error") == 1

    # Second call within cooldown should be suppressed
    caplog.clear()
    es.emit_api_error("OpenSky", "Error 2")
    assert "[event] api_error" not in caplog.text

    # Different API should still log
    es.emit_api_error("adsb.fi", "Different API error")
    assert "[event] api_error" in caplog.text


def test_emit_api_error_resets_after_cooldown(
    mock_webhook_url, reset_api_error_state, monkeypatch, caplog
):
    """emit_api_error allows new errors after cooldown expires."""
    import logging

    caplog.set_level(logging.INFO)

    # First call
    es.emit_api_error("OpenSky", "Error 1")
    assert caplog.text.count("[event] api_error") == 1

    # Simulate time passing beyond cooldown
    old_time = es._last_api_error["OpenSky"]
    es._last_api_error["OpenSky"] = old_time - dt.timedelta(
        seconds=es.API_ERROR_COOLDOWN_SEC + 1
    )

    # Now should log again
    caplog.clear()
    es.emit_api_error("OpenSky", "Error 2")
    assert "[event] api_error" in caplog.text


def test_fire_and_forget_skips_when_not_configured(monkeypatch):
    """_fire_and_forget does nothing when webhook is not configured."""
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "")

    # Should not raise
    es._fire_and_forget({"title": "Test"})
