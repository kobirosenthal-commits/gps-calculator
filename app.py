from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from gps_core import (fetch_almanac, parse_yuma, propagate, geodetic, gps_time_from_datetime,
                      fetch_tle_group, parse_tles, propagate_tle)
import math

app = Flask(__name__)

almanac_data = {'satellites': [], 'date': None, 'week': None, 'toa': None}
glo_data = {'tles': [], 'fetched': None}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/load-almanac', methods=['POST'])
def load_almanac():
    global almanac_data, glo_data

    payload = request.json
    if payload is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation = payload.get('constellation', 'GPS').upper()

    if constellation == 'GLONASS':
        text = fetch_tle_group('glo-ops')
        if not text:
            return jsonify({'error': 'Could not fetch GLONASS TLEs from Celestrak'}), 503
        tles = parse_tles(text)
        if not tles:
            return jsonify({'error': 'Could not parse GLONASS TLEs'}), 400
        for i, tle in enumerate(tles):
            tle['id'] = i + 1
            tle['label'] = f"R{i + 1:02d}"
        glo_data = {'tles': tles, 'fetched': datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        return jsonify({
            'success': True,
            'count': len(tles),
            'constellation': 'GLONASS',
            'source': 'Celestrak (current TLEs)',
        })

    # GPS path
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
        'constellation': 'GPS',
    })


@app.route('/api/satellites', methods=['GET'])
def get_satellites():
    constellation = request.args.get('constellation', 'GPS').upper()

    if constellation == 'GLONASS':
        if not glo_data['tles']:
            return jsonify({'error': 'No GLONASS TLEs loaded'}), 400
        result = [
            {'id': tle['id'], 'label': tle['label'], 'name': tle['name'], 'health': 'Healthy'}
            for tle in glo_data['tles']
        ]
        return jsonify({'satellites': result, 'date': glo_data['fetched'], 'constellation': 'GLONASS'})

    if not almanac_data['satellites']:
        return jsonify({'error': 'No almanac loaded'}), 400

    result = [
        {
            'id': sat['id'],
            'label': f"G{sat['id']:02d}",
            'health': 'Healthy' if sat['health'] == 0 else 'Unhealthy',
            'e': f"{sat['e']:.8f}",
            'sqA': f"{sat['sqA']:.1f}",
            'inc': f"{math.degrees(sat['inc']):.2f}",
        }
        for sat in almanac_data['satellites']
    ]

    return jsonify({'satellites': result, 'date': almanac_data['date'], 'constellation': 'GPS'})


@app.route('/live')
def live():
    return render_template('live.html')


@app.route('/api/live-positions', methods=['GET'])
def live_positions():
    global almanac_data, glo_data

    # Auto-load GPS almanac if needed
    if not almanac_data['satellites']:
        dt = datetime.now(timezone.utc) - timedelta(days=2)
        for offset in range(5):
            candidate = dt - timedelta(days=offset)
            text = fetch_almanac(candidate.year, candidate.timetuple().tm_yday)
            if text:
                satellites = parse_yuma(text)
                if satellites:
                    almanac_data = {
                        'satellites': satellites,
                        'date': candidate.strftime("%Y-%m-%d"),
                        'week': satellites[0]['wk'],
                        'toa': satellites[0]['toa'],
                    }
                    break

    # Auto-load GLONASS TLEs if needed
    if not glo_data['tles']:
        text = fetch_tle_group('glo-ops')
        if text:
            tles = parse_tles(text)
            if tles:
                for i, tle in enumerate(tles):
                    tle['id'] = i + 1
                    tle['label'] = f"R{i + 1:02d}"
                glo_data = {'tles': tles, 'fetched': datetime.now(timezone.utc).strftime("%Y-%m-%d")}

    if not almanac_data['satellites'] and not glo_data['tles']:
        return jsonify({'error': 'Could not load satellite data'}), 503

    now = datetime.now(timezone.utc)
    gps_sec = gps_time_from_datetime(now)
    positions = []

    for sat in almanac_data['satellites']:
        try:
            pos = propagate(sat, gps_sec)
            geo = geodetic(pos['x'], pos['y'], pos['z'])
            positions.append({
                'prn': sat['id'],
                'label': f"G{sat['id']:02d}",
                'constellation': 'GPS',
                'healthy': sat['health'] == 0,
                'x': pos['x'],
                'y': pos['y'],
                'z': pos['z'],
                'lat': round(geo['lat'], 4),
                'lon': round(geo['lon'], 4),
                'alt_km': round(geo['alt'] / 1000, 1),
            })
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    for tle in glo_data['tles']:
        try:
            pos = propagate_tle(tle, now)
            if not pos:
                continue
            geo = geodetic(pos['x'], pos['y'], pos['z'])
            positions.append({
                'prn': tle['id'],
                'label': tle['label'],
                'constellation': 'GLONASS',
                'healthy': True,
                'x': pos['x'],
                'y': pos['y'],
                'z': pos['z'],
                'lat': round(geo['lat'], 4),
                'lon': round(geo['lon'], 4),
                'alt_km': round(geo['alt'] / 1000, 1),
            })
        except Exception:
            pass

    return jsonify({
        'time': now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        'almanac_date': almanac_data['date'],
        'satellites': positions,
    })


@app.route('/api/calculate', methods=['POST'])
def calculate_position():
    payload = request.json
    if payload is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation = payload.get('constellation', 'GPS').upper()

    if constellation == 'GLONASS':
        if not glo_data['tles']:
            return jsonify({'error': 'No GLONASS TLEs loaded'}), 400

        try:
            sat_id = int(payload.get('prn', 0))
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid satellite ID'}), 400

        tle = next((t for t in glo_data['tles'] if t['id'] == sat_id), None)
        if not tle:
            return jsonify({'error': f'GLONASS satellite {sat_id} not found'}), 404

        time_str = payload.get('time', 'now')
        if time_str.lower() == 'now':
            dt = datetime.now(timezone.utc)
        else:
            try:
                dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return jsonify({'error': 'Invalid time format'}), 400

        pos = propagate_tle(tle, dt)
        if not pos:
            return jsonify({'error': 'TLE propagation failed'}), 500

        geo = geodetic(pos['x'], pos['y'], pos['z'])
        return jsonify({
            'prn': sat_id,
            'label': tle['label'],
            'constellation': 'GLONASS',
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

    # GPS path
    if not almanac_data['satellites']:
        return jsonify({'error': 'No almanac loaded'}), 400

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
        'label': f"G{prn:02d}",
        'constellation': 'GPS',
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
