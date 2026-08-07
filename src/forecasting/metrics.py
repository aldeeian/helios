"""Forecast evaluation metrics, implemented explicitly (no sklearn dependency)
so every number reported is a formula that can be defended in an interview."""

from __future__ import annotations

import numpy as np


def mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Mean absolute percentage error, ignoring zero-actual points.

    Zero actuals make MAPE undefined; they are excluded rather than fudged
    with an epsilon, and RMSE/MAE carry the signal for those points.
    """
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    mask = actual != 0
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])))


def rmse(actual: np.ndarray, predicted: np.ndarray) -> float:
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    return float(np.mean(np.abs(actual - predicted)))


def interval_coverage(actual: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> float:
    """Fraction of actuals inside [lo, hi] — validates that a nominal 80%
    interval empirically contains ~80% of outcomes (spec Phase 4 DoD)."""
    actual = np.asarray(actual, float)
    return float(np.mean((actual >= np.asarray(lo, float))
                         & (actual <= np.asarray(hi, float))))
