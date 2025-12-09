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
  proximity. Flight tracking (ADS-B via OpenSky/adsb.fi) is unaffected.
* See: https://ntrs.nasa.gov/citations/20250003355 for NASA DIP API docs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from dateutil import tz
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

from .api_logging import logged_request_async
from .arrival_cache import load as load_last, save as save_last
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
from .flight_service import get_plane_state
from .gdelt_service import get_latest_location
from . import calendar_service as cal
from .place_aliases import PLACE_ALIASES
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
CAL_WINDOW_H = 72.0  # hours

# Physical feasibility constants for geocoding disambiguation
MAX_TRAVEL_SPEED_KMH = 800.0  # Generous estimate for Air Force One effective speed
MIN_TIME_GAP_H = 0.1  # Minimum 6 minutes to avoid division issues
SUSPICIOUS_DISTANCE_KM = 500.0  # Alert if best result is this far from context

TRANSLATE = str.maketrans({"\u00a0": " ", "\u2011": "-", "\u2013": "-", "\u2014": "-"})

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
    global _prev_flight_status, _prev_location_reason, _is_initializing

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
                source="OpenSky/adsb.fi",
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
        r for r in results
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
    distance_km = _haversine_km(
        centroid[0], centroid[1], best.latitude, best.longitude
    )
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
_nominatim = Nominatim(user_agent="rain-on-trump")
_geocode_raw = RateLimiter(_nominatim.geocode, min_delay_seconds=1)

_geocode_log = logging.getLogger("location_service.geocode")


def _should_skip_geocode(location: str) -> bool:
    """Check if location should be skipped for geocoding."""
    cleaned = _clean(location)
    return cleaned in SKIP_LOCATIONS


# ── Geocode result cache ─────────────────────────────────────────────────
# Prevents redundant Nominatim calls for the same query within TTL period.
# Keyed by cleaned query string; stores (timestamp, result) tuples.
_geocode_cache: dict[str, tuple[dt.datetime, object]] = {}
GEOCODE_CACHE_TTL_S: Final = 900  # 15 minutes (matches calendar cache)


def _get_cached_geocode(query: str) -> object | None:
    """Return cached geocode result if still valid, else None."""
    key = _clean(query)
    if key not in _geocode_cache:
        return None
    ts, result = _geocode_cache[key]
    if (dt.datetime.now(UTC) - ts).total_seconds() < GEOCODE_CACHE_TTL_S:
        _geocode_log.debug("Cache hit for: %r", query)
        return result
    # Expired
    del _geocode_cache[key]
    return None


def _set_cached_geocode(query: str, result: object) -> None:
    """Store geocode result in cache."""
    key = _clean(query)
    _geocode_cache[key] = (dt.datetime.now(UTC), result)


def _smart_geocode(
    query: str,
    timeout: int = 10,
    context_events: list[dict] | None = None,
    target_dt: dt.datetime | None = None,
):
    """
    Geocode with US-first strategy, context disambiguation, and international fallback.

    Args:
        query: Location string to geocode.
        timeout: Timeout in seconds.
        context_events: Optional nearby resolved events with {"lat", "lon", "dt"}.
            When provided, enables multi-result disambiguation.
        target_dt: Timestamp of the event being geocoded (required if context_events).

    Returns:
        geopy.Location or None.

    Strategy:
        1. Skip if query matches skip list
        2. Try US-restricted search (with multiple results if context provided)
        3. If context provided, use hybrid 3-layer disambiguation
        4. If US returns nothing, try international search
        5. Log result for monitoring and alias curation
    """
    # Check skip list
    if _should_skip_geocode(query):
        _geocode_log.debug("Skipped (in skip list): %r", query)
        add_geocode_entry(query=query, result_type="skipped")
        return None

    # Check cache first (avoids redundant Nominatim calls)
    cached = _get_cached_geocode(query)
    if cached is not None:
        return cached

    # Determine if we should use multi-result mode
    use_disambiguation = bool(context_events and target_dt)

    # Try US-first
    try:
        if use_disambiguation:
            # Multi-result mode for disambiguation
            results = _geocode_raw(
                query,
                timeout=timeout,
                country_codes="us",
                addressdetails=True,
                exactly_one=False,
                limit=5,
            )
            # Convert generator to list if needed
            results = list(results) if results else []
        else:
            # Single-result mode (legacy behavior)
            result = _geocode_raw(
                query,
                timeout=timeout,
                country_codes="us",
                addressdetails=True,
            )
            results = [result] if result else []

        if results:
            # Apply disambiguation if we have context
            if use_disambiguation and len(results) > 1:
                result, alert = _disambiguate_results(
                    results=results,
                    context_events=context_events,
                    target_dt=target_dt,
                )
                # Emit alerts
                if alert:
                    if alert["type"] == "suspicious_distance":
                        emit_suspicious_geocode(
                            query=query,
                            best_lat=result.latitude,
                            best_lon=result.longitude,
                            centroid=alert["centroid"],
                            distance_km=alert["distance_km"],
                        )
                    elif alert["type"] == "all_infeasible":
                        emit_all_results_infeasible(
                            query=query,
                            results_count=alert["results_count"],
                            context_events=context_events,
                            target_dt=target_dt,
                        )
                _geocode_log.info(
                    "Geocoded (US, disambiguated from %d): %r → %.4f, %.4f",
                    len(results),
                    query,
                    result.latitude,
                    result.longitude,
                )
            else:
                result = results[0]
                # Check for suspicious distance even with single result
                if use_disambiguation:
                    _, alert = _disambiguate_results(
                        results=results,
                        context_events=context_events,
                        target_dt=target_dt,
                    )
                    if alert and alert["type"] == "suspicious_distance":
                        emit_suspicious_geocode(
                            query=query,
                            best_lat=result.latitude,
                            best_lon=result.longitude,
                            centroid=alert["centroid"],
                            distance_km=alert["distance_km"],
                        )

            addr = result.raw.get("address", {})
            state = addr.get("state", "")
            city = addr.get("city") or addr.get("town") or addr.get("county", "")
            importance = result.raw.get("importance")
            _geocode_log.info(
                "Geocoded (US): %r → %.4f, %.4f (%s, %s) [importance=%.3f]",
                query,
                result.latitude,
                result.longitude,
                city,
                state,
                importance or 0,
            )
            add_geocode_entry(
                query=query,
                result_type="us",
                lat=result.latitude,
                lon=result.longitude,
                display_name=result.address,
                country="United States",
                state=state,
                importance=importance,
            )
            # Alert on low importance scores
            if importance is not None and importance < LOW_IMPORTANCE_THRESHOLD:
                emit_low_importance_geocode(
                    query=query,
                    importance=importance,
                    lat=result.latitude,
                    lon=result.longitude,
                    display_name=result.address,
                )
            _set_cached_geocode(query, result)
            return result
    except Exception as e:  # noqa: BLE001
        _geocode_log.warning("US geocode failed for %r: %s", query, e)
        add_geocode_entry(query=query, result_type="error", error=str(e))
        # Don't emit Discord yet - try international fallback first

    # Fallback to international
    try:
        result = _geocode_raw(
            query,
            timeout=timeout,
            addressdetails=True,
        )
        if result:
            addr = result.raw.get("address", {})
            country = addr.get("country", "")
            state = addr.get("state", "")
            importance = result.raw.get("importance")
            _geocode_log.info(
                "Geocoded (international): %r → %.4f, %.4f (%s) [importance=%.3f]",
                query,
                result.latitude,
                result.longitude,
                country,
                importance or 0,
            )
            add_geocode_entry(
                query=query,
                result_type="international",
                lat=result.latitude,
                lon=result.longitude,
                display_name=result.address,
                country=country,
                state=state,
                importance=importance,
            )
            # Alert on low importance scores
            if importance is not None and importance < LOW_IMPORTANCE_THRESHOLD:
                emit_low_importance_geocode(
                    query=query,
                    importance=importance,
                    lat=result.latitude,
                    lon=result.longitude,
                    display_name=result.address,
                )
            _set_cached_geocode(query, result)
            return result
    except Exception as e:  # noqa: BLE001
        _geocode_log.warning("International geocode failed for %r: %s", query, e)
        add_geocode_entry(query=query, result_type="error", error=str(e))
        emit_geocode_failure(query=query, result_type="error", error=str(e))
        return None

    _geocode_log.warning("Geocode returned no results for: %r", query)
    add_geocode_entry(query=query, result_type="no_result")
    emit_geocode_failure(query=query, result_type="no_result")
    return None

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


# ── Main public helper ───────────────────────────────────────────────────
async def current_coords(
    *, trace: Optional[List[Dict[str, Any]]] = None
) -> Union[Dict[str, Any], Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Return best‑guess coordinates for Donald Trump **right now**."""

    ts_now = dt.datetime.now(UTC).isoformat()
    trace_log = trace if trace is not None else []
    trace_log.append({"ts": ts_now, "phase": "loc", "step": "start"})

    # ── 1️⃣  ADS‑B feeds --------------------------------------------------
    plane_raw = await asyncio.to_thread(get_plane_state)
    if isinstance(plane_raw, dict) and "state" in plane_raw:
        plane_state = plane_raw["state"]
        plane_errors = plane_raw.get("errors")
    else:
        plane_state = plane_raw  # type: ignore[assignment]
        plane_errors = None

    trace_log.append(
        {
            "ts": ts_now,
            "phase": "loc",
            "step": "plane",
            "state": plane_state,
            "errors": plane_errors,
        }
    )

    # ── helper: calendar event (may be None) -----------------------------
    event = cal.current_event()

    def _plane_newer_than_event() -> bool:
        if not (plane_state and event):
            return True  # nothing to compare → treat plane as newer
        return plane_state["ts"] > event["dtstart_utc"]

    if plane_state and plane_state["status"] == "airborne":
        # Always trust airborne snapshot — location = jet
        coords_air = _stamp(
            {
                "lat": plane_state["lat"],
                "lon": plane_state["lon"],
                "name": f"In flight ({plane_state['callsign']})",
                "in_flight": True,
                "confidence": plane_state.get("confidence", 95),
                "reason": "plane_air",
            },
            age_h=0.0,
            source="ADS‑B (airborne)",
            url=plane_state.get("tracker_url"),
        )
        _emit_state_change_events(coords_air, plane_state)
        return (coords_air, trace_log) if trace else coords_air

    if (
        plane_state
        and plane_state["status"] == "grounded"
        and _plane_newer_than_event()
    ):
        # Grounded but *newer* than any calendar entry → keep
        # NOTE: TFR check below is no-op when TFR_ENABLED=False (always near_tfr=False)
        near_tfr = False
        for rec in await _vip_json(include_security=True):
            coords = parse_tfr_coordinates(rec.get("description", ""))
            if not coords:
                continue
            if _haversine_km(plane_state["lat"], plane_state["lon"], coords["lat"], coords["lon"]) < 55:
                near_tfr = True
                break

        # Use confidence from flight_service (with age decay) + TFR bonus
        base_confidence = plane_state.get("confidence", 90)
        confidence = min(95, base_confidence + 10) if near_tfr else base_confidence

        coords_ground = _stamp(
            {
                "lat": plane_state["lat"],
                "lon": plane_state["lon"],
                "name": f"{plane_state['callsign']} parked",
                "tfr_confirmed": near_tfr,
                "confidence": confidence,
                "reason": "plane_tfr" if near_tfr else "plane_ground",
            },
            age_h=_age_h(plane_state["ts"]),
            source="ADS‑B (grounded)",
            url=plane_state.get("tracker_url"),
        )
        save_last(coords_ground["lat"], coords_ground["lon"])
        trace_log.append(
            {
                "ts": ts_now,
                "phase": "loc",
                "step": "plane_ground",
                "coords": coords_ground,
            }
        )
        _emit_state_change_events(coords_ground, plane_state)
        return (coords_ground, trace_log) if trace else coords_ground

    # ── 1.5️⃣ Overnight base inference ------------------------------------
    # During overnight hours (9PM-8AM ET), if evening and morning events
    # are in the same region (DC, FL, or NJ), infer the overnight base.
    overnight_base = cal.get_overnight_base()
    if overnight_base:
        # Determine reason based on base name
        name = overnight_base["name"]
        if "White House" in name:
            reason = "overnight_dc"
        elif "Mar-a-Lago" in name:
            reason = "overnight_fl"
        else:
            reason = "overnight_nj"  # Bedminster

        coords_overnight = _stamp(
            {
                **overnight_base,
                "confidence": 58,
                "reason": reason,
            },
            source="Overnight inference (evening→morning pattern)",
            url=FACTBASE_URL,
        )
        trace_log.append(
            {
                "ts": ts_now,
                "phase": "loc",
                "step": "overnight_base",
                "coords": coords_overnight,
            }
        )
        _emit_state_change_events(coords_overnight, plane_state)
        return (coords_overnight, trace_log) if trace else coords_overnight

    # ── 2️⃣  Calendar event ----------------------------------------------
    # Collect calendar candidate (don't return early - compare with TFR later)
    trace_log.append(
        {"ts": ts_now, "phase": "loc", "step": "calendar_event", "event": event}
    )
    coords_cal: Optional[dict] = None
    last_known_calendar: Optional[dict] = None

    if event:
        # Keep raw versions for display, cleaned versions for matching
        raw_location = (event.get("location", "") or "").strip()
        raw_summary = event.get("summary", "")
        desc = _clean(raw_location)
        summ = _clean(raw_summary)
        age_cal = _age_h(event["dtstart_utc"])
        if "no public events scheduled" not in summ:
            cal_conf = CAL_BASE_CONF - (
                (CAL_BASE_CONF - CAL_MIN_CONF)
                * min(age_cal, CAL_WINDOW_H)
                / CAL_WINDOW_H
            )

            # 2a. Alias on location
            for key, alias in PLACE_ALIASES.items():
                if key in desc:
                    coords_cal = _stamp(
                        {
                            **alias,
                            "confidence": cal_conf,
                            "reason": "calendar_alias",
                            "event_summary": raw_summary,
                        },
                        age_h=age_cal,
                        source="Factba.se schedule",
                        url=FACTBASE_URL,
                    )
                    if cal_conf < CAL_MIN_CONF:
                        last_known_calendar = coords_cal
                    break
            # 2b. Alias on summary
            if not coords_cal:
                for key, alias in PLACE_ALIASES.items():
                    if key in summ:
                        coords_cal = _stamp(
                            {
                                **alias,
                                "confidence": cal_conf,
                                "reason": "calendar_summary",
                                "event_summary": raw_summary,
                            },
                            age_h=age_cal,
                            source="Factba.se schedule",
                            url=FACTBASE_URL,
                        )
                        if cal_conf < CAL_MIN_CONF:
                            last_known_calendar = coords_cal
                        break
            # 2c. Geocode fallback (US-first with hybrid disambiguation)
            if desc and not coords_cal:
                # Get context events for disambiguation (coords + timestamps)
                context_events = cal.get_context_events(event)

                # Log context retrieval for debug endpoint
                trace_log.append({
                    "ts": ts_now,
                    "phase": "loc",
                    "step": "geocode_context",
                    "query": raw_location,
                    "context_count": len(context_events),
                })

                geocoded = _smart_geocode(
                    desc,
                    timeout=10,
                    context_events=context_events,
                    target_dt=event["dtstart_utc"],
                )
                if geocoded:
                    # Log geocode result for debug endpoint
                    trace_log.append({
                        "ts": ts_now,
                        "phase": "loc",
                        "step": "geocode_result",
                        "coords": {"lat": geocoded.latitude, "lon": geocoded.longitude},
                        "display_name": getattr(geocoded, "address", raw_location),
                    })

                    coords_cal = _stamp(
                        {
                            "lat": geocoded.latitude,
                            "lon": geocoded.longitude,
                            "name": raw_location,  # Preserve original case
                            "confidence": cal_conf,
                            "reason": "calendar_geocode",
                            "event_summary": raw_summary,
                        },
                        age_h=age_cal,
                        source="Factba.se schedule (geocoded)",
                        url=FACTBASE_URL,
                    )
                    if cal_conf < CAL_MIN_CONF:
                        last_known_calendar = coords_cal

    # ── 3️⃣  VIP/SECURITY TFR (JSON) -------------------------------------
    # Collect TFR candidate (don't return early - compare with calendar)
    # NOTE: This step is no-op when TFR_ENABLED=False (vip_recs always empty)
    coords_tfr: Optional[dict] = None
    vip_recs = await _vip_json(include_security=True)
    if vip_recs:
        best = vip_recs[0]  # newest because _vip_json keeps current only
        tfr_coords = parse_tfr_coordinates(best.get("description", ""))
        if tfr_coords:
            coords_tfr = _stamp(
                {
                    "lat": tfr_coords["lat"],
                    "lon": tfr_coords["lon"],
                    "name": best.get("shortDesc", "VIP‑TFR"),
                    "confidence": 40,
                    "reason": "tfr_json",
                },
                source="FAA VIP‑TFR JSON",
                url=TFR_JSON_URL,
            )
            trace_log.append(
                {"ts": ts_now, "phase": "loc", "step": "vip_json", "coords": coords_tfr}
            )

    # ── Compare calendar vs TFR by confidence ──
    candidates = [c for c in [coords_cal, coords_tfr] if c is not None]
    best_candidate = select_highest_confidence(candidates)
    if best_candidate:
        _emit_state_change_events(best_candidate, plane_state)
        return (best_candidate, trace_log) if trace else best_candidate

    # ── 4️⃣  Newswire ------------------------------------------------------
    news_coord = await get_latest_location()
    trace_log.append(
        {"ts": ts_now, "phase": "loc", "step": "news_probe", "candidate": news_coord}
    )
    if news_coord:
        coords_news = _stamp(
            {**news_coord, "confidence": 35, "reason": "newswire"},
            source="GDELT dateline",
            url="https://www.gdeltproject.org/",
        )
        _emit_state_change_events(coords_news, plane_state)
        return (coords_news, trace_log) if trace else coords_news

    # ── 5️⃣  Last aircraft arrival cache ----------------------------------
    last = load_last()
    if last:
        coords_last = _stamp(last, source="Last aircraft arrival")
        trace_log.append(
            {"ts": ts_now, "phase": "loc", "step": "last_known", "coords": coords_last}
        )
        _emit_state_change_events(coords_last, plane_state)
        return (coords_last, trace_log) if trace else coords_last

    # ── 🤷  Unknown --------------------------------------------------------
    coords_unknown: Dict[str, Any] = {
        "unknown": True,
        "confidence": 0,
        "reason": "unknown",
    }
    if last_known_calendar:
        coords_unknown["last_known"] = last_known_calendar
    trace_log.append({"ts": ts_now, "phase": "loc", "step": "unknown"})
    _emit_state_change_events(coords_unknown, plane_state)
    return (coords_unknown, trace_log) if trace else coords_unknown
