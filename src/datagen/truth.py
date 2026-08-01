"""Month-by-month simulation of the true organizational state.

Simulates at the *employee level* (individual hires/terminations against a
target headcount trajectory) rather than sampling aggregates directly, so the
HR roster, contractor roster, and financials are all consistent projections of
one underlying reality — exactly like real source systems.

Signal embedded (spec §2.3): trend, hiring/travel seasonality, gaussian noise,
and a structural break at month 24 that the drift detector must later catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import org_model as om

# Deterministic name pools (clearly synthetic; no real-person data).
_FIRST = [
    "Aiden", "Bella", "Carlos", "Dina", "Elias", "Fatima", "Grace", "Hassan",
    "Ivy", "Jonas", "Kira", "Liam", "Maya", "Noor", "Omar", "Priya", "Quinn",
    "Rosa", "Sam", "Tara", "Umar", "Vera", "Wes", "Ximena", "Yusuf", "Zara",
    "Anders", "Bianca", "Cyrus", "Delia", "Emre", "Freya", "Gustav", "Hana",
    "Idris", "Jade", "Kofi", "Lena", "Milan", "Nadia",
]
_LAST = [
    "Ahmed", "Baker", "Chen", "Dubois", "Evans", "Fischer", "Garcia", "Hansen",
    "Iqbal", "Johnson", "Khan", "Lopez", "Martin", "Nguyen", "Okafor", "Patel",
    "Quintero", "Rahman", "Singh", "Tremblay", "Ueda", "Volkov", "Wong", "Xu",
    "Yilmaz", "Zhang", "Almeida", "Bergström", "Costa", "Devi", "Eriksen",
    "Fontaine", "Grigoryan", "Haddad", "Ivanova", "Jansen", "Kaur", "Larsen",
    "Moreau", "Novak",
]


@dataclass
class Employee:
    employee_id: str
    full_name: str
    department: str
    role_level: str
    hire_date: date
    termination_date: date | None
    fte: float  # canonical representation: 0.0–1.0


@dataclass
class Contractor:
    vendor_id: str
    contractor_name: str
    department: str
    start_date: date
    end_date: date | None
    daily_rate: float
    status: str  # "active" | "ended"


@dataclass
class Truth:
    """Complete ground truth for one simulation run."""
    seed: int
    employees: list[Employee]
    contractors: list[Contractor]
    monthly: pd.DataFrame       # department x month: headcount_fte + cost components
    budget: pd.DataFrame        # cost_centre x month x category: budget_amount
    headcount_plan: pd.DataFrame  # department x month: planned_fte (workforce plan)
    params: dict = field(default_factory=dict)


def _make_name(rng: np.random.Generator, used: set[str]) -> str:
    """Unique synthetic full name. Uniqueness across the whole simulation means
    any cross-system name collision is a *deliberate* double-count injection,
    never a coincidence — so the reconciliation tests can assert exact behavior.
    (40x40 = 1600 combinations comfortably covers ~700 simulated people.)"""
    for _ in range(1000):
        name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        if name not in used:
            used.add(name)
            return name
    raise RuntimeError("name pool exhausted — enlarge _FIRST/_LAST")


def _sample_role(rng: np.random.Generator) -> str:
    roles = list(om.ROLE_MIX)
    return str(rng.choice(roles, p=[om.ROLE_MIX[r] for r in roles]))


def _sample_fte(rng: np.random.Generator) -> float:
    # 85% full-time; the rest part-time between 0.4 and 0.8.
    if rng.random() < 0.85:
        return 1.0
    return float(np.round(rng.uniform(0.4, 0.8), 1))


def _target_headcount(dept: om.Department, month: int, rng: np.random.Generator) -> float:
    """Trend x seasonality x break x noise — the true generating process."""
    cal_month = om.month_to_date(month).month
    trend = dept.base_headcount * (1.0 + dept.annual_growth) ** (month / 12.0)
    seasonal = om.HIRING_SEASONALITY[cal_month]
    brk = dept.break_multiplier if month >= om.STRUCTURAL_BREAK_MONTH else 1.0
    noise = rng.normal(1.0, 0.015)
    return trend * seasonal * brk * noise


def simulate(seed: int) -> Truth:
    rng = np.random.default_rng(seed)
    employees: list[Employee] = []
    contractors: list[Contractor] = []
    active: dict[str, list[Employee]] = {d.name: [] for d in om.DEPARTMENTS}
    active_ctr: dict[str, list[Contractor]] = {d.name: [] for d in om.DEPARTMENTS}
    next_emp = 1
    next_vendor = 1
    used_names: set[str] = set()
    monthly_rows: list[dict] = []

    def hire(dept: om.Department, month_date: date) -> Employee:
        nonlocal next_emp
        emp = Employee(
            employee_id=f"E{next_emp:05d}",
            full_name=_make_name(rng, used_names),
            department=dept.name,
            role_level=_sample_role(rng),
            hire_date=month_date + timedelta(days=int(rng.integers(0, 28))),
            termination_date=None,
            fte=_sample_fte(rng),
        )
        next_emp += 1
        employees.append(emp)
        active[dept.name].append(emp)
        return emp

    def engage_contractor(dept: om.Department, month_date: date, month: int) -> Contractor:
        nonlocal next_vendor
        rate = dept.contractor_day_rate * float(rng.normal(1.0, 0.08))
        if month >= om.STRUCTURAL_BREAK_MONTH:
            rate *= om.BREAK_CONTRACTOR_RATE_UPLIFT
        ctr = Contractor(
            vendor_id=f"V{next_vendor:04d}",
            contractor_name=_make_name(rng, used_names),
            department=dept.name,
            start_date=month_date + timedelta(days=int(rng.integers(0, 28))),
            end_date=None,
            daily_rate=round(rate, 2),
            status="active",
        )
        next_vendor += 1
        contractors.append(ctr)
        active_ctr[dept.name].append(ctr)
        return ctr

    for month in range(om.N_MONTHS):
        month_date = om.month_to_date(month)
        for dept in om.DEPARTMENTS:
            # --- employee churn toward target ---
            roster = active[dept.name]
            # natural attrition
            leavers = [e for e in roster if rng.random() < om.MONTHLY_ATTRITION]
            for e in leavers:
                e.termination_date = month_date + timedelta(days=int(rng.integers(0, 28)))
                roster.remove(e)
            target = _target_headcount(dept, month, rng)
            current_fte = sum(e.fte for e in roster)
            gap = target - current_fte
            if gap > 0.5:
                for _ in range(int(round(gap))):
                    hire(dept, month_date)
            elif gap < -1.5:  # downsizing (post-break Operations)
                for _ in range(int(round(-gap))):
                    if roster:
                        e = roster.pop(int(rng.integers(0, len(roster))))
                        e.termination_date = month_date + timedelta(days=int(rng.integers(0, 28)))

            # --- contractor churn toward ratio ---
            ctr_roster = active_ctr[dept.name]
            for c in list(ctr_roster):
                if rng.random() < 0.05:  # engagements end
                    c.end_date = month_date + timedelta(days=int(rng.integers(0, 28)))
                    c.status = "ended"
                    ctr_roster.remove(c)
            ratio = dept.contractor_ratio
            if dept.name == "Technology" and month >= om.STRUCTURAL_BREAK_MONTH:
                ratio *= 1.3  # reorg pushes more work to external staff
            ctr_target = sum(e.fte for e in roster) * ratio
            while len(ctr_roster) < int(round(ctr_target)):
                engage_contractor(dept, month_date, month)

            # --- true monthly costs ---
            headcount_fte = sum(e.fte for e in roster)
            salaries = sum(
                om.ROLE_SALARIES[e.role_level] / 12.0 * e.fte for e in roster
            ) * float(rng.normal(1.0, 0.01))
            contractor_cost = sum(c.daily_rate for c in ctr_roster) * 21.0
            software = headcount_fte * om.SOFTWARE_PER_FTE * float(rng.normal(1.0, 0.03))
            travel = (
                headcount_fte
                * om.TRAVEL_PER_FTE
                * om.TRAVEL_SEASONALITY[month_date.month]
                * float(rng.normal(1.0, 0.10))
            )
            monthly_rows.append({
                "month": month_date.isoformat(),
                "month_index": month,
                "department": dept.name,
                "cost_centre": dept.cost_centre,
                "headcount_fte": round(headcount_fte, 2),
                "Salaries": round(salaries, 2),
                "Contractor Costs": round(contractor_cost, 2),
                "Software": round(software, 2),
                "Travel": round(travel, 2),
            })

    monthly = pd.DataFrame(monthly_rows)
    budget = _build_budget(monthly)
    return Truth(
        seed=seed,
        employees=employees,
        contractors=contractors,
        monthly=monthly,
        budget=budget,
        headcount_plan=_build_headcount_plan(monthly),
        params={
            "n_months": om.N_MONTHS,
            "structural_break_month": om.STRUCTURAL_BREAK_MONTH,
            "break_multipliers": {d.name: d.break_multiplier for d in om.DEPARTMENTS},
            "monthly_attrition": om.MONTHLY_ATTRITION,
        },
    )


def _build_headcount_plan(monthly: pd.DataFrame) -> pd.DataFrame:
    """The workforce plan: planned FTE per department per month.

    Built with the same stale-at-year-start logic as the budget (last three
    observed months grown at a planner's 3%/yr with hiring seasonality) and
    equally ignorant of the month-24 reorg — so post-break plan-vs-actual gaps
    are genuine signal for variance attribution and vacancy analysis.
    """
    rows: list[dict] = []
    for dept_name, gdf in monthly.groupby("department"):
        gdf = gdf.sort_values("month_index")
        for year_start in (0, 12, 24):
            history = gdf[gdf["month_index"] < year_start].tail(3)
            base = history if not history.empty else gdf.head(1)
            planned_base = float(base["headcount_fte"].mean())
            for m in range(year_start, min(year_start + 12, len(gdf))):
                cal = om.month_to_date(m).month
                rows.append({
                    "department": dept_name,
                    "month": om.month_to_date(m).isoformat(),
                    "month_index": m,
                    "planned_fte": round(
                        planned_base * (1.03) ** ((m - year_start) / 12.0)
                        * om.HIRING_SEASONALITY[cal], 2
                    ),
                })
    return pd.DataFrame(rows)


def _build_budget(monthly: pd.DataFrame) -> pd.DataFrame:
    """Budgets set at each fiscal-year start, extrapolating the prior trajectory.

    Deliberately ignorant of the month-24 structural break — exactly how real
    budgets go stale — so post-break actuals diverge and Phase 5 has real
    variance to explain.
    """
    rows: list[dict] = []
    for dept_name, gdf in monthly.groupby("department"):
        gdf = gdf.sort_values("month_index")
        cc = gdf["cost_centre"].iloc[0]
        for year_start in (0, 12, 24):
            # Plan from the average of the last 3 observed months (or month-0
            # baseline for year 1), grown at a planner's assumed 3%/yr.
            history = gdf[gdf["month_index"] < year_start].tail(3)
            base = history if not history.empty else gdf.head(1)
            for cat in ("Salaries", "Contractor Costs", "Software", "Travel"):
                planned_base = float(base[cat].mean())
                for m in range(year_start, min(year_start + 12, len(gdf))):
                    growth = (1.03) ** ((m - year_start) / 12.0)
                    seasonal = 1.0
                    if cat == "Travel":
                        cal = om.month_to_date(m).month
                        seasonal = om.TRAVEL_SEASONALITY[cal]
                    rows.append({
                        "cost_centre": cc,
                        "department": dept_name,
                        "month": om.month_to_date(m).isoformat(),
                        "month_index": m,
                        "account_category": cat,
                        "budget_amount": round(planned_base * growth * seasonal, 2),
                    })
    return pd.DataFrame(rows)
