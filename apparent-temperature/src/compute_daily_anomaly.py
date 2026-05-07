"""
Compute daily apparent temperature anomaly from NDFD forecast.

Pulls NDFD apparent temperature forecast (gridded), finds today's max and min
across the day's forecast hours, interpolates per-station climatology to the
NDFD grid, computes the gridded anomaly, saves to NetCDF.
"""

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xarray as xr
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

CLIMATOLOGY_PATH = Path('data/processed/climatology.nc')
DAILY_ANOMALY_DIR = Path('data/processed/daily_anomalies')
NDFD_DIR = Path('data/raw/ndfd')

NDFD_APT_URL = (
    'https://tgftp.nws.noaa.gov/SL.us008001/ST.opnl/DF.gr2/DC.ndfd/'
    'AR.conus/VP.001-003/ds.apt.bin'
)

HEADERS = {'User-Agent': 'apparent-temp-anomaly-research/0.1'}


def download_ndfd_apt():
    NDFD_DIR.mkdir(parents=True, exist_ok=True)
    output = NDFD_DIR / 'apt.grib2'
    print(f'Downloading NDFD apt forecast...')
    r = requests.get(NDFD_APT_URL, headers=HEADERS, timeout=180, stream=True)
    r.raise_for_status()
    with open(output, 'wb') as f:
        for chunk in r.iter_content(chunk_size=128 * 1024):
            f.write(chunk)
    print(f'Saved {output} ({output.stat().st_size // 1024} KB)')
    return output


def load_ndfd(grib_path):
    """Open NDFD GRIB, return xarray Dataset."""
    ds = xr.open_dataset(
        grib_path, engine='cfgrib',
        backend_kwargs={'indexpath': '', 'errors': 'ignore'},
    )
    print(f'NDFD dataset:')
    print(ds)
    print(f'\nVariables: {list(ds.data_vars)}')
    print(f'Dimensions: {dict(ds.dims)}')
    return ds


def filter_to_target_day(ds, target_date):
    """Keep only forecast steps with valid_time in target_date (UTC)."""
    start = np.datetime64(target_date.isoformat())
    end = np.datetime64((target_date + dt.timedelta(days=1)).isoformat())

    valid = ds.valid_time.values
    mask = (valid >= start) & (valid < end)
    if not mask.any():
        return None
    return ds.isel(step=mask)


def to_fahrenheit(arr, source_units):
    """Convert temperature array to Fahrenheit. NDFD usually delivers K."""
    if source_units in ('K', 'kelvin'):
        return (arr - 273.15) * 9.0 / 5.0 + 32.0
    if source_units in ('C', 'celsius', 'degC'):
        return arr * 9.0 / 5.0 + 32.0
    if source_units in ('F', 'degF'):
        return arr
    print(f'WARNING: unknown units {source_units!r}, assuming Kelvin')
    return (arr - 273.15) * 9.0 / 5.0 + 32.0


def interpolate_climo_to_grid(climo, target_date, grid_lon, grid_lat):
    """Interpolate per-station max_mean and min_mean for target day-of-year to a grid."""
    doy = target_date.timetuple().tm_yday

    lons = climo.lon.values
    lats = climo.lat.values
    max_means = climo.max_mean.sel(doy=doy).values
    min_means = climo.min_mean.sel(doy=doy).values

    valid = ~np.isnan(max_means) & ~np.isnan(min_means)
    pts = np.column_stack([lons[valid], lats[valid]])

    def smooth_interp(values):
        cubic = griddata(pts, values, (grid_lon, grid_lat), method='cubic')
        nearest = griddata(pts, values, (grid_lon, grid_lat), method='nearest')
        merged = np.where(np.isnan(cubic), nearest, cubic)
        return gaussian_filter(merged, sigma=2.5)

    return smooth_interp(max_means[valid]), smooth_interp(min_means[valid])



def find_first_forecast_day(ds, min_hours=8):
    """Return the first UTC date with at least min_hours of forecast coverage."""
    valid_times = pd.to_datetime(ds.valid_time.values)
    dates = pd.Series(valid_times).dt.date
    counts = dates.value_counts().sort_index()
    for d, c in counts.items():
        if c >= min_hours:
            return d
    return counts.index[0]



def find_first_forecast_day(ds, min_hours=8):
    """Return the first UTC date with at least min_hours of forecast coverage."""
    valid_times = pd.to_datetime(ds.valid_time.values)
    dates = pd.Series(valid_times).dt.date
    counts = dates.value_counts().sort_index()
    for d, c in counts.items():
        if c >= min_hours:
            return d
    return counts.index[0]


def compute_anomaly(target_date):
    grib_path = NDFD_DIR / 'apt.grib2'
    if not grib_path.exists() or (
        dt.datetime.utcfromtimestamp(grib_path.stat().st_mtime).date() != dt.datetime.utcnow().date()
    ):
        download_ndfd_apt()

    ds = load_ndfd(grib_path)

    if target_date is None:
        target_date = find_first_forecast_day(ds)
        print(f"\nUsing forecast day: {target_date}")

    if target_date is None:
        target_date = find_first_forecast_day(ds)
        print(f"\nUsing forecast day: {target_date}")

    # NDFD apparent temperature variable. cfgrib often calls it 't2m', 'apt', or 'app'.
    apt_var = None
    for candidate in ['apt', 'app', 't2m', 'AT', 'temperature']:
        if candidate in ds.data_vars:
            apt_var = candidate
            break
    if apt_var is None:
        apt_var = list(ds.data_vars)[0]
        print(f'WARNING: using variable {apt_var!r} as apt; verify this is correct.')

    apt = ds[apt_var]
    units = apt.attrs.get('units', 'K')

    day_ds = filter_to_target_day(ds, target_date)
    if day_ds is None:
        raise RuntimeError(
            f'No NDFD forecast hours within {target_date} UTC. '
            f'Data range: {ds.valid_time.values.min()} to {ds.valid_time.values.max()}'
        )
    print(f'\n{day_ds.dims.get("step", 0)} forecast hours for {target_date}')

    apt_day = day_ds[apt_var]
    forecast_max = to_fahrenheit(apt_day.max(dim='step').values, units)
    forecast_min = to_fahrenheit(apt_day.min(dim='step').values, units)

    # Get grid coords. NDFD uses 2D lat/lon (Lambert Conformal projection)
    if 'longitude' in ds.coords and ds.longitude.ndim == 2:
        glon = ds.longitude.values
        glat = ds.latitude.values
    else:
        glon, glat = np.meshgrid(ds.longitude.values, ds.latitude.values)

    # Convert NDFD longitudes from 0-360 to -180-180 if needed
    if glon.max() > 180:
        glon = np.where(glon > 180, glon - 360, glon)

    print(f'Grid shape: {glon.shape}, lon range {glon.min():.1f} to {glon.max():.1f}, '
          f'lat range {glat.min():.1f} to {glat.max():.1f}')

    print('\nInterpolating climatology to NDFD grid...')
    climo = xr.open_dataset(CLIMATOLOGY_PATH)
    climo_max, climo_min = interpolate_climo_to_grid(climo, target_date, glon, glat)

    max_anomaly = forecast_max - climo_max
    min_anomaly = forecast_min - climo_min
    use_max = np.abs(max_anomaly) >= np.abs(min_anomaly)
    headline_anomaly = np.where(use_max, max_anomaly, min_anomaly)

    out = xr.Dataset(
        data_vars={
            'forecast_max': (('y', 'x'), forecast_max.astype(np.float32)),
            'forecast_min': (('y', 'x'), forecast_min.astype(np.float32)),
            'climo_max': (('y', 'x'), climo_max.astype(np.float32)),
            'climo_min': (('y', 'x'), climo_min.astype(np.float32)),
            'max_anomaly': (('y', 'x'), max_anomaly.astype(np.float32)),
            'min_anomaly': (('y', 'x'), min_anomaly.astype(np.float32)),
            'headline_anomaly': (('y', 'x'), headline_anomaly.astype(np.float32)),
            'lon': (('y', 'x'), glon.astype(np.float32)),
            'lat': (('y', 'x'), glat.astype(np.float32)),
        },
        attrs={
            'date': target_date.isoformat(),
            'description': 'Gridded apparent temperature anomaly: NDFD forecast vs 1995-2024 station climatology',
            'baseline': '1995-2024 ASOS-derived per-station climatology, interpolated to NDFD grid',
        }
    )

    DAILY_ANOMALY_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DAILY_ANOMALY_DIR / f'{target_date.isoformat()}.nc'
    out.to_netcdf(output_path)
    print(f'\nSaved {output_path}')
    print(f'Headline anomaly range: {np.nanmin(headline_anomaly):.1f}F to {np.nanmax(headline_anomaly):.1f}F')
    return output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', type=str, default=None)
    args = parser.parse_args()

    if args.date:
        target_date = dt.date.fromisoformat(args.date)
    else:
        target_date = None

    compute_anomaly(target_date)


if __name__ == '__main__':
    main()
