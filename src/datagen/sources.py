"""Project the ground truth into three lossy, mutually-inconsistent source systems.

Every messiness injection is deliberate, seeded, and logged to the run manifest
(spec §2.2) so the README can state exactly what defects exist and why —
they are the test fixtures for the Phase 2 reconciliation engine.

Injection catalogue
-------------------
HR (Excel):        inconsistent department spellings; FTE as 0.5 vs 50 (mixed
                   scales); trailing whitespace; ~2% exact duplicate rows.
Finance (CSV):     ~4% of amounts as currency strings ("$12,345.67"); two
                   periods missing for one cost centre; the cost-centre lookup
                   ships separately and is missing one code (CC-500).
Contractors (DB):  three date formats mixed; ~5% of contractors also appear in
                   the HR roster under the same name (double-count trap).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import org_model as om
from .truth import Truth


def build_hr_frame(truth: Truth, rng: np.random.Generator, log: dict) -> pd.DataFrame:
    rows = []
    for e in truth.employees:
        dept = str(rng.choice(om.DEPT_ALIASES[e.department]))
        # FTE scale inconsistency: ~30% of rows use percent (50) not ratio (0.5)
        fte: float = e.fte * 100 if rng.random() < 0.30 else e.fte
        name = e.full_name + ("  " if rng.random() < 0.05 else "")
        rows.append({
            "employee_id": e.employee_id,
            "full_name": name,
            "department": dept,
            "role_level": e.role_level,
            "hire_date": e.hire_date.isoformat(),
            "termination_date": e.termination_date.isoformat() if e.termination_date else "",
            "fte_percent": fte,
            "location": str(rng.choice(["Calgary", "Calgary", "Calgary", "Toronto", "Remote"])),
        })
    df = pd.DataFrame(rows)
    n_dup = max(1, int(len(df) * 0.02))
    dup_rows = df.sample(n=n_dup, random_state=int(rng.integers(0, 2**31)))
    df = pd.concat([df, dup_rows], ignore_index=True)
    df = df.sample(frac=1.0, random_state=int(rng.integers(0, 2**31))).reset_index(drop=True)
    log["hr"] = {
        "rows": len(df),
        "duplicate_rows_injected": n_dup,
        "fte_scale_mix": "~30% of rows use 0-100 scale",
        "department_aliases": True,
        "trailing_whitespace_names": "~5%",
    }
    return df


def build_financials_frame(truth: Truth, rng: np.random.Generator, log: dict) -> pd.DataFrame:
    actuals = truth.monthly.melt(
        id_vars=["month", "month_index", "cost_centre"],
        value_vars=list(om.ACCOUNT_CATEGORIES),
        var_name="account_category",
        value_name="actual_amount",
    )
    # small reporting noise between "finance system" and truth
    actuals["actual_amount"] = (
        actuals["actual_amount"] * rng.normal(1.0, 0.005, len(actuals))
    ).round(2)
    budget = truth.budget[["cost_centre", "month", "account_category", "budget_amount"]]
    df = budget.merge(
        actuals[["cost_centre", "month", "account_category", "actual_amount"]],
        on=["cost_centre", "month", "account_category"],
        how="left",
    )
    df = df.rename(columns={"month": "period"})
    df["period"] = df["period"].str[:7]  # finance system reports YYYY-MM

    # Missing periods: CC-300 loses two months entirely (spec: "some periods missing")
    missing = {"2024-02", "2025-06"}
    before = len(df)
    df = df[~((df["cost_centre"] == "CC-300") & (df["period"].isin(missing)))]
    # Currency-string mess on ~4% of actual amounts
    df = df.reset_index(drop=True)
    mask = rng.random(len(df)) < 0.04
    df["actual_amount"] = df["actual_amount"].astype(object)
    df.loc[mask, "actual_amount"] = df.loc[mask, "actual_amount"].map(
        lambda v: f"${v:,.2f}" if pd.notna(v) else v
    )
    df["cost_centre_code"] = df.pop("cost_centre")
    df = df[["cost_centre_code", "period", "budget_amount", "actual_amount", "account_category"]]
    log["financials"] = {
        "rows": len(df),
        "missing_periods_injected": sorted(missing),
        "rows_dropped_for_missing_periods": before - len(df),
        "currency_string_rows": int(mask.sum()),
        "lookup_gap": "CC-500 absent from cost_centre_map.csv",
    }
    return df


def build_lookup_frame(log: dict) -> pd.DataFrame:
    """Cost centre -> department map, deliberately missing CC-500 (Sales)."""
    rows = [
        {"cost_centre_code": d.cost_centre, "department": d.name}
        for d in om.DEPARTMENTS
        if d.cost_centre != "CC-500"
    ]
    log["lookup"] = {"rows": len(rows), "missing_codes": ["CC-500"]}
    return pd.DataFrame(rows)


_DATE_FORMATS = ("iso", "dmy", "long")


def _format_date(d, style: str) -> str:
    if style == "iso":
        return d.isoformat()
    if style == "dmy":
        return d.strftime("%d/%m/%Y")
    return d.strftime("%b %d, %Y")


def build_contractor_frame(truth: Truth, rng: np.random.Generator, log: dict) -> pd.DataFrame:
    # Double-count trap: ~5% of contractors get the name of a real employee in
    # the same department (they "converted" or moonlight in both systems).
    emp_by_dept: dict[str, list] = {}
    for e in truth.employees:
        emp_by_dept.setdefault(e.department, []).append(e)
    rows = []
    n_overlap = 0
    for c in truth.contractors:
        name = c.contractor_name
        pool = emp_by_dept.get(c.department, [])
        if pool and rng.random() < 0.05:
            name = pool[int(rng.integers(0, len(pool)))].full_name
            n_overlap += 1
        style = str(rng.choice(_DATE_FORMATS))
        dept_code = next(
            d.cost_centre for d in om.DEPARTMENTS if d.name == c.department
        )
        rows.append({
            "vendor_id": c.vendor_id,
            "contractor_name": name,
            "department_code": dept_code,
            "start_date": _format_date(c.start_date, style),
            "end_date": _format_date(c.end_date, style) if c.end_date else None,
            "daily_rate": c.daily_rate,
            "status": c.status,
        })
    log["contractors"] = {
        "rows": len(rows),
        "employee_name_overlaps_injected": n_overlap,
        "date_formats": list(_DATE_FORMATS),
    }
    return pd.DataFrame(rows)
