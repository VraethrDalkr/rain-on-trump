# tests/test_weather_service.py

import datetime as dt
import pytest

from app import weather_service as ws


@pytest.fixture(autouse=True)
def _clear_cache():
    # clear the per-function memo cache before and after each test
    ws.get_precip.cache_clear()
    yield
    ws.get_precip.cache_clear()


class _DummyAsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_):
        class _Resp:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        return _Resp(self._payload)


@pytest.mark.parametrize("rain,expect_rain", [(0.0, False), (1.2, True)])
@pytest.mark.asyncio
async def test_get_precip(monkeypatch, rain, expect_rain):
    """Hourly rain > 0 ⟶ `raining` True."""
    # ▶︎ match lookup format: top-of-hour "YYYY-MM-DDTHH:MM"
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [rain],
            "snowfall": [0.0],
            "weather_code": [0],
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["precipitating"] is expect_rain
    assert res["snowing"] is False
    assert res["precipitation_type"] == ("rain" if expect_rain else "none")


@pytest.mark.asyncio
async def test_get_precip_snow(monkeypatch):
    """Hourly snowfall > 0 ⟶ `snowing` True."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [0.0],
            "snowfall": [2.5],
            "weather_code": [71],
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["precipitating"] is True  # ANY precipitation
    assert res["snowing"] is True
    assert res["snow"] == 2.5
    assert res["precipitation_type"] == "snow"


@pytest.mark.asyncio
async def test_get_precip_both(monkeypatch):
    """Rain and snow together ⟶ precipitation_type 'both'."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [1.5],
            "snowfall": [3.0],
            "weather_code": [85],
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["precipitating"] is True
    assert res["snowing"] is True
    assert res["rain"] == 1.5
    assert res["snow"] == 3.0
    assert res["precipitation_type"] == "both"


# ── Error Handling Tests ────────────────────────────────────────────


class _ErrorAsyncClient:
    """Mock client that returns error responses."""

    def __init__(self, status_code, payload=None):
        self._status_code = status_code
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_):
        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        return _Resp(self._status_code, self._payload)


@pytest.mark.asyncio
async def test_get_precip_http_400_error(monkeypatch):
    """HTTP 400 error should return structured error response."""
    error_payload = {
        "error": True,
        "reason": "Cannot initialize WeatherVariable from invalid String",
    }
    monkeypatch.setattr(
        ws.httpx,
        "AsyncClient",
        lambda *_, **__: _ErrorAsyncClient(400, error_payload),
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    # Should return error structure instead of defaulting to 0.0
    assert res.get("error") is True
    assert "reason" in res
    assert res.get("precipitating") is None  # Not False - indicates error state


@pytest.mark.asyncio
async def test_get_precip_http_500_error(monkeypatch):
    """HTTP 500 error should return structured error response."""
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _ErrorAsyncClient(500, {})
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    # Should return error structure
    assert res.get("error") is True
    assert "reason" in res
    assert res.get("precipitating") is None


@pytest.mark.asyncio
async def test_get_precip_malformed_json(monkeypatch):
    """Malformed response (missing required fields) should return error."""
    # Missing 'hourly' key entirely
    malformed_payload = {"latitude": 40.0, "longitude": -70.0}
    monkeypatch.setattr(
        ws.httpx,
        "AsyncClient",
        lambda *_, **__: _DummyAsyncClient(malformed_payload),
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    # Should detect missing data and return error
    assert res.get("error") is True
    assert "reason" in res
    assert res.get("precipitating") is None


# ── Thunderstorm Detection Tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_precip_returns_weather_code(monkeypatch):
    """Weather code is included in response."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [0.0],
            "snowfall": [0.0],
            "weather_code": [3],  # Overcast
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["weather_code"] == 3
    assert res["thunderstorm"] is False
    assert res["thunderstorm_state"] == "none"


@pytest.mark.asyncio
async def test_thunderstorm_state_moderate(monkeypatch):
    """Weather code 95 → thunderstorm_state 'moderate'."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [5.0],
            "snowfall": [0.0],
            "weather_code": [95],  # Thunderstorm, slight or moderate
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["weather_code"] == 95
    assert res["thunderstorm"] is True
    assert res["thunderstorm_state"] == "moderate"


@pytest.mark.parametrize("code", [96, 97, 99])
@pytest.mark.asyncio
async def test_thunderstorm_state_severe(monkeypatch, code):
    """Weather codes 96, 97, 99 → thunderstorm_state 'severe'."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [10.0],
            "snowfall": [0.0],
            "weather_code": [code],
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["weather_code"] == code
    assert res["thunderstorm"] is True
    assert res["thunderstorm_state"] == "severe"


@pytest.mark.parametrize("code", [0, 1, 2, 3, 45, 51, 61, 71, 80, 85])
@pytest.mark.asyncio
async def test_thunderstorm_state_none(monkeypatch, code):
    """Non-thunderstorm weather codes → thunderstorm_state 'none'."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [1.0],
            "snowfall": [0.0],
            "weather_code": [code],
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["weather_code"] == code
    assert res["thunderstorm"] is False
    assert res["thunderstorm_state"] == "none"


# ── Sunrise/Sunset Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_precip_includes_sunrise_sunset(monkeypatch):
    """Sunrise and sunset are included in response when daily data present."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [0.0],
            "snowfall": [0.0],
            "weather_code": [0],
        },
        "daily": {
            "sunrise": ["2025-12-05T07:15"],
            "sunset": ["2025-12-05T16:45"],
        },
        "utc_offset_seconds": 0,
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["sunrise"] == "2025-12-05T07:15"
    assert res["sunset"] == "2025-12-05T16:45"


@pytest.mark.asyncio
async def test_get_precip_missing_daily_data(monkeypatch):
    """Missing daily data should return None for sunrise/sunset."""
    now_hour = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [0.0],
            "snowfall": [0.0],
            "weather_code": [0],
        }
        # No "daily" key
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["sunrise"] is None
    assert res["sunset"] is None
    # Should still have valid precipitation data
    assert res["precipitating"] is False


@pytest.mark.asyncio
async def test_get_precip_with_timezone_offset(monkeypatch):
    """Timezone offset is correctly applied for local time lookup."""
    # Test with EST timezone (UTC-5 = -18000 seconds)
    utc_offset = -18000

    # Current UTC time, converted to EST for the lookup
    now_utc = dt.datetime.now(dt.timezone.utc)
    local_tz = dt.timezone(dt.timedelta(seconds=utc_offset))
    now_local = now_utc.astimezone(local_tz).replace(minute=0, second=0, microsecond=0)
    lookup = now_local.strftime("%Y-%m-%dT%H:%M")

    payload = {
        "hourly": {
            "time": [lookup],
            "rain": [2.5],
            "snowfall": [0.0],
            "weather_code": [61],
        },
        "daily": {
            "sunrise": ["2025-12-05T07:00"],
            "sunset": ["2025-12-05T17:00"],
        },
        "utc_offset_seconds": utc_offset,
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)
    if isinstance(res, tuple):
        res = res[0]

    assert res["precipitating"] is True
    assert res["rain"] == 2.5
    assert res["sunrise"] == "2025-12-05T07:00"
    assert res["sunset"] == "2025-12-05T17:00"
