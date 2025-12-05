"""
main.py – FastAPI entry point
=============================

Key points in this revision
---------------------------
* **Robust `/debug.json`** – returns a synthetic trace whenever the plane is
  in flight *or* we still have no lat/lon (unknown location).  This removes
  the `KeyError: 'lat'` you hit during local testing.
* No other routes or helpers changed.
"""

from __future__ import annotations

# ─── Std-lib / third-party ────────────────────────────────────────────
import asyncio
import contextlib
import datetime as dt
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# ─── Project modules ──────────────────────────────────────────────────
from .event_service import emit_machine_started, emit_rain_state_changed
from .flight_service import get_plane_state
from .location_service import (
    _cached as _loc_cache,
    current_coords,
    mark_initialization_complete,
)
from .push_service import (
    add_subscription,
    broadcast,
    cleanup_old_subscriptions,
    get_preferences,
    get_subscription_stats,
    remove_subscription,
    update_preferences,
)
from .snapshot_service import add_snapshot, get_snapshot_stats, get_snapshots
from .geocode_log_service import get_geocode_entries, get_geocode_stats
from .weather_service import _cached as _weather_cache, get_precip

# ─── Logging ──────────────────────────────────────────────────────────
import logging
import sys

LOG_BG = logging.getLogger("bg")
LOG = logging.getLogger("rain_loop")

# Configure custom loggers to output to stdout
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
LOG_BG.addHandler(_handler)
LOG.addHandler(_handler)
LOG_BG.setLevel(logging.INFO)
LOG.setLevel(logging.INFO)

# ---------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------
load_dotenv()
BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN", "")

UTC = dt.timezone.utc

# Rate limiter for subscription endpoint
limiter = Limiter(key_func=get_remote_address)

# Feature flag: Set to True to require 2 consecutive checks before notifying.
# Disabled because: Production logs show no location/weather flapping, and
# hourly weather data makes flapping unlikely. Re-enable if notification spam occurs.
DEBOUNCE_NOTIFICATIONS = False

# Thunderstorm notification configuration
# State transitions: (from_state, to_state) -> (cooldown_seconds, message_template)
# States: "none", "moderate" (WMO code 95), "severe" (codes 96, 97, 99)
THUNDERSTORM_COOLDOWNS: dict[str, tuple[int, str]] = {
    "none->moderate": (1800, "Thunderstorm detected at {location}"),
    "none->severe": (1800, "Severe thunderstorm with hail at {location}!"),
    "moderate->severe": (
        900,
        "Thunderstorm intensifying at {location} - hail possible!",
    ),
    "moderate->none": (1800, "Thunderstorm has passed at {location}"),
    "severe->none": (1800, "Thunderstorm has passed at {location}"),
}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
async def _unwrap(awaitable):
    """Return the first element when the awaited value is ``(data, trace)``."""
    data = await awaitable
    return data[0] if isinstance(data, tuple) else data


def should_notify_state_change(
    history: list[str], prev_notified: str | None, curr_state: str
) -> tuple[bool, list[str]]:
    """
    Determine if a precipitation state change should trigger a notification.

    Uses hysteresis/debouncing to prevent notification spam when weather
    API flaps between states.

    Args:
        history: List of recent precipitation states (max 2).
        prev_notified: Last state that triggered a notification.
        curr_state: Current precipitation state.

    Returns:
        Tuple of (should_notify, new_history):
        - should_notify: True if notification should be sent.
        - new_history: Updated history list (limited to last 2 states).

    Logic:
        - Append current state to history
        - Keep only last 2 states
        - Notify only if:
            1. Current state != last notified state (actual change)
            2. Current state is stable (appears in last 2 observations)
    """
    # Append current state to history
    new_history = history + [curr_state]

    # Keep only last 2 states (sliding window)
    if len(new_history) > 2:
        new_history = new_history[-2:]

    # Need at least 2 observations to confirm stability
    if len(new_history) < 2:
        return (False, new_history)

    # Check if current state is stable (last 2 states match)
    is_stable = new_history[-1] == new_history[-2]

    # Check if current state is different from last notified
    is_different = curr_state != prev_notified

    # Notify only if stable AND different
    should_notify = is_stable and is_different

    return (should_notify, new_history)


def should_suppress_landing_notification(
    was_in_flight: bool, prev_type: str | None, curr_type: str
) -> bool:
    """
    Determine if notification should be suppressed due to landing.

    Problem: When plane lands in rain, users get "It just started raining!"
    even though it was already raining at the destination.

    Solution: Suppress notifications on the first ground check after landing
    when transitioning from in-flight state (prev_type="none").

    Args:
        was_in_flight: True if we were in-flight on the previous check.
        prev_type: Previous precipitation type ("none" during flight).
        curr_type: Current precipitation type at landing location.

    Returns:
        True if notification should be suppressed (just landed scenario).
    """
    # Only suppress if we just landed (transitioning from flight to ground)
    if not was_in_flight:
        return False

    # During flight, prev_type is "none"
    # If we land and there's precipitation, suppress the "just started" notification
    # because we just arrived - we didn't cause the weather change
    if prev_type == "none" and curr_type != "none":
        return True

    return False


def _maybe_send_thunderstorm_notification(
    app: FastAPI, precip: dict, coords: dict
) -> None:
    """
    Send state-change + severity thunderstorm notifications.

    Args:
        app: FastAPI app instance (for state tracking).
        precip: Result from get_precip() (includes thunderstorm_state).
        coords: Current location coordinates.
    """
    curr_state = precip.get("thunderstorm_state", "none")
    prev_state = getattr(app.state, "prev_thunderstorm_state", "none")

    # No state change - nothing to do
    if curr_state == prev_state:
        return

    location = coords.get("name", "Trump's location")
    now = dt.datetime.now(UTC)
    last_notified = getattr(app.state, "thunderstorm_last_notified", {})

    # Determine transition and check if we should notify
    transition = f"{prev_state}->{curr_state}"

    if transition in THUNDERSTORM_COOLDOWNS:
        cooldown, msg_template = THUNDERSTORM_COOLDOWNS[transition]
        last = last_notified.get(transition)
        cooldown_ok = last is None or (now - last).total_seconds() > cooldown

        if cooldown_ok:
            message = msg_template.format(location=location)
            broadcast("Thunderstorm Alert", message)
            last_notified[transition] = now
            app.state.thunderstorm_last_notified = last_notified
            LOG_BG.info(
                "[thunderstorm] Notified: %s -> %s: %s",
                prev_state,
                curr_state,
                message,
            )

    # Always update state after processing
    app.state.prev_thunderstorm_state = curr_state


# ---------------------------------------------------------------------
# Lifespan – background rain-status polling
# ---------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: N802 – FastAPI naming style
    """Start a 60-second polling loop that pushes notifications on changes."""

    async def _init_prev_raining() -> None:
        """Prime ``app.state.prev_precip_type`` and history at startup."""
        # Initialize state tracking
        app.state.prev_precip_type = None
        app.state.prev_thunderstorm_state = "none"  # Thunderstorm state tracking
        app.state.thunderstorm_last_notified = {}  # Thunderstorm cooldown tracking
        app.state.precip_history = []
        app.state.was_in_flight = False

        coords = await _unwrap(current_coords())

        # A) Still unknown → leave sentinel so loop keeps trying
        if not coords or coords.get("unknown") or "lat" not in coords:
            return

        # B) In flight → always "none"
        if coords.get("in_flight"):
            app.state.prev_precip_type = "none"
            app.state.prev_thunderstorm_state = "none"
            app.state.precip_history = []  # Reset history when in flight
            app.state.was_in_flight = True
            return

        precip = await _unwrap(get_precip(coords["lat"], coords["lon"]))
        app.state.prev_precip_type = precip["precipitation_type"]
        app.state.prev_thunderstorm_state = precip.get("thunderstorm_state", "none")

    async def _check_and_notify() -> None:
        """Run every minute – send a push if precipitation state changes."""
        LOG_BG.info("[loop] tick")
        coords = await _unwrap(current_coords())
        LOG_BG.info("[loop] coords %s", coords)
        if not coords or "lat" not in coords:
            return  # still unknown
        if coords.get("in_flight"):
            app.state.prev_precip_type = "none"
            app.state.precip_history = []  # Reset history when in flight
            app.state.was_in_flight = True  # Track that we were in flight
            return

        # Check if we just landed (transitioning from in-flight to ground)
        was_in_flight = getattr(app.state, "was_in_flight", False)

        precip = await _unwrap(get_precip(coords["lat"], coords["lon"]))

        # Handle weather API errors - skip notification but log
        if precip.get("error"):
            LOG_BG.warning(
                "[loop] Weather API error: %s - skipping notification check",
                precip.get("reason"),
            )
            return  # Keep previous state, retry next cycle

        prev_type = getattr(app.state, "prev_precip_type", None)
        curr_type = precip["precipitation_type"]

        LOG_BG.info(
            "[loop] precip_type=%s prev=%s (rain=%.1fmm/h snow=%.1fcm/h)",
            curr_type,
            prev_type,
            precip["rain"],
            precip["snow"],
        )
        LOG.info(
            "[loop] coords=%s precip_type=%s prev=%s",
            coords.get("name"),
            curr_type,
            prev_type,
        )

        # Check if notification should be sent
        if DEBOUNCE_NOTIFICATIONS:
            # Debouncing: require 2 consecutive checks with same state
            history = getattr(app.state, "precip_history", [])
            should_notify, new_history = should_notify_state_change(
                history, prev_type, curr_type
            )
            app.state.precip_history = new_history
        else:
            # No debouncing: notify immediately on state change
            should_notify = prev_type is not None and curr_type != prev_type

        # Check if we should suppress due to just landing
        suppress_landing = should_suppress_landing_notification(
            was_in_flight, prev_type, curr_type
        )

        LOG_BG.info(
            "[loop] should_notify=%s suppress_landing=%s was_in_flight=%s debounce=%s",
            should_notify,
            suppress_landing,
            was_in_flight,
            DEBOUNCE_NOTIFICATIONS,
        )

        # Clear the was_in_flight flag after first ground check
        app.state.was_in_flight = False

        # Send notification if state change is stable
        # Special handling for landing in precipitation
        # (different message, not suppressed)
        if should_notify:
            location = coords["name"]

            # Check if this is a landing-in-precipitation scenario
            if suppress_landing:
                # Trump just landed in precipitation - send accurate message
                title = "Trump Landed"
                if curr_type == "rain":
                    message = f"Trump just landed at {location} - it's raining there!"
                elif curr_type == "snow":
                    message = f"Trump just landed at {location} - it's snowing there!"
                elif curr_type == "both":
                    message = (
                        f"Trump just landed at {location} - rain and snow falling!"
                    )
                else:
                    # Landing in clear weather - no notification needed
                    app.state.prev_precip_type = curr_type
                    return

            # Normal transitions (not landing)
            elif prev_type == "none" and curr_type == "rain":
                title = "It's Raining!"
                message = f"It just started raining at {location}!"
            elif prev_type == "none" and curr_type == "snow":
                title = "It's Snowing!"
                message = f"It just started snowing at {location}!"
            elif prev_type == "none" and curr_type == "both":
                title = "Rain & Snow!"
                message = f"It just started raining and snowing at {location}!"
            elif curr_type == "none":
                # Stopped precipitating (was rain, snow, or both)
                title = "Weather Update"
                if prev_type == "rain":
                    message = f"It stopped raining at {location}."
                elif prev_type == "snow":
                    message = f"It stopped snowing at {location}."
                else:  # was "both"
                    message = f"Precipitation stopped at {location}."
            else:
                # Transition between precipitation types with specific messages
                if prev_type == "rain" and curr_type == "snow":
                    title = "It's Snowing!"
                    message = f"Rain turned to snow at {location}!"
                elif prev_type == "snow" and curr_type == "rain":
                    title = "It's Raining!"
                    message = f"Snow turned to rain at {location}!"
                elif prev_type == "rain" and curr_type == "both":
                    title = "Rain & Snow!"
                    message = f"Now it's raining AND snowing at {location}!"
                elif prev_type == "snow" and curr_type == "both":
                    title = "Rain & Snow!"
                    message = f"Rain started - now both rain and snow at {location}!"
                elif prev_type == "both" and curr_type == "rain":
                    title = "It's Raining!"
                    message = f"Snow stopped - just rain now at {location}."
                elif prev_type == "both" and curr_type == "snow":
                    title = "It's Snowing!"
                    message = f"Rain stopped - just snow now at {location}."
                else:
                    # Catch-all for unexpected transitions
                    title = "Weather Update"
                    message = f"Precipitation changed to {curr_type} at {location}!"

            broadcast(title, message)

            # Emit Discord event for rain state change
            emit_rain_state_changed(
                was=prev_type or "unknown",
                now=curr_type,
                location=location,
                rain_mmh=precip.get("rain", 0.0),
                snow_cmh=precip.get("snow", 0.0),
            )

            # Update last notified state
            app.state.prev_precip_type = curr_type

        # ── Thunderstorm check ────────────────────────────────────────
        # Check for thunderstorm state changes (independent of precipitation)
        LOG_BG.info(
            "[loop] thunderstorm=%s state=%s",
            precip.get("thunderstorm", False),
            precip.get("thunderstorm_state", "none"),
        )
        _maybe_send_thunderstorm_notification(app, precip, coords)

        # ── Capture debug snapshot ────────────────────────────────────
        # Store current state for historical debugging
        add_snapshot(coords=coords, precip=precip)

    try:
        await _init_prev_raining()
    except Exception as exc:
        LOG.warning("[init] Could not initialize rain state: %s", exc)
        LOG.warning("[init] Will retry in background loop")

    # Enable Discord events now that initial state is set
    mark_initialization_complete()

    async def _loop() -> None:
        while True:
            try:
                await _check_and_notify()
            except Exception as exc:
                LOG.error("[loop] crashed: %s", exc, exc_info=True)
            # 5 min polling interval. With DEBOUNCE_NOTIFICATIONS=False,
            # notifications trigger on first detection.
            # Set DEBOUNCE_NOTIFICATIONS=True if spam occurs.
            await asyncio.sleep(300)

    task = asyncio.create_task(_loop())
    app.state.check_and_notify = _check_and_notify  # type: ignore[attr-defined]

    # Notify Discord that the machine has started
    emit_machine_started()

    yield  # ⇢ application runs here

    # Shutdown: stop polling loop
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------
# FastAPI instance & middleware
# ---------------------------------------------------------------------
app = FastAPI(title="Is It Raining on Trump?", lifespan=lifespan)

# Rate limiter state
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Return 429 when rate limit is exceeded."""
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."},
    )


# CORS: restrict to known frontend origins
ALLOWED_ORIGINS = [
    "https://rain-on-trump.pages.dev",
    "http://localhost:8090",
    "http://127.0.0.1:8090",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# Health probe --------------------------------------------------------
@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> PlainTextResponse:
    """Return HTTP 200 with body “ok” if the app is up."""
    return PlainTextResponse("ok", status_code=200)


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/plane_state.json")
async def plane_state() -> JSONResponse:
    """
    Return the freshest aircraft state plus feed-level errors (if any).

    The payload always contains keys ``source`` and ``state``.
    """
    raw = get_plane_state()

    # Unpack the error envelope shape `{"state": …, "errors":[…]}`
    errors = raw.get("errors", []) if isinstance(raw, dict) else []
    state = raw["state"] if isinstance(raw, dict) and "state" in raw else raw

    source = "opensky" if state and not errors else "adsbfi" if state else "none"
    payload: dict[str, Any] = {"source": source, "state": state}
    if errors:
        payload["errors"] = errors
    return JSONResponse(content=jsonable_encoder(payload))


@app.get("/is_it_raining.json")
async def is_it_raining(
    lat: float | None = Query(None),
    lon: float | None = Query(None),
) -> JSONResponse:
    """Machine-readable answer to the titular question."""
    if lat is not None and lon is not None:
        coords = {"lat": lat, "lon": lon, "name": f"({lat:.2f},{lon:.2f})"}
    else:
        coords = await _unwrap(current_coords())

    # A) Plane in flight → always not raining, no thunderstorm
    if coords.get("in_flight"):
        return JSONResponse(
            {
                "precipitating": False,
                "mmh": 0.0,
                "precipitation_type": "none",
                "snow": 0.0,
                "thunderstorm": False,
                "thunderstorm_state": "none",
                "sunrise": None,
                "sunset": None,
                "coords": coords,
                "timestamp": dt.datetime.now(UTC).isoformat(),
            }
        )

    # B) Unknown location → unable to answer
    if coords.get("unknown"):
        return JSONResponse(
            {
                "precipitating": None,
                "mmh": None,
                "precipitation_type": None,
                "snow": None,
                "thunderstorm": None,
                "thunderstorm_state": None,
                "sunrise": None,
                "sunset": None,
                "coords": coords,
                "timestamp": dt.datetime.now(UTC).isoformat(),
            }
        )

    # C) Normal lat/lon - check weather
    precip = await _unwrap(get_precip(coords["lat"], coords["lon"]))

    # Handle weather API errors
    if precip.get("error"):
        return JSONResponse(
            {
                "precipitating": None,
                "mmh": None,
                "precipitation_type": None,
                "snow": None,
                "thunderstorm": None,
                "thunderstorm_state": None,
                "sunrise": None,
                "sunset": None,
                "coords": coords,
                "error": precip.get("reason", "Weather API error"),
                "timestamp": dt.datetime.now(UTC).isoformat(),
            },
            status_code=503,  # Service Unavailable
        )

    return JSONResponse(
        {
            "precipitating": precip["precipitating"],
            "mmh": precip.get("rain", 0.0),
            "precipitation_type": precip.get("precipitation_type", "none"),
            "snow": precip.get("snow", 0.0),
            "thunderstorm": precip.get("thunderstorm", False),
            "thunderstorm_state": precip.get("thunderstorm_state", "none"),
            "sunrise": precip.get("sunrise"),
            "sunset": precip.get("sunset"),
            "coords": coords,
            "timestamp": dt.datetime.now(UTC).isoformat(),
        }
    )


@app.post("/subscribe")
@limiter.limit("30/hour")
async def subscribe(sub: dict, request: Request) -> dict[str, Any]:
    """Store a new WebPush subscription (rate limited: 30/hour per IP).

    Returns preferences on success for client-side caching.
    """
    result = add_subscription(sub)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Invalid subscription or subscription limit reached"),
        )
    return result  # {"ok": True, "preferences": {...}}


@app.delete("/subscribe")
@limiter.limit("30/hour")
async def unsubscribe(body: dict, request: Request) -> dict[str, bool]:
    """Remove a WebPush subscription (unsubscribe).

    Args:
        body: Dict with 'endpoint' key.

    Returns:
        {"ok": True} on success.

    Raises:
        400: Missing endpoint.
        404: Subscription not found.
    """
    endpoint = body.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint required")

    success = remove_subscription(endpoint)
    if not success:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"ok": True}


@app.patch("/preferences")
@limiter.limit("60/hour")
async def update_preferences_route(body: dict, request: Request) -> dict[str, Any]:
    """Update notification preferences for a subscription.

    Args:
        body: Dict with 'endpoint' and 'preferences' keys.
              Preferences can be partial (only update provided fields).

    Returns:
        {"ok": True, "preferences": {...}} with full updated preferences.

    Raises:
        400: Missing endpoint or invalid preference values.
        404: Subscription not found.
    """
    endpoint = body.get("endpoint")
    preferences = body.get("preferences", {})

    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint required")

    # Validate preference values are booleans
    for key in ["rain_start", "rain_stop", "thunderstorm"]:
        if key in preferences and not isinstance(preferences[key], bool):
            raise HTTPException(status_code=400, detail=f"{key} must be boolean")

    result = update_preferences(endpoint, preferences)
    if result is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    return {"ok": True, "preferences": result}


@app.post("/broadcast")
async def broadcast_route(
    msg: str = Query(...),
    token: str = Query(...),
    notification_type: str | None = Query(None, description="Filter by type: rain_start, rain_stop, thunderstorm_start, thunderstorm_end"),
) -> dict[str, Any]:
    """Manually broadcast a push notification (protected by a token)."""
    if not secrets.compare_digest(token.strip(), BROADCAST_TOKEN.strip()):
        raise HTTPException(status_code=403, detail="Forbidden")
    sent_count = broadcast("Rain on Trump", msg, notification_type=notification_type)
    return {"ok": True, "message": msg, "sent_count": sent_count, "notification_type": notification_type}


@app.post("/cleanup_subscriptions")
async def cleanup_subscriptions_route(
    token: str = Query(...),
    max_days: int = Query(365),
) -> dict[str, Any]:
    """
    Admin endpoint to cleanup old subscriptions.

    Protected by BROADCAST_TOKEN. Only removes subscriptions that have
    NEVER received a notification (last_delivery is absent).

    Args:
        token: Admin token (must match BROADCAST_TOKEN).
        max_days: Maximum age in days for never-delivered subscriptions (default: 365).

    Returns:
        Dict with cleanup results.
    """
    if not secrets.compare_digest(token.strip(), BROADCAST_TOKEN.strip()):
        raise HTTPException(status_code=403, detail="Forbidden")

    removed_count = cleanup_old_subscriptions(max_days=max_days)
    stats = get_subscription_stats()

    return {
        "ok": True,
        "removed": removed_count,
        "remaining": stats["total"],
        "stats": stats,
    }


@app.get("/subscription_stats")
async def subscription_stats_route(
    token: str = Query(...),
) -> dict[str, Any]:
    """
    Get statistics about push subscriptions.

    Protected by BROADCAST_TOKEN.

    Args:
        token: Admin token (must match BROADCAST_TOKEN).

    Returns:
        Dict with subscription statistics.
    """
    if not secrets.compare_digest(token.strip(), BROADCAST_TOKEN.strip()):
        raise HTTPException(status_code=403, detail="Forbidden")

    stats = get_subscription_stats()
    return {"ok": True, "stats": stats}


# ---------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------
@app.get("/debug", response_class=HTMLResponse)
async def debug() -> HTMLResponse:
    """Human-readable trace for quick manual inspection."""
    _loc_cache.clear()
    _weather_cache.clear()

    coords, loc_trace = await current_coords(trace=[])
    if coords.get("in_flight"):
        precip = {"precipitating": False}
        weather_trace = [
            {
                "ts": dt.datetime.now(UTC).isoformat(),
                "phase": "weather",
                "step": "skipped – in flight",
            }
        ]
    elif coords.get("unknown") or "lat" not in coords:
        precip = {"precipitating": None}
        weather_trace = [
            {
                "ts": dt.datetime.now(UTC).isoformat(),
                "phase": "weather",
                "step": "skipped – unknown location",
            }
        ]
    else:
        precip, weather_trace = await get_precip(coords["lat"], coords["lon"], trace=[])

    now = dt.datetime.now(UTC).isoformat()
    answer = "🌧️ YES" if precip["precipitating"] else "☀️ NO"

    html_parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<title>Debug Trace</title>",
        "<style>body{font-family:system-ui,sans-serif;padding:1rem;} "
        "ul{padding-left:1.2rem;} code{background:#f4f4f4;padding:.2rem;"
        "display:block;margin:.2rem 0;}</style>",
        "</head><body>",
        f"<h1>Debug Trace</h1><p><strong>Run at:</strong> {now}</p>",
        "<h2>Location Steps</h2><ul>",
        *[f"<li><code>{step}</code></li>" for step in loc_trace],
        "</ul><h2>Weather Steps</h2><ul>",
        *[f"<li><code>{step}</code></li>" for step in weather_trace],
        "</ul>",
        f"<p><strong>Final answer:</strong> {answer} at {coords.get('name')}</p>",
        "</body></html>",
    ]
    return HTMLResponse("\n".join(html_parts))


@app.get("/debug.json")
async def debug_json() -> JSONResponse:  # noqa: D401
    """Machine-readable debug trace (used by `frontend/debug.html`)."""
    _loc_cache.clear()
    _weather_cache.clear()

    coords, loc_trace = await current_coords(trace=[])

    # Handle the three possible situations ➜ precip + weather_trace
    if coords.get("in_flight"):
        precip = {"precipitating": False}
        weather_trace = [
            {
                "ts": dt.datetime.now(UTC).isoformat(),
                "phase": "weather",
                "step": "skipped – in flight",
            }
        ]
    elif coords.get("unknown") or "lat" not in coords or "lon" not in coords:
        precip = {"precipitating": None}
        weather_trace = [
            {
                "ts": dt.datetime.now(UTC).isoformat(),
                "phase": "weather",
                "step": "skipped – unknown location",
            }
        ]
    else:
        precip, weather_trace = await get_precip(coords["lat"], coords["lon"], trace=[])

    def _serialise(obj: Any) -> Any:
        """Recursively convert datetimes → ISO 8601 strings for JSON."""
        if isinstance(obj, dt.datetime):
            return obj.isoformat()
        if isinstance(obj, list):
            return [_serialise(v) for v in obj]
        if isinstance(obj, dict):
            return {k: _serialise(v) for k, v in obj.items()}
        return obj

    return JSONResponse(
        {
            "coords": _serialise(coords),
            "loc_trace": _serialise(loc_trace),
            "precip": _serialise(precip),
            "weather_trace": _serialise(weather_trace),
        }
    )


@app.get("/debug/history.json")
async def debug_history_json(
    limit: int = Query(50, ge=1, le=500),
    since_hours: float | None = Query(None, ge=0.1, le=168),
) -> JSONResponse:
    """
    Retrieve historical debug snapshots.

    Args:
        limit: Maximum number of snapshots to return (default: 50, max: 500).
        since_hours: Only return snapshots from the last N hours (max: 168 = 7 days).

    Returns:
        JSON with snapshots array and statistics.
    """
    snapshots = get_snapshots(limit=limit, since_hours=since_hours)
    stats = get_snapshot_stats()

    return JSONResponse(
        {
            "snapshots": snapshots,
            "stats": stats,
            "query": {"limit": limit, "since_hours": since_hours},
        }
    )


@app.get("/debug/history", response_class=HTMLResponse)
async def debug_history(
    limit: int = Query(20, ge=1, le=100),
) -> HTMLResponse:
    """Human-readable view of recent debug snapshots."""
    snapshots = get_snapshots(limit=limit)
    stats = get_snapshot_stats()

    rows = []
    for snap in snapshots:
        ts = snap.get("ts", "?")
        coords = snap.get("coords", {})
        precip = snap.get("precip", {})

        location = coords.get("name", "Unknown")
        reason = coords.get("reason", "?")
        confidence = coords.get("confidence", "?")
        precip_type = precip.get("precipitation_type", "?")
        rain = precip.get("rain", 0)
        snow = precip.get("snow", 0)

        rows.append(
            f"<tr>"
            f"<td>{ts}</td>"
            f"<td>{location}</td>"
            f"<td>{reason}</td>"
            f"<td>{confidence}</td>"
            f"<td>{precip_type}</td>"
            f"<td>{rain:.1f}</td>"
            f"<td>{snow:.1f}</td>"
            f"</tr>"
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Debug History</title>
    <style>
        body {{ font-family: system-ui, sans-serif; padding: 1rem; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 0.5rem; text-align: left; }}
        th {{ background: #f4f4f4; }}
        .stats {{ margin-bottom: 1rem; padding: 1rem; background: #f9f9f9; border-radius: 4px; }}
    </style>
</head>
<body>
    <h1>Debug History</h1>
    <div class="stats">
        <strong>Snapshots:</strong> {stats.get('count', 0)} |
        <strong>Oldest:</strong> {stats.get('oldest', 'N/A')} |
        <strong>Newest:</strong> {stats.get('newest', 'N/A')} |
        <strong>Retention:</strong> {stats.get('max_age_hours', 168)} hours
    </div>
    <table>
        <thead>
            <tr>
                <th>Timestamp</th>
                <th>Location</th>
                <th>Source</th>
                <th>Confidence</th>
                <th>Precip</th>
                <th>Rain (mm/h)</th>
                <th>Snow (cm/h)</th>
            </tr>
        </thead>
        <tbody>
            {"".join(rows) if rows else "<tr><td colspan='7'>No snapshots yet</td></tr>"}
        </tbody>
    </table>
    <p><a href="/debug/history.json?limit={limit}">View as JSON</a> |
       <a href="/debug/geocode">Geocode Log</a></p>
</body>
</html>"""

    return HTMLResponse(html)


@app.get("/debug/geocode.json")
async def debug_geocode_json(
    limit: int = Query(100, ge=1, le=500),
    since_hours: float | None = Query(None, ge=0.1, le=168),
    result_type: str | None = Query(None),
) -> JSONResponse:
    """
    Retrieve geocode log entries for monitoring and alias curation.

    Args:
        limit: Maximum number of entries to return (default: 100, max: 500).
        since_hours: Only return entries from the last N hours.
        result_type: Filter by result type (us, international, no_result, error, skipped).

    Returns:
        JSON with entries array and statistics.
    """
    entries = get_geocode_entries(
        limit=limit,
        since_hours=since_hours,
        result_type=result_type,
    )
    stats = get_geocode_stats()

    return JSONResponse(
        {
            "entries": entries,
            "stats": stats,
            "query": {
                "limit": limit,
                "since_hours": since_hours,
                "result_type": result_type,
            },
        }
    )


@app.get("/debug/geocode", response_class=HTMLResponse)
async def debug_geocode(
    limit: int = Query(50, ge=1, le=200),
) -> HTMLResponse:
    """Human-readable view of geocode log entries."""
    entries = get_geocode_entries(limit=limit)
    stats = get_geocode_stats()

    rows = []
    for entry in entries:
        ts = entry.get("ts", "?")
        query = entry.get("query", "?")
        result_type = entry.get("result_type", "?")
        lat = entry.get("lat")
        lon = entry.get("lon")
        country = entry.get("country", "")
        state = entry.get("state", "")
        error = entry.get("error", "")

        # Format coordinates
        coords = f"{lat:.4f}, {lon:.4f}" if lat is not None else "-"

        # Format location
        location = ", ".join(filter(None, [state, country])) or "-"

        # Color code result type
        type_class = {
            "us": "us",
            "international": "intl",
            "no_result": "fail",
            "error": "fail",
            "skipped": "skip",
        }.get(result_type, "")

        rows.append(
            f"<tr>"
            f"<td>{ts}</td>"
            f"<td>{query}</td>"
            f"<td class='{type_class}'>{result_type}</td>"
            f"<td>{coords}</td>"
            f"<td>{location}</td>"
            f"<td>{error}</td>"
            f"</tr>"
        )

    # Stats by type
    by_type = stats.get("by_type", {})
    type_summary = " | ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))

    html = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Geocode Log</title>
    <style>
        body {{ font-family: system-ui, sans-serif; padding: 1rem; }}
        table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
        th, td {{ border: 1px solid #ddd; padding: 0.4rem; text-align: left; }}
        th {{ background: #f4f4f4; }}
        .stats {{ margin-bottom: 1rem; padding: 1rem; background: #f9f9f9; border-radius: 4px; }}
        .us {{ color: #080; }}
        .intl {{ color: #008; }}
        .fail {{ color: #d00; font-weight: 600; }}
        .skip {{ color: #888; }}
        td:nth-child(2) {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; }}
    </style>
</head>
<body>
    <h1>Geocode Log</h1>
    <div class="stats">
        <strong>Total Entries:</strong> {stats.get('count', 0)} |
        <strong>By Type:</strong> {type_summary or 'N/A'} |
        <strong>Retention:</strong> {stats.get('max_age_hours', 168)} hours
    </div>
    <p><a href="/debug/history">← Back to History</a></p>
    <table>
        <thead>
            <tr>
                <th>Timestamp</th>
                <th>Query</th>
                <th>Type</th>
                <th>Coords</th>
                <th>Location</th>
                <th>Error</th>
            </tr>
        </thead>
        <tbody>
            {"".join(rows) if rows else "<tr><td colspan='6'>No geocode entries yet</td></tr>"}
        </tbody>
    </table>
    <p><a href="/debug/geocode.json?limit={limit}">View as JSON</a> |
       <a href="/debug/geocode.json?result_type=no_result">Failures only</a> |
       <a href="/debug/geocode.json?result_type=international">International only</a></p>
</body>
</html>"""

    return HTMLResponse(html)
