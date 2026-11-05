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
from html import escape as _esc
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from limits import parse as _parse_limit
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

# ─── Project modules ──────────────────────────────────────────────────
from . import executors
from . import event_service as _event_service
from .event_service import emit_machine_started, emit_rain_state_changed
from . import adsbfi_service as _adsbfi_service
from . import calendar_service as _calendar_service
from .flight_service import _cache as _flight_cache, get_plane_state
from .location_service import (
    _cached as _loc_cache,
    current_coords,
    mark_initialization_complete,
    poll_flight_history,
    pregeocode_events,
)
from .push_service import (
    add_subscription,
    broadcast,
    cleanup_old_subscriptions,
    get_subscription_stats,
    remove_subscription,
    update_preferences,
)
from .snapshot_service import add_snapshot, get_snapshot_stats, get_snapshots
from .geocode_log_service import get_geocode_entries, get_geocode_stats
from .weather_service import get_precip

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
def _client_ip(request: Request) -> str:
    """Rate-limit key: Fly's ``Fly-Client-IP`` header, else the socket peer."""
    fly_ip = request.headers.get("fly-client-ip", "").strip()
    if fly_ip:
        return fly_ip
    return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(key_func=_client_ip)


ADMIN_HEADER = "x-admin-token"
ADMIN_LIMIT = "10/minute"
DEBUG_LIMIT = "6/minute"  # every /debug* route, per IP


def _require_admin_token(token: str | None) -> None:
    """Raise 403 unless *token* matches a non-empty BROADCAST_TOKEN."""
    expected = BROADCAST_TOKEN.strip()
    if not expected or not secrets.compare_digest(
        (token or "").strip().encode(), expected.encode()
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


def _admin_token(request: Request, query_token: str | None) -> str | None:
    """The admin token from the ``X-Admin-Token`` header, else the deprecated
    ``?token=`` query parameter (kept for backward compatibility)."""
    return request.headers.get(ADMIN_HEADER) or query_token


class _RedactTokens:
    """Logging filter: never let ``token=…`` query values reach the logs."""

    _RE = re.compile(r"(token=)[^&\s\"]*", re.I)

    def filter(self, record) -> bool:  # noqa: A003 – logging.Filter API
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._RE.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._RE.sub(r"\1[redacted]", record.msg)
        return True


for _name in ("uvicorn.access", "uvicorn.error", "rain_loop", "bg"):
    logging.getLogger(_name).addFilter(_RedactTokens())


# Background loop timing. Weather itself stays cached for 5 min
# (weather_service.get_precip), so most ticks only re-check the location.
LOOP_INTERVAL_SEC = 60
#: Tests that drive check_and_notify() themselves set this False, otherwise the
#: real loop's first tick races with them for the stubbed weather readings.
START_BACKGROUND_LOOP = True
TICK_TIMEOUT_SEC = 120  # a hung tick is abandoned after this long
HEALTH_STALE_SEC = 300  # /healthz fails if no tick finished for this long
SNAPSHOT_INTERVAL_SEC = 300  # debug snapshots are time-based, not per tick
FLIGHT_GRACE_SEC = 900  # ignore flight dropouts shorter than this
GEOCODE_PER_TICK = 5  # venues geocoded per loop tick (Nominatim ≤ 1 req/s)
GEOCODE_TIMEOUT_SEC = 60
RELOCATION_KM = 25.0  # a move this far without a flight is a relocation
OVERRIDE_LIMIT = _parse_limit("5/minute")  # ?lat=&lon= debug override, per IP
#: Upstream weather calls the override may trigger, across all clients
OVERRIDE_BUDGET_PER_HOUR = 300
OVERRIDE_CACHE_SEC = 300  # matches the weather cache: repeats cost nothing
MAX_BODY_BYTES = 16 * 1024  # request bodies (subscriptions are < 2 KB)
FORCED_REFRESH_SEC = 60  # admin-forced /debug refresh, at most once per this


class _OverrideBudget:
    """Global hourly budget of override lookups that reach Open-Meteo.

    A coordinate pair served within :data:`OVERRIDE_CACHE_SEC` hits the
    weather cache and is free; any other pair spends one unit.
    """

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._spent: deque[float] = deque()
        self._recent: dict[tuple[float, float], float] = {}
        self._lock = threading.Lock()

    def allow(self, key: tuple[float, float]) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._spent and now - self._spent[0] > 3600:
                self._spent.popleft()
            for k, ts in list(self._recent.items()):
                if now - ts > OVERRIDE_CACHE_SEC:
                    del self._recent[k]
            if key in self._recent:
                return True
            if len(self._spent) >= self.per_hour:
                return False
            self._spent.append(now)
            self._recent[key] = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._spent.clear()
            self._recent.clear()


override_budget = _OverrideBudget(OVERRIDE_BUDGET_PER_HOUR)

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


_last_forced_refresh: float | None = None


def _clear_debug_caches() -> None:
    """Drop every memoised layer (admin-forced /debug refresh only)."""
    _loc_cache.clear()
    _flight_cache.clear()
    _adsbfi_service._cache.clear()
    _calendar_service._fetch_events.cache_clear()  # type: ignore[attr-defined]
    get_precip.cache_clear()  # type: ignore[attr-defined]


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
    app: FastAPI, precip: dict, coords: dict, *, silent: bool = False
) -> None:
    """
    Send state-change + severity thunderstorm notifications.

    The tracked state always advances first, so a failing push can never make
    the next tick resend the same alert. An end alert is only sent when the
    matching start/escalation alert was not suppressed.

    Args:
        app: FastAPI app instance (for state tracking).
        precip: Result from get_precip() (includes thunderstorm_state).
        coords: Current location coordinates.
        silent: Adopt the new state without notifying (landing, relocation).
    """
    curr_state = precip.get("thunderstorm_state", "none")
    prev_state = getattr(app.state, "prev_thunderstorm_state", "none")

    # No state change - nothing to do
    if curr_state == prev_state:
        return

    # Always update state before any I/O
    app.state.prev_thunderstorm_state = curr_state

    if silent:
        app.state.thunderstorm_start_suppressed = False
        return

    location = coords.get("name", "Trump's location")
    now = dt.datetime.now(UTC)
    last_notified = getattr(app.state, "thunderstorm_last_notified", {})
    is_end = curr_state == "none"

    # Determine transition and check if we should notify
    transition = f"{prev_state}->{curr_state}"

    if transition not in THUNDERSTORM_COOLDOWNS:
        return
    if is_end and getattr(app.state, "thunderstorm_start_suppressed", False):
        # The start was never announced; a lone "has passed" would confuse.
        app.state.thunderstorm_start_suppressed = False
        LOG_BG.info("[thunderstorm] End alert skipped (start was suppressed)")
        return

    cooldown, msg_template = THUNDERSTORM_COOLDOWNS[transition]
    last = last_notified.get(transition)
    cooldown_ok = last is None or (now - last).total_seconds() > cooldown
    if not cooldown_ok:
        if not is_end:
            app.state.thunderstorm_start_suppressed = True
        return

    message = msg_template.format(location=location)
    ts_type = "thunderstorm_end" if is_end else "thunderstorm_start"
    last_notified[transition] = now
    app.state.thunderstorm_last_notified = last_notified
    app.state.thunderstorm_start_suppressed = False
    try:
        broadcast("Thunderstorm Alert", message, notification_type=ts_type)
    except Exception as exc:
        LOG_BG.error("[thunderstorm] broadcast failed: %s", exc)
        return
    LOG_BG.info(
        "[thunderstorm] Notified: %s -> %s: %s", prev_state, curr_state, message
    )


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _confirms_landing(coords: dict) -> bool:
    """True when the location came from a grounded plane or a landing anchor.

    Fusion reasons keep these prefixes (``landing``, ``landing_base``,
    ``landing_destination``, ``landing_inferred``); ``en_route`` sets
    ``in_flight`` and so never reaches this check.
    """
    reason = str(coords.get("reason") or "")
    return reason.startswith(("plane_ground", "plane_tfr", "landing"))


def _rain_message(
    prev_type: str, curr_type: str, location: str, landing: bool
) -> tuple[str, str]:
    """Return the ``(title, message)`` for a precipitation state change."""
    if landing:
        # Landed in precipitation (so curr_type != "none")
        what = {
            "rain": "it's raining there!",
            "snow": "it's snowing there!",
        }.get(curr_type, "rain and snow falling!")
        return "Trump Landed", f"Trump just landed at {location} - {what}"
    if prev_type == "none":
        return {
            "rain": ("It's Raining!", f"It just started raining at {location}!"),
            "snow": ("It's Snowing!", f"It just started snowing at {location}!"),
            "both": (
                "Rain & Snow!",
                f"It just started raining and snowing at {location}!",
            ),
        }.get(curr_type, ("Weather Update", f"Precipitation at {location}!"))
    if curr_type == "none":
        # Stopped precipitating (was rain, snow, or both)
        stopped = {
            "rain": f"It stopped raining at {location}.",
            "snow": f"It stopped snowing at {location}.",
        }.get(prev_type, f"Precipitation stopped at {location}.")
        return "Weather Update", stopped
    # Transition between precipitation types with specific messages
    table = {
        ("rain", "snow"): ("It's Snowing!", f"Rain turned to snow at {location}!"),
        ("snow", "rain"): ("It's Raining!", f"Snow turned to rain at {location}!"),
        ("rain", "both"): (
            "Rain & Snow!",
            f"Now it's raining AND snowing at {location}!",
        ),
        ("snow", "both"): (
            "Rain & Snow!",
            f"Rain started - now both rain and snow at {location}!",
        ),
        ("both", "rain"): (
            "It's Raining!",
            f"Snow stopped - just rain now at {location}.",
        ),
        ("both", "snow"): (
            "It's Snowing!",
            f"Rain stopped - just snow now at {location}.",
        ),
    }
    return table.get(
        (prev_type, curr_type),
        ("Weather Update", f"Precipitation changed to {curr_type} at {location}!"),
    )


# ---------------------------------------------------------------------
# Lifespan – background rain-status polling
# ---------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: N802 – FastAPI naming style
    """Start a 60-second polling loop that pushes notifications on changes."""
    app.state.started_mono = time.monotonic()
    app.state.last_tick = None
    app.state.last_snapshot_mono = None

    async def _poll_history() -> None:
        """Record this tick's fleet states (requests only read the history)."""
        try:
            await asyncio.to_thread(poll_flight_history)
        except Exception as exc:
            LOG_BG.warning("[loop] flight history update failed: %s", exc)

    async def _init_prev_raining() -> None:
        """Prime ``app.state.prev_precip_type`` at startup."""
        # Initialize state tracking
        app.state.prev_precip_type = None
        app.state.prev_thunderstorm_state = "none"  # Thunderstorm state tracking
        app.state.thunderstorm_last_notified = {}  # Thunderstorm cooldown tracking
        app.state.thunderstorm_start_suppressed = False
        app.state.prev_location = None
        app.state.last_in_flight_at = None
        app.state.was_in_flight = False

        await _poll_history()
        coords = await _unwrap(current_coords())

        # A) Still unknown → leave sentinel so loop keeps trying
        if not coords or coords.get("unknown") or "lat" not in coords:
            return

        # B) In flight → always "none"
        if coords.get("in_flight"):
            app.state.prev_precip_type = "none"
            app.state.prev_thunderstorm_state = "none"
            app.state.was_in_flight = True
            app.state.last_in_flight_at = time.monotonic()
            return

        precip = await _unwrap(get_precip(coords["lat"], coords["lon"]))
        if precip.get("error"):
            return  # leave sentinel so the loop keeps trying
        app.state.prev_precip_type = precip["precipitation_type"]
        app.state.prev_thunderstorm_state = precip.get("thunderstorm_state", "none")
        app.state.prev_location = (coords["lat"], coords["lon"])

    async def _notify_rain_change(
        coords: dict, precip: dict, prev_type: str, curr_type: str, landing: bool
    ) -> None:
        """Advance the rain state, then push + emit (failures never re-fire)."""
        location = coords.get("name", "Trump's location")
        title, message = _rain_message(prev_type, curr_type, location, landing)
        notif_type = "rain_start" if (landing or curr_type != "none") else "rain_stop"

        # Advance first: a failing push must not be retried (duplicates).
        app.state.prev_precip_type = curr_type
        try:
            await asyncio.to_thread(
                broadcast, title, message, notification_type=notif_type
            )
        except Exception as exc:
            LOG_BG.error("[loop] broadcast failed: %s", exc)
        try:
            emit_rain_state_changed(
                was=prev_type or "unknown",
                now=curr_type,
                location=location,
                rain_mmh=precip.get("rain", 0.0),
                snow_cmh=precip.get("snow", 0.0),
            )
        except Exception as exc:
            LOG_BG.error("[loop] event emit failed: %s", exc)

    async def _check_and_notify() -> None:
        """Run every tick – send a push if precipitation state changes."""
        LOG_BG.info("[loop] tick")
        await _poll_history()
        coords = await _unwrap(current_coords())
        LOG_BG.info("[loop] coords %s", coords)
        if not coords or "lat" not in coords:
            return  # still unknown
        now_mono = time.monotonic()
        if coords.get("in_flight"):
            app.state.prev_precip_type = "none"
            app.state.prev_thunderstorm_state = "none"
            app.state.thunderstorm_start_suppressed = False
            app.state.was_in_flight = True  # Track that we were in flight
            app.state.last_in_flight_at = now_mono
            return

        # Check if we just landed (transitioning from in-flight to ground)
        was_in_flight = getattr(app.state, "was_in_flight", False)

        # A feed dropout mid-flight falls back to calendar/cache with
        # in_flight False. Hold the flight state until a grounded plane /
        # landing anchor confirms the landing or the grace period runs out.
        if was_in_flight and not _confirms_landing(coords):
            last_flight = getattr(app.state, "last_in_flight_at", None)
            if last_flight is not None and now_mono - last_flight < FLIGHT_GRACE_SEC:
                LOG_BG.info("[loop] flight dropout, holding state (%s)", coords)
                return

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
        curr_storm = precip.get("thunderstorm_state", "none")

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

        # Moved without a flight (calendar / overnight base / arrival cache)?
        prev_loc = getattr(app.state, "prev_location", None)
        relocated = (
            prev_loc is not None
            and not was_in_flight
            and _distance_km(prev_loc[0], prev_loc[1], coords["lat"], coords["lon"])
            > RELOCATION_KM
        )
        app.state.prev_location = (coords["lat"], coords["lon"])

        # Clear the was_in_flight flag after first ground check
        app.state.was_in_flight = False

        if prev_type is None or relocated:
            # Never primed (startup outage) or teleported: the old state says
            # nothing about this place, so adopt the new one silently.
            if relocated:
                LOG_BG.info(
                    "[loop] relocated to %s, adopting state", coords.get("name")
                )
            app.state.prev_precip_type = curr_type
            # Silent: pure state update, no I/O (so no await/thread hop here)
            _maybe_send_thunderstorm_notification(app, precip, coords, silent=True)
        else:
            should_notify = curr_type != prev_type
            suppress_landing = should_suppress_landing_notification(
                was_in_flight, prev_type, curr_type
            )
            LOG_BG.info(
                "[loop] should_notify=%s suppress_landing=%s was_in_flight=%s",
                should_notify,
                suppress_landing,
                was_in_flight,
            )
            if should_notify:
                await _notify_rain_change(
                    coords, precip, prev_type, curr_type, suppress_landing
                )

            # ── Thunderstorm check (independent of precipitation) ─────────
            LOG_BG.info(
                "[loop] thunderstorm=%s state=%s",
                precip.get("thunderstorm", False),
                curr_storm,
            )
            try:
                await asyncio.to_thread(
                    _maybe_send_thunderstorm_notification,
                    app,
                    precip,
                    coords,
                    # The landing push already covers a landing in bad weather
                    silent=was_in_flight,
                )
                if was_in_flight:
                    app.state.thunderstorm_start_suppressed = curr_storm != "none"
            except Exception as exc:
                LOG_BG.error("[loop] thunderstorm check failed: %s", exc)

        # ── Capture debug snapshot (at most once per SNAPSHOT_INTERVAL_SEC) ─
        last_snap = getattr(app.state, "last_snapshot_mono", None)
        if last_snap is None or now_mono - last_snap >= SNAPSHOT_INTERVAL_SEC - 5:
            app.state.last_snapshot_mono = now_mono
            try:
                await asyncio.to_thread(add_snapshot, coords=coords, precip=precip)
            except Exception as exc:
                LOG_BG.error("[loop] snapshot failed: %s", exc)

    try:
        await _init_prev_raining()
    except Exception as exc:
        LOG.warning("[init] Could not initialize rain state: %s", exc)
        LOG.warning("[init] Will retry in background loop")

    # Enable Discord events now that initial state is set
    mark_initialization_complete()

    maintenance: dict[str, asyncio.Future | None] = {"job": None}

    def _maintenance_job() -> None:
        """Refresh the calendar before it expires, then geocode a few upcoming
        / recent venues into the persistent cache (blocking; one at a time).

        The request path never geocodes: an uncached venue yields no claim
        until this has run (geopy is rate limited to 1 req/s)."""
        try:
            _calendar_service.prefetch_events()
        except Exception as exc:
            LOG_BG.warning("[maintenance] calendar prefetch failed: %s", exc)
        try:
            done = pregeocode_events(GEOCODE_PER_TICK)
            if done:
                LOG_BG.info("[maintenance] geocoded %d venue(s)", done)
        except Exception as exc:
            LOG_BG.warning("[maintenance] pre-geocoding failed: %s", exc)

    async def _maintenance_loop() -> None:
        """Prefetch + geocoding, off the alert path. Single-flight: a batch
        still running in its thread (slow Nominatim) is never restarted."""
        loop = asyncio.get_running_loop()
        while True:
            job = maintenance["job"]
            if job is None or job.done():
                maintenance["job"] = loop.run_in_executor(None, _maintenance_job)
            else:
                LOG_BG.info("[maintenance] previous batch still running")
            await asyncio.sleep(LOOP_INTERVAL_SEC)

    async def _loop() -> None:
        while True:
            try:
                await asyncio.wait_for(_check_and_notify(), TICK_TIMEOUT_SEC)
                app.state.last_tick = time.monotonic()
            except asyncio.TimeoutError:
                LOG.error("[loop] tick timed out after %ss", TICK_TIMEOUT_SEC)
            except Exception as exc:
                LOG.error("[loop] crashed: %s", exc, exc_info=True)
                app.state.last_tick = time.monotonic()  # loop itself is alive
            await asyncio.sleep(LOOP_INTERVAL_SEC)

    _event_service.set_main_loop(asyncio.get_running_loop())
    task = asyncio.create_task(_loop()) if START_BACKGROUND_LOOP else None
    maint = asyncio.create_task(_maintenance_loop()) if START_BACKGROUND_LOOP else None
    app.state.check_and_notify = _check_and_notify  # type: ignore[attr-defined]

    # Notify Discord that the machine has started
    emit_machine_started()

    try:
        yield  # ⇢ application runs here
    finally:
        # Shutdown: stop polling loop
        app.state.started_mono = None  # /healthz: no loop to supervise
        for t in (task, maint):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        _event_service.set_main_loop(None)


# ---------------------------------------------------------------------
# FastAPI instance & middleware
# ---------------------------------------------------------------------
app = FastAPI(title="Is It Raining on Trump?", lifespan=lifespan)

# Rate limiter state
app.state.limiter = limiter


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """422 without echoing the submitted input back (no amplification)."""
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


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
    "https://rain-on-trump-dev.pages.dev",  # dev PWA (scripts/deploy-dev-pwa.sh)
    "http://localhost:8090",
    "http://127.0.0.1:8090",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# Request guard: body cap + bounded executor ----------------------------
async def _send_413(send) -> None:
    body = b'{"detail":"Request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    """Pure ASGI middleware, outermost.

    * Rejects bodies over :data:`MAX_BODY_BYTES` with 413 before they are
      parsed: by ``Content-Length``, and by counting streamed bytes for chunked
      requests (the rest is not read; the app's response is replaced).
    * Runs the request's blocking calls on the bounded request executor
      (:mod:`executors`), so the background loop always gets a worker.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ctx_token = executors.current_executor.set(executors.REQUEST_EXECUTOR)
        try:
            length = dict(scope.get("headers") or []).get(b"content-length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError:
                    declared = MAX_BODY_BYTES + 1
                if declared > MAX_BODY_BYTES:
                    await _send_413(send)
                    return
            state = {"bytes": 0, "too_large": False, "started": False}

            async def guarded_receive():
                if state["too_large"]:
                    return {"type": "http.request", "body": b"", "more_body": False}
                message = await receive()
                if message["type"] == "http.request":
                    state["bytes"] += len(message.get("body", b""))
                    if state["bytes"] > MAX_BODY_BYTES:
                        state["too_large"] = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                return message

            async def guarded_send(message):
                if state["too_large"]:
                    if (
                        message["type"] == "http.response.start"
                        and not state["started"]
                    ):
                        state["started"] = True
                        await _send_413(send)
                    return
                if message["type"] == "http.response.start":
                    state["started"] = True
                await send(message)

            await self.app(scope, guarded_receive, guarded_send)
        finally:
            executors.current_executor.reset(ctx_token)


# Cache-Control middleware for JSON endpoints -------------------------
@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    """Add Cache-Control headers for JSON API responses.

    Enables browser/CDN caching with 30-second max-age to reduce
    duplicate requests during rapid tab switches while keeping data fresh.
    """
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".json") and not path.startswith("/debug"):
        response.headers["Cache-Control"] = "public, max-age=30"
    return response


# Health probe --------------------------------------------------------
@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> PlainTextResponse:
    """Return 200 "ok", or 503 if the background loop has stalled.

    The first :data:`HEALTH_STALE_SEC` after start are always healthy; after
    that a tick must have completed within that window. Without a running
    lifespan (no loop to supervise) the probe is a plain liveness check.
    """
    started = getattr(app.state, "started_mono", None)
    if started is not None:
        now = time.monotonic()
        last = getattr(app.state, "last_tick", None)
        ref = started if last is None else last
        if now - started >= HEALTH_STALE_SEC and now - ref > HEALTH_STALE_SEC:
            return PlainTextResponse("background loop stalled", status_code=503)
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
    raw = await executors.to_thread(get_plane_state)

    # Unpack the error envelope shape `{"state": …, "errors":[…]}`
    errors = raw.get("errors", []) if isinstance(raw, dict) else []
    state = raw["state"] if isinstance(raw, dict) and "state" in raw else raw

    source = state.get("source", "unknown") if state else "none"
    payload: dict[str, Any] = {"source": source, "state": state}
    if errors:
        payload["errors"] = errors
    return JSONResponse(content=jsonable_encoder(payload))


@app.get("/is_it_raining.json")
async def is_it_raining(
    request: Request,
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
) -> JSONResponse:
    """Machine-readable answer to the titular question."""
    if (lat is None) != (lon is None):
        raise HTTPException(
            status_code=422, detail="lat and lon must be given together"
        )
    if lat is not None and lon is not None:
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise HTTPException(status_code=422, detail="lat/lon must be finite")
        if limiter.enabled and not limiter.limiter.hit(
            OVERRIDE_LIMIT, "coord-override", _client_ip(request)
        ):
            raise HTTPException(
                status_code=429, detail="Rate limit exceeded. Try again later."
            )
        # Round so nearby values share one weather-cache entry
        lat, lon = round(lat, 2), round(lon, 2)
        if not override_budget.allow((lat, lon)):
            raise HTTPException(
                status_code=429,
                detail="Coordinate override budget spent. Try again later.",
            )
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
                "sunrise_utc": None,
                "sunset_utc": None,
                "utc_offset_seconds": None,
                "wind_kmh": None,
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
                "sunrise_utc": None,
                "sunset_utc": None,
                "utc_offset_seconds": None,
                "wind_kmh": None,
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
                "sunrise_utc": None,
                "sunset_utc": None,
                "utc_offset_seconds": None,
                "wind_kmh": None,
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
            "sunrise_utc": precip.get("sunrise_utc"),
            "sunset_utc": precip.get("sunset_utc"),
            "utc_offset_seconds": precip.get("utc_offset_seconds"),
            "wind_kmh": precip.get("wind_kmh"),
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
    result = await executors.to_thread(add_subscription, sub)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get(
                "error", "Invalid subscription or subscription limit reached"
            ),
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

    success = await executors.to_thread(remove_subscription, endpoint)
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

    if not endpoint or not isinstance(endpoint, str):
        raise HTTPException(status_code=400, detail="endpoint required")
    if not isinstance(preferences, dict):
        raise HTTPException(status_code=400, detail="preferences must be an object")

    # Validate preference values are booleans
    for key in ["rain_start", "rain_stop", "thunderstorm"]:
        if key in preferences and not isinstance(preferences[key], bool):
            raise HTTPException(status_code=400, detail=f"{key} must be boolean")

    result = await executors.to_thread(update_preferences, endpoint, preferences)
    if result is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    return {"ok": True, "preferences": result}


_DEPRECATED_TOKEN = Query(
    None,
    deprecated=True,
    description="Deprecated: send the X-Admin-Token header instead.",
)


@app.post("/broadcast")
@limiter.limit(ADMIN_LIMIT)
async def broadcast_route(
    request: Request,
    msg: str = Query(...),
    token: str | None = _DEPRECATED_TOKEN,
    notification_type: str | None = Query(
        None,
        description=(
            "Filter by type: rain_start, rain_stop, thunderstorm_start, "
            "thunderstorm_end"
        ),
    ),
) -> dict[str, Any]:
    """Manually broadcast a push notification (protected by a token)."""
    _require_admin_token(_admin_token(request, token))
    sent_count = await executors.to_thread(
        broadcast, "Rain on Trump", msg, notification_type=notification_type
    )
    return {
        "ok": True,
        "message": msg,
        "sent_count": sent_count,
        "notification_type": notification_type,
    }


@app.post("/cleanup_subscriptions")
@limiter.limit(ADMIN_LIMIT)
async def cleanup_subscriptions_route(
    request: Request,
    token: str | None = _DEPRECATED_TOKEN,
    max_days: int = Query(365),
) -> dict[str, Any]:
    """
    Admin endpoint to cleanup old subscriptions.

    Protected by BROADCAST_TOKEN. Only removes subscriptions that have
    NEVER received a notification (last_delivery is absent).

    Args:
        token: Deprecated query form of the admin token (use X-Admin-Token).
        max_days: Maximum age in days for never-delivered subscriptions (default: 365).

    Returns:
        Dict with cleanup results.
    """
    _require_admin_token(_admin_token(request, token))

    removed_count = await executors.to_thread(
        cleanup_old_subscriptions, max_days=max_days
    )
    stats = await executors.to_thread(get_subscription_stats)

    return {
        "ok": True,
        "removed": removed_count,
        "remaining": stats["total"],
        "stats": stats,
    }


@app.get("/subscription_stats")
@limiter.limit(ADMIN_LIMIT)
async def subscription_stats_route(
    request: Request,
    token: str | None = _DEPRECATED_TOKEN,
) -> dict[str, Any]:
    """
    Get statistics about push subscriptions.

    Protected by BROADCAST_TOKEN.

    Args:
        token: Deprecated query form of the admin token (use X-Admin-Token).

    Returns:
        Dict with subscription statistics.
    """
    _require_admin_token(_admin_token(request, token))

    stats = await executors.to_thread(get_subscription_stats)
    return {"ok": True, "stats": stats}


# ---------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------
def _debug_refresh(request: Request, refresh: bool) -> str:
    """Forced refresh for /debug*: admin token only, once per 60 s globally.

    Anonymous calls never clear caches: the trace recomputes the (pure) fusion
    from the memoised inputs.
    """
    global _last_forced_refresh
    if not refresh:
        return "cached inputs"
    _require_admin_token(request.headers.get(ADMIN_HEADER))
    now = time.monotonic()
    if _last_forced_refresh is not None and (
        now - _last_forced_refresh < FORCED_REFRESH_SEC
    ):
        return f"refresh skipped (at most once per {FORCED_REFRESH_SEC} s)"
    _last_forced_refresh = now
    _clear_debug_caches()
    return "refreshed"


@app.get("/debug", response_class=HTMLResponse)
@limiter.limit(DEBUG_LIMIT)
async def debug(request: Request, refresh: bool = Query(False)) -> HTMLResponse:
    """Human-readable trace for quick manual inspection."""
    cache_state = _debug_refresh(request, refresh)

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
        f"<h1>Debug Trace</h1><p><strong>Run at:</strong> {now} "
        f"({_esc(cache_state)})</p>",
        "<h2>Location Steps</h2><ul>",
        *[f"<li><code>{_esc(str(step))}</code></li>" for step in loc_trace],
        "</ul><h2>Weather Steps</h2><ul>",
        *[f"<li><code>{_esc(str(step))}</code></li>" for step in weather_trace],
        "</ul>",
        f"<p><strong>Final answer:</strong> {answer} at "
        f"{_esc(str(coords.get('name')))}</p>",
        "</body></html>",
    ]
    return HTMLResponse("\n".join(html_parts))


@app.get("/debug.json")
@limiter.limit(DEBUG_LIMIT)
async def debug_json(  # noqa: D401
    request: Request, refresh: bool = Query(False)
) -> JSONResponse:
    """Machine-readable debug trace (used by `frontend/debug.html`)."""
    cache_state = _debug_refresh(request, refresh)

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
            "cache": cache_state,
        }
    )


@app.get("/debug/history.json")
@limiter.limit(DEBUG_LIMIT)
async def debug_history_json(
    request: Request,
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
    snapshots = await executors.to_thread(
        get_snapshots, limit=limit, since_hours=since_hours
    )
    stats = await executors.to_thread(get_snapshot_stats)

    return JSONResponse(
        {
            "snapshots": snapshots,
            "stats": stats,
            "query": {"limit": limit, "since_hours": since_hours},
        }
    )


@app.get("/debug/history", response_class=HTMLResponse)
@limiter.limit(DEBUG_LIMIT)
async def debug_history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> HTMLResponse:
    """Human-readable view of recent debug snapshots."""
    snapshots = await executors.to_thread(get_snapshots, limit=limit)
    stats = await executors.to_thread(get_snapshot_stats)

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
            f"<td>{_esc(str(ts))}</td>"
            f"<td>{_esc(str(location))}</td>"
            f"<td>{_esc(str(reason))}</td>"
            f"<td>{_esc(str(confidence))}</td>"
            f"<td>{_esc(str(precip_type))}</td>"
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
        .stats {{ margin-bottom: 1rem; padding: 1rem; background: #f9f9f9; \
border-radius: 4px; }}
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
            {"".join(rows) if rows else (
                "<tr><td colspan='7'>" "No snapshots yet</td></tr>"
            )}
        </tbody>
    </table>
    <p><a href="/debug/history.json?limit={limit}">View as JSON</a> |
       <a href="/debug/geocode">Geocode Log</a></p>
</body>
</html>"""

    return HTMLResponse(html)


@app.get("/debug/geocode.json")
@limiter.limit(DEBUG_LIMIT)
async def debug_geocode_json(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    since_hours: float | None = Query(None, ge=0.1, le=168),
    result_type: str | None = Query(None),
) -> JSONResponse:
    """
    Retrieve geocode log entries for monitoring and alias curation.

    Args:
        limit: Maximum number of entries to return (default: 100, max: 500).
        since_hours: Only return entries from the last N hours.
        result_type: Filter by result type (us, international, no_result,
            error, skipped).

    Returns:
        JSON with entries array and statistics.
    """
    entries = await executors.to_thread(
        get_geocode_entries,
        limit=limit,
        since_hours=since_hours,
        result_type=result_type,
    )
    stats = await executors.to_thread(get_geocode_stats)

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
@limiter.limit(DEBUG_LIMIT)
async def debug_geocode(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> HTMLResponse:
    """Human-readable view of geocode log entries."""
    entries = await executors.to_thread(get_geocode_entries, limit=limit)
    stats = await executors.to_thread(get_geocode_stats)

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
            f"<td>{_esc(str(ts))}</td>"
            f"<td>{_esc(str(query))}</td>"
            f"<td class='{type_class}'>{_esc(str(result_type))}</td>"
            f"<td>{_esc(str(coords))}</td>"
            f"<td>{_esc(str(location))}</td>"
            f"<td>{_esc(str(error))}</td>"
            f"</tr>"
        )

    # Stats by type
    by_type = stats.get("by_type", {})
    type_summary = _esc(" | ".join(f"{k}: {v}" for k, v in sorted(by_type.items())))

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
        .stats {{ margin-bottom: 1rem; padding: 1rem; background: #f9f9f9; \
border-radius: 4px; }}
        .us {{ color: #080; }}
        .intl {{ color: #008; }}
        .fail {{ color: #d00; font-weight: 600; }}
        .skip {{ color: #888; }}
        td:nth-child(2) {{ max-width: 200px; overflow: hidden; \
text-overflow: ellipsis; }}
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
            {"".join(rows) if rows else (
                "<tr><td colspan='6'>" "No geocode entries yet</td></tr>"
            )}
        </tbody>
    </table>
    <p><a href="/debug/geocode.json?limit={limit}">View as JSON</a> |
       <a href="/debug/geocode.json?result_type=no_result">Failures only</a> |
       <a href="/debug/geocode.json?result_type=international">\
International only</a></p>
</body>
</html>"""

    return HTMLResponse(html)


# Outermost middleware (added last): body cap + request executor
app.add_middleware(RequestGuard)
