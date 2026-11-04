# backend/app/gdelt_service.py

"""
gdelt_service.py
~~~~~~~~~~~~~~~~
Query the free **GDELT 2.0 Doc API** for very recent US‐based
news articles that mention *Donald Trump*, extracting the first
usable {lat, lon, name} from the article dateline.

Features
--------
- **Hybrid probe**:
  1) Narrow window (last `hours_back` hours via `startdatetime`)
  2) Fallback to a 7-day span (`timespan=7d`) only on network/HTTP errors
- **5 min TTL cache** via `_cache` to avoid duplicate network calls
- **Typed result**: returns a simple `Coords` dict or `None`
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Final, TypedDict

import httpx
from dateutil import tz

from .constants import USER_AGENT

UTC: Final = tz.UTC
LOG = logging.getLogger("gdelt_service")

# In-memory TTL cache: fn_name → (last_call_time_utc, value)
_cache: dict[str, tuple[dt.datetime, Any]] = {}


def _memo(seconds: int = 300):
    """
    Decorator for async TTL caching of the decorated function.

    Args:
        seconds: how long (in seconds) to cache the result.
    """

    def decorator(fn):
        async def wrapper(*args, **kwargs):
            last_ts, val = _cache.get(
                fn.__name__, (dt.datetime.min.replace(tzinfo=UTC), None)
            )
            if (dt.datetime.now(UTC) - last_ts).total_seconds() < seconds:
                return val  # type: ignore[return-value]
            val = await fn(*args, **kwargs)
            _cache[fn.__name__] = (dt.datetime.now(UTC), val)
            return val

        wrapper.__wrapped__ = fn  # preserve introspection
        return wrapper

    return decorator


class Coords(TypedDict, total=False):
    """
    Result shape: a simple coordinate with a human‐readable name, plus the
    article's ``seendate`` (aware UTC datetime) when GDELT reports one.
    """

    lat: float
    lon: float
    name: str
    seendate: dt.datetime


def _seendate(art: dict) -> dt.datetime | None:
    """Parse GDELT's ``seendate`` ("20251205T120000Z"), else None."""
    raw = art.get("seendate")
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class _BadBody(Exception):
    """HTTP succeeded but the body was not valid JSON."""


async def _get_json(url: str) -> Any:
    """GET ``url``; transport/HTTP errors propagate, bad JSON -> _BadBody."""
    async with httpx.AsyncClient(
        timeout=10.0, headers={"User-Agent": USER_AGENT}
    ) as cli:
        resp = await cli.get(url)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise _BadBody(str(exc)) from exc


def _first_dateline(data: Any) -> Coords | None:
    """Return the first article dateline with valid coordinates, else None."""
    if not isinstance(data, dict):
        return None
    articles = data.get("articles")
    if not isinstance(articles, list):
        return None
    for art in articles:
        if not isinstance(art, dict):
            continue
        spatial = art.get("spatial")
        if not isinstance(spatial, list):
            continue
        for loc in spatial:
            try:
                lat = float(loc["lat"])
                lon = float(loc["lon"])
                place = str(loc.get("location") or "").title()
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            if not (
                math.isfinite(lat)
                and math.isfinite(lon)
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                continue
            coords: Coords = {
                "lat": lat,
                "lon": lon,
                "name": f"News dateline: {place}",
            }
            seen = _seendate(art)
            if seen is not None:
                coords["seendate"] = seen
            return coords
    return None


@_memo(300)
async def get_latest_location(hours_back: int = 2) -> Coords | None:
    """
    Fetch the freshest GDELT dateline with spatial coords for “Donald Trump.”

    1) Try a narrow window of the last `hours_back` hours via `startdatetime=…`
    2) On any exception (network error or non-200), fall back to a 7-day span
       via `timespan=7d`

    Args:
        hours_back: how many hours in the past to probe first.

    Returns:
        A Coords dict if a valid lat/lon is found, else None.
    """
    # ─── 1) Narrow probe: last `hours_back` hours ─────────────────────────
    since = (dt.datetime.now(UTC) - dt.timedelta(hours=hours_back)).strftime(
        "%Y%m%d%H%M%S"
    )
    url_narrow = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        "?query=Donald%20Trump"
        "&filter=sourceCountry:US"
        "&mode=ArtList"
        "&format=json"
        "&maxrecords=75"
        f"&startdatetime={since}"
        "&include=locations"
    )
    try:
        data = await _get_json(url_narrow)
    except _BadBody as exc:
        LOG.warning("[GDELT narrow] unusable body: %s", exc)
        return None  # rate-limit / error text: "no data", not a stale fallback
    except Exception as exc:
        LOG.warning("[GDELT narrow] %s", exc)
    else:
        # No articles → no fallback; treat as “no hit”
        return _first_dateline(data)

    # ─── 2) Fallback probe: last 7 days via `timespan=7d` ─────────────────
    url_fallback = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        "?query=Donald%20Trump"
        "&filter=sourceCountry:US"
        "&mode=ArtList"
        "&format=json"
        "&maxrecords=75"
        "&timespan=7d"
        "&include=locations"
    )
    try:
        data = await _get_json(url_fallback)
    except Exception as exc:
        LOG.warning("[GDELT fallback] %s", exc)
        return None
    return _first_dateline(data)
