"""Warehouse tests: idempotency, Type 2 SCD behavior, and view correctness
against hand-calculated expectations (spec Phase 3 definition of done)."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

from src.datagen import sources
from src.datagen.truth import simulate
from src.reconciliation import engine as recon
from src.warehouse import load as wh


@pytest.fixture(scope="module")
def reconciled_dir(tmp_path_factory):
    """Generate + reconcile into a temp dir so tests never touch data/."""
    truth = simulate(42)
    rng = np.random.default_rng(43)
    log = {}
    hr = sources.build_hr_frame(truth, rng, log)
    fin = sources.build_financials_frame(truth, rng, log)
    lookup = sources.build_lookup_frame(log)
    ctr = sources.build_contractor_frame(truth, rng, log)
    plan = truth.headcount_plan[["department", "month", "planned_fte"]]
    result = recon.run(hr, fin, ctr, lookup, plan_raw=plan)
    d = tmp_path_factory.mktemp("reconciled")
    result.employees.to_csv(d / "employees.csv", index=False)
    result.contractors.to_csv(d / "contractors.csv", index=False)
    result.financials.to_csv(d / "financials.csv", index=False)
    result.plan.to_csv(d / "plan.csv", index=False)
    return d


@pytest.fixture()
def engine(tmp_path):
    eng = sa.create_engine(f"sqlite:///{(tmp_path / 'wh.db').as_posix()}")
    yield eng
    eng.dispose()


AS_OF = dt.date(2026, 1, 15)


class TestIdempotency:
    def test_double_load_identical_counts(self, reconciled_dir, engine):
        r1 = wh.load(reconciled_dir, engine, as_of=AS_OF)
        r2 = wh.load(reconciled_dir, engine, as_of=AS_OF)
        assert r1["row_counts"] == r2["row_counts"]
        assert r2["scd2"]["inserted"] == 0
        assert r2["scd2"]["versioned"] == 0
        assert r2["scd2"]["unchanged"] == r1["scd2"]["inserted"]


class TestScd2:
    def test_department_change_versions_the_row(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        emp = pd.read_csv(reconciled_dir / "employees.csv")
        victim = emp.iloc[0]["employee_id"]
        old_dept = emp.iloc[0]["department"]
        new_dept = "Technology" if old_dept != "Technology" else "Finance"
        emp.loc[emp["employee_id"] == victim, "department"] = new_dept
        d2 = reconciled_dir.parent / "reconciled_v2"
        d2.mkdir(exist_ok=True)
        emp.to_csv(d2 / "employees.csv", index=False)
        for f in ("contractors.csv", "financials.csv"):
            (d2 / f).write_bytes((reconciled_dir / f).read_bytes())

        later = AS_OF + dt.timedelta(days=30)
        r = wh.load(d2, engine, as_of=later)
        assert r["scd2"]["versioned"] == 1

        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT valid_from, valid_to, is_current FROM dim_employee "
                "WHERE employee_id = :e ORDER BY valid_from"), {"e": victim}
            ).fetchall()
        assert len(rows) == 2
        old, new = rows
        assert old.valid_to == later.isoformat() and not old.is_current
        assert new.valid_to is None and new.is_current

    def test_exactly_one_current_row_per_employee(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        with engine.connect() as conn:
            dup = conn.execute(sa.text(
                "SELECT employee_id, COUNT(*) c FROM dim_employee "
                "WHERE is_current = 1 GROUP BY employee_id HAVING c > 1"
            )).fetchall()
        assert dup == []


class TestViewCorrectness:
    def test_budget_vs_actual_matches_hand_calc(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        fin = pd.read_csv(reconciled_dir / "financials.csv")
        row = fin[(fin["cost_centre_code"] == "CC-100")
                  & (fin["account_category"] == "Salaries")].iloc[0]
        with engine.connect() as conn:
            got = conn.execute(sa.text(
                "SELECT budget_amount, actual_amount, variance_amount "
                "FROM vw_budget_vs_actual "
                "WHERE cost_centre_code = 'CC-100' AND account_category = 'Salaries' "
                "AND month_start = :m"),
                {"m": f"{row['period']}-01"},
            ).one()
        assert got.budget_amount == pytest.approx(float(row["budget_amount"]))
        assert got.actual_amount == pytest.approx(float(row["actual_amount"]))
        assert got.variance_amount == pytest.approx(
            float(row["actual_amount"]) - float(row["budget_amount"]), abs=0.01
        )

    def test_headcount_matches_manual_active_filter(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        emp = pd.read_csv(reconciled_dir / "employees.csv",
                          parse_dates=["hire_date", "termination_date"])
        month_start, month_end = dt.date(2024, 6, 1), dt.date(2024, 6, 30)
        ops = emp[emp["department"] == "Operations"]
        active = ops[
            (ops["hire_date"].dt.date <= month_end)
            & (ops["termination_date"].isna()
               | (ops["termination_date"].dt.date >= month_start))
        ]
        expected = round(active["fte"].sum(), 2)
        with engine.connect() as conn:
            got = conn.execute(sa.text(
                "SELECT employee_fte FROM vw_headcount_trend "
                "WHERE department_name = 'Operations' AND month_start = '2024-06-01'"
            )).scalar_one()
        assert got == pytest.approx(expected)

    def test_contractor_mix_share_bounds(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        with engine.connect() as conn:
            shares = [r[0] for r in conn.execute(sa.text(
                "SELECT contractor_share FROM vw_contractor_mix "
                "WHERE contractor_share IS NOT NULL"))]
        assert shares and all(0.0 <= s < 1.0 for s in shares)

    def test_vacancy_view_matches_plan_minus_employees(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT employee_fte, planned_fte, vacancy_fte, vacancy_rate "
                "FROM vw_headcount_vs_plan "
                "WHERE department_name = 'Operations' AND month_start = '2024-06-01'"
            )).one()
        assert row.vacancy_fte == pytest.approx(row.planned_fte - row.employee_fte)
        assert row.vacancy_rate == pytest.approx(
            (row.planned_fte - row.employee_fte) / row.planned_fte
        )

    def test_every_headcount_fact_has_plan(self, reconciled_dir, engine):
        wh.load(reconciled_dir, engine, as_of=AS_OF)
        with engine.connect() as conn:
            nulls = conn.execute(sa.text(
                "SELECT COUNT(*) FROM fact_headcount WHERE planned_fte IS NULL"
            )).scalar_one()
        assert nulls == 0
