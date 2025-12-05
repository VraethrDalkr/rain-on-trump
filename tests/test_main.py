"""
FastAPI integration tests for /is_it_raining.json and /broadcast.

All heavyweight dependencies (geocoding, weather lookup, WebPush) are
stubbed so the test suite is offline-friendly.
"""

from __future__ import annotations
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _build_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient with stubs patched in."""

    # ▸  current_coords --------------------------------------------------- #
    async def _fake_coords() -> dict[str, Any]:
        return {"lat": 40.0, "lon": -75.0, "name": "Somewhere, USA"}

    # ▸  get_precip ------------------------------------------------------- #
    async def _fake_precip(lat: float, lon: float) -> dict[str, Any]:
        assert (lat, lon) == (40.0, -75.0)
        return {"precipitating": False}

    monkeypatch.setattr(main, "current_coords", _fake_coords, raising=True)
    monkeypatch.setattr(main, "get_precip", _fake_precip, raising=True)
    monkeypatch.setattr(main, "broadcast", lambda *_, **__: None, raising=True)
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret", raising=False)

    return TestClient(main.app)


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #
def test_is_it_raining(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(monkeypatch)

    resp = client.get("/is_it_raining.json")
    assert resp.status_code == 200
    data = resp.json()

    assert data["precipitating"] is False
    assert data["coords"]["name"] == "Somewhere, USA"
    assert "mmh" in data and data["mmh"] == 0.0

    # Timestamp ends with either “Z” (old style) or “+00:00” (current code)
    assert data["timestamp"].endswith(("Z", "+00:00"))


def test_broadcast_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def mock_broadcast(title: str, body: str, notification_type: str | None = None) -> int:
        calls.append((title, body, notification_type))
        return 1  # Return sent_count

    monkeypatch.setattr(main, "broadcast", mock_broadcast)
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret", raising=False)

    client = TestClient(main.app)
    resp = client.post("/broadcast?msg=hi&token=secret")

    assert resp.status_code == 200
    assert resp.json()["sent_count"] == 1
    assert calls == [("Rain on Trump", "hi", None)]


def test_broadcast_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(monkeypatch)
    assert client.post("/broadcast?msg=hi&token=bad").status_code == 403


# ------------------------------------------------------------------ #
# Thunderstorm Integration Tests
# ------------------------------------------------------------------ #
def test_is_it_raining_includes_thunderstorm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Response should include thunderstorm data from weather service."""

    async def _fake_coords() -> dict[str, Any]:
        return {"lat": 40.0, "lon": -75.0, "name": "Somewhere, USA"}

    async def _fake_precip(lat: float, lon: float) -> dict[str, Any]:
        return {
            "precipitating": True,
            "rain": 5.0,
            "thunderstorm": True,
            "thunderstorm_state": "moderate",
        }

    monkeypatch.setattr(main, "current_coords", _fake_coords, raising=True)
    monkeypatch.setattr(main, "get_precip", _fake_precip, raising=True)
    monkeypatch.setattr(main, "broadcast", lambda *_, **__: None, raising=True)
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret", raising=False)

    client = TestClient(main.app)
    resp = client.get("/is_it_raining.json")
    assert resp.status_code == 200
    data = resp.json()

    # Should include thunderstorm fields
    assert "thunderstorm" in data
    assert data["thunderstorm"] is True
    assert data["thunderstorm_state"] == "moderate"


def test_is_it_raining_no_thunderstorm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Response should show no thunderstorm when weather code is normal."""

    async def _fake_coords() -> dict[str, Any]:
        return {"lat": 40.0, "lon": -75.0, "name": "Somewhere, USA"}

    async def _fake_precip(lat: float, lon: float) -> dict[str, Any]:
        return {
            "precipitating": True,
            "rain": 2.0,
            "thunderstorm": False,
            "thunderstorm_state": "none",
        }

    monkeypatch.setattr(main, "current_coords", _fake_coords, raising=True)
    monkeypatch.setattr(main, "get_precip", _fake_precip, raising=True)
    monkeypatch.setattr(main, "broadcast", lambda *_, **__: None, raising=True)
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret", raising=False)

    client = TestClient(main.app)
    resp = client.get("/is_it_raining.json")
    assert resp.status_code == 200
    data = resp.json()

    assert data["thunderstorm"] is False
    assert data["thunderstorm_state"] == "none"


def test_is_it_raining_in_flight_no_thunderstorm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thunderstorm should be False when plane is in flight."""

    async def _fake_coords() -> dict[str, Any]:
        return {
            "lat": 40.0,
            "lon": -75.0,
            "name": "In Flight",
            "in_flight": True,
        }

    async def _fake_precip(lat: float, lon: float) -> dict[str, Any]:
        return {"precipitating": False}

    monkeypatch.setattr(main, "current_coords", _fake_coords, raising=True)
    monkeypatch.setattr(main, "get_precip", _fake_precip, raising=True)
    monkeypatch.setattr(main, "broadcast", lambda *_, **__: None, raising=True)
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret", raising=False)

    client = TestClient(main.app)
    resp = client.get("/is_it_raining.json")
    data = resp.json()

    # In-flight should show no precipitation and no thunderstorm
    assert data["precipitating"] is False
    assert data["thunderstorm"] is False
    assert data["thunderstorm_state"] == "none"


# ------------------------------------------------------------------ #
# Thunderstorm Notification Tests
# ------------------------------------------------------------------ #
def test_thunderstorm_notification_on_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification should be sent when thunderstorm starts."""
    notifications: list[tuple[str, str]] = []

    def _capture_broadcast(title: str, body: str, notification_type: str | None = None) -> None:
        notifications.append((title, body))

    monkeypatch.setattr(main, "broadcast", _capture_broadcast, raising=True)

    # Test the notification function directly
    class MockApp:
        class state:
            prev_thunderstorm_state = "none"
            thunderstorm_last_notified: dict = {}

    precip = {"thunderstorm_state": "moderate"}
    coords = {"name": "Mar-a-Lago"}

    main._maybe_send_thunderstorm_notification(MockApp, precip, coords)

    assert len(notifications) == 1
    assert notifications[0][0] == "Thunderstorm Alert"
    assert "Thunderstorm detected" in notifications[0][1]
    assert "Mar-a-Lago" in notifications[0][1]


def test_thunderstorm_notification_on_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notification should be sent when thunderstorm escalates to severe."""
    notifications: list[tuple[str, str]] = []

    def _capture_broadcast(title: str, body: str, notification_type: str | None = None) -> None:
        notifications.append((title, body))

    monkeypatch.setattr(main, "broadcast", _capture_broadcast, raising=True)

    class MockApp:
        class state:
            prev_thunderstorm_state = "moderate"
            thunderstorm_last_notified: dict = {}

    precip = {"thunderstorm_state": "severe"}
    coords = {"name": "The White House"}

    main._maybe_send_thunderstorm_notification(MockApp, precip, coords)

    assert len(notifications) == 1
    assert "intensifying" in notifications[0][1].lower()
    assert "hail" in notifications[0][1].lower()


def test_thunderstorm_notification_on_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification should be sent when thunderstorm ends."""
    notifications: list[tuple[str, str]] = []

    def _capture_broadcast(title: str, body: str, notification_type: str | None = None) -> None:
        notifications.append((title, body))

    monkeypatch.setattr(main, "broadcast", _capture_broadcast, raising=True)

    class MockApp:
        class state:
            prev_thunderstorm_state = "moderate"
            thunderstorm_last_notified: dict = {}

    precip = {"thunderstorm_state": "none"}
    coords = {"name": "Trump Tower"}

    main._maybe_send_thunderstorm_notification(MockApp, precip, coords)

    assert len(notifications) == 1
    assert "passed" in notifications[0][1].lower()


def test_thunderstorm_no_notification_same_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No notification when state doesn't change."""
    notifications: list[tuple[str, str]] = []

    def _capture_broadcast(title: str, body: str, notification_type: str | None = None) -> None:
        notifications.append((title, body))

    monkeypatch.setattr(main, "broadcast", _capture_broadcast, raising=True)

    class MockApp:
        class state:
            prev_thunderstorm_state = "moderate"
            thunderstorm_last_notified: dict = {}

    precip = {"thunderstorm_state": "moderate"}  # Same as prev
    coords = {"name": "Somewhere"}

    main._maybe_send_thunderstorm_notification(MockApp, precip, coords)

    assert len(notifications) == 0  # No notification sent
