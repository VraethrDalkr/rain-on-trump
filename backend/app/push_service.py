"""
push_service.py

Manages WebPush subscriptions and broadcasting of push notifications
using pywebpush and stored VAPID keys.
"""

import datetime as dt
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from pywebpush import WebPushException, webpush


def _determine_persist_dir() -> Path:
    base = Path(os.getenv("PUSH_DATA_DIR", "/data")).expanduser()
    if not base.is_absolute():
        base = (Path(__file__).resolve().parent.parent / base).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except (PermissionError, OSError):
        fallback = (Path(__file__).resolve().parent.parent / "local_data").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        logging.warning("Using %s instead of %s", fallback, base)
        return fallback


PERSIST_DIR = _determine_persist_dir()
SUB_FILE = PERSIST_DIR / "push_service.subs.json"


# VAPID keys should be set as environment variables in production
VAPID_PUBLIC = os.getenv("VAPID_PUBLIC")
VAPID_PRIVATE = os.getenv("VAPID_PRIVATE")

# Security limits
MAX_SUBSCRIPTIONS = 50000  # Cap to prevent storage abuse (~25MB at this size)
MAX_PAYLOAD_SIZE = 2048  # 2KB max payload size for subscription

# Default notification preferences (all ON)
DEFAULT_PREFERENCES: dict[str, bool] = {
    "rain_start": True,
    "rain_stop": True,
    "thunderstorm": True,
}

# Configure logging
logger = logging.getLogger("push")


def _load_subscriptions() -> list[dict]:
    """
    Read the JSON file of stored subscriptions.

    Returns:
        A list of subscription dicts.
    """
    if not SUB_FILE.exists():
        return []
    return json.loads(SUB_FILE.read_text())


def _save_subscriptions(subs: list[dict]) -> None:
    """
    Overwrite SUB_FILE with the given list of subscriptions.
    """
    SUB_FILE.write_text(json.dumps(subs, indent=2))


def validate_subscription(sub: dict) -> bool:
    """
    Validate that a subscription dict has the required WebPush structure.

    Args:
        sub: The subscription dict to validate.

    Returns:
        True if valid, False otherwise.
    """
    # Check payload size (rough estimate)
    try:
        if len(json.dumps(sub)) > MAX_PAYLOAD_SIZE:
            logger.warning("[validation] Subscription payload too large")
            return False
    except (TypeError, ValueError):
        logger.warning("[validation] Subscription not JSON-serializable")
        return False

    # Must have endpoint
    endpoint = sub.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        logger.warning("[validation] Invalid or missing endpoint")
        return False

    # Must have keys dict with p256dh and auth
    keys = sub.get("keys")
    if not isinstance(keys, dict):
        logger.warning("[validation] Missing keys dict")
        return False

    if not isinstance(keys.get("p256dh"), str) or not keys.get("p256dh"):
        logger.warning("[validation] Missing or invalid p256dh key")
        return False

    if not isinstance(keys.get("auth"), str) or not keys.get("auth"):
        logger.warning("[validation] Missing or invalid auth key")
        return False

    return True


def add_subscription(sub: dict) -> dict:
    """
    Add a new push subscription or refresh an existing one.

    If the endpoint already exists, the subscription_date is refreshed
    to signal the user is still interested (prevents cleanup removal).
    Existing preferences are preserved on refresh.

    Args:
        sub: The subscription dict from the browser's PushManager.
             May include optional 'preferences' dict.

    Returns:
        Dict with 'ok' (bool) and 'preferences' (dict) on success,
        or {'ok': False, 'error': str} on failure.
    """
    # Validate subscription structure
    if not validate_subscription(sub):
        return {"ok": False, "error": "Invalid subscription"}

    subs = _load_subscriptions()

    # Check for existing subscription with same endpoint (refresh is always allowed)
    for existing in subs:
        if existing["endpoint"] == sub["endpoint"]:
            # Update timestamp - user is still interested
            existing["subscription_date"] = dt.datetime.now(dt.timezone.utc).isoformat()
            # Preserve existing preferences on refresh (don't overwrite)
            prefs = existing.get("preferences", DEFAULT_PREFERENCES.copy())
            _save_subscriptions(subs)
            logger.info("[subscriber refreshed] %s", sub["endpoint"])
            return {"ok": True, "preferences": prefs}

    # Check subscription cap for new subscriptions
    if len(subs) >= MAX_SUBSCRIPTIONS:
        logger.warning(
            "[subscription cap] Rejected new subscription, at limit (%d)",
            MAX_SUBSCRIPTIONS,
        )
        return {"ok": False, "error": "Subscription limit reached"}

    # New subscription - use provided preferences or defaults
    sub["subscription_date"] = dt.datetime.now(dt.timezone.utc).isoformat()
    sub["preferences"] = sub.get("preferences", DEFAULT_PREFERENCES.copy())
    subs.append(sub)
    _save_subscriptions(subs)
    logger.info("[subscriber added] %s", sub["endpoint"])
    return {"ok": True, "preferences": sub["preferences"]}


def get_preferences(endpoint: str) -> dict | None:
    """
    Get notification preferences for a subscription.

    Args:
        endpoint: The subscription endpoint URL.

    Returns:
        Preferences dict, or None if endpoint not found.
        Legacy subscriptions (no preferences) return DEFAULT_PREFERENCES.
    """
    subs = _load_subscriptions()
    for sub in subs:
        if sub["endpoint"] == endpoint:
            return sub.get("preferences", DEFAULT_PREFERENCES.copy())
    return None


def update_preferences(endpoint: str, preferences: dict) -> dict | None:
    """
    Update notification preferences for an existing subscription.

    Args:
        endpoint: The subscription endpoint URL.
        preferences: Dict with preference keys to update (partial update allowed).

    Returns:
        Updated preferences dict, or None if endpoint not found.
    """
    subs = _load_subscriptions()
    for sub in subs:
        if sub["endpoint"] == endpoint:
            # Get existing preferences (or defaults for legacy subs)
            current = sub.get("preferences", DEFAULT_PREFERENCES.copy())
            # Merge updates
            current.update(preferences)
            sub["preferences"] = current
            _save_subscriptions(subs)
            logger.info("[preferences updated] %s", endpoint)
            return current
    return None


def remove_subscription(endpoint: str) -> bool:
    """
    Remove a subscription by endpoint (unsubscribe).

    Args:
        endpoint: The subscription endpoint URL.

    Returns:
        True if removed, False if not found.
    """
    subs = _load_subscriptions()
    original_len = len(subs)
    subs = [s for s in subs if s["endpoint"] != endpoint]

    if len(subs) < original_len:
        _save_subscriptions(subs)
        logger.info("[subscriber removed] %s", endpoint)
        return True
    return False


def _audience(endpoint: str) -> str:
    """
    Derive the proper VAPID audience (the `aud` claim) from an endpoint URL.

    Args:
        endpoint: The push-service endpoint URL.

    Returns:
        A string suitable for the `aud` claim.
    """
    host = urlparse(endpoint).netloc
    if host.endswith("push.apple.com"):
        # Apple's APNS web-push gateway
        return f"https://{host.split(':')[0]}"
    if host.startswith("fcm.") or "googleapis" in host:
        # FCM → use Google's audience
        return "https://fcm.googleapis.com"
    # Default to the origin of the endpoint
    return f"https://{host.split(':')[0]}"


def _should_notify(sub: dict, notification_type: str | None) -> bool:
    """
    Determine if a subscription should receive this notification type.

    Args:
        sub: Subscription dict (may or may not have 'preferences').
        notification_type: Type of notification, or None for manual broadcast.

    Returns:
        True if notification should be sent to this subscriber.

    Notification types:
        - 'rain_start': Requires preferences['rain_start'] = True
        - 'rain_stop': Requires preferences['rain_stop'] = True
        - 'thunderstorm_start': Requires rain_start AND thunderstorm = True
        - 'thunderstorm_end': Requires rain_stop AND thunderstorm = True
        - None: Send to all (manual broadcast)
    """
    # No type filter = send to everyone (manual broadcast)
    if notification_type is None:
        return True

    # Get preferences (default to all ON for legacy subscriptions)
    prefs = sub.get("preferences", DEFAULT_PREFERENCES)

    # Simple types: just check the matching preference
    if notification_type == "rain_start":
        return prefs.get("rain_start", True)
    if notification_type == "rain_stop":
        return prefs.get("rain_stop", True)

    # Thunderstorm types: require BOTH the base pref AND thunderstorm pref
    if notification_type == "thunderstorm_start":
        return prefs.get("rain_start", True) and prefs.get("thunderstorm", True)
    if notification_type == "thunderstorm_end":
        return prefs.get("rain_stop", True) and prefs.get("thunderstorm", True)

    # Unknown type - default to sending
    logger.warning("[_should_notify] Unknown notification type: %s", notification_type)
    return True


def broadcast(title: str, body: str, notification_type: str | None = None) -> int:
    """
    Send a push notification with the given title and body to subscribers
    who have opted in for this notification type.

    Args:
        title: Notification title.
        body:  Notification body.
        notification_type: Type of notification for filtering (None = send to all).

    Returns:
        Number of notifications successfully sent.

    Raises:
        WebPushException: If VAPID keys are missing or invalid.
    """
    if not VAPID_PRIVATE or not VAPID_PUBLIC:
        raise WebPushException("Missing VAPID keys")

    subs = _load_subscriptions()
    alive = []
    sent_count = 0

    for sub in subs:
        # Check if subscriber wants this notification type
        if not _should_notify(sub, notification_type):
            alive.append(sub)  # Keep sub but don't send
            continue

        try:
            aud = _audience(sub["endpoint"])
            vapid_claims = {
                "sub": "mailto:you@example.com",
                "aud": aud,
            }
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims=vapid_claims,
            )
            # Update last successful delivery timestamp (keeps subscription alive)
            sub["last_delivery"] = dt.datetime.now(dt.timezone.utc).isoformat()
            alive.append(sub)
            sent_count += 1
            logger.info("[push ✅] %s", sub["endpoint"])
        except WebPushException as exc:
            logger.warning("[push] drop dead sub: %s", exc)

    _save_subscriptions(alive)
    return sent_count


def cleanup_old_subscriptions(max_days: int = 365) -> int:
    """
    Remove subscriptions that have NEVER received a notification after max_days.

    Subscriptions with last_delivery are NEVER removed by this function.
    Dead subscriptions are handled by broadcast() on delivery failure.

    Args:
        max_days: Maximum age in days for never-delivered subscriptions.

    Returns:
        Number of subscriptions removed.
    """
    subs = _load_subscriptions()
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max_days)

    kept = []
    removed_count = 0

    for sub in subs:
        # If we've ever delivered successfully, NEVER remove by time
        # Dead subscriptions will be caught by broadcast() on next attempt
        if sub.get("last_delivery"):
            kept.append(sub)
            continue

        # Only apply time-based cleanup to never-delivered subscriptions
        timestamp_str = sub.get("subscription_date")

        # Keep subscriptions without any timestamps (backward compatibility)
        if not timestamp_str:
            kept.append(sub)
            continue

        try:
            sub_date = dt.datetime.fromisoformat(timestamp_str)
            if sub_date >= cutoff:
                kept.append(sub)
            else:
                removed_count += 1
                logger.info(
                    "[cleanup] Removed never-delivered subscription: %s (age: %d days)",
                    sub["endpoint"],
                    (now - sub_date).days,
                )
        except (ValueError, TypeError) as e:
            # Keep subscriptions with invalid timestamps
            logger.warning(
                "[cleanup] Invalid timestamp for %s: %s",
                sub["endpoint"],
                e,
            )
            kept.append(sub)

    _save_subscriptions(kept)
    return removed_count


def get_subscription_stats() -> dict:
    """
    Get statistics about subscriptions.

    Returns:
        Dict with subscription statistics:
        - total: Total number of subscriptions
        - with_timestamp: Number with any timestamp (subscription_date or last_delivery)
        - without_timestamp: Number without any timestamp
        - never_delivered: Number that have never received a notification
        - stale_never_delivered: Never delivered AND >365 days old (cleanup candidates)
        - recently_active: Number with last_delivery in last 7 days
    """
    subs = _load_subscriptions()
    now = dt.datetime.now(dt.timezone.utc)
    cutoff_365 = now - dt.timedelta(days=365)
    cutoff_7 = now - dt.timedelta(days=7)

    stats = {
        "total": len(subs),
        "with_timestamp": 0,
        "without_timestamp": 0,
        "never_delivered": 0,
        "stale_never_delivered": 0,
        "recently_active": 0,
    }

    for sub in subs:
        has_subscription_date = "subscription_date" in sub
        has_last_delivery = "last_delivery" in sub

        if not has_subscription_date and not has_last_delivery:
            stats["without_timestamp"] += 1
            continue

        stats["with_timestamp"] += 1

        # Track never-delivered subscriptions
        if not has_last_delivery:
            stats["never_delivered"] += 1
            # Check if stale (cleanup candidate)
            try:
                sub_date = dt.datetime.fromisoformat(sub["subscription_date"])
                if sub_date < cutoff_365:
                    stats["stale_never_delivered"] += 1
            except (ValueError, TypeError):
                pass

        # Track recently active (based on last_delivery)
        if has_last_delivery:
            try:
                last_delivery = dt.datetime.fromisoformat(sub["last_delivery"])
                if last_delivery >= cutoff_7:
                    stats["recently_active"] += 1
            except (ValueError, TypeError):
                pass

    return stats
