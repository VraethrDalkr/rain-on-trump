"""
weather_service.py
~~~~~~~~~~~~~~~~~~
Fetch current precipitation at a given lat/lon (Open-Meteo)
and return both rain and snow data with precipitation type.

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
            "sunrise": str | None,      # ISO8601 local time
            "sunset": str | None,       # ISO8601 local time
        }
        or (result, trace) when *trace* list supplied.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from .constants import USER_AGENT

# ── Tiny per-argument TTL cache ──────────────────────────────────────────
_cached: dict[str, tuple[dt.datetime, Any]] = {}


def memo(seconds: int = 600):
    """Per-argument TTL cache that ignores the debug *trace* kwarg."""

    def deco(fn):
        cache: dict[tuple, tuple[dt.datetime, object]] = {}

        async def wrapped(*args, **kwargs):
            key_kwargs = tuple(
                sorted((k, repr(v)) for k, v in kwargs.items() if k != "trace")
            )
            key = (args, key_kwargs)

            now = dt.datetime.now(dt.timezone.utc)
            ts, val = cache.get(
                key,
                (dt.datetime.min.replace(tzinfo=dt.timezone.utc), None),
            )
            if (now - ts).total_seconds() < seconds:
                return val
            val = await fn(*args, **kwargs)
            cache[key] = (now, val)
            return val

        wrapped.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapped

    return deco


# ── Public helper ───────────────────────────────────────────────────────
@memo(300)
async def get_precip(
    lat: float,
    lon: float,
    *,
    trace: list[dict] | None = None,
) -> dict | tuple[dict, list[dict]]:
    """
    Return precipitation data including rain and snow.

    Args:
        lat, lon: Decimal degrees.
        trace:    Optional list that collects diagnostic steps.

    Returns:
        {
            "precipitating": bool,        # true if ANY precipitation (rain or snow)
            "rain": float,                # mm/h of rain
            "snowing": bool,              # true if snow > 0
            "snow": float,                # mm/h of snowfall
            "precipitation_type": str     # "rain", "snow", "both", or "none"
        }
        •or• (result, trace) when *trace* arg supplied.
    """
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    trace = trace if trace is not None else []
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
        "&daily=sunrise,sunset"
        "&timezone=auto"
    )
    trace.append({"ts": ts, "phase": "weather", "step": "fetch", "url": url})

    async with httpx.AsyncClient(
        timeout=10, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = await client.get(url)
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
        return (result, trace) if trace is not None else result

    # Parse successful response
    try:
        data = resp.json()
    except Exception as exc:
        reason = f"Failed to parse JSON response: {exc}"
        trace.append({"ts": ts, "phase": "weather", "step": "error", "reason": reason})
        result = {"error": True, "reason": reason, "precipitating": None}
        return (result, trace) if trace is not None else result

    # Get timezone offset from API response (returns local time with timezone=auto)
    utc_offset_seconds = data.get("utc_offset_seconds", 0)
    local_tz = dt.timezone(dt.timedelta(seconds=utc_offset_seconds))

    # Round current time down to the top of the current LOCAL hour,
    # because Open-Meteo returns one value per hour in local time.
    now_local = dt.datetime.now(dt.timezone.utc).astimezone(local_tz)
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
    except (KeyError, ValueError, IndexError) as exc:
        reason = f"Malformed API response: missing or invalid data ({exc})"
        trace.append({"ts": ts, "phase": "weather", "step": "error", "reason": reason})
        result = {"error": True, "reason": reason, "precipitating": None}
        return (result, trace) if trace is not None else result

    # Extract sunrise/sunset from daily data (returns today's values)
    try:
        daily = data.get("daily", {})
        sunrise = daily.get("sunrise", [None])[0]
        sunset = daily.get("sunset", [None])[0]
    except (KeyError, IndexError):
        sunrise = None
        sunset = None

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
    }
    return (result, trace) if trace is not None else result
