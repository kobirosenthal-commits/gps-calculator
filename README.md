# GPS / GLONASS Calculator

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

## File Structure

```
gps-calculator/
├── main.py                    # Flask entry point
├── app.py                     # Flask routes & API endpoints
├── gps_core.py                # GPS/GLONASS propagation (Kepler, SGP4, WGS84)
├── requirements.txt           # Python dependencies
├── Procfile                   # Render deployment config
├── templates/
│   ├── index.html             # Calculator UI (GPS/GLONASS toggle)
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

## Deploy to Render

1. **Create a Render account** at https://render.com (free tier available)

2. **Connect your GitHub repo:**
   - In Render dashboard, click **New → Web Service**
   - Select **Connect a repository** and choose this repo
   - Render auto-detects the `Procfile`

3. **Set environment variables** (if needed):
   - No secrets required for this app

4. **Deploy:**
   - Click **Deploy**
   - Render builds, installs dependencies, and starts the server
   - Your app is live at `https://gps-calculator-xxxxx.onrender.com`

5. **Auto-deploy on push:**
   - Every `git push` to main triggers a new deploy
   - Check logs in the Render dashboard

## Technical Notes

- **GPS Almanac**: Fetched from NAVCEN (https://www.navcen.uscg.gov), parsed via regex on abbreviated YUMA field names, propagated using Kepler iteration (12-step Newton-Raphson for eccentric anomaly)
- **GLONASS TLEs**: Fetched from Celestrak (https://celestrak.org), propagated via SGP4 library with ECI→ECEF conversion using GMST
- **3D Rendering**: Three.js r128, ECEF ↔ Three.js coordinate swap (Z-up → Y-up), dynamic sprite scaling for constant apparent text size
- **RF Horizon**: Elevation mask = −arccos(R/(R+h)), allowing negative elevation angles at altitude (sats visible "below" the horizon)
- **Single Worker**: The app uses global in-memory caches for almanac/TLE data. Render is configured with `--workers 1` to avoid cache inconsistency

## License

Public domain.
