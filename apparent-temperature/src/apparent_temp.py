"""
Apparent temperature calculations using the NWS piecewise formula.

Wind chill applies when T <= 50°F and wind > 3 mph.
Heat index applies when T >= 80°F.
Otherwise the apparent temperature equals the air temperature.
"""

import numpy as np
import metpy.calc as mpcalc
from metpy.units import units


def wind_chill(temperature_f, wind_speed_mph):
    """NWS 2001 wind chill formula. Inputs in °F and mph. Output in °F."""
    T = np.asarray(temperature_f, dtype=float)
    V = np.asarray(wind_speed_mph, dtype=float)
    return 35.74 + 0.6215 * T - 35.75 * np.power(V, 0.16) + 0.4275 * T * np.power(V, 0.16)


def heat_index(temperature_f, relative_humidity_pct):
    """NWS heat index formula (Rothfusz regression with adjustments). Inputs in °F and %. Output in °F."""
    T = np.asarray(temperature_f, dtype=float)
    RH = np.asarray(relative_humidity_pct, dtype=float)

    HI_simple = 0.5 * (T + 61.0 + (T - 68.0) * 1.2 + RH * 0.094)

    HI_full = (
        -42.379
        + 2.04901523 * T
        + 10.14333127 * RH
        - 0.22475541 * T * RH
        - 6.83783e-3 * T**2
        - 5.481717e-2 * RH**2
        + 1.22874e-3 * T**2 * RH
        + 8.5282e-4 * T * RH**2
        - 1.99e-6 * T**2 * RH**2
    )

    low_mask = (RH < 13) & (T >= 80) & (T <= 112)
    low_adj = ((13 - RH) / 4) * np.sqrt(np.maximum(0, (17 - np.abs(T - 95)) / 17))
    HI_full = np.where(low_mask, HI_full - low_adj, HI_full)

    high_mask = (RH > 85) & (T >= 80) & (T <= 87)
    high_adj = ((RH - 85) / 10) * ((87 - T) / 5)
    HI_full = np.where(high_mask, HI_full + high_adj, HI_full)

    return np.where(HI_simple < 80, HI_simple, HI_full)


def apparent_temperature(temperature_f, dewpoint_f, wind_speed_mph):
    """NWS apparent temperature using piecewise wind chill / heat index / air temp."""
    T = np.asarray(temperature_f, dtype=float)
    Td = np.asarray(dewpoint_f, dtype=float)
    V = np.asarray(wind_speed_mph, dtype=float)

    T_q = T * units.degF
    Td_q = Td * units.degF
    rh = mpcalc.relative_humidity_from_dewpoint(T_q, Td_q).to('percent').magnitude

    WC = wind_chill(T, V)
    HI = heat_index(T, rh)

    use_wc = (T <= 50) & (V > 3)
    use_hi = (T >= 80)

    AT = np.where(use_wc, WC, np.where(use_hi, HI, T))

    if AT.ndim == 0:
        return float(AT)
    return AT
