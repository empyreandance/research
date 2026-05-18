"""
Validation script: compare our HDW computation against the operational
USFS/MSU/SCSU HDW product.

The operational product is visible at:
    https://www.fireweather.msu.edu/hdw/

Strategy:
  1. Pick a few grid points at different terrain elevations
  2. Pull the same GEFS data used by the operational product
  3. Compute HDW with our module
  4. Compare against the operational value

This script is meant to be run locally where NOMADS/AWS access
is available.  It requires: herbie, xarray, cfgrib, numpy.

Usage:
    python -m hdw.validation
"""

import numpy as np
from pathlib import Path
import json
from datetime import datetime

from hdw.core import (
    saturation_vapor_pressure,
    mixing_ratio_from_specific_humidity,
    surface_adjusted_vpd,
    surface_vpd,
    wind_speed,
    qualifying_levels_mask,
    hdw_single_timestep,
    hdw_daily,
    HDW_PRESSURE_LEVELS if hasattr(__import__("hdw.core"), "HDW_PRESSURE_LEVELS") else None,
)


# ── Validation grid points ──────────────────────────────────────────
# Selected to span a range of terrain elevations across CONUS.
VALIDATION_POINTS = [
    {
        "name": "Northern Minnesota (Pagami Creek area)",
        "lat": 48.0,
        "lon": -91.5,
        "notes": "Low terrain, continental. Used in McDonald et al. 2018.",
    },
    {
        "name": "Southern California (Cedar Fire area)",
        "lat": 33.0,
        "lon": -116.5,
        "notes": "Moderate terrain, Santa Ana wind regime. McDonald et al. 2018.",
    },
    {
        "name": "Central Texas (Bastrop area)",
        "lat": 30.0,
        "lon": -97.0,
        "notes": "Low terrain, subtropical. McDonald et al. 2018.",
    },
    {
        "name": "Central Washington (east of Cascades)",
        "lat": 47.5,
        "lon": -120.0,
        "notes": "Moderate-high terrain, leeward drying. McDonald et al. 2018.",
    },
    {
        "name": "Denver, Colorado",
        "lat": 39.75,
        "lon": -104.87,
        "notes": "High terrain (~1600m). Tests layer selection at elevation.",
    },
    {
        "name": "San Joaquin Valley, CA (near HNX CWA)",
        "lat": 36.3,
        "lon": -119.8,
        "notes": "Valley floor, moderate elevation. Alex's CWA.",
    },
]


def validation_report_template():
    """
    Print a template for manually recording validation results.

    Since the operational HDW product displays values on a clickable
    map, you'll need to read the values manually and enter them here
    for comparison.
    """
    report = """
╔══════════════════════════════════════════════════════════════╗
║              HDW COMPUTATION VALIDATION REPORT              ║
╠══════════════════════════════════════════════════════════════╣

Date of comparison: _______________
GEFS initialization: ___________00Z
Source of operational values: https://www.fireweather.msu.edu/hdw/

For each grid point, record:
  - The operational HDW value (from the USFS/MSU tool)
  - Our computed HDW value
  - The difference and percent error

"""
    for i, pt in enumerate(VALIDATION_POINTS):
        report += f"""
Point {i+1}: {pt['name']}
  Lat: {pt['lat']:.1f}°N  Lon: {pt['lon']:.1f}°{'W' if pt['lon'] < 0 else 'E'}
  Notes: {pt['notes']}
  ┌─────────────────────────────────────────┐
  │ Operational HDW:  ________              │
  │ Our computed HDW: ________              │
  │ Difference:       ________              │
  │ Percent error:    ________%             │
  │                                         │
  │ Surface P (our):    ________ hPa        │
  │ Qualifying levels:  ________________    │
  │ Max VPD (adj):      ________ hPa        │
  │ Max wind:           ________ m/s        │
  └─────────────────────────────────────────┘
"""
    report += """
NOTES ON DISCREPANCIES
━━━━━━━━━━━━━━━━━━━━━━
Expected sources of small differences:
  1. Grid interpolation: USFS uses 0.5° CFSR/GEFS; we may use 0.25°
  2. Time step handling: which UTC times define the burning period
  3. Layer depth estimation: hypsometric vs. geopotential height method
  4. Moisture variable: SPFH vs. RH in source data

If discrepancies > 10%, investigate:
  - Are the same pressure levels being selected?
  - Is the surface-adjusted VPD calculation matching?
  - Is the burning-period time window the same?
"""
    print(report)
    return report


def compute_hdw_at_point_from_xarray(ds, lat, lon):
    """
    Compute HDW at a single grid point from an xarray Dataset.

    This is a convenience wrapper for validation. It extracts the
    necessary fields from a loaded GEFS dataset and runs the HDW
    computation.

    Parameters
    ----------
    ds : xarray.Dataset
        Must contain: TMP, SPFH, UGRD, VGRD, HGT on pressure levels,
        plus surface fields (PRES_surface, TMP_2m, DPT_2m, etc.)
    lat, lon : float
        Grid point coordinates.

    Returns
    -------
    result : dict
        HDW value and all intermediate quantities for debugging.
    """
    # Select nearest grid point
    pt = ds.sel(latitude=lat, longitude=lon % 360, method="nearest")

    # Extract fields — variable names depend on GRIB2 decoding
    # This will need adjustment based on actual cfgrib output names
    result = {
        "lat": float(pt.latitude),
        "lon": float(pt.longitude),
        "note": "Variable extraction depends on cfgrib naming conventions. "
                "Adjust field names in this function after inspecting ds.data_vars.",
    }
    return result


if __name__ == "__main__":
    validation_report_template()
