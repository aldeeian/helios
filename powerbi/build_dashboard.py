"""Generate a self-contained HTML dashboard from the live Helios warehouse.

This is a faithful, screenshot-able preview of the four-page Power BI design
(docs in dashboard_spec.md) rendered from the *same* warehouse and reports the
Power BI model reads. It is not a substitute for the .pbix — it exists so the
design and the real numbers are visible without opening Power BI Desktop, and
so the repo has a visual artifact. All figures come from the warehouse and
data/reports; nothing here is mocked.

    python -m powerbi.build_dashboard   ->   powerbi/dashboard.html
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

from src import config
from src.variance import analysis as va

OUT = Path(__file__).resolve().parent / "dashboard.html"


def _engine() -> sa.Engine:
    return sa.create_engine(config.warehouse_db_url())


def _q(engine, sql: str) -> pd.DataFrame:
    with engine.connect() as c:
        return pd.read_sql(sa.text(sql), c)


def collect() -> dict:
    engine = _engine()
    hc = _q(engine, "SELECT month_start, department_name, employee_fte, "
                    "contractor_fte, total_fte FROM vw_headcount_trend "
                    "ORDER BY month_start")
    vac = _q(engine, "SELECT month_start, department_name, employee_fte, "
                     "planned_fte, vacancy_fte, vacancy_rate FROM vw_headcount_vs_plan")
    hc["month_start"] = pd.to_datetime(hc["month_start"])
    vac["month_start"] = pd.to_datetime(vac["month_start"])

    # --- headcount total trend (semi-additive: sum across depts within month) ---
    total_by_month = hc.groupby("month_start")[["employee_fte", "contractor_fte", "total_fte"]].sum()
    months = [d.strftime("%Y-%m") for d in total_by_month.index]

    # --- forecast vs actual (SARIMA on total headcount, 6 months forward) ---
    from src.forecasting.datasets import load_targets
    from src.forecasting.models import sarima
    targets = load_targets(engine)
    tot = targets["headcount::TOTAL"].dropna()
    fc = sarima(tot, 6)
    fc_months = pd.date_range(tot.index[-1], periods=7, freq="MS")[1:]

    # --- variance ---
    var_df = va.compute_variance(va.load_spend_frame(engine))
    var_df = va.detect_anomalies(var_df)
    latest = var_df["month_start"].max()
    latest_var = var_df[var_df["month_start"] == latest]
    top_var = va.top_variances(latest_var, n=6, material_only=False)
    dec = va.decompose_salaries(var_df, pd.Timestamp(latest)).as_dict()

    # department budget vs actual (latest month, summed across categories)
    dept_ba = (latest_var.groupby("department_name")[["budget_amount", "actual_amount"]]
               .sum().reset_index())

    # spend trend total
    spend_month = (var_df.groupby("month_start")[["budget_amount", "actual_amount"]]
                   .sum())

    # --- KPIs (latest month) ---
    latest_hc = float(total_by_month.loc[latest, "total_fte"]) if latest in total_by_month.index \
        else float(total_by_month.iloc[-1]["total_fte"])
    kpis = {
        "actual": float(latest_var["actual_amount"].sum()),
        "budget": float(latest_var["budget_amount"].sum()),
        "variance": float(latest_var["actual_amount"].sum() - latest_var["budget_amount"].sum()),
        "headcount": latest_hc,
        "n_material": int(latest_var["material"].sum()),
        "month": pd.Timestamp(latest).strftime("%B %Y"),
    }
    kpis["variance_pct"] = kpis["variance"] / kpis["budget"] if kpis["budget"] else 0.0

    # --- model health: forecast accuracy + drift + data quality ---
    reports = config.DATA_DIR / "reports"
    accuracy = []
    bt_path = reports / "backtest_report.json"
    if bt_path.exists():
        bt = json.loads(bt_path.read_text())
        for target, champ in bt["champions"].items():
            accuracy.append({"target": target.replace("::", " · "),
                             "mape": champ["champion_mape"],
                             "model": champ["champion"],
                             "beat_baseline": champ["beat_baseline"]})
        accuracy.sort(key=lambda r: r["mape"])
    drift = []
    dr_path = reports / "drift_report.json"
    if dr_path.exists():
        dr = json.loads(dr_path.read_text())
        for target, cyc in dr["cycles"].items():
            drift.append({"target": target.replace("::", " · "),
                          "retrain": cyc["retrain"]["triggered"],
                          "reasons": cyc["retrain"]["reasons"],
                          "promoted": bool(cyc["promotion"] and cyc["promotion"]["promoted"]),
                          "improvement": (cyc["promotion"]["improvement"] if cyc["promotion"] else 0)})
    quarantine = []
    sc_path = config.RECONCILED_DIR / "scorecard.json"
    if sc_path.exists():
        sc = json.loads(sc_path.read_text())
        for reason, n in sorted(sc.get("quarantine_reasons", {}).items(),
                                key=lambda kv: -kv[1]):
            quarantine.append({"reason": reason.replace("_", " ").title(), "rows": n})

    engine.dispose()
    return {
        "generated_from": "Helios warehouse (live)",
        "months": months,
        "headcount_total": {
            "employee": total_by_month["employee_fte"].round(1).tolist(),
            "contractor": total_by_month["contractor_fte"].round(1).tolist(),
            "total": total_by_month["total_fte"].round(1).tolist(),
        },
        "forecast": {
            "hist_months": months,
            "hist": tot.round(1).tolist(),
            "fc_months": [d.strftime("%Y-%m") for d in fc_months],
            "fc_mean": [round(float(x), 1) for x in fc.mean],
            "fc_lo": [round(float(x), 1) for x in fc.lo80],
            "fc_hi": [round(float(x), 1) for x in fc.hi80],
        },
        "kpis": kpis,
        "top_variances": [
            {"label": f"{r['department_name']} · {r['account_category']}",
             "variance": float(r["variance_amount"]),
             "pct": float(r["variance_pct"]) if pd.notna(r["variance_pct"]) else 0.0}
            for _, r in top_var.iterrows()
        ],
        "dept_budget_actual": [
            {"dept": r["department_name"], "budget": float(r["budget_amount"]),
             "actual": float(r["actual_amount"])}
            for _, r in dept_ba.iterrows()
        ],
        "decomposition": dec,
        "spend_trend": {
            "months": [d.strftime("%Y-%m") for d in spend_month.index],
            "budget": spend_month["budget_amount"].round(0).tolist(),
            "actual": spend_month["actual_amount"].round(0).tolist(),
        },
        "vacancy": [
            {"dept": d, "rate": float(g[g["month_start"] == latest]["vacancy_rate"].mean())}
            for d, g in vac.groupby("department_name")
        ],
        "contractor_mix": [
            {"dept": d,
             "share": float(g[g["month_start"] == latest]["contractor_fte"].sum()
                            / max(g[g["month_start"] == latest]["employee_fte"].sum()
                                  + g[g["month_start"] == latest]["contractor_fte"].sum(), 1e-9))}
            for d, g in hc.groupby("department_name")
        ],
        "accuracy": accuracy,
        "drift": drift,
        "quarantine": quarantine,
        "structural_break": "2025-01",
    }


def _json_default(o):
    """Coerce numpy scalars (np.bool_/int64/float64) to native Python types."""
    import numpy as np
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"not serializable: {type(o)}")


def main() -> None:
    data = collect()
    template = (Path(__file__).resolve().parent / "_dashboard_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/", json.dumps(data, default=_json_default))
    OUT.write_text(html, encoding="utf-8")
    print(f"Dashboard written -> {OUT}  ({OUT.stat().st_size // 1024} KB)")
    print(f"Latest month: {data['kpis']['month']}  |  "
          f"variance {data['kpis']['variance']:+,.0f} "
          f"({data['kpis']['variance_pct']:+.1%})  |  "
          f"headcount {data['kpis']['headcount']:.0f} FTE")


if __name__ == "__main__":
    main()
