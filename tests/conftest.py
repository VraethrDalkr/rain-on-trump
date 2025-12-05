"""
tests/conftest.py
~~~~~~~~~~~~~~~~~
Global pytest fixtures.

`isolate_arrival_cache_tmp` ensures that *arrival_cache* writes its JSON
file into a per-test temporary directory, so nothing is left behind under
`backend/local_data/` after the suite runs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def isolate_arrival_cache_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Redirect ``arrival_cache.FILE`` & ``arrival_cache.DIR`` to *tmp_path*
    for every test.

    Because ``arrival_cache`` computes those globals at *import time*, we
    patch the module attributes **after** import and **before** each test
    executes, ensuring all reads/writes use the temporary directory.
    """
    # ------------------------------------------------------------------ #
    # 1. Build a unique directory for this test invocation               #
    # ------------------------------------------------------------------ #
    temp_cache_dir = tmp_path / "arrival_cache"
    temp_cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 2. Monkey-patch environment + module globals                       #
    # ------------------------------------------------------------------ #
    # Environment variable (used if arrival_cache is re-imported)
    monkeypatch.setenv("PERSIST_DIR", str(temp_cache_dir))

    # Patch the already-imported module attributes
    from app import arrival_cache as ac  # imported here to ensure patching

    ac.DIR = temp_cache_dir
    ac.FILE = temp_cache_dir / "last_arrival.json"

    # If any other module imported push_service.PERSIST_DIR, patch that too
    try:
        from app import push_service  # type: ignore

        monkeypatch.setattr(push_service, "PERSIST_DIR", temp_cache_dir, raising=False)
    except Exception:  # push_service may not be imported in pure-unit tests
        pass

    yield

    # ------------------------------------------------------------------ #
    # 3. Cleanup (pytest will also delete tmp_path)                      #
    # ------------------------------------------------------------------ #
    if temp_cache_dir.exists():
        shutil.rmtree(temp_cache_dir)
