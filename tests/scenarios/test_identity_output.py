"""
tests/scenarios/test_identity_output.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
An aircraft pick carries an ``identity`` block (factor, reason, broadcast
callsign, AF1 seen, primary), so the debug history shows after the fact how a
flight was rated. Times are ET.
"""

from __future__ import annotations

from .harness import AVL, JBA, World, describe


def test_af1_flight_reports_its_identity(world: World) -> None:
    w = world
    c = w.at(
        "2026-11-12T12:00",
        w.plane("92-9000", AVL, "airborne", "2026-11-12T11:59", 30000, flight_id="AF1"),
    )
    assert c["reason"] == "plane_air", describe(c)
    ident = c["identity"]
    assert ident["factor"] == 1.0 and ident["why"] == "af1 callsign", ident
    assert ident["flight_id"] == "AF1" and ident["af1_seen"] is True, ident
    assert ident["primary"] is True, ident


def test_calendar_pick_has_no_identity(world: World) -> None:
    w = world
    w.ev("2026-11-12T10:00", "The President delivers remarks", "The White House")
    c = w.at("2026-11-12T11:00")
    assert c["reason"].startswith("calendar"), describe(c)
    assert "identity" not in c, describe(c)


def test_unconfirmed_landing_reports_its_factor(world: World) -> None:
    """No callsign, no schedule item: a transit flight (0.75, trusted) becomes
    the primary airframe, and the record says so."""
    w = world
    w.at("2026-11-12T09:00", w.plane("92-9000", JBA, "grounded", "2026-11-12T08:59"))
    w.at(
        "2026-11-12T10:00",
        w.plane("92-9000", JBA, "airborne", "2026-11-12T09:59", 20000),
    )
    c = w.at(
        "2026-11-12T12:30",
        w.plane("92-9000", AVL, "grounded", "2026-11-12T12:29"),
    )
    ident = c["identity"]
    assert ident["flight_id"] is None and ident["af1_seen"] is False, ident
    assert ident["why"] == "primary airframe" and ident["primary"] is True, ident
