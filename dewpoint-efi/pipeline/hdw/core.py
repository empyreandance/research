"""
Hot-Dry-Windy Index (HDW) computation following Srock et al. (2018).

HDW = max over burning period of [max_layer(VPD_sfc_adj) × max_layer(U)]

where the layer is the lowest 500 m AGL, VPD is surface-adjusted
(parcel descended dry-adiabatically to surface pressure), and U is
wind speed. Units: VPD in hPa, U in m/s, HDW dimensionless (hPa × m/s).

References
----------
Srock, A. F., Charney, J. J., Potter, B. E., & Goodrick, S. L. (2018).
    The Hot-Dry-Windy Index: A new fire weather index.
    Atmosphere, 9(7), 279.

McDonald, J. M., Srock, A. F., & Charney, J. J. (2018).
    Development and Application of a Hot-Dry-Windy Index (HDW) Climatology.
    Atmosphere, 9(7), 285.
"""

import numpy as np
from typing import Optional

# ── Physical constants ────────────────────────────────────────────────
R_D = 287.04       # specific gas constant for dry air  [J kg⁻¹ K⁻¹]
R_V = 461.50       # specific gas constant for water vapor [J kg⁻¹ K⁻¹]
EPSILON = R_D / R_V  # ≈ 0.6220
C_P = 1004.0       # specific heat at constant pressure [J kg⁻¹ K⁻¹]
KAPPA = R_D / C_P   # Poisson constant ≈ 0.2859
G = 9.80665         # gravitational acceleration [m s⁻²]
LAYER_DEPTH = 500.0  # HDW analysis layer depth [m]


# ── Saturation vapor pressure ────────────────────────────────────────

def saturation_vapor_pressure(T_celsius: np.ndarray) -> np.ndarray:
    """
    Saturation vapor pressure via Bolton (1980).

    Parameters
    ----------
    T_celsius : array-like
        Temperature in degrees Celsius.

    Returns
    -------
    e_s : ndarray
        Saturation vapor pressure in hPa (mb).
    """
    T_c = np.asarray(T_celsius, dtype=np.float64)
    return 6.112 * np.exp(17.67 * T_c / (T_c + 243.5))


def vapor_pressure_from_dewpoint(Td_celsius: np.ndarray) -> np.ndarray:
    """
    Actual vapor pressure from dewpoint temperature.

    Identical formula to saturation_vapor_pressure, since at the
    dewpoint the air is saturated by definition.

    Parameters
    ----------
    Td_celsius : array-like
        Dewpoint temperature in degrees Celsius.

    Returns
    -------
    e : ndarray
        Vapor pressure in hPa.
    """
    return saturation_vapor_pressure(Td_celsius)


# ── Moisture conversions ─────────────────────────────────────────────

def mixing_ratio_from_specific_humidity(q: np.ndarray) -> np.ndarray:
    """
    Convert specific humidity to mixing ratio.

    Parameters
    ----------
    q : array-like
        Specific humidity in kg/kg.

    Returns
    -------
    w : ndarray
        Mixing ratio in kg/kg.
    """
    q = np.asarray(q, dtype=np.float64)
    return q / (1.0 - q)


def vapor_pressure_from_mixing_ratio(
    w: np.ndarray, P: np.ndarray
) -> np.ndarray:
    """
    Actual vapor pressure from mixing ratio and total pressure.

    e = w * P / (w + epsilon)

    Parameters
    ----------
    w : array-like
        Mixing ratio in kg/kg.
    P : array-like
        Total pressure in hPa.

    Returns
    -------
    e : ndarray
        Vapor pressure in hPa.
    """
    w = np.asarray(w, dtype=np.float64)
    P = np.asarray(P, dtype=np.float64)
    return w * P / (w + EPSILON)


def specific_humidity_from_relative_humidity(
    RH: np.ndarray, T_kelvin: np.ndarray, P_hpa: np.ndarray
) -> np.ndarray:
    """
    Derive specific humidity from relative humidity, temperature, and pressure.

    Parameters
    ----------
    RH : array-like
        Relative humidity as a fraction (0–1). If values > 2 are
        detected, they are assumed to be percentages and divided by 100.
    T_kelvin : array-like
        Temperature in Kelvin.
    P_hpa : array-like
        Pressure in hPa.

    Returns
    -------
    q : ndarray
        Specific humidity in kg/kg.
    """
    RH = np.asarray(RH, dtype=np.float64)
    T_kelvin = np.asarray(T_kelvin, dtype=np.float64)
    P_hpa = np.asarray(P_hpa, dtype=np.float64)

    if np.nanmax(RH) > 2.0:
        RH = RH / 100.0

    T_c = T_kelvin - 273.15
    e_s = saturation_vapor_pressure(T_c)
    e = RH * e_s
    w = EPSILON * e / (P_hpa - e)
    q = w / (1.0 + w)
    return q


# ── Surface-adjusted VPD ─────────────────────────────────────────────

def surface_adjusted_vpd(
    T_kelvin: np.ndarray,
    q: np.ndarray,
    P_level_hpa: np.ndarray,
    P_sfc_hpa: np.ndarray,
) -> np.ndarray:
    """
    Vapor pressure deficit of a parcel after dry-adiabatic descent to
    the surface.

    The parcel at (T, q, P_level) is brought down to P_sfc:
      - Temperature increases by dry-adiabatic compression.
      - Mixing ratio (and hence specific humidity) is conserved.
      - VPD is computed at the new (warmer) temperature and the
        conserved moisture, evaluated at surface pressure.

    Parameters
    ----------
    T_kelvin : array-like
        Temperature at the pressure level, in Kelvin.
    q : array-like
        Specific humidity at the pressure level, in kg/kg.
    P_level_hpa : array-like
        Pressure of the level, in hPa.
    P_sfc_hpa : array-like
        Surface pressure, in hPa.

    Returns
    -------
    vpd : ndarray
        Surface-adjusted vapor pressure deficit in hPa.
        Clipped to >= 0 (saturated parcels yield VPD = 0).
    """
    T_kelvin = np.asarray(T_kelvin, dtype=np.float64)
    q = np.asarray(q, dtype=np.float64)
    P_level_hpa = np.asarray(P_level_hpa, dtype=np.float64)
    P_sfc_hpa = np.asarray(P_sfc_hpa, dtype=np.float64)

    # Dry-adiabatic descent: T_adj = T * (P_sfc / P_level)^kappa
    T_adjusted = T_kelvin * (P_sfc_hpa / P_level_hpa) ** KAPPA

    # Conserved mixing ratio
    w = mixing_ratio_from_specific_humidity(q)

    # Actual vapor pressure at surface pressure with conserved moisture
    e_actual = vapor_pressure_from_mixing_ratio(w, P_sfc_hpa)

    # Saturation vapor pressure at adjusted temperature
    T_adj_celsius = T_adjusted - 273.15
    e_sat = saturation_vapor_pressure(T_adj_celsius)

    vpd = e_sat - e_actual
    return np.maximum(vpd, 0.0)


def surface_vpd(
    T_2m_kelvin: np.ndarray, Td_2m_kelvin: np.ndarray
) -> np.ndarray:
    """
    VPD at the surface from 2-m temperature and 2-m dewpoint.

    No adiabatic adjustment needed — already at the surface.

    Parameters
    ----------
    T_2m_kelvin : array-like
        2-m temperature in Kelvin.
    Td_2m_kelvin : array-like
        2-m dewpoint temperature in Kelvin.

    Returns
    -------
    vpd : ndarray
        Surface VPD in hPa, clipped to >= 0.
    """
    T_2m_kelvin = np.asarray(T_2m_kelvin, dtype=np.float64)
    Td_2m_kelvin = np.asarray(Td_2m_kelvin, dtype=np.float64)

    e_sat = saturation_vapor_pressure(T_2m_kelvin - 273.15)
    e_act = saturation_vapor_pressure(Td_2m_kelvin - 273.15)
    return np.maximum(e_sat - e_act, 0.0)


# ── Layer selection ──────────────────────────────────────────────────

def layer_top_pressure(
    P_sfc_hpa: np.ndarray,
    T_mean_kelvin: float = 288.0,
    depth_m: float = LAYER_DEPTH,
) -> np.ndarray:
    """
    Estimate the pressure at a given height AGL using the hypsometric
    equation with a mean virtual temperature.

    This is the fallback when geopotential height data is unavailable.

    Parameters
    ----------
    P_sfc_hpa : array-like
        Surface pressure in hPa.
    T_mean_kelvin : float
        Estimated mean virtual temperature of the layer (K).
        288 K is a reasonable CONUS warm-season default.
    depth_m : float
        Layer depth in meters (default 500).

    Returns
    -------
    P_top : ndarray
        Estimated pressure at the top of the layer, in hPa.
    """
    P_sfc_hpa = np.asarray(P_sfc_hpa, dtype=np.float64)
    return P_sfc_hpa * np.exp(-G * depth_m / (R_D * T_mean_kelvin))


def qualifying_levels_mask(
    P_levels_hpa: np.ndarray,
    P_sfc_hpa: np.ndarray,
    Z_levels_m: Optional[np.ndarray] = None,
    Z_sfc_m: Optional[np.ndarray] = None,
    T_mean_kelvin: float = 288.0,
    depth_m: float = LAYER_DEPTH,
) -> np.ndarray:
    """
    Boolean mask identifying pressure levels within `depth_m` meters AGL.

    Two modes:
      1. If Z_levels_m and Z_sfc_m are provided, use geopotential height
         directly (preferred — no approximation).
      2. Otherwise, estimate the layer top pressure via the hypsometric
         equation.

    A level qualifies if:
      - It is above the surface (P_level < P_sfc), AND
      - It is within depth_m meters of the surface.

    Parameters
    ----------
    P_levels_hpa : array, shape (n_levels,)
        Pressure levels available in the data, in hPa (descending order
        is fine but not required).
    P_sfc_hpa : array, shape (...,)
        Surface pressure at each grid point, in hPa.
    Z_levels_m : array, shape (n_levels, ...), optional
        Geopotential height at each pressure level and grid point, in m.
    Z_sfc_m : array, shape (...,), optional
        Surface geopotential height at each grid point, in m.
    T_mean_kelvin : float
        Fallback mean virtual temperature for hypsometric estimate.
    depth_m : float
        Layer depth (default 500 m).

    Returns
    -------
    mask : ndarray, shape (n_levels, ...)
        True where the level qualifies (above surface, within layer).
    """
    P_levels_hpa = np.asarray(P_levels_hpa, dtype=np.float64)
    P_sfc_hpa = np.asarray(P_sfc_hpa, dtype=np.float64)

    # Broadcast P_levels to (n_levels, ...) shape
    n_levels = P_levels_hpa.shape[0]
    P_levels_bc = P_levels_hpa.reshape(
        (n_levels,) + (1,) * P_sfc_hpa.ndim
    )

    # Condition 1: level is above the surface
    above_surface = P_levels_bc < P_sfc_hpa

    if Z_levels_m is not None and Z_sfc_m is not None:
        # Condition 2 (geopotential height method): within depth_m of surface
        Z_levels_m = np.asarray(Z_levels_m, dtype=np.float64)
        Z_sfc_m = np.asarray(Z_sfc_m, dtype=np.float64)
        height_agl = Z_levels_m - Z_sfc_m
        within_layer = height_agl <= depth_m
    else:
        # Condition 2 (hypsometric fallback): P_level > P_top
        P_top = layer_top_pressure(P_sfc_hpa, T_mean_kelvin, depth_m)
        within_layer = P_levels_bc > P_top

    return above_surface & within_layer


# ── Wind speed ───────────────────────────────────────────────────────

def wind_speed(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """
    Horizontal wind speed from u and v components.

    Parameters
    ----------
    u, v : array-like
        Wind components in m/s.

    Returns
    -------
    speed : ndarray
        Wind speed in m/s.
    """
    return np.sqrt(np.asarray(u) ** 2 + np.asarray(v) ** 2)


# ── HDW computation ──────────────────────────────────────────────────

def hdw_single_timestep(
    T_levels_K: np.ndarray,
    q_levels: np.ndarray,
    u_levels: np.ndarray,
    v_levels: np.ndarray,
    P_levels_hpa: np.ndarray,
    P_sfc_hpa: np.ndarray,
    T_2m_K: np.ndarray,
    Td_2m_K: np.ndarray,
    u_10m: np.ndarray,
    v_10m: np.ndarray,
    Z_levels_m: Optional[np.ndarray] = None,
    Z_sfc_m: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    Compute HDW at a single time step for all grid points.

    This finds the maximum surface-adjusted VPD and maximum wind speed
    in the lowest 500 m AGL (including the surface), then multiplies
    them.

    Parameters
    ----------
    T_levels_K : array, shape (n_levels, ...)
        Temperature on pressure levels, in Kelvin.
    q_levels : array, shape (n_levels, ...)
        Specific humidity on pressure levels, in kg/kg.
    u_levels : array, shape (n_levels, ...)
        U-wind on pressure levels, in m/s.
    v_levels : array, shape (n_levels, ...)
        V-wind on pressure levels, in m/s.
    P_levels_hpa : array, shape (n_levels,)
        Pressure levels, in hPa.
    P_sfc_hpa : array, shape (...)
        Surface pressure, in hPa.
    T_2m_K : array, shape (...)
        2-m temperature, in Kelvin.
    Td_2m_K : array, shape (...)
        2-m dewpoint temperature, in Kelvin.
    u_10m : array, shape (...)
        10-m U-wind, in m/s.
    v_10m : array, shape (...)
        10-m V-wind, in m/s.
    Z_levels_m : array, shape (n_levels, ...), optional
        Geopotential height on pressure levels, in meters.
    Z_sfc_m : array, shape (...), optional
        Surface geopotential height, in meters.

    Returns
    -------
    hdw : ndarray, shape (...)
        HDW value at each grid point for this time step.
    """
    n_levels = P_levels_hpa.shape[0]

    # Build the qualifying-levels mask
    mask = qualifying_levels_mask(
        P_levels_hpa, P_sfc_hpa,
        Z_levels_m=Z_levels_m, Z_sfc_m=Z_sfc_m,
    )

    # ── Surface-adjusted VPD at each qualifying level ──
    # Broadcast P_levels to match level data shape
    P_bc = P_levels_hpa.reshape((n_levels,) + (1,) * P_sfc_hpa.ndim)

    vpd_levels = surface_adjusted_vpd(T_levels_K, q_levels, P_bc, P_sfc_hpa)
    spd_levels = wind_speed(u_levels, v_levels)

    # Mask out non-qualifying levels with NaN, then take max over levels
    vpd_levels = np.where(mask, vpd_levels, np.nan)
    spd_levels = np.where(mask, spd_levels, np.nan)

    # ── Include the surface itself ──
    vpd_sfc = surface_vpd(T_2m_K, Td_2m_K)
    spd_sfc = wind_speed(u_10m, v_10m)

    # Max VPD across all qualifying levels + surface
    max_vpd = np.fmax(np.nanmax(vpd_levels, axis=0), vpd_sfc)

    # Max wind speed across all qualifying levels + surface
    max_spd = np.fmax(np.nanmax(spd_levels, axis=0), spd_sfc)

    return max_vpd * max_spd


def hdw_daily(timestep_hdw_values: list[np.ndarray]) -> np.ndarray:
    """
    Daily HDW: maximum across burning-period time steps.

    Parameters
    ----------
    timestep_hdw_values : list of ndarray
        HDW values from each burning-period time step (e.g., 12Z, 18Z, 00Z).

    Returns
    -------
    hdw : ndarray
        Daily maximum HDW at each grid point.
    """
    stacked = np.stack(timestep_hdw_values, axis=0)
    return np.nanmax(stacked, axis=0)
