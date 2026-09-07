# Runbook — operating Helios

## Services

| Service | What it is | Run |
|---|---|---|
| API | FastAPI read-only serving layer | `uvicorn src.api.app:app` (`:8080` in the container) |
| Warehouse | SQLite (dev) or PostgreSQL (prod) | `python -m src.warehouse` loads it |
| Flows | Prefect daily/weekly/monthly | `python -m flows.pipelines serve` |

## Health

`GET /health` returns **200** only when the warehouse is reachable and has fact
rows; **503** otherwise. Point an uptime monitor here — a 503 means the backend
is genuinely down (empty or unreachable warehouse), not merely slow. The
container's `HEALTHCHECK` and the Container Apps liveness/readiness probes use
the same endpoint.

## Local run

```
# API on the default SQLite warehouse (zero setup)
python -m src.datagen --seed 42 && python -m src.reconciliation && python -m src.warehouse
uvicorn src.api.app:app --reload

# Full stack (Postgres + API) via Docker
docker compose up --build
# then seed: HELIOS_WAREHOUSE_URL=postgresql+psycopg://helios:helios@localhost:5432/helios python -m src.warehouse
```

## Deploy to Azure

Infrastructure is declared in `infra/main.bicep` (Container Apps, PostgreSQL
Flexible Server, Blob storage, Key Vault, Container Registry). One-time:

```
az group create -n helios-rg -l canadacentral
az deployment group create -g helios-rg -f infra/main.bicep \
  -p namePrefix=helios pgAdminPassword=$PG_PASSWORD
```

Then set the GitHub repo secrets the deploy workflow needs (`AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_LOGIN_SERVER`,
`AZURE_RESOURCE_GROUP`, `AZURE_CONTAINERAPP_NAME`) — via OIDC federation, so no
cloud credential is stored. Every push to `main` then: runs the test +
data-quality gate, builds the image in ACR, and rolls a new Container Apps
revision. A failing gate blocks the deploy.

**Secrets:** the DB connection string lives only in Key Vault (written by Bicep)
and is injected into the app as a secret reference. Nothing sensitive is in the
repo — `.env` is gitignored and `.env.example` documents the shape.

## Common alerts and what they mean

| Symptom | Likely cause | Action |
|---|---|---|
| `/health` 503 | warehouse unreachable or empty | check DB connectivity; re-run `python -m src.warehouse` |
| Drift alert (weekly flow) | a monitored target's forecast degraded | expected after a real regime change; the challenger is auto-gated — review `data/reports/drift_report.json` |
| QC-gate failure (daily flow) | a source's quarantine rate crossed 50% | a source feed is likely broken — inspect `data/reconciled/quarantine.csv` |
| `/api/ask` returns "blocked by safety check" | the model produced non-SELECT SQL | expected guardrail behavior; not an error |

## Data regeneration

All of `data/` is reproducible and gitignored:
`python -m src.datagen --seed 42` → `src.reconciliation` → `src.warehouse`.
Re-running is idempotent — the warehouse load never duplicates.
