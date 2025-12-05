"""
tests/test_confidence_priority.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test that location sources are prioritized by confidence score.
"""

from __future__ import annotations

import pytest


def test_select_highest_confidence_basic():
    """Should select the candidate with highest confidence."""
    from app.location_service import select_highest_confidence

    candidates = [
        {"name": "Calendar", "confidence": 35, "lat": 40.0, "lon": -74.0},
        {"name": "TFR", "confidence": 40, "lat": 39.0, "lon": -77.0},
    ]

    best = select_highest_confidence(candidates)

    assert best["name"] == "TFR"
    assert best["confidence"] == 40


def test_select_highest_confidence_single():
    """Single candidate should be returned."""
    from app.location_service import select_highest_confidence

    candidates = [
        {"name": "Calendar", "confidence": 35, "lat": 40.0, "lon": -74.0},
    ]

    best = select_highest_confidence(candidates)

    assert best["name"] == "Calendar"


def test_select_highest_confidence_empty():
    """Empty candidates should return None."""
    from app.location_service import select_highest_confidence

    best = select_highest_confidence([])

    assert best is None


def test_select_highest_confidence_tie():
    """On tie, first candidate wins (stable sort)."""
    from app.location_service import select_highest_confidence

    candidates = [
        {"name": "First", "confidence": 50, "lat": 40.0, "lon": -74.0},
        {"name": "Second", "confidence": 50, "lat": 39.0, "lon": -77.0},
    ]

    best = select_highest_confidence(candidates)

    # First one wins on tie (stable behavior)
    assert best["name"] == "First"


def test_select_highest_confidence_many():
    """Should work with many candidates."""
    from app.location_service import select_highest_confidence

    candidates = [
        {"name": "Low", "confidence": 10, "lat": 40.0, "lon": -74.0},
        {"name": "High", "confidence": 90, "lat": 39.0, "lon": -77.0},
        {"name": "Medium", "confidence": 50, "lat": 38.0, "lon": -76.0},
        {"name": "VeryLow", "confidence": 5, "lat": 37.0, "lon": -75.0},
    ]

    best = select_highest_confidence(candidates)

    assert best["name"] == "High"
    assert best["confidence"] == 90
