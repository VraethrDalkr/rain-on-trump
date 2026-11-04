"""
tests/scenarios/test_feed_noise.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Regressions from the full-corpus replay review: schedule items that place
nobody, pool calls, bogus origins, unbounded arrival searches, contextual
geocoding of departure destinations and generic places. Times are ET.
"""

from __future__ import annotations

from .harness import CAMP_DAVID, MAL, WH, World, describe, km, near, pos

TURNBERRY = (55.3272, -4.8364)
ABERDEEN_SCOT = (57.1497, -2.0943)
ABERDEEN_SD = (45.4647, -98.4865)


def test_lid_at_the_white_house_does_not_undo_camp_david(world: World) -> None:
    """2026-06-19: a 17:13 "Travel/Photo lid" at the White House superseded
    the 17:08 Camp David arrival for ~14 h."""
    w = world
    w.ev(
        "2026-06-19T16:30",
        "The President departs the White House en route to " "Camp David",
        "South Lawn",
    )
    w.ev("2026-06-19T17:08", "The President arrives at Camp David", "Camp David")
    w.ev(
        "2026-06-19T17:13",
        "White House Press Office: Travel/Photo lid called",
        "The White House",
    )
    for t in ("2026-06-19T17:30", "2026-06-19T23:00", "2026-06-20T07:00"):
        c = w.at(t)
        assert near(c, CAMP_DAVID, 5), (t, describe(c))


def test_lunch_lid_cancelled_and_first_lady_items_place_nobody(
    world: World,
) -> None:
    w = world
    w.ev("2026-01-04T09:27", "The President arrives at Mar-a-Lago", "Mar-a-Lago")
    w.ev(
        "2026-01-04T11:30",
        "White House Press Office: Lunch lid until 1:00 PM",
        "The White House",
    )
    w.ev(
        "2026-01-04T12:00",
        "CANCELLED: The President holds a rally",
        "Coleman Coliseum, Tuscaloosa, AL",
    )
    w.ev("2026-01-04T12:30", "The First Lady hosts a luncheon", "State Dining Room")
    w.ev(
        "2026-01-04T12:45", "The Vice President meets with governors", "Roosevelt Room"
    )
    w.place("Coleman Coliseum, Tuscaloosa, AL", (33.2037, -87.5394, "us"))
    c = w.at("2026-01-04T13:30")
    assert near(c, MAL, 5), describe(c)


def test_first_lady_with_the_president_still_counts(world: World) -> None:
    w = world
    w.ev("2026-01-04T09:27", "The President arrives at Mar-a-Lago", "Mar-a-Lago")
    w.ev(
        "2026-01-04T12:30",
        "The First Lady and The President host a luncheon",
        "State Dining Room",
    )
    c = w.at("2026-01-04T13:00")
    assert near(c, WH, 5), describe(c)


def test_pool_call_never_supersedes(world: World) -> None:
    """Pool call items are press logistics (R 0.5): an "In-Town Pool Call
    Time" at the White House does not move him from Mar-a-Lago."""
    w = world
    w.ev("2026-03-23T07:00", "The President arrives at Mar-a-Lago", "Mar-a-Lago")
    w.ev("2026-03-23T08:00", "In-Town Pool Call Time", "The White House")
    c = w.at("2026-03-23T09:00")
    assert near(c, MAL, 5), describe(c)
    # Alone, a pool call is still a (weak) hint
    w.events[:] = []
    w.ev("2026-03-24T08:00", "In-Town Pool Call Time", "")
    c = w.at("2026-03-24T09:00")
    assert near(c, WH, 5) and c["confidence"] <= 50, describe(c)


def test_summary_origin_wins_over_a_stale_location(world: World) -> None:
    """2025-11-16 21:17: "departs Joint Base Andrews for the White House" with
    the location still "Palm Beach International Airport" was a 22 h "In flight
    to The White House"."""
    w = world
    w.ev(
        "2025-11-16T20:59",
        "The President arrives at Joint Base Andrews",
        "Palm Beach International Airport",
    )
    w.ev(
        "2025-11-16T21:17",
        "The President departs Joint Base Andrews for the " "White House",
        "Palm Beach International Airport",
    )
    w.ev(
        "2025-11-17T19:15",
        "The President arrives at the White House",
        "The White House",
    )
    for t in ("2025-11-16T22:30", "2025-11-17T09:00", "2025-11-17T14:00"):
        c = w.at(t)
        assert not c.get("in_flight") and near(c, WH, 30), (t, describe(c))


def test_arrival_search_is_bounded(world: World) -> None:
    """A long leg without a listed arrival ends at its ETA, not at an
    "arrives" item a day later."""
    w = world
    w.place("Pittsburgh International Airport", (40.4915, -80.2329, "us"))
    w.ev(
        "2026-11-10T09:00",
        "The President departs Joint Base Andrews for "
        "Pittsburgh International Airport",
        "Joint Base Andrews",
    )
    w.ev(
        "2026-11-11T09:00",
        "The President arrives at Pittsburgh International " "Airport",
        "Pittsburgh International Airport",
    )
    assert w.at("2026-11-10T09:30").get("in_flight") is True
    c = w.at("2026-11-10T12:00")  # ETA ≈ 09:00 + 0.31 h + 0.5 h
    assert not c.get("in_flight"), describe(c)


def test_departure_destination_uses_context(world: World) -> None:
    """Aberdeen after Turnberry is the Scottish one, not Aberdeen, SD."""
    w = world
    w.place("Aberdeen, Scotland", (*ABERDEEN_SD, "us"), (*ABERDEEN_SCOT, "gb"))
    w.ev("2026-07-27T08:00", "The President plays golf", "Trump Turnberry")
    w.ev(
        "2026-07-27T12:00",
        "The President departs Trump Turnberry en route " "Aberdeen, Scotland",
        "Trump Turnberry",
    )
    c = w.at("2026-07-27T12:15")
    assert c.get("in_flight") is True, describe(c)
    assert km(pos(c), TURNBERRY) < 300, describe(c)  # between the two, in Scotland


def test_generic_places_are_never_geocoded(world: World) -> None:
    w = world
    w.ev(
        "2026-03-28T08:00", "The President participates in Executive Time", "Mar-a-Lago"
    )
    w.ev(
        "2026-03-28T08:55",
        "The President departs Mar-a-Lago for his golf club",
        "Mar-a-Lago",
    )
    w.ev(
        "2026-03-28T09:30", "The President departs for the Landing Zone", "Landing Zone"
    )
    w.at("2026-03-28T10:00")
    assert not any(
        q.lower() in ("his golf club", "landing zone") for q in w.geocode_calls
    ), w.geocode_calls
