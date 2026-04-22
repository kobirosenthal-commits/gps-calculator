from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from gps_core import (fetch_almanac, parse_yuma, propagate, geodetic, gps_time_from_datetime,
                      fetch_tle_group, parse_tles, propagate_tle)
import logging
import math
import threading
import time

# Surface INFO-level logs from gps_core (TLE fetch attempts) in gunicorn's stream
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
logging.getLogger('gps_core').setLevel(logging.INFO)

app = Flask(__name__)

almanac_data = {'satellites': [], 'date': None, 'week': None, 'toa': None}
glo_data = {'tles': [], 'fetched': None}
bei_data = {'tles': [], 'fetched': None}
gal_data = {'tles': [], 'fetched': None}


def _load_tle_constellation(group, label_prefix):
    """Fetch, parse and label TLEs for a constellation. Returns list or None."""
    try:
        text = fetch_tle_group(group)
        if not text:
            return None
        tles = parse_tles(text)
        if not tles:
            return None
        for i, tle in enumerate(tles):
            tle['id'] = i + 1
            tle['label'] = f"{label_prefix}{i + 1:02d}"
        return tles
    except Exception:
        return None


def _tle_refresh_worker():
    """Background thread that refreshes TLE constellations without blocking requests."""
    global glo_data, bei_data, gal_data
    while True:
        all_ok = True
        for group, prefix, target in (
            ('glo-ops',  'R', 'glo'),
            ('beidou',   'C', 'bei'),
            ('galileo',  'E', 'gal'),
        ):
            try:
                tles = _load_tle_constellation(group, prefix)
                if tles:
                    entry = {'tles': tles, 'fetched': datetime.now(timezone.utc).strftime("%Y-%m-%d")}
                    if target == 'glo':
                        glo_data = entry
                    elif target == 'bei':
                        bei_data = entry
                    else:
                        gal_data = entry
                    app.logger.info(f"TLE refresh: loaded {len(tles)} {target} satellites")
                else:
                    all_ok = False
                    app.logger.warning(f"TLE refresh: failed to load {target}")
            except Exception as e:
                all_ok = False
                app.logger.warning(f"TLE refresh: exception loading {target}: {e}")
        # If everything loaded, refresh every 6h; otherwise retry in 2 minutes
        time.sleep(6 * 3600 if all_ok else 120)


threading.Thread(target=_tle_refresh_worker, daemon=True).start()


@app.errorhandler(500)
def _api_500(err):
    """Return JSON for API 500s so the frontend never sees an HTML error page."""
    if request.path.startswith('/api/'):
        app.logger.exception(f"Unhandled error in {request.path}")
        original = getattr(err, 'original_exception', err)
        return jsonify({'error': f'{type(original).__name__}: {original}'}), 500
    return err


@app.route('/')
def index():
    return render_template('cesium.html')


@app.route('/calculator')
def calculator():
    return render_template('index.html')


@app.route('/api/load-almanac', methods=['POST'])
def load_almanac():
    global almanac_data, glo_data, bei_data, gal_data

    payload = request.json
    if payload is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation = payload.get('constellation', 'GPS').upper()

    if constellation in ('GLONASS', 'BEIDOU', 'GALILEO'):
        group  = {'GLONASS': 'glo-ops', 'BEIDOU': 'beidou', 'GALILEO': 'galileo'}[constellation]
        prefix = {'GLONASS': 'R',       'BEIDOU': 'C',      'GALILEO': 'E'}[constellation]
        tles = _load_tle_constellation(group, prefix)
        if not tles:
            return jsonify({'error': f'Could not fetch {constellation} TLEs from Celestrak'}), 503
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if constellation == 'GLONASS':
            glo_data = {'tles': tles, 'fetched': fetched}
        elif constellation == 'BEIDOU':
            bei_data = {'tles': tles, 'fetched': fetched}
        else:
            gal_data = {'tles': tles, 'fetched': fetched}
        return jsonify({
            'success': True,
            'count': len(tles),
            'constellation': constellation,
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

    text = fetch_almanac(dt.year, dt.timetuple().tm_yday)
    if not text:
        return jsonify({'error': f'Almanac not found for {dt.year} day {dt.timetuple().tm_yday}'}), 404

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
        result = [{'id': t['id'], 'label': t['label'], 'name': t['name'], 'health': 'Healthy'} for t in glo_data['tles']]
        return jsonify({'satellites': result, 'date': glo_data['fetched'], 'constellation': 'GLONASS'})

    if constellation == 'BEIDOU':
        if not bei_data['tles']:
            return jsonify({'error': 'No BeiDou TLEs loaded'}), 400
        result = [{'id': t['id'], 'label': t['label'], 'name': t['name'], 'health': 'Healthy'} for t in bei_data['tles']]
        return jsonify({'satellites': result, 'date': bei_data['fetched'], 'constellation': 'BEIDOU'})

    if constellation == 'GALILEO':
        if not gal_data['tles']:
            return jsonify({'error': 'No Galileo TLEs loaded'}), 400
        result = [{'id': t['id'], 'label': t['label'], 'name': t['name'], 'health': 'Healthy'} for t in gal_data['tles']]
        return jsonify({'satellites': result, 'date': gal_data['fetched'], 'constellation': 'GALILEO'})

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


@app.route('/cesium')
def cesium_view():
    return render_template('cesium.html')


@app.route('/api/satellite-detail', methods=['GET'])
def satellite_detail():
    label = request.args.get('label', '')
    constellation = request.args.get('constellation', 'GPS').upper()

    if constellation == 'GPS':
        try:
            prn = int(label[1:])
        except (ValueError, IndexError):
            return jsonify({'error': 'Invalid label'}), 400
        sat = next((s for s in almanac_data['satellites'] if s['id'] == prn), None)
        if not sat:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({
            'label': label, 'constellation': 'GPS',
            'almanac': {
                'gps_week':          sat['wk'],
                'toa':               sat['toa'],
                'sqrt_a':            sat['sqA'],
                'semi_major_axis_km': sat['sqA'] ** 2 / 1000,
                'eccentricity':      sat['e'],
                'inclination_deg':   math.degrees(sat['inc']),
                'raan_deg':          math.degrees(sat['Om0']),
                'raan_rate':         sat['dOm'],
                'arg_perigee_deg':   math.degrees(sat['w']),
                'mean_anomaly_deg':  math.degrees(sat['M0']),
                'af0':               sat['af0'],
                'af1':               sat['af1'],
                'health':            sat['health'],
            }
        })

    cache = {'GLONASS': glo_data, 'BEIDOU': bei_data, 'GALILEO': gal_data}.get(constellation)
    if not cache or not cache['tles']:
        return jsonify({'error': f'No {constellation} data available'}), 400
    tle = next((t for t in cache['tles'] if t['label'] == label), None)
    if not tle:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'label': label, 'constellation': constellation,
        'tle': {'name': tle['name'], 'line1': tle['line1'], 'line2': tle['line2']},
    })


@app.route('/api/live-positions', methods=['GET'])
def live_positions():
    global almanac_data, glo_data, bei_data, gal_data

    # Auto-load GPS almanac (never let a network glitch 500 the endpoint)
    if not almanac_data['satellites']:
        dt = datetime.now(timezone.utc) - timedelta(days=2)
        for offset in range(5):
            candidate = dt - timedelta(days=offset)
            try:
                text = fetch_almanac(candidate.year, candidate.timetuple().tm_yday)
            except Exception:
                text = None
            if text:
                try:
                    satellites = parse_yuma(text)
                except Exception:
                    satellites = None
                if satellites:
                    almanac_data = {
                        'satellites': satellites,
                        'date': candidate.strftime("%Y-%m-%d"),
                        'week': satellites[0]['wk'],
                        'toa': satellites[0]['toa'],
                    }
                    break

    # GLONASS/BeiDou TLEs are loaded by a background thread — never blocks this request

    if not almanac_data['satellites'] and not glo_data['tles'] and not bei_data['tles'] and not gal_data['tles']:
        return jsonify({'error': 'Could not load satellite data'}), 503

    now = datetime.now(timezone.utc)
    gps_sec = gps_time_from_datetime(now)
    positions = []

    for sat in almanac_data['satellites']:
        try:
            pos = propagate(sat, gps_sec)
            geo = geodetic(pos['x'], pos['y'], pos['z'])
            positions.append({
                'prn': sat['id'], 'label': f"G{sat['id']:02d}", 'constellation': 'GPS',
                'healthy': sat['health'] == 0,
                'x': pos['x'], 'y': pos['y'], 'z': pos['z'],
                'lat': round(geo['lat'], 4), 'lon': round(geo['lon'], 4), 'alt_km': round(geo['alt'] / 1000, 1),
            })
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    for constellation, cache in (('GLONASS', glo_data), ('BEIDOU', bei_data), ('GALILEO', gal_data)):
        for tle in cache['tles']:
            try:
                pos = propagate_tle(tle, now)
                if not pos:
                    continue
                geo = geodetic(pos['x'], pos['y'], pos['z'])
                positions.append({
                    'prn': tle['id'], 'label': tle['label'], 'constellation': constellation,
                    'name': tle['name'],
                    'healthy': True,
                    'x': pos['x'], 'y': pos['y'], 'z': pos['z'],
                    'lat': round(geo['lat'], 4), 'lon': round(geo['lon'], 4), 'alt_km': round(geo['alt'] / 1000, 1),
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

    if constellation in ('GLONASS', 'BEIDOU', 'GALILEO'):
        cache = {'GLONASS': glo_data, 'BEIDOU': bei_data, 'GALILEO': gal_data}[constellation]
        if not cache['tles']:
            return jsonify({'error': f'No {constellation} TLEs loaded'}), 400

        try:
            sat_id = int(payload.get('prn', 0))
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid satellite ID'}), 400

        tle = next((t for t in cache['tles'] if t['id'] == sat_id), None)
        if not tle:
            return jsonify({'error': f'{constellation} satellite {sat_id} not found'}), 404

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
            'prn': sat_id, 'label': tle['label'], 'constellation': constellation,
            'time': dt.strftime("%Y-%m-%d %H:%M:%S"),
            'ecef': {'x': f"{pos['x']:,.0f}", 'y': f"{pos['y']:,.0f}", 'z': f"{pos['z']:,.0f}", 'r': f"{pos['r'] / 1000:,.1f}"},
            'geodetic': {'latitude': f"{geo['lat']:.4f}", 'longitude': f"{geo['lon']:.4f}", 'altitude': f"{geo['alt'] / 1000:.1f}"},
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
        'prn': prn, 'label': f"G{prn:02d}", 'constellation': 'GPS',
        'time': dt.strftime("%Y-%m-%d %H:%M:%S"),
        'ecef': {'x': f"{pos['x']:,.0f}", 'y': f"{pos['y']:,.0f}", 'z': f"{pos['z']:,.0f}", 'r': f"{pos['r'] / 1000:,.1f}"},
        'geodetic': {'latitude': f"{geo['lat']:.4f}", 'longitude': f"{geo['lon']:.4f}", 'altitude': f"{geo['alt'] / 1000:.1f}"},
    })
