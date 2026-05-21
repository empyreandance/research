"""
config.py — All settings for the Temperature Calendar Map project.

This file contains every knob you might want to turn, all in one place.
Nothing else in the project has hardcoded values — everything reads from here.
"""

import os

# =============================================================================
# FILE PATHS
# =============================================================================

# Where this project lives on disk
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# The pre-built climatology file (created once by build_climatology.py)
CLIMATOLOGY_FILE = os.path.join(PROJECT_DIR, "climatology.nc")

# Where the daily map PNG gets saved
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# Intermediate data (raw station downloads, etc.)
CACHE_DIR = os.path.join(PROJECT_DIR, "cache")


# =============================================================================
# CLIMATOLOGY GRID DEFINITION
# =============================================================================

# Bounding box for the continental US (west, east, south, north)
LON_WEST = -125.0
LON_EAST = -66.0
LAT_SOUTH = 24.0
LAT_NORTH = 50.0

# Grid spacing in degrees. 0.25° ≈ 28 km, which gives ~236 x 104 cells.
# This is fine enough to show regional patterns without being huge.
# If you want smoother output, try 0.125 (doubles resolution in each direction).
GRID_SPACING = 0.25


# =============================================================================
# ACIS API SETTINGS
# =============================================================================

# RCC-ACIS endpoints
ACIS_URL = "http://data.rcc-acis.org/MultiStnData"     # Used by build_climatology.py
ACIS_GRID_URL = "http://data.rcc-acis.org/GridData"    # Used by build_observations.py

# All 48 contiguous US states (no Alaska/Hawaii — their climatology is
# very different and the map projection doesn't include them)
STATES = [
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "DC"
]

# How long to pause between ACIS API calls (in seconds).
# Be polite to the server — it's a free public service.
API_DELAY = 1.0

# How many times to retry a failed API call before giving up
API_MAX_RETRIES = 3


# =============================================================================
# MAP APPEARANCE — DAY-OFFSET MAP (the existing climatology product)
# =============================================================================

# The anomaly range in days. Values beyond this get clipped to the max color.
# ±45 means anything more than 45 days ahead/behind shows as the darkest shade.
ANOMALY_MAX_DAYS = 45

# Output image size in inches (width, height) and resolution
MAP_WIDTH_INCHES = 14
MAP_HEIGHT_INCHES = 8
MAP_DPI = 300


# =============================================================================
# OBSERVATION CUBE — STREAK MAPS ("days since last reached this temperature")
# =============================================================================

# RCC-ACIS GridData ID. Grid 3 is NRCC Lite — a daily interpolated grid at
# 0.125° resolution covering 1979-01-01 to present. We pull at 0.125° and
# downsample to the 0.25° climatology grid for visual consistency with the
# day-offset tool. Other grids worth knowing about:
#
#   grid 1   NRCC interpolated 5km, 1893-present (more detail, slower, more data)
#   grid 21  PRISM 4km daily, 1981-present (highest quality, but heavy)
#
# Grid 3 is the lightweight choice that still goes back the better part of
# half a century.
OBS_GRID_ID = "3"

# Earliest date in the period of record. NRCC Lite (grid 3) begins 1979-01-01;
# do not push earlier than that or the API returns nothing.
OBS_PERIOD_START = "1979-01-01"

# Output NetCDF files (one per element)
OBS_FILE_MAXT = os.path.join(PROJECT_DIR, "observations_maxt.nc")
OBS_FILE_MINT = os.path.join(PROJECT_DIR, "observations_mint.nc")

# Where yearly checkpoint files go during the build. If the build is
# interrupted, rerunning the script picks up from the last completed year.
OBS_CHECKPOINT_DIR = os.path.join(CACHE_DIR, "obs_yearly")


# =============================================================================
# STREAK MAP APPEARANCE
# =============================================================================

# Color scale cap (days). Cells with a longer streak still get the saturated
# end of the colormap, so a typical day shows rich detail in the 1-365 range
# without a few extreme cells crushing the dynamic range.
STREAK_COLOR_CAP = 365

# Contour line spacing in days. Fixed interval matches the visual logic of
# the existing day-offset map. The contour function only draws lines where
# the field actually crosses each level, so a typical day shows a handful
# of lines, not 36.
STREAK_CONTOUR_INTERVAL = 10

# Gaussian smoothing applied to the streak field before contouring.
# Larger than the day-offset map's 1.5 because the streak metric has more
# threshold-crossing noise inherent in the math. Tune by eye if needed.
STREAK_SMOOTH_SIGMA = 2.0

# Whether to label every contour line (True) or every other (False) etc.
# The line interval times this stride gives the labeled-line spacing in days.
# 1 = label every line (lines at 10, 20, 30 ... all labeled)
# 2 = label every other (labels at 20, 40, 60 ...)
STREAK_LABEL_STRIDE = 1


# =============================================================================
# REGIONAL CROPS
# =============================================================================
# Each region is defined by [west_lon, east_lon, south_lat, north_lat].
# These get rendered as separate high-res maps in addition to the national view.

REGIONS = {
    "northwest": {
        "name": "Pacific Northwest",
        "extent": [-125.0, -110.0, 41.0, 49.5]
    },
    "west": {
        "name": "West",
        "extent": [-125.0, -110.0, 31.0, 42.5]
    },
    "northern_plains": {
        "name": "Northern Plains",
        "extent": [-110.0, -95.0, 41.0, 49.5]
    },
    "southern_plains": {
        "name": "Southern Plains",
        "extent": [-108.0, -93.0, 25.5, 40.0]
    },
    "upper_midwest": {
        "name": "Upper Midwest",
        "extent": [-97.0, -82.0, 41.0, 49.5]
    },
    "midwest": {
        "name": "Midwest",
        "extent": [-97.0, -82.0, 35.0, 42.5]
    },
    "southeast": {
        "name": "Southeast",
        "extent": [-93.0, -75.0, 24.5, 37.0]
    },
    "northeast": {
        "name": "Northeast",
        "extent": [-82.0, -66.5, 37.0, 47.5]
    },
}
