from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .location_service import current_coords
from .weather_service import get_precip
from . import push_service as push

# ── FastAPI app ─────────────────────────────────────────────────────
app = FastAPI(title="Is It Raining on Trump?")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── API route ───────────────────────────────────────────────────────
@app.get("/is_it_raining.json")
async def is_it_raining(
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
):
    loc = (
        {"lat": lat, "lon": lon, "name": f"({lat:.2f},{lon:.2f})"}
        if lat is not None and lon is not None
        else await current_coords()
    )
    raining = await get_precip(loc["lat"], loc["lon"])
    return {
        "raining":  raining,
        "location": loc["name"],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

# ── Push subscription endpoint ─────────────────────────────────────
@app.post("/subscribe")
async def subscribe(sub: dict):
    push.add_subscription(sub)
    print("[subscriber added]", sub["endpoint"][:50])
    return {"ok": True}

# ── Background rain-flip watcher via lifespan ───────────────────────
async def _rain_watch():
    prev: bool | None = None
    while True:
        loc = await current_coords()
        raining = await get_precip(loc["lat"], loc["lon"])
        if prev is not None and raining != prev:
            verb = "started" if raining else "stopped"
            push.broadcast(
                title="Rain on Trump",
                body=f"It just {verb} raining in {loc['name']}!",
            )
        prev = raining
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(_app: FastAPI):   # leading underscore silences “unused/shadow”
    task = asyncio.create_task(_rain_watch())
    try:
        yield
    finally:
        task.cancel()

app.router.lifespan_context = lifespan    # type: ignore[attr-defined]
