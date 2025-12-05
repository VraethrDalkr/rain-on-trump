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


def test_adsbfi_fresh_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh <40 min snapshot ⇒ expect PlaneState dict with correct keys."""
    now = dt.datetime.now(UTC).timestamp()
    payload = {
        "aircraft": {
            "seen_pos": now - 60,  # 1 min ago (fresh)
            "lat": 50.0,
            "lon": -75.0,
            "alt_baro": 1500,
            "ground": False,
        }
    }
    # Reset memo-cache
    monkeypatch.setattr(svc, "_cache", {}, raising=False)

    # Provide exactly one fresh snapshot. After that, DummyClient returns 404.
    monkeypatch.setattr(
        svc.httpx,
        "Client",
        lambda **_: _DummyClient([_json_resp(payload)]),
        raising=True,
    )

    plane = svc.get_plane_state_adsb()
    assert plane is not None
    assert plane["status"] == "airborne"
    assert pytest.approx(50.0) == plane["lat"]
    assert pytest.approx(-75.0) == plane["lon"]
