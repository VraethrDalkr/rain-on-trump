"""
tests/test_flight_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test flight service confidence decay logic.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app import flight_service as fs


def test_calculate_confidence_fresh_airborne():
    """Fresh airborne position (2 min old) should have full confidence (95)."""
    age_seconds = 120  # 2 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=True)

    assert confidence == 95


def test_calculate_confidence_stale_airborne():
    """Stale airborne position (7 min old) should have decayed confidence."""
    age_seconds = 420  # 7 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=True)

    # Airborne decay: 5-10 min range, linear decay from 95 to 75
    # At 7 min: 95 - ((7-5) / 5) * 20 = 95 - 8 = 87
    assert 85 <= confidence <= 89


def test_calculate_confidence_very_old_airborne():
    """Very old airborne position (15 min) should be rejected (None)."""
    age_seconds = 900  # 15 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=True)

    assert confidence is None


def test_calculate_confidence_fresh_grounded():
    """Fresh grounded position (5 min old) should have full confidence (90)."""
    age_seconds = 300  # 5 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=False)

    assert confidence == 90


def test_calculate_confidence_stale_grounded():
    """Stale grounded position (15 min old) should have decayed confidence."""
    age_seconds = 900  # 15 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=False)

    # Grounded decay: 10-20 min range, linear decay from 90 to 70
    # At 15 min: 90 - ((15-10) / 10) * 20 = 90 - 10 = 80
    assert 78 <= confidence <= 82


def test_calculate_confidence_very_old_grounded():
    """Very old grounded position (25 min) should be rejected (None)."""
    age_seconds = 1500  # 25 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=False)

    assert confidence is None


def test_calculate_confidence_boundary_airborne():
    """Airborne position at exactly 10 min should have minimum confidence (75)."""
    age_seconds = 600  # 10 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=True)

    # Should be at the boundary (just barely accepted)
    assert 74 <= confidence <= 76


def test_calculate_confidence_boundary_grounded():
    """Grounded position at exactly 20 min should have minimum confidence (70)."""
    age_seconds = 1200  # 20 minutes
    confidence = fs.calculate_confidence(age_seconds, is_airborne=False)

    # Should be at the boundary (just barely accepted)
    assert 69 <= confidence <= 71
