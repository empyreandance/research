"""
Build per-station per-calendar-date climatology of daily max/min apparent temperature.
Uses ±7 day smoothing window. Excludes stations with fewer than 10 years of valid data.
Output: data/processed/climatology.nc
"""

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

DAILY_DIR = Path('data/processed/daily_appt')
STATIONS_PATH = Path('data/processed/stations.csv')
OUTPUT_PATH = Path('data/processed/climatology.nc')

MIN_YEARS = 10
MIN_DAYS_PER_YEAR = 200
SMOOTHING_DAYS = 7


def doy_window_mask(doys, target_doy, window):
    diff = np.abs(doys - target_doy)
    diff = np.minimum(diff, 366 - diff)
    return diff <= window


def build_station_clim(station_id, df):
    df = df.copy()
    df['date'] = pd.to_datetime(df['local_date'])
    df['year'] = df['date'].dt.year
    df['doy'] = df['date'].dt.dayofyear

    year_counts = df.groupby('year').size()
    valid_years = year_counts[year_counts >= MIN_DAYS_PER_YEAR].index
    n_years = len(valid_years)

    if n_years < MIN_YEARS:
        return None, n_years

    df = df[df['year'].isin(valid_years)]

    doys = df['doy'].values
    appt_max = df['appt_max'].values
    appt_min = df['appt_min'].values

    out = np.full((366, 5), np.nan, dtype=np.float32)
    for d in range(1, 367):
        mask = doy_window_mask(doys, d, SMOOTHING_DAYS)
        if mask.sum() < 30:
            continue
        out[d - 1, 0] = appt_max[mask].mean()
        out[d - 1, 1] = appt_max[mask].std()
        out[d - 1, 2] = appt_min[mask].mean()
        out[d - 1, 3] = appt_min[mask].std()
        out[d - 1, 4] = mask.sum()

    return out, n_years


def main():
    stations_df = pd.read_csv(STATIONS_PATH).set_index('station_id')

    daily_files = sorted(DAILY_DIR.glob('*.parquet'))
    print(f'Found {len(daily_files)} daily files')

    station_ids = []
    lats = []
    lons = []
    elevs = []
    n_years_list = []
    arrays = []

    for i, f in enumerate(daily_files, 1):
        sid = f.stem
        if sid not in stations_df.index:
            continue
        try:
            df = pd.read_parquet(f)
        except Exception as e:
            print(f'[{i}/{len(daily_files)}] PARSE_ERR {sid}: {e}', flush=True)
            continue

        result, n_years = build_station_clim(sid, df)
        if result is None:
            print(f'[{i}/{len(daily_files)}] SKIP {sid} (only {n_years} valid years)', flush=True)
            continue

        meta = stations_df.loc[sid]
        station_ids.append(sid)
        lats.append(float(meta['lat']))
        lons.append(float(meta['lon']))
        elevs.append(float(meta['elev_m']) if pd.notna(meta['elev_m']) else np.nan)
        n_years_list.append(n_years)
        arrays.append(result)

        if i % 100 == 0:
            print(f'[{i}/{len(daily_files)}] processed {len(station_ids)} valid stations', flush=True)

    print(f'\nKept {len(station_ids)} stations meeting {MIN_YEARS}-year minimum')

    data = np.stack(arrays, axis=0)

    ds = xr.Dataset(
        data_vars={
            'max_mean': (('station', 'doy'), data[:, :, 0]),
            'max_std': (('station', 'doy'), data[:, :, 1]),
            'min_mean': (('station', 'doy'), data[:, :, 2]),
            'min_std': (('station', 'doy'), data[:, :, 3]),
            'n_samples': (('station', 'doy'), data[:, :, 4]),
            'lat': ('station', np.array(lats)),
            'lon': ('station', np.array(lons)),
            'elev_m': ('station', np.array(elevs)),
            'n_years': ('station', np.array(n_years_list, dtype=np.int16)),
        },
        coords={
            'station': np.array(station_ids),
            'doy': np.arange(1, 367),
        },
        attrs={
            'description': 'Per-station per-calendar-date climatology of daily max/min apparent temperature',
            'baseline_period': '1995-2024',
            'smoothing_window_days': SMOOTHING_DAYS * 2 + 1,
            'min_years_required': MIN_YEARS,
            'min_days_per_year': MIN_DAYS_PER_YEAR,
        },
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(OUTPUT_PATH)
    print(f'\nSaved climatology to {OUTPUT_PATH}')
    print(f'File size: {OUTPUT_PATH.stat().st_size / 1024 / 1024:.1f} MB')


if __name__ == '__main__':
    main()
