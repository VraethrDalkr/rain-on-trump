"""geocode_log_service.py
~~~~~~~~~~~~~~~~~~~~~~~~
Rolling geocode log manager for monitoring Nominatim queries.

Stores the last N hours of geocode attempts for debugging and alias curation.
Helps identify locations that fail or return unexpected results.

Configuration:
    GEOCODE_LOG_MAX_AGE_H: Maximum age of entries to keep (default: 168 = 7 days)

Storage:
    - Production: /data/geocode_log.json (Fly.io volume)
    - Development: local_data/geocode_log.json
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Final

UTC: Final = dt.timezone.utc
LOG = logging.getLogger("geocode_log_service")

# ── Configuration ─────────────────────────────────────────────────────────
GEOCODE_LOG_MAX_AGE_H = int(os.getenv("GEOCODE_LOG_MAX_AGE_H", "168"))  # 7 days


# ── Persistence Directory ─────────────────────────────────────────────────
def _determine_dir() -> Path:
    """Determine storage directory (reuse push_service pattern)."""
    try:
        from .push_service import PERSIST_DIR

        return PERSIST_DIR
    except Exception:
        base = Path(os.getenv("PERSIST_DIR", "local_data")).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return base


DIR = _determine_dir()
FILE = DIR / "geocode_log.json"


# ── Log Entry Storage ─────────────────────────────────────────────────────


def _load_entries() -> list[dict[str, Any]]:
    """Load existing log entries from disk."""
    if not FILE.exists():
        return []

    try:
        data = json.loads(FILE.read_text())
        return data.get("entries", [])
    except Exception as exc:
        LOG.warning("[geocode_log] Failed to load: %s", exc)
        return []


def _save_entries(entries: list[dict[str, Any]]) -> None:
    """Save entries to disk."""
    try:
        data = {
            "entries": entries,
            "max_age_hours": GEOCODE_LOG_MAX_AGE_H,
            "updated_at": dt.datetime.now(UTC).isoformat(),
        }
        FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as exc:
        LOG.error("[geocode_log] Failed to save: %s", exc)


def _prune_old(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove entries older than GEOCODE_LOG_MAX_AGE_H."""
    cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=GEOCODE_LOG_MAX_AGE_H)

    kept = []
    for entry in entries:
        try:
            ts_str = entry.get("ts", "")
            ts = dt.datetime.fromisoformat(ts_str)
            if ts >= cutoff:
                kept.append(entry)
        except Exception:
            continue

    pruned = len(entries) - len(kept)
    if pruned > 0:
        LOG.info("[geocode_log] Pruned %d old entries", pruned)

    return kept


def add_geocode_entry(
    query: str,
    result_type: str,
    lat: float | None = None,
    lon: float | None = None,
    display_name: str | None = None,
    country: str | None = None,
    state: str | None = None,
    error: str | None = None,
    importance: float | None = None,
) -> None:
    """
    Add a geocode attempt to the rolling log.

    Args:
        query: The location string that was geocoded.
        result_type: One of "us", "international", "skipped", "no_result", "error".
        lat: Latitude of result (if successful).
        lon: Longitude of result (if successful).
        display_name: Full display name from Nominatim.
        country: Country from address details.
        state: State/province from address details.
        error: Error message (if failed).
        importance: Nominatim importance score (0-1), if available.
    """
    now = dt.datetime.now(UTC)

    entry: dict[str, Any] = {
        "ts": now.isoformat(),
        "query": query,
        "result_type": result_type,
    }

    if lat is not None:
        entry["lat"] = round(lat, 6)
    if lon is not None:
        entry["lon"] = round(lon, 6)
    if display_name:
        entry["display_name"] = display_name
    if country:
        entry["country"] = country
    if state:
        entry["state"] = state
    if error:
        entry["error"] = error
    if importance is not None:
        entry["importance"] = round(importance, 3)

    # Load existing, add new, prune old, save
    entries = _load_entries()
    entries.append(entry)
    entries = _prune_old(entries)
    _save_entries(entries)


def get_geocode_entries(
    limit: int | None = None,
    since_hours: float | None = None,
    result_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Retrieve stored geocode log entries with optional filtering.

    Args:
        limit: Maximum number of entries to return (newest first).
        since_hours: Only return entries from the last N hours.
        result_type: Filter by result type ("us", "international", "no_result", etc.).

    Returns:
        List of entries, newest first.
    """
    entries = _load_entries()

    # Filter by time
    if since_hours is not None:
        cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=since_hours)
        filtered = []
        for entry in entries:
            try:
                ts = dt.datetime.fromisoformat(entry.get("ts", ""))
                if ts >= cutoff:
                    filtered.append(entry)
            except Exception:
                continue
        entries = filtered

    # Filter by result type
    if result_type is not None:
        entries = [e for e in entries if e.get("result_type") == result_type]

    # Sort newest first
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)

    # Apply limit
    if limit is not None:
        entries = entries[:limit]

    return entries


def get_geocode_stats() -> dict[str, Any]:
    """Get statistics about geocode log entries."""
    entries = _load_entries()

    if not entries:
        return {
            "count": 0,
            "by_type": {},
            "oldest": None,
            "newest": None,
            "max_age_hours": GEOCODE_LOG_MAX_AGE_H,
        }

    # Count by result type
    by_type: dict[str, int] = {}
    timestamps: list[dt.datetime] = []

    for entry in entries:
        rt = entry.get("result_type", "unknown")
        by_type[rt] = by_type.get(rt, 0) + 1

        try:
            ts = dt.datetime.fromisoformat(entry.get("ts", ""))
            timestamps.append(ts)
        except Exception:
            continue

    oldest = min(timestamps) if timestamps else None
    newest = max(timestamps) if timestamps else None

    return {
        "count": len(entries),
        "by_type": by_type,
        "oldest": oldest.isoformat() if oldest else None,
        "newest": newest.isoformat() if newest else None,
        "max_age_hours": GEOCODE_LOG_MAX_AGE_H,
    }
