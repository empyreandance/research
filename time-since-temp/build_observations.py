#!/usr/bin/env python3
"""
build_observations.py — Build a historical daily MaxT/MinT observation cube.

Pulls daily maximum and minimum temperature observations from RCC-ACIS
MultiStnData, state by state, for the full requested period. Interpolates
each day's station values onto the 0.25° climatology grid. Saves the result
as two NetCDF cubes that the streak-map daily runner uses to answer:

    "When was the last time this location actually saw a temperature
     at least this high (or at most this low)?"

Two output files (paths in config.py):
    observations_maxt.nc    daily max temp, °F, (time × lat × lon)
    observations_mint.nc    daily min temp, °F, (time × lat × lon)

We chose state-by-state station queries instead of one big GridData pull
because ACIS rate-limits GridData responses aggressively when the bbox is
large. MultiStnData returns small per-state responses (~500 KB) that the
server processes quickly and we can pace politely. The downside is that we
have to interpolate from stations to a regular grid ourselves — but that's
exactly what build_climatology.py does for the 1991-2020 normals, so this
script mirrors that pattern. The two builders stay consistent.

YOU RUN THIS ONCE for the full historical period. After it finishes, the
daily runner appends one new time slice each morning. The full ~47-year
pull takes roughly 2-4 hours wall clock — mostly polite API pacing.

Usage:
    python build_observations.py --test           # Verify the MultiStnData query works
    python build_observations.py --years 1        # Most recent year only (~10-15 min)
    python build_observations.py --years 5        # Last five years
    python build_observations.py                  # Full record (1979 → yesterday)
    python build_observations.py --clear-checkpoints   # Force fresh start

How the build works:
    1. Walk the requested period one YEAR at a time.
    2. For each year, query each of the 49 CONUS states + DC for the
       full year's daily maxt or mint via MultiStnData. One call per
       state per element. Combine into a single set of stations with
       per-day values.
    3. For each day in the year, interpolate the valid stations'
       values onto the 0.25° climatology grid (linear pass then
       nearest-neighbor fill, same approach as the climatology builder).
    4. Save a yearly checkpoint NetCDF to CACHE_DIR.
    5. After the last year is in, merge all yearly checkpoints into the
       final observations_maxt.nc and observations_mint.nc.
"""

import os
import sys
import time
import argparse
import warnings
import shutil
from datetime import datetime, date, timedelta

import numpy as np
import xarray as xr
import requests
from tqdm import tqdm

from config import (
    LON_WEST, LON_EAST, LAT_SOUTH, LAT_NORTH, GRID_SPACING,
    PROJECT_DIR, CACHE_DIR,
    OBS_FILE_MAXT, OBS_FILE_MINT, OBS_CHECKPOINT_DIR,
    OBS_PERIOD_START, ACIS_URL,
    STATES, API_DELAY, API_MAX_RETRIES,
)

# Suppress some xarray / NetCDF chatter that's not useful here
warnings.filterwarnings("ignore", category=FutureWarning)


# =============================================================================
# STEP 1: PER-STATE MultiStnData QUERY
# =============================================================================

def fetch_state_observations_multi(state, elements, start_date, end_date):
    """
    Query ACIS MultiStnData for one state across a date range, requesting
    multiple elements (e.g., maxt AND mint) in a single call.

    For multi-element requests, each station's daily values are returned as
    lists with one entry per requested element, in the order they were
    requested. So if elements=["maxt", "mint"], each day's entry looks like
    ["75.0", "55.0"] — first element is maxt, second is mint.

    Parameters
    ----------
    state : str
        Two-letter state abbreviation (e.g., "KS", "CA").
    elements : list of str
        List of element names, e.g., ["maxt"], ["maxt", "mint"].
    start_date, end_date : date
        Inclusive date range.

    Returns
    -------
    dict
        Maps each element name to a list of station dicts, where each
        station dict has: name, lat, lon, daily (numpy.ndarray of float32).
        Empty lists on API failure.
    """
    n_days = (end_date - start_date).days + 1

    payload = {
        "state": state,
        "sdate": start_date.strftime("%Y-%m-%d"),
        "edate": end_date.strftime("%Y-%m-%d"),
        "elems": [{"name": e} for e in elements],
        "meta": ["name", "state", "ll", "sids"]
    }

    result = None
    for attempt in range(API_MAX_RETRIES):
        try:
            response = requests.post(
                ACIS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120
            )
            if response.status_code == 429:
                wait = 30 * (attempt + 1)
                tqdm.write(f"    {state}: 429 rate-limited. Sleeping {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            result = response.json()
            break
        except requests.exceptions.RequestException as e:
            tqdm.write(f"    {state} attempt {attempt + 1}/{API_MAX_RETRIES} failed: {e}")
            if attempt < API_MAX_RETRIES - 1:
                time.sleep(10)
            else:
                tqdm.write(f"    GIVING UP on {state} after {API_MAX_RETRIES} attempts.")
                return {e: [] for e in elements}

    if result is None:
        return {e: [] for e in elements}

    if "data" not in result:
        tqdm.write(f"    {state}: unexpected response keys = {list(result.keys())}")
        if "error" in result:
            tqdm.write(f"    ACIS error: {result['error']}")
        return {e: [] for e in elements}

    output = {e: [] for e in elements}

    for station in result["data"]:
        meta = station.get("meta", {})
        data = station.get("data", [])

        ll = meta.get("ll")
        if ll is None or len(ll) < 2:
            continue
        lon, lat = ll[0], ll[1]

        if not (LON_WEST <= lon <= LON_EAST and LAT_SOUTH <= lat <= LAT_NORTH):
            continue

        # One array per element, filled with NaN; we'll overwrite valid days.
        per_element_daily = {
            e: np.full(n_days, np.nan, dtype=np.float32) for e in elements
        }

        for i, day_values in enumerate(data[:n_days]):
            if not isinstance(day_values, list):
                continue
            for j, elem_name in enumerate(elements):
                if j >= len(day_values):
                    continue
                val = day_values[j]
                if val in ("M", "T", "", None, "None", "-999"):
                    continue
                try:
                    per_element_daily[elem_name][i] = float(val)
                except (ValueError, TypeError):
                    continue

        station_name = meta.get("name", "Unknown")
        for elem_name in elements:
            output[elem_name].append({
                "name": station_name,
                "lat": lat,
                "lon": lon,
                "daily": per_element_daily[elem_name],
            })

    return output


def fetch_state_observations(state, element, start_date, end_date):
    """
    Single-element wrapper around fetch_state_observations_multi. Kept so
    the test mode and any single-element callers don't need to change.

    Returns
    -------
    list of dict
        Station records for the one requested element.
    """
    result = fetch_state_observations_multi(state, [element], start_date, end_date)
    return result[element]


# =============================================================================
# STEP 2: INTERPOLATE ONE DAY'S STATIONS TO THE 0.25° GRID
# =============================================================================

def build_climatology_grid_coords():
    """
    Build the 1-D longitude and latitude arrays for the climatology grid.

    Mirrors how build_climatology.py constructs the grid so the streak cube
    ends up co-registered with the climatology cube.

    Returns
    -------
    (numpy.ndarray, numpy.ndarray)
        (lons_1d, lats_1d) — both in ascending order.
    """
    lons = np.arange(LON_WEST, LON_EAST, GRID_SPACING, dtype=np.float32)
    lats = np.arange(LAT_SOUTH, LAT_NORTH, GRID_SPACING, dtype=np.float32)
    return lons, lats


def interpolate_day_to_grid(stations, day_idx, target_lats, target_lons,
                            min_valid_stations=20):
    """
    Interpolate one day's worth of valid station values onto the target grid.

    Same approach as build_climatology.py's per-day step:
        1. Build a list of station (lon, lat) points and values for those
           that reported a finite value on this day.
        2. Linear interpolation (scipy.interpolate.griddata) onto the grid.
        3. Nearest-neighbor fill for cells outside the convex hull of the
           reporting stations (coastal edges, etc.).

    Parameters
    ----------
    stations : list of dict
        From fetch_state_observations() across all states for the year.
    day_idx : int
        Index into each station's `daily` array.
    target_lats, target_lons : numpy.ndarray
        1-D climatology grid coordinates.
    min_valid_stations : int
        If fewer stations than this reported a finite value on this day, give
        up and return an all-NaN grid. Defensive against catastrophic gaps.

    Returns
    -------
    numpy.ndarray
        Shape (len(target_lats), len(target_lons)), dtype float32. All-NaN if
        too few stations reported on this day.
    """
    from scipy.interpolate import griddata

    # Pull the valid (lon, lat, value) triples for this day
    lons = []
    lats = []
    vals = []
    for s in stations:
        v = s["daily"][day_idx]
        if not np.isnan(v):
            lons.append(s["lon"])
            lats.append(s["lat"])
            vals.append(v)

    out_shape = (len(target_lats), len(target_lons))
    if len(vals) < min_valid_stations:
        return np.full(out_shape, np.nan, dtype=np.float32)

    points = np.column_stack([lons, lats]).astype(np.float32)
    values = np.array(vals, dtype=np.float32)

    lon_grid, lat_grid = np.meshgrid(target_lons, target_lats)

    # Linear interpolation inside the convex hull of the station network
    result = griddata(points, values, (lon_grid, lat_grid), method="linear")

    # Nearest-neighbor fill for cells outside the convex hull (coastal,
    # corner cells, anywhere with sparse coverage)
    nan_mask = np.isnan(result)
    if nan_mask.any():
        result_nearest = griddata(points, values, (lon_grid, lat_grid),
                                  method="nearest")
        result[nan_mask] = result_nearest[nan_mask]

    return result.astype(np.float32)


# =============================================================================
# STEP 3: PER-YEAR BUILD WITH CHECKPOINTING
# =============================================================================

def checkpoint_path(year, element):
    """Path to the yearly checkpoint file for a given element."""
    return os.path.join(OBS_CHECKPOINT_DIR, f"obs_{element}_{year}.nc")


def build_one_year(year, target_lats, target_lons, elements_to_build,
                   first_date=None, last_date=None, workers=4):
    """
    Pull a whole year of station observations across all CONUS states for
    one or more elements, interpolate each day to the climatology grid, and
    save a yearly checkpoint NetCDF per element.

    Two key efficiencies vs the original implementation:
        1. Both elements (maxt + mint) are pulled in the SAME ACIS call,
           halving the request count.
        2. State queries are dispatched concurrently across a thread pool,
           so wall clock time is divided by approximately the worker count.

    Skips any element whose checkpoint file already exists. If all requested
    elements are already on disk, returns immediately.

    Parameters
    ----------
    year : int
        Calendar year to pull.
    target_lats, target_lons : numpy.ndarray
        Climatology grid coordinates.
    elements_to_build : list of str
        Subset of ["maxt", "mint"] — typically both.
    first_date, last_date : date, optional
        Truncate to this range at period boundaries.
    workers : int
        Thread pool size for concurrent state fetches.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Filter to elements that don't yet have a checkpoint
    pending = [e for e in elements_to_build
               if not os.path.exists(checkpoint_path(year, e))]
    if not pending:
        return  # Both already on disk

    # Determine the year's effective range
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    if first_date and first_date > year_start:
        year_start = first_date
    if last_date and last_date < year_end:
        year_end = last_date

    n_days = (year_end - year_start).days + 1
    if n_days <= 0:
        return

    # Worker function — runs once per state in the thread pool. Pulls both
    # pending elements in a single ACIS call, applies per-station length
    # normalization, sleeps the politeness delay, returns the result. The
    # sleeps happen in parallel across workers so they're effectively free.
    def _fetch_one_state(state):
        result = fetch_state_observations_multi(state, pending, year_start, year_end)
        for elem in pending:
            for s in result[elem]:
                if len(s["daily"]) != n_days:
                    fixed = np.full(n_days, np.nan, dtype=np.float32)
                    copy_len = min(len(s["daily"]), n_days)
                    fixed[:copy_len] = s["daily"][:copy_len]
                    s["daily"] = fixed
        time.sleep(API_DELAY)
        return state, result

    # Accumulate stations per element
    stations_by_element = {e: [] for e in pending}

    elements_label = "+".join(pending)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_one_state, st): st for st in STATES}
        progress = tqdm(
            as_completed(futures),
            total=len(STATES),
            desc=f"  {year} {elements_label} states (×{workers} workers)",
            unit="state",
            leave=False
        )
        for future in progress:
            try:
                state, state_result = future.result()
                for elem in pending:
                    stations_by_element[elem].extend(state_result[elem])
            except Exception as e:
                state = futures.get(future, "?")
                tqdm.write(f"    Worker failed for {state}: {e}")

    # Build, interpolate, and save each pending element separately
    for elem in pending:
        all_stations = stations_by_element[elem]
        if len(all_stations) < 100:
            tqdm.write(f"    Year {year} {elem}: only {len(all_stations)} stations. "
                       f"Skipping (insufficient coverage).")
            continue

        tqdm.write(f"    Year {year} {elem}: {len(all_stations)} stations, "
                   f"{n_days} days — interpolating each day to grid...")

        year_grid = np.full((n_days, len(target_lats), len(target_lons)),
                            np.nan, dtype=np.float32)
        day_iter = tqdm(range(n_days),
                        desc=f"  {year} {elem} days",
                        unit="day", leave=False)
        for d in day_iter:
            year_grid[d] = interpolate_day_to_grid(all_stations, d,
                                                   target_lats, target_lons)

        times = np.array([np.datetime64(year_start + timedelta(days=d))
                          for d in range(n_days)], dtype="datetime64[D]")

        da = xr.DataArray(
            data=year_grid,
            dims=["time", "latitude", "longitude"],
            coords={
                "time": times,
                "latitude": target_lats,
                "longitude": target_lons
            },
            attrs={
                "long_name": (f"Daily {'Maximum' if elem == 'maxt' else 'Minimum'} "
                              f"Temperature"),
                "units": "degF",
                "source": (f"RCC-ACIS MultiStnData, station observations interpolated "
                           f"to a {GRID_SPACING}° grid (linear with nearest-neighbor "
                           f"gap fill)."),
                "year": year,
                "n_stations": len(all_stations),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        ds = xr.Dataset({elem: da})
        encoding = {elem: {"zlib": True, "complevel": 5, "dtype": "float32"}}

        out_path = checkpoint_path(year, elem)
        os.makedirs(OBS_CHECKPOINT_DIR, exist_ok=True)
        ds.to_netcdf(out_path, encoding=encoding)

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        tqdm.write(f"    Saved {out_path} ({size_mb:.1f} MB, {n_days} days, "
                   f"{len(all_stations)} stations)")


# =============================================================================
# STEP 4: MERGE YEARLY CHECKPOINTS INTO THE MASTER CUBE
# =============================================================================

def merge_checkpoints(element, output_path):
    """
    Combine all yearly checkpoint files for an element into a single
    compressed NetCDF cube.

    Uses explicit per-file open + xr.concat instead of xr.open_mfdataset
    so we don't need dask as a dependency.
    """
    import glob
    pattern = os.path.join(OBS_CHECKPOINT_DIR, f"obs_{element}_*.nc")
    files = sorted(glob.glob(pattern))
    print(f"\nMerging {len(files)} files → {output_path}")

    if len(files) == 0:
        print(f"  No checkpoint files matched {pattern}. Nothing to merge.")
        return

    datasets = [xr.open_dataset(f) for f in files]
    ds = xr.concat(datasets, dim="time").sortby("time")

    encoding = {element: {"zlib": True, "complevel": 5, "dtype": "float32"}}
    ds.to_netcdf(output_path, encoding=encoding)

    for d in datasets:
        d.close()
    ds.close()

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Saved {output_path} ({size_mb:.1f} MB)")


# =============================================================================
# TEST MODE
# =============================================================================

def run_test():
    """
    Quick MultiStnData query against one state for a few recent days. Verifies
    the API responds, the response shape matches what we parse, and the
    interpolation step produces reasonable values.
    """
    print("=" * 60)
    print("TEST MODE — ACIS MultiStnData sanity check")
    print("=" * 60)
    print(f"Endpoint: {ACIS_URL}")
    print(f"Method:   station-by-station, state-batched")

    test_end = date.today() - timedelta(days=3)
    test_start = test_end - timedelta(days=2)
    print(f"Querying Kansas for {test_start} → {test_end} (maxt)")

    stations = fetch_state_observations("KS", "maxt", test_start, test_end)
    print(f"\nReturned {len(stations)} stations within the CONUS bbox.\n")

    if len(stations) == 0:
        print("ERROR: no stations returned. Likely an API problem.")
        sys.exit(1)

    # Show details for the first 3 stations
    print("-" * 60)
    for i, s in enumerate(stations[:3]):
        print(f"Station {i + 1}: {s['name']}")
        print(f"  Coordinates: ({s['lat']:.4f}°N, {s['lon']:.4f}°W)")
        print(f"  Daily values: {s['daily'].tolist()}")
        print()

    # Summary stats across all stations
    n_days = stations[0]["daily"].shape[0]
    print(f"Daily coverage across all {len(stations)} KS stations:")
    for d in range(n_days):
        day_vals = np.array([s["daily"][d] for s in stations])
        valid = day_vals[~np.isnan(day_vals)]
        day = test_start + timedelta(days=d)
        if len(valid) > 0:
            print(f"  {day}: {len(valid)} stations reporting, "
                  f"range {valid.min():.0f}°F → {valid.max():.0f}°F, "
                  f"mean {valid.mean():.1f}°F")
        else:
            print(f"  {day}: no valid reports")

    # Test the interpolation step on the first day
    print("\n" + "-" * 60)
    print("Testing interpolation to the 0.25° grid...")
    target_lons, target_lats = build_climatology_grid_coords()
    day0_grid = interpolate_day_to_grid(stations, 0, target_lats, target_lons,
                                        min_valid_stations=5)
    valid_cells = day0_grid[~np.isnan(day0_grid)]
    if len(valid_cells) > 0:
        print(f"  Interpolated shape: {day0_grid.shape}")
        print(f"  Valid cells: {len(valid_cells)}/{day0_grid.size} "
              f"({100 * len(valid_cells) / day0_grid.size:.0f}%)")
        print(f"  Interpolated value range: "
              f"{valid_cells.min():.1f}°F → {valid_cells.max():.1f}°F")
        print(f"  (Note: only Kansas stations were used here, so only cells "
              f"inside KS are physically meaningful; everywhere else comes "
              f"from nearest-neighbor fill. The real build uses all 49 states.)")
    else:
        print("  WARNING: interpolated grid is all NaN.")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
    print("If Kansas temperatures look reasonable for late May")
    print("(typically highs in the 70s–90s°F), the pipeline is working.")
    print("You can now run:")
    print("    python build_observations.py --years 1     # ~10-15 min")
    print("    python build_observations.py               # full ~47-year build")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Build the historical observation cube for the streak maps."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Quick sanity check against ACIS without doing a full build."
    )
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help=("Only build the most recent N years (counted back from yesterday). "
              "Useful for testing or for a lightweight installation. "
              "Default: build the full record from %s onward." % OBS_PERIOD_START)
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=("(Default behavior anyway.) Resume an interrupted build by "
              "skipping years whose checkpoint files already exist.")
    )
    parser.add_argument(
        "--clear-checkpoints",
        action="store_true",
        help="Delete all yearly checkpoint files before building. Forces a fresh start."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help=("Number of concurrent state-fetch threads. Default: 4. "
              "Set to 1 for sequential. Higher values speed up the build "
              "but risk ACIS rate-limiting; 4-8 is usually safe.")
    )
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    # --- Setup ---
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   STREAK CUBE BUILDER — Historical Daily Observations   ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  Pulls daily MaxT and MinT from RCC-ACIS MultiStnData,  ║")
    print("║  state by state, interpolates each day to the 0.25°     ║")
    print("║  climatology grid, saves NetCDF.                        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OBS_CHECKPOINT_DIR, exist_ok=True)

    if args.clear_checkpoints:
        if os.path.isdir(OBS_CHECKPOINT_DIR):
            print(f"Clearing {OBS_CHECKPOINT_DIR}...")
            shutil.rmtree(OBS_CHECKPOINT_DIR)
            os.makedirs(OBS_CHECKPOINT_DIR, exist_ok=True)

    # --- Build the target grid (climatology grid) ---
    target_lons, target_lats = build_climatology_grid_coords()
    print(f"Target grid: {len(target_lats)} lats × {len(target_lons)} lons "
          f"at {GRID_SPACING}°")

    # --- Determine the period of record ---
    yesterday = date.today() - timedelta(days=1)
    period_start = datetime.strptime(OBS_PERIOD_START, "%Y-%m-%d").date()

    if args.years is not None:
        period_start = max(period_start, date(yesterday.year - args.years + 1, 1, 1))

    period_end = yesterday
    print(f"Period of record: {period_start} → {period_end}")
    n_years = period_end.year - period_start.year + 1
    print(f"Years to pull: {n_years} ({period_start.year} → {period_end.year})")
    print(f"Concurrent workers: {args.workers}")
    print()

    # --- Build year by year, pulling both elements per state in one call ---
    elements = ["maxt", "mint"]
    print("=" * 60)
    print("BUILDING MAXT + MINT (combined per-state calls)")
    print("=" * 60)

    for year in tqdm(range(period_start.year, period_end.year + 1),
                     desc="years", unit="year"):
        first = period_start if year == period_start.year else None
        last = period_end if year == period_end.year else None
        build_one_year(year, target_lats, target_lons, elements,
                       first_date=first, last_date=last,
                       workers=args.workers)

    print()

    # --- Merge yearly checkpoints into the master cubes ---
    print("=" * 60)
    print("MERGING YEARLY CHECKPOINTS")
    print("=" * 60)
    merge_checkpoints("maxt", OBS_FILE_MAXT)
    merge_checkpoints("mint", OBS_FILE_MINT)

    print()
    print("=" * 60)
    print("ALL DONE")
    print("=" * 60)
    print(f"  {OBS_FILE_MAXT}")
    print(f"  {OBS_FILE_MINT}")
    print()
    print("Next: daily_run.py (after the streak compute is wired in)")


if __name__ == "__main__":
    main()
