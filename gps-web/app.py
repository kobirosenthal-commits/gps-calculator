#!/usr/bin/env python3
"""
GPS Navigation Message Calculator - Web Edition
Flask app to serve interactive web interface
"""

from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import requests
import math
import re
import json

app = Flask(__name__)

# GPS Constants
MU = 3.986005e14
OMEGA_E = 7.2921151467e-5
SPW = 604800
PI = math.pi
GPS_EPOCH = datetime(1980, 1, 6)


def fetch_almanac(year, doy):
    """Download YUMA almanac from NAVCEN"""
    url = f"https://www.navcen.uscg.gov/sites/default/files/gps/almanac/{year}/Yuma/{doy:03d}.alm"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        return None


def parse_yuma(text):
    """Parse YUMA almanac text"""
    sats = []
    for block in text.split("*" * 5):
        if len(block.strip()) < 50:
            continue

        def g(key):
            m = re.search(rf"^\s*{key}[^:]*:\s*(-?[0-9.eE+\-]+)", block, re.M | re.I)
            return float(m.group(1)) if m else None

        prn = g("ID") or g("PRN")
        if not prn:
            continue

        week = int(g("week") or 0)
        if 0 < week < 1024:
            week += 2048

        sat = {
            'id': int(prn),
            'health': int(g("Health") or 0),
            'e': g("Eccentricity"),
            'toa': g("Time of Applicability"),
            'inc': g("Orbital Inclination"),
            'dOm': g("Rate of Right Ascen"),
            'sqA': g("SQRT"),
            'Om0': g("Right Ascen at Week"),
            'w': g("Argument of Perigee"),
            'M0': g("Mean Anom"),
            'af0': g("Af0") or 0,
            'af1': g("Af1") or 0,
            'wk': week,
        }
        sats.append(sat)

    return sats


def propagate(sat, gps_sec):
    """Calculate satellite ECEF position"""
    A = sat['sqA'] ** 2
    n0 = math.sqrt(MU / A ** 3)
    t_ref = sat['wk'] * SPW + sat['toa']
    tk = gps_sec - t_ref

    M = sat['M0'] + n0 * tk
    E = M
    for _ in range(12):
        E = M + sat['e'] * math.sin(E)

    cE = math.cos(E)
    sE = math.sin(E)
    nu = math.atan2(math.sqrt(1 - sat['e'] ** 2) * sE, cE - sat['e'])

    phi = nu + sat['w']
    r = A * (1 - sat['e'] * cE)
    xo = r * math.cos(phi)
    yo = r * math.sin(phi)

    Om = sat['Om0'] + (sat['dOm'] - OMEGA_E) * tk - OMEGA_E * t_ref
    cO = math.cos(Om)
    sO = math.sin(Om)
    ci = math.cos(sat['inc'])
    si = math.sin(sat['inc'])

    x = xo * cO - yo * ci * sO
    y = xo * sO + yo * ci * cO
    z = yo * si

    return {'x': x, 'y': y, 'z': z, 'r': math.sqrt(x ** 2 + y ** 2 + z ** 2)}


def geodetic(x, y, z):
    """Convert ECEF to WGS-84 geodetic"""
    a = 6378137.0
    f = 1.0 / 298.257223563
    e2 = 2 * f - f * f

    lon = math.atan2(y, x)
    p = math.sqrt(x ** 2 + y ** 2)
    lat = math.atan2(z, p * (1 - e2))

    for _ in range(10):
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * N * math.sin(lat), p)

    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    if abs(lat) < PI / 4:
        alt = p / math.cos(lat) - N
    else:
        alt = z / math.sin(lat) - N * (1 - e2)

    return {'lat': math.degrees(lat), 'lon': math.degrees(lon), 'alt': alt}


def gps_time_from_datetime(dt):
    """Convert datetime to GPS seconds"""
    return (dt - GPS_EPOCH).total_seconds()


# Store almanac data in session
almanac_data = {'satellites': [], 'date': None, 'week': None}


@app.route('/')
def index():
    """Serve main page"""
    return render_template('index.html')


@app.route('/api/load-almanac', methods=['POST'])
def load_almanac():
    """API endpoint to load almanac"""
    global almanac_data

    data = request.json
    date_str = data.get('date', 'today')

    if date_str.lower() == 'today':
        dt = datetime.utcnow() - timedelta(days=2)
    else:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except:
            return jsonify({'error': 'Invalid date format'}), 400

    if dt > datetime.utcnow():
        return jsonify({'error': 'Cannot load future dates'}), 400

    year = dt.year
    doy = dt.timetuple().tm_yday

    text = fetch_almanac(year, doy)
    if not text:
        return jsonify({'error': f'Almanac not found for {year} day {doy}'}), 404

    satellites = parse_yuma(text)
    if not satellites:
        return jsonify({'error': 'Could not parse satellites'}), 400

    almanac_data = {
        'satellites': satellites,
        'date': dt.strftime("%Y-%m-%d"),
        'week': satellites[0]['wk'],
        'toa': satellites[0]['toa']
    }

    return jsonify({
        'success': True,
        'count': len(satellites),
        'week': satellites[0]['wk'],
        'toa': satellites[0]['toa']
    })


@app.route('/api/satellites', methods=['GET'])
def get_satellites():
    """Get list of loaded satellites"""
    if not almanac_data['satellites']:
        return jsonify({'error': 'No almanac loaded'}), 400

    sats = []
    for s in almanac_data['satellites']:
        sats.append({
            'id': s['id'],
            'health': 'Healthy' if s['health'] == 0 else 'Unhealthy',
            'e': f"{s['e']:.8f}",
            'sqA': f"{s['sqA']:.1f}",
            'inc': f"{math.degrees(s['inc']):.2f}"
        })

    return jsonify({'satellites': sats, 'date': almanac_data['date']})


@app.route('/api/calculate', methods=['POST'])
def calculate_position():
    """Calculate satellite position"""
    if not almanac_data['satellites']:
        return jsonify({'error': 'No almanac loaded'}), 400

    data = request.json
    prn = int(data.get('prn'))
    time_str = data.get('time', 'now')

    sat = next((s for s in almanac_data['satellites'] if s['id'] == prn), None)
    if not sat:
        return jsonify({'error': f'PRN {prn} not found'}), 404

    if time_str.lower() == 'now':
        dt = datetime.utcnow()
    else:
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except:
            return jsonify({'error': 'Invalid time format'}), 400

    gps_sec = gps_time_from_datetime(dt)
    pos = propagate(sat, gps_sec)
    geo = geodetic(pos['x'], pos['y'], pos['z'])

    return jsonify({
        'prn': prn,
        'time': dt.strftime("%Y-%m-%d %H:%M:%S"),
        'ecef': {
            'x': f"{pos['x']:,.0f}",
            'y': f"{pos['y']:,.0f}",
            'z': f"{pos['z']:,.0f}",
            'r': f"{pos['r'] / 1000:,.1f}"
        },
        'geodetic': {
            'latitude': f"{geo['lat']:.4f}",
            'longitude': f"{geo['lon']:.4f}",
            'altitude': f"{geo['alt'] / 1000:.1f}"
        }
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)