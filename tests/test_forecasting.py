"""Forecasting tests: metric formulas, leakage-free splits, baseline behavior,
and one fast integration pass per model family on a synthetic seasonal series.
Prophet is exercised only if installed (it is the slowest dependency)."""

import numpy as np
import pandas as pd
import pytest

from src.forecasting import backtest as bt
from src.forecasting import metrics
from src.forecasting import models


def make_series(n=36, seed=0) -> pd.Series:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    y = 100 + 1.5 * t + 10 * np.sin(2 * np.pi * t / 12) + rng.normal(0, 2, n)
    idx = pd.date_range("2023-01-01", periods=n, freq="MS")
    return pd.Series(y, index=idx)


class TestMetrics:
    def test_mape_known_value(self):
        assert metrics.mape([100, 200], [110, 180]) == pytest.approx(0.10)

    def test_mape_ignores_zero_actuals(self):
        assert metrics.mape([0, 100], [5, 110]) == pytest.approx(0.10)

    def test_rmse_mae_known_values(self):
        assert metrics.rmse([0, 0], [3, 4]) == pytest.approx(np.sqrt(12.5))
        assert metrics.mae([0, 0], [3, 4]) == pytest.approx(3.5)

    def test_coverage(self):
        assert metrics.interval_coverage([1, 5, 9], [0, 6, 8], [2, 7, 10]) == pytest.approx(2 / 3)


class TestSplits:
    def test_origins_never_leak(self):
        n = 36
        for origin in bt.split_origins(n):
            assert origin >= bt.MIN_TRAIN
            assert origin + bt.HORIZON <= n  # test window inside the series

    def test_expected_fold_count_36_months(self):
        assert bt.split_origins(36) == [18, 21, 24, 27, 30, 33]

    def test_backtest_train_strictly_before_test(self):
        calls = []

        def spy(train, horizon):
            calls.append((len(train), horizon))
            return models.naive(train, horizon)

        s = make_series()
        bt.backtest(s, spy, "spy", "t")
        assert [c[0] for c in calls] == [18, 21, 24, 27, 30, 33]


class TestBaselines:
    def test_naive_carries_last_value(self):
        s = make_series()
        fc = models.naive(s, 3)
        assert np.allclose(fc.mean, s.iloc[-1])
        assert (fc.lo80 < fc.mean).all() and (fc.hi80 > fc.mean).all()
        # intervals widen with horizon
        widths = fc.hi80 - fc.lo80
        assert widths[2] > widths[0]

    def test_seasonal_naive_repeats_last_year(self):
        s = make_series()
        fc = models.seasonal_naive(s, 3)
        assert np.allclose(fc.mean, s.iloc[-12:-9].to_numpy())

    def test_seasonal_naive_short_history_falls_back(self):
        s = make_series(n=10)
        fc = models.seasonal_naive(s, 2)
        assert np.allclose(fc.mean, s.iloc[-1])


class TestModelFamilies:
    """On a clean trend+seasonal series, every real model must beat naive."""

    @pytest.mark.parametrize("name", ["sarima", "xgboost"])
    def test_beats_naive_on_seasonal_trend(self, name):
        s = make_series()
        fn = models.CANDIDATES[name]
        m_model = bt.backtest(s, fn, name, "t").metrics()
        m_naive = bt.backtest(s, models.naive, "naive", "t").metrics()
        assert m_model["mape"] < m_naive["mape"]

    def test_prophet_runs_if_installed(self):
        pytest.importorskip("prophet")
        s = make_series()
        fc = models.prophet(s.iloc[:30], 3)
        assert len(fc.mean) == 3
        assert (fc.lo80 <= fc.hi80).all()

    def test_forecast_shapes_and_interval_order(self):
        s = make_series()
        for name, fn in {**models.BASELINES, "sarima": models.sarima,
                         "xgboost": models.xgboost_lags}.items():
            fc = fn(s.iloc[:24], 3)
            assert len(fc.mean) == len(fc.lo80) == len(fc.hi80) == 3, name
            assert (fc.lo80 <= fc.hi80).all(), name
