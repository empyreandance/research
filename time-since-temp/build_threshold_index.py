#!/usr/bin/env python3
"""
build_threshold_index.py — Build threshold-indexed last-date arrays from the
observation cubes.

This script is a one-time pre-processor. It reads observations_maxt.nc and
observations_mint.nc (the ~800 MB grid × time cubes that build_observations.py
produced) and converts them into much smaller threshold × space arrays:

    last_dates_maxt.nc       shape (n_thresholds, lat, lon), int16
    last_dates_mint.nc       same shape

For the HIGH side:
    last_dates[bin_for_T, i, j] = days since 1979-01-01 of the most recent day
    where the observation at cell (i, j) was AT LEAST temperature T.

For the LOW side:
    last_dates[bin_for_T, i, j] = days since 1979-01-01 of the most recent day
    where the observation at cell (i, j) was AT MOST temperature T.

Sentinel value (-1) means "never observed at this threshold in the period of
record" — those cells get the hatched overlay on the streak maps.

Bin layout:
    1800 bins covering -50.0°F to 129.9°F in 0.1°F steps.
    Bin index for temperature T = round((T + 50) * 10), clipped to [0, 1799].

Date storage:
    int16 days since 1979-01-01. Range -32768 to 32767, so good through ~2068.

File size: ~84 MiB per element, ~170 MiB total. Stored UNCOMPRESSED at the
NetCDF layer so git's delta compression can do its job on daily updates.

The algorithm is a scatter-then-cumulative-max:

    1. Walk the cube once chronologically. For each (day, cell), look up the
       cell's bin index B for that day's observation. Write today's date into
       last_occurrence[B, i, j]. This is a SINGLE write per cell per day —
       not the ~880 writes you'd do with a "set bins 0..B" mask approach.
    2. After the scatter, last_occurrence[b, i, j] holds the most recent date
       on which cell (i, j) had its observation fall in bin b exactly.
    3. Apply a cumulative max along the threshold axis:
         - HIGH: from high bin down to low (because "obs >= T" is the union
           of "obs in bin T or any higher bin").
         - LOW:  from low bin up to high (mirror).
       That converts "max date where obs hit exactly bin b" into "max date
       where obs hit bin b or higher (for high) / lower (for low)."

This is roughly 60× faster than the direct mask-and-assign approach, and
produces bit-identical output.

Usage:
    python build_threshold_index.py            # Build from default cube paths
    python build_threshold_index.py --check    # Sanity-check existing output files
"""

import os
import sys
import time
import argparse

import numpy as np
import xarray as xr
from tqdm import tqdm

from config import (
    PROJECT_DIR,
    OBS_FILE_MAXT, OBS_FILE_MINT,
)


# =============================================================================
# CONSTANTS — bin layout
# =============================================================================

BIN_MIN_F = -50.0
BIN_MAX_F = 129.9
BIN_STEP_F = 0.1

# 1800 bins: (-50.0, -49.9, -49.8, ..., 129.8, 129.9)
N_BINS = int(round((BIN_MAX_F - BIN_MIN_F) / BIN_STEP_F)) + 1

# Date epoch
EPOCH = np.datetime64("1979-01-01")

# Sentinel for "never observed at this threshold in record"
SENTINEL = np.int16(-1)
SENTINEL_INT32 = np.int32(-1)  # working-storage version

# Output paths
THRESHOLDS_FILE_MAXT = os.path.join(PROJECT_DIR, "last_dates_maxt.nc")
THRESHOLDS_FILE_MINT = os.path.join(PROJECT_DIR, "last_dates_mint.nc")


# =============================================================================
# HELPERS
# =============================================================================

def temp_to_bin_for_scatter(temp_arr):
    """
    Convert temperatures to bin indices for use with np.put_along_axis scatter.

    Valid temperatures map to bins [0, N_BINS - 1]. NaN cells map to N_BINS
    (the "junk slot" — we'll allocate one extra slot in the scatter target
    and trim it off afterward, so NaN values don't corrupt valid bins).

    Parameters
    ----------
    temp_arr : numpy.ndarray
        Temperatures in °F. May contain NaN.

    Returns
    -------
    numpy.ndarray of int32
        Bin indices, same shape as input. NaN cells → N_BINS.
    """
    nan_mask = np.isnan(temp_arr)
    # Replace NaN with a safe value (0) for the cast — we'll overwrite below.
    safe = np.where(nan_mask, 0.0, temp_arr)
    bin_f = (safe - BIN_MIN_F) / BIN_STEP_F
    bin_idx = np.round(bin_f).astype(np.int32)
    bin_idx = np.clip(bin_idx, 0, N_BINS - 1)
    # NaN cells go to the junk slot (N_BINS), which we'll drop later.
    bin_idx = np.where(nan_mask, N_BINS, bin_idx)
    return bin_idx


def datetime64_to_days_since_epoch(times):
    """Convert array of np.datetime64 to int32 days since EPOCH."""
    return ((times - EPOCH) / np.timedelta64(1, "D")).astype(np.int32)


# =============================================================================
# BUILD ONE SIDE
# =============================================================================

def build_threshold_index(cube_path, output_path, side, element):
    """
    Build the threshold-indexed last-date array from one cube.

    Two-step algorithm:
        1. Scatter pass: walk the cube chronologically. For each day's
           observation at each cell, scatter today's date into the cell's
           exact bin slot in last_occurrence.
        2. Cumulative max pass: convert per-bin "last date observed AT this
           bin exactly" into per-bin "last date observed AT LEAST this bin"
           (or AT MOST for the low side) via np.maximum.accumulate along
           the threshold axis.

    Parameters
    ----------
    cube_path : str
        Path to the input cube NetCDF (observations_maxt.nc or _mint.nc).
    output_path : str
        Path to write the threshold-indexed NetCDF.
    side : str
        "high" → last date AT LEAST threshold.
        "low"  → last date AT MOST threshold.
    element : str
        Name of the data variable in the cube ("maxt" or "mint").
    """
    if side not in ("high", "low"):
        raise ValueError(f"side must be 'high' or 'low', got {side!r}")

    print(f"\n{'=' * 60}")
    print(f"BUILDING {side.upper()}-SIDE THRESHOLD INDEX ({element})")
    print(f"{'=' * 60}")
    print(f"Source cube: {cube_path}")

    if not os.path.exists(cube_path):
        print(f"ERROR: cube file not found. Run build_observations.py first.")
        sys.exit(1)

    print("Loading cube into memory...")
    ds = xr.open_dataset(cube_path)
    obs = ds[element].values  # (n_days, n_lat, n_lon) float32
    times = ds.time.values
    lats = ds.latitude.values
    lons = ds.longitude.values

    n_days, n_lat, n_lon = obs.shape
    cube_mb = obs.nbytes / (1024 * 1024)
    print(f"  Shape: {obs.shape}  ({cube_mb:.0f} MB in memory)")
    print(f"  Date range: {str(times[0])[:10]} → {str(times[-1])[:10]}")

    days_since_epoch = datetime64_to_days_since_epoch(times)
    if days_since_epoch.max() >= 32767 or days_since_epoch.min() < -32768:
        print(f"  WARNING: some dates exceed int16 range and won't fit.")
        print(f"  Range: {days_since_epoch.min()} to {days_since_epoch.max()}")

    # --- Step 1: scatter ---
    # last_occurrence with an extra "junk slot" at N_BINS to absorb NaN cells.
    # Using int32 during processing so dates have full headroom; we narrow
    # to int16 at the end.
    mem_mb = (N_BINS + 1) * n_lat * n_lon * 4 / (1024 * 1024)
    print(f"\n  Allocating last_occurrence working array "
          f"({N_BINS + 1}, {n_lat}, {n_lon}) int32 ({mem_mb:.1f} MB)")
    last_occurrence = np.full((N_BINS + 1, n_lat, n_lon),
                              SENTINEL_INT32, dtype=np.int32)

    print(f"\n  Scattering daily observations into bin slots ({n_days} days)...")
    for d in tqdm(range(n_days), desc=f"  {side} scatter", unit="day"):
        vals = obs[d]
        bin_idx = temp_to_bin_for_scatter(vals)  # (n_lat, n_lon) int32
        # Scatter today's date into the cell's bin slot. Days are processed
        # chronologically, so a repeat hit at the same bin overwrites with
        # the more recent date — which is exactly what we want.
        np.put_along_axis(
            last_occurrence,
            bin_idx[None, :, :],
            np.int32(days_since_epoch[d]),
            axis=0
        )

    # Drop the junk slot — those writes don't represent real observations.
    last_occurrence = last_occurrence[:N_BINS]

    # --- Step 2: cumulative max along threshold axis ---
    # After scatter: last_occurrence[b, i, j] = most recent day cell (i, j)
    # had its observation fall in bin b EXACTLY.
    #
    # We want: for the high side, the most recent day the cell was IN BIN b
    # OR ANY HIGHER BIN. That's max over [b, b+1, ..., N_BINS-1]. A reversed
    # cumulative max along axis 0 gives this in one pass.
    #
    # For the low side: max over [0, 1, ..., b]. Forward cumulative max.
    print(f"\n  Computing cumulative max along threshold axis ({side} side)...")
    if side == "high":
        last_dates_i32 = np.maximum.accumulate(last_occurrence[::-1], axis=0)[::-1]
    else:
        last_dates_i32 = np.maximum.accumulate(last_occurrence, axis=0)

    # Narrow to int16 for storage (we've verified dates fit)
    last_dates = last_dates_i32.astype(np.int16)

    # --- Save ---
    print(f"\n  Saving to {output_path}...")

    thresholds = np.arange(N_BINS, dtype=np.float32) * BIN_STEP_F + BIN_MIN_F

    da = xr.DataArray(
        last_dates,
        dims=["threshold", "latitude", "longitude"],
        coords={
            "threshold": thresholds,
            "latitude": lats,
            "longitude": lons,
        },
        attrs={
            "long_name": (
                f"Days since {str(EPOCH)} of last date temperature "
                f"was {'at least' if side == 'high' else 'at most'} threshold."
            ),
            "units": "days",
            "epoch": str(EPOCH),
            "sentinel": int(SENTINEL),
            "sentinel_meaning": "never observed at this threshold in record",
            "threshold_units": "degF",
            "bin_step_F": BIN_STEP_F,
            "bin_min_F": BIN_MIN_F,
            "bin_max_F": BIN_MAX_F,
            "side": side,
            "source_cube": os.path.basename(cube_path),
            "cube_first_date": str(times[0])[:10],
            "cube_last_date": str(times[-1])[:10],
            "n_days_in_record": n_days,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )

    out_ds = xr.Dataset({"last_date": da})

    # NO compression — keep raw bytes so git's delta compression can find
    # the small daily diffs efficiently. int16 dtype enforced.
    #
    # Intentionally NOT setting _FillValue here. With it set, xarray's
    # mask_and_scale machinery promotes the array to int64 on read (with -1
    # replaced by NaN). We want to keep the compact int16 representation and
    # handle the -1 sentinel ourselves in downstream code. The attrs above
    # document the sentinel value.
    encoding = {
        "last_date": {
            "zlib": False,
            "dtype": "int16",
        }
    }
    out_ds.to_netcdf(output_path, encoding=encoding)
    ds.close()

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Saved {output_path} ({size_mb:.1f} MB)")

    # --- Sanity check on the output ---
    print(f"\n  Sanity check:")
    n_sentinel = int((last_dates == SENTINEL).sum())
    n_total = int(last_dates.size)
    print(f"    Sentinel cells: {n_sentinel:,} / {n_total:,} "
          f"({100 * n_sentinel / n_total:.1f}%)")

    n_recent = int((last_dates == np.int16(days_since_epoch[-1])).sum())
    print(f"    Cells touched on last day in record: "
          f"{n_recent:,} ({100 * n_recent / n_total:.1f}%)")

    # Spot-check monotonicity on a populated cell
    mid_i, mid_j = n_lat // 2, n_lon // 2
    cell_series = last_dates[:, mid_i, mid_j]
    nonsent = cell_series[cell_series != SENTINEL]
    if len(nonsent) >= 2:
        diffs = np.diff(cell_series)
        if side == "high":
            # last_dates should be monotonically non-increasing in threshold
            # for cells where higher thresholds were ever reached. The sentinel
            # transitions are handled by comparing only consecutive nonsentinel
            # pairs, but simpler: just check that the array is non-increasing
            # over the populated range.
            populated_idx = np.where(cell_series != SENTINEL)[0]
            if len(populated_idx) >= 2:
                lo, hi = populated_idx[0], populated_idx[-1]
                sub = cell_series[lo:hi + 1]
                ok = np.all(np.diff(sub) <= 0)
                print(f"    Center cell ({mid_i}, {mid_j}) monotonicity "
                      f"(should be non-increasing): {'OK' if ok else 'FAILED'}")
        else:
            populated_idx = np.where(cell_series != SENTINEL)[0]
            if len(populated_idx) >= 2:
                lo, hi = populated_idx[0], populated_idx[-1]
                sub = cell_series[lo:hi + 1]
                ok = np.all(np.diff(sub) >= 0)
                print(f"    Center cell ({mid_i}, {mid_j}) monotonicity "
                      f"(should be non-decreasing): {'OK' if ok else 'FAILED'}")


# =============================================================================
# CHECK MODE
# =============================================================================

def check_outputs():
    """Open both threshold-indexed files and report basic stats."""
    for path, expected_side in [(THRESHOLDS_FILE_MAXT, "high"),
                                (THRESHOLDS_FILE_MINT, "low")]:
        print(f"\n{'=' * 60}")
        print(f"CHECKING {os.path.basename(path)}")
        print(f"{'=' * 60}")

        if not os.path.exists(path):
            print(f"  Not present.")
            continue

        ds = xr.open_dataset(path)
        arr = ds.last_date.values

        print(f"  Shape: {arr.shape}")
        print(f"  Dtype: {arr.dtype}")
        print(f"  Size on disk: {os.path.getsize(path) / 1024 / 1024:.1f} MB")
        print(f"  Side: {ds.last_date.attrs.get('side', '?')} "
              f"(expected {expected_side})")
        print(f"  Cube record: {ds.last_date.attrs.get('cube_first_date', '?')} → "
              f"{ds.last_date.attrs.get('cube_last_date', '?')}")
        print(f"  Sentinel cells: "
              f"{(arr == SENTINEL).sum():,} / {arr.size:,} "
              f"({100 * (arr == SENTINEL).sum() / arr.size:.1f}%)")
        nonsentinel = arr[arr != SENTINEL]
        if len(nonsentinel) > 0:
            print(f"  Non-sentinel date range (days since epoch): "
                  f"{nonsentinel.min()} → {nonsentinel.max()}")
        ds.close()


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Build threshold-indexed last-date arrays from the cubes."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print sanity stats on existing output files, no rebuild."
    )
    args = parser.parse_args()

    if args.check:
        check_outputs()
        return

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║         THRESHOLD-INDEX BUILDER — One-time pass         ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  Converts the 800 MB obs cubes into compact ~84 MB      ║")
    print("║  threshold × space last-date arrays. Scatter-then-      ║")
    print("║  cumulative-max algorithm. Roughly 1-2 minutes per side.║")
    print("╚══════════════════════════════════════════════════════════╝")

    t0 = time.time()
    build_threshold_index(OBS_FILE_MAXT, THRESHOLDS_FILE_MAXT,
                          side="high", element="maxt")
    build_threshold_index(OBS_FILE_MINT, THRESHOLDS_FILE_MINT,
                          side="low",  element="mint")

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"DONE in {elapsed / 60:.1f} minutes.")
    print(f"{'=' * 60}")
    print(f"  {THRESHOLDS_FILE_MAXT}")
    print(f"  {THRESHOLDS_FILE_MINT}")


if __name__ == "__main__":
    main()
