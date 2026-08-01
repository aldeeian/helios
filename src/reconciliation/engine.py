"""The reconciliation engine: three messy sources -> one trusted view + quarantine.

Core invariant (the professionally credible part, spec Phase 2):
    rows_in == rows_kept + rows_quarantined        (per source, asserted)

No record is ever silently dropped. Every exclusion lands in the quarantine
frame with a machine-readable reason code and enough context to investigate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import normalize as nz

# Reason codes — the quarantine vocabulary. Documented in docs/data_dictionary.md.
DUPLICATE_ROW = "DUPLICATE_ROW"
UNKNOWN_DEPARTMENT = "UNKNOWN_DEPARTMENT"
INVALID_FTE = "INVALID_FTE"
INVALID_DATE = "INVALID_DATE"
UNMAPPED_COST_CENTRE = "UNMAPPED_COST_CENTRE"
UNPARSEABLE_AMOUNT = "UNPARSEABLE_AMOUNT"
CONTRACTOR_ALSO_EMPLOYEE = "CONTRACTOR_ALSO_EMPLOYEE"


@dataclass
class ReconciliationResult:
    employees: pd.DataFrame        # clean, deduplicated, canonical HR roster
    contractors: pd.DataFrame      # clean roster, double-counts removed
    financials: pd.DataFrame       # mapped to departments, numeric amounts
    quarantine: pd.DataFrame       # every excluded row: source, reason, payload
    plan: pd.DataFrame | None = None  # validated workforce plan (optional source)
    scorecard: dict = field(default_factory=dict)


def _quarantine_rows(df: pd.DataFrame, mask: pd.Series, source: str, reason: str,
                     bucket: list[pd.DataFrame]) -> pd.DataFrame:
    """Move rows matching mask into the quarantine bucket; return the survivors."""
    bad = df[mask].copy()
    if not bad.empty:
        q = pd.DataFrame({
            "source": source,
            "reason": reason,
            "record": bad.astype(str).apply(lambda r: r.to_json(), axis=1),
        })
        bucket.append(q)
    return df[~mask].copy()


def reconcile_hr(hr_raw: pd.DataFrame, bucket: list[pd.DataFrame]) -> pd.DataFrame:
    df = hr_raw.copy()
    df["full_name"] = df["full_name"].astype(str).str.strip()

    dup_mask = df.duplicated(keep="first")  # exact duplicate export rows
    df = _quarantine_rows(df, dup_mask, "hr", DUPLICATE_ROW, bucket)

    df["department"] = df["department"].map(nz.normalize_department)
    df = _quarantine_rows(df, df["department"].isna(), "hr", UNKNOWN_DEPARTMENT, bucket)

    df["fte"] = df["fte_percent"].map(nz.normalize_fte)
    df = _quarantine_rows(df, df["fte"].isna(), "hr", INVALID_FTE, bucket)

    df["hire_date"] = df["hire_date"].map(nz.parse_date_multi)
    df["termination_date"] = df["termination_date"].map(nz.parse_date_multi)
    df = _quarantine_rows(df, df["hire_date"].isna(), "hr", INVALID_DATE, bucket)

    df["name_key"] = df["full_name"].map(nz.normalize_person_name)
    return df.drop(columns=["fte_percent"]).reset_index(drop=True)


def reconcile_contractors(ctr_raw: pd.DataFrame, employees: pd.DataFrame,
                          cc_map: pd.DataFrame,
                          bucket: list[pd.DataFrame]) -> pd.DataFrame:
    df = ctr_raw.copy()
    df["start_date"] = df["start_date"].map(nz.parse_date_multi)
    df["end_date"] = df["end_date"].map(nz.parse_date_multi)
    df = _quarantine_rows(df, df["start_date"].isna(), "contractor", INVALID_DATE, bucket)

    # department_code -> department via the same (incomplete) lookup finance
    # uses. Unmapped codes (CC-500 by design) are quarantined and surfaced on
    # the scorecard — the real-world fix is completing the lookup, not the code
    # guessing at ground truth it should not have access to.
    mapping = dict(zip(cc_map["cost_centre_code"], cc_map["department"]))
    df["department"] = df["department_code"].map(mapping)
    df = _quarantine_rows(df, df["department"].isna(), "contractor",
                          UNMAPPED_COST_CENTRE, bucket)

    # Double-count detection: same normalized name, same department, and the
    # contractor engagement overlaps the employment period -> the person is
    # already counted as an employee. Count once (keep employee, quarantine
    # contractor row) so combined headcount and spend are not inflated.
    df["name_key"] = df["contractor_name"].map(nz.normalize_person_name)
    emp_idx = employees.set_index(["name_key", "department"]).sort_index()

    def overlaps_employment(row) -> bool:
        key = (row["name_key"], row["department"])
        if key not in emp_idx.index:
            return False
        matches = emp_idx.loc[[key]]
        for _, emp in matches.iterrows():
            emp_end = emp["termination_date"] or pd.Timestamp.max.date()
            ctr_end = row["end_date"] or pd.Timestamp.max.date()
            if row["start_date"] <= emp_end and emp["hire_date"] <= ctr_end:
                return True
        return False

    double_mask = df.apply(overlaps_employment, axis=1)
    df = _quarantine_rows(df, double_mask, "contractor", CONTRACTOR_ALSO_EMPLOYEE, bucket)
    return df.reset_index(drop=True)


def reconcile_financials(fin_raw: pd.DataFrame, cc_map: pd.DataFrame,
                         bucket: list[pd.DataFrame]) -> pd.DataFrame:
    df = fin_raw.copy()
    mapping = dict(zip(cc_map["cost_centre_code"], cc_map["department"]))
    df["department"] = df["cost_centre_code"].map(mapping)
    # Unmapped codes are quarantined, NOT dropped — the scorecard surfaces the
    # gap so someone fixes the lookup (spec: "handle unmapped codes explicitly").
    df = _quarantine_rows(df, df["department"].isna(), "financials",
                          UNMAPPED_COST_CENTRE, bucket)

    df["budget_amount"] = df["budget_amount"].map(nz.parse_amount)
    df["actual_amount"] = df["actual_amount"].map(nz.parse_amount)
    bad_amount = df["budget_amount"].isna() & df["actual_amount"].isna()
    df = _quarantine_rows(df, bad_amount, "financials", UNPARSEABLE_AMOUNT, bucket)
    return df.reset_index(drop=True)


def find_period_gaps(financials: pd.DataFrame) -> dict[str, list[str]]:
    """Missing months per cost centre — flagged, since a gap is a finding, not a row."""
    gaps: dict[str, list[str]] = {}
    all_periods = sorted(financials["period"].unique())
    full_range = pd.period_range(all_periods[0], all_periods[-1], freq="M").astype(str)
    for cc, g in financials.groupby("cost_centre_code"):
        have = set(g["period"])
        missing = [p for p in full_range if p not in have]
        if missing:
            gaps[str(cc)] = missing
    return gaps


def reconcile_plan(plan_raw: pd.DataFrame, bucket: list[pd.DataFrame]) -> pd.DataFrame:
    """The planning template is analyst-maintained and ships clean, but is
    validated like any other source — typos in a hand-edited file are exactly
    the defect this pipeline exists to catch."""
    df = plan_raw.copy()
    df["department"] = df["department"].map(nz.normalize_department)
    df = _quarantine_rows(df, df["department"].isna(), "plan", UNKNOWN_DEPARTMENT, bucket)
    bad_fte = ~pd.to_numeric(df["planned_fte"], errors="coerce").ge(0)
    df = _quarantine_rows(df, bad_fte, "plan", INVALID_FTE, bucket)
    df["planned_fte"] = df["planned_fte"].astype(float)
    return df.reset_index(drop=True)


def run(hr_raw: pd.DataFrame, fin_raw: pd.DataFrame, ctr_raw: pd.DataFrame,
        cc_map: pd.DataFrame,
        plan_raw: pd.DataFrame | None = None) -> ReconciliationResult:
    bucket: list[pd.DataFrame] = []

    employees = reconcile_hr(hr_raw, bucket)
    contractors = reconcile_contractors(ctr_raw, employees, cc_map, bucket)
    financials = reconcile_financials(fin_raw, cc_map, bucket)
    plan = reconcile_plan(plan_raw, bucket) if plan_raw is not None else None

    quarantine = (
        pd.concat(bucket, ignore_index=True)
        if bucket else pd.DataFrame(columns=["source", "reason", "record"])
    )

    # The accounting identity: nothing vanished.
    totals_in = {"hr": len(hr_raw), "contractor": len(ctr_raw), "financials": len(fin_raw)}
    totals_kept = {"hr": len(employees), "contractor": len(contractors),
                   "financials": len(financials)}
    if plan is not None:
        totals_in["plan"] = len(plan_raw)
        totals_kept["plan"] = len(plan)
    q_by_source = quarantine.groupby("source").size().to_dict() if len(quarantine) else {}
    for source in totals_in:
        kept, quarantined = totals_kept[source], q_by_source.get(source, 0)
        assert totals_in[source] == kept + quarantined, (
            f"{source}: {totals_in[source]} in != {kept} kept + {quarantined} quarantined"
        )

    reasons = quarantine["reason"].value_counts().to_dict() if len(quarantine) else {}
    scorecard = {
        "rows_in": totals_in,
        "rows_kept": totals_kept,
        "rows_quarantined": q_by_source,
        "quarantine_reasons": reasons,
        "period_gaps": find_period_gaps(financials),
        "identity_holds": True,  # the assertions above would have raised otherwise
    }
    return ReconciliationResult(employees, contractors, financials, quarantine,
                                plan, scorecard)
