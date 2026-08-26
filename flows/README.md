# Orchestration (Phase 7)

Prefect 3 flows that run the Helios pipeline unattended, with retries, failure
alerting, freshness gating, and idempotent steps.

## Install

```
pip install -e ".[orchestration]"
```

Prefect is an **optional** extra — the pipeline steps in `flows/steps.py` are
plain functions with no Prefect dependency, so the orchestration logic is fully
testable (and CI-runnable) without a workflow engine.

## Flows

| Flow | Schedule | Does |
|---|---|---|
| `daily_data_pipeline` | `0 6 * * *` | freshness gate → ingest → reconcile → **QC gate** → warehouse load |
| `weekly_monitoring_pipeline` | `0 7 * * 1` | drift check → conditional retraining → champion-challenger, alert on drift |
| `monthly_reporting_pipeline` | `0 8 1 * *` | variance analysis → report generation |

## Run

```
python -m flows.pipelines daily      # one run of the daily flow
python -m flows.pipelines weekly
python -m flows.pipelines monthly
python -m flows.pipelines serve      # serve all three on their cron schedules
```

## Reliability features

- **Retries with exponential backoff** — every task retries twice with 2s → 8s
  → 32s delays; transient failures self-heal.
- **Failure alerting** — `on_failure` hooks persist an alert to `data/alerts/`
  (a single choke point, swap for email/Slack/webhook without touching a flow).
- **Freshness gate** — missing sources are flagged before anything forecasts on
  them; the pipeline refuses to run on nothing.
- **QC gate** — a catastrophic quarantine rate (>50%, well above the ~20%
  known-design CC-500 gap) halts the load rather than poisoning the warehouse.
- **Idempotency** — every step is safe to re-run: deterministic source
  regeneration, pure reconciliation, idempotent warehouse upserts.

Prefect state is kept local and analytics disabled by default (see the env
setup at the top of `pipelines.py`); point `PREFECT_API_URL` at a real Prefect
server to run against shared infrastructure.
