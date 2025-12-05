"""
tests/test_tfr_parsing.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Test TFR coordinate parsing with validation.
"""

from __future__ import annotations

import pytest
from app.location_service import parse_tfr_coordinates


def test_parse_valid_north_west():
    """Valid N/W coordinates should parse correctly."""
    description = "VIP SECURITY N40.7128, W74.0060"
    coords = parse_tfr_coordinates(description)

    assert coords is not None
    assert coords["lat"] == pytest.approx(40.7128)
    assert coords["lon"] == pytest.approx(-74.0060)


def test_parse_valid_south_east():
    """Valid S/E coordinates should parse correctly (Southern/Eastern hemispheres)."""
    description = "SECURITY TFR S33.8688, E151.2093"
    coords = parse_tfr_coordinates(description)

    assert coords is not None
    assert coords["lat"] == pytest.approx(-33.8688)
    assert coords["lon"] == pytest.approx(151.2093)


def test_parse_with_extra_text():
    """Coordinates embedded in longer description should parse."""
    description = "VIP TFR in effect N38.9072, W77.0369 until 2300Z"
    coords = parse_tfr_coordinates(description)

    assert coords is not None
    assert coords["lat"] == pytest.approx(38.9072)
    assert coords["lon"] == pytest.approx(-77.0369)


def test_parse_no_coordinates():
    """Description without coordinates should return None."""
    description = "VIP SECURITY TFR in effect"
    coords = parse_tfr_coordinates(description)

    assert coords is None


def test_parse_invalid_latitude_high():
    """Latitude > 90 should be rejected."""
    description = "N95.0, W74.0"  # Invalid: lat > 90
    coords = parse_tfr_coordinates(description)

    assert coords is None


def test_parse_invalid_latitude_low():
    """Latitude < -90 should be rejected."""
    description = "S95.0, E151.0"  # Invalid: lat < -90
    coords = parse_tfr_coordinates(description)

    assert coords is None


def test_parse_invalid_longitude_high():
    """Longitude > 180 should be rejected."""
    description = "N40.7, E185.0"  # Invalid: lon > 180
    coords = parse_tfr_coordinates(description)

    assert coords is None


def test_parse_invalid_longitude_low():
    """Longitude < -180 should be rejected."""
    description = "N40.7, W185.0"  # Invalid: lon < -180
    coords = parse_tfr_coordinates(description)

    assert coords is None


def test_parse_boundary_values():
    """Boundary values (±90, ±180) should be accepted."""
    # North pole
    assert parse_tfr_coordinates("N90.0, W0.0") == {"lat": 90.0, "lon": 0.0}

    # South pole
    assert parse_tfr_coordinates("S90.0, E0.0") == {"lat": -90.0, "lon": 0.0}

    # International date line (both sides)
    assert parse_tfr_coordinates("N0.0, E180.0") == {"lat": 0.0, "lon": 180.0}
    assert parse_tfr_coordinates("N0.0, W180.0") == {"lat": 0.0, "lon": -180.0}


def test_parse_malformed_format():
    """Malformed coordinate strings should return None."""
    # Missing direction letter
    assert parse_tfr_coordinates("40.7, 74.0") is None

    # Invalid direction letters
    assert parse_tfr_coordinates("X40.7, Y74.0") is None

    # Non-numeric values
    assert parse_tfr_coordinates("NABC, WDEF") is None
