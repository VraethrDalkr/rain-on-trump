"""snapshot_service.py
~~~~~~~~~~~~~~~~~~~~~
Rolling debug snapshot manager for production observability.

Periodically captures the full debug state (location + weather) and stores
it in a JSON file with automatic rotation. Keeps the last N hours of snapshots.

Configuration:
    SNAPSHOT_MAX_AGE_H: Maximum age of snapshots to keep (default: 168 = 7 days)

Storage:
    - Production: /data/debug_history.json (Fly.io volume)
    - Development: local_data/debug_history.json
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Final

UTC: Final = dt.timezone.utc
LOG = logging.getLogger("snapshot_service")

# ── Configuration ─────────────────────────────────────────────────────────
SNAPSHOT_MAX_AGE_H = int(os.getenv("SNAPSHOT_MAX_AGE_H", "168"))  # 7 days default


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
FILE = DIR / "debug_history.json"


# ── Snapshot Storage ──────────────────────────────────────────────────────


def _load_snapshots() -> list[dict[str, Any]]:
    """Load existing snapshots from disk."""
    if not FILE.exists():
        return []

    try:
        data = json.loads(FILE.read_text())
        return data.get("snapshots", [])
    except Exception as exc:
        LOG.warning("[snapshot] Failed to load history: %s", exc)
        return []


def _save_snapshots(snapshots: list[dict[str, Any]]) -> None:
    """Save snapshots to disk."""
    try:
        data = {
            "snapshots": snapshots,
            "max_age_hours": SNAPSHOT_MAX_AGE_H,
            "updated_at": dt.datetime.now(UTC).isoformat(),
        }
        FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as exc:
        LOG.error("[snapshot] Failed to save history: %s", exc)


def _prune_old(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove snapshots older than SNAPSHOT_MAX_AGE_H."""
    cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=SNAPSHOT_MAX_AGE_H)

    kept = []
    for snap in snapshots:
        try:
            ts_str = snap.get("ts", "")
            ts = dt.datetime.fromisoformat(ts_str)
            if ts >= cutoff:
                kept.append(snap)
        except Exception:
            # Skip malformed entries
            continue

    pruned = len(snapshots) - len(kept)
    if pruned > 0:
        LOG.info("[snapshot] Pruned %d old snapshots", pruned)

    return kept


def add_snapshot(
    coords: dict[str, Any],
    precip: dict[str, Any],
    loc_trace: list[dict[str, Any]] | None = None,
    weather_trace: list[dict[str, Any]] | None = None,
) -> None:
    """
    Add a new debug snapshot to the rolling history.

    Args:
        coords: Current location coordinates (from current_coords).
        precip: Current precipitation data (from get_precip).
        loc_trace: Location service trace (optional).
        weather_trace: Weather service trace (optional).
    """
    now = dt.datetime.now(UTC)

    snapshot = {
        "ts": now.isoformat(),
        "coords": coords,
        "precip": precip,
    }

    if loc_trace:
        snapshot["loc_trace"] = loc_trace
    if weather_trace:
        snapshot["weather_trace"] = weather_trace

    # Load existing, add new, prune old, save
    snapshots = _load_snapshots()
    snapshots.append(snapshot)
    snapshots = _prune_old(snapshots)
    _save_snapshots(snapshots)

    LOG.debug("[snapshot] Added snapshot at %s (total: %d)", now.isoformat(), len(snapshots))


def get_snapshots(
    limit: int | None = None,
    since_hours: float | None = None,
) -> list[dict[str, Any]]:
    """
    Retrieve stored snapshots with optional filtering.

    Args:
        limit: Maximum number of snapshots to return (newest first).
        since_hours: Only return snapshots from the last N hours.

    Returns:
        List of snapshots, newest first.
    """
    snapshots = _load_snapshots()

    # Filter by time if requested
    if since_hours is not None:
        cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=since_hours)
        filtered = []
        for snap in snapshots:
            try:
                ts = dt.datetime.fromisoformat(snap.get("ts", ""))
                if ts >= cutoff:
                    filtered.append(snap)
            except Exception:
                continue
        snapshots = filtered

    # Sort newest first
    snapshots.sort(key=lambda s: s.get("ts", ""), reverse=True)

    # Apply limit
    if limit is not None:
        snapshots = snapshots[:limit]

    return snapshots


def get_snapshot_stats() -> dict[str, Any]:
    """Get statistics about stored snapshots."""
    snapshots = _load_snapshots()

    if not snapshots:
        return {
            "count": 0,
            "oldest": None,
            "newest": None,
            "max_age_hours": SNAPSHOT_MAX_AGE_H,
        }

    # Get timestamps
    timestamps = []
    for snap in snapshots:
        try:
            ts = dt.datetime.fromisoformat(snap.get("ts", ""))
            timestamps.append(ts)
        except Exception:
            continue

    if not timestamps:
        return {
            "count": len(snapshots),
            "oldest": None,
            "newest": None,
            "max_age_hours": SNAPSHOT_MAX_AGE_H,
        }

    oldest = min(timestamps)
    newest = max(timestamps)

    return {
        "count": len(snapshots),
        "oldest": oldest.isoformat(),
        "newest": newest.isoformat(),
        "age_hours": (dt.datetime.now(UTC) - oldest).total_seconds() / 3600,
        "max_age_hours": SNAPSHOT_MAX_AGE_H,
    }
