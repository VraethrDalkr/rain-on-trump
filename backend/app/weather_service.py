"""
Very tiny wrapper around Open‑Meteo current‑weather endpoint.
Returns **True** when it’s raining *now* at the given coordinates.
Results are cached per‑bucket for 5 minutes to avoid API spam.
"""
from __future__ import annotations

import functools
from datetime import datetime, timezone
from typing import Dict, Tuple

import httpx

# ── cache -------------------------------------------------------------------
TTL = 300  # seconds
ZERO = datetime.min.replace(tzinfo=timezone.utc)  # tz‑aware sentinel
_cache: Dict[Tuple[int, int], Tuple[datetime, bool]] = {}


def _bucket(lat: float, lon: float, r: int = 2) -> Tuple[int, int]:
    """Round coords so nearby points share a cache slot."""
    return round(lat, r), round(lon, r)


async def get_precip(lat: float, lon: float) -> bool:
    """Return **True** if Open‑Meteo says it’s raining at *lat,lon* right now."""
    now = datetime.now(timezone.utc)
    key = _bucket(lat, lon)

    ts, cached = _cache.get(key, (ZERO, None))
    if (now - ts).total_seconds() < TTL:
        return cached  # type: ignore[return-value]

    api = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current=rain,weathercode"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            cur = (await cli.get(api)).json()["current"]

        # 1️⃣  instantaneous rain‑rate in mm/h
        raining = cur.get("rain", 0.0) > 0.0

        # 2️⃣  fallback to WMO weather codes
        if not raining:
            code = int(cur.get("weathercode", 0))
            raining = 61 <= code <= 67 or 80 <= code <= 82  # drizzle & showers

    except Exception as exc:
        print("[weather] Open‑Meteo error:", exc)
        raining = False

    _cache[key] = (now, raining)
    return raining
