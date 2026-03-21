#!/usr/bin/env python3
"""
build_streak_history.py — Initialize the streak map from recent observed data.

This script builds the starting state for the streak map by pulling the last
90 days of observed high temperatures from ACIS, comparing each day against
the climatological normal, and walking forward through time to compute the
current streak at each grid cell.

YOU RUN THIS ONCE. After it finishes, you'll have a file called streak_state.nc
that the daily runner (daily_streak.py) updates each day.

Usage:
    python build_streak_history.py --test    # Quick test: pull one day and check
    python build_streak_history.py           # Full 90-day backfill

Requires:
    - climatology.nc must already exist (from build_climatology.py)
"""

import os
import sys
import json
import time
import argparse
import numpy as np
import requests
from datetime import datetime, date, timedelta
from tqdm import tqdm

from config import (
    ACIS_URL, STATES, API_DELAY, API_MAX_RETRIES,
    LON_WEST, LON_EAST, LAT_SOUTH, LAT_NORTH, GRID_SPACING,
    CLIMATOLOGY_FILE, STREAK_STATE_FILE, STREAK_BACKFILL_DAYS,
    CACHE_DIR
)


def fetch_observed_maxt(target_date):
    """
    Pull observed maximum temperatures from all CONUS stations for one date.

    Parameters
    ----------
    target_date : date
        The date to fetch observations for

    Returns
    -------
    list of dict
        Each dict has 'lat', 'lon', 'maxt' (float, in °F).
        Returns empty list if the API call fails.
    """
    date_str = target_date.strftime("%Y-%m-%d")

    payload = {
        "state": ",".join(STATES),
        "sdate": date_str,
        "edate": date_str,
        "elems": [{"name": "maxt"}],
        "meta": ["ll"]
    }

    for attempt in range(API_MAX_RETRIES):
        try:
            response = requests.post(
                ACIS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120
            )
            response.raise_for_status()
            result = response.json()
            break
        except requests.exceptions.RequestException as e:
            if attempt < API_MAX_RETRIES - 1:
                time.sleep(5)
            else:
                print(f"    FAILED to fetch {date_str} after {API_MAX_RETRIES} attempts: {e}")
                return []

    if "data" not in result:
        print(f"    WARNING: No 'data' key in response for {date_str}")
        return []

    observations = []
    for station in result["data"]:
        meta = station.get("meta", {})
        data = station.get("data", [])

        ll = meta.get("ll")
        if ll is None or len(ll) < 2:
            continue

        lon, lat = ll[0], ll[1]

        if not (LON_WEST <= lon <= LON_EAST and LAT_SOUTH <= lat <= LAT_NORTH):
            continue

        if len(data) == 0 or len(data[0]) == 0:
            continue

        val = data[0][0] if isinstance(data[0], list) else data[0]

        if val in ("M", "T", "", None, "None"):
            continue

        try:
            observations.append({
                "lat": lat,
                "lon": lon,
                "maxt": float(val)
            })
        except (ValueError, TypeError):
            continue

    return observations


def grid_observations(observations, clim_lons, clim_lats):
    """
    Interpolate station observations onto the climatology grid.

    Parameters
    ----------
    observations : list of dict
        Station observations from fetch_observed_maxt()
    clim_lons, clim_lats : numpy.ndarray
        1D arrays defining the grid

    Returns
    -------
    numpy.ndarray
        2D array of temperatures on the climatology grid.
        NaN where no data is available.
    """
    from scipy.interpolate import griddata

    if len(observations) < 50:
        return np.full((len(clim_lats), len(clim_lons)), np.nan)

    station_lons = np.array([o["lon"] for o in observations])
    station_lats = np.array([o["lat"] for o in observations])
    station_temps = np.array([o["maxt"] for o in observations])
    station_points = np.column_stack([station_lons, station_lats])

    lon_grid, lat_grid = np.meshgrid(clim_lons, clim_lats)

    gridded = griddata(
        station_points, station_temps,
        (lon_grid, lat_grid),
        method="linear"
    )

    # Fill edges with nearest neighbor
    nan_mask = np.isnan(gridded)
    if np.any(nan_mask):
        nearest = griddata(
            station_points, station_temps,
            (lon_grid, lat_grid),
            method="nearest"
        )
        gridded[nan_mask] = nearest[nan_mask]

    return gridded


def run_test():
    """Quick test: pull one day of observations and verify."""
    import xarray as xr

    print("=" * 60)
    print("TEST MODE — Pulling yesterday's observed highs")
    print("=" * 60)

    if not os.path.exists(CLIMATOLOGY_FILE):
        print(f"ERROR: Climatology file not found: {CLIMATOLOGY_FILE}")
        print("Run build_climatology.py first!")
        sys.exit(1)

    yesterday = date.today() - timedelta(days=1)
    print(f"\nFetching observations for {yesterday}...")

    obs = fetch_observed_maxt(yesterday)
    print(f"Got {len(obs)} valid station observations.")

    if len(obs) > 0:
        temps = [o["maxt"] for o in obs]
        print(f"Temperature range: {min(temps):.0f}°F to {max(temps):.0f}°F")
        print(f"Mean: {np.mean(temps):.1f}°F")

        # Grid them
        ds = xr.open_dataset(CLIMATOLOGY_FILE)
        clim_lons = ds.longitude.values
        clim_lats = ds.latitude.values
        ds.close()

        print(f"\nGridding to {len(clim_lats)}×{len(clim_lons)} grid...")
        gridded = grid_observations(obs, clim_lons, clim_lats)
        valid_pct = 100 * np.sum(~np.isnan(gridded)) / gridded.size
        print(f"Valid cells: {valid_pct:.0f}%")
        print(f"Gridded range: {np.nanmin(gridded):.0f}°F to {np.nanmax(gridded):.0f}°F")

    print("\n" + "=" * 60)
    if len(obs) > 500:
        print("✓ Looking good. Ready for the full backfill:")
        print("  python build_streak_history.py")
    else:
        print("⚠ Low station count. ACIS may be having issues — try again later.")


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the streak map from recent observed data."
    )
    parser.add_argument("--test", action="store_true",
                        help="Quick test: pull one day and verify")
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    # --- FULL BACKFILL ---
    import xarray as xr

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║       STREAK MAP — HISTORY BACKFILL                     ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  Building the initial streak state from the last 90     ║")
    print("║  days of observed high temperatures.                    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # Load climatology
    if not os.path.exists(CLIMATOLOGY_FILE):
        print(f"ERROR: Climatology file not found: {CLIMATOLOGY_FILE}")
        print("Run build_climatology.py first!")
        sys.exit(1)

    print("Loading climatology...")
    ds = xr.open_dataset(CLIMATOLOGY_FILE)
    clim_normals = ds.normal_maxt.values  # (365, nlat, nlon)
    clim_lons = ds.longitude.values
    clim_lats = ds.latitude.values
    ds.close()

    nlat, nlon = len(clim_lats), len(clim_lons)
    print(f"Grid: {nlat}×{nlon}")

    # Initialize streak array: 0 = no streak established yet
    streak = np.zeros((nlat, nlon), dtype=np.float32)

    # Walk through the last N days, oldest to newest
    today = date.today()
    start_date = today - timedelta(days=STREAK_BACKFILL_DAYS)

    print(f"\nBackfilling from {start_date} to {today - timedelta(days=1)}")
    print(f"({STREAK_BACKFILL_DAYS} days, with {API_DELAY}s delay between requests)\n")

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, "streak_backfill_cache.json")

    # Check for cached backfill data (in case of interruption)
    cached_obs = {}
    if os.path.exists(cache_file):
        print(f"Found cached backfill data, loading...")
        with open(cache_file, "r") as f:
            cached_obs = json.load(f)
        print(f"Cached: {len(cached_obs)} days\n")

    all_obs = dict(cached_obs)
    failed_days = []

    for i in tqdm(range(STREAK_BACKFILL_DAYS), desc="Downloading", unit="day"):
        d = start_date + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")

        if d_str in all_obs:
            continue  # Already cached

        obs = fetch_observed_maxt(d)

        if len(obs) > 0:
            all_obs[d_str] = obs
        else:
            failed_days.append(d_str)
            tqdm.write(f"  No data for {d_str}")

        time.sleep(API_DELAY)

        # Save cache periodically
        if i % 10 == 0:
            with open(cache_file, "w") as f:
                json.dump(all_obs, f)

    # Final cache save
    with open(cache_file, "w") as f:
        json.dump(all_obs, f)

    print(f"\nDownloaded {len(all_obs)} days, {len(failed_days)} failed.")

    if len(failed_days) > 0:
        print(f"Failed dates: {failed_days}")

    # Now walk forward through time computing streaks
    print("\nComputing streaks...")

    for i in tqdm(range(STREAK_BACKFILL_DAYS), desc="Processing", unit="day"):
        d = start_date + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")

        if d_str not in all_obs:
            continue  # Skip failed days — streak is unchanged

        # Grid the observations
        gridded = grid_observations(all_obs[d_str], clim_lons, clim_lats)

        # Get the normal for this day of year
        doy = d.timetuple().tm_yday
        if doy > 365:
            doy = 365
        normal = clim_normals[doy - 1, :, :]

        # Compare: above normal = positive streak, below = negative
        above = gridded > normal
        below = gridded < normal
        equal = ~above & ~below  # Exactly equal to normal

        # Update streaks:
        # If above and streak was positive → increment
        # If above and streak was negative or zero → reset to +1
        # If below and streak was negative → decrement (more negative)
        # If below and streak was positive or zero → reset to -1
        # If equal → reset to 0

        new_streak = np.zeros_like(streak)

        # Above normal
        mask = above & (streak > 0)
        new_streak[mask] = streak[mask] + 1

        mask = above & (streak <= 0)
        new_streak[mask] = 1

        # Below normal
        mask = below & (streak < 0)
        new_streak[mask] = streak[mask] - 1

        mask = below & (streak >= 0)
        new_streak[mask] = -1

        # Equal to normal — reset
        new_streak[equal] = 0

        # Preserve NaN from gridding failures
        nan_mask = np.isnan(gridded) | np.isnan(normal)
        new_streak[nan_mask] = streak[nan_mask]  # Don't update where we have no data

        streak = new_streak

    # Save the streak state
    print("\nSaving streak state...")

    last_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    da = xr.DataArray(
        data=streak,
        dims=["latitude", "longitude"],
        coords={
            "latitude": clim_lats,
            "longitude": clim_lons
        },
        attrs={
            "long_name": "Consecutive days above (positive) or below (negative) normal high temperature",
            "units": "day_count",
            "last_updated": last_date,
            "backfill_start": start_date.strftime("%Y-%m-%d"),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    )

    ds_out = xr.Dataset({"streak": da})
    ds_out.to_netcdf(STREAK_STATE_FILE)

    size_kb = os.path.getsize(STREAK_STATE_FILE) / 1024
    print(f"Saved to: {STREAK_STATE_FILE} ({size_kb:.0f} KB)")

    # Report
    above_max = np.nanmax(streak)
    below_min = np.nanmin(streak)
    mean_streak = np.nanmean(streak)

    print(f"\nStreak summary as of {last_date}:")
    print(f"  Longest above-normal streak: +{above_max:.0f} days")
    print(f"  Longest below-normal streak: {below_min:.0f} days")
    print(f"  Mean streak: {mean_streak:+.1f} days")

    print("\n" + "=" * 60)
    print("BACKFILL COMPLETE")
    print("=" * 60)
    print(f"Streak state saved to: {STREAK_STATE_FILE}")
    print(f"\nNext step: test the daily runner:")
    print(f"  python daily_streak.py")


if __name__ == "__main__":
    main()
