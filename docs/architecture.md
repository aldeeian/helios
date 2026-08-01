# Architecture

## System (all phases)

```
 ┌── src/datagen ──────────────────────────────────────────────────────────┐
 │ one seeded "true" org model → four lossy, inconsistent source systems    │
 └───┬─────────────┬──────────────┬───────────────┬────────────────────────┘
     ▼             ▼              ▼               ▼
 hr_system.xlsx  financials.csv  contractor_    headcount_plan.csv
 (Excel+formulas)(+incomplete    roster (SQL)   (workforce plan)
                  lookup)
     │             │              │               │
     ▼             ▼              ▼               ▼
 ┌── src/ingestion (read-only readers) ────────────────────────────────────┐
 └───┬─────────────────────────────────────────────────────────────────────┘
     ▼
 ┌── src/reconciliation ───────────────────────────────────────────────────┐
 │ normalize → dedupe → entity-match → quarantine w/ reason codes           │
 │ invariant: rows_in == rows_kept + rows_quarantined  (+ QC scorecard)     │
 └───┬─────────────────────────────────────────────────────────────────────┘
     ▼
 ┌── src/warehouse (star schema; SQLite dev / PostgreSQL prod) ─────────────┐
 │ fact_headcount, fact_spend · dim_* · Type 2 dim_employee · idempotent    │
 │ views: budget_vs_actual, headcount_trend, contractor_mix, headcount_vs_plan│
 └───┬───────────────┬───────────────┬──────────────┬──────────────┬────────┘
     ▼               ▼               ▼              ▼              ▼
 src/forecasting  src/variance   src/monitoring  powerbi/      src/agent
 SARIMA/Prophet/  volume/mix/    PSI+KS drift,   DAX + 4-page  NL→validated
 XGBoost, rolling rate decomp,   champion-       dashboard     read-only SQL
 CV, MLflow       materiality    challenger      (live preview)(guardrails)
     │               │               │              │              │
     └───────────────┴───────┬───────┴──────────────┴──────────────┘
                             ▼
                    ┌── src/api (FastAPI, read-only) ──┐
                    │ /health (503 when unhealthy),    │
                    │ /api/kpis, /api/variance,        │
                    │ /api/ask (NL agent), /api/schema │
                    └───────────────┬──────────────────┘
                                    ▼
              Docker (non-root) → Azure Container Apps
              PostgreSQL Flexible Server · Blob · Key Vault · ACR  (infra/main.bicep)
              GitHub Actions: test+data-quality gate → ACR build → rolling deploy

 flows/ (Prefect): daily data pipeline · weekly monitoring · monthly reporting
 data/truth/ (ground truth) sits OUTSIDE this flow — evaluation only.
```

## Security posture

- **No secrets in the repo.** DB URLs come from the environment (gitignored
  `.env`); in Azure the connection string lives only in Key Vault and is
  injected into the app as a secret reference. CI authenticates to Azure via
  OIDC federation — no stored cloud credential.
- **No SQL string-building.** All warehouse access is SQLAlchemy with fixed
  statements. The NL agent is the only component that runs generated SQL, and it
  is gated by an AST-level guardrail (parse with sqlglot; single SELECT, schema
  allowlist, row limit, no DDL/DML/PRAGMA/ATTACH/file-functions) then executed
  read-only with a statement timeout.
- **Generated data is gitignored** and reproducible from the seed — even
  synthetic PII-shaped data never enters git history.
- **Ground-truth isolation** — reconciliation, forecasting, and the agent never
  read `data/truth/`; keeps evaluation honest.
- **API is read-only**; the container runs as a **non-root** user with a
  container healthcheck.

## Key design decisions

- Employee-level simulation (not aggregate sampling), so cross-system
  reconciliation is meaningful.
- Budgets/plans built ignorant of the month-24 break, so post-break variance and
  drift are genuine signal.
- Quarantine over clamping/dropping — defects surface as findings.
- Unique ground-truth names, so any cross-system collision is a deliberate
  double-count injection and entity matching is exactly testable.
- Distributional drift monitors abstain below a minimum sample size; residual-
  based monitors are primary on 36-point monthly series (honest for the data).
- Champion-challenger gate: a retrained model is promoted only if it beats the
  incumbent by a margin on a fresh holdout — you cannot auto-deploy a worse model.
