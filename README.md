# CreditLens + GPS / GLONASS Calculator

The home page is now a Hebrew, RTL CreditLens credit-risk demo. It aggregates
customer records from multiple in-app source adapters, calculates an explainable
1–10 risk score, highlights policy exceptions, and shows the regulatory request
status. The customer data and Bank of Israel response are sandbox fixtures:
connectors must be replaced with authenticated, consent-based integrations
before production use.

A web-based satellite position calculator with real-time 3D visualization of GPS and GLONASS constellations.

## Features

- **GPS Almanac Calculator** — Load YUMA almanac data from NAVCEN and calculate satellite positions
- **GLONASS Support** — Fetch current GLONASS TLEs from Celestrak and propagate positions via SGP4
- **Live Sky View** — Real-time 3D Earth with moving GPS & GLONASS satellites, receiver location input, and RF line-of-sight masking
- **Dynamic Receiver** — Set receiver location (lat/lon/altitude) and see which satellites are in view
- **Horizon Masking** — The Earth's surface acts as the only obstruction; higher altitudes reveal more satellites below the horizon
- **Interactive 3D** — Drag to rotate, scroll to zoom, hover for satellite details

## Local Setup

```bash
cd gps-calculator
pip install -r requirements.txt
python main.py
```

Open **http://localhost:5000** in your browser.

`main.py` now starts with **Waitress** by default (production-like WSGI).  
For explicit debug mode only: set `CREDIT_DEBUG=1` before launch.

## Desktop app

Double-click the **CreditLens** shortcut on the Desktop or Start Menu (or run
`CreditLens.vbs`). The launcher starts the Flask server hidden via `pythonw`
if it is not already running, waits until it responds, and opens the system in
a dedicated Edge app window with the CreditLens icon. To reinstall shortcuts,
recreate them pointing at `CreditLens.vbs` with `static/creditlens.ico`.

## Credit decision API

`POST /api/credit/assess` accepts `{ "customer_id": "CUS-10482", "amount": 100000 }`
(name or ID works) and returns a 1–10 risk score (low = good), weighted
criteria, scanned sources, exceptions, hard compliance blocks, and a traceable
sandbox regulatory request. Unknown customers return HTTP 404. The GPS
calculator remains available at `/cesium`.

`GET /api/credit/customers` lists demo customers for the picker.

## Security

All CreditLens pages and APIs require login (session cookie, PBKDF2-hashed
passwords, 5-attempt lockout). Roles: `underwriter` (assess/search/consent),
`risk_manager` (adds decision history + compliance policy), `admin` (adds user
management at `/api/users`). The audit operator is always taken from the
logged-in session. Demo users: `dan.k/Loan!2026`, `rachel.r/Risk!2026`,
`5331/5331` (admin) — replace with the bank IdP (SSO) before production, and
serve behind TLS.

## Stage 4: Operational hardening

- **Health probes**: `GET /healthz` (liveness), `GET /readyz` (readiness + index/db checks).
- **Backups**: `python scripts/backup_creditlens.py` writes timestamped snapshots to `backups/`.
- **Maintenance**: `python scripts/maintenance_creditlens.py` runs compliance retention purge + SQLite VACUUM.
- **Smoke checks**: `python scripts/smoke_check.py` validates login, role gates, scoring flow and readiness.

## Stage 5: Business layer

- **Portfolio report** (`/portfolio`, risk manager): decision volume, approval mix,
  override rate, approved exposure, score distribution, top adverse-action reasons,
  daily trend and underwriter load — all computed from the immutable decision log
  (`GET /api/credit/portfolio?days=90`).
- **Batch pre-screen** (`POST /api/credit/batch`): scores up to 500 named customers or
  a sample of up to 5,000 customers from the index in one pass, for book review and
  pre-approved campaigns. Pre-screening is an internal risk measurement: it is never
  written to the decision log and is never a customer-facing answer.
- **Audit export** (`GET /api/credit/decisions.csv?days=365`): UTF-8 BOM CSV of the
  decision log for regulators and internal audit (opens correctly in Excel in Hebrew).
- **Customer decision notice** (`/decision/<decision_id>`): printable A4 letter with the
  outcome, standardized reason codes, exceptions, score breakdown, the sources consulted,
  the customer's rights, and — where judgment override was applied — the red exception
  notice. Print/Save-as-PDF ready.
- **User administration** (`/admin`, admin only): create users, see roles, lockouts and
  the authentication event log.
- Navigation links are role-gated in the UI and enforced server-side.

In the normal bank flow, no upload is needed. The engine incrementally ingests
every supported file in `CREDIT_SOURCE_DIRECTORY` (default: `credit_data/`) into
a persistent, customer-keyed SQLite index. New and modified files are reindexed
automatically; unchanged spreadsheets are not reparsed. Each application then
queries only that customer's records while retaining source-file and row-level
traceability. Set `CREDIT_INDEX_DATABASE` to move the index from its default
`credit_index.sqlite3` location.

For a production bank deployment, point the source directory at read-only
governed storage. At larger scale, the same ingestion contract should target
the bank's approved warehouse/search platform instead of local SQLite; the
request-time scoring flow remains an indexed customer lookup, never a full
spreadsheet scan.

The demo generator creates 20 XLSX source systems with 20,000 synthetic
customers each — 400,000 rows total (`python generate_demo_data.py`). The UI
search box queries the live index by Hebrew name or `CUS-` ID prefix
(`GET /api/credit/customers?q=...`); pick any of the 20,000 customers and the
engine retrieves their records from all 20 files in milliseconds.

## File Structure

```
gps-calculator/
├── main.py                    # Flask entry point
├── app.py                     # Flask routes & API endpoints
├── gps_core.py                # GPS/GLONASS propagation (Kepler, SGP4, WGS84)
├── requirements.txt           # Python dependencies
├── Procfile                   # Render deployment config
├── scripts/
│   ├── backup_creditlens.py   # Consistent SQLite backup snapshots
│   ├── maintenance_creditlens.py # Retention purge + VACUUM
│   └── smoke_check.py         # Auth/scoring/roles smoke tests
├── templates/
│   ├── login.html             # CreditLens authentication UI
│   ├── index.html             # Credit risk workspace (RTL Hebrew)
│   ├── validation.html        # Model validation dashboard
│   ├── portfolio.html         # Portfolio report + batch pre-screen
│   ├── admin.html             # User & permission administration
│   ├── decision_letter.html   # Printable customer decision notice
│   └── live.html              # 3D Sky View (Three.js + WebGL)
```

## API Endpoints

### `POST /api/load-almanac`
Load GPS YUMA or GLONASS TLE data.

**Request:**
```json
{ "constellation": "GPS", "date": "2026-04-22" }
{ "constellation": "GLONASS" }
```

**Response:** `{ "success": true, "count": N, "constellation": "...", ... }`

### `GET /api/satellites?constellation=GPS`
List loaded satellites.

### `POST /api/calculate`
Calculate satellite position at a given time.

**Request:**
```json
{ "prn": 1, "time": "2026-04-22 12:00:00", "constellation": "GPS" }
```

**Response:** `{ "prn": 1, "label": "G01", "ecef": {...}, "geodetic": {...}, ... }`

### `GET /api/live-positions`
Real-time positions of all GPS & GLONASS satellites (auto-loads data if needed).

## Deploy to Render (permanent public link)

This repo includes a `render.yaml` blueprint, so the whole app (CreditLens +
the GPS calculator) can be deployed with one click and gets a stable
`https://<name>.onrender.com` URL that stays online — no local machine,
no tunnel, no timer.

1. **One-click deploy:** open
   **https://render.com/deploy?repo=https://github.com/kobirosenthal-commits/gps-calculator**
   and sign in with GitHub (free, no credit card).
2. Render reads `render.yaml`, provisions a free **Web Service**, installs
   `requirements.txt`, and starts `gunicorn ... main:app` — the same
   command as running it locally.
3. After the build finishes (a few minutes) the app is live at the Render
   URL shown in the dashboard. Share that link — it works for anyone,
   anytime, without your computer being on.
4. Every `git push` to `main` auto-deploys a new version.

Manual setup instead of the button works the same way:
- Render dashboard → **New → Web Service** → connect this repo → it
  auto-detects `Procfile`/`render.yaml` → **Deploy**.
- No secrets are required for the demo; the login/session key and demo
  users are created automatically on first start.

**Free-tier limits to know before a sales demo:**
- The free instance **spins down after ~15 minutes idle** and takes
  ~30–50 seconds to wake up on the next visit — open the link a minute
  before a meeting starts.
- The filesystem is **ephemeral on redeploy**: decision history, consents
  and validation runs reset when a new version is deployed (not on every
  wake-up, only on redeploy). The 20 demo Excel sources and 20,000
  synthetic customers are committed to the repo, so they are always
  present after every deploy.
- For an always-warm instance with persistent disk (real decision
  history that survives deploys), upgrade the Render service to a paid
  plan — no code changes needed.

## Technical Notes

- **GPS Almanac**: Fetched from NAVCEN (https://www.navcen.uscg.gov), parsed via regex on abbreviated YUMA field names, propagated using Kepler iteration (12-step Newton-Raphson for eccentric anomaly)
- **GLONASS TLEs**: Fetched from Celestrak (https://celestrak.org), propagated via SGP4 library with ECI→ECEF conversion using GMST
- **3D Rendering**: Three.js r128, ECEF ↔ Three.js coordinate swap (Z-up → Y-up), dynamic sprite scaling for constant apparent text size
- **RF Horizon**: Elevation mask = −arccos(R/(R+h)), allowing negative elevation angles at altitude (sats visible "below" the horizon)
- **Single Worker**: The app uses global in-memory caches for almanac/TLE data. Render is configured with `--workers 1` to avoid cache inconsistency

## License

Public domain.
