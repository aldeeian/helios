import numpy as np
import pandas as pd

from src.datagen import org_model as om
from src.datagen import sources
from src.datagen.truth import simulate


def _mess_log():
    return {}


class TestReproducibility:
    def test_same_seed_identical_truth(self):
        a, b = simulate(42), simulate(42)
        pd.testing.assert_frame_equal(a.monthly, b.monthly)
        pd.testing.assert_frame_equal(a.budget, b.budget)
        assert [e.employee_id for e in a.employees] == [e.employee_id for e in b.employees]
        assert [e.full_name for e in a.employees] == [e.full_name for e in b.employees]

    def test_same_seed_identical_sources(self):
        t1, t2 = simulate(7), simulate(7)
        hr1 = sources.build_hr_frame(t1, np.random.default_rng(8), _mess_log())
        hr2 = sources.build_hr_frame(t2, np.random.default_rng(8), _mess_log())
        pd.testing.assert_frame_equal(hr1, hr2)

    def test_different_seed_differs(self):
        a, b = simulate(42), simulate(43)
        assert not a.monthly["headcount_fte"].equals(b.monthly["headcount_fte"])


class TestSignals:
    def test_structural_break_visible(self):
        t = simulate(42)
        m = t.monthly
        brk = om.STRUCTURAL_BREAK_MONTH
        for dept, direction in (("Operations", -1), ("Technology", +1)):
            g = m[m.department == dept].set_index("month_index")["headcount_fte"]
            pre = g.loc[brk - 4:brk - 1].mean()
            post = g.loc[brk:brk + 3].mean()
            change = (post - pre) / pre
            assert direction * change > 0.08, f"{dept} break not visible: {change:.2%}"

    def test_positive_trend_pre_break(self):
        t = simulate(42)
        total = t.monthly.groupby("month_index")["headcount_fte"].sum()
        assert total.loc[20] > total.loc[0]

    def test_budget_covers_all_dept_categories(self):
        t = simulate(42)
        got = set(map(tuple, t.budget[["department", "account_category"]].drop_duplicates().values))
        want = {(d.name, c) for d in om.DEPARTMENTS for c in om.ACCOUNT_CATEGORIES}
        assert got == want

    def test_headcount_plan_covers_every_dept_month(self):
        t = simulate(42)
        assert len(t.headcount_plan) == len(om.DEPARTMENTS) * om.N_MONTHS
        assert (t.headcount_plan["planned_fte"] > 0).all()
        got = set(map(tuple, t.headcount_plan[["department", "month_index"]].values))
        want = {(d.name, m) for d in om.DEPARTMENTS for m in range(om.N_MONTHS)}
        assert got == want


class TestInjections:
    def test_hr_has_documented_mess(self):
        t = simulate(42)
        log = {}
        hr = sources.build_hr_frame(t, np.random.default_rng(43), log)
        assert hr.duplicated().sum() >= log["hr"]["duplicate_rows_injected"]
        assert (hr["fte_percent"] > 1.5).any()          # percent-scale rows exist
        assert (hr["fte_percent"] <= 1.0).any()         # ratio-scale rows exist
        raw_depts = set(hr["department"])
        assert any(d not in {x.name for x in om.DEPARTMENTS} for d in raw_depts)

    def test_lookup_missing_cc500(self):
        log = {}
        lookup = sources.build_lookup_frame(log)
        assert "CC-500" not in set(lookup["cost_centre_code"])

    def test_financials_currency_strings_and_gaps(self):
        t = simulate(42)
        log = {}
        fin = sources.build_financials_frame(t, np.random.default_rng(44), log)
        assert fin["actual_amount"].astype(str).str.startswith("$").any()
        cc300 = fin[fin["cost_centre_code"] == "CC-300"]
        assert "2024-02" not in set(cc300["period"])
