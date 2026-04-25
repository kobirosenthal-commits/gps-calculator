#!/usr/bin/env python3
from datetime import datetime, timezone
import requests
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
import logging
import math
import os
import re

log = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def _read_cached_rinex(filename):
    """Return cached RINEX text from data/ if present and well-formed, else None.

    The update-tles GitHub Action keeps these files fresh every 6h, which lets
    Render serve RINEX without depending on its (often blocked) outbound network.
    """
    path = os.path.join(_DATA_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None
    if 'END OF HEADER' not in text:
        return None
    log.info(f"RINEX cache hit: {path} ({len(text)} bytes)")
    return text

MU = 3.986005e14
OMEGA_E = 7.2921151467e-5
SPW = 604800
PI = math.pi
GPS_EPOCH = datetime(1980, 1, 6)


def fetch_almanac(year, doy):
    url = f"https://www.navcen.uscg.gov/sites/default/files/gps/almanac/{year}/Yuma/{doy:03d}.alm"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return response.text
    except requests.exceptions.HTTPError as http_err:
        if http_err.response.status_code == 404:
            return None
        raise
    except requests.RequestException:
        return None


def parse_yuma(text):
    satellites = []
    for block in text.split("*" * 5):
        if len(block.strip()) < 50:
            continue

        def g(key):
            match = re.search(rf"^\s*{key}[^:]*:\s*(-?[0-9.eE+\-]+)", block, re.M | re.I)
            return float(match.group(1)) if match else None

        prn = g("ID") or g("PRN")
        if not prn:
            continue

        week = int(g("week") or 0)
        if 0 < week < 1024:
            week += 2048

        satellites.append({
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
        })

    return satellites


def propagate(sat, gps_sec):
    a = sat['sqA'] ** 2
    n0 = math.sqrt(MU / a ** 3)
    t_ref = sat['wk'] * SPW + sat['toa']
    tk = gps_sec - t_ref

    mean_anomaly = sat['M0'] + n0 * tk
    eccentric_anomaly = mean_anomaly
    for _ in range(12):
        eccentric_anomaly = mean_anomaly + sat['e'] * math.sin(eccentric_anomaly)

    cos_e = math.cos(eccentric_anomaly)
    sin_e = math.sin(eccentric_anomaly)
    nu = math.atan2(math.sqrt(1 - sat['e'] ** 2) * sin_e, cos_e - sat['e'])

    phi = nu + sat['w']
    r = a * (1 - sat['e'] * cos_e)
    xo = r * math.cos(phi)
    yo = r * math.sin(phi)

    omega = sat['Om0'] + (sat['dOm'] - OMEGA_E) * tk - OMEGA_E * sat['toa']
    cos_o = math.cos(omega)
    sin_o = math.sin(omega)
    cos_i = math.cos(sat['inc'])
    sin_i = math.sin(sat['inc'])

    x = xo * cos_o - yo * cos_i * sin_o
    y = xo * sin_o + yo * cos_i * cos_o
    z = yo * sin_i

    return {'x': x, 'y': y, 'z': z, 'r': math.sqrt(x ** 2 + y ** 2 + z ** 2)}


def geodetic(x, y, z):
    a = 6378137.0
    f = 1.0 / 298.257223563
    e2 = 2 * f - f * f

    lon = math.atan2(y, x)
    p = math.sqrt(x ** 2 + y ** 2)
    lat = math.atan2(z, p * (1 - e2))

    for _ in range(10):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * n * math.sin(lat), p)

    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    if abs(lat) < PI / 4:
        alt = p / math.cos(lat) - n
    else:
        alt = z / math.sin(lat) - n * (1 - e2)

    return {'lat': math.degrees(lat), 'lon': math.degrees(lon), 'alt': alt}


def gps_time_from_datetime(dt):
    naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    return (naive - GPS_EPOCH).total_seconds()


def _fortran_float(s):
    try:
        return float(s.strip().replace('D', 'E').replace('d', 'e'))
    except ValueError:
        return 0.0


def fetch_gps_rinex(dt):
    """Fetch GPS RINEX 2.x broadcast nav file for date dt. Returns text or None."""
    cached = _read_cached_rinex('gps_rinex2.txt')
    if cached:
        return cached
    import gzip as gz
    doy = dt.timetuple().tm_yday
    yy = dt.year % 100
    yyyy = dt.year
    urls = [
        f"https://noaa-cors-pds.s3.amazonaws.com/rinex/{yyyy}/{doy:03d}/brdc{doy:03d}0.{yy:02d}n.gz",
        f"https://geodesy.noaa.gov/corsdata/rinex/{yyyy}/{doy:03d}/brdc{doy:03d}0.{yy:02d}n.gz",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                log.warning(f"fetch_gps_rinex {url}: HTTP {r.status_code}")
                continue
            content = gz.decompress(r.content)
            text = content.decode('utf-8', errors='replace')
            if 'END OF HEADER' in text:
                log.info(f"fetch_gps_rinex OK: {url} ({len(text)} bytes)")
                return text
        except Exception as e:
            log.warning(f"fetch_gps_rinex {url}: {type(e).__name__}: {e}")
    return None


def parse_rinex2_nav(text):
    """Parse RINEX 2.x GPS broadcast nav. Returns {prn: eph_dict} keyed on most-recent TOE."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if 'END OF HEADER' in lines[i]:
            i += 1
            break
        i += 1

    result = {}
    while i < len(lines) - 7:
        ln = lines[i]
        if len(ln) < 22:
            i += 1
            continue
        try:
            prn   = int(ln[0:2])
            yy    = int(ln[3:5])
            year  = 2000 + yy if yy < 80 else 1900 + yy
            month = int(ln[6:8])
            day   = int(ln[9:11])
            hour  = int(ln[12:14])
            minute = int(ln[15:17])
            sec   = float(ln[17:22])
            af0   = _fortran_float(ln[22:41])
            af1   = _fortran_float(ln[41:60])
            af2   = _fortran_float(ln[60:79])
        except (ValueError, IndexError):
            i += 1
            continue

        vals = []
        for j in range(1, 8):
            ol = lines[i + j] if i + j < len(lines) else ''
            for k in range(4):
                s = 3 + k * 19
                vals.append(_fortran_float(ol[s:s + 19]) if len(ol) > s else 0.0)

        if len(vals) < 28:
            i += 8
            continue

        toe = vals[8]
        if prn not in result or toe > result[prn]['toe']:
            result[prn] = {
                'prn':      prn,
                'epoch':    f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{int(sec):02d}",
                'af0': af0, 'af1': af1, 'af2': af2,
                'iode': vals[0], 'crs': vals[1], 'delta_n': vals[2], 'm0': vals[3],
                'cuc': vals[4], 'e': vals[5],    'cus': vals[6], 'sqrt_a': vals[7],
                'toe': toe,
                'cic': vals[9],  'omega0': vals[10], 'cis': vals[11],
                'i0': vals[12],  'crc': vals[13],    'omega': vals[14], 'omega_dot': vals[15],
                'idot': vals[16], 'l2_codes': int(vals[17]),
                'gps_week': int(vals[18]), 'l2_p_flag': int(vals[19]),
                'ura':    vals[20], 'health': int(vals[21]),
                'tgd':    vals[22], 'iodc':   int(vals[23]),
                'fit_interval': vals[25],
            }
        i += 8

    return result


_SATNOGS_NAME_FILTER = {
    'glo-ops': 'COSMOS 2',
    'beidou':  'BEIDOU',
    'galileo': 'GSAT0',
}


def _fetch_tle_satnogs(group):
    """SatNOGS DB public API — fallback when Celestrak is unavailable."""
    name_contains = _SATNOGS_NAME_FILTER.get(group.lower())
    if not name_contains:
        return None
    import urllib.parse
    encoded = urllib.parse.quote(name_contains)
    # Try satellite name filter first, then TLE line-0 filter as fallback
    candidate_urls = [
        f'https://db.satnogs.org/api/tle/?format=json&page_size=500&satellite__name__icontains={encoded}',
        f'https://db.satnogs.org/api/tle/?format=json&page_size=500&tle0__icontains={encoded}',
    ]
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    for url in candidate_urls:
        try:
            r = requests.get(url, timeout=25, headers=headers)
            if r.status_code != 200:
                log.warning(f"_fetch_tle_satnogs({group}) {url}: HTTP {r.status_code}")
                continue
            data = r.json()
            entries = data.get('results', data) if isinstance(data, dict) else data
            lines = []
            for e in entries:
                n  = str(e.get('tle0', '')).strip()
                l1 = str(e.get('tle1', '')).strip()
                l2 = str(e.get('tle2', '')).strip()
                # Always apply name filter in Python regardless of whether API filtered
                if l1.startswith('1 ') and l2.startswith('2 ') and name_contains.upper() in n.upper():
                    lines.extend([n, l1, l2])
            if lines:
                log.info(f"_fetch_tle_satnogs({group}) OK via {url}: {len(lines)//3} TLEs")
                return '\n'.join(lines)
            log.warning(f"_fetch_tle_satnogs({group}) {url}: no matches for '{name_contains}'")
        except Exception as e:
            log.warning(f"_fetch_tle_satnogs({group}) {url}: {type(e).__name__}: {e}")
    return None


_GITHUB_RAW = "https://raw.githubusercontent.com/kobirosenthal-commits/gps-calculator/main/data/{group}.tle"


def fetch_tle_group(group):
    urls = [
        # GitHub raw — updated by Actions workflow every 6h, always reachable from Render
        _GITHUB_RAW.format(group=group),
        # Celestrak as fallback (may be blocked from cloud IPs)
        f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle",
        f"https://celestrak.com/NORAD/elements/{group}.txt",
    ]
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/plain,text/*,*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://celestrak.org/NORAD/elements/',
    }
    for url in urls:
        try:
            r = requests.get(url, timeout=10, headers=headers, verify=False, allow_redirects=True)
        except Exception as e:
            log.warning(f"fetch_tle_group({group}) {url}: {type(e).__name__}: {e}")
            continue
        if r.status_code != 200:
            log.warning(f"fetch_tle_group({group}) {url}: HTTP {r.status_code}")
            continue
        text = r.text or ''
        if '1 ' in text and not text.lstrip().lower().startswith('no gp'):
            log.info(f"fetch_tle_group({group}) OK via {url} ({len(text)} bytes)")
            return text
        log.warning(f"fetch_tle_group({group}) {url}: no valid TLEs in response")
    log.info(f"fetch_tle_group({group}): all primary sources failed, trying SatNOGS")
    return _fetch_tle_satnogs(group)


def fetch_glonass_slot_map():
    """Fetch {NORAD: slot} mapping for operational GLONASS sats from the Russian IAC.
       Slot is the orbital point number that Trimble and receivers display as R01-R24."""
    url = 'https://glonass-iac.ru/glonass/sostavOG/sostavOG_json.php?lang=en&sort=point'
    headers = {'User-Agent': 'Mozilla/5.0'}
    mapping = {}
    try:
        r = requests.get(url, timeout=15, verify=False, headers=headers)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"GLONASS slot map fetch failed: {type(e).__name__}: {e}")
        return mapping
    for rec in data:
        if rec.get('name') != 'OG':
            continue
        for sat in rec.get('data', []):
            # IAC field 'point' is the orbital slot (R01-R24); 'slot' is the FDMA channel.
            point = str(sat.get('point', '')).strip()
            try:
                norad = int(sat['NORAD'])
            except (KeyError, ValueError, TypeError):
                continue
            if point.isdigit():
                n = int(point)
                if 1 <= n <= 24:
                    mapping[norad] = n
    log.info(f"GLONASS slot map: {len(mapping)} sats with operational slots")
    return mapping


def parse_tles(text):
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tles = []
    i = 0
    while i + 2 <= len(lines) - 1:
        if lines[i + 1].startswith('1 ') and lines[i + 2].startswith('2 '):
            tles.append({'name': lines[i], 'line1': lines[i + 1], 'line2': lines[i + 2]})
            i += 3
        else:
            i += 1
    return tles


def _gmst_rad(jd):
    T = (jd - 2451545.0) / 36525.0
    theta = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
             + T * T * (0.000387933 - T / 38710000.0))
    return math.radians(theta % 360.0)


def propagate_tle(tle, dt):
    from sgp4.api import Satrec, jday
    sat = Satrec.twoline2rv(tle['line1'], tle['line2'])
    naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    jd, fr = jday(naive.year, naive.month, naive.day,
                  naive.hour, naive.minute, naive.second + naive.microsecond / 1e6)
    e, r, _ = sat.sgp4(jd, fr)
    if e != 0:
        return None
    rx, ry, rz = r[0] * 1000.0, r[1] * 1000.0, r[2] * 1000.0  # km → m (ECI)
    gmst = _gmst_rad(jd + fr)
    cg, sg = math.cos(gmst), math.sin(gmst)
    x = rx * cg + ry * sg
    y = -rx * sg + ry * cg
    z = rz
    return {'x': x, 'y': y, 'z': z, 'r': math.sqrt(x * x + y * y + z * z)}


def fetch_gps_rinex4(dt):
    """Fetch RINEX 4 multi-GNSS broadcast nav for date dt from BKG. Returns text or None."""
    cached = _read_cached_rinex('gps_rinex4.txt')
    if cached:
        return cached
    import gzip as gz
    doy = dt.timetuple().tm_yday
    yyyy = dt.year
    url = (f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{yyyy}/{doy:03d}/"
           f"BRD400DLR_S_{yyyy}{doy:03d}0000_01D_MN.rnx.gz")
    try:
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            log.warning(f"fetch_gps_rinex4 {url}: HTTP {r.status_code}")
            return None
        content = gz.decompress(r.content)
        text = content.decode('utf-8', errors='replace')
        if 'END OF HEADER' in text:
            log.info(f"fetch_gps_rinex4 OK: {url} ({len(text)} bytes)")
            return text
        log.warning(f"fetch_gps_rinex4 {url}: no END OF HEADER")
        return None
    except Exception as e:
        log.warning(f"fetch_gps_rinex4 {url}: {type(e).__name__}: {e}")
        return None


def parse_rinex4_nav(text):
    """Parse RINEX 4 GPS CNAV (L2C/L5) records. Returns {prn: cnav_dict} with most recent TOE."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if 'END OF HEADER' in lines[i]:
            i += 1
            break
        i += 1

    result = {}
    while i < len(lines):
        ln = lines[i]
        if ln.startswith('> EPH ') and len(ln) >= 12:
            parts = ln.split()
            if len(parts) >= 4 and parts[2].startswith('G') and parts[3] == 'CNAV':
                try:
                    prn = int(parts[2][1:])
                except (ValueError, IndexError):
                    i += 1
                    continue
                i += 1
                if i >= len(lines):
                    break
                # Epoch line: Gnn YYYY MM DD HH MM SS af0 af1 af2
                el = lines[i]
                try:
                    year   = int(el[4:8])
                    month  = int(el[9:11])
                    day    = int(el[12:14])
                    hour   = int(el[15:17])
                    minute = int(el[18:20])
                    af0    = float(el[23:42])
                    af1    = float(el[42:61])
                    af2    = float(el[61:80])
                except (ValueError, IndexError):
                    i += 1
                    continue
                # 8 broadcast orbit lines, 4 × E19.12 each, 4-space indent
                vals = []
                for j in range(1, 9):
                    if i + j >= len(lines):
                        break
                    ol = lines[i + j]
                    if ol.startswith('>'):
                        break
                    for k in range(4):
                        s = 4 + k * 19
                        try:
                            v = float(ol[s:s + 19]) if len(ol) >= s + 8 else 0.0
                        except (ValueError, IndexError):
                            v = 0.0
                        vals.append(v)
                if len(vals) < 29:
                    i += 1
                    continue
                toe = vals[8]
                if prn not in result or toe > result[prn]['toe']:
                    result[prn] = {
                        'prn':        prn,
                        'epoch':      f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:00",
                        'af0': af0, 'af1': af1, 'af2': af2,
                        'adot':       vals[0],
                        'crs':        vals[1],
                        'delta_n':    vals[2],
                        'm0':         vals[3],
                        'cuc':        vals[4],
                        'e':          vals[5],
                        'cus':        vals[6],
                        'sqrt_a':     vals[7],
                        'toe':        toe,
                        'cic':        vals[9],
                        'omega0':     vals[10],
                        'cis':        vals[11],
                        'i0':         vals[12],
                        'crc':        vals[13],
                        'omega':      vals[14],
                        'omega_dot':  vals[15],
                        'idot':       vals[16],
                        'delta_n_dot': vals[17],
                        'urai_oe':    vals[18],
                        'urai_ed':    vals[20],
                        'tgd':        vals[22],
                        'isc_l1ca':   vals[24] if len(vals) > 24 else 0.0,
                        'isc_l2c':    vals[25] if len(vals) > 25 else 0.0,
                        'isc_l5i5':   vals[26] if len(vals) > 26 else 0.0,
                        'isc_l5q5':   vals[27] if len(vals) > 27 else 0.0,
                        'top':        vals[28],
                        'gps_week':   int(vals[29]) if len(vals) > 29 else 0,
                    }
                i += 1
                continue
        i += 1

    return result


def _read_rinex4_record(lines, i, n_data_lines):
    """Read one RINEX 4 EPH record starting at the epoch line at lines[i].
    Returns (vals, year, month, day, hour, minute, af0, af1, af2, next_i)
    or (None, ...) on parse error. vals is a flat list of 4 * n_data_lines floats.
    """
    if i >= len(lines):
        return None
    el = lines[i]
    try:
        year   = int(el[4:8])
        month  = int(el[9:11])
        day    = int(el[12:14])
        hour   = int(el[15:17])
        minute = int(el[18:20])
        af0    = float(el[23:42])
        af1    = float(el[42:61])
        af2    = float(el[61:80])
    except (ValueError, IndexError):
        return None
    vals = []
    for j in range(1, n_data_lines + 1):
        if i + j >= len(lines):
            break
        ol = lines[i + j]
        if ol.startswith('>'):
            break
        for k in range(4):
            s = 4 + k * 19
            try:
                v = float(ol[s:s + 19]) if len(ol) >= s + 8 else 0.0
            except (ValueError, IndexError):
                v = 0.0
            vals.append(v)
    return {
        'vals': vals, 'year': year, 'month': month, 'day': day,
        'hour': hour, 'minute': minute,
        'af0': af0, 'af1': af1, 'af2': af2,
    }


def parse_rinex4_beidou(text):
    """Parse RINEX 4 BeiDou D1/D2 (legacy B1I/B2I/B3I) and CNV1 (B-CNAV1, B1C) records.

    Returns {'d': {prn: dict}, 'cnv1': {prn: dict}}, each value keyed on most-recent ToE.
    The 'd' bucket merges D1 (MEO/IGSO) and D2 (GEO) since both encode the same
    Kepler set with identical 8-line layout; the dict's 'msg_type' field tells them apart.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if 'END OF HEADER' in lines[i]:
            i += 1
            break
        i += 1

    d_result = {}
    cnv1_result = {}

    while i < len(lines):
        ln = lines[i]
        if not ln.startswith('> EPH '):
            i += 1
            continue
        parts = ln.split()
        if len(parts) < 4 or not parts[2].startswith('C'):
            i += 1
            continue
        msg_type = parts[3]
        try:
            prn = int(parts[2][1:])
        except (ValueError, IndexError):
            i += 1
            continue

        i += 1  # advance to epoch line
        if msg_type in ('D1', 'D2'):
            rec = _read_rinex4_record(lines, i, 8)
            # Field layout per line (4 vals each, indexes 0-31):
            #   L1 0-3: AODE, Crs, dn, M0     L2 4-7: Cuc, e, Cus, sqrtA
            #   L3 8-11: Toe, Cic, Om0, Cis   L4 12-15: i0, Crc, om, OmDot
            #   L5 16-19: IDOT, _, BDTwk, _   L6 20-23: SVacc, SatH1, TGD1, TGD2
            #   L7 24-27: TxTime, AODC, _, _
            if not rec or len(rec['vals']) < 26:
                i += 1
                continue
            v = rec['vals']
            toe = v[8]
            week = int(v[18]) if len(v) > 18 else 0
            existing = d_result.get(prn)
            if (not existing) or (week, toe) > (existing.get('bdt_week', 0), existing['toe']):
                d_result[prn] = {
                    'prn':       prn,
                    'msg_type':  msg_type,
                    'epoch':     f"{rec['year']:04d}-{rec['month']:02d}-{rec['day']:02d} {rec['hour']:02d}:{rec['minute']:02d}:00",
                    'af0': rec['af0'], 'af1': rec['af1'], 'af2': rec['af2'],
                    'aode':      v[0],
                    'crs':       v[1],
                    'delta_n':   v[2],
                    'm0':        v[3],
                    'cuc':       v[4],
                    'e':         v[5],
                    'cus':       v[6],
                    'sqrt_a':    v[7],
                    'toe':       toe,
                    'cic':       v[9],
                    'omega0':    v[10],
                    'cis':       v[11],
                    'i0':        v[12],
                    'crc':       v[13],
                    'omega':     v[14],
                    'omega_dot': v[15],
                    'idot':      v[16],
                    'bdt_week':  week,
                    'ura_index': v[20] if len(v) > 20 else 0.0,
                    'sat_h1':    int(v[21]) if len(v) > 21 else 0,
                    'tgd1':      v[22] if len(v) > 22 else 0.0,
                    'tgd2':      v[23] if len(v) > 23 else 0.0,
                    'tx_time':   v[24] if len(v) > 24 else 0.0,
                    'aodc':      v[25] if len(v) > 25 else 0.0,
                }
            i += 1
            continue
        if msg_type == 'CNV1':
            rec = _read_rinex4_record(lines, i, 10)
            if not rec or len(rec['vals']) < 30:
                i += 1
                continue
            v = rec['vals']
            toe = v[8]
            existing = cnv1_result.get(prn)
            if (not existing) or toe > existing['toe']:
                cnv1_result[prn] = {
                    'prn':         prn,
                    'epoch':       f"{rec['year']:04d}-{rec['month']:02d}-{rec['day']:02d} {rec['hour']:02d}:{rec['minute']:02d}:00",
                    'af0': rec['af0'], 'af1': rec['af1'], 'af2': rec['af2'],
                    'adot':        v[0],
                    'crs':         v[1],
                    'delta_n':     v[2],
                    'm0':          v[3],
                    'cuc':         v[4],
                    'e':           v[5],
                    'cus':         v[6],
                    'sqrt_a':      v[7],
                    'toe':         toe,
                    'cic':         v[9],
                    'omega0':      v[10],
                    'cis':         v[11],
                    'i0':          v[12],
                    'crc':         v[13],
                    'omega':       v[14],
                    'omega_dot':   v[15],
                    'idot':        v[16],
                    'delta_n_dot': v[17],
                    'sat_type':    int(v[18]) if len(v) > 18 else 0,
                    'top':         v[19] if len(v) > 19 else 0.0,
                    'sisai_oe':    v[20] if len(v) > 20 else 0.0,
                    'sisai_ocb':   v[21] if len(v) > 21 else 0.0,
                    'sisai_oc1':   v[22] if len(v) > 22 else 0.0,
                    'sisai_oc2':   v[23] if len(v) > 23 else 0.0,
                    'isc_b1cd':    v[24] if len(v) > 24 else 0.0,
                    'tgd_b1cp':    v[26] if len(v) > 26 else 0.0,
                    'tgd_b2ap':    v[27] if len(v) > 27 else 0.0,
                    'sismai':      v[28] if len(v) > 28 else 0.0,
                    'health':      int(v[29]) if len(v) > 29 else 0,
                    'integrity':   int(v[30]) if len(v) > 30 else 0,
                    'tx_time':     v[32] if len(v) > 32 else 0.0,
                }
            i += 1
            continue
        i += 1

    return {'d': d_result, 'cnv1': cnv1_result}
