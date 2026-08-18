"""Variance tests: the volume/mix/rate identity, materiality classification,
anomaly detection, and end-to-end decomposition against the warehouse."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

from src.datagen import sources
from src.datagen.truth import simulate
from src.reconciliation import engine as recon
from src.variance import analysis as va
from src.warehouse import load as wh


class TestDecompositionIdentity:
    """volume + mix + rate must equal total variance — the core claim."""

    def test_reconciles_on_synthetic_cells(self):
        cells = pd.DataFrame({
            "department": ["A", "B", "C"],
            "actual_qty": [100.0, 50.0, 30.0],
            "budget_qty": [90.0, 55.0, 25.0],
            "actual_spend": [520000.0, 260000.0, 180000.0],
            "budget_spend": [450000.0, 275000.0, 150000.0],
        })
        d = va.decompose(cells)
        total = cells["actual_spend"].sum() - cells["budget_spend"].sum()
        assert d.reconciles
        assert d.volume_variance + d.mix_variance + d.rate_variance == pytest.approx(total)
        assert d.total_variance == pytest.approx(total)

    def test_pure_rate_change_isolates_to_rate(self):
        # Quantities identical to plan; only rate differs -> volume=mix=0.
        cells = pd.DataFrame({
            "department": ["A", "B"],
            "actual_qty": [100.0, 50.0],
            "budget_qty": [100.0, 50.0],
            "actual_spend": [110000.0, 55000.0],
            "budget_spend": [100000.0, 50000.0],
        })
        d = va.decompose(cells)
        assert d.volume_variance == pytest.approx(0.0, abs=1e-6)
        assert d.mix_variance == pytest.approx(0.0, abs=1e-6)
        assert d.rate_variance == pytest.approx(15000.0)

    def test_pure_volume_change_isolates_to_volume(self):
        # Same rate everywhere and same mix; only total quantity scales up.
        # rate = 1000/unit for both; mix preserved (A:B = 2:1) -> mix=0.
        cells = pd.DataFrame({
            "department": ["A", "B"],
            "actual_qty": [120.0, 60.0],
            "budget_qty": [100.0, 50.0],
            "actual_spend": [120000.0, 60000.0],
            "budget_spend": [100000.0, 50000.0],
        })
        d = va.decompose(cells)
        assert d.rate_variance == pytest.approx(0.0, abs=1e-6)
        assert d.mix_variance == pytest.approx(0.0, abs=1e-6)
        assert d.volume_variance == pytest.approx(30000.0)

    def test_residual_from_zero_qty_cell_folded_so_identity_holds(self):
        # A cell with zero actual qty but nonzero spend must not break the identity.
        cells = pd.DataFrame({
            "department": ["A", "B"],
            "actual_qty": [100.0, 0.0],
            "budget_qty": [100.0, 10.0],
            "actual_spend": [100000.0, 5000.0],
            "budget_spend": [100000.0, 10000.0],
        })
        d = va.decompose(cells)
        total = cells["actual_spend"].sum() - cells["budget_spend"].sum()
        assert d.reconciles
        assert d.volume_variance + d.mix_variance + d.rate_variance == pytest.approx(total)


class TestVarianceClassification:
    def _frame(self):
        return pd.DataFrame({
            "month_start": pd.to_datetime(["2024-01-01"] * 3),
            "department_name": ["Ops", "Ops", "Tech"],
            "cost_centre_code": ["CC-1", "CC-1", "CC-2"],
            "account_category": ["Salaries", "Travel", "Salaries"],
            "budget_amount": [1_000_000.0, 2_000.0, 500_000.0],
            "actual_amount": [1_100_000.0, 3_000.0, 505_000.0],
            "employee_fte": [100.0, 100.0, 40.0],
            "planned_fte": [95.0, 95.0, 38.0],
        })

    def test_direction_and_amount(self):
        out = va.compute_variance(self._frame())
        ops_sal = out[(out.department_name == "Ops") & (out.account_category == "Salaries")].iloc[0]
        assert ops_sal["variance_amount"] == pytest.approx(100_000.0)
        assert ops_sal["direction"] == "unfavourable"

    def test_materiality_needs_both_pct_and_dollars(self):
        out = va.compute_variance(self._frame())
        # Ops Salaries: +10% and +$100k -> material
        ops_sal = out[(out.department_name == "Ops") & (out.account_category == "Salaries")].iloc[0]
        assert ops_sal["material"]
        # Ops Travel: +50% but only +$1k -> NOT material (fails dollar threshold)
        ops_trav = out[(out.department_name == "Ops") & (out.account_category == "Travel")].iloc[0]
        assert not ops_trav["material"]
        # Tech Salaries: +$5k but only +1% -> NOT material (fails pct threshold)
        tech_sal = out[out.department_name == "Tech"].iloc[0]
        assert not tech_sal["material"]

    def test_thresholds_configurable(self):
        out = va.compute_variance(self._frame(), pct_threshold=0.0, abs_threshold=0.0)
        assert out["material"].all()  # everything material when thresholds off


class TestAnomalies:
    def test_flags_outlier_against_stable_history(self):
        months = pd.date_range("2023-01-01", periods=12, freq="MS")
        stable = [0.01, 0.02, 0.0, -0.01, 0.015, 0.005, -0.005, 0.01, 0.0, 0.02, 0.01, 0.60]
        df = pd.DataFrame({
            "month_start": months,
            "department_name": ["Ops"] * 12,
            "account_category": ["Salaries"] * 12,
            "variance_pct": stable,
            "variance_amount": [x * 1e6 for x in stable],
            "material": [False] * 12,
        })
        out = va.detect_anomalies(df, z_threshold=2.0)
        assert out.iloc[-1]["anomaly"]           # the 60% spike
        assert not out.iloc[:-1]["anomaly"].any()  # the stable months are clean


@pytest.fixture(scope="module")
def warehouse_engine(tmp_path_factory):
    truth = simulate(42)
    rng = np.random.default_rng(43)
    log = {}
    hr = sources.build_hr_frame(truth, rng, log)
    fin = sources.build_financials_frame(truth, rng, log)
    lookup = sources.build_lookup_frame(log)
    ctr = sources.build_contractor_frame(truth, rng, log)
    plan = truth.headcount_plan[["department", "month", "planned_fte"]]
    result = recon.run(hr, fin, ctr, lookup, plan_raw=plan)
    d = tmp_path_factory.mktemp("recon")
    for name, frame in [("employees", result.employees), ("contractors", result.contractors),
                        ("financials", result.financials), ("plan", result.plan)]:
        frame.to_csv(d / f"{name}.csv", index=False)
    eng = sa.create_engine(f"sqlite:///{(tmp_path_factory.mktemp('wh') / 'wh.db').as_posix()}")
    wh.load(d, eng, as_of=dt.date(2026, 1, 1))
    yield eng
    eng.dispose()


class TestEndToEnd:
    def test_salary_decomposition_reconciles_every_month(self, warehouse_engine):
        df = va.load_spend_frame(warehouse_engine)
        var = va.compute_variance(df)
        for month in var["month_start"].unique():
            d = va.decompose_salaries(var, pd.Timestamp(month))
            if d.total_variance != 0:
                assert d.reconciles, f"{month} does not reconcile"

    def test_structural_break_creates_material_variance(self, warehouse_engine):
        df = va.load_spend_frame(warehouse_engine)
        var = va.compute_variance(df)
        post = var[var["month_start"] >= pd.Timestamp("2025-01-01")]
        # The month-24 reorg must surface as material variances post-break.
        assert post["material"].sum() > 0

    def test_summary_renders(self, warehouse_engine):
        df = va.load_spend_frame(warehouse_engine)
        var = va.compute_variance(df)
        text = va.summarize_period(var, pd.Timestamp("2025-06-01"))
        assert "Variance summary" in text and "drivers" in text
