#!/usr/bin/env python3
"""
fetch_and_process.py
====================
Downloads the latest EFI/SOT GRIB2 file from WPC's public FTP server,
clips the fields to each NWS CWA polygon, and writes JSON files for
the web frontend.

The WPC public FTP mirror is unreliable — the GRIB often lands late, fails
to push, or gets purged — while NCEP's ESAT endpoint (which is fed from the
same upstream run over a different pipe) stays current.  So this script has
two modes:

    full  — GRIB is available: clip the grids, fetch exact ESAT table values,
            write efi_latest.json + per-CWA grids/*.json.
    esat  — GRIB is missing: fetch fresh ESAT table values, carry the
            last-good grids and CWA-clipped values forward unchanged, and
            flag the output as grids_stale so the frontend warns the user.

Mode is auto-detected from FTP availability; force it with --mode.
A stdlib-only `--mode check` prints CI gating output (new_data / mode) without
importing the heavy scientific stack.

Run every 30 min via the efi-daily workflow.

Data sources
------------
GRIB:  https://ftp.wpc.ncep.noaa.gov/efi/   (gridded, unreliable)
ESAT:  https://satable.ncep.noaa.gov/        (per-CWA tables, reliable)

Dependencies (full mode only)
-----------------------------
    pip install cfgrib xarray numpy geopandas shapely requests

On some systems you also need the eccodes library:
    - macOS: brew install eccodes
    - Ubuntu: sudo apt install libeccodes-dev
"""

# NOTE: keep module-level imports to the standard library only, so that
# `--mode check` runs on a bare Python (CI gating step) without cfgrib,
# geopandas, numpy, etc. installed.  Heavy libs are imported lazily inside
# the functions that need them.
import json
import os
import sys
import glob
import ftplib
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Where to store downloaded GRIB2 files and output JSON.
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
WEB_DIR = PROJECT_DIR / "web"
JSON_DIR = WEB_DIR / "data"

# The committed, deployed data (assets/efi at the repo root).  In ESAT-only
# mode we read the last-good run from here to carry grids/clipped values
# forward, since the local web/data dir is empty on a fresh CI checkout.
DEPLOYED_DIR = PROJECT_DIR.parent / "assets" / "efi"
DEPLOYED_LATEST_JSON = DEPLOYED_DIR / "efi_latest.json"

# WPC FTP server
FTP_HOST = "ftp.wpc.ncep.noaa.gov"
FTP_DIR = "/efi"

# Durable R2 mirror of the GRIB, maintained by grib_catcher.py on the Mac
# Studio (the WPC FTP drops the file quickly; the catcher grabs it the moment
# it appears and stashes it here).  Read publicly, no creds needed.
R2_EFI_BASE = "https://hrrr-data.alexcooke.co/efi-grib"

# ECMWF EFI is produced from the 00Z and 12Z ensemble runs.
EFI_CYCLE_HOURS = (0, 12)

# NWS CWA shapefile — downloaded once, cached locally.
CWA_SHAPEFILE_URL = (
    "https://www.weather.gov/source/gis/Shapefiles/WSOM/w_18mr25.zip"
)
CWA_SHAPEFILE_LOCAL = DATA_DIR / "cwa_shapefile"

# Human-readable names for each EFI variable.
EFI_PARAM_NAMES = {
    "capei":  "CAPE",
    "capesi": "CAPES",
    "tpi":    "Total Precipitation",
    "t2i":    "2m Temperature",
    "mn2ti":  "Min Temperature",
    "mx2ti":  "Max Temperature",
    "ws10i":  "10m Wind Speed",
    "fg10i":  "10m Wind Gust",
    "sfi":    "Snowfall",
}

# Which CWAs to process. Set to None to process ALL CWAs.
# For a lightweight run, set to a list: ["HNX", "MEG", "OHX"]
TARGET_CWAS = None


# ---------------------------------------------------------------------------
# Step 1: Download the latest GRIB2 from WPC FTP
# ---------------------------------------------------------------------------

def fetch_latest_grib():
    """
    Get the latest EFI GRIB2 to DATA_DIR.  Tries the WPC FTP first; if it has
    no GRIB (the common case — it drops the file quickly), falls back to the
    durable R2 mirror maintained by grib_catcher.py.  Returns the local path,
    or None if neither source has it (caller then goes ESAT-only).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _fetch_grib_from_ftp()
    if path is not None:
        return path
    print("  WPC FTP had no GRIB — checking R2 mirror (grib-catcher) ...")
    return _fetch_grib_from_r2()


def _fetch_grib_from_r2():
    """Pull the catcher's mirrored GRIB from R2 via its public URL (stdlib)."""
    import urllib.request

    try:
        name = urllib.request.urlopen(
            f"{R2_EFI_BASE}/latest.txt", timeout=20
        ).read().decode().strip()
    except Exception as e:
        print(f"  R2 mirror unavailable: {e}")
        return None
    if not name:
        print("  R2 mirror has no pointer yet.")
        return None

    local_path = DATA_DIR / name
    if local_path.exists():
        print(f"  Already have {name} (from R2 mirror).")
        return local_path

    print(f"  Pulling {name} from R2 mirror ...")
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".part")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        urllib.request.urlretrieve(f"{R2_EFI_BASE}/latest.grb2", str(tmp_path))
        tmp_path.replace(local_path)
    except Exception as e:
        print(f"  R2 download failed: {e}")
        tmp_path.unlink(missing_ok=True)
        return None
    print(f"  Saved {local_path}  ({local_path.stat().st_size / 1e6:.1f} MB) from R2")
    return local_path


def _fetch_grib_from_ftp():
    """
    Connects to the WPC FTP server, finds the most recent EFI GRIB2
    file, and downloads it to DATA_DIR.  Returns the local file path,
    or None if the FTP server has no GRIB (the common failure mode —
    the caller then falls back to the R2 mirror, then ESAT-only).
    """
    print(f"Connecting to {FTP_HOST} ...")
    try:
        ftp = ftplib.FTP(FTP_HOST, timeout=60)
        ftp.login()  # anonymous
        ftp.cwd(FTP_DIR)
    except Exception as e:
        print(f"  FTP connection failed: {e}")
        return None

    # List files and pick the newest .grb2 / .grib2
    files = []
    try:
        ftp.retrlines("NLST", files.append)
    except Exception as e:
        print(f"  FTP listing failed: {e}")
        try:
            ftp.quit()
        except Exception:
            pass
        return None

    grib_files = [f for f in files if f.endswith((".grb2", ".grib2"))]
    if not grib_files:
        print("  No GRIB2 files on FTP server (will fall back to ESAT-only).")
        try:
            ftp.quit()
        except Exception:
            pass
        return None

    # Sort by name (they typically include a date stamp) — newest last.
    grib_files.sort()
    target = grib_files[-1]

    local_path = DATA_DIR / target
    if local_path.exists():
        print(f"Already have {target}, skipping download.")
        ftp.quit()
        return local_path

    # Download to a temp file first, then atomically rename, so an
    # interrupted transfer never leaves a half-written GRIB that the
    # exists() check above would treat as complete next run.
    print(f"Downloading {target} ...")
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".part")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_path, "wb") as fh:
            ftp.retrbinary(f"RETR {target}", fh.write)
        tmp_path.replace(local_path)
    except Exception as e:
        print(f"  Download failed: {e}")
        tmp_path.unlink(missing_ok=True)
        try:
            ftp.quit()
        except Exception:
            pass
        return None

    ftp.quit()
    print(f"Saved to {local_path}  ({local_path.stat().st_size / 1e6:.1f} MB)")
    return local_path


# ---------------------------------------------------------------------------
# Step 2: Load the CWA shapefile
# ---------------------------------------------------------------------------

def load_cwa_polygons():
    """
    Downloads (once) and loads the NWS CWA boundary shapefile.
    Returns a GeoDataFrame with CWA identifiers and geometry.
    """
    import geopandas as gpd

    CWA_SHAPEFILE_LOCAL.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded.
    shp_files = list(CWA_SHAPEFILE_LOCAL.glob("*.shp"))
    if not shp_files:
        print("Downloading CWA shapefile ...")
        import requests, zipfile, io
        resp = requests.get(CWA_SHAPEFILE_URL, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(CWA_SHAPEFILE_LOCAL)
        shp_files = list(CWA_SHAPEFILE_LOCAL.glob("*.shp"))

    gdf = gpd.read_file(shp_files[0])

    # The CWA identifier column varies by shapefile version.
    # Common names: 'CWA', 'WFO', 'SITE_ID', 'ID'
    id_col = None
    for candidate in ["CWA", "WFO", "SITE_ID", "ID"]:
        if candidate in gdf.columns:
            id_col = candidate
            break

    if id_col is None:
        print(f"WARNING: Could not find CWA identifier column.")
        print(f"  Available columns: {list(gdf.columns)}")
        # Fall back to first string-like column
        for col in gdf.columns:
            if gdf[col].dtype == object and col != "geometry":
                id_col = col
                break

    gdf = gdf.rename(columns={id_col: "cwa"})
    gdf["cwa"] = gdf["cwa"].str.upper().str.strip()

    # Ensure WGS84
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    print(f"Loaded {len(gdf)} CWA polygons (id column: '{id_col}')")
    return gdf


# ---------------------------------------------------------------------------
# Step 3: Load EFI and SOT fields from GRIB2
# ---------------------------------------------------------------------------

def load_efi_sot(grib_path):
    """
    Loads EFI and SOT datasets from the GRIB2 file using cfgrib.
    Returns (efi_ds, sot_ds) as xarray Datasets.

    Different parameters have different step counts (e.g. 7 vs 10 steps),
    so we use cfgrib.open_datasets() which returns one dataset per group
    of compatible variables, then merge them onto a common step axis.

    SOT fields may also have a 'number' dimension (percentiles 10, 90).
    We select the 90th percentile (upper tail) to get a 2D field.
    """
    import cfgrib
    import xarray as xr

    print("Loading EFI fields ...")
    efi_datasets = cfgrib.open_datasets(
        grib_path,
        backend_kwargs={"filter_by_keys": {"dataType": "efi"}},
    )
    # Merge all EFI sub-datasets. Variables with different step counts
    # get NaN-filled where steps don't overlap.
    efi_ds = xr.merge(efi_datasets, compat="override", join="outer")
    print(f"  EFI variables: {list(efi_ds.data_vars)}")
    print(f"  Steps: {efi_ds.step.values if 'step' in efi_ds.dims else 'scalar'}")

    print("Loading SOT fields ...")
    sot_datasets = cfgrib.open_datasets(
        grib_path,
        backend_kwargs={"filter_by_keys": {"dataType": "sot"}},
    )
    sot_ds = xr.merge(sot_datasets, compat="override", join="outer")

    # SOT may have a 'number' dimension (percentile 10 = lower tail,
    # 90 = upper tail). Select the 90th percentile for the upper tail.
    if "number" in sot_ds.dims:
        if 90 in sot_ds.number.values:
            sot_ds = sot_ds.sel(number=90)
            print("  Selected SOT 90th percentile (upper tail)")
        else:
            # Take the last (highest) percentile available.
            sot_ds = sot_ds.isel(number=-1)
            print(f"  Selected SOT number={sot_ds.number.values}")

    print(f"  SOT variables: {list(sot_ds.data_vars)}")

    return efi_ds, sot_ds


# ---------------------------------------------------------------------------
# Step 4: Clip to CWA and compute statistics
# ---------------------------------------------------------------------------

def clip_to_cwa(ds, cwa_geom, var_names=None):
    """
    Given an xarray Dataset on a regular lat/lon grid and a Shapely
    polygon (in WGS84), returns the max value of each variable within
    the polygon for each forecast step.

    Returns a dict: {variable_name: {step_hours: max_value, ...}, ...}
    """
    import numpy as np

    if var_names is None:
        var_names = list(ds.data_vars)

    # Get the bounding box of the CWA for fast pre-filtering.
    minx, miny, maxx, maxy = cwa_geom.bounds

    # Determine coordinate names (could be latitude/longitude or lat/lon).
    lat_name = "latitude" if "latitude" in ds.dims else "lat"
    lon_name = "longitude" if "longitude" in ds.dims else "lon"

    lats = ds[lat_name].values
    lons = ds[lon_name].values

    # GRIB longitudes are often 0-360; convert CWA bounds if needed.
    if lons.min() >= 0 and lons.max() > 180:
        # Convert CWA bounds from [-180,180] to [0,360]
        minx_360 = minx % 360
        maxx_360 = maxx % 360
        if minx_360 > maxx_360:
            # Wraps around the antimeridian — rare for CONUS
            lon_mask = (lons >= minx_360) | (lons <= maxx_360)
        else:
            lon_mask = (lons >= minx_360 - 1) & (lons <= maxx_360 + 1)
        convert_lon = True
    else:
        lon_mask = (lons >= minx - 1) & (lons <= maxx + 1)
        convert_lon = False

    lat_mask = (lats >= miny - 1) & (lats <= maxy + 1)

    # Subset the dataset to the bounding box.
    ds_sub = ds.isel(
        **{lat_name: lat_mask, lon_name: lon_mask}
    )

    sub_lats = ds_sub[lat_name].values
    sub_lons = ds_sub[lon_name].values

    # Convert grid lons back to [-180, 180] for Shapely intersection.
    if convert_lon:
        sub_lons_geo = np.where(sub_lons > 180, sub_lons - 360, sub_lons)
    else:
        sub_lons_geo = sub_lons

    # Build a boolean mask: True where the grid point falls inside the CWA.
    from shapely.geometry import Point
    from shapely.prepared import prep

    prepared_geom = prep(cwa_geom)
    mask_2d = np.zeros((len(sub_lats), len(sub_lons)), dtype=bool)
    for i, lat in enumerate(sub_lats):
        for j, lon in enumerate(sub_lons_geo):
            if prepared_geom.contains(Point(lon, lat)):
                mask_2d[i, j] = True

    if not mask_2d.any():
        return {}

    results = {}
    for var in var_names:
        if var not in ds_sub:
            continue
        da = ds_sub[var]
        # Squeeze out any leftover singleton dimensions.
        extra_dims = [d for d in da.dims if d not in (lat_name, lon_name, "step")]
        for d in extra_dims:
            if da.sizes[d] == 1:
                da = da.squeeze(d)
            else:
                da = None
                break
        if da is None:
            continue
        results[var] = {}

        if "step" in da.dims:
            for step_idx in range(len(da.step)):
                step_val = da.step.values[step_idx]
                # Convert timedelta to hours.
                if hasattr(step_val, "total_seconds"):
                    hours = int(step_val.total_seconds() / 3600)
                else:
                    hours = int(np.timedelta64(step_val, "h").astype(int))

                field = da.isel(step=step_idx).values
                if field.ndim != 2:
                    continue
                masked = np.where(mask_2d, field, np.nan)
                max_val = float(np.nanmax(masked))
                min_val = float(np.nanmin(masked))
                results[var][hours] = {
                    "max": round(max_val, 3) if np.isfinite(max_val) else None,
                    "min": round(min_val, 3) if np.isfinite(min_val) else None,
                }
        else:
            field = da.values
            if field.ndim != 2:
                continue
            masked = np.where(mask_2d, field, np.nan)
            max_val = float(np.nanmax(masked))
            min_val = float(np.nanmin(masked))
            results[var]["all"] = {
                "max": round(max_val, 3) if np.isfinite(max_val) else None,
                "min": round(min_val, 3) if np.isfinite(min_val) else None,
            }

    return results


def extract_grid_for_cwa(ds, cwa_geom, var_names=None, padding_deg=2.5):
    """
    Extracts the raw gridded values in a padded bounding box around a CWA.
    Returns a dict suitable for JSON serialization:
    {
        "lats": [list of latitudes],
        "lons": [list of longitudes],
        "cell_size": 0.5,
        "mask": [[bool, ...], ...],   # True = inside CWA
        "fields": {
            var_name: {
                step_hours: [[val, ...], ...],   # 2D array [lat][lon]
                ...
            }
        }
    }
    At 0.5° resolution this is ~20x20 cells for a typical CWA — very compact.
    """
    import numpy as np

    if var_names is None:
        var_names = list(ds.data_vars)

    minx, miny, maxx, maxy = cwa_geom.bounds

    lat_name = "latitude" if "latitude" in ds.dims else "lat"
    lon_name = "longitude" if "longitude" in ds.dims else "lon"

    lats = ds[lat_name].values
    lons = ds[lon_name].values

    # Handle 0-360 longitude convention.
    if lons.min() >= 0 and lons.max() > 180:
        minx_g = (minx - padding_deg) % 360
        maxx_g = (maxx + padding_deg) % 360
        if minx_g > maxx_g:
            lon_mask = (lons >= minx_g) | (lons <= maxx_g)
        else:
            lon_mask = (lons >= minx_g) & (lons <= maxx_g)
        convert_lon = True
    else:
        lon_mask = (lons >= minx - padding_deg) & (lons <= maxx + padding_deg)
        convert_lon = False

    lat_mask = (lats >= miny - padding_deg) & (lats <= maxy + padding_deg)

    ds_sub = ds.isel(**{lat_name: lat_mask, lon_name: lon_mask})

    sub_lats = ds_sub[lat_name].values
    sub_lons = ds_sub[lon_name].values

    if convert_lon:
        sub_lons_geo = np.where(sub_lons > 180, sub_lons - 360, sub_lons)
    else:
        sub_lons_geo = sub_lons

    # Build CWA interior mask for the subgrid.
    from shapely.geometry import Point
    from shapely.prepared import prep

    prepared_geom = prep(cwa_geom)
    mask_2d = []
    for i, lat in enumerate(sub_lats):
        row = []
        for j, lon in enumerate(sub_lons_geo):
            row.append(prepared_geom.contains(Point(lon, lat)))
        mask_2d.append(row)

    # Extract field values.
    fields = {}
    for var in var_names:
        if var not in ds_sub:
            continue
        da = ds_sub[var]
        # Squeeze out any leftover singleton dimensions (e.g. 'number').
        extra_dims = [d for d in da.dims if d not in (lat_name, lon_name, "step")]
        for d in extra_dims:
            if da.sizes[d] == 1:
                da = da.squeeze(d)
            else:
                # Multi-valued extra dim — skip this variable.
                da = None
                break
        if da is None:
            continue
        fields[var] = {}

        if "step" in da.dims:
            for step_idx in range(len(da.step)):
                step_val = da.step.values[step_idx]
                if hasattr(step_val, "total_seconds"):
                    hours = int(step_val.total_seconds() / 3600)
                else:
                    hours = int(np.timedelta64(step_val, "h").astype(int))

                grid = da.isel(step=step_idx).values
                if grid.ndim != 2:
                    continue
                fields[var][hours] = [
                    [
                        round(float(grid[i, j]), 3)
                        if not np.isnan(grid[i, j]) else None
                        for j in range(grid.shape[1])
                    ]
                    for i in range(grid.shape[0])
                ]
        else:
            grid = da.values
            if grid.ndim != 2:
                continue
            fields[var]["all"] = [
                [
                    round(float(grid[i, j]), 3)
                    if not np.isnan(grid[i, j]) else None
                    for j in range(grid.shape[1])
                ]
                for i in range(grid.shape[0])
            ]

    return {
        "lats": [round(float(l), 4) for l in sub_lats],
        "lons": [round(float(l), 4) for l in sub_lons_geo],
        "cell_size": 0.5,
        "mask": mask_2d,
        "fields": fields,
    }


def cwa_geom_to_geojson(geom):
    """
    Converts a Shapely geometry to a GeoJSON-compatible dict of
    coordinates. Handles both Polygon and MultiPolygon.
    """
    from shapely.geometry import mapping
    gj = mapping(geom)
    return gj


def compute_full_domain_stats(ds, var_names=None):
    """
    Computes the max value of each variable across the FULL image domain
    for each forecast step.  This reproduces the original ESAT table behavior.
    """
    import numpy as np

    if var_names is None:
        var_names = list(ds.data_vars)

    lat_name = "latitude" if "latitude" in ds.dims else "lat"
    lon_name = "longitude" if "longitude" in ds.dims else "lon"

    results = {}
    for var in var_names:
        if var not in ds:
            continue
        da = ds[var]
        # Squeeze out any leftover singleton dimensions.
        extra_dims = [d for d in da.dims if d not in (lat_name, lon_name, "step")]
        for d in extra_dims:
            if da.sizes[d] == 1:
                da = da.squeeze(d)
            else:
                da = None
                break
        if da is None:
            continue
        results[var] = {}

        if "step" in da.dims:
            for step_idx in range(len(da.step)):
                step_val = da.step.values[step_idx]
                if hasattr(step_val, "total_seconds"):
                    hours = int(step_val.total_seconds() / 3600)
                else:
                    hours = int(np.timedelta64(step_val, "h").astype(int))

                field = da.isel(step=step_idx).values
                max_val = float(np.nanmax(field))
                min_val = float(np.nanmin(field))
                results[var][hours] = {
                    "max": round(max_val, 3) if np.isfinite(max_val) else None,
                    "min": round(min_val, 3) if np.isfinite(min_val) else None,
                }
        else:
            field = da.values
            max_val = float(np.nanmax(field))
            min_val = float(np.nanmin(field))
            results[var]["all"] = {
                "max": round(max_val, 3) if np.isfinite(max_val) else None,
                "min": round(min_val, 3) if np.isfinite(min_val) else None,
            }

    return results


def _regional_max_from_grid(grid_data):
    """
    Fallback: computes the max value for each variable/step over the entire
    padded grid region. Used when the ESAT endpoint is unavailable.
    """
    results = {}
    if not grid_data or "fields" not in grid_data:
        return results

    for var, step_dict in grid_data["fields"].items():
        results[var] = {}
        for step_key, grid_2d in step_dict.items():
            vals = []
            for row in grid_2d:
                for v in row:
                    if v is not None:
                        vals.append(v)
            if vals:
                results[var][step_key] = {
                    "max": round(max(vals), 3),
                    "min": round(min(vals), 3),
                }
            else:
                results[var][step_key] = {"max": None, "min": None}
    return results


# ESAT parameter name → our internal GRIB variable name.
ESAT_TO_GRIB = {
    "CAPE": "capei", "CAPES": "capesi", "10WS": "ws10i", "10WG": "fg10i",
    "TMAX": "mx2ti", "TMIN": "mn2ti", "QPF": "tpi", "SNOW": "sfi",
}


def fetch_esat_table(cwa_id, init_time_str):
    """
    Fetches the exact ESAT table values from the NCEP endpoint.
    Two-step process matching the ESAT's own JS:
      1. POST to generateEFItable.php to create the data file
      2. GET parseToJson.php to read the generated data

    init_time_str should be like "2026040212" (YYYYMMDDHH).
    """
    import requests

    yr = init_time_str[0:4]
    mo = init_time_str[4:6]
    dy = init_time_str[6:8]
    hr = init_time_str[8:10]

    # Step 1: Trigger table generation.
    gen_url = "https://satable.ncep.noaa.gov/phpService/generateEFItable.php"
    try:
        requests.post(gen_url, data={
            "yr": yr, "mo": mo, "dy": dy, "hr": hr,
            "r": cwa_id.lower(), "interval": "24", "model": "ens",
            "cachetime": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        }, timeout=15)
    except Exception:
        pass  # Generation may fail but the file might already exist.

    # Brief pause to let the server write the file.
    time.sleep(1.0)

    # Step 2: Read the generated JSON (with one retry).
    parse_url = (
        f"https://satable.ncep.noaa.gov/efi/php/parseToJson.php"
        f"?pt={cwa_id.lower()}_ens_efi_24h_{init_time_str}.txt"
    )
    data = None
    for attempt in range(2):
        try:
            resp = requests.get(parse_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(2.0)  # retry after 2s
            else:
                print(f"ESAT fetch failed for {cwa_id}: {e}")
                return None

    if data is None:
        return None

    results = {}
    for esat_name, step_dict in data.get("params", {}).items():
        grib_name = ESAT_TO_GRIB.get(esat_name)
        if not grib_name:
            continue
        results[grib_name] = {}
        for step_str, val_str in step_dict.items():
            try:
                step_hours = int(step_str)
                val = float(val_str)
                results[grib_name][step_hours] = {
                    "max": round(val, 2),
                    "min": round(val, 2),
                }
            except (ValueError, TypeError):
                continue

    return results if results else None


# ---------------------------------------------------------------------------
# ESAT run discovery + bulk fetch (works with no GRIB at all)
# ---------------------------------------------------------------------------

def _esat_candidates(now=None, lookback_days=4):
    """Yield YYYYMMDDHH init strings, newest first, for recent EFI cycles."""
    if now is None:
        now = datetime.now(timezone.utc)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cycles = []
    for d in range(lookback_days + 1):
        base = day0 - timedelta(days=d)
        for hh in EFI_CYCLE_HOURS:
            t = base.replace(hour=hh)
            if t <= now:
                cycles.append(t)
    cycles.sort(reverse=True)
    for t in cycles:
        yield t.strftime("%Y%m%d%H")


def latest_esat_init(probe):
    """
    Find the newest EFI run available on ESAT.  `probe(cwa, init_str)` must
    return truthy table data when that run exists, else None.  Returns
    (init_str, seed_data) for the first available cycle, or (None, None).
    """
    for init_str in _esat_candidates():
        data = probe("HNX", init_str)
        if data:
            print(f"  Latest ESAT run: {init_str}")
            return init_str, data
    return None, None


def fetch_all_esat(cwa_ids, esat_init_str, seed=None):
    """Fetch ESAT table values for many CWAs in parallel. Returns {cwa: data}."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    esat_cache = {}
    remaining = list(cwa_ids)
    if seed is not None and "HNX" in remaining:
        esat_cache["HNX"] = seed
        remaining = [c for c in remaining if c != "HNX"]

    def _fetch(cwa):
        return cwa, fetch_esat_table(cwa, esat_init_str)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, c): c for c in remaining}
        done = 0
        for fut in as_completed(futures):
            cwa, data = fut.result()
            if data:
                esat_cache[cwa] = data
            done += 1
            if done % 20 == 0:
                print(f"    ... {done}/{len(remaining)}")

    # Sequential retry for CONUS CWAs that failed (skip territories).
    territories = {"PPG", "PQE", "PQW", "GUM", "HFO", "SJU", "KEY"}
    failed = [c for c in remaining if c not in esat_cache and c not in territories]
    if failed:
        print(f"  Retrying {len(failed)} failed CWAs sequentially ...")
        for cwa in failed:
            time.sleep(1.0)
            data = fetch_esat_table(cwa, esat_init_str)
            if data:
                esat_cache[cwa] = data

    return esat_cache


# ---------------------------------------------------------------------------
# Full pipeline (GRIB available)
# ---------------------------------------------------------------------------

def run_full(grib_path, cwa_gdf):
    import numpy as np

    # Load EFI and SOT
    efi_ds, sot_ds = load_efi_sot(grib_path)

    efi_vars = [v for v in efi_ds.data_vars if v in EFI_PARAM_NAMES]
    sot_vars = list(sot_ds.data_vars)

    # Get model init time from the dataset.
    init_time = None
    if "time" in efi_ds.coords:
        t = efi_ds.time.values
        if hasattr(t, "item"):
            t = t.item()
        init_time = str(t)

    # Get available forecast steps (hours).
    steps = []
    if "step" in efi_ds.dims:
        for s in efi_ds.step.values:
            if hasattr(s, "total_seconds"):
                steps.append(int(s.total_seconds() / 3600))
            else:
                steps.append(int(np.timedelta64(s, "h").astype(int)))

    # Compute init time string (YYYYMMDDHH) for ESAT endpoint.
    esat_init_str = None
    if init_time:
        try:
            if init_time.isdigit():
                init_dt = datetime.fromtimestamp(int(init_time) / 1e9, tz=timezone.utc)
            else:
                init_dt = datetime.fromisoformat(init_time.replace("Z", "+00:00"))
            esat_init_str = init_dt.strftime("%Y%m%d%H")
            print(f"  ESAT init string: {esat_init_str}")
        except Exception as e:
            print(f"  Could not parse init time for ESAT: {e}")

    if TARGET_CWAS:
        process_cwas = cwa_gdf[cwa_gdf["cwa"].isin(TARGET_CWAS)]
    else:
        process_cwas = cwa_gdf

    cwa_results = {}
    cwa_grids = {}
    cwa_polygons = {}

    # Fetch ESAT table values for all CWAs (exact match).
    print("\nFetching ESAT table values ...")
    esat_cache = {}
    if esat_init_str:
        test_cwa = TARGET_CWAS[0] if TARGET_CWAS else "HNX"
        print(f"  Testing ESAT availability with {test_cwa} ...")
        test_data = fetch_esat_table(test_cwa, esat_init_str)
        if test_data:
            all_cwas = [row["cwa"] for _, row in process_cwas.iterrows()]
            if test_cwa not in all_cwas:
                all_cwas = [test_cwa] + all_cwas
            print(f"  ESAT available — fetching {len(all_cwas)} CWAs in parallel ...")
            esat_cache = fetch_all_esat(all_cwas, esat_init_str, seed=test_data)
            print(f"  Fetched ESAT values for {len(esat_cache)} CWAs")
        else:
            print("  ESAT not available for this run — falling back to grid-based regional max")
    else:
        print("  No init time — falling back to grid-based regional max")

    print("\nClipping to CWA polygons ...")
    for idx, row in process_cwas.iterrows():
        cwa_id = row["cwa"]
        geom = row.geometry
        print(f"  Clipping to {cwa_id} ...", end=" ", flush=True)

        efi_clipped = clip_to_cwa(efi_ds, geom, efi_vars)
        sot_clipped = clip_to_cwa(sot_ds, geom, sot_vars)

        if efi_clipped:
            # Extract gridded data for the map.
            grid_efi = extract_grid_for_cwa(efi_ds, geom, efi_vars)
            grid_sot = extract_grid_for_cwa(sot_ds, geom, sot_vars)

            # Use exact ESAT values if available, else fallback to grid.
            if cwa_id in esat_cache:
                regional_efi = esat_cache[cwa_id]
            else:
                regional_efi = _regional_max_from_grid(grid_efi)
            regional_sot = _regional_max_from_grid(grid_sot)

            cwa_results[cwa_id] = {
                "efi": efi_clipped,
                "sot": sot_clipped,
                "regional_efi": regional_efi,
                "regional_sot": regional_sot,
            }

            cwa_grids[cwa_id] = {"efi": grid_efi, "sot": grid_sot}
            cwa_polygons[cwa_id] = cwa_geom_to_geojson(geom)

            print("OK")
        else:
            print("no grid points in CWA")

    # Build the output JSON.
    esat_available = len(esat_cache) > 0
    output = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "init_time": init_time,
        "esat_init": esat_init_str,
        "steps_hours": sorted(steps),
        "source_file": grib_path.name,
        "param_names": EFI_PARAM_NAMES,
        "esat_available": esat_available,
        "esat_cwa_count": len(esat_cache),
        "grids_stale": False,
        "grids_init_time": init_time,
        "grids_source_file": grib_path.name,
        "grids_steps_hours": sorted(steps),
        "by_cwa": cwa_results,
        "cwa_list": sorted(cwa_results.keys()),
    }

    out_path = JSON_DIR / "efi_latest.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    # Write per-CWA grid + polygon files (loaded on demand by the frontend).
    grids_dir = JSON_DIR / "grids"
    grids_dir.mkdir(parents=True, exist_ok=True)
    for cwa_id in cwa_grids:
        cwa_file = grids_dir / f"{cwa_id}.json"
        with open(cwa_file, "w") as f:
            json.dump({
                "efi": cwa_grids[cwa_id]["efi"],
                "sot": cwa_grids[cwa_id]["sot"],
                "polygon": cwa_polygons[cwa_id],
            }, f)

    print(f"\nWrote {out_path}  ({out_path.stat().st_size / 1e3:.1f} KB)")
    print(f"Wrote {len(cwa_grids)} per-CWA grid files to {grids_dir}/")
    print(f"CWAs processed: {len(cwa_results)}")
    print("Done.")


# ---------------------------------------------------------------------------
# ESAT-only pipeline (GRIB missing — table updates, grids carried forward)
# ---------------------------------------------------------------------------

def run_esat_only():
    """
    Produce a fresh efi_latest.json from ESAT alone.  The per-CWA grids/*.json
    and the CWA-clipped (efi/sot) table values are NOT recomputed — they're
    carried forward from the last good full run (read from the deployed
    assets) and the output is flagged grids_stale so the frontend warns.
    Returns True if it wrote output, False if no ESAT data was available.
    """
    prev = {}
    if DEPLOYED_LATEST_JSON.exists():
        try:
            prev = json.loads(DEPLOYED_LATEST_JSON.read_text())
        except Exception as e:
            print(f"  Could not read deployed JSON ({DEPLOYED_LATEST_JSON}): {e}")

    esat_init_str, seed = latest_esat_init(fetch_esat_table)
    if not esat_init_str:
        print("  No EFI run available on ESAT either.")
        return False

    # Never regress. If the deployed run is already at this cycle or newer (a
    # full GRIB run for the same cycle, or ESAT briefly serving an older one),
    # refuse to overwrite it with an ESAT-only update — a full grid is strictly
    # better than blanked grids for the same cycle, and we must never go back.
    dep_cycle = int(prev["esat_init"]) if str(prev.get("esat_init", "")).isdigit() else 0
    if dep_cycle and int(esat_init_str) <= dep_cycle:
        print(f"  Deployed cycle {dep_cycle} >= ESAT {esat_init_str} — not downgrading; nothing to do.")
        return False

    # CWA list: reuse the last deployed list (grids already on disk for these).
    cwa_ids = prev.get("cwa_list")
    if not cwa_ids:
        cwa_ids = sorted(p.stem for p in (DEPLOYED_DIR / "grids").glob("*.json"))
    if TARGET_CWAS:
        cwa_ids = [c for c in cwa_ids if c in TARGET_CWAS]
    if "HNX" not in cwa_ids:
        cwa_ids = ["HNX"] + list(cwa_ids)

    print(f"  Fetching ESAT values for {len(cwa_ids)} CWAs (init {esat_init_str}) ...")
    esat_cache = fetch_all_esat(cwa_ids, esat_init_str, seed=seed)
    print(f"  Got ESAT values for {len(esat_cache)} CWAs")

    # Forecast steps come from the ESAT tables themselves.
    steps = set()
    for data in esat_cache.values():
        for var_steps in data.values():
            steps.update(int(h) for h in var_steps.keys())
    steps = sorted(steps)

    # Grid metadata from the most recent FULL run, propagated forward through
    # successive ESAT-only runs. The ESAT Region table moves to the new cycle,
    # but the precise CWA-clipped table + map keep the last full grid, clearly
    # labeled a cycle behind (grids_init_time/_steps_hours), until that cycle's
    # GRIB posts. Only if there's NO prior grid at all do we blank (cold start).
    if prev.get("grids_stale", True):
        grids_init = prev.get("grids_init_time")
        grids_src = prev.get("grids_source_file")
        grids_steps = prev.get("grids_steps_hours")
    else:  # prev was a full run
        grids_init = prev.get("grids_init_time") or prev.get("init_time")
        grids_src = prev.get("grids_source_file") or prev.get("source_file")
        grids_steps = prev.get("grids_steps_hours") or prev.get("steps_hours")
    have_grid = bool(grids_init)

    prev_by_cwa = prev.get("by_cwa", {})
    by_cwa = {}
    for cwa in sorted(set(list(esat_cache.keys()) + list(prev_by_cwa.keys()))):
        pc = prev_by_cwa.get(cwa, {})
        entry = {
            # Carried CWA clip from the last full run (one cycle behind), or
            # blank if we've never had a grid for this CWA.
            "efi": pc.get("efi", {}) if have_grid else {},
            "sot": pc.get("sot", {}) if have_grid else {},
            # ESAT Region values are FRESH at the new cycle (broader area).
            "regional_efi": esat_cache.get(cwa, {}),
            "regional_sot": pc.get("regional_sot", {}) if have_grid else {},
        }
        if entry["regional_efi"] or entry["efi"]:
            by_cwa[cwa] = entry

    init_dt = datetime.strptime(esat_init_str, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    output = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "init_time": init_dt.isoformat(),
        "esat_init": esat_init_str,
        "steps_hours": steps,
        "source_file": f"ESAT {esat_init_str} — GRIB pending on WPC",
        "param_names": EFI_PARAM_NAMES,
        "esat_available": True,
        "esat_cwa_count": len(esat_cache),
        # ESAT Region table is at this (new) cycle; the grid/CWA table is the
        # carried full run identified by grids_init_time. grids_stale=True tells
        # the frontend to label the CWA table + map with the grid's own cycle.
        "grids_stale": True,
        "grids_init_time": grids_init,
        "grids_source_file": grids_src,
        "grids_steps_hours": grids_steps,
        "by_cwa": by_cwa,
        "cwa_list": sorted(by_cwa.keys()),
    }

    JSON_DIR.mkdir(parents=True, exist_ok=True)
    out_path = JSON_DIR / "efi_latest.json"
    out_path.write_text(json.dumps(output, indent=2))

    print(f"\nESAT-only update written: {out_path}")
    if have_grid:
        print(f"  ESAT Region live @ {esat_init_str}; grid/CWA table carried from {grids_src} (cycle behind)")
    else:
        print(f"  ESAT Region live @ {esat_init_str}; no prior grid — CWA table + map blank")
    print(f"  CWAs: {len(by_cwa)}")
    return True


# ---------------------------------------------------------------------------
# CI gating check (stdlib only — no heavy deps)
# ---------------------------------------------------------------------------

def run_check():
    """
    Decide whether there is new data worth running for, without importing the
    scientific stack.  Prints `new_data=` and `mode=` lines (GITHUB_OUTPUT
    format).  mode is one of full | esat | none.
    """
    import urllib.request
    import urllib.parse

    # 1. Latest GRIB on the WPC FTP (may be absent).
    latest_grib = ""
    try:
        ftp = ftplib.FTP(FTP_HOST, timeout=30)
        ftp.login()
        ftp.cwd(FTP_DIR)
        files = []
        ftp.retrlines("NLST", files.append)
        ftp.quit()
        gribs = sorted(f for f in files if f.endswith((".grb2", ".grib2")))
        latest_grib = gribs[-1] if gribs else ""
    except Exception as e:
        print(f"# FTP check failed: {e}")

    # If the FTP has nothing, the catcher may still have mirrored a GRIB to R2.
    if not latest_grib:
        try:
            latest_grib = urllib.request.urlopen(
                f"{R2_EFI_BASE}/latest.txt", timeout=15
            ).read().decode().strip()
        except Exception:
            pass

    # 2. Latest ESAT run (stdlib probe — no requests).
    def _probe(cwa, init_str):
        gen = "https://satable.ncep.noaa.gov/phpService/generateEFItable.php"
        body = urllib.parse.urlencode({
            "yr": init_str[:4], "mo": init_str[4:6], "dy": init_str[6:8],
            "hr": init_str[8:10], "r": cwa.lower(), "interval": "24",
            "model": "ens", "cachetime": "1",
        }).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(gen, data=body), timeout=15
            ).read()
        except Exception:
            pass
        time.sleep(1.0)
        url = ("https://satable.ncep.noaa.gov/efi/php/parseToJson.php"
               f"?pt={cwa.lower()}_ens_efi_24h_{init_str}.txt")
        try:
            raw = urllib.request.urlopen(url, timeout=15).read()
            return (json.loads(raw).get("params")) or None
        except Exception:
            return None

    latest_esat, _ = latest_esat_init(_probe)

    # 3. What's currently deployed.
    dep = {}
    if DEPLOYED_LATEST_JSON.exists():
        try:
            dep = json.loads(DEPLOYED_LATEST_JSON.read_text())
        except Exception:
            pass
    dep_source = dep.get("source_file", "")
    dep_esat = dep.get("esat_init", "")
    dep_cycle = int(dep_esat) if str(dep_esat).isdigit() else 0

    # Only ever move FORWARD. A GRIB that isn't the deployed one → full run.
    # An ESAT cycle is only worth an esat-only run if it is STRICTLY NEWER than
    # what's deployed — otherwise a flaky ESAT serving an older cycle (or the
    # same cycle we already have as a full grid run) would regress/clobber it.
    if latest_grib and latest_grib != dep_source:
        mode, new = "full", "true"
    elif latest_esat and str(latest_esat).isdigit() and int(latest_esat) > dep_cycle:
        mode, new = "esat", "true"
    else:
        mode, new = "none", "false"

    print(f"# grib={latest_grib or '-'} esat={latest_esat or '-'} "
          f"dep_source={dep_source or '-'} dep_esat={dep_esat or '-'}")
    print(f"new_data={new}")
    print(f"mode={mode}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="EFI/SOT ingest + processing")
    parser.add_argument(
        "--mode", choices=["auto", "full", "esat", "check"], default="auto",
        help="auto: detect GRIB and pick full/esat; check: stdlib CI gate.",
    )
    args = parser.parse_args()

    if args.mode == "check":
        sys.exit(run_check())

    JSON_DIR.mkdir(parents=True, exist_ok=True)

    if args.mode == "esat":
        ok = run_esat_only()
        sys.exit(0 if ok else 0)  # a normal upstream gap is not an error

    # auto or full: try the GRIB first.
    grib_path = fetch_latest_grib()

    if grib_path is not None:
        cwa_gdf = load_cwa_polygons()
        run_full(grib_path, cwa_gdf)
        return

    if args.mode == "full":
        print("ERROR: --mode full but no GRIB available on FTP.")
        sys.exit(1)

    # auto + no GRIB → ESAT-only (carry grids forward).
    print("No GRIB on FTP — running ESAT-only update (grids carried forward).")
    ok = run_esat_only()
    if not ok:
        print("No EFI data from FTP or ESAT — leaving deployed data unchanged.")
    # exit 0 either way: a missing upstream run is normal, not a failure.


if __name__ == "__main__":
    main()
