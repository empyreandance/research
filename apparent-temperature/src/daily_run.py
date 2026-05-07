"""
Daily run: download NDFD apt forecast, compute today's max/min, regrid to
climatology grid, compute apparent temperature CALENDAR anomaly per cell
(days ahead/behind the climatological apparent-temperature schedule), and
render the result with MAX/MIN hatching for cells outside the climatological
envelope.
"""

import argparse
import datetime as dt
import sys
import warnings
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import xarray as xr
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter, label as ndi_label

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

GRIDDED_CLIM = Path("data/processed/climatology_gridded.nc")
CACHE_DIR = Path("data/raw/ndfd")
OUTPUT_DIR = Path("output")
REGIONS_OUTPUT_DIR = OUTPUT_DIR / "regions"

LON_WEST, LON_EAST = -125.0, -66.0
LAT_SOUTH, LAT_NORTH = 24.0, 50.0

MAP_WIDTH_INCHES = 14
MAP_HEIGHT_INCHES = 8
MAP_DPI = 300

REGIONS = {
    "northwest":         {"name": "Pacific Northwest", "extent": [-125, -110, 41, 49.5]},
    "west":              {"name": "West",              "extent": [-125, -110, 31, 42.5]},
    "northern_plains":   {"name": "Northern Plains",   "extent": [-110, -95, 41, 49.5]},
    "southern_plains":   {"name": "Southern Plains",   "extent": [-108, -93, 25.5, 40]},
    "upper_midwest":     {"name": "Upper Midwest",     "extent": [-97, -82, 41, 49.5]},
    "midwest":           {"name": "Midwest",           "extent": [-97, -82, 35, 42.5]},
    "southeast":         {"name": "Southeast",         "extent": [-93, -75, 24.5, 37]},
    "northeast":         {"name": "Northeast",         "extent": [-82, -66.5, 37, 47.5]},
}

NDFD_URL = ("https://tgftp.nws.noaa.gov/SL.us008001/ST.opnl/DF.gr2/"
            "DC.ndfd/AR.conus/VP.001-003/ds.apt.bin")


def download_ndfd_apt():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output = CACHE_DIR / "apt.grib2"
    print("Downloading NDFD apt forecast...")
    r = requests.get(NDFD_URL, timeout=180, stream=True)
    r.raise_for_status()
    with open(output, "wb") as f:
        for chunk in r.iter_content(chunk_size=128 * 1024):
            f.write(chunk)
    print(f"  {output.stat().st_size / (1024 * 1024):.1f} MB")
    return output


def load_ndfd(grib_path):
    return xr.open_dataset(grib_path, engine="cfgrib",
                           backend_kwargs={"indexpath": "", "errors": "ignore"})


def first_forecast_day(ds, min_hours=8):
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
        raise RuntimeError(f"No forecast hours within {target_date} UTC")
    print(f"Found {step_mask.sum()} forecast hours for {target_date}")

    apt = ds[apt_var].isel(step=step_mask).values
    sample = float(np.nanmedian(apt))
    if sample > 200:
        apt_f = (apt - 273.15) * 9 / 5 + 32
    elif -30 < sample < 60:
        apt_f = apt * 9 / 5 + 32
    else:
        apt_f = apt
    print(f"Forecast apt range: {np.nanmin(apt_f):.1f}-{np.nanmax(apt_f):.1f}F")
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
    lin = griddata(pts, vals, (target_lon, target_lat), method="linear")
    nrst = griddata(pts, vals, (target_lon, target_lat), method="nearest")
    return np.where(np.isnan(lin), nrst, lin)


def compute_day_anomaly(forecast_grid, clim_normals, today_doy):
    """For each cell, find days ahead/behind on the climatological apt schedule.
    Also return capped_max/capped_min booleans where the forecast exceeded the
    annual peak or fell below the annual trough of the climatology curve."""
    nlat, nlon = forecast_grid.shape
    anomaly = np.full((nlat, nlon), np.nan)
    capped_max = np.zeros((nlat, nlon), dtype=bool)
    capped_min = np.zeros((nlat, nlon), dtype=bool)
    n_days = clim_normals.shape[0]
    d0 = (today_doy - 1) % n_days

    def wrap(off):
        if off > n_days // 2:
            return off - n_days
        if off < -(n_days // 2):
            return off + n_days
        return off

    for i in range(nlat):
        for j in range(nlon):
            fcst = forecast_grid[i, j]
            if np.isnan(fcst):
                continue
            curve = clim_normals[:, i, j]
            if np.any(np.isnan(curve)):
                continue

            peak_day = int(np.argmax(curve))
            peak_v = curve[peak_day]
            trough_day = int(np.argmin(curve))
            trough_v = curve[trough_day]

            if fcst >= peak_v:
                anomaly[i, j] = wrap(peak_day - d0)
                capped_max[i, j] = True
                continue
            if fcst <= trough_v:
                anomaly[i, j] = wrap(trough_day - d0)
                capped_min[i, j] = True
                continue

            if trough_day < peak_day:
                ascending = (d0 >= trough_day) and (d0 <= peak_day)
            else:
                ascending = (d0 >= trough_day) or (d0 <= peak_day)

            today_normal = curve[d0]

            if fcst >= today_normal:
                if ascending:
                    if d0 <= peak_day:
                        search = list(range(d0, peak_day + 1))
                    else:
                        search = list(range(d0, n_days)) + list(range(0, peak_day + 1))
                else:
                    if peak_day <= d0:
                        search = list(range(d0, peak_day - 1, -1))
                    else:
                        search = list(range(d0, -1, -1)) + list(range(n_days - 1, peak_day - 1, -1))
            else:
                if ascending:
                    if trough_day <= d0:
                        search = list(range(d0, trough_day - 1, -1))
                    else:
                        search = list(range(d0, -1, -1)) + list(range(n_days - 1, trough_day - 1, -1))
                else:
                    if d0 <= trough_day:
                        search = list(range(d0, trough_day + 1))
                    else:
                        search = list(range(d0, n_days)) + list(range(0, trough_day + 1))

            matched = None
            for k in range(len(search) - 1):
                d1 = search[k]
                d2 = search[k + 1]
                v1 = curve[d1] - fcst
                v2 = curve[d2] - fcst
                if v1 == 0:
                    matched = float(d1)
                    break
                if v1 * v2 < 0:
                    frac = abs(v1) / (abs(v1) + abs(v2))
                    matched = d1 + frac * (1 if d2 > d1 else -1)
                    break
            if matched is None:
                matched = float(peak_day if fcst >= today_normal else trough_day)

            anomaly[i, j] = wrap(matched - d0)

    return anomaly, capped_max, capped_min


def build_cmap():
    return mcolors.LinearSegmentedColormap.from_list(
        "apt_calendar_anomaly",
        ["#08306b", "#2171b5", "#6baed6", "#c6dbef",
         "#f7f7f7",
         "#fcbba1", "#fb6a4a", "#cb181d", "#67000d"],
        N=256,
    )


def add_capped_layer(ax, capped, hatch, lon2d, lat2d):
    """Draw hatching where capped is True. Direction is conveyed by the hatch
    pattern; see the legend below the colorbar."""
    if not capped.any():
        return
    ax.contourf(
        lon2d, lat2d, capped.astype(float),
        levels=[0.5, 1.5], hatches=[hatch], colors="none",
        transform=ccrs.PlateCarree(), zorder=2.8,
    )


def render_map(anomaly, clim_lons, clim_lats, run_date, output_path,
               region_name=None, region_extent=None, archive_path=None,
               capped_max=None, capped_min=None):
    is_regional = region_name is not None
    fig = plt.figure(figsize=(MAP_WIDTH_INCHES, MAP_HEIGHT_INCHES))
    proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=39,
                                  standard_parallels=(33, 45))
    ax = fig.add_axes([0.02, 0.13, 0.96, 0.73], projection=proj)

    if region_extent:
        w, e, s, n = region_extent
        ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    else:
        ax.set_extent([LON_WEST + 1, LON_EAST - 1, LAT_SOUTH + 1, LAT_NORTH - 1],
                      crs=ccrs.PlateCarree())

    ax.add_feature(cfeature.LAND, facecolor="#f5f5f5", zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor="#e6f0f7", zorder=0)
    ax.add_feature(cfeature.LAKES, facecolor="#e6f0f7", edgecolor="#cccccc",
                   linewidth=0.5, zorder=1)

    smoothed = anomaly.copy()
    nan_mask = np.isnan(smoothed)
    smoothed[nan_mask] = 0
    smoothed = gaussian_filter(smoothed, sigma=1.5)
    smoothed[nan_mask] = np.nan

    abs_max = max(int(np.ceil(np.nanmax(np.abs(anomaly)) / 5.0) * 5), 20)
    vmin, vmax = -abs_max, abs_max
    fill_levels = np.linspace(vmin, vmax, 91)
    if abs_max <= 45:
        line_interval = 5
    elif abs_max <= 90:
        line_interval = 10
    else:
        line_interval = 15
    line_levels = np.arange(vmin, vmax + 1, line_interval)
    if abs_max <= 30: tick_spacing = 10
    elif abs_max <= 60: tick_spacing = 15
    elif abs_max <= 120: tick_spacing = 20
    else: tick_spacing = 30

    lon2d, lat2d = np.meshgrid(clim_lons, clim_lats)
    bbox_mask = (lat2d >= 24.5) & (lat2d <= 50.0) & (lon2d >= -125.0) & (lon2d <= -66.0)

    filled = ax.contourf(
        lon2d, lat2d, smoothed,
        levels=fill_levels, cmap=build_cmap(),
        vmin=vmin, vmax=vmax, transform=ccrs.PlateCarree(),
        zorder=2, extend="both",
    )
    lines = ax.contour(
        lon2d, lat2d, smoothed,
        levels=line_levels, colors="#444444",
        linewidths=0.6 if is_regional else 0.4,
        transform=ccrs.PlateCarree(), zorder=2.5,
    )
    ax.clabel(lines, inline=True,
              fontsize=9 if is_regional else 7,
              fmt="%+.0f", inline_spacing=5, colors="#333333")

    if capped_max is not None:
        add_capped_layer(ax, capped_max & bbox_mask, "///", lon2d, lat2d)
    if capped_min is not None:
        add_capped_layer(ax, capped_min & bbox_mask, "\\\\\\", lon2d, lat2d)

    ax.add_feature(cfeature.STATES, edgecolor="#888888", linewidth=0.5, zorder=3)
    ax.add_feature(cfeature.BORDERS, edgecolor="#444444", linewidth=1.0, zorder=3)
    ax.add_feature(cfeature.COASTLINE, edgecolor="#666666", linewidth=0.7, zorder=3)

    cbar_ax = fig.add_axes([0.15, 0.10, 0.70, 0.025])
    cbar = fig.colorbar(filled, cax=cbar_ax, orientation="horizontal")
    cbar.set_ticks(np.arange(vmin, vmax + 1, tick_spacing))
    cbar.set_label("Days Ahead (+) or Behind (\u2212) Normal Apparent Temperature Schedule",
                   fontsize=11, fontweight="bold", labelpad=8)
    cbar.ax.tick_params(labelsize=10)

    # Hatch legend below the colorbar label. Manual placement with rectangles
    # to avoid fig.legend's auto-layout snapping when the bottom margin is tight.
    from matplotlib.patches import Rectangle
    swatch_y = 0.025
    swatch_w = 0.024
    swatch_h = 0.014
    for sx, sh, lbl in [
        (0.290, "///",   "Above climatological max"),
        (0.555, "\\\\\\", "Below climatological min"),
    ]:
        fig.add_artist(Rectangle(
            (sx, swatch_y - swatch_h / 2), swatch_w, swatch_h,
            transform=fig.transFigure,
            facecolor="white", edgecolor="black", linewidth=0.5, hatch=sh,
        ))
        fig.text(
            sx + swatch_w + 0.005, swatch_y,
            lbl, fontsize=8, va="center", ha="left",
        )

    date_str = run_date.strftime("%A, %B %-d, %Y")
    if is_regional:
        title = f"Apparent Temperature Calendar Anomaly: {region_name}"
        subtitle = date_str
    else:
        title = f"Apparent Temperature Calendar Anomaly: {date_str}"
        subtitle = ("How many days ahead or behind is each location's forecast "
                    "apparent temperature compared to the normal schedule?")
    ax.set_title(title, fontsize=16, fontweight="bold", pad=32)
    fig.text(0.5, 0.88, subtitle, ha="center", fontsize=10, color="#555555",
             style="italic")

    fig.text(0.92, 0.025, "Data: NWS NDFD / 1995\u20132024 ASOS Climatology",
             ha="right", va="center", fontsize=7, color="#999999")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=MAP_DPI, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    print(f"  Saved {output_path}")

    if archive_path is not None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(archive_path, dpi=150, bbox_inches="tight",
                    facecolor="white", edgecolor="none", format="jpg",
                    pil_kwargs={"quality": 85})
        print(f"  Saved {archive_path}")

    plt.close(fig)



# Daily map QC: mask cells where today's forecast deviates from both the
# climatological normal and the neighbor median by more than the configured
# thresholds, in either direction. Catches coverage-gap artifacts; real cold
# fronts and heat waves pass because their neighbors are also extreme.
QC_NORMAL_THRESHOLD = 25.0
QC_MEDIAN_THRESHOLD = 15.0
QC_WINDOW_SIZE = 9  # 9 cells * 0.25 degree = +/- 1 degree neighborhood


def qc_forecast_cells(fmax_grid, fmin_grid, normal_max, normal_min):
    """Return (fail_mask, stats) where fail_mask is True at cells to mask."""
    from scipy.ndimage import generic_filter

    nbr_med_max = generic_filter(fmax_grid, np.nanmedian, size=QC_WINDOW_SIZE)
    nbr_med_min = generic_filter(fmin_grid, np.nanmedian, size=QC_WINDOW_SIZE)

    dev_norm_max = fmax_grid - normal_max
    dev_med_max  = fmax_grid - nbr_med_max
    dev_norm_min = fmin_grid - normal_min
    dev_med_min  = fmin_grid - nbr_med_min

    max_cold = (dev_norm_max < -QC_NORMAL_THRESHOLD) & (dev_med_max < -QC_MEDIAN_THRESHOLD)
    max_hot  = (dev_norm_max >  QC_NORMAL_THRESHOLD) & (dev_med_max >  QC_MEDIAN_THRESHOLD)
    min_cold = (dev_norm_min < -QC_NORMAL_THRESHOLD) & (dev_med_min < -QC_MEDIAN_THRESHOLD)
    min_hot  = (dev_norm_min >  QC_NORMAL_THRESHOLD) & (dev_med_min >  QC_MEDIAN_THRESHOLD)

    fail = max_cold | max_hot | min_cold | min_hot
    stats = {
        "max_cold": int(np.nansum(max_cold)),
        "max_hot": int(np.nansum(max_hot)),
        "min_cold": int(np.nansum(min_cold)),
        "min_hot": int(np.nansum(min_hot)),
        "total_failed": int(np.nansum(fail)),
        "total_valid": int(np.sum(~np.isnan(fmax_grid))),
    }
    return fail, stats



def fill_qc_masked_with_neighbor_median(arr, fail_mask, window_size=11):
    """Replace cells specified by fail_mask with the median of valid neighbors
    within a window. Leaves other NaN cells (off-CONUS) untouched."""
    from scipy.ndimage import generic_filter
    if not np.any(fail_mask):
        return arr
    nbr_median = generic_filter(arr, np.nanmedian, size=window_size)
    filled = arr.copy()
    filled[fail_mask] = nbr_median[fail_mask]
    return filled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()

    if not GRIDDED_CLIM.exists():
        print(f"ERROR: gridded climatology not found at {GRIDDED_CLIM}")
        sys.exit(1)

    climo = xr.open_dataset(GRIDDED_CLIM)
    clim_lons = climo.longitude.values
    clim_lats = climo.latitude.values
    clim_max_curve = climo.normal_max_apt.values
    clim_min_curve = climo.normal_min_apt.values

    grib_path = CACHE_DIR / "apt.grib2"
    if not grib_path.exists() or (
        dt.datetime.utcfromtimestamp(grib_path.stat().st_mtime).date()
        != dt.datetime.utcnow().date()
    ):
        download_ndfd_apt()

    ds = load_ndfd(grib_path)
    target_date = dt.date.fromisoformat(args.date) if args.date else first_forecast_day(ds)
    print(f"Target date: {target_date}")

    fmax_native, fmin_native = daily_max_min(ds, target_date)
    src_lon, src_lat = get_coords(ds)

    print("Regridding forecast to climatology grid...")
    fmax_grid = regrid(src_lon, src_lat, fmax_native, clim_lons, clim_lats)
    fmin_grid = regrid(src_lon, src_lat, fmin_native, clim_lons, clim_lats)

    doy = min(target_date.timetuple().tm_yday, clim_max_curve.shape[0])
    print("Computing calendar anomaly for max apt channel...")
    # Daily map QC: mask grid cells where today's forecast falls far from
    # both the climatological normal AND the neighbor median.
    today_norm_max = clim_max_curve[doy - 1]
    today_norm_min = clim_min_curve[doy - 1]
    fail_mask, qc_stats = qc_forecast_cells(fmax_grid, fmin_grid, today_norm_max, today_norm_min)
    print(f"QC masked {qc_stats['total_failed']:,} of {qc_stats['total_valid']:,} cells "
          f"(max_cold={qc_stats['max_cold']}, max_hot={qc_stats['max_hot']}, "
          f"min_cold={qc_stats['min_cold']}, min_hot={qc_stats['min_hot']})")
    fmax_grid = np.where(fail_mask, np.nan, fmax_grid)
    fmin_grid = np.where(fail_mask, np.nan, fmin_grid)

    max_anom, cap_hi_M, cap_lo_M = compute_day_anomaly(fmax_grid, clim_max_curve, doy)
    print("Computing calendar anomaly for min apt channel...")
    min_anom, cap_hi_m, cap_lo_m = compute_day_anomaly(fmin_grid, clim_min_curve, doy)

    # Fill QC-masked cells with neighbor median anomaly so contours flow
    # smoothly across them. Capped flags stay False at these cells, so no
    # hatching appears - just continuous color.
    max_anom = fill_qc_masked_with_neighbor_median(max_anom, fail_mask)
    min_anom = fill_qc_masked_with_neighbor_median(min_anom, fail_mask)

    capped_max = cap_hi_M | cap_hi_m
    capped_min = cap_lo_M | cap_lo_m

    use_max = np.abs(np.nan_to_num(max_anom)) >= np.abs(np.nan_to_num(min_anom))
    headline = np.where(use_max, max_anom, min_anom)

    print(f"Calendar anomaly: {np.nanmin(headline):.0f} to {np.nanmax(headline):+.0f} days "
          f"(mean {np.nanmean(headline):+.1f})")
    print(f"Capped MAX cells: {capped_max.sum()}, capped MIN cells: {capped_min.sum()}")

    print("Rendering national map...")
    render_map(headline, clim_lons, clim_lats, target_date,
               OUTPUT_DIR / "apt_anomaly_latest.png",
               archive_path=OUTPUT_DIR / "archive" / f"apt_anomaly_{target_date.strftime('%Y%m%d')}.jpg",
               capped_max=capped_max, capped_min=capped_min)

    print(f"Rendering {len(REGIONS)} regional maps...")
    for key, region in REGIONS.items():
        render_map(
            headline, clim_lons, clim_lats, target_date,
            REGIONS_OUTPUT_DIR / f"apt_anomaly_{key}_latest.png",
            region_name=region["name"], region_extent=region["extent"],
            capped_max=capped_max, capped_min=capped_min,
        )


if __name__ == "__main__":
    main()
