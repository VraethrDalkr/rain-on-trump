"""
weather_service.py
~~~~~~~~~~~~~~~~~~
Fetch current precipitation at a given lat/lon (Open-Meteo)
and return both rain and snow data with precipitation type.

"Raining" means rain in any of the last four completed 15-minute steps
(equivalently: wet from the first wet quarter-hour until 60 dry minutes in a
row). A 90-day study at the White House, Mar-a-Lago and Bedminster
(an internal threshold study, 2026-11-04) found the old rule, the
previous full hour's total, lagged by up to two hours and missed 20-30 h of
rain per quarter; this rule missed none at a similar notification rate.
``rain``/``snow`` are the totals over that last hour (mm, i.e. mm/h). If
15-minute data is unavailable the hourly value is used as before.

Public helper
-------------
    get_precip(lat, lon, trace=None) ->
        {
            "precipitating": bool,
            "rain": float,
            "snowing": bool,
            "snow": float,
            "precipitation_type": str,  # "rain", "snow", "both", or "none"
            "weather_code": int,        # WMO weather code
            "thunderstorm": bool,       # True if code 95-99
            "thunderstorm_state": str,  # "none", "moderate", or "severe"
            "sunrise": str | None,      # ISO8601, naive local time
            "sunset": str | None,       # ISO8601, naive local time
            "utc_offset_seconds": int,  # the location's UTC offset
            "sunrise_utc": str | None,  # ISO8601 UTC with "Z"
            "sunset_utc": str | None,   # ISO8601 UTC with "Z"
        }
        The bare dict is returned unless a *trace* list is supplied, in which
        case it returns ``(result, trace)``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from .constants import USER_AGENT

#: "Raining" = precipitation in any of the last N completed 15-min steps
PRECIP_WINDOW_STEPS: int = 4

#: Upper bound on memoised entries (the key space includes user-supplied coords)
MEMO_MAX_ENTRIES: int = 512


def _utcnow() -> dt.datetime:
    """Current UTC time (single seam so tests can freeze the clock)."""
    return dt.datetime.now(dt.timezone.utc)


# ── Tiny per-argument TTL cache ──────────────────────────────────────────
def _key_part(value: Any) -> Any:
    """Round floats (coordinates) to 2 decimals so the key space stays small."""
    return round(value, 2) if isinstance(value, float) else value


def _is_error(val: Any) -> bool:
    """True for an error-shaped result, bare or wrapped as ``(result, trace)``."""
    res = val[0] if isinstance(val, tuple) else val
    return isinstance(res, dict) and bool(res.get("error"))


def memo(seconds: int = 600):
    """Per-argument TTL cache that ignores the debug *trace* kwarg.

    Error results are never cached, coordinates are rounded in the key, and
    expired entries are evicted on insert so the dict cannot grow forever.
    """

    def deco(fn):
        cache: dict[tuple, tuple[dt.datetime, object]] = {}

        async def wrapped(*args, **kwargs):
            key_kwargs = tuple(
                sorted(
                    (k, repr(_key_part(v))) for k, v in kwargs.items() if k != "trace"
                )
            )
            key = (tuple(_key_part(a) for a in args), key_kwargs)

            now = dt.datetime.now(dt.timezone.utc)
            ts, val = cache.get(
                key,
                (dt.datetime.min.replace(tzinfo=dt.timezone.utc), None),
            )
            if (now - ts).total_seconds() < seconds:
                return val
            val = await fn(*args, **kwargs)
            if not _is_error(val):
                done = dt.datetime.now(dt.timezone.utc)
                for k in [
                    k
                    for k, (t, _) in cache.items()
                    if (done - t).total_seconds() >= seconds
                ]:
                    del cache[k]
                while len(cache) >= MEMO_MAX_ENTRIES:
                    del cache[min(cache, key=lambda k: cache[k][0])]
                cache[key] = (done, val)
            return val

        wrapped.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapped

    return deco


# ── Public helper ───────────────────────────────────────────────────────
def _recent_15min(data: dict, now_local: dt.datetime) -> tuple[float, float] | None:
    """Rain and snow totals over the last completed 15-min steps.

    Each ``minutely_15`` value is the sum over the preceding 15 minutes, so a
    step is complete once its timestamp is not in the future.

    Returns:
        (rain, snow) summed over the last :data:`PRECIP_WINDOW_STEPS` completed
        steps, or None if the response has no usable 15-minute data.
    """
    try:
        m = data["minutely_15"]
        now_key = now_local.strftime("%Y-%m-%dT%H:%M")
        done = [
            (r, sn)
            for t, r, sn in zip(m["time"], m["rain"], m["snowfall"])
            if t <= now_key
        ][-PRECIP_WINDOW_STEPS:]
    except (KeyError, TypeError):
        return None
    if len(done) < PRECIP_WINDOW_STEPS or any(r is None for r, _ in done):
        return None
    rain = round(sum(float(r) for r, _ in done), 2)
    snow = round(sum(float(sn or 0) for _, sn in done), 2)
    return rain, snow


def _utc_iso(local_iso: str | None, offset_s: int) -> str | None:
    """Convert Open-Meteo's naive local ISO time to ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not local_iso:
        return None
    try:
        naive = dt.datetime.fromisoformat(local_iso)
    except (TypeError, ValueError):
        return None
    return (naive - dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_precip(
    lat: float,
    lon: float,
    *,
    trace: list[dict] | None = None,
) -> dict | tuple[dict, list[dict]]:
    """
    Return precipitation data including rain and snow (TTL cached).

    Args:
        lat, lon: Decimal degrees.
        trace:    Optional list that collects diagnostic steps.

    Returns:
        The result dict (see module docstring), or ``(result, trace)`` when the
        caller supplied a *trace* list.
    """
    result, own_trace = await _fetch_precip(lat, lon)
    if trace is None:
        return result
    trace.extend(own_trace)
    return result, trace


@memo(300)
async def _fetch_precip(lat: float, lon: float) -> tuple[dict, list[dict]]:
    """Fetch and parse Open-Meteo; always returns ``(result, trace)``."""
    ts = _utcnow().isoformat()
    trace: list[dict] = []
    trace.append(
        {
            "ts": ts,
            "phase": "weather",
            "step": "start",
            "coords": {"lat": lat, "lon": lon},
        }
    )

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=rain,snowfall,weather_code"
        "&minutely_15=rain,snowfall"
        f"&past_minutely_15={PRECIP_WINDOW_STEPS + 4}&forecast_minutely_15=1"
        "&daily=sunrise,sunset"
        "&timezone=auto"
    )
    trace.append({"ts": ts, "phase": "weather", "step": "fetch", "url": url})

    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        reason = f"Weather request failed: {exc!r}"
        trace.append({"ts": ts, "phase": "weather", "step": "error", "reason": reason})
        result = {"error": True, "reason": reason, "precipitating": None}
        return result, trace
    trace.append(
        {
            "ts": ts,
            "phase": "weather",
            "step": "status",
            "status": resp.status_code,
        }
    )

    # Check HTTP status before parsing JSON
    if resp.status_code != 200:
        try:
            error_data = resp.json()
            reason = error_data.get("reason", f"HTTP {resp.status_code} error")
        except Exception:
            reason = f"HTTP {resp.status_code} error (unable to parse response)"

        trace.append({"ts": ts, "phase": "weather", "step": "error", "reason": reason})
        result = {"error": True, "reason": reason, "precipitating": None}
        return result, trace

    # Parse successful response
    try:
        data = resp.json()
    except Exception as exc:
        reason = f"Failed to parse JSON response: {exc}"
        trace.append({"ts": ts, "phase": "weather", "step": "error", "reason": reason})
        result = {"error": True, "reason": reason, "precipitating": None}
        return result, trace

    # Get timezone offset from API response (returns local time with timezone=auto)
    try:
        utc_offset_seconds = int(data.get("utc_offset_seconds") or 0)
    except (TypeError, ValueError):
        utc_offset_seconds = 0
    local_tz = dt.timezone(dt.timedelta(seconds=utc_offset_seconds))

    # Round current time down to the top of the current LOCAL hour,
    # because Open-Meteo returns one value per hour in local time.
    now_local = _utcnow().astimezone(local_tz)
    now_hour = now_local.replace(minute=0, second=0, microsecond=0)
    lookup = now_hour.strftime("%Y-%m-%dT%H:%M")  # e.g. "2025-05-31T17:00"

    # Extract precipitation data with proper error handling
    try:
        times = data["hourly"]["time"]
        rains = data["hourly"]["rain"]
        snows = data["hourly"]["snowfall"]
        weather_codes = data["hourly"]["weather_code"]
        idx = times.index(lookup)
        rain = float(rains[idx])
        snow = float(snows[idx])
        weather_code = int(weather_codes[idx])
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        reason = f"Malformed API response: missing or invalid data ({exc})"
        trace.append({"ts": ts, "phase": "weather", "step": "error", "reason": reason})
        result = {"error": True, "reason": reason, "precipitating": None}
        return result, trace

    recent = _recent_15min(data, now_local)
    if recent is not None:
        rain, snow = recent
        trace.append(
            {"ts": ts, "phase": "weather", "step": "minutely_15", "window_mm": recent}
        )

    # Extract sunrise/sunset from daily data (returns today's values)
    try:
        daily = data.get("daily", {})
        sunrise = daily.get("sunrise", [None])[0]
        sunset = daily.get("sunset", [None])[0]
    except (KeyError, IndexError):
        sunrise = None
        sunset = None

    sunrise_utc = _utc_iso(sunrise, utc_offset_seconds)
    sunset_utc = _utc_iso(sunset, utc_offset_seconds)

    # Determine thunderstorm state from weather code
    # WMO codes: 95 = moderate thunderstorm, 96/97/99 = severe (with hail)
    if weather_code in (96, 97, 99):
        thunderstorm_state = "severe"
        thunderstorm = True
    elif weather_code == 95:
        thunderstorm_state = "moderate"
        thunderstorm = True
    else:
        thunderstorm_state = "none"
        thunderstorm = False

    raining = rain > 0.0
    snowing = snow > 0.0

    # Determine precipitation type
    if raining and snowing:
        precip_type = "both"
    elif raining:
        precip_type = "rain"
    elif snowing:
        precip_type = "snow"
    else:
        precip_type = "none"

    trace.append(
        {
            "ts": ts,
            "phase": "weather",
            "step": "result",
            "precipitating": raining or snowing,
            "rain": rain,
            "snowing": snowing,
            "snow": snow,
            "precipitation_type": precip_type,
            "weather_code": weather_code,
            "thunderstorm": thunderstorm,
            "thunderstorm_state": thunderstorm_state,
            "sunrise": sunrise,
            "sunset": sunset,
            "utc_offset_seconds": utc_offset_seconds,
        }
    )

    result = {
        "precipitating": raining or snowing,  # true if ANY precipitation (rain or snow)
        "rain": rain,
        "snowing": snowing,
        "snow": snow,
        "precipitation_type": precip_type,
        "weather_code": weather_code,
        "thunderstorm": thunderstorm,
        "thunderstorm_state": thunderstorm_state,
        "sunrise": sunrise,
        "sunset": sunset,
        "utc_offset_seconds": utc_offset_seconds,
        "sunrise_utc": sunrise_utc,
        "sunset_utc": sunset_utc,
    }
    return result, trace


get_precip.cache_clear = _fetch_precip.cache_clear  # type: ignore[attr-defined]
