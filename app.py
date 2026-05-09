from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from gps_core import (fetch_almanac, parse_yuma, propagate, geodetic, gps_time_from_datetime,
                      fetch_tle_group, parse_tles, propagate_tle, fetch_glonass_slot_map,
                      fetch_glonass_constellation_status,
                      fetch_gps_rinex, parse_rinex2_nav,
                      fetch_gps_rinex4, parse_rinex4_nav, parse_rinex4_beidou,
                      parse_rinex4_galileo, parse_rinex4_combined, _rinex4_line_iter,
                      propagate_glonass_eph)
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
bei_d_data   = {'ephemeris': {}, 'date': None}  # BeiDou D1/D2 (legacy B1I/B2I/B3I)
bei_cnv1_data = {'ephemeris': {}, 'date': None}  # BeiDou-3 B-CNAV1 (B1C)
bei_cnv2_data = {'ephemeris': {}, 'date': None}  # BeiDou-3 B-CNAV2 (B2a)
bei_cnv3_data = {'ephemeris': {}, 'date': None}  # BeiDou-3 B-CNAV3 (B2b)
gal_inav_data = {'ephemeris': {}, 'date': None}  # Galileo I/NAV (E1-B, E5b-I)
gal_fnav_data = {'ephemeris': {}, 'date': None}  # Galileo F/NAV (E5a-I)
glo_fdma_data = {'ephemeris': {}, 'date': None}  # GLONASS L1OF/L2OF FDMA (state-vector eph)
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


def _load_one_constellation(group, prefix, target):
    """Load a single constellation; returns (target, entry_or_None)."""
    try:
        tles = _load_tle_constellation(group, prefix)
        if tles:
            entry = {'tles': tles, 'fetched': datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            app.logger.info(f"TLE refresh: loaded {len(tles)} {target} satellites")
            return target, entry
        app.logger.warning(f"TLE refresh: failed to load {target}")
    except Exception as e:
        app.logger.warning(f"TLE refresh: exception loading {target}: {e}")
    return target, None


def _tle_refresh_worker():
    """Background thread: fetches all 3 non-GPS constellations in parallel."""
    global glo_data, bei_data, gal_data
    from concurrent.futures import ThreadPoolExecutor, as_completed
    while True:
        jobs = [('glo-ops', 'R', 'glo'), ('beidou', 'C', 'bei'), ('galileo', 'E', 'gal')]
        all_ok = True
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_load_one_constellation, *j): j for j in jobs}
            for f in as_completed(futures):
                target, entry = f.result()
                if entry:
                    if target == 'glo':   glo_data = entry
                    elif target == 'bei': bei_data = entry
                    else:                 gal_data = entry
                else:
                    all_ok = False
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


rinex4_diag = {'last_error': None, 'file_size': None, 'file_exists': None,
               'attempts': 0, 'parsed_counts': None, 'stage': 'init',
               'rss_mb': None}


def _rss_mb():
    try:
        with open('/proc/self/status', 'r') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


def _load_rinex4_json(filename):
    """Load a pre-parsed RINEX 4 JSON committed by the update-tles workflow.
    Returns {'ephemeris': {prn(int): record}, 'date': str} or None."""
    import json, os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    eph_str = blob.get('ephemeris') or {}
    eph = {int(k): v for k, v in eph_str.items()}
    return {'ephemeris': eph, 'date': blob.get('date')}


def _rinex4_refresh_worker():
    """Load pre-parsed RINEX 4 JSON files committed every 6 h by GitHub Actions.
    Parsing the 12 MB raw file inside this process took 30+ minutes on Render's
    free tier — far longer than the spin-down window — so the constellations
    never appeared. The workflow now does the parse in CI and writes small JSONs
    that load in milliseconds. Falls back to streaming the raw file if the
    JSONs are absent (first deploy after this change, or workflow failure)."""
    global rinex4_data, bei_d_data, bei_cnv1_data, bei_cnv2_data, bei_cnv3_data, gal_inav_data, gal_fnav_data, glo_fdma_data, rinex4_diag
    import os, traceback
    rinex4_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'gps_rinex4.txt')
    json_targets = [
        ('rinex4_gps_cnav.json', 'rinex4_data',    'GPS CNAV'),
        ('rinex4_bds_d.json',    'bei_d_data',     'BeiDou D1/D2'),
        ('rinex4_bds_cnv1.json', 'bei_cnv1_data',  'BeiDou CNV1'),
        ('rinex4_bds_cnv2.json', 'bei_cnv2_data',  'BeiDou CNV2'),
        ('rinex4_bds_cnv3.json', 'bei_cnv3_data',  'BeiDou CNV3'),
        ('rinex4_gal_inav.json', 'gal_inav_data',  'Galileo I/NAV'),
        ('rinex4_glo_fdma.json', 'glo_fdma_data',  'GLONASS FDMA'),
        ('rinex4_gal_fnav.json', 'gal_fnav_data',  'Galileo F/NAV'),
    ]
    while True:
        loaded = False
        rinex4_diag['attempts'] += 1
        rinex4_diag['stage'] = 'load_json'
        rinex4_diag['rss_mb'] = _rss_mb()
        try:
            rinex4_diag['file_exists'] = os.path.exists(rinex4_path)
            rinex4_diag['file_size'] = os.path.getsize(rinex4_path) if rinex4_diag['file_exists'] else None
        except Exception as e:
            rinex4_diag['last_error'] = f"stat: {type(e).__name__}: {e}"

        counts = {}
        for fname, varname, label in json_targets:
            try:
                blob = _load_rinex4_json(fname)
                if blob and blob['ephemeris']:
                    globals()[varname] = blob
                    counts[varname] = len(blob['ephemeris'])
                    app.logger.info(f"RINEX4 JSON loaded: {len(blob['ephemeris'])} {label} PRNs from {blob['date']}")
                    loaded = True
                else:
                    counts[varname] = 0
            except Exception as e:
                tb = traceback.format_exc(limit=3)
                rinex4_diag['last_error'] = f"json {fname}: {type(e).__name__}: {e}\n{tb}"
                app.logger.warning(f"RINEX4 JSON load error {fname}: {e}")
        rinex4_diag['parsed_counts'] = counts
        rinex4_diag['rss_mb'] = _rss_mb()

        if loaded:
            rinex4_diag['stage'] = 'done'
            rinex4_diag['last_error'] = None
            time.sleep(6 * 3600)
            continue

        # JSONs missing — fall back to streaming the raw RINEX 4 file. On Render's
        # free tier this is too slow to actually finish, but locally and on
        # capable hosts it works.
        for offset in range(1, 4):
            dt = datetime.now(timezone.utc) - timedelta(days=offset)
            try:
                rinex4_diag['stage'] = f'stream:offset{offset}'
                rinex4_diag['rss_mb'] = _rss_mb()
                date_str = dt.strftime('%Y-%m-%d')
                parsed = parse_rinex4_combined(_rinex4_line_iter(dt), progress=rinex4_diag)
                rinex4_diag['rss_mb'] = _rss_mb()
                rinex4_diag['stage'] = 'assign'
                rinex4_diag['parsed_counts'] = {
                    'gps_cnav': len(parsed['gps_cnav']),
                    'bds_d':    len(parsed['bds_d']),
                    'bds_cnv1': len(parsed['bds_cnv1']),
                    'gal_inav': len(parsed['gal_inav']),
                    'gal_fnav': len(parsed['gal_fnav']),
                    'glo_fdma': len(parsed.get('glo_fdma', {})),
                    'bds_cnv2': len(parsed.get('bds_cnv2', {})),
                    'bds_cnv3': len(parsed.get('bds_cnv3', {})),
                }
                if parsed['gps_cnav']:
                    rinex4_data = {'ephemeris': parsed['gps_cnav'], 'date': date_str}; loaded = True
                if parsed['bds_d']:
                    bei_d_data = {'ephemeris': parsed['bds_d'], 'date': date_str}; loaded = True
                if parsed['bds_cnv1']:
                    bei_cnv1_data = {'ephemeris': parsed['bds_cnv1'], 'date': date_str}; loaded = True
                if parsed.get('bds_cnv2'):
                    bei_cnv2_data = {'ephemeris': parsed['bds_cnv2'], 'date': date_str}; loaded = True
                if parsed.get('bds_cnv3'):
                    bei_cnv3_data = {'ephemeris': parsed['bds_cnv3'], 'date': date_str}; loaded = True
                if parsed['gal_inav']:
                    gal_inav_data = {'ephemeris': parsed['gal_inav'], 'date': date_str}; loaded = True
                if parsed['gal_fnav']:
                    gal_fnav_data = {'ephemeris': parsed['gal_fnav'], 'date': date_str}; loaded = True
                if parsed.get('glo_fdma'):
                    glo_fdma_data = {'ephemeris': parsed['glo_fdma'], 'date': date_str}; loaded = True
                rinex4_diag['stage'] = 'done' if loaded else 'no_data'
                if loaded:
                    rinex4_diag['last_error'] = None
                    break
            except Exception as e:
                tb = traceback.format_exc(limit=3)
                rinex4_diag['last_error'] = f"{type(e).__name__}: {e}\n{tb}"
                rinex4_diag['stage'] = f'error:{type(e).__name__}'
                app.logger.warning(f"RINEX4 refresh error: {e}\n{tb}")
        rinex4_diag['stage'] = f'sleep:{6*3600 if loaded else 120}s'
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
    resp = {
        'label': label, 'constellation': constellation,
        'tle': {'name': tle['name'], 'line1': tle['line1'], 'line2': tle['line2']},
    }
    if constellation == 'BEIDOU':
        prn = tle.get('id')
        d = bei_d_data['ephemeris'].get(prn)
        if d:
            resp['beidou_d'] = {
                'msg_type':  d['msg_type'],
                'toc':       d['epoch'],
                'bdt_week':  d['bdt_week'],
                'aode':      int(d['aode']),
                'aodc':      int(d['aodc']),
                'af0':       d['af0'],
                'af1':       d['af1'],
                'af2':       d['af2'],
                'tgd1':      d['tgd1'],
                'tgd2':      d['tgd2'],
                'ura_index': d['ura_index'],
                'sat_h1':    d['sat_h1'],
                'toe':       d['toe'],
                'sqrt_a':    d['sqrt_a'],
                'e':         d['e'],
                'm0_deg':    math.degrees(d['m0']),
                'delta_n':   d['delta_n'],
                'crs':       d['crs'],
                'cus':       d['cus'],
                'cuc':       d['cuc'],
                'omega0_deg': math.degrees(d['omega0']),
                'i0_deg':    math.degrees(d['i0']),
                'omega_deg': math.degrees(d['omega']),
                'omega_dot': d['omega_dot'],
                'idot':      d['idot'],
                'crc':       d['crc'],
                'cic':       d['cic'],
                'cis':       d['cis'],
                'tx_time':   d['tx_time'],
                'rinex_date': bei_d_data['date'],
            }
        for src_data, key in (
            (bei_cnv2_data, 'beidou_cnv2'),
            (bei_cnv3_data, 'beidou_cnv3'),
        ):
            rec = src_data['ephemeris'].get(prn)
            if rec:
                resp[key] = {
                    'msg_type':    rec['msg_type'],
                    'toc':         rec['epoch'],
                    'af0':         rec['af0'],
                    'af1':         rec['af1'],
                    'af2':         rec['af2'],
                    'isc_b2ad':    rec.get('isc_b2ad', 0.0),
                    'tgd_b1cp':    rec.get('tgd_b1cp', 0.0),
                    'tgd_b2ap':    rec.get('tgd_b2ap', 0.0),
                    'sf_b2bi':     rec.get('sf_b2bi', 0.0),
                    'toe':         rec['toe'],
                    'top':         rec['top'],
                    'sqrt_a':      rec['sqrt_a'],
                    'adot':        rec['adot'],
                    'e':           rec['e'],
                    'm0_deg':      math.degrees(rec['m0']),
                    'delta_n':     rec['delta_n'],
                    'delta_n_dot': rec['delta_n_dot'],
                    'omega0_deg':  math.degrees(rec['omega0']),
                    'i0_deg':      math.degrees(rec['i0']),
                    'omega_deg':   math.degrees(rec['omega']),
                    'omega_dot':   rec['omega_dot'],
                    'idot':        rec['idot'],
                    'crs':         rec['crs'],
                    'cuc':         rec['cuc'],
                    'cus':         rec['cus'],
                    'crc':         rec['crc'],
                    'cic':         rec['cic'],
                    'cis':         rec['cis'],
                    'sat_type':    rec['sat_type'],
                    'sisai_oe':    rec['sisai_oe'],
                    'sisai_ocb':   rec['sisai_ocb'],
                    'sisai_oc1':   rec['sisai_oc1'],
                    'sisai_oc2':   rec['sisai_oc2'],
                    'sismai':      rec['sismai'],
                    'health':      rec['health'],
                    'integrity':   rec['integrity'],
                    'tx_time':     rec['tx_time'],
                    'rinex_date':  src_data['date'],
                }
        cnv1 = bei_cnv1_data['ephemeris'].get(prn)
        if cnv1:
            resp['beidou_cnv1'] = {
                'toc':         cnv1['epoch'],
                'af0':         cnv1['af0'],
                'af1':         cnv1['af1'],
                'af2':         cnv1['af2'],
                'tgd_b1cp':    cnv1['tgd_b1cp'],
                'tgd_b2ap':    cnv1['tgd_b2ap'],
                'isc_b1cd':    cnv1['isc_b1cd'],
                'toe':         cnv1['toe'],
                'top':         cnv1['top'],
                'sqrt_a':      cnv1['sqrt_a'],
                'adot':        cnv1['adot'],
                'e':           cnv1['e'],
                'm0_deg':      math.degrees(cnv1['m0']),
                'delta_n':     cnv1['delta_n'],
                'delta_n_dot': cnv1['delta_n_dot'],
                'omega0_deg':  math.degrees(cnv1['omega0']),
                'i0_deg':      math.degrees(cnv1['i0']),
                'omega_deg':   math.degrees(cnv1['omega']),
                'omega_dot':   cnv1['omega_dot'],
                'idot':        cnv1['idot'],
                'crs':         cnv1['crs'],
                'cuc':         cnv1['cuc'],
                'cus':         cnv1['cus'],
                'crc':         cnv1['crc'],
                'cic':         cnv1['cic'],
                'cis':         cnv1['cis'],
                'sat_type':    cnv1['sat_type'],
                'sisai_oe':    cnv1['sisai_oe'],
                'sisai_ocb':   cnv1['sisai_ocb'],
                'sisai_oc1':   cnv1['sisai_oc1'],
                'sisai_oc2':   cnv1['sisai_oc2'],
                'sismai':      cnv1['sismai'],
                'health':      cnv1['health'],
                'integrity':   cnv1['integrity'],
                'rinex_date':  bei_cnv1_data['date'],
            }
    if constellation == 'GLONASS':
        prn = tle.get('id')
        g = glo_fdma_data['ephemeris'].get(prn)
        if g:
            resp['glonass_fdma'] = {
                'toc':         g['epoch'],
                'tau_n':       g['tau_n'],
                'gamma_n':     g['gamma_n'],
                'tk_msg':      g['tk_msg'],
                'x_km':        g['x_km'],
                'y_km':        g['y_km'],
                'z_km':        g['z_km'],
                'vx_kms':      g['vx_kms'],
                'vy_kms':      g['vy_kms'],
                'vz_kms':      g['vz_kms'],
                'ax_kms2':     g['ax_kms2'],
                'ay_kms2':     g['ay_kms2'],
                'az_kms2':     g['az_kms2'],
                'health':      g['health'],
                'health_flags': g['health_flags'],
                'freq_num':    g['freq_num'],
                'age_op':      g['age_op'],
                'status_flags': g['status_flags'],
                'delta_tau':   g['delta_tau'],
                'urai':        g['urai'],
                'rinex_date':  glo_fdma_data['date'],
            }
    if constellation == 'GALILEO':
        prn = tle.get('id')
        for src_name, src_data, key in (
            ('inav', gal_inav_data, 'galileo_inav'),
            ('fnav', gal_fnav_data, 'galileo_fnav'),
        ):
            rec = src_data['ephemeris'].get(prn)
            if rec:
                resp[key] = {
                    'msg_type':     rec['msg_type'],
                    'toc':          rec['epoch'],
                    'gal_week':     rec['gal_week'],
                    'iodnav':       rec['iodnav'],
                    'data_sources': rec['data_sources'],
                    'sisa':         rec['sisa'],
                    'sv_health':    rec['sv_health'],
                    'bgd_e5a_e1':   rec['bgd_e5a_e1'],
                    'bgd_e5b_e1':   rec['bgd_e5b_e1'],
                    'af0':          rec['af0'],
                    'af1':          rec['af1'],
                    'af2':          rec['af2'],
                    'toe':          rec['toe'],
                    'sqrt_a':       rec['sqrt_a'],
                    'e':            rec['e'],
                    'm0_deg':       math.degrees(rec['m0']),
                    'delta_n':      rec['delta_n'],
                    'crs':          rec['crs'],
                    'cus':          rec['cus'],
                    'cuc':          rec['cuc'],
                    'omega0_deg':   math.degrees(rec['omega0']),
                    'i0_deg':       math.degrees(rec['i0']),
                    'omega_deg':    math.degrees(rec['omega']),
                    'omega_dot':    rec['omega_dot'],
                    'idot':         rec['idot'],
                    'crc':          rec['crc'],
                    'cic':          rec['cic'],
                    'cis':          rec['cis'],
                    'tx_time':      rec['tx_time'],
                    'rinex_date':   src_data['date'],
                }
    return jsonify(resp)


@app.route('/api/rinex-status', methods=['GET'])
def rinex_status():
    return jsonify({
        'lnav':  {'loaded': bool(rinex_data['ephemeris']),    'date': rinex_data['date'],    'prns': len(rinex_data['ephemeris'])},
        'cnav':  {'loaded': bool(rinex4_data['ephemeris']),   'date': rinex4_data['date'],   'prns': len(rinex4_data['ephemeris'])},
        'bds_d':    {'loaded': bool(bei_d_data['ephemeris']),    'date': bei_d_data['date'],    'prns': len(bei_d_data['ephemeris'])},
        'bds_cnv1': {'loaded': bool(bei_cnv1_data['ephemeris']), 'date': bei_cnv1_data['date'], 'prns': len(bei_cnv1_data['ephemeris'])},
        'gal_inav': {'loaded': bool(gal_inav_data['ephemeris']), 'date': gal_inav_data['date'], 'prns': len(gal_inav_data['ephemeris'])},
        'gal_fnav': {'loaded': bool(gal_fnav_data['ephemeris']), 'date': gal_fnav_data['date'], 'prns': len(gal_fnav_data['ephemeris'])},
        'rinex4_diag': rinex4_diag,
    })


@app.route('/api/refresh-data', methods=['POST'])
def refresh_data_endpoint():
    """Run the local data-refresh (TLEs, RINEX 2, RINEX 4, re-parse JSONs).
    Useful when running on a machine that has bandwidth/CPU headroom (i.e.
    your PC). Reload the in-memory caches afterwards."""
    try:
        from refresh_data import refresh_all
        lines = []
        summary = refresh_all(log_fn=lines.append)
        # Reload in-memory caches without restart
        try:
            global rinex_data
            rpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'data', 'gps_rinex2.txt')
            with open(rpath, 'r', encoding='utf-8') as f:
                eph = parse_rinex2_nav(f.read())
            if eph:
                from datetime import datetime as _dt
                rinex_data = {'ephemeris': eph,
                              'date': _dt.utcfromtimestamp(os.path.getmtime(rpath)).strftime('%Y-%m-%d')}
                lines.append(f"reload rinex2 OK: {len(eph)} PRNs")
        except Exception as e:
            lines.append(f"reload rinex2 fail: {e}")
        try:
            for fname, varname, _ in [
                ('rinex4_gps_cnav.json', 'rinex4_data',    'GPS CNAV'),
                ('rinex4_bds_d.json',    'bei_d_data',     'BeiDou D1/D2'),
                ('rinex4_bds_cnv1.json', 'bei_cnv1_data',  'BeiDou CNV1'),
                ('rinex4_bds_cnv2.json', 'bei_cnv2_data',  'BeiDou CNV2'),
                ('rinex4_bds_cnv3.json', 'bei_cnv3_data',  'BeiDou CNV3'),
                ('rinex4_gal_inav.json', 'gal_inav_data',  'Galileo I/NAV'),
                ('rinex4_glo_fdma.json', 'glo_fdma_data',  'GLONASS FDMA'),
                ('rinex4_gal_fnav.json', 'gal_fnav_data',  'Galileo F/NAV'),
            ]:
                blob = _load_rinex4_json(fname)
                if blob and blob['ephemeris']:
                    globals()[varname] = blob
        except Exception as e:
            lines.append(f"reload rinex4 fail: {e}")
        return jsonify({'ok': True, 'summary': summary, 'log': lines})
    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/ionosphere', methods=['GET'])
def ionosphere_data():
    """Return broadcast ionospheric model coefficients from RINEX 4:
    Klobuchar (GPS), NeQuick-G (Galileo), BDGIM (BeiDou)."""
    import os, json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', 'rinex4_iono.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            blob = json.load(f)
        blob['mtime'] = int(os.path.getmtime(path))
        return jsonify(blob)
    except FileNotFoundError:
        return jsonify({'iono': {'klobuchar': None, 'nequick': None, 'bdgim': None},
                        'date': None, 'mtime': None}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/gps-almanac', methods=['GET'])
def gps_almanac():
    """All 32 GPS SV slots from the loaded YUMA almanac."""
    sats = almanac_data.get('satellites') or []
    out = {}
    for s in sats:
        try:
            out[s['id']] = {
                'prn':       s['id'],
                'health':    int(s.get('health', 0)),
                'e':         float(s.get('e', 0.0)),
                'sqrt_a':    float(s.get('sqA', 0.0)),
                'inc_deg':   math.degrees(s.get('inc', 0.0)),
                'omega_deg': math.degrees(s.get('w', 0.0)),
                'omega0_deg': math.degrees(s.get('Om0', 0.0)),
                'm0_deg':    math.degrees(s.get('M0', 0.0)),
                'toa':       float(s.get('toa', 0.0)),
                'gps_week':  int(s.get('wk', 0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return jsonify({'slots': out, 'date': almanac_data.get('date')})


_glo_almanac_cache = {'data': {}, 'ts': 0}

@app.route('/api/glonass-almanac', methods=['GET'])
def glonass_almanac():
    """Full GLONASS constellation status (all 24 slots) — IAC source, cached
    for 30 minutes to avoid hammering glonass-iac.ru."""
    import time as _t
    if _t.time() - _glo_almanac_cache['ts'] > 1800 or not _glo_almanac_cache['data']:
        try:
            _glo_almanac_cache['data'] = fetch_glonass_constellation_status()
            _glo_almanac_cache['ts'] = _t.time()
        except Exception as e:
            return jsonify({'error': str(e), 'slots': {}}), 200
    return jsonify({'slots': _glo_almanac_cache['data'],
                    'fetched_ts': int(_glo_almanac_cache['ts'])})


@app.route('/api/system-time', methods=['GET'])
def system_time_data():
    """Return RINEX 4 STO records (inter-system time offsets) and EOP
    (Earth Orientation Parameters from GPS CNAV MT 32)."""
    import os, json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', 'rinex4_systime.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            blob = json.load(f)
        blob['mtime'] = int(os.path.getmtime(path))
        return jsonify(blob)
    except FileNotFoundError:
        return jsonify({'sto': {}, 'eop': None, 'date': None, 'mtime': None}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/data-freshness', methods=['GET'])
def data_freshness():
    """Return mtimes of the data files so the UI can show 'last updated' tags."""
    import os
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    files = {
        'gps_lnav':  'gps_rinex2.txt',
        'gps_cnav':  'rinex4_gps_cnav.json',
        'bds_d':     'rinex4_bds_d.json',
        'bds_cnv1':  'rinex4_bds_cnv1.json',
        'bds_cnv2':  'rinex4_bds_cnv2.json',
        'bds_cnv3':  'rinex4_bds_cnv3.json',
        'gal_inav':  'rinex4_gal_inav.json',
        'gal_fnav':  'rinex4_gal_fnav.json',
        'glo_fdma':  'rinex4_glo_fdma.json',
        'tle_gps':   'gps.tle',
        'tle_glo':   'glo-ops.tle',
        'tle_bds':   'beidou.tle',
        'tle_gal':   'galileo.tle',
    }
    out = {}
    for key, fname in files.items():
        path = os.path.join(base, fname)
        try:
            out[key] = int(os.path.getmtime(path))
        except OSError:
            out[key] = None
    return jsonify(out)


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

    # Optional ?at=<iso8601> freezes propagation at a chosen UTC instant.
    # Lets the frontend pin satellite state to a specific moment for analysis.
    at_str = request.args.get('at')
    frozen = False
    if at_str:
        try:
            now = datetime.fromisoformat(at_str.replace('Z', '+00:00'))
            now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
            frozen = True
        except ValueError:
            return jsonify({'error': f'Invalid "at" timestamp: {at_str!r}'}), 400
    else:
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
                pos = None
                # GLONASS: prefer broadcast ephemeris (state-vector + RK4) when
                # available; fall back to TLE/SGP4 for sats not in the RINEX 4
                # file or when the JSON hasn't loaded yet.
                if constellation == 'GLONASS' and glo_fdma_data['ephemeris']:
                    eph = glo_fdma_data['ephemeris'].get(int(tle['id']))
                    if eph:
                        pos = propagate_glonass_eph(eph, now)
                if pos is None:
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
                log.warning(f"propagate({constellation} {tle.get('label')}): {type(e).__name__}: {e}")

    tos = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return jsonify({
        'time': now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        'time_iso': now.isoformat().replace('+00:00', 'Z'),
        'tos': round(tos, 3),
        'frozen': frozen,
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
