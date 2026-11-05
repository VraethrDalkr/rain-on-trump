# Is It Raining on Trump?

A FastAPI backend and a PWA that answer one question: **is it raining where Donald J. Trump is right now?**

The back-end works out Trump's most likely location from public data (aircraft tracking and his published schedule), then asks Open-Meteo whether it is raining or snowing there. The front-end answers with a big **YES** or **NO** over an animated sky, shows the rain and snow rates (mm/h and cm/h, or in/h), and explains where the location comes from and how sure it is.

**Live:** https://rain-on-trump.pages.dev/

Turn on notifications to hear when it starts or stops raining on him.

---

## Quick-start (local dev)

```bash
# clone & create venv
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 1 prepare env-vars (see below)
cp .env.template .env  &&  $EDITOR .env

# 2 run the API (hot-reload)
uvicorn backend.app.main:app --reload

# 3 serve the PWA (ES modules need http://, not file://)
python -m http.server 8090 --directory frontend
```

Open **[http://127.0.0.1:8090](http://127.0.0.1:8090)**. FastAPI listens on **[http://127.0.0.1:8000](http://127.0.0.1:8000)**; the front-end detects `localhost` / LAN hostnames and sends its API calls to port 8000 on the same host.

### Env vars

| Variable          | Purpose                                                              |
| ----------------- | -------------------------------------------------------------------- |
| `VAPID_PUBLIC`    | WebPush public key (URL-safe Base64), required                       |
| `VAPID_PRIVATE`   | WebPush private key, required                                        |
| `BROADCAST_TOKEN` | Admin token, sent in the `X-Admin-Token` header, required            |
| `PUSH_DATA_DIR`   | Optional. Data dir, default `/data` (the Fly volume). A relative value such as `local_data` resolves against `backend/` |

Generate VAPID keys once:

```bash
python - <<'PY'
import base64
from cryptography.hazmat.primitives import serialization as s
from py_vapid import Vapid

def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

v = Vapid()
v.generate_keys()
print("VAPID_PRIVATE=" + b64(v.private_key.private_numbers().private_value.to_bytes(32, "big")))
print("VAPID_PUBLIC=" + b64(v.public_key.public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint)))
PY
```

Put the same public key in `VAPID_PUBLIC` in `frontend/js/notify.js`. The two
must match, and changing the keys invalidates every existing subscription.

---

## Data sources & confidence

### How the location is found

Every source produces **claims**: "he was at place P at time t", each with a
reliability between 0 and 1. The answer is the **newest** claim whose
reliability is at least 0.6 (a trusted claim); weaker claims only fill gaps
when no trusted claim exists. A claim stops counting once a newer trusted
claim places him more than 60 km away.

| Source | Claim | Reliability |
| ------ | ----- | ----------- |
| **ADS-B (adsb.fi → adsb.lol)** | one of his aircraft (VC-25A, the 747-8 stopgap, C-32A, his private 757) airborne, on the ground, or a detected landing | up to 1.0, scaled by aircraft identity (below) |
| **ADS-B, track lost in flight** | still airborne until the estimated arrival (at most 14 h) | 0.9 × identity; only for corroborated or transit flights |
| **Factba.se schedule**, known venue | event start time and place | 0.82 (timed), 0.5 (date only) |
| **Factba.se schedule**, geocoded venue | event start time and place | 0.7; 0.45 if implausible given recent context |
| **Factba.se schedule**, "departs X for Y" (more than 150 km) | in flight to Y until the "arrives" item or the estimated arrival | 0.85 |
| **Overnight base inference** | White House / Mar-a-Lago / Bedminster overnight | 0.9 |
| Press pool calls, phone interviews | event start | 0.5 (never moves the answer) |
| ~~FAA VIP/SECURITY TFRs~~ | _(disabled 2025-12: the FAA endpoint no longer returns JSON)_ | |
| ~~Newswire (GDELT)~~ | _(disabled: never useful in practice)_ | |

**Aircraft identity.** Several aircraft can fly as Air Force One. A flight
counts in full (1.0) only when it is corroborated: the "AF1" callsign was
seen, a matching departure or arrival appears on the schedule within 3 h and
60 km, or it is the aircraft he was last known to use. An uncorroborated
transit flight (take-off and current position more than 150 km apart) counts
0.75, which is still trusted; a short local flight (training, for example)
counts 0.4 and only fills gaps. The C-32A (the smaller
757-based aircraft) is only tracked while it broadcasts "AF1".

**Schedule venues.** Common venues come from a hand-curated alias list. Other
venues are geocoded in the background with OpenStreetMap Nominatim; among
several candidates, physically impossible ones (given his recent known
positions and a 1000 km/h travel limit) are discarded and the one nearest to
the surrounding schedule wins. Items that don't say where he is (press "lids",
cancelled events, items for the First Lady or the Vice President alone) are
ignored.

**Overnight inference.** Between 9 PM and 8 AM Eastern, if the latest evening
timed event (after 5 PM) and the next morning timed event (before noon) are
both within 80 km of the same base (Washington, Palm Beach or Bedminster), he
is placed at the White House, Mar-a-Lago or Bedminster respectively.

**Confidence** shown in the app = reliability × how long ago the claim was
made: full for 3 h, then 65% at 24 h, 47% at 72 h and 20% at 7 days, never
above 95%. So a fresh timed schedule item shows 82%. The number is for display
only; it never picks between trusted claims.
Near one of the three bases the age penalty stops at 50% (he tends to stay).
Claims older than 7 days are dropped. If nothing usable is left and nothing is
scheduled in the next 24 h, the app shows **???** (unknown) with the last place
he was seen. An answer is marked as getting old once its confidence drops
below 30% or it is more than 72 h old.

### Weather

- **Precipitation:** [Open-Meteo](https://open-meteo.com/) 15-minute data. It
  counts as raining (or snowing) when any of the **last four completed
  15-minute steps** had measurable rain (or snow), i.e. from the first wet
  quarter-hour until a full dry hour. The rate shown is the total over that
  hour. If 15-minute data is missing, the previous hour is used.
- **Thunderstorm:** the hourly Open-Meteo WMO weather code: 95 = thunderstorm,
  96, 97 or 99 = severe thunderstorm (hail).
- **In flight:** no weather check while the claim says he is airborne; the
  answer is "NO" until landing.

### Push notifications

Subscribers get a WebPush notification when rain or snow starts or stops
where he is (including rain turning to snow and back), and when a
thunderstorm starts, turns severe or passes. Settings has three switches:
rain starts, rain stops, and thunderstorms. Thunderstorm alerts ride on the
rain switches: a storm starting or turning severe needs "rain starts" on, a
storm passing needs "rain stops" on. A move of more than 25 km without a flight (e.g. a schedule
update) adopts the new place's weather silently instead of announcing a change.

### Limitations

The answer is a best-effort estimate from public data:

- **Location:** depends on the public schedule and ADS-B coverage. The
  schedule is sometimes late or wrong, and the aircraft isn't always visible
- **Weather:** forecast-model data, with no gauge at his location. Very short
  showers can be missed, and the answer can run about 20 minutes behind
  (thunderstorms up to an hour, since they come from hourly data)
- **In flight:** reported as "not raining" until landing

---

## API reference

| Method & path                      | Description                                                 |
| ---------------------------------- | ----------------------------------------------------------- |
| `GET  /is_it_raining.json`         | Main endpoint. `?lat=&lon=` overrides auto-location (debug) |
| `GET  /plane_state.json`           | Raw ADS-B aircraft state                                    |
| `POST /subscribe`, `DELETE /subscribe` | WebPush subscribe / unsubscribe                          |
| `POST /broadcast` **protected**    | `?msg=...` manual push (header `X-Admin-Token`); `&notification_type=` limits it to subscribers with that alert on |
| `PATCH /preferences`               | Update a subscription's alert switches (`rain_start`, `rain_stop`, `thunderstorm`) |
| `POST /cleanup_subscriptions`      | `?max_days=365` remove subscriptions older than that which never received a notification (admin header) |
| `GET  /subscription_stats`         | subscription statistics (admin header)                      |
| `GET  /healthz`                    | Health probe: "ok", or 503 if the background loop stalled   |
| `GET  /debug`, `/debug.json`       | Pretty & raw decision traces (6/min per IP)                 |
| `GET  /debug/history[.json]`       | Rolling debug snapshots (last 7 days)                       |
| `GET  /debug/geocode[.json]`       | Rolling geocoding log                                       |

Admin routes take the token in the `X-Admin-Token` header (the `?token=`
query parameter still works but is deprecated: query strings end up in access
logs). They are limited to 10/min per IP.

### API Response (`/is_it_raining.json`)

```json
{
  "precipitating": true,
  "mmh": 2.5,
  "precipitation_type": "rain",
  "snow": 0.0,
  "thunderstorm": true,
  "thunderstorm_state": "moderate",
  "sunrise": "2026-11-04T06:38",
  "sunset": "2026-11-04T17:04",
  "sunrise_utc": "2026-11-04T11:38:00Z",
  "sunset_utc": "2026-11-04T22:04:00Z",
  "utc_offset_seconds": -18000,
  "wind_kmh": 12.4,
  "coords": {
    "lat": 38.8977, "lon": -77.0365, "name": "The White House",
    "reason": "calendar_alias", "confidence": 81, "age_h": 3.75,
    "observed_at": "2026-11-04T20:30:00+00:00", "stale": false,
    "source_display": "Factba.se schedule, 3 h ago",
    "evidence": [{"t": "…", "place": "…", "kind": "…", "source": "…", "current": true}]
  },
  "timestamp": "2026-11-05T00:15:23+00:00"
}
```

`sunrise` / `sunset` are local times at the location; `wind_kmh` is `null`
in flight or when unknown. `coords.evidence` is the "how we know" list (up to
four recent places, oldest first). When the location is unknown, `coords` is
`{"unknown": true, "last_known": {...}}` without `lat` / `lon`.

---

## Running the test-suite

```bash
pytest -q            # offline-safe unit tests
node --test 'tests/frontend/*.test.mjs'   # front-end view-model tests
black --check .      # formatting
flake8               # lint
```

All network calls (ADS-B, geocoding, weather, WebPush) are stubbed, so the unit tests run offline. `INTEGRATION_TESTS=1 pytest tests/integration/` runs the live API checks.

---

## Deploying to Fly.io

```bash
# first time only: create the app (fly.toml names it) and its volume
fly apps create rain-api
fly volumes create push_data --region yul
fly secrets set \
  VAPID_PUBLIC=... \
  VAPID_PRIVATE=... \
  BROADCAST_TOKEN=...

fly deploy                           # push a new version
```

After the first deploy on a new volume, run once:
`fly ssh console -C "chown -R rainuser /data"`. Fly mounts the volume as root;
without this the app can't write to `/data` and falls back to non-persistent
storage (it logs "Persist dir /data is not writable").

`fly.toml` mounts a small volume at **/data** for push subscriptions, flight history, the geocode cache and debug snapshots.

---

## License & Credits

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).

### Data Source Attributions

* [Open-Meteo](https://open-meteo.com/) – weather data
* [OpenStreetMap](https://www.openstreetmap.org/copyright) / [Nominatim](https://nominatim.org/) – geocoding
* [adsb.fi](https://adsb.fi/) – ADS-B flight tracking
* [adsb.lol](https://adsb.lol/) – backup ADS-B feed
* [Factba.se](https://rollcall.com/factbase/trump/topic/calendar/) – public schedule data

---

**About / contact:** [https://rain-on-trump.pages.dev/about](https://rain-on-trump.pages.dev/about)
