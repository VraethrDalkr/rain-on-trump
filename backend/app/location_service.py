"""
Fetches Donald Trump’s *best-guess* current coordinates.

Algorithm
1. Query FAA Temporary-Flight-Restriction (TFR) *list* and keep only type = VIP.
2. Pick the most‑recent VIP NOTAM (it’s almost always the President).
3.   • If the record includes numeric lat/lon → return that.
     • Else geocode the city/state in its description.
4. Cache the result for 10 min.
5. Fallback: Mar‑a‑Lago (winter) or Bedminster (summer).
"""
from __future__ import annotations
import httpx, re, datetime as dt, asyncio, functools
from bs4 import BeautifulSoup
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from dateutil import parser as dtparse, tz

# ------------------------------------------------------------------
TFR_LIST_URL = "https://tfr.faa.gov/tfr3/?page=tfr"        # HTML table
TFR_JSON_URL = "https://tfr.faa.gov/tfr3/list.json"        # fails sometimes
HOME_WINTER = dict(lat=26.6758, lon=-80.0364, name="Mar-a-Lago, FL")
HOME_SUMMER = dict(lat=40.685,  lon=-74.642,  name="Bedminster, NJ")
KEYWORDS    = ["Palm Beach", "Bedminster", "White House", "Trump Tower", "Andrews"]

# ── simple in‑memory cache ────────────────────────────────────────
_cached: dict[str, tuple[dt.datetime, dict]] = {}

def memo(seconds: int = 600):
    """Lightweight async memoization decorator (UTC‑aware timestamps)."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapped(*a, **k):
            now = dt.datetime.now(dt.timezone.utc)
            ts, val = _cached.get(fn.__name__, (dt.datetime.min.replace(tzinfo=dt.timezone.utc), None))
            if (now - ts).total_seconds() < seconds:
                return val
            val = await fn(*a, **k)
            _cached[fn.__name__] = (now, val)
            return val
        return wrapped
    return deco

geocode = RateLimiter(Nominatim(user_agent="rain-on-trump").geocode, min_delay_seconds=1)

# ── helpers ───────────────────────────────────────────────────────
VIP_RE   = re.compile(r"VIP", re.I)
COORD_RE = re.compile(r"([NS]\\d+\.\\d+),\\s*([EW-]?\\d+\.\\d+)")

@memo()
async def current_coords() -> dict:
    """Return best‑guess coordinates as a dict with lat, lon, name keys."""
    # 0️⃣   Jet seen in the last 20 minutes? → override
    plane = await _lookup_plane()
    if plane:
        return plane

    # 1️⃣   Active VIP‑TFR
    loc = await _try_json() or await _try_html()
    if loc:
        return loc

    # 2️⃣   Seasonal default
    summer = 5 <= dt.datetime.now(dt.timezone.utc).month <= 10
    return HOME_SUMMER if summer else HOME_WINTER

# ──────────────────────────────────────────────────────────────────

def _score(row: dict) -> int:
    desc = row.get("description", "")
    for i, kw in enumerate(KEYWORDS):
        if kw.lower() in desc.lower():
            return 100 - i  # higher is better
    return 0

async def _try_json() -> dict | None:
    # noinspection PyBroadException
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            now = dt.datetime.now(dt.timezone.utc)
            vip = [
                r for r in (await c.get(TFR_JSON_URL)).json()
                if VIP_RE.search(r["type"])
                and dtparse.parse(r["effectiveBegin"]) <= now <= dtparse.parse(r["effectiveEnd"])
            ]
    except Exception:
        return None
    if not vip:
        return None
    best = max(vip, key=_score)
    return await _row_to_location(best)

async def _try_html() -> dict | None:
    # noinspection PyBroadException
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            html = (await c.get(TFR_LIST_URL, headers={"User-Agent": "Mozilla"})).text
    except Exception:
        return None
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("table tbody tr")
    best, best_dt = None, dt.datetime.min
    for tr in rows:
        cols = [td.get_text(strip=True) for td in tr.select("td")]
        if len(cols) < 6 or not VIP_RE.search(cols[4]):
            continue
        issued = dt.datetime.strptime(cols[0], "%m/%d/%Y")
        if issued > best_dt:
            best_dt, best = issued, {
                "description": cols[5],
                "details_url": tr.select_one("a[href*='detail']")["href"],
            }
    return await _row_to_location(best) if best else None

async def _row_to_location(row: dict) -> dict | None:
    """Extract lat/lon from a JSON or HTML row; geocode if needed."""
    if "lat" in row and "lon" in row:
        return {"lat": row["lat"], "lon": row["lon"], "name": row.get("description", "VIP TFR")}

    # Embedded coords like "26.675N, 080.036W"
    m = COORD_RE.search(row.get("description", ""))
    if m:
        lat, lon = _to_float(m.group(1)), _to_float(m.group(2))
        return {"lat": lat, "lon": lon, "name": row["description"]}

    # Last‑resort geocode the textual place
    place = row.get("description", "").split("—")[0][:60]
    loc = geocode(place)
    if loc:
        return {"lat": loc.latitude, "lon": loc.longitude, "name": place}
    return None

# ──────────────────────────────────────────────────────────────────

def _to_float(coord: str) -> float:
    """Convert '26.6758N' or '-80.0364' to signed float."""
    coord = coord.rstrip("NnSsEeWw")
    val = float(coord.replace(" ", "").replace("N", "").replace("E", ""))
    if any(ch in coord for ch in "SsWw-"):
        val = -abs(val)
    return val

# ──────────────────────────────────────────────────────────────────

OPENSKY_TAILS      = {"N757AF", "N76DT", "N99DT"}  # known Trump jets
OPENSKY_CACHE_SEC  = 300

@memo(OPENSKY_CACHE_SEC)
async def _lookup_plane() -> dict | None:
    """Return lat/lon if a Trump jet broadcast in the last 20 min."""
    from opensky_api import OpenSkyApi

    api = OpenSkyApi()  # anonymous = 1 req/10 s
    states = api.get_states().states or []
    now = dt.datetime.now(dt.timezone.utc)
    for s in states:
        if s.callsign and s.callsign.strip() in OPENSKY_TAILS:
            ts = dt.datetime.fromtimestamp(
                s.time_position or s.last_contact,
                dt.timezone.utc,
            )
            if (now - ts).total_seconds() <= 1200:  # ≤ 20 min
                print(
                    f"[opensky] {s.callsign.strip()} lat={s.latitude:.2f} lon={s.longitude:.2f} age={(now - ts).seconds}s"
                )
                return {
                    "lat": s.latitude,
                    "lon": s.longitude,
                    "name": f"In‑flight ({s.callsign.strip()})",
                }
    return None
