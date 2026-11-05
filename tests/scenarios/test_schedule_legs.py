"""
tests/scenarios/test_schedule_legs.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Scheduled flights ("departs X for Y") that the schedule never closes with a
matching "arrives Y" item, found by the 2026-11-05 review. Times are ET.
"""

from __future__ import annotations

from .harness import MAL, PBI, TT, World, describe, near

DORAL = (25.8136, -80.3389)
PRESTWICK = (55.5094, -4.5867)
TURNBERRY = (55.3272, -4.8364)

JBA_TO_PBI = (
    "The President departs Joint Base Andrews en route "
    "Palm Beach International Airport"
)


def test_arrival_of_a_later_trip_does_not_end_the_first_flight(world: World) -> None:
    """The arrival search stops at a later departure from Y."""
    w = world
    w.place("Trump National Doral, Miami", (*DORAL, "us"))
    # Leg 1 has no "arrives" item (ETA ~10:04)
    w.ev("2026-03-06T08:00", JBA_TO_PBI, "Joint Base Andrews")
    w.ev("2026-03-06T10:30", "The President hosts a lunch", "Mar-a-Lago")
    w.ev(
        "2026-03-06T10:45",
        "The President departs Palm Beach International Airport en route "
        "Trump National Doral, Miami",
        "Palm Beach International Airport",
    )
    w.ev(
        "2026-03-06T11:30",
        "The President delivers remarks",
        "Trump National Doral, Miami",
    )
    # The return from Doral: this arrival belongs to a later trip
    w.ev(
        "2026-03-06T13:30",
        "The President arrives Palm Beach International Airport",
        "Palm Beach International Airport",
    )
    c = w.at("2026-03-06T11:00")
    assert not c.get("in_flight") and near(c, MAL, 10), describe(c)
    c = w.at("2026-03-06T12:30")
    assert not c.get("in_flight") and near(c, DORAL, 10), describe(c)


def test_items_listed_during_a_flight_stay_noise_after_its_eta(world: World) -> None:
    """An "Oval Office" item listed while airborne doesn't win after the ETA."""
    w = world
    w.ev("2026-03-06T09:00", "The President participates in a meeting", "Oval Office")
    w.ev("2026-03-06T10:00", JBA_TO_PBI, "Joint Base Andrews")
    w.ev("2026-03-06T11:00", "The President signs executive orders", "Oval Office")
    mid = w.at("2026-03-06T11:30")
    assert mid.get("in_flight"), describe(mid)
    after = w.at("2026-03-06T13:30")  # ETA ~12:04, no "arrives" item
    assert near(after, PBI, 40), describe(after)


def test_origin_named_only_in_the_summary_is_geocoded(world: World) -> None:
    """An empty location field: the summary's origin gives the real ETA."""
    w = world
    w.place("Trump Turnberry, Scotland", (*TURNBERRY, "gb"))
    w.place("Glasgow Prestwick Airport", (*PRESTWICK, "gb"))
    w.ev("2026-07-28T09:00", "The President plays golf", "Trump Turnberry, Scotland")
    w.ev(
        "2026-07-28T12:00",
        "The President departs Glasgow Prestwick Airport en route "
        "Joint Base Andrews",
        "",
    )
    c = w.at("2026-07-28T16:00")  # 4 h into a ~7.3 h flight
    assert c.get("in_flight"), describe(c)


def test_departure_destination_airport_snaps_to_base(world: World) -> None:
    """A flight to PBI with no "arrives" item ends at Mar-a-Lago, not the airport."""
    w = world
    w.ev("2026-03-06T10:00", JBA_TO_PBI, "Joint Base Andrews")
    c = w.at("2026-03-06T13:30")  # ETA ~12:04
    assert c["reason"] == "departure_dest", describe(c)
    assert near(c, MAL, 1), describe(c)


def test_a_guessed_flight_end_does_not_hide_a_real_venue(world: World) -> None:
    """Unknown origin (3 h assumed leg): a rally listed in those hours counts."""
    pit, johnstown = (40.4915, -80.2329), (40.3267, -78.9220)
    w = world
    w.place("Pittsburgh International Airport", (*pit, "us"))
    w.place("Johnstown, PA", (*johnstown, "us"))
    w.ev(
        "2026-03-06T13:00",
        "The President departs Somewhere Regional Airport en route "
        "Pittsburgh International Airport",
        "Somewhere Regional Airport",
    )
    w.ev("2026-03-06T14:30", "The President delivers remarks", "Johnstown, PA")
    c = w.at("2026-03-06T17:00")
    assert near(c, johnstown, 20), describe(c)


def test_arrival_near_the_origin_cancels_the_flight(world: World) -> None:
    """2025-09-23: "departs the UN for the White House", then "arrives at
    Trump Tower" five minutes later. The flight never happened."""
    w = world
    w.ev(
        "2026-09-23T15:00", "The President participates in a meeting", "United Nations"
    )
    w.ev(
        "2026-09-23T17:05",
        "The President departs the United Nations for the White House",
        "United Nations",
    )
    w.ev("2026-09-23T17:10", "The President arrives at Trump Tower", "Trump Tower")
    w.ev("2026-09-23T17:15", "White House Press Office: Dinner lid", "Trump Tower")
    for t in ("2026-09-23T17:30", "2026-09-23T18:30"):
        c = w.at(t)
        assert not c.get("in_flight") and near(c, TT, 10), describe(c)


def test_a_later_departure_from_the_origin_keeps_the_flight(world: World) -> None:
    """2025-10-13: "departs Sharm El Sheikh en route the White House" is listed
    before the ceremony there; the later departure for the airport shows the
    flight still happens."""
    w = world
    sharm, ceremony = (27.9773, 34.3950), (27.9128, 34.3297)
    w.place("Sharm El-Sheikh International Airport, Egypt", (*sharm, "eg"))
    w.place("Sharm El Sheikh, Egypt", (*sharm, "eg"))
    venue = "Tonino Lamborghini International Convention Center, Sharm El-Sheikh"
    w.place(venue, (*ceremony, "eg"))
    w.ev(
        "2026-10-13T10:00",
        "The President departs Sharm El Sheikh, Egypt, en route the White House",
        "Sharm El-Sheikh International Airport, Egypt",
    )
    w.ev("2026-10-13T10:48", "The President arrives at the peace ceremony", venue)
    w.ev(
        "2026-10-13T13:42",
        "The President departs the peace ceremony for the airport",
        venue,
    )
    c = w.at("2026-10-13T16:00")
    assert c.get("in_flight"), describe(c)
