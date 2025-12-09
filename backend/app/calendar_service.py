"""
calendar_service.py
~~~~~~~~~~~~~~~~~~~
Retrieve Donald Trump’s public schedule from the **Factba.se JSON feed**
and pick the most relevant entry for our location pipeline.

Key behaviour
-------------
* The feed (<https://media-cdn.factba.se/rss/json/trump/calendar-full.json>)
  is cached in-process for 15 min.
* When choosing an event we **prefer the first one (chronologically) in the
  look-back / look-ahead window that has a *non-empty* `location` field**.
  If none have a location we fall back to the time-nearest entry (original
  behaviour).
* Local times published by Factba.se are assumed **America/New_York** and
  converted to UTC.

Public API
----------
    current_event(now=None, past_hours=36, future_hours=24) -> dict | None
        Example returned dict:

        {
            "dtstart_utc": datetime,      # aware, in UTC
            "summary":     "The President arrives The White House",
            "location":    "South Lawn"
        }
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import math
from typing import Final

import httpx
from dateutil import tz

# ── Constants ────────────────────────────────────────────────────────────
from .constants import USER_AGENT

UTC: Final = tz.UTC
NYC: Final = tz.gettz("America/New_York")

# Summaries that imply a known location even when location field is empty
IMPLICIT_LOCATION_SUMMARIES: Final[tuple[str, ...]] = (
    "in-town pool call time",  # Implies White House
)
FEED_URL: Final = "https://media-cdn.factba.se/rss/json/trump/calendar-full.json"
CACHE_SEC: Final = 900  # 15 min


# ── Tiny synchronous TTL-cache decorator ─────────────────────────────────
def _memo(seconds: int = CACHE_SEC):
    def deco(fn):
        cache: dict[tuple, tuple[dt.datetime, object]] = {}

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            ts, val = cache.get(key, (dt.datetime.min.replace(tzinfo=UTC), None))
            if (dt.datetime.now(UTC) - ts).total_seconds() < seconds:
                return val
            val = fn(*args, **kwargs)
            cache[key] = (dt.datetime.now(UTC), val)
            return val

        wrapped.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapped

    return deco


# ── Feed download & normalisation ────────────────────────────────────────
@_memo(CACHE_SEC)
def _fetch_events() -> list[dict[str, object]]:
    """Return a list of normalised schedule items (UTC datetimes)."""
    resp = httpx.get(FEED_URL, timeout=15.0, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    raw_items: list[dict] = json.loads(resp.text)
    events: list[dict[str, object]] = []

    for item in raw_items:
        # Skip the “no public events scheduled” stubs up-front
        summary = str(item.get("details") or "")
        if summary.lower().startswith("the president has no public events"):
            continue

        date_str: str = item["date"]  # "2025-05-30"
        time_str: str = item.get("time") or "00:00:00"
        dt_local = dt.datetime.fromisoformat(f"{date_str}T{time_str}").replace(
            tzinfo=NYC
        )
        events.append(
            {
                "dtstart_utc": dt_local.astimezone(UTC),
                "summary": summary,
                "location": str(item.get("location") or "").strip(),
            }
        )
    return events


# ── Public helper ────────────────────────────────────────────────────────
def _has_effective_location(event: dict) -> bool:
    """
    Check if an event has an effective location.

    An event has an effective location if:
    - It has a non-empty 'location' field, OR
    - Its 'summary' contains an implicit location indicator (e.g., "In-Town Pool Call Time")
    """
    if event.get("location"):
        return True
    summary = str(event.get("summary") or "").lower().strip()
    return any(pattern in summary for pattern in IMPLICIT_LOCATION_SUMMARIES)


def current_event(
    now: dt.datetime | None = None,
    past_hours: float = 36,
    future_hours: float = 24,
) -> dict[str, object] | None:
    """
    Pick the schedule entry that best represents where Trump most likely is.

    Preference rules (applied in order):

    1. **Recent past** window (`past_hours`):
       first event *with an effective location* closest to `now` while going backwards.
       Effective location = explicit location field OR implicit via summary pattern.
       Fallback → closest-in-time even if location empty.
    2. **Upcoming** window (`future_hours`):
       same logic but chronological forward search.
    3. No match → return *None*.
    """
    now = now or dt.datetime.now(UTC)
    events = _fetch_events()

    def _within(event, hours, past=True):
        delta = (now - event["dtstart_utc"]) if past else (event["dtstart_utc"] - now)
        return 0 <= delta.total_seconds() / 3600 <= hours

    # 1️⃣ scan recent past (newest ➜ oldest)
    recent = [e for e in events if _within(e, past_hours, past=True)]
    recent.sort(key=lambda e: now - e["dtstart_utc"])  # newest first
    for ev in recent:
        if _has_effective_location(ev):
            return ev
    if recent:
        return recent[0]  # time-closest even if location empty

    # 2️⃣ scan upcoming (soonest ➜ later)
    upcoming = [e for e in events if _within(e, future_hours, past=False)]
    upcoming.sort(key=lambda e: e["dtstart_utc"] - now)  # soonest first
    for ev in upcoming:
        if _has_effective_location(ev):
            return ev
    if upcoming:
        return upcoming[0]

    return None


# ── Constants for overnight base inference ───────────────────────────────────
# Three known presidential bases with overnight stays
OVERNIGHT_BASES: Final[dict[str, dict]] = {
    "dc": {
        "center": (38.9072, -77.0369),  # Washington DC
        "coords": {"lat": 38.897676, "lon": -77.036529, "name": "The White House"},
    },
    "fl": {
        "center": (26.6758, -80.0364),  # Palm Beach area
        "coords": {"lat": 26.6758, "lon": -80.0364, "name": "Mar-a-Lago"},
    },
    "nj": {
        "center": (40.6456, -74.6392),  # Bedminster area
        "coords": {
            "lat": 40.645560,
            "lon": -74.639170,
            "name": "Trump Nat'l Golf Club Bedminster",
        },
    },
}
OVERNIGHT_RADIUS_KM: Final[float] = 80.0

# Import place_aliases to resolve event locations to coordinates
from .place_aliases import PLACE_ALIASES


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    φ1, φ2 = map(math.radians, (lat1, lat2))
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _get_region(lat: float, lon: float) -> str | None:
    """Return region key ('dc', 'fl', 'nj') if coords are within radius of known bases."""
    for region, data in OVERNIGHT_BASES.items():
        center = data["center"]
        if _haversine_km(lat, lon, center[0], center[1]) < OVERNIGHT_RADIUS_KM:
            return region
    return None


def _resolve_location_to_coords(location: str) -> tuple[float, float] | None:
    """
    Resolve a location string to (lat, lon) using place_aliases.

    Returns None if location can't be resolved.
    """
    if not location:
        return None

    location_lower = location.lower().strip()

    # Check place aliases for a match (substring matching like location_service)
    for key, alias in PLACE_ALIASES.items():
        if key in location_lower:
            return (alias["lat"], alias["lon"])

    return None


def get_overnight_base(now: dt.datetime | None = None) -> dict[str, object] | None:
    """
    Infer overnight location based on evening→morning event pattern.

    Supports THREE bases:
    - DC area → White House
    - Florida (Palm Beach) → Mar-a-Lago
    - New Jersey (Bedminster) → Trump National Golf Club Bedminster

    Returns base coords if:
    - Current time is overnight (9PM-8AM Eastern)
    - Last evening event (after 5PM ET) and next morning event (before noon ET)
      are both in the SAME region

    Returns None if:
    - Not overnight hours
    - Pattern doesn't match (e.g., travel between regions)
    - Insufficient data (no evening or morning event found)
    """
    now = now or dt.datetime.now(UTC)
    now_local = now.astimezone(NYC)
    hour = now_local.hour

    # Only apply during overnight hours (9PM-8AM Eastern)
    if not (hour >= 21 or hour < 8):
        return None

    events = _fetch_events()
    if not events:
        return None

    # Find last evening event (after 5PM ET, within past 14 hours)
    # 14h ensures a 6PM event is still valid at 8AM (end of overnight window)
    evening_cutoff_hour = 17  # 5PM
    evening_event = None
    evening_coords = None

    for ev in events:
        ev_local = ev["dtstart_utc"].astimezone(NYC)
        ev_hour = ev_local.hour
        hours_ago = (now - ev["dtstart_utc"]).total_seconds() / 3600

        # Event must be after 5PM and within past 14 hours
        if ev_hour >= evening_cutoff_hour and 0 < hours_ago <= 14:
            location = ev.get("location", "")
            coords = _resolve_location_to_coords(location)
            if coords:
                evening_event = ev
                evening_coords = coords
                break  # Take the most recent evening event

    if not evening_coords:
        return None

    # Find next morning event (before noon ET, within next 18 hours)
    morning_cutoff_hour = 12  # noon
    morning_event = None
    morning_coords = None

    for ev in events:
        ev_local = ev["dtstart_utc"].astimezone(NYC)
        ev_hour = ev_local.hour
        hours_ahead = (ev["dtstart_utc"] - now).total_seconds() / 3600

        # Event must be before noon and within next 18 hours
        if ev_hour < morning_cutoff_hour and 0 < hours_ahead <= 18:
            location = ev.get("location", "")
            coords = _resolve_location_to_coords(location)
            if coords:
                morning_event = ev
                morning_coords = coords
                break  # Take the soonest morning event

    if not morning_coords:
        return None

    # Check if both events are in the same region
    evening_region = _get_region(*evening_coords)
    morning_region = _get_region(*morning_coords)

    if evening_region and evening_region == morning_region:
        # Same region: return the base coordinates for that region
        return dict(OVERNIGHT_BASES[evening_region]["coords"])

    return None  # Different regions or unknown region = likely traveling


# ── Constants for geocoding context ───────────────────────────────────────────
MIN_CONTEXT_EVENTS: Final[int] = 2  # Minimum nearby resolved events for disambiguation


def get_context_events(
    target_event: dict,
    all_events: list[dict] | None = None,
    min_context: int = MIN_CONTEXT_EVENTS,
) -> list[dict]:
    """
    Get resolved events near target_event with coords AND timestamps.

    Used for geocoding disambiguation: expands outward from target until
    min_context resolved coordinates are found.

    Args:
        target_event: Event needing geocoding (must have dtstart_utc).
        all_events: Full event list (defaults to _fetch_events()).
        min_context: Minimum context events to find before stopping.

    Returns:
        List of {"lat": float, "lon": float, "dt": datetime} dicts,
        sorted by temporal distance from target (closest first).
    """
    if all_events is None:
        all_events = _fetch_events()

    target_dt = target_event["dtstart_utc"]

    # Sort events by temporal distance from target
    sorted_events = sorted(
        all_events,
        key=lambda e: abs((e["dtstart_utc"] - target_dt).total_seconds()),
    )

    context: list[dict] = []
    for ev in sorted_events:
        # Skip the target event itself
        if ev is target_event:
            continue

        # Try to resolve via alias
        location = ev.get("location", "")
        coords = _resolve_location_to_coords(location)
        if coords:
            context.append(
                {
                    "lat": coords[0],
                    "lon": coords[1],
                    "dt": ev["dtstart_utc"],
                }
            )

        if len(context) >= min_context:
            break

    return context
