"""Forecasting model families behind one interface.

    forecast_fn(train: pd.Series, horizon: int) -> Forecast(mean, lo80, hi80)

Baselines (naive, seasonal naive) come first: every real model must beat them
or it does not ship — reporting against baselines is the mark of seriousness
the spec demands (Phase 4 step 1).

Interval philosophy: SARIMA and Prophet produce native 80% intervals; the
baselines and XGBoost use gaussian intervals from in-sample residuals scaled
by sqrt(h) — an honest approximation whose empirical coverage is *measured*
in backtesting rather than assumed.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

Z80 = 1.2816  # two-sided 80% normal quantile


@dataclass
class Forecast:
    mean: np.ndarray
    lo80: np.ndarray
    hi80: np.ndarray


ForecastFn = Callable[[pd.Series, int], Forecast]


def _gaussian_interval(mean: np.ndarray, sigma1: float) -> Forecast:
    h = np.arange(1, len(mean) + 1)
    half = Z80 * sigma1 * np.sqrt(h)
    return Forecast(mean=mean, lo80=mean - half, hi80=mean + half)


def naive(train: pd.Series, horizon: int) -> Forecast:
    """Last value carried forward."""
    y = train.to_numpy(float)
    mean = np.full(horizon, y[-1])
    resid = np.diff(y)
    sigma = float(np.std(resid, ddof=1)) if len(resid) > 2 else float(np.std(y))
    return _gaussian_interval(mean, sigma)


def seasonal_naive(train: pd.Series, horizon: int) -> Forecast:
    """Same month last year; falls back to naive when history < 13 months."""
    y = train.to_numpy(float)
    if len(y) < 13:
        return naive(train, horizon)
    mean = np.array([y[len(y) - 12 + (h % 12)] for h in range(horizon)])
    resid = y[12:] - y[:-12]
    sigma = float(np.std(resid, ddof=1)) if len(resid) > 2 else float(np.std(y))
    return _gaussian_interval(mean, sigma)


def sarima(train: pd.Series, horizon: int) -> Forecast:
    """SARIMAX with a history-length-aware order heuristic.

    Full seasonal differencing (0,1,1,12) needs ~2.5 seasonal cycles; shorter
    histories get a seasonal AR term instead, and very short ones a plain
    ARIMA(1,1,1). The heuristic is deterministic and documented rather than
    auto-searched — 36-point series do not support a credible grid search.
    """
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    n = len(train)
    if n >= 30:
        seasonal_order = (0, 1, 1, 12)
    elif n >= 16:
        seasonal_order = (1, 0, 0, 12)
    else:
        seasonal_order = (0, 0, 0, 0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = SARIMAX(
            train.to_numpy(float), order=(1, 1, 1), seasonal_order=seasonal_order,
            enforce_stationarity=False, enforce_invertibility=False,
        )
        fit = model.fit(disp=False)
        sf = fit.get_forecast(horizon).summary_frame(alpha=0.20)
    return Forecast(
        mean=sf["mean"].to_numpy(),
        lo80=sf["mean_ci_lower"].to_numpy(),
        hi80=sf["mean_ci_upper"].to_numpy(),
    )


def prophet(train: pd.Series, horizon: int) -> Forecast:
    """Prophet with yearly seasonality only (monthly data has no weekly/daily)."""
    import logging

    from prophet import Prophet

    logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
    logging.getLogger("prophet").setLevel(logging.ERROR)
    df = pd.DataFrame({"ds": train.index, "y": train.to_numpy(float)})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = Prophet(
            yearly_seasonality=True, weekly_seasonality=False,
            daily_seasonality=False, interval_width=0.80,
        )
        m.fit(df)
        future = m.make_future_dataframe(periods=horizon, freq="MS")
        out = m.predict(future).tail(horizon)
    return Forecast(
        mean=out["yhat"].to_numpy(),
        lo80=out["yhat_lower"].to_numpy(),
        hi80=out["yhat_upper"].to_numpy(),
    )


def xgboost_lags(train: pd.Series, horizon: int) -> Forecast:
    """XGBoost on lag features with recursive multi-step forecasting."""
    from xgboost import XGBRegressor

    y = train.to_numpy(float)
    months = train.index.month.to_numpy()
    use_lag12 = len(y) > 15
    min_lag = 12 if use_lag12 else 3

    def make_row(series: np.ndarray, t: int, month: int) -> list[float]:
        row = [series[t - 1], series[t - 2], series[t - 3]]
        if use_lag12:
            row.append(series[t - 12])
        row += [np.sin(2 * np.pi * month / 12), np.cos(2 * np.pi * month / 12), float(t)]
        return row

    X = np.array([make_row(y, t, months[t]) for t in range(min_lag, len(y))])
    target = y[min_lag:]
    model = XGBRegressor(
        n_estimators=250, max_depth=3, learning_rate=0.08,
        random_state=0, verbosity=0,
    )
    model.fit(X, target)

    sigma = float(np.std(target - model.predict(X), ddof=1))
    # In-sample residuals understate error; floor sigma with the naive
    # 1-step residual scale so intervals are not absurdly optimistic.
    sigma = max(sigma, float(np.std(np.diff(y), ddof=1)) * 0.5)

    history = list(y)
    last_month = int(months[-1])
    preds = []
    for h in range(1, horizon + 1):
        month = (last_month + h - 1) % 12 + 1
        t = len(history)
        row = np.array([make_row(np.asarray(history), t, month)])
        p = float(model.predict(row)[0])
        preds.append(p)
        history.append(p)  # recursive: feed own predictions forward
    return _gaussian_interval(np.asarray(preds), sigma)


BASELINES: dict[str, ForecastFn] = {
    "naive": naive,
    "seasonal_naive": seasonal_naive,
}

CANDIDATES: dict[str, ForecastFn] = {
    "sarima": sarima,
    "prophet": prophet,
    "xgboost": xgboost_lags,
}
