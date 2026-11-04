"""location_service.py
~~~~~~~~~~~~~~~~~~~~~~
Determine Donald Trump’s most likely location **right now**.

2025‑06 refresh
---------------
* **Plane vs Calendar** — plane wins *only* when its timestamp is newer
  than the calendar event (audit finding #3).
* FAA HTML table removed; JSON feed is used exclusively.
* SECURITY‑labelled TFRs are optionally considered when they look
  Trump‑related (audit finding #5).
* External HTTP calls are now logged via :pyfile:`api_logging.py`.

2025-12 TFR API Deprecation
---------------------------
* **FAA TFR API Broken** - As of late 2025, the FAA changed their TFR
  endpoint (tfr.faa.gov/tfr3/export/json) from a JSON API to a JavaScript
  SPA. The endpoint now returns HTML instead of JSON, breaking the API.
* TFR functionality is **disabled** but code is preserved for potential
  future restoration with NASA DIP API (dip.amesaero.nasa.gov) which
  provides NOTAMs data via REST. Note: NASA docs indicate TFRs in NOTAMs
  often lack geometry data; may need to merge with other sources.
* Impact: Grounded aircraft no longer get +10 confidence boost from TFR
  proximity. Flight tracking (ADS-B via adsb.fi/adsb.lol) is unaffected.
* See: https://ntrs.nasa.gov/citations/20250003355 for NASA DIP API docs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Final, List, Optional, Tuple, Union

import httpx
from dateutil import tz
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

from .api_logging import logged_request_async
from . import arrival_cache, clock, executors, location_fusion
from .event_service import (
    LOW_IMPORTANCE_THRESHOLD,
    emit_all_results_infeasible,
    emit_flight_detected,
    emit_geocode_failure,
    emit_landing_detected,
    emit_location_changed,
    emit_low_confidence,
    emit_low_importance_geocode,
    emit_suspicious_geocode,
)
from .fleet import FLEET, airframe_of, home_base_of, is_af1_callsign
from .flight_service import get_fleet_states
from .gdelt_service import get_latest_location
from . import calendar_service as cal
from .place_aliases import find_alias
from .geocode_log_service import add_geocode_entry

# ── Constants ─────────────────────────────────────────────────────────────
from .constants import USER_AGENT

UTC: tz.tzutc = tz.UTC

FACTBASE_URL: str = "https://rollcall.com/factbase/trump/topic/calendar/"

# TFR API Configuration
# NOTE: FAA TFR API is DISABLED as of 2025-12. The endpoint now returns HTML
# instead of JSON. Set TFR_ENABLED=True to re-enable when a replacement API
# is integrated (e.g., NASA DIP at dip.amesaero.nasa.gov).
TFR_ENABLED = False  # DISABLED - FAA API broken (returns HTML SPA)
TFR_JSON_URL = "https://tfr.faa.gov/tfr3/export/json"
VIP_RE = re.compile(r"VIP", re.I)
SECURITY_RE = re.compile(r"SECURITY", re.I)
COORD_RE = re.compile(r"([NS]\d+\.\d+),\s*([EW\-]?\d+\.\d+)")

R_EARTH_KM = 6_371.0
CAL_BASE_CONF = 70
CAL_MIN_CONF = 30
CAL_WINDOW_H = 72.0  # hours (decay span; current_event() only looks back 36 h)

# Physical feasibility constants for geocoding disambiguation
MAX_TRAVEL_SPEED_KMH = 800.0  # Generous estimate for Air Force One effective speed
MIN_TIME_GAP_H = 0.1  # Minimum 6 minutes to avoid division issues
SUSPICIOUS_DISTANCE_KM = 500.0  # Alert if best result is this far from context

TRANSLATE = str.maketrans(
    {"\u00a0": " ", "\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-"}
)

# ── Skip list for non-geocodable locations ────────────────────────────────
# Locations that shouldn't be geocoded (based on empirical analysis of calendar)
SKIP_LOCATIONS: set[str] = {
    "stakeout location",
    "the sticks - the white house",
}

# ── State tracking for Discord events ─────────────────────────────────────
# Track previous states to emit events only on changes
_prev_flight_status: str | None = None  # "airborne" | "grounded" | None
_prev_location_reason: str | None = None  # reason field from last coords
_is_initializing: bool = True  # Suppress events during startup


def _emit_state_change_events(
    coords: dict,
    plane_state: dict | None,
) -> None:
    """
    Emit Discord events for state changes.

    Compares current state to previous state and emits:
    - flight_detected: when aircraft transitions to airborne
    - landing_detected: when aircraft transitions to grounded
    - location_changed: when winning location source changes

    During initialization (_is_initializing=True), updates state tracking
    variables but skips event emission to avoid startup noise.
    """
    global _prev_flight_status, _prev_location_reason

    # During initialization, just update state without emitting events
    if _is_initializing:
        if plane_state:
            _prev_flight_status = plane_state.get("status")
        _prev_location_reason = coords.get("reason")
        return

    # ── Flight state transitions ──
    if plane_state:
        curr_status = plane_state.get("status")

        if curr_status == "airborne" and _prev_flight_status != "airborne":
            emit_flight_detected(
                callsign=plane_state.get("callsign", "Unknown"),
                lat=plane_state.get("lat", 0),
                lon=plane_state.get("lon", 0),
                altitude=plane_state.get("altitude", 0),
                source="adsb.fi/adsb.lol",
            )

        elif curr_status == "grounded" and _prev_flight_status == "airborne":
            emit_landing_detected(
                callsign=plane_state.get("callsign", "Unknown"),
                lat=plane_state.get("lat", 0),
                lon=plane_state.get("lon", 0),
                location_name=coords.get("name"),
            )

        _prev_flight_status = curr_status

    # ── Location source transitions ──
    curr_reason = coords.get("reason")
    if curr_reason and curr_reason != _prev_location_reason:
        emit_location_changed(
            from_reason=_prev_location_reason,
            to_reason=curr_reason,
            location_name=coords.get("name", "Unknown"),
            confidence=coords.get("confidence", 0),
            lat=coords.get("lat"),
            lon=coords.get("lon"),
            event_summary=coords.get("event_summary"),
        )
        _prev_location_reason = curr_reason

    # ── Low confidence warning ──
    confidence = coords.get("confidence", 0)
    emit_low_confidence(
        confidence=confidence,
        location_name=coords.get("name", "Unknown"),
        reason=curr_reason or "unknown",
        source=coords.get("source_display"),
    )


LOG = logging.getLogger("location_service")


def mark_initialization_complete() -> None:
    """Mark initialization as complete, enabling state change events.

    Call this after the first call to current_coords() during startup
    to enable Discord event emission for subsequent state changes.
    """
    global _is_initializing
    _is_initializing = False
    LOG.info("Location service initialization complete, events enabled")


# ── Helper functions ─────────────────────────────────────────────────────


def select_highest_confidence(candidates: list[dict]) -> dict | None:
    """
    Select the candidate with the highest confidence score.

    Args:
        candidates: List of location candidates with 'confidence' key.

    Returns:
        The candidate with highest confidence, or None if list is empty.
        On tie, first candidate wins (stable behavior).
    """
    if not candidates:
        return None

    return max(candidates, key=lambda c: c.get("confidence", 0))


def _clean(text: str) -> str:
    """Normalise Unicode, squash NBSP & fancy dashes, strip + lower‑case."""

    return unicodedata.normalize("NFKC", text).translate(TRANSLATE).strip().lower()


def parse_tfr_coordinates(description: str) -> dict[str, float] | None:
    """
    Parse and validate coordinates from TFR description.

    Args:
        description: TFR description text containing coordinates.

    Returns:
        Dict with 'lat' and 'lon' keys, or None if invalid/not found.

    Validation:
        - Latitude must be between -90 and 90
        - Longitude must be between -180 and 180
        - Coordinates must match format: N40.7, W74.0 (or S/E)
    """
    import logging

    # Match coordinates like "N40.7128, W74.0060"
    m = COORD_RE.search(description)
    if not m:
        return None

    try:
        # Parse latitude (group 1: like "N40.7128")
        lat_str = m.group(1)
        lat_value = float(lat_str[1:])  # Remove direction letter

        # Apply sign based on direction (N=positive, S=negative)
        if lat_str[0] == "S":
            lat_value = -lat_value
        elif lat_str[0] != "N":
            return None  # Invalid direction

        # Parse longitude (group 2: like "W74.0060" or "E151.2093")
        lon_str = m.group(2)
        # Handle both "W74" and "-74" formats
        if lon_str[0] in ("W", "E"):
            lon_value = float(lon_str[1:])
            if lon_str[0] == "W":
                lon_value = -lon_value
        else:
            lon_value = float(lon_str)  # Already has sign

        # Validate bounds
        if not (-90 <= lat_value <= 90):
            logging.getLogger("location_service").warning(
                "Invalid latitude in TFR description: %s (lat=%.2f)",
                description,
                lat_value,
            )
            return None

        if not (-180 <= lon_value <= 180):
            logging.getLogger("location_service").warning(
                "Invalid longitude in TFR description: %s (lon=%.2f)",
                description,
                lon_value,
            )
            return None

        return {"lat": lat_value, "lon": lon_value}

    except (ValueError, IndexError) as e:
        logging.getLogger("location_service").warning(
            "Failed to parse TFR coordinates from '%s': %s",
            description,
            e,
        )
        return None


def _age_h(ts_utc: dt.datetime) -> float:
    """Return age in hours for a UTC timestamp."""

    return (dt.datetime.now(UTC) - ts_utc).total_seconds() / 3600.0


def _age_human(age_h: float) -> str:
    """Human‑friendly age string — “just now”, “3 h ago”, “2 d ago”."""

    if age_h < 1:
        return "just now"
    if age_h < 24:
        return f"{int(age_h)} h ago"
    return f"{int(round(age_h / 24))} d ago"


def _stamp(
    coords: dict,
    *,
    age_h: float | None = None,
    source: str = "",
    url: str | None = None,
) -> dict:
    """Attach provenance fields to *coords* and return the same dict."""

    coords["source_display"] = (
        f"{source}, {_age_human(age_h)}" if age_h is not None else source
    )
    if url:
        coords["source_url"] = url
    return coords


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great‑circle distance (km) between *lat1/lon1* and *lat2/lon2*."""

    φ1, φ2 = map(math.radians, (lat1, lat2))
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * R_EARTH_KM * math.asin(math.sqrt(a))


def _is_physically_feasible(
    result_lat: float,
    result_lon: float,
    context_events: list[dict],
    target_dt: dt.datetime,
    max_speed_kmh: float = MAX_TRAVEL_SPEED_KMH,
) -> bool:
    """
    Check if a location is physically reachable from any context event.

    Uses generous speed estimate (800 km/h by default) to avoid false positives.
    Returns True if reachable from at least one context event.

    Args:
        result_lat: Latitude of the geocode result to check.
        result_lon: Longitude of the geocode result to check.
        context_events: List of nearby resolved events with coords and timestamps.
            Each dict must have {"lat": float, "lon": float, "dt": datetime}.
        target_dt: Timestamp of the event being geocoded.
        max_speed_kmh: Maximum travel speed in km/h (default: 800 for AF1).

    Returns:
        True if the location is reachable from at least one context event,
        or if context_events is empty (no data to contradict).
    """
    if not context_events:
        return True  # No context → can't determine infeasibility

    for ctx in context_events:
        time_gap_h = abs((ctx["dt"] - target_dt).total_seconds() / 3600)
        if time_gap_h < MIN_TIME_GAP_H:
            time_gap_h = MIN_TIME_GAP_H  # Minimum threshold to avoid edge cases

        max_distance_km = time_gap_h * max_speed_kmh
        actual_distance_km = _haversine_km(
            ctx["lat"], ctx["lon"], result_lat, result_lon
        )

        if actual_distance_km <= max_distance_km:
            return True  # Reachable from this context event

    return False  # Unreachable from all context events


def _compute_centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    """
    Compute geographic centroid of coordinate list.

    Args:
        coords: List of (lat, lon) tuples.

    Returns:
        (lat, lon) tuple representing the centroid.
        Returns (0.0, 0.0) if coords is empty.
    """
    if not coords:
        return (0.0, 0.0)
    avg_lat = sum(c[0] for c in coords) / len(coords)
    avg_lon = sum(c[1] for c in coords) / len(coords)
    return (avg_lat, avg_lon)


def _disambiguate_results(
    results: list,
    context_events: list[dict],
    target_dt: dt.datetime,
) -> tuple[object, dict | None]:
    """
    Apply hybrid 3-layer disambiguation to geocoding results.

    Layer 1: Filter physically infeasible results
    Layer 2: Rank by proximity to context centroid
    Layer 3: Flag suspicious distances (>500km from context)

    Args:
        results: List of geopy Location objects from Nominatim.
        context_events: Nearby resolved events with {"lat", "lon", "dt"}.
        target_dt: Timestamp of the event being geocoded.

    Returns:
        Tuple of (best_result, alert_dict or None).
        alert_dict has keys: type, distance_km, query, etc.
    """
    if not results:
        return None, None

    # Single result: use as-is
    if len(results) == 1:
        result = results[0]
        # Check Layer 3 even for single result if we have context
        if context_events:
            context_coords = [(ev["lat"], ev["lon"]) for ev in context_events]
            centroid = _compute_centroid(context_coords)
            distance_km = _haversine_km(
                centroid[0], centroid[1], result.latitude, result.longitude
            )
            if distance_km > SUSPICIOUS_DISTANCE_KM:
                return result, {
                    "type": "suspicious_distance",
                    "distance_km": distance_km,
                    "centroid": centroid,
                }
        return result, None

    # No context: fall back to highest importance
    if not context_events:
        best = max(results, key=lambda r: r.raw.get("importance", 0))
        return best, None

    # === LAYER 1: Physical Feasibility Filter ===
    feasible_results = [
        r
        for r in results
        if _is_physically_feasible(r.latitude, r.longitude, context_events, target_dt)
    ]

    if not feasible_results:
        # All results are physically impossible!
        best = max(results, key=lambda r: r.raw.get("importance", 0))
        return best, {
            "type": "all_infeasible",
            "results_count": len(results),
        }

    # === LAYER 2: Context Proximity Ranking ===
    context_coords = [(ev["lat"], ev["lon"]) for ev in context_events]
    centroid = _compute_centroid(context_coords)
    best = min(
        feasible_results,
        key=lambda r: _haversine_km(centroid[0], centroid[1], r.latitude, r.longitude),
    )

    # === LAYER 3: Suspicious Distance Alert ===
    distance_km = _haversine_km(centroid[0], centroid[1], best.latitude, best.longitude)
    if distance_km > SUSPICIOUS_DISTANCE_KM:
        return best, {
            "type": "suspicious_distance",
            "distance_km": distance_km,
            "centroid": centroid,
        }

    return best, None


def _likely_trump_tfr(rec: dict) -> bool:
    """Heuristic: is a SECURITY TFR likely linked to Trump?"""

    descr = (rec.get("description") or "").lower()
    place = (rec.get("shortDesc") or "").lower()
    keywords = ("palm beach", "bedminster", "morristown", "white house", "trump")
    if any(k in place for k in keywords):
        return True
    if "secret service" in descr or "usss" in descr:
        return True
    return False


# ── Geocoder (polite rate‑limited) ───────────────────────────────────────
_nominatim = Nominatim(user_agent=USER_AGENT)
# Errors must surface (not come back as "no result"), and are never retried
# inside a batch: the background loop simply tries again later
_geocode_raw = RateLimiter(
    _nominatim.geocode, min_delay_seconds=1, max_retries=0, swallow_exceptions=False
)

_geocode_log = logging.getLogger("location_service.geocode")


def _should_skip_geocode(location: str) -> bool:
    """True for skip-listed strings and generic phrases ("his golf club")."""
    cleaned = _clean(location)
    return cleaned in SKIP_LOCATIONS or location_fusion.is_generic_place(cleaned)


# ── Persistent geocode cache ─────────────────────────────────────────────
# Positive results (all candidates of a query) are kept in the persist dir
# for GEOCODE_CACHE_TTL_S, keyed by the cleaned query, and are NOT cleared by
# /debug. Geocoding happens in the background loop (pregeocode_events); the
# request path only reads this cache.
GEOCODE_CACHE_TTL_S: Final = 30 * 86400  # 30 days
GEOCODE_CACHE_NAME: Final = "geocode_cache.json"
GEOCODE_CANDIDATES: Final = 5
_geocode_lock = threading.RLock()
_geocode_cache: dict[str, dict[str, Any]] = {}  # in-memory mirror of the file
_geocode_cache_path: Path | None = None


def _persist_dir() -> Path:
    try:
        from .push_service import PERSIST_DIR  # looked up at call time (tests)

        return Path(PERSIST_DIR)
    except Exception:  # noqa: BLE001 – fall back next to the flight history
        return arrival_cache.FILE.parent


def _geocode_store() -> dict[str, dict[str, Any]]:
    """The in-memory cache, (re)loaded when the persist dir changed."""
    global _geocode_cache_path
    path = _persist_dir() / GEOCODE_CACHE_NAME
    if path != _geocode_cache_path:
        _geocode_cache.clear()
        try:
            data = json.loads(path.read_text())
            entries = data.get("entries", {}) if isinstance(data, dict) else {}
            _geocode_cache.update(
                {
                    k: v
                    for k, v in entries.items()
                    if isinstance(v, dict) and _fresh(v.get("ts"), GEOCODE_CACHE_TTL_S)
                }
            )
            negative = data.get("negative", {}) if isinstance(data, dict) else {}
            for key, ts in negative.items():
                if _fresh(ts, GEOCODE_NEG_TTL_S):
                    _geocode_neg_cache[key] = dt.datetime.fromisoformat(ts)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 – corrupt cache: start afresh
            _geocode_log.warning("Unreadable geocode cache %s: %s", path, exc)
        _geocode_cache_path = path
    return _geocode_cache


def _fresh(ts: Any, ttl_s: float) -> bool:
    """True if ISO timestamp *ts* is younger than *ttl_s*."""
    try:
        age = dt.datetime.now(UTC) - dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return False
    return age.total_seconds() <= ttl_s


def _save_geocode_store() -> None:
    """Persist fresh positive entries and genuine negative results (expired
    entries are pruned on every save)."""
    for key in [
        k
        for k, v in _geocode_cache.items()
        if not _fresh(v.get("ts"), GEOCODE_CACHE_TTL_S)
    ]:
        del _geocode_cache[key]
    _prune_negative()
    negative = {k: ts.isoformat() for k, ts in _geocode_neg_cache.items()}
    path = _persist_dir() / GEOCODE_CACHE_NAME
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(
                {"version": 1, "entries": _geocode_cache, "negative": negative}, fh
            )
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cached_candidates(query: str) -> list[dict[str, Any]] | None:
    """Cached geocode candidates for *query* (None if not cached / expired)."""
    key = _clean(query)
    with _geocode_lock:
        entry = _geocode_store().get(key)
    if not entry:
        return None
    try:
        age = (
            dt.datetime.now(UTC) - dt.datetime.fromisoformat(entry["ts"])
        ).total_seconds()
    except Exception:  # noqa: BLE001
        return None
    if age > GEOCODE_CACHE_TTL_S:
        return None
    return list(entry.get("candidates") or []) or None


_geocode_version = 0  # bumped on every cache write (decision memo key)


def _put_candidates(query: str, candidates: list[dict[str, Any]]) -> None:
    global _geocode_version
    with _geocode_lock:
        _geocode_version += 1
        _geocode_store()[_clean(query)] = {
            "ts": dt.datetime.now(UTC).isoformat(),
            "query": query,
            "candidates": candidates,
        }
        try:
            _save_geocode_store()
        except OSError as exc:
            _geocode_log.warning("Could not persist geocode cache: %s", exc)


# Negative cache: genuine empty results are not retried for 12 h (persisted
# with the positive cache); geocoder errors only back off briefly and are
# never cached as a result.
_geocode_neg_cache: dict[str, dt.datetime] = {}
_geocode_err_cache: dict[str, dt.datetime] = {}
GEOCODE_NEG_TTL_S: Final = 12 * 3600  # failures are rarely fixed sooner
GEOCODE_ERROR_TTL_S: Final = 300  # outage / rate limit: retry soon


def _prune_negative() -> None:
    now = dt.datetime.now(UTC)
    for cache, ttl in (
        (_geocode_neg_cache, GEOCODE_NEG_TTL_S),
        (_geocode_err_cache, GEOCODE_ERROR_TTL_S),
    ):
        for key in [k for k, ts in cache.items() if (now - ts).total_seconds() >= ttl]:
            del cache[key]


def _neg_cached(query: str) -> bool:
    """True if *query* had no result recently, or errored very recently."""
    with _geocode_lock:
        _geocode_store()  # loads persisted negatives after a restart
    key = _clean(query)
    now = dt.datetime.now(UTC)
    for cache, ttl in (
        (_geocode_neg_cache, GEOCODE_NEG_TTL_S),
        (_geocode_err_cache, GEOCODE_ERROR_TTL_S),
    ):
        ts = cache.get(key)
        if ts is None:
            continue
        if (now - ts).total_seconds() < ttl:
            return True
        cache.pop(key, None)
    return False


def _set_neg_cached(query: str) -> None:
    """Remember a genuine empty result (persisted)."""
    with _geocode_lock:
        _geocode_store()
        _prune_negative()
        _geocode_neg_cache[_clean(query)] = dt.datetime.now(UTC)
        try:
            _save_geocode_store()
        except OSError as exc:
            _geocode_log.warning("Could not persist geocode cache: %s", exc)


def _set_error_backoff(query: str) -> None:
    """Back off briefly after a geocoder error (never cached as a result)."""
    _prune_negative()
    _geocode_err_cache[_clean(query)] = dt.datetime.now(UTC)


def _as_list(result: Any) -> list[Any]:
    """Normalise a geopy answer (None, one Location, or a list) to a list."""
    if not result:
        return []
    if isinstance(result, (list, tuple)):
        return [r for r in result if r]
    return [result]


def _candidate(result: Any, scope: str) -> dict[str, Any]:
    raw = getattr(result, "raw", None) or {}
    addr = raw.get("address", {}) if isinstance(raw, dict) else {}
    return {
        "lat": float(result.latitude),
        "lon": float(result.longitude),
        "name": str(getattr(result, "address", "") or ""),
        "country_code": str(
            addr.get("country_code") or ("us" if scope == "us" else "")
        ),
        "country": str(addr.get("country") or ""),
        "state": str(addr.get("state") or ""),
        "importance": raw.get("importance") if isinstance(raw, dict) else None,
        "scope": scope,
    }


def _geocode_candidates(query: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Geocode *query* to up to 5 US and 5 worldwide candidates (cached).

    Both searches always run (the worldwide one first), so a foreign venue
    with a US namesake ("Elysee Palace, Paris") keeps its real candidate; the
    fusion picks among them using the trip's context. US candidates are
    listed first. Results are cached persistently; failures negatively.
    """
    if _should_skip_geocode(query):
        _geocode_log.debug("Skipped (in skip list): %r", query)
        add_geocode_entry(query=query, result_type="skipped")
        return []
    cached = cached_candidates(query)
    if cached is not None:
        return cached
    if _neg_cached(query):
        _geocode_log.debug("Negative cache hit for: %r", query)
        return []

    found: dict[str, list[dict[str, Any]]] = {"intl": [], "us": []}
    errors: list[str] = []
    for scope, extra in (("intl", {}), ("us", {"country_codes": "us"})):
        try:
            results = _geocode_raw(
                query,
                timeout=timeout,
                addressdetails=True,
                exactly_one=False,
                limit=GEOCODE_CANDIDATES,
                **extra,
            )
            found[scope] = [_candidate(r, scope) for r in _as_list(results)]
        except Exception as e:  # noqa: BLE001
            _geocode_log.warning("%s geocode failed for %r: %s", scope, query, e)
            add_geocode_entry(query=query, result_type="error", error=str(e))
            errors.append(str(e))

    candidates: list[dict[str, Any]] = []
    for cand in found["us"] + found["intl"]:
        if all(
            _haversine_km(cand["lat"], cand["lon"], c["lat"], c["lon"]) > 1.0
            for c in candidates
        ):
            candidates.append(cand)

    if errors:
        # A failed scope means the answer is incomplete: use what came back
        # now, but never cache it (positively or negatively); retry soon.
        _set_error_backoff(query)
        if not candidates:
            emit_geocode_failure(query=query, result_type="error", error=errors[-1])
        return candidates

    if not candidates:
        _geocode_log.warning("Geocode returned no results for: %r", query)
        add_geocode_entry(query=query, result_type="no_result")
        emit_geocode_failure(query=query, result_type="no_result")
        _set_neg_cached(query)
        return []

    top = candidates[0]
    us = top["scope"] == "us"
    _geocode_log.info(
        "Geocoded (%s, %d candidates): %r → %.4f, %.4f [importance=%.3f]",
        "US" if us else "international",
        len(candidates),
        query,
        top["lat"],
        top["lon"],
        top["importance"] or 0,
    )
    add_geocode_entry(
        query=query,
        result_type="us" if us else "international",
        lat=top["lat"],
        lon=top["lon"],
        display_name=top["name"],
        country="United States" if us else top["country"],
        state=top["state"],
        importance=top["importance"],
    )
    importance = top["importance"]
    if importance is not None and importance < LOW_IMPORTANCE_THRESHOLD:
        emit_low_importance_geocode(
            query=query,
            importance=importance,
            lat=top["lat"],
            lon=top["lon"],
            display_name=top["name"],
        )
    _put_candidates(query, candidates)
    return candidates


def _smart_geocode(
    query: str,
    timeout: int = 10,
    context_events: list[dict] | None = None,
    target_dt: dt.datetime | None = None,
):
    """Geocode *query* now and return one geopy-like result (or None).

    A convenience wrapper over :func:`_geocode_candidates` for ad-hoc use:
    with ``context_events`` (``{"lat", "lon", "dt"}``) and ``target_dt`` the
    candidates are disambiguated by :func:`_disambiguate_results`, else the
    first (US) candidate wins. The location pipeline itself never geocodes in
    the request path; it reads cached candidates.
    """
    cands = _geocode_candidates(query, timeout=timeout)
    if not cands:
        return None
    results = [
        SimpleNamespace(
            latitude=c["lat"],
            longitude=c["lon"],
            address=c["name"],
            raw={"importance": c.get("importance") or 0},
        )
        for c in cands
    ]
    if context_events and target_dt:
        best, alert = _disambiguate_results(results, context_events, target_dt)
        if alert and alert["type"] == "suspicious_distance":
            emit_suspicious_geocode(
                query=query,
                best_lat=best.latitude,
                best_lon=best.longitude,
                centroid=alert["centroid"],
                distance_km=alert["distance_km"],
            )
        elif alert and alert["type"] == "all_infeasible":
            emit_all_results_infeasible(
                query=query,
                results_count=alert["results_count"],
                context_events=context_events,
                target_dt=target_dt,
            )
        return best
    return results[0]


# ── Async TTL cache decorator ────────────────────────────────────────────
_cached: Dict[str, Tuple[dt.datetime, Any]] = {}


def memo(seconds: int = 600):
    """Decorate a coroutine with a simple per‑process TTL cache."""

    def decorator(fn):
        async def wrapper(*args, **kwargs):  # type: ignore[override]
            now = dt.datetime.now(UTC)
            ts, val = _cached.get(
                fn.__name__, (dt.datetime.min.replace(tzinfo=UTC), None)
            )
            if (now - ts).total_seconds() < seconds:
                return val
            val = await fn(*args, **kwargs)
            _cached[fn.__name__] = (now, val)
            return val

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ── FAA VIP‑TFR JSON helper ─────────────────────────────────────────────
@memo(300)
async def _vip_json(include_security: bool = False) -> List[Dict[str, Any]]:
    """Return active VIP/SECURITY TFR JSON records (UTC now inside validity).

    NOTE: Returns empty list when TFR_ENABLED=False (FAA API deprecated 2025-12).
    """
    # TFR disabled - FAA API now returns HTML instead of JSON
    if not TFR_ENABLED:
        return []

    now = dt.datetime.now(UTC)
    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as cli:
            resp = await logged_request_async(cli, "get", TFR_JSON_URL)
            data = resp.json()
    except Exception:  # noqa: BLE001 – network/JSON errors → empty list
        return []

    def _wanted(rec: dict) -> bool:
        if VIP_RE.search(rec.get("type", "")):
            return True
        if include_security and SECURITY_RE.fullmatch(rec.get("type", "").strip()):
            return _likely_trump_tfr(rec)
        return False

    records: list[dict] = []
    for rec in data:
        try:
            begin = dt.datetime.fromisoformat(rec["effectiveBegin"])
            end = dt.datetime.fromisoformat(rec["effectiveEnd"])
        except Exception:  # noqa: BLE001 – bad record, skip
            continue
        if not (begin <= now <= end):
            continue
        if _wanted(rec):
            records.append(rec)
    return records


# ── Flight history ───────────────────────────────────────────────────────
def poll_flight_history() -> None:
    """Record this poll's fleet states in the flight history (background loop).

    Requests only read the history; the background loop calls this once per
    tick (blocking: call it off the event loop).
    """
    states, errors = _unpack_fleet(get_fleet_states())
    _record_flight_history(states, feeds_failed=bool(errors) and not states)


def _record_flight_history(states: list[dict], feeds_failed: bool) -> None:
    """Persist airborne fixes / landings per airframe, or infer landings.

    Landing inference runs for every airframe not seen in this poll, but only
    when the feeds answered (an outage is not aircraft silence). Never raises:
    flight history is an optimisation, not a requirement.
    """
    try:
        for st in states:
            tail = st.get("callsign")
            if st.get("status") == "airborne":
                known = st.get("altitude_known", True)
                arrival_cache.record_airborne(
                    st["lat"],
                    st["lon"],
                    st.get("altitude") if known else None,
                    st.get("vertical_rate"),
                    st["ts"],
                    tail,
                    st.get("flight_id"),
                )
            elif st.get("status") == "grounded":
                arrival_cache.save(
                    st["lat"], st["lon"], st["ts"], kind="observed", tail=tail
                )
                if is_af1_callsign(st.get("flight_id")):
                    arrival_cache.mark_af1(tail)
        if not feeds_failed:
            seen = {airframe_of(s.get("callsign")) or s.get("callsign") for s in states}
            if arrival_cache.maybe_infer_landing(
                clock.now(), skip={s for s in seen if s}
            ):
                LOG.info("Inferred landing from a track lost low and descending")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not update flight history: %s", exc)


def _tracker_url(tail: str | None) -> str | None:
    meta = FLEET.get(tail or "")
    return f"https://globe.adsbexchange.com/?icao={meta['icao']}" if meta else None


def _build_tracks(
    states: list[dict], history: dict[str, dict[str, Any]]
) -> list[location_fusion.Track]:
    """Merge this poll's states with the per-airframe flight history."""
    live_by: dict[str, dict] = {}
    for st in states:
        key = airframe_of(st.get("callsign")) or st.get("callsign") or "unknown"
        if key not in live_by or st["ts"] > live_by[key]["ts"]:
            live_by[key] = st
    tracks = []
    for key in sorted(set(history) | set(live_by)):
        rec = history.get(key, {})
        landing, air = rec.get("landing"), rec.get("airborne")
        if landing and air:
            if air["ts"] > landing["ts"]:
                landing = None  # took off again
            else:
                air = None
        live = live_by.get(key)
        tail = (
            (live or {}).get("callsign")
            or (air or {}).get("tail")
            or (landing or {}).get("tail")
            or (None if key == arrival_cache.UNKNOWN_AIRFRAME else key)
        )
        tracks.append(
            location_fusion.Track(
                airframe=key,
                tail=tail,
                live=live,
                landing=landing,
                airborne=air,
                takeoff=rec.get("takeoff"),
                af1=bool(rec.get("af1")),
                home_base=home_base_of(key),
                tracker_url=(live or {}).get("tracker_url") or _tracker_url(tail),
            )
        )
    return tracks


# ── Venues to geocode ────────────────────────────────────────────────────
def _window(events: list[dict], now: dt.datetime) -> list[dict]:
    """Items the fusion can use: 7 days back (+ context) to 24 h ahead."""
    pad = location_fusion.CONTEXT_WINDOW_H
    lo = now - dt.timedelta(hours=location_fusion.MAX_AGE_H + pad)
    hi = now + dt.timedelta(hours=location_fusion.FUTURE_H + pad)
    return cal.events_between(events, lo, hi)


def _venue_queries(events: list[dict], now: dt.datetime) -> list[str]:
    """Event locations and departure destinations that need geocoding.

    Covers items in the fusion window (7 days back, 24 h ahead), nearest in
    time first; aliased, skip-listed and generic strings and items that place
    nobody (lids, cancellations) are left out.
    """
    lo = now - dt.timedelta(hours=location_fusion.MAX_AGE_H)
    hi = now + dt.timedelta(hours=location_fusion.FUTURE_H)
    window = sorted(
        (e for e in events if lo <= e["dtstart_utc"] <= hi),
        key=lambda e: abs((e["dtstart_utc"] - now).total_seconds()),
    )
    seen: set[str] = set()
    out: list[str] = []
    for ev in window:
        if location_fusion.presence(str(ev.get("summary") or "")) == "none":
            continue
        texts = [str(ev.get("location") or "")]
        m = location_fusion.DEPARTS_RE.search(str(ev.get("summary") or ""))
        if m:
            texts.append(m.group("dest").strip().rstrip("."))
        for text in texts:
            key = _clean(text)
            if not key or key in seen or find_alias(key) or _should_skip_geocode(key):
                continue
            seen.add(key)
            out.append(text.strip())
    return out


def _cached_geocodes(events: list[dict], now: dt.datetime) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for query in _venue_queries(events, now):
        cands = cached_candidates(query)
        if cands:
            out[_clean(query)] = cands
    return out


def pregeocode_events(limit: int = 5) -> int:
    """Geocode up to *limit* uncached venues of the current window.

    Runs in the background loop (blocking: call it off the event loop); the
    rate limiter keeps Nominatim at ≤ 1 req/s. Returns the number of venues
    looked up.
    """
    now = clock.now()
    done = 0
    for query in _venue_queries(_window(cal.get_events(), now), now):
        if done >= limit:
            break
        if cached_candidates(query) is not None or _neg_cached(query):
            continue
        _geocode_candidates(query)
        done += 1
    return done


# ── Main public helper ───────────────────────────────────────────────────
NEWSWIRE_ENABLED: Final = False  # finding #8: never produced a claim in prod

#: Display source → link for each claim kind
_KIND_URLS: Final[dict[str, str]] = {
    "calendar_alias": FACTBASE_URL,
    "calendar_summary": FACTBASE_URL,
    "calendar_geocode": FACTBASE_URL,
    "departure_dest": FACTBASE_URL,
    "overnight_dc": FACTBASE_URL,
    "overnight_fl": FACTBASE_URL,
    "overnight_nj": FACTBASE_URL,
}


def _unpack_fleet(snap: Any) -> tuple[list[dict], list[str]]:
    """Accept ``{"states", "errors"}`` (or a legacy single-state envelope)."""
    if isinstance(snap, dict) and "states" in snap:
        return list(snap.get("states") or []), list(snap.get("errors") or [])
    if isinstance(snap, dict) and "state" in snap:
        st = snap.get("state")
        return ([st] if st else []), list(snap.get("errors") or [])
    return ([snap] if snap else []), []


def _present(coords: dict[str, Any]) -> dict[str, Any]:
    """Turn the fusion output into the public coords dict."""
    coords = dict(coords)
    source = coords.pop("_source", "") or ""
    url = coords.pop("_url", None) or _KIND_URLS.get(str(coords.get("reason")))
    if coords.get("unknown"):
        return coords
    return _stamp(coords, age_h=coords.get("age_h"), source=source, url=url)


DECISION_TTL_S: Final = 45.0
DECISION_BUCKET_S: Final = 30
#: Last decision and the inputs it was made from (kept alive so ids stay valid)
_decision_memo: dict[str, Any] = {}


async def current_coords(
    *, trace: Optional[List[Dict[str, Any]]] = None
) -> Union[Dict[str, Any], Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Return best‑guess coordinates for Donald Trump **right now**.

    Gathers the fleet states, the calendar window, cached geocodes, the
    overnight-base inference and (if enabled) the newswire, then lets
    :func:`location_fusion.fuse` decide. Read-only: flight history is recorded
    by the background loop (:func:`poll_flight_history`). The decision is
    memoised for up to :data:`DECISION_TTL_S` while its inputs are unchanged
    (same memoised feed objects, no history / geocode writes, same 30 s
    bucket of ``clock.now()``); a *trace* request always recomputes. Returns
    the coords dict, or ``(coords, trace)`` when a *trace* list is passed.
    """
    now = clock.now()
    ts_now = now.isoformat()
    trace_log = trace if trace is not None else []
    trace_log.append({"ts": ts_now, "phase": "loc", "step": "start"})

    # ── ADS-B (memoised 60 s; blocking HTTP → thread) and the calendar ────
    # Requests run these on the bounded request pool (see executors)
    snap = await executors.to_thread(get_fleet_states)
    events = await executors.to_thread(cal.get_events)
    key = (
        id(snap),
        id(events),
        _geocode_version,
        arrival_cache.version(),
        int(now.timestamp() // DECISION_BUCKET_S),
    )
    memo = _decision_memo
    if (
        trace is None
        and memo.get("key") == key
        and time.monotonic() - memo["at"] < DECISION_TTL_S
    ):
        return dict(memo["coords"])

    states, errors = _unpack_fleet(snap)
    plane_state = max(states, key=lambda s: s["ts"]) if states else None
    trace_log.append(
        {
            "ts": ts_now,
            "phase": "loc",
            "step": "plane",
            "state": plane_state,
            "states": states,
            "errors": errors or None,
        }
    )

    window = _window(events, now)
    geocodes = _cached_geocodes(window, now)
    lo, hi = now - dt.timedelta(hours=14), now + dt.timedelta(hours=18)
    overnight_events = [
        e
        for e in window
        if lo <= e["dtstart_utc"] <= hi
        and location_fusion.presence(str(e.get("summary") or "")) == "full"
    ]
    overnight = cal.get_overnight_base(
        now,
        events=overnight_events,
        resolve=location_fusion.make_resolver(window, geocodes, now),
    )
    news = await get_latest_location() if NEWSWIRE_ENABLED else None
    try:
        history = arrival_cache.airframes()
        primary_hint = arrival_cache.primary_airframe()
    except Exception as exc:  # noqa: BLE001 – never break the pipeline
        LOG.warning("Could not read flight history: %s", exc)
        history, primary_hint = {}, None

    decision = location_fusion.fuse(
        location_fusion.Inputs(
            events=window,
            geocodes=geocodes,
            tracks=_build_tracks(states, history),
            overnight=overnight,
            news=news,
            primary_hint=primary_hint,
        ),
        now,
    )
    if decision.primary_gated and decision.primary != primary_hint:
        try:
            arrival_cache.set_primary_airframe(decision.primary)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not store primary airframe: %s", exc)

    coords = _present(decision.coords)
    if trace is not None:
        trace_log.append(
            {
                "ts": ts_now,
                "phase": "loc",
                "step": "candidates",
                "primary": decision.primary,
                "candidates": decision.trace(now),
            }
        )
        trace_log.append(
            {"ts": ts_now, "phase": "loc", "step": "decision", "coords": coords}
        )
    memo.clear()
    memo.update(
        key=(
            id(snap),
            id(events),
            _geocode_version,
            arrival_cache.version(),
            key[-1],
        ),
        refs=(snap, events),
        coords=dict(coords),
        at=time.monotonic(),
    )
    _emit_state_change_events(coords, plane_state)
    return (coords, trace_log) if trace is not None else coords
