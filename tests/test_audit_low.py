"""Regression tests for the low-severity audit fixes (#11, #24, #25, #32, ...)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import stat

import httpx
import pytest

from app import calendar_service as cal
from app import event_service as es
from app import geocode_log_service as gl
from app import location_service as loc
from app import main
from app import snapshot_service as ss
from app import weather_service as ws
from app.place_aliases import PLACE_ALIASES, find_alias

UTC = dt.timezone.utc


# #11 ---------------------------------------------------------------
def test_longest_alias_key_wins(monkeypatch):
    monkeypatch.setattr(
        "app.place_aliases.PLACE_ALIASES",
        {
            "room": {"lat": 1.0, "lon": 1.0, "name": "short"},
            "east room": {"lat": 2.0, "lon": 2.0, "name": "long"},
        },
    )
    assert find_alias("the east room, wh")["name"] == "long"
    assert find_alias("nowhere") is None


def test_calendar_resolve_uses_longest_and_cleans(monkeypatch):
    monkeypatch.setattr(
        "app.place_aliases.PLACE_ALIASES",
        {
            "room": {"lat": 1.0, "lon": 1.0, "name": "short"},
            "mar-a-lago": {"lat": 2.0, "lon": 2.0, "name": "maral"},
        },
    )
    # U+2011 non-breaking hyphens and NBSP, NFKC -> U+2010 must be mapped
    assert cal._resolve_location_to_coords("Mar‑a‑Lago room") == (2.0, 2.0)
    assert cal._resolve_location_to_coords("x room") == (1.0, 1.0)


def test_clean_maps_u2010_in_both_modules():
    for fn in (loc._clean, cal._clean):
        assert fn("Mar‑a‑Lago") == "mar-a-lago"
        assert fn("Mar‐a‐Lago") == "mar-a-lago"


def test_location_summary_alias_longest(monkeypatch):
    assert "the white house" in PLACE_ALIASES
    assert find_alias("meeting at the white house")["name"] == "The White House"


# #24 ---------------------------------------------------------------
class _Client:
    def __init__(self, exc=None, payload=None):
        self.exc, self.payload = exc, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_):
        if self.exc:
            raise self.exc

        class R:
            status_code = 200

            def json(_self):
                return self.payload

        return R()


@pytest.fixture(autouse=False)
def fresh_weather():
    ws.get_precip.cache_clear()
    yield
    ws.get_precip.cache_clear()


async def test_get_precip_network_error_returns_error(monkeypatch, fresh_weather):
    monkeypatch.setattr(
        ws.httpx,
        "AsyncClient",
        lambda *a, **k: _Client(exc=httpx.ConnectError("down")),
    )
    res = await ws.get_precip(10.0, 10.0)
    assert res["error"] is True and res["precipitating"] is None


async def test_get_precip_null_hourly_value_returns_error(monkeypatch, fresh_weather):
    hour = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    payload = {
        "hourly": {
            "time": [hour.strftime("%Y-%m-%dT%H:%M")],
            "rain": [None],
            "snowfall": [0.0],
            "weather_code": [0],
        }
    }
    monkeypatch.setattr(
        ws.httpx, "AsyncClient", lambda *a, **k: _Client(payload=payload)
    )
    res = await ws.get_precip(11.0, 11.0)
    assert res["error"] is True


# #25 ---------------------------------------------------------------
async def test_memo_skips_errors_rounds_keys_and_evicts():
    calls = []

    @ws.memo(300)
    async def fn(lat, lon, *, trace=None):
        calls.append((lat, lon))
        return {"error": True} if len(calls) == 1 else {"ok": len(calls)}

    assert (await fn(1.0, 2.0))["error"]  # error not cached
    assert (await fn(1.0, 2.0)) == {"ok": 2}
    assert (await fn(1.001, 2.001)) == {"ok": 2}  # rounded key -> cache hit
    assert len(calls) == 2


async def test_memo_evicts_expired_entries():
    @ws.memo(0)
    async def fn(lat, lon):
        return {"ok": True}

    for i in range(5):
        await fn(float(i), 0.0)
    cell = [
        c.cell_contents for c in fn.__closure__ if isinstance(c.cell_contents, dict)
    ]
    assert all(len(c) <= 1 for c in cell)


# #32 ---------------------------------------------------------------
def test_clear_debug_caches_clears_all_layers(monkeypatch):
    from app import flight_service

    flight_service._cache["x"] = (dt.datetime.now(UTC), 1)
    main._loc_cache["y"] = (dt.datetime.now(UTC), 1)
    called = []
    monkeypatch.setattr(cal._fetch_events, "cache_clear", lambda: called.append("cal"))
    monkeypatch.setattr(main.get_precip, "cache_clear", lambda: called.append("wx"))
    main._clear_debug_caches()
    assert not flight_service._cache and not main._loc_cache
    assert called == ["cal", "wx"]


def test_debug_json_not_publicly_cacheable(monkeypatch):
    from fastapi.testclient import TestClient

    async def coords(trace=None):
        return {"unknown": True}, []

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main.get_precip, "cache_clear", lambda: None, raising=False)
    with TestClient(main.app) as c:
        r = c.get("/debug.json")
        assert "Cache-Control" not in r.headers
        r2 = c.get("/healthz")
        assert r2.status_code == 200


# #35 ---------------------------------------------------------------
async def test_in_flight_resets_thunderstorm_state(monkeypatch):
    async def init_coords():
        return {"unknown": True}

    async def flying():
        return {"lat": 1.0, "lon": 2.0, "in_flight": True}

    monkeypatch.setattr(main, "current_coords", init_coords)
    monkeypatch.setattr(main, "mark_initialization_complete", lambda: None)
    monkeypatch.setattr(main, "emit_machine_started", lambda: None)
    monkeypatch.setattr(main, "START_BACKGROUND_LOOP", False)
    async with main.lifespan(main.app):
        main.app.state.prev_thunderstorm_state = "moderate"
        monkeypatch.setattr(main, "current_coords", flying)
        await main.app.state.check_and_notify()
        assert main.app.state.prev_thunderstorm_state == "none"


# #38 / #44 ---------------------------------------------------------
@pytest.fixture
def store_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "FILE", tmp_path / "debug_history.json")
    monkeypatch.setattr(gl, "FILE", tmp_path / "geocode_log.json")
    return tmp_path


def test_snapshot_write_atomic_compact_and_readable(store_files):
    ss.add_snapshot({"lat": 1.0}, {"rain": 0})
    mode = stat.S_IMODE(os.stat(ss.FILE).st_mode)
    assert mode == 0o644
    text = ss.FILE.read_text()
    assert "\n" not in text  # no indent
    assert json.loads(text)["snapshots"][0]["coords"] == {"lat": 1.0}
    assert len(ss.get_snapshots()) == 1
    assert not list(store_files.glob("*.tmp"))


def test_snapshot_failed_write_keeps_old_file_and_no_temp(store_files, monkeypatch):
    ss.add_snapshot({"a": 1}, {})

    def boom(*a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(ss.os, "replace", boom)
        ss.add_snapshot({"a": 2}, {})  # must not raise
    assert len(json.loads(ss.FILE.read_text())["snapshots"]) == 1
    assert not list(store_files.glob("*.tmp"))


def test_geocode_log_atomic_and_corrupt_safe(store_files):
    gl.FILE.write_text("{truncated")
    assert gl._load_entries() == []  # no crash
    assert (store_files / "geocode_log.json.corrupt").exists()
    gl.add_geocode_entry(query="q", result_type="no_result")
    assert stat.S_IMODE(os.stat(gl.FILE).st_mode) == 0o644
    assert "\n" not in gl.FILE.read_text()
    assert len(gl.get_geocode_entries()) == 1


def test_snapshot_corrupt_file_does_not_crash(store_files):
    ss.FILE.write_text("{")
    assert ss.get_snapshots() == []
    ss.add_snapshot({}, {})
    assert len(ss.get_snapshots()) == 1


# #42 ---------------------------------------------------------------
async def test_fire_and_forget_keeps_task_reference(monkeypatch):
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "https://example.test/hook")
    gate = asyncio.Event()

    async def slow(embed):
        await gate.wait()
        return True

    monkeypatch.setattr(es, "_post_webhook", slow)
    es._fire_and_forget({"title": "t"})
    assert len(es._tasks) == 1
    gate.set()
    await asyncio.sleep(0.01)
    assert not es._tasks


# #43 ---------------------------------------------------------------
async def test_cooldown_cleared_when_post_fails_and_pruned(monkeypatch):
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "https://example.test/hook")
    es._last_geocode_error.clear()
    old = dt.datetime.now(UTC) - dt.timedelta(seconds=es.GEOCODE_ERROR_COOLDOWN_SEC + 5)
    es._last_geocode_error["stale"] = old

    async def fail(embed):
        return False

    monkeypatch.setattr(es, "_post_webhook", fail)
    es.emit_geocode_failure("nowhere", "no_result")
    assert "stale" not in es._last_geocode_error  # pruned
    await asyncio.sleep(0.01)
    assert "nowhere" not in es._last_geocode_error  # failed post -> no cooldown

    async def ok(embed):
        return True

    monkeypatch.setattr(es, "_post_webhook", ok)
    es.emit_geocode_failure("nowhere", "no_result")
    await asyncio.sleep(0.01)
    assert "nowhere" in es._last_geocode_error
    es._last_geocode_error.clear()
