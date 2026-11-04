"""Regression tests for push/main audit fixes (#27-#41)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from app import main
from app import push_service as ps
from push_keys import AUTH, P256DH

FCM = "https://fcm.googleapis.com/fcm/send/"


def _sub(n: int | str, **extra: Any) -> dict[str, Any]:
    return {
        "endpoint": f"{FCM}{n}",
        "keys": {"p256dh": P256DH, "auth": AUTH},
        **extra,
    }


@pytest.fixture
def subs_file(tmp_path, monkeypatch):
    f = tmp_path / "subs.json"
    monkeypatch.setattr(ps, "SUB_FILE", f)
    monkeypatch.setattr(ps, "VAPID_PRIVATE", "k")
    monkeypatch.setattr(ps, "VAPID_PUBLIC", "k")
    return f


# #27 ---------------------------------------------------------------
@pytest.mark.parametrize("token", ["", "   ", "bad", "ünï"])
def test_admin_refused_when_token_unset(monkeypatch, token):
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "")
    monkeypatch.setattr(main, "broadcast", lambda *a, **k: 1)
    c = TestClient(main.app)
    assert c.post("/broadcast", params={"msg": "x", "token": token}).status_code == 403
    assert c.get("/subscription_stats", params={"token": token}).status_code == 403
    assert c.post("/cleanup_subscriptions", params={"token": token}).status_code == 403


def test_non_ascii_token_is_403_not_500(monkeypatch):
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret")
    c = TestClient(main.app)
    r = c.get("/subscription_stats", params={"token": "ünï"})
    assert r.status_code == 403


# #28 ---------------------------------------------------------------
async def test_unprimed_state_adopted_then_notifies(monkeypatch):
    calls: list[tuple] = []
    seq = iter(["none", "rain"])

    async def coords():
        return {"lat": 1.0, "lon": 2.0, "name": "X"}

    async def precip(lat, lon):
        return {"precipitation_type": next(seq), "rain": 1.0, "snow": 0.0}

    monkeypatch.setattr(main, "current_coords", coords)
    monkeypatch.setattr(main, "get_precip", precip)
    monkeypatch.setattr(main, "broadcast", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(main, "add_snapshot", lambda **k: None)
    monkeypatch.setattr(main, "emit_rain_state_changed", lambda **k: None)
    monkeypatch.setattr(main, "mark_initialization_complete", lambda: None)
    monkeypatch.setattr(main, "emit_machine_started", lambda: None)
    monkeypatch.setattr(
        main, "_maybe_send_thunderstorm_notification", lambda *a, **k: None
    )

    async def fake_init_coords():
        return {"unknown": True}

    # Start up with unknown location -> prev_precip_type stays None
    monkeypatch.setattr(main, "current_coords", fake_init_coords)
    monkeypatch.setattr(main, "START_BACKGROUND_LOOP", False)
    async with main.lifespan(main.app):
        monkeypatch.setattr(main, "current_coords", coords)
        assert main.app.state.prev_precip_type is None
        await main.app.state.check_and_notify()  # adopts "none", no alert
        assert calls == []
        assert main.app.state.prev_precip_type == "none"
        await main.app.state.check_and_notify()  # none -> rain
        assert len(calls) == 1
        # #29: preference type passed through
        assert calls[0][1]["notification_type"] == "rain_start"


# #29 ---------------------------------------------------------------
def test_thunderstorm_passes_notification_type(monkeypatch):
    got: list[str | None] = []
    monkeypatch.setattr(
        main,
        "broadcast",
        lambda t, b, notification_type=None: got.append(notification_type),
    )

    class App:
        class state:
            prev_thunderstorm_state = "none"
            thunderstorm_last_notified: dict = {}

    main._maybe_send_thunderstorm_notification(
        App, {"thunderstorm_state": "moderate"}, {}
    )
    main._maybe_send_thunderstorm_notification(App, {"thunderstorm_state": "none"}, {})
    assert got == ["thunderstorm_start", "thunderstorm_end"]


# #34 ---------------------------------------------------------------
def test_rate_limit_key_prefers_fly_client_ip():
    class Req:
        headers = {"fly-client-ip": "9.9.9.9"}

        class client:
            host = "172.16.0.1"

    assert main._client_ip(Req) == "9.9.9.9"
    Req.headers = {}
    assert main._client_ip(Req) == "172.16.0.1"


# #36 / #37 -----------------------------------------------------------
def _exc(status: int | None) -> WebPushException:
    class R:
        status_code = status

    return WebPushException("boom", response=R() if status else None)


def test_only_404_410_prune(subs_file, monkeypatch):
    for n in (1, 2, 3, 4):
        assert ps.add_subscription(_sub(n))["ok"]
    codes = {"1": 404, "2": 410, "3": 503, "4": 429}

    def fake(subscription_info, **kw):
        raise _exc(codes[subscription_info["endpoint"].rsplit("/", 1)[1]])

    monkeypatch.setattr(ps, "webpush", fake)
    assert ps.broadcast("t", "b") == 0
    left = {s["endpoint"].rsplit("/", 1)[1] for s in json.loads(subs_file.read_text())}
    assert left == {"3", "4"}


def test_bad_sub_never_aborts_broadcast(subs_file, monkeypatch):
    for n in (1, 2, 3):
        ps.add_subscription(_sub(n))
    data = json.loads(subs_file.read_text())
    data[1]["preferences"] = "x"  # poisoned prefs
    subs_file.write_text(json.dumps(data))
    seen: list[str] = []

    def fake(subscription_info, **kw):
        n = subscription_info["endpoint"].rsplit("/", 1)[1]
        if n == "1":
            raise ConnectionError("down")
        seen.append(n)

    monkeypatch.setattr(ps, "webpush", fake)
    assert ps.broadcast("t", "b", notification_type="rain_start") == 2
    assert seen == ["2", "3"]
    assert len(json.loads(subs_file.read_text())) == 3


# #38 ---------------------------------------------------------------
def test_corrupt_file_does_not_crash(subs_file):
    subs_file.write_text('[{"endpoint": "https://fcm.googleapis.com/x"')
    assert ps._load_subscriptions() == []
    assert ps.add_subscription(_sub(1))["ok"]
    assert len(json.loads(subs_file.read_text())) == 1


def test_save_is_atomic(subs_file, monkeypatch):
    ps._save_subscriptions([{"endpoint": "https://fcm.googleapis.com/keep"}])

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ps.os, "replace", boom)
    with pytest.raises(OSError):
        ps._save_subscriptions([])
    assert json.loads(subs_file.read_text())[0]["endpoint"].endswith("/keep")
    assert not list(subs_file.parent.glob("*.tmp"))


# #39 ---------------------------------------------------------------
def test_broadcast_route_runs_off_event_loop(monkeypatch):
    import threading

    seen: list[int] = []
    monkeypatch.setattr(main, "BROADCAST_TOKEN", "secret")
    monkeypatch.setattr(
        main, "broadcast", lambda *a, **k: seen.append(threading.get_ident()) or 1
    )
    c = TestClient(main.app)
    assert (
        c.post("/broadcast", params={"msg": "x", "token": "secret"}).status_code == 200
    )
    assert seen and seen[0] != threading.get_ident()


def test_webpush_gets_timeout(subs_file, monkeypatch):
    ps.add_subscription(_sub(1))
    kw_seen: dict = {}
    monkeypatch.setattr(ps, "webpush", lambda **kw: kw_seen.update(kw))
    ps.broadcast("t", "b")
    assert kw_seen["timeout"] == ps.PUSH_TIMEOUT_SEC


# #40 / #41 -----------------------------------------------------------
def test_preferences_sanitised(subs_file):
    ps.add_subscription(_sub(1, preferences="x", last_delivery="2999-01-01"))
    stored = json.loads(subs_file.read_text())[0]
    assert "last_delivery" not in stored
    assert stored["preferences"] == ps.DEFAULT_PREFERENCES
    out = ps.update_preferences(stored["endpoint"], {"rain_stop": False, "evil": 1})
    assert out["rain_stop"] is False and "evil" not in out


@pytest.mark.parametrize(
    "endpoint,ok",
    [
        ("https://fcm.googleapis.com/fcm/send/a", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/a", True),
        ("https://wns2.notify.windows.com/x", True),
        ("https://web.push.apple.com/x", True),
        ("https://internal.flycast/x", False),
        ("https://fcm.googleapis.com.evil.com/x", False),
        ("https://x@evil.com/x", False),
        ("https://evilfcm.googleapis.com@evil.com/", False),
    ],
)
def test_endpoint_allowlist(endpoint, ok):
    sub = {"endpoint": endpoint, "keys": {"p256dh": P256DH, "auth": AUTH}}
    assert ps.validate_subscription(sub) is ok


def test_subscribe_rejects_unknown_host(subs_file):
    c = TestClient(main.app)
    r = c.post("/subscribe", json={**_sub(1), "endpoint": "https://evil.example/x"})
    assert r.status_code == 400


# #33 / #31 -----------------------------------------------------------
def test_latlon_validation():
    c = TestClient(main.app)
    assert c.get("/is_it_raining.json?lat=999&lon=0").status_code == 422
    assert c.get("/is_it_raining.json?lat=10").status_code == 422
