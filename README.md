# GPS / GLONASS / BeiDou / Galileo Calculator

A Flask web app for GNSS satellite data, position calculation, and real-time 3D sky visualization. It combines GPS YUMA/RINEX navigation data with GLONASS, BeiDou, and Galileo TLE/RINEX 4 data, then exposes both browser views and JSON APIs.

## Features

- **Multi-constellation support** — GPS, GLONASS, BeiDou, and Galileo in the calculator and live views.
- **Position calculation** — GPS uses YUMA almanac propagation; GLONASS/BeiDou/Galileo use Celestrak TLEs with SGP4. GLONASS live positions prefer RINEX 4 FDMA broadcast state vectors when loaded.
- **Live 3D sky view** — Cesium-based Earth view with constellation toggles, receiver location, RF line-of-sight masking, ionosphere/system-time panels, and satellite-detail overlays.
- **Legacy calculator UI** — Form-based calculator for loading data, listing satellites, and calculating an individual satellite position.
- **Broadcast-data panels** — GPS LNAV/CNAV/CNV2, GLONASS FDMA, BeiDou D1/D2/CNV1/CNV2/CNV3, Galileo I/NAV/F/NAV, ionosphere, and system-time data when cached files are present.
- **Refresh workflows** — Data can refresh in background workers, by local script, by admin API, or by the hourly GitHub Actions workflow.

## Local setup

```bash
pip install -r requirements.txt
python main.py
```

Open <http://localhost:5000>. `main.py` starts the Flask app and the background cache-refresh workers. To run without network/background workers:

```bash
$env:GPS_CALCULATOR_NO_WORKERS = "1"   # PowerShell
python main.py
```

## Templates and browser routes

- `GET /` and `GET /cesium` render `templates/cesium.html`, the current primary live sky view.
- `GET /calculator` renders `templates/index.html`, the form-based calculator.
- `templates/live.html` is retained as an older Three.js live-view template and is not currently bound to a Flask route.
- The Cesium page's CSS/JS live in `static/css/cesium.css` and `static/js/cesium.js` (moved out of inline `<style>`/`<script>` blocks so they can be cached/versioned independently). The template references them through an `asset_url()` helper that appends a `?v=<mtime>` cache-busting suffix.
- Every response carries a `Content-Security-Policy` (scoped to same-origin + the CesiumJS CDN) plus `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Permissions-Policy` headers, set in `app.py`'s `_add_security_headers` after-request hook.

## Project layout

```text
.
├── app.py                    # Flask routes, caches, API endpoints, workers
├── main.py                   # Production/local entry point; starts workers
├── gps_core.py               # GNSS parsing/propagation helpers
├── refresh_data.py           # Local/API data refresh implementation
├── openapi.yaml              # Versioned API schema for current endpoints
├── data/                     # Cached TLE, RINEX, and parsed JSON data
├── templates/
│   ├── cesium.html           # Primary Cesium UI
│   ├── index.html            # Calculator UI
│   └── live.html             # Older Three.js live view, not routed
├── static/
│   ├── css/cesium.css        # Extracted styles for the Cesium UI
│   └── js/cesium.js          # Extracted script for the Cesium UI
├── Dockerfile                # Gunicorn container for Fly.io/other platforms
├── Procfile                  # Render-style Gunicorn command
└── .github/workflows/
    ├── update-tles.yml       # Hourly TLE/RINEX refresh and commit
    ├── fly-deploy.yml        # Fly.io deployment
    └── static.yml            # GitHub Pages static publish
```

## Data pipeline and refresh workflow

Data files live under `data/` and are safe to refresh independently of app code.

- **TLEs**: `glo-ops.tle`, `beidou.tle`, `galileo.tle`, and `gps.tle` are fetched from Celestrak.
- **GPS LNAV**: `gps_rinex2.txt` is fetched from NOAA CORS RINEX 2 broadcast files.
- **RINEX 4 multi-GNSS**: `gps_rinex4.txt` is fetched from BKG IGS broadcast files and pre-parsed into JSON files such as `rinex4_gps_cnav.json`, `rinex4_bds_cnv1.json`, `rinex4_gal_inav.json`, `rinex4_glo_fdma.json`, `rinex4_iono.json`, and `rinex4_systime.json`.
- **Background workers**: `start_background_workers()` starts TLE, RINEX 2, and RINEX 4 workers unless `GPS_CALCULATOR_NO_WORKERS=1` or Flask testing mode is set.
- **Local refresh**: run `python refresh_data.py` to fetch TLE/RINEX data and rewrite `data/` atomically.
- **Admin API refresh**: `POST /api/refresh-data` runs the same refresh and reloads in-memory caches; it requires `ADMIN_TOKEN` unless `GPS_ALLOW_UNAUTHENTICATED_ADMIN=1` is explicitly set for local use.
- **CI refresh**: `.github/workflows/update-tles.yml` runs hourly and on demand, refreshes data, pre-parses RINEX 4 JSON, and commits changed `data/` files.
- **Browser fallback**: `/api/fetch-tles` reports whether cached TLEs are ready; `/api/push-tles` lets the browser provide checked TLE text for GLONASS/BeiDou/Galileo when the server cannot reach Celestrak.

## API schema

The versioned OpenAPI description is in [`openapi.yaml`](openapi.yaml). It documents the currently implemented endpoints and the stable JSON error shape used by API routes:

```json
{ "error": "human-readable message" }
```

Common error statuses are `400` for invalid input or missing loaded data, `401` for admin-auth failures, `404` for missing requested resources, `413` for rejected TLE payloads, `503` for disabled/unavailable upstream/cache operations, and JSON `500` errors for unhandled `/api/*` failures.

## API examples

### Load data: `POST /api/load-almanac`

GPS loads a NAVCEN YUMA almanac for `date` (`YYYY-MM-DD`) or `today` (internally uses recent data):

```bash
curl -s -X POST http://localhost:5000/api/load-almanac \
  -H "Content-Type: application/json" \
  -d '{"constellation":"GPS","date":"2026-04-22"}'
```

```json
{ "success": true, "count": 32, "week": 2416, "toa": 319488, "constellation": "GPS" }
```

GLONASS, BeiDou, and Galileo load current Celestrak TLEs and do not require a date:

```bash
curl -s -X POST http://localhost:5000/api/load-almanac \
  -H "Content-Type: application/json" \
  -d '{"constellation":"GALILEO"}'
```

```json
{ "success": true, "count": 31, "constellation": "GALILEO", "source": "Celestrak (current TLEs)" }
```

### List loaded satellites: `GET /api/satellites`

```bash
curl -s "http://localhost:5000/api/satellites?constellation=BEIDOU"
```

```json
{ "constellation": "BEIDOU", "date": "2026-09-11", "satellites": [ { "id": 29, "label": "C29", "name": "BEIDOU-3 M21 (C29)", "health": "Healthy" } ] }
```

### Calculate one position: `POST /api/calculate`

```bash
curl -s -X POST http://localhost:5000/api/calculate \
  -H "Content-Type: application/json" \
  -d '{"constellation":"GPS","prn":1,"time":"2026-04-22 12:00:00"}'
```

```json
{
  "prn": 1,
  "label": "G01",
  "constellation": "GPS",
  "time": "2026-04-22 12:00:00",
  "ecef": { "x": "15,123,456", "y": "-20,123,456", "z": "11,123,456", "r": "26,560.0" },
  "geodetic": { "latitude": "23.4567", "longitude": "-53.2100", "altitude": "20,200.0" }
}
```

Use `"time":"now"` to calculate at the current server time.

### Live positions: `GET /api/live-positions`

```bash
curl -s "http://localhost:5000/api/live-positions?at=2026-09-11T10:00:00Z"
```

```json
{
  "time": "2026-09-11 10:00:00 UTC",
  "time_iso": "2026-09-11T10:00:00Z",
  "tos": 36000.0,
  "frozen": true,
  "almanac_date": "2026-09-09",
  "satellites": [ { "prn": 1, "label": "G01", "constellation": "GPS", "healthy": true, "x": 15123456.0, "y": -20123456.0, "z": 11123456.0, "lat": 23.4567, "lon": -53.21, "alt_km": 20200.0 } ]
}
```

### Satellite detail: `GET /api/satellite-detail`

```bash
curl -s "http://localhost:5000/api/satellite-detail?constellation=GLONASS&label=R01"
```

```json
{ "label": "R01", "constellation": "GLONASS", "tle": { "name": "COSMOS ...", "line1": "1 ...", "line2": "2 ..." }, "glonass_fdma": { "toc": "2026-09-11T00:00:00", "freq_num": 1, "health": 0 } }
```

GPS details may include `almanac`, `ephemeris`, `cnav`, and `cnv2`; BeiDou details may include `beidou_d`, `beidou_cnv1`, `beidou_cnv2`, and `beidou_cnv3`; Galileo details may include `galileo_inav` and `galileo_fnav`.

### RINEX/cache status: `GET /api/rinex-status`

```bash
curl -s http://localhost:5000/api/rinex-status
```

```json
{ "lnav": { "loaded": true, "date": "2026-09-11", "prns": 31 }, "cnav": { "loaded": true, "date": "2026-09-10", "prns": 12 }, "rinex4_diag": { "stage": "done", "last_error": null } }
```

### Almanac-style summaries

```bash
curl -s http://localhost:5000/api/gps-almanac
curl -s http://localhost:5000/api/glonass-almanac
curl -s http://localhost:5000/api/beidou-almanac
curl -s http://localhost:5000/api/galileo-almanac
```

Each endpoint returns per-slot/per-PRN summaries under `slots` plus a date or fetch timestamp.

### Ionosphere and system time

```bash
curl -s http://localhost:5000/api/ionosphere
curl -s http://localhost:5000/api/system-time
```

`/api/ionosphere` returns Klobuchar, NeQuick-G, and BDGIM broadcast coefficients when available. `/api/system-time` returns RINEX 4 STO records, EOP, leap-second data, date, and file `mtime`.

### Data freshness: `GET /api/data-freshness`

```bash
curl -s http://localhost:5000/api/data-freshness
```

```json
{ "gps_lnav": 1789123456, "gps_cnav": 1789123457, "tle_glo": 1789123458, "tle_bds": 1789123458, "tle_gal": 1789123458 }
```

Values are Unix mtimes or `null` for missing files.

### TLE cache browser fallback

```bash
curl -s -X POST http://localhost:5000/api/fetch-tles \
  -H "Content-Type: application/json" \
  -d '{"constellation":"BEIDOU"}'
```

```json
{ "success": true, "count": 49 }
```

```bash
curl -s -X POST http://localhost:5000/api/push-tles \
  -H "Content-Type: application/json" \
  -d '{"constellation":"BEIDOU","text":"BEIDOU-3 M21 (C29)\n1 ...checksum\n2 ...checksum\n"}'
```

```json
{ "success": true, "count": 1, "constellation": "BEIDOU" }
```

`push-tles` accepts only GLONASS, BeiDou, and Galileo, enforces size/count limits, validates TLE checksums, and maps identities to stable R/C/E labels.

### Admin refresh: `POST /api/refresh-data`

```bash
curl -s -X POST http://localhost:5000/api/refresh-data \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

```json
{ "ok": true, "complete": true, "partial_failures": [], "summary": { "rinex2": true, "rinex4": true }, "log": ["→ TLEs", "Done."] }
```

## Deployment

### Fly.io / Docker

The included `Dockerfile` runs Gunicorn on port `8080` with one worker and four threads. `fly.toml` maps Fly's HTTP service to the container port, forces HTTPS, and keeps one machine running by default. Deploy with the Fly workflow or locally:

```bash
fly launch --no-deploy   # first-time app setup, if needed
fly deploy
```

Set `FLY_API_TOKEN` in GitHub secrets for `.github/workflows/fly-deploy.yml`.

### Render-style Procfile

`Procfile` contains:

```text
web: gunicorn --workers 1 main:app
```

Use a single worker because satellite and RINEX data are held in process-local memory caches. Multiple workers would not share freshly loaded data.

### Environment variables

- `ADMIN_TOKEN` — enables and secures `POST /api/refresh-data`.
- `GPS_ALLOW_UNAUTHENTICATED_ADMIN=1` — local-only opt-in that allows admin refresh without `ADMIN_TOKEN`.
- `GPS_CALCULATOR_NO_WORKERS=1` — disables background refresh workers.

## Technical notes

- GPS YUMA almanacs are fetched from NAVCEN and propagated with Kepler iteration.
- GLONASS/BeiDou/Galileo TLEs are fetched from Celestrak and propagated with SGP4 plus ECI→ECEF conversion.
- RINEX 4 parsing is pre-computed into smaller JSON files to avoid expensive startup/background parsing on small hosts.
- RF horizon masking uses Earth geometry so elevated receivers can see satellites below a sea-level horizon.
- API 500 responses under `/api/*` are JSON; non-API HTML routes use Flask's normal behavior.

## License

Public domain.
