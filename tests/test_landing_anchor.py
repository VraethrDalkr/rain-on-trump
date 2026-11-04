"""
tests/test_landing_anchor.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A landing outlives the parked jet's ADS-B silence: it outranks older calendar
events, snaps to a destination venue or base, and is cancelled by a later
takeoff. Tracks lost low and descending become inferred landings.

Landings only outrank the calendar when the airframe is corroborated (here:
an Air Force One callsign, :func:`cache.mark_af1`); see location_fusion.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import arrival_cache as cache
from app import location_service as loc

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
# Asheville, NC: far from every presidential base
AVL = (35.436, -82.541)


def _event(hours_ago: float, location: str = "", has_time: bool = True) -> dict:
    return {
        "summary": "The President delivers remarks",
        "location": location,
        "dtstart_utc": NOW - dt.timedelta(hours=hours_ago),
        "has_time": has_time,
    }


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch):
    """current_coords with no live plane, no overnight base, given event."""
    state: dict = {"event": None, "plane": None}

    def _fleet() -> dict:
        return {"states": [state["plane"]] if state["plane"] else [], "errors": []}

    monkeypatch.setattr(loc, "get_fleet_states", _fleet)
    monkeypatch.setattr(
        loc.cal, "get_events", lambda: [state["event"]] if state["event"] else []
    )
    monkeypatch.setattr(loc.cal, "get_overnight_base", lambda *a, **k: None)

    async def run() -> dict:
        loc.poll_flight_history()  # as the background loop does each tick
        res = await loc.current_coords()
        return res[0] if isinstance(res, tuple) else res

    state["run"] = run
    return state


# ── confidence curve ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "age_h, kind, expected",
    [
        (1, "observed", 85),
        (24, "observed", 55),
        (72, "observed", 40),
        (1, "inferred", 65),
        (24, "inferred", 55),
    ],
)
def test_landing_confidence(age_h, kind, expected) -> None:
    assert cache.landing_confidence(age_h, kind) == expected


# ── waterfall behaviour ───────────────────────────────────────────────────
async def test_landing_beats_older_calendar_event(pipeline) -> None:
    """The bug Daniel saw: after landing, location reverted to the calendar."""
    cache.save(*AVL, NOW - dt.timedelta(hours=2), tail="82-8000")
    cache.mark_af1("82-8000")
    pipeline["event"] = _event(hours_ago=6, location="The White House")

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing"
    assert coords["lat"] == pytest.approx(AVL[0])
    assert coords["confidence"] == 95  # R 1.0 × stay 1.0, capped at 0.95


async def test_newer_calendar_event_supersedes_landing(pipeline) -> None:
    cache.save(*AVL, NOW - dt.timedelta(hours=5))
    pipeline["event"] = _event(hours_ago=1, location="The White House")

    coords = await pipeline["run"]()
    assert not coords["reason"].startswith("landing")


async def test_untimed_event_same_day_does_not_supersede(pipeline) -> None:
    """A date-only item (midnight start) on the landing day is not newer."""
    landed = NOW - dt.timedelta(minutes=30)
    cache.save(*AVL, landed)
    cache.mark_af1(None)
    midnight = landed.astimezone(loc.cal.NYC).replace(hour=0, minute=0, second=0)
    pipeline["event"] = {
        **_event(0),
        "dtstart_utc": midnight.astimezone(UTC),
        "has_time": False,
    }

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing"


async def test_landing_expires_after_72h(pipeline) -> None:
    cache.save(*AVL, NOW - dt.timedelta(hours=73))

    coords = await pipeline["run"]()
    assert coords["reason"] == "last_known"


async def test_takeoff_cancels_landing(pipeline) -> None:
    cache.save(*AVL, NOW - dt.timedelta(hours=3))
    cache.record_airborne(36.0, -80.0, 30000, 0, NOW - dt.timedelta(hours=1))

    coords = await pipeline["run"]()
    assert not coords["reason"].startswith("landing")


async def test_destination_event_venue_used(pipeline) -> None:
    """Event scheduled before a late arrival, venue near the airport."""
    venue = (35.60, -82.55)  # ~18 km from AVL
    loc._put_candidates(
        "Asheville Civic Center", [{"lat": venue[0], "lon": venue[1], "name": "x"}]
    )
    cache.save(*AVL, NOW - dt.timedelta(hours=1))
    cache.mark_af1(None)
    pipeline["event"] = _event(hours_ago=2, location="Asheville Civic Center")

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing_destination"
    assert (coords["lat"], coords["lon"]) == venue
    assert coords["confidence"] == 95


async def test_far_older_event_is_departure_not_destination(pipeline) -> None:
    cache.save(*AVL, NOW - dt.timedelta(hours=1))
    cache.mark_af1(None)
    pipeline["event"] = _event(hours_ago=4, location="The White House")

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing"
    assert coords["lat"] == pytest.approx(AVL[0])


async def test_landing_at_palm_beach_snaps_to_mar_a_lago(pipeline) -> None:
    pbi = (26.6832, -80.0956)
    cache.save(*pbi, NOW - dt.timedelta(hours=1))

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing_base"
    assert coords["name"] == "Mar-a-Lago"


async def test_grounded_sighting_records_touchdown_once(pipeline) -> None:
    """Repeat grounded polls keep the first touchdown time."""
    first = NOW - dt.timedelta(minutes=10)
    for ts in (first, NOW - dt.timedelta(minutes=5), NOW):
        pipeline["plane"] = {
            "callsign": "82-8000",
            "lat": AVL[0],
            "lon": AVL[1],
            "altitude": 0.0,
            "on_ground": True,
            "ts": ts,
            "status": "grounded",
            "confidence": 90,
        }
        await pipeline["run"]()

    assert cache.current_landing()["ts"] == first


# ── inferred landings ─────────────────────────────────────────────────────
def test_track_lost_low_and_descending_is_inferred_landing() -> None:
    seen = NOW - dt.timedelta(minutes=8)
    cache.record_airborne(*AVL, 1800, -900, seen, "adfdf8")

    assert cache.maybe_infer_landing(NOW) is True
    landing = cache.current_landing(NOW)
    assert landing["kind"] == "inferred"
    assert landing["ts"] == seen


@pytest.mark.parametrize(
    "alt, rate, minutes_ago",
    [
        (31000, 0, 8),  # lost at cruise: coverage gap, not a landing
        (2500, 600, 8),  # climbing out after takeoff
        (1800, -900, 2),  # too soon to call it
        (1800, -900, 45),  # too stale to call it
        (2500, None, 8),  # no rate and not low enough
    ],
)
def test_no_inferred_landing(alt, rate, minutes_ago) -> None:
    cache.record_airborne(*AVL, alt, rate, NOW - dt.timedelta(minutes=minutes_ago))
    assert cache.maybe_infer_landing(NOW) is False


def test_grounded_sighting_upgrades_inferred_landing() -> None:
    seen = NOW - dt.timedelta(minutes=8)
    cache.record_airborne(*AVL, 1800, -900, seen)
    cache.maybe_infer_landing(NOW)

    cache.save(AVL[0] + 0.02, AVL[1], NOW, kind="observed")
    landing = cache.current_landing(NOW)
    assert landing["kind"] == "observed"
    assert landing["ts"] == seen


# ── review regressions ────────────────────────────────────────────────────
async def test_locationless_newer_event_does_not_cancel_landing(pipeline) -> None:
    """ "Closed Press"-style items place no one, so the landing stands."""
    cache.save(*AVL, NOW - dt.timedelta(hours=3))
    pipeline["event"] = {**_event(hours_ago=1), "summary": "Closed Press"}

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing"


async def test_unknown_altitude_never_infers_landing(pipeline) -> None:
    """An airborne fix without altitude (stored as 0.0) is not 'low'."""
    pipeline["plane"] = {
        "callsign": "82-8000",
        "lat": 36.0,
        "lon": -80.0,
        "altitude": 0.0,
        "altitude_known": False,
        "vertical_rate": None,
        "on_ground": False,
        "ts": NOW - dt.timedelta(minutes=8),
        "status": "airborne",
        "confidence": 95,
    }
    await pipeline["run"]()
    assert cache.maybe_infer_landing(NOW) is False


async def test_feed_outage_is_not_silence(pipeline, monkeypatch) -> None:
    cache.record_airborne(*AVL, 1800, -900, NOW - dt.timedelta(minutes=8))
    monkeypatch.setattr(
        loc, "get_fleet_states", lambda: {"states": [], "errors": ["adsb.fi: 503"]}
    )
    await pipeline["run"]()
    assert cache.current_landing(NOW) is None


async def test_silent_feed_infers_landing_through_pipeline(pipeline) -> None:
    cache.record_airborne(
        *AVL, 1800, -900, NOW - dt.timedelta(minutes=8), flight_id="AF1"
    )

    coords = await pipeline["run"]()
    assert coords["reason"] == "landing_inferred"
    assert coords["confidence"] == 80  # R 0.8 (inferred) × stay 1.0


def test_climb_out_without_rate_is_not_a_landing() -> None:
    cache.record_airborne(*AVL, 600, None, NOW - dt.timedelta(minutes=9))
    cache.record_airborne(*AVL, 1200, None, NOW - dt.timedelta(minutes=8))
    assert cache.maybe_infer_landing(NOW) is False


def test_descent_without_rate_is_a_landing() -> None:
    cache.record_airborne(*AVL, 2500, None, NOW - dt.timedelta(minutes=9))
    cache.record_airborne(*AVL, 1200, None, NOW - dt.timedelta(minutes=8))
    assert cache.maybe_infer_landing(NOW) is True


def test_airborne_sighting_cancels_inferred_landing() -> None:
    cache.record_airborne(*AVL, 1800, -900, NOW - dt.timedelta(minutes=8))
    cache.maybe_infer_landing(NOW)
    cache.record_airborne(36.0, -80.0, 9000, 1500, NOW)
    assert cache.current_landing(NOW) is None


async def test_corrupt_landing_file_does_not_break_pipeline(pipeline) -> None:
    cache.FILE.write_text('{"ts": "2026-11-04T00:00:00+00:00"}')

    coords = await pipeline["run"]()
    assert coords is not None
    assert not coords.get("reason", "").startswith("landing")


def test_cache_files_are_world_readable() -> None:
    cache.save(*AVL, NOW)
    assert cache.FILE.stat().st_mode & 0o777 == 0o644
