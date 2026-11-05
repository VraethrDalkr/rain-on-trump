"""
tests/scenarios/test_evidence_output.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The public ``coords`` fields that back the frontend's details sheet:
``evidence`` (the "how we know" steps), and ``destination`` / ``eta`` for a
scheduled flight. Times are ET.
"""

from __future__ import annotations

import datetime as dt

from .harness import World, et


def _trip(w: World) -> None:
    w.place("Palm Beach, FL", (26.7056, -80.0364, "us"))
    w.ev(
        "2026-11-13T14:00",
        "The President departs Joint Base Andrews en route to Palm Beach, FL",
        "Joint Base Andrews",
    )
    w.ev(
        "2026-11-13T16:10",
        "The President arrives at Palm Beach International Airport",
        "Palm Beach International Airport",
    )
    w.ev(
        "2026-11-13T16:40",
        "The President arrives at Mar-a-Lago",
        "Mar-a-Lago",
    )


def test_evidence_ends_with_pick(world: World) -> None:
    _trip(world)
    coords = world.at("2026-11-13T18:00")

    ev = coords["evidence"]
    assert 1 <= len(ev) <= 4
    assert ev[-1]["current"] is True
    assert ev[-1]["place"] == coords["name"]
    assert all(not e["current"] for e in ev[:-1])
    times = [dt.datetime.fromisoformat(e["t"]) for e in ev]
    assert times == sorted(times)
    assert {"t", "place", "kind", "source", "current"} <= set(ev[-1])
    # Places he has since left (superseded claims) are part of the story
    assert ev[0]["place"].startswith("Joint Base Andrews")
    assert len(ev) == 3


def test_evidence_skips_weak_and_future(world: World) -> None:
    _trip(world)
    world.ev(
        "2026-11-13T17:00",
        "Out-of-Town Travel Pool Call Time",
        "Mar-a-Lago",
    )
    world.ev("2026-11-13T20:00", "The President attends a Dinner", "Mar-a-Lago")
    coords = world.at("2026-11-13T18:00")

    now = et("2026-11-13T18:00")
    for e in coords["evidence"]:
        assert dt.datetime.fromisoformat(e["t"]) <= now
    assert len({(e["kind"], e["place"]) for e in coords["evidence"]}) == len(
        coords["evidence"]
    )
    assert not any(
        dt.datetime.fromisoformat(e["t"]) == et("2026-11-13T17:00")
        for e in coords["evidence"]
    )


def test_en_route_scheduled_has_destination_and_eta(world: World) -> None:
    _trip(world)
    coords = world.at("2026-11-13T14:30")

    assert coords["in_flight"] is True
    assert coords["destination"].startswith("Palm Beach")
    assert dt.datetime.fromisoformat(coords["eta"]) > et("2026-11-13T14:30")


def test_unknown_has_no_evidence(world: World) -> None:
    coords = world.at("2026-11-13T18:00")

    assert coords.get("unknown") is True
    assert "evidence" not in coords
    assert "destination" not in coords


def test_evidence_merges_a_stay_at_one_site(world: World) -> None:
    """Rooms of the White House (and the Mall) are one place, not four steps."""
    world.ev(
        "2026-11-13T10:00", "The President speaks", "Lincoln Memorial Reflecting Pool"
    )
    world.ev("2026-11-13T12:00", "The President signs bills", "Oval Office")
    world.ev("2026-11-13T15:00", "The President hosts a reception", "East Room")
    coords = world.at("2026-11-13T18:00")

    assert len(coords["evidence"]) == 1
    assert coords["evidence"][0]["current"] is True


def test_evidence_steps_carry_the_event_text(world: World) -> None:
    """Each step says which schedule item it comes from (findable on Factba.se)."""
    _trip(world)
    coords = world.at("2026-11-13T18:00")

    by_place = {e["place"]: e["summary"] for e in coords["evidence"]}
    assert by_place["Mar-a-Lago, FL"] == "The President arrives at Mar-a-Lago"
    assert any(
        s.startswith("The President departs Joint Base Andrews")
        for s in by_place.values()
    )


def test_current_step_says_when_the_stay_began(world: World) -> None:
    """The current step's `since` is the start of the stay, not its newest report."""
    world.ev(
        "2026-11-13T10:00", "The President speaks", "Lincoln Memorial Reflecting Pool"
    )
    world.ev("2026-11-13T12:00", "The President signs bills", "Oval Office")
    world.ev("2026-11-13T15:00", "The President hosts a reception", "East Room")
    coords = world.at("2026-11-13T18:00")

    current = coords["evidence"][-1]
    assert dt.datetime.fromisoformat(current["t"]) == et("2026-11-13T15:00")
    assert dt.datetime.fromisoformat(current["since"]) == et("2026-11-13T10:00")


def test_stay_start_stops_at_the_last_move(world: World) -> None:
    """A visit to the same place before a trip is a different stay."""
    _trip(world)
    world.ev("2026-11-13T09:00", "The President has lunch", "The White House")
    world.ev("2026-11-13T21:00", "The President dines", "Mar-a-Lago")
    coords = world.at("2026-11-13T22:00")

    since = dt.datetime.fromisoformat(coords["evidence"][-1]["since"])
    assert since == et("2026-11-13T16:40")  # arrived at Mar-a-Lago, not earlier
