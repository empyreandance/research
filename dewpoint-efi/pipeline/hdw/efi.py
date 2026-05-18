"""
Extreme Forecast Index (EFI) and Shift of Tails (SOT) for HDW.

Implements the ECMWF EFI/SOT framework applied to the Hot-Dry-Windy
Index, producing a Fire Weather EFI (FW-EFI) and FW-SOT.

EFI formula (Zsótér, 2006; Lalaurette, 2003):

    EFI = (2/π) ∫₀¹ [p − Fꜰ(p)] / √[p(1−p)] dp

where Fꜰ(p) is the fraction of ensemble members with HDW below the
p-th quantile of the M-climate distribution.

SOT formula (upper tail):

    SOT = [Qꜰ(0.9) − Qc(0.9)] / [Qc(0.99) − Qc(0.9)]

where Qꜰ(0.9) is the 90th percentile of the forecast ensemble and
Qc(0.9), Qc(0.99) are the 90th and 99th percentiles of M-climate.

References
----------
Lalaurette, F. (2003). Early detection of abnormal weather conditions
    using a probabilistic extreme forecast index. Q.J.R. Meteorol.
    Soc., 129, 3037–3057.

Zsótér, E. (2006). Recent developments in extreme weather forecasting.
    ECMWF Newsletter, 107, 8–17.
"""

import numpy as np
from typing import Optional


def compute_efi(
    ensemble_hdw: np.ndarray,
    mclimate_quantiles: np.ndarray,
    mclimate_probs: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    Compute the Extreme Forecast Index for HDW.

    Parameters
    ----------
    ensemble_hdw : array, shape (n_members, ...)
        HDW values from each ensemble member at each grid point.
    mclimate_quantiles : array, shape (n_quantiles, ...)
        Quantile values of the M-climate HDW distribution.  These are
        the HDW values corresponding to evenly spaced probabilities.
    mclimate_probs : array, shape (n_quantiles,), optional
        The probability levels corresponding to each quantile.
        If None, assumed to be np.linspace(0, 1, n_quantiles) from
        the 0th to 100th percentile — i.e., evenly spaced over [0, 1].

    Returns
    -------
    efi : ndarray, shape (...)
        EFI values at each grid point.  Range: approximately [-1, +1].
        Positive values indicate the ensemble is shifted toward higher
        HDW (more extreme fire weather) than M-climate.
    """
    ensemble_hdw = np.asarray(ensemble_hdw, dtype=np.float64)
    mclimate_quantiles = np.asarray(mclimate_quantiles, dtype=np.float64)
    n_q = mclimate_quantiles.shape[0]

    if mclimate_probs is None:
        mclimate_probs = np.linspace(0.0, 1.0, n_q)
    else:
        mclimate_probs = np.asarray(mclimate_probs, dtype=np.float64)

    n_members = ensemble_hdw.shape[0]
    spatial_shape = ensemble_hdw.shape[1:]

    # For numerical stability, skip the endpoints p=0 and p=1
    # where the weighting function 1/sqrt(p(1-p)) diverges.
    interior = (mclimate_probs > 0.0) & (mclimate_probs < 1.0)
    p = mclimate_probs[interior]
    q_c = mclimate_quantiles[interior]

    # Broadcast q_c to (n_interior, ...) and ensemble to (n_members, ...)
    # For each M-climate quantile value, compute the fraction of
    # ensemble members at or below that value.
    # F_f(p) = fraction of members with HDW <= Q_c(p)

    # Shape: (n_interior, n_members, ...)
    below = ensemble_hdw[np.newaxis, :, ...] <= q_c[:, np.newaxis, ...]

    # F_f at each probability level: shape (n_interior, ...)
    F_f = below.sum(axis=1) / n_members

    # Integrand: [p - F_f(p)] / sqrt(p * (1-p))
    # Broadcast p to (n_interior, 1, 1, ...) for spatial dims
    p_bc = p.reshape((-1,) + (1,) * len(spatial_shape))
    weight = 1.0 / np.sqrt(p_bc * (1.0 - p_bc))
    integrand = (p_bc - F_f) * weight

    # Trapezoidal integration over the probability dimension
    dp = np.diff(p)
    # Average adjacent integrand values, multiply by dp
    integral = np.sum(
        0.5 * (integrand[:-1] + integrand[1:]) * dp.reshape((-1,) + (1,) * len(spatial_shape)),
        axis=0,
    )

    efi = (2.0 / np.pi) * integral
    return np.clip(efi, -1.0, 1.0)


def compute_sot(
    ensemble_hdw: np.ndarray,
    mclimate_p90: np.ndarray,
    mclimate_p99: np.ndarray,
) -> np.ndarray:
    """
    Compute the Shift of Tails (upper tail) for HDW.

    SOT = [Q_f(0.9) - Q_c(0.9)] / [Q_c(0.99) - Q_c(0.9)]

    Parameters
    ----------
    ensemble_hdw : array, shape (n_members, ...)
        HDW values from each ensemble member.
    mclimate_p90 : array, shape (...)
        90th percentile of the M-climate HDW distribution.
    mclimate_p99 : array, shape (...)
        99th percentile of the M-climate HDW distribution.

    Returns
    -------
    sot : ndarray, shape (...)
        SOT values.  Positive means >= 10% of ensemble exceeds the
        M-climate 90th percentile.  SOT > 1 means >= 10% exceeds
        the 99th percentile — historically unprecedented.
        Where Q_c(0.99) == Q_c(0.9) (degenerate climate), SOT = NaN.
    """
    ensemble_hdw = np.asarray(ensemble_hdw, dtype=np.float64)
    mclimate_p90 = np.asarray(mclimate_p90, dtype=np.float64)
    mclimate_p99 = np.asarray(mclimate_p99, dtype=np.float64)

    # 90th percentile of the forecast ensemble
    Q_f_90 = np.percentile(ensemble_hdw, 90, axis=0)

    denominator = mclimate_p99 - mclimate_p90

    # Guard against degenerate M-climate (e.g., zero spread in the upper tail)
    with np.errstate(divide="ignore", invalid="ignore"):
        sot = (Q_f_90 - mclimate_p90) / denominator

    sot = np.where(denominator == 0.0, np.nan, sot)
    return sot


def build_mclimate_quantiles(
    hdw_samples: np.ndarray,
    n_quantiles: int = 101,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Build M-climate quantile arrays from a collection of HDW samples.

    Parameters
    ----------
    hdw_samples : array, shape (n_samples, ...)
        HDW values from the reforecast archive for a given (DOY, lead_day)
        at each grid point.
    n_quantiles : int
        Number of evenly spaced quantiles to store (default 101, i.e.,
        every 1 percentile from 0 to 100).

    Returns
    -------
    quantiles : ndarray, shape (n_quantiles, ...)
        The HDW values at each quantile level.
    probs : ndarray, shape (n_quantiles,)
        The probability levels (0.0, 0.01, 0.02, ..., 1.0).
    p90 : ndarray, shape (...)
        90th percentile (for SOT).
    p99 : ndarray, shape (...)
        99th percentile (for SOT).
    """
    probs = np.linspace(0.0, 100.0, n_quantiles)
    quantiles = np.percentile(hdw_samples, probs, axis=0)
    p90 = np.percentile(hdw_samples, 90, axis=0)
    p99 = np.percentile(hdw_samples, 99, axis=0)
    return quantiles, probs / 100.0, p90, p99
