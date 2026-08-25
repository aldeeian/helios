"""Pipeline steps as plain, idempotent functions — the unit of orchestration.

Deliberately Prefect-free so the orchestration logic is testable in CI without
installing a workflow engine; `flows/pipelines.py` wraps these with Prefect
tasks, retries, and failure hooks. Every step is safe to re-run: sources are
regenerated deterministically, reconciliation is pure, and the warehouse load
is idempotent.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from src import config


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: dict


# --- data freshness -------------------------------------------------------

def check_source_freshness(max_age_days: float = 2.0) -> StepResult:
    """Flag stale or missing source systems before we forecast on them.

    Forecasting on stale inputs silently produces confident-but-wrong numbers;
    a freshness gate turns that into an explicit, visible condition.
    """
    sources = {
        "hr": config.HR_XLSX,
        "financials": config.FINANCIALS_CSV,
        "headcount_plan": config.HEADCOUNT_PLAN_CSV,
        "cost_centre_map": config.COST_CENTRE_MAP_CSV,
    }
    now = dt.datetime.now()
    status, missing, stale = {}, [], []
    for name, path in sources.items():
        p = Path(path)
        if not p.exists():
            missing.append(name)
            status[name] = {"exists": False}
            continue
        age_days = (now - dt.datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 86400
        is_stale = age_days > max_age_days
        if is_stale:
            stale.append(name)
        status[name] = {"exists": True, "age_days": round(age_days, 2), "stale": is_stale}
    return StepResult("check_source_freshness",
                      ok=not missing,
                      detail={"status": status, "missing": missing, "stale": stale})


# --- ingestion / reconciliation / QC gate --------------------------------

def ensure_sources(seed: int = 42) -> StepResult:
    """Ingestion stand-in: guarantee the source systems exist (regenerate the
    synthetic feeds if absent). In production this is where real extracts land."""
    from src.datagen.__main__ import main as gen_main
    import sys

    if not Path(config.HR_XLSX).exists():
        argv = sys.argv
        sys.argv = ["datagen", "--seed", str(seed)]
        try:
            gen_main()
        finally:
            sys.argv = argv
        regenerated = True
    else:
        regenerated = False
    return StepResult("ensure_sources", ok=True, detail={"regenerated": regenerated})


def run_reconciliation() -> StepResult:
    from src.ingestion import readers
    from src.reconciliation import engine

    result = engine.run(readers.read_hr(), readers.read_financials(),
                         readers.read_contractors(), readers.read_cost_centre_map(),
                         plan_raw=readers.read_headcount_plan())
    out = config.RECONCILED_DIR
    out.mkdir(parents=True, exist_ok=True)
    result.employees.to_csv(out / "employees.csv", index=False)
    result.contractors.to_csv(out / "contractors.csv", index=False)
    result.financials.to_csv(out / "financials.csv", index=False)
    if result.plan is not None:
        result.plan.to_csv(out / "plan.csv", index=False)
    result.quarantine.to_csv(out / "quarantine.csv", index=False)
    (out / "scorecard.json").write_text(json.dumps(result.scorecard, indent=2),
                                        encoding="utf-8")
    return StepResult("run_reconciliation", ok=True, detail={"scorecard": result.scorecard})


def qc_gate(scorecard: dict, max_quarantine_rate: float = 0.50) -> StepResult:
    """Block the load if any source's quarantine rate is catastrophic.

    Known-by-design quarantines (the CC-500 lookup gap ~= 20% of financials) sit
    well under the 50% bar; a source crossing it signals a broken feed, and the
    pipeline should halt rather than load garbage into the warehouse.
    """
    rates = {}
    breaches = []
    for src, n_in in scorecard["rows_in"].items():
        q = scorecard["rows_quarantined"].get(src, 0)
        rate = q / n_in if n_in else 0.0
        rates[src] = round(rate, 4)
        if rate > max_quarantine_rate:
            breaches.append(src)
    return StepResult("qc_gate", ok=not breaches,
                      detail={"quarantine_rates": rates, "breaches": breaches,
                              "threshold": max_quarantine_rate})


def load_warehouse() -> StepResult:
    from src.warehouse import load
    report = load.load()
    return StepResult("load_warehouse", ok=True, detail=report)


# --- monitoring / retraining / reporting ---------------------------------

def run_drift_monitoring() -> StepResult:
    from src.monitoring.__main__ import DEMO_TARGETS
    from src.forecasting import datasets
    from src.forecasting.models import sarima
    from src.monitoring import pipeline
    from src.datagen.org_model import STRUCTURAL_BREAK_MONTH

    targets = datasets.load_targets()
    triggered = {}
    for name in DEMO_TARGETS:
        result = pipeline.run_cycle(targets[name], name, sarima, "sarima",
                                    break_index=STRUCTURAL_BREAK_MONTH)
        triggered[name] = {
            "retrain": result.retrain.triggered,
            "reasons": result.retrain.reasons,
            "promoted": bool(result.promotion and result.promotion.promoted),
        }
    n = sum(v["retrain"] for v in triggered.values())
    return StepResult("run_drift_monitoring", ok=True,
                      detail={"targets": triggered, "n_retrain_triggered": n})


def run_variance() -> StepResult:
    from src.variance import analysis
    df = analysis.load_spend_frame()
    var = analysis.compute_variance(df)
    var = analysis.detect_anomalies(var)
    reports = config.DATA_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    var.to_csv(reports / "variance_report.csv", index=False)
    return StepResult("run_variance", ok=True,
                      detail={"n_material": int(var["material"].sum()),
                              "n_anomalies": int(var["anomaly"].sum())})


# --- alerting -------------------------------------------------------------

def emit_alert(subject: str, body: str, level: str = "warning") -> Path:
    """Persist an alert and fan it out to any configured channel.

    The single choke point for notifications. src.alerting always writes to
    data/alerts/ (gitignored) and additionally delivers to Slack/email when
    those are configured, so a flow failure is visible without anyone tailing
    logs. Delivery failures are swallowed by design — alerting must never be
    the reason a run fails, since it only runs when something is already wrong.
    """
    from src.alerting.channels import Alert, FileSender, send

    alert = Alert(subject=subject, body_text=body, level=level)
    results = send(alert)
    written = next((r.detail for r in results if r.channel == FileSender.name and r.ok),
                   None)
    if written:
        return Path(written)
    # Channel construction failed entirely; keep the durable write regardless.
    return Path(FileSender().send(alert).detail)
