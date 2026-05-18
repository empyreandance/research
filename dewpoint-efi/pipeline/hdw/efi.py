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
        EFI values at each grid point, in [-1, +1].  A fully displaced
        ensemble (every member outside the entire M-climate range)
        reaches +/-1.  Positive values indicate the ensemble is shifted
        toward higher HDW (more extreme fire weather) than M-climate.

    Notes
    -----
    The EFI weight 1/sqrt(p(1-p)) is singular at p=0 and p=1.  Dropping
    those endpoints discards a non-negligible share of the integral --
    because the weight diverges there, the slivers near the endpoints
    carry roughly 9% of the total -- which caps |EFI| near 0.91 instead
    of 1.0.  Instead, the singular weight is integrated analytically:
    its antiderivative is W(p) = 2*arcsin(sqrt(p)), which is finite at
    p=0 and p=1.  Each quantile sample is given a quadrature weight
    equal to the exact integral of 1/sqrt(p(1-p)) across its cell, and
    the smooth, bounded factor (p - F_f) is evaluated on the supplied
    quantile grid.  No endpoints are dropped and a fully displaced
    ensemble integrates to +/-1.
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
    extra = (1,) * len(spatial_shape)
    p = mclimate_probs  # all quantile levels, including p=0 and p=1

    # F_f(p): fraction of ensemble members at or below each M-climate
    # quantile value.  Shape: (n_q, n_members, ...) -> (n_q, ...)
    below = ensemble_hdw[np.newaxis, :, ...] <= mclimate_quantiles[:, np.newaxis, ...]
    F_f = below.sum(axis=1) / n_members

    # Smooth, bounded factor g(p) = p - F_f(p), values in [-1, 1].
    g = p.reshape((-1,) + extra) - F_f

    # Analytic quadrature weights for the singular EFI weight
    # 1/sqrt(p(1-p)).  Antiderivative: W(p) = 2*arcsin(sqrt(p)).
    # Each sample's weight is the exact integral of the singular weight
    # across its cell; cell edges are the midpoints between adjacent
    # samples, with the two end cells running to p=0 and p=1.
    edges = np.empty(n_q + 1, dtype=np.float64)
    edges[0] = 0.0
    edges[-1] = 1.0
    edges[1:-1] = 0.5 * (p[:-1] + p[1:])
    cell_w = np.diff(2.0 * np.arcsin(np.sqrt(edges)))  # shape (n_q,)

    # EFI = (2/pi) * integral of g(p) / sqrt(p(1-p)) dp
    integral = np.sum(g * cell_w.reshape((-1,) + extra), axis=0)

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
