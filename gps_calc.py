#!/usr/bin/env python3
"""
GPS Navigation Message Calculator
Fetches NAVCEN YUMA almanac and calculates satellite positions
Based on IS-GPS-200N standard
"""

from datetime import datetime, timedelta
import requests
import math
import re

# GPS Constants
MU = 3.986005e14
OMEGA_E = 7.2921151467e-5
SPW = 604800
PI = math.pi
GPS_EPOCH = datetime(1980, 1, 6)

def fetch_almanac(year, doy):
    """Download YUMA almanac from NAVCEN"""
    url = f"https://www.navcen.uscg.gov/sites/default/files/gps/almanac/{year}/Yuma/{doy:03d}.alm"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.text
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print(f"❌ Almanac not found for {year} day {doy}")
            return None
        else:
            print(f"❌ HTTP Error {e.response.status_code}")
            return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def parse_yuma(text):
    """Parse YUMA almanac text"""
    sats = []
    for block in text.split("*" * 5):
        if len(block.strip()) < 50:
            continue
        
        def g(key):
            m = re.search(rf"^\s*{key}[^:]*:\s*(-?[0-9.eE+\-]+)", block, re.M | re.I)
            return float(m.group(1)) if m else None
        
        prn = g("ID") or g("PRN")
        if not prn:
            continue
        
        week = int(g("week") or 0)
        if 0 < week < 1024:
            week += 2048
        
        sat = {
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
        }
        sats.append(sat)
    
    return sats

def propagate(sat, gps_sec):
    """Calculate satellite ECEF position"""
    A = sat['sqA'] ** 2
    n0 = math.sqrt(MU / A ** 3)
    t_ref = sat['wk'] * SPW + sat['toa']
    tk = gps_sec - t_ref
    
    M = sat['M0'] + n0 * tk
    E = M
    for _ in range(12):
        E = M + sat['e'] * math.sin(E)
    
    cE = math.cos(E)
    sE = math.sin(E)
    nu = math.atan2(math.sqrt(1 - sat['e']**2) * sE, cE - sat['e'])
    
    phi = nu + sat['w']
    r = A * (1 - sat['e'] * cE)
    xo = r * math.cos(phi)
    yo = r * math.sin(phi)
    
    Om = sat['Om0'] + (sat['dOm'] - OMEGA_E) * tk - OMEGA_E * t_ref
    cO = math.cos(Om)
    sO = math.sin(Om)
    ci = math.cos(sat['inc'])
    si = math.sin(sat['inc'])
    
    x = xo * cO - yo * ci * sO
    y = xo * sO + yo * ci * cO
    z = yo * si
    
    return {'x': x, 'y': y, 'z': z, 'r': math.sqrt(x**2 + y**2 + z**2)}

def geodetic(x, y, z):
    """Convert ECEF to WGS-84 geodetic"""
    a = 6378137.0
    f = 1.0 / 298.257223563
    e2 = 2*f - f*f
    
    lon = math.atan2(y, x)
    p = math.sqrt(x**2 + y**2)
    lat = math.atan2(z, p*(1-e2))
    
    for _ in range(10):
        N = a / math.sqrt(1 - e2*math.sin(lat)**2)
        lat = math.atan2(z + e2*N*math.sin(lat), p)
    
    N = a / math.sqrt(1 - e2*math.sin(lat)**2)
    if abs(lat) < PI/4:
        alt = p / math.cos(lat) - N
    else:
        alt = z / math.sin(lat) - N*(1-e2)
    
    return {'lat': math.degrees(lat), 'lon': math.degrees(lon), 'alt': alt}

def gps_time_from_datetime(dt):
    """Convert datetime to GPS seconds"""
    return (dt - GPS_EPOCH).total_seconds()

def main():
    print("\n" + "="*65)
    print(" GPS NAVIGATION MESSAGE CALCULATOR")
    print(" Based on IS-GPS-200N, WGS-84")
    print("="*65)
    
    satellites = []
    
    while True:
        print("\n" + "-"*65)
        print("MENU")
        print("-"*65)
        print("1. Load almanac (by date)")
        if satellites:
            print(f"2. List satellites ({len(satellites)} loaded)")
            print("3. Calculate position")
        else:
            print("2. List satellites (load first)")
            print("3. Calculate position (load first)")
        print("4. Exit")
        
        choice = input("\nEnter choice (1-4): ").strip()
        
        if choice == "1":
            print("\n" + "-"*65)
            print("LOAD ALMANAC")
            print("-"*65)
            date_str = input("Enter date (YYYY-MM-DD) or 'today': ").strip()
            
            if date_str.lower() == "today":
                dt = datetime.utcnow() - timedelta(days=2)
            else:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    print("❌ Invalid date format. Use YYYY-MM-DD")
                    continue
            
            if dt > datetime.utcnow():
                print("❌ Cannot load future dates")
                continue
            
            year = dt.year
            doy = dt.timetuple().tm_yday
            
            print(f"\nFetching {year} day {doy}...")
            text = fetch_almanac(year, doy)
            
            if text:
                satellites = parse_yuma(text)
                if satellites:
                    print(f"✓ Loaded {len(satellites)} satellites")
                    print(f"  GPS Week: {satellites[0]['wk']}")
                    print(f"  Time of Applicability: {satellites[0]['toa']:.0f} seconds")
                else:
                    print("❌ Could not parse satellites")
        
        elif choice == "2":
            if not satellites:
                print("❌ No almanac loaded. Load one first.")
                continue
            
            print(f"\n" + "-"*65)
            print(f"SATELLITES ({len(satellites)} total)")
            print("-"*65)
            print("PRN  Health  Eccentricity    √A(m)      Inclination")
            print("-"*65)
            for s in satellites:
                status = "✓" if s['health'] == 0 else "✗"
                print(f"{s['id']:3d}    {status}      {s['e']:.8f}      "
                      f"{s['sqA']:8.1f}     {math.degrees(s['inc']):6.2f}°")
        
        elif choice == "3":
            if not satellites:
                print("❌ No almanac loaded. Load one first.")
                continue
            
            print("\n" + "-"*65)
            print("CALCULATE POSITION")
            print("-"*65)
            prn_str = input("Enter PRN number (1-32): ").strip()
            
            try:
                prn = int(prn_str)
                sat = next((s for s in satellites if s['id'] == prn), None)
                if not sat:
                    print(f"❌ PRN {prn} not in current almanac")
                    continue
            except:
                print("❌ Invalid PRN")
                continue
            
            time_str = input("Enter time (YYYY-MM-DD HH:MM:SS) or 'now': ").strip()
            
            if time_str.lower() == "now":
                dt = datetime.utcnow()
            else:
                try:
                    dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                except:
                    print("❌ Invalid format. Use: YYYY-MM-DD HH:MM:SS")
                    continue
            
            gps_sec = gps_time_from_datetime(dt)
            pos = propagate(sat, gps_sec)
            geo = geodetic(pos['x'], pos['y'], pos['z'])
            
            print(f"\n✓ PRN {prn} at {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"\n  ECEF Cartesian Coordinates:")
            print(f"    X = {pos['x']:>14,.0f} m")
            print(f"    Y = {pos['y']:>14,.0f} m")
            print(f"    Z = {pos['z']:>14,.0f} m")
            print(f"    r = {pos['r']/1000:>14,.1f} km")
            print(f"\n  Geodetic Coordinates (WGS-84):")
            print(f"    Latitude  = {geo['lat']:>13.4f}°")
            print(f"    Longitude = {geo['lon']:>13.4f}°")
            print(f"    Altitude  = {geo['alt']/1000:>13.1f} km")
        
        elif choice == "4":
            print("\nGoodbye!\n")
            break
        
        else:
            print("❌ Invalid choice")

if __name__ == "__main__":
    main()
