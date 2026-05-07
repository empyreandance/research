"""
Parse raw METAR CSVs, compute hourly apparent temperature, aggregate to daily max/min.
Output: one parquet file per station in data/processed/daily_appt/.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from src.apparent_temp import apparent_temperature

RAW_DIR = Path('data/raw')
DAILY_DIR = Path('data/processed/daily_appt')

# QC bounds (anything outside is treated as bad data and dropped)
T_MIN_F, T_MAX_F = -80, 130
TD_MIN_F, TD_MAX_F = -100, 90
WIND_MIN_KT, WIND_MAX_KT = 0, 250

# Daily QC: a day must have at least this many valid hourly observations
MIN_OBS_PER_DAY = 12


def process_station(station_id, longitude):
    raw_path = RAW_DIR / f'{station_id}.csv.gz'
    output_path = DAILY_DIR / f'{station_id}.parquet'

    if not raw_path.exists():
        return f'NO_FILE {station_id}'
    if output_path.exists():
        return f'SKIP {station_id}'

    try:
        df = pd.read_csv(raw_path, parse_dates=['valid'], low_memory=False)
    except Exception as e:
        return f'PARSE_ERR {station_id}: {e}'

    needed = {'valid', 'tmpf', 'dwpf', 'sknt'}
    if not needed.issubset(df.columns):
        return f'BAD_COLS {station_id}: {set(df.columns)}'

    for col in ['tmpf', 'dwpf', 'sknt']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=['tmpf', 'dwpf', 'sknt', 'valid'])

    df = df[(df['tmpf'] >= T_MIN_F) & (df['tmpf'] <= T_MAX_F)]
    df = df[(df['dwpf'] >= TD_MIN_F) & (df['dwpf'] <= TD_MAX_F)]
    df = df[(df['sknt'] >= WIND_MIN_KT) & (df['sknt'] <= WIND_MAX_KT)]
    df = df[df['dwpf'] <= df['tmpf'] + 1]  # 1F slop for measurement error

    if len(df) == 0:
        return f'EMPTY_AFTER_QC {station_id}'

    df['wind_mph'] = df['sknt'] * 1.15078

    df['appt'] = apparent_temperature(
        df['tmpf'].values,
        df['dwpf'].values,
        df['wind_mph'].values,
    )

    offset_hours = longitude / 15.0
    df['local_dt'] = df['valid'] + pd.Timedelta(hours=offset_hours)
    df['local_date'] = df['local_dt'].dt.normalize()

    daily = df.groupby('local_date').agg(
        appt_max=('appt', 'max'),
        appt_min=('appt', 'min'),
        n_obs=('appt', 'count'),
    ).reset_index()

    daily = daily[daily['n_obs'] >= MIN_OBS_PER_DAY].copy()

    if len(daily) == 0:
        return f'NO_VALID_DAYS {station_id}'

    daily['station_id'] = station_id
    daily['local_date'] = pd.to_datetime(daily['local_date']).dt.date

    output_path.parent.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(output_path)

    return f'OK {station_id} ({len(daily)} days)'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    stations_df = pd.read_csv('data/processed/stations.csv')
    stations = list(zip(stations_df['station_id'], stations_df['lon']))

    print(f'Processing {len(stations)} stations with {args.workers} workers')
    print()

    n_ok = n_skip = n_fail = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(process_station, sid, lon): sid
            for sid, lon in stations
        }
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            print(f'[{i}/{len(stations)}] {result}', flush=True)
            if result.startswith('OK'):
                n_ok += 1
            elif result.startswith('SKIP'):
                n_skip += 1
            else:
                n_fail += 1

    print(f'\nSummary: {n_ok} processed, {n_skip} skipped, {n_fail} failed/empty')


if __name__ == '__main__':
    main()
