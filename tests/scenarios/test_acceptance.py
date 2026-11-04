"""
tests/scenarios/test_acceptance.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The 17 acceptance scenarios of the location fusion design,
plus the landing-anchor behaviours the fusion must keep. Times are ET.
"""

from __future__ import annotations

from .harness import (
    AVL,
    BED,
    CAMP_DAVID,
    ELYSEE,
    JBA,
    MAL,
    MRR,
    ORY,
    PARIS,
    PARIS_TX,
    PBI,
    TT,
    WH,
    World,
    describe,
    km,
    near,
    pos,
)

PIT_AIRPORT = (40.4915, -80.2329)
PPG_ARENA = (40.4395, -79.9892)
LATROBE_AIRPORT = (40.2747, -79.4048)
GREENSBURG_RALLY = (40.3015, -79.5056)
UN_HALL = (40.7494, -73.9681)
COLISEUM = (40.7229, -73.5904)
RIYADH = (24.7136, 46.6753)


def _not_in_flight(c: dict) -> bool:
    return not c.get("in_flight")


# 1 ────────────────────────────────────────────────────────────────────────
def test_01_backup_vc25a_training_does_not_move_him(world: World) -> None:
    w = world
    w.place("Palm Beach, FL", (26.7056, -80.0364, "us"))
    w.ev(
        "2026-11-13T15:00",
        "The President departs the White House en route to " "Palm Beach, FL",
        "South Lawn",
    )
    w.ev(
        "2026-11-13T17:45",
        "The President arrives at Palm Beach International " "Airport",
        "Palm Beach International Airport",
    )
    w.at("2026-11-13T17:41", w.plane("82-8000", PBI, "grounded", "2026-11-13T17:40"))
    w.at("2026-11-14T10:00")

    c = w.at(
        "2026-11-14T11:00",
        w.plane("92-9000", (39.2, -76.5), "airborne", "2026-11-14T11:00", 4000),
    )
    assert near(c, MAL, 5) and _not_in_flight(c), describe(c)
    c = w.at(
        "2026-11-14T12:31", w.plane("92-9000", JBA, "grounded", "2026-11-14T12:30")
    )
    assert near(c, MAL, 5) and _not_in_flight(c), describe(c)
    for t in ("2026-11-14T15:00", "2026-11-16T09:00"):
        c = w.at(t)
        assert near(c, MAL, 5) and _not_in_flight(c), (t, describe(c))


# 2 ────────────────────────────────────────────────────────────────────────
def test_02_unannounced_trip_is_followed(world: World) -> None:
    w = world
    w.ev("2026-11-13T18:00", "The President hosts a dinner", "State Dining Room")
    w.at(
        "2026-11-13T22:10",
        w.plane("82-8000", (38.85, -76.95), "airborne", "2026-11-13T22:10", 6000),
    )
    w.at(
        "2026-11-13T23:30",
        w.plane("82-8000", (33.5, -79.5), "airborne", "2026-11-13T23:30", 37000),
    )
    w.at("2026-11-14T00:31", w.plane("82-8000", PBI, "grounded", "2026-11-14T00:30"))
    for t in ("2026-11-14T01:00", "2026-11-14T12:00"):
        c = w.at(t)
        assert near(c, MAL, 5) and c["name"] == "Mar-a-Lago", (t, describe(c))


# 3 ────────────────────────────────────────────────────────────────────────
def test_03_track_lost_on_descent_rally_wins(world: World) -> None:
    w = world
    w.place("Latrobe, PA", (40.3212, -79.3795, "us"))
    w.place("Arnold Palmer Regional Airport, Latrobe, PA", (*LATROBE_AIRPORT, "us"))
    w.place(
        "Westmoreland County Fairgrounds, Greensburg, PA", (*GREENSBURG_RALLY, "us")
    )
    w.ev("2026-11-11T11:00", "Intelligence briefing", "Oval Office")
    w.ev(
        "2026-11-11T17:15",
        "The President departs the White House en route to " "Latrobe, PA",
        "South Lawn",
    )
    w.ev(
        "2026-11-11T18:40",
        "The President arrives at Arnold Palmer Regional " "Airport",
        "Arnold Palmer Regional Airport, Latrobe, PA",
    )
    w.ev(
        "2026-11-11T19:30",
        "The President delivers remarks at a rally",
        "Westmoreland County Fairgrounds, Greensburg, PA",
    )
    w.at(
        "2026-11-11T17:40",
        w.plane("92-9000", (38.83, -76.92), "airborne", "2026-11-11T17:40", 3000, 2000),
    )
    w.at(
        "2026-11-11T17:55",
        w.plane("92-9000", (39.6, -78.6), "airborne", "2026-11-11T17:55", 25000),
    )
    w.at(
        "2026-11-11T18:20",
        w.plane(
            "92-9000", (40.45, -80.05), "airborne", "2026-11-11T18:20", 8000, -1500
        ),
    )
    for t in ("2026-11-11T19:45", "2026-11-11T22:00"):
        c = w.at(t)
        assert near(c, GREENSBURG_RALLY, 5) and _not_in_flight(c), (t, describe(c))


# 4 ────────────────────────────────────────────────────────────────────────
def _transatlantic(w: World) -> None:
    w.place("Paris, France", (*PARIS, "fr"))
    w.ev(
        "2026-11-17T09:00",
        "The President departs the White House en route to " "Joint Base Andrews",
        "South Lawn",
    )
    w.ev(
        "2026-11-17T09:20",
        "The President departs Joint Base Andrews en route to " "Paris, France",
        "Joint Base Andrews",
    )
    w.at("2026-11-17T09:26", w.plane("82-8000", JBA, "grounded", "2026-11-17T09:25"))
    w.at(
        "2026-11-17T10:30",
        w.plane("82-8000", (40.5, -70.0), "airborne", "2026-11-17T10:30", 37000, 0),
    )
    for t in ("2026-11-17T11:00", "2026-11-17T14:00"):
        c = w.at(t)
        assert c.get("in_flight") is True, (t, describe(c))
        assert not near(c, JBA, 60) and not near(c, WH, 60), (t, describe(c))


def test_04a_transatlantic_then_orly(world: World) -> None:
    w = world
    _transatlantic(w)
    w.at("2026-11-17T15:31", w.plane("82-8000", ORY, "grounded", "2026-11-17T15:30"))
    c = w.at("2026-11-17T20:00")
    assert near(c, PARIS, 40) and _not_in_flight(c), describe(c)


def test_04b_transatlantic_no_landing(world: World) -> None:
    w = world
    _transatlantic(w)
    c = w.at("2026-11-17T17:00")
    assert c.get("in_flight") is True, describe(c)
    for t in ("2026-11-17T19:30", "2026-11-18T02:00"):
        c = w.at(t)
        assert _not_in_flight(c), (t, describe(c))
        # Implementation note: after the scheduled ETA the answer is the
        # scheduled destination (scheduled-flight rule); otherwise
        # stale / unknown. Never back at JBA / the White House.
        assert c.get("unknown") or c.get("stale") or near(c, PARIS, 40), describe(c)
        place = pos(c) if "lat" in c else None
        assert place is None or (km(place, JBA) > 100 and km(place, WH) > 100)


# 5 ────────────────────────────────────────────────────────────────────────
def test_05_namesake_paris_resolves_in_france(world: World) -> None:
    w = world
    w.place("Elysee Palace, Paris", (*PARIS_TX, "us"), (*ELYSEE, "fr"))
    w.at(
        "2026-11-17T13:00",
        w.plane(
            "82-8000", (48.0, -10.0), "airborne", "2026-11-17T13:00", 37000, 0, "AF1"
        ),
    )
    w.at("2026-11-17T15:31", w.plane("82-8000", ORY, "grounded", "2026-11-17T15:30"))
    w.ev(
        "2026-11-18T10:00",
        "Bilateral meeting with President Macron",
        "Elysee Palace, Paris",
    )
    for t in (
        "2026-11-17T20:00",
        "2026-11-18T08:00",
        "2026-11-18T10:30",
        "2026-11-18T16:00",
    ):
        c = w.at(t)
        assert near(c, PARIS, 40), (t, describe(c))
    c = w.at("2026-11-18T10:30")
    assert near(c, ELYSEE, 2) and c["reason"] == "calendar_geocode", describe(c)


# 6 ────────────────────────────────────────────────────────────────────────
def test_06_unseen_c32a_to_morristown(world: World) -> None:
    w = world
    w.place("Morristown, NJ", (40.7968, -74.4815, "us"))
    w.ev("2026-11-12T19:00", "The President arrives at the White House", "South Lawn")
    w.ev(
        "2026-11-13T15:00",
        "The President departs the White House en route to " "Morristown, NJ",
        "South Lawn",
    )
    w.ev(
        "2026-11-13T16:30",
        "The President arrives at Morristown Municipal Airport",
        "Morristown Municipal Airport",
    )
    w.ev("2026-11-16T11:00", "Oval Office meeting", "Oval Office")
    w.at("2026-11-12T18:41", w.plane("92-9000", JBA, "grounded", "2026-11-12T18:40"))
    for t in (
        "2026-11-13T17:00",
        "2026-11-14T12:00",
        "2026-11-15T04:00",
        "2026-11-15T20:00",
        "2026-11-16T10:59",
    ):
        c = w.at(t)
        assert near(c, BED, 5), (t, describe(c))
        assert not near(c, JBA, 30), (t, describe(c))
    for t in ("2026-11-16T11:00", "2026-11-16T15:00"):
        c = w.at(t)
        assert near(c, WH, 5), (t, describe(c))


# 7 ────────────────────────────────────────────────────────────────────────
def test_07_dc_to_pittsburgh_rally(world: World) -> None:
    w = world
    w.place("Pittsburgh", (40.4406, -79.9959, "us"))
    w.place("Pittsburgh International Airport", (*PIT_AIRPORT, "us"))
    w.place("PPG Paints Arena, Pittsburgh, PA", (*PPG_ARENA, "us"))
    w.ev("2026-11-11T11:00", "Intelligence briefing", "Oval Office")
    w.ev(
        "2026-11-11T17:15",
        "The President departs the White House en route to " "Pittsburgh",
        "South Lawn",
    )
    w.ev(
        "2026-11-11T18:40",
        "The President arrives Pittsburgh",
        "Pittsburgh International Airport",
    )
    w.ev(
        "2026-11-11T19:30",
        "The President delivers remarks at a rally",
        "PPG Paints Arena, Pittsburgh, PA",
    )
    w.ev("2026-11-11T23:00", "The President arrives at the White House", "South Lawn")
    w.ev("2026-11-12T10:30", "Executive order signing", "Oval Office")
    c = w.at(
        "2026-11-11T17:50",
        w.plane("92-9000", (38.9, -77.5), "airborne", "2026-11-11T17:50", 20000),
    )
    assert c.get("in_flight"), describe(c)
    w.at(
        "2026-11-11T18:36",
        w.plane("92-9000", PIT_AIRPORT, "grounded", "2026-11-11T18:35"),
    )
    for t in ("2026-11-11T21:05", "2026-11-11T22:30"):
        c = w.at(t)
        assert near(c, PPG_ARENA, 3), (t, describe(c))
    c = w.at("2026-11-11T23:05")
    assert near(c, WH, 5), describe(c)


# 8 ────────────────────────────────────────────────────────────────────────
def test_08_nyc_multi_stop(world: World) -> None:
    w = world
    w.place("UN General Assembly Hall", (*UN_HALL, "us"))
    w.place("Nassau Veterans Memorial Coliseum, Uniondale, NY", (*COLISEUM, "us"))
    w.ev(
        "2026-11-19T10:00",
        "The President addresses the General Assembly",
        "UN General Assembly Hall",
    )
    w.ev("2026-11-19T13:00", "The President meets with donors", "Trump Tower")
    w.ev(
        "2026-11-19T16:00",
        "The President delivers remarks",
        "Nassau Veterans Memorial Coliseum, Uniondale, NY",
    )
    c = w.at("2026-11-19T10:30")
    assert near(c, UN_HALL, 2), describe(c)
    c = w.at("2026-11-19T13:30")
    assert near(c, TT, 2), describe(c)
    c = w.at("2026-11-19T16:10")
    assert near(c, COLISEUM, 2), describe(c)


# 9 ────────────────────────────────────────────────────────────────────────
def test_09_morning_event_beats_older_landing(world: World) -> None:
    w = world
    w.ev(
        "2026-11-13T16:30",
        "The President arrives at Morristown Municipal Airport",
        "Morristown Municipal Airport",
    )
    w.ev(
        "2026-11-14T06:00", "The President participates in an interview", "Trump Tower"
    )
    w.at("2026-11-13T17:01", w.plane("82-8000", MRR, "grounded", "2026-11-13T17:00"))
    c = w.at("2026-11-14T06:30")
    assert near(c, TT, 2), describe(c)


# 10 ───────────────────────────────────────────────────────────────────────
def test_10_marine_one_to_camp_david(world: World) -> None:
    w = world
    w.ev("2026-11-14T10:00", "The President meets with advisers", "Oval Office")
    w.ev(
        "2026-11-14T13:00",
        "The President departs the White House en route to " "Camp David",
        "South Lawn",
    )
    c = w.at("2026-11-14T13:20")
    assert near(c, WH, 5), describe(c)
    c = w.at("2026-11-14T15:00")
    assert near(c, CAMP_DAVID, 5) and c["reason"] == "departure_dest", describe(c)


# 11 ───────────────────────────────────────────────────────────────────────
def test_11_backup_airborne_near_jba_on_oval_day(world: World) -> None:
    w = world
    w.ev(
        "2026-11-12T10:00",
        "The President meets with the Secretary of State",
        "Oval Office",
    )
    c = w.at(
        "2026-11-12T11:00",
        w.plane("92-9000", (38.9, -76.6), "airborne", "2026-11-12T11:00", 4000),
    )
    assert near(c, WH, 5) and _not_in_flight(c), describe(c)


# 12 ───────────────────────────────────────────────────────────────────────
def test_12_747_8_address_swap(world: World) -> None:
    w = world
    w.ev(
        "2026-11-20T17:45",
        "The President arrives at Palm Beach International " "Airport",
        "Palm Beach International Airport",
    )
    w.at("2026-11-20T17:41", w.plane("25-3300", PBI, "grounded", "2026-11-20T17:40"))
    c = w.at("2026-11-21T12:00")
    assert near(c, MAL, 5), describe(c)
    w.at(
        "2026-11-22T15:00",
        w.plane("N7478D", (26.9, -80.0), "airborne", "2026-11-22T15:00", 9000, 2000),
    )
    w.at(
        "2026-11-22T16:00",
        w.plane("N7478D", (33.0, -78.0), "airborne", "2026-11-22T16:00", 37000),
    )
    w.at("2026-11-22T17:01", w.plane("N7478D", JBA, "grounded", "2026-11-22T17:00"))
    for t in ("2026-11-22T18:00", "2026-11-23T09:00", "2026-11-24T09:00"):
        c = w.at(t)
        assert near(c, WH, 5), (t, describe(c))
        assert not near(c, MAL, 100), (t, describe(c))


# 13 ───────────────────────────────────────────────────────────────────────
def test_13_no_resurrection_of_old_landing(world: World) -> None:
    w = world
    w.at(
        "2026-11-12T18:41",
        w.plane("92-9000", JBA, "grounded", "2026-11-12T18:40", flight_id="AF1"),
    )
    w.ev(
        "2026-11-13T12:00",
        "The President plays golf",
        "Trump National Golf Club Bedminster",
    )
    c = w.at("2026-11-14T12:00")
    assert near(c, BED, 5), describe(c)
    for t in ("2026-11-16T20:00", "2026-11-17T13:00"):
        c = w.at(t)
        assert c.get("unknown") or near(c, BED, 5), (t, describe(c))
        assert c.get("unknown") or c.get("stale"), (t, describe(c))
        assert not near(c, JBA, 60), (t, describe(c))


# 14 ───────────────────────────────────────────────────────────────────────
def test_14_long_mar_a_lago_stay(world: World) -> None:
    w = world
    w.at(
        "2026-11-15T14:01",
        w.plane("82-8000", PBI, "grounded", "2026-11-15T14:00", flight_id="AF1"),
    )
    c = w.at("2026-11-18T14:00")
    assert near(c, MAL, 5) and not c.get("unknown"), describe(c)
    c = w.at("2026-11-21T12:00")
    assert near(c, MAL, 5) and not c.get("unknown"), describe(c)
    assert c["stale"] is True, describe(c)


# 15 ───────────────────────────────────────────────────────────────────────
def test_15_newswire_dateline_does_not_move_him(world: World, monkeypatch) -> None:
    from app import location_service as loc

    monkeypatch.setattr(loc, "NEWSWIRE_ENABLED", True, raising=False)
    w = world
    w.at(
        "2026-11-15T14:01",
        w.plane("82-8000", PBI, "grounded", "2026-11-15T14:00", flight_id="AF1"),
    )
    from .harness import et

    w.news = {
        "lat": 39.9042,
        "lon": 116.4074,
        "name": "News dateline: Beijing",
        "seendate": et("2026-11-18T13:30"),
    }
    c = w.at("2026-11-18T14:00")
    assert near(c, MAL, 5), describe(c)


# 16 ───────────────────────────────────────────────────────────────────────
def test_16a_date_only_trip_item_does_not_win(world: World) -> None:
    w = world
    w.place("Riyadh, Saudi Arabia", (*RIYADH, "sa"))
    w.ev("2026-11-18T16:00", "The President delivers remarks", "East Room")
    w.ev(
        "2026-11-19T00:00",
        "The President travels to Saudi Arabia",
        "Riyadh, Saudi Arabia",
        has_time=False,
    )
    w.ev("2026-11-19T13:00", "Lunch with the Vice President", "Private Dining Room")
    w.ev(
        "2026-11-19T19:00",
        "The President departs Joint Base Andrews en route to " "Riyadh",
        "Joint Base Andrews",
    )
    for t in ("2026-11-19T08:30", "2026-11-19T12:00"):
        c = w.at(t)
        assert near(c, WH, 5), (t, describe(c))


def test_16b_only_a_future_event(world: World) -> None:
    w = world
    w.ev("2026-11-20T10:00", "The President meets with advisers", "Oval Office")
    c = w.at("2026-11-19T19:00")
    assert c["name"].startswith("Scheduled"), describe(c)
    assert c["confidence"] <= 45, describe(c)


# 17 ───────────────────────────────────────────────────────────────────────
def test_17_live_af1_is_in_flight(world: World) -> None:
    w = world
    w.ev("2026-11-12T10:00", "The President meets with advisers", "Oval Office")
    c = w.at(
        "2026-11-12T11:00",
        w.plane(
            "98-0001", (39.5, -78.0), "airborne", "2026-11-12T11:00", 30000, 0, "AF1"
        ),
    )
    assert c.get("in_flight") is True and c["reason"] == "plane_air", describe(c)


# ── Landing-anchor behaviours ─────────────────────────────────────────────
def _fly_to_avl(w: World, land: str) -> None:
    """An AF1 flight from DC that lands at Asheville at *land* (ET)."""
    w.at(
        "2026-11-10T09:00",
        w.plane(
            "82-8000", (38.9, -77.2), "airborne", "2026-11-10T09:00", 9000, 2000, "AF1"
        ),
    )
    w.at(land, w.plane("82-8000", AVL, "grounded", land))


def test_landing_beats_older_calendar_event(world: World) -> None:
    w = world
    w.ev("2026-11-10T08:00", "The President meets with advisers", "Oval Office")
    _fly_to_avl(w, "2026-11-10T10:30")
    c = w.at("2026-11-10T12:30")
    assert near(c, AVL, 5) and c["reason"] == "landing", describe(c)


def test_newer_located_event_supersedes_landing(world: World) -> None:
    w = world
    _fly_to_avl(w, "2026-11-10T10:30")
    w.ev("2026-11-10T16:00", "The President meets with advisers", "Oval Office")
    c = w.at("2026-11-10T16:30")
    assert near(c, WH, 5), describe(c)


def test_locationless_newer_event_keeps_landing(world: World) -> None:
    w = world
    _fly_to_avl(w, "2026-11-10T10:30")
    w.ev("2026-11-10T12:00", "Closed Press", "")
    c = w.at("2026-11-10T12:30")
    assert c["reason"] == "landing", describe(c)


def test_destination_venue_refines_landing(world: World) -> None:
    w = world
    venue = (35.60, -82.55)  # ~18 km from the airport
    w.place("Asheville Civic Center", (*venue, "us"))
    w.ev("2026-11-10T10:00", "The President delivers remarks", "Asheville Civic Center")
    _fly_to_avl(w, "2026-11-10T10:30")
    c = w.at("2026-11-10T11:30")
    assert c["reason"] == "landing_destination" and near(c, venue, 1), describe(c)


def test_landing_near_base_snaps_to_base(world: World) -> None:
    w = world
    w.at(
        "2026-11-10T09:00",
        w.plane(
            "82-8000", (33.0, -79.0), "airborne", "2026-11-10T09:00", 30000, 0, "AF1"
        ),
    )
    w.at("2026-11-10T10:30", w.plane("82-8000", PBI, "grounded", "2026-11-10T10:30"))
    c = w.at("2026-11-10T11:30")
    assert c["reason"] == "landing_base" and c["name"] == "Mar-a-Lago", describe(c)


def test_takeoff_cancels_landing(world: World) -> None:
    w = world
    _fly_to_avl(w, "2026-11-10T10:30")
    c = w.at(
        "2026-11-10T14:00",
        w.plane(
            "82-8000", (36.0, -80.0), "airborne", "2026-11-10T14:00", 30000, 0, "AF1"
        ),
    )
    assert c["reason"] == "plane_air" and c["in_flight"], describe(c)


def test_track_lost_low_is_inferred_landing(world: World) -> None:
    w = world
    w.at(
        "2026-11-10T09:00",
        w.plane(
            "82-8000", (38.9, -77.2), "airborne", "2026-11-10T09:00", 9000, 2000, "AF1"
        ),
    )
    w.at(
        "2026-11-10T10:20",
        w.plane("82-8000", AVL, "airborne", "2026-11-10T10:20", 1800, -900),
    )
    c = w.at("2026-11-10T10:28")
    assert c["reason"] == "landing_inferred" and near(c, AVL, 5), describe(c)


def test_feed_outage_is_not_silence(world: World) -> None:
    from app import arrival_cache

    w = world
    w.at(
        "2026-11-10T10:20",
        w.plane("82-8000", AVL, "airborne", "2026-11-10T10:20", 1800, -900, "AF1"),
    )
    w.feed_errors = ["adsb.fi error: HTTP 503"]
    w.at("2026-11-10T10:28")
    assert arrival_cache.current_landing(w.now) is None
