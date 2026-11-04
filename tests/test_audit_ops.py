"""Regression tests for the ops/notification audit fixes (deep #21-#37)."""

from __future__ import annotations

import asyncio
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import adsbfi_service, main
from app import push_service as ps
from push_keys import AUTH, P256DH

ROOT = Path(__file__).resolve().parent.parent
SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/aaa",
    "keys": {"p256dh": P256DH, "auth": AUTH},
}


def _sub(n: int) -> dict:
    return {**SUB, "endpoint": f"https://fcm.googleapis.com/fcm/send/{n}"}


# ───────────────────────── push_service (#21, #24, #30) ─────────────────────
@pytest.fixture
def subs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "SUB_FILE", tmp_path / "subs.json")
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "priv")
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "pub")
    return tmp_path / "subs.json"


def _endpoints() -> set[str]:
    return {s["endpoint"] for s in ps._load_subscriptions()}


def test_broadcast_does_not_revert_concurrent_changes(subs_file, monkeypatch):
    for n in (1, 2, 3):
        assert ps.add_subscription(_sub(n))["ok"]

    def fake_webpush(subscription_info, **_kw):
        # Mid-broadcast: user 2 unsubscribes, user 4 signs up, 3 changes prefs.
        if subscription_info["endpoint"].endswith("/1"):
            ps.remove_subscription(_sub(2)["endpoint"])
            ps.add_subscription(_sub(4))
            ps.update_preferences(_sub(3)["endpoint"], {"rain_stop": False})

    monkeypatch.setattr(ps, "webpush", fake_webpush)
    ps.broadcast("t", "b")

    subs = {s["endpoint"]: s for s in ps._load_subscriptions()}
    assert set(subs) == {_sub(1)["endpoint"], _sub(3)["endpoint"], _sub(4)["endpoint"]}
    assert subs[_sub(3)["endpoint"]]["preferences"]["rain_stop"] is False
    assert "last_delivery" in subs[_sub(1)["endpoint"]]


def test_broadcast_prunes_dead_subs_by_delta(subs_file, monkeypatch):
    ps.add_subscription(_sub(1))
    ps.add_subscription(_sub(2))

    class Gone(Exception):
        class response:
            status_code = 410

    def fake_webpush(subscription_info, **_kw):
        if subscription_info["endpoint"].endswith("/1"):
            ps.add_subscription(_sub(5))  # concurrent signup must survive
            raise Gone()

    monkeypatch.setattr(ps, "webpush", fake_webpush)
    assert ps.broadcast("t", "b") == 1
    assert _endpoints() == {_sub(2)["endpoint"], _sub(5)["endpoint"]}


def test_broadcast_survives_save_failure(subs_file, monkeypatch):
    ps.add_subscription(_sub(1))
    monkeypatch.setattr(ps, "webpush", lambda **_k: None)

    def boom(_subs):
        raise OSError("disk full")

    monkeypatch.setattr(ps, "_save_subscriptions", boom)
    assert ps.broadcast("t", "b") == 1  # pushes went out: no exception, no retry


def test_concurrent_adds_are_not_lost(subs_file):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda n: ps.add_subscription(_sub(n)), range(40)))
    assert len(_endpoints()) == 40


def test_persist_dir_falls_back_when_not_writable(tmp_path, monkeypatch):
    """mkdir() succeeds on an existing root-owned dir; the write probe must not."""
    monkeypatch.setenv("PUSH_DATA_DIR", str(tmp_path / "vol"))

    def deny(*_a, **_k):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(ps.tempfile, "TemporaryFile", deny)
    got = ps._determine_persist_dir()
    assert got != tmp_path / "vol"
    assert got.name == "local_data"


def test_persist_dir_used_when_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("PUSH_DATA_DIR", str(tmp_path / "vol"))
    assert ps._determine_persist_dir() == tmp_path / "vol"
    assert list((tmp_path / "vol").iterdir()) == []  # probe cleaned up


# ─────────────────────── background loop (#22-#26, #28, #29) ──────────────────
class Env:
    def __init__(self) -> None:
        self.coords: dict[str, Any] = {"unknown": True}
        self.precip: dict[str, Any] = {}
        self.sent: list[tuple] = []
        self.snaps: list[dict] = []
        self.events: list[dict] = []
        self.broadcast_error: Exception | None = None

    async def tick(self) -> None:
        await main.app.state.check_and_notify()

    def titles(self) -> list[str]:
        return [c[0] for c in self.sent]


def _precip(kind: str = "none", storm: str = "none") -> dict:
    return {
        "precipitation_type": kind,
        "precipitating": kind != "none",
        "rain": 1.0 if kind in ("rain", "both") else 0.0,
        "snow": 0.0,
        "thunderstorm_state": storm,
        "thunderstorm": storm != "none",
    }


@pytest.fixture
async def env(monkeypatch):
    e = Env()

    async def coords():
        return e.coords

    async def precip(lat, lon):
        return e.precip

    def bcast(title, body, notification_type=None):
        if e.broadcast_error:
            raise e.broadcast_error
        e.sent.append((title, body, notification_type))

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "get_precip", precip)
    monkeypatch.setattr(main, "broadcast", bcast)
    monkeypatch.setattr(main, "add_snapshot", lambda **k: e.snaps.append(k))
    monkeypatch.setattr(main, "emit_rain_state_changed", lambda **k: e.events.append(k))
    monkeypatch.setattr(main, "mark_initialization_complete", lambda: None)
    monkeypatch.setattr(main, "emit_machine_started", lambda: None)
    async with main.lifespan(main.app):
        await asyncio.sleep(0.05)  # let the loop's first (unknown) tick finish
        e.snaps.clear()
        yield e


DC = {"lat": 38.9, "lon": -77.04, "name": "White House"}
FL = {"lat": 26.68, "lon": -80.04, "name": "Mar-a-Lago"}


def _prime(kind="none", loc=DC, storm="none") -> None:
    st = main.app.state
    st.prev_precip_type = kind
    st.prev_thunderstorm_state = storm
    st.prev_location = (loc["lat"], loc["lon"])
    st.was_in_flight = False
    st.last_snapshot_mono = None


async def test_loop_interval_is_60s_and_tick_recorded(env):
    assert main.LOOP_INTERVAL_SEC == 60
    assert main.app.state.last_tick is not None  # first tick completed


async def test_relocation_without_flight_does_not_announce(env):
    _prime("rain", DC)
    env.coords, env.precip = {**FL, "reason": "calendar_alias"}, _precip("none")
    await env.tick()
    assert env.sent == []  # no "stopped raining at Mar-a-Lago"
    assert main.app.state.prev_precip_type == "none"
    assert main.app.state.prev_location == (FL["lat"], FL["lon"])
    env.precip = _precip("rain")  # real change at the new place still notifies
    await env.tick()
    assert env.titles() == ["It's Raining!"]


async def test_relocation_resets_thunderstorm_silently(env):
    _prime("none", DC, storm="moderate")
    env.coords, env.precip = {**FL}, _precip("none", "none")
    await env.tick()
    assert env.sent == []  # no "Thunderstorm has passed at Mar-a-Lago"
    assert main.app.state.prev_thunderstorm_state == "none"


async def test_same_place_change_still_notifies(env):
    _prime("rain", DC)
    env.coords, env.precip = dict(DC), _precip("none")
    await env.tick()
    assert env.sent[0][1] == "It stopped raining at White House."
    assert env.sent[0][2] == "rain_stop"


async def test_unprimed_adoption_includes_thunderstorm_state(env):
    st = main.app.state
    st.prev_precip_type, st.prev_thunderstorm_state = None, "none"
    st.prev_location = None
    env.coords, env.precip = dict(DC), _precip("rain", "moderate")
    await env.tick()
    assert st.prev_thunderstorm_state == "moderate"
    await env.tick()  # storm already underway: no "detected" alert
    assert env.sent == []


async def test_broadcast_failure_advances_state_and_runs_later_steps(env):
    _prime("none", DC)
    env.coords, env.precip = dict(DC), _precip("rain", "moderate")
    env.broadcast_error = RuntimeError("Missing VAPID keys")
    await env.tick()
    assert main.app.state.prev_precip_type == "rain"
    assert main.app.state.prev_thunderstorm_state == "moderate"
    assert len(env.events) == 1  # Discord event still emitted
    assert len(env.snaps) == 1  # snapshot still captured
    env.broadcast_error = None
    await env.tick()
    assert env.sent == []  # no retry storm / duplicates


async def test_flight_dropout_holds_state_until_grounded(env):
    st = main.app.state
    _prime("none", DC)
    st.was_in_flight, st.last_in_flight_at = True, time.monotonic()
    env.coords = {**FL, "reason": "calendar_alias"}  # feeds dropped out
    env.precip = _precip("rain")
    await env.tick()
    assert env.sent == [] and st.was_in_flight is True
    assert st.prev_precip_type == "none"
    env.coords = {**FL, "reason": "plane_ground"}  # confirmed landing
    await env.tick()
    assert env.titles() == ["Trump Landed"]
    assert st.was_in_flight is False


async def test_flight_dropout_grace_expires(env):
    st = main.app.state
    _prime("none", DC)
    st.was_in_flight = True
    st.last_in_flight_at = time.monotonic() - main.FLIGHT_GRACE_SEC - 1
    env.coords, env.precip = {**FL, "reason": "landing_inferred"}, _precip("none")
    await env.tick()
    assert st.was_in_flight is False


async def test_snapshots_at_most_once_per_5_min(env):
    _prime("none", DC)
    env.coords, env.precip = dict(DC), _precip("none")
    for _ in range(4):
        await env.tick()
    assert len(env.snaps) == 1
    main.app.state.last_snapshot_mono -= main.SNAPSHOT_INTERVAL_SEC
    await env.tick()
    assert len(env.snaps) == 2


async def test_landing_in_storm_sends_only_landing_push(env):
    st = main.app.state
    _prime("none", DC)
    st.was_in_flight, st.last_in_flight_at = True, time.monotonic()
    env.coords = {**FL, "reason": "plane_ground"}
    env.precip = _precip("rain", "moderate")
    await env.tick()
    assert env.titles() == ["Trump Landed"]  # no extra "Thunderstorm detected"
    env.precip = _precip("rain", "none")
    await env.tick()
    assert env.titles() == ["Trump Landed"]  # and no orphan "has passed"


def test_debounce_path_is_gone():
    assert not hasattr(main, "DEBOUNCE_NOTIFICATIONS")
    assert not hasattr(main, "should_notify_state_change")


# thunderstorm pairing (#25) ----------------------------------------------
def _fresh_app(prev="none", last=None):
    class A:
        class state:
            prev_thunderstorm_state = prev
            thunderstorm_last_notified = dict(last or {})

    return A


def test_end_alert_skipped_when_start_was_cooled_down(monkeypatch):
    got: list[str] = []
    monkeypatch.setattr(
        main, "broadcast", lambda t, b, notification_type=None: got.append(b)
    )
    now = main.dt.datetime.now(main.UTC)
    app = _fresh_app("none", {"none->moderate": now})
    main._maybe_send_thunderstorm_notification(
        app, {"thunderstorm_state": "moderate"}, {}
    )
    main._maybe_send_thunderstorm_notification(app, {"thunderstorm_state": "none"}, {})
    assert got == []  # no lone "has passed"
    # a later, un-suppressed storm gets its full pair
    app.state.thunderstorm_last_notified = {}
    main._maybe_send_thunderstorm_notification(
        app, {"thunderstorm_state": "moderate"}, {}
    )
    main._maybe_send_thunderstorm_notification(app, {"thunderstorm_state": "none"}, {})
    assert len(got) == 2


def test_thunderstorm_state_advances_even_if_broadcast_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no vapid")

    monkeypatch.setattr(main, "broadcast", boom)
    app = _fresh_app()
    main._maybe_send_thunderstorm_notification(
        app, {"thunderstorm_state": "severe"}, {}
    )
    assert app.state.prev_thunderstorm_state == "severe"


# healthz (#34) ---------------------------------------------------------
@pytest.fixture
def health_state():
    def _set(_mp, started, last):
        main.app.state.started_mono = started
        main.app.state.last_tick = last

    yield _set
    main.app.state.started_mono = None
    main.app.state.last_tick = None


def test_healthz_grace_period_then_stall_then_recovery(monkeypatch, health_state):
    _set = health_state
    c = TestClient(main.app)
    now = time.monotonic()
    _set(monkeypatch, now - 60, None)  # fresh start, no tick yet
    assert c.get("/healthz").status_code == 200
    _set(monkeypatch, now - 400, None)  # never ticked, past grace
    assert c.get("/healthz").status_code == 503
    _set(monkeypatch, now - 4000, now - 301)  # stalled
    assert c.get("/healthz").status_code == 503
    _set(monkeypatch, now - 4000, now - 30)  # healthy
    assert c.get("/healthz").status_code == 200


def test_healthz_ok_without_loop(monkeypatch, health_state):
    _set = health_state
    _set(monkeypatch, None, None)
    assert TestClient(main.app).get("/healthz").text == "ok"


# override rate limit + rounding (#31) -------------------------------------
def test_coord_override_is_rate_limited_and_rounded(monkeypatch):
    seen: list[tuple] = []

    async def precip(lat, lon):
        seen.append((lat, lon))
        return {"precipitating": False, "rain": 0.0}

    monkeypatch.setattr(main, "get_precip", precip)
    main.limiter.reset()
    c = TestClient(main.app)
    try:
        r = c.get("/is_it_raining.json?lat=38.897712&lon=-77.036512")
        assert r.status_code == 200
        assert seen == [(38.9, -77.04)]
        assert r.json()["coords"]["lat"] == 38.9
        codes = [
            c.get(f"/is_it_raining.json?lat={i / 7}&lon=1").status_code
            for i in range(25)
        ]
        assert codes.count(429) >= 5 and codes[0] == 200
    finally:
        main.limiter.reset()


def test_plain_requests_are_not_rate_limited_by_override(monkeypatch):
    async def coords():
        return {"lat": 1.0, "lon": 2.0, "name": "X"}

    async def precip(lat, lon):
        return {"precipitating": False}

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "get_precip", precip)
    main.limiter.reset()
    c = TestClient(main.app)
    assert all(c.get("/is_it_raining.json").status_code == 200 for _ in range(30))


# response fields (day/night) ------------------------------------------------
def test_response_carries_utc_sun_times_and_offset(monkeypatch):
    async def coords():
        return {"lat": 1.0, "lon": 2.0, "name": "X"}

    async def precip(lat, lon):
        return {
            "precipitating": False,
            "sunrise": "2025-06-15T05:30",
            "sunset": "2025-06-15T20:15",
            "sunrise_utc": "2025-06-15T10:30:00Z",
            "sunset_utc": "2025-06-16T01:15:00Z",
            "utc_offset_seconds": -18000,
        }

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "get_precip", precip)
    j = TestClient(main.app).get("/is_it_raining.json").json()
    assert j["sunrise"] == "2025-06-15T05:30"
    assert j["sunrise_utc"].endswith("Z") and j["sunset_utc"].endswith("Z")
    assert j["utc_offset_seconds"] == -18000


@pytest.mark.parametrize("which", ["in_flight", "unknown", "error"])
def test_fallback_responses_have_same_keys(monkeypatch, which):
    async def coords():
        return {
            "in_flight": {"lat": 1.0, "lon": 2.0, "in_flight": True},
            "unknown": {"unknown": True},
            "error": {"lat": 1.0, "lon": 2.0},
        }[which]

    async def precip(lat, lon):
        return {"error": True, "reason": "down", "precipitating": None}

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "get_precip", precip)
    j = TestClient(main.app).get("/is_it_raining.json").json()
    for k in ("sunrise_utc", "sunset_utc", "utc_offset_seconds"):
        assert k in j and j[k] is None


# debug cache clear (frontend #33) --------------------------------------------
def test_clear_debug_caches_clears_adsb_feed_cache():
    adsbfi_service._cache["get_plane_state_adsb"] = (
        main.dt.datetime.now(main.UTC),
        None,
    )
    main._clear_debug_caches()
    assert not adsbfi_service._cache


# deployment files (#30, #34, #35, #36, #32/#33) ----------------------------------
def test_fly_toml_has_healthz_check_and_kill_timeout():
    cfg = tomllib.loads((ROOT / "fly.toml").read_text())
    checks = cfg["http_service"]["checks"]
    assert any(c["path"] == "/healthz" for c in checks)
    assert cfg["kill_timeout"] >= 10


def test_dockerfile_creates_data_dir_owned_by_rainuser():
    text = (ROOT / "backend" / "Dockerfile").read_text()
    assert "mkdir -p /data" in text and "chown -R rainuser:rainuser /app /data" in text


def test_requirements_are_pinned():
    lines = [
        ln.split("#")[0].strip()
        for ln in (ROOT / "requirements.txt").read_text().splitlines()
    ]
    reqs = [ln for ln in lines if ln]
    assert reqs and all("==" in ln for ln in reqs)


def test_no_private_paths_in_public_source():
    # Split so the public tree never holds the literal names release.sh greps for
    private = (".clau" + "de/", "CLAU" + "DE.md")
    for f in (ROOT / "backend" / "app").glob("*.py"):
        text = f.read_text()
        assert not any(p in text for p in private), f.name


def test_release_script_scans_contents_and_parses():
    path = ROOT / "scripts" / "release.sh"
    if not path.exists():  # stripped from public releases
        pytest.skip("release.sh is dev-only")
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text()
    assert "git grep" in text and "CLAUDE" in text
