"""
tests/test_push_cleanup.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test WebPush subscription cleanup logic.

Key behavior:
- Subscriptions with last_delivery are NEVER removed by time-based cleanup
- Only subscriptions that have NEVER received a notification can be removed
- Default cleanup window is 365 days for never-delivered subs
- Resubscribing (same endpoint) refreshes the subscription_date
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from app import push_service


def test_subscription_includes_timestamp(tmp_path, monkeypatch):
    """New subscriptions should include subscription_date timestamp."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    sub = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/test123",
        "keys": {"p256dh": "abc", "auth": "def"},
    }

    push_service.add_subscription(sub)

    subs = json.loads(sub_file.read_text())
    assert len(subs) == 1
    assert "subscription_date" in subs[0]
    assert isinstance(subs[0]["subscription_date"], str)  # ISO format


def test_resubscribe_refreshes_timestamp(tmp_path, monkeypatch):
    """Resubscribing with same endpoint should refresh subscription_date."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    now = dt.datetime.now(dt.timezone.utc)
    old_date = (now - dt.timedelta(days=400)).isoformat()

    # Existing subscription with old timestamp
    subs = [
        {
            "endpoint": "https://fcm.googleapis.com/existing",
            "keys": {"p256dh": "abc", "auth": "def"},
            "subscription_date": old_date,
        }
    ]
    sub_file.write_text(json.dumps(subs))

    # User resubscribes with same endpoint
    push_service.add_subscription(
        {
            "endpoint": "https://fcm.googleapis.com/existing",
            "keys": {"p256dh": "abc", "auth": "def"},
        }
    )

    # Check timestamp was refreshed
    subs = json.loads(sub_file.read_text())
    assert len(subs) == 1  # Still one subscription
    new_date = dt.datetime.fromisoformat(subs[0]["subscription_date"])
    assert (now - new_date).total_seconds() < 60  # Refreshed to recent time


def test_cleanup_uses_365_day_default(tmp_path, monkeypatch):
    """Default cleanup window should be 365 days for never-delivered subs."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    now = dt.datetime.now(dt.timezone.utc)
    date_300_days = (now - dt.timedelta(days=300)).isoformat()
    date_400_days = (now - dt.timedelta(days=400)).isoformat()

    subs = [
        {
            "endpoint": "https://fcm.googleapis.com/300-days",
            "keys": {},
            "subscription_date": date_300_days,
            # No last_delivery
        },
        {
            "endpoint": "https://fcm.googleapis.com/400-days",
            "keys": {},
            "subscription_date": date_400_days,
            # No last_delivery
        },
    ]

    sub_file.write_text(json.dumps(subs))

    # Default cleanup (365 days)
    removed_count = push_service.cleanup_old_subscriptions()

    assert removed_count == 1  # Only 400-day-old removed

    remaining = json.loads(sub_file.read_text())
    assert len(remaining) == 1
    assert remaining[0]["endpoint"] == "https://fcm.googleapis.com/300-days"


def test_never_remove_if_last_delivery_exists(tmp_path, monkeypatch):
    """Subscriptions with last_delivery should NEVER be removed by time-based cleanup."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    now = dt.datetime.now(dt.timezone.utc)
    very_old_subscription = (now - dt.timedelta(days=500)).isoformat()
    very_old_delivery = (now - dt.timedelta(days=400)).isoformat()

    subs = [
        {
            "endpoint": "https://fcm.googleapis.com/delivered-long-ago",
            "keys": {},
            "subscription_date": very_old_subscription,
            "last_delivery": very_old_delivery,  # Delivered 400 days ago
        },
    ]

    sub_file.write_text(json.dumps(subs))

    # Even with aggressive cleanup, sub with last_delivery is kept
    removed_count = push_service.cleanup_old_subscriptions(max_days=30)

    assert removed_count == 0  # NOT removed despite being very old

    remaining = json.loads(sub_file.read_text())
    assert len(remaining) == 1
    assert remaining[0]["endpoint"] == "https://fcm.googleapis.com/delivered-long-ago"


def test_only_cleanup_never_delivered_subscriptions(tmp_path, monkeypatch):
    """Time-based cleanup should only apply to subscriptions that never received a notification."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    now = dt.datetime.now(dt.timezone.utc)
    old_date = (now - dt.timedelta(days=400)).isoformat()
    old_delivery = (now - dt.timedelta(days=300)).isoformat()

    subs = [
        {
            "endpoint": "https://fcm.googleapis.com/has-delivery",
            "keys": {},
            "subscription_date": old_date,
            "last_delivery": old_delivery,  # Has received at least one notification
        },
        {
            "endpoint": "https://fcm.googleapis.com/never-delivered",
            "keys": {},
            "subscription_date": old_date,
            # No last_delivery - never received any notification
        },
    ]

    sub_file.write_text(json.dumps(subs))

    removed_count = push_service.cleanup_old_subscriptions(max_days=365)

    assert removed_count == 1  # Only the never-delivered one removed

    remaining = json.loads(sub_file.read_text())
    assert len(remaining) == 1
    assert remaining[0]["endpoint"] == "https://fcm.googleapis.com/has-delivery"


def test_cleanup_subscriptions_no_timestamp(tmp_path, monkeypatch):
    """Subscriptions without any timestamp should be kept (backward compat)."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    subs = [
        {
            "endpoint": "https://fcm.googleapis.com/no-timestamp",
            "keys": {},
            # No subscription_date or last_delivery
        },
    ]

    sub_file.write_text(json.dumps(subs))

    removed_count = push_service.cleanup_old_subscriptions(max_days=30)

    assert removed_count == 0  # Should not remove subs without timestamps
    remaining = json.loads(sub_file.read_text())
    assert len(remaining) == 1


def test_get_subscription_stats(tmp_path, monkeypatch):
    """Should return statistics about subscriptions."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    now = dt.datetime.now(dt.timezone.utc)
    recent_date = (now - dt.timedelta(days=3)).isoformat()
    old_never_delivered = (now - dt.timedelta(days=400)).isoformat()
    has_delivery = (now - dt.timedelta(days=2)).isoformat()

    subs = [
        {
            "endpoint": "https://fcm.googleapis.com/1",
            "keys": {},
            "subscription_date": old_never_delivered,
            # No last_delivery - stale, never delivered
        },
        {
            "endpoint": "https://fcm.googleapis.com/2",
            "keys": {},
            "subscription_date": recent_date,
            "last_delivery": has_delivery,  # Recently active
        },
        {"endpoint": "https://fcm.googleapis.com/3", "keys": {}},  # No timestamp
    ]

    sub_file.write_text(json.dumps(subs))

    stats = push_service.get_subscription_stats()

    assert stats["total"] == 3
    assert stats["with_timestamp"] == 2
    assert stats["without_timestamp"] == 1
    assert stats["never_delivered"] == 1  # Sub 1 has no last_delivery
    assert stats["stale_never_delivered"] == 1  # Sub 1 is >365 days and no delivery
    assert stats["recently_active"] == 1  # Sub 2 has recent last_delivery


def test_stats_with_mixed_subscriptions(tmp_path, monkeypatch):
    """Stats should correctly categorize various subscription states."""
    sub_file = tmp_path / "subs.json"
    monkeypatch.setattr(push_service, "SUB_FILE", sub_file)

    now = dt.datetime.now(dt.timezone.utc)

    subs = [
        # Has delivery, recently active
        {
            "endpoint": "https://example.com/1",
            "keys": {},
            "subscription_date": (now - dt.timedelta(days=500)).isoformat(),
            "last_delivery": (now - dt.timedelta(days=2)).isoformat(),
        },
        # Has delivery, not recently active (but still kept)
        {
            "endpoint": "https://example.com/2",
            "keys": {},
            "subscription_date": (now - dt.timedelta(days=400)).isoformat(),
            "last_delivery": (now - dt.timedelta(days=100)).isoformat(),
        },
        # Never delivered, recent subscription
        {
            "endpoint": "https://example.com/3",
            "keys": {},
            "subscription_date": (now - dt.timedelta(days=30)).isoformat(),
        },
        # Never delivered, stale subscription (cleanup candidate)
        {
            "endpoint": "https://example.com/4",
            "keys": {},
            "subscription_date": (now - dt.timedelta(days=400)).isoformat(),
        },
    ]

    sub_file.write_text(json.dumps(subs))

    stats = push_service.get_subscription_stats()

    assert stats["total"] == 4
    assert stats["with_timestamp"] == 4
    assert stats["without_timestamp"] == 0
    assert stats["never_delivered"] == 2  # Subs 3 and 4
    assert stats["stale_never_delivered"] == 1  # Only sub 4 (>365 days, no delivery)
    assert stats["recently_active"] == 1  # Only sub 1 (delivery in last 7 days)
