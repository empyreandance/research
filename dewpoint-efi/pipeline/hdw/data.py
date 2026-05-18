"""
Data fetching utilities for GEFS operational and reforecast data.

This module handles pulling the specific GRIB2 fields needed for HDW
computation from NOMADS (operational GEFS) and AWS S3 (reforecasts).

Dependencies: herbie, xarray, cfgrib

Usage (run locally — network access to NOMADS/AWS required):

    from hdw.data import fetch_gefs_operational, fetch_gefs_reforecast
    ds = fetch_gefs_operational("2026-05-14", member=0, fhour=12)
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional


# ── Pressure levels to pull ──────────────────────────────────────────
# Covers the lowest 500m AGL across all CONUS terrain elevations.
# Sea level: need down to ~950 hPa.
# Highest CONUS terrain (~3500m, P_sfc ~660 hPa): need up to ~625 hPa.
# We pull 1000 through 700 hPa to cover all cases with margin.
HDW_PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 700]

# GRIB2 variable names needed for HDW
GEFS_FIELDS = {
    "pressure_levels": {
        "TMP": "temperature",
        "SPFH": "specific_humidity",
        "UGRD": "u_wind",
        "VGRD": "v_wind",
        "HGT": "geopotential_height",
    },
    "surface": {
        "PRES:surface": "surface_pressure",
        "TMP:2 m above ground": "T_2m",
        "DPT:2 m above ground": "Td_2m",
        "UGRD:10 m above ground": "u_10m",
        "VGRD:10 m above ground": "v_10m",
        "HGT:surface": "surface_geopotential_height",
    },
}


def check_dependencies():
    """Check that required packages are installed."""
    missing = []
    for pkg in ["herbie", "xarray", "cfgrib", "eccodes"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print("Install with:")
        print(f"  pip install {' '.join(missing)}")
        return False
    return True


def fetch_gefs_operational(
    date: str,
    member: int = 0,
    fhour: int = 12,
    output_dir: str = "./data/operational",
) -> Optional[Path]:
    """
    Fetch a single GEFS member forecast using Herbie.

    Parameters
    ----------
    date : str
        Initialization date, e.g. "2026-05-14".
    member : int
        Ensemble member number (0 = control, 1–30 = perturbations).
    fhour : int
        Forecast hour.
    output_dir : str
        Directory to save downloaded GRIB2 files.

    Returns
    -------
    path : Path or None
        Path to downloaded file, or None if download failed.
    """
    if not check_dependencies():
        return None

    from herbie import Herbie

    H = Herbie(
        date,
        model="gefs",
        member=member,
        fxx=fhour,
        product="atmos.25",  # 0.25° atmospheric fields
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Pull pressure-level fields
    for var in ["TMP", "SPFH", "UGRD", "VGRD", "HGT"]:
        for plev in HDW_PRESSURE_LEVELS:
            search = f":{var}:{plev} mb:"
            try:
                H.download(search, save_dir=out)
            except Exception as e:
                print(f"  Warning: could not fetch {var} at {plev} hPa: {e}")

    # Pull surface fields
    for search_pattern in [
        ":PRES:surface:",
        ":TMP:2 m above ground:",
        ":DPT:2 m above ground:",
        ":UGRD:10 m above ground:",
        ":VGRD:10 m above ground:",
        ":HGT:surface:",
    ]:
        try:
            H.download(search_pattern, save_dir=out)
        except Exception as e:
            print(f"  Warning: could not fetch {search_pattern}: {e}")

    return out


def describe_reforecast_archive():
    """
    Print information about the GEFS reforecast archive on AWS S3.

    The archive lives at s3://noaa-gefs-retrospective/GEFSv12/reforecast/
    and is organized as:
        {year}/{year}{month}{day}{cycle}/
            c00/  (control)
            p01/ through p04/  (4 perturbation members)

    Each member directory contains GRIB2 files with pressure-level
    and surface fields.

    Run this locally to explore the archive structure before building
    the M-climate pipeline.
    """
    info = """
GEFS v12 Reforecast Archive
============================
Location: s3://noaa-gefs-retrospective/GEFSv12/reforecast/
Access:   Public, no credentials needed

Structure:
  {year}/{year}{month}{day}00/
    c00/atmos/  → control member
    p01/atmos/  → perturbation 1
    p02/atmos/  → perturbation 2
    p03/atmos/  → perturbation 3
    p04/atmos/  → perturbation 4

Members per initialization: 5 (1 control + 4 perturbations)
Initialization frequency:   Once per day (00Z) — but check which days
                            are actually populated (may be once per week
                            in earlier years)
Period: ~2000–2019 (20 years)
Resolution: 0.25° globally
Forecast length: up to 35 days (Days 0–16 at 3-hourly, Days 16–35 at 6-hourly)
Output frequency: 3-hourly through Day 8, 6-hourly thereafter

File naming example:
  s3://noaa-gefs-retrospective/GEFSv12/reforecast/
    2019/2019071500/c00/atmos/
      gec00.t00z.pgrb2a.0p25.f012
      gec00.t00z.pgrb2b.0p25.f012

  pgrb2a = primary fields (most standard vars)
  pgrb2b = secondary/additional fields

To explore:
  aws s3 ls s3://noaa-gefs-retrospective/GEFSv12/reforecast/2019/ --no-sign-request
  aws s3 ls s3://noaa-gefs-retrospective/GEFSv12/reforecast/2019/2019071500/c00/atmos/ --no-sign-request

Relevant variables for HDW:
  TMP, SPFH (or RH), UGRD, VGRD, HGT on pressure levels
  PRES:surface, TMP:2m, DPT:2m, UGRD:10m, VGRD:10m, HGT:surface
"""
    print(info)
    return info
