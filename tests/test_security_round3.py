"""
tests/test_security_round3.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Regressions for the round-3 security review and bug hunt (findings #0-#17).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from push_keys import AUTH, P256DH

from app import arrival_cache
from app import calendar_service as cal
from app import event_service as es
from app import location_fusion as fusion
from app import location_service as loc
from app import main, push_service

UTC = dt.timezone.utc
SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
    "keys": {"p256dh": P256DH, "auth": AUTH},
}


@pytest.fixture(autouse=True)
def isolated_subs(monkeypatch, tmp_path):
    monkeypatch.setattr(push_service, "SUB_FILE", tmp_path / "subs.json")


@pytest.fixture
def stub_location(monkeypatch):
    async def coords(trace=None):
        c = {"lat": 38.9, "lon": -77.0, "name": "WH", "reason": "calendar_alias"}
        return (c, [{"step": "candidates"}]) if trace is not None else c

    async def precip(lat, lon, trace=None):
        r = {"precipitating": False, "rain": 0.0, "snow": 0.0}
        return (r, []) if trace is not None else r

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "get_precip", precip)


# ── #0 / #12: /debug never clears caches for anonymous callers ────────────
def test_debug_does_not_clear_caches_anonymously(monkeypatch, stub_location):
    cleared = []
    monkeypatch.setattr(main, "_clear_debug_caches", lambda: cleared.append(1))
    c = TestClient(main.app)
    r = c.get("/debug.json")
    assert r.status_code == 200 and r.json()["cache"] == "cached inputs"
    assert c.get("/debug").status_code == 200
    assert cleared == []


def test_forced_debug_refresh_needs_token_and_is_throttled(monkeypatch, stub_location):
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret")
    cleared = []
    monkeypatch.setattr(main, "_clear_debug_caches", lambda: cleared.append(1))
    c = TestClient(main.app)
    assert c.get("/debug.json?refresh=1").status_code == 403
    hdr = {"X-Admin-Token": "secret"}
    assert c.get("/debug.json?refresh=1", headers=hdr).json()["cache"] == "refreshed"
    second = c.get("/debug.json?refresh=1", headers=hdr).json()["cache"]
    assert second.startswith("refresh skipped") and cleared == [1]


def test_debug_routes_are_rate_limited(monkeypatch, stub_location):
    monkeypatch.setattr(main, "get_snapshots", lambda **k: [])
    c = TestClient(main.app)
    codes = [c.get("/debug.json").status_code for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429
    codes = [c.get("/debug/history.json").status_code for _ in range(7)]
    assert codes[-1] == 429


def test_request_threads_use_the_bounded_pool(monkeypatch):
    seen = {}

    def plane():
        seen["thread"] = threading.current_thread().name
        return {"state": None, "errors": []}

    monkeypatch.setattr(main, "get_plane_state", plane)
    assert TestClient(main.app).get("/plane_state.json").status_code == 200
    assert seen["thread"].startswith("rot-request")


# ── #1: body cap, no input echo ───────────────────────────────────────────
def test_large_body_is_rejected_by_content_length():
    r = TestClient(main.app).post("/subscribe", content=b"[" + b"0," * 20_000 + b"0]")
    assert r.status_code == 413


def test_large_chunked_body_is_rejected_while_streaming():
    def chunks():
        for _ in range(40):
            yield b"0" * 1024

    r = TestClient(main.app).post(
        "/subscribe", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_422_does_not_echo_input():
    r = TestClient(main.app).post("/subscribe", json=[1, 2, 3])
    assert r.status_code == 422
    assert "input" not in r.text


# ── #2: subscription keys, failure purge, budget ──────────────────────────
@pytest.mark.parametrize(
    "p256dh, auth",
    [
        ("x", AUTH),
        (base64.urlsafe_b64encode(b"\x05" + bytes(64)).decode(), AUTH),
        (base64.urlsafe_b64encode(b"\x04" + bytes(63)).decode(), AUTH),
        (P256DH, base64.urlsafe_b64encode(bytes(15)).decode()),
        (P256DH, "not base64!"),
    ],
)
def test_malformed_keys_are_rejected(p256dh, auth):
    sub = {**SUB, "keys": {"p256dh": p256dh, "auth": auth}}
    assert push_service.validate_subscription(sub) is False


def test_valid_padded_keys_are_accepted():
    padded = {"p256dh": P256DH + "=" * (-len(P256DH) % 4), "auth": AUTH + "=="}
    assert push_service.validate_subscription({**SUB, "keys": padded})


def test_sub_purged_after_consecutive_send_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(push_service, "VAPID_PUBLIC", "pub")
    monkeypatch.setattr(push_service, "VAPID_PRIVATE", "priv")
    assert push_service.add_subscription(SUB)["ok"]
    outcome = {"fail": True}

    def webpush(**_k):
        if outcome["fail"]:
            raise RuntimeError("400 bad request")

    monkeypatch.setattr(push_service, "webpush", webpush)
    push_service.broadcast("t", "b")
    push_service.broadcast("t", "b")
    assert push_service.get_subscription_stats()["total"] == 1
    outcome["fail"] = False  # a success resets the count
    push_service.broadcast("t", "b")
    outcome["fail"] = True
    for _ in range(2):
        push_service.broadcast("t", "b")
    assert push_service.get_subscription_stats()["total"] == 1
    push_service.broadcast("t", "b")  # third consecutive failure
    assert push_service.get_subscription_stats()["total"] == 0


def test_global_new_subscription_budget(monkeypatch):
    monkeypatch.setattr(push_service, "NEW_SUBS_PER_HOUR", 2)
    for i in range(2):
        sub = {**SUB, "endpoint": f"https://fcm.googleapis.com/fcm/send/{i}"}
        assert push_service.add_subscription(sub)["ok"]
    sub = {**SUB, "endpoint": "https://fcm.googleapis.com/fcm/send/9"}
    assert not push_service.add_subscription(sub)["ok"]
    # refreshing an existing subscription is always allowed
    sub0 = {**SUB, "endpoint": "https://fcm.googleapis.com/fcm/send/0"}
    assert push_service.add_subscription(sub0)["ok"]


# ── #3: override limits ───────────────────────────────────────────────────
def test_override_per_ip_limit_is_5_per_minute(monkeypatch, stub_location):
    c = TestClient(main.app)
    codes = [c.get(f"/is_it_raining.json?lat={i}&lon=1").status_code for i in range(6)]
    assert codes == [200] * 5 + [429]


def test_override_global_budget(monkeypatch, stub_location):
    monkeypatch.setattr(main.override_budget, "per_hour", 2)
    monkeypatch.setattr(main.limiter, "enabled", False)
    c = TestClient(main.app)
    assert c.get("/is_it_raining.json?lat=1&lon=1").status_code == 200
    assert c.get("/is_it_raining.json?lat=2&lon=1").status_code == 200
    assert c.get("/is_it_raining.json?lat=1&lon=1").status_code == 200  # cached
    assert c.get("/is_it_raining.json?lat=3&lon=1").status_code == 429


# ── #4: admin token header, rate limit, redaction ─────────────────────────
def test_admin_token_header_and_deprecated_query(monkeypatch):
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret")
    monkeypatch.setattr(main, "get_subscription_stats", lambda: {"total": 0})
    c = TestClient(main.app)
    hdr = {"X-Admin-Token": "secret"}
    assert c.get("/subscription_stats", headers=hdr).status_code == 200
    assert c.get("/subscription_stats?token=secret").status_code == 200
    assert c.get("/subscription_stats").status_code == 403
    assert c.get("/subscription_stats", headers={"X-Admin-Token": "x"}).status_code == (
        403
    )


def test_admin_routes_are_rate_limited(monkeypatch):
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret")
    c = TestClient(main.app)
    codes = [c.get("/subscription_stats").status_code for _ in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429


def test_token_is_redacted_from_logs():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "POST", "/broadcast?msg=hi&token=s3cret", "1.1", 200),
        None,
    )
    main._RedactTokens().filter(record)
    assert "s3cret" not in record.getMessage()
    assert "token=[redacted]" in record.getMessage()


# ── #6: CI token is read-only ─────────────────────────────────────────────
def test_ci_workflow_has_read_only_permissions():
    ci = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    text = ci.read_text()
    assert "permissions:" in text and "contents: read" in text


# ── #8 / #9: calendar stale-while-error, lock-free hits ───────────────────
def _fail():
    raise cal.httpx.ConnectError("down")


def test_outage_serves_last_good_calendar(monkeypatch):
    good = [{"dtstart_utc": dt.datetime.now(UTC), "summary": "x", "location": "y"}]
    monkeypatch.setattr(cal._download_events, "__wrapped__", lambda: good)
    assert cal._fetch_events() == good
    cal._download_events.cache_clear()  # memo expired
    errors = []
    monkeypatch.setattr(cal, "emit_api_error", lambda *a: errors.append(a))
    monkeypatch.setattr(cal._download_events, "__wrapped__", _fail)
    assert cal._fetch_events() == good  # stale-while-error
    assert errors and errors[0][0] == "factba.se"
    assert cal._fetch_events() == good  # during the back-off too


def test_last_good_calendar_expires_after_24h(monkeypatch):
    old = dt.datetime.now(UTC) - dt.timedelta(hours=25)
    monkeypatch.setattr(cal, "_last_good", ([{"x": 1}], old))
    monkeypatch.setattr(cal._download_events, "__wrapped__", _fail)
    monkeypatch.setattr(cal, "emit_api_error", lambda *a: None)
    assert cal._fetch_events() == []


def test_cache_hit_and_stale_miss_never_wait_for_a_download(monkeypatch):
    good = [{"x": 1}]
    cal._download_events.prime(good)
    with cal._DOWNLOAD_LOCK:  # a slow download in progress
        start = time.monotonic()
        assert cal._fetch_events() == good  # hit: no lock
        cal._download_events.cache_clear()
        monkeypatch.setattr(cal, "_last_good", (good, dt.datetime.now(UTC)))
        assert cal._fetch_events() == good  # miss with a stale copy: no wait
        assert time.monotonic() - start < 0.5


def test_prefetch_honours_the_failure_backoff(monkeypatch):
    calls = []

    def _download():
        calls.append(1)
        raise cal.httpx.ConnectError("down")

    monkeypatch.setattr(cal._download_events, "__wrapped__", _download)
    monkeypatch.setattr(cal, "emit_api_error", lambda *a: None)
    cal.prefetch_events()
    cal.prefetch_events()
    assert len(calls) == 1 and cal._feed_failed_at is not None


# ── #10 / #16: geocoder errors are never cached ───────────────────────────
class _Loc:
    latitude, longitude, address = 39.0, -77.0, "Somewhere"
    raw = {"importance": 0.6, "address": {"country_code": "us"}}


def test_partial_geocoder_failure_is_not_persisted(monkeypatch):
    def raw(query, country_codes=None, **k):
        if country_codes is None:
            raise RuntimeError("429 Too Many Requests")
        return [_Loc()]

    monkeypatch.setattr(loc, "_geocode_raw", raw)
    monkeypatch.setattr(loc, "add_geocode_entry", lambda **k: None)
    got = loc._geocode_candidates("Partial Venue")
    assert got and loc.cached_candidates("Partial Venue") is None
    key = loc._clean("Partial Venue")
    assert key not in loc._geocode_neg_cache and key in loc._geocode_err_cache


def test_total_geocoder_failure_backs_off_briefly(monkeypatch):
    def raw(*a, **k):
        raise RuntimeError("timeout")

    failures = []
    monkeypatch.setattr(loc, "_geocode_raw", raw)
    monkeypatch.setattr(loc, "add_geocode_entry", lambda **k: None)
    monkeypatch.setattr(loc, "emit_geocode_failure", lambda **k: failures.append(k))
    assert loc._geocode_candidates("Down Venue") == []
    assert failures[0]["result_type"] == "error"
    key = loc._clean("Down Venue")
    assert key not in loc._geocode_neg_cache
    loc._geocode_err_cache[key] -= dt.timedelta(seconds=loc.GEOCODE_ERROR_TTL_S + 1)
    assert not loc._neg_cached("Down Venue")  # retried after 5 min, not 12 h


def test_geocoder_raises_instead_of_swallowing():
    assert loc._geocode_raw.swallow_exceptions is False
    assert loc._geocode_raw.max_retries == 0


def test_negative_results_survive_a_restart_and_expired_entries_are_pruned(
    monkeypatch,
):
    monkeypatch.setattr(loc, "_geocode_raw", lambda *a, **k: None)
    monkeypatch.setattr(loc, "add_geocode_entry", lambda **k: None)
    monkeypatch.setattr(loc, "emit_geocode_failure", lambda **k: None)
    old = (dt.datetime.now(UTC) - dt.timedelta(days=31)).isoformat()
    loc._geocode_store()["stale venue"] = {"ts": old, "candidates": [{"lat": 1}]}
    assert loc._geocode_candidates("Nowhere Hall") == []
    loc._geocode_neg_cache.clear()
    loc._geocode_cache.clear()
    monkeypatch.setattr(loc, "_geocode_cache_path", None)  # restart
    assert loc._neg_cached("Nowhere Hall")
    assert "stale venue" not in loc._geocode_store()


# ── #11: repeated airborne fixes ──────────────────────────────────────────
def test_repeated_fix_keeps_the_descent_visible():
    now = dt.datetime.now(UTC)
    t1, t2 = now - dt.timedelta(minutes=10), now - dt.timedelta(minutes=8)
    arrival_cache.record_airborne(35.4, -82.5, 3000, None, t1, "82-8000")
    arrival_cache.record_airborne(35.4, -82.5, 1200, None, t2, "82-8000")
    writes = arrival_cache.version()
    arrival_cache.record_airborne(35.4, -82.5, 1200, None, t2, "82-8000")  # memo
    assert arrival_cache.version() == writes  # no rewrite
    assert arrival_cache.airframes()["82-8000"]["airborne"]["prev_altitude_ft"] == 3000
    assert arrival_cache.maybe_infer_landing(now) is True


# ── #13: webhook posts never block worker threads ─────────────────────────
def test_webhook_from_a_thread_does_not_block(monkeypatch):
    monkeypatch.setattr(es, "DISCORD_WEBHOOK_URL", "https://discord.example/hook")
    done = threading.Event()

    async def slow(embed):
        await asyncio.sleep(0.5)
        done.set()
        return True

    monkeypatch.setattr(es, "_post_webhook", slow)
    elapsed = {}

    def worker():
        start = time.monotonic()
        es._fire_and_forget({"title": "t"})
        elapsed["s"] = time.monotonic() - start

    th = threading.Thread(target=worker)
    th.start()
    th.join()
    assert elapsed["s"] < 0.2
    assert done.wait(2)


# ── #14: maintenance runs off the alert path, single-flight ───────────────
async def test_maintenance_is_single_flight(monkeypatch):
    calls, release = [], threading.Event()

    def slow_pregeocode(limit):
        calls.append(1)
        release.wait(2)
        return 0

    async def coords():
        return {"unknown": True}

    monkeypatch.setattr(main, "pregeocode_events", slow_pregeocode)
    monkeypatch.setattr(main._calendar_service, "prefetch_events", lambda: None)
    monkeypatch.setattr(main, "poll_flight_history", lambda: None)
    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "LOOP_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(main, "emit_machine_started", lambda: None)
    monkeypatch.setattr(main, "mark_initialization_complete", lambda: None)
    async with main.lifespan(main.app):
        await asyncio.sleep(0.4)
        assert main.app.state.last_tick is not None  # alerts not held up
        assert len(calls) == 1  # still-running batch never restarted
        release.set()
    release.set()


# ── #15: geocode candidates chosen with context before corroboration ──────
def test_foreign_arrival_corroborates_with_the_right_candidate():
    now = dt.datetime(2026, 6, 15, 11, 0, tzinfo=UTC)
    gva = (46.2381, 6.1090)
    events = [
        {
            "dtstart_utc": now - dt.timedelta(hours=1),
            "summary": "The President arrives at Geneva Airport",
            "location": "Geneva Airport",
        },
        {
            "dtstart_utc": now - dt.timedelta(minutes=20),
            "summary": "The President arrives at Evian Resort",
            "location": "Evian Resort, Evian-les-Baines, France",
        },
    ]
    geocodes = {
        "geneva airport": [
            {"lat": 42.8687, "lon": -76.9777, "name": "Geneva, NY"},
            {"lat": gva[0], "lon": gva[1], "name": "Geneva Airport"},
        ]
    }
    landing = {
        "lat": gva[0],
        "lon": gva[1],
        "ts": now - dt.timedelta(hours=1, minutes=5),
        "kind": "observed",
        "tail": "82-8000",
    }
    track = fusion.Track(airframe="82-8000", tail="82-8000", landing=landing)
    decision = fusion.fuse(
        fusion.Inputs(events=events, geocodes=geocodes, tracks=[track]), now
    )
    land = next(c for c in decision.claims if c.kind == "landing")
    assert land.R == 1.0  # corroborated by the "arrives at Geneva Airport" item
