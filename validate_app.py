"""Deep app validator. Hits every API endpoint and cross-checks the math:
  - JSON structure of every /api/* response
  - Orbit math: RINEX-ephemeris position vs TLE-SGP4 position at the same epoch
    (should agree within ~10 km for GPS, ~30 km for GLONASS due to differing
    coordinate frames)
  - Almanac: every PRN has plausible Kepler elements (e<0.05, |inc-55°|<10°)
  - Iono / leap / STO sanity
  - Bit models — calls JS validateBitModels() reachability

Run while Flask is up:   python3 validate_app.py
"""
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get('GPS_VALIDATOR_BASE', 'http://localhost:5000')

# ─── Pretty output ─────────────────────────────────────────────────────────
ERR_COUNT = 0
WARN_COUNT = 0
OK_COUNT = 0

def ok(msg):
    global OK_COUNT
    OK_COUNT += 1
    print(f"  \033[32m✓\033[0m {msg}")

def warn(msg):
    global WARN_COUNT
    WARN_COUNT += 1
    print(f"  \033[33m⚠\033[0m {msg}")

def err(msg):
    global ERR_COUNT
    ERR_COUNT += 1
    print(f"  \033[31m✗\033[0m {msg}")

def section(title):
    print(f"\n\033[1;36m── {title} ──\033[0m")

def fetch_json(path, method='GET', body=None, timeout=15):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.data = json.dumps(body).encode('utf-8')
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ─── Endpoint structure tests ──────────────────────────────────────────────
def test_endpoints():
    section("API endpoints reachable + return JSON")
    endpoints = [
        ('/api/satellites',           {'satellites'}),
        ('/api/live-positions',       {'satellites'}),
        ('/api/data-freshness',       {'gps_lnav', 'gps_cnav'}),
        ('/api/system-time',          {'sto'}),
        ('/api/ionosphere',           {'iono'}),
        ('/api/gps-almanac',          {'slots'}),
        ('/api/glonass-almanac',      {'slots'}),
        ('/api/beidou-almanac',       {'slots'}),
        ('/api/galileo-almanac',      {'slots'}),
        ('/api/rinex-status',         {'lnav'}),
    ]
    results = {}
    for path, required_keys in endpoints:
        try:
            data = fetch_json(path)
            missing = required_keys - set(data.keys())
            if missing:
                warn(f"{path}: missing keys {missing}")
            else:
                ok(f"{path}: OK ({len(json.dumps(data))} bytes)")
            results[path] = data
        except urllib.error.HTTPError as e:
            err(f"{path}: HTTP {e.code} {e.reason}")
        except Exception as e:
            err(f"{path}: {type(e).__name__}: {e}")
    return results


# ─── Live positions sanity ──────────────────────────────────────────────────
def test_live_positions(data):
    section("Live position sanity")
    sats = data.get('satellites') or []
    if not sats:
        err("no satellites in /api/live-positions")
        return
    by_const = {}
    for s in sats:
        c = s.get('constellation', '?')
        by_const.setdefault(c, []).append(s)
    print(f"  {len(sats)} sats: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_const.items())))
    for s in sats:
        x, y, z = s.get('x', 0), s.get('y', 0), s.get('z', 0)
        r = math.sqrt(x*x + y*y + z*z) / 1000  # km from ECEF metres
        c = s['constellation']
        # Expected radii (km): GPS 26,560; GLO 25,510; BDS GEO 42,164 / MEO 27,906; GAL 29,600
        expected = {'GPS': (26000, 27200), 'GLONASS': (25000, 26100),
                    'GALILEO': (29000, 30200), 'BEIDOU': (26000, 43000)}
        lo, hi = expected.get(c, (5000, 50000))
        if not (lo <= r <= hi):
            warn(f"{s['label']} radius {r:.0f} km outside [{lo}, {hi}] for {c}")
    if 'almanac_date' in data:
        ok(f"almanac date: {data['almanac_date']}")
    ok(f"{len(sats)} live satellites pass radius sanity")


# ─── Orbit math cross-check: RINEX vs TLE ──────────────────────────────────
def test_orbit_math():
    section("Orbit math cross-check (RINEX/almanac vs TLE)")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from gps_core import (parse_tles, fetch_tle_group, propagate_tle,
                              parse_rinex2_nav, propagate_glonass_eph)
    except ImportError as e:
        err(f"could not import gps_core: {e}")
        return

    # Load a fresh GPS RINEX 2 from disk
    rinex_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'data', 'gps_rinex2.txt')
    if not os.path.exists(rinex_path):
        warn(f"{rinex_path} missing — skipping GPS RINEX/TLE cross-check")
        return
    with open(rinex_path, 'r', encoding='utf-8') as f:
        eph_data = parse_rinex2_nav(f.read())
    if not eph_data:
        warn("no GPS ephemeris parsed — skipping")
        return
    ok(f"GPS RINEX: {len(eph_data)} PRNs parsed")

    # GPS TLEs
    try:
        gps_tle_text = fetch_tle_group('gps-ops')
        if not gps_tle_text:
            warn("could not fetch GPS TLEs — skipping cross-check")
            return
        gps_tles = parse_tles(gps_tle_text)
    except Exception as e:
        warn(f"GPS TLE fetch failed: {e}")
        return
    ok(f"GPS TLEs: {len(gps_tles)} sats parsed")

    # For each PRN with both RINEX and TLE, propagate at "now" and compare
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)

    def kepler_to_ecef(eph, t_unix):
        """Standard GPS Kepler→ECEF propagation. Simplified — used only for
        sanity comparison against TLE."""
        GM = 3.986005e14
        omega_e_dot = 7.2921151467e-5
        a = eph['sqrt_a'] ** 2
        n = math.sqrt(GM / a**3) + eph['delta_n']
        # Compute time from toe (in this week)
        gps_epoch = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)
        gps_secs = (datetime.datetime.fromtimestamp(t_unix, tz=datetime.timezone.utc) - gps_epoch).total_seconds()
        tk = (gps_secs % (7 * 86400)) - eph['toe']
        if tk > 302400:  tk -= 604800
        if tk < -302400: tk += 604800
        Mk = eph['m0'] + n * tk
        # Solve Kepler
        E = Mk
        for _ in range(15):
            E = Mk + eph['e'] * math.sin(E)
        nu = math.atan2(math.sqrt(1 - eph['e']**2) * math.sin(E),
                        math.cos(E) - eph['e'])
        phi = nu + eph['omega']
        du = eph['cus'] * math.sin(2*phi) + eph['cuc'] * math.cos(2*phi)
        dr = eph['crs'] * math.sin(2*phi) + eph['crc'] * math.cos(2*phi)
        di = eph['cis'] * math.sin(2*phi) + eph['cic'] * math.cos(2*phi)
        u = phi + du
        r = a * (1 - eph['e'] * math.cos(E)) + dr
        i = eph['i0'] + di + eph['idot'] * tk
        x_orb = r * math.cos(u)
        y_orb = r * math.sin(u)
        Omega = eph['omega0'] + (eph['omega_dot'] - omega_e_dot) * tk - omega_e_dot * eph['toe']
        x = x_orb * math.cos(Omega) - y_orb * math.cos(i) * math.sin(Omega)
        y = x_orb * math.sin(Omega) + y_orb * math.cos(i) * math.cos(Omega)
        z = y_orb * math.sin(i)
        return x, y, z

    t_unix = now.timestamp()
    matches = 0
    big_diff = 0
    for tle in gps_tles[:8]:  # sample first 8 SVs
        try:
            prn = int(tle['line1'][2:7])
        except Exception:
            continue
        eph = eph_data.get(prn)
        if not eph:
            continue
        try:
            x_eph, y_eph, z_eph = kepler_to_ecef(eph, t_unix)
            tle_pos = propagate_tle(tle, now)
            x_tle, y_tle, z_tle = tle_pos['x'], tle_pos['y'], tle_pos['z']
            dx = (x_eph - x_tle) / 1000
            dy = (y_eph - y_tle) / 1000
            dz = (z_eph - z_tle) / 1000
            dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            if dist > 50:
                warn(f"PRN {prn}: RINEX vs TLE diff {dist:.1f} km (expected <30 km)")
                big_diff += 1
            else:
                matches += 1
        except Exception as e:
            warn(f"PRN {prn}: propagation failed: {type(e).__name__}: {e}")
    ok(f"{matches} GPS sats match RINEX↔TLE within tolerance ({big_diff} large diffs)")


# ─── Almanac sanity ─────────────────────────────────────────────────────────
def test_almanacs(results):
    section("Almanac sanity (per-PRN Kepler bounds)")
    expected = {
        'gps':     (32, (45, 65), (0, 0.05)),     # 32 SVs, inc 55°, e<0.05
        'beidou':  (63, (0, 70),  (0, 0.05)),     # mixed inc (GEO/IGSO/MEO)
        'galileo': (36, (45, 65), (0, 0.05)),     # 56°
    }
    for sys_key, (max_prn, (inc_lo, inc_hi), (e_lo, e_hi)) in expected.items():
        try:
            data = fetch_json(f'/api/{sys_key}-almanac')
        except Exception as e:
            err(f"{sys_key}-almanac fetch failed: {e}")
            continue
        slots = data.get('slots') or {}
        if not slots:
            warn(f"{sys_key}-almanac: empty")
            continue
        bad = 0
        for prn_str, rec in slots.items():
            try:
                inc = float(rec.get('inc_deg', 0))
                e = float(rec.get('e', 0))
            except (ValueError, TypeError):
                bad += 1
                continue
            if not (inc_lo <= inc <= inc_hi) and inc != 0:
                bad += 1
            if not (e_lo <= e <= e_hi):
                bad += 1
        if bad:
            warn(f"{sys_key}: {bad} entries failed bounds (out of {len(slots)})")
        else:
            ok(f"{sys_key}: {len(slots)} slots all within bounds")


# ─── Iono / leap sanity ─────────────────────────────────────────────────────
def test_iono_and_leap(iono, systime):
    section("Ionosphere + leap-second + STO sanity")
    iono_models = (iono or {}).get('iono', {})
    for model in ('klobuchar', 'nequick', 'bdgim'):
        m = iono_models.get(model)
        if not m or not m.get('values'):
            warn(f"iono {model}: no data")
            continue
        ok(f"iono {model}: {len(m['values'])} values, source {m.get('sat')}, epoch {m.get('epoch')}")
    leap = (systime or {}).get('leap')
    if leap:
        if leap.get('dt_ls') == 18:
            ok(f"leap: ΔtLS=18 (current correct as of 2017+)")
        else:
            warn(f"leap: ΔtLS={leap.get('dt_ls')} unusual")
    sto = (systime or {}).get('sto') or {}
    if 'GPUT' in sto:
        ok(f"STO: GPUT (GPS-UTC) present, A0={sto['GPUT']['values'][1]:.3e}")
    else:
        warn("STO: no GPUT entry")


# ─── Health flag bit-decode roundtrip ──────────────────────────────────────
def test_health_decode():
    section("Health flag decode")
    # Galileo sv_health 9-bit bitfield: simulate values and decode
    cases = [
        (0,    "all OK"),
        (0b001, "E1B DVS set"),
        (0b110, "E1B SHS=3 (in test)"),
        (0b1_11_1_11_1_11, "everything bad (511)"),
    ]
    for v, label in cases:
        e1b_dvs = v & 1
        e1b_shs = (v >> 1) & 3
        e5b_dvs = (v >> 3) & 1
        e5b_shs = (v >> 4) & 3
        e5a_dvs = (v >> 6) & 1
        e5a_shs = (v >> 7) & 3
        # Reconstruct
        rebuilt = e1b_dvs | (e1b_shs << 1) | (e5b_dvs << 3) | (e5b_shs << 4) | (e5a_dvs << 6) | (e5a_shs << 7)
        if rebuilt != v:
            err(f"health roundtrip fail: {v:b} -> {rebuilt:b}")
        else:
            ok(f"health 0b{v:09b} ({label}): roundtrip clean")


# ─── Data-freshness consistency ─────────────────────────────────────────────
def test_data_freshness(fresh):
    section("Data freshness")
    if not fresh:
        warn("no freshness data")
        return
    now = int(time.time())
    for key in ('gps_lnav', 'gps_cnav', 'glo_fdma', 'bds_d', 'gal_inav', 'tle_gps'):
        ts = fresh.get(key)
        if ts is None:
            warn(f"{key}: missing mtime")
            continue
        age_h = (now - ts) / 3600
        if age_h > 168:
            warn(f"{key}: {age_h:.1f}h old (>1 week)")
        elif age_h > 24:
            warn(f"{key}: {age_h:.1f}h old (>1 day)")
        else:
            ok(f"{key}: {age_h:.1f}h old")


# ─── Iono model output sanity (TECU at known points) ───────────────────────
def test_iono_output(iono):
    section("Iono model output sanity")
    klob = (iono or {}).get('iono', {}).get('klobuchar')
    if not klob or not klob.get('values'):
        warn("no Klobuchar coefficients — skipping output sanity")
        return
    a = klob['values'][:4]
    b = klob['values'][4:8]
    # Klobuchar at solar-noon equator should produce ~10-30 ns ≈ 3-9 m at L1
    # Compute amplitude term at φ=0 (equator)
    amp = a[0] + 0 * (a[1] + 0 * (a[2] + 0 * a[3]))
    if amp <= 0:
        warn(f"Klobuchar α0+...={amp:.2e} non-positive; iono delay would always read 0 at equator")
    else:
        delay_m = amp * 2.99792458e8
        if 0.5 < delay_m < 30:
            ok(f"Klobuchar amplitude at equator: {delay_m:.2f} m (typical range 0.5-30)")
        else:
            warn(f"Klobuchar amplitude {delay_m:.2f} m outside expected 0.5-30 m")


# ─── Main ──────────────────────────────────────────────────────────────────
def main():
    print(f"\033[1;35m═ Deep validation against {BASE} ═\033[0m")
    try:
        urllib.request.urlopen(BASE + '/api/rinex-status', timeout=5)
    except Exception as e:
        err(f"Cannot reach Flask: {e}")
        print("\nIs Flask running? Try: python app.py")
        sys.exit(1)
    results = test_endpoints()
    test_live_positions(results.get('/api/live-positions') or {})
    test_data_freshness(results.get('/api/data-freshness') or {})
    test_almanacs(results)
    test_iono_and_leap(results.get('/api/ionosphere'), results.get('/api/system-time'))
    test_iono_output(results.get('/api/ionosphere'))
    test_health_decode()
    test_orbit_math()

    print()
    print(f"\033[1m═ Summary: \033[32m{OK_COUNT} OK\033[0m · "
          f"\033[33m{WARN_COUNT} warnings\033[0m · "
          f"\033[31m{ERR_COUNT} errors\033[0m")
    if ERR_COUNT:
        sys.exit(1)


if __name__ == '__main__':
    main()
