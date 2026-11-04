"""
fleet.py
~~~~~~~~
Single source of truth for every aircraft *Is It Raining on Trump?* tracks.

Keys are tail numbers / USAF serials; each entry carries:
    ``icao``             : six-digit Mode-S hex code (lower-case)
    ``role``             : plain descriptor ("vc25a", "vc25b_bridge", "c32a", "private")
    ``require_callsign`` : only count a sighting when the broadcast flight ID is
                           an Air Force One callsign (see :data:`AF1_CALLSIGNS`)
    ``airframe``         : physical aircraft; several Mode-S addresses may map
                           to one airframe (flight history is kept per airframe)
    ``home_base``        : (lat, lon) of the airframe's home field, or None

Hex codes were verified on 2026-11-04 against the tar1090-db aircraft database
(snapshot 2026-11-02) and hexdb.io.

* The VC-25A pair and the ex-Qatari 747-8 ("VC-25B bridge", reported in service
  as Air Force One since July 2026) almost only fly with the President, so any
  sighting counts. The 747-8 is listed under its military serial and under its
  earlier civil registration, because it may broadcast either address.
* The C-32A pool routinely carries the Vice President, the First Lady and
  cabinet members, so a C-32A only counts when it flies as Air Force One.
* N757AF ("Trump Force One") is Trump's private 757.
"""

from __future__ import annotations

from typing import Final, TypedDict

#: Joint Base Andrews, home of the 89th Airlift Wing and the VC-25s
JBA: Final[tuple[float, float]] = (38.810830, -76.866940)


class FleetEntry(TypedDict):
    icao: str
    role: str
    require_callsign: bool
    airframe: str
    home_base: tuple[float, float] | None


def _entry(
    icao: str,
    role: str,
    require_callsign: bool = False,
    *,
    airframe: str,
    home_base: tuple[float, float] | None = JBA,
) -> FleetEntry:
    return {
        "icao": icao,
        "role": role,
        "require_callsign": require_callsign,
        "airframe": airframe,
        "home_base": home_base,
    }


FLEET: Final[dict[str, FleetEntry]] = {
    # VC-25A (747-200), "Air Force One" through mid-2026
    "82-8000": _entry("adfdf8", "vc25a", airframe="82-8000"),
    "92-9000": _entry("adfdf9", "vc25a", airframe="92-9000"),
    # Ex-Qatari 747-8, presidential stopgap; tar1090-db lists the serial as
    # "25-33000" (sic) and the civil registration N7478D (owner USAF)
    "25-3300": _entry("af83f3", "vc25b_bridge", airframe="vc25b-bridge"),
    "N7478D": _entry("aa0fff", "vc25b_bridge", airframe="vc25b-bridge"),
    # C-32A (757-200) pool of the 89th Airlift Wing: only with AF1 callsign
    "98-0001": _entry("adfeb7", "c32a", require_callsign=True, airframe="98-0001"),
    "98-0002": _entry("adfeb8", "c32a", require_callsign=True, airframe="98-0002"),
    "99-0003": _entry("adfeb9", "c32a", require_callsign=True, airframe="99-0003"),
    "99-0004": _entry("adfeba", "c32a", require_callsign=True, airframe="99-0004"),
    "09-0015": _entry("ae4ae6", "c32a", require_callsign=True, airframe="09-0015"),
    "09-0016": _entry("ae4ae8", "c32a", require_callsign=True, airframe="09-0016"),
    "09-0017": _entry("ae4aea", "c32a", require_callsign=True, airframe="09-0017"),
    # Trump Force One (private 757-200, DJT Operations I LLC)
    "N757AF": _entry("aa3410", "private", airframe="N757AF", home_base=None),
}

# Broadcast flight IDs that mean "the President is aboard". Unverified which
# form the transponder actually sends; fleet sightings log their flight ID so
# this can be checked in /debug/history.
AF1_CALLSIGNS: Final[frozenset[str]] = frozenset({"AF1", "AF01", "AIRFORCE1"})

TAIL_BY_ICAO: Final[dict[str, str]] = {
    meta["icao"]: tail for tail, meta in FLEET.items()
}


AIRFRAME_BY_TAIL: Final[dict[str, str]] = {
    tail: meta["airframe"] for tail, meta in FLEET.items()
}


def airframe_of(tail: str | None) -> str | None:
    """Airframe key for a fleet tail (None for unknown or missing tails)."""
    return AIRFRAME_BY_TAIL.get(tail or "")


def home_base_of(airframe: str | None) -> tuple[float, float] | None:
    """Home field of *airframe* (None if unknown or not based anywhere)."""
    for meta in FLEET.values():
        if meta["airframe"] == airframe:
            return meta["home_base"]
    return None


def is_af1_callsign(flight_id: str | None) -> bool:
    """True if *flight_id* is an Air Force One callsign (padding ignored)."""
    return (flight_id or "").strip().upper().replace(" ", "") in AF1_CALLSIGNS


def counts_as_president(tail: str, flight_id: str | None) -> bool:
    """Return True if a sighting of *tail* broadcasting *flight_id* counts.

    Args:
        tail: Fleet key (tail number or USAF serial).
        flight_id: Broadcast callsign/flight ID, possibly padded or empty.

    Returns:
        False for aircraft outside the fleet, and for callsign-gated aircraft
        that are not flying as Air Force One.
    """
    meta = FLEET.get(tail)
    if meta is None:
        return False
    if not meta["require_callsign"]:
        return True
    return is_af1_callsign(flight_id)


__all__ = [
    "AF1_CALLSIGNS",
    "AIRFRAME_BY_TAIL",
    "FLEET",
    "JBA",
    "TAIL_BY_ICAO",
    "airframe_of",
    "counts_as_president",
    "home_base_of",
    "is_af1_callsign",
]
