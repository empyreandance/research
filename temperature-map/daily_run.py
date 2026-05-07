#!/usr/bin/env python3
"""
daily_run.py — Download today's forecast, compute "days ahead/behind," render the map.

This script is the daily workhorse of the Temperature Calendar Map project.
It runs once per day (either manually or via GitHub Actions) and produces a
single PNG image showing how far ahead or behind the normal temperature
schedule each part of the US is.

Usage:
    python daily_run.py              # Normal run: download forecast, make map
    python daily_run.py --test       # Use fake data to test map rendering
    python daily_run.py --date 2026-07-04  # Pretend today is July 4 (uses current forecast)

What it does:
    1. Downloads the NDFD (National Digital Forecast Database) maximum
       temperature forecast for CONUS as a GRIB2 file
    2. Regrids it to match the climatology grid (0.25°)
    3. For each grid cell, finds what calendar date has that temperature
       as its normal high → computes the offset in days from today
    4. Renders a color-coded map and saves it as a PNG
"""

import os
import sys
import argparse
import warnings
from datetime import datetime, date, timedelta

import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")  # Use non-interactive backend (no GUI window needed)
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.patheffects as pe
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import requests

from config import (
    CLIMATOLOGY_FILE, OUTPUT_DIR, CACHE_DIR,
    LON_WEST, LON_EAST, LAT_SOUTH, LAT_NORTH, GRID_SPACING,
    ANOMALY_MAX_DAYS, MAP_WIDTH_INCHES, MAP_HEIGHT_INCHES, MAP_DPI,
    REGIONS
)

# Suppress some noisy warnings from cartopy and cfgrib
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# =============================================================================
# STEP 1: DOWNLOAD THE NDFD FORECAST
# =============================================================================

def download_ndfd_maxt(cache_dir):
    """
    Download the NDFD maximum temperature forecast for CONUS.

    The NDFD is the National Digital Forecast Database — it's the gridded
    version of what you see on weather.gov. NOAA publishes it as GRIB2 files
    on their bulk data servers. We grab the "MaxT" (maximum temperature)
    field, which covers the next several days.

    We want the forecast for TODAY, which is typically the first time step
    in the MaxT file (the forecast for "today's high").

    Parameters
    ----------
    cache_dir : str
        Directory to save the downloaded GRIB2 file

    Returns
    -------
    str
        Path to the downloaded GRIB2 file, or None if download failed
    """
    os.makedirs(cache_dir, exist_ok=True)

    # The NDFD CONUS MaxT GRIB2 file URL.
    # This is the "VP.001-003" operational forecast for days 1-3.
    # The file contains multiple time steps; we'll extract the first one.
    url = ("https://tgftp.nws.noaa.gov/SL.us008001/ST.opnl/DF.gr2/"
           "DC.ndfd/AR.conus/VP.001-003/ds.maxt.bin")

    output_path = os.path.join(cache_dir, "ndfd_maxt.grib2")

    print("Downloading NDFD MaxT forecast...")
    print(f"  URL: {url}")

    try:
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()

        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  Downloaded: {size_mb:.1f} MB → {output_path}")
        return output_path

    except requests.exceptions.RequestException as e:
        print(f"  ERROR downloading NDFD: {e}")
        print("  The NOAA server may be temporarily unavailable.")
        print("  This happens occasionally — try again in a few minutes.")
        return None


def load_ndfd_forecast(grib_path):
    """
    Open the NDFD GRIB2 file and extract the first MaxT time step.

    The GRIB2 file contains forecasts for multiple days. We want just
    the first time step, which is today's forecast high temperature.

    The NDFD uses Fahrenheit natively for the US, so no unit conversion
    is needed — our climatology is also in Fahrenheit.

    Parameters
    ----------
    grib_path : str
        Path to the downloaded GRIB2 file

    Returns
    -------
    tuple of (numpy.ndarray, numpy.ndarray, numpy.ndarray)
        (lons_2d, lats_2d, temperatures) — all 2D arrays on the NDFD native grid
    """
    import cfgrib

    print("Reading NDFD GRIB2 file...")

    # Open the GRIB2 file. cfgrib returns an xarray Dataset.
    # The NDFD may have multiple "messages" (time steps) in one file.
    # We open them all and grab the first valid time.
    try:
        ds = xr.open_dataset(
            grib_path,
            engine="cfgrib",
            backend_kwargs={"indexpath": ""}  # Don't create index files
        )
    except Exception as e:
        print(f"  Error opening GRIB2 with default settings: {e}")
        print("  Trying alternative approach...")
        ds = xr.open_dataset(
            grib_path,
            engine="cfgrib",
            backend_kwargs={
                "indexpath": "",
                "errors": "ignore"
            }
        )

    # --- DIAGNOSTICS: Print everything so we can see what cfgrib gave us ---
    print(f"\n  === GRIB2 DIAGNOSTICS ===")
    print(f"  Data variables: {list(ds.data_vars)}")
    print(f"  Coordinates: {list(ds.coords)}")
    print(f"  Dimensions: {dict(ds.dims)}")
    for coord_name in ds.coords:
        c = ds.coords[coord_name]
        if c.size > 1:
            print(f"  Coord '{coord_name}': shape={c.shape}, "
                  f"min={float(c.min()):.4f}, max={float(c.max()):.4f}, "
                  f"dtype={c.dtype}")
        else:
            print(f"  Coord '{coord_name}': scalar value={float(c):.4f}")
    print(f"  =========================\n")

    # Find the temperature variable
    temp_var = None
    for var_name in ["tmax", "t2m", "unknown", "maxt"]:
        if var_name in ds.data_vars:
            temp_var = var_name
            break

    if temp_var is None:
        temp_var = list(ds.data_vars)[0]
        print(f"  Note: Using variable '{temp_var}' (couldn't find expected name)")

    data = ds[temp_var]
    print(f"  Variable '{temp_var}' dims: {data.dims}, shape: {data.shape}")

    # If there are multiple time steps, take the first one
    if "time" in data.dims:
        data = data.isel(time=0)
    elif "step" in data.dims:
        data = data.isel(step=0)
    elif "valid_time" in data.dims:
        data = data.isel(valid_time=0)

    temps = data.values

    # --- EXTRACT COORDINATES ---
    # NDFD GRIB2 files are typically on a Lambert Conformal grid.
    # cfgrib usually provides 2D latitude and longitude arrays as coordinates,
    # even though the grid dimensions are named x/y or something else.
    # We need to find the GEOGRAPHIC (degree) coordinates, not projected ones.

    lats = None
    lons = None

    # Check for 2D geographic coordinate arrays (most common for NDFD)
    if "latitude" in ds.coords and "longitude" in ds.coords:
        lats = ds.coords["latitude"].values
        lons = ds.coords["longitude"].values
    elif "lat" in ds.coords and "lon" in ds.coords:
        lats = ds.coords["lat"].values
        lons = ds.coords["lon"].values

    # If we found geographic coords, check their shape and range
    if lats is not None:
        print(f"  Lat array: shape={lats.shape}, range=[{np.nanmin(lats):.2f}, {np.nanmax(lats):.2f}]")
        print(f"  Lon array: shape={lons.shape}, range=[{np.nanmin(lons):.2f}, {np.nanmax(lons):.2f}]")

        # Convert longitude from 0-360 to -180-180 if needed
        if np.nanmax(lons) > 180:
            print(f"  Converting longitude from 0-360 to -180/180 range")
            lons = np.where(lons > 180, lons - 360, lons)
            print(f"  Lon after conversion: range=[{np.nanmin(lons):.2f}, {np.nanmax(lons):.2f}]")

        # Check for projected coordinates disguised as lat/lon
        # (values in meters instead of degrees)
        if np.nanmax(np.abs(lats)) > 90 or np.nanmax(np.abs(lons)) > 360:
            print(f"  WARNING: Coordinates look like projected (meters), not geographic (degrees).")
            print(f"  Falling back to x/y coordinate reconstruction.")
            lats = None  # Force fallback below

    # Fallback: if no geographic coords found, try x/y
    if lats is None:
        if "y" in ds.coords and "x" in ds.coords:
            x = ds.coords["x"].values
            y = ds.coords["y"].values
            print(f"  Using x/y coordinates: x range=[{x.min():.2f}, {x.max():.2f}], "
                  f"y range=[{y.min():.2f}, {y.max():.2f}]")

            # These are projected coordinates — we need to convert to lat/lon.
            # But this is complex and depends on the projection parameters.
            # Instead, let's try to get lat/lon from cfgrib differently.
            raise ValueError(
                f"GRIB file has only projected (x/y) coordinates, not geographic (lat/lon). "
                f"Available coords: {list(ds.coords)}. "
                f"Try: pip install eccodes; or check if the GRIB file is valid."
            )
        else:
            raise ValueError(f"Cannot find lat/lon coordinates. Available: {list(ds.coords)}")

    # --- VERIFY DATA-COORDINATE ALIGNMENT ---
    # The data array and lat/lon arrays must have matching shapes.
    # If lat/lon are 2D, they should match the data shape exactly.
    # If lat/lon are 1D, they define the grid axes.
    print(f"  Data shape: {temps.shape}")
    print(f"  Lat shape:  {lats.shape}")
    print(f"  Lon shape:  {lons.shape}")

    if lats.ndim == 2 and temps.ndim == 2:
        if lats.shape != temps.shape:
            print(f"  WARNING: Shape mismatch — lat {lats.shape} vs data {temps.shape}")
            print(f"  Attempting transpose...")
            if lats.shape == temps.shape[::-1]:
                temps = temps.T
                print(f"  Transposed data to {temps.shape}")

    # --- SANITY CHECK: spot-check coordinate-data alignment ---
    # The warmest cell should be in the southern half of the grid.
    # If it's in the northern half, something is flipped.
    if lats.ndim == 2:
        valid_temps = np.where(np.isnan(temps), -999, temps)
        warmest_idx = np.unravel_index(np.argmax(valid_temps), temps.shape)
        warmest_lat = lats[warmest_idx]
        warmest_lon = lons[warmest_idx]
        warmest_temp = temps[warmest_idx]

        coldest_temps = np.where(np.isnan(temps), 999, temps)
        coldest_idx = np.unravel_index(np.argmin(coldest_temps), temps.shape)
        coldest_lat = lats[coldest_idx]
        coldest_temp = temps[coldest_idx]

        print(f"\n  Warmest cell: {warmest_temp:.1f} at lat={warmest_lat:.2f}, lon={warmest_lon:.2f}")
        print(f"  Coldest cell: {coldest_temp:.1f} at lat={coldest_lat:.2f}")
        print(f"  (Warmest should be in southern US, coldest in northern US)")

    # NDFD temps may be in Kelvin — check and convert if needed
    sample_mean = np.nanmean(temps)

    if sample_mean > 200:
        print(f"  Converting from Kelvin (sample mean: {sample_mean:.1f}K)")
        temps = (temps - 273.15) * 9 / 5 + 32
    elif sample_mean < 60 and sample_mean > -30:
        print(f"  Converting from Celsius (sample mean: {sample_mean:.1f}°C)")
        temps = temps * 9 / 5 + 32
    else:
        print(f"  Temperatures appear to be in Fahrenheit (sample mean: {sample_mean:.1f}°F)")

    print(f"  Final temp range: {np.nanmin(temps):.0f}°F to {np.nanmax(temps):.0f}°F")

    ds.close()
    return lons, lats, temps


# =============================================================================
# STEP 2: REGRID FORECAST TO MATCH CLIMATOLOGY
# =============================================================================

def regrid_to_climatology(fcst_lons, fcst_lats, fcst_temps, clim_lons, clim_lats):
    """
    Regrid the high-resolution NDFD forecast to match the climatology grid.

    The NDFD grid is ~2.5 km resolution and the climatology is 0.25° (~28 km).
    We use scipy's griddata to interpolate the forecast onto the coarser grid.
    This is necessary because we need the forecast and climatology to be on
    the same grid to do cell-by-cell comparisons.

    Parameters
    ----------
    fcst_lons, fcst_lats : numpy.ndarray
        NDFD grid coordinates (may be 1D or 2D)
    fcst_temps : numpy.ndarray
        NDFD forecast temperatures (2D)
    clim_lons, clim_lats : numpy.ndarray
        1D arrays defining the climatology grid

    Returns
    -------
    numpy.ndarray
        Forecast temperatures on the climatology grid, shape (len(clim_lats), len(clim_lons))
    """
    from scipy.interpolate import griddata

    print("Regridding forecast to climatology grid...")

    # Create the target grid (2D mesh)
    target_lon, target_lat = np.meshgrid(clim_lons, clim_lats)

    # The NDFD coordinates might be 1D or 2D depending on the projection.
    # We need them as matching 1D arrays of (lon, lat) pairs.
    if fcst_lons.ndim == 1 and fcst_lats.ndim == 1:
        # Regular grid — create mesh then flatten
        flon2d, flat2d = np.meshgrid(fcst_lons, fcst_lats)
    elif fcst_lons.ndim == 2 and fcst_lats.ndim == 2:
        flon2d, flat2d = fcst_lons, fcst_lats
    else:
        # Mixed dimensions — try to make it work
        flon2d = np.broadcast_to(fcst_lons, fcst_temps.shape)
        flat2d = np.broadcast_to(fcst_lats, fcst_temps.shape)

    # Flatten everything for griddata
    source_points = np.column_stack([flon2d.ravel(), flat2d.ravel()])
    source_values = fcst_temps.ravel()

    # Remove NaN values (ocean/border cells in NDFD)
    valid = ~np.isnan(source_values)
    source_points = source_points[valid]
    source_values = source_values[valid]

    # Interpolate
    regridded = griddata(
        source_points,
        source_values,
        (target_lon, target_lat),
        method="linear"
    )

    # Fill coastal NaN cells with nearest-neighbor
    nan_mask = np.isnan(regridded)
    if np.any(nan_mask):
        nearest = griddata(
            source_points, source_values,
            (target_lon, target_lat),
            method="nearest"
        )
        regridded[nan_mask] = nearest[nan_mask]

    valid_pct = 100 * np.sum(~np.isnan(regridded)) / regridded.size
    print(f"  Regridded to {regridded.shape[0]}×{regridded.shape[1]}, "
          f"{valid_pct:.0f}% valid cells")

    # Sanity check: are the warmest cells in the southern part of the grid?
    valid_regridded = np.where(np.isnan(regridded), -999, regridded)
    warmest_idx = np.unravel_index(np.argmax(valid_regridded), regridded.shape)
    warmest_lat = clim_lats[warmest_idx[0]]
    warmest_lon = clim_lons[warmest_idx[1]]
    warmest_val = regridded[warmest_idx]
    print(f"  Regridded warmest cell: {warmest_val:.1f}°F at "
          f"lat={warmest_lat:.2f}, lon={warmest_lon:.2f}")
    print(f"  (Should be in southern US for a typical March day)")

    return regridded


# =============================================================================
# STEP 3: COMPUTE THE DAY-OFFSET ANOMALY
# =============================================================================

def compute_day_anomaly(forecast_grid, clim_normals, today_doy):
    """
    For each grid cell, find how many days ahead or behind schedule the
    forecast temperature is.

    This is the core algorithm. For each cell:
      1. Look at the 365-day climatological normal curve
      2. Determine if today is on the ascending (warming) or descending
         (cooling) branch of the annual cycle
      3. Find the date(s) on the SAME branch where the normal matches
         the forecast temperature
      4. Compute the offset: matched_date - today

    Parameters
    ----------
    forecast_grid : numpy.ndarray
        2D array of forecast high temperatures (°F), shape (nlat, nlon)
    clim_normals : numpy.ndarray
        3D array of climatological normals, shape (365, nlat, nlon)
    today_doy : int
        Day of year for today (1-365)

    Returns
    -------
    numpy.ndarray
        2D array of day-offset anomalies, same shape as forecast_grid.
        Positive = ahead of schedule (warmer), negative = behind (cooler).
    """
    print(f"Computing day-offset anomalies for day-of-year {today_doy}...")

    nlat, nlon = forecast_grid.shape
    anomaly = np.full((nlat, nlon), np.nan)
    capped_max = np.zeros((nlat, nlon), dtype=bool)  # Warmer than any normal day
    capped_min = np.zeros((nlat, nlon), dtype=bool)  # Colder than any normal day

    # Today's index (0-based)
    d0 = today_doy - 1  # Convert 1-based DOY to 0-based index

    for i in range(nlat):
        for j in range(nlon):
            fcst_t = forecast_grid[i, j]

            # Skip NaN cells (ocean, outside CONUS)
            if np.isnan(fcst_t):
                continue

            # Get the 365-day normal curve for this cell
            curve = clim_normals[:, i, j]

            if np.any(np.isnan(curve)):
                continue

            # Find the annual peak (warmest normal day)
            peak_day = np.argmax(curve)
            peak_temp = curve[peak_day]

            # Find the annual trough (coldest normal day)
            trough_day = np.argmin(curve)
            trough_temp = curve[trough_day]

            # If forecast exceeds the annual max normal, cap the anomaly
            if fcst_t >= peak_temp:
                # Temperature is warmer than any normal day — use peak offset
                offset = peak_day - d0
                # Handle year wraparound
                if offset > 182:
                    offset -= 365
                elif offset < -182:
                    offset += 365
                anomaly[i, j] = offset
                capped_max[i, j] = True
                continue

            # If forecast is below the annual min normal, cap it
            if fcst_t <= trough_temp:
                offset = trough_day - d0
                if offset > 182:
                    offset -= 365
                elif offset < -182:
                    offset += 365
                anomaly[i, j] = offset
                capped_min[i, j] = True
                continue

            # Determine which branch of the annual cycle we're on.
            # Ascending = temperatures are generally rising (winter → summer)
            # Descending = temperatures are generally falling (summer → winter)
            if trough_day < peak_day:
                ascending = (d0 >= trough_day) and (d0 <= peak_day)
            else:
                ascending = (d0 >= trough_day) or (d0 <= peak_day)

            # Get today's normal temperature to determine search direction
            today_normal = curve[d0]

            if fcst_t >= today_normal:
                # Warmer than normal → search FORWARD from today toward peak
                # (how many days ahead of schedule?)
                if ascending:
                    # Forward means toward the peak
                    if d0 <= peak_day:
                        search_days = list(range(d0, peak_day + 1))
                    else:
                        search_days = (list(range(d0, 365)) +
                                       list(range(0, peak_day + 1)))
                else:
                    # Descending branch, warmer than normal means search
                    # BACKWARD toward the peak (we've already passed it)
                    if peak_day <= d0:
                        search_days = list(range(d0, peak_day - 1, -1))
                    else:
                        search_days = (list(range(d0, -1, -1)) +
                                       list(range(364, peak_day - 1, -1)))
            else:
                # Cooler than normal → search BACKWARD from today toward trough
                # (how many days behind schedule?)
                if ascending:
                    # Backward means toward the trough
                    if trough_day <= d0:
                        search_days = list(range(d0, trough_day - 1, -1))
                    else:
                        search_days = (list(range(d0, -1, -1)) +
                                       list(range(364, trough_day - 1, -1)))
                else:
                    # Descending branch, cooler than normal means search
                    # FORWARD toward the trough
                    if d0 <= trough_day:
                        search_days = list(range(d0, trough_day + 1))
                    else:
                        search_days = (list(range(d0, 365)) +
                                       list(range(0, trough_day + 1)))

            # Find the NEAREST crossing — the first place the curve hits
            # the forecast temperature as we search outward from today.
            matched_day = None
            for k in range(len(search_days) - 1):
                d1 = search_days[k]
                d2 = search_days[k + 1]
                v1 = curve[d1] - fcst_t
                v2 = curve[d2] - fcst_t

                if v1 == 0:
                    matched_day = d1
                    break
                elif v1 * v2 < 0:
                    # Linear interpolation for fractional day
                    frac = abs(v1) / (abs(v1) + abs(v2))
                    matched_day = d1 + frac
                    break

            if matched_day is None:
                # No crossing found — forecast exceeds the range on this branch.
                # Use the endpoint (peak or trough) as the cap.
                if fcst_t >= today_normal:
                    matched_day = peak_day
                else:
                    matched_day = trough_day

            # Compute offset from today
            offset = matched_day - d0

            # Handle year wraparound
            if offset > 182:
                offset -= 365
            elif offset < -182:
                offset += 365

            anomaly[i, j] = offset

    valid_pct = 100 * np.sum(~np.isnan(anomaly)) / anomaly.size
    n_max = np.sum(capped_max)
    n_min = np.sum(capped_min)
    print(f"  Valid cells: {valid_pct:.0f}%")
    print(f"  Anomaly range: {np.nanmin(anomaly):.0f} to "
          f"{np.nanmax(anomaly):+.0f} days")
    print(f"  Mean anomaly: {np.nanmean(anomaly):+.1f} days")
    print(f"  Capped at MAX (warmer than any normal day): {n_max} cells")
    print(f"  Capped at MIN (colder than any normal day): {n_min} cells")

    return anomaly, capped_max, capped_min


# =============================================================================
# STEP 4: RENDER THE MAP
# =============================================================================

def render_map(anomaly, clim_lons, clim_lats, run_date, output_path,
               region_name=None, region_extent=None,
               capped_max=None, capped_min=None):
    """
    Create a nice-looking map of the day-offset anomalies.

    Uses Cartopy for the map projection and Matplotlib for the rendering.
    Grid cells are shaded with a diverging colormap: blue for "behind schedule"
    (cooler than normal for this date) and red/orange for "ahead of schedule"
    (warmer than normal).

    Parameters
    ----------
    anomaly : numpy.ndarray
        2D array of day-offset anomalies, shape (nlat, nlon)
    clim_lons, clim_lats : numpy.ndarray
        1D coordinate arrays for the grid
    run_date : date
        The date this map represents (for the title)
    output_path : str
        Where to save the PNG
    region_name : str, optional
        Human-readable name of the region (e.g., "Southeast"). If None,
        renders the full CONUS map.
    region_extent : list, optional
        [west_lon, east_lon, south_lat, north_lat] for the regional crop.
        If None, uses the full CONUS extent.
    """
    is_regional = region_name is not None

    if is_regional:
        print(f"  Rendering {region_name}...")
    else:
        print("Rendering map...")

    # --- Set up the figure and map projection ---
    fig = plt.figure(figsize=(MAP_WIDTH_INCHES, MAP_HEIGHT_INCHES))

    # Lambert Conformal Conic — the standard projection for US weather maps
    projection = ccrs.LambertConformal(
        central_longitude=-96,
        central_latitude=39,
        standard_parallels=(33, 45)
    )

    ax = fig.add_axes([0.02, 0.08, 0.96, 0.78], projection=projection)

    # Set the map extent
    if region_extent:
        w, e, s, n = region_extent
        ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    else:
        ax.set_extent([LON_WEST + 1, LON_EAST - 1, LAT_SOUTH + 1, LAT_NORTH - 1],
                      crs=ccrs.PlateCarree())

    # --- Add map features ---
    # Land and ocean background
    ax.add_feature(cfeature.LAND, facecolor="#f5f5f5", zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor="#e6f0f7", zorder=0)
    ax.add_feature(cfeature.LAKES, facecolor="#e6f0f7", edgecolor="#cccccc",
                   linewidth=0.5, zorder=1)

    # State borders
    ax.add_feature(cfeature.STATES, edgecolor="#888888", linewidth=0.5, zorder=3)

    # National border (thicker)
    ax.add_feature(cfeature.BORDERS, edgecolor="#444444", linewidth=1.0, zorder=3)

    # Coastlines
    ax.add_feature(cfeature.COASTLINE, edgecolor="#666666", linewidth=0.7, zorder=3)

    # --- Build the colormap ---
    # Custom diverging colormap: blue → white → red/orange
    # Blue = behind schedule (cooler than normal for this time of year)
    # Red = ahead of schedule (warmer than normal)
    colors_neg = [
        "#08306b",  # very dark blue (-45)
        "#2171b5",  # medium blue (-30)
        "#6baed6",  # light blue (-15)
        "#c6dbef",  # very light blue (-5)
    ]
    colors_center = ["#f7f7f7"]  # near-white at zero
    colors_pos = [
        "#fcbba1",  # very light red (+5)
        "#fb6a4a",  # light red (+15)
        "#cb181d",  # medium red (+30)
        "#67000d",  # very dark red (+45)
    ]

    all_colors = colors_neg + colors_center + colors_pos
    n_bins = 256  # Smooth gradient
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "temp_calendar", all_colors, N=n_bins
    )

    # --- Determine the display range dynamically from the data ---
    # Use the actual data extremes, rounded up to the nearest 5 for clean ticks.
    # Make the scale symmetric around zero so white always means "on schedule."
    abs_max = np.nanmax(np.abs(anomaly))
    # Round up to nearest 5
    abs_max = int(np.ceil(abs_max / 5.0) * 5)
    # Enforce a minimum range so mild days still look good
    abs_max = max(abs_max, 20)

    vmin = -abs_max
    vmax = abs_max

    # Pick tick spacing based on the range: every 10 for small ranges,
    # every 15 for medium, every 20 or 30 for very large ranges
    if abs_max <= 30:
        tick_spacing = 10
    elif abs_max <= 60:
        tick_spacing = 15
    elif abs_max <= 120:
        tick_spacing = 20
    else:
        tick_spacing = 30

    print(f"  Color scale: ±{abs_max} days (ticks every {tick_spacing})")

    # --- Plot the data ---
    lon2d, lat2d = np.meshgrid(clim_lons, clim_lats)

    # Smooth the anomaly field slightly for cleaner contours.
    # Without this, contourf can produce very jagged edges from grid noise.
    from scipy.ndimage import gaussian_filter
    anomaly_smooth = anomaly.copy()
    # Only smooth where we have valid data
    nan_mask = np.isnan(anomaly_smooth)
    anomaly_smooth[nan_mask] = 0  # Temporarily fill NaN for filtering
    anomaly_smooth = gaussian_filter(anomaly_smooth, sigma=1.5)
    anomaly_smooth[nan_mask] = np.nan  # Restore NaN

    # Define contour levels: fine levels for the color fill, coarser for lines
    fill_levels = np.linspace(vmin, vmax, 91)   # Smooth color gradient

    # Contour line interval: every 5 days for small ranges, scaling up for large
    if abs_max <= 45:
        line_interval = 5
    elif abs_max <= 90:
        line_interval = 10
    else:
        line_interval = 15
    line_levels = np.arange(vmin, vmax + 1, line_interval)

    # Filled contours (smooth color field)
    filled = ax.contourf(
        lon2d, lat2d, anomaly_smooth,
        levels=fill_levels,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        transform=ccrs.PlateCarree(),
        zorder=2,
        extend="both"
    )

    # Contour lines every 5 days
    lines = ax.contour(
        lon2d, lat2d, anomaly_smooth,
        levels=line_levels,
        colors="#444444",
        linewidths=0.6 if is_regional else 0.4,
        transform=ccrs.PlateCarree(),
        zorder=2.5
    )

    # Label the contour lines
    clabels = ax.clabel(
        lines,
        inline=True,
        fontsize=9 if is_regional else 7,
        fmt="%+.0f",           # Show sign: "+10", "-15", etc.
        inline_spacing=5,
        colors="#333333"
    )

    # --- Hatching and MAX/MIN labels for capped areas ---
    hatch_artists = []

    # Mask capped areas to CONUS bounds so hatching doesn't cover oceans
    conus_mask = (lat2d >= 24.5) & (lat2d <= 50.0) & (lon2d >= -125.0) & (lon2d <= -66.0)

    if capped_max is not None and np.any(capped_max):
        from scipy.ndimage import label as ndlabel
        capped_max_masked = capped_max & conus_mask
        # Draw hatching over areas warmer than any normal day
        hatch_max = ax.contourf(
            lon2d, lat2d, capped_max_masked.astype(float),
            levels=[0.5, 1.5],
            hatches=['///'],
            colors='none',
            transform=ccrs.PlateCarree(),
            zorder=2.8
        )
        hatch_artists.append(hatch_max)
        # Place "MAX" text at centroids of connected capped regions
        labeled_array, num_features = ndlabel(capped_max_masked)
        for region_id in range(1, num_features + 1):
            region_cells = labeled_array == region_id
            if np.sum(region_cells) < 4:
                continue  # Skip tiny isolated cells
            rlats = lat2d[region_cells]
            rlons = lon2d[region_cells]
            clat = np.mean(rlats)
            clon = np.mean(rlons)
            # Only place label if centroid is within CONUS bounds
            if clat < 24.5 or clat > 50.0 or clon < -125.0 or clon > -66.0:
                continue
            ax.text(
                clon, clat, "MAX",
                transform=ccrs.PlateCarree(),
                fontsize=8 if is_regional else 6,
                fontweight="bold",
                color="#67000d",
                ha="center", va="center",
                zorder=4,
                path_effects=[
                    pe.withStroke(linewidth=2, foreground="white")
                ]
            )

    if capped_min is not None and np.any(capped_min):
        from scipy.ndimage import label as ndlabel
        capped_min_masked = capped_min & conus_mask
        # Draw hatching over areas colder than any normal day
        hatch_min = ax.contourf(
            lon2d, lat2d, capped_min_masked.astype(float),
            levels=[0.5, 1.5],
            hatches=['\\\\\\'],
            colors='none',
            transform=ccrs.PlateCarree(),
            zorder=2.8
        )
        hatch_artists.append(hatch_min)
        # Place "MIN" text at centroids of connected capped regions
        labeled_array, num_features = ndlabel(capped_min_masked)
        for region_id in range(1, num_features + 1):
            region_cells = labeled_array == region_id
            if np.sum(region_cells) < 4:
                continue
            rlats = lat2d[region_cells]
            rlons = lon2d[region_cells]
            clat = np.mean(rlats)
            clon = np.mean(rlons)
            # Only place label if centroid is within CONUS bounds
            if clat < 24.5 or clat > 50.0 or clon < -125.0 or clon > -66.0:
                continue
            ax.text(
                clon, clat, "MIN",
                transform=ccrs.PlateCarree(),
                fontsize=8 if is_regional else 6,
                fontweight="bold",
                color="#08306b",
                ha="center", va="center",
                zorder=4,
                path_effects=[
                    pe.withStroke(linewidth=2, foreground="white")
                ]
            )

    # --- Colorbar ---
    cbar_ax = fig.add_axes([0.15, 0.06, 0.70, 0.025])
    cbar = fig.colorbar(filled, cax=cbar_ax, orientation="horizontal")
    cbar.set_label("Days Ahead (+) or Behind (−) Normal Temperature Schedule",
                   fontsize=11, fontweight="bold", labelpad=8)
    cbar.set_ticks(np.arange(vmin, vmax + 1, tick_spacing))
    cbar.ax.tick_params(labelsize=10)

    # --- Title ---
    date_str = run_date.strftime("%A, %B %-d, %Y")
    if is_regional:
        title = f"Temperature Calendar Anomaly: {region_name}"
        subtitle = date_str
    else:
        title = f"Temperature Calendar Anomaly: {date_str}"
        subtitle = ("How many days ahead or behind is each location's forecast high "
                    "compared to the normal temperature schedule?")

    ax.set_title(title, fontsize=16, fontweight="bold", pad=32)
    fig.text(0.5, 0.88, subtitle, ha="center", fontsize=10, color="#555555",
             style="italic")

    # --- Credit line ---
    fig.text(0.99, 0.01, "Data: NWS NDFD / ACIS 1991-2020 Normals",
             ha="right", fontsize=7, color="#999999")

    # --- Save ---
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=MAP_DPI, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"  Saved: {output_path} ({size_kb:.0f} KB)")


# =============================================================================
# TEST MODE — Render a map with synthetic data
# =============================================================================

def run_test_mode(run_date):
    """
    Create a test map using synthetic data so you can see the map rendering
    without needing to download a real forecast.
    """
    print("=" * 60)
    print("TEST MODE — Rendering map with synthetic data")
    print("=" * 60)

    # Load the climatology just to get the grid coordinates
    if not os.path.exists(CLIMATOLOGY_FILE):
        print(f"ERROR: Climatology file not found: {CLIMATOLOGY_FILE}")
        print("Run build_climatology.py first!")
        sys.exit(1)

    ds = xr.open_dataset(CLIMATOLOGY_FILE)
    clim_lons = ds.longitude.values
    clim_lats = ds.latitude.values

    # Create synthetic anomaly data for testing:
    # A smooth gradient from -30 (northwest) to +30 (southeast)
    # with some random noise added
    nlat, nlon = len(clim_lats), len(clim_lons)
    lon2d, lat2d = np.meshgrid(
        np.linspace(-1, 1, nlon),
        np.linspace(1, -1, nlat)
    )

    # Gradient + noise
    np.random.seed(42)
    synthetic = (lon2d + lat2d) * 15 + np.random.normal(0, 5, (nlat, nlon))

    # Mask out ocean (cells where climatology is NaN on July 1)
    clim_sample = ds.normal_maxt.sel(day_of_year=182).values
    synthetic[np.isnan(clim_sample)] = np.nan

    ds.close()

    output_path = os.path.join(OUTPUT_DIR, "temp_anomaly_TEST.png")
    render_map(synthetic, clim_lons, clim_lats, run_date, output_path)

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
    print(f"Check the test map at: {output_path}")
    print("If it looks good, run without --test for a real forecast map.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate the daily Temperature Calendar Map."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Render a test map with synthetic data (no forecast download)"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Override today's date (format: YYYY-MM-DD). "
             "Useful for testing. The forecast is still the current one, "
             "but the day-of-year calculation uses this date."
    )
    args = parser.parse_args()

    # Determine the run date
    if args.date:
        try:
            run_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.")
            sys.exit(1)
    else:
        run_date = date.today()

    # Day of year (1-365, no leap day handling for simplicity)
    today_doy = run_date.timetuple().tm_yday
    if today_doy > 365:
        today_doy = 365  # Leap day → treat as Dec 31

    print(f"\nTemperature Calendar Map — {run_date.strftime('%A, %B %-d, %Y')}")
    print(f"Day of year: {today_doy}\n")

    # Create output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # TEST MODE
    if args.test:
        run_test_mode(run_date)
        return

    # --- FULL RUN ---

    # 1. Check that climatology exists
    if not os.path.exists(CLIMATOLOGY_FILE):
        print(f"ERROR: Climatology file not found: {CLIMATOLOGY_FILE}")
        print("You need to run build_climatology.py first!")
        sys.exit(1)

    # 2. Load climatology
    print("Loading climatology...")
    ds = xr.open_dataset(CLIMATOLOGY_FILE)
    clim_normals = ds.normal_maxt.values      # shape: (365, nlat, nlon)
    clim_lons = ds.longitude.values            # shape: (nlon,)
    clim_lats = ds.latitude.values             # shape: (nlat,)
    ds.close()
    print(f"  Climatology grid: {clim_normals.shape}")

    # 3. Download forecast
    os.makedirs(CACHE_DIR, exist_ok=True)
    grib_path = download_ndfd_maxt(CACHE_DIR)
    if grib_path is None:
        print("Cannot proceed without forecast data. Exiting.")
        sys.exit(1)

    # 4. Load and parse the forecast
    fcst_lons, fcst_lats, fcst_temps = load_ndfd_forecast(grib_path)

    # 5. Regrid forecast to climatology grid
    forecast_regridded = regrid_to_climatology(
        fcst_lons, fcst_lats, fcst_temps,
        clim_lons, clim_lats
    )

    # 6. Compute anomalies
    anomaly, capped_max, capped_min = compute_day_anomaly(forecast_regridded, clim_normals, today_doy)

    # 7. Render the national map at full 300 DPI
    latest_path = os.path.join(OUTPUT_DIR, "temp_anomaly_latest.png")
    render_map(anomaly, clim_lons, clim_lats, run_date, latest_path,
               capped_max=capped_max, capped_min=capped_min)

    # 8. Create a lightweight archive copy (150 DPI JPEG, ~100-200 KB)
    # This is what the archive gallery page shows. Full-res PNGs are only
    # kept as "latest" so the repo doesn't balloon over time.
    from PIL import Image
    archive_dir = os.path.join(OUTPUT_DIR, "archive")
    os.makedirs(archive_dir, exist_ok=True)

    archive_path = os.path.join(
        archive_dir,
        f"temp_anomaly_{run_date.strftime('%Y%m%d')}.jpg"
    )

    print("Creating archive JPEG...")
    img = Image.open(latest_path)
    # Resize to 150 DPI equivalent (half of 300 DPI in each dimension)
    new_size = (img.width // 2, img.height // 2)
    img_resized = img.resize(new_size, Image.LANCZOS)
    img_resized = img_resized.convert("RGB")  # JPEG doesn't support alpha
    img_resized.save(archive_path, "JPEG", quality=85, optimize=True)

    archive_kb = os.path.getsize(archive_path) / 1024
    print(f"  Archive JPEG: {archive_path} ({archive_kb:.0f} KB)")

    # 9. Render regional crops (full 300 DPI, latest only — no archive)
    print(f"\nRendering {len(REGIONS)} regional maps...")
    regional_dir = os.path.join(OUTPUT_DIR, "regions")
    os.makedirs(regional_dir, exist_ok=True)

    for region_key, region_info in REGIONS.items():
        latest_region_path = os.path.join(
            regional_dir,
            f"temp_anomaly_{region_key}_latest.png"
        )

        render_map(
            anomaly, clim_lons, clim_lats, run_date, latest_region_path,
            region_name=region_info["name"],
            region_extent=region_info["extent"],
            capped_max=capped_max,
            capped_min=capped_min
        )

    # 10. Done!
    print("\n" + "=" * 60)
    print("DAILY RUN COMPLETE")
    print("=" * 60)
    print(f"National (300 DPI): {latest_path}")
    print(f"Archive  (150 DPI): {archive_path}")
    print(f"Regional maps:      {regional_dir}/")

    # Clean up the GRIB file to save disk space
    try:
        os.remove(grib_path)
        print("Cleaned up GRIB file.")
    except OSError:
        pass


if __name__ == "__main__":
    main()
