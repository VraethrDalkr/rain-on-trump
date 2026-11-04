"""
tests/test_location_fusion_units.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for the pieces around the claim-based location fusion: per-airframe
flight history (and its migration), the persistent geocode cache, fuzzy place
aliases, the overnight-base fix, and the fusion's stay curve.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app import arrival_cache as cache
from app import calendar_service as cal
from app import location_fusion as fusion
from app import location_service as loc
from app.place_aliases import find_alias

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
PBI = (26.6832, -80.0956)
JBA = (38.81083, -76.86694)


# ── flight history ────────────────────────────────────────────────────────
def test_history_is_kept_per_airframe() -> None:
    cache.save(*PBI, NOW, tail="82-8000")
    cache.record_airborne(
        39.2, -76.5, 4000, 1000, NOW + dt.timedelta(hours=1), "92-9000"
    )

    hist = cache.airframes()
    assert hist["82-8000"]["landing"]["lat"] == pytest.approx(PBI[0])
    assert hist["92-9000"]["airborne"]["altitude_ft"] == 4000
    # The backup's takeoff does not cancel the other airframe's landing
    assert cache.current_landing(NOW + dt.timedelta(hours=2))["tail"] == "82-8000"


def test_747_8_addresses_share_one_airframe() -> None:
    cache.save(*PBI, NOW, tail="25-3300")
    cache.record_airborne(27.0, -80.0, 9000, 2000, NOW + dt.timedelta(days=1), "N7478D")

    rec = cache.airframes()["vc25b-bridge"]
    assert rec["airborne"]["tail"] == "N7478D"
    assert rec["takeoff"]["lat"] == pytest.approx(PBI[0])  # took off where it landed
    assert cache.current_landing(NOW + dt.timedelta(days=1, hours=1)) is None


def test_history_file_is_versioned_and_world_readable() -> None:
    cache.save(*PBI, NOW, tail="82-8000")
    path = cache.history_file()
    assert json.loads(path.read_text())["version"] == 2
    assert path.stat().st_mode & 0o777 == 0o644


def test_legacy_files_are_migrated_once() -> None:
    cache.FILE.write_text(
        json.dumps({"lat": PBI[0], "lon": PBI[1], "ts": NOW.isoformat(), "tail": None})
    )
    later = (NOW + dt.timedelta(hours=1)).isoformat()
    cache.FILE.with_name("last_airborne.json").write_text(
        json.dumps({"lat": 30.0, "lon": -80.0, "ts": later, "altitude_ft": 9000})
    )
    hist = cache.airframes()
    # A newer unattributed airborne record still supersedes the landing
    assert set(hist) == {cache.UNKNOWN_AIRFRAME}
    assert cache.current_landing(NOW + dt.timedelta(hours=2)) is None


def test_legacy_file_is_written_for_the_primary_airframe_only() -> None:
    cache.set_primary_airframe("82-8000")
    cache.save(*JBA, NOW, tail="92-9000")
    assert not cache.FILE.exists()
    cache.save(*PBI, NOW, tail="82-8000")
    assert cache.load()["lat"] == pytest.approx(PBI[0])


def test_inference_skips_airframes_seen_in_this_poll() -> None:
    seen = NOW - dt.timedelta(minutes=8)
    cache.record_airborne(35.4, -82.5, 1800, -900, seen, "82-8000")
    assert cache.maybe_infer_landing(NOW, skip={"82-8000"}) is False
    assert cache.maybe_infer_landing(NOW) is True


# ── geocode cache ─────────────────────────────────────────────────────────
def test_geocode_cache_survives_a_restart(monkeypatch) -> None:
    calls: list[dict] = []

    class _Loc:
        latitude, longitude, address = 40.0, -75.0, "Somewhere, PA"
        raw = {"importance": 0.6, "address": {"country_code": "us"}}

    def _raw(query, **kw):
        calls.append(kw)
        return [_Loc()]

    monkeypatch.setattr(loc, "_geocode_raw", _raw)
    monkeypatch.setattr(loc, "add_geocode_entry", lambda **k: None)
    assert loc._geocode_candidates("Somewhere Hall")[0]["lat"] == 40.0
    assert len(calls) == 2  # worldwide + US
    loc._geocode_cache.clear()
    monkeypatch.setattr(loc, "_geocode_cache_path", None)  # simulate a restart
    assert loc.cached_candidates("Somewhere Hall")[0]["lon"] == -75.0
    loc._geocode_candidates("Somewhere Hall")
    assert len(calls) == 2  # served from the persistent cache


# ── place aliases ─────────────────────────────────────────────────────────
def test_fuzzy_alias_absorbs_feed_typos() -> None:
    assert find_alias("joint base andreews")["name"] == "Joint Base Andrews, MD"
    assert find_alias("zzz qqq") is None


def test_renamed_pbi_is_aliased() -> None:
    hit = find_alias(
        loc._clean(
            "President Donald J. Trump International Airport, West Palm Beach, FL"
        )
    )
    assert (hit["lat"], hit["lon"]) == PBI


# ── overnight base ────────────────────────────────────────────────────────
def _ev(utc: dt.datetime, location: str) -> dict:
    return {"dtstart_utc": utc, "summary": "x", "location": location}


def test_overnight_needs_the_newest_evening_event_at_a_base() -> None:
    """Bug A: an evening rally elsewhere after an evening event at the White
    House is not an overnight at the White House."""
    now = dt.datetime(2026, 10, 29, 1, 3, tzinfo=UTC)  # 21:03 ET
    events = [
        _ev(dt.datetime(2026, 10, 28, 21, 30, tzinfo=UTC), "Joint Base Andrews"),
        _ev(dt.datetime(2026, 10, 28, 23, 10, tzinfo=UTC), "Shoemaker Center, OH"),
        _ev(dt.datetime(2026, 10, 29, 12, 0, tzinfo=UTC), "The White House"),
    ]
    assert cal.get_overnight_base(now, events=events) is None
    events[1]["location"] = "The White House"
    res = cal.get_overnight_base(now, events=events)
    assert res["region"] == "dc" and res["evening_ts"] == events[1]["dtstart_utc"]


# ── fusion helpers ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "age_h, expected",
    [(0, 1.0), (3, 1.0), (24, 0.65), (72, 0.47), (168, 0.2), (169, 0.0)],
)
def test_stay_curve(age_h: float, expected: float) -> None:
    assert fusion.stay(age_h) == pytest.approx(expected)


def test_departure_parsing_accepts_feed_phrasings() -> None:
    for summary, dest in [
        ("The President departs Joint Base Andrews for McGhee Tyson Airport", 1),
        ("TBD: The President departs the White House en route Knoxville", 1),
        ("The President departs Geneva Airport en route to Evian [4:18 PM Local]", 1),
        ("The President depart Asheville Regional Airport en route to LAX", 1),
    ]:
        m = fusion.DEPARTS_RE.search(summary)
        assert m and m.group("dest") and "[" not in m.group("dest"), summary


# ── review batch 2: presence, generic places, perf, Nominatim load ────────
@pytest.mark.parametrize(
    "summary, expected",
    [
        ("White House Press Office: Travel/Photo lid called", "none"),
        ("White House Press Office: Lunch lid until 1:15 PM", "none"),
        ("CANCELLED: The President holds a political rally", "none"),
        ("The First Lady hosts a reading event", "none"),
        ("The Vice President meets with governors", "none"),
        ("The First Lady and The President greet the Prime Minister", "full"),
        ("In-Town Pool Call Time", "weak"),
        ("Out-of-Town Travel Pool Call Time", "weak"),
        ("The President conducts a phone interview with Fox News", "weak"),
        ("The President participates in a Tele-Rally", "weak"),
        ("The President arrives at Mar-a-Lago", "full"),
    ],
)
def test_presence(summary: str, expected: str) -> None:
    assert fusion.presence(summary) == expected


@pytest.mark.parametrize(
    "text", ["his golf club", "Landing Zone", "the airport", "His Residence"]
)
def test_generic_places_never_resolve_or_geocode(text: str) -> None:
    cands = {loc._clean(text): [{"lat": 1.0, "lon": 2.0, "name": text}]}
    assert fusion.resolve_place(text, cands) is None
    assert loc._should_skip_geocode(text)


def test_resolve_place_uses_context() -> None:
    t = dt.datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
    cands = {
        "aberdeen, scotland": [
            {"lat": 45.4647, "lon": -98.4865, "name": "Aberdeen, SD"},
            {"lat": 57.1497, "lon": -2.0943, "name": "Aberdeen, Scotland"},
        ]
    }
    assert fusion.resolve_place("Aberdeen, Scotland", cands)[0] == 45.4647
    context = [(55.3272, -4.8364, t)]  # Turnberry
    assert fusion.resolve_place("Aberdeen, Scotland", cands, context, t)[0] == (57.1497)


def test_nominatim_identifies_the_app_and_failures_wait_hours() -> None:
    from app.constants import USER_AGENT

    assert loc._nominatim.headers["User-Agent"] == USER_AGENT
    assert loc.GEOCODE_NEG_TTL_S >= 12 * 3600


def test_events_between_indexes_once_per_feed() -> None:
    base = dt.datetime(2026, 11, 1, tzinfo=UTC)
    feed = [{"dtstart_utc": base + dt.timedelta(hours=h)} for h in (5, 1, 3)]
    got = cal.events_between(feed, base + dt.timedelta(hours=2), base)
    assert got == []
    got = cal.events_between(
        feed, base + dt.timedelta(hours=1), base + dt.timedelta(hours=3)
    )
    assert [e["dtstart_utc"].hour for e in got] == [1, 3]
    assert cal._index[0] is feed


async def test_requests_read_history_and_reuse_the_decision(monkeypatch) -> None:
    """Requests never write flight history (the background loop does), and a
    decision is reused while its inputs are unchanged."""
    state = {
        "callsign": "82-8000",
        "lat": PBI[0],
        "lon": PBI[1],
        "status": "grounded",
        "ts": dt.datetime.now(UTC),
        "altitude": 0.0,
    }
    snap = {"states": [state], "errors": []}
    events: list[dict] = []
    monkeypatch.setattr(loc, "get_fleet_states", lambda: snap)
    monkeypatch.setattr(loc.cal, "get_events", lambda: events)
    calls = []
    real_fuse = fusion.fuse
    monkeypatch.setattr(
        fusion, "fuse", lambda *a, **k: calls.append(1) or real_fuse(*a, **k)
    )
    loc._decision_memo.clear()

    first = await loc.current_coords()
    second = await loc.current_coords()
    assert first == second and len(calls) == 1
    assert cache.airframes() == {}  # nothing recorded by requests
    await loc.current_coords(trace=[])  # debug always recomputes
    assert len(calls) == 2

    loc.poll_flight_history()  # background loop
    assert "82-8000" in cache.airframes()
    await loc.current_coords()  # history changed → recomputed
    assert len(calls) == 3


def test_prefetch_refreshes_the_feed_before_it_expires(monkeypatch) -> None:
    downloads = []

    def _download():
        downloads.append(1)
        return [{"n": len(downloads)}]

    monkeypatch.setattr(cal._download_events, "__wrapped__", _download)
    cal._download_events.cache_clear()
    cal.prefetch_events()  # empty cache → download
    assert len(downloads) == 1
    cal.prefetch_events()  # fresh → nothing
    assert len(downloads) == 1
    monkeypatch.setattr(cal, "CACHE_SEC", 60)  # now within the refresh margin
    cal.prefetch_events()
    assert len(downloads) == 2
    cal._download_events.cache_clear()


def test_stakeout_location_is_the_white_house() -> None:
    hit = find_alias("stakeout location")
    assert (hit["lat"], hit["lon"]) == (38.897676, -77.036529)
    assert find_alias("cincinnati/northern kentucky international airport")
