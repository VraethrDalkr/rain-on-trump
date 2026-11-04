"""Regression tests for audit findings #8-#11 (calendar) and #13-#14 (gdelt)."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from dateutil import tz

import app.gdelt_service as gs
from app import calendar_service as cal

UTC = tz.UTC
NOW = dt.datetime(2025, 12, 5, 4, 0, tzinfo=UTC)  # 11PM ET


def _ev(y, m, d, h, loc, **kw):
    return {
        "dtstart_utc": dt.datetime(y, m, d, h, 0, tzinfo=UTC),
        "summary": "x",
        "location": loc,
        **kw,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_overnight_picks_latest_evening_soonest_morning(monkeypatch, reverse):
    """#8: result must not depend on feed order."""
    events = [
        _ev(2025, 12, 4, 22, "Oval Office"),  # 5PM ET, DC (older)
        _ev(
            2025, 12, 5, 1, "Trump National Golf Club Bedminster"
        ),  # 8PM ET, NJ (latest)
        _ev(
            2025, 12, 5, 14, "Trump National Golf Club Bedminster"
        ),  # 9AM ET NJ (soonest)
        _ev(2025, 12, 5, 16, "Oval Office"),  # 11AM ET DC
    ]
    if reverse:
        events.reverse()
    monkeypatch.setattr(cal, "_fetch_events", lambda: events)
    res = cal.get_overnight_base(now=NOW)
    assert res and "Bedminster" in res["name"]


def test_overnight_ignores_untimed_events(monkeypatch):
    """#9: date-only events are not morning anchors."""
    events = [
        _ev(2025, 12, 4, 23, "Oval Office"),
        _ev(2025, 12, 5, 5, "Mar-a-Lago", has_time=False),  # 00:00 ET
        _ev(2025, 12, 5, 15, "Oval Office"),
    ]
    monkeypatch.setattr(cal, "_fetch_events", lambda: events)
    res = cal.get_overnight_base(now=NOW)
    assert res and "White House" in res["name"]


def test_download_marks_untimed_and_skips_malformed(monkeypatch):
    """#9/#10: bad items are skipped, good ones kept, has_time recorded."""
    items = [
        {"date": "2025-12-05", "time": "TBD", "details": "bad"},
        {"time": "10:00:00"},
        None,
        "str",
        {"date": "2025-12-05", "details": "untimed", "location": "Oval Office"},
        {"date": "2025-12-05", "time": "10:00:00", "details": "ok"},
    ]
    resp = SimpleNamespace(text=json.dumps(items), raise_for_status=lambda: None)
    monkeypatch.setattr(cal.httpx, "get", lambda *a, **k: resp)
    cal._fetch_events.cache_clear()
    evs = cal._fetch_events()
    assert [e["summary"] for e in evs] == ["untimed", "ok"]
    assert [e["has_time"] for e in evs] == [False, True]
    cal._fetch_events.cache_clear()


def test_non_list_feed_is_fail_soft(monkeypatch):
    """#10: JSON object body yields [] instead of raising."""
    resp = SimpleNamespace(text='{"error": "x"}', raise_for_status=lambda: None)
    monkeypatch.setattr(cal.httpx, "get", lambda *a, **k: resp)
    cal._fetch_events.cache_clear()
    assert cal._fetch_events() == []
    cal._fetch_events.cache_clear()


def test_resolve_normalizes_fancy_characters():
    """#11: NBSP / non-breaking hyphen resolve like location_service._clean."""
    assert cal._resolve_location_to_coords("The\u00a0White House") is not None
    assert cal._resolve_location_to_coords("Mar\u2013a\u2013Lago") is not None


class _Resp:
    def __init__(self, data=None, bad_json=False):
        self._d, self._bad = data, bad_json

    def raise_for_status(self):
        pass

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._d


def _patch_gdelt(monkeypatch, responses):
    urls = []

    class Cli:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            urls.append(url)
            return responses.pop(0)

    gs._cache.clear()
    monkeypatch.setattr(gs.httpx, "AsyncClient", Cli)
    return urls


@pytest.mark.asyncio
async def test_gdelt_non_json_body_does_not_fall_back(monkeypatch):
    """#13: plain-text 200 body -> None and no 7-day fallback request."""
    stale = _Resp({"articles": [{"spatial": [{"lat": 1, "lon": 2, "location": "x"}]}]})
    urls = _patch_gdelt(monkeypatch, [_Resp(bad_json=True), stale])
    assert await gs.get_latest_location(hours_back=1) is None
    assert len(urls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spatial",
    [
        [{"lat": None, "lon": 1}],
        [{"lat": 1, "lon": 1, "location": None}],  # valid, name empty
        ["junk"],
        [{"lat": 999, "lon": 0}],
        [{"lat": "nan", "lon": 0}],
        [{"lat": 0, "lon": -181}],
    ],
)
async def test_gdelt_bad_spatial_never_raises_or_returns_invalid(monkeypatch, spatial):
    """#14: malformed/out-of-range coords are skipped, no exception."""
    _patch_gdelt(monkeypatch, [_Resp({"articles": [{"spatial": spatial}]})])
    res = await gs.get_latest_location(hours_back=1)
    if res is not None:
        assert -90 <= res["lat"] <= 90 and -180 <= res["lon"] <= 180
        assert res["name"] == "News dateline: "


@pytest.mark.asyncio
async def test_gdelt_non_dict_payload(monkeypatch):
    """#14: list payload yields None."""
    _patch_gdelt(monkeypatch, [_Resp([1, 2])])
    assert await gs.get_latest_location(hours_back=1) is None
