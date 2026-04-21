#!/usr/bin/env python3
from datetime import datetime, timezone
import requests
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
