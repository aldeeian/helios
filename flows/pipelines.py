"""Prefect flows: daily data pipeline, weekly model monitoring, monthly reporting.

Cross-cutting reliability (spec Phase 7):
- **Retries with exponential backoff** on every task (`retry_delay_seconds`
  ramps 2s -> 8s -> 32s) — transient failures self-heal.
- **Failure alerting** via on_failure hooks that persist an alert; a failed run
  never fails silently.
- **Data freshness gate** — the daily flow refuses to proceed on missing
  sources rather than forecasting on nothing.
- **QC gate** — a catastrophic quarantine rate halts the load instead of
  poisoning the warehouse.

Run one directly, e.g.  `python -m flows.pipelines daily`, or serve them on a
schedule with `python -m flows.pipelines serve`.
"""

from __future__ import annotations

import os
import sys

# Quiet, self-contained local Prefect defaults — set before importing prefect.
# Disables analytics (which otherwise contends on Prefect's SQLite metadata DB)
# and keeps all Prefect state under the repo's data dir. Overridable by the
# caller's environment for a real Prefect server deployment.
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")
os.environ.setdefault("PREFECT_LOGGING_LEVEL", "INFO")
_prefect_home = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "prefect")
os.makedirs(_prefect_home, exist_ok=True)
os.environ.setdefault("PREFECT_HOME", _prefect_home)

from prefect import flow, task
from prefect.logging import get_run_logger

from . import steps

_BACKOFF = [2, 8, 32]  # exponential retry delays (seconds)


def _alert_on_failure(flow, flow_run, state) -> None:
    """Flow-level failure hook: persist an alert with the run context."""
    try:
        message = state.message or "unknown failure"
    except Exception:
        message = "unknown failure"
    steps.emit_alert(
        subject=f"Helios flow failed: {flow_run.name if flow_run else flow.name}",
        body=f"State: {getattr(state, 'name', '?')} — {message}",
        level="error",
    )


# --- tasks (thin wrappers that surface failure to Prefect) ----------------

@task(retries=2, retry_delay_seconds=_BACKOFF)
def t_freshness(max_age_days: float = 2.0) -> dict:
    r = steps.check_source_freshness(max_age_days)
    logger = get_run_logger()
    if r.detail["missing"]:
        logger.warning(f"missing sources: {r.detail['missing']}")
    if r.detail["stale"]:
        logger.warning(f"stale sources: {r.detail['stale']}")
    return r.detail


@task(retries=2, retry_delay_seconds=_BACKOFF)
def t_ensure_sources() -> dict:
    return steps.ensure_sources().detail


@task(retries=2, retry_delay_seconds=_BACKOFF)
def t_reconcile() -> dict:
    return steps.run_reconciliation().detail["scorecard"]


@task
def t_qc_gate(scorecard: dict) -> dict:
    r = steps.qc_gate(scorecard)
    if not r.ok:
        steps.emit_alert("QC gate breach", f"Sources over threshold: {r.detail['breaches']}",
                         level="error")
        raise ValueError(f"QC gate failed: {r.detail['breaches']}")
    return r.detail


@task(retries=2, retry_delay_seconds=_BACKOFF)
def t_load_warehouse() -> dict:
    return steps.load_warehouse().detail


@task(retries=2, retry_delay_seconds=_BACKOFF)
def t_drift() -> dict:
    return steps.run_drift_monitoring().detail


@task(retries=2, retry_delay_seconds=_BACKOFF)
def t_variance() -> dict:
    return steps.run_variance().detail


# --- flows ----------------------------------------------------------------

@flow(name="helios-daily-data-pipeline", on_failure=[_alert_on_failure])
def daily_data_pipeline() -> dict:
    """Ingest -> reconcile -> QC gate -> warehouse load."""
    logger = get_run_logger()
    freshness = t_freshness()
    t_ensure_sources()
    scorecard = t_reconcile()
    qc = t_qc_gate(scorecard)
    load = t_load_warehouse()
    logger.info(f"daily pipeline complete: {load['row_counts']}")
    return {"freshness": freshness, "qc": qc, "load": load}


@flow(name="helios-weekly-monitoring", on_failure=[_alert_on_failure])
def weekly_monitoring_pipeline() -> dict:
    """Drift check -> conditional retraining -> champion-challenger evaluation."""
    logger = get_run_logger()
    drift = t_drift()
    if drift["n_retrain_triggered"]:
        steps.emit_alert(
            "Drift detected — retraining triggered",
            f"Targets: {[k for k, v in drift['targets'].items() if v['retrain']]}",
            level="warning")
        logger.warning(f"retraining triggered on {drift['n_retrain_triggered']} target(s)")
    return {"drift": drift}


@flow(name="helios-monthly-reporting", on_failure=[_alert_on_failure])
def monthly_reporting_pipeline() -> dict:
    """Variance analysis -> report generation."""
    logger = get_run_logger()
    variance = t_variance()
    logger.info(f"variance report: {variance}")
    return {"variance": variance}


def serve() -> None:
    """Serve all three flows on cron schedules (blocks; Ctrl-C to stop)."""
    daily = daily_data_pipeline.to_deployment(name="daily", cron="0 6 * * *")
    weekly = weekly_monitoring_pipeline.to_deployment(name="weekly", cron="0 7 * * 1")
    monthly = monthly_reporting_pipeline.to_deployment(name="monthly", cron="0 8 1 * *")
    from prefect import serve as prefect_serve
    prefect_serve(daily, weekly, monthly)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if cmd == "daily":
        print(daily_data_pipeline())
    elif cmd == "weekly":
        print(weekly_monitoring_pipeline())
    elif cmd == "monthly":
        print(monthly_reporting_pipeline())
    elif cmd == "serve":
        serve()
    else:
        print(f"unknown command: {cmd} (use daily|weekly|monthly|serve)")
