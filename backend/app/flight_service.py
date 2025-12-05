"""flight_service.py
~~~~~~~~~~~~~~~~~~~~
Return the freshest **Trump‑fleet** aircraft state with automatic fallback
from **OpenSky** to **adsb.fi**.

Revision 2025‑06
----------------
* Fleet is now imported from :pyfile:`fleet.py` (removes duplication).
* OpenSky failures/timeouts are still caught, but we now log API latency via
  :pyfile:`api_logging.py` where possible.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Final, Literal, TypedDict, cast

import httpx
from dateutil import tz

from .adsbfi_service import get_plane_state_adsb
from .constants import USER_AGENT
from .event_service import emit_api_error
from .fleet import FLEET

# ── Constants & logging ──────────────────────────────────────────────────
UTC: Final = tz.UTC
LOG = logging.getLogger("flight_service")

CALLSIGNS: Final[set[str]] = set(FLEET)

#: Freshness windows for ADS‑B snapshots (seconds)
AIRBORNE_WINDOW_SEC: Final[int] = 600  # 10 min
GROUNDED_WINDOW_SEC: Final[int] = 1_200  # 20 min
#: Altitude (ft) below which we assume the aircraft is on the ground
GROUND_ALT_FT: Final[int] = 300

#: Confidence values
AIRBORNE_CONFIDENCE_BASE: Final[int] = 95
AIRBORNE_CONFIDENCE_MIN: Final[int] = 75
GROUNDED_CONFIDENCE_BASE: Final[int] = 90
GROUNDED_CONFIDENCE_MIN: Final[int] = 70



def calculate_confidence(age_seconds: float, is_airborne: bool) -> int | None:
    """
    Calculate confidence score for aircraft position based on age.
    
    Args:
        age_seconds: Age of the position data in seconds.
        is_airborne: True if aircraft is airborne, False if grounded.
    
    Returns:
        Confidence score (75-95 for airborne, 70-90 for grounded) or None if too old.
    
    Airborne positions:
        - 0-5 min: confidence 95 (full confidence)
        - 5-10 min: linear decay from 95 to 75
        - >10 min: rejected (None)
    
    Grounded positions:
        - 0-10 min: confidence 90 (full confidence)
        - 10-20 min: linear decay from 90 to 70
        - >20 min: rejected (None)
    """
    age_minutes = age_seconds / 60
    
    if is_airborne:
        if age_seconds > AIRBORNE_WINDOW_SEC:
            return None  # Too old
        
        if age_minutes <= 5:
            return AIRBORNE_CONFIDENCE_BASE  # Full confidence
        
        # Linear decay from 5-10 minutes
        decay_progress = (age_minutes - 5) / 5  # 0 to 1
        confidence = AIRBORNE_CONFIDENCE_BASE - (
            decay_progress * (AIRBORNE_CONFIDENCE_BASE - AIRBORNE_CONFIDENCE_MIN)
        )
        return int(confidence)
    
    else:  # Grounded
        if age_seconds > GROUNDED_WINDOW_SEC:
            return None  # Too old
        
        if age_minutes <= 10:
            return GROUNDED_CONFIDENCE_BASE  # Full confidence
        
        # Linear decay from 10-20 minutes
        decay_progress = (age_minutes - 10) / 10  # 0 to 1
        confidence = GROUNDED_CONFIDENCE_BASE - (
            decay_progress * (GROUNDED_CONFIDENCE_BASE - GROUNDED_CONFIDENCE_MIN)
        )
        return int(confidence)


class PlaneState(TypedDict, total=False):
    """Homogeneous snapshot shape used throughout the project."""

    callsign: str
    lat: float
    lon: float
    altitude: float
    on_ground: bool
    ts: dt.datetime
    status: Literal["airborne", "grounded"]
    tracker_url: str
    confidence: int


# ── In‑memory TTL cache ──────────────────────────────────────────────────
_cache: dict[str, tuple[dt.datetime, Any]] = {}


def _memo(seconds: int = 60):
    """UTC TTL‑cache decorator (argument‑insensitive)."""

    def decorator(func):
        def wrapper(*args, **kwargs):  # type: ignore[override]
            ts, val = _cache.get(
                func.__name__, (dt.datetime.min.replace(tzinfo=UTC), None)
            )
            if (dt.datetime.now(UTC) - ts).total_seconds() < seconds:
                return val
            val = func(*args, **kwargs)
            _cache[func.__name__] = (dt.datetime.now(UTC), val)
            return val

        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ── Public helper ────────────────────────────────────────────────────────
@_memo(300)  # 5 min cache to stay under OpenSky's 400 req/day anonymous limit
def get_plane_state() -> PlaneState | dict[str, Any]:
    """Return freshest :class:`PlaneState` or an error envelope.

    * Query **OpenSky** first.
    * Fallback to **adsb.fi** if OpenSky fails or is stale.
    * If both silent, return ``{"state": None, "errors": [...]} ``.
    """

    errors: list[str] = []

    # 1️⃣  OpenSky ----------------------------------------------------------
    try:
        state = _opensky_state()
    except Exception as exc:  # noqa: BLE001 – auth/network errors
        msg = f"OpenSky error: {exc}"
        LOG.warning(msg)
        errors.append(msg)
        emit_api_error("OpenSky", str(exc))
        state = None

    if state:
        if errors:
            state = cast(PlaneState, {**state, "errors": errors})
        return state

    # 2️⃣  adsb.fi fallback -------------------------------------------------
    try:
        state = get_plane_state_adsb()
    except Exception as exc:  # noqa: BLE001
        msg = f"adsb.fi error: {exc}"
        LOG.warning(msg)
        errors.append(msg)
        emit_api_error("adsb.fi", str(exc))
        state = None

    if state:
        if errors:
            state = cast(PlaneState, {**state, "errors": errors})
        return state

    # 3️⃣  Both feeds silent ------------------------------------------------
    return {"state": None, "errors": errors}


# ── Internal helpers ─────────────────────────────────────────────────────


def _opensky_state() -> PlaneState | None:
    """
    Return freshest Trump-fleet OpenSky snapshot ≤40 min old.

    Now uses httpx with proper timeout handling (10s).
    Query is limited to our fleet's ICAO24 addresses for efficiency.
    Runs in sync context via asyncio.run for compatibility.
    """
    async def _fetch() -> PlaneState | None:
        now = dt.datetime.now(UTC)

        # Build ICAO24 filter from fleet (e.g., "ae4e11,ae4d8a,aa3410")
        icao_codes = ",".join(aircraft["icao"] for aircraft in FLEET.values())
        url = f"https://opensky-network.org/api/states/all?icao24={icao_codes}"

        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers={"User-Agent": USER_AGENT}
            ) as client:
                resp = await client.get(url)

            if resp.status_code != 200:
                LOG.warning("OpenSky API returned status %d", resp.status_code)
                return None

            data = resp.json()
            all_states = data.get("states", []) or []

        except (httpx.TimeoutException, httpx.RequestError) as exc:
            LOG.warning("OpenSky API request failed: %s", exc)
            return None
        except Exception as exc:
            LOG.error("Unexpected error querying OpenSky: %s", exc, exc_info=True)
            return None

        fresh: list[PlaneState] = []

        # OpenSky state vector format (17 elements):
        # [0]=icao24, [1]=callsign, [3]=time_position, [4]=last_contact,
        # [5]=longitude, [6]=latitude, [7]=baro_altitude, [8]=on_ground, ...
        for s in all_states:
            if not s or len(s) < 9:
                continue  # Skip malformed entries

            callsign_raw = s[1] or ""
            callsign = callsign_raw.strip() if isinstance(callsign_raw, str) else ""

            # Match against tail numbers (e.g., "92-9000", "N757AF")
            if callsign not in CALLSIGNS:
                continue

            # Parse timestamps
            time_position = s[3]
            last_contact = s[4]
            timestamp = time_position if time_position else last_contact
            if not timestamp:
                continue

            last_ts = dt.datetime.fromtimestamp(timestamp, UTC)
            age_seconds = (now - last_ts).total_seconds()

            # Parse position and altitude
            lon = s[5]
            lat = s[6]
            if lat is None or lon is None:
                continue  # No position data

            altitude = float(s[7] or 0.0)  # baro_altitude in meters
            altitude_ft = altitude * 3.28084  # Convert to feet

            on_ground = bool(s[8]) or altitude_ft < GROUND_ALT_FT
            is_airborne = not on_ground
            status = "grounded" if on_ground else "airborne"

            # Calculate confidence with age-based decay
            confidence = calculate_confidence(age_seconds, is_airborne=is_airborne)
            if confidence is None:
                continue  # Too stale

            fresh.append(
                PlaneState(
                    callsign=callsign,
                    lat=float(lat),
                    lon=float(lon),
                    altitude=altitude_ft,
                    on_ground=on_ground,
                    ts=last_ts,
                    status=status,
                    tracker_url=f"https://globe.adsbexchange.com/?icao={callsign}",
                    confidence=confidence,
                )
            )

        # Newest snapshot wins
        return min(fresh, key=lambda st: now - st["ts"]) if fresh else None

    # Run async function in sync context
    return asyncio.run(_fetch())

