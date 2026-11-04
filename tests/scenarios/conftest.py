"""Fixtures for the time-frozen location scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from .harness import World


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    """A stubbed world; persist dir and logs isolated in *tmp_path*."""
    from app import geocode_log_service, snapshot_service

    monkeypatch.setattr(geocode_log_service, "FILE", tmp_path / "geocode_log.json")
    monkeypatch.setattr(snapshot_service, "FILE", tmp_path / "debug_history.json")
    return World(monkeypatch)
