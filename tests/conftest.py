"""
tests/conftest.py
~~~~~~~~~~~~~~~~~
Global pytest fixtures.

`isolate_arrival_cache_tmp` ensures that *arrival_cache* writes its JSON
file into a per-test temporary directory, so nothing is left behind under
`backend/local_data/` after the suite runs.

`block_real_network` makes every real outbound HTTP request fail in unit
tests, so a forgotten stub can't hit live APIs or post to Discord.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest
import requests
import requests.adapters

pytest_plugins = ["pytest_asyncio"]

_NO_NETWORK = "real network access is disabled in unit tests (stub this call)"


@pytest.fixture(autouse=True)
def block_real_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail real HTTP requests and disable the Discord webhook in unit tests.

    ``main.py`` calls ``load_dotenv()``, so a real ``DISCORD_WEBHOOK_URL`` from
    ``.env`` would otherwise make event tests post to the live channel. Only
    the real transports are patched: Starlette's ``TestClient`` and
    pytest-httpx mocks (applied after autouse fixtures) keep working. Tests
    under ``tests/integration/`` are left untouched.
    """
    if "integration" in request.path.parts:
        return

    from app import event_service

    monkeypatch.setattr(event_service, "DISCORD_WEBHOOK_URL", "")

    def _blocked_sync(self: object, req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"{_NO_NETWORK}: {req.url}", request=req)

    async def _blocked_async(self: object, req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"{_NO_NETWORK}: {req.url}", request=req)

    def _blocked_requests(self: object, req: requests.PreparedRequest, **_: object):
        raise requests.ConnectionError(f"{_NO_NETWORK}: {req.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_sync)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async
    )
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked_requests)


@pytest.fixture(autouse=True)
def clear_calendar_cache() -> None:
    """
    Clear the calendar_service cache before each test.

    The _fetch_events() function caches results for 15 minutes, which can
    cause monkeypatched httpx.get to be bypassed if stale cache data exists.
    """
    from app import calendar_service as cal

    if hasattr(cal._fetch_events, "cache_clear"):
        cal._fetch_events.cache_clear()
    yield
    # Clear again after test to avoid cross-test pollution
    if hasattr(cal._fetch_events, "cache_clear"):
        cal._fetch_events.cache_clear()


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


@pytest.fixture(autouse=True)
def reset_request_budgets() -> None:
    """Reset per-process request budgets (rate limits, hourly budgets) so one
    test's traffic never throttles the next."""
    import sys

    from app import push_service

    push_service._new_sub_times.clear()
    main = sys.modules.get("app.main")
    if main is not None:
        main.limiter.reset()
        main.override_budget.reset()
        main._last_forced_refresh = None
