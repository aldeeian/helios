"""Monitoring tests: PSI/KS correctness, the four monitors, retrain gating,
and an end-to-end cycle where an injected break triggers detect->retrain->promote."""

import numpy as np
import pandas as pd
import pytest

from src.forecasting.models import naive, sarima
from src.monitoring import drift, monitors, pipeline, retrain


class TestPSI:
    def test_identical_distributions_near_zero(self):
        rng = np.random.default_rng(0)
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(0, 1, 5000)
        r = drift.population_stability_index(ref, cur)
        assert r.psi < drift.PSI_STABLE
        assert r.verdict == "stable"

    def test_large_shift_significant(self):
        rng = np.random.default_rng(1)
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(2.0, 1, 5000)
        r = drift.population_stability_index(ref, cur)
        assert r.psi > drift.PSI_MODERATE
        assert r.verdict == "significant_shift"

    def test_per_bin_contributions_sum_to_psi(self):
        rng = np.random.default_rng(2)
        ref = rng.normal(0, 1, 2000)
        cur = rng.normal(0.5, 1.2, 500)
        r = drift.population_stability_index(ref, cur)
        assert sum(b["contribution"] for b in r.per_bin) == pytest.approx(r.psi, abs=1e-4)

    def test_psi_is_nonnegative(self):
        rng = np.random.default_rng(3)
        for _ in range(5):
            ref = rng.normal(0, 1, 1000)
            cur = rng.normal(rng.uniform(-2, 2), rng.uniform(0.5, 2), 500)
            assert drift.population_stability_index(ref, cur).psi >= 0


class TestKS:
    def test_identical_high_p_no_drift(self):
        rng = np.random.default_rng(0)
        a = rng.normal(0, 1, 1000)
        b = rng.normal(0, 1, 1000)
        r = drift.ks_two_sample(a, b)
        assert r.p_value > 0.05 and not r.drift

    def test_shifted_low_p_drift(self):
        rng = np.random.default_rng(1)
        a = rng.normal(0, 1, 1000)
        b = rng.normal(1.5, 1, 1000)
        r = drift.ks_two_sample(a, b)
        assert r.p_value < 0.05 and r.drift

    def test_statistic_matches_manual_ecdf(self):
        a = np.array([1.0, 2, 3, 4])
        b = np.array([1.5, 2.5, 3.5, 4.5, 5.5])
        r = drift.ks_two_sample(a, b)
        # brute-force max ECDF gap
        grid = np.concatenate([a, b])
        d = max(abs((a <= x).mean() - (b <= x).mean()) for x in grid)
        assert r.statistic == pytest.approx(d)

    def test_q_function_bounds(self):
        assert drift._kolmogorov_q(0) == 1.0
        assert 0.0 <= drift._kolmogorov_q(2.0) <= 1.0


class TestMonitors:
    def test_data_drift_fires_and_stays_quiet(self):
        rng = np.random.default_rng(0)
        ref = rng.normal(10, 2, 500)
        assert not monitors.data_drift(ref, rng.normal(10, 2, 200)).drift
        assert monitors.data_drift(ref, rng.normal(16, 2, 200)).drift

    def test_performance_decay_ratio(self):
        ref_err = np.array([1.0, -1, 2, -2, 1])
        assert not monitors.performance_decay(ref_err, ref_err * 1.2).drift
        assert monitors.performance_decay(ref_err, ref_err * 3.0).drift

    def test_concept_drift_on_biased_residuals(self):
        rng = np.random.default_rng(0)
        ref = rng.normal(0, 1, 300)
        assert not monitors.concept_drift(ref, rng.normal(0, 1, 100)).drift
        assert monitors.concept_drift(ref, rng.normal(3, 1, 100)).drift  # biased


class TestRetrainGate:
    def _signal(self, name, drift_flag):
        return monitors.DriftSignal(name, drift_flag, {})

    def test_data_drift_alone_does_not_trigger(self):
        sigs = [self._signal("data_drift", True),
                self._signal("prediction_drift", False),
                self._signal("concept_drift", False),
                self._signal("performance_decay", False)]
        assert not retrain.should_retrain(sigs).triggered

    def test_performance_decay_triggers(self):
        sigs = [self._signal("data_drift", False),
                self._signal("performance_decay", True)]
        d = retrain.should_retrain(sigs)
        assert d.triggered and "performance_decay" in d.reasons

    def test_challenger_promoted_only_when_better(self):
        # Series with a level break at index 24; champion trained pre-break is
        # worse on the recent holdout than a challenger trained through it.
        idx = pd.date_range("2023-01-01", periods=36, freq="MS")
        y = np.concatenate([np.full(24, 100.0), np.full(12, 150.0)])
        y = y + np.random.default_rng(0).normal(0, 1, 36)
        s = pd.Series(y, index=idx)
        res = retrain.champion_challenger(
            s, naive, naive, "naive", "naive-refit", train_end=24, holdout=6)
        assert res.promoted
        assert res.challenger_mae < res.champion_mae

    def test_challenger_rejected_when_no_improvement(self):
        # Stationary series: retraining on more data yields no real gain.
        idx = pd.date_range("2023-01-01", periods=36, freq="MS")
        y = 100 + np.random.default_rng(1).normal(0, 1, 36)
        s = pd.Series(y, index=idx)
        res = retrain.champion_challenger(
            s, naive, naive, "naive", "naive-refit", train_end=24, holdout=6)
        assert not res.promoted


class TestEndToEndCycle:
    def _series_with_break(self, break_at=24, jump=50.0):
        idx = pd.date_range("2023-01-01", periods=36, freq="MS")
        t = np.arange(36)
        base = 100 + 8 * np.sin(2 * np.pi * t / 12)
        base[break_at:] += jump
        return pd.Series(base + np.random.default_rng(0).normal(0, 1, 36), index=idx)

    def test_break_triggers_and_promotes(self):
        s = self._series_with_break()
        result = pipeline.run_cycle(s, "test", sarima, "sarima", break_index=24)
        assert result.retrain.triggered
        assert result.detail["cur_mae"] > result.detail["ref_mae"]
        assert result.promotion is not None and result.promotion.promoted

    def test_no_break_no_retrain(self):
        # Stationary seasonal series, no break -> monitors should stay quiet
        # enough that retraining is not triggered.
        idx = pd.date_range("2023-01-01", periods=36, freq="MS")
        t = np.arange(36)
        s = pd.Series(100 + 8 * np.sin(2 * np.pi * t / 12)
                      + np.random.default_rng(5).normal(0, 1, 36), index=idx)
        result = pipeline.run_cycle(s, "test", sarima, "sarima", break_index=24)
        assert not result.retrain.triggered
