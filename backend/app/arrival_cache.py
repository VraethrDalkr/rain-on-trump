"""
arrival_cache.py
~~~~~~~~~~~~~~~~
Per-airframe **flight history**, so the location fusion can keep using a
landing (or a lost airborne track) after the aircraft stops transmitting.

``flight_history.json`` (version 2) lives in the persist dir next to
:data:`FILE` and is written atomically. It is keyed by airframe (see
``fleet.airframe_of``; both 747-8 addresses share one airframe):

* ``landing``: ``{lat, lon, ts, kind, tail}``. ``ts`` is the touchdown time
  (first grounded sighting, or the last airborne sighting for an inferred
  landing) and never moves forward while the jet stays parked. ``kind`` is
  ``"observed"`` or ``"inferred"``.
* ``airborne``: the latest airborne fix ``{lat, lon, altitude_ft,
  prev_altitude_ft, vertical_rate_fpm, ts, tail, flight_id}``. A landing only
  counts while it is newer than this.
* ``takeoff``: where and when the current (or last) flight started, plus
  ``af1``: whether that flight ever broadcast an Air Force One callsign.

The top-level ``primary`` key remembers the airframe the fusion last trusted.

Legacy files: ``last_arrival.json`` (:data:`FILE`) is still written for the
primary airframe (rollback safety; :func:`load` reads it), and the old
``last_arrival.json`` / ``last_airborne.json`` pair is migrated once when no
history file exists yet.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Final, TypedDict

from . import clock
from .fleet import airframe_of, is_af1_callsign

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
_LOCK = threading.RLock()

HISTORY_VERSION: Final = 2
_writes = 0  # bumped on every history write (decision memo key)
UNKNOWN_AIRFRAME: Final = "unknown"

# ── landing rules ────────────────────────────────────────────────────────
LANDING_MAX_AGE_H: Final[float] = 72.0
SAME_STAY_KM: Final[float] = 25.0  # grounded sightings this close = same stay
NEW_FLIGHT_GAP_H: Final[float] = 12.0  # airborne fixes further apart = new flight
# Inferred landing: last airborne fix low and descending, then silence
INFER_MAX_ALT_FT: Final[float] = 3_000.0
INFER_MAX_ALT_NO_RATE_FT: Final[float] = 1_500.0
INFER_MIN_SILENCE_S: Final[float] = 4 * 60
INFER_MAX_SILENCE_S: Final[float] = 30 * 60
INFERRED_CONF_CAP: Final[float] = 65.0


class Landing(TypedDict):
    lat: float
    lon: float
    ts: dt.datetime
    kind: str  # "observed" | "inferred"
    tail: str | None


def history_file() -> Path:
    # Derived from FILE at call time so tests that redirect FILE cover it.
    return FILE.with_name("flight_history.json")


def _airborne_file() -> Path:
    return FILE.with_name("last_airborne.json")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, φ2 = map(math.radians, (lat1, lat2))
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace *path* with *data* (temp file + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _parse_ts(raw: Any) -> dt.datetime:
    ts = dt.datetime.fromisoformat(raw)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a legacy single-record file (``lat``, ``lon``, ``ts`` required)."""
    try:
        data = json.loads(path.read_text())
        float(data["lat"]), float(data["lon"])  # validate before use
        return {**data, "ts": _parse_ts(data["ts"])}
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 – corrupted file
        LOG.warning("[arrival_cache] unreadable %s: %s", path.name, exc)
        return None


def _iso(ts: dt.datetime) -> str:
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


def _key(tail: str | None) -> str:
    return airframe_of(tail) or tail or UNKNOWN_AIRFRAME


# ── history file ─────────────────────────────────────────────────────────
def _decode_record(rec: Any) -> dict[str, Any] | None:
    if not isinstance(rec, dict):
        return None
    try:
        float(rec["lat"]), float(rec["lon"])
        return {**rec, "ts": _parse_ts(rec["ts"])}
    except Exception:  # noqa: BLE001 – drop a malformed record
        return None


def _encode_record(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if rec is None else {**rec, "ts": _iso(rec["ts"])}


def _migrate() -> dict[str, Any]:
    """Build a history from the legacy landing / airborne files."""
    hist: dict[str, Any] = {"version": HISTORY_VERSION, "airframes": {}}
    landing = _read_json(FILE)
    air = _read_json(_airborne_file())
    if landing:
        key = _key(landing.get("tail"))
        hist["airframes"].setdefault(key, {})["landing"] = {
            "lat": float(landing["lat"]),
            "lon": float(landing["lon"]),
            "ts": landing["ts"],
            "kind": str(landing.get("kind", "observed")),
            "tail": landing.get("tail"),
        }
    if air:
        key = _key(air.get("tail"))
        if air.get("tail") is None and landing and air["ts"] > landing["ts"]:
            # An unattributed takeoff superseded the landing in the old
            # single-aircraft model: keep that behaviour.
            key = _key(landing.get("tail"))
        hist["airframes"].setdefault(key, {})["airborne"] = air
    return hist


def _load() -> dict[str, Any]:
    """Return the decoded history (migrating legacy files on first use)."""
    path = history_file()
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or raw.get("version") != HISTORY_VERSION:
            raise ValueError("unsupported history version")
    except FileNotFoundError:
        return _migrate()
    except Exception as exc:  # noqa: BLE001 – corrupted file: start afresh
        LOG.warning("[arrival_cache] unreadable %s: %s", path.name, exc)
        return {"version": HISTORY_VERSION, "airframes": {}}
    airframes: dict[str, Any] = {}
    for key, rec in (raw.get("airframes") or {}).items():
        if not isinstance(rec, dict):
            continue
        airframes[key] = {
            "landing": _decode_record(rec.get("landing")),
            "airborne": _decode_record(rec.get("airborne")),
            "takeoff": _decode_record(rec.get("takeoff")),
            "af1": bool(rec.get("af1")),
        }
    return {
        "version": HISTORY_VERSION,
        "airframes": airframes,
        "primary": raw.get("primary"),
    }


def version() -> int:
    """Changes whenever this process writes the flight history."""
    return _writes


def _store(hist: dict[str, Any]) -> None:
    global _writes
    _writes += 1
    out = {
        "version": HISTORY_VERSION,
        "primary": hist.get("primary"),
        "airframes": {
            key: {
                "landing": _encode_record(rec.get("landing")),
                "airborne": _encode_record(rec.get("airborne")),
                "takeoff": _encode_record(rec.get("takeoff")),
                "af1": bool(rec.get("af1")),
            }
            for key, rec in hist["airframes"].items()
        },
    }
    _write_json(history_file(), out)


def airframes() -> dict[str, dict[str, Any]]:
    """Per-airframe flight history: ``{airframe: {landing, airborne, takeoff,
    af1}}`` with datetimes decoded (records may be None)."""
    with _LOCK:
        hist = _load()
    return {
        key: {
            "landing": rec.get("landing"),
            "airborne": rec.get("airborne"),
            "takeoff": rec.get("takeoff"),
            "af1": bool(rec.get("af1")),
        }
        for key, rec in hist["airframes"].items()
    }


def primary_airframe() -> str | None:
    """The airframe the location fusion last trusted (None if unknown)."""
    with _LOCK:
        return _load().get("primary")


def set_primary_airframe(airframe: str | None) -> None:
    """Remember *airframe* as primary (no write when unchanged)."""
    with _LOCK:
        hist = _load()
        if hist.get("primary") == airframe:
            return
        hist["primary"] = airframe
        _store(hist)


# ── recording ────────────────────────────────────────────────────────────
def _active(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The airframe's landing unless a later airborne fix superseded it."""
    landing, air = rec.get("landing"), rec.get("airborne")
    if not landing:
        return None
    if air and air["ts"] > landing["ts"]:
        return None
    return landing


def save(
    lat: float,
    lon: float,
    ts: dt.datetime | None = None,
    *,
    kind: str = "observed",
    tail: str | None = None,
) -> None:
    """Record a landing (grounded sighting or inferred touchdown).

    A repeat sighting of the same ground stay keeps the earlier touchdown
    time; an observed sighting upgrades an inferred landing at the same spot.

    Args:
        lat, lon: Decimal degrees.
        ts: Time of the sighting (UTC); defaults to now.
        kind: "observed" or "inferred".
        tail: Fleet key of the aircraft, if known.
    """
    ts = ts or clock.now()
    key = _key(tail)
    with _LOCK:
        hist = _load()
        rec = hist["airframes"].setdefault(key, {})
        current = _active(rec)
        if (
            current
            and _haversine_km(current["lat"], current["lon"], lat, lon) <= SAME_STAY_KM
        ):
            ts = min(ts, current["ts"])
            if current["kind"] == "observed":
                kind = "observed"
            if current["ts"] == ts and current["kind"] == kind:
                return  # same stay, nothing new
        landing = {"lat": float(lat), "lon": float(lon), "ts": ts}
        landing |= {"kind": kind, "tail": tail}
        rec["landing"] = landing
        _store(hist)
        primary = hist.get("primary")
        if primary is None or primary == key:
            # Legacy single-record file, kept for one release (rollback)
            _write_json(FILE, _encode_record(landing) or {})
    LOG.info("[landing saved] %s %s", key, landing)


def record_airborne(
    lat: float,
    lon: float,
    altitude_ft: float | None,
    vertical_rate_fpm: float | None,
    ts: dt.datetime,
    tail: str | None = None,
    flight_id: str | None = None,
) -> None:
    """Persist the latest airborne fix of an airframe.

    The previous fix's altitude is kept so a descent can be recognised when no
    vertical rate is reported. The first fix after a landing (or after a long
    gap) starts a new flight, whose take-off position is the landing spot when
    the aircraft was seen on the ground, else this fix.
    """
    key = _key(tail)
    with _LOCK:
        hist = _load()
        rec = hist["airframes"].setdefault(key, {})
        prev, landing = rec.get("airborne"), rec.get("landing")
        if prev and prev["ts"] >= ts:
            # A repeated (memoised) or out-of-order fix: keep the previous
            # altitude for the descent check, and don't rewrite the file
            return
        new_flight = (
            prev is None
            or (landing is not None and landing["ts"] >= prev["ts"])
            or (ts - prev["ts"]).total_seconds() > NEW_FLIGHT_GAP_H * 3600
            or rec.get("takeoff") is None
        )
        if new_flight:
            on_ground = landing and (not prev or landing["ts"] >= prev["ts"])
            origin = landing if on_ground else {"lat": lat, "lon": lon}
            rec["takeoff"] = {
                "lat": float(origin["lat"]),
                "lon": float(origin["lon"]),
                "ts": ts,
            }
            rec["af1"] = False
        rec["af1"] = bool(rec.get("af1")) or is_af1_callsign(flight_id)
        prev_alt = prev.get("altitude_ft") if prev and not new_flight else None
        rec["airborne"] = {
            "lat": float(lat),
            "lon": float(lon),
            "altitude_ft": altitude_ft,
            "prev_altitude_ft": prev_alt,
            "vertical_rate_fpm": vertical_rate_fpm,
            "ts": ts,
            "tail": tail,
            "flight_id": flight_id,
        }
        _store(hist)


def mark_af1(tail: str | None) -> None:
    """Flag the airframe's current flight / stay as flying as Air Force One."""
    key = _key(tail)
    with _LOCK:
        hist = _load()
        rec = hist["airframes"].setdefault(key, {})
        if rec.get("af1"):
            return
        rec["af1"] = True
        _store(hist)


def low_and_descending(air: dict[str, Any], max_alt_ft: float) -> bool:
    """True if the fix is at or below *max_alt_ft* and descending.

    Without a vertical rate, require a lower fix than the previous one (so a
    departure climbing out of coverage is not mistaken for an arrival).
    """
    alt, rate = air.get("altitude_ft"), air.get("vertical_rate_fpm")
    if alt is None:
        return False
    if rate is None:
        prev_alt = air.get("prev_altitude_ft")
        return alt <= min(max_alt_ft, INFER_MAX_ALT_NO_RATE_FT) and (
            prev_alt is not None and prev_alt > alt
        )
    return alt <= max_alt_ft and rate < 0


def maybe_infer_landing(
    now: dt.datetime | None = None, *, skip: set[str] | frozenset[str] = frozenset()
) -> bool:
    """Record inferred landings for airframes whose track vanished low.

    Per airframe (except those in *skip*, i.e. seen in this poll): no landing
    newer than the last airborne fix; that fix was at or below
    :data:`INFER_MAX_ALT_FT` and descending; and the aircraft has been silent
    for 4-30 minutes since. The caller must not call this while the feeds are
    failing (an outage is not silence), and a missed 4-30 min window (e.g. a
    restart) means no inference here (the fusion's low-fix rule takes over).

    Returns:
        True if an inferred landing was recorded.
    """
    now = now or clock.now()
    inferred = False
    for key, rec in airframes().items():
        if key in skip:
            continue
        air, landing = rec.get("airborne"), rec.get("landing")
        if not air or (landing and landing["ts"] >= air["ts"]):
            continue
        silence = (now - air["ts"]).total_seconds()
        if not INFER_MIN_SILENCE_S <= silence <= INFER_MAX_SILENCE_S:
            continue
        if not low_and_descending(air, INFER_MAX_ALT_FT):
            continue
        tail = air.get("tail") or (None if key == UNKNOWN_AIRFRAME else key)
        save(air["lat"], air["lon"], air["ts"], kind="inferred", tail=tail)
        inferred = True
    return inferred


# ── reading ──────────────────────────────────────────────────────────────
def _landing_of(rec: dict[str, Any]) -> Landing | None:
    landing = _active(rec)
    if not landing:
        return None
    return Landing(
        lat=float(landing["lat"]),
        lon=float(landing["lon"]),
        ts=landing["ts"],
        kind=str(landing.get("kind", "observed")),
        tail=landing.get("tail"),
    )


def current_landing(now: dt.datetime | None = None) -> Landing | None:
    """Newest active landing (primary airframe first), at most
    :data:`LANDING_MAX_AGE_H` old."""
    now = now or clock.now()
    primary = primary_airframe()
    found: list[tuple[bool, Landing]] = []
    for key, rec in airframes().items():
        landing = _landing_of(rec)
        if not landing:
            continue
        age_h = (now - landing["ts"]).total_seconds() / 3600
        if 0 <= age_h <= LANDING_MAX_AGE_H:
            found.append((key == primary, landing))
    if not found:
        return None
    return max(found, key=lambda f: (f[0], f[1]["ts"]))[1]


def landing_confidence(age_h: float, kind: str = "observed") -> float:
    """Legacy confidence curve for a landing anchor of the given age.

    85 for the first 3 h, then linear to 55 at 24 h and 40 at 72 h; inferred
    landings are capped at :data:`INFERRED_CONF_CAP`. (The fusion uses its
    own reliability × stay score.)
    """
    if age_h <= 3:
        conf = 85.0
    elif age_h <= 24:
        conf = 85.0 - 30.0 * (age_h - 3) / 21
    else:
        conf = 55.0 - 15.0 * min(age_h - 24, 48) / 48
    if kind == "inferred":
        conf = min(conf, INFERRED_CONF_CAP)
    return round(conf, 1)


def load(max_days: int = 7) -> dict | None:
    """Legacy fallback: the last landing in ``last_arrival.json``, if not older
    than *max_days*.

    Returns:
        {"lat", "lon", "name", "reason": "last_known", "confidence"} or None.
    """
    data = _read_json(FILE)
    if not data:
        return None
    age_seconds = (clock.now() - data["ts"]).total_seconds()
    if age_seconds > max_days * 86400:
        return None

    # Start at 30, decay 3 points/day, floor at 10
    confidence = max(10, 30 - int(age_seconds / 86400 * 3))
    return {
        "lat": data["lat"],
        "lon": data["lon"],
        "name": "Last known (jet arrival)",
        "reason": "last_known",
        "confidence": confidence,
    }
