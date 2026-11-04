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

import bisect
import datetime as dt
import functools
import json
import logging
import math
import threading
import unicodedata
from typing import Callable, Final

import httpx
from dateutil import tz

# ── Constants ────────────────────────────────────────────────────────────
from .constants import USER_AGENT
from . import clock
from .event_service import emit_api_error
from .place_aliases import find_alias

UTC: Final = tz.UTC
NYC: Final = tz.gettz("America/New_York")

# Summaries that imply a known location even when location field is empty
IMPLICIT_LOCATION_SUMMARIES: Final[tuple[str, ...]] = (
    "in-town pool call time",  # Implies White House
)
FEED_URL: Final = "https://media-cdn.factba.se/rss/json/trump/calendar-full.json"
CACHE_SEC: Final = 900  # 15 min
FEED_RETRY_SEC: Final = 60  # back-off after a failed feed fetch

LOG = logging.getLogger("calendar_service")


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

        def age_s() -> float | None:
            """Seconds since the newest cached value was stored (None: empty)."""
            if not cache:
                return None
            newest = max(ts for ts, _ in cache.values())
            return (dt.datetime.now(UTC) - newest).total_seconds()

        def peek():
            """The cached no-argument result if still fresh, else None."""
            ts, val = cache.get(((), ()), (dt.datetime.min.replace(tzinfo=UTC), None))
            if (dt.datetime.now(UTC) - ts).total_seconds() < seconds:
                return val
            return None

        def prime(val) -> None:
            """Store *val* as the cached result of a no-argument call."""
            cache[((), ())] = (dt.datetime.now(UTC), val)

        wrapped.cache_clear = cache.clear  # type: ignore[attr-defined]
        wrapped.age_s = age_s  # type: ignore[attr-defined]
        wrapped.prime = prime  # type: ignore[attr-defined]
        wrapped.peek = peek  # type: ignore[attr-defined]
        return wrapped

    return deco


# ── Feed download & normalisation ────────────────────────────────────────
#: Serve the last good feed this long while Factba.se is failing
STALE_MAX_SEC: Final = 24 * 3600
_FEED_ERRORS: Final = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError)

_feed_failed_at: dt.datetime | None = None
#: Last successful download and when it happened (stale-while-error)
_last_good: tuple[list[dict[str, object]], dt.datetime] | None = None
#: Single-flight downloads; never held on the cache-hit path
_DOWNLOAD_LOCK = threading.Lock()


def _stale_or_empty(now: dt.datetime) -> list[dict[str, object]]:
    """The last good feed while it is younger than :data:`STALE_MAX_SEC`."""
    if _last_good and (now - _last_good[1]).total_seconds() <= STALE_MAX_SEC:
        return _last_good[0]
    return []


def _in_backoff(now: dt.datetime) -> bool:
    return bool(
        _feed_failed_at and (now - _feed_failed_at).total_seconds() < FEED_RETRY_SEC
    )


def _refresh(blocking: bool) -> list[dict[str, object]]:
    """Download the feed (single-flight) and publish it.

    Returns the fresh feed, or the last good one (≤ 24 h) when the download
    fails, is in its back-off, or another thread is downloading and
    *blocking* is False.
    """
    global _feed_failed_at, _last_good
    now = dt.datetime.now(UTC)
    if _in_backoff(now):
        return _stale_or_empty(now)
    if not _DOWNLOAD_LOCK.acquire(blocking=blocking):
        return _stale_or_empty(now)
    try:
        cached = _download_events.peek()  # type: ignore[attr-defined]
        if cached is not None and blocking:
            return cached  # another thread refreshed while we waited
        try:
            events = _download_events.__wrapped__()  # type: ignore[attr-defined]
        except _FEED_ERRORS as exc:
            _feed_failed_at = now
            stale = _stale_or_empty(now)
            LOG.warning(
                "Calendar feed unavailable (%r); serving %s",
                exc,
                f"the last good copy ({len(stale)} items)" if stale else "no events",
            )
            emit_api_error("factba.se", str(exc))
            return stale
        _download_events.prime(events)  # type: ignore[attr-defined]
        _last_good = (events, now)
        _feed_failed_at = None
        return events
    finally:
        _DOWNLOAD_LOCK.release()


def _fetch_events() -> list[dict[str, object]]:
    """Return normalised schedule items (cached for ``CACHE_SEC``).

    A Factba.se outage, timeout or malformed body must never break the location
    pipeline: the last good feed keeps being served for up to 24 h
    (stale-while-error, logged and reported as an ``api_error``), else ``[]``.
    After a failure the feed is not retried for ``FEED_RETRY_SEC``. A cache hit
    takes no lock; a miss waits for a download only when there is no
    last-good copy to serve meanwhile.
    """
    cached = _download_events.peek()  # type: ignore[attr-defined]
    if cached is not None:
        return cached
    return _refresh(blocking=_last_good is None)


def get_events() -> list[dict[str, object]]:
    """All normalised schedule items (blocking: call it off the event loop)."""
    return _fetch_events()


def prefetch_events(margin_s: float = 120.0) -> None:
    """Refresh the feed shortly before the cache expires (background loop), so
    requests don't block on the download. Honours the failure back-off and
    never waits for a download already in progress. Blocking (it downloads):
    call it off the event loop."""
    age = _download_events.age_s()  # type: ignore[attr-defined]
    if age is not None and age < CACHE_SEC - margin_s:
        return
    _refresh(blocking=False)


#: Sorted index of the last feed list seen by :func:`events_between`
_index: tuple[list, list, list] | None = None


def events_between(events: list[dict], lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    """Items of *events* starting in [lo, hi], via a sorted index that is
    rebuilt only when a new feed list comes in (once per download)."""
    global _index
    if _index is None or _index[0] is not events:
        ordered = sorted(events, key=lambda e: e["dtstart_utc"])
        _index = (events, ordered, [e["dtstart_utc"] for e in ordered])
    _, ordered, keys = _index
    return ordered[bisect.bisect_left(keys, lo) : bisect.bisect_right(keys, hi)]


def _clear_feed_cache() -> None:
    """Drop cached events, the last good copy and any failure back-off."""
    global _feed_failed_at, _last_good
    _feed_failed_at = None
    _last_good = None
    _download_events.cache_clear()  # type: ignore[attr-defined]


_fetch_events.cache_clear = _clear_feed_cache  # type: ignore[attr-defined]


@_memo(CACHE_SEC)
def _download_events() -> list[dict[str, object]]:
    """Download and normalise the feed (raises on network or parse errors)."""
    resp = httpx.get(FEED_URL, timeout=15.0, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    raw_items = json.loads(resp.text)
    if not isinstance(raw_items, list):
        raise ValueError(f"unexpected calendar payload: {type(raw_items).__name__}")
    events: list[dict[str, object]] = []

    for item in raw_items:
        try:
            if not isinstance(item, dict):
                raise TypeError("item is not an object")
            # Skip the “no public events scheduled” stubs up-front
            summary = str(item.get("details") or "")
            if summary.lower().startswith("the president has no public events"):
                continue

            date_str: str = item["date"]  # "2025-05-30"
            raw_time = item.get("time")
            time_str: str = raw_time or "00:00:00"
            dt_local = dt.datetime.fromisoformat(f"{date_str}T{time_str}").replace(
                tzinfo=NYC
            )
            events.append(
                {
                    "dtstart_utc": dt_local.astimezone(UTC),
                    "summary": summary,
                    "location": str(item.get("location") or "").strip(),
                    "has_time": bool(raw_time),
                }
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            LOG.warning("Skipping malformed calendar item %r: %r", item, exc)
    return events


# ── Public helper ────────────────────────────────────────────────────────
def _has_effective_location(event: dict) -> bool:
    """
    Check if an event has an effective location.

    An event has an effective location if:
    - It has a non-empty 'location' field, OR
    - Its 'summary' contains an implicit location indicator
      (e.g., "In-Town Pool Call Time")
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
    now = now or clock.now()
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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    φ1, φ2 = map(math.radians, (lat1, lat2))
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _get_region(lat: float, lon: float) -> str | None:
    """Return region key ('dc', 'fl', 'nj') if coords are within radius of
    known bases."""
    for region, data in OVERNIGHT_BASES.items():
        center = data["center"]
        if _haversine_km(lat, lon, center[0], center[1]) < OVERNIGHT_RADIUS_KM:
            return region
    return None


_TRANSLATE: Final = str.maketrans(
    {"\u00a0": " ", "\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-"}
)


def _clean(text: str) -> str:
    """Same normalisation as ``location_service._clean`` (kept local to avoid
    a circular import)."""
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATE).strip().lower()


def _resolve_location_to_coords(location: str) -> tuple[float, float] | None:
    """
    Resolve a location string to (lat, lon) using place_aliases.

    Returns None if location can't be resolved.
    """
    if not location:
        return None

    location_lower = _clean(location)

    # Check place aliases for a match (substring matching like location_service)
    alias = find_alias(location_lower)
    if alias:
        return (alias["lat"], alias["lon"])

    return None


def get_overnight_base(
    now: dt.datetime | None = None,
    events: list[dict] | None = None,
    resolve: Callable[[str], tuple[float, float] | None] | None = None,
) -> dict[str, object] | None:
    """
    Infer overnight location based on evening→morning event pattern.

    Supports THREE bases:
    - DC area → White House
    - Florida (Palm Beach) → Mar-a-Lago
    - New Jersey (Bedminster) → Trump National Golf Club Bedminster

    Returns base coords (plus ``evening_ts``, the start of the evening event)
    if:
    - Current time is overnight (9PM-8AM Eastern)
    - The newest *located* evening event (after 5PM ET, past 14 h) and the
      soonest located morning event (before noon ET, next 18 h) both resolve
      to the SAME base region

    Returns None if:
    - Not overnight hours
    - The newest located evening event does not resolve, or is not in a base
      region (e.g. an evening rally elsewhere: he may still be there or
      flying back), even if an older evening event was at a base
    - Pattern doesn't match (e.g., travel between regions)
    - Insufficient data (no evening or morning event found)

    Args:
        now: Reference time (defaults to ``clock.now()``).
        events: Schedule items (defaults to the feed).
        resolve: Location string → (lat, lon); defaults to aliases only.
    """
    now = now or clock.now()
    now_local = now.astimezone(NYC)
    hour = now_local.hour

    # Only apply during overnight hours (9PM-8AM Eastern)
    if not (hour >= 21 or hour < 8):
        return None

    if events is None:
        events = _fetch_events()
    if not events:
        return None
    resolve = resolve or _resolve_location_to_coords

    # Timed, located events only: date-only items default to 00:00 and carry
    # no real time; location-less items place no one
    timed = [e for e in events if e.get("has_time", True) and e.get("location")]

    # Newest located evening event (after 5PM ET, within past 14 hours)
    # 14h ensures a 6PM event is still valid at 8AM (end of overnight window)
    evening_cutoff_hour = 17  # 5PM
    evening = [
        e
        for e in timed
        if e["dtstart_utc"].astimezone(NYC).hour >= evening_cutoff_hour
        and 0 < (now - e["dtstart_utc"]).total_seconds() / 3600 <= 14
    ]
    if not evening:
        return None
    last_evening = max(evening, key=lambda e: e["dtstart_utc"])
    evening_coords = resolve(str(last_evening.get("location", "")))
    if not evening_coords:
        return None

    # Soonest located morning event (before noon ET, within next 18 hours)
    morning_cutoff_hour = 12  # noon
    morning = [
        e
        for e in timed
        if e["dtstart_utc"].astimezone(NYC).hour < morning_cutoff_hour
        and 0 < (e["dtstart_utc"] - now).total_seconds() / 3600 <= 18
    ]
    if not morning:
        return None
    first_morning = min(morning, key=lambda e: e["dtstart_utc"])
    morning_coords = resolve(str(first_morning.get("location", "")))
    if not morning_coords:
        return None

    # Check if both events are in the same region
    evening_region = _get_region(*evening_coords)
    morning_region = _get_region(*morning_coords)

    if evening_region and evening_region == morning_region:
        # Same region: return the base coordinates for that region
        return {
            **OVERNIGHT_BASES[evening_region]["coords"],
            "region": evening_region,
            "evening_ts": last_evening["dtstart_utc"],
        }

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
