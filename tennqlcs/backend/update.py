#!/usr/bin/env python3
"""
OHX QLCS Tornado Detection — Real-Time Backend
=================================================
Polls MRMS S3 for new azshear/reflectivity scans, processes them,
renders overlay images, and outputs track JSON for the web viewer.

Designed to run in a GitHub Actions loop with 10-second polling.

Usage:
    python3 update.py              # Single update cycle
    python3 update.py --loop       # Continuous polling (10s interval)
    python3 update.py --test-render  # Render test frame from cache
"""

import argparse
import gc
import gzip
import json
import logging
import os
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ohx-qlcs")

# ─── Configuration ──────────────────────────────────────────────────────

CWA = {
    "name": "OHX",
    "lat_min": 34.99, "lat_max": 36.68,
    "lon_min": -88.07, "lon_max": -84.66,
}

# Display extends beyond CWA so storms approaching/leaving are visible
DISPLAY_PAD = 1.0  # degrees beyond CWA in each direction
DISPLAY = {
    "lat_min": CWA["lat_min"] - DISPLAY_PAD,
    "lat_max": CWA["lat_max"] + DISPLAY_PAD,
    "lon_min": CWA["lon_min"] - DISPLAY_PAD,
    "lon_max": CWA["lon_max"] + DISPLAY_PAD,
}

S3_BASE = "https://noaa-mrms-pds.s3.amazonaws.com"
MRMS_PRODUCTS = {
    "azshear": "CONUS/MergedAzShear_0-2kmAGL_00.50",
    "reflectivity": "CONUS/MergedBaseReflectivity_00.50",
}

AZ_THRESH = 6.0          # Minimum azshear for peak detection (x10^-3 s^-1)
REFL_MASK = 35.0          # dBZ threshold for convective masking
MAX_FILTER_SIZE = 5       # Local max filter (pixels, ~5 km)
TOP_N_TRACKS = 12         # Number of tracks to display
MATCH_RADIUS_KM = 15.0    # Track matching radius across scans
SCAN_BUFFER = 5           # Number of scans to retain in rolling window
POLL_INTERVAL = 10        # Seconds between S3 checks

# ─── HRRR Environment Scoring ──────────────────────────────────────────

HRRR_S3 = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
HRRR_BUFFER_MIN = 105     # Minutes after init before HRRR run completes

# T-3h scoring params (AUC 0.883, best discrimination)
SCORING_PARAMS = [
    {"var": "sfc_rh",          "weight": 0.957, "thresh": 75.4315,
     "dir": ">", "extreme": 96.2494},
    {"var": "mean_wind_0_6_u", "weight": 0.939, "thresh": 22.3229,
     "dir": ">", "extreme": 42.5974},
    {"var": "sfc_wspd",        "weight": 0.938, "thresh": 9.5598,
     "dir": ">", "extreme": 15.6228},
    {"var": "sfc_temp",        "weight": 0.921, "thresh": 22.0522,
     "dir": "<", "extreme": 13.6255},
    {"var": "sbcin",           "weight": 0.719, "thresh": -104.5635,
     "dir": ">", "extreme": 0.0},
]
SUM_WEIGHTS = sum(p["weight"] for p in SCORING_PARAMS)

# GRIB .idx search strings
HRRR_SURFACE_FIELDS = {
    "sfc_rh":   ":RH:2 m above ground:",
    "sfc_temp": ":TMP:2 m above ground:",
    "sfc_u10":  ":UGRD:10 m above ground:",
    "sfc_v10":  ":VGRD:10 m above ground:",
    "sbcin":    ":CIN:surface:",
}
UGRD_LEVELS = [1000, 925, 850, 775, 700, 600, 500]

OUTPUT_DIR = Path("docs/data")
STATE_FILE = Path("docs/data/state.json")

# ─── GRIB Reading ───────────────────────────────────────────────────────

def read_grib_to_array(filepath):
    """Read a GRIB2 file, return data + lat/lon arrays clipped to display.
    Uses pygrib.data() with lat/lon bounds to read only the needed subset."""

    # Decompress if gzipped
    actual_path = str(filepath)
    tmp_path = None
    if actual_path.endswith(".gz"):
        try:
            with gzip.open(filepath, "rb") as gz:
                decompressed = gz.read()
            tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
            tmp.write(decompressed)
            tmp.close()
            actual_path = tmp.name
            tmp_path = tmp.name
            del decompressed
        except Exception as e:
            log.warning(f"Decompression failed for {filepath}: {e}")
            return None

    try:
        import pygrib
        grbs = pygrib.open(actual_path)
        msg = grbs[1]

        # Use data() with geographic bounds — returns only the subset
        # MRMS lons are 0-360 in GRIB, convert our -180..180 bounds
        lon_min_360 = DISPLAY["lon_min"] % 360
        lon_max_360 = DISPLAY["lon_max"] % 360
        data, lats_2d, lons_2d = msg.data(
            lat1=DISPLAY["lat_min"], lat2=DISPLAY["lat_max"],
            lon1=lon_min_360, lon2=lon_max_360)
        grbs.close()
        del msg, grbs
        gc.collect()

        data = np.asarray(data, dtype=np.float32)
        lons_2d = np.asarray(lons_2d)
        lats_2d = np.asarray(lats_2d)
        if lons_2d.max() > 180:
            lons_2d = lons_2d - 360

        if lats_2d.ndim == 2:
            lats = lats_2d[:, 0].astype(np.float32)
            lons = lons_2d[0, :].astype(np.float32)
        else:
            lats = lats_2d.astype(np.float32)
            lons = lons_2d.astype(np.float32)
        del lats_2d, lons_2d

        return {"data": data, "lats": lats, "lons": lons}
    except Exception as e:
        log.warning(f"GRIB read failed for {filepath}: {e}")
        gc.collect()
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def download_and_read_grib(url):
    """Download a gzipped GRIB2 from URL, decompress, read."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OHX-QLCS/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            compressed = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning(f"Download failed: {e}")
        return None

    try:
        decompressed = gzip.decompress(compressed)
    except Exception:
        decompressed = compressed

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tmp:
        tmp.write(decompressed)
        tmp_path = tmp.name

    try:
        result = read_grib_to_array(tmp_path)
    finally:
        os.unlink(tmp_path)

    return result

# ─── S3 Polling ─────────────────────────────────────────────────────────

def list_s3_latest(product_path, max_keys=30):
    """List the most recent files in an S3 prefix using the REST API.
    Structure: CONUS/{Product}/{YYYYMMDD}/MRMS_{Product}_{YYYYMMDD}-{HHMMSS}.grib2.gz

    NOTE: S3 ``start-after`` returns keys ASCENDING from ~15 min ago, capped at
    ``max_keys``. So ``max_keys`` MUST exceed the number of files in that 15-min
    window or the listing stops before the newest file and the picked "latest"
    scan is stale. MergedAzShear/reflectivity publish ~every 2 min (~8/window),
    so 30 leaves wide margin. (Was 5 -> pinned the feed ~7 min behind real-time.)"""

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y%m%d")

    # Start listing from ~15 minutes ago to get only recent files
    start_time = now - timedelta(minutes=15)
    start_key = (f"{product_path}/{today}/MRMS_"
                 f"{product_path.split('/')[-1]}_{start_time.strftime('%Y%m%d-%H%M%S')}")

    try:
        list_url = (f"{S3_BASE}?list-type=2"
                    f"&prefix={product_path}/{today}/MRMS_"
                    f"&start-after={start_key}"
                    f"&max-keys={max_keys}")
        req = urllib.request.Request(list_url,
                                     headers={"User-Agent": "OHX-QLCS/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning(f"S3 listing failed: {e}")
        return []

    # Parse keys from XML
    keys = []
    for chunk in xml_text.split("<Key>")[1:]:
        key = chunk.split("</Key>")[0]
        if key.endswith(".grib2.gz"):
            keys.append(key)

    keys.sort()
    return keys[-max_keys:] if keys else []


def parse_mrms_timestamp(filename):
    """Extract datetime from MRMS filename like
    MRMS_MergedAzShear_0-2kmAGL_00.50_20260418-231200.grib2.gz"""
    parts = filename.replace(".grib2.gz", "").split("_")
    for part in reversed(parts):
        if "-" in part and len(part) >= 13:
            try:
                dt = datetime.strptime(part, "%Y%m%d-%H%M%S")
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def get_latest_scan():
    """Find and download the latest azshear + reflectivity pair."""
    az_keys = list_s3_latest(MRMS_PRODUCTS["azshear"])
    rf_keys = list_s3_latest(MRMS_PRODUCTS["reflectivity"])

    if not az_keys or not rf_keys:
        return None, None, None

    # Get the newest azshear file
    az_key = az_keys[-1]
    az_time = parse_mrms_timestamp(az_key.split("/")[-1])

    if az_time is None:
        log.warning(f"Cannot parse time from {az_key}")
        return None, None, None

    # Find the closest reflectivity scan
    best_rf_key = None
    best_rf_dt = 9999
    for rf_key in rf_keys:
        rf_time = parse_mrms_timestamp(rf_key.split("/")[-1])
        if rf_time is None:
            continue
        dt = abs((rf_time - az_time).total_seconds())
        if dt < best_rf_dt:
            best_rf_dt = dt
            best_rf_key = rf_key

    if best_rf_key is None or best_rf_dt > 120:
        log.warning(f"No reflectivity match within 120s of {az_time}")
        return None, None, None

    return az_key, best_rf_key, az_time

# ─── Peak Detection ────────────────────────────────────────────────────

def find_peaks(azd, rf_data, lats, lons, az_min=AZ_THRESH, refl_min=REFL_MASK):
    """Find azshear local maxima within the reflectivity mask."""
    from scipy.ndimage import maximum_filter

    masked = np.where(rf_data > refl_min, azd, -999)
    masked = np.nan_to_num(masked, nan=-999)
    local_max = maximum_filter(masked, size=MAX_FILTER_SIZE)
    pr, pc = np.where((masked == local_max) & (masked >= az_min))
    pv = masked[pr, pc]

    peaks = []
    for k in range(len(pr)):
        peaks.append({
            "lat": float(lats[pr[k]]),
            "lon": float(lons[pc[k]]),
            "val": float(pv[k]),
        })

    return peaks

# ─── Track Management ──────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = (np.sin(dlat/2)**2 +
         np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) *
         np.sin(dlon/2)**2)
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))


def update_tracks(existing_tracks, new_peaks, scan_time,
                  match_radius=MATCH_RADIUS_KM, max_age_scans=SCAN_BUFFER):
    """Match new peaks to existing tracks, create new tracks for unmatched."""
    scan_time_str = scan_time.isoformat()

    matched_tracks = set()
    matched_peaks = set()

    # Build candidate matches
    candidates = []
    for ti, track in enumerate(existing_tracks):
        for pi, pk in enumerate(new_peaks):
            dlat = abs(track["last_lat"] - pk["lat"]) * 111
            dlon = abs(track["last_lon"] - pk["lon"]) * 90
            if dlat > match_radius or dlon > match_radius:
                continue
            d = np.sqrt(dlat**2 + dlon**2)
            if d <= match_radius:
                candidates.append((d, ti, pi))

    candidates.sort()

    for d, ti, pi in candidates:
        if ti in matched_tracks or pi in matched_peaks:
            continue
        pk = new_peaks[pi]
        track = existing_tracks[ti]
        track["last_lat"] = pk["lat"]
        track["last_lon"] = pk["lon"]
        track["last_scan"] = scan_time_str
        track["n_scans"] += 1
        track["positions"].append({
            "lat": pk["lat"], "lon": pk["lon"],
            "val": pk["val"], "time": scan_time_str,
        })
        if pk["val"] > track["max_val"]:
            track["max_val"] = pk["val"]
            track["best_lat"] = pk["lat"]
            track["best_lon"] = pk["lon"]
        matched_tracks.add(ti)
        matched_peaks.add(pi)

    # New tracks from unmatched peaks
    for pi, pk in enumerate(new_peaks):
        if pi in matched_peaks:
            continue
        existing_tracks.append({
            "max_val": pk["val"],
            "best_lat": pk["lat"], "best_lon": pk["lon"],
            "last_lat": pk["lat"], "last_lon": pk["lon"],
            "last_scan": scan_time_str,
            "first_scan": scan_time_str,
            "n_scans": 1,
            "positions": [{
                "lat": pk["lat"], "lon": pk["lon"],
                "val": pk["val"], "time": scan_time_str,
            }],
        })

    # Expire old tracks (not seen in last max_age_scans scans worth of time)
    cutoff = scan_time - timedelta(minutes=max_age_scans * 2.5)
    cutoff_str = cutoff.isoformat()
    existing_tracks[:] = [
        t for t in existing_tracks
        if t["last_scan"] >= cutoff_str
    ]

    return existing_tracks

# ─── Rendering ──────────────────────────────────────────────────────────

def render_reflectivity_png(rf_data, lats, lons, output_path):
    """Render reflectivity as a transparent PNG overlay using PIL."""
    from PIL import Image as PILImage

    NWS_TABLE = [
        (5, 4, 233, 231), (10, 1, 159, 244), (15, 3, 0, 244),
        (20, 2, 253, 2), (25, 1, 197, 1), (30, 0, 142, 0),
        (35, 253, 248, 2), (40, 229, 188, 0), (45, 253, 149, 0),
        (50, 253, 0, 0), (55, 212, 0, 0), (60, 188, 0, 0),
        (65, 248, 0, 253), (70, 152, 84, 198), (75, 253, 253, 253),
    ]

    h, w = rf_data.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)

    for i, (dbz_thresh, r, g, b) in enumerate(NWS_TABLE):
        next_dbz = NWS_TABLE[i + 1][0] if i + 1 < len(NWS_TABLE) else 80
        mask = (rf_data >= dbz_thresh) & (rf_data < next_dbz)
        rgba[mask] = [r, g, b, 255]

    # Leaflet imageOverlay: image top = north bound.
    # If lats ascend (south first in array), flip so north is at top.
    if len(lats) > 1 and lats[0] < lats[-1]:
        rgba = rgba[::-1]

    img = PILImage.fromarray(rgba, "RGBA")
    img = img.resize((w * 6, h * 6), PILImage.NEAREST)
    img.save(str(output_path))


def extract_contour_lines(azd, rf_data, lats, lons):
    """Extract azshear contour lines as lat/lon coordinate arrays with label positions."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Soft mask: 20 dBZ for contour display (vs 35 for peak detection)
    # This extends contours into stratiform regions without noise outside precip
    azc = np.where(rf_data > 20, azd, 0)
    azc = np.nan_to_num(azc, nan=0)

    # Light smoothing to reduce contour fragmentation
    from scipy.ndimage import uniform_filter
    azc = uniform_filter(azc, size=3)

    # Orient north-up
    if len(lats) > 1 and lats[0] < lats[-1]:
        azc = azc[::-1]
        plot_lats = lats[::-1]
    else:
        plot_lats = lats

    fig, ax = plt.subplots()
    levels = [3, 5, 8, 12]
    try:
        cs = ax.contour(lons, plot_lats, azc, levels=levels)
    except Exception:
        plt.close(fig)
        return []

    contours = []
    for li, level in enumerate(levels):
        if li >= len(cs.allsegs):
            break
        for seg in cs.allsegs[li]:
            if len(seg) < 5:
                continue
            # Downsample very long segments (>300 pts) to keep JSON reasonable
            if len(seg) > 300:
                step = max(1, len(seg) // 300)
                seg_s = seg[::step]
            else:
                seg_s = seg
            coords = [[float(seg_s[k, 1]), float(seg_s[k, 0])]
                       for k in range(len(seg_s))]
            # Label position at midpoint of the contour
            mid = len(seg) // 2
            label_pos = [float(seg[mid, 1]), float(seg[mid, 0])]
            contours.append({
                "level": float(level),
                "coords": coords,
                "label": label_pos,
            })

    plt.close(fig)
    return contours


# ─── HRRR Live Environment Scoring ────────────────────────────────────

def find_latest_hrrr_run():
    """Find the latest complete HRRR run (105-min production buffer).
    Returns (date_str 'YYYYMMDD', hour int) or (None, None)."""
    avail = datetime.now(timezone.utc) - timedelta(minutes=HRRR_BUFFER_MIN)
    return avail.strftime("%Y%m%d"), avail.hour


def fetch_hrrr_idx(date_str, hour, fxx):
    """Fetch and parse a HRRR .idx file. Returns list of
    (msg_num, byte_start, field_str) tuples, or [] on failure."""
    url = (f"{HRRR_S3}/hrrr.{date_str}/conus/"
           f"hrrr.t{hour:02d}z.wrfprsf{fxx:02d}.grib2.idx")
    try:
        req = urllib.request.Request(url,
                                     headers={"User-Agent": "OHX-QLCS/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning(f"HRRR idx fetch failed: {e}")
        return []

    entries = []
    for line in text.strip().split("\n"):
        parts = line.split(":")
        if len(parts) >= 7:
            entries.append((int(parts[0]), int(parts[1]),
                            f":{parts[3]}:{parts[4]}:"))
    return entries


def download_hrrr_field(date_str, hour, fxx, idx_entries, search_str):
    """Download a single GRIB field via HTTP byte-range request.
    Returns a 2D numpy array clipped to the CWA, or None."""
    # Find matching entry in idx
    match_idx = None
    for i, (_, _, field) in enumerate(idx_entries):
        if search_str in field:
            match_idx = i
            break
    if match_idx is None:
        log.warning(f"Field not found in idx: {search_str}")
        return None

    byte_start = idx_entries[match_idx][1]
    if match_idx + 1 < len(idx_entries):
        byte_end = idx_entries[match_idx + 1][1] - 1
    else:
        byte_end = ""

    grib_url = (f"{HRRR_S3}/hrrr.{date_str}/conus/"
                f"hrrr.t{hour:02d}z.wrfprsf{fxx:02d}.grib2")
    try:
        req = urllib.request.Request(grib_url,
                                     headers={"User-Agent": "OHX-QLCS/1.0"})
        req.add_header("Range", f"bytes={byte_start}-{byte_end}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            grib_bytes = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning(f"HRRR field download failed: {e}")
        return None

    # Write to temp file, read with pygrib
    try:
        import pygrib
        tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
        tmp.write(grib_bytes)
        tmp.close()
        del grib_bytes

        grbs = pygrib.open(tmp.name)
        msg = grbs[1]
        # HRRR is Lambert Conformal — msg.data() with lat/lon bounds
        # doesn't work. Use values + latlons and mask.
        data = msg.values.astype(np.float32)
        lats_full, lons_full = msg.latlons()
        grbs.close()
        os.unlink(tmp.name)
        del msg, grbs

        if lons_full.max() > 180:
            lons_full = lons_full - 360

        mask = ((lats_full >= CWA["lat_min"]) &
                (lats_full <= CWA["lat_max"]) &
                (lons_full >= CWA["lon_min"]) &
                (lons_full <= CWA["lon_max"]))
        result = data[mask].astype(np.float32)
        del data, lats_full, lons_full, mask
        gc.collect()
        return result

    except Exception as e:
        log.warning(f"HRRR GRIB read failed: {e}")
        return None


def compute_hrrr_score(date_str, hour, fxx):
    """Download HRRR fields for one forecast hour, compute CWA-mean score.
    Returns (valid_time_iso, score_float) or (None, None)."""

    idx = fetch_hrrr_idx(date_str, hour, fxx)
    if not idx:
        return None, None

    # Download surface fields
    fields = {}
    for name, search in HRRR_SURFACE_FIELDS.items():
        arr = download_hrrr_field(date_str, hour, fxx, idx, search)
        if arr is None:
            log.warning(f"Missing HRRR field: {name}")
            return None, None
        fields[name] = arr

    # Download pressure-level U-wind for 0-6km mean
    ugrd_layers = []
    for plev in UGRD_LEVELS:
        arr = download_hrrr_field(date_str, hour, fxx, idx,
                                  f":UGRD:{plev} mb:")
        if arr is not None:
            ugrd_layers.append(arr)
    if len(ugrd_layers) < 3:
        log.warning(f"Only {len(ugrd_layers)} UGRD levels found")
        return None, None

    # Compute the 5 scoring variables (CWA means)
    sfc_rh = float(np.nanmean(fields["sfc_rh"]))        # already %
    sfc_temp = float(np.nanmean(fields["sfc_temp"])) - 273.15  # K → °C
    u10 = fields["sfc_u10"]
    v10 = fields["sfc_v10"]
    sfc_wspd = float(np.nanmean(np.sqrt(u10**2 + v10**2))) * 1.944  # m/s → kt
    sbcin = float(np.nanmean(fields["sbcin"]))            # J/kg
    mean_u = float(np.nanmean(np.stack(ugrd_layers))) * 1.944  # m/s → kt

    var_values = {
        "sfc_rh": sfc_rh,
        "mean_wind_0_6_u": mean_u,
        "sfc_wspd": sfc_wspd,
        "sfc_temp": sfc_temp,
        "sbcin": sbcin,
    }

    # Apply scoring formula
    score = 0.0
    for p in SCORING_PARAMS:
        val = var_values[p["var"]]
        denom = p["extreme"] - p["thresh"]
        if abs(denom) < 1e-10:
            contrib = 1.0 if val == p["thresh"] else 0.0
        else:
            contrib = (val - p["thresh"]) / denom
        contrib = max(0.0, min(1.0, contrib))
        score += contrib * p["weight"]
    score = score / SUM_WEIGHTS * 10.0

    # Valid time
    init = datetime.strptime(date_str, "%Y%m%d").replace(
        hour=hour, tzinfo=timezone.utc)
    valid = init + timedelta(hours=fxx)

    log.info(f"  HRRR {date_str}/{hour:02d}z f{fxx:02d} → "
             f"rh={sfc_rh:.0f}% T={sfc_temp:.1f}C "
             f"wspd={sfc_wspd:.1f}kt U6km={mean_u:.1f}kt "
             f"CIN={sbcin:.0f} → score={score:.1f}")

    del fields, ugrd_layers
    gc.collect()

    return valid.isoformat(), round(score, 1)


def compute_live_env_timeline():
    """Compute environment scores for -2h to +3h relative to now.
    Uses one HRRR run, multiple forecast hours.
    Returns list of {time: ISO, score: float} or empty list."""
    date_str, run_hour = find_latest_hrrr_run()
    if date_str is None:
        return []

    now = datetime.now(timezone.utc)
    init = datetime.strptime(date_str, "%Y%m%d").replace(
        hour=run_hour, tzinfo=timezone.utc)

    timeline = []
    for offset_h in range(-2, 4):  # -2h to +3h
        target = now + timedelta(hours=offset_h)
        fxx = int(round((target - init).total_seconds() / 3600))
        if fxx < 0 or fxx > 18:
            continue
        valid_iso, score = compute_hrrr_score(date_str, run_hour, fxx)
        if valid_iso is not None:
            timeline.append({"time": valid_iso, "score": score})
        gc.collect()

    log.info(f"HRRR env timeline: {len(timeline)} scores from "
             f"{date_str}/{run_hour:02d}z")
    return timeline


# ─── State Management ──────────────────────────────────────────────────

def load_state():
    """Load persistent state from JSON."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "last_scan_time": None,
        "tracks": [],
        "scan_count": 0,
    }


def save_state(state):
    """Save persistent state to JSON."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ─── Output Generation ─────────────────────────────────────────────────

def generate_output(state, scan_time, az_grid, rf_grid):
    """Generate all output files for the web viewer."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    lats = rf_grid["lats"]
    lons = rf_grid["lons"]

    # Render reflectivity with timestamped filename
    ts = scan_time.strftime("%H%M%S")
    refl_fname = f"refl_live_{ts}.png"
    render_reflectivity_png(
        rf_grid["data"], lats, lons,
        OUTPUT_DIR / refl_fname)

    # Also write current as reflectivity.png for backward compat
    import shutil
    shutil.copy2(OUTPUT_DIR / refl_fname, OUTPUT_DIR / "reflectivity.png")

    # Extract contour lines
    contours = extract_contour_lines(
        az_grid["data"], rf_grid["data"], lats, lons)

    # Find current-scan peaks
    peaks = find_peaks(az_grid["data"], rf_grid["data"],
                       rf_grid["lats"], rf_grid["lons"])
    peaks.sort(key=lambda p: p["val"], reverse=True)
    display_peaks = peaks[:TOP_N_TRACKS]

    bounds = {
        "south": float(min(lats)),
        "north": float(max(lats)),
        "west": float(min(lons)),
        "east": float(max(lons)),
    }

    # Build frame for this scan
    frame = {
        "time": scan_time.isoformat(),
        "time_str": scan_time.strftime("%H:%M:%S UTC"),
        "refl_img": f"data/{refl_fname}",
        "contours": contours,
        "tracks": [{
            "max_val": p["val"],
            "current_lat": p["lat"],
            "current_lon": p["lon"],
            "n_scans": 1,
        } for p in display_peaks],
    }

    # Rolling buffer: keep last 30 frames (~1 hour)
    if "live_frames" not in state:
        state["live_frames"] = []
    state["live_frames"].append(frame)
    state["live_frames"] = state["live_frames"][-30:]

    # Clean up old reflectivity PNGs (keep only those in buffer)
    keep_fnames = set()
    for fr in state["live_frames"]:
        keep_fnames.add(os.path.basename(fr["refl_img"]))
    keep_fnames.add("reflectivity.png")
    for f in OUTPUT_DIR.glob("refl_live_*.png"):
        if f.name not in keep_fnames:
            f.unlink()

    # Build viewer JSON with all buffered frames
    viewer_data = {
        "scan_time": scan_time.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_count": state["scan_count"],
        "cwa": CWA,
        "bounds": bounds,
        "n_frames": len(state["live_frames"]),
        "frames": state["live_frames"],
        "env_timeline": state.get("env_timeline"),
        "algorithm": {
            "az_threshold": AZ_THRESH,
            "refl_mask": REFL_MASK,
            "top_n": TOP_N_TRACKS,
        },
    }

    with open(OUTPUT_DIR / "latest.json", "w") as f:
        json.dump(viewer_data, f)

    log.info(f"Output: {len(display_peaks)} peaks, "
             f"{len(state['live_frames'])} buffered frames")

# ─── Main Loop ──────────────────────────────────────────────────────────

# HRRR environment scoring (~120s: downloads HRRR fields) is computed OFF the
# scan loop in a background thread so it never blocks fetching the next radar
# scan. Only the main thread mutates `state`; the worker stashes its result here
# and the main loop applies it on its next pass.
_env_thread = None
_env_result = None          # (hrrr_key, timeline) when a background score is ready
_env_lock = threading.Lock()


def _compute_env_async(hrrr_key):
    global _env_result
    try:
        timeline = compute_live_env_timeline()
        with _env_lock:
            _env_result = (hrrr_key, timeline)
    except Exception as e:
        log.warning(f"HRRR scoring failed: {e}")


def run_update(state):
    """Single update cycle: check for new data, process if found."""

    # Check S3 for latest scan
    az_key, rf_key, scan_time = get_latest_scan()

    if az_key is None:
        return state, False

    # Skip if we already processed this scan
    scan_time_str = scan_time.isoformat()
    if state["last_scan_time"] == scan_time_str:
        return state, False

    log.info(f"New scan: {scan_time.strftime('%H:%M:%S')} UTC")

    # Download and read both products
    t0 = time.time()
    az_url = f"{S3_BASE}/{az_key}"
    rf_url = f"{S3_BASE}/{rf_key}"

    az_grid = download_and_read_grib(az_url)
    rf_grid = download_and_read_grib(rf_url)

    if az_grid is None or rf_grid is None:
        log.warning("Failed to read GRIB data")
        return state, False

    t_download = time.time() - t0
    log.info(f"Downloaded in {t_download:.1f}s")

    # Resample azshear to reflectivity grid if shapes differ
    if az_grid["data"].shape != rf_grid["data"].shape:
        from scipy.interpolate import RegularGridInterpolator
        ri = np.array([np.argmin(np.abs(az_grid["lats"] - d))
                       for d in rf_grid["lats"]])
        ci = np.array([np.argmin(np.abs(az_grid["lons"] - d))
                       for d in rf_grid["lons"]])
        az_grid["data"] = az_grid["data"][np.ix_(ri, ci)]
        az_grid["lats"] = rf_grid["lats"]
        az_grid["lons"] = rf_grid["lons"]

    # Find peaks
    peaks = find_peaks(
        az_grid["data"], rf_grid["data"],
        rf_grid["lats"], rf_grid["lons"])
    log.info(f"Found {len(peaks)} peaks (az >= {AZ_THRESH})")

    # Update tracks
    state["tracks"] = update_tracks(
        state["tracks"], peaks, scan_time)
    state["last_scan_time"] = scan_time_str
    state["scan_count"] += 1

    # Apply a completed background HRRR env-score refresh, if one finished
    # (main thread only — keeps state writes race-free with save_state).
    global _env_thread, _env_result
    with _env_lock:
        if _env_result is not None:
            done_key, timeline = _env_result
            state["env_timeline"] = timeline
            state["hrrr_run"] = done_key
            _env_result = None
            log.info(f"Applied HRRR env timeline ({len(timeline)} scores) for {done_key}")
    # Kick off a background refresh when a new HRRR run appears (non-blocking, so
    # the next radar scan isn't delayed by the ~120s HRRR download/scoring).
    hrrr_date, hrrr_hour = find_latest_hrrr_run()
    hrrr_key = f"{hrrr_date}/{hrrr_hour:02d}z"
    if hrrr_key != state.get("hrrr_run") and (_env_thread is None or not _env_thread.is_alive()):
        log.info(f"New HRRR run available: {hrrr_key} (scoring in background)")
        _env_thread = threading.Thread(
            target=_compute_env_async, args=(hrrr_key,), daemon=True)
        _env_thread.start()

    # Generate output
    generate_output(state, scan_time, az_grid, rf_grid)

    t_total = time.time() - t0
    log.info(f"Processed in {t_total:.1f}s total")

    return state, True


def run_batch(tornado_file, cache_dir=None, hrrr_dir=None):
    """Process all unique event dates from the tornado file."""
    import csv as csvmod
    from collections import defaultdict
    TZ_CST = timezone(timedelta(hours=-6))

    # Exclude confirmed supercell events
    EXCLUDE_DATES = {"2024-05-08", "2025-05-20", "2025-06-06"}

    events = defaultdict(list)

    with open(tornado_file) as f:
        for row in csvmod.DictReader(f):
            if row["event_date"] < "2020-10-14":
                continue
            if row["event_date"] in EXCLUDE_DATES:
                continue
            local = datetime.strptime(row["begin_dt_utc"], "%Y-%m-%d %H:%M:%S")
            utc = local.replace(tzinfo=TZ_CST).astimezone(timezone.utc)
            events[row["event_date"]].append(utc.hour)

    dates = sorted(events.keys())
    log.info(f"Batch mode: {len(dates)} event dates")

    for di, date_str in enumerate(dates):
        hours = events[date_str]
        start_h = max(0, min(hours) - 1)
        end_h = min(24, max(hours) + 2)
        log.info(f"\n[{di+1}/{len(dates)}] {date_str} "
                 f"({start_h:02d}-{end_h:02d} UTC, {len(hours)} tornadoes)")

        # Skip if already processed
        out_path = OUTPUT_DIR / "history" / f"{date_str}.json"
        if out_path.exists():
            log.info(f"  Already exists, skipping")
            continue

        try:
            run_historical(date_str, start_h, end_h, cache_dir, tornado_file,
                          hrrr_dir)
        except Exception as e:
            log.error(f"  Failed: {e}")

    log.info(f"\nBatch complete: {len(dates)} dates processed")

    # Generate index.json listing all available dates
    hist_dir = OUTPUT_DIR / "history"
    index = []
    for date_str in dates:
        json_path = hist_dir / f"{date_str}.json"
        if json_path.exists():
            index.append({
                "date": date_str,
                "n_tornadoes": len(events[date_str]),
            })
    index.sort(key=lambda x: x["date"])

    index_path = hist_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump(index, f)
    log.info(f"Index: {index_path} ({len(index)} dates)")


def main():
    parser = argparse.ArgumentParser(description="OHX QLCS real-time backend")
    parser.add_argument("--loop", action="store_true",
                        help="Continuous polling mode")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    parser.add_argument("--historical", type=str, metavar="YYYY-MM-DD",
                        help="Process a historical date")
    parser.add_argument("--cache-dir", type=str, default=None,
                        help="Local MRMS cache directory (e.g. /path/to/cache/mrms)")
    parser.add_argument("--hrrr-dir", type=str, default=None,
                        help="Path to HRRR score_grids cache (e.g. /path/to/cache/score_grids)")
    parser.add_argument("--tornado-file", type=str, default=None,
                        help="Path to qlcs_tornadoes_verified.csv for historical overlay")
    parser.add_argument("--start-hour", type=int, default=0,
                        help="Start hour UTC for historical mode (default: 0)")
    parser.add_argument("--end-hour", type=int, default=24,
                        help="End hour UTC for historical mode (default: 24)")
    parser.add_argument("--test-render", action="store_true",
                        help="Render a test frame without downloading")
    parser.add_argument("--batch", action="store_true",
                        help="Process all event dates from tornado file")
    args = parser.parse_args()

    if args.batch:
        if not args.tornado_file:
            log.error("--batch requires --tornado-file")
            return
        run_batch(args.tornado_file, args.cache_dir, args.hrrr_dir)
        return

    if args.historical:
        run_historical(args.historical, args.start_hour, args.end_hour,
                       args.cache_dir, args.tornado_file, args.hrrr_dir)
        return

    state = load_state()
    log.info(f"State loaded: {state['scan_count']} scans processed")

    if args.loop:
        log.info(f"Starting continuous polling (every {args.interval}s)")
        while True:
            try:
                state, updated = run_update(state)
                if updated:
                    save_state(state)
            except KeyboardInterrupt:
                log.info("Interrupted")
                save_state(state)
                break
            except Exception as e:
                log.error(f"Update failed: {e}")
            time.sleep(args.interval)
    else:
        state, updated = run_update(state)
        if updated:
            save_state(state)
        elif not updated:
            log.info("No new data")


# ─── Historical Replay ─────────────────────────────────────────────────

# AWS archive structure (from original download script):
# s3://noaa-mrms-pds/CONUS/{ProductDir}/{YYYYMMDD}/MRMS_{Product}_{YYYYMMDD}-{HHMMSS}.grib2.gz
AWS_ARCHIVE_PRODUCTS = {
    "azshear": "MergedAzShear_0-2kmAGL_00.50",
    "reflectivity": "MergedBaseReflectivity_00.50",
}


def list_aws_archive_files(date_str, product):
    """List available MRMS files on AWS S3 archive for a given date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    product_name = AWS_ARCHIVE_PRODUCTS[product]
    date_compact = dt.strftime("%Y%m%d")
    prefix = f"CONUS/{product_name}/{date_compact}/"

    url = f"{S3_BASE}?list-type=2&prefix={prefix}&max-keys=1000"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OHX-QLCS/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as e:
        log.error(f"AWS archive listing failed for {product}: {e}")
        return []

    # Parse keys from XML
    files = []
    for chunk in xml_text.split("<Key>")[1:]:
        key = chunk.split("</Key>")[0]
        if key.endswith(".grib2.gz"):
            files.append(f"{S3_BASE}/{key}")

    files.sort()
    log.info(f"  AWS archive: {len(files)} files for {product_name}/{date_compact}")
    return files


def run_historical(date_str, start_hour=0, end_hour=24, cache_dir=None,
                   tornado_file=None, hrrr_dir=None):
    """Process all scans for a historical date and output replay JSON.

    If cache_dir is provided, reads from local files:
        {cache_dir}/{date}/azshear/*.grib2.gz
        {cache_dir}/{date}/reflectivity/*.grib2.gz
    Otherwise downloads from AWS S3 archive.

    If tornado_file is provided, loads verified tornado locations for overlay.
    If hrrr_dir is provided, loads HRRR score grid for environmental overlay.
    """
    log.info(f"Historical mode: {date_str} ({start_hour:02d}-{end_hour:02d} UTC)")

    import csv as csvmod
    import glob as globmod
    import pickle as pkl

    # Load HRRR environmental scores
    # Each pickle has event_datetime (CST string), lead_hours (int), scores (grid)
    # We compute each score's valid UTC time and store it with the CWA-mean score
    env_timeline = []
    if hrrr_dir:
        TZ_CST = timezone(timedelta(hours=-6))
        hrrr_files = sorted(globmod.glob(
            str(Path(hrrr_dir) / f"{date_str}_T-*h.pkl")))
        for hf in hrrr_files:
            with open(hf, "rb") as f:
                hrrr_data = pkl.load(f)
            if "scores" not in hrrr_data or "event_datetime" not in hrrr_data:
                continue
            lead = int(hrrr_data.get("lead_hours", 0))
            # event_datetime is fixed CST (UTC-6) year-round
            evt_naive = datetime.strptime(str(hrrr_data["event_datetime"]),
                                          "%Y-%m-%d %H:%M:%S")
            evt_utc = evt_naive.replace(tzinfo=TZ_CST).astimezone(timezone.utc)
            valid_utc = evt_utc - timedelta(hours=lead)
            mean_score = round(float(np.mean(hrrr_data["scores"])), 1)
            env_timeline.append({
                "time": valid_utc.isoformat(),
                "score": mean_score,
            })
        env_timeline.sort(key=lambda x: x["time"])
        if env_timeline:
            log.info(f"  Env timeline: {len(env_timeline)} scores "
                     f"({env_timeline[0]['score']}–{env_timeline[-1]['score']})")
            for e in env_timeline:
                log.info(f"    {e['time']} → {e['score']}")

    # Load tornadoes for this date if file provided
    tornadoes = []
    if tornado_file and os.path.exists(tornado_file):
        TZ_CST = timezone(timedelta(hours=-6))
        with open(tornado_file) as tf:
            for row in csvmod.DictReader(tf):
                if row["event_date"] != date_str:
                    continue
                # begin_dt_utc is fixed CST (UTC-6) year-round despite name
                local = datetime.strptime(row["begin_dt_utc"], "%Y-%m-%d %H:%M:%S")
                utc = local.replace(tzinfo=TZ_CST).astimezone(timezone.utc)
                tornadoes.append({
                    "lat": float(row["begin_lat"]),
                    "lon": float(row["begin_lon"]),
                    "time": utc.isoformat(),
                    "ef": row.get("f_scale", "EF?"),
                })
        log.info(f"  Loaded {len(tornadoes)} tornado reports for {date_str}")

    def parse_file_time(filepath):
        fname = os.path.basename(filepath)
        parts = fname.replace(".grib2.gz", "").split("_")
        for part in reversed(parts):
            if "-" in part and len(part) >= 13:
                try:
                    return datetime.strptime(part, "%Y%m%d-%H%M%S").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    continue
        return None

    # Collect files — local cache or AWS
    if cache_dir:
        cache_path = Path(cache_dir)
        az_files = sorted(globmod.glob(
            str(cache_path / date_str / "azshear" / "*.grib2.gz")))
        rf_files = sorted(globmod.glob(
            str(cache_path / date_str / "reflectivity" / "*.grib2.gz")))
        log.info(f"Local cache: {len(az_files)} azshear, "
                 f"{len(rf_files)} reflectivity files")
        source = "local"
    else:
        az_files = list_aws_archive_files(date_str, "azshear")
        rf_files = list_aws_archive_files(date_str, "reflectivity")
        log.info(f"AWS archive: {len(az_files)} azshear, "
                 f"{len(rf_files)} reflectivity files")
        source = "aws"

    if not az_files or not rf_files:
        log.error("No data found")
        if not cache_dir:
            log.info("Tip: use --cache-dir to point to a local MRMS cache")
        return

    # Parse timestamps and filter to hour range
    az_timed = [(parse_file_time(f), f) for f in az_files]
    az_timed = [(t, f) for t, f in az_timed if t is not None
                and start_hour <= t.hour < end_hour]
    az_timed.sort()

    rf_timed = [(parse_file_time(f), f) for f in rf_files]
    rf_timed = [(t, f) for t, f in rf_timed if t is not None]
    rf_timed.sort()

    log.info(f"Filtered to {len(az_timed)} azshear scans in hour range")

    if not az_timed:
        log.error("No scans in the specified hour range")
        return

    def load_grib(path_or_url):
        if source == "local":
            return read_grib_to_array(path_or_url)
        else:
            return download_and_read_grib(path_or_url)

    # Process each scan
    frames = []
    bounds = None

    for si, (az_time, az_path) in enumerate(az_timed):
        pct = 100 * (si + 1) / len(az_timed)
        sys.stderr.write(f"\r  Processing: {si+1}/{len(az_timed)} ({pct:.0f}%) "
                         f"{az_time.strftime('%H:%M:%S')}")
        sys.stderr.flush()

        # Find closest reflectivity scan
        best_rf = None
        best_rf_dt = 9999
        for rf_time, rf_path in rf_timed:
            dt = abs((rf_time - az_time).total_seconds())
            if dt < best_rf_dt:
                best_rf_dt = dt
                best_rf = rf_path
        if best_rf is None or best_rf_dt > 120:
            continue

        # Load
        az_grid = load_grib(az_path)
        rf_grid = load_grib(best_rf)
        if az_grid is None or rf_grid is None:
            continue

        # Resample if needed
        if az_grid["data"].shape != rf_grid["data"].shape:
            ri = np.array([np.argmin(np.abs(az_grid["lats"] - d))
                           for d in rf_grid["lats"]])
            ci = np.array([np.argmin(np.abs(az_grid["lons"] - d))
                           for d in rf_grid["lons"]])
            az_grid["data"] = az_grid["data"][np.ix_(ri, ci)]

        # Set bounds on first valid scan
        if bounds is None:
            lats = rf_grid["lats"]
            lons = rf_grid["lons"]
            bounds = {
                "south": float(min(lats)),
                "north": float(max(lats)),
                "west": float(lons[0]),
                "east": float(lons[-1]),
            }

        # Find peaks on THIS scan only
        peaks = find_peaks(az_grid["data"], rf_grid["data"],
                           rf_grid["lats"], rf_grid["lons"])

        # Sort by value, take top-N for display
        peaks.sort(key=lambda p: p["val"], reverse=True)
        display_peaks = peaks[:TOP_N_TRACKS]

        # Render reflectivity PNG for this frame
        hist_img_dir = OUTPUT_DIR / "history" / date_str
        hist_img_dir.mkdir(parents=True, exist_ok=True)

        refl_fname = f"refl_{az_time.strftime('%H%M%S')}.png"
        render_reflectivity_png(rf_grid["data"], rf_grid["lats"],
                                rf_grid["lons"],
                                hist_img_dir / refl_fname)

        # Extract contour lines as coordinates
        contours = extract_contour_lines(
            az_grid["data"], rf_grid["data"],
            rf_grid["lats"], rf_grid["lons"])

        # Build frame data — current scan peaks only
        frame = {
            "time": az_time.isoformat(),
            "time_str": az_time.strftime("%H:%M:%S UTC"),
            "refl_img": f"data/history/{date_str}/{refl_fname}",
            "contours": contours,
            "n_peaks": len(peaks),
            "tracks": [{
                "max_val": p["val"],
                "current_lat": p["lat"],
                "current_lon": p["lon"],
                "n_scans": 1,
            } for p in display_peaks],
        }
        frames.append(frame)

    sys.stderr.write(f"\r  Processing complete: {len(frames)} frames     \n")

    if not frames:
        log.error("No valid frames produced")
        return

    # Write history JSON
    hist_dir = OUTPUT_DIR / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)

    history = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_frames": len(frames),
        "bounds": bounds,
        "cwa": CWA,
        "tornadoes": tornadoes,
        "env_timeline": env_timeline if env_timeline else None,
        "algorithm": {
            "az_threshold": AZ_THRESH,
            "refl_mask": REFL_MASK,
            "top_n": TOP_N_TRACKS,
            "match_radius_km": MATCH_RADIUS_KM,
            "scan_window": SCAN_BUFFER,
        },
        "frames": frames,
    }

    out_path = hist_dir / f"{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(history, f)

    log.info(f"Saved: {out_path} ({len(frames)} frames)")
    log.info(f"Images: {hist_img_dir}")


if __name__ == "__main__":
    main()
