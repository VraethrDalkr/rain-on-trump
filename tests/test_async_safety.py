"""
tests/test_async_safety.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Document async safety patterns and limitations in the codebase.

These tests verify that the codebase correctly handles the interaction
between sync and async code, particularly around:
- asyncio.run() usage (cannot be nested)
- asyncio.to_thread() for calling sync code from async context
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


class TestOpenSkyAsyncSafety:
    """Document OpenSky's asyncio.run() usage pattern."""

    def test_opensky_uses_asyncio_run_internally(self) -> None:
        """Document: _opensky_state uses asyncio.run() internally.

        This means _opensky_state CANNOT be called from an already-running
        async context (nested asyncio.run() raises RuntimeError).

        Currently safe because get_plane_state() is called via asyncio.to_thread()
        in location_service.py.

        If this test fails, the implementation has changed and async safety
        should be reviewed.
        """
        from app.flight_service import _opensky_state

        source = inspect.getsource(_opensky_state)

        # Verify the function uses asyncio.run
        assert "asyncio.run" in source, (
            "_opensky_state implementation changed - verify async safety. "
            "If asyncio.run is removed, the to_thread wrapper in location_service "
            "may need to be updated."
        )

    async def test_get_plane_state_callable_from_async(self) -> None:
        """get_plane_state should be callable from async context via to_thread.

        This test verifies the pattern used in location_service.py works.
        """
        from app.flight_service import get_plane_state

        # Should not raise RuntimeError about nested asyncio.run
        result = await asyncio.to_thread(get_plane_state)

        # Just verify it returns something (actual value depends on ADS-B feeds)
        assert result is not None


class TestLocationServiceAsyncSafety:
    """Document location_service async patterns."""

    def test_flight_service_called_via_to_thread(self) -> None:
        """Verify location_service uses to_thread for sync flight_service.

        The flight_service.get_plane_state() uses asyncio.run() internally,
        so it must be wrapped in asyncio.to_thread() when called from the
        async current_coords() function.

        If this test fails, the implementation has changed and could cause
        RuntimeError for nested event loops.
        """
        from app.location_service import current_coords

        source = inspect.getsource(current_coords)

        # Must use to_thread to avoid nested asyncio.run
        assert "to_thread" in source, (
            "location_service.current_coords must use asyncio.to_thread() "
            "when calling get_plane_state() to avoid nested asyncio.run()"
        )

    async def test_current_coords_callable_from_async(self) -> None:
        """current_coords should be safely callable from async context.

        This is a smoke test that the async safety pattern works end-to-end.
        """
        from app.location_service import current_coords

        # Mock external calls to avoid actual network requests
        from unittest.mock import AsyncMock, patch

        with patch("app.location_service.get_plane_state") as mock_plane:
            mock_plane.return_value = {"state": None, "errors": []}

            with patch("app.location_service._vip_json", new_callable=AsyncMock) as mock_tfr:
                mock_tfr.return_value = []

                with patch("app.location_service.cal.current_event") as mock_cal:
                    mock_cal.return_value = None

                    with patch("app.location_service.get_latest_location", new_callable=AsyncMock) as mock_news:
                        mock_news.return_value = None

                        with patch("app.location_service.load_last") as mock_last:
                            mock_last.return_value = None

                            # Should not raise
                            result = await current_coords()

        assert result is not None
        assert "unknown" in result


class TestAdsbfiSyncPattern:
    """Document adsb.fi sync client usage."""

    def test_adsbfi_uses_sync_client(self) -> None:
        """adsb.fi service uses synchronous httpx.Client.

        This is intentional as it's called from the sync get_plane_state_adsb()
        which is in turn called from get_plane_state() (also sync).

        If this test fails, verify the calling pattern is still safe.
        """
        from app.adsbfi_service import get_plane_state_adsb

        source = inspect.getsource(get_plane_state_adsb)

        # Should use sync Client, not AsyncClient
        assert "httpx.Client" in source, "adsb.fi should use sync httpx.Client"
        # The function should NOT be async
        assert not asyncio.iscoroutinefunction(get_plane_state_adsb), (
            "get_plane_state_adsb should be sync (not async)"
        )


class TestWeatherServiceAsyncPattern:
    """Document weather_service async pattern."""

    def test_weather_service_is_async(self) -> None:
        """Weather service get_precip should be async.

        It's called with await from the async endpoint handlers.
        """
        import app.weather_service as ws

        # Read the module source to verify get_precip is async
        source = inspect.getsource(ws)
        # Check that get_precip is defined as async
        assert "async def get_precip" in source, "get_precip should be async"

    def test_weather_uses_async_client(self) -> None:
        """Weather service should use AsyncClient."""
        import app.weather_service as ws

        source = inspect.getsource(ws)
        assert "AsyncClient" in source, "Weather should use httpx.AsyncClient"
