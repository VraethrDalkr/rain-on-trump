"""
arrival_cache.py
~~~~~~~~~~~~~~~~
Store and retrieve the **last confirmed arrival** of Trump’s aircraft
so we can fall back on it when every live data source is silent.

* A very small JSON file is kept in `local_data/last_arrival.json`
  (or in the directory given by $PERSIST_DIR/$PUSH_DATA_DIR).
* We ignore arrivals older than *max_days* (default 7 days).

This is deliberately lightweight — no database needed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Final, TypedDict

UTC: Final = dt.timezone.utc


# ── persistence dir (re-use the push_service helper when available) ─────────
def _determine_dir() -> Path:
    try:
        from .push_service import PERSIST_DIR  # type: ignore

        return PERSIST_DIR
    except Exception:  # noqa: BLE001 – fallback for tests
        base = Path(os.getenv("PERSIST_DIR", "local_data")).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return base


DIR = _determine_dir()
FILE = DIR / "last_arrival.json"
LOG = logging.getLogger("arrival_cache")


class Arrival(TypedDict):
    lat: float
    lon: float
    ts: str  # ISO-8601


def save(lat: float, lon: float, ts: dt.datetime | None = None) -> None:
    """
    Persist the latest grounded aircraft coordinates.

    Args:
        lat, lon:  Decimal degrees.
        ts:        Timestamp UTC (defaults to now).
    """
    ts = ts or dt.datetime.now(UTC)
    data: Arrival = {"lat": float(lat), "lon": float(lon), "ts": ts.isoformat()}
    FILE.write_text(json.dumps(data))
    LOG.info("[arrival saved] %s", data)


def load(max_days: int = 7) -> dict | None:
    """
    Return the arrival coord‐dict *if* it is not stale.

    Args:
        max_days:  How old (days) we still consider valid.

    Returns:
        {"lat":…, "lon":…, "name": "Last known (jet arrival)"} or *None*.
    """
    if not FILE.exists():
        return None

    try:
        data: Arrival = json.loads(FILE.read_text())
        ts = dt.datetime.fromisoformat(data["ts"])
        # Use total_seconds() for precise age comparison (not .days which rounds down)
        age_seconds = (dt.datetime.now(UTC) - ts).total_seconds()
        if age_seconds > max_days * 86400:
            return None

        # Calculate confidence with time-based decay
        # Start at 30, decay 3 points/day, floor at 10
        age_days = age_seconds / 86400
        confidence = max(10, 30 - int(age_days * 3))

        return {
            "lat": data["lat"],
            "lon": data["lon"],
            "name": "Last known (jet arrival)",
            "reason": "last_known",
            "confidence": confidence,
        }
    except Exception as exc:  # noqa: BLE001 – corrupted file?
        LOG.warning("[arrival_cache] %s", exc)
        return None
