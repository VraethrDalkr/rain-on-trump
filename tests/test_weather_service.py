# tests/test_weather_service.py

import datetime as dt
import pytest

from app import weather_service as ws

FROZEN = dt.datetime(2025, 6, 15, 14, 37, 20, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """One fixed instant for the code under test AND the payload builders.

    Without it a test building its hourly key and get_precip reading the clock
    could straddle an hour/quarter boundary and flake (deep #18).
    """
    monkeypatch.setattr(ws, "_utcnow", lambda: FROZEN)


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
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["precipitating"] is expect_rain
    assert res["snowing"] is False
    assert res["precipitation_type"] == ("rain" if expect_rain else "none")


@pytest.mark.asyncio
async def test_get_precip_snow(monkeypatch):
    """Hourly snowfall > 0 ⟶ `snowing` True."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["precipitating"] is True  # ANY precipitation
    assert res["snowing"] is True
    assert res["snow"] == 2.5
    assert res["precipitation_type"] == "snow"


@pytest.mark.asyncio
async def test_get_precip_both(monkeypatch):
    """Rain and snow together ⟶ precipitation_type 'both'."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    # Should detect missing data and return error
    assert res.get("error") is True
    assert "reason" in res
    assert res.get("precipitating") is None


# ── Thunderstorm Detection Tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_precip_returns_weather_code(monkeypatch):
    """Weather code is included in response."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["weather_code"] == 3
    assert res["thunderstorm"] is False
    assert res["thunderstorm_state"] == "none"


@pytest.mark.asyncio
async def test_thunderstorm_state_moderate(monkeypatch):
    """Weather code 95 → thunderstorm_state 'moderate'."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["weather_code"] == 95
    assert res["thunderstorm"] is True
    assert res["thunderstorm_state"] == "moderate"


@pytest.mark.parametrize("code", [96, 97, 99])
@pytest.mark.asyncio
async def test_thunderstorm_state_severe(monkeypatch, code):
    """Weather codes 96, 97, 99 → thunderstorm_state 'severe'."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["weather_code"] == code
    assert res["thunderstorm"] is True
    assert res["thunderstorm_state"] == "severe"


@pytest.mark.parametrize("code", [0, 1, 2, 3, 45, 51, 61, 71, 80, 85])
@pytest.mark.asyncio
async def test_thunderstorm_state_none(monkeypatch, code):
    """Non-thunderstorm weather codes → thunderstorm_state 'none'."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["weather_code"] == code
    assert res["thunderstorm"] is False
    assert res["thunderstorm_state"] == "none"


# ── Sunrise/Sunset Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_precip_includes_sunrise_sunset(monkeypatch):
    """Sunrise and sunset are included in response when daily data present."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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

    assert res["sunrise"] == "2025-12-05T07:15"
    assert res["sunset"] == "2025-12-05T16:45"


@pytest.mark.asyncio
async def test_get_precip_missing_daily_data(monkeypatch):
    """Missing daily data should return None for sunrise/sunset."""
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
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
    now_utc = ws._utcnow()
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

    assert res["precipitating"] is True
    assert res["rain"] == 2.5
    assert res["sunrise"] == "2025-12-05T07:00"
    assert res["sunset"] == "2025-12-05T17:00"


# ── 15-minute window (rain in any of the last 4 completed steps) ──────────
def _payload_15min(
    rain_steps, snow_steps=None, hourly_rain=0.0, future=0.0, offset=0, now=None
):
    """Build a response whose minutely_15 steps end at the current quarter-hour.

    *rain_steps* are the completed steps, oldest first; *future* is one
    not-yet-completed step that must be ignored. Timestamps are in the
    location's local zone (*offset* seconds from UTC), like Open-Meteo's.
    """
    tz = dt.timezone(dt.timedelta(seconds=offset))
    now = (now or ws._utcnow()).astimezone(tz)
    q = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    n = len(rain_steps)
    times = [(q - dt.timedelta(minutes=15 * (n - 1 - i))) for i in range(n)]
    times.append(q + dt.timedelta(minutes=15))
    fmt = [t.strftime("%Y-%m-%dT%H:%M") for t in times]
    hour = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    return {
        "utc_offset_seconds": offset,
        "hourly": {
            "time": [hour],
            "rain": [hourly_rain],
            "snowfall": [0.0],
            "weather_code": [0],
        },
        "minutely_15": {
            "time": fmt,
            "rain": list(rain_steps) + [future],
            "snowfall": list(snow_steps or [0.0] * n) + [0.0],
        },
    }


async def _precip(monkeypatch, payload):
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )
    res = await ws.get_precip(40.0, -70.0)
    return res


@pytest.mark.parametrize(
    "steps, expected",
    [
        ([0, 0, 0, 0, 0, 0], False),
        ([0, 0, 0, 0, 0, 0.1], True),  # rain in the latest quarter-hour
        ([0, 0, 0.3, 0, 0, 0], True),  # last rain 45 min ago: still a spell
        ([0.5, 0.2, 0, 0, 0, 0], False),  # 60 dry minutes: spell over
    ],
)
async def test_rain_window_last_hour(monkeypatch, steps, expected):
    res = await _precip(monkeypatch, _payload_15min(steps))
    assert res["precipitating"] is expected


async def test_rain_total_is_last_hour_sum(monkeypatch):
    res = await _precip(monkeypatch, _payload_15min([9.9, 0.1, 0.2, 0.0, 0.3]))
    assert res["rain"] == pytest.approx(0.6)


async def test_future_step_is_ignored(monkeypatch):
    res = await _precip(monkeypatch, _payload_15min([0, 0, 0, 0], future=2.0))
    assert res["precipitating"] is False


async def test_15min_overrides_lagging_hourly_value(monkeypatch):
    """The previous hour was wet but the last 60 min are dry: not raining."""
    res = await _precip(monkeypatch, _payload_15min([0] * 5, hourly_rain=1.4))
    assert res["precipitating"] is False


async def test_snow_from_15min(monkeypatch):
    payload = _payload_15min([0] * 4, snow_steps=[0, 0, 0.2, 0])
    res = await _precip(monkeypatch, payload)
    assert res["precipitation_type"] == "snow"


async def test_missing_15min_values_fall_back_to_hourly(monkeypatch):
    payload = _payload_15min([0, 0, 0, None], hourly_rain=0.4)
    res = await _precip(monkeypatch, payload)
    assert res["precipitating"] is True
    assert res["rain"] == pytest.approx(0.4)


# ── deep #11: non-UTC locations (UTC would pick the wrong completed steps) ──
OFFSETS = [-18000, 0, 19800, -12600]


@pytest.mark.parametrize("offset", OFFSETS)
@pytest.mark.parametrize(
    "steps, future, expected",
    [
        ([0, 0, 0, 0, 0, 0], 0.0, False),
        ([0, 0, 0, 0, 0, 0.1], 0.0, True),
        ([0.5, 0.2, 0, 0, 0, 0], 0.0, False),
        ([0, 0, 0, 0, 0, 0], 9.0, False),  # current step not complete: ignored
    ],
)
async def test_rain_window_in_local_zone(monkeypatch, offset, steps, future, expected):
    res = await _precip(
        monkeypatch, _payload_15min(steps, future=future, offset=offset)
    )
    assert res["precipitating"] is expected
    assert res["utc_offset_seconds"] == offset


async def test_rain_window_straddles_local_midnight(monkeypatch):
    now = dt.datetime(2025, 6, 15, 19, 5, 0, tzinfo=dt.timezone.utc)  # 00:35 +05:30
    monkeypatch.setattr(ws, "_utcnow", lambda: now)
    payload = _payload_15min([0.3, 0, 0, 0, 0, 0], offset=19800, now=now)
    # window is the last four steps only: the 0.3 step (>1 h ago) is excluded
    assert (await _precip(monkeypatch, payload))["precipitating"] is False
    ws.get_precip.cache_clear()
    payload = _payload_15min([0, 0, 0.3, 0, 0, 0], offset=19800, now=now)
    assert (await _precip(monkeypatch, payload))["precipitating"] is True


async def test_utc_clock_would_be_wrong_for_offset_zone(monkeypatch):
    """Guard against the mutant that compares local keys with the UTC clock."""
    payload = _payload_15min([0, 0, 0, 0, 0, 0.4], offset=19800)
    assert (await _precip(monkeypatch, payload))["rain"] == 0.4


# ── deep #20: documented return shape ─────────────────────────────────────
async def test_get_precip_returns_bare_dict_without_trace(monkeypatch):
    res = await _precip(monkeypatch, _payload_15min([0, 0, 0, 0]))
    assert isinstance(res, dict) and "precipitating" in res


async def test_get_precip_returns_tuple_with_trace_even_when_cached(monkeypatch):
    monkeypatch.setattr(
        ws.httpx,
        "AsyncClient",
        lambda *_, **__: _DummyAsyncClient(_payload_15min([0, 0, 0, 0])),
    )
    bare = await ws.get_precip(40.0, -70.0)  # populates the memo
    res, trace = await ws.get_precip(40.0, -70.0, trace=[])  # cache hit
    assert res == bare and isinstance(trace, list) and trace
    again = await ws.get_precip(40.0, -70.0)
    assert isinstance(again, dict)


# ── day/night: UTC sunrise/sunset + offset ────────────────────────────────
async def test_sunrise_sunset_utc_and_offset(monkeypatch):
    payload = _payload_15min([0, 0, 0, 0], offset=-18000)
    payload["daily"] = {
        "sunrise": ["2025-06-15T05:30"],
        "sunset": ["2025-06-15T20:15"],
    }
    res = await _precip(monkeypatch, payload)
    assert res["sunrise"] == "2025-06-15T05:30"  # unchanged, naive local
    assert res["sunrise_utc"] == "2025-06-15T10:30:00Z"
    assert res["sunset_utc"] == "2025-06-16T01:15:00Z"  # crosses UTC midnight
    assert res["utc_offset_seconds"] == -18000


async def test_sunrise_utc_none_without_daily(monkeypatch):
    res = await _precip(monkeypatch, _payload_15min([0, 0, 0, 0]))
    assert res["sunrise_utc"] is None and res["sunset_utc"] is None


def test_memo_is_size_capped():
    ws.MEMO_MAX_ENTRIES  # exists
    import asyncio

    @ws.memo(300)
    async def fn(a):
        return {"a": a}

    async def go():
        for i in range(ws.MEMO_MAX_ENTRIES + 20):
            await fn(i)

    asyncio.run(go())
    cell = [
        c.cell_contents for c in fn.__closure__ if isinstance(c.cell_contents, dict)
    ]
    assert all(len(c) <= ws.MEMO_MAX_ENTRIES for c in cell)


def _hourly_payload(**extra):
    now_hour = ws._utcnow().replace(minute=0, second=0, microsecond=0)
    return {
        "hourly": {
            "time": [now_hour.strftime("%Y-%m-%dT%H:%M")],
            "rain": [0.0],
            "snowfall": [0.0],
            "weather_code": [0],
        },
        **extra,
    }


@pytest.mark.asyncio
async def test_wind_kmh_from_current(monkeypatch):
    """Current 10 m wind speed (km/h) drives the frontend's rain slant."""
    urls = []

    class _Client(_DummyAsyncClient):
        async def get(self, url, *a):
            urls.append(url)
            return await super().get(url, *a)

    payload = _hourly_payload(current={"wind_speed_10m": 23.4})
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda *_, **__: _Client(payload))

    res = await ws.get_precip(40.0, -70.0)

    assert res["wind_kmh"] == 23.4
    assert "current=wind_speed_10m" in urls[0]


@pytest.mark.parametrize("current", [None, {}, {"wind_speed_10m": None}, "junk"])
@pytest.mark.asyncio
async def test_wind_kmh_missing_is_none(monkeypatch, current):
    payload = _hourly_payload(**({} if current is None else {"current": current}))
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *_, **__: _DummyAsyncClient(payload)
    )

    res = await ws.get_precip(40.0, -70.0)

    assert res["wind_kmh"] is None
