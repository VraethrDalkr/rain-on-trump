"""
adsbfi_service.py
~~~~~~~~~~~~~~~~~
Fetch the freshest Trump-fleet snapshot from community ADS-B aggregators that
serve the ADSBexchange-v2 ("readsb") format: **adsb.fi** and **adsb.lol**.
Neither needs a key; each poll is one request for the whole fleet.

* adsb.fi: personal, non-commercial use, 1 req/s, attribution required.
* adsb.lol: free, dynamic rate limits, data under ODbL (attribution); a
  feeder-linked API key is announced for the future.
* airplanes.live was evaluated 2026-11-04 and now requires per-project
  approval by email.

The parser accepts the v2 shape (``ac`` list, relative ``seen_pos``,
``alt_baro`` possibly ``"ground"``, MLAT-positioned records) and the legacy
adsb.fi v1 shape.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final, Literal, TypedDict

import httpx
from dateutil import tz

from .api_logging import logged_request
from .constants import USER_AGENT
from .fleet import FLEET, TAIL_BY_ICAO, counts_as_president

# ─────────────────────────────── constants ──────────────────────────────
UTC: Final = tz.UTC
LOG = logging.getLogger("adsbfi_service")

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
    flight_id: str
    source: str
    vertical_rate: float | None  # ft/min, negative = descending
    altitude_known: bool


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
ADSBFI_URL: Final[str] = "https://opendata.adsb.fi/api/v2/icao/{hexes}"
ADSBLOL_URL: Final[str] = "https://api.adsb.lol/v2/icao/{hexes}"
_EPOCH_THRESHOLD: Final[float] = 1e9  # larger seen_pos values are UNIX timestamps


def _aircraft_list(data: object) -> list[dict]:
    """Extract aircraft records from either known response shape.

    The documented v2 API is ADSBexchange-v2 compatible (``{"ac": [...]}``);
    the legacy v1 API returned ``{"aircraft": {...}}``.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("ac", data.get("aircraft"))
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [ac for ac in raw if isinstance(ac, dict)]


def _vertical_rate(ac: dict) -> float | None:
    """Barometric (else geometric) vertical rate in ft/min, if reported."""
    for key in ("baro_rate", "geom_rate"):
        val = ac.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _parse_aircraft(
    ac: dict, now: dt.datetime, source: str = "adsb.fi"
) -> PlaneState | None:
    """Convert one aircraft record to a :class:`PlaneState` (None if unusable)."""
    hex_code = str(ac.get("hex", "")).lower().lstrip("~")
    tail = TAIL_BY_ICAO.get(hex_code)
    if tail is None:
        return None  # not one of ours
    flight_id = str(ac.get("flight") or "").strip()
    if not counts_as_president(tail, flight_id):
        LOG.debug("[adsb.fi] %s as %r is not flying as AF1", tail, flight_id)
        return None

    lat, lon, seen_pos = ac.get("lat"), ac.get("lon"), ac.get("seen_pos")
    if lat is None or lon is None or not isinstance(seen_pos, (int, float)):
        return None

    # readsb/ADSBx-v2 report seconds since the last position; v1 used epoch time
    if seen_pos > _EPOCH_THRESHOLD:
        last_ts = dt.datetime.fromtimestamp(seen_pos, UTC)
    else:
        last_ts = now - dt.timedelta(seconds=float(seen_pos))
    if (now - last_ts).total_seconds() > FRESH_WINDOW_SEC:
        return None  # snapshot too old

    alt_raw = ac.get("alt_baro")
    if alt_raw == "ground":
        altitude, on_ground = 0.0, True
    else:
        try:
            altitude = float(alt_raw) if alt_raw is not None else None
        except (TypeError, ValueError):
            altitude = None
        on_ground = bool(ac.get("ground")) or (
            altitude is not None and altitude < GROUND_ALT_FT
        )
    status: Literal["grounded", "airborne"] = "grounded" if on_ground else "airborne"

    icao = ICAO_BY_CS[tail]
    return PlaneState(
        callsign=tail,
        lat=float(lat),
        lon=float(lon),
        altitude=altitude or 0.0,
        altitude_known=altitude is not None,
        on_ground=on_ground,
        ts=last_ts,
        status=status,
        tracker_url=f"https://globe.adsbexchange.com/?icao={icao}",
        flight_id=flight_id,
        source=source,
        vertical_rate=_vertical_rate(ac),
    )


def _fetch_v2(url_template: str, source: str) -> list[PlaneState]:
    """Query one v2-format aggregator for the whole fleet.

    Returns:
        One snapshot per visible fleet aircraft (empty on HTTP 404 or an
        empty ``ac`` list).

    Raises:
        RuntimeError: On other HTTP errors or a malformed body, so the caller
            can record the failure and try the next feed.
    """
    now = dt.datetime.now(UTC)
    url = url_template.format(hexes=",".join(ICAO_BY_CS.values()))
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0) as cli:
        resp = logged_request(cli, "get", url, raise_for_status=False)

    if resp.status_code == 404:
        LOG.debug("[%s] %s → 404 (not tracked now)", source, url)
        return []
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"bad JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("unexpected response shape")

    aircraft = _aircraft_list(data)
    return [
        st for ac in aircraft if (st := _parse_aircraft(ac, now, source)) is not None
    ]


def _newest(states: list[PlaneState]) -> PlaneState | None:
    return max(states, key=lambda st: st["ts"]) if states else None


@_memo(55)
def get_fleet_adsb() -> list[PlaneState]:
    """Every visible fleet aircraft from adsb.fi (≤ 40 min old)."""
    return _fetch_v2(ADSBFI_URL, "adsb.fi")


@_memo(55)
def get_fleet_adsblol() -> list[PlaneState]:
    """Every visible fleet aircraft from adsb.lol (≤ 40 min old)."""
    return _fetch_v2(ADSBLOL_URL, "adsb.lol")


def get_plane_state_adsb() -> PlaneState | None:
    """Freshest fleet snapshot from adsb.fi (≤ 40 min old), or None."""
    return _newest(get_fleet_adsb())


def get_plane_state_adsblol() -> PlaneState | None:
    """Freshest fleet snapshot from adsb.lol (≤ 40 min old), or None."""
    return _newest(get_fleet_adsblol())
