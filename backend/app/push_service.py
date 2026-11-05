"""
push_service.py

Manages WebPush subscriptions and broadcasting of push notifications
using pywebpush and stored VAPID keys.
"""

import base64
import binascii
import datetime as dt
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from pywebpush import WebPushException, webpush


def _determine_persist_dir() -> Path:
    base = Path(os.getenv("PUSH_DATA_DIR", "/data")).expanduser()
    if not base.is_absolute():
        base = (Path(__file__).resolve().parent.parent / base).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        # mkdir succeeds on an existing root-owned dir (e.g. a fresh Fly volume),
        # so prove we can actually write there.
        with tempfile.TemporaryFile(dir=base):
            pass
        return base
    except (PermissionError, OSError) as exc:
        fallback = (Path(__file__).resolve().parent.parent / "local_data").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        logging.error(
            "Persist dir %s is not writable (%s); using %s. Data will NOT "
            "survive a redeploy until the volume ownership is fixed.",
            base,
            exc,
            fallback,
        )
        return fallback


PERSIST_DIR = _determine_persist_dir()
SUB_FILE = PERSIST_DIR / "push_service.subs.json"

#: Serialises every load-modify-save of SUB_FILE (workers run in threads).
_SUBS_LOCK = threading.RLock()


# VAPID keys should be set as environment variables in production
VAPID_PUBLIC = os.getenv("VAPID_PUBLIC")
VAPID_PRIVATE = os.getenv("VAPID_PRIVATE")

# Security limits
MAX_SUBSCRIPTIONS = 50000  # Cap to prevent storage abuse (~25MB at this size)
MAX_PAYLOAD_SIZE = 2048  # 2KB max payload size for subscription

# Hosts (matched by suffix) of the browser push services we accept
ALLOWED_PUSH_HOSTS: tuple[str, ...] = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)
MAX_ENDPOINT_LEN = 1024
P256DH_BYTES = 65  # uncompressed P-256 point: 0x04 || X || Y
AUTH_BYTES = 16
MAX_SEND_FAILURES = 3  # consecutive non-404/410 failures before a sub is purged
NEW_SUBS_PER_HOUR = 300  # global budget for brand-new subscriptions
_new_sub_times: deque[float] = deque()
PUSH_TIMEOUT_SEC = 10

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
    try:
        data = json.loads(SUB_FILE.read_text())
    except (ValueError, OSError) as exc:
        logger.error("[subs] Could not read %s: %s", SUB_FILE, exc)
        try:
            os.replace(SUB_FILE, SUB_FILE.with_name(SUB_FILE.name + ".corrupt"))
        except OSError:
            pass
        return []
    if not isinstance(data, list):
        logger.error("[subs] %s is not a list; ignoring", SUB_FILE)
        return []
    # Drop malformed records so later endpoint lookups cannot crash
    return [
        s for s in data if isinstance(s, dict) and isinstance(s.get("endpoint"), str)
    ]


def _save_subscriptions(subs: list[dict]) -> None:
    """
    Overwrite SUB_FILE with the given list of subscriptions.
    """
    fd, tmp = tempfile.mkstemp(dir=SUB_FILE.parent, prefix=SUB_FILE.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(subs, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SUB_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _allowed_endpoint(endpoint: str) -> bool:
    """True if endpoint is an https URL on a known push service host."""
    if len(endpoint) > MAX_ENDPOINT_LEN:
        return False
    try:
        parsed = urlparse(endpoint)
        host = (parsed.hostname or "").lower()
        has_userinfo = parsed.username is not None or parsed.password is not None
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or has_userinfo:
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_PUSH_HOSTS)


def _sanitize_preferences(prefs: object) -> dict[str, bool]:
    """Keep only known boolean preference keys (anything else is dropped)."""
    if not isinstance(prefs, dict):
        return {}
    return {
        k: v
        for k, v in prefs.items()
        if k in DEFAULT_PREFERENCES and isinstance(v, bool)
    }


def _b64url_bytes(value: str) -> bytes | None:
    """Decode unpadded/padded base64url (None if malformed)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+=*", value or ""):
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        return None


def _valid_keys(keys: dict) -> bool:
    """p256dh = 65-byte uncompressed P-256 point, auth = 16 bytes."""
    p256dh = _b64url_bytes(keys.get("p256dh") or "")
    auth = _b64url_bytes(keys.get("auth") or "")
    return (
        p256dh is not None
        and len(p256dh) == P256DH_BYTES
        and p256dh[0] == 0x04
        and auth is not None
        and len(auth) == AUTH_BYTES
    )


def _new_subscription_allowed() -> bool:
    """Spend one unit of the global hourly new-subscription budget."""
    now = time.monotonic()
    while _new_sub_times and now - _new_sub_times[0] > 3600:
        _new_sub_times.popleft()
    if len(_new_sub_times) >= NEW_SUBS_PER_HOUR:
        return False
    _new_sub_times.append(now)
    return True


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
    if not isinstance(endpoint, str) or not _allowed_endpoint(endpoint):
        logger.warning("[validation] Invalid, disallowed or missing endpoint")
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

    if not _valid_keys(keys):
        logger.warning("[validation] Malformed p256dh / auth key")
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

    with _SUBS_LOCK:
        return _add_subscription_locked(sub)


def _add_subscription_locked(sub: dict) -> dict:
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

    # Check subscription cap and the global hourly budget for new ones
    if not _new_subscription_allowed():
        logger.warning("[subscription budget] Rejected: hourly budget spent")
        return {"ok": False, "error": "Too many new subscriptions, try later"}
    if len(subs) >= MAX_SUBSCRIPTIONS:
        logger.warning(
            "[subscription cap] Rejected new subscription, at limit (%d)",
            MAX_SUBSCRIPTIONS,
        )
        return {"ok": False, "error": "Subscription limit reached"}

    # New subscription - use provided preferences or defaults
    # Build a fresh record: never trust client-supplied last_delivery etc.
    record: dict = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
        "subscription_date": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preferences": {
            **DEFAULT_PREFERENCES,
            **_sanitize_preferences(sub.get("preferences")),
        },
    }
    if "expirationTime" in sub:
        record["expirationTime"] = sub["expirationTime"]
    subs.append(record)
    _save_subscriptions(subs)
    logger.info("[subscriber added] %s", record["endpoint"])
    return {"ok": True, "preferences": record["preferences"]}


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
    with _SUBS_LOCK:
        return _update_preferences_locked(endpoint, preferences)


def _update_preferences_locked(endpoint: str, preferences: dict) -> dict | None:
    subs = _load_subscriptions()
    for sub in subs:
        if sub["endpoint"] == endpoint:
            # Get existing preferences (or defaults for legacy subs)
            current = sub.get("preferences")
            if not isinstance(current, dict):
                current = DEFAULT_PREFERENCES.copy()
            # Merge updates (known boolean keys only)
            current.update(_sanitize_preferences(preferences))
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
    with _SUBS_LOCK:
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
    if not isinstance(prefs, dict):
        prefs = DEFAULT_PREFERENCES

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

    # Snapshot without the lock (sending can take minutes); the outcome is
    # merged back as a delta so concurrent (un)subscribes are not reverted.
    subs = _load_subscriptions()
    dead: set[str] = set()
    failed: set[str] = set()
    delivered: dict[str, str] = {}
    sent_count = 0

    for sub in subs:
        # Check if subscriber wants this notification type
        try:
            wanted = _should_notify(sub, notification_type)
        except Exception as exc:
            logger.warning("[push] bad preferences, skipping sub: %s", exc)
            wanted = False
        if not wanted:
            continue

        try:
            aud = _audience(sub["endpoint"])
            vapid_claims: dict[str, str | int] = {
                "sub": "mailto:you@example.com",
                "aud": aud,
            }
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims=vapid_claims,
                timeout=PUSH_TIMEOUT_SEC,
            )
            # Update last successful delivery timestamp (keeps subscription alive)
            delivered[sub["endpoint"]] = dt.datetime.now(dt.timezone.utc).isoformat()
            sent_count += 1
            logger.info("[push ✅] %s", sub["endpoint"])
        except Exception as exc:  # one bad sub must never abort the broadcast
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                logger.warning("[push] drop dead sub (%s): %s", status, exc)
                dead.add(sub["endpoint"])
            elif int(sub.get("send_failures") or 0) + 1 >= MAX_SEND_FAILURES:
                logger.warning(
                    "[push] drop sub after %d consecutive failures: %s",
                    MAX_SEND_FAILURES,
                    exc,
                )
                dead.add(sub["endpoint"])
            else:
                logger.warning("[push] send failed, keeping sub: %s", exc)
                failed.add(sub["endpoint"])

    if dead or delivered or failed:
        try:
            with _SUBS_LOCK:
                current = _load_subscriptions()
                kept = []
                for s in current:
                    if s["endpoint"] in dead:
                        continue
                    if s["endpoint"] in delivered:
                        s["last_delivery"] = delivered[s["endpoint"]]
                        s.pop("send_failures", None)
                    elif s["endpoint"] in failed:
                        s["send_failures"] = int(s.get("send_failures") or 0) + 1
                    kept.append(s)
                _save_subscriptions(kept)
        except OSError as exc:
            # Never fail the broadcast after pushes went out: the caller would
            # retry and send duplicates.
            logger.error("[push] could not persist delivery state: %s", exc)
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
    with _SUBS_LOCK:
        return _cleanup_locked(max_days)


def _cleanup_locked(max_days: int) -> int:
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
