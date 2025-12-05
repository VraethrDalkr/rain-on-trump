"""
tests/test_debug_history.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests for /debug/history and /debug/history.json endpoints.
"""

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app import snapshot_service as ss


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    """Redirect snapshot storage to temp directory."""
    snapshot_file = tmp_path / "debug_history.json"
    monkeypatch.setattr(ss, "DIR", tmp_path)
    monkeypatch.setattr(ss, "FILE", snapshot_file)
    return tmp_path


@pytest.fixture
def client_with_snapshots(monkeypatch, snapshot_dir):
    """Create a test client with some snapshots pre-populated."""
    now = dt.datetime.now(dt.timezone.utc)

    # Create some test snapshots
    snapshots = []
    for i in range(5):
        ts = (now - dt.timedelta(hours=i)).isoformat()
        snapshots.append(
            {
                "ts": ts,
                "coords": {
                    "lat": 26.0 + i * 0.1,
                    "lon": -80.0,
                    "name": f"Location {i}",
                    "reason": "calendar_alias" if i % 2 == 0 else "newswire",
                    "confidence": 70 - i * 5,
                },
                "precip": {
                    "precipitating": i % 2 == 0,
                    "precipitation_type": "rain" if i % 2 == 0 else "none",
                    "rain": 1.5 if i % 2 == 0 else 0.0,
                    "snow": 0.0,
                },
            }
        )

    ss._save_snapshots(snapshots)
    return TestClient(main.app)


def test_debug_history_json_returns_snapshots(client_with_snapshots):
    """GET /debug/history.json returns snapshot data."""
    resp = client_with_snapshots.get("/debug/history.json")
    assert resp.status_code == 200

    data = resp.json()
    assert "snapshots" in data
    assert "stats" in data
    assert "query" in data

    assert len(data["snapshots"]) == 5
    assert data["stats"]["count"] == 5


def test_debug_history_json_respects_limit(client_with_snapshots):
    """GET /debug/history.json?limit=2 returns limited results."""
    resp = client_with_snapshots.get("/debug/history.json?limit=2")
    assert resp.status_code == 200

    data = resp.json()
    assert len(data["snapshots"]) == 2
    assert data["query"]["limit"] == 2


def test_debug_history_json_respects_since_hours(client_with_snapshots):
    """GET /debug/history.json?since_hours=2 filters by time."""
    resp = client_with_snapshots.get("/debug/history.json?since_hours=2")
    assert resp.status_code == 200

    data = resp.json()
    # Should only get snapshots from last 2 hours (Location 0, 1)
    assert len(data["snapshots"]) <= 3  # Up to 2 hours back
    assert data["query"]["since_hours"] == 2.0


def test_debug_history_json_validates_limit(client_with_snapshots):
    """GET /debug/history.json validates limit parameter."""
    # Limit too high
    resp = client_with_snapshots.get("/debug/history.json?limit=1000")
    assert resp.status_code == 422  # Validation error

    # Limit too low
    resp = client_with_snapshots.get("/debug/history.json?limit=0")
    assert resp.status_code == 422


def test_debug_history_html_returns_html(client_with_snapshots):
    """GET /debug/history returns HTML page."""
    resp = client_with_snapshots.get("/debug/history")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<title>Debug History</title>" in resp.text


def test_debug_history_html_shows_table(client_with_snapshots):
    """GET /debug/history shows snapshot data in table."""
    resp = client_with_snapshots.get("/debug/history")
    assert resp.status_code == 200

    # Check for table headers
    assert "Timestamp" in resp.text
    assert "Location" in resp.text
    assert "Source" in resp.text
    assert "Confidence" in resp.text

    # Check for data
    assert "Location 0" in resp.text


def test_debug_history_html_respects_limit(client_with_snapshots):
    """GET /debug/history?limit=2 limits table rows."""
    resp = client_with_snapshots.get("/debug/history?limit=2")
    assert resp.status_code == 200

    # Count occurrences of location names
    assert resp.text.count("Location 0") == 1  # Newest
    assert resp.text.count("Location 1") == 1
    assert resp.text.count("Location 4") == 0  # Oldest, excluded


def test_debug_history_json_empty_when_no_snapshots(snapshot_dir):
    """GET /debug/history.json returns empty when no snapshots."""
    client = TestClient(main.app)
    resp = client.get("/debug/history.json")
    assert resp.status_code == 200

    data = resp.json()
    assert data["snapshots"] == []
    assert data["stats"]["count"] == 0


def test_debug_history_html_shows_message_when_empty(snapshot_dir):
    """GET /debug/history shows message when no snapshots."""
    client = TestClient(main.app)
    resp = client.get("/debug/history")
    assert resp.status_code == 200
    assert "No snapshots yet" in resp.text


def test_debug_history_json_link_in_html(client_with_snapshots):
    """GET /debug/history includes link to JSON endpoint."""
    resp = client_with_snapshots.get("/debug/history")
    assert resp.status_code == 200
    assert "/debug/history.json" in resp.text
