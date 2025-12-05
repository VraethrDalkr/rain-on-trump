"""
tests/test_push_preferences.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test notification preference storage, retrieval, and filtering.

TDD: These tests are written BEFORE implementation.
Run to verify RED phase, then implement to achieve GREEN.
"""

import json

import pytest

from app import push_service as ps


# ------------------------------------------------------------------ #
# Test Helpers
# ------------------------------------------------------------------ #
def _make_sub(endpoint_id: int, prefs: dict | None = None) -> dict:
    """Create a valid subscription dict with optional preferences."""
    sub = {
        "endpoint": f"https://fcm.googleapis.com/{endpoint_id}",
        "keys": {"p256dh": "test_key", "auth": "test_auth"},
    }
    if prefs is not None:
        sub["preferences"] = prefs
    return sub


class _SpyWebPush(list):
    """Collect webpush calls for verification."""

    def __call__(self, *_, **kw):
        self.append(kw)


# ------------------------------------------------------------------ #
# Preference Storage Tests
# ------------------------------------------------------------------ #
def test_add_subscription_with_preferences(tmp_path, monkeypatch):
    """Subscription with explicit preferences should store them correctly."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    sub = _make_sub(1, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": True})
    result = ps.add_subscription(sub)

    assert result["ok"] is True
    assert result["preferences"] == {"rain_start": True, "rain_stop": False, "thunderstorm": True}

    stored = json.loads(sub_file.read_text())
    assert stored[0]["preferences"] == {"rain_start": True, "rain_stop": False, "thunderstorm": True}


def test_add_subscription_default_preferences(tmp_path, monkeypatch):
    """Subscription without preferences should get defaults (all True)."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    sub = _make_sub(1)  # No preferences
    result = ps.add_subscription(sub)

    assert result["ok"] is True
    assert result["preferences"] == {"rain_start": True, "rain_stop": True, "thunderstorm": True}

    stored = json.loads(sub_file.read_text())
    assert stored[0]["preferences"] == {"rain_start": True, "rain_stop": True, "thunderstorm": True}


def test_add_subscription_refresh_returns_existing_preferences(tmp_path, monkeypatch):
    """Refreshing existing subscription should return current preferences."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    # First subscription with custom prefs
    sub = _make_sub(1, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": False})
    ps.add_subscription(sub)

    # Refresh same endpoint (no prefs in request)
    refresh_sub = _make_sub(1)  # Same endpoint, no prefs
    result = ps.add_subscription(refresh_sub)

    # Should return existing preferences, not overwrite with defaults
    assert result["preferences"] == {"rain_start": True, "rain_stop": False, "thunderstorm": False}


# ------------------------------------------------------------------ #
# Update Preferences Tests
# ------------------------------------------------------------------ #
def test_update_preferences_existing_subscription(tmp_path, monkeypatch):
    """update_preferences should modify prefs for existing endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    # Add subscription with defaults
    sub = _make_sub(1)
    ps.add_subscription(sub)

    # Update rain_stop to False
    result = ps.update_preferences(sub["endpoint"], {"rain_stop": False})

    assert result is not None
    assert result["rain_start"] is True  # Unchanged
    assert result["rain_stop"] is False  # Updated
    assert result["thunderstorm"] is True  # Unchanged

    # Verify persisted
    stored = json.loads(sub_file.read_text())
    assert stored[0]["preferences"]["rain_stop"] is False


def test_update_preferences_partial_update(tmp_path, monkeypatch):
    """update_preferences with partial data should only update provided fields."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    sub = _make_sub(1, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    ps.add_subscription(sub)

    # Only update thunderstorm
    result = ps.update_preferences(sub["endpoint"], {"thunderstorm": False})

    assert result == {"rain_start": True, "rain_stop": True, "thunderstorm": False}


def test_update_preferences_nonexistent_returns_none(tmp_path, monkeypatch):
    """update_preferences should return None for unknown endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    result = ps.update_preferences("https://unknown.endpoint/123", {"rain_stop": False})

    assert result is None


# ------------------------------------------------------------------ #
# Get Preferences Tests
# ------------------------------------------------------------------ #
def test_get_preferences_returns_stored_values(tmp_path, monkeypatch):
    """get_preferences should return stored preferences for endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    sub = _make_sub(1, prefs={"rain_start": False, "rain_stop": True, "thunderstorm": False})
    ps.add_subscription(sub)

    result = ps.get_preferences(sub["endpoint"])

    assert result == {"rain_start": False, "rain_stop": True, "thunderstorm": False}


def test_get_preferences_unknown_endpoint_returns_none(tmp_path, monkeypatch):
    """get_preferences should return None for unknown endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    result = ps.get_preferences("https://unknown.endpoint/123")

    assert result is None


# ------------------------------------------------------------------ #
# Remove Subscription Tests
# ------------------------------------------------------------------ #
def test_remove_subscription_success(tmp_path, monkeypatch):
    """remove_subscription should delete subscription by endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    sub = _make_sub(1)
    ps.add_subscription(sub)

    result = ps.remove_subscription(sub["endpoint"])

    assert result is True
    stored = json.loads(sub_file.read_text())
    assert len(stored) == 0


def test_remove_subscription_nonexistent_returns_false(tmp_path, monkeypatch):
    """remove_subscription should return False for unknown endpoint."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    result = ps.remove_subscription("https://unknown.endpoint/123")

    assert result is False


# ------------------------------------------------------------------ #
# Backwards Compatibility Tests
# ------------------------------------------------------------------ #
def test_legacy_subscription_without_preferences_treated_as_all_on(tmp_path, monkeypatch):
    """Existing subscriptions without 'preferences' field should be treated as all ON."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    # Write legacy subscription (no preferences field)
    legacy_sub = {
        "endpoint": "https://fcm.googleapis.com/legacy",
        "keys": {"p256dh": "test", "auth": "test"},
        "subscription_date": "2024-01-01T00:00:00Z",
    }
    sub_file.write_text(json.dumps([legacy_sub]))

    # get_preferences should return defaults for legacy
    result = ps.get_preferences(legacy_sub["endpoint"])

    assert result == {"rain_start": True, "rain_stop": True, "thunderstorm": True}


# ------------------------------------------------------------------ #
# Broadcast Filtering Tests
# ------------------------------------------------------------------ #
def test_broadcast_filters_by_rain_start_preference(tmp_path, monkeypatch):
    """broadcast() with type='rain_start' should skip subs with rain_start=False."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Sub 1: wants rain_start
    sub1 = _make_sub(1, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    # Sub 2: doesn't want rain_start
    sub2 = _make_sub(2, prefs={"rain_start": False, "rain_stop": True, "thunderstorm": True})
    ps.add_subscription(sub1)
    ps.add_subscription(sub2)

    sent_count = ps.broadcast("Title", "Body", notification_type="rain_start")

    assert sent_count == 1
    assert len(spy) == 1
    assert "fcm.googleapis.com/1" in spy[0]["subscription_info"]["endpoint"]


def test_broadcast_filters_by_rain_stop_preference(tmp_path, monkeypatch):
    """broadcast() with type='rain_stop' should skip subs with rain_stop=False."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Sub 1: wants rain_stop
    sub1 = _make_sub(1, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    # Sub 2: doesn't want rain_stop
    sub2 = _make_sub(2, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": True})
    ps.add_subscription(sub1)
    ps.add_subscription(sub2)

    sent_count = ps.broadcast("Title", "Body", notification_type="rain_stop")

    assert sent_count == 1
    assert len(spy) == 1


def test_broadcast_filters_by_thunderstorm_start_preference(tmp_path, monkeypatch):
    """broadcast() with type='thunderstorm_start' requires rain_start AND thunderstorm."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Sub 1: has both rain_start AND thunderstorm
    sub1 = _make_sub(1, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    # Sub 2: has rain_start but NOT thunderstorm
    sub2 = _make_sub(2, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": False})
    # Sub 3: has thunderstorm but NOT rain_start
    sub3 = _make_sub(3, prefs={"rain_start": False, "rain_stop": True, "thunderstorm": True})
    ps.add_subscription(sub1)
    ps.add_subscription(sub2)
    ps.add_subscription(sub3)

    sent_count = ps.broadcast("Title", "Body", notification_type="thunderstorm_start")

    assert sent_count == 1  # Only sub1 should receive
    assert len(spy) == 1
    assert "fcm.googleapis.com/1" in spy[0]["subscription_info"]["endpoint"]


def test_broadcast_filters_by_thunderstorm_end_preference(tmp_path, monkeypatch):
    """broadcast() with type='thunderstorm_end' requires rain_stop AND thunderstorm."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Sub 1: has both rain_stop AND thunderstorm
    sub1 = _make_sub(1, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    # Sub 2: has rain_stop but NOT thunderstorm
    sub2 = _make_sub(2, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": False})
    # Sub 3: has thunderstorm but NOT rain_stop
    sub3 = _make_sub(3, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": True})
    ps.add_subscription(sub1)
    ps.add_subscription(sub2)
    ps.add_subscription(sub3)

    sent_count = ps.broadcast("Title", "Body", notification_type="thunderstorm_end")

    assert sent_count == 1  # Only sub1 should receive
    assert len(spy) == 1


def test_broadcast_with_none_type_sends_to_all(tmp_path, monkeypatch):
    """broadcast() with type=None should send to all subscribers (manual broadcast)."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Sub with all prefs off
    sub1 = _make_sub(1, prefs={"rain_start": False, "rain_stop": False, "thunderstorm": False})
    # Sub with all prefs on
    sub2 = _make_sub(2, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True})
    ps.add_subscription(sub1)
    ps.add_subscription(sub2)

    sent_count = ps.broadcast("Title", "Body", notification_type=None)

    assert sent_count == 2  # Both should receive
    assert len(spy) == 2


def test_broadcast_legacy_subscription_receives_all_types(tmp_path, monkeypatch):
    """Legacy subscriptions (no prefs) should receive all notification types."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Write legacy subscription (no preferences field)
    legacy_sub = {
        "endpoint": "https://fcm.googleapis.com/legacy",
        "keys": {"p256dh": "test", "auth": "test"},
    }
    sub_file.write_text(json.dumps([legacy_sub]))

    # Should receive rain_start
    sent_count = ps.broadcast("Title", "Body", notification_type="rain_start")
    assert sent_count == 1

    spy.clear()

    # Should receive rain_stop
    sent_count = ps.broadcast("Title", "Body", notification_type="rain_stop")
    assert sent_count == 1

    spy.clear()

    # Should receive thunderstorm_start
    sent_count = ps.broadcast("Title", "Body", notification_type="thunderstorm_start")
    assert sent_count == 1


def test_broadcast_mixed_preferences(tmp_path, monkeypatch):
    """Correctly filter when subs have different preference combinations."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "dummy")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "dummy")

    # Various preference combinations
    ps.add_subscription(_make_sub(1, prefs={"rain_start": True, "rain_stop": False, "thunderstorm": False}))
    ps.add_subscription(_make_sub(2, prefs={"rain_start": False, "rain_stop": True, "thunderstorm": False}))
    ps.add_subscription(_make_sub(3, prefs={"rain_start": True, "rain_stop": True, "thunderstorm": True}))

    # rain_start: should reach sub1 and sub3
    sent = ps.broadcast("Title", "Body", notification_type="rain_start")
    assert sent == 2

    spy.clear()

    # rain_stop: should reach sub2 and sub3
    sent = ps.broadcast("Title", "Body", notification_type="rain_stop")
    assert sent == 2

    spy.clear()

    # thunderstorm_start: only sub3 (needs rain_start AND thunderstorm)
    sent = ps.broadcast("Title", "Body", notification_type="thunderstorm_start")
    assert sent == 1
