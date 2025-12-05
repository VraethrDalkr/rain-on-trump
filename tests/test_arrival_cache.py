"""
tests/test_arrival_cache.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test arrival cache expiration logic, specifically the .days vs .total_seconds bug.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from app import arrival_cache as cache


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Isolate cache file to tmp_path for testing."""
    cache_file = tmp_path / "last_arrival.json"
    monkeypatch.setattr(cache, "FILE", cache_file)
    yield cache_file


def test_load_fresh_arrival(isolated_cache):
    """Recent arrival (1 hour ago) should be returned."""
    ts_recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    cache.save(40.0, -75.0, ts=ts_recent)

    result = cache.load(max_days=7)

    assert result is not None
    assert result["lat"] == 40.0
    assert result["lon"] == -75.0
    assert result["confidence"] == 30


def test_load_expired_arrival(isolated_cache):
    """Arrival older than max_days should return None."""
    ts_old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    cache.save(40.0, -75.0, ts=ts_old)

    result = cache.load(max_days=7)

    assert result is None


def test_load_boundary_sub_day_precision(isolated_cache):
    """
    CRITICAL BUG TEST: Arrival at 7d 0h 1m should be rejected (> 7 days).

    Current bug: Uses .days which rounds down (7 .days returns 7, but comparison is > not >=)
    Expected: Should use .total_seconds() for precise comparison
    """
    # Create timestamp that's 7 days + 1 minute ago (should be rejected)
    ts_boundary = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=7, minutes=1
    )
    cache.save(40.0, -75.0, ts=ts_boundary)

    result = cache.load(max_days=7)

    # 7 days + 1 minute should be rejected (> max_days)
    assert result is None, "Arrival at 7d 1m should be considered stale (> 7 days)"


def test_load_exact_7_days(isolated_cache):
    """Arrival at exactly 7 days (minus 1 second to avoid timing race) should be accepted."""
    # Use 7 days - 1 second to avoid test timing race condition
    ts_just_under = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7, seconds=-1)
    cache.save(40.0, -75.0, ts=ts_just_under)

    result = cache.load(max_days=7)

    # Just under 7 days boundary should be accepted
    assert result is not None


def test_load_just_under_7_days(isolated_cache):
    """Arrival at 6d 23h 59m 59s should still be accepted."""
    ts_just_under = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=6, hours=23, minutes=59, seconds=59
    )
    cache.save(40.0, -75.0, ts=ts_just_under)

    result = cache.load(max_days=7)

    # Just under 7 days should be accepted
    assert result is not None


def test_load_nonexistent_file(isolated_cache):
    """Load should return None when file doesn't exist."""
    result = cache.load(max_days=7)
    assert result is None


def test_load_corrupted_json(isolated_cache):
    """Load should return None and log warning for corrupted JSON."""
    isolated_cache.write_text("{ invalid json }")

    result = cache.load(max_days=7)

    assert result is None


def test_confidence_decay_fresh(isolated_cache):
    """Fresh arrival (1 hour old) should have high confidence (~30)."""
    ts_fresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    cache.save(40.0, -75.0, ts=ts_fresh)

    result = cache.load(max_days=7)

    assert result is not None
    # 1 hour = 0.042 days → confidence ≈ 30 - (0.042 * 3) ≈ 29.87
    assert result["confidence"] >= 29
    assert result["confidence"] <= 30


def test_confidence_decay_1_day(isolated_cache):
    """1-day-old arrival should have slightly decayed confidence (~27)."""
    ts_1day = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    cache.save(40.0, -75.0, ts=ts_1day)

    result = cache.load(max_days=7)

    assert result is not None
    # 1 day → confidence = 30 - (1 * 3) = 27
    assert 26.5 <= result["confidence"] <= 27.5


def test_confidence_decay_3_days(isolated_cache):
    """3-day-old arrival should have moderately decayed confidence (~21)."""
    ts_3days = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    cache.save(40.0, -75.0, ts=ts_3days)

    result = cache.load(max_days=7)

    assert result is not None
    # 3 days → confidence = 30 - (3 * 3) = 21
    assert 20.5 <= result["confidence"] <= 21.5


def test_confidence_decay_6_days(isolated_cache):
    """6-day-old arrival should have low confidence (~12)."""
    ts_6days = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)
    cache.save(40.0, -75.0, ts=ts_6days)

    result = cache.load(max_days=7)

    assert result is not None
    # 6 days → confidence = 30 - (6 * 3) = 12
    assert 11.5 <= result["confidence"] <= 12.5


def test_confidence_decay_floor(isolated_cache):
    """Very old arrival should have minimum confidence (10), not go negative."""
    ts_old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6, hours=23)
    cache.save(40.0, -75.0, ts=ts_old)

    result = cache.load(max_days=7)

    assert result is not None
    # 6.96 days → confidence = 30 - (6.96 * 3) = 9.12 → floor at 10
    assert result["confidence"] == 10
