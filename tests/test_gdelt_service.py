"""
tests/test_gdelt_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit-tests for the GDELT-based newswire fallback in gdelt_service.py.
"""

import pytest

import app.gdelt_service as gs
from app.gdelt_service import get_latest_location


class DummyResponse:
    """Minimal stand-in for httpx.Response with raise_for_status & json."""

    def __init__(self, json_data: dict, fail: bool = False):
        self._json = json_data
        self._fail = fail

    def raise_for_status(self) -> None:
        """Simulate HTTP error if requested."""
        if self._fail:
            raise Exception("HTTP error")

    def json(self) -> dict:
        """Return the payload we were constructed with."""
        return self._json


class DummyAsyncClient:
    """
    Dummy replacement for httpx.AsyncClient.

    Usage is via async with DummyAsyncClient() as cli: ...
    """

    def __init__(self, *args, **kwargs):
        # The test will monkey-patch self.get
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # do not suppress exceptions

    async def get(self, url: str) -> DummyResponse:
        """This method will be replaced in each test by monkeypatch."""
        raise NotImplementedError


@pytest.mark.asyncio
async def test_no_articles(monkeypatch):
    """When GDELT returns no articles, get_latest_location returns None."""
    # clear cache so each test is independent
    gs._cache.clear()

    # patch the AsyncClient to our dummy and stub .get to return no articles
    monkeypatch.setattr(gs.httpx, "AsyncClient", DummyAsyncClient)

    async def dummy_get(self, url: str):
        return DummyResponse({"articles": []})

    monkeypatch.setattr(DummyAsyncClient, "get", dummy_get)

    result = await get_latest_location(hours_back=1)
    assert result is None


@pytest.mark.asyncio
async def test_spatial_extraction(monkeypatch):
    """When GDELT returns spatial coords, we extract the first lat/lon."""
    gs._cache.clear()
    monkeypatch.setattr(gs.httpx, "AsyncClient", DummyAsyncClient)

    # Prepare a fake article with one spatial entry
    payload = {
        "articles": [
            {"spatial": [{"lat": "38.90", "lon": "-77.04", "location": "WASHINGTON"}]}
        ]
    }

    async def dummy_get(self, url: str):
        return DummyResponse(payload)

    monkeypatch.setattr(DummyAsyncClient, "get", dummy_get)

    coords = await get_latest_location(hours_back=3)
    assert isinstance(coords, dict), "Expected a coords dict"
    # Confirm numeric casting and title-casing of place name
    assert coords["lat"] == pytest.approx(38.90)
    assert coords["lon"] == pytest.approx(-77.04)
    assert coords["name"] == "News dateline: Washington"


@pytest.mark.asyncio
async def test_malformed_spatial(monkeypatch):
    """
    Entries missing lat or lon should be skipped, resulting in None when
    no valid spatial points are found.
    """
    gs._cache.clear()
    monkeypatch.setattr(gs.httpx, "AsyncClient", DummyAsyncClient)

    # Articles with spatial entries lacking numeric coords
    payload = {
        "articles": [
            {"spatial": [{"lat": "N/A", "lon": "N/A", "location": ""}]},
            {"spatial": []},
        ]
    }

    async def dummy_get(self, url: str):
        return DummyResponse(payload)

    monkeypatch.setattr(DummyAsyncClient, "get", dummy_get)

    result = await get_latest_location(hours_back=4)
    assert result is None


@pytest.mark.asyncio
async def test_http_error(monkeypatch):
    """
    If raise_for_status() fails, we catch the exception and return None.
    """
    gs._cache.clear()
    monkeypatch.setattr(gs.httpx, "AsyncClient", DummyAsyncClient)

    # Simulate an HTTP error
    async def dummy_get(self, url: str):
        return DummyResponse({}, fail=True)

    monkeypatch.setattr(DummyAsyncClient, "get", dummy_get)

    result = await get_latest_location()
    assert result is None


@pytest.mark.asyncio
async def test_request_exception(monkeypatch):
    """
    If .get() itself raises (network error), we catch and return None.
    """
    gs._cache.clear()
    monkeypatch.setattr(gs.httpx, "AsyncClient", DummyAsyncClient)

    async def dummy_get(self, url: str):
        raise Exception("Network down")

    monkeypatch.setattr(DummyAsyncClient, "get", dummy_get)

    result = await get_latest_location()
    assert result is None


@pytest.mark.asyncio
async def test_caching(monkeypatch):
    """
    The 5-minute TTL cache should prevent multiple network calls within the window.
    """
    gs._cache.clear()
    monkeypatch.setattr(gs.httpx, "AsyncClient", DummyAsyncClient)

    call_count = 0

    async def dummy_get(self, url: str):
        nonlocal call_count
        call_count += 1
        return DummyResponse({"articles": []})

    monkeypatch.setattr(DummyAsyncClient, "get", dummy_get)

    # First call triggers the network stub
    await get_latest_location()
    # Second call within TTL uses cache, so no new network call
    await get_latest_location()
    assert call_count == 1, "Expected only one network request due to caching"
