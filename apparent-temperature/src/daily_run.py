"""
Daily run: download NDFD apt forecast, compute today's max/min, regrid to
climatology grid, compute apparent temperature anomaly per cell, render
matching the temperature calendar style.
"""

import argparse
import datetime as dt
import sys
import warnings
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib
matplotlib.use('Agg')
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import xarray as xr
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

GRIDDED_CLIM = Path('data/processed/climatology_gridded.nc')
CACHE_DIR = Path('data/raw/ndfd')
OUTPUT_DIR = Path('output')
REGIONS_OUTPUT_DIR = OUTPUT_DIR / 'regions'

LON_WEST, LON_EAST = -125.0, -66.0
LAT_SOUTH, LAT_NORTH = 24.0, 50.0

MAP_WIDTH_INCHES = 14
MAP_HEIGHT_INCHES = 8
MAP_DPI = 300

REGIONS = {
    'northwest': {'name': 'Pacific Northwest', 'extent': [-125, -116, 42, 49.5]},
    'northern_plains':   {'name': 'Northern Plains',   'extent': [-115, -96, 40, 49.5]},
    'upper_midwest':     {'name': 'Upper Midwest',     'extent': [-97, -80, 41, 49]},
    'northeast':         {'name': 'Northeast',         'extent': [-82, -66, 37, 47.5]},
    'west':              {'name': 'West',              'extent': [-125, -108, 31, 42.5]},
    'southern_plains':   {'name': 'Southern Plains',   'extent': [-107, -88, 25, 40]},
    'midwest':           {'name': 'Midwest',           'extent': [-95, -80, 35, 43]},
    'southeast':         {'name': 'Southeast',         'extent': [-92, -75, 24, 38]},
}

NDFD_URL = ('https://tgftp.nws.noaa.gov/SL.us008001/ST.opnl/DF.gr2/'
            'DC.ndfd/AR.conus/VP.001-003/ds.apt.bin')


def download_ndfd_apt():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output = CACHE_DIR / 'apt.grib2'
    print('Downloading NDFD apt forecast...')
    r = requests.get(NDFD_URL, timeout=180, stream=True)
    r.raise_for_status()
    with open(output, 'wb') as f:
        for chunk in r.iter_content(chunk_size=128 * 1024):
            f.write(chunk)
    print(f'  {output.stat().st_size / (1024 * 1024):.1f} MB')
    return output


def load_ndfd(grib_path):
    return xr.open_dataset(grib_path, engine='cfgrib',
                           backend_kwargs={'indexpath': '', 'errors': 'ignore'})


def find_first_forecast_day(ds, min_hours=8):
    valid = pd.to_datetime(ds.valid_time.values)
    counts = pd.Series(valid).dt.date.value_counts().sort_index()
    for d, c in counts.items():
        if c >= min_hours:
            return d
    return counts.index[0]


def daily_max_min(ds, target_date):
    apt_var = list(ds.data_vars)[0]
    start = np.datetime64(target_date.isoformat())
    end = np.datetime64((target_date + dt.timedelta(days=1)).isoformat())
    valid = ds.valid_time.values
    step_mask = (valid >= start) & (valid < end)
    if not step_mask.any():
        raise RuntimeError(f'No forecast hours within {target_date} UTC')
    print(f'Found {step_mask.sum()} forecast hours for {target_date}')

    apt = ds[apt_var].isel(step=step_mask).values
    sample = float(np.nanmedian(apt))
    if sample > 200:
        apt_f = (apt - 273.15) * 9 / 5 + 32
    elif -30 < sample < 60:
        apt_f = apt * 9 / 5 + 32
    else:
        apt_f = apt
    print(f'Forecast apt range: {np.nanmin(apt_f):.1f}-{np.nanmax(apt_f):.1f}F')
    return np.nanmax(apt_f, axis=0), np.nanmin(apt_f, axis=0)


def get_coords(ds):
    glon = ds.longitude.values
    glat = ds.latitude.values
    if glon.max() > 180:
        glon = np.where(glon > 180, glon - 360, glon)
    return glon, glat


def regrid(src_lon, src_lat, src_data, target_lons, target_lats):
    target_lon, target_lat = np.meshgrid(target_lons, target_lats)
    if src_lon.ndim == 1:
        src_lon, src_lat = np.meshgrid(src_lon, src_lat)
    pts = np.column_stack([src_lon.ravel(), src_lat.ravel()])
    vals = src_data.ravel()
    valid = ~np.isnan(vals)
    pts = pts[valid]
    vals = vals[valid]
    lin = griddata(pts, vals, (target_lon, target_lat), method='linear')
    nrst = griddata(pts, vals, (target_lon, target_lat), method='nearest')
    return np.where(np.isnan(lin), nrst, lin)


def build_cmap():
    return mcolors.LinearSegmentedColormap.from_list(
        'apt_anomaly',
        ['#08306b', '#2171b5', '#6baed6', '#c6dbef',
         '#f7f7f7',
         '#fcbba1', '#fb6a4a', '#cb181d', '#67000d'],
        N=256,
    )


def render_map(anomaly, clim_lons, clim_lats, run_date, output_path,
               region_name=None, region_extent=None, archive_path=None):
    is_regional = region_name is not None
    fig = plt.figure(figsize=(MAP_WIDTH_INCHES, MAP_HEIGHT_INCHES))
    proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=39,
                                  standard_parallels=(33, 45))
    ax = fig.add_axes([0.02, 0.08, 0.96, 0.78], projection=proj)

    if region_extent:
        w, e, s, n = region_extent
        ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    else:
        ax.set_extent([LON_WEST + 1, LON_EAST - 1, LAT_SOUTH + 1, LAT_NORTH - 1],
                      crs=ccrs.PlateCarree())

    ax.add_feature(cfeature.LAND, facecolor='#f5f5f5', zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor='#e6f0f7', zorder=0)
    ax.add_feature(cfeature.LAKES, facecolor='#e6f0f7', edgecolor='#cccccc',
                   linewidth=0.5, zorder=1)

    smoothed = anomaly.copy()
    nan_mask = np.isnan(smoothed)
    smoothed[nan_mask] = 0
    smoothed = gaussian_filter(smoothed, sigma=1.5)
    smoothed[nan_mask] = np.nan

    abs_max = max(int(np.ceil(np.nanmax(np.abs(anomaly)) / 5.0) * 5), 10)
    vmin, vmax = -abs_max, abs_max
    fill_levels = np.linspace(vmin, vmax, 91)
    line_interval = 5 if abs_max <= 30 else (10 if abs_max <= 60 else 15)
    line_levels = np.arange(vmin, vmax + 1, line_interval)
    tick_spacing = 5 if abs_max <= 30 else 10

    lon2d, lat2d = np.meshgrid(clim_lons, clim_lats)

    filled = ax.contourf(
        lon2d, lat2d, smoothed,
        levels=fill_levels, cmap=build_cmap(),
        vmin=vmin, vmax=vmax, transform=ccrs.PlateCarree(),
        zorder=2, extend='both',
    )
    lines = ax.contour(
        lon2d, lat2d, smoothed,
        levels=line_levels, colors='#444444',
        linewidths=0.6 if is_regional else 0.4,
        transform=ccrs.PlateCarree(), zorder=2.5,
    )
    ax.clabel(lines, inline=True,
              fontsize=9 if is_regional else 7,
              fmt='%+.0f', inline_spacing=5, colors='#333333')

    ax.add_feature(cfeature.STATES, edgecolor='#888888', linewidth=0.5, zorder=3)
    ax.add_feature(cfeature.BORDERS, edgecolor='#444444', linewidth=1.0, zorder=3)
    ax.add_feature(cfeature.COASTLINE, edgecolor='#666666', linewidth=0.7, zorder=3)

    cbar_ax = fig.add_axes([0.15, 0.06, 0.70, 0.025])
    cbar = fig.colorbar(filled, cax=cbar_ax, orientation='horizontal')
    cbar.set_ticks(np.arange(vmin, vmax + 1, tick_spacing))
    cbar.set_label('Apparent Temperature Anomaly (\u00b0F)',
                   fontsize=11, fontweight='bold', labelpad=8)
    cbar.ax.tick_params(labelsize=10)

    date_str = run_date.strftime('%A, %B %-d, %Y')
    if is_regional:
        title = f'Apparent Temperature Anomaly: {region_name}'
        subtitle = date_str
    else:
        title = f'Apparent Temperature Anomaly: {date_str}'
        subtitle = ('How does the forecast apparent temperature compare to the '
                    '1995\u20132024 normal for this date?')
    ax.set_title(title, fontsize=16, fontweight='bold', pad=32)
    fig.text(0.5, 0.88, subtitle, ha='center', fontsize=10, color='#555555',
             style='italic')

    fig.text(0.99, 0.01, 'Data: NWS NDFD / 1995\u20132024 ASOS Climatology',
             ha='right', fontsize=7, color='#999999')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=MAP_DPI, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    print(f'  Saved {output_path}')

    if archive_path is not None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(archive_path, dpi=150, bbox_inches='tight',
                    facecolor='white', edgecolor='none', format='jpg',
                    pil_kwargs={'quality': 85})
        print(f'  Saved {archive_path}')

    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', type=str, default=None)
    args = parser.parse_args()

    if not GRIDDED_CLIM.exists():
        print(f'ERROR: gridded climatology not found at {GRIDDED_CLIM}')
        print('Run: uv run python -m src.build_gridded_climatology')
        sys.exit(1)

    climo = xr.open_dataset(GRIDDED_CLIM)
    clim_lons = climo.longitude.values
    clim_lats = climo.latitude.values

    grib_path = CACHE_DIR / 'apt.grib2'
    if not grib_path.exists() or (
        dt.datetime.utcfromtimestamp(grib_path.stat().st_mtime).date()
        != dt.datetime.utcnow().date()
    ):
        download_ndfd_apt()

    ds = load_ndfd(grib_path)
    target_date = dt.date.fromisoformat(args.date) if args.date else find_first_forecast_day(ds)
    print(f'Target date: {target_date}')

    fmax_native, fmin_native = daily_max_min(ds, target_date)
    src_lon, src_lat = get_coords(ds)

    print('Regridding forecast to climatology grid...')
    fmax_grid = regrid(src_lon, src_lat, fmax_native, clim_lons, clim_lats)
    fmin_grid = regrid(src_lon, src_lat, fmin_native, clim_lons, clim_lats)

    doy = min(target_date.timetuple().tm_yday, 366)
    cmax = climo.normal_max_apt.sel(day_of_year=doy).values
    cmin = climo.normal_min_apt.sel(day_of_year=doy).values

    max_anom = fmax_grid - cmax
    min_anom = fmin_grid - cmin
    use_max = np.abs(np.nan_to_num(max_anom)) >= np.abs(np.nan_to_num(min_anom))
    headline = np.where(use_max, max_anom, min_anom)

    print(f'Anomaly: {np.nanmin(headline):.1f}F to {np.nanmax(headline):.1f}F '
          f'(mean {np.nanmean(headline):+.1f}F)')

    print('Rendering national map...')
    render_map(headline, clim_lons, clim_lats, target_date,
               OUTPUT_DIR / 'apt_anomaly_latest.png',
               archive_path=OUTPUT_DIR / 'archive' / f'apt_anomaly_{target_date.strftime("%Y%m%d")}.jpg')

    print(f'Rendering {len(REGIONS)} regional maps...')
    for key, region in REGIONS.items():
        render_map(
            headline, clim_lons, clim_lats, target_date,
            REGIONS_OUTPUT_DIR / f'apt_anomaly_{key}_latest.png',
            region_name=region['name'], region_extent=region['extent'],
        )


if __name__ == '__main__':
    main()
