#!/usr/bin/env python3
"""
build_climatology.py — Build a gridded daily-normal high temperature dataset.

This script pulls 1991-2020 daily normal high temperatures from thousands of
weather stations via the RCC-ACIS API, then interpolates them onto a regular
latitude/longitude grid covering the continental US. The result is saved as a
NetCDF file that the daily runner uses to compute "days ahead/behind schedule."

YOU RUN THIS ONCE. After it finishes, you'll have a file called climatology.nc
that never needs to be regenerated (unless you want to change the grid spacing
or a new 30-year normal period comes out).

Usage:
    python build_climatology.py --test     # Quick test: query one state, show what comes back
    python build_climatology.py            # Full build (takes 10-20 minutes)

What it does step by step:
    1. Queries the RCC-ACIS API state by state to get daily normal highs
    2. Cleans the data (removes stations with missing values)
    3. Saves the raw station data to a CSV cache (so you don't have to re-download)
    4. Interpolates station data onto a regular 0.25° grid for each day of year
    5. Saves the final grid as a NetCDF file (climatology.nc)
"""

import os
import sys
import json
import time
import argparse
import numpy as np
import requests
from tqdm import tqdm

# Import settings from our config file
from config import (
    ACIS_URL, STATES, API_DELAY, API_MAX_RETRIES,
    LON_WEST, LON_EAST, LAT_SOUTH, LAT_NORTH, GRID_SPACING,
    CLIMATOLOGY_FILE, CACHE_DIR
)


# =============================================================================
# STEP 1: DOWNLOAD STATION NORMALS FROM ACIS
# =============================================================================

def fetch_state_normals(state_abbrev):
    """
    Query ACIS for all stations in one state and get their 365-day normal highs.

    The ACIS "MultiStnData" endpoint lets us request data for every station
    in a state at once. We ask for the daily normal maximum temperature
    ("maxt" with the "normal" flag set to 1).

    We use a non-leap year (2019) as our date range so we get exactly 365 days.
    The normal values don't depend on the year — they're 30-year averages.

    Parameters
    ----------
    state_abbrev : str
        Two-letter state abbreviation, like "KS" or "CA"

    Returns
    -------
    list of dict
        Each dict has keys: 'name', 'lat', 'lon', 'normals' (a list of 365 floats)
        Returns an empty list if the API call fails.
    """

    # This is the JSON payload we send to the ACIS API.
    # "normal": 1 tells ACIS to return the 1991-2020 climate normal
    # instead of actual observed data.
    payload = {
        "state": state_abbrev,
        "sdate": "2019-01-01",       # Start date (non-leap year)
        "edate": "2019-12-31",       # End date (gives us exactly 365 days)
        "elems": [{
            "name": "maxt",           # Maximum temperature
            "normal": "1"             # Return the 30-year normal value
        }],
        "meta": ["name", "state", "ll", "sids"]  # Station metadata to include
    }

    # Try the API call, with retries for network hiccups
    for attempt in range(API_MAX_RETRIES):
        try:
            response = requests.post(
                ACIS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120  # 2-minute timeout (some states have many stations)
            )
            response.raise_for_status()  # Raises an error for HTTP 4xx/5xx
            result = response.json()
            break  # Success — exit the retry loop

        except requests.exceptions.RequestException as e:
            print(f"    Attempt {attempt + 1}/{API_MAX_RETRIES} failed for {state_abbrev}: {e}")
            if attempt < API_MAX_RETRIES - 1:
                time.sleep(5)  # Wait 5 seconds before retrying
            else:
                print(f"    GIVING UP on {state_abbrev} after {API_MAX_RETRIES} attempts.")
                return []

    # Parse the response into a clean list of station records
    stations = []

    # The ACIS response has a "data" key containing one entry per station
    if "data" not in result:
        print(f"    WARNING: Unexpected response format for {state_abbrev}.")
        print(f"    Response keys: {list(result.keys())}")
        if "error" in result:
            print(f"    Error message: {result['error']}")
        return []

    for station in result["data"]:
        meta = station.get("meta", {})
        data = station.get("data", [])

        # Extract lat/lon from the "ll" field (which is [longitude, latitude])
        ll = meta.get("ll")
        if ll is None or len(ll) < 2:
            continue  # Skip stations without coordinates

        lon, lat = ll[0], ll[1]

        # Make sure this station is actually in the CONUS bounding box
        if not (LON_WEST <= lon <= LON_EAST and LAT_SOUTH <= lat <= LAT_NORTH):
            continue

        # Parse the 365 normal values.
        # ACIS returns each day as a list: either ["72.3"] for a single element,
        # or possibly ["72.3", "5.1"] if departure was also requested.
        # We grab the first element from each day.
        normals = []
        valid = True
        for day_values in data:
            if isinstance(day_values, list) and len(day_values) > 0:
                val = day_values[0]
            else:
                val = day_values

            # ACIS uses "M" for missing data and "T" for trace
            if val in ("M", "T", "", None, "None"):
                valid = False
                break

            try:
                normals.append(float(val))
            except (ValueError, TypeError):
                valid = False
                break

        # Only keep stations with a full 365 days of valid normals
        if valid and len(normals) == 365:
            stations.append({
                "name": meta.get("name", "Unknown"),
                "lat": lat,
                "lon": lon,
                "normals": normals
            })

    return stations


def download_all_normals(cache_file):
    """
    Download normals for all CONUS states and save to a cache file.

    This is the most time-consuming step (10-15 minutes) because we're
    making 49 separate API calls. The cache file means you only do this once.

    Parameters
    ----------
    cache_file : str
        Path to save the JSON cache file

    Returns
    -------
    list of dict
        All station records from all states
    """
    all_stations = []

    print("=" * 60)
    print("DOWNLOADING STATION NORMALS FROM ACIS")
    print("=" * 60)
    print(f"Querying {len(STATES)} states. This will take 10-15 minutes.")
    print(f"(Being polite to the ACIS server with {API_DELAY}s delays between requests)\n")

    for state in tqdm(STATES, desc="States", unit="state"):
        stations = fetch_state_normals(state)
        all_stations.extend(stations)
        tqdm.write(f"  {state}: {len(stations)} stations with valid normals")

        # Be polite — don't hammer the API
        time.sleep(API_DELAY)

    print(f"\nTotal stations collected: {len(all_stations)}")

    # Save to cache so we never have to download again
    print(f"Saving cache to {cache_file}...")

    # Convert to a JSON-serializable format
    # (numpy arrays aren't JSON-serializable, but our normals are plain lists)
    cache_data = {
        "stations": all_stations,
        "metadata": {
            "source": "RCC-ACIS MultiStnData",
            "element": "maxt normal (1991-2020)",
            "num_stations": len(all_stations),
            "date_generated": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    }

    with open(cache_file, "w") as f:
        json.dump(cache_data, f)

    size_mb = os.path.getsize(cache_file) / (1024 * 1024)
    print(f"Cache saved ({size_mb:.1f} MB)")

    return all_stations


# =============================================================================
# STEP 2: INTERPOLATE STATIONS TO A REGULAR GRID
# =============================================================================

def build_grid(all_stations):
    """
    Interpolate station normals onto a regular lat/lon grid.

    For each of the 365 days of the year, we take the point observations
    from ~5000-8000 stations and interpolate them onto a regular grid using
    scipy's griddata function. This gives us a smooth, continuous field of
    normal temperatures that we can compare against gridded forecasts.

    Parameters
    ----------
    all_stations : list of dict
        Station records from download_all_normals()

    Returns
    -------
    tuple of (numpy.ndarray, numpy.ndarray, numpy.ndarray)
        (lon_grid_1d, lat_grid_1d, normals_grid) where normals_grid has
        shape (365, num_lats, num_lons)
    """
    # Lazy import — scipy is big, only load it when we need it
    from scipy.interpolate import griddata
    from scipy.ndimage import gaussian_filter

    print("\n" + "=" * 60)
    print("INTERPOLATING TO REGULAR GRID")
    print("=" * 60)

    # Build the target grid
    lons_1d = np.arange(LON_WEST, LON_EAST, GRID_SPACING)
    lats_1d = np.arange(LAT_SOUTH, LAT_NORTH, GRID_SPACING)
    lon_grid, lat_grid = np.meshgrid(lons_1d, lats_1d)

    print(f"Grid dimensions: {len(lats_1d)} lat x {len(lons_1d)} lon "
          f"= {len(lats_1d) * len(lons_1d):,} cells")
    print(f"Grid spacing: {GRID_SPACING}° ({GRID_SPACING * 111:.0f} km at equator)")

    # Extract station coordinates as arrays
    station_lons = np.array([s["lon"] for s in all_stations])
    station_lats = np.array([s["lat"] for s in all_stations])
    station_points = np.column_stack([station_lons, station_lats])

    # Build the normals matrix: shape (num_stations, 365)
    station_normals = np.array([s["normals"] for s in all_stations])

    # Allocate the output grid: shape (365, num_lats, num_lons)
    normals_grid = np.full((365, len(lats_1d), len(lons_1d)), np.nan)

    print(f"\nInterpolating 365 days across {len(all_stations)} stations...")
    print("(This takes a few minutes — one interpolation per day of year)\n")

    for day in tqdm(range(365), desc="Days", unit="day"):
        # Get the normal value at each station for this day of year
        values = station_normals[:, day]

        # Use 'linear' interpolation. 'cubic' is smoother but can produce
        # wild extrapolation artifacts at CONUS edges. Linear is safer.
        # The nearest-neighbor fill handles any grid cells outside the
        # convex hull of the station network (coastal edges, etc.)
        try:
            interpolated = griddata(
                station_points,          # Known locations (lon, lat)
                values,                  # Known values at those locations
                (lon_grid, lat_grid),    # Target grid
                method="linear"
            )

            # Fill any remaining NaN cells (outside convex hull) with
            # nearest-neighbor interpolation
            nan_mask = np.isnan(interpolated)
            if np.any(nan_mask):
                nearest = griddata(
                    station_points,
                    values,
                    (lon_grid, lat_grid),
                    method="nearest"
                )
                interpolated[nan_mask] = nearest[nan_mask]

            # Light Gaussian smoothing to remove interpolation noise.
            # sigma=1.0 means the smoothing kernel is about 1 grid cell wide.
            # This is subtle — it just takes the edge off without blurring features.
            interpolated = gaussian_filter(interpolated, sigma=1.0)

            normals_grid[day, :, :] = interpolated

        except Exception as e:
            print(f"\n  WARNING: Interpolation failed for day {day + 1}: {e}")
            # Leave as NaN — daily_run.py will handle missing cells

    # Report on data quality
    total_cells = 365 * len(lats_1d) * len(lons_1d)
    nan_cells = np.sum(np.isnan(normals_grid))
    print(f"\nGrid complete. NaN cells: {nan_cells:,} / {total_cells:,} "
          f"({100 * nan_cells / total_cells:.1f}%)")

    return lons_1d, lats_1d, normals_grid


# =============================================================================
# STEP 3: SAVE AS NetCDF
# =============================================================================

def save_climatology(lons, lats, normals_grid):
    """
    Save the interpolated climatology grid as a NetCDF file.

    NetCDF is the standard file format for gridded climate data. It's self-
    describing (the file contains metadata about what's in it) and efficient
    for large arrays. The xarray library makes it very easy to work with.

    Parameters
    ----------
    lons : numpy.ndarray
        1-D array of longitude values
    lats : numpy.ndarray
        1-D array of latitude values
    normals_grid : numpy.ndarray
        3-D array of shape (365, len(lats), len(lons))
    """
    import xarray as xr

    print("\n" + "=" * 60)
    print("SAVING NetCDF FILE")
    print("=" * 60)

    # Day-of-year coordinate: 1 through 365
    doy = np.arange(1, 366)

    # Build an xarray DataArray with proper coordinates and metadata
    da = xr.DataArray(
        data=normals_grid,
        dims=["day_of_year", "latitude", "longitude"],
        coords={
            "day_of_year": doy,
            "latitude": lats,
            "longitude": lons
        },
        attrs={
            "long_name": "1991-2020 Daily Normal Maximum Temperature",
            "units": "degF",
            "source": "RCC-ACIS station normals, interpolated to regular grid",
            "grid_spacing": f"{GRID_SPACING} degrees",
            "interpolation_method": "linear with nearest-neighbor gap filling",
            "smoothing": "Gaussian sigma=1.0 grid cells",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "notes": "Day-of-year 1 = January 1, 365 = December 31 (no leap day)"
        }
    )

    # Wrap in a Dataset and save
    ds = xr.Dataset({"normal_maxt": da})
    ds.to_netcdf(CLIMATOLOGY_FILE)

    size_mb = os.path.getsize(CLIMATOLOGY_FILE) / (1024 * 1024)
    print(f"Saved to: {CLIMATOLOGY_FILE}")
    print(f"File size: {size_mb:.1f} MB")
    print("Done! This file is ready for the daily runner.")


# =============================================================================
# TEST MODE
# =============================================================================

def run_test():
    """
    Quick test: query one state and show exactly what the ACIS API returns.

    Run this first to make sure everything is working before committing
    to the full 15-minute download. If the output looks wrong, we'll know
    the API format needs adjusting.
    """
    print("=" * 60)
    print("TEST MODE — Querying Kansas as a test")
    print("=" * 60)
    print(f"API endpoint: {ACIS_URL}\n")

    # Make the same kind of request we'd make in the full build,
    # but just for one state
    payload = {
        "state": "KS",
        "sdate": "2019-01-01",
        "edate": "2019-12-31",
        "elems": [{"name": "maxt", "normal": "1"}],
        "meta": ["name", "state", "ll", "sids"]
    }

    print("Sending request...")
    print(f"Payload: {json.dumps(payload, indent=2)}\n")

    try:
        response = requests.post(
            ACIS_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        print(f"ERROR: Request failed: {e}")
        print("\nPossible causes:")
        print("  - No internet connection")
        print("  - ACIS server is down (try again later)")
        print("  - Firewall blocking HTTP requests")
        sys.exit(1)

    # Show what we got back
    if "data" not in result:
        print("UNEXPECTED RESPONSE — no 'data' key found.")
        print(f"Response keys: {list(result.keys())}")
        print(f"Full response (first 1000 chars):")
        print(json.dumps(result, indent=2)[:1000])
        sys.exit(1)

    num_stations = len(result["data"])
    print(f"SUCCESS! Got {num_stations} stations from Kansas.\n")

    # Show details for the first 3 stations
    print("-" * 60)
    for i, station in enumerate(result["data"][:3]):
        meta = station.get("meta", {})
        data = station.get("data", [])

        print(f"Station {i + 1}: {meta.get('name', 'Unknown')}")
        print(f"  State: {meta.get('state', '?')}")
        print(f"  Coordinates: {meta.get('ll', 'N/A')}")
        print(f"  Station IDs: {meta.get('sids', 'N/A')}")
        print(f"  Number of data points: {len(data)}")

        if len(data) > 0:
            print(f"  First 5 values (Jan 1-5): {data[:5]}")
            print(f"  Mid-year values (Jul 1-5): {data[181:186]}")
            print(f"  Last 5 values (Dec 27-31): {data[-5:]}")
        print()

    # Try parsing all Kansas stations as we would in the full build
    print("-" * 60)
    print("Attempting to parse all Kansas stations...\n")
    parsed = fetch_state_normals("KS")
    print(f"\nSuccessfully parsed {len(parsed)} stations with full 365-day normals.")

    if len(parsed) > 0:
        sample = parsed[0]
        normals = sample["normals"]
        print(f"\nSample station: {sample['name']}")
        print(f"  Location: ({sample['lat']:.4f}°N, {sample['lon']:.4f}°W)")
        print(f"  Jan 1 normal high: {normals[0]:.1f}°F")
        print(f"  Jul 1 normal high: {normals[181]:.1f}°F")
        print(f"  Annual min normal: {min(normals):.1f}°F")
        print(f"  Annual max normal: {max(normals):.1f}°F")
        print(f"\n  (These should look like reasonable temperatures for Kansas)")
        print(f"   Jan around 30-45°F, Jul around 90-100°F = good")
        print(f"   Anything wildly outside that range = something is wrong)")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
    if len(parsed) > 20:
        print("✓ Everything looks good. You're ready to run the full build:")
        print("  python build_climatology.py")
    else:
        print("⚠ Something may be off — expected more parseable stations.")
        print("  Check the raw data output above for clues.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    # Set up command-line arguments
    parser = argparse.ArgumentParser(
        description="Build a gridded climatology of daily normal high temperatures."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a quick test query against one state to verify the API works"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip the download step and use cached station data (if available)"
    )
    args = parser.parse_args()

    # TEST MODE: just poke the API and show what comes back
    if args.test:
        run_test()
        return

    # FULL BUILD MODE
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     TEMPERATURE CALENDAR — CLIMATOLOGY GRID BUILDER    ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  This builds a 365-day gridded normal high temperature  ║")
    print("║  dataset for the continental US. Run once, keep forever.║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # Create directories if they don't exist
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(CLIMATOLOGY_FILE) or ".", exist_ok=True)

    cache_file = os.path.join(CACHE_DIR, "acis_station_normals.json")

    # STEP 1: Get station data (download or load from cache)
    if args.skip_download and os.path.exists(cache_file):
        print(f"Loading cached station data from {cache_file}...")
        with open(cache_file, "r") as f:
            cache_data = json.load(f)
        all_stations = cache_data["stations"]
        print(f"Loaded {len(all_stations)} stations from cache.\n")
    else:
        all_stations = download_all_normals(cache_file)

    if len(all_stations) < 100:
        print(f"\nERROR: Only got {len(all_stations)} stations. Expected 3000-8000.")
        print("Something went wrong with the ACIS download.")
        print("Try running with --test first to diagnose.")
        sys.exit(1)

    # STEP 2: Interpolate to grid
    lons, lats, normals_grid = build_grid(all_stations)

    # STEP 3: Save as NetCDF
    save_climatology(lons, lats, normals_grid)

    print("\n" + "=" * 60)
    print("ALL DONE!")
    print("=" * 60)
    print(f"Your climatology file is at: {CLIMATOLOGY_FILE}")
    print(f"Grid shape: {normals_grid.shape} (days × lat × lon)")
    print(f"\nNext step: test the daily runner with:")
    print(f"  python daily_run.py")


if __name__ == "__main__":
    main()
