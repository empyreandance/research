"""
config.py — All settings for the Temperature Streak Map project.
"""

import os

# =============================================================================
# FILE PATHS
# =============================================================================

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# The climatology file (copy from the Temperature Anomaly Map project,
# or rebuild with build_climatology.py from that project)
CLIMATOLOGY_FILE = os.path.join(PROJECT_DIR, "climatology.nc")

# Running streak state (updated daily)
STREAK_STATE_FILE = os.path.join(PROJECT_DIR, "streak_state.nc")

# Output images
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# Intermediate data
CACHE_DIR = os.path.join(PROJECT_DIR, "cache")


# =============================================================================
# GRID DEFINITION (must match climatology.nc)
# =============================================================================

LON_WEST = -125.0
LON_EAST = -66.0
LAT_SOUTH = 24.0
LAT_NORTH = 50.0
GRID_SPACING = 0.25


# =============================================================================
# ACIS API SETTINGS
# =============================================================================

ACIS_URL = "http://data.rcc-acis.org/MultiStnData"

STATES = [
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "DC"
]

API_DELAY = 1.0
API_MAX_RETRIES = 3


# =============================================================================
# STREAK MAP SETTINGS
# =============================================================================

# How many days to look back when initializing the streak from scratch
STREAK_BACKFILL_DAYS = 90

# Maximum streak to display on the color scale
STREAK_MAX_DAYS = 30


# =============================================================================
# MAP APPEARANCE
# =============================================================================

MAP_WIDTH_INCHES = 14
MAP_HEIGHT_INCHES = 8
MAP_DPI = 300

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
