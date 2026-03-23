#!/usr/bin/env python3
"""
daily_streak.py — Update the streak map and render today's image.

This script runs once per day (via GitHub Actions or manually). It:
    1. Loads yesterday's streak state
    2. Pulls yesterday's observed high temperatures from ACIS
    3. Compares each grid cell against the climatological normal
    4. Increments or resets each cell's streak
    5. Saves the updated streak state
    6. Renders the map as a PNG

Usage:
    python daily_streak.py           # Normal run
    python daily_streak.py --test    # Render using current streak state (no update)
    python daily_streak.py --date 2026-03-20  # Pretend today is a specific date

Requires:
    - climatology.nc (from build_climatology.py)
    - streak_state.nc (from build_streak_history.py)
"""

import os
import sys
import time
import argparse
import warnings
from datetime import datetime, date, timedelta

import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import cartopy.crs as ccrs
import cartopy.feature as cfeature

from config import (
    CLIMATOLOGY_FILE, STREAK_STATE_FILE, OUTPUT_DIR, CACHE_DIR,
    LON_WEST, LON_EAST, LAT_SOUTH, LAT_NORTH,
    STREAK_MAX_DAYS, MAP_WIDTH_INCHES, MAP_HEIGHT_INCHES, MAP_DPI,
    REGIONS
)

# Import the observation-fetching functions from the backfill script
from build_streak_history import fetch_observed_maxt, grid_observations, qc_observations

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


def update_streak(streak, observed_grid, normal_grid):
    """
    Update the streak array based on today's observed vs. normal.

    Parameters
    ----------
    streak : numpy.ndarray
        Current streak grid. Positive = consecutive above-normal days,
        negative = consecutive below-normal days.
    observed_grid : numpy.ndarray
        Gridded observed high temperatures for yesterday
    normal_grid : numpy.ndarray
        Climatological normal high for yesterday's day-of-year

    Returns
    -------
    numpy.ndarray
        Updated streak grid
    """
    above = observed_grid > normal_grid
    below = observed_grid < normal_grid
    equal = ~above & ~below

    new_streak = np.zeros_like(streak)

    # Above normal: extend positive streak or start new one
    mask = above & (streak > 0)
    new_streak[mask] = streak[mask] + 1
    mask = above & (streak <= 0)
    new_streak[mask] = 1

    # Below normal: extend negative streak or start new one
    mask = below & (streak < 0)
    new_streak[mask] = streak[mask] - 1
    mask = below & (streak >= 0)
    new_streak[mask] = -1

    # Equal to normal: reset
    new_streak[equal] = 0

    # Don't update where observations are missing
    nan_mask = np.isnan(observed_grid) | np.isnan(normal_grid)
    new_streak[nan_mask] = streak[nan_mask]

    return new_streak


def render_streak_map(streak, clim_lons, clim_lats, run_date, output_path,
                      region_name=None, region_extent=None):
    """
    Render the streak map.

    Red/warm colors = consecutive above-normal days.
    Blue/cool colors = consecutive below-normal days.
    White/near-white = just flipped (streak of 0-1 day).
    Intensity deepens as the streak lengthens.

    Parameters
    ----------
    streak : numpy.ndarray
        2D streak grid
    clim_lons, clim_lats : numpy.ndarray
        Grid coordinates
    run_date : date
        Date for the title
    output_path : str
        Where to save the PNG
    region_name : str, optional
        If rendering a regional crop
    region_extent : list, optional
        [west, east, south, north] for regional crop
    """
    is_regional = region_name is not None

    if is_regional:
        print(f"  Rendering {region_name}...")
    else:
        print("Rendering streak map...")

    if is_regional and region_extent:
        w, e, s, n = region_extent
        aspect = (e - w) / (n - s)
        fig_w = 10
        fig = plt.figure(figsize=(fig_w, fig_w / aspect))
    else:
        fig = plt.figure(figsize=(MAP_WIDTH_INCHES, MAP_HEIGHT_INCHES))

    projection = ccrs.LambertConformal(
        central_longitude=-96,
        central_latitude=39,
        standard_parallels=(33, 45)
    )

    ax = fig.add_axes([0.02, 0.13, 0.96, 0.74], projection=projection)

    if region_extent:
        w, e, s, n = region_extent
        ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    else:
        ax.set_extent([LON_WEST + 1, LON_EAST - 1, LAT_SOUTH + 1, LAT_NORTH - 1],
                      crs=ccrs.PlateCarree())

    # Map features
    ax.add_feature(cfeature.LAND, facecolor="#f5f5f5", zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor="#e6f0f7", zorder=0)
    ax.add_feature(cfeature.LAKES, facecolor="#e6f0f7", edgecolor="#cccccc",
                   linewidth=0.5, zorder=1)
    ax.add_feature(cfeature.STATES, edgecolor="#888888", linewidth=0.5, zorder=3)
    ax.add_feature(cfeature.BORDERS, edgecolor="#444444", linewidth=1.0, zorder=3)
    ax.add_feature(cfeature.COASTLINE, edgecolor="#666666", linewidth=0.7, zorder=3)

    # Colormap: blue (below) → white (zero) → red (above)
    # Discrete steps so the streak count is visually countable
    colors_neg = [
        "#08306b",  # -30 (very dark blue)
        "#1d5da0",  # -25
        "#2e7ebc",  # -20
        "#4a9ad0",  # -15
        "#72b5d8",  # -10
        "#a6cee3",  # -5
        "#d0e1f0",  # -2
    ]
    colors_center = ["#f7f7f7"]  # 0
    colors_pos = [
        "#f0d0cf",  # +2
        "#e3a5a0",  # +5
        "#d87272",  # +10
        "#d04a4a",  # +15
        "#bc2e2e",  # +20
        "#a01d1d",  # +25
        "#6b0000",  # +30
    ]

    all_colors = colors_neg + colors_center + colors_pos
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "streak", all_colors, N=256
    )

    vmin = -STREAK_MAX_DAYS
    vmax = STREAK_MAX_DAYS

    # Smooth lightly for contours
    from scipy.ndimage import gaussian_filter
    streak_smooth = streak.copy()
    nan_mask = np.isnan(streak_smooth)
    streak_smooth[nan_mask] = 0
    streak_smooth = gaussian_filter(streak_smooth, sigma=1.0)
    streak_smooth[nan_mask] = np.nan

    lon2d, lat2d = np.meshgrid(clim_lons, clim_lats)

    fill_levels = np.linspace(vmin, vmax, 61)

    # Contour lines every 5 days
    line_levels = np.arange(vmin, vmax + 1, 5)
    # Remove the zero line — it would be everywhere and clutter the map
    line_levels = line_levels[line_levels != 0]

    filled = ax.contourf(
        lon2d, lat2d, streak_smooth,
        levels=fill_levels,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        transform=ccrs.PlateCarree(),
        zorder=2,
        extend="both"
    )

    lines = ax.contour(
        lon2d, lat2d, streak_smooth,
        levels=line_levels,
        colors="#444444",
        linewidths=0.6 if is_regional else 0.4,
        transform=ccrs.PlateCarree(),
        zorder=2.5
    )

    clabels = ax.clabel(
        lines,
        inline=True,
        fontsize=9 if is_regional else 7,
        fmt="%+.0f",
        inline_spacing=5,
        colors="#333333"
    )

    if is_regional:
        # Clip all contour data to the axes boundary
        for artist in [filled, lines]:
            if hasattr(artist, 'collections'):
                for col in artist.collections:
                    col.set_clip_path(ax.patch)
            else:
                artist.set_clip_path(ax.patch)
        if clabels:
            for txt in clabels:
                txt.set_clip_on(True)
    else:
        # Clip contours to CONUS boundary so oceans and Canada/Mexico are clean
        import cartopy.io.shapereader as shpreader
        from matplotlib.path import Path as MplPath
        from matplotlib.patches import PathPatch

        shapefile = shpreader.natural_earth(
            resolution='110m', category='cultural', name='admin_0_countries'
        )
        reader = shpreader.Reader(shapefile)
        us_geom = None
        for record in reader.records():
            if record.attributes.get('NAME') == 'United States of America' or \
               record.attributes.get('ISO_A2') == 'US':
                us_geom = record.geometry
                break

        if us_geom is not None:
            projected_geom = projection.project_geometry(us_geom, ccrs.PlateCarree())
            vertices = []
            codes = []
            for geom in projected_geom.geoms:
                coords = list(geom.exterior.coords)
                vertices.extend(coords)
                codes.append(MplPath.MOVETO)
                codes.extend([MplPath.LINETO] * (len(coords) - 2))
                codes.append(MplPath.CLOSEPOLY)

            clip_path = MplPath(vertices, codes)
            clip_patch = PathPatch(clip_path, transform=ax.transData, facecolor='none')
            ax.add_patch(clip_patch)

            for artist in [filled, lines]:
                if hasattr(artist, 'collections'):
                    for col in artist.collections:
                        col.set_clip_path(clip_patch)
                else:
                    artist.set_clip_path(clip_patch)

            if clabels:
                for txt in clabels:
                    x, y = txt.get_position()
                    if not clip_path.contains_point((x, y)):
                        txt.remove()

    # Get the US boundary from Natural Earth
    shapefile = shpreader.natural_earth(
        resolution='110m', category='cultural', name='admin_0_countries'
    )
    reader = shpreader.Reader(shapefile)
    us_geom = None
    for record in reader.records():
        if record.attributes.get('NAME') == 'United States of America' or \
           record.attributes.get('ISO_A2') == 'US':
            us_geom = record.geometry
            break

    if us_geom is not None:
        # Convert the geometry to the map's projection and create a clip path
        projected_geom = projection.project_geometry(us_geom, ccrs.PlateCarree())

        # Build a matplotlib Path from all the polygon parts
        vertices = []
        codes = []
        for geom in projected_geom.geoms:
            # Get exterior ring
            coords = list(geom.exterior.coords)
            vertices.extend(coords)
            codes.append(MplPath.MOVETO)
            codes.extend([MplPath.LINETO] * (len(coords) - 2))
            codes.append(MplPath.CLOSEPOLY)

        clip_path = MplPath(vertices, codes)
        clip_patch = PathPatch(clip_path, transform=ax.transData, facecolor='none')
        ax.add_patch(clip_patch)

        # Apply clip — handle both old and new matplotlib APIs
        for artist in [filled, lines]:
            if hasattr(artist, 'collections'):
                for col in artist.collections:
                    col.set_clip_path(clip_patch)
            else:
                artist.set_clip_path(clip_patch)

        # Remove contour labels outside CONUS
        if clabels:
            for txt in clabels:
                # Get label position in data coordinates
                x, y = txt.get_position()
                if not clip_path.contains_point((x, y)):
                    txt.remove()

    # Colorbar
    cbar_ax = fig.add_axes([0.15, 0.08, 0.70, 0.025])
    cbar = fig.colorbar(filled, cax=cbar_ax, orientation="horizontal")
    cbar.set_label("Consecutive Days Above (+) or Below (−) Normal High Temperature",
                   fontsize=11, fontweight="bold", labelpad=8)
    cbar.set_ticks(np.arange(vmin, vmax + 1, 5))
    cbar.ax.tick_params(labelsize=10)

    # Title
    date_str = run_date.strftime("%A, %B %-d, %Y")
    if is_regional:
        title = f"Temperature Streak Map: {region_name}"
        subtitle = date_str
    else:
        title = f"Temperature Streak Map: {date_str}"
        subtitle = ("How many consecutive days has each location's high temperature "
                    "been above or below normal?")

    ax.set_title(title, fontsize=16, fontweight="bold", pad=32)
    fig.text(0.5, 0.88, subtitle, ha="center", fontsize=10, color="#555555",
             style="italic")

    fig.text(0.99, 0.01, "Data: ACIS Observations / 1991-2020 Normals",
             ha="right", fontsize=7, color="#999999")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=MAP_DPI,
                facecolor="white", edgecolor="none")
    plt.close(fig)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"  Saved: {output_path} ({size_kb:.0f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Update the temperature streak map."
    )
    parser.add_argument("--test", action="store_true",
                        help="Render using current streak state without updating")
    parser.add_argument("--date", type=str, default=None,
                        help="Override today's date (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.date:
        try:
            run_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.")
            sys.exit(1)
    else:
        run_date = date.today()

    yesterday = run_date - timedelta(days=1)

    print(f"\nTemperature Streak Map — {run_date.strftime('%A, %B %-d, %Y')}")
    print(f"Updating with observations from {yesterday}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load climatology
    if not os.path.exists(CLIMATOLOGY_FILE):
        print(f"ERROR: Climatology file not found: {CLIMATOLOGY_FILE}")
        sys.exit(1)

    ds = xr.open_dataset(CLIMATOLOGY_FILE)
    clim_normals = ds.normal_maxt.values
    clim_lons = ds.longitude.values
    clim_lats = ds.latitude.values
    ds.close()

    # Load streak state
    if not os.path.exists(STREAK_STATE_FILE):
        print(f"ERROR: Streak state file not found: {STREAK_STATE_FILE}")
        print("Run build_streak_history.py first!")
        sys.exit(1)

    ds_streak = xr.open_dataset(STREAK_STATE_FILE)
    streak = ds_streak.streak.values.astype(np.float32).copy()
    last_updated = ds_streak.streak.attrs.get("last_updated", "unknown")
    ds_streak.close()

    print(f"Loaded streak state (last updated: {last_updated})")
    print(f"Current max streak: +{np.nanmax(streak):.0f} / {np.nanmin(streak):.0f}")

    # --- Update the streak (unless --test) ---
    if not args.test:
        print(f"\nFetching observations for {yesterday}...")
        obs = fetch_observed_maxt(yesterday)
        print(f"  Got {len(obs)} station observations.")

        if len(obs) < 100:
            print("  WARNING: Very few observations. ACIS may be delayed.")
            print("  Rendering map with current streak state (no update).")
        else:
            # Get the normal for yesterday's DOY
            doy = yesterday.timetuple().tm_yday
            if doy > 365:
                doy = 365
            normal_grid = clim_normals[doy - 1, :, :]

            # QC the observations
            obs_clean = qc_observations(obs, clim_normals, clim_lons, clim_lats, doy)

            # Grid the observations
            observed_grid = grid_observations(obs_clean, clim_lons, clim_lats)

            # Update the streak
            streak = update_streak(streak, observed_grid, normal_grid)

            print(f"  Updated. Max streak: +{np.nanmax(streak):.0f} / {np.nanmin(streak):.0f}")

            # Save the updated streak state
            da = xr.DataArray(
                data=streak,
                dims=["latitude", "longitude"],
                coords={
                    "latitude": clim_lats,
                    "longitude": clim_lons
                },
                attrs={
                    "long_name": "Consecutive days above/below normal high temperature",
                    "units": "day_count",
                    "last_updated": yesterday.strftime("%Y-%m-%d"),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            ds_out = xr.Dataset({"streak": da})
            ds_out.to_netcdf(STREAK_STATE_FILE)
            print(f"  Streak state saved.")
    else:
        print("TEST MODE — rendering without updating streak.")

    # --- Render the national map ---
    latest_path = os.path.join(OUTPUT_DIR, "streak_latest.png")
    render_streak_map(streak, clim_lons, clim_lats, run_date, latest_path)

    # --- Archive JPEG ---
    from PIL import Image
    archive_dir = os.path.join(OUTPUT_DIR, "archive")
    os.makedirs(archive_dir, exist_ok=True)

    archive_path = os.path.join(
        archive_dir,
        f"streak_{run_date.strftime('%Y%m%d')}.jpg"
    )

    print("Creating archive JPEG...")
    img = Image.open(latest_path)
    new_size = (img.width // 2, img.height // 2)
    img_resized = img.resize(new_size, Image.LANCZOS)
    img_resized = img_resized.convert("RGB")
    img_resized.save(archive_path, "JPEG", quality=85, optimize=True)
    archive_kb = os.path.getsize(archive_path) / 1024
    print(f"  Archive JPEG: {archive_path} ({archive_kb:.0f} KB)")

    # --- Regional crops ---
    print(f"\nRendering {len(REGIONS)} regional maps...")
    regional_dir = os.path.join(OUTPUT_DIR, "streak_regions")
    os.makedirs(regional_dir, exist_ok=True)

    for region_key, region_info in REGIONS.items():
        latest_region_path = os.path.join(
            regional_dir,
            f"streak_{region_key}_latest.png"
        )
        render_streak_map(
            streak, clim_lons, clim_lats, run_date, latest_region_path,
            region_name=region_info["name"],
            region_extent=region_info["extent"]
        )

    # --- Done ---
    print("\n" + "=" * 60)
    print("STREAK MAP COMPLETE")
    print("=" * 60)
    print(f"National map: {latest_path}")
    print(f"Archive:      {archive_path}")
    print(f"Regions:      {regional_dir}/")


if __name__ == "__main__":
    main()
