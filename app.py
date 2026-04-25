from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from gps_core import (fetch_almanac, parse_yuma, propagate, geodetic, gps_time_from_datetime,
                      fetch_tle_group, parse_tles, propagate_tle, fetch_glonass_slot_map,
                      fetch_gps_rinex, parse_rinex2_nav,
                      fetch_gps_rinex4, parse_rinex4_nav)
import logging
log = logging.getLogger(__name__)
import math
import re
import threading
import time

# Surface INFO-level logs from gps_core (TLE fetch attempts) in gunicorn's stream
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
logging.getLogger('gps_core').setLevel(logging.INFO)

app = Flask(__name__)

# GSAT serial to Galileo PRN/SVID. Source: European GNSS Service Centre constellation
# status. Stable — only changes when a satellite launches or is retired.
GSAT_TO_PRN = {
    101: 11, 102: 12, 103: 19,
    201: 18, 202: 14,
    203: 26, 206: 30, 207: 7, 208: 8, 209: 9,
    211: 2, 212: 3, 213: 4, 214: 5,
    215: 21, 216: 25, 217: 27, 218: 31, 219: 36,
    220: 13, 221: 15, 222: 33, 223: 34, 224: 10, 225: 29,
    226: 23, 227: 6, 232: 16,
}

almanac_data = {'satellites': [], 'date': None, 'week': None, 'toa': None}
rinex_data   = {'ephemeris': {}, 'date': None}
rinex4_data  = {'ephemeris': {}, 'date': None}
glo_data = {'tles': [], 'fetched': None}
bei_data = {'tles': [], 'fetched': None}
gal_data = {'tles': [], 'fetched': None}


def _load_tle_constellation(group, label_prefix):
    """Fetch, parse and label TLEs for a constellation. Returns list or None.
       GLONASS labels are mapped to real orbital slot numbers (R01-R24) via the
       Russian IAC feed; other constellations fall back to file order."""
    try:
        text = fetch_tle_group(group)
        if not text:
            return None
        tles = parse_tles(text)
        if not tles:
            return None
        if label_prefix == 'R':
            slot_map = fetch_glonass_slot_map()
            kept = []
            for tle in tles:
                try:
                    norad = int(tle['line1'][2:7])
                except (ValueError, KeyError):
                    continue
                slot = slot_map.get(norad)
                if not slot:
                    continue  # not an operational GLONASS slot — drop
                tle['id'] = slot
                tle['label'] = f"R{slot:02d}"
                kept.append(tle)
            kept.sort(key=lambda t: t['id'])
            return kept
        for i, tle in enumerate(tles):
            tle['id'] = i + 1
            tle['label'] = f"{label_prefix}{i + 1:02d}"
        return tles
    except Exception as e:
        log.warning(f"_load_tle_constellation({group}): {type(e).__name__}: {e}")
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


def _rinex_refresh_worker():
    global rinex_data
    while True:
        loaded = False
        for offset in range(3):
            dt = datetime.now(timezone.utc) - timedelta(days=offset)
            try:
                text = fetch_gps_rinex(dt)
                if text:
                    eph = parse_rinex2_nav(text)
                    if eph:
                        rinex_data = {'ephemeris': eph, 'date': dt.strftime('%Y-%m-%d')}
                        app.logger.info(f"RINEX loaded: {len(eph)} PRNs from {dt.strftime('%Y-%m-%d')}")
                        loaded = True
                        break
            except Exception as e:
                app.logger.warning(f"RINEX refresh error: {e}")
        time.sleep(6 * 3600 if loaded else 120)


threading.Thread(target=_rinex_refresh_worker, daemon=True).start()


def _rinex4_refresh_worker():
    global rinex4_data
    while True:
        loaded = False
        # BKG RINEX 4 files are typically available with ~1 day delay; try yesterday first
        for offset in range(1, 4):
            dt = datetime.now(timezone.utc) - timedelta(days=offset)
            try:
                text = fetch_gps_rinex4(dt)
                if text:
                    eph = parse_rinex4_nav(text)
                    if eph:
                        rinex4_data = {'ephemeris': eph, 'date': dt.strftime('%Y-%m-%d')}
                        app.logger.info(f"RINEX4 loaded: {len(eph)} CNAV PRNs from {dt.strftime('%Y-%m-%d')}")
                        loaded = True
                        break
            except Exception as e:
                app.logger.warning(f"RINEX4 refresh error: {e}")
        time.sleep(6 * 3600 if loaded else 120)


threading.Thread(target=_rinex4_refresh_worker, daemon=True).start()


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
        resp = {
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
        }
        eph = rinex_data['ephemeris'].get(prn)
        if eph:
            resp['ephemeris'] = {
                # SF1 — Clock
                'toc':       eph['epoch'],
                'gps_week':  eph['gps_week'],
                'iodc':      eph['iodc'],
                'af2':       eph['af2'],
                'tgd':       eph['tgd'],
                'ura':       eph['ura'],
                'health_eph': eph['health'],
                'l2_codes':  eph['l2_codes'],
                'l2_p_flag': eph['l2_p_flag'],
                # SF2 — Ephemeris 1
                'iode':      int(eph['iode']),
                'toe':       eph['toe'],
                'delta_n':   eph['delta_n'],
                'm0_deg':    math.degrees(eph['m0']),
                'e_eph':     eph['e'],
                'sqrt_a_eph': eph['sqrt_a'],
                'crs':       eph['crs'],
                'cus':       eph['cus'],
                'cuc':       eph['cuc'],
                'fit_interval': eph['fit_interval'],
                # SF3 — Ephemeris 2
                'omega0_deg': math.degrees(eph['omega0']),
                'i0_deg':    math.degrees(eph['i0']),
                'omega_deg': math.degrees(eph['omega']),
                'omega_dot': eph['omega_dot'],
                'idot':      eph['idot'],
                'crc':       eph['crc'],
                'cic':       eph['cic'],
                'cis':       eph['cis'],
                'rinex_date': rinex_data['date'],
            }
        cnav = rinex4_data['ephemeris'].get(prn)
        if cnav:
            resp['cnav'] = {
                'toc':          cnav['epoch'],
                'gps_week':     cnav['gps_week'],
                'toe':          cnav['toe'],
                'af2':          cnav['af2'],
                'tgd':          cnav['tgd'],
                'adot':         cnav['adot'],
                'delta_n':      cnav['delta_n'],
                'delta_n_dot':  cnav['delta_n_dot'],
                'm0_deg':       math.degrees(cnav['m0']),
                'e_cnav':       cnav['e'],
                'sqrt_a_cnav':  cnav['sqrt_a'],
                'omega0_deg':   math.degrees(cnav['omega0']),
                'i0_deg':       math.degrees(cnav['i0']),
                'omega_deg':    math.degrees(cnav['omega']),
                'omega_dot':    cnav['omega_dot'],
                'idot':         cnav['idot'],
                'crs':          cnav['crs'],
                'cuc':          cnav['cuc'],
                'cus':          cnav['cus'],
                'crc':          cnav['crc'],
                'cic':          cnav['cic'],
                'cis':          cnav['cis'],
                'urai_oe':      cnav['urai_oe'],
                'urai_ed':      cnav['urai_ed'],
                'isc_l1ca':     cnav['isc_l1ca'],
                'isc_l2c':      cnav['isc_l2c'],
                'isc_l5i5':     cnav['isc_l5i5'],
                'isc_l5q5':     cnav['isc_l5q5'],
                'top':          cnav['top'],
                'rinex4_date':  rinex4_data['date'],
            }
        return jsonify(resp)

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


@app.route('/api/rinex-status', methods=['GET'])
def rinex_status():
    return jsonify({
        'lnav': {'loaded': bool(rinex_data['ephemeris']), 'date': rinex_data['date'], 'prns': len(rinex_data['ephemeris'])},
        'cnav': {'loaded': bool(rinex4_data['ephemeris']), 'date': rinex4_data['date'], 'prns': len(rinex4_data['ephemeris'])},
    })


@app.route('/api/fetch-tles', methods=['POST'])
def fetch_tles_proxy():
    """Return already-cached TLE data loaded by the background thread.
    Never makes outbound network calls — avoids Render's 30s request timeout."""
    payload = request.json or {}
    constellation = payload.get('constellation', '').upper()
    cache = {'GLONASS': glo_data, 'BEIDOU': bei_data, 'GALILEO': gal_data}.get(constellation)
    if cache and cache.get('tles'):
        return jsonify({'success': True, 'count': len(cache['tles'])})
    return jsonify({'success': False, 'error': 'not yet loaded — background thread still fetching'}), 503


@app.route('/api/push-tles', methods=['POST'])
def push_tles():
    """Accept TLE text fetched by the user's browser and cache it server-side.
    Needed because Render's outbound IPs cannot reach celestrak.org directly —
    the browser does the fetch and hands the text back to us."""
    global glo_data, bei_data, gal_data

    payload = request.json
    if payload is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation = payload.get('constellation', '').upper()
    text = payload.get('text', '')

    if constellation not in ('GLONASS', 'BEIDOU', 'GALILEO'):
        return jsonify({'error': f'Invalid constellation: {constellation}'}), 400
    if not text or '1 ' not in text:
        return jsonify({'error': 'No valid TLE content'}), 400

    try:
        tles = parse_tles(text)
    except Exception as e:
        return jsonify({'error': f'Parse error: {e}'}), 400

    if not tles:
        return jsonify({'error': 'No TLEs parsed'}), 400

    prefix = {'GLONASS': 'R', 'BEIDOU': 'C', 'GALILEO': 'E'}[constellation]
    if constellation == 'GLONASS':
        slot_map = fetch_glonass_slot_map()
        kept = []
        for tle in tles:
            try:
                norad = int(tle['line1'][2:7])
            except (ValueError, KeyError):
                continue
            slot = slot_map.get(norad)
            if not slot:
                continue
            tle['id'] = slot
            tle['label'] = f"R{slot:02d}"
            kept.append(tle)
        kept.sort(key=lambda t: t['id'])
        tles = kept
    elif constellation == 'BEIDOU':
        kept = []
        for tle in tles:
            m = re.search(r'\(C(\d+)\)', tle['name'])
            if not m:
                continue
            prn = int(m.group(1))
            tle['id'] = prn
            tle['label'] = f"C{prn:02d}"
            kept.append(tle)
        kept.sort(key=lambda t: t['id'])
        tles = kept
    elif constellation == 'GALILEO':
        kept = []
        for tle in tles:
            m = re.search(r'GSAT(\d{4})', tle['name'])
            if not m:
                continue
            gsat = int(m.group(1))
            prn = GSAT_TO_PRN.get(gsat)
            if not prn:
                continue
            tle['id'] = prn
            tle['label'] = f"E{prn:02d}"
            kept.append(tle)
        kept.sort(key=lambda t: t['id'])
        tles = kept
    else:
        for i, tle in enumerate(tles):
            tle['id'] = i + 1
            tle['label'] = f"{prefix}{i + 1:02d}"

    entry = {'tles': tles, 'fetched': datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    if constellation == 'GLONASS':
        glo_data = entry
    elif constellation == 'BEIDOU':
        bei_data = entry
    else:
        gal_data = entry

    app.logger.info(f"Browser pushed {len(tles)} {constellation} TLEs")
    return jsonify({'success': True, 'count': len(tles), 'constellation': constellation})


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
            except Exception as e:
                log.warning(f"fetch_almanac: {type(e).__name__}: {e}")
                text = None
            if text:
                try:
                    satellites = parse_yuma(text)
                except Exception as e:
                    log.warning(f"parse_yuma: {type(e).__name__}: {e}")
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
            except Exception as e:
                log.warning(f"propagate_tle({tle.get('label')}): {type(e).__name__}: {e}")

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
