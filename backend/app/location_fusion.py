"""
location_fusion.py
~~~~~~~~~~~~~~~~~~
Claim-based location fusion.

Every source produces **claims** ("he was at place P at time t") with a
reliability ``R`` (0-1). The answer is the **newest non-superseded claim with
R ≥ GATE**, refined to a better venue if one is nearby; weaker claims only fill
gaps. The displayed confidence is ``round(100 × min(R × stay(age), 0.95))``;
the score never picks between gated claims.

Everything here is pure: :func:`fuse` takes already-fetched data (calendar
events, cached geocode candidates, per-airframe flight tracks, overnight base,
newswire) and an injected ``now``, and returns a :class:`Decision`. No I/O, no
clock reads.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping

from .fleet import is_af1_callsign
from .place_aliases import find_alias

# ── Tunables ─────────────────────────────────────────────────────────────
GATE: Final = 0.6
MAX_AGE_H: Final = 7 * 24.0
FUTURE_H: Final = 24.0
SUPERSEDE_KM: Final = 60.0
BASE_KM: Final = 40.0
BASE_DWELL_STAY: Final = 0.5
REFINE_KM: Final = 75.0
REFINE_BEFORE_H: Final = 2.0
STALE_SCORE: Final = 0.3
STALE_AGE_H: Final = 72.0
MAX_CONF: Final = 0.95
EVIDENCE_SITE_KM: Final = 2.0  # evidence steps closer than this are one stay

R_CAL_TIMED: Final = 0.82
R_CAL_DATE_ONLY: Final = 0.5
R_POOL_CALL: Final = 0.5  # press logistics: never supersedes
R_GEOCODE: Final = 0.7
R_GEOCODE_SUSPICIOUS: Final = 0.45
R_DEPARTURE_DEST: Final = 0.7
R_OVERNIGHT: Final = 0.9
R_NEWSWIRE: Final = 0.45
R_EN_ROUTE: Final = 0.9
R_SCHEDULED_FLIGHT: Final = 0.85
R_LANDING_INFERRED: Final = 0.8
R_LOW_FIX_LANDING: Final = 0.6

DEPARTURE_MASK_H: Final = 0.75
LONG_LEG_KM: Final = 150.0
SCHEDULE_SPEED_KMH: Final = 800.0
SCHEDULE_PAD_H: Final = 0.5
ARRIVAL_SEARCH_PAD_H: Final = 2.0  # an arrival counts within 2 × ETA + 2 h
UNKNOWN_LEG_H: Final = 3.0  # flight time assumed when the origin is unknown

# Aircraft identity
TF_CORROBORATED: Final = 1.0
TF_TRANSIT: Final = 0.75
TF_LOCAL: Final = 0.4
TRANSIT_KM: Final = 150.0
CORROBORATE_KM: Final = 60.0
CORROBORATE_H: Final = 3.0
HOME_BASE_KM: Final = 30.0
SAME_STAY_KM: Final = 25.0

# en_route
EN_ROUTE_MAX_H: Final = 14.0
EN_ROUTE_SPEED_KMH: Final = 850.0
EN_ROUTE_PAD_H: Final = 1.0
EN_ROUTE_END_KM: Final = 75.0
LOW_FIX_FT: Final = 10_000.0
LOW_FIX_SILENCE_H: Final = 0.5
NEXT_EVENT_H: Final = 14.0

# Geocoding
SUSPICIOUS_KM: Final = 500.0
CONTEXT_WINDOW_H: Final = 36.0
FEASIBLE_KMH: Final = 1000.0
FEASIBLE_PAD_H: Final = 1.5
FEASIBLE_SLACK_KM: Final = 100.0

AIRCRAFT_KINDS: Final = frozenset(
    {"plane_air", "plane_ground", "landing", "landing_inferred", "en_route"}
)
CALENDAR_KINDS: Final = frozenset(
    {"calendar_alias", "calendar_summary", "calendar_geocode", "departure_dest"}
)

#: Presidential bases: (lat, lon) and display name
BASES: Final[dict[str, dict[str, Any]]] = {
    "dc": {"lat": 38.897676, "lon": -77.036529, "name": "The White House"},
    "fl": {"lat": 26.6758, "lon": -80.0364, "name": "Mar-a-Lago"},
    "nj": {
        "lat": 40.645560,
        "lon": -74.639170,
        "name": "Trump Nat'l Golf Club Bedminster",
    },
}

DEPARTS_RE: Final = re.compile(
    r"\bdeparts?\s+(?P<origin>.+?)\s+(?:en\s+route(?:\s+to)?|for)\s+(?P<dest>.+?)"
    r"\s*(?:\[[^\]]*\])?\s*$",
    re.I,
)
DEPARTS_ONLY_RE: Final = re.compile(r"\bdeparts?\b", re.I)
ARRIVES_RE: Final = re.compile(r"\barrives?\b", re.I)
AF1_RE: Final = re.compile(r"\bair\s+force\s+one\b", re.I)
AIRPORT_RE: Final = re.compile(
    r"airport|airfield|air\s*base|air\s+force\s+base|joint\s+base|\bafb\b|"
    r"air\s+national\s+guard|air\s+reserve|\bheliport\b|\braf\s",
    re.I,
)
NO_EVENTS_RE: Final = re.compile(r"no public events scheduled", re.I)
#: Items that place nobody: press-office lids, cancelled / postponed items
NO_PRESENCE_RE: Final = re.compile(
    r"white house press office:|\blid\b|^\s*(?:tbd:\s*)?(?:cancell?ed|postponed)\b",
    re.I,
)
#: First Lady / Vice President items (unless the President is mentioned)
COMPANION_RE: Final = re.compile(
    r"^\s*(?:tbd:\s*)?(?:the\s+)?(?:first\s+lady|vice\s+president)\b", re.I
)
PRESIDENT_RE: Final = re.compile(r"\bthe\s+president\b", re.I)
#: Press logistics and remote appearances: where the press gathers or the
#: call is listed says little about where the President is (R below the gate)
POOL_CALL_RE: Final = re.compile(
    r"\bpool\s+call\b|\b(?:tele)?phone\b|\bcalls?\s+in\b|\btele-?rally\b", re.I
)
#: Destination part of a departure summary ("… for Y", "… en route (to) Y")
DESTINATION_TAIL_RE: Final = re.compile(r"\s+(?:en\s+route(?:\s+to)?|for)\s+.*$", re.I)
#: Generic places that must never be geocoded or resolved
GENERIC_PLACE_RE: Final = re.compile(
    r"^(?:his|her|their|the president's)\b|^(?:the\s+)?(?:airport|residence|"
    r"hotel|golf club|motorcade|helicopter|landing zone)\b|^landing zone|"
    r"^(?:tbd|tba|undisclosed|location tbd)\b",
    re.I,
)


def presence(summary: str) -> str:
    """How much a schedule item says about where the President is.

    ``"none"`` for items that place nobody (lids, cancellations, First Lady /
    Vice President items), ``"weak"`` for press pool call times and phone
    appearances (R below the gate), else ``"full"``.
    """
    if NO_PRESENCE_RE.search(summary):
        return "none"
    if COMPANION_RE.search(summary) and not PRESIDENT_RE.search(summary):
        return "none"
    if POOL_CALL_RE.search(summary):
        return "weak"
    return "full"


def is_generic_place(text: str) -> bool:
    """True for "his golf club", "Landing Zone", "the airport" and the like."""
    return bool(GENERIC_PLACE_RE.search(clean(text or "")))


_TRANSLATE: Final = str.maketrans({" ": " ", "‐": "-", "‑": "-", "–": "-", "—": "-"})


def clean(text: str) -> str:
    """Normalise Unicode, squash NBSP & fancy dashes, strip + lower-case."""
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATE).strip().lower()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (km)."""
    φ1, φ2 = map(math.radians, (lat1, lat2))
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _km(a: Any, b: Any) -> float:
    return haversine_km(a.lat, a.lon, b.lat, b.lon)


def _hours(delta: dt.timedelta) -> float:
    return delta.total_seconds() / 3600.0


def base_near(lat: float, lon: float, km: float = BASE_KM) -> str | None:
    """Key of the presidential base within *km* of the point, if any."""
    for key, base in BASES.items():
        if haversine_km(lat, lon, base["lat"], base["lon"]) <= km:
            return key
    return None


def in_us(lat: float, lon: float) -> bool:
    """Rough "is this point in the United States" test (CONUS, AK, HI)."""
    return (
        (24.4 <= lat <= 49.5 and -125.0 <= lon <= -66.9)
        or (51.0 <= lat <= 72.0 and -170.0 <= lon <= -129.0)
        or (18.5 <= lat <= 22.5 and -161.0 <= lon <= -154.0)
    )


def stay(age_h: float) -> float:
    """Decay of "still there": 1.0 for 3 h, then 0.65 at 24 h, 0.47 at 72 h,
    0.2 at 7 days; 0 beyond (the claim is dropped)."""
    if age_h < 0:
        return 1.0
    if age_h <= 3:
        return 1.0
    if age_h <= 24:
        return 1.0 - 0.35 * (age_h - 3) / 21
    if age_h <= 72:
        return 0.65 - 0.18 * (age_h - 24) / 48
    if age_h <= MAX_AGE_H:
        return 0.47 - 0.27 * (age_h - 72) / 96
    return 0.0


def is_airport_venue(text: str) -> bool:
    """True for airports, air bases and heliports (arrival points, not venues)."""
    return bool(AIRPORT_RE.search(text or ""))


# ── Data model ───────────────────────────────────────────────────────────
@dataclass
class Claim:
    """A located observation: "he was at (lat, lon) at time t"."""

    kind: str
    t: dt.datetime
    lat: float
    lon: float
    R: float
    name: str
    source: str = ""
    url: str | None = None
    airframe: str | None = None
    venue: str = ""
    summary: str = ""
    future: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # Filled in by fuse()
    score: float = 0.0
    status: str = "live"  # live | dropped | ignored | superseded | expired
    why: str = ""

    def age_h(self, now: dt.datetime) -> float:
        return _hours(now - self.t)

    def as_trace(self, now: dt.datetime) -> dict[str, Any]:
        """Plain-dict view for the debug trace."""
        return {
            "kind": self.kind,
            "t": self.t.isoformat(),
            "R": round(self.R, 3),
            "score": round(self.score, 3),
            "place": self.name,
            "lat": round(self.lat, 4),
            "lon": round(self.lon, 4),
            "age_h": round(self.age_h(now), 2),
            "status": self.status,
            "why": self.why,
            **({"airframe": self.airframe} if self.airframe else {}),
        }


@dataclass
class Event:
    """A calendar item with its location resolved (or not)."""

    t: dt.datetime
    summary: str
    location: str
    has_time: bool
    lat: float | None = None
    lon: float | None = None
    name: str = ""
    how: str = ""  # alias | summary | geocode | ""
    presence: str = "full"  # full | weak | none (see presence())
    suspicious: bool = False
    departs: dict[str, Any] | None = None  # see _parse_departure

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None

    def km_to(self, point: tuple[float, float]) -> float:
        """Distance (km) from the resolved venue to *point* (inf if unknown)."""
        if self.lat is None or self.lon is None:
            return math.inf
        return haversine_km(self.lat, self.lon, *point)

    @property
    def is_arrival(self) -> bool:
        return bool(ARRIVES_RE.search(self.summary))

    @property
    def is_movement(self) -> bool:
        return bool(
            DEPARTS_ONLY_RE.search(self.summary)
            or ARRIVES_RE.search(self.summary)
            or AF1_RE.search(self.summary)
        )


@dataclass
class Track:
    """One airframe's ADS-B picture: live state this poll plus history."""

    airframe: str
    tail: str | None
    live: dict[str, Any] | None = None
    landing: dict[str, Any] | None = None  # active (not followed by airborne)
    airborne: dict[str, Any] | None = None  # last fix, if newer than landing
    takeoff: dict[str, Any] | None = None
    af1: bool = False
    home_base: tuple[float, float] | None = None
    tracker_url: str | None = None


@dataclass
class Inputs:
    """Everything the fusion needs, already fetched."""

    events: list[dict[str, Any]] = field(default_factory=list)
    geocodes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tracks: list[Track] = field(default_factory=list)
    overnight: dict[str, Any] | None = None
    news: Mapping[str, Any] | None = None
    primary_hint: str | None = None


@dataclass
class Decision:
    coords: dict[str, Any]
    pick: Claim | None
    claims: list[Claim]
    primary: str | None
    primary_gated: bool = False  # primary came from a gated aircraft claim

    def trace(self, now: dt.datetime) -> list[dict[str, Any]]:
        order = sorted(self.claims, key=lambda c: c.t, reverse=True)
        return [c.as_trace(now) for c in order]


# ── Calendar ─────────────────────────────────────────────────────────────
def _alias(text: str) -> dict[str, Any] | None:
    return find_alias(clean(text)) if text else None


Context = list[tuple[float, float, dt.datetime]]


def _near(context: Context, t: dt.datetime, k: int = 3) -> Context:
    """The *k* context points closest in time to *t* (within 36 h)."""
    near = [p for p in context if abs(_hours(p[2] - t)) <= CONTEXT_WINDOW_H]
    return sorted(near, key=lambda p: abs(_hours(p[2] - t)))[:k]


def resolve_place(
    text: str,
    geocodes: dict[str, list[dict[str, Any]]],
    context: Context | None = None,
    t: dt.datetime | None = None,
) -> tuple[float, float, str, list[dict[str, Any]]] | None:
    """Resolve a place string via alias, else a cached geocode candidate.

    With *context* (points with times) and *t*, the candidate is chosen by
    :func:`_choose_candidate`; otherwise the first one. Generic phrases
    ("his golf club", "Landing Zone") never resolve. Returns
    ``(lat, lon, name, candidates)``; candidates is empty for aliases.
    """
    if not text or is_generic_place(text):
        return None
    alias = _alias(text)
    if alias:
        return float(alias["lat"]), float(alias["lon"]), str(alias["name"]), []
    cands = geocodes.get(clean(text)) or []
    if cands:
        c = cands[0]
        if context and t is not None:
            c = _choose_candidate(cands, _near(context, t), t)[0]
        return float(c["lat"]), float(c["lon"]), text.strip(), cands
    return None


def make_resolver(
    raw: Iterable[dict[str, Any]],
    geocodes: dict[str, list[dict[str, Any]]],
    now: dt.datetime,
):
    """A ``text → (lat, lon)`` resolver using alias events near *now* as
    geocoding context (used for the overnight-base inference)."""
    context = _alias_context(raw)

    def resolve(text: str) -> tuple[float, float] | None:
        hit = resolve_place(text, geocodes, context, now)
        return (hit[0], hit[1]) if hit else None

    return resolve


def _alias_context(raw: Iterable[dict[str, Any]]) -> Context:
    """(lat, lon, t) of timed items whose location has a place alias."""
    out: Context = []
    for item in raw:
        t = item.get("dtstart_utc")
        loc = str(item.get("location") or "")
        if not isinstance(t, dt.datetime) or not item.get("has_time", True):
            continue
        if presence(str(item.get("summary") or "")) != "full":
            continue
        alias = _alias(loc)
        if alias:
            out.append((float(alias["lat"]), float(alias["lon"]), t))
    return out


def _parse_departure(
    ev: Event, geocodes: dict[str, list[dict[str, Any]]], context: Context
) -> dict[str, Any] | None:
    """Origin X / destination Y of a "departs X for|en route (to) Y" item.

    The origin named in the summary wins over the location field when both
    resolve and disagree (the feed sometimes repeats the previous venue).
    """
    m = DEPARTS_RE.search(ev.summary)
    if not m:
        if not DEPARTS_ONLY_RE.search(ev.summary):
            return None
        origin_text, dest_text = ev.location, ""
    else:
        origin_text = ev.location or m.group("origin")
        dest_text = m.group("dest").strip().rstrip(".")
    origin = (
        resolve_place(origin_text, geocodes, context, ev.t) if origin_text else None
    )
    if m:
        named = resolve_place(m.group("origin").strip(), geocodes, context, ev.t)
        if named and (
            origin is None
            or haversine_km(origin[0], origin[1], named[0], named[1]) > SUPERSEDE_KM
        ):
            origin = named
    dest = resolve_place(dest_text, geocodes, context, ev.t) if dest_text else None
    return {
        "origin_text": (m.group("origin") if m else "") or origin_text,
        "origin": (origin[0], origin[1]) if origin else None,
        "dest": (dest[0], dest[1]) if dest else None,
        "dest_name": (dest[2] if dest else dest_text) or "",
        "dest_text": dest_text,
    }


def build_events(
    raw: Iterable[dict[str, Any]],
    geocodes: dict[str, list[dict[str, Any]]],
    now: dt.datetime,
) -> list[Event]:
    """Normalise and resolve the calendar items in [now - 7 d, now + 24 h]."""
    lo, hi = now - dt.timedelta(hours=MAX_AGE_H), now + dt.timedelta(hours=FUTURE_H)
    window = [
        item
        for item in raw
        if isinstance(item.get("dtstart_utc"), dt.datetime)
        and lo - dt.timedelta(hours=CONTEXT_WINDOW_H)
        <= item["dtstart_utc"]
        <= hi + dt.timedelta(hours=CONTEXT_WINDOW_H)
    ]
    context = _alias_context(window)
    out: list[Event] = []
    for item in window:
        t = item.get("dtstart_utc")
        if not isinstance(t, dt.datetime) or not lo <= t <= hi:
            continue
        summary = str(item.get("summary") or "")
        if NO_EVENTS_RE.search(summary):
            continue
        ev = Event(
            t=t,
            summary=summary,
            location=str(item.get("location") or "").strip(),
            has_time=bool(item.get("has_time", True)),
            presence=presence(summary),
        )
        if ev.has_time and ev.presence != "none":
            ev.departs = _parse_departure(ev, geocodes, context)
        if ev.presence == "none":
            out.append(ev)  # places nobody: no location claim
            continue
        # Location alias, then geocoded location; the summary alias only when
        # the location is empty (and never on the "for / en route Y" part)
        alias = _alias(ev.location)
        generic = is_generic_place(ev.location)
        cands = geocodes.get(clean(ev.location)) if ev.location else None
        if alias:
            ev.lat, ev.lon, ev.name, ev.how = (
                float(alias["lat"]),
                float(alias["lon"]),
                str(alias["name"]),
                "alias",
            )
        elif cands and not generic:
            # Choose the candidate now (alias context), so aircraft
            # corroboration and en_route destinations see the right place;
            # the calendar claims re-check it with the aircraft context too.
            ev.how, ev.name = "geocode", ev.location
            best = _choose_candidate(cands, _near(context, t), t)[0]
            ev.lat, ev.lon = float(best["lat"]), float(best["lon"])
        elif not ev.location:
            alias = _alias(DESTINATION_TAIL_RE.sub("", summary))
            if alias:
                ev.lat, ev.lon, ev.name, ev.how = (
                    float(alias["lat"]),
                    float(alias["lon"]),
                    str(alias["name"]),
                    "summary",
                )
        out.append(ev)
    out.sort(key=lambda e: e.t)
    return out


def _choose_candidate(
    cands: list[dict[str, Any]],
    context: list[tuple[float, float, dt.datetime]],
    target: dt.datetime,
) -> tuple[dict[str, Any], bool]:
    """Pick the geocode candidate that fits the context; flag it if suspicious.

    Infeasible candidates (not reachable at :data:`FEASIBLE_KMH` from any
    context point) are dropped, the rest ranked by distance to the nearest
    context point. Suspicious = farther than :data:`SUSPICIOUS_KM` from every
    context point.
    """
    if not context:
        return cands[0], False

    def nearest(c: dict[str, Any]) -> float:
        return min(haversine_km(c["lat"], c["lon"], p[0], p[1]) for p in context)

    def feasible(c: dict[str, Any]) -> bool:
        for lat, lon, t in context:
            gap = max(abs(_hours(t - target)), 0.1) + FEASIBLE_PAD_H
            if haversine_km(c["lat"], c["lon"], lat, lon) <= gap * FEASIBLE_KMH:
                return True
        return False

    pool = [c for c in cands if feasible(c)] or cands
    best = min(pool, key=nearest)
    return best, nearest(best) > SUSPICIOUS_KM


# ── Aircraft ─────────────────────────────────────────────────────────────
def _fix(rec: dict[str, Any] | None) -> tuple[float, float] | None:
    return (float(rec["lat"]), float(rec["lon"])) if rec else None


def _near_home(track: Track, point: tuple[float, float]) -> bool:
    hb = track.home_base
    return bool(hb and haversine_km(*point, *hb) <= HOME_BASE_KM)


def _event_corroborates(
    track: Track, point: tuple[float, float] | None, when: dt.datetime | None
) -> bool:
    return point is not None and when is not None and not _near_home(track, point)


def tail_factor(
    track: Track, events: list[Event], primary: str | None
) -> tuple[float, str]:
    """Reliability factor of this airframe's current flight / stay."""
    live = track.live or {}
    if track.af1 or is_af1_callsign(live.get("flight_id")):
        return TF_CORROBORATED, "af1 callsign"
    if primary and track.airframe == primary:
        return TF_CORROBORATED, "primary airframe"
    fixes: list[tuple[tuple[float, float] | None, dt.datetime | None]] = []
    if track.takeoff:
        fixes.append((_fix(track.takeoff), track.takeoff["ts"]))
    end = _end_point(track)
    if end:
        fixes.append(end)
    for point, when in fixes:
        if not _event_corroborates(track, point, when):
            continue
        assert point is not None and when is not None
        for ev in events:
            if not (ev.has_time and ev.located and ev.is_movement):
                continue
            if abs(_hours(ev.t - when)) > CORROBORATE_H:
                continue
            if ev.km_to(point) <= CORROBORATE_KM:
                return TF_CORROBORATED, f"event: {ev.summary[:60]}"
    start = _fix(track.takeoff)
    if start and end and end[0] and haversine_km(*start, *end[0]) > TRANSIT_KM:
        return TF_TRANSIT, "transit flight"
    return TF_LOCAL, "uncorroborated local flight"


def _end_point(
    track: Track,
) -> tuple[tuple[float, float], dt.datetime] | None:
    """Where the flight is / ended: live fix, landing, or last airborne fix."""
    if track.live:
        return (float(track.live["lat"]), float(track.live["lon"])), track.live["ts"]
    if track.landing:
        return _fix(track.landing), track.landing["ts"]  # type: ignore[return-value]
    if track.airborne:
        return _fix(track.airborne), track.airborne["ts"]  # type: ignore[return-value]
    return None


def _low_and_descending(fix: dict[str, Any]) -> bool:
    alt, rate = fix.get("altitude_ft"), fix.get("vertical_rate_fpm")
    if alt is None or alt >= LOW_FIX_FT:
        return False
    if rate is None:
        prev = fix.get("prev_altitude_ft")
        return prev is not None and prev > alt
    return rate < 0


def _aircraft_claims(
    track: Track, tf: float, why: str, now: dt.datetime
) -> list[Claim]:
    tail = track.tail or track.airframe
    common = {"airframe": track.airframe, "url": track.tracker_url}
    live = track.live
    if live:
        ts = min(live["ts"], now)
        if live.get("status") == "airborne":
            return [
                Claim(
                    "plane_air",
                    ts,
                    float(live["lat"]),
                    float(live["lon"]),
                    tf,
                    f"In flight ({tail})",
                    source="ADS‑B (airborne)",
                    why=why,
                    extra={"tf": tf},
                    **common,
                )
            ]
        landing = track.landing
        if landing and (
            haversine_km(landing["lat"], landing["lon"], live["lat"], live["lon"])
            <= SAME_STAY_KM
        ):
            ts = min(ts, landing["ts"])
        return [
            Claim(
                "plane_ground",
                ts,
                float(live["lat"]),
                float(live["lon"]),
                tf,
                f"{tail} parked",
                source="ADS‑B (grounded)",
                why=why,
                extra={"tf": tf, "observed_ts": live["ts"]},
                **common,
            )
        ]
    if track.landing:
        ld = track.landing
        inferred = ld.get("kind") == "inferred"
        return [
            Claim(
                "landing_inferred" if inferred else "landing",
                ld["ts"],
                float(ld["lat"]),
                float(ld["lon"]),
                (R_LANDING_INFERRED if inferred else 1.0) * tf,
                f"Landed ({ld.get('tail') or tail})",
                source=f"ADS‑B {'inferred landing' if inferred else 'landing'}"
                f" ({ld.get('tail') or tail})",
                why=why,
                extra={"tf": tf, "landing_kind": ld.get("kind", "observed")},
                **common,
            )
        ]
    return []


def _en_route_claim(
    track: Track,
    tf: float,
    why: str,
    events: list[Event],
    now: dt.datetime,
) -> Claim | None:
    """Claim for a corroborated airframe last seen airborne (no live state)."""
    fix = track.airborne
    if not fix or track.live or tf < TF_TRANSIT:
        return None
    tail = track.tail or track.airframe
    silence_h = _hours(now - fix["ts"])
    if _low_and_descending(fix) and silence_h >= LOW_FIX_SILENCE_H:
        return Claim(
            "landing_inferred",
            fix["ts"],
            float(fix["lat"]),
            float(fix["lon"]),
            R_LOW_FIX_LANDING * tf,
            f"Landed ({tail})",
            source=f"ADS‑B inferred landing ({tail})",
            airframe=track.airframe,
            url=track.tracker_url,
            why=f"{why}; track lost low and descending",
            extra={"tf": tf, "landing_kind": "inferred"},
        )
    origin = _fix(track.takeoff)
    ref_t = track.takeoff["ts"] if track.takeoff else fix["ts"]
    dest: tuple[float, float] | None = None
    dest_name = ""
    departures = [
        ev
        for ev in events
        if ev.departs
        and ev.departs["dest"]
        and abs(_hours(ev.t - ref_t)) <= CORROBORATE_H
        and (
            origin is None or haversine_km(*origin, *ev.departs["dest"]) > SUPERSEDE_KM
        )
    ]
    if departures:
        last = max(departures, key=lambda e: e.t)
        assert last.departs is not None
        dest, dest_name = last.departs["dest"], last.departs["dest_name"]
    else:
        nxt = [
            ev
            for ev in events
            if ev.has_time
            and ev.located
            and fix["ts"] < ev.t <= fix["ts"] + dt.timedelta(hours=NEXT_EVENT_H)
        ]
        if nxt:
            ev = nxt[0]
            dest, dest_name = (ev.lat, ev.lon), ev.name  # type: ignore[assignment]
    if dest:
        flight_h = haversine_km(fix["lat"], fix["lon"], *dest) / EN_ROUTE_SPEED_KMH
        expiry_h = min(EN_ROUTE_MAX_H, flight_h + EN_ROUTE_PAD_H)
    else:
        expiry_h = EN_ROUTE_MAX_H
    return Claim(
        "en_route",
        fix["ts"],
        float(fix["lat"]),
        float(fix["lon"]),
        R_EN_ROUTE * tf,
        f"In flight, last seen {_age_text(silence_h)} ({tail})",
        source="ADS‑B (last airborne fix)",
        airframe=track.airframe,
        url=track.tracker_url,
        why=why,
        extra={
            "tf": tf,
            "origin": origin,
            "dest": dest,
            "dest_name": dest_name,
            "until": fix["ts"] + dt.timedelta(hours=expiry_h),
        },
    )


def _age_text(age_h: float) -> str:
    if age_h < 1:
        return f"{max(1, int(age_h * 60))} min ago"
    if age_h < 24:
        return f"{int(age_h)} h ago"
    return f"{int(round(age_h / 24))} d ago"


# ── Calendar claims ──────────────────────────────────────────────────────
def _calendar_claims(
    events: list[Event],
    geocodes: dict[str, list[dict[str, Any]]],
    now: dt.datetime,
    aircraft_ctx: tuple[float, float, dt.datetime] | None,
) -> list[Claim]:
    claims: list[Claim] = []
    alias_ctx = [
        (e.lat, e.lon, e.t)
        for e in events
        if e.lat is not None
        and e.lon is not None
        and e.how != "geocode"
        and e.presence == "full"
    ]
    dest_ctx = [
        (e.departs["dest"][0], e.departs["dest"][1], e.t)
        for e in events
        if e.departs and e.departs["dest"]
    ]
    for ev in events:
        if not ev.located:
            continue
        lat, lon = ev.lat, ev.lon
        assert lat is not None and lon is not None
        kind = {
            "alias": "calendar_alias",
            "summary": "calendar_summary",
            "geocode": "calendar_geocode",
        }[ev.how]
        R = R_CAL_TIMED if ev.has_time else R_CAL_DATE_ONLY
        if ev.presence == "weak":
            R = min(R, R_POOL_CALL)
        source = "Factba.se schedule"
        if ev.how == "geocode":
            source = "Factba.se schedule (geocoded)"
            near = sorted(
                (p for p in alias_ctx + dest_ctx if abs(_hours(p[2] - ev.t)) <= 36),
                key=lambda p: abs(_hours(p[2] - ev.t)),
            )[:3]
            ctx = list(near) + ([aircraft_ctx] if aircraft_ctx else [])
            best, suspicious = _choose_candidate(
                geocodes[clean(ev.location)], ctx, ev.t
            )
            lat, lon = float(best["lat"]), float(best["lon"])
            ev.lat, ev.lon = lat, lon
            if aircraft_ctx and in_us(lat, lon) != in_us(*aircraft_ctx[:2]):
                suspicious = True  # a different country than the active landing
            ev.suspicious = suspicious
            if ev.has_time and ev.presence == "full":
                R = R_GEOCODE_SUSPICIOUS if suspicious else R_GEOCODE
        claims.append(
            Claim(
                kind,
                ev.t,
                lat,
                lon,
                R,
                ev.name,
                source=source,
                venue=ev.location,
                summary=ev.summary,
                future=ev.t > now,
                why="; ".join(
                    w
                    for w, on in (
                        ("suspicious geocode", ev.suspicious),
                        ("date-only item", not ev.has_time),
                        ("pool call (press logistics)", ev.presence == "weak"),
                    )
                    if on
                ),
                extra={"has_time": ev.has_time},
            )
        )
    return claims


def _movement_claims(
    events: list[Event], now: dt.datetime
) -> tuple[list[Claim], list[dict[str, Any]]]:
    """Scheduled flights, departure destinations, and origin masks."""
    claims: list[Claim] = []
    masks: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        dep = ev.departs
        if not dep or ev.t > now:
            continue
        origin, dest = dep["origin"], dep["dest"]
        leg_km = haversine_km(*origin, *dest) if origin and dest else None
        later = [
            e
            for e in events[i + 1 :]
            if e.has_time and e.located and e.presence == "full" and e.t <= now
        ]
        mask_at = ev.t + dt.timedelta(hours=DEPARTURE_MASK_H)
        if origin and (dest is None or (leg_km or 0) > SUPERSEDE_KM) and now >= mask_at:
            # Claims up to the departure itself are masked (newer ones stand)
            masks.append({"origin": origin, "until": ev.t, "event": ev.summary})
        if not dest:
            continue
        name = dep["dest_name"] or dep["dest_text"]

        def flight_end(
            latest: dt.datetime, default: dt.datetime
        ) -> tuple[dt.datetime, bool]:
            """End of the flight: the "arrives Y" item, else *default* (ETA).

            Returns (end, listed): *listed* is True when the schedule itself
            closes the flight (an arrival, or a departure from Y).

            Other items at Y don't end it (a rally's listed start often comes
            before the real arrival). The search stops at a later departure
            from Y: he arrived before it, and an arrival listed after it
            belongs to a later trip.
            """
            assert dest is not None
            for e in events[i + 1 :]:
                if e.t > latest:
                    break
                if not (e.has_time and e.located and e.presence == "full"):
                    continue
                if e.is_arrival and e.km_to(dest) <= SUPERSEDE_KM:
                    return e.t, True
                if e.departs:
                    left = e.departs["origin"]
                    km = haversine_km(*left, *dest) if left else e.km_to(dest)
                    if km <= SUPERSEDE_KM:
                        return min(default, e.t), True
            return default, False

        if origin is None and (
            is_airport_venue(ev.location) or is_airport_venue(dep["origin_text"])
        ):
            # Taking off from an airport we cannot place (not geocoded yet):
            # in flight to Y until its arrival, else for UNKNOWN_LEG_H.
            end, end_known = flight_end(
                ev.t + dt.timedelta(hours=EN_ROUTE_MAX_H),
                ev.t + dt.timedelta(hours=UNKNOWN_LEG_H),
            )
            claims.append(
                Claim(
                    "en_route",
                    ev.t,
                    dest[0],
                    dest[1],
                    R_SCHEDULED_FLIGHT,
                    f"In flight to {name}",
                    source="Factba.se schedule (flight)",
                    summary=ev.summary,
                    extra={
                        "scheduled": True,
                        "origin": None,
                        "dest": dest,
                        "dest_name": name,
                        "until": end,
                        "end_known": end_known,
                    },
                )
            )
            dest_t, flight_until = end, end if end_known else None
        elif leg_km is not None and leg_km > LONG_LEG_KM:
            eta_h = leg_km / SCHEDULE_SPEED_KMH + SCHEDULE_PAD_H
            end, _ = flight_end(
                ev.t + dt.timedelta(hours=2 * eta_h + ARRIVAL_SEARCH_PAD_H),
                ev.t + dt.timedelta(hours=eta_h),
            )
            end_known = True  # listed, or a computed ETA
            assert origin is not None
            relay = next(
                (
                    e
                    for e in events[i + 1 :]
                    if ev.t < e.t < end
                    and e.departs
                    and e.departs["origin"]
                    and e.departs["dest"]
                    and e.presence == "full"
                    and haversine_km(*origin, *e.departs["origin"]) <= LONG_LEG_KM
                    and haversine_km(*dest, *e.departs["dest"]) <= LONG_LEG_KM
                    and haversine_km(*e.departs["origin"], *e.departs["dest"])
                    > LONG_LEG_KM
                ),
                None,
            )
            if relay is not None:
                # An itinerary item ("departs the White House en route Calgary")
                # followed by the actual long leg from the same area to the same
                # destination ("departs JBA for Calgary"): that leg is the flight.
                continue
            during = [
                e
                for e in events[i + 1 :]
                if ev.t < e.t < end
                and e.has_time
                and e.located
                and e.presence == "full"
            ]
            stayed = any(
                e.is_arrival
                and e.km_to(dest) > SUPERSEDE_KM
                and e.km_to(origin) <= SUPERSEDE_KM
                and not is_airport_venue(e.location)
                and not is_airport_venue(e.summary)
                for e in during
            )
            leaves_later = any(
                e.departs
                and (
                    haversine_km(*e.departs["origin"], *origin)
                    if e.departs["origin"]
                    else e.km_to(origin)
                )
                <= SUPERSEDE_KM
                for e in during
            )
            if stayed and not leaves_later:
                # An arrival at a venue near the origin during the flight, and
                # no later departure from there ("departs the UN for the White
                # House", then "arrives at Trump Tower"): the flight never
                # happened, so no en_route and no departure_dest. A later
                # departure from the origin means the listed time was early
                # (Sharm El-Sheikh, 2025-10-13: ceremony first, then the flight).
                continue
            claims.append(
                Claim(
                    "en_route",
                    ev.t,
                    origin[0],
                    origin[1],
                    R_SCHEDULED_FLIGHT,
                    f"In flight to {name}",
                    source="Factba.se schedule (flight)",
                    summary=ev.summary,
                    extra={
                        "scheduled": True,
                        "origin": origin,
                        "dest": dest,
                        "dest_name": name,
                        "until": end,
                        "end_known": end_known,
                    },
                )
            )
            dest_t, flight_until = end, end
        else:
            dest_t, flight_until = mask_at, None
        # Items listed during a scheduled flight are noise (ignored in fuse):
        # only an item from its end on means the schedule has moved on. Not
        # when the end is a guess (unknown origin, UNKNOWN_LEG_H).
        moved_on = flight_until or ev.t
        if dest_t > now or any(e.t > ev.t and e.t >= moved_on for e in later):
            continue  # not yet, or the schedule has moved on
        claims.append(
            Claim(
                "departure_dest",
                dest_t,
                dest[0],
                dest[1],
                R_DEPARTURE_DEST,
                name,
                source="Factba.se schedule (departure)",
                venue=dep["dest_text"],
                summary=ev.summary,
            )
        )
    return claims, masks


# ── Selection ────────────────────────────────────────────────────────────
def _interpolate(c: Claim, now: dt.datetime) -> tuple[float, float]:
    """Position along a scheduled flight (straight line in lat/lon)."""
    origin, dest, until = c.extra["origin"], c.extra["dest"], c.extra["until"]
    if origin is None:
        return dest[0], dest[1]
    total = max(_hours(until - c.t), 1e-6)
    f = min(max(_hours(now - c.t) / total, 0.0), 1.0)
    return origin[0] + f * (dest[0] - origin[0]), origin[1] + f * (dest[1] - origin[1])


def _score(c: Claim, now: dt.datetime) -> float:
    s = stay(c.age_h(now))
    if s > 0 and base_near(c.lat, c.lon):
        s = max(s, BASE_DWELL_STAY)
    return c.R * s


def _tie_key(c: Claim) -> tuple[dt.datetime, float, int]:
    return (c.t, c.R, 1 if c.kind in CALENDAR_KINDS else 0)


def fuse(inputs: Inputs, now: dt.datetime) -> Decision:
    """Turn fetched data into one location decision (see module docstring)."""
    events = build_events(inputs.events, inputs.geocodes, now)

    # 1. Aircraft: tail factors without continuity → primary → recompute
    def build_aircraft(primary: str | None) -> list[tuple[Track, float, str]]:
        return [(tr, *tail_factor(tr, events, primary)) for tr in inputs.tracks]

    def aircraft_claims(rated: list[tuple[Track, float, str]]) -> list[Claim]:
        out: list[Claim] = []
        for tr, tf, why in rated:
            out += _aircraft_claims(tr, tf, why, now)
            enr = _en_route_claim(tr, tf, why, events, now)
            if enr:
                out.append(enr)
        return [c for c in out if 0 <= c.age_h(now) <= MAX_AGE_H]

    pre_rated = build_aircraft(None)
    pre = aircraft_claims(pre_rated)
    gated_air = [c for c in pre if c.R >= GATE]
    primary_gated = bool(gated_air)
    primary = (
        max(gated_air, key=lambda c: c.t).airframe if gated_air else inputs.primary_hint
    )
    rated = build_aircraft(primary) if primary else pre_rated
    air = aircraft_claims(rated) if primary else pre
    gated_air = [c for c in air if c.R >= GATE]
    freshest_air = max(gated_air, key=lambda c: c.t) if gated_air else None
    air_ctx = (
        (freshest_air.lat, freshest_air.lon, freshest_air.t) if freshest_air else None
    )

    # 2. Calendar, movements, overnight, newswire
    claims = air + _calendar_claims(events, inputs.geocodes, now, air_ctx)
    moves, masks = _movement_claims(events, now)
    claims += moves
    if inputs.overnight and inputs.overnight.get("evening_ts"):
        on = inputs.overnight
        region = on.get("region") or base_near(on["lat"], on["lon"], 80) or "dc"
        claims.append(
            Claim(
                f"overnight_{region}",
                on["evening_ts"] + dt.timedelta(minutes=1),
                float(on["lat"]),
                float(on["lon"]),
                R_OVERNIGHT,
                str(on["name"]),
                source="Overnight inference (evening→morning pattern)",
            )
        )
    news = inputs.news
    if news and isinstance(news.get("seendate"), dt.datetime):
        claims.append(
            Claim(
                "newswire",
                news["seendate"],
                float(news["lat"]),
                float(news["lon"]),
                R_NEWSWIRE,
                str(news.get("name") or "News dateline"),
                source="GDELT dateline",
                url="https://www.gdeltproject.org/",
            )
        )

    past = [c for c in claims if not c.future and c.t <= now]
    future = [c for c in claims if c.future]
    past = [c for c in past if c.age_h(now) <= MAX_AGE_H]

    # 3. Feasibility of geocoded claims vs the freshest gated aircraft claim
    if freshest_air:
        for c in past + future:
            if c.kind != "calendar_geocode":
                continue
            gap = abs(_hours(c.t - freshest_air.t)) + FEASIBLE_PAD_H
            if _km(c, freshest_air) > FEASIBLE_KMH * gap + FEASIBLE_SLACK_KM:
                c.status, c.why = "dropped", "infeasible vs aircraft"

    # 4. en_route lifetimes, and claims ignored while a flight is active
    for c in past:
        if c.kind != "en_route":
            continue
        until = c.extra["until"]
        ended_by = [
            x
            for x in past
            if x is not c
            and x.status == "live"
            and x.t > c.t
            and x.R >= GATE
            and (
                _km(x, c) <= EN_ROUTE_END_KM
                or (
                    c.extra.get("dest")
                    and haversine_km(x.lat, x.lon, *c.extra["dest"]) <= EN_ROUTE_END_KM
                )
            )
        ]
        if now >= until:
            c.status, c.why = "expired", "flight time elapsed"
        elif ended_by and not c.extra.get("scheduled"):
            c.status, c.why = "expired", f"arrived: {ended_by[0].name}"
    # Schedule items listed during a scheduled flight (until its arrival /
    # ETA) are noise: press pool calls, "Oval Office" while airborne. They stay
    # ignored after the flight ends, or they would win as the newest items,
    # unless its end was only guessed (unknown origin: a real venue listed in
    # those UNKNOWN_LEG_H hours may be where he went). ADS-B still counts.
    for flight in past:
        if not (
            flight.kind == "en_route"
            and flight.extra.get("scheduled")
            and (
                flight.status == "live"
                or (flight.status == "expired" and flight.extra.get("end_known"))
            )
        ):
            continue
        for x in past:
            if (
                x.status == "live"
                and x.kind not in AIRCRAFT_KINDS
                and (flight.t < x.t < flight.extra["until"])
            ):
                x.status, x.why = "ignored", "listed during a scheduled flight"
    active = [c for c in past if c.kind == "en_route" and c.status == "live"]
    for flight in active:
        if flight.extra.get("scheduled"):
            continue
        if not flight.extra.get("dest"):
            continue  # unknown destination: can't tell cancelled items apart
        anchors = [
            p for p in (flight.extra.get("origin"), flight.extra.get("dest")) if p
        ]
        for x in past:
            if (
                x.status != "live"
                or x is flight
                or x.t <= flight.t
                or x.kind in AIRCRAFT_KINDS
            ):
                continue
            if all(haversine_km(x.lat, x.lon, *p) > SUPERSEDE_KM for p in anchors):
                x.status, x.why = "ignored", "far from an active flight"

    # 5. Persistent supersession + departure masks
    voters = [c for c in past if c.status in ("live", "expired") and c.R >= GATE]
    for c in past:
        if c.status not in ("live", "expired"):
            continue
        newer = [x for x in voters if x.t > c.t and _km(x, c) > SUPERSEDE_KM]
        if newer:
            by = max(newer, key=lambda x: x.t)
            if c.status == "live":
                c.status = "superseded"
            c.why = f"superseded by {by.kind} '{by.name}'"
            continue
        for m in masks:
            if (
                c.t <= m["until"]
                and haversine_km(c.lat, c.lon, *m["origin"]) <= SUPERSEDE_KM
            ):
                if c.kind in ("departure_dest", "en_route"):
                    continue
                if c.status == "live":
                    c.status = "superseded"
                c.why = f"departed: {m['event'][:60]}"
                break

    for c in past + future:
        c.score = _score(c, now) if not c.future else 0.5 * c.R

    # 6. Pick
    live = [c for c in past if c.status == "live"]
    gated = [c for c in live if c.R >= GATE]
    pick: Claim | None = None
    scheduled = False
    if gated:
        pick = max(gated, key=_tie_key)
    elif live:
        pick = max(live, key=lambda c: (c.score, c.t))
    else:
        upcoming = [c for c in future if c.status == "live"]
        if upcoming and not past:
            pick = min(upcoming, key=lambda c: c.t)
            scheduled = True

    coords = _output(pick, scheduled, past, now)
    if pick is not None and pick.airframe:
        rating = next((r for r in rated if r[0].airframe == pick.airframe), None)
        if rating:
            coords["identity"] = _identity(*rating, primary)
    return Decision(
        coords=coords,
        pick=pick,
        claims=past + future,
        primary=primary,
        primary_gated=primary_gated,
    )


# ── Output ───────────────────────────────────────────────────────────────
def _identity(track: Track, tf: float, why: str, primary: str | None) -> dict[str, Any]:
    """How an aircraft pick was rated, for the debug history."""
    return {
        "factor": tf,
        "why": why,
        "flight_id": (track.live or {}).get("flight_id") or None,
        "af1_seen": track.af1,
        "primary": track.airframe == primary,
    }


def _refine(pick: Claim, claims: list[Claim], now: dt.datetime) -> dict[str, Any]:
    """Venue / base refinement of landings, parked jets and airport arrivals."""
    out: dict[str, Any] = {"lat": pick.lat, "lon": pick.lon, "name": pick.name}
    if pick.kind in ("landing", "landing_inferred", "plane_ground"):
        base = base_near(pick.lat, pick.lon) if pick.kind != "plane_ground" else None
        if base:
            return {**out, **BASES[base], "reason": "landing_base"}
        window = [
            c
            for c in claims
            if c.kind in CALENDAR_KINDS
            and c.kind != "departure_dest"
            and c.status != "dropped"
            and pick.t - dt.timedelta(hours=REFINE_BEFORE_H) <= c.t <= now
            and _km(c, pick) <= REFINE_KM
            and not is_airport_venue(c.venue or c.name)
        ]
        if window:
            venue = max(window, key=lambda c: c.t)
            out |= {
                "lat": venue.lat,
                "lon": venue.lon,
                "name": venue.venue or venue.name,
                "event_summary": venue.summary,
            }
            if pick.kind != "plane_ground":
                out["reason"] = "landing_destination"
        return out
    if (
        pick.kind in CALENDAR_KINDS
        and is_airport_venue(pick.venue or pick.name)
        # "departs PBI" means leaving the airport; a departure_dest claim's
        # summary is the departure item too, but it places him at Y on arrival
        and (pick.kind == "departure_dest" or not DEPARTS_ONLY_RE.search(pick.summary))
    ):
        base = base_near(pick.lat, pick.lon)
        if base:
            out |= {**BASES[base], "refined": "base"}
    return out


def _output(
    pick: Claim | None, scheduled: bool, claims: list[Claim], now: dt.datetime
) -> dict[str, Any]:
    if pick is None:
        newest = max(
            (c for c in claims if c.status != "dropped"),
            key=lambda c: c.t,
            default=None,
        )
        out: dict[str, Any] = {"unknown": True, "confidence": 0, "reason": "unknown"}
        if newest:
            out["last_known"] = {
                "lat": newest.lat,
                "lon": newest.lon,
                "name": newest.name,
                "kind": newest.kind,
                "observed_at": newest.t.isoformat(),
                "age_h": round(newest.age_h(now), 2),
            }
        return out

    age_h = max(0.0, pick.age_h(now))
    out = {"reason": pick.kind, **_refine(pick, claims, now)}
    if pick.kind == "en_route":
        out["in_flight"] = True
        if pick.extra.get("scheduled"):
            out["lat"], out["lon"] = _interpolate(pick, now)
    elif pick.kind == "plane_air":
        out["in_flight"] = True
    if pick.kind in ("landing", "landing_inferred"):
        out["landing_kind"] = pick.extra.get("landing_kind", "observed")
        if age_h > STALE_AGE_H and out["reason"] in ("landing", "landing_inferred"):
            out["reason"] = "last_known"
            out["name"] = "Last known (jet arrival)"
    if pick.summary and "event_summary" not in out:
        out["event_summary"] = pick.summary
    if scheduled:
        out["name"] = f"Scheduled: {out['name']}"
        out["scheduled"] = True
        confidence = round(100 * 0.5 * pick.R)
        stale = False
    else:
        confidence = round(100 * min(pick.score, MAX_CONF))
        stale = pick.score < STALE_SCORE or age_h > STALE_AGE_H
    out |= {
        "confidence": confidence,
        "age_h": round(age_h, 2),
        "observed_at": pick.t.astimezone(dt.timezone.utc).isoformat(),
        "stale": stale,
        "_source": pick.source,
        "_url": pick.url,
    }
    if pick.airframe:
        out["airframe"] = pick.airframe
    if pick.kind == "en_route":
        if pick.extra.get("dest_name"):
            out["destination"] = pick.extra["dest_name"]
        if pick.extra.get("scheduled") and pick.extra.get("until"):
            out["eta"] = pick.extra["until"].astimezone(dt.timezone.utc).isoformat()
    if not scheduled:
        out["evidence"] = _evidence(pick, claims, now)
    return out


def _evidence(
    pick: Claim, claims: list[Claim], now: dt.datetime
) -> list[dict[str, Any]]:
    """The "how we know" steps behind *pick*: up to 4 claims, oldest first.

    Only trustworthy (gated), already-happened claims count, including places
    he has since left (``superseded``); flights other than the pick are left
    out. A claim within :data:`EVIDENCE_SITE_KM` of the next newer step is the
    same stay (White House rooms) and a (kind, place) pair appears once
    (newest kept). Each step carries its schedule text (``summary``); the last
    entry is the pick itself (``current``) with ``since``, the start of the
    current stay: the oldest claim merged into it before the last move.
    """

    def eligible(c: Claim) -> bool:
        return (
            c is not pick
            and c.status in ("live", "superseded")
            and c.R >= GATE
            and not c.future
            and c.t <= now
            and c.kind != "en_route"
        )

    seen: set[tuple[str, str]] = {(pick.kind, pick.name)}
    steps: list[Claim] = []
    anchor = pick  # newest kept step: a stay at one site is one step
    stay_start = pick.t
    in_current_stay = True
    for c in sorted(claims, key=lambda c: c.t, reverse=True):
        if not eligible(c):
            continue
        if _km(c, anchor) <= EVIDENCE_SITE_KM:
            if in_current_stay:
                stay_start = min(stay_start, c.t)
            continue
        in_current_stay = False
        key = (c.kind, c.name)
        if key in seen:
            continue
        seen.add(key)
        steps.append(c)
        anchor = c
        if len(steps) == 3:
            break

    def iso(t: dt.datetime) -> str:
        return t.astimezone(dt.timezone.utc).isoformat()

    def entry(c: Claim, current: bool) -> dict[str, Any]:
        out = {
            "t": iso(c.t),
            "place": c.name,
            "kind": c.kind,
            "source": c.source,
            "summary": c.summary,
            "current": current,
        }
        if current:
            out["since"] = iso(stay_start)
        return out

    return [entry(c, False) for c in reversed(steps)] + [entry(pick, True)]
