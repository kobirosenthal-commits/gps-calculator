#!/usr/bin/env python3
from datetime import datetime, timezone
import requests
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
import math
import re

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

    omega = sat['Om0'] + (sat['dOm'] - OMEGA_E) * tk - OMEGA_E * t_ref
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


def fetch_tle_group(group):
    urls = [
        f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle",
        f"https://www.celestrak.com/NORAD/elements/{group}.txt",
        f"https://celestrak.org/pub/TLE/groups/{group}.txt",
    ]
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for url in urls:
        for verify in (True, False):
            try:
                r = requests.get(url, timeout=2, headers=headers, verify=verify)
                r.raise_for_status()
                text = r.text
                if text and '1 ' in text and not text.lstrip().lower().startswith('no gp'):
                    return text
                break
            except Exception:
                continue
    return None


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
