"""The 'true' organizational model that all three source systems are lossy views of.

Every parameter here is ground truth. Because we generate the data, we can later
report honestly how close the forecasting models got to the real underlying process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SIM_START = date(2023, 1, 1)   # month index 0
N_MONTHS = 36
STRUCTURAL_BREAK_MONTH = 24    # a "reorganization" in month 24 (2025-01)

# Annual salary midpoint per role level (CAD). Monthly cost = salary / 12 * FTE.
ROLE_SALARIES: dict[str, float] = {
    "L1": 58_000.0,
    "L2": 72_000.0,
    "L3": 92_000.0,
    "L4": 115_000.0,
    "M1": 132_000.0,
    "M2": 160_000.0,
}

# Probability of each role level for a new hire (sums to 1).
ROLE_MIX: dict[str, float] = {
    "L1": 0.28, "L2": 0.30, "L3": 0.22, "L4": 0.10, "M1": 0.07, "M2": 0.03,
}

MONTHLY_ATTRITION = 0.013  # ~1.3% of staff leave per month (~15%/yr — realistic)

# Hiring seasonality by calendar month: Q1 and September hiring spikes (spec §2.3).
HIRING_SEASONALITY: dict[int, float] = {
    1: 1.06, 2: 1.05, 3: 1.03, 4: 1.00, 5: 0.99, 6: 0.98,
    7: 0.97, 8: 0.98, 9: 1.05, 10: 1.01, 11: 0.99, 12: 0.97,
}

# Travel spend seasonality: fiscal year-end (Q4) spike, summer lull (spec §2.3).
TRAVEL_SEASONALITY: dict[int, float] = {
    1: 0.85, 2: 0.95, 3: 1.05, 4: 1.00, 5: 1.00, 6: 0.80,
    7: 0.70, 8: 0.75, 9: 1.10, 10: 1.20, 11: 1.35, 12: 1.25,
}


@dataclass(frozen=True)
class Department:
    name: str
    cost_centre: str
    base_headcount: int          # FTE at month 0
    annual_growth: float         # organic trend, e.g. 0.03 = 3 %/yr
    contractor_ratio: float      # contractors as a share of employee headcount
    contractor_day_rate: float   # mean daily rate (CAD)
    # Structural break: multiplier applied to target headcount from month 24 on.
    # The 2025-01 reorg shifts work from Operations into Technology + contractors.
    break_multiplier: float


DEPARTMENTS: tuple[Department, ...] = (
    Department("Operations",      "CC-100", 120, 0.03, 0.12, 640.0, 0.82),
    Department("Technology",      "CC-200",  60, 0.04, 0.30, 890.0, 1.28),
    Department("Finance",         "CC-300",  25, 0.02, 0.05, 750.0, 1.00),
    Department("Human Resources", "CC-400",  15, 0.02, 0.04, 610.0, 1.00),
    Department("Sales",           "CC-500",  45, 0.05, 0.08, 580.0, 1.05),
)

# Contractor day rates rise 15% post-break (reorg leans on external staff).
BREAK_CONTRACTOR_RATE_UPLIFT = 1.15

# Non-labour cost model (per FTE per month, before noise).
SOFTWARE_PER_FTE = 88.0
TRAVEL_PER_FTE = 145.0

ACCOUNT_CATEGORIES = ("Salaries", "Contractor Costs", "Software", "Travel")

# Messy department spellings each source emits (the reconciliation challenge).
DEPT_ALIASES: dict[str, list[str]] = {
    "Operations":      ["Operations", "OPS", "Operations ", "operations"],
    "Technology":      ["Technology", "Tech", "IT & Technology", "Technology "],
    "Finance":         ["Finance", "FIN", "Finance & Accounting"],
    "Human Resources": ["Human Resources", "HR", "Human Resources ", "People & Culture"],
    "Sales":           ["Sales", "SALES", "Sales "],
}


def month_to_date(month_index: int) -> date:
    """Month index (0-based from SIM_START) -> first-of-month date."""
    y, m = divmod(SIM_START.month - 1 + month_index, 12)
    return date(SIM_START.year + y, m + 1, 1)
