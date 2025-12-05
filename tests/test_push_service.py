import json

import pytest

from app import push_service as ps


# ------------------------------------------------------------------ #
# Validation Tests
# ------------------------------------------------------------------ #
def test_validate_subscription_valid():
    """Valid subscription with all required fields should pass."""
    sub = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
        "keys": {"p256dh": "BNcRdreALRFX...", "auth": "tBHItJI5svbp..."},
    }
    assert ps.validate_subscription(sub) is True


def test_validate_subscription_missing_endpoint():
    """Subscription without endpoint should fail."""
    sub = {"keys": {"p256dh": "test", "auth": "test"}}
    assert ps.validate_subscription(sub) is False


def test_validate_subscription_http_endpoint():
    """Subscription with http:// (not https://) should fail."""
    sub = {
        "endpoint": "http://insecure.example.com/push",
        "keys": {"p256dh": "test", "auth": "test"},
    }
    assert ps.validate_subscription(sub) is False


def test_validate_subscription_missing_keys():
    """Subscription without keys dict should fail."""
    sub = {"endpoint": "https://example.com/push"}
    assert ps.validate_subscription(sub) is False


def test_validate_subscription_missing_p256dh():
    """Subscription without p256dh key should fail."""
    sub = {
        "endpoint": "https://example.com/push",
        "keys": {"auth": "test"},
    }
    assert ps.validate_subscription(sub) is False


def test_validate_subscription_missing_auth():
    """Subscription without auth key should fail."""
    sub = {
        "endpoint": "https://example.com/push",
        "keys": {"p256dh": "test"},
    }
    assert ps.validate_subscription(sub) is False


def test_validate_subscription_empty_keys():
    """Subscription with empty string keys should fail."""
    sub = {
        "endpoint": "https://example.com/push",
        "keys": {"p256dh": "", "auth": "test"},
    }
    assert ps.validate_subscription(sub) is False


def test_validate_subscription_too_large():
    """Subscription exceeding MAX_PAYLOAD_SIZE should fail."""
    sub = {
        "endpoint": "https://example.com/push",
        "keys": {"p256dh": "x" * 3000, "auth": "test"},  # >2KB
    }
    assert ps.validate_subscription(sub) is False


# ------------------------------------------------------------------ #
# Subscription Cap Tests
# ------------------------------------------------------------------ #
def test_add_subscription_cap(tmp_path, monkeypatch):
    """New subscriptions should be rejected when cap is reached."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)
    monkeypatch.setattr(ps, "MAX_SUBSCRIPTIONS", 2)  # Low cap for testing

    valid_sub = lambda n: {
        "endpoint": f"https://example.com/{n}",
        "keys": {"p256dh": "test", "auth": "test"},
    }

    # First two should succeed
    assert ps.add_subscription(valid_sub(1))["ok"] is True
    assert ps.add_subscription(valid_sub(2))["ok"] is True

    # Third should fail (cap reached)
    assert ps.add_subscription(valid_sub(3))["ok"] is False

    # Verify only 2 stored
    stored = json.loads(sub_file.read_text())
    assert len(stored) == 2


def test_add_subscription_refresh_allowed_at_cap(tmp_path, monkeypatch):
    """Refreshing existing subscription should work even at cap."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)
    monkeypatch.setattr(ps, "MAX_SUBSCRIPTIONS", 1)

    sub = {
        "endpoint": "https://example.com/existing",
        "keys": {"p256dh": "test", "auth": "test"},
    }

    # Add first subscription
    assert ps.add_subscription(sub)["ok"] is True

    # Refresh same subscription (should work even at cap)
    assert ps.add_subscription(sub)["ok"] is True

    # New subscription should fail
    new_sub = {
        "endpoint": "https://example.com/new",
        "keys": {"p256dh": "test", "auth": "test"},
    }
    assert ps.add_subscription(new_sub)["ok"] is False


def test_add_subscription_returns_false_on_invalid(tmp_path, monkeypatch):
    """add_subscription should return ok=False for invalid subscriptions."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    invalid_sub = {"endpoint": "http://insecure.com", "keys": {}}
    assert ps.add_subscription(invalid_sub)["ok"] is False

    # File should not be created
    assert not sub_file.exists()


# ------------------------------------------------------------------ #
# Original Tests
# ------------------------------------------------------------------ #
class _SpyWebPush(list):
    """Collect positional/kw-args each time `webpush` is invoked."""

    def __call__(self, *_, **kw):
        self.append(kw)


def test_add_subscription_deduplicates(tmp_path, monkeypatch):
    """Adding the same endpoint twice should not create duplicate entries."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    # Valid WebPush subscription format with required keys
    sub = {
        "endpoint": "https://example.com/123",
        "keys": {"p256dh": "test_p256dh_key", "auth": "test_auth_key"},
    }
    ps.add_subscription(sub)
    ps.add_subscription(sub)  # duplicate - should refresh timestamp, not add

    stored = json.loads(sub_file.read_text())
    assert len(stored) == 1  # Still only one subscription
    assert stored[0]["endpoint"] == sub["endpoint"]
    assert stored[0]["keys"] == sub["keys"]
    assert "subscription_date" in stored[0]  # Has timestamp


def test_broadcast_happy_path(tmp_path, monkeypatch):
    sub_file = tmp_path / "subs.json"
    sub_file.write_text(
        json.dumps([{"endpoint": "https://fcm.example/abc", "keys": {}}])
    )
    monkeypatch.setattr(ps, "SUB_FILE", sub_file)

    spy = _SpyWebPush()
    monkeypatch.setattr(ps, "webpush", spy)

    # inject test VAPID keys
    for k in ("VAPID_PUBLIC", "VAPID_PRIVATE"):
        monkeypatch.setenv(k, "dummy")
        setattr(ps, k, "dummy")

    ps.broadcast("Title", "Body")

    assert len(spy) == 1  # webpush called once
    assert json.loads(sub_file.read_text())[0]["endpoint"].endswith(
        "abc"
    )  # still stored
