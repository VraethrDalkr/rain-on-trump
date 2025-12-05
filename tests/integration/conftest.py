"""
tests/integration/conftest.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration test configuration - skip unless INTEGRATION_TESTS=1.

Usage:
    # Run only unit tests (default, CI-safe)
    pytest -q

    # Run integration tests locally
    INTEGRATION_TESTS=1 pytest tests/integration/ -v

    # Run everything
    INTEGRATION_TESTS=1 pytest -v
"""

from __future__ import annotations

import os
import time
from typing import Generator

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip all integration tests unless INTEGRATION_TESTS=1."""
    if os.getenv("INTEGRATION_TESTS"):
        # Integration tests enabled - run normally
        return

    skip_marker = pytest.mark.skip(
        reason="Integration tests disabled (set INTEGRATION_TESTS=1 to enable)"
    )
    for item in items:
        # Check if test is in the integration directory
        if "integration" in str(item.fspath):
            item.add_marker(skip_marker)


@pytest.fixture
def rate_limiter() -> Generator[None, None, None]:
    """Simple rate limiter for API tests.

    Adds 1 second delay after each test to respect rate limits.
    Especially important for Nominatim (1 req/sec limit).
    """
    yield
    time.sleep(1.0)


@pytest.fixture
def integration_timeout() -> float:
    """Default timeout for integration test HTTP calls (seconds)."""
    return 30.0
