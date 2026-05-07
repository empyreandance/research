"""
One-time: interpolate per-station apparent temperature climatology to a
0.25-degree CONUS grid with CONUS land mask.
Output mirrors the structure of an ACIS-style gridded climatology so daily_run
can use NaN propagation for natural CONUS clipping.
"""

from pathlib import Path

import cartopy.io.shapereader as shpreader
import numpy as np
import shapely
import xarray as xr
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter
from shapely.ops import unary_union

LON_WEST, LON_EAST = -125.0, -66.0
LAT_SOUTH, LAT_NORTH = 24.0, 50.0
GRID_SPACING = 0.25

PER_STATION = Path('data/processed/climatology.nc')
OUTPUT = Path('data/processed/climatology_gridded.nc')


def get_conus_geom():
    shp = shpreader.natural_earth(resolution='50m', category='cultural',
                                   name='admin_1_states_provinces')
    polys = [r.geometry for r in shpreader.Reader(shp).records()
             if r.attributes.get('admin') == 'United States of America'
             and r.attributes.get('name') not in ('Alaska', 'Hawaii')]
    return unary_union(polys)


def main():
    print('Loading per-station climatology...')
    climo = xr.open_dataset(PER_STATION)
    sta_lons = climo.lon.values
    sta_lats = climo.lat.values

    grid_lons = np.arange(LON_WEST, LON_EAST + GRID_SPACING / 2, GRID_SPACING)
    grid_lats = np.arange(LAT_SOUTH, LAT_NORTH + GRID_SPACING / 2, GRID_SPACING)
    target_lon, target_lat = np.meshgrid(grid_lons, grid_lats)
    print(f'Target grid: {target_lon.shape}')

    print('Building CONUS land mask...')
    geom = get_conus_geom()
    pts = shapely.points(target_lon.ravel(), target_lat.ravel())
    mask = shapely.contains(geom, pts).reshape(target_lon.shape)
    print(f'CONUS cells: {mask.sum()}/{mask.size}')

    n_doys = 366
    nlat, nlon = target_lon.shape
    gmax_all = np.full((n_doys, nlat, nlon), np.nan, dtype=np.float32)
    gmin_all = np.full((n_doys, nlat, nlon), np.nan, dtype=np.float32)

    print('Interpolating climatology for each day-of-year...')
    for doy in range(1, n_doys + 1):
        max_means = climo.max_mean.sel(doy=doy).values
        min_means = climo.min_mean.sel(doy=doy).values
        valid = ~np.isnan(max_means) & ~np.isnan(min_means)
        if valid.sum() < 100:
            continue
        sta_pts = np.column_stack([sta_lons[valid], sta_lats[valid]])

        def fill(values):
            lin = griddata(sta_pts, values, (target_lon, target_lat), method='linear')
            nrst = griddata(sta_pts, values, (target_lon, target_lat), method='nearest')
            merged = np.where(np.isnan(lin), nrst, lin)
            return gaussian_filter(merged, sigma=1.0)

        gmax = fill(max_means[valid])
        gmin = fill(min_means[valid])
        gmax[~mask] = np.nan
        gmin[~mask] = np.nan
        gmax_all[doy - 1] = gmax
        gmin_all[doy - 1] = gmin

        if doy % 50 == 0:
            print(f'  DOY {doy}: {valid.sum()} stations, '
                  f'max range {np.nanmin(gmax):.1f}-{np.nanmax(gmax):.1f}F, '
                  f'min range {np.nanmin(gmin):.1f}-{np.nanmax(gmin):.1f}F')

    out = xr.Dataset(
        {
            'normal_max_apt': (('day_of_year', 'latitude', 'longitude'), gmax_all),
            'normal_min_apt': (('day_of_year', 'latitude', 'longitude'), gmin_all),
        },
        coords={
            'day_of_year': np.arange(1, n_doys + 1),
            'latitude': grid_lats,
            'longitude': grid_lons,
        },
        attrs={
            'description': 'Gridded apparent temperature climatology, CONUS-masked',
            'baseline': '1995-2024 ASOS-derived per-station, interpolated to 0.25-degree grid',
            'grid_spacing_degrees': GRID_SPACING,
        }
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_netcdf(OUTPUT)
    print(f'\nSaved {OUTPUT} ({OUTPUT.stat().st_size // 1024 // 1024} MB)')


if __name__ == '__main__':
    main()
