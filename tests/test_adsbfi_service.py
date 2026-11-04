"""
tests/test_adsbfi_service.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Exercise the *adsb.fi* fallback helper with controlled HTTP responses:

1. 404  ⇒ return ``None``.
2. 200/old snapshot  ⇒ return ``None``.
3. 200/fresh snapshot ⇒ full PlaneState dict.

We monkey-patch ``httpx.Client`` with a dummy context-manager class so the
production code remains untouched. If the canned response list runs out,
we treat it as a 404 (aircraft not currently visible).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from dateutil import tz

from app import adsbfi_service as svc

UTC = tz.UTC


class _DummyClient:
    """
    Context-manager replacement for httpx.Client.

    It holds a list of pre-canned httpx.Response objects. Once that list is
    empty, any further .get() calls return a 404 Response automatically.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)

    def __enter__(self) -> "_DummyClient":  # noqa: D401
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False  # propagate exceptions

    def get(self, url: str, *a: Any, **k: Any) -> httpx.Response:
        """
        Pop the next canned response if available, otherwise return 404.
        This way, looping over all fleet call-signs never IndexErrors.
        """
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(status_code=404)


def _json_resp(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    """
    Helper → build httpx.Response from JSON payload dict, tied to a dummy request
    so raise_for_status() works if needed.
    """
    dummy_req = httpx.Request("GET", "https://x.test/json")
    return httpx.Response(
        status_code=status,
        json=payload,
        headers={"Content-Type": "application/json"},
        request=dummy_req,
    )


@pytest.mark.parametrize(
    "responses, expect_none",
    [
        # 1️⃣ 404 first (and then all others default to 404) → None
        (
            [
                httpx.Response(
                    status_code=404, request=httpx.Request("GET", "https://x.test/404")
                )
            ],
            True,
        ),
        # 2️⃣ 200 but stale snapshot (90-min old) → None
        (
            [
                _json_resp(
                    {
                        "aircraft": {
                            "seen_pos": dt.datetime.now(UTC).timestamp() - 5400,
                            "lat": 0.0,
                            "lon": 0.0,
                            "ground": True,
                        }
                    }
                )
            ],
            True,
        ),
    ],
)
def test_adsbfi_none(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx.Response],
    expect_none: bool,
) -> None:
    """Scenarios where the helper MUST yield ``None``."""
    # Flush memo-cache so we always run the loop
    monkeypatch.setattr(svc, "_cache", {}, raising=False)

    # Patch httpx.Client to our DummyClient
    monkeypatch.setattr(
        svc.httpx, "Client", lambda **_: _DummyClient(responses), raising=True
    )

    assert svc.get_plane_state_adsb() is None


def _patch_client(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(svc, "_cache", {}, raising=False)
    monkeypatch.setattr(
        svc.httpx,
        "Client",
        lambda **_: _DummyClient([_json_resp(payload)]),
        raising=True,
    )


def test_adsbfi_fresh_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh v2 snapshot (relative seen_pos) ⇒ PlaneState with correct keys."""
    payload = {
        "now": dt.datetime.now(UTC).timestamp() * 1000,
        "ac": [
            {
                "hex": "adfdf9",
                "seen_pos": 60.0,  # seconds since last position (readsb style)
                "lat": 50.0,
                "lon": -75.0,
                "alt_baro": 1500,
            }
        ],
    }
    _patch_client(monkeypatch, payload)

    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["callsign"] == "92-9000"
    assert plane["status"] == "airborne"
    assert plane["tracker_url"].endswith("icao=adfdf9")
    age = (dt.datetime.now(UTC) - plane["ts"]).total_seconds()
    assert 55 < age < 120


def test_adsbfi_alt_baro_ground_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """alt_baro == "ground" must mean grounded, not a float() crash."""
    payload = {
        "ac": [
            {
                "hex": "aa3410",
                "seen_pos": 5,
                "lat": 26.7,
                "lon": -80.1,
                "alt_baro": "ground",
            }
        ]
    }
    _patch_client(monkeypatch, payload)

    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["callsign"] == "N757AF"
    assert plane["on_ground"] is True
    assert plane["status"] == "grounded"


def test_adsbfi_freshest_aircraft_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """With several fleet aircraft visible, the most recent position wins."""
    payload = {
        "ac": [
            {
                "hex": "adfdf9",
                "seen_pos": 900,
                "lat": 1.0,
                "lon": 1.0,
                "alt_baro": 30000,
            },
            {
                "hex": "AA3410",
                "seen_pos": 10,
                "lat": 2.0,
                "lon": 2.0,
                "alt_baro": 30000,
            },
            {"hex": "123456", "seen_pos": 1, "lat": 3.0, "lon": 3.0, "alt_baro": 30000},
        ]
    }
    _patch_client(monkeypatch, payload)

    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["callsign"] == "N757AF"


def test_adsbfi_legacy_epoch_seen_pos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy v1 shape with an absolute epoch seen_pos still parses."""
    now = dt.datetime.now(UTC).timestamp()
    payload = {
        "aircraft": {
            "hex": "adfdf9",
            "seen_pos": now - 60,
            "lat": 50.0,
            "lon": -75.0,
            "alt_baro": 1500,
        }
    }
    _patch_client(monkeypatch, payload)

    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["callsign"] == "92-9000"


def test_adsbfi_c32a_needs_af1_callsign(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-32A sightings only count when the flight ID is AF1."""
    rec = {"hex": "adfeb8", "seen_pos": 5, "lat": 1.0, "lon": 2.0, "alt_baro": 30000}
    _patch_client(monkeypatch, {"ac": [{**rec, "flight": "SAM0002 "}]})
    assert svc.get_plane_state_adsb() is None

    _patch_client(monkeypatch, {"ac": [{**rec, "flight": "AF1     "}]})
    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["callsign"] == "98-0002"
    assert plane["flight_id"] == "AF1"


def test_real_mlat_record_with_blank_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shape observed live on adsb.lol 2026-11-04 (VC-25A, MLAT, blank flight)."""
    payload = {
        "ac": [
            {
                "hex": "adfdf8",
                "type": "mlat",
                "flight": "        ",
                "r": "82-8000",
                "t": "B742",
                "alt_baro": 31000,
                "gs": 572.0,
                "baro_rate": 32,
                "lat": 40.1,
                "lon": -80.2,
                "seen_pos": 1.2,
            }
        ],
        "now": dt.datetime.now(UTC).timestamp() * 1000,
    }
    _patch_client(monkeypatch, payload)

    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["callsign"] == "82-8000"
    assert plane["status"] == "airborne"
    assert plane["flight_id"] == ""
