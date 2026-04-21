# GPS Navigation Message Calculator

A Python application to fetch NAVCEN YUMA GPS almanac data and calculate satellite positions.

## Quick Start

### On Desktop (Windows)
1. Copy `gps_calc.py` and `requirements.txt` to your project folder
2. Open PowerShell in that folder
3. Run:
   ```bash
   pip install -r requirements.txt
   python gps_calc.py
   ```

### On Android Phone (Pydroid 3)
1. Install `requests` via Pip menu
2. Copy `gps_calc.py` to your Pydroid editor
3. Tap Run (green play button)

## Features
- Fetch NAVCEN YUMA almanac for any date
- List all GPS satellites with orbital elements
- Calculate satellite ECEF position at any time
- Convert to WGS-84 geodetic coordinates (lat/lon/altitude)

## Usage
```
1. Load almanac (by date)
2. List satellites
3. Calculate position
4. Exit
```

## References
- IS-GPS-200N GPS Receiver Performance Standards
- WGS-84 Coordinate System
