"""Rolling-origin (expanding window) cross-validation for time series.

The point (spec Phase 4, 'critical technical point'): a random train/test
split leaks future information into the past. Here every fold trains strictly
on observations *before* the forecast origin and is scored on the next
`horizon` observations — the only honest way to estimate forecast accuracy.
Fold structure with the defaults on a 36-month series:

    origin 18 -> test 19..21     origin 27 -> test 28..30
    origin 21 -> test 22..24     origin 30 -> test 31..33
    origin 24 -> test 25..27     origin 33 -> test 34..36
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import metrics
from .models import Forecast, ForecastFn

MIN_TRAIN = 18
HORIZON = 3
STEP = 3


@dataclass
class Fold:
    origin: int                 # train = series[:origin], test = series[origin:origin+h]
    actual: np.ndarray
    forecast: Forecast


@dataclass
class BacktestResult:
    model: str
    target: str
    folds: list[Fold] = field(default_factory=list)

    @property
    def actuals(self) -> np.ndarray:
        return np.concatenate([f.actual for f in self.folds])

    @property
    def predictions(self) -> np.ndarray:
        return np.concatenate([f.forecast.mean for f in self.folds])

    def metrics(self) -> dict[str, float]:
        a, p = self.actuals, self.predictions
        lo = np.concatenate([f.forecast.lo80 for f in self.folds])
        hi = np.concatenate([f.forecast.hi80 for f in self.folds])
        return {
            "mape": metrics.mape(a, p),
            "rmse": metrics.rmse(a, p),
            "mae": metrics.mae(a, p),
            "coverage80": metrics.interval_coverage(a, lo, hi),
            "n_folds": float(len(self.folds)),
            "n_points": float(len(a)),
        }


def split_origins(n: int, min_train: int = MIN_TRAIN, horizon: int = HORIZON,
                  step: int = STEP) -> list[int]:
    """Forecast origins for an n-point series. Each origin o yields
    train [0, o) and test [o, o+horizon) — never overlapping, never leaking."""
    return list(range(min_train, n - horizon + 1, step))


def backtest(series: pd.Series, fn: ForecastFn, model_name: str, target: str,
             min_train: int = MIN_TRAIN, horizon: int = HORIZON,
             step: int = STEP) -> BacktestResult:
    y = series.dropna()
    result = BacktestResult(model=model_name, target=target)
    for origin in split_origins(len(y), min_train, horizon, step):
        train = y.iloc[:origin]
        test = y.iloc[origin:origin + horizon]
        fc = fn(train, len(test))
        assert len(fc.mean) == len(test)
        result.folds.append(Fold(origin=origin, actual=test.to_numpy(float), forecast=fc))
    return result
