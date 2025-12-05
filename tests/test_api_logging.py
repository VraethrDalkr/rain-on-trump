"""
tests/test_api_logging.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Validate `api_logging.logged_request()` behaviour for the three main
branches: HTTP 200, HTTP 404, HTTP 500.

We inject a *toy* client object whose `.get()` returns a pre-canned
``httpx.Response`` (tied to a dummy `httpx.Request`) so that
`raise_for_status()` raises the correct exception type.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.api_logging import logged_request, logged_request_async


class _ToyClient:
    """Minimal stand-in for ``httpx.Client`` or ``httpx.AsyncClient``."""

    def __init__(self, response: httpx.Response) -> None:
        self._resp = response

    # pylint: disable=unused-argument
    def get(self, url: str, *a: Any, **k: Any) -> httpx.Response:
        return self._resp


@pytest.mark.parametrize(
    "status, expect_raise, expect_level",
    [
        (200, False, logging.INFO),
        (404, False, logging.INFO),
        (500, True, logging.WARNING),
    ],
)
def test_logged_request_levels(
    caplog: pytest.LogCaptureFixture,
    status: int,
    expect_raise: bool,
    expect_level: int,
) -> None:
    """
    * 200  → INFO, no exception.
    * 404  → INFO, no exception (normal cache miss).
    * 500+ → WARNING and *raises* by default.
    """
    caplog.set_level(logging.DEBUG, logger="extapi")

    # Build a dummy httpx.Request so that Response.raise_for_status() works.
    dummy_req = httpx.Request("GET", "https://x.test/foo")
    resp = httpx.Response(status_code=status, content=b"{}", request=dummy_req)
    toy = _ToyClient(resp)

    if expect_raise:
        with pytest.raises(httpx.HTTPStatusError):
            logged_request(toy, "get", "https://x.test/foo")
    else:
        logged_request(toy, "get", "https://x.test/foo")

    # exactly one log record should have been emitted
    (rec,) = caplog.records
    assert rec.levelno == expect_level


class _ToyAsyncClient:
    """Minimal async stand-in for ``httpx.AsyncClient``."""

    def __init__(self, response: httpx.Response) -> None:
        self._resp = response

    async def get(self, url: str, *a: Any, **k: Any) -> httpx.Response:
        return self._resp


@pytest.mark.parametrize(
    "status, expect_raise, expect_level",
    [
        (200, False, logging.INFO),
        (404, False, logging.INFO),
        (500, True, logging.WARNING),
    ],
)
async def test_logged_request_async_levels(
    caplog: pytest.LogCaptureFixture,
    status: int,
    expect_raise: bool,
    expect_level: int,
) -> None:
    """
    Async version: same behavior as sync logged_request.

    * 200  → INFO, no exception.
    * 404  → INFO, no exception (normal cache miss).
    * 500+ → WARNING and *raises* by default.
    """
    caplog.set_level(logging.DEBUG, logger="extapi")

    dummy_req = httpx.Request("GET", "https://x.test/foo")
    resp = httpx.Response(status_code=status, content=b"{}", request=dummy_req)
    toy = _ToyAsyncClient(resp)

    if expect_raise:
        with pytest.raises(httpx.HTTPStatusError):
            await logged_request_async(toy, "get", "https://x.test/foo")
    else:
        await logged_request_async(toy, "get", "https://x.test/foo")

    # exactly one log record should have been emitted
    (rec,) = caplog.records
    assert rec.levelno == expect_level
