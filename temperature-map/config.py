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

# RCC-ACIS base URL for multi-station data queries
ACIS_URL = "http://data.rcc-acis.org/MultiStnData"

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
# MAP APPEARANCE (used by daily_run.py, included here for completeness)
# =============================================================================

# The anomaly range in days. Values beyond this get clipped to the max color.
# ±45 means anything more than 45 days ahead/behind shows as the darkest shade.
ANOMALY_MAX_DAYS = 45

# Output image size in inches (width, height) and resolution
MAP_WIDTH_INCHES = 14
MAP_HEIGHT_INCHES = 8
MAP_DPI = 300

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
