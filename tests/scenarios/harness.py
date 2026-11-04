"""
tests/scenarios/harness.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
A small world for time-frozen location scenarios: stubbed calendar events,
fleet states (one ADS-B poll per :meth:`World.at`), geocoder candidates and
newswire, with ``app.clock.now`` frozen and the persist dir in ``tmp_path``.

Times are written in Eastern Time ("2026-11-13T17:40"), as Factba.se does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from types import SimpleNamespace
from typing import Any

from dateutil import tz

from app import calendar_service as cal
from app import clock
from app import location_service as loc

NYC = tz.gettz("America/New_York")
UTC = dt.timezone.utc

# ── Places ────────────────────────────────────────────────────────────────
WH = (38.897676, -77.036529)
JBA = (38.81083, -76.86694)
MAL = (26.6758, -80.0364)
PBI = (26.6832, -80.0956)
BED = (40.64556, -74.63917)
MRR = (40.79935, -74.41487)
TT = (40.7625, -73.973)
ORY = (48.7262, 2.3652)
PARIS = (48.8566, 2.3522)
ELYSEE = (48.8704, 2.3167)
PARIS_TX = (33.6609, -95.5555)
CAMP_DAVID = (39.6481, -77.4650)
AVL = (35.436, -82.541)  # Asheville, NC: far from every base


def et(s: str) -> dt.datetime:
    """Eastern-time ISO string → aware UTC datetime."""
    return dt.datetime.fromisoformat(s).replace(tzinfo=NYC).astimezone(UTC)


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def pos(coords: dict) -> tuple[float, float]:
    return (coords["lat"], coords["lon"])


def near(coords: dict, place: tuple[float, float], radius_km: float = 40.0) -> bool:
    return "lat" in coords and km(pos(coords), place) <= radius_km


def _clean(text: str) -> str:
    return loc._clean(text)


class World:
    """Stubbed data sources + frozen clock for one scenario."""

    def __init__(self, monkeypatch: Any) -> None:
        self.mp = monkeypatch
        self.events: list[dict] = []
        self.geo: dict[str, list[tuple[float, float, str]]] = {}
        self.fleet: list[dict] = []
        self.feed_errors: list[str] = []
        self.news: dict | None = None
        self.now = et("2026-11-04T12:00")
        self.geocode_calls: list[str] = []

        monkeypatch.setattr(clock, "now", lambda: self.now)
        clock._test_now = self.now  # type: ignore[attr-defined]
        monkeypatch.setattr(cal, "_fetch_events", lambda: list(self.events))
        monkeypatch.setattr(loc, "_geocode_raw", self._fake_geocode, raising=False)
        monkeypatch.setattr(loc, "add_geocode_entry", lambda **_: None, raising=False)

        def _snapshot() -> dict:
            return {"states": list(self.fleet), "errors": list(self.feed_errors)}

        def _plane_state() -> dict:  # pre-rewrite pipeline
            if not self.fleet:
                return {"state": None, "errors": list(self.feed_errors)}
            return max(self.fleet, key=lambda s: s["ts"])

        monkeypatch.setattr(loc, "get_fleet_states", _snapshot, raising=False)
        monkeypatch.setattr(loc, "get_plane_state", _plane_state, raising=False)

        async def _news() -> dict | None:
            return self.news

        monkeypatch.setattr(loc, "get_latest_location", _news, raising=False)

    # ── stubs ──────────────────────────────────────────────────────────
    def _fake_geocode(
        self,
        query: str,
        timeout: Any = None,
        country_codes: str | None = None,
        exactly_one: bool = True,
        **_: Any,
    ) -> Any:
        self.geocode_calls.append(query)
        cands = self.geo.get(_clean(query), [])
        if country_codes:
            cands = [c for c in cands if c[2] == country_codes]
        objs = [
            SimpleNamespace(
                latitude=lat,
                longitude=lon,
                address=f"{query} ({cc})",
                raw={
                    "importance": 0.6,
                    "address": {"country_code": cc, "country": cc.upper()},
                },
            )
            for lat, lon, cc in cands
        ]
        if exactly_one:
            return objs[0] if objs else None
        return objs

    # ── building the world ─────────────────────────────────────────────
    def ev(self, when: str, summary: str, location: str = "", **kw: Any) -> None:
        has_time = kw.get("has_time", True)
        self.events.append(
            {
                "dtstart_utc": et(when),
                "summary": summary,
                "location": location,
                "has_time": has_time,
            }
        )

    def place(self, query: str, *cands: tuple[float, float, str]) -> None:
        self.geo[_clean(query)] = list(cands)

    @staticmethod
    def plane(
        tail: str,
        where: tuple[float, float],
        status: str,
        when: str,
        alt: float = 0.0,
        vr: float | None = None,
        flight_id: str = "",
    ) -> dict:
        airborne = status == "airborne"
        return {
            "callsign": tail,
            "lat": where[0],
            "lon": where[1],
            "altitude": alt,
            "altitude_known": True,
            "on_ground": not airborne,
            "ts": et(when),
            "status": status,
            "tracker_url": f"https://globe.adsbexchange.com/?tail={tail}",
            "confidence": 95 if airborne else 90,
            "flight_id": flight_id,
            "source": "adsb.fi",
            "vertical_rate": vr,
        }

    # ── running ────────────────────────────────────────────────────────
    def at(self, when: str, *fleet: dict) -> dict:
        """Poll at *when* (ET) seeing *fleet*, as the background loop would."""
        self.now = et(when)
        clock._test_now = self.now  # type: ignore[attr-defined]
        self.fleet = list(fleet)
        loc._cached.clear()
        if hasattr(loc, "poll_flight_history"):
            loc.poll_flight_history()  # the background loop records history
        if hasattr(loc, "pregeocode_events"):
            loc.pregeocode_events(limit=100)
        res = asyncio.run(loc.current_coords())
        return res[0] if isinstance(res, tuple) else res


def describe(coords: dict) -> str:
    """One-line summary for assertion messages."""
    keys = ("reason", "name", "lat", "lon", "confidence", "in_flight", "stale")
    return ", ".join(f"{k}={coords.get(k)}" for k in keys)
