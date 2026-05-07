"""
Fetch CONUS ASOS station metadata from Iowa Environmental Mesonet.
Saves to data/processed/stations.csv.
"""

import json
from pathlib import Path

import pandas as pd
import requests

CONUS_STATES = [
    'AL', 'AR', 'AZ', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'IA',
    'ID', 'IL', 'IN', 'KS', 'KY', 'LA', 'MA', 'MD', 'ME', 'MI',
    'MN', 'MO', 'MS', 'MT', 'NC', 'ND', 'NE', 'NH', 'NJ', 'NM',
    'NV', 'NY', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN',
    'TX', 'UT', 'VA', 'VT', 'WA', 'WI', 'WV', 'WY'
]


def extract_station(feature):
    """Defensively extract station info from a GeoJSON feature."""
    props = feature.get('properties', {}) or {}
    geom = feature.get('geometry') or {}
    coords = geom.get('coordinates') if geom else None

    if not coords or len(coords) < 2:
        return None

    station_id = (
        feature.get('id')
        or props.get('sid')
        or props.get('id')
        or props.get('station')
        or props.get('station_id')
    )
    if not station_id:
        return None

    return {
        'station_id': station_id,
        'name': props.get('sname') or props.get('name'),
        'lon': coords[0],
        'lat': coords[1],
        'elev_m': props.get('elevation'),
        'tzname': props.get('tzname'),
    }


def fetch_station_list():
    all_stations = []
    debug_printed = False
    for state in CONUS_STATES:
        url = f"https://mesonet.agron.iastate.edu/api/1/network/{state}_ASOS.geojson"
        print(f"Fetching {state}...", end=' ', flush=True)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
            features = data.get('features', [])

            if not debug_printed and features:
                print()
                print(f"  [debug] First feature from {state}:")
                print(f"  [debug] {json.dumps(features[0], indent=2)[:400]}")
                debug_printed = True

            n_added = 0
            for f in features:
                rec = extract_station(f)
                if rec is None:
                    continue
                rec['state'] = state
                all_stations.append(rec)
                n_added += 1
            print(f"{n_added}/{len(features)} stations")
        except Exception as e:
            print(f"FAILED: {e}")

    return pd.DataFrame(all_stations)


if __name__ == '__main__':
    output_path = Path('data/processed/stations.csv')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df = fetch_station_list()
    df.to_csv(output_path, index=False)
    print(f"\nSaved {len(df)} stations to {output_path}")
    if len(df) > 0:
        print(f"States covered: {df['state'].nunique()}")
        print(df.head())
