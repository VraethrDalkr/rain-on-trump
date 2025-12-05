"""
api_logging.py
~~~~~~~~~~~~~~
Tiny wrapper that prints **one concise log line** per outbound HTTP request
and (optionally) raises for server-side errors.

Usage example
-------------
>>> from .api_logging import logged_request
>>> with httpx.Client() as cli:
...     resp = logged_request(cli, "get", "https://example.org/json",
...                           raise_for_status=False)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

LOG = logging.getLogger("extapi")


def _serialise(obj: Any) -> str:
    """Best-effort JSON serialiser for logging."""
    try:
        return json.dumps(obj)
    except Exception:  # noqa: BLE001
        return str(obj)


def logged_request(
    client: Any,
    method: str,
    url: str,
    *args: Any,
    raise_for_status: bool = True,
    **kwargs: Any,
):
    """
    Issue one HTTP request **and** emit a concise log line.

    Parameters
    ----------
    client:
        ``httpx.Client`` or ``httpx.AsyncClient`` instance.
    method:
        HTTP verb – e.g. ``"get"``, ``"post"`` … **lower-case** or **upper-case**.
    url:
        Absolute URL.
    raise_for_status:
        *True* ⇒ propagate 5xx (and by default 4xx) via
        :pymeth:`httpx.Response.raise_for_status`.
        *False* ⇒ never raise; the caller decides.

    Returns
    -------
    httpx.Response
        Raw response so the caller can inspect status / JSON / headers.

    Notes
    -----
    * **404** responses are logged at *INFO*; they’re normal for
      per-aircraft endpoints when the target isn’t currently in view.
    * **≥500** responses are still logged + re-raised (unless
      ``raise_for_status=False``).
    """
    verb = method.upper()
    t0 = time.perf_counter()
    try:
        response = getattr(client, method)(url, *args, **kwargs)
    except Exception as exc:  # network error before we get a response
        latency_ms = (time.perf_counter() - t0) * 1000.0
        LOG.warning("FAIL %s %s %.0f ms %s", verb, url, latency_ms, exc)
        raise

    latency_ms = (time.perf_counter() - t0) * 1000.0
    code = response.status_code

    if code == 404:
        LOG.info("%s %s → 404 (%.0f ms)", verb, url, latency_ms)
    elif code >= 500:
        LOG.warning("%s %s → %s (%.0f ms)", verb, url, code, latency_ms)
    else:
        LOG.info("%s %s → %s (%.0f ms)", verb, url, code, latency_ms)

    if raise_for_status and code >= 500:
        response.raise_for_status()

    return response


async def logged_request_async(
    client: Any,
    method: str,
    url: str,
    *args: Any,
    raise_for_status: bool = True,
    **kwargs: Any,
):
    """
    Async version of logged_request for httpx.AsyncClient.

    Parameters
    ----------
    client:
        ``httpx.AsyncClient`` instance.
    method:
        HTTP verb – e.g. ``"get"``, ``"post"`` … **lower-case** or **upper-case**.
    url:
        Absolute URL.
    raise_for_status:
        *True* ⇒ propagate 5xx via :pymeth:`httpx.Response.raise_for_status`.
        *False* ⇒ never raise; the caller decides.

    Returns
    -------
    httpx.Response
        Raw response so the caller can inspect status / JSON / headers.
    """
    verb = method.upper()
    t0 = time.perf_counter()
    try:
        response = await getattr(client, method)(url, *args, **kwargs)
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        LOG.warning("FAIL %s %s %.0f ms %s", verb, url, latency_ms, exc)
        raise

    latency_ms = (time.perf_counter() - t0) * 1000.0
    code = response.status_code

    if code == 404:
        LOG.info("%s %s → 404 (%.0f ms)", verb, url, latency_ms)
    elif code >= 500:
        LOG.warning("%s %s → %s (%.0f ms)", verb, url, code, latency_ms)
    else:
        LOG.info("%s %s → %s (%.0f ms)", verb, url, code, latency_ms)

    if raise_for_status and code >= 500:
        response.raise_for_status()

    return response


__all__ = ["logged_request", "logged_request_async"]
