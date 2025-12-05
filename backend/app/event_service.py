"""event_service.py
~~~~~~~~~~~~~~~~~~
Discord webhook event emitter for production observability.

Sends structured events to a Discord channel via webhook. Events are
fire-and-forget (non-blocking, errors logged but don't break main flow).

Configuration:
    DISCORD_WEBHOOK_URL: Discord webhook endpoint (optional - events skipped if not set)

Event Types:
    - machine_started: Fly.io machine started/restarted
    - flight_detected: Aircraft becomes airborne
    - landing_detected: Aircraft transitions to grounded
    - location_changed: Winning location source changes
    - rain_state_changed: Precipitation type changes
    - low_confidence: Location confidence dropped below threshold
    - api_error: External API call fails
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from typing import Any

import httpx
from dateutil import tz

# ── Configuration ─────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
UTC = tz.UTC
LOG = logging.getLogger("event_service")

# ── API Error Deduplication ───────────────────────────────────────────────
# Track last error time per API to avoid spam (1 per API per hour)
_last_api_error: dict[str, dt.datetime] = {}
API_ERROR_COOLDOWN_SEC = 3600  # 1 hour

# ── Low Confidence Deduplication ──────────────────────────────────────────
# Track when we last warned about low confidence (avoid spam)
_last_low_confidence_warning: dt.datetime | None = None
LOW_CONFIDENCE_COOLDOWN_SEC = 3600  # 1 hour
LOW_CONFIDENCE_THRESHOLD = 40  # Warn below this confidence level


# ── Event Emitters ────────────────────────────────────────────────────────


def _is_configured() -> bool:
    """Check if Discord webhook is configured."""
    return bool(DISCORD_WEBHOOK_URL.strip())


async def _post_webhook(embed: dict[str, Any]) -> None:
    """Post an embed to Discord webhook. Fire-and-forget with error logging."""
    if not _is_configured():
        return

    payload = {"embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(DISCORD_WEBHOOK_URL, json=payload)
            if resp.status_code not in (200, 204):
                LOG.warning(
                    "Discord webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as exc:
        LOG.warning("Discord webhook failed: %s", exc)


def _fire_and_forget(embed: dict[str, Any]) -> None:
    """Schedule webhook post without blocking."""
    if not _is_configured():
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_post_webhook(embed))
    except RuntimeError:
        # No running loop - run synchronously (shouldn't happen in FastAPI)
        asyncio.run(_post_webhook(embed))


def emit_flight_detected(
    callsign: str,
    lat: float,
    lon: float,
    altitude: float,
    source: str,
) -> None:
    """Emit event when aircraft becomes airborne."""
    embed = {
        "title": "Aircraft Detected",
        "color": 0x3498DB,  # Blue
        "fields": [
            {"name": "Callsign", "value": callsign, "inline": True},
            {"name": "Altitude", "value": f"{altitude:,.0f} ft", "inline": True},
            {"name": "Source", "value": source, "inline": True},
            {"name": "Position", "value": f"{lat:.4f}, {lon:.4f}", "inline": False},
        ],
        "timestamp": dt.datetime.now(UTC).isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info("[event] flight_detected: %s at %.4f, %.4f", callsign, lat, lon)


def emit_landing_detected(
    callsign: str,
    lat: float,
    lon: float,
    location_name: str | None = None,
) -> None:
    """Emit event when aircraft transitions to grounded."""
    embed = {
        "title": "Aircraft Landed",
        "color": 0x2ECC71,  # Green
        "fields": [
            {"name": "Callsign", "value": callsign, "inline": True},
            {
                "name": "Location",
                "value": location_name or f"{lat:.4f}, {lon:.4f}",
                "inline": True,
            },
            {"name": "Position", "value": f"{lat:.4f}, {lon:.4f}", "inline": False},
        ],
        "timestamp": dt.datetime.now(UTC).isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info("[event] landing_detected: %s at %s", callsign, location_name or "unknown")


def emit_location_changed(
    from_reason: str | None,
    to_reason: str,
    location_name: str,
    confidence: int,
    lat: float | None = None,
    lon: float | None = None,
    event_summary: str | None = None,
) -> None:
    """Emit event when winning location source changes."""
    # Map reasons to human-readable labels
    reason_labels = {
        "plane_air": "ADS-B (airborne)",
        "plane_ground": "ADS-B (grounded)",
        "plane_tfr": "ADS-B + TFR",
        "calendar_alias": "Calendar (alias)",
        "calendar_summary": "Calendar (summary)",
        "calendar_geocode": "Calendar (geocoded)",
        "overnight_dc": "Overnight (DC)",
        "overnight_fl": "Overnight (Florida)",
        "overnight_nj": "Overnight (NJ)",
        "tfr_json": "FAA TFR",
        "newswire": "GDELT News",
        "last_arrival": "Last Arrival Cache",
        "unknown": "Unknown",
    }

    from_label = reason_labels.get(from_reason or "", from_reason or "None")
    to_label = reason_labels.get(to_reason, to_reason)

    fields = [
        {"name": "From", "value": from_label, "inline": True},
        {"name": "To", "value": to_label, "inline": True},
        {"name": "Confidence", "value": str(confidence), "inline": True},
        {"name": "Location", "value": location_name, "inline": False},
    ]

    if event_summary:
        fields.append(
            {"name": "Event", "value": event_summary[:100], "inline": False}
        )

    if lat is not None and lon is not None:
        fields.append(
            {"name": "Position", "value": f"{lat:.4f}, {lon:.4f}", "inline": False}
        )

    embed = {
        "title": "Location Source Changed",
        "color": 0x9B59B6,  # Purple
        "fields": fields,
        "timestamp": dt.datetime.now(UTC).isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info(
        "[event] location_changed: %s -> %s (%s, conf=%d)",
        from_reason,
        to_reason,
        location_name,
        confidence,
    )


def emit_rain_state_changed(
    was: str,
    now: str,
    location: str,
    rain_mmh: float = 0.0,
    snow_cmh: float = 0.0,
) -> None:
    """Emit event when precipitation state changes."""
    # Determine emoji and color based on new state
    if now == "none":
        emoji = ""
        color = 0xF1C40F  # Yellow (sunny)
        title = "Precipitation Stopped"
    elif now == "rain":
        emoji = ""
        color = 0x3498DB  # Blue
        title = "Rain Started"
    elif now == "snow":
        emoji = ""
        color = 0xECF0F1  # White-ish
        title = "Snow Started"
    elif now == "both":
        emoji = ""
        color = 0x95A5A6  # Gray
        title = "Rain & Snow"
    else:
        emoji = ""
        color = 0x7F8C8D
        title = "Weather Changed"

    fields = [
        {"name": "Was", "value": was or "unknown", "inline": True},
        {"name": "Now", "value": now, "inline": True},
        {"name": "Location", "value": location, "inline": False},
    ]

    if rain_mmh > 0:
        fields.append({"name": "Rain", "value": f"{rain_mmh:.1f} mm/h", "inline": True})
    if snow_cmh > 0:
        fields.append({"name": "Snow", "value": f"{snow_cmh:.1f} cm/h", "inline": True})

    embed = {
        "title": f"{emoji} {title}",
        "color": color,
        "fields": fields,
        "timestamp": dt.datetime.now(UTC).isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info("[event] rain_state_changed: %s -> %s at %s", was, now, location)


def emit_low_confidence(
    confidence: float,
    location_name: str,
    reason: str,
    source: str | None = None,
) -> None:
    """Emit warning when location confidence drops below threshold (deduplicated)."""
    global _last_low_confidence_warning

    # Skip if above threshold
    if confidence >= LOW_CONFIDENCE_THRESHOLD:
        return

    # Deduplicate: max 1 warning per hour
    now = dt.datetime.now(UTC)
    if _last_low_confidence_warning:
        elapsed = (now - _last_low_confidence_warning).total_seconds()
        if elapsed < LOW_CONFIDENCE_COOLDOWN_SEC:
            return

    _last_low_confidence_warning = now

    # Map reason codes to human-readable labels
    reason_labels = {
        "plane_air": "Aircraft (airborne)",
        "plane_grounded": "Aircraft (grounded)",
        "overnight_dc": "Overnight (DC)",
        "overnight_fl": "Overnight (Florida)",
        "overnight_nj": "Overnight (NJ)",
        "calendar_alias": "Calendar (alias)",
        "calendar_summary": "Calendar (summary)",
        "calendar_geocoded": "Calendar (geocoded)",
        "newswire": "Newswire",
        "last_arrival": "Last arrival cache",
    }
    reason_display = reason_labels.get(reason, reason)

    fields = [
        {"name": "Confidence", "value": f"**{confidence:.0f}%**", "inline": True},
        {"name": "Location", "value": location_name, "inline": True},
        {"name": "Source", "value": reason_display, "inline": True},
    ]

    if source:
        fields.append({"name": "Details", "value": source[:200], "inline": False})

    embed = {
        "title": "⚠️ Low Location Confidence",
        "description": "Location data may be stale or unreliable.",
        "color": 0xF39C12,  # Orange/warning
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info(
        "[event] low_confidence: %.0f%% at %s (reason=%s)",
        confidence,
        location_name,
        reason,
    )


# ── Geocode Error Deduplication ──────────────────────────────────────────
# Track last geocode error per query to avoid spam (1 per query per 24h)
_last_geocode_error: dict[str, dt.datetime] = {}
GEOCODE_ERROR_COOLDOWN_SEC = 86400  # 24 hours

# ── Low Importance Threshold ─────────────────────────────────────────────
# Alert when Nominatim importance score falls below this value
LOW_IMPORTANCE_THRESHOLD = 0.35


def emit_geocode_failure(
    query: str,
    result_type: str,
    error: str | None = None,
) -> None:
    """Emit event when geocoding fails or returns no results (deduplicated per query)."""
    now = dt.datetime.now(UTC)

    # Check cooldown to avoid spam (same query within 24h)
    last_error = _last_geocode_error.get(query)
    if last_error:
        elapsed = (now - last_error).total_seconds()
        if elapsed < GEOCODE_ERROR_COOLDOWN_SEC:
            LOG.debug(
                "[event] geocode_failure suppressed for %r (cooldown: %ds remaining)",
                query,
                GEOCODE_ERROR_COOLDOWN_SEC - elapsed,
            )
            return

    _last_geocode_error[query] = now

    # Choose color based on result type
    if result_type == "error":
        color = 0xE74C3C  # Red
        title = "Geocode Error"
    else:
        color = 0xE67E22  # Orange
        title = "Geocode No Results"

    fields = [
        {"name": "Query", "value": query[:100], "inline": False},
        {"name": "Result", "value": result_type, "inline": True},
    ]

    if error:
        fields.append({"name": "Error", "value": error[:200], "inline": False})

    fields.append(
        {
            "name": "Action",
            "value": "Consider adding to `place_aliases.py`",
            "inline": False,
        }
    )

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info("[event] geocode_failure: %r (%s)", query, result_type)


def emit_low_importance_geocode(
    query: str,
    importance: float,
    lat: float,
    lon: float,
    display_name: str | None = None,
) -> None:
    """Emit event when geocoding returns a low-importance result (deduplicated per query).

    Low importance scores (< LOW_IMPORTANCE_THRESHOLD) often indicate:
    - Ambiguous queries resolved to wrong locations
    - Obscure places that may not be the intended result
    - Results that should be added to place_aliases.py
    """
    now = dt.datetime.now(UTC)

    # Check cooldown to avoid spam (same query within 24h)
    # Reuse _last_geocode_error dict since both are geocode issues needing attention
    last_alert = _last_geocode_error.get(query)
    if last_alert:
        elapsed = (now - last_alert).total_seconds()
        if elapsed < GEOCODE_ERROR_COOLDOWN_SEC:
            LOG.debug(
                "[event] low_importance suppressed for %r (cooldown: %ds remaining)",
                query,
                GEOCODE_ERROR_COOLDOWN_SEC - elapsed,
            )
            return

    _last_geocode_error[query] = now

    fields = [
        {"name": "Query", "value": query[:100], "inline": False},
        {"name": "Importance", "value": f"{importance:.3f}", "inline": True},
        {"name": "Threshold", "value": f"< {LOW_IMPORTANCE_THRESHOLD}", "inline": True},
        {"name": "Coordinates", "value": f"{lat:.4f}, {lon:.4f}", "inline": False},
    ]

    if display_name:
        fields.append(
            {"name": "Resolved To", "value": display_name[:200], "inline": False}
        )

    fields.append(
        {
            "name": "Action",
            "value": "Review and consider adding to `place_aliases.py`",
            "inline": False,
        }
    )

    embed = {
        "title": "Low Confidence Geocode",
        "color": 0xF39C12,  # Yellow/amber (warning)
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info(
        "[event] low_importance_geocode: %r (importance=%.3f)", query, importance
    )


def emit_api_error(api_name: str, error_message: str) -> None:
    """Emit event when an external API call fails (deduplicated)."""
    now = dt.datetime.now(UTC)

    # Check cooldown to avoid spam
    last_error = _last_api_error.get(api_name)
    if last_error:
        elapsed = (now - last_error).total_seconds()
        if elapsed < API_ERROR_COOLDOWN_SEC:
            LOG.debug(
                "[event] api_error suppressed for %s (cooldown: %ds remaining)",
                api_name,
                API_ERROR_COOLDOWN_SEC - elapsed,
            )
            return

    _last_api_error[api_name] = now

    embed = {
        "title": "API Error",
        "color": 0xE74C3C,  # Red
        "fields": [
            {"name": "API", "value": api_name, "inline": True},
            {"name": "Error", "value": error_message[:500], "inline": False},
        ],
        "timestamp": now.isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info("[event] api_error: %s - %s", api_name, error_message[:100])


def emit_machine_started(version: str | None = None) -> None:
    """Emit event when Fly.io machine starts or restarts."""
    import subprocess

    # Try to get git commit hash for version info
    commit_hash = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            commit_hash = result.stdout.strip()
    except Exception:
        pass

    fields = [
        {"name": "Event", "value": "Machine Started", "inline": True},
        {"name": "Time", "value": f"<t:{int(dt.datetime.now(UTC).timestamp())}:R>", "inline": True},
    ]

    if commit_hash:
        fields.append({"name": "Commit", "value": f"`{commit_hash}`", "inline": True})

    if version:
        fields.append({"name": "Version", "value": version, "inline": True})

    embed = {
        "title": "🚀 Rain API Started",
        "color": 0x9B59B6,  # Purple
        "fields": fields,
        "timestamp": dt.datetime.now(UTC).isoformat(),
    }
    _fire_and_forget(embed)
    LOG.info("[event] machine_started: commit=%s", commit_hash or "unknown")
