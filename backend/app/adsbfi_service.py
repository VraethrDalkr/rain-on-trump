"""
adsbfi_service.py
~~~~~~~~~~~~~~~~~
Fetch the freshest Trump-fleet snapshot from **adsb.fi** (unfiltered ADS-B)
as a fallback to OpenSky.

2025-06-02 fixes
----------------
* Calls :pyfunc:`api_logging.logged_request` with ``raise_for_status=False``
  so an HTTP 404 is *expected* (aircraft not currently visible) and no longer
  bubbles up as a warning.
* A 404 now logs at DEBUG and the loop simply continues to the next tail.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final, Literal, TypedDict

import httpx
from dateutil import tz

from .api_logging import logged_request
from .constants import USER_AGENT
from .fleet import FLEET

# ─────────────────────────────── constants ──────────────────────────────
UTC: Final = tz.UTC
LOG = logging.getLogger("adsbfi_service")

CALLSIGNS: Final[set[str]] = set(FLEET)
ICAO_BY_CS: Final[dict[str, str]] = {cs: meta["icao"] for cs, meta in FLEET.items()}

FRESH_WINDOW_SEC: Final[int] = 2_400  # 40 min
GROUND_ALT_FT: Final[int] = 300  # ft – treat lower alt as “ground”


# ─────────────────────────────── typing ─────────────────────────────────
class PlaneState(TypedDict, total=False):
    callsign: str
    lat: float
    lon: float
    altitude: float
    on_ground: bool
    ts: dt.datetime
    status: Literal["airborne", "grounded"]
    tracker_url: str


# ───────────────────────────── mini-cache ───────────────────────────────
_cache: dict[str, tuple[dt.datetime, PlaneState | None]] = {}


def _memo(seconds: int = 60):
    """Simple synchronous TTL cache (argument-insensitive)."""

    def deco(fn):
        def wrapped(*args, **kwargs):  # type: ignore[override]
            ts, val = _cache.get(
                fn.__name__, (dt.datetime.min.replace(tzinfo=UTC), None)
            )
            if (dt.datetime.now(UTC) - ts).total_seconds() < seconds:
                return val
            val = fn(*args, **kwargs)
            _cache[fn.__name__] = (dt.datetime.now(UTC), val)
            return val

        wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapped

    return deco


# ───────────────────────────── public helper ────────────────────────────
@_memo(60)
def get_plane_state_adsb() -> PlaneState | None:
    """
    Return the freshest Trump-fleet *adsb.fi* snapshot seen ≤ 40 min ago.

    A 404 (“aircraft not currently in DB”) is **normal** and only logged at
    *DEBUG*; other non-200 codes fall through to the next tail.
    """
    now = dt.datetime.now(UTC)
    headers = {"User-Agent": USER_AGENT}

    for callsign, icao in ICAO_BY_CS.items():
        url = f"https://api.adsb.fi/v1/aircraft/{icao}"
        with httpx.Client(headers=headers, timeout=10.0) as cli:
            resp = logged_request(cli, "get", url, raise_for_status=False)

        if resp.status_code == 404:
            LOG.debug("[adsb.fi] %s → 404 (not tracked now)", url)
            continue
        if resp.status_code != 200:
            LOG.warning("[adsb.fi] %s → HTTP %s", url, resp.status_code)
            continue

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 – malformed JSON
            LOG.warning("[adsb.fi] bad JSON %s → %s", url, exc)
            continue

        aircraft = data.get("aircraft")
        if not aircraft:
            continue

        last_ts = dt.datetime.fromtimestamp(aircraft["seen_pos"], UTC)
        if (now - last_ts).total_seconds() > FRESH_WINDOW_SEC:
            continue  # snapshot too old

        altitude = float(aircraft.get("alt_baro", 0.0))
        raw_ground = bool(aircraft.get("ground"))
        on_ground = raw_ground or altitude < GROUND_ALT_FT
        status: Literal["grounded", "airborne"] = (
            "grounded" if on_ground else "airborne"
        )

        return PlaneState(
            callsign=callsign,
            lat=float(aircraft["lat"]),
            lon=float(aircraft["lon"]),
            altitude=altitude,
            on_ground=on_ground,
            ts=last_ts,
            status=status,
            tracker_url=f"https://globe.adsbexchange.com/?icao={callsign}",
        )

    # Nothing fresh for any aircraft in the fleet
    return None
