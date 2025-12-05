"""Tests for geocoding features: skip list, smart geocode, and log service."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSkipLocations:
    """Tests for the skip list functionality."""

    def test_skip_list_contains_known_entries(self):
        """SKIP_LOCATIONS contains the expected non-geocodable terms."""
        from app.location_service import SKIP_LOCATIONS

        assert "stakeout location" in SKIP_LOCATIONS
        assert "the sticks - the white house" in SKIP_LOCATIONS

    def test_should_skip_geocode_returns_true_for_skip_list(self):
        """_should_skip_geocode returns True for entries in skip list."""
        from app.location_service import _should_skip_geocode

        assert _should_skip_geocode("Stakeout Location") is True
        assert _should_skip_geocode("THE STICKS - THE WHITE HOUSE") is True

    def test_should_skip_geocode_returns_false_for_normal_locations(self):
        """_should_skip_geocode returns False for normal locations."""
        from app.location_service import _should_skip_geocode

        assert _should_skip_geocode("Mar-a-Lago") is False
        assert _should_skip_geocode("The White House") is False
        assert _should_skip_geocode("Miami, FL") is False


class TestGeocodeLogService:
    """Tests for geocode_log_service.py."""

    @pytest.fixture
    def temp_log_file(self, tmp_path: Path):
        """Fixture to use a temporary directory for geocode logs."""
        log_file = tmp_path / "geocode_log.json"
        with patch("app.geocode_log_service.FILE", log_file):
            yield log_file

    def test_add_geocode_entry_creates_file(self, temp_log_file: Path):
        """add_geocode_entry creates the log file if it doesn't exist."""
        from app.geocode_log_service import add_geocode_entry

        add_geocode_entry(
            query="Test Location",
            result_type="us",
            lat=40.7128,
            lon=-74.0060,
            country="United States",
            state="New York",
        )

        assert temp_log_file.exists()
        data = json.loads(temp_log_file.read_text())
        assert len(data["entries"]) == 1
        assert data["entries"][0]["query"] == "Test Location"
        assert data["entries"][0]["result_type"] == "us"

    def test_add_geocode_entry_appends_to_existing(self, temp_log_file: Path):
        """add_geocode_entry appends to existing entries."""
        from app.geocode_log_service import add_geocode_entry

        add_geocode_entry(query="Location 1", result_type="us")
        add_geocode_entry(query="Location 2", result_type="international")

        data = json.loads(temp_log_file.read_text())
        assert len(data["entries"]) == 2
        assert data["entries"][0]["query"] == "Location 1"
        assert data["entries"][1]["query"] == "Location 2"

    def test_get_geocode_entries_returns_newest_first(self, temp_log_file: Path):
        """get_geocode_entries returns entries in reverse chronological order."""
        from app.geocode_log_service import (
            add_geocode_entry,
            get_geocode_entries,
        )

        add_geocode_entry(query="First", result_type="us")
        add_geocode_entry(query="Second", result_type="us")
        add_geocode_entry(query="Third", result_type="us")

        entries = get_geocode_entries()
        assert len(entries) == 3
        assert entries[0]["query"] == "Third"  # Newest first
        assert entries[2]["query"] == "First"  # Oldest last

    def test_get_geocode_entries_respects_limit(self, temp_log_file: Path):
        """get_geocode_entries respects the limit parameter."""
        from app.geocode_log_service import (
            add_geocode_entry,
            get_geocode_entries,
        )

        for i in range(10):
            add_geocode_entry(query=f"Location {i}", result_type="us")

        entries = get_geocode_entries(limit=3)
        assert len(entries) == 3

    def test_get_geocode_entries_filters_by_result_type(self, temp_log_file: Path):
        """get_geocode_entries filters by result_type."""
        from app.geocode_log_service import (
            add_geocode_entry,
            get_geocode_entries,
        )

        add_geocode_entry(query="US Location", result_type="us")
        add_geocode_entry(query="Failed", result_type="no_result")
        add_geocode_entry(query="International", result_type="international")

        us_only = get_geocode_entries(result_type="us")
        assert len(us_only) == 1
        assert us_only[0]["query"] == "US Location"

        failures = get_geocode_entries(result_type="no_result")
        assert len(failures) == 1
        assert failures[0]["query"] == "Failed"

    def test_get_geocode_stats_returns_counts_by_type(self, temp_log_file: Path):
        """get_geocode_stats returns correct counts by result type."""
        from app.geocode_log_service import (
            add_geocode_entry,
            get_geocode_stats,
        )

        add_geocode_entry(query="US 1", result_type="us")
        add_geocode_entry(query="US 2", result_type="us")
        add_geocode_entry(query="Intl", result_type="international")
        add_geocode_entry(query="Fail", result_type="no_result")

        stats = get_geocode_stats()
        assert stats["count"] == 4
        assert stats["by_type"]["us"] == 2
        assert stats["by_type"]["international"] == 1
        assert stats["by_type"]["no_result"] == 1

    def test_add_geocode_entry_stores_importance(self, temp_log_file: Path):
        """add_geocode_entry stores the importance score when provided."""
        from app.geocode_log_service import add_geocode_entry

        add_geocode_entry(
            query="Mar-a-Lago",
            result_type="us",
            lat=26.6776,
            lon=-80.0369,
            importance=0.468,
        )

        data = json.loads(temp_log_file.read_text())
        assert data["entries"][0]["importance"] == 0.468

    def test_add_geocode_entry_omits_importance_when_none(self, temp_log_file: Path):
        """add_geocode_entry omits importance field when not provided."""
        from app.geocode_log_service import add_geocode_entry

        add_geocode_entry(query="Test", result_type="us")

        data = json.loads(temp_log_file.read_text())
        assert "importance" not in data["entries"][0]


class TestSmartGeocode:
    """Tests for _smart_geocode function."""

    @pytest.fixture
    def mock_geocode(self):
        """Mock the rate-limited geocoder."""
        with patch("app.location_service._geocode_raw") as mock:
            yield mock

    @pytest.fixture
    def mock_geocode_log(self):
        """Mock the geocode log service."""
        with patch("app.location_service.add_geocode_entry"):
            yield

    @pytest.fixture
    def mock_discord(self):
        """Mock the Discord event service (failures)."""
        with patch("app.location_service.emit_geocode_failure"):
            yield

    @pytest.fixture
    def mock_low_importance(self):
        """Mock the low importance alert."""
        with patch("app.location_service.emit_low_importance_geocode") as mock:
            yield mock

    def test_smart_geocode_skips_skip_list(
        self, mock_geocode, mock_geocode_log, mock_discord
    ):
        """_smart_geocode returns None for skip list entries."""
        from app.location_service import _smart_geocode

        result = _smart_geocode("Stakeout Location")

        assert result is None
        mock_geocode.assert_not_called()

    def test_smart_geocode_tries_us_first(
        self, mock_geocode, mock_geocode_log, mock_discord
    ):
        """_smart_geocode tries US-restricted search first."""
        from app.location_service import _smart_geocode

        # Mock successful US geocode
        mock_result = MagicMock()
        mock_result.latitude = 40.7128
        mock_result.longitude = -74.0060
        mock_result.address = "New York, NY, USA"
        mock_result.raw = {"address": {"state": "New York", "country": "United States"}}
        mock_geocode.return_value = mock_result

        result = _smart_geocode("New York City")

        assert result is not None
        assert result.latitude == 40.7128
        # First call should have country_codes='us'
        call_kwargs = mock_geocode.call_args[1]
        assert call_kwargs.get("country_codes") == "us"

    def test_smart_geocode_falls_back_to_international(
        self, mock_geocode, mock_geocode_log, mock_discord
    ):
        """_smart_geocode falls back to international when US returns nothing."""
        from app.location_service import _smart_geocode

        # First call (US) returns None, second call (international) succeeds
        mock_result = MagicMock()
        mock_result.latitude = 51.5074
        mock_result.longitude = -0.1278
        mock_result.address = "London, UK"
        mock_result.raw = {"address": {"country": "United Kingdom"}}
        mock_geocode.side_effect = [None, mock_result]

        result = _smart_geocode("London")

        assert result is not None
        assert result.latitude == 51.5074
        assert mock_geocode.call_count == 2

    def test_smart_geocode_returns_none_when_both_fail(
        self, mock_geocode, mock_geocode_log, mock_discord
    ):
        """_smart_geocode returns None when both US and international fail."""
        from app.location_service import _smart_geocode

        mock_geocode.return_value = None

        result = _smart_geocode("Nonexistent Place XYZ123")

        assert result is None
        assert mock_geocode.call_count == 2  # Tried both US and international

    def test_smart_geocode_alerts_on_low_importance(
        self, mock_geocode, mock_geocode_log, mock_discord, mock_low_importance
    ):
        """_smart_geocode emits low importance alert when score < threshold."""
        from app.location_service import _smart_geocode

        # Mock result with low importance (below 0.35 threshold)
        mock_result = MagicMock()
        mock_result.latitude = 33.5684
        mock_result.longitude = -86.0508
        mock_result.address = "Oval Office, Anniston, Alabama"
        mock_result.raw = {
            "address": {"state": "Alabama", "country": "United States"},
            "importance": 0.04,  # Very low - likely wrong result
        }
        mock_geocode.return_value = mock_result

        result = _smart_geocode("Oval Office")

        assert result is not None
        # Should have called low importance alert
        mock_low_importance.assert_called_once()
        call_kwargs = mock_low_importance.call_args[1]
        assert call_kwargs["query"] == "Oval Office"
        assert call_kwargs["importance"] == 0.04

    def test_smart_geocode_skips_alert_on_high_importance(
        self, mock_geocode, mock_geocode_log, mock_discord, mock_low_importance
    ):
        """_smart_geocode does not alert when importance >= threshold."""
        from app.location_service import _smart_geocode

        # Mock result with good importance (above 0.35 threshold)
        mock_result = MagicMock()
        mock_result.latitude = 38.8977
        mock_result.longitude = -77.0365
        mock_result.address = "White House, Washington DC"
        mock_result.raw = {
            "address": {"state": "District of Columbia", "country": "United States"},
            "importance": 0.686,  # Good importance
        }
        mock_geocode.return_value = mock_result

        result = _smart_geocode("White House")

        assert result is not None
        # Should NOT have called low importance alert
        mock_low_importance.assert_not_called()


class TestEmitGeocodeFailure:
    """Tests for emit_geocode_failure event."""

    @pytest.fixture(autouse=True)
    def reset_cooldown(self):
        """Reset the cooldown tracker between tests."""
        from app import event_service

        event_service._last_geocode_error.clear()
        yield

    def test_emit_geocode_failure_respects_cooldown(self):
        """emit_geocode_failure deduplicates by query within cooldown period."""
        from app.event_service import (
            _last_geocode_error,
            emit_geocode_failure,
        )

        with patch("app.event_service._fire_and_forget") as mock_fire:
            # First call should emit
            emit_geocode_failure(query="Test Query", result_type="no_result")
            assert mock_fire.call_count == 1

            # Second call within cooldown should be suppressed
            emit_geocode_failure(query="Test Query", result_type="no_result")
            assert mock_fire.call_count == 1  # Still 1

            # Different query should emit
            emit_geocode_failure(query="Different Query", result_type="no_result")
            assert mock_fire.call_count == 2

    def test_emit_geocode_failure_includes_error_field(self):
        """emit_geocode_failure includes error message when provided."""
        with patch("app.event_service._fire_and_forget") as mock_fire:
            from app.event_service import emit_geocode_failure

            emit_geocode_failure(
                query="Bad Query",
                result_type="error",
                error="Connection timeout",
            )

            embed = mock_fire.call_args[0][0]
            field_values = [f["value"] for f in embed["fields"]]
            assert "Connection timeout" in field_values


class TestEmitLowImportanceGeocode:
    """Tests for emit_low_importance_geocode event."""

    @pytest.fixture(autouse=True)
    def reset_cooldown(self):
        """Reset the cooldown tracker between tests."""
        from app import event_service

        event_service._last_geocode_error.clear()
        yield

    def test_emit_low_importance_geocode_fires_below_threshold(self):
        """emit_low_importance_geocode fires for low importance scores."""
        with patch("app.event_service._fire_and_forget") as mock_fire:
            from app.event_service import emit_low_importance_geocode

            emit_low_importance_geocode(
                query="Oval Office",
                importance=0.04,
                lat=33.5684,
                lon=-86.0508,
                display_name="Oval Office, Anniston, Alabama",
            )

            assert mock_fire.call_count == 1
            embed = mock_fire.call_args[0][0]
            assert embed["title"] == "Low Confidence Geocode"
            assert embed["color"] == 0xF39C12  # Yellow

    def test_emit_low_importance_geocode_respects_cooldown(self):
        """emit_low_importance_geocode deduplicates by query within cooldown period."""
        with patch("app.event_service._fire_and_forget") as mock_fire:
            from app.event_service import emit_low_importance_geocode

            # First call should emit
            emit_low_importance_geocode(
                query="Test Query", importance=0.1, lat=0, lon=0
            )
            assert mock_fire.call_count == 1

            # Second call within cooldown should be suppressed
            emit_low_importance_geocode(
                query="Test Query", importance=0.1, lat=0, lon=0
            )
            assert mock_fire.call_count == 1  # Still 1

            # Different query should emit
            emit_low_importance_geocode(
                query="Different Query", importance=0.2, lat=0, lon=0
            )
            assert mock_fire.call_count == 2

    def test_emit_low_importance_geocode_includes_all_fields(self):
        """emit_low_importance_geocode includes all expected fields."""
        with patch("app.event_service._fire_and_forget") as mock_fire:
            from app.event_service import emit_low_importance_geocode

            emit_low_importance_geocode(
                query="Test Location",
                importance=0.15,
                lat=40.7128,
                lon=-74.0060,
                display_name="Test Location, New York",
            )

            embed = mock_fire.call_args[0][0]
            field_names = [f["name"] for f in embed["fields"]]

            assert "Query" in field_names
            assert "Importance" in field_names
            assert "Threshold" in field_names
            assert "Coordinates" in field_names
            assert "Resolved To" in field_names
            assert "Action" in field_names
