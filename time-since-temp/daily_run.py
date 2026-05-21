#!/usr/bin/env python3
"""
daily_run.py — Daily orchestrator for the Time Since Temperature streak maps.

What this script does, in order:

    1. Refresh data (sliding-window).
       Re-pulls the last N days of observations from ACIS (default 30),
       overwrites those slices in observations_maxt.nc / observations_mint.nc,
       then rebuilds last_dates_maxt.nc / last_dates_mint.nc from the updated
       cubes via build_threshold_index. Idempotent — any ACIS revisions to
       preliminary data within the sliding window flow through cleanly.
       Past the window, cube data is treated as final.

    2. Download today's NDFD forecast (ds.maxt.bin and ds.mint.bin).

    3. Parse GRIB2 and extract today's forecast field (with K vs °C vs °F
       sanity checks).

    4. Regrid forecast to the 0.25° climatology grid (scipy griddata,
       linear + nearest fill).

    5. Compute streak: for each cell, look up
       last_dates[bin_for_forecast, lat, lon] and subtract from today_idx.
       Sentinel cells (threshold never reached in record) flagged for hatching.

    6. Apply CONUS land mask (Natural Earth admin_0_countries) so offshore
       and Great Lakes cells, which hold extrapolated garbage, don't render.

    7. Render two map products (high streak, low streak), each as:
       - National PNG at 300 DPI (for live serving)
       - National JPG at 150 DPI (for the archive)
       - 8 regional PNGs at 300 DPI

       CONUS shape comes from the natural NaN pattern of the streak field
       plus the land mask, not from any shapely clip path. The colorbar
       scales dynamically based on the 99th percentile of the smoothed
       field, rounded up to a "nice" cap.

Usage:
    python daily_run.py                              # Today's run, all defaults
    python daily_run.py --date 2026-05-20            # Back-date for testing
    python daily_run.py --skip-update                # Skip the ACIS refresh
    python daily_run.py --test                       # Synthetic forecast (1yr-ago obs)
    python daily_run.py --sliding-window 14          # Smaller refresh window
"""

import os
import sys
import time
import argparse
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import xarray as xr
import requests
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.io.shapereader as shpreader
import shapely
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

from config import (
    PROJECT_DIR,
    OUTPUT_DIR,
    REGIONS,
    STREAK_SMOOTH_SIGMA,
    STREAK_LABEL_STRIDE,
)

from build_observations import (
    interpolate_day_to_grid,
    build_climatology_grid_coords,
)

from build_threshold_index import (
    BIN_MIN_F, BIN_MAX_F, BIN_STEP_F, N_BINS, EPOCH, SENTINEL,
    THRESHOLDS_FILE_MAXT, THRESHOLDS_FILE_MINT,
    build_threshold_index as rebuild_threshold_index,
)

# Paths to the observation cubes (read/updated by the sliding-window refresh).
# Derived from PROJECT_DIR rather than imported from config to keep this
# module's dependencies on config minimal.
OBS_FILE_MAXT = os.path.join(PROJECT_DIR, "observations_maxt.nc")
OBS_FILE_MINT = os.path.join(PROJECT_DIR, "observations_mint.nc")

# Plain-date companion to the numpy datetime64 EPOCH, used for date arithmetic
# without the numpy-vs-stdlib-date conversion gymnastics.
EPOCH_DATE = date(1979, 1, 1)


# =============================================================================
# CONSTANTS — operational settings
# =============================================================================

# NDFD forecast URLs (CONUS, 1-3 day forecast period, GRIB2)
NDFD_BASE = (
    "https://tgftp.nws.noaa.gov/SL.us008001/ST.opnl/DF.gr2/"
    "DC.ndfd/AR.conus/VP.001-003/"
)
NDFD_URLS = {
    "maxt": NDFD_BASE + "ds.maxt.bin",
    "mint": NDFD_BASE + "ds.mint.bin",
}

# Output paths (live "latest" files for the website)
OUTPUT_HIGH_LATEST = os.path.join(OUTPUT_DIR, "temp_streak_high_latest.png")
OUTPUT_LOW_LATEST  = os.path.join(OUTPUT_DIR, "temp_streak_low_latest.png")

# Archive directory (date-stamped JPGs)
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "archive")

# Regional output directory
REGIONS_DIR = os.path.join(OUTPUT_DIR, "regions")

# Sequential colormaps (white → saturated red for high, white → blue for low)
# Endpoints chosen for high contrast and readability against state lines.
HIGH_COLORS = [
    "#ffffff", "#fee5d9", "#fcbba1", "#fc9272", "#fb6a4a",
    "#ef3b2c", "#cb181d", "#a50f15", "#67000d",
]
LOW_COLORS = [
    "#ffffff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6",
    "#4292c6", "#2171b5", "#08519c", "#08306b",
]

CMAP_HIGH = LinearSegmentedColormap.from_list("streak_high", HIGH_COLORS, N=256)
CMAP_LOW  = LinearSegmentedColormap.from_list("streak_low",  LOW_COLORS,  N=256)

# CONUS bounding box for the climatology grid
CONUS_LON_MIN, CONUS_LON_MAX = -125.0, -66.0
CONUS_LAT_MIN, CONUS_LAT_MAX = 24.0, 50.0

# Map output resolution (national PNG; archive JPG is 150)
MAP_DPI = 300


# =============================================================================
# CONUS LAND MASK
# =============================================================================

_LAND_MASK_CACHE = {}


def synthesize_test_forecast(target_date):
    """
    Build a synthetic forecast for --test mode by reading observations from
    exactly 365 days before target_date out of the cube. Fast (one chunk
    read per element) and produces realistic spatial structure that respects
    elevation, latitude, ocean influence, and every other local effect baked
    into the observation record.

    Streaks in the resulting map will hover around 365 days for typical
    cells, deviating by year-over-year temperature anomalies. Good for
    confirming the pipeline produces a sensible-looking map. Not a
    substitute for real NDFD when doing actual QC.

    Parameters
    ----------
    target_date : datetime.date
        The date the synthetic forecast is "valid" for.

    Returns
    -------
    fcst_high, fcst_low : 2-D float32 numpy arrays
    """
    sample_date = target_date - timedelta(days=365)
    print(f"  Reading observations from {sample_date} (365 days before "
          f"{target_date}) to use as synthetic forecast.")

    obs_maxt_path = os.path.join(PROJECT_DIR, "observations_maxt.nc")
    obs_mint_path = os.path.join(PROJECT_DIR, "observations_mint.nc")

    def load_one_day(cube_path, var_name):
        with xr.open_dataset(cube_path) as ds:
            time_dates = ds.time.values.astype("datetime64[D]")
            target = np.datetime64(sample_date)
            idx = np.where(time_dates == target)[0]
            if len(idx) == 0:
                raise RuntimeError(
                    f"No {var_name} observation in cube for {sample_date}. "
                    f"The cube must cover the date 365 days earlier than the "
                    f"target. Either pass --date with a date inside the "
                    f"covered range, or extend the cube."
                )
            return ds[var_name].isel(time=int(idx[0])).values.astype(np.float32)

    print(f"    Loading maxt...")
    fcst_high = load_one_day(obs_maxt_path, "maxt")
    print(f"    Loading mint...")
    fcst_low = load_one_day(obs_mint_path, "mint")

    for name, arr in [("high", fcst_high), ("low", fcst_low)]:
        finite = arr[np.isfinite(arr)]
        if len(finite) > 0:
            print(f"    Forecast {name} range: {finite.min():.1f} → "
                  f"{finite.max():.1f}°F  (median {np.median(finite):.1f}°F)")

    return fcst_high, fcst_low


def build_conus_land_mask(lons, lats):
    """
    Build a (n_lat, n_lon) boolean mask that's True where the grid cell falls
    on United States land per the 50m Natural Earth admin_0_countries polygon.

    The Natural Earth polygon treats the Great Lakes as water (not USA land),
    which is exactly what we want — lake cells in our threshold index hold
    nearest-neighbor-extrapolated values from the nearest land station, and
    we don't want to render that garbage.

    Cached by grid shape so repeated calls (e.g., one per render pass) are
    free after the first.

    Parameters
    ----------
    lons, lats : 1-D numpy arrays
        Grid coordinates. Cell at (i, j) is at (lats[i], lons[j]).

    Returns
    -------
    numpy.ndarray of bool, shape (len(lats), len(lons))
        True where the cell center is inside the USA land polygon.
    """
    key = (tuple(lons), tuple(lats))
    if key in _LAND_MASK_CACHE:
        return _LAND_MASK_CACHE[key]

    shp_path = shpreader.natural_earth(
        resolution="50m", category="cultural", name="admin_0_countries"
    )
    reader = shpreader.Reader(shp_path)
    usa_geom = None
    for rec in reader.records():
        if rec.attributes.get("ADMIN") == "United States of America":
            usa_geom = rec.geometry
            break
    if usa_geom is None:
        raise RuntimeError(
            "Could not find USA in Natural Earth admin_0_countries shapefile."
        )

    lon2d, lat2d = np.meshgrid(lons, lats)
    mask = shapely.contains_xy(
        usa_geom, lon2d.ravel(), lat2d.ravel()
    ).reshape(lon2d.shape)

    _LAND_MASK_CACHE[key] = mask
    return mask


# =============================================================================
# STEP 1 — SLIDING-WINDOW DATA REFRESH
# =============================================================================
#
# Each daily run re-pulls the last N days from ACIS (default 30), overwrites
# those slices in the observation cubes, and rebuilds the threshold index
# from scratch. This is idempotent: any ACIS revisions to preliminary data
# within the sliding window get picked up automatically. Past the window,
# data is treated as final and we don't re-touch it.
#
# Why we don't do an incremental update instead: the threshold index is a
# destructive representation — for each (bin, cell) it stores only the
# single most-recent date. If a recent observation gets revised after the
# fact (e.g., ACIS QC flags yesterday's stuck-sensor reading as bad), we
# can't roll back the threshold index without rebuilding because the
# "previous most-recent date" was discarded during the scatter. Full
# rebuild from the cube fixes this cleanly.


# 49 CONUS states + DC (ACIS uses 2-letter postal codes)
_CONUS_STATES = [
    "AL","AZ","AR","CA","CO","CT","DE","FL","GA","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD",
    "TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
]


# ACIS MultiStnData endpoint
_ACIS_URL = "https://data.rcc-acis.org/MultiStnData"


def _parse_acis_value(v):
    """
    Convert an ACIS-returned value to a float (or NaN).

    ACIS returns "M" for missing (already-filtered failed-QC values from
    GHCN-Daily), "T" for trace (precip only — shouldn't appear for temp but
    we handle it), occasionally values with flag suffixes like "82.4A"
    (accumulated). Anything that isn't a parseable number becomes NaN.
    """
    if v is None:
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.strip()
        if v in ("M", "T", "", "S"):
            return float("nan")
        # Strip trailing flag character if present (e.g., "82.4A" → "82.4")
        if v and not v[-1].isdigit() and v[-1] != ".":
            v = v[:-1]
        try:
            return float(v)
        except ValueError:
            return float("nan")
    return float("nan")


def fetch_state_window_acis(state, start_date, end_date, max_retries=5):
    """
    Pull maxt + mint observations for one state across a date window from
    ACIS MultiStnData. Direct call — does not depend on build_observations.

    Returns a list of station dicts, each shaped:
        {
            "lat": float,
            "lon": float,
            "maxt": [d1, d2, ..., dN],   # NaN for missing/flagged
            "mint": [d1, d2, ..., dN],
        }

    Handles 429 rate-limit responses with exponential backoff.
    """
    n_days = (end_date - start_date).days + 1
    params = {
        "state": state,
        "sdate": start_date.strftime("%Y-%m-%d"),
        "edate": end_date.strftime("%Y-%m-%d"),
        "elems": [
            {"name": "maxt", "interval": "dly"},
            {"name": "mint", "interval": "dly"},
        ],
        "meta": ["name", "ll"],
    }

    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                _ACIS_URL,
                json={"params": params},
                timeout=120,
            )
            if resp.status_code == 429:
                # Rate-limited; back off and retry
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            payload = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            continue
    else:
        # All retries exhausted
        raise RuntimeError(
            f"ACIS pull for {state} {start_date}→{end_date} failed: {last_exc}"
        )

    stations = []
    for record in payload.get("data", []):
        meta = record.get("meta", {})
        ll = meta.get("ll")
        if not ll or len(ll) != 2:
            continue
        lon, lat = float(ll[0]), float(ll[1])

        rows = record.get("data", [])
        if len(rows) != n_days:
            # Skip stations whose data array doesn't span the full window
            continue

        maxt_vals = []
        mint_vals = []
        for row in rows:
            # Each row is [maxt, mint] (we didn't ask for date in the row)
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                maxt_vals.append(float("nan"))
                mint_vals.append(float("nan"))
                continue
            maxt_vals.append(_parse_acis_value(row[0]))
            mint_vals.append(_parse_acis_value(row[1]))

        stations.append({
            "lat": lat,
            "lon": lon,
            "maxt": maxt_vals,
            "mint": mint_vals,
        })

    return stations


def pull_window_grids(start_date, end_date, target_lats, target_lons, workers=10):
    """
    Pull all days in [start_date, end_date] from ACIS in a single call per
    state (parallel across states), then interpolate each day to the grid.

    This is much faster than one ACIS call per day: 30 days × 49 states with
    10 workers takes about 30-60 seconds total, vs. several minutes if we
    issued daily calls.

    Returns
    -------
    dict mapping date → (maxt_grid, mint_grid)
        Each grid is a (n_lat, n_lon) float32 array with NaN where the cell
        has too few nearby stations.
    """
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    n_days = (end_date - start_date).days + 1

    print(f"    Pulling {start_str} → {end_str} ({n_days} days) "
          f"from {len(_CONUS_STATES)} states with {workers} workers...")

    # Each station carries an n_days-long values list, indexed from start_date.
    all_stations_maxt = []
    all_stations_mint = []

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_state_window_acis,
                state, start_date, end_date
            ): state
            for state in _CONUS_STATES
        }
        for future in as_completed(futures):
            state = futures[future]
            try:
                stations = future.result()
                for s in stations:
                    # Only accept stations whose values list matches the
                    # expected window length — protects against malformed
                    # responses with truncated time series. The key name
                    # "daily" matches what interpolate_day_to_grid expects.
                    if ("maxt" in s and s["maxt"] is not None
                            and len(s["maxt"]) == n_days):
                        all_stations_maxt.append({
                            "lat": s["lat"], "lon": s["lon"], "daily": s["maxt"]
                        })
                    if ("mint" in s and s["mint"] is not None
                            and len(s["mint"]) == n_days):
                        all_stations_mint.append({
                            "lat": s["lat"], "lon": s["lon"], "daily": s["mint"]
                        })
            except Exception as e:
                print(f"      WARN: {state} failed: {e}")

    elapsed = time.time() - t0
    print(f"      Got {len(all_stations_maxt)} maxt and {len(all_stations_mint)} "
          f"mint stations in {elapsed:.1f}s")

    print(f"    Interpolating each day to the 0.25° grid...")
    results = {}
    for day_idx in range(n_days):
        day_date = start_date + timedelta(days=day_idx)
        maxt_grid = interpolate_day_to_grid(
            all_stations_maxt, day_idx=day_idx,
            target_lats=target_lats, target_lons=target_lons,
            min_valid_stations=20,
        )
        mint_grid = interpolate_day_to_grid(
            all_stations_mint, day_idx=day_idx,
            target_lats=target_lats, target_lons=target_lons,
            min_valid_stations=20,
        )
        n_maxt_v = int(np.sum(~np.isnan(maxt_grid)))
        n_mint_v = int(np.sum(~np.isnan(mint_grid)))
        n_cells = maxt_grid.size
        print(f"      {day_date}: grid coverage "
              f"maxt {n_maxt_v}/{n_cells} ({100*n_maxt_v/n_cells:.0f}%), "
              f"mint {n_mint_v}/{n_cells} ({100*n_mint_v/n_cells:.0f}%)")
        results[day_date] = (maxt_grid, mint_grid)

    return results


def update_cube_slices(cube_path, new_slabs, element):
    """
    Update or append slices in an observation cube for the given dates.

    Merge behavior: for cells where the new value is NaN, the existing
    value is kept. This protects against transient ACIS outages where a
    state's pull returned no data — we don't want to overwrite good cube
    data with NaN. For cells where the new value is non-NaN, the new value
    replaces the old (this is where ACIS revisions to preliminary data take
    effect).

    Parameters
    ----------
    cube_path : str
        Path to observations_{maxt,mint}.nc.
    new_slabs : dict of date → 2-D float32 array
        Each entry is a day's interpolated grid.
    element : str
        "maxt" or "mint" — the data variable name in the cube.
    """
    print(f"    Loading {os.path.basename(cube_path)}...")
    t0 = time.time()
    with xr.open_dataset(cube_path) as ds:
        existing_data = ds[element].values.copy()
        existing_times = ds.time.values
        lats = ds.latitude.values
        lons = ds.longitude.values
        # Preserve compression/dtype from the original file, but filter out
        # keys the netCDF4 backend doesn't accept (it'll reject anything not
        # in its valid set, e.g., szip/zstd/bzip2/blosc that xarray may have
        # exposed in .encoding for round-tripping).
        valid_nc4_encoding_keys = {
            "chunksizes", "dtype", "contiguous", "least_significant_digit",
            "shuffle", "compression", "significant_digits", "blosc_shuffle",
            "zlib", "complevel", "szip_pixels_per_block", "_FillValue",
            "szip_coding", "quantize_mode", "endian", "fletcher32",
        }
        raw_encoding = dict(ds[element].encoding)
        encoding = {
            element: {
                k: v for k, v in raw_encoding.items()
                if k in valid_nc4_encoding_keys
            }
        }
    print(f"      Loaded in {time.time()-t0:.1f}s "
          f"({existing_data.shape}, {existing_data.nbytes / 1e6:.0f} MB)")

    # Strip encoding fields that conflict with a new array shape
    for key in ("chunksizes", "original_shape", "preferred_chunks"):
        encoding[element].pop(key, None)

    # Index existing dates for fast lookup
    existing_index = {
        str(t.astype("datetime64[D]")): i for i, t in enumerate(existing_times)
    }

    n_overwrite = 0
    n_append = 0
    new_dates_to_append = []
    new_arrays_to_append = []

    for d in sorted(new_slabs.keys()):
        grid = new_slabs[d]
        d_str = d.strftime("%Y-%m-%d")
        if d_str in existing_index:
            idx = existing_index[d_str]
            # Merge: where new is NaN, keep existing (safe against pull failures)
            merged = np.where(np.isnan(grid), existing_data[idx], grid)
            existing_data[idx] = merged.astype(existing_data.dtype)
            n_overwrite += 1
        else:
            new_dates_to_append.append(np.datetime64(d_str))
            new_arrays_to_append.append(grid.astype(existing_data.dtype))
            n_append += 1

    print(f"    Overwrote {n_overwrite} existing day(s), "
          f"appending {n_append} new day(s)")

    # Combine and sort
    if new_arrays_to_append:
        combined_data = np.concatenate(
            [existing_data, np.stack(new_arrays_to_append)], axis=0
        )
        combined_times = np.concatenate(
            [existing_times, np.array(new_dates_to_append)]
        )
        sort_idx = np.argsort(combined_times)
        combined_data = combined_data[sort_idx]
        combined_times = combined_times[sort_idx]
    else:
        combined_data = existing_data
        combined_times = existing_times

    # Build dataset
    da = xr.DataArray(
        combined_data,
        dims=["time", "latitude", "longitude"],
        coords={"time": combined_times, "latitude": lats, "longitude": lons},
        attrs={"last_refresh": time.strftime("%Y-%m-%d %H:%M:%S")},
    )
    out_ds = xr.Dataset({element: da})

    # Atomic write
    tmp_path = cube_path + ".tmp"
    print(f"    Writing cube ({combined_data.shape}, "
          f"{combined_data.nbytes / 1e6:.0f} MB)...")
    t0 = time.time()
    out_ds.to_netcdf(tmp_path, encoding=encoding)
    os.replace(tmp_path, cube_path)
    size_mb = os.path.getsize(cube_path) / (1024 * 1024)
    print(f"      Saved in {time.time()-t0:.1f}s ({size_mb:.0f} MB on disk)")


def refresh_data(today_date, sliding_window_days, workers, lats_1d, lons_1d):
    """
    Full daily refresh: pull last N days from ACIS, overwrite cube slices,
    rebuild the threshold index from the updated cubes.

    Three steps:
        1. Pull observations for the sliding window (one ACIS call per state,
           covering all N days).
        2. Update both maxt and mint cubes — overwrite existing slices for
           days inside the window, append slices for days beyond the cube's
           current range. Merge logic preserves good cube data where the
           new pull is sparse.
        3. Rebuild both threshold index files from the now-current cubes.
    """
    print("\n" + "=" * 60)
    print(f"REFRESHING DATA (sliding window: {sliding_window_days} days)")
    print("=" * 60)

    window_end = today_date - timedelta(days=1)
    window_start = window_end - timedelta(days=sliding_window_days - 1)
    print(f"  Window: {window_start} → {window_end} ({sliding_window_days} days)")

    # Step 1: pull
    print(f"\n  [1/3] Pull observations from ACIS")
    day_grids = pull_window_grids(
        window_start, window_end, lats_1d, lons_1d, workers
    )

    # Step 2: update cubes
    print(f"\n  [2/3] Update observation cubes")
    maxt_slabs = {d: g[0] for d, g in day_grids.items()}
    mint_slabs = {d: g[1] for d, g in day_grids.items()}
    update_cube_slices(OBS_FILE_MAXT, maxt_slabs, "maxt")
    update_cube_slices(OBS_FILE_MINT, mint_slabs, "mint")

    # Step 3: full threshold-index rebuild from updated cubes
    print(f"\n  [3/3] Rebuild threshold index from cubes")
    rebuild_threshold_index(
        OBS_FILE_MAXT, THRESHOLDS_FILE_MAXT, side="high", element="maxt"
    )
    rebuild_threshold_index(
        OBS_FILE_MINT, THRESHOLDS_FILE_MINT, side="low", element="mint"
    )


def load_threshold_indices():
    """Load both threshold index files into memory and return their arrays."""
    print(f"\n  Loading threshold indices...")
    with xr.open_dataset(THRESHOLDS_FILE_MAXT) as ds:
        last_dates_maxt = ds.last_date.values.copy()
    with xr.open_dataset(THRESHOLDS_FILE_MINT) as ds:
        last_dates_mint = ds.last_date.values.copy()
    print(f"    maxt: {last_dates_maxt.shape} {last_dates_maxt.dtype}")
    print(f"    mint: {last_dates_mint.shape} {last_dates_mint.dtype}")
    return last_dates_maxt, last_dates_mint


# =============================================================================
# STEP 2 — DOWNLOAD NDFD FORECAST
# =============================================================================

def download_ndfd_forecast(element, cache_dir):
    """
    Download today's NDFD forecast for one element (maxt or mint) to
    cache_dir. Returns the path to the downloaded file.
    """
    url = NDFD_URLS[element]
    out_path = os.path.join(cache_dir, f"ndfd_{element}.bin")

    print(f"  Downloading {url}...")
    t0 = time.time()
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)
    size_mb = len(resp.content) / (1024 * 1024)
    elapsed = time.time() - t0
    print(f"    Saved {out_path} ({size_mb:.1f} MB in {elapsed:.1f}s)")
    return out_path


# =============================================================================
# STEP 3 — PARSE GRIB2 AND EXTRACT TODAY'S FORECAST
# =============================================================================

def load_ndfd_forecast(grib_path, element, today_date):
    """
    Open an NDFD GRIB2 file with cfgrib, extract today's forecast field.

    NDFD MaxT/MinT are issued in K. They cover successive 12-24 hour valid
    periods. We pick the time step whose valid period contains today_date.

    Returns (lons_2d, lats_2d, temps_F_2d). lats/lons are on NDFD's native
    Lambert Conformal grid (~2.5 km resolution).
    """
    print(f"  Parsing {os.path.basename(grib_path)}...")

    ds = xr.open_dataset(grib_path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})

    # NDFD uses 'tmax' or 'tmin' as the var name when read by cfgrib
    var_name_candidates = {
        "maxt": ["tmax", "t2m", "mxt2m"],
        "mint": ["tmin", "t2m", "mnt2m"],
    }[element]

    var = None
    for cand in var_name_candidates:
        if cand in ds.data_vars:
            var = ds[cand]
            break
    if var is None:
        # Fall back: take the first data var
        var_name = list(ds.data_vars)[0]
        var = ds[var_name]
        print(f"    NOTE: using fallback var name {var_name!r}")

    print(f"    Variable: {var.name}, dims {var.dims}, shape {var.shape}")
    print(f"    Units attr: {var.attrs.get('units', '?')}")

    # NDFD VP.001-003 files contain three forecast steps (today, +1, +2).
    # Step 0 is always "the next maxt/mint to occur after issue time" — which
    # for normal morning/midday runs is today's max (or tonight's min).
    #
    # We don't try to match valid_time to today_date directly because NDFD's
    # valid_time convention differs by element: maxt's valid_time is the END
    # of the period (i.e., midnight UTC the next day) while mint's is the
    # morning of the day the min falls on. That mismatch made the previous
    # "nearest valid_time" logic emit confusing warnings even though it was
    # picking the right step.
    valid_times = ds.valid_time.values
    step_idx = 0
    vt = valid_times[step_idx]
    vt_date = np.datetime64(vt, "D")
    target = np.datetime64(today_date)
    delta_days = int((vt_date - target).astype("timedelta64[D]").astype(int))
    print(f"    Using step {step_idx} (valid_time {vt}, "
          f"date offset from target: {delta_days:+d} day(s))")
    if abs(delta_days) > 1:
        print(f"    WARNING: step 0 valid_time is {abs(delta_days)} days from "
              f"target — NDFD file may be stale or target date is unusual.")

    temps_native = var.isel(step=step_idx).values  # (y, x) in source units

    # Diagnostic: check value range to verify unit assumption
    finite = temps_native[np.isfinite(temps_native)]
    if len(finite) == 0:
        raise RuntimeError("Forecast field is entirely NaN — bad GRIB2 file?")
    vmin, vmax = float(finite.min()), float(finite.max())
    print(f"    Raw forecast range: [{vmin:.1f}, {vmax:.1f}]")

    # Unit conversion based on observed range
    units = var.attrs.get("units", "").lower()
    if units == "k" or (vmin > 150 and vmax < 350):
        # Kelvin
        temps_F = (temps_native - 273.15) * 9 / 5 + 32
        print(f"    Converted K → °F. Range: [{float(temps_F[np.isfinite(temps_F)].min()):.1f}, "
              f"{float(temps_F[np.isfinite(temps_F)].max()):.1f}]")
    elif units == "c" or (vmin > -60 and vmax < 60):
        # Celsius
        temps_F = temps_native * 9 / 5 + 32
        print(f"    Converted °C → °F.")
    elif units == "f" or (vmin > -60 and vmax < 150):
        # Already Fahrenheit (unusual for NDFD GRIB)
        temps_F = temps_native
        print(f"    Already °F.")
    else:
        raise RuntimeError(
            f"Could not determine forecast units from units={units!r} and "
            f"range [{vmin:.1f}, {vmax:.1f}]"
        )

    # Native grid coords
    lats_2d = ds.latitude.values
    lons_2d = ds.longitude.values
    # NDFD longitudes come as 0..360; convert to -180..180 if needed
    lons_2d = np.where(lons_2d > 180, lons_2d - 360, lons_2d)

    ds.close()
    return lons_2d, lats_2d, temps_F.astype(np.float32)


# =============================================================================
# STEP 4 — REGRID FORECAST TO CLIMATOLOGY GRID
# =============================================================================

def regrid_forecast(fcst_lons, fcst_lats, fcst_temps, target_lons, target_lats):
    """
    Regrid the native NDFD forecast field onto the 0.25° climatology grid
    using scipy griddata with linear interpolation, falling back to nearest
    where linear fails.

    Parameters
    ----------
    fcst_lons, fcst_lats : 2-D numpy arrays
        Source grid coordinates (NDFD Lambert Conformal, ~2.5 km).
    fcst_temps : 2-D numpy array
        Source field, °F.
    target_lons, target_lats : 1-D numpy arrays
        Target grid (0.25° CONUS).

    Returns
    -------
    numpy.ndarray
        Regridded forecast on the target grid, shape (len(target_lats),
        len(target_lons)), float32, NaN outside CONUS or interpolation failure.
    """
    print(f"  Regridding {fcst_temps.shape} → "
          f"({len(target_lats)}, {len(target_lons)})...")

    # Flatten source grid and drop NaN cells
    src_points = np.column_stack([fcst_lons.ravel(), fcst_lats.ravel()])
    src_values = fcst_temps.ravel()
    valid = np.isfinite(src_values)
    src_points = src_points[valid]
    src_values = src_values[valid]
    print(f"    {len(src_values):,} valid source points")

    # Build target meshgrid
    tlon2d, tlat2d = np.meshgrid(target_lons, target_lats)
    tgt_points = np.column_stack([tlon2d.ravel(), tlat2d.ravel()])

    # Linear interpolation first
    t0 = time.time()
    out_linear = griddata(src_points, src_values, tgt_points,
                          method="linear").reshape(tlon2d.shape)
    print(f"    Linear interp: {time.time()-t0:.1f}s, "
          f"{int(np.sum(~np.isnan(out_linear)))} / {out_linear.size} valid")

    # Nearest fill for any remaining NaN cells inside the source convex hull
    if np.isnan(out_linear).any():
        t0 = time.time()
        out_nearest = griddata(src_points, src_values, tgt_points,
                               method="nearest").reshape(tlon2d.shape)
        out_linear = np.where(np.isnan(out_linear), out_nearest, out_linear)
        print(f"    Nearest fill: {time.time()-t0:.1f}s")

    return out_linear.astype(np.float32)


# =============================================================================
# STEP 5 — COMPUTE STREAK
# =============================================================================

def compute_streak(last_dates, forecast, side, today_idx):
    """
    Per-cell lookup of "days since last match" given a forecast grid.

    For HIGH side: streak = today_idx - last_dates[bin_for_forecast, lat, lon]
    where last_dates[b, ...] = "most recent day temperature was >= bin b".

    Parameters
    ----------
    last_dates : numpy.ndarray, shape (N_BINS, n_lat, n_lon), int16
        Threshold-indexed last-date array.
    forecast : numpy.ndarray, shape (n_lat, n_lon), float32
        Forecast field in °F. May contain NaN.
    side : str
        "high" or "low" — only used here for sanity checking; the lookup
        logic is symmetric (the index encodes the asymmetry).
    today_idx : int
        Days since EPOCH for the day the forecast is valid.

    Returns
    -------
    streak : numpy.ndarray, shape (n_lat, n_lon), float32
        Days since this cell last matched the forecast. NaN where forecast
        is NaN or where the threshold was never reached in record.
    no_match_mask : numpy.ndarray, shape (n_lat, n_lon), bool
        True where the threshold was never reached in record (sentinel hits).
        Used to draw the hatched overlay.
    """
    forecast_nan = np.isnan(forecast)

    # Compute bin index per cell. Replace NaN with a placeholder for the cast,
    # then mask out post-hoc.
    safe = np.where(forecast_nan, 0.0, forecast)
    bin_idx = np.round((safe - BIN_MIN_F) / BIN_STEP_F).astype(np.int32)
    bin_idx = np.clip(bin_idx, 0, N_BINS - 1)

    # Look up each cell's last-date for its specific bin
    last_date = np.take_along_axis(
        last_dates, bin_idx[None, :, :], axis=0
    )[0]  # shape (n_lat, n_lon)

    # Sentinel cells = threshold never reached in record
    no_match_mask = (last_date == SENTINEL) & ~forecast_nan

    # Streak in days. Cast to float so we can NaN-out invalid cells.
    streak = (today_idx - last_date.astype(np.int32)).astype(np.float32)
    streak[forecast_nan] = np.nan
    streak[no_match_mask] = np.nan  # rendered as hatching, not color

    # Brief stats
    valid = ~np.isnan(streak)
    if valid.any():
        s = streak[valid]
        print(f"    Streak distribution: "
              f"min={s.min():.0f}, median={np.median(s):.0f}, "
              f"95th={np.percentile(s, 95):.0f}, max={s.max():.0f} days")
    print(f"    No-match cells: {int(no_match_mask.sum()):,} "
          f"({100*no_match_mask.sum()/no_match_mask.size:.1f}%)")

    return streak, no_match_mask


# =============================================================================
# STEP 6 — RENDER STREAK MAP
# =============================================================================

# Nice round-number caps for the dynamic colorbar. Picked from common temporal
# milestones: a couple weeks, a month, a couple months, a season, half-year,
# year, multi-year out to the full record (47 years ≈ 17,500 days).
NICE_CAPS = [10, 20, 30, 60, 90, 180, 365, 730, 1825, 3650, 7300, 17500]
# Candidate intervals for contour lines and colorbar ticks. The largest steps
# (3650 = 10 years, 7300 = 20 years) keep tick spacing sensible at the very
# top of the cap range, where a 47-year cap would otherwise get crowded with
# 5-year ticks.
NICE_INTERVALS = [1, 2, 5, 10, 15, 30, 60, 90, 180, 365, 730, 1825, 3650]


def choose_streak_scale(field):
    """
    Pick a colorbar cap, contour-line interval, and tick interval from a
    streak field.

    The cap is the smallest "nice" round number that's at least the 99th
    percentile of valid (smoothed, non-sentinel) values. The line and tick
    intervals are then chosen so the visible range gets roughly 8-12 contour
    lines and 4-7 colorbar ticks.

    Parameters
    ----------
    field : 2-D numpy.ndarray
        The streak field (typically the smoothed version). NaN cells are
        ignored.

    Returns
    -------
    (cap, line_interval, tick_interval) : tuple of int
    """
    valid = field[~np.isnan(field)]
    if valid.size < 100:
        # Not enough data to scale meaningfully — fall back to a small window
        return 30, 5, 10

    p99 = float(np.percentile(valid, 99))
    # Round up to next "nice" cap
    cap = next((n for n in NICE_CAPS if n >= p99), NICE_CAPS[-1])

    # Aim for 8-12 contour lines; pick smallest interval that gives ≤ 12 lines
    line_interval = next(
        (n for n in NICE_INTERVALS if cap / n <= 12),
        NICE_INTERVALS[-1],
    )
    # Aim for 4-7 ticks; pick smallest interval that gives ≤ 7 ticks
    tick_interval = next(
        (n for n in NICE_INTERVALS if cap / n <= 7),
        NICE_INTERVALS[-1],
    )

    return cap, line_interval, tick_interval


def render_streak_map(streak, no_match_mask, lons, lats, today_date, side,
                      output_path, region_name=None, region_extent=None,
                      also_save_jpg=None):
    """
    Render one streak map.

    The CONUS shape is established by the natural NaN pattern of the streak
    field: streak is NaN over ocean (where the NDFD forecast is NaN) and over
    sentinel cells (threshold never reached in record), and contourf doesn't
    draw at NaN cells. No shapely clip path is used — that approach was
    fragile and unnecessary.

    Parameters
    ----------
    streak : 2-D float32, NaN where missing/no-match
    no_match_mask : 2-D bool, True where to draw hatching (sentinel cells)
    lons, lats : 1-D coord arrays
    today_date : date
    side : "high" or "low"
    output_path : str
    region_name, region_extent : optional, for regional maps
    also_save_jpg : optional, save a 150-DPI JPG alongside the PNG
    """
    is_regional = region_name is not None

    # Hatching style — must be set on rcParams before figure creation
    plt.rcParams["hatch.linewidth"] = 0.5
    plt.rcParams["hatch.color"] = "#444444"

    fig = plt.figure(figsize=(14, 8))
    proj = ccrs.LambertConformal(
        central_longitude=-96, central_latitude=39,
        standard_parallels=(33, 45),
    )
    ax = fig.add_axes([0.02, 0.08, 0.96, 0.78], projection=proj)

    if region_extent:
        w, e, s, n = region_extent
        ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    else:
        # Trim 1° off each edge to avoid the artificial rectangle edge
        ax.set_extent([
            CONUS_LON_MIN + 1, CONUS_LON_MAX - 1,
            CONUS_LAT_MIN + 1, CONUS_LAT_MAX - 1,
        ], crs=ccrs.PlateCarree())

    # Base map features — LAND/OCEAN at the bottom so the NaN regions of the
    # forecast (over water) show the ocean color. LAKES drawn ABOVE land but
    # below the data so lake areas show through the streak fill (where the
    # forecast is NaN over a Great Lake, the lake's blue fill shows up).
    ax.add_feature(cfeature.LAND, facecolor="#f5f5f5", zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor="#e6f0f7", zorder=0)
    ax.add_feature(cfeature.LAKES, facecolor="#e6f0f7", edgecolor="#cccccc",
                   linewidth=0.5, zorder=1)

    # Smooth the field with a median-fill (not zero-fill, which would create
    # dark "halos" near coasts for a non-negative field that maps 0 → white)
    nan_mask = np.isnan(streak)
    if (~nan_mask).any():
        fill_value = float(np.nanmedian(streak))
    else:
        fill_value = 0.0
    smoothed = streak.copy()
    smoothed[nan_mask] = fill_value
    smoothed = gaussian_filter(smoothed, sigma=STREAK_SMOOTH_SIGMA)
    smoothed[nan_mask] = np.nan

    # Dynamic scale based on the smoothed field's 99th percentile
    cap, line_interval, tick_interval = choose_streak_scale(smoothed)
    fill_levels = np.linspace(0, cap, 91)
    line_levels = np.arange(line_interval, cap + 1, line_interval)

    cmap = CMAP_HIGH if side == "high" else CMAP_LOW
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Color fill. extend="max" so cells above the cap show the saturated end
    # of the colormap rather than being blank.
    filled = ax.contourf(
        lon2d, lat2d, smoothed,
        levels=fill_levels, cmap=cmap,
        vmin=0, vmax=cap, extend="max",
        transform=ccrs.PlateCarree(), zorder=2,
    )

    # Contour lines
    lines = ax.contour(
        lon2d, lat2d, smoothed,
        levels=line_levels, colors="#444444",
        linewidths=0.6 if is_regional else 0.4,
        transform=ccrs.PlateCarree(), zorder=2.5,
    )

    # Label every Nth line per STREAK_LABEL_STRIDE
    if STREAK_LABEL_STRIDE > 0 and len(line_levels) > 0:
        labeled = line_levels[::STREAK_LABEL_STRIDE]
        try:
            ax.clabel(lines, levels=labeled, inline=True,
                      fontsize=9 if is_regional else 7,
                      fmt="%.0f", inline_spacing=5, colors="#333333")
        except Exception:
            # clabel occasionally fails if no contours form closed loops;
            # non-fatal — just skip labels
            pass

    # Hatched overlay for sentinel cells. We only draw the hatch pattern,
    # not a fill color (colors="none"). Smoothing the boolean mask gives
    # softer edges to the hatched region.
    if no_match_mask.any():
        flag = no_match_mask.astype(np.float32)
        flag = gaussian_filter(flag, sigma=STREAK_SMOOTH_SIGMA)
        ax.contourf(
            lon2d, lat2d, flag,
            levels=[0.5, 1.5],
            colors="none", hatches=["xxxxx"],
            transform=ccrs.PlateCarree(), zorder=2.7,
        )

    # Political boundaries on top of the fill
    ax.add_feature(cfeature.STATES, edgecolor="#888888",
                   linewidth=0.5, zorder=3)
    ax.add_feature(cfeature.BORDERS, edgecolor="#444444",
                   linewidth=1.0, zorder=3)
    ax.add_feature(cfeature.COASTLINE, edgecolor="#666666",
                   linewidth=0.7, zorder=3)

    # Colorbar — ticks at the chosen interval
    cbar_ax = fig.add_axes([0.15, 0.06, 0.70, 0.025])
    cbar = fig.colorbar(filled, cax=cbar_ax, orientation="horizontal")
    cbar.set_ticks(np.arange(0, cap + 1, tick_interval))
    cbar.set_label("Days Since Last At-Threshold Day",
                   fontsize=11, fontweight="bold", labelpad=8)
    cbar.ax.tick_params(labelsize=10)

    # Title and subtitle
    date_str = today_date.strftime("%A, %B %-d, %Y")
    if side == "high":
        product = "Days Since Forecast High Was Last Reached"
    else:
        product = "Days Since Forecast Low Was Last Recorded"

    if is_regional:
        title = f"{product}: {region_name}"
        subtitle = date_str
    else:
        title = f"{product}: {date_str}"
        subtitle = ("How long has it been since this location's temperature "
                    "last matched today's NDFD forecast? Cross-hatching = "
                    "never matched since 1979.")
    ax.set_title(title, fontsize=16, fontweight="bold", pad=32)
    fig.text(0.5, 0.88, subtitle, ha="center", fontsize=10, color="#555555",
             style="italic")

    fig.text(0.99, 0.01,
             "Data: NWS NDFD / RCC-ACIS station observations 1979-present",
             ha="right", fontsize=7, color="#999999")

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    fig.savefig(output_path, dpi=MAP_DPI, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    print(f"  Saved {output_path}  (cap={cap}, line_int={line_interval}, "
          f"tick_int={tick_interval})")

    if also_save_jpg:
        archive_dir = os.path.dirname(also_save_jpg)
        if archive_dir:
            os.makedirs(archive_dir, exist_ok=True)
        fig.savefig(also_save_jpg, dpi=150, bbox_inches="tight",
                    facecolor="white", edgecolor="none", format="jpg",
                    pil_kwargs={"quality": 85})
        print(f"  Saved {also_save_jpg}")

    plt.close(fig)


# =============================================================================
# MAIN ORCHESTRATION
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Daily run for the Time Since Temperature streak maps."
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Override target date (YYYY-MM-DD). Default: today UTC.")
    parser.add_argument("--skip-update", action="store_true",
                        help="Skip the observation pull / threshold update step.")
    parser.add_argument("--test", action="store_true",
                        help="Skip NDFD download; use synthetic forecast (for testing).")
    parser.add_argument("--workers", type=int, default=10,
                        help="ThreadPool workers for ACIS state pulls (default 10).")
    parser.add_argument("--sliding-window", type=int, default=30,
                        help="Days back to re-pull from ACIS each run (default 30). "
                             "Anything inside this window gets refreshed with the "
                             "latest ACIS values; outside the window is treated as final.")
    args = parser.parse_args()

    # Determine target date
    if args.date:
        today_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        today_date = datetime.now(timezone.utc).date()
    today_idx = (today_date - EPOCH_DATE).days

    print()
    print("=" * 60)
    print(f"DAILY STREAK MAP RUN — {today_date} (today_idx={today_idx})")
    print("=" * 60)

    # Grid coords
    lons_1d, lats_1d = build_climatology_grid_coords()

    # --- Step 1: refresh data (sliding window) ---
    if args.skip_update:
        print("\n  --skip-update: using existing threshold index without refresh.")
    else:
        refresh_data(
            today_date,
            sliding_window_days=args.sliding_window,
            workers=args.workers,
            lats_1d=lats_1d,
            lons_1d=lons_1d,
        )

    last_dates_maxt, last_dates_mint = load_threshold_indices()

    # --- Steps 2-3: download and parse NDFD forecasts ---
    print("\n" + "=" * 60)
    print("STEP 2-3: DOWNLOAD AND PARSE NDFD FORECASTS")
    print("=" * 60)

    if args.test:
        print("  --test: using observations from 365 days ago as forecast.")
        fcst_high_grid, fcst_low_grid = synthesize_test_forecast(today_date)
    else:
        with tempfile.TemporaryDirectory() as cache:
            print("\n  Downloading maxt forecast...")
            maxt_grib = download_ndfd_forecast("maxt", cache)
            print("\n  Downloading mint forecast...")
            mint_grib = download_ndfd_forecast("mint", cache)

            print("\n  Parsing maxt forecast...")
            mx_lons, mx_lats, mx_temps = load_ndfd_forecast(maxt_grib, "maxt", today_date)
            print("\n  Parsing mint forecast...")
            mn_lons, mn_lats, mn_temps = load_ndfd_forecast(mint_grib, "mint", today_date)

            # --- Step 4: regrid forecasts ---
            print("\n" + "=" * 60)
            print("STEP 4: REGRID FORECASTS TO CLIMATOLOGY GRID")
            print("=" * 60)
            print("\n  Regridding maxt forecast...")
            fcst_high_grid = regrid_forecast(mx_lons, mx_lats, mx_temps,
                                              lons_1d, lats_1d)
            print("\n  Regridding mint forecast...")
            fcst_low_grid = regrid_forecast(mn_lons, mn_lats, mn_temps,
                                             lons_1d, lats_1d)

    # --- Step 5: compute streaks ---
    print("\n" + "=" * 60)
    print("STEP 5: COMPUTE STREAKS")
    print("=" * 60)
    print("\n  High streak (maxt forecast vs maxt records):")
    streak_high, no_match_high = compute_streak(
        last_dates_maxt, fcst_high_grid, side="high", today_idx=today_idx
    )
    print("\n  Low streak (mint forecast vs mint records):")
    streak_low, no_match_low = compute_streak(
        last_dates_mint, fcst_low_grid, side="low", today_idx=today_idx
    )

    # --- Apply CONUS land mask ---
    # Ocean and Great Lakes cells in the threshold index hold nearest-neighbor
    # extrapolated values from the build, which produce nonsense streaks
    # (different from neighboring land cells and visually misleading). Mask
    # them out so we only render CONUS land. This also tightens the dynamic
    # colorbar scale since offshore noise no longer pulls up the 99th percentile.
    print("\n  Building CONUS land mask...")
    land_mask = build_conus_land_mask(lons_1d, lats_1d)
    n_land = int(land_mask.sum())
    n_total = land_mask.size
    print(f"    {n_land:,} / {n_total:,} cells are CONUS land "
          f"({100 * n_land / n_total:.1f}%)")

    not_land = ~land_mask
    streak_high[not_land]    = np.nan
    streak_low[not_land]     = np.nan
    no_match_high[not_land]  = False
    no_match_low[not_land]   = False

    # --- Step 6: render ---
    print("\n" + "=" * 60)
    print("STEP 6: RENDER MAPS")
    print("=" * 60)

    archive_date_str = today_date.strftime("%Y%m%d")
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    os.makedirs(REGIONS_DIR, exist_ok=True)

    # National + archive (each side renders once, saves PNG + JPG from same figure)
    print("\n  Rendering national high-side map...")
    render_streak_map(
        streak_high, no_match_high, lons_1d, lats_1d,
        today_date, side="high",
        output_path=OUTPUT_HIGH_LATEST,
        also_save_jpg=os.path.join(
            ARCHIVE_DIR, f"temp_streak_high_{archive_date_str}.jpg"),
    )
    print("\n  Rendering national low-side map...")
    render_streak_map(
        streak_low, no_match_low, lons_1d, lats_1d,
        today_date, side="low",
        output_path=OUTPUT_LOW_LATEST,
        also_save_jpg=os.path.join(
            ARCHIVE_DIR, f"temp_streak_low_{archive_date_str}.jpg"),
    )

    # Regional — flat layout (one file per region+side in REGIONS_DIR) to
    # match the day-offset tool's `temp_anomaly_<region>_latest.png` convention.
    # The site's `assets/regions/` folder consumes a flat structure, and a flat
    # local layout keeps the workflow's copy step uncomplicated.
    for region_key, region_info in REGIONS.items():
        region_label = region_info.get("name", region_key)
        region_extent = region_info["extent"]

        print(f"\n  Rendering regional ({region_label}) high-side map...")
        render_streak_map(
            streak_high, no_match_high, lons_1d, lats_1d,
            today_date, side="high",
            output_path=os.path.join(
                REGIONS_DIR, f"temp_streak_high_{region_key}_latest.png"),
            region_name=region_label,
            region_extent=region_extent,
        )
        print(f"\n  Rendering regional ({region_label}) low-side map...")
        render_streak_map(
            streak_low, no_match_low, lons_1d, lats_1d,
            today_date, side="low",
            output_path=os.path.join(
                REGIONS_DIR, f"temp_streak_low_{region_key}_latest.png"),
            region_name=region_label,
            region_extent=region_extent,
        )

    print("\n" + "=" * 60)
    print("DAILY RUN COMPLETE")
    print("=" * 60)
    print(f"  National high: {OUTPUT_HIGH_LATEST}")
    print(f"  National low:  {OUTPUT_LOW_LATEST}")
    print(f"  Archive: {ARCHIVE_DIR}/temp_streak_*_{archive_date_str}.jpg")
    print(f"  Regional: {REGIONS_DIR}/temp_streak_*_<region>_latest.png "
          f"({2 * len(REGIONS)} files)")


if __name__ == "__main__":
    main()
