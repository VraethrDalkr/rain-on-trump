# Is It Raining on Trump?

A tiny FastAPI + PWA that answers one question in real-time: **is it currently raining where Donald J. Trump is?**

The back-end derives Trump's most likely coordinates from a handful of public data sources, then asks Open-Meteo for the latest precipitation. The front-end shows a big **🌧 YES** or **☀ NO**, the rain rate in mm/h, and a provenance link.

**🔗 Live:** https://rain-on-trump.pages.dev/

Get notified when it rains. What you do with that information — smile or pray — is entirely up to you.

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

# 3 open the PWA
xdg-open frontend/index.html   # or serve the folder
```

FastAPI listens on **[http://127.0.0.1:8000](http://127.0.0.1:8000)**; the JS front-end auto-detects `localhost` and proxies all API calls there.

### Required env vars

| Variable          | Purpose                                                              |
| ----------------- | -------------------------------------------------------------------- |
| `VAPID_PUBLIC`    | WebPush public key (URL-safe Base64)                                 |
| `VAPID_PRIVATE`   | WebPush private key                                                  |
| `BROADCAST_TOKEN` | Secret query param for `/broadcast` endpoint                         |
| `PUSH_DATA_DIR`   | Where to store subscription JSON (`/data` on Fly, `local_data/` dev) |

Generate VAPID keys once:

```bash
python - <<'PY'
from pywebpush import generate_vapid_private_key, generate_vapid_public_key
priv = generate_vapid_private_key()
print('VAPID_PRIVATE=', priv)
print('VAPID_PUBLIC =', generate_vapid_public_key(priv))
PY
```

---

## Data sources & confidence

### Location Sources

| Order | Source                          | Example                      | Confidence                       |
| ----- | ------------------------------- | ---------------------------- | -------------------------------- |
| 1️⃣   | **ADS-B (OpenSky → adsb.fi)**   | plane callsign / lat-lon     | 95 (air), 80-90 (ground)         |
| 1.5️⃣ | **Overnight base inference**    | DC evening + DC morning      | 58                               |
| 2️⃣   | **Factba.se JSON calendar**     | latest event with a location | 70 → 30 **linear decay** over 72h |
| 3️⃣   | ~~FAA VIP/SECURITY-TFR JSON~~   | _(disabled 2025-12)_         | ~~40~~                           |
| 4️⃣   | **Newswire (GDELT)**            | dateline coords              | 35                               |
| 5️⃣   | **Last-arrival cache**          | last plane arrival (<7 d)    | 30                               |

> **Note:** FAA TFR API was disabled in December 2025 after the endpoint changed to return HTML instead of JSON.

#### Overnight Location Inference

During overnight hours (9PM–8AM Eastern), if the last evening event and next morning
event are both in the same region, the system infers the overnight base location:

- **DC area events** → White House (confidence 58)
- **Florida events** → Mar-a-Lago (confidence 58)
- **New Jersey events** → Bedminster (confidence 58)

This heuristic is based on calendar analysis showing ~85% accuracy for same-region
evening→morning patterns. It fills the gap between a past event ending and the next
morning event starting. Bedminster serves as the "Summer White House" (May–October).

### Weather & Thunderstorm Detection

- **Weather:** [Open-Meteo](https://open-meteo.com/) provides current precipitation (rain **and** snow)
- **Thunderstorm:** Open-Meteo weather codes (WMO 95-99) indicate thunderstorm activity
  - Code 95: Moderate thunderstorm
  - Codes 96, 97, 99: Severe thunderstorm (with hail)

If nothing fresh <72 h exists the app shows **🤷 Unknown** plus the last known location.

### Limitations

This is a **best-effort estimate**, not real-time surveillance:

- **Location accuracy:** Derived from public data; confidence decays 70%→30% over 72h
- **Weather granularity:** Hourly data from Open-Meteo; brief showers may be missed
- **In-flight:** No weather check while airborne—reports "not raining" until landing
- **Unknown state:** Displays "Unknown" when no data is fresher than 72 hours

---

## API reference

| Method & path                      | Description                                                 |
| ---------------------------------- | ----------------------------------------------------------- |
| `GET  /is_it_raining.json`         | Main endpoint. `?lat=&lon=` overrides auto-location (debug) |
| `GET  /plane_state.json`           | Raw ADS-B aircraft state                                    |
| `POST /subscribe`                  | WebPush subscription (from service-worker)                  |
| `POST /broadcast` **protected**    | `?msg=...&token=<BROADCAST_TOKEN>` manual push              |
| `POST /cleanup_subscriptions`      | `?token=...&max_days=365` remove old subscriptions          |
| `GET  /subscription_stats`         | `?token=...` subscription statistics                        |
| `GET  /healthz`                    | Health probe (returns "ok")                                 |
| `GET  /debug`, `/debug.json`       | Pretty & raw traces (for debugging)                         |

### API Response (`/is_it_raining.json`)

```json
{
  "precipitating": true,
  "mmh": 2.5,
  "thunderstorm": true,
  "thunderstorm_state": "moderate",
  "coords": {"lat": 26.67, "lon": -80.03, "name": "Mar-a-Lago"},
  "timestamp": "2025-12-03T14:30:00+00:00"
}
```

---

## Running the test-suite

```bash
pytest -q            # offline-safe unit tests
black --check .      # formatting
flake8               # lint
```

All network calls (OpenSky, geocoding, weather, WebPush) are stubbed, so CI remains fast and deterministic.

---

## Deploying to Fly.io

```bash
fly launch                           # create app + volume the first time
fly secrets set \
  VAPID_PUBLIC=... \
  VAPID_PRIVATE=... \
  BROADCAST_TOKEN=...

fly deploy                           # push a new version
```

`fly.toml` mounts a small volume at **/data** for push subscriptions.

---

## License & Credits

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).

### Data Source Attributions

This project uses the following external data sources:

* **Weather data by [Open-Meteo.com](https://open-meteo.com/)** – precipitation & thunderstorm detection
* **Geocoding © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors** via [Nominatim](https://nominatim.org/)
* [OpenSky Network](https://opensky-network.org/) – ADS-B flight tracking
* [adsb.fi](https://adsb.fi/) – backup ADS-B feed
* [Factba.se Calendar](https://rollcall.com/factbase/trump/topic/calendar/) – public schedule data

---

**About / contact:** [https://rain-on-trump.pages.dev/about](https://rain-on-trump.pages.dev/about)
