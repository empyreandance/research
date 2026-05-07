"""
Render apparent temperature anomaly maps from per-station data.
Interpolates station anomalies to a regular grid, clips to CONUS land,
contours and labels matching the temperature calendar style.
"""

import argparse
import datetime as dt
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.io.shapereader as shpreader
import matplotlib.pyplot as plt
import numpy as np
import shapely
import xarray as xr
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter
from shapely.ops import unary_union

DAILY_ANOMALY_DIR = Path('data/processed/daily_anomalies')
MAPS_DIR = Path('web/maps')
REGIONS_DIR = Path('web/maps/regions')

ANOMALY_RANGE = 25
CONUS_EXTENT = [-125, -66, 24, 50]

REGIONS = {
    'pacific_northwest': {'name': 'Pacific Northwest', 'extent': [-125, -116, 42, 49.5]},
    'northern_plains':   {'name': 'Northern Plains',   'extent': [-115, -96, 40, 49.5]},
    'upper_midwest':     {'name': 'Upper Midwest',     'extent': [-97, -80, 41, 49]},
    'northeast':         {'name': 'Northeast',         'extent': [-82, -66, 37, 47.5]},
    'west':              {'name': 'West',              'extent': [-125, -108, 31, 42.5]},
    'southern_plains':   {'name': 'Southern Plains',   'extent': [-107, -88, 25, 40]},
    'midwest':           {'name': 'Midwest',           'extent': [-95, -80, 35, 43]},
    'southeast':         {'name': 'Southeast',         'extent': [-92, -75, 24, 38]},
}

_conus_geom = None


def get_conus_geom():
    global _conus_geom
    if _conus_geom is not None:
        return _conus_geom
    shp = shpreader.natural_earth(resolution='50m', category='cultural',
                                   name='admin_1_states_provinces')
    recs = list(shpreader.Reader(shp).records())
    polys = [r.geometry for r in recs
             if r.attributes.get('admin') == 'United States of America'
             and r.attributes.get('name') not in ('Alaska', 'Hawaii')]
    _conus_geom = unary_union(polys)
    return _conus_geom


def conus_mask(lon2d, lat2d):
    geom = get_conus_geom()
    pts = shapely.points(lon2d.ravel(), lat2d.ravel())
    return shapely.contains(geom, pts).reshape(lon2d.shape)


def interp_grid(lons, lats, vals, extent, resolution=600):
    aspect = (extent[3] - extent[2]) / (extent[1] - extent[0])
    nx = resolution
    ny = max(50, int(resolution * aspect))
    grid_lon = np.linspace(extent[0], extent[1], nx)
    grid_lat = np.linspace(extent[2], extent[3], ny)
    glon, glat = np.meshgrid(grid_lon, grid_lat)
    pts = np.column_stack([lons, lats])
    cubic = griddata(pts, vals, (glon, glat), method='cubic')
    nearest = griddata(pts, vals, (glon, glat), method='nearest')
    merged = np.where(np.isnan(cubic), nearest, cubic)
    merged = gaussian_filter(merged, sigma=2.5)
    return glon, glat, merged


def render_map(target_date, extent=None, region_name=None, output_path=None,
               figsize=(14, 8.5)):
    nc_path = DAILY_ANOMALY_DIR / f'{target_date.isoformat()}.nc'
    ds = xr.open_dataset(nc_path)
    lats = ds.lat.values
    lons = ds.lon.values
    anom = ds.headline_anomaly.values

    if extent is None:
        extent = CONUS_EXTENT

    buf = 4
    sel = ((lons >= extent[0] - buf) & (lons <= extent[1] + buf) &
           (lats >= extent[2] - buf) & (lats <= extent[3] + buf))
    glon, glat, grid = interp_grid(lons[sel], lats[sel], anom[sel], extent)

    print('Applying CONUS land mask...')
    grid = np.where(conus_mask(glon, glat), grid, np.nan)

    proj = ccrs.AlbersEqualArea(
        central_longitude=(extent[0] + extent[1]) / 2,
        central_latitude=(extent[2] + extent[3]) / 2,
        standard_parallels=(29.5, 45.5),
    )
    fig = plt.figure(figsize=figsize, dpi=120)
    ax = plt.axes(projection=proj)
    ax.set_extent(extent, crs=ccrs.PlateCarree())

    ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor='#dde9f4', zorder=0)
    ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='#f7f6f1', zorder=0)

    levels = np.linspace(-ANOMALY_RANGE, ANOMALY_RANGE, 51)
    cf = ax.contourf(
        glon, glat, grid, levels=levels, cmap='RdBu_r',
        vmin=-ANOMALY_RANGE, vmax=ANOMALY_RANGE,
        transform=ccrs.PlateCarree(), extend='both', zorder=1,
    )

    line_levels = np.arange(-ANOMALY_RANGE, ANOMALY_RANGE + 0.01, 5)
    line_styles = ['dashed' if lvl < 0 else 'solid' for lvl in line_levels]
    cs = ax.contour(
        glon, glat, grid, levels=line_levels,
        colors='#222', linewidths=0.5, linestyles=line_styles,
        transform=ccrs.PlateCarree(), zorder=2,
    )
    ax.clabel(cs, fontsize=8, fmt='%+d', inline=True)

    ax.add_feature(cfeature.STATES.with_scale('50m'), edgecolor='#444',
                   linewidth=0.5, zorder=3)
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), edgecolor='#222',
                   linewidth=0.7, zorder=3)
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), edgecolor='#222',
                   linewidth=0.5, zorder=3)
    ax.spines['geo'].set_edgecolor('black')
    ax.spines['geo'].set_linewidth(1.5)

    main_title = f'Apparent Temperature Anomaly: {region_name}' if region_name else 'Apparent Temperature Anomaly'
    fig.text(0.5, 0.96, main_title, ha='center', fontsize=16, fontweight='bold')
    fig.text(0.5, 0.93, target_date.strftime('%A, %B %-d, %Y'),
             ha='center', fontsize=11, style='italic', color='#666')

    cbar = fig.colorbar(cf, ax=ax, orientation='horizontal', pad=0.04, shrink=0.7,
                        extend='both', ticks=np.arange(-25, 26, 5))
    cbar.set_label('Apparent temperature anomaly (\u00b0F)', fontsize=11, fontweight='bold')
    cbar.ax.tick_params(labelsize=10)

    fig.text(0.99, 0.01, 'Data: NWS NDFD | Climatology: 1995–2024 ASOS',
             ha='right', va='bottom', fontsize=8, color='#888')

    if output_path is None:
        MAPS_DIR.mkdir(parents=True, exist_ok=True)
        output_path = MAPS_DIR / f'{target_date.isoformat()}.png'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'Saved {output_path}')
    return output_path


def render_all(target_date):
    render_map(target_date)
    for key, region in REGIONS.items():
        out = REGIONS_DIR / key / f'{target_date.isoformat()}.png'
        render_map(target_date, extent=region['extent'],
                   region_name=region['name'], output_path=out, figsize=(10, 7))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', type=str, default=None)
    parser.add_argument('--region', type=str, default=None,
                        help=f'One of: {", ".join(REGIONS.keys())}, or "all"')
    args = parser.parse_args()

    if args.date:
        target_date = dt.date.fromisoformat(args.date)
    else:
        # find the latest available daily file
        files = sorted(DAILY_ANOMALY_DIR.glob('*.nc'))
        if not files:
            raise RuntimeError('No daily anomaly file found; run compute_daily_anomaly first')
        target_date = dt.date.fromisoformat(files[-1].stem)

    if args.region == 'all' or args.region is None:
        render_all(target_date)
    else:
        region = REGIONS[args.region]
        out = REGIONS_DIR / args.region / f'{target_date.isoformat()}.png'
        render_map(target_date, extent=region['extent'],
                   region_name=region['name'], output_path=out, figsize=(10, 7))


if __name__ == '__main__':
    main()
