from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from gps_core import fetch_almanac, parse_yuma, propagate, geodetic, gps_time_from_datetime
import math

app = Flask(__name__)

almanac_data = {'satellites': [], 'date': None, 'week': None, 'toa': None}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/load-almanac', methods=['POST'])
def load_almanac():
    global almanac_data

    payload = request.json
    if payload is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    date_str = payload.get('date', 'today')

    if date_str.lower() == 'today':
        dt = datetime.now(timezone.utc) - timedelta(days=2)
    else:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({'error': 'Invalid date format'}), 400

    if dt > datetime.now(timezone.utc):
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
        'toa': satellites[0]['toa'],
    }

    return jsonify({
        'success': True,
        'count': len(satellites),
        'week': satellites[0]['wk'],
        'toa': satellites[0]['toa'],
    })


@app.route('/api/satellites', methods=['GET'])
def get_satellites():
    if not almanac_data['satellites']:
        return jsonify({'error': 'No almanac loaded'}), 400

    result = [
        {
            'id': sat['id'],
            'health': 'Healthy' if sat['health'] == 0 else 'Unhealthy',
            'e': f"{sat['e']:.8f}",
            'sqA': f"{sat['sqA']:.1f}",
            'inc': f"{math.degrees(sat['inc']):.2f}",
        }
        for sat in almanac_data['satellites']
    ]

    return jsonify({'satellites': result, 'date': almanac_data['date']})


@app.route('/api/calculate', methods=['POST'])
def calculate_position():
    if not almanac_data['satellites']:
        return jsonify({'error': 'No almanac loaded'}), 400

    payload = request.json
    if payload is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    try:
        prn = int(payload.get('prn', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid PRN'}), 400

    time_str = payload.get('time', 'now')

    satellite = next((s for s in almanac_data['satellites'] if s['id'] == prn), None)
    if not satellite:
        return jsonify({'error': f'PRN {prn} not found'}), 404

    if time_str.lower() == 'now':
        dt = datetime.now(timezone.utc)
    else:
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({'error': 'Invalid time format'}), 400

    gps_sec = gps_time_from_datetime(dt)
    pos = propagate(satellite, gps_sec)
    geo = geodetic(pos['x'], pos['y'], pos['z'])

    return jsonify({
        'prn': prn,
        'time': dt.strftime("%Y-%m-%d %H:%M:%S"),
        'ecef': {
            'x': f"{pos['x']:,.0f}",
            'y': f"{pos['y']:,.0f}",
            'z': f"{pos['z']:,.0f}",
            'r': f"{pos['r'] / 1000:,.1f}",
        },
        'geodetic': {
            'latitude': f"{geo['lat']:.4f}",
            'longitude': f"{geo['lon']:.4f}",
            'altitude': f"{geo['alt'] / 1000:.1f}",
        },
    })
