"""
tests/test_calendar_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Exercises calendar_service.current_event() and _fetch_events() logic.

NOTE: The _fetch_events() function uses a @_memo() cache. The conftest.py
autouse fixture `clear_calendar_cache` clears this cache before each test
to ensure monkeypatched httpx.get is actually called.
"""

import datetime as dt
import json
from types import SimpleNamespace

import httpx
from dateutil import tz

from app import calendar_service as cal

UTC = tz.UTC


def _dummy_json_response(items):
    """
    Return a fake httpx.Response-like object with .text containing
    the JSON-encoded `items`. We only need .text and raise_for_status().
    """
    return SimpleNamespace(
        text=json.dumps(items),
        status_code=200,
        raise_for_status=lambda: None,
    )


def test_fetch_events_returns_expected_list(monkeypatch):
    """
    When httpx.get returns a JSON array of events, _fetch_events() should:
      - skip entries whose 'details' start with
        "The President has no public events scheduled"
      - parse 'date', 'time', 'details' → dtstart_utc, summary
      - include 'location' exactly as given
      - attach a UTC-aware datetime to 'dtstart_utc'
    """
    # Build two sample items: one placeholder (should be skipped), one real
    now_local = dt.datetime(2025, 5, 30, 14, 5, tzinfo=tz.gettz("America/New_York"))
    now_str = now_local.strftime("%Y-%m-%d")
    # Placeholder item – should be ignored
    placeholder = {
        "date": now_str,
        "time": "00:00:00",
        "details": "The President has no public events scheduled",
        "location": "",
    }
    # Real item – should be parsed
    real_time = "12:34:00"
    real_details = "Test Event"
    real_location = "Somewhere, USA"
    real_item = {
        "date": now_str,
        "time": real_time,
        "details": real_details,
        "location": real_location,
    }

    # Monkeypatch httpx.get to return our two-element array
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: _dummy_json_response([placeholder, real_item]),
    )

    # Call _fetch_events() and inspect result
    events = cal._fetch_events()
    # We expect exactly one event (the real one)
    assert isinstance(events, list)
    assert len(events) == 1

    evt = events[0]
    # Summary should match "Test Event"
    assert evt["summary"] == real_details
    # Location should match exactly
    assert evt["location"] == real_location

    # dtstart_utc should be a datetime in UTC, equal to local→UTC conversion
    local_dt = dt.datetime.fromisoformat(f"{now_str}T{real_time}").replace(
        tzinfo=tz.gettz("America/New_York")
    )
    expected_utc = local_dt.astimezone(UTC).replace(microsecond=0)
    assert isinstance(evt["dtstart_utc"], dt.datetime)
    assert evt["dtstart_utc"].tzinfo is UTC
    assert evt["dtstart_utc"].replace(microsecond=0) == expected_utc


def test_current_event_prefers_nonempty_location(monkeypatch):
    """
    current_event() should choose the first (chronologically) past
    event that has a non-empty 'location'. If none have location, it
    should fall back to the time-closest event even if location is empty.
    """
    now = dt.datetime(2025, 5, 31, 12, 0, tzinfo=UTC)

    # Create three events:
    #  - past1: 2h ago, no location
    #  - past2: 3h ago, with location
    #  - future1: 1h ahead, with location
    past1 = now - dt.timedelta(hours=2)
    past2 = now - dt.timedelta(hours=3)
    future1 = now + dt.timedelta(hours=1)

    # Monkeypatch _fetch_events() to return these three dicts
    monkeypatch.setattr(
        cal,
        "_fetch_events",
        lambda: [
            {"dtstart_utc": past1, "summary": "NoLocPast", "location": ""},
            {"dtstart_utc": past2, "summary": "LocPast", "location": "Place A"},
            {"dtstart_utc": future1, "summary": "LocFuture", "location": "Place B"},
        ],
    )

    # Since LocPast (3h ago) has a non-empty location, and is within
    # past_hours=36, current_event() should pick that (even though it's
    # older than the no-location one).
    chosen = cal.current_event(now=now, past_hours=36, future_hours=24)
    assert chosen["summary"] == "LocPast"
    assert chosen["location"] == "Place A"

    # If we remove location from both past events, it should pick the most recent past
    monkeypatch.setattr(
        cal,
        "_fetch_events",
        lambda: [
            {"dtstart_utc": past1, "summary": "NoLocPast1", "location": ""},
            {"dtstart_utc": past2, "summary": "NoLocPast2", "location": ""},
            {"dtstart_utc": future1, "summary": "LocFuture", "location": "Place B"},
        ],
    )
    chosen2 = cal.current_event(now=now, past_hours=36, future_hours=24)
    # Between past1 (2h ago) and past2 (3h ago), past1 is closer in time → pick that
    assert chosen2["summary"] == "NoLocPast1"

    # If no past events at all, but a future one with location exists, pick that
    monkeypatch.setattr(
        cal,
        "_fetch_events",
        lambda: [
            {"dtstart_utc": future1, "summary": "LocFutureOnly", "location": "Place B"}
        ],
    )
    chosen3 = cal.current_event(now=now, past_hours=36, future_hours=24)
    assert chosen3["summary"] == "LocFutureOnly"
    assert chosen3["location"] == "Place B"

    # If no events in range, return None
    monkeypatch.setattr(cal, "_fetch_events", lambda: [])
    chosen4 = cal.current_event(now=now, past_hours=36, future_hours=24)
    assert chosen4 is None


def test_current_event_prefers_implicit_location_summary(monkeypatch):
    """
    current_event() should treat 'In-Town Pool Call Time' as having an
    implicit location, even when the location field is empty.
    """
    now = dt.datetime(2025, 12, 5, 14, 30, tzinfo=UTC)  # 9:30 AM ET

    # Scenario: "In-Town Pool Call Time" 30 min ago (no location),
    # "The Ellipse" 15h ago (has location)
    pool_call = now - dt.timedelta(minutes=30)
    ellipse = now - dt.timedelta(hours=15)

    monkeypatch.setattr(
        cal,
        "_fetch_events",
        lambda: [
            {
                "dtstart_utc": pool_call,
                "summary": "In-Town Pool Call Time",
                "location": "",  # No explicit location
            },
            {
                "dtstart_utc": ellipse,
                "summary": "Christmas Tree Lighting",
                "location": "The Ellipse",
            },
        ],
    )

    # Should pick "In-Town Pool Call Time" because it has implicit location
    chosen = cal.current_event(now=now, past_hours=36, future_hours=24)
    assert chosen["summary"] == "In-Town Pool Call Time"
    assert chosen["location"] == ""  # Still empty, but was preferred


# ── Tests for overnight base inference ───────────────────────────────────────

import pytest

NYC = tz.gettz("America/New_York")


class TestGetOvernightBase:
    """Tests for get_overnight_base() overnight inference logic."""

    def test_overnight_dc_pattern_returns_white_house(self, monkeypatch):
        """DC evening + DC morning at 11PM → returns White House."""
        # 11PM Eastern on a Thursday (4AM UTC next day)
        fake_now = dt.datetime(2025, 12, 5, 4, 0, tzinfo=UTC)

        # Mock events: Ellipse at 6PM (11PM UTC), Oval Office at 10AM (3PM UTC)
        events = [
            {
                "dtstart_utc": dt.datetime(2025, 12, 4, 23, 0, tzinfo=UTC),  # 6PM ET
                "summary": "Christmas Tree Lighting",
                "location": "The Ellipse",
            },
            {
                "dtstart_utc": dt.datetime(2025, 12, 5, 15, 0, tzinfo=UTC),  # 10AM ET
                "summary": "Intelligence Briefing",
                "location": "Oval Office",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is not None
        assert "White House" in result["name"]
        assert result["lat"] == pytest.approx(38.897676, abs=0.001)

    def test_overnight_florida_pattern_returns_mar_a_lago(self, monkeypatch):
        """Florida evening + Florida morning → returns Mar-a-Lago."""
        # 11PM Eastern on a Saturday (4AM UTC Sunday)
        fake_now = dt.datetime(2025, 11, 30, 4, 0, tzinfo=UTC)

        events = [
            {
                "dtstart_utc": dt.datetime(2025, 11, 30, 0, 0, tzinfo=UTC),  # 7PM ET Sat
                "summary": "Dinner",
                "location": "Mar-a-Lago",
            },
            {
                "dtstart_utc": dt.datetime(2025, 11, 30, 14, 0, tzinfo=UTC),  # 9AM ET Sun
                "summary": "Pool Call Time",
                "location": "Mar-a-Lago",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is not None
        assert "Mar-a-Lago" in result["name"]
        assert result["lat"] == pytest.approx(26.6758, abs=0.01)

    def test_overnight_bedminster_pattern_returns_bedminster(self, monkeypatch):
        """NJ evening + NJ morning → returns Bedminster (Summer White House)."""
        # 11PM Eastern on a summer Saturday (3AM UTC Sunday)
        fake_now = dt.datetime(2025, 7, 13, 3, 0, tzinfo=UTC)

        events = [
            {
                "dtstart_utc": dt.datetime(2025, 7, 13, 0, 0, tzinfo=UTC),  # 8PM ET Sat
                "summary": "Dinner",
                "location": "Trump National Golf Club Bedminster",
            },
            {
                "dtstart_utc": dt.datetime(2025, 7, 13, 13, 0, tzinfo=UTC),  # 9AM ET Sun
                "summary": "Golf",
                "location": "Trump National Golf Club Bedminster",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is not None
        assert "Bedminster" in result["name"]
        assert result["lat"] == pytest.approx(40.6456, abs=0.01)

    def test_overnight_travel_pattern_returns_none(self, monkeypatch):
        """DC evening + Florida morning → returns None (travel detected)."""
        # 11PM Eastern Friday (4AM UTC Saturday)
        fake_now = dt.datetime(2025, 11, 29, 4, 0, tzinfo=UTC)

        events = [
            {
                "dtstart_utc": dt.datetime(2025, 11, 28, 22, 0, tzinfo=UTC),  # 5PM ET
                "summary": "Departs",
                "location": "South Lawn",  # DC area
            },
            {
                "dtstart_utc": dt.datetime(2025, 11, 29, 14, 0, tzinfo=UTC),  # 9AM ET
                "summary": "Arrives",
                "location": "Mar-a-Lago",  # Florida
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is None  # Travel detected, different regions

    def test_daytime_returns_none(self, monkeypatch):
        """During daytime hours (2PM ET) → returns None."""
        # 2PM Eastern (7PM UTC)
        fake_now = dt.datetime(2025, 12, 5, 19, 0, tzinfo=UTC)

        events = [
            {
                "dtstart_utc": dt.datetime(2025, 12, 4, 23, 0, tzinfo=UTC),
                "summary": "Event",
                "location": "The White House",
            },
            {
                "dtstart_utc": dt.datetime(2025, 12, 5, 15, 0, tzinfo=UTC),
                "summary": "Event",
                "location": "Oval Office",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is None  # Daytime, not overnight hours

    def test_no_evening_event_returns_none(self, monkeypatch):
        """No evening event found → returns None."""
        # 3AM Eastern (8AM UTC) - overnight but no evening event
        fake_now = dt.datetime(2025, 12, 5, 8, 0, tzinfo=UTC)

        events = [
            {
                "dtstart_utc": dt.datetime(2025, 12, 5, 15, 0, tzinfo=UTC),  # 10AM ET
                "summary": "Morning briefing",
                "location": "Oval Office",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is None  # No evening event to match

    def test_no_morning_event_returns_none(self, monkeypatch):
        """No morning event found → returns None."""
        # 11PM Eastern (4AM UTC)
        fake_now = dt.datetime(2025, 12, 5, 4, 0, tzinfo=UTC)

        events = [
            {
                "dtstart_utc": dt.datetime(2025, 12, 4, 23, 0, tzinfo=UTC),  # 6PM ET
                "summary": "Evening event",
                "location": "The White House",
            },
            # No morning event
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_overnight_base(now=fake_now)

        assert result is None  # No morning event to match


# ── Tests for get_context_events() ───────────────────────────────────────────


class TestGetContextEvents:
    """Tests for get_context_events() schedule context extraction."""

    def test_returns_coords_and_timestamps_from_aliased_events(self, monkeypatch):
        """Should return lat, lon, dt dicts for events resolved via aliases."""
        target_dt = dt.datetime(2025, 12, 9, 20, 0, tzinfo=UTC)
        target_event = {
            "dtstart_utc": target_dt,
            "summary": "VP Christmas Reception",
            "location": "Some Unknown Location",
        }

        # Context events with aliased locations (White House is in aliases)
        events = [
            target_event,
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=2),
                "summary": "Earlier Event",
                "location": "The White House",
            },
            {
                "dtstart_utc": target_dt + dt.timedelta(hours=3),
                "summary": "Later Event",
                "location": "Oval Office",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_context_events(target_event, min_context=2)

        assert len(result) == 2
        # Each result should have lat, lon, dt
        for ctx in result:
            assert "lat" in ctx
            assert "lon" in ctx
            assert "dt" in ctx
            assert isinstance(ctx["dt"], dt.datetime)
        # Coords should be White House area
        assert result[0]["lat"] == pytest.approx(38.897676, abs=0.01)

    def test_sorts_by_temporal_distance(self, monkeypatch):
        """Should prefer events closest in time to target."""
        target_dt = dt.datetime(2025, 12, 9, 12, 0, tzinfo=UTC)
        target_event = {
            "dtstart_utc": target_dt,
            "summary": "Target Event",
            "location": "Unknown",
        }

        events = [
            target_event,
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=10),
                "summary": "Far Past",
                "location": "The White House",
            },
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=1),
                "summary": "Near Past",
                "location": "Oval Office",
            },
            {
                "dtstart_utc": target_dt + dt.timedelta(hours=5),
                "summary": "Far Future",
                "location": "Cabinet Room",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_context_events(target_event, min_context=2)

        # Should pick "Near Past" (1h) and "Far Future" (5h), not "Far Past" (10h)
        assert len(result) == 2
        timestamps = [ctx["dt"] for ctx in result]
        # Near Past should be first (closest)
        assert timestamps[0] == target_dt - dt.timedelta(hours=1)

    def test_expands_until_min_context_found(self, monkeypatch):
        """Should skip unresolved events and keep searching until min_context met."""
        target_dt = dt.datetime(2025, 12, 9, 12, 0, tzinfo=UTC)
        target_event = {
            "dtstart_utc": target_dt,
            "summary": "Target Event",
            "location": "Unknown Location X",
        }

        # First two nearby events have unknown locations, third is far but aliased
        events = [
            target_event,
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=1),
                "summary": "Near Unknown 1",
                "location": "Some Random Place",  # Not in aliases
            },
            {
                "dtstart_utc": target_dt + dt.timedelta(hours=2),
                "summary": "Near Unknown 2",
                "location": "Another Random Place",  # Not in aliases
            },
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=24),
                "summary": "Far But Aliased 1",
                "location": "The White House",
            },
            {
                "dtstart_utc": target_dt + dt.timedelta(hours=30),
                "summary": "Far But Aliased 2",
                "location": "Mar-a-Lago",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_context_events(target_event, min_context=2)

        # Should skip the unknown ones and find White House + Mar-a-Lago
        assert len(result) == 2
        # White House is closer (24h vs 30h)
        assert result[0]["lat"] == pytest.approx(38.897676, abs=0.01)  # White House
        assert result[1]["lat"] == pytest.approx(26.6758, abs=0.01)  # Mar-a-Lago

    def test_skips_target_event(self, monkeypatch):
        """Should not include the target event itself in context."""
        target_dt = dt.datetime(2025, 12, 9, 12, 0, tzinfo=UTC)
        # Target event has an aliased location - should still be skipped
        target_event = {
            "dtstart_utc": target_dt,
            "summary": "Target",
            "location": "The White House",
        }

        events = [
            target_event,
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=1),
                "summary": "Other",
                "location": "Oval Office",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_context_events(target_event, min_context=2)

        # Should only find 1, not 2 (target excluded even though it has alias)
        assert len(result) == 1

    def test_returns_empty_when_no_aliased_events(self, monkeypatch):
        """Should return empty list if no events can be resolved via aliases."""
        target_dt = dt.datetime(2025, 12, 9, 12, 0, tzinfo=UTC)
        target_event = {
            "dtstart_utc": target_dt,
            "summary": "Target",
            "location": "Unknown",
        }

        events = [
            target_event,
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=1),
                "summary": "Unknown 1",
                "location": "Random Place A",
            },
            {
                "dtstart_utc": target_dt + dt.timedelta(hours=1),
                "summary": "Unknown 2",
                "location": "Random Place B",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        result = cal.get_context_events(target_event, min_context=2)

        assert len(result) == 0

    def test_respects_min_context_parameter(self, monkeypatch):
        """Should stop searching once min_context is reached."""
        target_dt = dt.datetime(2025, 12, 9, 12, 0, tzinfo=UTC)
        target_event = {
            "dtstart_utc": target_dt,
            "summary": "Target",
            "location": "Unknown",
        }

        events = [
            target_event,
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=1),
                "summary": "E1",
                "location": "The White House",
            },
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=2),
                "summary": "E2",
                "location": "Oval Office",
            },
            {
                "dtstart_utc": target_dt - dt.timedelta(hours=3),
                "summary": "E3",
                "location": "Cabinet Room",
            },
        ]
        monkeypatch.setattr(cal, "_fetch_events", lambda: events)

        # Request only 2
        result = cal.get_context_events(target_event, min_context=2)
        assert len(result) == 2

        # Request 3
        result = cal.get_context_events(target_event, min_context=3)
        assert len(result) == 3
