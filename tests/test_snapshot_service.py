"""
tests/test_snapshot_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests for rolling debug snapshot manager.
"""

import datetime as dt
import json

import pytest

from app import snapshot_service as ss


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    """Redirect snapshot storage to temp directory."""
    snapshot_file = tmp_path / "debug_history.json"
    monkeypatch.setattr(ss, "DIR", tmp_path)
    monkeypatch.setattr(ss, "FILE", snapshot_file)
    return tmp_path


@pytest.fixture
def sample_coords():
    """Sample coordinates for testing."""
    return {
        "lat": 26.6857,
        "lon": -80.0998,
        "name": "Palm Beach",
        "reason": "calendar_alias",
        "confidence": 65,
    }


@pytest.fixture
def sample_precip():
    """Sample precipitation data for testing."""
    return {
        "precipitating": True,
        "precipitation_type": "rain",
        "rain": 2.5,
        "snow": 0.0,
    }


def test_load_snapshots_returns_empty_when_no_file(snapshot_dir):
    """_load_snapshots returns empty list when file doesn't exist."""
    result = ss._load_snapshots()
    assert result == []


def test_load_snapshots_returns_empty_on_corrupted_file(snapshot_dir):
    """_load_snapshots returns empty list when file is corrupted."""
    ss.FILE.write_text("not valid json")
    result = ss._load_snapshots()
    assert result == []


def test_save_and_load_snapshots(snapshot_dir):
    """Snapshots can be saved and loaded."""
    snapshots = [
        {"ts": "2025-01-01T12:00:00+00:00", "coords": {"lat": 1, "lon": 2}},
        {"ts": "2025-01-01T13:00:00+00:00", "coords": {"lat": 3, "lon": 4}},
    ]
    ss._save_snapshots(snapshots)

    loaded = ss._load_snapshots()
    assert len(loaded) == 2
    assert loaded[0]["coords"]["lat"] == 1
    assert loaded[1]["coords"]["lat"] == 3


def test_prune_old_removes_stale_entries(snapshot_dir, monkeypatch):
    """_prune_old removes entries older than max age."""
    monkeypatch.setattr(ss, "SNAPSHOT_MAX_AGE_H", 24)  # 24 hours

    now = dt.datetime.now(dt.timezone.utc)
    old_ts = (now - dt.timedelta(hours=48)).isoformat()
    recent_ts = (now - dt.timedelta(hours=12)).isoformat()

    snapshots = [
        {"ts": old_ts, "coords": {"lat": 1}},
        {"ts": recent_ts, "coords": {"lat": 2}},
    ]

    pruned = ss._prune_old(snapshots)
    assert len(pruned) == 1
    assert pruned[0]["coords"]["lat"] == 2


def test_prune_old_keeps_all_when_fresh(snapshot_dir, monkeypatch):
    """_prune_old keeps all entries when all are within max age."""
    monkeypatch.setattr(ss, "SNAPSHOT_MAX_AGE_H", 24)

    now = dt.datetime.now(dt.timezone.utc)
    ts1 = (now - dt.timedelta(hours=1)).isoformat()
    ts2 = (now - dt.timedelta(hours=2)).isoformat()

    snapshots = [
        {"ts": ts1, "coords": {"lat": 1}},
        {"ts": ts2, "coords": {"lat": 2}},
    ]

    pruned = ss._prune_old(snapshots)
    assert len(pruned) == 2


def test_add_snapshot_creates_file(snapshot_dir, sample_coords, sample_precip):
    """add_snapshot creates file if it doesn't exist."""
    assert not ss.FILE.exists()

    ss.add_snapshot(coords=sample_coords, precip=sample_precip)

    assert ss.FILE.exists()
    data = json.loads(ss.FILE.read_text())
    assert len(data["snapshots"]) == 1
    assert data["snapshots"][0]["coords"]["name"] == "Palm Beach"


def test_add_snapshot_appends_to_existing(snapshot_dir, sample_coords, sample_precip):
    """add_snapshot appends to existing snapshots."""
    # Add first snapshot
    ss.add_snapshot(coords=sample_coords, precip=sample_precip)

    # Add second snapshot with different data
    coords2 = {**sample_coords, "name": "Mar-a-Lago"}
    ss.add_snapshot(coords=coords2, precip=sample_precip)

    snapshots = ss._load_snapshots()
    assert len(snapshots) == 2
    assert snapshots[0]["coords"]["name"] == "Palm Beach"
    assert snapshots[1]["coords"]["name"] == "Mar-a-Lago"


def test_add_snapshot_includes_traces(snapshot_dir, sample_coords, sample_precip):
    """add_snapshot includes trace data when provided."""
    loc_trace = [{"step": "start"}, {"step": "plane"}]
    weather_trace = [{"step": "api_call"}]

    ss.add_snapshot(
        coords=sample_coords,
        precip=sample_precip,
        loc_trace=loc_trace,
        weather_trace=weather_trace,
    )

    snapshots = ss._load_snapshots()
    assert snapshots[0]["loc_trace"] == loc_trace
    assert snapshots[0]["weather_trace"] == weather_trace


def test_get_snapshots_returns_newest_first(snapshot_dir, sample_precip):
    """get_snapshots returns snapshots sorted newest first."""
    now = dt.datetime.now(dt.timezone.utc)

    # Add snapshots with different timestamps
    for i in range(3):
        coords = {"lat": i, "name": f"Location {i}"}
        ss.add_snapshot(coords=coords, precip=sample_precip)

    snapshots = ss.get_snapshots()

    # Should be sorted newest first (Location 2 is newest)
    assert len(snapshots) == 3
    assert snapshots[0]["coords"]["name"] == "Location 2"
    assert snapshots[2]["coords"]["name"] == "Location 0"


def test_get_snapshots_respects_limit(snapshot_dir, sample_precip):
    """get_snapshots respects limit parameter."""
    for i in range(10):
        coords = {"lat": i, "name": f"Location {i}"}
        ss.add_snapshot(coords=coords, precip=sample_precip)

    snapshots = ss.get_snapshots(limit=3)
    assert len(snapshots) == 3


def test_get_snapshots_filters_by_since_hours(snapshot_dir, monkeypatch, sample_precip):
    """get_snapshots filters by since_hours parameter."""
    now = dt.datetime.now(dt.timezone.utc)

    # Create snapshots with specific timestamps
    old_snapshot = {
        "ts": (now - dt.timedelta(hours=48)).isoformat(),
        "coords": {"name": "Old"},
        "precip": sample_precip,
    }
    recent_snapshot = {
        "ts": (now - dt.timedelta(hours=1)).isoformat(),
        "coords": {"name": "Recent"},
        "precip": sample_precip,
    }

    ss._save_snapshots([old_snapshot, recent_snapshot])

    # Get only recent snapshots (last 24 hours)
    snapshots = ss.get_snapshots(since_hours=24)
    assert len(snapshots) == 1
    assert snapshots[0]["coords"]["name"] == "Recent"


def test_get_snapshot_stats_empty(snapshot_dir):
    """get_snapshot_stats returns zeros when no snapshots."""
    stats = ss.get_snapshot_stats()
    assert stats["count"] == 0
    assert stats["oldest"] is None
    assert stats["newest"] is None


def test_get_snapshot_stats_with_data(snapshot_dir, sample_coords, sample_precip):
    """get_snapshot_stats returns correct statistics."""
    # Add some snapshots
    for _ in range(5):
        ss.add_snapshot(coords=sample_coords, precip=sample_precip)

    stats = ss.get_snapshot_stats()
    assert stats["count"] == 5
    assert stats["oldest"] is not None
    assert stats["newest"] is not None
    assert "age_hours" in stats
    assert stats["max_age_hours"] == ss.SNAPSHOT_MAX_AGE_H


def test_snapshot_auto_prunes_old(snapshot_dir, monkeypatch, sample_coords, sample_precip):
    """add_snapshot automatically prunes old entries."""
    monkeypatch.setattr(ss, "SNAPSHOT_MAX_AGE_H", 1)  # 1 hour max age

    now = dt.datetime.now(dt.timezone.utc)

    # Create an old snapshot directly
    old_snapshot = {
        "ts": (now - dt.timedelta(hours=2)).isoformat(),
        "coords": {"name": "Old"},
        "precip": {},
    }
    ss._save_snapshots([old_snapshot])

    # Add a new snapshot - should trigger pruning
    ss.add_snapshot(coords=sample_coords, precip=sample_precip)

    snapshots = ss._load_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0]["coords"]["name"] == "Palm Beach"  # Only new one remains
