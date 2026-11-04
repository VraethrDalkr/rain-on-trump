"""flight_service.py
~~~~~~~~~~~~~~~~~~~~
Return **Trump-fleet** aircraft states from community ADS-B feeds:
**adsb.fi** first, then **adsb.lol**. OpenSky was dropped in 2026-11: its
terms limit the API to non-profit research/education and require a written
licence for operational use in a live service.

Every feed's snapshot gets the same freshness window and age-decayed
confidence (:func:`calculate_confidence`).

* :func:`get_fleet_states` returns every visible fleet aircraft (used by the
  location fusion, which keeps flight history per airframe).
* :func:`get_plane_state` keeps its single-state shape for
  ``/plane_state.json``: the primary airframe's state when it is visible,
  else the newest.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Final, Literal, TypedDict, cast

from dateutil import tz

from .adsbfi_service import get_fleet_adsb, get_fleet_adsblol
from .event_service import emit_api_error
from .fleet import airframe_of

# ── Constants & logging ──────────────────────────────────────────────────
UTC: Final = tz.UTC
LOG = logging.getLogger("flight_service")


#: Freshness windows for ADS‑B snapshots (seconds)
AIRBORNE_WINDOW_SEC: Final[int] = 600  # 10 min
GROUNDED_WINDOW_SEC: Final[int] = 1_200  # 20 min

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
    flight_id: str
    source: str
    vertical_rate: float | None
    altitude_known: bool


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


# ── Public helpers ───────────────────────────────────────────────────────
#: (feed name, fetcher) in priority order
#: (looked up at call time so tests can stub the module-level fetchers)
FEEDS: Final = (
    ("adsb.fi", lambda: get_fleet_adsb()),
    ("adsb.lol", lambda: get_fleet_adsblol()),
)


class FleetSnapshot(TypedDict):
    states: list[PlaneState]
    errors: list[str]


def _with_confidence(state: PlaneState) -> PlaneState | None:
    """Attach age-decayed confidence; None if the snapshot is too old."""
    age = (dt.datetime.now(UTC) - state["ts"]).total_seconds()
    conf = calculate_confidence(age, is_airborne=state["status"] == "airborne")
    return None if conf is None else cast(PlaneState, {**state, "confidence": conf})


@_memo(60)
def get_fleet_states() -> FleetSnapshot:
    """Return every fresh fleet state from the first feed that sees any.

    Feeds are tried in :data:`FEEDS` order; the next feed is queried when the
    previous one fails or sees no fresh fleet aircraft. ``errors`` lists the
    feeds that failed (an outage is not the same as aircraft silence).
    """
    errors: list[str] = []
    for name, fetch in FEEDS:
        try:
            raw = fetch() or []
        except Exception as exc:  # noqa: BLE001 – network/HTTP/parse errors
            msg = f"{name} error: {exc}"
            LOG.warning(msg)
            errors.append(msg)
            emit_api_error(name, str(exc))
            continue
        states = [
            st for st in (_with_confidence(cast(PlaneState, r)) for r in raw) if st
        ]
        if states:
            return {"states": states, "errors": errors}
    return {"states": [], "errors": errors}


def _primary_airframe() -> str | None:
    try:
        from . import arrival_cache

        return arrival_cache.primary_airframe()
    except Exception:  # noqa: BLE001 – history is optional here
        return None


def get_plane_state() -> PlaneState | dict[str, Any]:
    """Return one :class:`PlaneState` or an error envelope.

    The primary airframe's state wins when visible, else the newest state.
    Without any fresh state, return ``{"state": None, "errors": [...]}``.
    """
    snap = get_fleet_states()
    states, errors = snap["states"], snap["errors"]
    if not states:
        return {"state": None, "errors": errors}
    primary = _primary_airframe()
    mine = [s for s in states if primary and airframe_of(s["callsign"]) == primary]
    state = max(mine or states, key=lambda st: st["ts"])
    if errors:
        state = cast(PlaneState, {**state, "errors": errors})
    return state
