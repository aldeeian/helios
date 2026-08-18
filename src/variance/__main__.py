"""Entry point: `python -m src.variance` computes budget-vs-actual variance,
driver attribution, anomalies, and per-period summaries from the warehouse,
writing data/reports/variance_report.{csv,json}."""

from __future__ import annotations

import json

import pandas as pd

from .. import config
from . import analysis

REPORTS_DIR = config.DATA_DIR / "reports"


def main() -> None:
    df = analysis.load_spend_frame()
    var = analysis.compute_variance(df)
    var = analysis.detect_anomalies(var)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    var.to_csv(REPORTS_DIR / "variance_report.csv", index=False)

    months = sorted(var["month_start"].unique())
    latest = pd.Timestamp(months[-1])
    decomps = {
        str(pd.Timestamp(m).date()): analysis.decompose_salaries(var, pd.Timestamp(m)).as_dict()
        for m in months
    }
    report = {
        "n_line_items": int(len(var)),
        "n_material": int(var["material"].sum()),
        "n_anomalies": int(var["anomaly"].sum()),
        "salary_driver_decomposition_by_month": decomps,
        "top_material_variances_latest_month": (
            analysis.top_variances(var[var["month_start"] == latest], n=5)
            [["department_name", "account_category", "variance_amount", "variance_pct"]]
            .to_dict(orient="records")
        ),
    }
    (REPORTS_DIR / "variance_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(analysis.summarize_period(var, latest))
    print(f"\n{report['n_material']} material / {report['n_line_items']} line items, "
          f"{report['n_anomalies']} anomalies flagged.")
    all_reconcile = all(d["reconciles"] for d in decomps.values())
    print(f"Salary decomposition reconciles every month: {all_reconcile}")
    print(f"Reports -> {REPORTS_DIR}")


if __name__ == "__main__":
    main()
