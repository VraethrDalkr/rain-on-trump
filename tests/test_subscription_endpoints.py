"""
tests/test_subscription_endpoints.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test subscription-related API endpoints: POST /subscribe, DELETE /subscribe, PATCH /preferences.

TDD: These tests are written BEFORE implementation.
Run to verify RED phase, then implement to achieve GREEN.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app import push_service as ps


# ------------------------------------------------------------------ #
# Test Helpers
# ------------------------------------------------------------------ #
def _make_sub(endpoint_id: int = 1, prefs: dict | None = None) -> dict:
    """Create a valid subscription dict with optional preferences."""
    sub = {
        "endpoint": f"https://fcm.googleapis.com/{endpoint_id}",
        "keys": {"p256dh": "test_key", "auth": "test_auth"},
    }
    if prefs is not None:
        sub["preferences"] = prefs
    return sub


def _build_client() -> TestClient:
    """Return a TestClient for the app."""
    return TestClient(main.app)


# ------------------------------------------------------------------ #
# POST /subscribe Tests
# ------------------------------------------------------------------ #
def test_subscribe_new_returns_default_preferences(tmp_path, monkeypatch):
    """New subscription should return default preferences (all True)."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()
    sub = _make_sub(1)

    resp = client.post("/subscribe", json=sub)

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["preferences"] == {"rain_start": True, "rain_stop": True, "thunderstorm": True}


def test_subscribe_with_custom_preferences(tmp_path, monkeypatch):
    """Subscription with custom preferences should store and return them."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()
    sub = _make_sub(1, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": False})

    resp = client.post("/subscribe", json=sub)

    assert resp.status_code == 200
    data = resp.json()
    assert data["preferences"] == {"rain_start": True, "rain_stop": False, "thunderstorm": False}


def test_subscribe_refresh_returns_existing_preferences(tmp_path, monkeypatch):
    """Refreshing existing subscription should return stored preferences."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    # First subscription with custom prefs
    sub = _make_sub(1, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": False})
    client.post("/subscribe", json=sub)

    # Refresh (same endpoint, no prefs in request)
    refresh_sub = _make_sub(1)
    resp = client.post("/subscribe", json=refresh_sub)

    assert resp.status_code == 200
    data = resp.json()
    # Should return existing prefs, not overwrite with defaults
    assert data["preferences"] == {"rain_start": True, "rain_stop": False, "thunderstorm": False}


def test_subscribe_invalid_returns_400(tmp_path, monkeypatch):
    """Invalid subscription should return 400."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()
    invalid_sub = {"endpoint": "http://insecure.com"}  # http, not https

    resp = client.post("/subscribe", json=invalid_sub)

    assert resp.status_code == 400


# ------------------------------------------------------------------ #
# DELETE /subscribe Tests
# ------------------------------------------------------------------ #
def test_unsubscribe_removes_subscription(tmp_path, monkeypatch):
    """DELETE /subscribe should remove subscription by endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    # Add subscription first
    sub = _make_sub(1)
    client.post("/subscribe", json=sub)

    # Verify it's stored
    stored = json.loads(sub_file.read_text())
    assert len(stored) == 1

    # Unsubscribe
    resp = client.request("DELETE", "/subscribe", json={"endpoint": sub["endpoint"]})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify removed
    stored = json.loads(sub_file.read_text())
    assert len(stored) == 0


def test_unsubscribe_nonexistent_returns_404(tmp_path, monkeypatch):
    """DELETE /subscribe for unknown endpoint should return 404."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    resp = client.request("DELETE", "/subscribe", json={"endpoint": "https://unknown/123"})

    assert resp.status_code == 404


def test_unsubscribe_requires_endpoint_body(tmp_path, monkeypatch):
    """DELETE /subscribe without endpoint should return 400."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    resp = client.request("DELETE", "/subscribe", json={})

    assert resp.status_code == 400


# ------------------------------------------------------------------ #
# PATCH /preferences Tests
# ------------------------------------------------------------------ #
def test_patch_preferences_updates_successfully(tmp_path, monkeypatch):
    """PATCH /preferences should update preferences for existing subscription."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    # Add subscription with defaults
    sub = _make_sub(1)
    client.post("/subscribe", json=sub)

    # Update rain_stop to False
    resp = client.patch("/preferences", json={
        "endpoint": sub["endpoint"],
        "preferences": {"rain_stop": False}
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["preferences"]["rain_start"] is True  # Unchanged
    assert data["preferences"]["rain_stop"] is False  # Updated
    assert data["preferences"]["thunderstorm"] is True  # Unchanged


def test_patch_preferences_partial_update(tmp_path, monkeypatch):
    """PATCH /preferences with partial data should only update provided fields."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    # Add subscription
    sub = _make_sub(1, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    client.post("/subscribe", json=sub)

    # Only update thunderstorm
    resp = client.patch("/preferences", json={
        "endpoint": sub["endpoint"],
        "preferences": {"thunderstorm": False}
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["preferences"] == {"rain_start": True, "rain_stop": True, "thunderstorm": False}


def test_patch_preferences_nonexistent_returns_404(tmp_path, monkeypatch):
    """PATCH /preferences for unknown endpoint should return 404."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    resp = client.patch("/preferences", json={
        "endpoint": "https://unknown/123",
        "preferences": {"rain_stop": False}
    })

    assert resp.status_code == 404


def test_patch_preferences_invalid_types_returns_400(tmp_path, monkeypatch):
    """PATCH /preferences with non-boolean values should return 400."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    # Add subscription first
    sub = _make_sub(1)
    client.post("/subscribe", json=sub)

    # Try to update with non-boolean
    resp = client.patch("/preferences", json={
        "endpoint": sub["endpoint"],
        "preferences": {"rain_stop": "yes"}  # String instead of bool
    })

    assert resp.status_code == 400


def test_patch_preferences_requires_endpoint(tmp_path, monkeypatch):
    """PATCH /preferences without endpoint should return 400."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    client = _build_client()

    resp = client.patch("/preferences", json={
        "preferences": {"rain_stop": False}
    })

    assert resp.status_code == 400
