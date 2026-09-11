"""Tests for app.py: side-effect-free import, timezone-aware almanac dates,
constellation validation, admin auth, push-tles hardening, and stable
BeiDou/Galileo identity mapping helpers.
"""
import subprocess
import sys
import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import app as app_module


# ─── Side-effect-free import ────────────────────────────────────────────────

def test_import_app_starts_no_background_threads():
    """Importing `app` (already done at collection time via conftest) must
    not have started any of the TLE/RINEX2/RINEX4 refresh threads."""
    assert app_module._workers_started is False
    names = {t.name for t in threading.enumerate()}
    # None of our worker threads should be alive as a side effect of import.
    assert not any('refresh' in n.lower() for n in names)


def test_fresh_process_import_has_no_network_threads():
    """Belt-and-braces: import app in a brand-new subprocess and confirm the
    only live thread is MainThread — this is what actually protects against
    a regression that re-adds a module-level Thread(...).start()."""
    code = (
        "import threading, app\n"
        "names = sorted(t.name for t in threading.enumerate())\n"
        "assert names == ['MainThread'], names\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, '-c', code],
        cwd=__file__.rsplit('tests', 1)[0],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'OK' in result.stdout


def test_start_background_workers_is_idempotent_and_disableable(monkeypatch):
    monkeypatch.setenv('GPS_CALCULATOR_NO_WORKERS', '1')
    monkeypatch.setattr(app_module, '_workers_started', False)
    app_module.start_background_workers()
    # Disabled by env — must not flip _workers_started or spawn threads.
    assert app_module._workers_started is False


# ─── Timezone-aware custom almanac dates ────────────────────────────────────

def test_load_almanac_custom_date_no_tz_error(client):
    """Previously raised TypeError comparing naive vs aware datetimes."""
    with patch.object(app_module, 'fetch_almanac', return_value=None):
        resp = client.post('/api/load-almanac', json={'constellation': 'GPS', 'date': '2020-01-15'})
    # Should reach the 404 "not found" branch, not blow up with a 500 TypeError.
    assert resp.status_code == 404
    assert 'not found' in resp.get_json()['error'].lower()


def test_load_almanac_rejects_future_custom_date(client):
    future = (datetime.now(timezone.utc).year + 1)
    resp = client.post('/api/load-almanac', json={'constellation': 'GPS', 'date': f'{future}-01-01'})
    assert resp.status_code == 400
    assert 'future' in resp.get_json()['error'].lower()


def test_load_almanac_rejects_bad_date_format(client):
    resp = client.post('/api/load-almanac', json={'constellation': 'GPS', 'date': 'not-a-date'})
    assert resp.status_code == 400


# ─── Unknown-constellation rejection (standardized) ─────────────────────────

@pytest.mark.parametrize('endpoint,method,kwargs', [
    ('/api/load-almanac', 'POST', dict(json={'constellation': 'MARS'})),
    ('/api/satellites', 'GET', dict(query_string={'constellation': 'MARS'})),
    ('/api/satellite-detail', 'GET', dict(query_string={'constellation': 'MARS', 'label': 'X1'})),
    ('/api/fetch-tles', 'POST', dict(json={'constellation': 'MARS'})),
])
def test_unknown_constellation_rejected_everywhere(client, endpoint, method, kwargs):
    resp = client.open(endpoint, method=method, **kwargs)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body is not None and 'error' in body


def test_reject_unknown_constellation_helper():
    with app_module.app.app_context():
        assert app_module._reject_unknown_constellation('GPS') is None
        assert app_module._reject_unknown_constellation('GLONASS') is None
        result = app_module._reject_unknown_constellation('MARS')
        assert result is not None
        response, status = result
        assert status == 400


# ─── Admin auth for /api/refresh-data ───────────────────────────────────────

def test_refresh_data_disabled_without_admin_token(client, monkeypatch):
    monkeypatch.setattr(app_module, 'ADMIN_TOKEN', '')
    monkeypatch.delenv('GPS_ALLOW_UNAUTHENTICATED_ADMIN', raising=False)
    resp = client.post('/api/refresh-data')
    assert resp.status_code == 503


def test_refresh_data_rejects_missing_or_wrong_token(client, admin_token):
    resp = client.post('/api/refresh-data')
    assert resp.status_code == 401

    resp = client.post('/api/refresh-data', headers={'X-Admin-Token': 'wrong'})
    assert resp.status_code == 401


def test_refresh_data_accepts_correct_token(client, admin_token):
    with patch('refresh_data.refresh_all', return_value={'tles': {}, 'rinex2': True, 'rinex4': True}):
        resp = client.post('/api/refresh-data', headers={'X-Admin-Token': admin_token})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True


def test_refresh_data_accepts_bearer_auth_header(client, admin_token):
    with patch('refresh_data.refresh_all', return_value={'tles': {}, 'rinex2': True, 'rinex4': True}):
        resp = client.post('/api/refresh-data', headers={'Authorization': f'Bearer {admin_token}'})
    assert resp.status_code == 200


def test_refresh_data_allows_unauthenticated_when_opted_in(client, monkeypatch):
    monkeypatch.setattr(app_module, 'ADMIN_TOKEN', '')
    monkeypatch.setenv('GPS_ALLOW_UNAUTHENTICATED_ADMIN', '1')
    with patch('refresh_data.refresh_all', return_value={'tles': {}, 'rinex2': True, 'rinex4': True}):
        resp = client.post('/api/refresh-data')
    assert resp.status_code == 200


def test_refresh_data_reports_partial_failures(client, admin_token):
    summary = {'tles': {'gps.tle': 1234, 'glo-ops.tle': None}, 'rinex2': True, 'rinex4': False}
    with patch('refresh_data.refresh_all', return_value=summary):
        resp = client.post('/api/refresh-data', headers={'X-Admin-Token': admin_token})
    body = resp.get_json()
    assert body['ok'] is True
    assert body['complete'] is False
    assert 'tle:glo-ops.tle' in body['partial_failures']
    assert 'rinex4' in body['partial_failures']
    assert 'tle:gps.tle' not in body['partial_failures']


# ─── push-tles hardening (NOT admin-gated: legitimate anonymous flow) ───────

def _valid_tle_pair(name, line1_base, line2_base):
    """Build a name/line1/line2 TLE record with correct mod-10 checksums
    appended, so tests exercise real validation rather than being tautological."""
    def with_checksum(line_no_checksum):
        total = 0
        for ch in line_no_checksum:
            if ch.isdigit():
                total += int(ch)
            elif ch == '-':
                total += 1
        return line_no_checksum + str(total % 10)

    return name, with_checksum(line1_base), with_checksum(line2_base)


def test_tle_checksum_ok_accepts_valid_and_rejects_corrupt():
    line1_base = "1 25544U 98067A   24001.00000000  .00001000  00000-0  10000-3 0  999"
    total = sum(int(c) for c in line1_base if c.isdigit()) + line1_base.count('-')
    line1 = line1_base + str(total % 10)
    assert app_module._tle_checksum_ok(line1) is True
    corrupted = line1[:-1] + ('1' if line1[-1] != '1' else '2')
    assert app_module._tle_checksum_ok(corrupted) is False


def test_push_tles_rejects_bad_constellation(client):
    resp = client.post('/api/push-tles', json={'constellation': 'MARS', 'text': 'x'})
    assert resp.status_code == 400


def test_push_tles_rejects_oversized_payload(client, monkeypatch):
    monkeypatch.setattr(app_module, '_PUSH_TLES_MAX_TEXT_BYTES', 10)
    resp = client.post('/api/push-tles', json={
        'constellation': 'BEIDOU',
        'text': '1 ' + 'x' * 50,
    })
    assert resp.status_code == 413


def test_push_tles_rejects_too_many_records(client, monkeypatch):
    monkeypatch.setattr(app_module, '_PUSH_TLES_MAX_COUNT', 1)
    name1, l1a, l2a = _valid_tle_pair(
        'BEIDOU-3 M1 (C01)',
        "1 43001U 18008A   24001.00000000  .00000010  00000-0  10000-3 0  99",
        "2 43001  55.0000 100.0000 0010000  90.0000 270.0000  1.86000000 1000")
    name2, l1b, l2b = _valid_tle_pair(
        'BEIDOU-3 M2 (C02)',
        "1 43002U 18009A   24001.00000000  .00000010  00000-0  10000-3 0  99",
        "2 43002  55.0000 100.0000 0010000  90.0000 270.0000  1.86000000 1000")
    text = f"{name1}\n{l1a}\n{l2a}\n{name2}\n{l1b}\n{l2b}\n"
    resp = client.post('/api/push-tles', json={'constellation': 'BEIDOU', 'text': text})
    assert resp.status_code == 413


def test_push_tles_rejects_checksum_failures(client):
    text = (
        "BEIDOU-3 M1 (C01)\n"
        "1 43001U 18008A   24001.00000000  .00000010  00000-0  10000-3 0  999\n"
        "2 43001  55.0000 100.0000 0010000  90.0000 270.0000  1.86000000 10009\n"
    )
    resp = client.post('/api/push-tles', json={'constellation': 'BEIDOU', 'text': text})
    assert resp.status_code == 400
    assert 'checksum' in resp.get_json()['error'].lower()


def test_push_tles_accepts_valid_beidou_and_maps_stable_prn(client):
    name, l1, l2 = _valid_tle_pair(
        'BEIDOU-3 M21 (C29)',
        "1 43001U 18008A   24001.00000000  .00000010  00000-0  10000-3 0  99",
        "2 43001  55.0000 100.0000 0010000  90.0000 270.0000  1.86000000 1000")
    text = f"{name}\n{l1}\n{l2}\n"
    resp = client.post('/api/push-tles', json={'constellation': 'BEIDOU', 'text': text})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert body['count'] == 1
    assert app_module.bei_data['tles'][0]['id'] == 29
    assert app_module.bei_data['tles'][0]['label'] == 'C29'


def test_push_tles_glonass_returns_503_when_slot_map_unavailable(client):
    name, l1, l2 = _valid_tle_pair(
        'COSMOS 2xxx',
        "1 43001U 18008A   24001.00000000  .00000010  00000-0  10000-3 0  99",
        "2 43001  55.0000 100.0000 0010000  90.0000 270.0000  2.13000000 1000")
    text = f"{name}\n{l1}\n{l2}\n"
    with patch.object(app_module, 'fetch_glonass_slot_map', return_value={}):
        resp = client.post('/api/push-tles', json={'constellation': 'GLONASS', 'text': text})
    assert resp.status_code == 503


# ─── Stable BeiDou/Galileo identity mapping helpers ─────────────────────────

def test_beidou_id_from_name_extracts_prn():
    assert app_module._beidou_id_from_name('BEIDOU-3 M21 (C29)') == 29
    assert app_module._beidou_id_from_name('BEIDOU-2 IGSO-1') is None


def test_galileo_id_from_name_uses_gsat_map():
    assert app_module._galileo_id_from_name('GSAT0101 (GALILEO-PFM)') == 11
    assert app_module._galileo_id_from_name('GSAT9999 (UNKNOWN)') is None
    assert app_module._galileo_id_from_name('NO GSAT HERE') is None


def test_load_tle_constellation_beidou_uses_stable_prn_not_file_order():
    """Regression test: previously BeiDou/Galileo satellites were assigned
    tle['id'] = i + 1 (sequential file-order index), which silently broke
    ephemeris correlation whenever Celestrak reordered the file. The loader
    must key off the real PRN embedded in the name instead."""
    tle_text = (
        "BEIDOU-3 M21 (C29)\n"
        "1 43001U 18008A   24001.00000000  .00000010  00000-0  10000-3 0  9999\n"
        "2 43001  55.0000 100.0000 0010000  90.0000 270.0000  1.86000000100001\n"
        "BEIDOU-3 M1 (C19)\n"
        "1 41586U 16021A   24001.00000000  .00000010  00000-0  10000-3 0  9999\n"
        "2 41586  55.0000 100.0000 0010000  90.0000 270.0000  1.86000000100001\n"
    )
    with patch.object(app_module, 'fetch_tle_group', return_value=tle_text):
        result = app_module._load_tle_constellation('beidou', 'C')
    assert result is not None
    ids = {t['id'] for t in result}
    # IDs must be the real PRNs (29, 19), not file-order (1, 2).
    assert ids == {29, 19}


def test_load_tle_constellation_glonass_returns_none_on_slot_map_failure():
    """GLONASS mapping-failure hardening: an empty/unavailable slot map must
    be a hard, explicit failure (None) rather than a silently-empty list."""
    tle_text = (
        "COSMOS 2xxx\n"
        "1 43001U 18008A   24001.00000000  .00000010  00000-0  10000-3 0  9999\n"
        "2 43001  55.0000 100.0000 0010000  90.0000 270.0000  2.13000000100001\n"
    )
    with patch.object(app_module, 'fetch_tle_group', return_value=tle_text), \
         patch.object(app_module, 'fetch_glonass_slot_map', return_value={}):
        result = app_module._load_tle_constellation('glo-ops', 'R')
    assert result is None


# ─── Health / readiness probes ──────────────────────────────────────────────

def test_healthz_always_ok_even_with_empty_caches(client, monkeypatch):
    """Liveness probe must succeed regardless of data-cache state — it checks
    only that the process can handle a request, not that data is loaded."""
    monkeypatch.setitem(app_module.almanac_data, 'satellites', [])
    monkeypatch.setitem(app_module.glo_data, 'tles', [])
    monkeypatch.setitem(app_module.bei_data, 'tles', [])
    monkeypatch.setitem(app_module.gal_data, 'tles', [])
    resp = client.get('/healthz')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


def test_readyz_not_ready_when_all_caches_empty(client, monkeypatch):
    monkeypatch.setitem(app_module.almanac_data, 'satellites', [])
    monkeypatch.setitem(app_module.glo_data, 'tles', [])
    monkeypatch.setitem(app_module.bei_data, 'tles', [])
    monkeypatch.setitem(app_module.gal_data, 'tles', [])
    resp = client.get('/readyz')
    assert resp.status_code == 503
    data = resp.get_json()
    assert data['status'] == 'not_ready'
    assert data['checks'] == {
        'gps_almanac': False, 'glonass_tles': False,
        'beidou_tles': False, 'galileo_tles': False,
    }


def test_readyz_ready_when_any_cache_populated(client, monkeypatch):
    monkeypatch.setitem(app_module.almanac_data, 'satellites', [{'id': 1}])
    monkeypatch.setitem(app_module.glo_data, 'tles', [])
    monkeypatch.setitem(app_module.bei_data, 'tles', [])
    monkeypatch.setitem(app_module.gal_data, 'tles', [])
    resp = client.get('/readyz')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ready'
    assert data['checks']['gps_almanac'] is True


# ─── Refresh metrics (structured timing) ────────────────────────────────────

def test_record_refresh_metric_updates_counts_and_last_fields(monkeypatch):
    fresh = {'runs': 0, 'successes': 0, 'last_finished': None,
              'last_duration_ms': None, 'last_success': None, 'last_error': None}
    monkeypatch.setitem(app_module._refresh_metrics, 'tle', dict(fresh))

    app_module._record_refresh_metric('tle', duration_ms=12.345, success=True)
    m = app_module._refresh_metrics['tle']
    assert m['runs'] == 1
    assert m['successes'] == 1
    assert m['last_success'] is True
    assert m['last_duration_ms'] == 12.3
    assert m['last_error'] is None
    assert m['last_finished'] is not None

    app_module._record_refresh_metric('tle', duration_ms=5.0, success=False, error='boom')
    m = app_module._refresh_metrics['tle']
    assert m['runs'] == 2
    assert m['successes'] == 1  # unchanged — this run failed
    assert m['last_success'] is False
    assert m['last_error'] == 'boom'


# ─── Data freshness: _meta / overall_status / refresh_metrics ──────────────

def test_freshness_status_helper_classifies_missing_stale_ok():
    assert app_module._freshness_status(None) == 'missing'
    now_ts = datetime.now(timezone.utc).timestamp()
    assert app_module._freshness_status(now_ts) == 'ok'
    stale_ts = now_ts - app_module._FRESHNESS_STALE_SECONDS - 10
    assert app_module._freshness_status(stale_ts) == 'stale'


def test_data_freshness_endpoint_shape_is_backward_compatible_and_additive(client):
    resp = client.get('/api/data-freshness')
    assert resp.status_code == 200
    data = resp.get_json()

    # New additive keys.
    assert 'overall_status' in data
    assert data['overall_status'] in ('ok', 'degraded', 'missing')
    assert '_meta' in data
    assert 'refresh_metrics' in data
    for name in ('tle', 'rinex2', 'rinex4', 'refresh_data_endpoint'):
        assert name in data['refresh_metrics']
        assert set(data['refresh_metrics'][name]) >= {
            'runs', 'successes', 'last_finished', 'last_duration_ms',
            'last_success', 'last_error',
        }

    # Original flat keys are untouched: every non-meta key still maps
    # directly to an epoch int or None (same shape as before this feature).
    for key, meta in data['_meta'].items():
        assert key in data
        assert set(meta) == {'mtime', 'age_seconds', 'status'}
        assert meta['status'] in ('ok', 'stale', 'missing')
        assert meta['mtime'] == data[key]


def test_data_freshness_overall_status_missing_when_all_files_absent(client, monkeypatch, tmp_path):
    # Point the freshness lookup at an empty temp directory so every
    # tracked file is reported missing, regardless of the real data/ dir.
    monkeypatch.setattr(app_module.os.path, 'getmtime',
                         lambda p: (_ for _ in ()).throw(OSError('missing')))
    resp = client.get('/api/data-freshness')
    data = resp.get_json()
    assert data['overall_status'] == 'missing'
    assert all(m['status'] == 'missing' for m in data['_meta'].values())
    assert all(v is None for k, v in data.items()
               if k not in ('_meta', 'overall_status', 'refresh_metrics'))


# ─── Ephemeris-vs-TLE comparison ────────────────────────────────────────────

def test_ephemeris_vs_tle_rejects_non_glonass(client):
    resp = client.get('/api/ephemeris-vs-tle?constellation=GPS&prn=1')
    assert resp.status_code == 400
    assert 'GLONASS' in resp.get_json()['error']


def test_ephemeris_vs_tle_404_when_satellite_not_in_tle_cache(client, monkeypatch):
    monkeypatch.setitem(app_module.glo_data, 'tles', [])
    resp = client.get('/api/ephemeris-vs-tle?constellation=GLONASS&prn=42')
    assert resp.status_code == 404


def test_ephemeris_vs_tle_rejects_bad_at_timestamp(client, monkeypatch):
    monkeypatch.setitem(app_module.glo_data, 'tles',
                         [{'id': 42, 'label': 'R42', 'name': 'x', 'line1': 'a', 'line2': 'b'}])
    resp = client.get('/api/ephemeris-vs-tle?constellation=GLONASS&prn=42&at=not-a-date')
    assert resp.status_code == 400


def test_ephemeris_vs_tle_returns_tle_only_when_no_broadcast_ephemeris(client, monkeypatch):
    fake_tle = {'id': 42, 'label': 'R42', 'name': 'COSMOS 2xxx', 'line1': 'x', 'line2': 'y'}
    monkeypatch.setitem(app_module.glo_data, 'tles', [fake_tle])
    monkeypatch.setitem(app_module.glo_fdma_data, 'ephemeris', {})
    with patch.object(app_module, 'propagate_tle', return_value={'x': 1.0, 'y': 2.0, 'z': 3.0, 'r': 3.74}):
        resp = client.get('/api/ephemeris-vs-tle?constellation=GLONASS&prn=42')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['tle'] == {'x': 1.0, 'y': 2.0, 'z': 3.0}
    assert data['broadcast'] is None
    assert data['delta_m'] is None
    assert 'note' in data
    assert 'caveat' in data


def test_ephemeris_vs_tle_computes_delta_when_both_available(client, monkeypatch):
    fake_tle = {'id': 42, 'label': 'R42', 'name': 'COSMOS 2xxx', 'line1': 'x', 'line2': 'y'}
    monkeypatch.setitem(app_module.glo_data, 'tles', [fake_tle])
    monkeypatch.setitem(app_module.glo_fdma_data, 'ephemeris', {42: {'fake': 'eph'}})
    with patch.object(app_module, 'propagate_tle',
                       return_value={'x': 1000.0, 'y': 2000.0, 'z': 3000.0, 'r': 3742.0}), \
         patch.object(app_module, 'propagate_glonass_eph',
                      return_value={'x': 1003.0, 'y': 2004.0, 'z': 3000.0}):
        resp = client.get('/api/ephemeris-vs-tle?constellation=GLONASS&prn=42&at=2024-01-01T00:00:00Z')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['broadcast'] == {'x': 1003.0, 'y': 2004.0, 'z': 3000.0}
    assert data['delta_m'] == pytest.approx((3 ** 2 + 4 ** 2) ** 0.5, abs=0.05)
    assert data['time'] == '2024-01-01T00:00:00Z'


# ─── Request timing hook (before_request / after_request) ──────────────────

def test_request_timing_hook_does_not_break_normal_requests(client):
    """The before/after_request timing hooks added for structured
    http_request logs must be transparent — a normal request still returns
    its usual status/body."""
    resp = client.get('/healthz')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


# ─── Request IDs, generic 500s, and consistent 400/413/415 JSON errors ──────

def test_request_id_header_present_on_every_response(client):
    resp = client.get('/healthz')
    assert resp.headers.get('X-Request-Id')


def test_request_id_echoes_client_supplied_header(client):
    resp = client.get('/healthz', headers={'X-Request-Id': 'client-supplied-id'})
    assert resp.headers.get('X-Request-Id') == 'client-supplied-id'


def test_unhandled_exception_returns_generic_500_with_request_id_no_leak(client, monkeypatch):
    """An unhandled exception inside a view must never surface its message,
    type name, or traceback to the client — only a generic message plus the
    request_id an operator can grep the server log for."""
    monkeypatch.setitem(app_module.almanac_data, 'satellites', [{'id': 1, 'wk': 1, 'toa': 1}])
    monkeypatch.setattr(app_module.app, 'config',
                         {**app_module.app.config, 'PROPAGATE_EXCEPTIONS': False, 'TESTING': True})
    secret = 'boom internal detail /secret/path/should/not/leak'
    with patch.object(app_module, 'propagate', side_effect=RuntimeError(secret)):
        resp = client.post('/api/calculate', json={'constellation': 'GPS', 'prn': 1, 'time': 'now'})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body['error'] == 'Internal server error'
    assert 'request_id' in body and body['request_id']
    raw = resp.get_data(as_text=True)
    assert secret not in raw
    assert 'RuntimeError' not in raw
    assert 'Traceback' not in raw


def test_malformed_json_body_returns_json_400_not_html(client):
    resp = client.post('/api/calculate', data='{not valid json',
                        content_type='application/json')
    assert resp.status_code == 400
    assert resp.content_type.startswith('application/json')
    body = resp.get_json()
    assert 'error' in body and 'request_id' in body


def test_wrong_content_type_returns_json_415_not_html(client):
    resp = client.post('/api/calculate', data='irrelevant', content_type='text/plain')
    assert resp.status_code == 415
    assert resp.content_type.startswith('application/json')
    assert 'error' in resp.get_json()


def test_oversized_body_returns_json_413_not_html(client):
    huge = b'{"text": "' + b'x' * (3 * 1024 * 1024) + b'"}'
    resp = client.post('/api/push-tles', data=huge, content_type='application/json')
    assert resp.status_code == 413
    assert resp.content_type.startswith('application/json')
    assert 'error' in resp.get_json()


def test_max_content_length_is_configured_and_reasonable():
    """A body-size cap must exist at all (defense-in-depth ahead of any
    per-endpoint size check) and be generous enough for the largest
    legitimate JSON body this app accepts (a full TLE push, capped at
    _PUSH_TLES_MAX_TEXT_BYTES plus JSON/field overhead)."""
    limit = app_module.app.config.get('MAX_CONTENT_LENGTH')
    assert limit is not None
    assert limit > app_module._PUSH_TLES_MAX_TEXT_BYTES
    assert limit < 50 * 1024 * 1024  # sanity: not absurdly large either


# ─── Security headers ───────────────────────────────────────────────────────

def test_security_headers_present_on_html_page(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert 'Content-Security-Policy' in resp.headers
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['X-Frame-Options'] == 'DENY'
    assert resp.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert 'Permissions-Policy' in resp.headers


def test_security_headers_present_on_api_response(client):
    """Headers apply globally (after_request), not just to the HTML page."""
    resp = client.get('/healthz')
    assert 'Content-Security-Policy' in resp.headers
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'


def test_csp_allows_only_self_and_cesium_cdn_for_scripts():
    csp = app_module._CSP
    assert "script-src 'self' https://cesium.com" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp


def test_frame_ancestors_and_x_frame_options_both_block_framing(client):
    """Belt-and-braces clickjacking protection: modern (CSP) + legacy
    (X-Frame-Options) browsers are both covered."""
    resp = client.get('/')
    assert 'frame-ancestors' in resp.headers['Content-Security-Policy']
    assert resp.headers['X-Frame-Options'] == 'DENY'


# ─── Versioned static assets (CSS/JS extracted from the inline template) ────

def test_cesium_css_and_js_are_served_as_static_files(client):
    css = client.get('/static/css/cesium.css')
    js = client.get('/static/js/cesium.js')
    assert css.status_code == 200
    assert js.status_code == 200
    assert 'text/css' in css.content_type
    assert 'javascript' in js.content_type


def test_cesium_template_references_versioned_static_assets(client):
    resp = client.get('/')
    html = resp.get_data(as_text=True)
    assert '/static/css/cesium.css?v=' in html
    assert '/static/js/cesium.js?v=' in html
    # The huge inline <style>/<script> blocks should be gone from the page.
    assert '#cesiumContainer { position: absolute' not in html
    assert "Cesium.Ion.defaultAccessToken" not in html


def test_asset_url_helper_appends_a_cache_busting_version(client):
    """asset_url() is a context processor, exercised via real template
    rendering — its version suffix should be the static file's mtime."""
    html = client.get('/').get_data(as_text=True)
    assert 'cesium.css?v=' in html
    version = html.split('cesium.css?v=')[1].split('"')[0]
    assert version.isdigit()


# ─── Strict payload type validation on JSON-body endpoints ──────────────────

def test_calculate_rejects_non_string_constellation_with_400_not_500(client):
    resp = client.post('/api/calculate', json={'constellation': 123, 'prn': 1})
    assert resp.status_code == 400


def test_calculate_rejects_non_string_time_with_400_not_500(client, monkeypatch):
    monkeypatch.setitem(app_module.almanac_data, 'satellites', [{'id': 1, 'wk': 1, 'toa': 1}])
    resp = client.post('/api/calculate', json={'constellation': 'GPS', 'prn': 1, 'time': 12345})
    assert resp.status_code == 400
    assert 'time' in resp.get_json()['error'].lower()


def test_calculate_rejects_infinite_prn_with_400_not_500(client, monkeypatch):
    """JSON's `Infinity` literal (a Python json.loads extension, though not
    valid per the JSON spec) must not reach a plain int(...) call and raise
    an uncaught OverflowError."""
    monkeypatch.setitem(app_module.glo_data, 'tles', [{'id': 1, 'label': 'R01', 'name': 'x'}])
    resp = client.post('/api/calculate', json={'constellation': 'GLONASS', 'prn': float('inf')})
    assert resp.status_code == 400
    assert 'satellite id' in resp.get_json()['error'].lower()


def test_calculate_rejects_non_dict_json_body(client):
    resp = client.post('/api/calculate', json=[1, 2, 3])
    assert resp.status_code == 400


def test_push_tles_rejects_non_string_constellation_with_400(client):
    resp = client.post('/api/push-tles', json={'constellation': 5, 'text': 'irrelevant'})
    assert resp.status_code == 400


def test_push_tles_rejects_non_string_text_with_400_not_500(client):
    resp = client.post('/api/push-tles', json={'constellation': 'GLONASS', 'text': 12345})
    assert resp.status_code == 400


def test_push_tles_oversized_text_returns_413_before_content_scan(client):
    """The size cap must be checked before the '1 '-substring content check
    so an oversized-but-content-invalid payload gets 413 (accurate), not a
    misleading 400 'No valid TLE content'."""
    text = 'x' * (app_module._PUSH_TLES_MAX_TEXT_BYTES + 1)
    resp = client.post('/api/push-tles', json={'constellation': 'GLONASS', 'text': text})
    assert resp.status_code == 413


def test_load_almanac_rejects_non_string_constellation_with_400(client):
    resp = client.post('/api/load-almanac', json={'constellation': 123})
    assert resp.status_code == 400


def test_load_almanac_rejects_non_dict_json_body(client):
    resp = client.post('/api/load-almanac', json='not-a-dict')
    assert resp.status_code == 400


def test_fetch_tles_proxy_rejects_non_string_constellation_with_400(client):
    resp = client.post('/api/fetch-tles', json={'constellation': 123})
    assert resp.status_code == 400


# ─── /api/rinex-status: full dataset coverage + diagnostics ────────────────

_RINEX_STATUS_DATASETS = (
    'lnav', 'cnav', 'cnv2', 'bds_d', 'bds_cnv1', 'bds_cnv2', 'bds_cnv3',
    'gal_inav', 'gal_fnav', 'glo_fdma',
)


def test_rinex_status_includes_every_dataset_with_required_fields(client):
    resp = client.get('/api/rinex-status')
    assert resp.status_code == 200
    data = resp.get_json()
    for key in _RINEX_STATUS_DATASETS:
        assert key in data, f"missing dataset {key!r} in /api/rinex-status"
        entry = data[key]
        assert set(entry) >= {
            'loaded', 'date', 'prns', 'source', 'age_seconds',
            'last_success', 'last_error', 'malformed',
        }
        assert isinstance(entry['source'], str) and entry['source']
    assert 'rinex4_diag' in data


def test_rinex_status_lnav_reports_malformed_record_counts(client, monkeypatch):
    monkeypatch.setitem(app_module.rinex2_diag, 'records_skipped_short', 2)
    monkeypatch.setitem(app_module.rinex2_diag, 'records_skipped_parse_error', 1)
    monkeypatch.setitem(app_module.rinex2_diag, 'records_skipped_truncated', 0)
    resp = client.get('/api/rinex-status')
    assert resp.get_json()['lnav']['malformed'] == 3


def test_rinex_status_surfaces_last_success_and_error_from_refresh_metrics(client, monkeypatch):
    monkeypatch.setitem(app_module._refresh_metrics['rinex4'], 'last_success', False)
    monkeypatch.setitem(app_module._refresh_metrics['rinex4'], 'last_error', 'RuntimeError: no data')
    resp = client.get('/api/rinex-status')
    data = resp.get_json()
    assert data['cnav']['last_success'] is False
    assert data['cnav']['last_error'] == 'RuntimeError: no data'


def test_rinex_status_never_leaks_a_traceback():
    """rinex4_diag['last_error'] must be a short type/message string —
    _rinex4_refresh_worker is responsible for stripping the traceback before
    storing it, since this dict is serialized as-is into a public endpoint."""
    if app_module.rinex4_diag.get('last_error'):
        assert 'Traceback' not in app_module.rinex4_diag['last_error']


# ─── Generic errors on read-only endpoints (no raw exception exposure) ─────

def test_ionosphere_endpoint_hides_exception_details_on_failure(client):
    secret = 'disk failure at /very/secret/internal/path'
    with patch.object(app_module.json, 'load', side_effect=RuntimeError(secret)):
        resp = client.get('/api/ionosphere')
    assert resp.status_code == 500
    raw = resp.get_data(as_text=True)
    assert secret not in raw
    assert 'RuntimeError' not in raw
    assert 'error' in resp.get_json()


def test_system_time_endpoint_hides_exception_details_on_failure(client):
    secret = 'disk failure at /very/secret/internal/path'
    with patch.object(app_module.json, 'load', side_effect=RuntimeError(secret)):
        resp = client.get('/api/system-time')
    assert resp.status_code == 500
    raw = resp.get_data(as_text=True)
    assert secret not in raw
    assert 'RuntimeError' not in raw


def test_glonass_almanac_endpoint_hides_exception_details_on_failure(client, monkeypatch):
    monkeypatch.setitem(app_module._glo_almanac_cache, 'ts', 0)
    monkeypatch.setitem(app_module._glo_almanac_cache, 'data', {})
    secret = 'connection refused to glonass-iac.ru internal detail'
    with patch.object(app_module, 'fetch_glonass_constellation_status',
                       side_effect=RuntimeError(secret)):
        resp = client.get('/api/glonass-almanac')
    assert resp.status_code == 200  # degrades gracefully, doesn't 500
    raw = resp.get_data(as_text=True)
    assert secret not in raw
    assert 'RuntimeError' not in raw
    assert resp.get_json()['slots'] == {}

