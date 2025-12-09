"""tests/test_location_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Regression tests for the **alias-shortcut** branch inside
app.location_service.current_coords().
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import calendar_service as cal
from app import location_service as loc


@pytest.mark.asyncio
async def test_alias_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Calendar event ‘The White House’ → alias coordinates are returned
    (no geocoding, no plane lookup).
    """
    # 1️⃣ Plane: force *no data* so pipeline proceeds to calendar branch
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)

    # 2️⃣ Calendar: fake a pool-call event at the White House.
    now_utc = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        cal,
        "current_event",
        lambda *_: {
            "location": "The White House",
            "summary": "Pool call",
            "dtstart_utc": now_utc,
        },
        raising=True,
    )

    # 3️⃣ Flush memo-cache between tests
    loc._cached.clear()  # type: ignore[attr-defined]

    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # when trace is returned
        coords = coords[0]

    assert "white house" in coords["name"].lower()
    assert coords["lat"] == pytest.approx(38.897676)
    assert coords["lon"] == pytest.approx(-77.036529)


@pytest.mark.asyncio
async def test_in_town_pool_call_time_resolves_to_white_house(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    'In-Town Pool Call Time' in summary (without location) → White House.

    This tests strategy 2b (alias on summary) when the location field is empty.
    'In-Town Pool Call Time' means the press pool must report to the White House.
    """
    # 1️⃣ Plane: force *no data* so pipeline proceeds to calendar branch
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)

    # 2️⃣ Calendar: event with empty location but implicit WH indicator in summary
    now_utc = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        cal,
        "current_event",
        lambda *_: {
            "location": "",  # Empty location field
            "summary": "In-Town Pool Call Time",
            "dtstart_utc": now_utc,
        },
        raising=True,
    )

    # 3️⃣ Flush memo-cache between tests
    loc._cached.clear()  # type: ignore[attr-defined]

    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # when trace is returned
        coords = coords[0]

    assert "white house" in coords["name"].lower()
    assert coords["lat"] == pytest.approx(38.897676)
    assert coords["lon"] == pytest.approx(-77.036529)
    assert coords["reason"] == "calendar_summary"  # via summary, not location


@pytest.mark.asyncio
async def test_overnight_inference_returns_white_house(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    At 11PM after DC event, with DC morning event → returns WH via overnight inference.

    Tests that overnight base inference takes precedence over stale calendar events.
    """
    # 1️⃣ Plane: force *no data* so pipeline proceeds to overnight/calendar branch
    monkeypatch.setattr(loc, "get_plane_state", lambda: None, raising=True)

    # 2️⃣ Mock overnight base to return WH (simulating DC evening + DC morning pattern)
    monkeypatch.setattr(
        cal,
        "get_overnight_base",
        lambda now=None: {"lat": 38.897676, "lon": -77.036529, "name": "The White House"},
    )

    # 3️⃣ Calendar returns old Ellipse event (would normally win without overnight inference)
    now_utc = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        cal,
        "current_event",
        lambda *_: {
            "location": "The Ellipse",
            "summary": "Christmas Tree Lighting",
            "dtstart_utc": now_utc - dt.timedelta(hours=5),
        },
        raising=True,
    )

    # 4️⃣ Flush memo-cache between tests
    loc._cached.clear()  # type: ignore[attr-defined]

    coords = await loc.current_coords()
    if isinstance(coords, tuple):  # when trace is returned
        coords = coords[0]

    # Should return White House from overnight inference, not Ellipse from calendar
    assert "white house" in coords["name"].lower()
    assert coords["lat"] == pytest.approx(38.897676)
    assert coords["lon"] == pytest.approx(-77.036529)
    assert coords["reason"] == "overnight_dc"
    assert coords["confidence"] == 58


# ── Physical Feasibility Tests ─────────────────────────────────────────────────


class TestIsPhysicallyFeasible:
    """
    Tests for _is_physically_feasible() function.

    This function checks if a location result is physically reachable from
    any context event given the time gap and max travel speed (800 km/h).
    """

    # Reference coordinates
    DC_LAT, DC_LON = 38.8977, -77.0365  # White House
    ARIZONA_LAT, ARIZONA_LON = 32.2319, -110.9501  # Tucson AZ (Naval Obs there)
    NY_LAT, NY_LON = 40.7128, -74.0060  # NYC

    def test_dc_to_arizona_in_2h_is_infeasible(self) -> None:
        """
        DC→Arizona in 2h is IMPOSSIBLE.

        Distance: ~3,100 km
        Max feasible at 800 km/h for 2h: 1,600 km
        Result: Should return False
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt - dt.timedelta(hours=2),  # 2h before
            }
        ]

        result = loc._is_physically_feasible(
            result_lat=self.ARIZONA_LAT,
            result_lon=self.ARIZONA_LON,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is False

    def test_dc_to_dc_in_2h_is_feasible(self) -> None:
        """
        DC→DC in 2h is FEASIBLE.

        Distance: ~5 km (within DC)
        Max feasible at 800 km/h for 2h: 1,600 km
        Result: Should return True
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt - dt.timedelta(hours=2),
            }
        ]

        # Naval Observatory DC is ~3km from White House
        naval_obs_dc_lat, naval_obs_dc_lon = 38.9217, -77.0669

        result = loc._is_physically_feasible(
            result_lat=naval_obs_dc_lat,
            result_lon=naval_obs_dc_lon,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is True

    def test_dc_to_ny_in_4h_is_feasible(self) -> None:
        """
        DC→NY in 4h is FEASIBLE.

        Distance: ~350 km
        Max feasible at 800 km/h for 4h: 3,200 km
        Result: Should return True
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt - dt.timedelta(hours=4),
            }
        ]

        result = loc._is_physically_feasible(
            result_lat=self.NY_LAT,
            result_lon=self.NY_LON,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is True

    def test_zero_time_gap_uses_minimum_threshold(self) -> None:
        """
        When time gap is 0, use minimum threshold (0.1h = 6 min).

        This allows ~80km of travel to avoid false negatives for
        near-simultaneous events at slightly different venues.
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt,  # Same time (0 gap)
            }
        ]

        # Capitol Building is ~3km from White House - should be feasible
        capitol_lat, capitol_lon = 38.8899, -77.0091

        result = loc._is_physically_feasible(
            result_lat=capitol_lat,
            result_lon=capitol_lon,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is True

    def test_zero_time_gap_rejects_far_locations(self) -> None:
        """
        Zero time gap should still reject locations far away.

        Min threshold 0.1h at 800 km/h = 80km max.
        NYC is ~350km from DC, so should be rejected.
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt,  # Same time (0 gap)
            }
        ]

        result = loc._is_physically_feasible(
            result_lat=self.NY_LAT,
            result_lon=self.NY_LON,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is False

    def test_feasible_from_any_context_event(self) -> None:
        """
        Result is feasible if reachable from ANY context event.

        Even if one context event is too close in time, if another
        has sufficient time gap, the result should be feasible.
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                # Too close: 0.5h = 400km max, NY is 350km - barely feasible
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt - dt.timedelta(minutes=30),
            },
            {
                # Plenty of time: 4h = 3200km max
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt - dt.timedelta(hours=4),
            },
        ]

        result = loc._is_physically_feasible(
            result_lat=self.NY_LAT,
            result_lon=self.NY_LON,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is True

    def test_empty_context_returns_true(self) -> None:
        """
        If no context events, assume feasible (no data to contradict).

        This is a graceful fallback - can't determine infeasibility
        without reference points.
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events: list[dict] = []

        result = loc._is_physically_feasible(
            result_lat=self.ARIZONA_LAT,
            result_lon=self.ARIZONA_LON,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is True

    def test_future_context_event_also_works(self) -> None:
        """
        Context events can be in the future (upcoming schedule).

        If there's a confirmed event 2h AFTER target, and the result
        is 3000km away from that future event, it's infeasible.
        """
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context_events = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt + dt.timedelta(hours=2),  # 2h AFTER
            }
        ]

        # Arizona is ~3100km from DC, can't get there and back in 2h
        result = loc._is_physically_feasible(
            result_lat=self.ARIZONA_LAT,
            result_lon=self.ARIZONA_LON,
            context_events=context_events,
            target_dt=target_dt,
        )

        assert result is False


# ── Centroid Computation Tests ─────────────────────────────────────────────────


class TestComputeCentroid:
    """Tests for _compute_centroid() helper function."""

    def test_single_point_returns_itself(self) -> None:
        """Centroid of one point is that point."""
        coords = [(38.8977, -77.0365)]
        result = loc._compute_centroid(coords)
        assert result == (38.8977, -77.0365)

    def test_two_points_returns_midpoint(self) -> None:
        """Centroid of two points is their midpoint."""
        coords = [(0.0, 0.0), (10.0, 10.0)]
        result = loc._compute_centroid(coords)
        assert result == pytest.approx((5.0, 5.0))

    def test_empty_list_returns_zero_zero(self) -> None:
        """Empty list returns (0, 0) as fallback."""
        result = loc._compute_centroid([])
        assert result == (0.0, 0.0)

    def test_dc_area_points(self) -> None:
        """Centroid of DC area points is in DC area."""
        coords = [
            (38.8977, -77.0365),  # White House
            (38.9217, -77.0669),  # Naval Observatory
            (38.8899, -77.0091),  # Capitol
        ]
        result = loc._compute_centroid(coords)
        # Should be roughly in the middle of DC
        assert 38.88 < result[0] < 38.93
        assert -77.07 < result[1] < -77.00


# ── Hybrid Disambiguation Tests ────────────────────────────────────────────────


class TestHybridDisambiguation:
    """
    Tests for the hybrid 3-layer geocoding disambiguation.

    These tests verify the _disambiguate_results() helper function that:
    1. Filters physically infeasible results (Layer 1)
    2. Ranks by proximity to context centroid (Layer 2)
    3. Flags suspicious distances (Layer 3)
    """

    # Reference coordinates
    DC_LAT, DC_LON = 38.8977, -77.0365  # White House
    ARIZONA_LAT, ARIZONA_LON = 32.2319, -110.9501  # Tucson AZ
    VIRGINIA_LAT, VIRGINIA_LON = 38.7102, -77.0888  # Mount Vernon VA
    NY_LAT, NY_LON = 40.7128, -74.0060  # NYC

    def _make_mock_result(
        self, lat: float, lon: float, importance: float = 0.5, name: str = "Test"
    ):
        """Create a mock geopy Location-like object."""

        class MockLocation:
            def __init__(self, latitude, longitude, importance, address):
                self.latitude = latitude
                self.longitude = longitude
                self.address = address
                self.raw = {"importance": importance, "display_name": address}

        return MockLocation(lat, lon, importance, name)

    def test_single_result_returns_as_is(self) -> None:
        """Single result is returned without modification."""
        result = self._make_mock_result(self.DC_LAT, self.DC_LON)
        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        context = [{"lat": self.DC_LAT, "lon": self.DC_LON, "dt": target_dt}]

        best, alert = loc._disambiguate_results(
            results=[result],
            context_events=context,
            target_dt=target_dt,
        )

        assert best is result
        assert alert is None

    def test_no_context_returns_highest_importance(self) -> None:
        """Without context, return result with highest importance score."""
        low_imp = self._make_mock_result(self.ARIZONA_LAT, self.ARIZONA_LON, 0.3, "AZ")
        high_imp = self._make_mock_result(self.DC_LAT, self.DC_LON, 0.8, "DC")

        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)

        best, alert = loc._disambiguate_results(
            results=[low_imp, high_imp],
            context_events=[],
            target_dt=target_dt,
        )

        # Should pick DC due to higher importance
        assert best.latitude == self.DC_LAT

    def test_layer1_voids_infeasible_results(self) -> None:
        """Layer 1 filters out physically impossible results."""
        arizona = self._make_mock_result(
            self.ARIZONA_LAT, self.ARIZONA_LON, 0.9, "Arizona"
        )
        dc = self._make_mock_result(self.DC_LAT, self.DC_LON, 0.3, "DC")

        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        # Context: DC event 2h before target
        context = [
            {"lat": self.DC_LAT, "lon": self.DC_LON, "dt": target_dt - dt.timedelta(hours=2)}
        ]

        best, alert = loc._disambiguate_results(
            results=[arizona, dc],
            context_events=context,
            target_dt=target_dt,
        )

        # Arizona is 3100km from DC, can't reach in 2h at 800km/h
        # Should pick DC even though Arizona has higher importance
        assert best.latitude == self.DC_LAT
        assert alert is None

    def test_layer2_picks_closest_to_centroid(self) -> None:
        """Layer 2 picks feasible result closest to context centroid."""
        ny = self._make_mock_result(self.NY_LAT, self.NY_LON, 0.5, "NY")
        va = self._make_mock_result(self.VIRGINIA_LAT, self.VIRGINIA_LON, 0.5, "VA")

        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        # Context: DC events (VA is closer to DC than NY)
        context = [
            {"lat": self.DC_LAT, "lon": self.DC_LON, "dt": target_dt - dt.timedelta(hours=4)},
            {"lat": self.DC_LAT, "lon": self.DC_LON, "dt": target_dt + dt.timedelta(hours=4)},
        ]

        best, alert = loc._disambiguate_results(
            results=[ny, va],
            context_events=context,
            target_dt=target_dt,
        )

        # Both are feasible (4h = 3200km range), but VA is ~20km from DC vs NY ~350km
        assert best.latitude == self.VIRGINIA_LAT
        assert alert is None

    def test_layer3_flags_suspicious_distance(self) -> None:
        """Layer 3 flags results far from context (>500km)."""
        # Only result is in California - feasible but suspicious
        california = self._make_mock_result(34.0522, -118.2437, 0.8, "Los Angeles")

        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        # Context: DC events (California is ~4000km away)
        context = [
            {
                "lat": self.DC_LAT,
                "lon": self.DC_LON,
                "dt": target_dt - dt.timedelta(hours=10),  # Feasible (10h = 8000km range)
            }
        ]

        best, alert = loc._disambiguate_results(
            results=[california],
            context_events=context,
            target_dt=target_dt,
        )

        # Should return California but flag as suspicious
        assert best.latitude == pytest.approx(34.0522)
        assert alert is not None
        assert alert["type"] == "suspicious_distance"
        assert alert["distance_km"] > 500

    def test_all_results_infeasible_returns_highest_importance_with_alert(self) -> None:
        """When all results are infeasible, return highest importance with alert."""
        az1 = self._make_mock_result(self.ARIZONA_LAT, self.ARIZONA_LON, 0.9, "AZ High")
        az2 = self._make_mock_result(
            self.ARIZONA_LAT + 0.1, self.ARIZONA_LON, 0.3, "AZ Low"
        )

        target_dt = dt.datetime(2025, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
        # Context: DC event only 1h away - can't reach Arizona
        context = [
            {"lat": self.DC_LAT, "lon": self.DC_LON, "dt": target_dt - dt.timedelta(hours=1)}
        ]

        best, alert = loc._disambiguate_results(
            results=[az1, az2],
            context_events=context,
            target_dt=target_dt,
        )

        # All infeasible → fall back to highest importance
        assert best.latitude == self.ARIZONA_LAT
        assert alert is not None
        assert alert["type"] == "all_infeasible"
