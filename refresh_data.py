"""Local data-refresh script. Replicates .github/workflows/update-tles.yml so
you can refresh TLEs and RINEX nav data on your own machine without going
through git. Safe to run from CLI or from the Flask /api/refresh-data
endpoint. Writes into data/ next to this file."""

import os
import sys
import gzip
import json
import datetime
import urllib.request
from io import StringIO

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
HEADERS = {'User-Agent': 'gps-calculator-bot'}


def _http_get(url, timeout=120):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read()


def _fetch_text(urls, timeout=60):
    """Try each URL in order, return decoded body of first success."""
    for url in urls:
        try:
            body = _http_get(url, timeout=timeout)
            if url.endswith('.gz'):
                body = gzip.decompress(body)
            return body.decode('utf-8', errors='replace'), url
        except Exception as e:
            yield_msg = f"FAIL {url}: {type(e).__name__}: {e}"
            print(yield_msg)
    return None, None


def refresh_tles(log):
    groups = {
        'glo-ops.tle': [
            'https://celestrak.org/NORAD/elements/gp.php?GROUP=glo-ops&FORMAT=tle',
            'https://celestrak.com/NORAD/elements/glo-ops.txt',
        ],
        'beidou.tle': [
            'https://celestrak.org/NORAD/elements/gp.php?GROUP=beidou&FORMAT=tle',
            'https://celestrak.com/NORAD/elements/beidou.txt',
        ],
        'galileo.tle': [
            'https://celestrak.org/NORAD/elements/gp.php?GROUP=galileo&FORMAT=tle',
            'https://celestrak.com/NORAD/elements/galileo.txt',
        ],
        'gps.tle': [
            'https://celestrak.org/NORAD/elements/gp.php?GROUP=gps-ops&FORMAT=tle',
            'https://celestrak.com/NORAD/elements/gps-ops.txt',
        ],
    }
    out = {}
    for fname, urls in groups.items():
        text, used = None, None
        for url in urls:
            try:
                body = _http_get(url, timeout=60)
                text = body.decode('utf-8', errors='replace')
                used = url
                break
            except Exception as e:
                log(f"  TLE {fname} fail {url}: {type(e).__name__}: {e}")
        if text and 'TLE' not in text and len(text) > 50:
            with open(os.path.join(DATA_DIR, fname), 'w', encoding='utf-8') as f:
                f.write(text)
            out[fname] = len(text)
            log(f"  TLE OK: {fname} ({len(text)} B from {used})")
        else:
            out[fname] = None
            log(f"  TLE FAIL: {fname}")
    return out


def refresh_rinex2(log):
    for offset in range(0, 3):
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=offset)
        doy = dt.timetuple().tm_yday
        yy = dt.year % 100
        yyyy = dt.year
        urls = [
            f"https://noaa-cors-pds.s3.amazonaws.com/rinex/{yyyy}/{doy:03d}/brdc{doy:03d}0.{yy:02d}n.gz",
            f"https://geodesy.noaa.gov/corsdata/rinex/{yyyy}/{doy:03d}/brdc{doy:03d}0.{yy:02d}n.gz",
        ]
        for url in urls:
            try:
                body = _http_get(url, timeout=60)
                text = gzip.decompress(body).decode('utf-8', errors='replace')
                if 'END OF HEADER' in text:
                    with open(os.path.join(DATA_DIR, 'gps_rinex2.txt'), 'w', encoding='utf-8') as f:
                        f.write(text)
                    log(f"  RINEX2 OK: {url} ({len(text)} B)")
                    return True
            except Exception as e:
                log(f"  RINEX2 fail {url}: {type(e).__name__}: {e}")
    log("  RINEX2 FAIL — keeping previous file")
    return False


def refresh_rinex4(log):
    for offset in range(1, 5):
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=offset)
        doy = dt.timetuple().tm_yday
        yyyy = dt.year
        url = (f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{yyyy}/{doy:03d}/"
               f"BRD400DLR_S_{yyyy}{doy:03d}0000_01D_MN.rnx.gz")
        try:
            body = _http_get(url, timeout=180)
            text = gzip.decompress(body).decode('utf-8', errors='replace')
            if 'END OF HEADER' in text:
                with open(os.path.join(DATA_DIR, 'gps_rinex4.txt'), 'w', encoding='utf-8') as f:
                    f.write(text)
                log(f"  RINEX4 OK: {url} ({len(text)} B)")
                return dt
        except Exception as e:
            log(f"  RINEX4 fail {url}: {type(e).__name__}: {e}")
    log("  RINEX4 FAIL — keeping previous file")
    return None


def reparse_rinex4_jsons(dt, log):
    """Mirror of the CI 'Pre-parse RINEX 4' step."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gps_core import parse_rinex4_combined, _rinex4_line_iter
    parsed = parse_rinex4_combined(_rinex4_line_iter(dt))
    date_str = dt.strftime('%Y-%m-%d')
    targets = (
        ('gps_cnav', 'rinex4_gps_cnav.json'),
        ('bds_d',    'rinex4_bds_d.json'),
        ('bds_cnv1', 'rinex4_bds_cnv1.json'),
        ('bds_cnv2', 'rinex4_bds_cnv2.json'),
        ('bds_cnv3', 'rinex4_bds_cnv3.json'),
        ('gal_inav', 'rinex4_gal_inav.json'),
        ('gal_fnav', 'rinex4_gal_fnav.json'),
        ('glo_fdma', 'rinex4_glo_fdma.json'),
    )
    out = {}
    for key, fname in targets:
        eph = parsed.get(key) or {}
        if not eph:
            log(f"  JSON skip (empty): {fname}")
            out[fname] = 0
            continue
        eph_str = {str(k): v for k, v in eph.items()}
        with open(os.path.join(DATA_DIR, fname), 'w', encoding='utf-8') as f:
            json.dump({'ephemeris': eph_str, 'date': date_str}, f)
        log(f"  JSON OK: {fname} ({len(eph)} PRNs)")
        out[fname] = len(eph)
    iono = parsed.get('iono') or {}
    if any(iono.values()):
        with open(os.path.join(DATA_DIR, 'rinex4_iono.json'), 'w', encoding='utf-8') as f:
            json.dump({'iono': iono, 'date': date_str}, f)
        log(f"  JSON OK: rinex4_iono.json (klobuchar/nequick/bdgim)")
        out['rinex4_iono.json'] = sum(1 for v in iono.values() if v)
    return out


def refresh_all(log_fn=None):
    """Run the full refresh. Returns a summary dict. log_fn(str) is called
    for each progress line (defaults to print)."""
    log = log_fn or print
    os.makedirs(DATA_DIR, exist_ok=True)
    summary = {'started': datetime.datetime.utcnow().isoformat() + 'Z'}
    log("→ TLEs")
    summary['tles'] = refresh_tles(log)
    log("→ RINEX 2 GPS LNAV")
    summary['rinex2'] = refresh_rinex2(log)
    log("→ RINEX 4 multi-GNSS CNAV")
    dt = refresh_rinex4(log)
    summary['rinex4'] = bool(dt)
    if dt:
        log("→ Re-parse RINEX 4 → per-constellation JSONs")
        summary['jsons'] = reparse_rinex4_jsons(dt, log)
    summary['finished'] = datetime.datetime.utcnow().isoformat() + 'Z'
    log("Done.")
    return summary


if __name__ == '__main__':
    refresh_all()
