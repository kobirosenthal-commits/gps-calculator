from flask import Flask, render_template, request, jsonify, g, url_for
from datetime import datetime, timedelta, timezone
from functools import wraps
from werkzeug.exceptions import HTTPException
from gps_core import (fetch_almanac, parse_yuma, propagate, geodetic, gps_time_from_datetime,
                      fetch_tle_group, parse_tles, propagate_tle, fetch_glonass_slot_map,
                      fetch_glonass_constellation_status,
                      fetch_gps_rinex, parse_rinex2_nav,
                      fetch_gps_rinex4, parse_rinex4_nav, parse_rinex4_beidou,
                      parse_rinex4_galileo, parse_rinex4_combined, _rinex4_line_iter,
                      propagate_glonass_eph)
import hmac
import json
import logging
log = logging.getLogger(__name__)
import math
import os
import re
import threading
import time
import uuid

# Surface INFO-level logs from gps_core (TLE fetch attempts) in gunicorn's stream
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
logging.getLogger('gps_core').setLevel(logging.INFO)

app = Flask(__name__)

# Reject request bodies above this size before they're ever read into memory
# (Werkzeug enforces this against the Content-Length header — and aborts a
# chunked/streamed body once it's exceeded — so an oversized payload never
# gets fully buffered or JSON-parsed just to be rejected afterwards). Every
# JSON body this app accepts (TLE pushes, almanac loads, calculate requests)
# is comfortably under 1 MB; this is generous headroom above the largest of
# those (see _PUSH_TLES_MAX_TEXT_BYTES) while still blocking abusive uploads.
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2 MB


# ─── Versioned static assets ─────────────────────────────────────────────────
# `asset_url('css/cesium.css')` builds a normal `/static/...` URL with a
# `?v=<mtime>` cache-busting suffix, so browsers/CDNs can cache these files
# aggressively without needing a manual rename every time one changes.
@app.context_processor
def _inject_asset_url():
    def asset_url(filename):
        path = os.path.join(app.static_folder or 'static', filename)
        try:
            version = int(os.path.getmtime(path))
        except OSError:
            version = 0
        return f"{url_for('static', filename=filename)}?v={version}"
    return dict(asset_url=asset_url)


# ─── Security headers ────────────────────────────────────────────────────────
# Applied to every response (API JSON and HTML pages alike). The CSP is scoped
# to what this app actually loads: CesiumJS + its bundled assets from
# cesium.com (see templates/cesium.html), same-origin scripts/styles/static
# assets, and same-origin XHR/fetch for the /api/* endpoints plus the TLE
# fallback fetches to github/celestrak (see fetchTleGroup in cesium.js).
# 'unsafe-inline' remains necessary for script-src/style-src because the
# Cesium page uses many inline onclick="..." handlers and inline style
# attributes rather than a nonce/hash scheme — removing those is a much
# larger refactor than this pass; 'unsafe-eval'/blob: remain necessary
# because CesiumJS bootstraps its own web workers via blob: URLs internally.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cesium.com 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' https://cesium.com 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https://cesium.com; "
    "connect-src 'self' https:; "
    "worker-src 'self' blob:; "
    "child-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


@app.after_request
def _add_security_headers(response):
    response.headers.setdefault('Content-Security-Policy', _CSP)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault(
        'Permissions-Policy',
        'geolocation=(self), camera=(), microphone=(), payment=(), usb=()'
    )
    return response


# ─── Test / no-network control ──────────────────────────────────────────────
# Importing this module must never have side effects (no threads, no sockets).
# Background refresh workers are started explicitly by main.py (the real
# production entrypoint) via start_background_workers(). Anything that imports
# `app` directly — pytest, a REPL, another script — gets a plain Flask app
# with empty caches and no network activity, which is what makes the app
# testable without mocking out threads or DNS.
#
# GPS_CALCULATOR_NO_WORKERS=1 additionally lets an operator running main.py
# itself (e.g. in CI, or offline) opt out of the background workers.
_workers_started = False
_workers_lock = threading.Lock()


def _workers_disabled_by_env():
    return os.environ.get('GPS_CALCULATOR_NO_WORKERS', '').strip().lower() in ('1', 'true', 'yes')


# ─── Thread-safe cache writes ───────────────────────────────────────────────
# almanac_data / rinex*_data / glo_data / bei_data / gal_data are simple dicts
# read by request handlers and written wholesale (never mutated in place) by
# both background workers and a few endpoints (load_almanac, push_tles,
# refresh_data_endpoint). Because each update rebinds the module-level name to
# a brand-new dict, readers always see a complete, internally-consistent
# snapshot — CPython guarantees a name rebind is atomic — so no lock is needed
# to prevent torn reads. `_cache_lock` instead serializes *writers* so that a
# refresh triggered from an HTTP request and a background worker refresh can
# never interleave their multi-step updates (e.g. writing rinex4_data and its
# sibling *_data caches) and clobber one another's results.
_cache_lock = threading.RLock()

# ─── Refresh metrics (structured timing) ────────────────────────────────────
# Lightweight in-memory counters/timings for the background refresh workers
# and the manual /api/refresh-data trigger. Exposed read-only via
# /api/data-freshness (key "refresh_metrics") for operational visibility.
# Every refresh cycle — successful or not — also emits one structured log
# line via _record_refresh_metric so refresh health is visible in logs even
# without hitting the API.
_metrics_lock = threading.Lock()
_refresh_metrics = {
    'tle':                   {'runs': 0, 'successes': 0, 'last_finished': None,
                               'last_duration_ms': None, 'last_success': None, 'last_error': None},
    'rinex2':                {'runs': 0, 'successes': 0, 'last_finished': None,
                               'last_duration_ms': None, 'last_success': None, 'last_error': None},
    'rinex4':                {'runs': 0, 'successes': 0, 'last_finished': None,
                               'last_duration_ms': None, 'last_success': None, 'last_error': None},
    'refresh_data_endpoint': {'runs': 0, 'successes': 0, 'last_finished': None,
                               'last_duration_ms': None, 'last_success': None, 'last_error': None},
}


def _record_refresh_metric(name, *, duration_ms, success, error=None):
    """Update _refresh_metrics[name] and emit one structured log line. Shared
    by every background refresh worker and the manual /api/refresh-data
    endpoint so all data-refresh paths are observable the same way."""
    with _metrics_lock:
        m = _refresh_metrics[name]
        m['runs'] += 1
        if success:
            m['successes'] += 1
        m['last_finished'] = datetime.now(timezone.utc).isoformat()
        m['last_duration_ms'] = round(duration_ms, 1)
        m['last_success'] = success
        m['last_error'] = error
    log.info(f"refresh_cycle name={name} success={success} duration_ms={duration_ms:.1f}"
             + (f" error={error!r}" if error else ""))


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

# The set of constellations the app understands. Any request that names a
# constellation outside this set (typos, stale clients, probing) is rejected
# with a 400 rather than silently falling back to GPS or another default.
VALID_CONSTELLATIONS = {'GPS', 'GLONASS', 'BEIDOU', 'GALILEO'}


def _reject_unknown_constellation(constellation):
    """Return a (response, status) tuple if `constellation` isn't one the app
    supports, else None. Callers should `return` the tuple immediately."""
    if constellation not in VALID_CONSTELLATIONS:
        return jsonify({'error': f'Unknown constellation: {constellation!r}. '
                                  f'Expected one of {sorted(VALID_CONSTELLATIONS)}'}), 400
    return None


def _payload_str(payload, key, default=''):
    """Fetch `key` from a parsed JSON body as a string, coercing any other
    JSON type (int, float, bool, list, dict, null) to `default` instead of
    letting it flow into a `.upper()`/`.lower()` call downstream and raise an
    unhandled AttributeError. Only appropriate when *any* string value
    (including the empty string) is subsequently validated against an
    allow-list — so a wrong-typed input still ends up rejected with 400, just
    via that allow-list check rather than a dedicated type error. Do not use
    this for a field whose default is itself a value that would silently
    succeed (see `_constellation_from_payload`)."""
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def _constellation_from_payload(payload, default='GPS'):
    """Extract and validate the 'constellation' field from a JSON body.
    Absent -> `default`. Present but not a string -> reject with 400 instead
    of silently substituting the default: `_payload_str` can't be used here
    because 'GPS' (the usual default) is itself a *valid* constellation, so
    silently coercing a wrong-typed value to it would mask a real client bug
    (e.g. `{"constellation": 123}`) behind an unrelated success or error
    instead of a clear, honest 400.

    Returns (uppercased_value, error_response_or_None); callers should
    `return` the error response immediately if it isn't None."""
    if 'constellation' not in payload:
        return default.upper(), None
    value = payload.get('constellation')
    if not isinstance(value, str):
        return None, (jsonify({'error': 'Invalid "constellation": must be a string'}), 400)
    return value.upper(), None


def _payload_int(payload, key, default=0):
    """Fetch `key` from a parsed JSON body as an int, returning None if it
    can't be cleanly converted — including non-finite floats (Infinity/NaN,
    which the `json` module happily decodes even though they aren't valid
    JSON) that would otherwise raise an uncaught OverflowError past a plain
    `int(...)` call. Callers should treat a None return as a 400."""
    value = payload.get(key, default)
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return None


# ─── Admin authentication ───────────────────────────────────────────────────
# /api/refresh-data triggers outbound network fetches to several upstream
# hosts and rewrites files under data/ — an expensive, abusable action that
# must not be reachable by arbitrary visitors of the public page. It is gated
# behind a shared-secret admin token.
#
# Configure via the ADMIN_TOKEN environment variable and send it either as
# header `X-Admin-Token: <token>` or `Authorization: Bearer <token>`.
# Fails closed: if ADMIN_TOKEN isn't configured, the endpoint is disabled
# (503) rather than silently open, unless GPS_ALLOW_UNAUTHENTICATED_ADMIN=1 is
# set for local development convenience.
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '').strip()


def _admin_token_configured():
    return bool(ADMIN_TOKEN)


def _extract_admin_token(req):
    token = req.headers.get('X-Admin-Token', '')
    if not token:
        auth = req.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[len('Bearer '):]
    return token


def _admin_authorized(req):
    if not _admin_token_configured():
        return os.environ.get('GPS_ALLOW_UNAUTHENTICATED_ADMIN', '').strip().lower() in ('1', 'true', 'yes')
    supplied = _extract_admin_token(req)
    return bool(supplied) and hmac.compare_digest(supplied, ADMIN_TOKEN)


def require_admin(fn):
    """Gate an endpoint behind the ADMIN_TOKEN shared secret (see above)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _admin_token_configured():
            if not _admin_authorized(request):
                return jsonify({'error': 'Admin endpoint disabled: set the ADMIN_TOKEN '
                                          'environment variable to enable it'}), 503
        elif not _admin_authorized(request):
            return jsonify({'error': 'Unauthorized: valid X-Admin-Token header required'}), 401
        return fn(*args, **kwargs)
    return wrapper


almanac_data = {'satellites': [], 'date': None, 'week': None, 'toa': None}
rinex_data   = {'ephemeris': {}, 'date': None}
rinex4_data  = {'ephemeris': {}, 'date': None}
bei_d_data   = {'ephemeris': {}, 'date': None}  # BeiDou D1/D2 (legacy B1I/B2I/B3I)
bei_cnv1_data = {'ephemeris': {}, 'date': None}  # BeiDou-3 B-CNAV1 (B1C)
bei_cnv2_data = {'ephemeris': {}, 'date': None}  # BeiDou-3 B-CNAV2 (B2a)
bei_cnv3_data = {'ephemeris': {}, 'date': None}  # BeiDou-3 B-CNAV3 (B2b)
gal_inav_data = {'ephemeris': {}, 'date': None}  # Galileo I/NAV (E1-B, E5b-I)
gal_fnav_data = {'ephemeris': {}, 'date': None}  # Galileo F/NAV (E5a-I)
gps_cnv2_data = {'ephemeris': {}, 'date': None}  # GPS L1C (Block IIIA)
glo_fdma_data = {'ephemeris': {}, 'date': None}  # GLONASS L1OF/L2OF FDMA (state-vector eph)
glo_data = {'tles': [], 'fetched': None}
bei_data = {'tles': [], 'fetched': None}
gal_data = {'tles': [], 'fetched': None}


def _beidou_id_from_name(name):
    """Extract the real BeiDou PRN (Cnn) from a TLE name like
    'BEIDOU-3 M21 (C29)'. Returns int or None if not present."""
    m = re.search(r'\(C(\d+)\)', name)
    return int(m.group(1)) if m else None


def _galileo_id_from_name(name):
    """Extract the real Galileo PRN via the GSAT serial embedded in the TLE
    name, e.g. 'GSAT0201 (GALILEO 5)' -> GSAT 201 -> PRN via GSAT_TO_PRN.
    Returns int or None if the GSAT serial is missing or unmapped (e.g. a
    satellite that launched after GSAT_TO_PRN was last updated)."""
    m = re.search(r'GSAT(\d{4})', name)
    if not m:
        return None
    return GSAT_TO_PRN.get(int(m.group(1)))


def _load_tle_constellation(group, label_prefix):
    """Fetch, parse and label TLEs for a constellation. Returns list or None.

    Identity mapping must be *stable* (same satellite -> same id/label across
    refreshes) and must match the PRNs used to key RINEX ephemeris, or
    satellite-detail lookups silently return the wrong (or no) ephemeris:
      - GLONASS: NORAD ID -> orbital slot (R01-R24) via the Russian IAC feed.
      - BeiDou:  PRN parsed from the TLE name, e.g. '... (C29)' -> C29.
      - Galileo: GSAT serial parsed from the TLE name, mapped to PRN via
        GSAT_TO_PRN, e.g. 'GSAT0201 (...)' -> E18.
    Any TLE whose identity can't be determined is dropped rather than given a
    made-up sequential id, so displayed labels always correspond to the
    correct real-world satellite and its ephemeris.
    """
    try:
        text = fetch_tle_group(group)
        if not text:
            return None
        tles = parse_tles(text)
        if not tles:
            return None

        if label_prefix == 'R':
            slot_map = fetch_glonass_slot_map()
            if not slot_map:
                # The IAC mapping service is down/unreachable — we have no
                # reliable way to turn NORAD IDs into orbital slots, so treat
                # this as a hard failure (keep whatever data was previously
                # cached) instead of silently dropping every satellite and
                # returning an empty-but-"successful" list.
                log.warning("_load_tle_constellation(glo-ops): GLONASS slot map "
                            "unavailable — cannot map NORAD IDs to slots, refresh skipped")
                return None
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
            if not kept:
                log.warning("_load_tle_constellation(glo-ops): slot map fetched but "
                            "matched none of the current TLEs — refresh skipped")
                return None
            kept.sort(key=lambda t: t['id'])
            return kept

        if label_prefix == 'C':
            kept = []
            for tle in tles:
                prn = _beidou_id_from_name(tle['name'])
                if prn is None:
                    continue
                tle['id'] = prn
                tle['label'] = f"C{prn:02d}"
                kept.append(tle)
            if not kept:
                log.warning("_load_tle_constellation(beidou): no TLE names matched "
                            "the '(Cnn)' PRN pattern — refresh skipped")
                return None
            kept.sort(key=lambda t: t['id'])
            return kept

        if label_prefix == 'E':
            kept = []
            for tle in tles:
                prn = _galileo_id_from_name(tle['name'])
                if prn is None:
                    continue
                tle['id'] = prn
                tle['label'] = f"E{prn:02d}"
                kept.append(tle)
            if not kept:
                log.warning("_load_tle_constellation(galileo): no TLE names matched a "
                            "known GSAT serial — refresh skipped")
                return None
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
        t0 = time.perf_counter()
        jobs = [('glo-ops', 'R', 'glo'), ('beidou', 'C', 'bei'), ('galileo', 'E', 'gal')]
        all_ok = True
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_load_one_constellation, *j): j for j in jobs}
            for f in as_completed(futures):
                target, entry = f.result()
                if entry:
                    with _cache_lock:
                        if target == 'glo':   glo_data = entry
                        elif target == 'bei': bei_data = entry
                        else:                 gal_data = entry
                else:
                    all_ok = False
        _record_refresh_metric('tle', duration_ms=(time.perf_counter() - t0) * 1000, success=all_ok,
                                error=None if all_ok else 'one or more constellations failed to load')
        time.sleep(6 * 3600 if all_ok else 120)


def _rinex_refresh_worker():
    global rinex_data
    while True:
        t0 = time.perf_counter()
        loaded = False
        last_err = None
        for offset in range(3):
            dt = datetime.now(timezone.utc) - timedelta(days=offset)
            try:
                text = fetch_gps_rinex(dt)
                if text:
                    diag = {}
                    eph = parse_rinex2_nav(text, diag=diag)
                    rinex2_diag.clear()
                    rinex2_diag.update(diag)
                    if eph:
                        with _cache_lock:
                            rinex_data = {'ephemeris': eph, 'date': dt.strftime('%Y-%m-%d')}
                        app.logger.info(f"RINEX loaded: {len(eph)} PRNs from {dt.strftime('%Y-%m-%d')}")
                        loaded = True
                        break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                app.logger.warning(f"RINEX refresh error: {e}")
        _record_refresh_metric('rinex2', duration_ms=(time.perf_counter() - t0) * 1000, success=loaded,
                                error=None if loaded else (last_err or 'no valid RINEX2 found in lookback window'))
        time.sleep(6 * 3600 if loaded else 120)

# Malformed/truncated-record diagnostics from the last RINEX 2 parse attempt
# (see gps_core.parse_rinex2_nav's `diag` parameter) — exposed read-only via
# /api/rinex-status so a stream that "loads" but silently drops most of its
# records is visible, not just a loaded/not-loaded boolean.
rinex2_diag = {}

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
    global rinex4_data, bei_d_data, bei_cnv1_data, bei_cnv2_data, bei_cnv3_data, gal_inav_data, gal_fnav_data, glo_fdma_data, gps_cnv2_data, rinex4_diag
    import traceback
    rinex4_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'gps_rinex4.txt')
    json_targets = [
        ('rinex4_gps_cnav.json', 'rinex4_data',    'GPS CNAV'),
        ('rinex4_gps_cnv2.json', 'gps_cnv2_data',  'GPS CNV2 (L1C)'),
        ('rinex4_bds_d.json',    'bei_d_data',     'BeiDou D1/D2'),
        ('rinex4_bds_cnv1.json', 'bei_cnv1_data',  'BeiDou CNV1'),
        ('rinex4_bds_cnv2.json', 'bei_cnv2_data',  'BeiDou CNV2'),
        ('rinex4_bds_cnv3.json', 'bei_cnv3_data',  'BeiDou CNV3'),
        ('rinex4_gal_inav.json', 'gal_inav_data',  'Galileo I/NAV'),
        ('rinex4_glo_fdma.json', 'glo_fdma_data',  'GLONASS FDMA'),
        ('rinex4_gal_fnav.json', 'gal_fnav_data',  'Galileo F/NAV'),
    ]
    while True:
        t0 = time.perf_counter()
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
                    with _cache_lock:
                        globals()[varname] = blob
                    counts[varname] = len(blob['ephemeris'])
                    app.logger.info(f"RINEX4 JSON loaded: {len(blob['ephemeris'])} {label} PRNs from {blob['date']}")
                    loaded = True
                else:
                    counts[varname] = 0
            except Exception as e:
                # rinex4_diag is exposed read-only via /api/rinex-status and
                # /api/data-freshness (both public, unauthenticated) — keep
                # the traceback out of the dict entirely and only log it.
                tb = traceback.format_exc(limit=3)
                rinex4_diag['last_error'] = f"json {fname}: {type(e).__name__}: {e}"
                app.logger.warning(f"RINEX4 JSON load error {fname}: {e}\n{tb}")
        rinex4_diag['parsed_counts'] = counts
        rinex4_diag['rss_mb'] = _rss_mb()

        if loaded:
            rinex4_diag['stage'] = 'done'
            rinex4_diag['last_error'] = None
            _record_refresh_metric('rinex4', duration_ms=(time.perf_counter() - t0) * 1000, success=True)
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
                    with _cache_lock:
                        rinex4_data = {'ephemeris': parsed['gps_cnav'], 'date': date_str}
                    loaded = True
                if parsed['bds_d']:
                    with _cache_lock:
                        bei_d_data = {'ephemeris': parsed['bds_d'], 'date': date_str}
                    loaded = True
                if parsed['bds_cnv1']:
                    with _cache_lock:
                        bei_cnv1_data = {'ephemeris': parsed['bds_cnv1'], 'date': date_str}
                    loaded = True
                if parsed.get('bds_cnv2'):
                    with _cache_lock:
                        bei_cnv2_data = {'ephemeris': parsed['bds_cnv2'], 'date': date_str}
                    loaded = True
                if parsed.get('bds_cnv3'):
                    with _cache_lock:
                        bei_cnv3_data = {'ephemeris': parsed['bds_cnv3'], 'date': date_str}
                    loaded = True
                if parsed['gal_inav']:
                    with _cache_lock:
                        gal_inav_data = {'ephemeris': parsed['gal_inav'], 'date': date_str}
                    loaded = True
                if parsed['gal_fnav']:
                    with _cache_lock:
                        gal_fnav_data = {'ephemeris': parsed['gal_fnav'], 'date': date_str}
                    loaded = True
                if parsed.get('glo_fdma'):
                    with _cache_lock:
                        glo_fdma_data = {'ephemeris': parsed['glo_fdma'], 'date': date_str}
                    loaded = True
                rinex4_diag['stage'] = 'done' if loaded else 'no_data'
                if loaded:
                    rinex4_diag['last_error'] = None
                    break
            except Exception as e:
                tb = traceback.format_exc(limit=3)
                rinex4_diag['last_error'] = f"{type(e).__name__}: {e}"
                rinex4_diag['stage'] = f'error:{type(e).__name__}'
                app.logger.warning(f"RINEX4 refresh error: {e}\n{tb}")
        rinex4_diag['stage'] = f'sleep:{6*3600 if loaded else 120}s'
        _record_refresh_metric('rinex4', duration_ms=(time.perf_counter() - t0) * 1000, success=loaded,
                                error=None if loaded else (rinex4_diag.get('last_error') or 'no RINEX4 data available'))
        time.sleep(6 * 3600 if loaded else 120)


def start_background_workers():
    """Start the daemon threads that keep TLE/RINEX caches fresh.

    Must be called explicitly (from main.py, the production entrypoint) —
    importing this module never starts network activity on its own, so it
    stays safe to import from tests, scripts, or a REPL. Idempotent: calling
    it more than once is a no-op. Set GPS_CALCULATOR_NO_WORKERS=1 to opt out
    (e.g. offline/CI runs of main.py itself)."""
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        if _workers_disabled_by_env():
            log.info("start_background_workers: GPS_CALCULATOR_NO_WORKERS set — skipping")
            return
        if app.config.get('TESTING'):
            log.info("start_background_workers: app.config['TESTING'] set — skipping")
            return
        threading.Thread(target=_tle_refresh_worker, daemon=True).start()
        threading.Thread(target=_rinex_refresh_worker, daemon=True).start()
        threading.Thread(target=_rinex4_refresh_worker, daemon=True).start()
        _workers_started = True
        log.info("start_background_workers: TLE/RINEX2/RINEX4 refresh threads started")


def _request_id():
    """Stable per-request correlation ID: echoes a client-supplied
    X-Request-Id (so an ID assigned by an upstream proxy/load balancer
    threads straight through) or mints a fresh one otherwise. Attached to
    every structured log line and every error response body so an operator
    can find the exact server-side log lines for a user-reported failure
    without ever needing to expose the underlying exception to the client."""
    return request.headers.get('X-Request-Id') or uuid.uuid4().hex


def _json_error_response(status_code, message):
    """Build the {'error', 'request_id'} JSON body every API error response
    uses — 400/413/415/500 alike — so a client never has to special-case an
    HTML error page vs. a JSON one, and every failure is correlatable to a
    server log line via the same request_id."""
    return jsonify({'error': message, 'request_id': getattr(g, 'request_id', None)}), status_code


@app.errorhandler(HTTPException)
def _api_http_exception(err):
    """Render every HTTPException Werkzeug/Flask raises itself — malformed
    JSON (400), wrong Content-Type (415), oversized body (413), unknown
    route (404), and so on — as JSON for /api/ routes instead of the default
    HTML error page, matching the plain-JSON contract the rest of this API
    already uses. Non-API routes (the HTML pages) keep normal HTML
    rendering."""
    if not request.path.startswith('/api/'):
        return err
    message = err.description or err.name or 'Request error'
    return _json_error_response(err.code or 500, message)


@app.errorhandler(500)
def _api_500(err):
    """Return a generic JSON error for unhandled API 500s — never the raw
    exception message or a traceback, either of which can leak internals
    (file paths, arguments, third-party library error text) to a public
    caller. Full details still go to the server log, tagged with the same
    request_id returned to the client, so an operator can find the exact
    failure without exposing it."""
    if request.path.startswith('/api/'):
        rid = getattr(g, 'request_id', None)
        app.logger.exception(f"Unhandled error in {request.path} request_id={rid}")
        return _json_error_response(500, 'Internal server error')
    return err


# ─── Structured per-request timing logs ─────────────────────────────────────
# Every request gets one structured log line (method/path/status/duration_ms)
# on completion. This is deliberately simple (no external metrics backend)
# but gives the same operational visibility as the refresh-cycle metrics
# above, using the same log stream gunicorn already captures.
@app.before_request
def _time_request_start():
    request._perf_start = time.perf_counter()
    g.request_id = _request_id()


@app.after_request
def _time_request_end(response):
    request_id = getattr(g, 'request_id', None)
    if request_id:
        response.headers.setdefault('X-Request-Id', request_id)
    start = getattr(request, '_perf_start', None)
    if start is not None:
        duration_ms = (time.perf_counter() - start) * 1000
        log.info(f"http_request method={request.method} path={request.path} "
                 f"status={response.status_code} duration_ms={duration_ms:.1f} "
                 f"request_id={request_id}")
    return response


@app.route('/healthz', methods=['GET'])
def healthz():
    """Liveness probe: is the process up and able to handle a request at all?

    Deliberately checks nothing else — no cache state, no file I/O, no
    upstream network — so a slow upstream fetch or a temporarily stale cache
    never causes an orchestrator to kill/restart an otherwise-healthy
    process. Use /readyz to check whether there's actual data to serve."""
    return jsonify({'status': 'ok'}), 200


@app.route('/readyz', methods=['GET'])
def readyz():
    """Readiness probe: does the app have at least some satellite data to
    serve? Mirrors the guard already used by /api/live-positions. Returns 503
    with a per-source breakdown while background workers are still doing
    their first fetch (e.g. right after a cold start), so a load balancer or
    orchestrator can hold off routing traffic here until real data exists."""
    checks = {
        'gps_almanac':  bool(almanac_data['satellites']),
        'glonass_tles': bool(glo_data['tles']),
        'beidou_tles':  bool(bei_data['tles']),
        'galileo_tles': bool(gal_data['tles']),
    }
    ready = any(checks.values())
    return jsonify({'status': 'ready' if ready else 'not_ready', 'checks': checks}), (200 if ready else 503)


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
    if not isinstance(payload, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation, err = _constellation_from_payload(payload, 'GPS')
    if err:
        return err
    rejected = _reject_unknown_constellation(constellation)
    if rejected:
        return rejected

    if constellation in ('GLONASS', 'BEIDOU', 'GALILEO'):
        group  = {'GLONASS': 'glo-ops', 'BEIDOU': 'beidou', 'GALILEO': 'galileo'}[constellation]
        prefix = {'GLONASS': 'R',       'BEIDOU': 'C',      'GALILEO': 'E'}[constellation]
        tles = _load_tle_constellation(group, prefix)
        if not tles:
            return jsonify({'error': f'Could not fetch {constellation} TLEs from Celestrak'}), 503
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = {'tles': tles, 'fetched': fetched}
        with _cache_lock:
            if constellation == 'GLONASS':
                glo_data = entry
            elif constellation == 'BEIDOU':
                bei_data = entry
            else:
                gal_data = entry
        return jsonify({
            'success': True,
            'count': len(tles),
            'constellation': constellation,
            'source': 'Celestrak (current TLEs)',
        })

    # GPS path
    date_str = _payload_str(payload, 'date', 'today')

    if date_str.lower() == 'today':
        dt = datetime.now(timezone.utc) - timedelta(days=2)
    else:
        try:
            # Custom dates are calendar days with no time-of-day component;
            # treat them as UTC so they compare correctly against the
            # timezone-aware "now" below (naive vs aware comparison raises
            # TypeError otherwise).
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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

    with _cache_lock:
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
    rejected = _reject_unknown_constellation(constellation)
    if rejected:
        return rejected

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
    rejected = _reject_unknown_constellation(constellation)
    if rejected:
        return rejected

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
                # Raw values for bit-level subframe reconstruction
                'af0_raw':   eph['af0'],
                'af1_raw':   eph['af1'],
                'm0_rad':    eph['m0'],
                'omega0_rad': eph['omega0'],
                'omega_rad': eph['omega'],
                'i0_rad':    eph['i0'],
                'omega_dot_rad': eph['omega_dot'],
                'idot_rad':  eph['idot'],
                'delta_n_rad': eph['delta_n'],
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
        cnv2 = gps_cnv2_data['ephemeris'].get(prn) or gps_cnv2_data['ephemeris'].get(str(prn))
        if cnv2:
            resp['cnv2'] = {
                'toc':         cnv2['epoch'],
                'gps_week':    cnv2['gps_week'],
                'top':         cnv2['top'],
                'toe':         cnv2['toe'],
                'af0':         cnv2['af0'],
                'af1':         cnv2['af1'],
                'af2':         cnv2['af2'],
                'sqrt_a':      cnv2['sqrt_a'],
                'e':           cnv2['e'],
                'm0_deg':      math.degrees(cnv2['m0']),
                'omega0_deg':  math.degrees(cnv2['omega0']),
                'omega_deg':   math.degrees(cnv2['omega']),
                'i0_deg':      math.degrees(cnv2['i0']),
                'tgd':         cnv2['tgd'],
                'isc_l1cd':    cnv2['isc_l1cd'],
                'isc_l1cp':    cnv2['isc_l1cp'],
                'isc_l1ca':    cnv2['isc_l1ca'],
                'isc_l2c':     cnv2['isc_l2c'],
                'isc_l5i5':    cnv2['isc_l5i5'],
                'isc_l5q5':    cnv2['isc_l5q5'],
                'sisai_oe':    cnv2['sisai_oe'],
                'sisai_ocb':   cnv2['sisai_ocb'],
                'rinex4_date': gps_cnv2_data['date'],
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


def _data_file_mtime(fname):
    """mtime (unix seconds) of a file under data/, or None if it doesn't
    exist — shared by /api/rinex-status and /api/data-freshness so both
    report file age the same way."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', fname)
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


@app.route('/api/rinex-status', methods=['GET'])
def rinex_status():
    """Per-dataset load/freshness diagnostics for every navigation-message
    stream the app understands — RINEX 2 LNAV plus every RINEX 4 stream
    (CNAV, CNV2/L1C, BeiDou D1/D2 + CNV1/2/3, Galileo I/NAV + F/NAV, GLONASS
    FDMA) — not just the handful that happened to be wired up first.

    Each entry reports: whether it's loaded, its source file, on-disk age,
    PRN count, the last background-refresh success/error for the cycle that
    feeds it (see _record_refresh_metric), and a malformed-record count where
    the underlying parser tracks one (RINEX 2 always; RINEX 4 only counts
    towards `rinex4_diag.eph_truncated` — a single streaming-parse counter
    shared by every RINEX 4 sub-dataset, not tracked per-file — because the
    normal path loads pre-parsed JSON written by CI and has nothing left to
    validate at request time).

    Error strings are short ("<ExceptionType>: <message>", no traceback):
    this is a public, unauthenticated endpoint, and full tracebacks only
    ever go to the server log (see _rinex4_refresh_worker/_rinex_refresh_worker).
    """
    now_ts = datetime.now(timezone.utc).timestamp()

    def _entry(cache, fname, metric_key, malformed=None):
        mtime = _data_file_mtime(fname)
        with _metrics_lock:
            metric = dict(_refresh_metrics.get(metric_key, {}))
        return {
            'loaded':       bool(cache['ephemeris']),
            'date':         cache['date'],
            'prns':         len(cache['ephemeris']),
            'source':       fname,
            'age_seconds':  int(now_ts - mtime) if mtime is not None else None,
            'last_success': metric.get('last_success'),
            'last_error':   metric.get('last_error'),
            'malformed':    malformed,
        }

    rinex2_malformed = sum(rinex2_diag.get(k, 0) for k in (
        'records_skipped_short', 'records_skipped_parse_error', 'records_skipped_truncated',
    )) if rinex2_diag else None

    resp = {
        'lnav':     _entry(rinex_data,     'gps_rinex2.txt',        'rinex2', rinex2_malformed),
        'cnav':     _entry(rinex4_data,    'rinex4_gps_cnav.json',  'rinex4'),
        'cnv2':     _entry(gps_cnv2_data,  'rinex4_gps_cnv2.json',  'rinex4'),
        'bds_d':    _entry(bei_d_data,     'rinex4_bds_d.json',     'rinex4'),
        'bds_cnv1': _entry(bei_cnv1_data,  'rinex4_bds_cnv1.json',  'rinex4'),
        'bds_cnv2': _entry(bei_cnv2_data,  'rinex4_bds_cnv2.json',  'rinex4'),
        'bds_cnv3': _entry(bei_cnv3_data,  'rinex4_bds_cnv3.json',  'rinex4'),
        'gal_inav': _entry(gal_inav_data,  'rinex4_gal_inav.json',  'rinex4'),
        'gal_fnav': _entry(gal_fnav_data,  'rinex4_gal_fnav.json',  'rinex4'),
        'glo_fdma': _entry(glo_fdma_data,  'rinex4_glo_fdma.json',  'rinex4'),
        # Shared streaming-parse diagnostics (see docstring above) — already
        # traceback-free (see _rinex4_refresh_worker), safe to expose as-is.
        'rinex4_diag': dict(rinex4_diag),
    }
    return jsonify(resp)


@app.route('/api/refresh-data', methods=['POST'])
@require_admin
def refresh_data_endpoint():
    """Run the local data-refresh (TLEs, RINEX 2, RINEX 4, re-parse JSONs).
    Useful when running on a machine that has bandwidth/CPU headroom (i.e.
    your PC). Reload the in-memory caches afterwards. Requires an admin
    token (see require_admin) since this triggers outbound network fetches
    and rewrites files under data/."""
    global rinex_data
    t0 = time.perf_counter()
    try:
        from refresh_data import refresh_all
        lines = []
        summary = refresh_all(log_fn=lines.append)

        # Surface partial failures instead of a blanket 'ok: True' regardless
        # of what actually refreshed — the frontend/operator needs to know if
        # e.g. one TLE group or RINEX 4 failed while everything else worked.
        partial_failures = []
        tle_summary = summary.get('tles') or {}
        for fname, size in tle_summary.items():
            if not size:
                partial_failures.append(f"tle:{fname}")
        if not summary.get('rinex2'):
            partial_failures.append('rinex2')
        if not summary.get('rinex4'):
            partial_failures.append('rinex4')

        # Reload in-memory caches without restart. Each reload is independent —
        # a failure in one must not prevent the others from being attempted or
        # corrupt the previously-cached value, so cache writes only happen
        # once a reload has fully succeeded and are serialized via the shared
        # cache lock alongside the background workers.
        try:
            rpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'data', 'gps_rinex2.txt')
            diag = {}
            with open(rpath, 'r', encoding='utf-8') as f:
                eph = parse_rinex2_nav(f.read(), diag=diag)
            rinex2_diag.clear()
            rinex2_diag.update(diag)
            if eph:
                with _cache_lock:
                    rinex_data = {'ephemeris': eph,
                                  'date': datetime.fromtimestamp(os.path.getmtime(rpath), tz=timezone.utc).strftime('%Y-%m-%d')}
                lines.append(f"reload rinex2 OK: {len(eph)} PRNs")
            else:
                lines.append("reload rinex2 fail: parsed 0 PRNs — keeping previous cache")
                partial_failures.append('reload:rinex2')
        except Exception as e:
            lines.append(f"reload rinex2 fail: {type(e).__name__}: {e}")
            partial_failures.append('reload:rinex2')

        for fname, varname, label in [
            ('rinex4_gps_cnav.json', 'rinex4_data',    'GPS CNAV'),
            ('rinex4_gps_cnv2.json', 'gps_cnv2_data',  'GPS CNV2 (L1C)'),
            ('rinex4_bds_d.json',    'bei_d_data',     'BeiDou D1/D2'),
            ('rinex4_bds_cnv1.json', 'bei_cnv1_data',  'BeiDou CNV1'),
            ('rinex4_bds_cnv2.json', 'bei_cnv2_data',  'BeiDou CNV2'),
            ('rinex4_bds_cnv3.json', 'bei_cnv3_data',  'BeiDou CNV3'),
            ('rinex4_gal_inav.json', 'gal_inav_data',  'Galileo I/NAV'),
            ('rinex4_glo_fdma.json', 'glo_fdma_data',  'GLONASS FDMA'),
            ('rinex4_gal_fnav.json', 'gal_fnav_data',  'Galileo F/NAV'),
        ]:
            try:
                blob = _load_rinex4_json(fname)
                if blob and blob['ephemeris']:
                    with _cache_lock:
                        globals()[varname] = blob
                    lines.append(f"reload {fname} OK: {len(blob['ephemeris'])} {label} PRNs")
                else:
                    partial_failures.append(f"reload:{fname}")
            except Exception as e:
                lines.append(f"reload {fname} fail: {e}")
                partial_failures.append(f"reload:{fname}")

        _record_refresh_metric('refresh_data_endpoint', duration_ms=(time.perf_counter() - t0) * 1000,
                                success=not partial_failures,
                                error=None if not partial_failures else '; '.join(partial_failures))
        return jsonify({
            'ok': True,
            'complete': not partial_failures,
            'partial_failures': partial_failures,
            'summary': summary,
            'log': lines,
        })
    except Exception as e:
        _record_refresh_metric('refresh_data_endpoint', duration_ms=(time.perf_counter() - t0) * 1000,
                                success=False, error=f'{type(e).__name__}: {e}')
        # Full traceback goes to the server log only — this response is
        # JSON returned to whoever holds the admin token, which shouldn't
        # include internals like file paths or third-party error text either.
        app.logger.exception(f"refresh_data_endpoint failed request_id={getattr(g, 'request_id', None)}")
        return jsonify({'ok': False, 'error': 'Refresh failed — see server log',
                         'request_id': getattr(g, 'request_id', None)}), 500


@app.route('/api/ionosphere', methods=['GET'])
def ionosphere_data():
    """Return broadcast ionospheric model coefficients from RINEX 4:
    Klobuchar (GPS), NeQuick-G (Galileo), BDGIM (BeiDou)."""
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
        log.warning(f"ionosphere_data: {type(e).__name__}: {e}")
        return jsonify({'error': 'Could not load ionosphere data — see server log'}), 500


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


@app.route('/api/beidou-almanac', methods=['GET'])
def beidou_almanac():
    """Per-PRN almanac-style summary derived from the loaded D1/D2 ephemeris."""
    out = {}
    eph = (bei_d_data.get('ephemeris') or {})
    for prn, d in eph.items():
        try:
            out[int(prn)] = {
                'prn':        int(prn),
                'health':     int(d.get('sat_h1', 0)),
                'e':          float(d['e']),
                'sqrt_a':     float(d['sqrt_a']),
                'inc_deg':    math.degrees(d['i0']),
                'omega0_deg': math.degrees(d['omega0']),
                'm0_deg':     math.degrees(d['m0']),
                'msg_type':   d.get('msg_type', '?'),
                'bdt_week':   int(d.get('bdt_week', 0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return jsonify({'slots': out, 'date': bei_d_data.get('date')})


@app.route('/api/galileo-almanac', methods=['GET'])
def galileo_almanac():
    """Per-PRN almanac-style summary from Galileo I/NAV (falls back to F/NAV)."""
    out = {}
    primary = (gal_inav_data.get('ephemeris') or {})
    secondary = (gal_fnav_data.get('ephemeris') or {})
    all_prns = set(int(p) for p in primary) | set(int(p) for p in secondary)
    for prn in all_prns:
        d = primary.get(prn) or primary.get(str(prn)) or secondary.get(prn) or secondary.get(str(prn))
        if not d:
            continue
        try:
            out[prn] = {
                'prn':        prn,
                'health':     int(d.get('sv_health', 0)),
                'e':          float(d['e']),
                'sqrt_a':     float(d['sqrt_a']),
                'inc_deg':    math.degrees(d['i0']),
                'omega0_deg': math.degrees(d['omega0']),
                'm0_deg':     math.degrees(d['m0']),
                'sisa':       float(d.get('sisa', 0.0)),
                'gal_week':   int(d.get('gal_week', 0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return jsonify({'slots': out,
                    'date': gal_inav_data.get('date') or gal_fnav_data.get('date')})


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
            log.warning(f"glonass_almanac: fetch_glonass_constellation_status failed: {type(e).__name__}: {e}")
            return jsonify({'error': 'Could not fetch GLONASS constellation status — see server log',
                             'slots': {}}), 200
    return jsonify({'slots': _glo_almanac_cache['data'],
                    'fetched_ts': int(_glo_almanac_cache['ts'])})


@app.route('/api/system-time', methods=['GET'])
def system_time_data():
    """Return RINEX 4 STO records (inter-system time offsets) and EOP
    (Earth Orientation Parameters from GPS CNAV MT 32)."""
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
        log.warning(f"system_time_data: {type(e).__name__}: {e}")
        return jsonify({'error': 'Could not load system-time data — see server log'}), 500


# Upstream data is refreshed roughly hourly (see .github/workflows/update-tles.yml
# and the in-process background workers above). Allow a generous buffer for a
# handful of missed cycles before calling a file "stale" so a transient
# upstream/CI hiccup doesn't immediately flip the UI to a degraded state.
_FRESHNESS_STALE_SECONDS = 6 * 3600


def _freshness_status(mtime):
    """Classify a data file's age: 'missing' (never fetched — no mtime at
    all), 'stale' (older than the refresh cadence tolerates), or 'ok'."""
    if mtime is None:
        return 'missing'
    age = datetime.now(timezone.utc).timestamp() - mtime
    return 'stale' if age > _FRESHNESS_STALE_SECONDS else 'ok'


@app.route('/api/data-freshness', methods=['GET'])
def data_freshness():
    """Return mtimes of the data files so the UI can show 'last updated' tags.

    Also returns (new, additive — existing flat `<key>: epoch_seconds` entries
    are unchanged for backward compatibility):
      - `_meta.<key>`: {mtime, age_seconds, status} where status is one of
        'ok' / 'stale' / 'missing'.
      - `overall_status`: worst-case rollup across all tracked files
        ('ok' / 'degraded' / 'missing').
      - `refresh_metrics`: last-run duration/success/error for each background
        refresh cycle (see _record_refresh_metric).
    """
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
    meta = {}
    now_ts = datetime.now(timezone.utc).timestamp()
    for key, fname in files.items():
        mtime = _data_file_mtime(fname)
        out[key] = int(mtime) if mtime is not None else None
        meta[key] = {
            'mtime': out[key],
            'age_seconds': int(now_ts - mtime) if mtime is not None else None,
            'status': _freshness_status(mtime),
        }

    statuses = {m['status'] for m in meta.values()}
    if statuses == {'missing'}:
        overall = 'missing'
    elif 'stale' in statuses or 'missing' in statuses:
        overall = 'degraded'
    else:
        overall = 'ok'

    out['_meta'] = meta
    out['overall_status'] = overall
    with _metrics_lock:
        out['refresh_metrics'] = {k: dict(v) for k, v in _refresh_metrics.items()}
    return jsonify(out)


@app.route('/api/fetch-tles', methods=['POST'])
def fetch_tles_proxy():
    """Return already-cached TLE data loaded by the background thread.
    Never makes outbound network calls — avoids Render's 30s request timeout."""
    payload = request.json
    if not isinstance(payload, dict):
        payload = {}
    constellation, err = _constellation_from_payload(payload, '')
    if err:
        return err
    if constellation not in ('GLONASS', 'BEIDOU', 'GALILEO'):
        return jsonify({'error': f'Invalid constellation: {constellation!r}'}), 400
    cache = {'GLONASS': glo_data, 'BEIDOU': bei_data, 'GALILEO': gal_data}.get(constellation)
    if cache and cache.get('tles'):
        return jsonify({'success': True, 'count': len(cache['tles'])})
    return jsonify({'success': False, 'error': 'not yet loaded — background thread still fetching'}), 503


# Anti-abuse limits for the anonymous browser-fallback push endpoint below.
_PUSH_TLES_MAX_TEXT_BYTES = 300_000   # a full Celestrak group file is well under this
_PUSH_TLES_MAX_COUNT = 400            # generous headroom over any real constellation size


def _tle_checksum_ok(line):
    """Validate the trailing mod-10 checksum digit of a TLE line (each digit
    adds its value, '-' adds 1, everything else adds 0). Rejects garbage or
    hand-crafted spoofed lines that happen to match the loose '1 '/'2 '
    prefix check but aren't real TLE data."""
    if len(line) < 2 or not line[-1].isdigit():
        return False
    total = 0
    for ch in line[:-1]:
        if ch.isdigit():
            total += int(ch)
        elif ch == '-':
            total += 1
    return total % 10 == int(line[-1])


@app.route('/api/push-tles', methods=['POST'])
def push_tles():
    """Accept TLE text fetched by the user's browser and cache it server-side.
    Needed because Render's outbound IPs cannot reach celestrak.org directly —
    the browser does the fetch and hands the text back to us.

    This is intentionally *not* gated behind an admin token: it's a normal
    part of the anonymous user flow (see docstring context above), not an
    admin action. Because it writes into caches shared by every visitor,
    hardening here focuses on rejecting malformed/spoofed input instead:
    a size cap, a cap on the number of TLE records, and per-line TLE
    checksum validation so a client can't poison the shared cache with
    fabricated orbital data disguised as real TLEs.
    """
    global glo_data, bei_data, gal_data

    payload = request.json
    if not isinstance(payload, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation, err = _constellation_from_payload(payload, '')
    if err:
        return err
    text = _payload_str(payload, 'text', '')

    if constellation not in ('GLONASS', 'BEIDOU', 'GALILEO'):
        return jsonify({'error': f'Invalid constellation: {constellation}'}), 400
    # Size cap first (cheap length check) so an oversized-but-otherwise-junk
    # payload is rejected with 413 before spending time scanning its content
    # or handing it to the TLE parser.
    if len(text) > _PUSH_TLES_MAX_TEXT_BYTES:
        return jsonify({'error': f'TLE payload too large (max {_PUSH_TLES_MAX_TEXT_BYTES} bytes)'}), 413
    if not text or '1 ' not in text:
        return jsonify({'error': 'No valid TLE content'}), 400

    try:
        tles = parse_tles(text)
    except Exception as e:
        log.warning(f"push_tles: parse_tles failed: {type(e).__name__}: {e}")
        return jsonify({'error': 'Could not parse TLE text — check the format and try again'}), 400

    if not tles:
        return jsonify({'error': 'No TLEs parsed'}), 400
    if len(tles) > _PUSH_TLES_MAX_COUNT:
        return jsonify({'error': f'Too many TLE records ({len(tles)} > {_PUSH_TLES_MAX_COUNT})'}), 413

    # Reject records whose checksum doesn't match before trusting any of their
    # orbital elements — cheap to check, and the single strongest guard
    # against spoofed/corrupted input reaching the shared cache.
    tles = [t for t in tles if _tle_checksum_ok(t['line1']) and _tle_checksum_ok(t['line2'])]
    if not tles:
        return jsonify({'error': 'No TLEs passed checksum validation'}), 400

    prefix = {'GLONASS': 'R', 'BEIDOU': 'C', 'GALILEO': 'E'}[constellation]
    if constellation == 'GLONASS':
        slot_map = fetch_glonass_slot_map()
        if not slot_map:
            return jsonify({'error': 'GLONASS slot map unavailable — cannot validate satellite identities'}), 503
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
            prn = _beidou_id_from_name(tle['name'])
            if prn is None:
                continue
            tle['id'] = prn
            tle['label'] = f"C{prn:02d}"
            kept.append(tle)
        kept.sort(key=lambda t: t['id'])
        tles = kept
    elif constellation == 'GALILEO':
        kept = []
        for tle in tles:
            prn = _galileo_id_from_name(tle['name'])
            if prn is None:
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

    if not tles:
        return jsonify({'error': f'No {constellation} satellites could be identified in the pushed TLEs'}), 400

    entry = {'tles': tles, 'fetched': datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    with _cache_lock:
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
                    with _cache_lock:
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
    _prop_t0 = time.perf_counter()

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

    log.info(f"propagation event=live_positions satellites={len(positions)} "
             f"duration_ms={(time.perf_counter() - _prop_t0) * 1000:.1f}")

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
    if not isinstance(payload, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    constellation, err = _constellation_from_payload(payload, 'GPS')
    if err:
        return err

    if constellation in ('GLONASS', 'BEIDOU', 'GALILEO'):
        cache = {'GLONASS': glo_data, 'BEIDOU': bei_data, 'GALILEO': gal_data}[constellation]
        if not cache['tles']:
            return jsonify({'error': f'No {constellation} TLEs loaded'}), 400

        sat_id = _payload_int(payload, 'prn', 0)
        if sat_id is None:
            return jsonify({'error': 'Invalid satellite ID'}), 400

        tle = next((t for t in cache['tles'] if t['id'] == sat_id), None)
        if not tle:
            return jsonify({'error': f'{constellation} satellite {sat_id} not found'}), 404

        time_raw = payload.get('time', 'now')
        if not isinstance(time_raw, str):
            return jsonify({'error': 'Invalid time format'}), 400
        if time_raw.lower() == 'now':
            dt = datetime.now(timezone.utc)
        else:
            try:
                dt = datetime.strptime(time_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return jsonify({'error': 'Invalid time format'}), 400

        _prop_t0 = time.perf_counter()
        pos = propagate_tle(tle, dt)
        log.info(f"propagation event=calculate constellation={constellation} prn={sat_id} "
                 f"duration_ms={(time.perf_counter() - _prop_t0) * 1000:.1f}")
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

    prn = _payload_int(payload, 'prn', 0)
    if prn is None:
        return jsonify({'error': 'Invalid PRN'}), 400

    time_raw = payload.get('time', 'now')
    if not isinstance(time_raw, str):
        return jsonify({'error': 'Invalid time format'}), 400
    satellite = next((s for s in almanac_data['satellites'] if s['id'] == prn), None)
    if not satellite:
        return jsonify({'error': f'PRN {prn} not found'}), 404

    if time_raw.lower() == 'now':
        dt = datetime.now(timezone.utc)
    else:
        try:
            dt = datetime.strptime(time_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({'error': 'Invalid time format'}), 400

    gps_sec = gps_time_from_datetime(dt)
    _prop_t0 = time.perf_counter()
    pos = propagate(satellite, gps_sec)
    log.info(f"propagation event=calculate constellation=GPS prn={prn} "
             f"duration_ms={(time.perf_counter() - _prop_t0) * 1000:.1f}")
    geo = geodetic(pos['x'], pos['y'], pos['z'])

    return jsonify({
        'prn': prn, 'label': f"G{prn:02d}", 'constellation': 'GPS',
        'time': dt.strftime("%Y-%m-%d %H:%M:%S"),
        'ecef': {'x': f"{pos['x']:,.0f}", 'y': f"{pos['y']:,.0f}", 'z': f"{pos['z']:,.0f}", 'r': f"{pos['r'] / 1000:,.1f}"},
        'geodetic': {'latitude': f"{geo['lat']:.4f}", 'longitude': f"{geo['lon']:.4f}", 'altitude': f"{geo['alt'] / 1000:.1f}"},
    })


@app.route('/api/ephemeris-vs-tle', methods=['GET'])
def ephemeris_vs_tle():
    """Compare a broadcast-ephemeris-derived ECEF position against the
    TLE/SGP4-derived position for the same satellite at the same instant.

    Currently supported for GLONASS only: it's the one constellation where
    /api/live-positions already runs a broadcast RK4 propagator
    (propagate_glonass_eph) side-by-side with a TLE/SGP4 fallback, so both
    inputs are already loaded and consistent with what the map/DOP/sky-plot
    show. Read-only GET, no admin auth — same public-read convention as
    /api/calculate and /api/live-positions.
    """
    constellation = request.args.get('constellation', 'GLONASS').upper()
    if constellation != 'GLONASS':
        return jsonify({'error': 'Only GLONASS is currently supported for ephemeris-vs-TLE '
                                  'comparison (the only constellation with both a broadcast '
                                  'RK4 propagator and a TLE fallback loaded).'}), 400

    try:
        sat_id = int(request.args.get('prn', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid satellite ID'}), 400

    at_str = request.args.get('at')
    if at_str:
        try:
            now = datetime.fromisoformat(at_str.replace('Z', '+00:00'))
            now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        except ValueError:
            return jsonify({'error': f'Invalid "at" timestamp: {at_str!r}'}), 400
    else:
        now = datetime.now(timezone.utc)

    tle = next((t for t in glo_data['tles'] if t['id'] == sat_id), None)
    if not tle:
        return jsonify({'error': f'GLONASS satellite {sat_id} not found in TLE cache'}), 404

    tle_pos = propagate_tle(tle, now)
    if not tle_pos:
        return jsonify({'error': 'TLE propagation failed'}), 500

    result = {
        'prn': sat_id, 'constellation': constellation,
        'time': now.isoformat().replace('+00:00', 'Z'),
        'tle': {'x': tle_pos['x'], 'y': tle_pos['y'], 'z': tle_pos['z']},
        'broadcast': None,
        'delta_m': None,
        'caveat': (
            "TLE/SGP4 and broadcast ephemeris (RK4 from the RINEX 4 state vector) "
            "are independent data sources with different update cadences, accuracy "
            "targets, and reference frames (TEME vs PZ-90.11). Differences from a "
            "few hundred meters up to several kilometers are normal and do not by "
            "themselves indicate an error in either source."
        ),
    }
    eph = glo_fdma_data['ephemeris'].get(sat_id)
    if eph:
        eph_pos = propagate_glonass_eph(eph, now)
        dx = eph_pos['x'] - tle_pos['x']
        dy = eph_pos['y'] - tle_pos['y']
        dz = eph_pos['z'] - tle_pos['z']
        result['broadcast'] = {'x': eph_pos['x'], 'y': eph_pos['y'], 'z': eph_pos['z']}
        result['delta_m'] = round((dx * dx + dy * dy + dz * dz) ** 0.5, 1)
    else:
        result['note'] = ('No broadcast ephemeris currently cached for this satellite '
                           '(RINEX 4 data may still be loading); showing TLE-only position.')
    return jsonify(result)
