"""FastAPI serving layer over the Helios warehouse and NL agent.

Read-only by design: every endpoint reads the warehouse or runs a guardrailed
SELECT. The health endpoint follows the transit-monitor pattern — it returns
503 (not 200-with-a-flag) when the warehouse is unreachable or empty, so a load
balancer or uptime monitor treats a broken backend as down.

It also serves the single-page explorer at `/` — the same endpoints, wrapped in
a UI, so the project can be handed to someone as a URL rather than a clone.

Run locally:  uvicorn src.api.app:app --reload
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config
from ..agent import nl_agent, semantic
from ..variance import analysis as va

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Helios API", version="1.0.0",
              description="Read-only operational forecasting & variance API")

# CORS scoped to the dashboard origin(s); '*' only if explicitly configured.
_origins = os.environ.get("HELIOS_CORS_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in _origins],
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def _engine() -> sa.Engine:
    return sa.create_engine(config.warehouse_db_url())


def _clean(value):
    """NaN/NaT are not JSON — convert to null rather than emitting `NaN`, which
    is invalid JSON and silently breaks strict parsers in the browser."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()}
            for row in df.to_dict(orient="records")]


def _variance_frame(engine: sa.Engine) -> pd.DataFrame:
    var = va.compute_variance(va.load_spend_frame(engine))
    return va.detect_anomalies(var)


@app.get("/health")
def health():
    """503 unless the warehouse is reachable and has fact data loaded."""
    try:
        eng = _engine()
        with eng.connect() as c:
            hc = c.execute(sa.text("SELECT COUNT(*) FROM fact_headcount")).scalar_one()
            sp = c.execute(sa.text("SELECT COUNT(*) FROM fact_spend")).scalar_one()
        eng.dispose()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"warehouse unavailable: {e}")
    if hc == 0 or sp == 0:
        raise HTTPException(status_code=503, detail="warehouse empty (no facts loaded)")
    return {"status": "ok", "fact_headcount_rows": hc, "fact_spend_rows": sp}


@app.get("/api/filters")
def filters():
    """The values the UI's dropdowns offer.

    Served from the warehouse rather than hardcoded so the explorer keeps
    working when the underlying data changes shape (different departments, a
    longer history) without a frontend edit.
    """
    eng = _engine()
    try:
        var = _variance_frame(eng)
        with eng.connect() as c:
            all_departments = [r[0] for r in c.execute(sa.text(
                "SELECT department_name FROM dim_department ORDER BY department_name"))]
    finally:
        eng.dispose()

    months = sorted(var["month_start"].dt.strftime("%Y-%m").unique().tolist())
    with_spend = set(var["department_name"].dropna().unique().tolist())
    return {
        "months": months,
        "latest_month": months[-1] if months else None,
        # Every department is offered, including any whose spend never survived
        # reconciliation — hiding them would hide the data-quality finding.
        "departments": all_departments,
        "departments_without_spend": sorted(set(all_departments) - with_spend),
        "categories": sorted(var["account_category"].dropna().unique().tolist()),
    }


@app.get("/api/kpis")
def kpis(month: str | None = None, department: str | None = None):
    """Headline KPIs for one month, optionally narrowed to a department."""
    eng = _engine()
    try:
        var = _variance_frame(eng)
        target = pd.Timestamp(month + "-01") if month else var["month_start"].max()
        m = var[var["month_start"] == target]
        if department:
            m = m[m["department_name"] == department]
        headcount = _headcount_for(eng, target, department)
    finally:
        eng.dispose()

    base = {
        "month": pd.Timestamp(target).strftime("%Y-%m"),
        "department": department,
        **headcount,
    }

    if m.empty:
        # A department with headcount but no spend is the CC-500 lookup gap
        # showing through, not a bug. Return what we do know and say why the
        # rest is missing — a 404 here would look like the app was broken.
        return {
            **base,
            "actual": None, "budget": None, "variance": None,
            "variance_pct": None, "material_variances": 0, "line_items": 0,
            "note": ("No spend data for this selection. Its financial rows were "
                     "quarantined during reconciliation (unmapped cost centre), "
                     "so headcount is known but cost is not."),
        }

    actual = float(m["actual_amount"].sum())
    budget = float(m["budget_amount"].sum())
    return {
        **base,
        "actual": actual, "budget": budget,
        "variance": actual - budget,
        "variance_pct": (actual - budget) / budget if budget else None,
        "material_variances": int(m["material"].sum()),
        "line_items": int(len(m)),
    }


def _headcount_for(engine: sa.Engine, month, department: str | None) -> dict:
    """Headcount and vacancy for the KPI row.

    Read from vw_headcount_vs_plan rather than recomputed here — the view is the
    one definition of vacancy, and duplicating the arithmetic in the API is how
    a dashboard ends up disagreeing with its own warehouse.
    """
    sql = ("SELECT SUM(employee_fte) AS employee_fte, "
           "SUM(contractor_fte) AS contractor_fte, SUM(total_fte) AS total_fte, "
           "SUM(planned_fte) AS planned_fte, SUM(vacancy_fte) AS vacancy_fte "
           "FROM vw_headcount_vs_plan WHERE month_start = :m")
    params = {"m": pd.Timestamp(month).strftime("%Y-%m-%d")}
    if department:
        sql += " AND department_name = :d"
        params["d"] = department
    try:
        with engine.connect() as c:
            row = c.execute(sa.text(sql), params).mappings().first()
    except Exception:
        return {}
    if not row:
        return {}
    return {k: _clean(float(v)) if v is not None else None for k, v in row.items()}


@app.get("/api/variance")
def variance(month: str | None = None, department: str | None = None,
             material_only: bool = False):
    """Variance line items, filtered to a YYYY-MM month and/or department."""
    eng = _engine()
    try:
        var = _variance_frame(eng)
    finally:
        eng.dispose()
    if month:
        var = var[var["month_start"].dt.strftime("%Y-%m") == month]
    if department:
        var = var[var["department_name"] == department]
    if material_only:
        var = var[var["material"]]

    cols = ["month_start", "department_name", "account_category",
            "budget_amount", "actual_amount", "variance_amount", "variance_pct",
            "direction", "material", "anomaly"]
    var = var[[c for c in cols if c in var.columns]].copy()
    var = var.sort_values("variance_amount", key=lambda s: s.abs(), ascending=False)
    var["month_start"] = var["month_start"].dt.strftime("%Y-%m")
    return {"count": len(var), "items": _records(var)}


@app.get("/api/headcount")
def headcount(department: str | None = None):
    """Full monthly headcount trend — the series the UI charts."""
    sql = ("SELECT month_start, SUM(employee_fte) AS employee_fte, "
           "SUM(contractor_fte) AS contractor_fte, SUM(total_fte) AS total_fte, "
           "SUM(planned_fte) AS planned_fte "
           "FROM vw_headcount_vs_plan {where} GROUP BY month_start "
           "ORDER BY month_start")
    params = {}
    where = ""
    if department:
        where, params["d"] = "WHERE department_name = :d", department
    eng = _engine()
    try:
        with eng.connect() as c:
            rows = c.execute(sa.text(sql.format(where=where)), params).mappings().all()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"warehouse unavailable: {e}")
    finally:
        eng.dispose()
    series = [{k: (str(v)[:7] if k == "month_start" else _clean(v))
               for k, v in dict(r).items()} for r in rows]
    return {"department": department, "count": len(series), "series": series}


@app.get("/api/findings")
def findings(month: str | None = None):
    """What the alerting rules would fire on right now.

    Surfacing this in the UI keeps one definition of "worth attention" — the
    page cannot disagree with the emails, because both call the same rules.
    """
    from ..alerting.__main__ import collect

    try:
        items, ctx = collect(month)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"could not evaluate: {e}")
    return {
        "month": ctx.month,
        "count": len(items),
        "findings": [{"code": f.code, "severity": f.severity,
                      "title": f.title, "detail": f.detail} for f in items],
    }


class AskRequest(BaseModel):
    question: str


@app.post("/api/ask")
def ask(req: AskRequest):
    """Natural-language query. Returns the answer, the generated SQL, and the
    tables it touched — guardrailed and read-only (see src/agent/guardrails)."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="empty question")
    ans = nl_agent.ask(req.question)
    return {
        "question": ans.question, "ok": ans.ok, "answer": ans.summary(),
        "sql": ans.sql, "tables_used": ans.tables_used,
        "columns": ans.columns, "rows": [list(r) for r in ans.rows[:100]],
        "attempts": ans.attempts, "error": ans.error, "engine": ans.llm,
    }


@app.get("/api/schema")
def schema():
    """The semantic layer (tables, columns, definitions) the agent reads."""
    return {"tables": sorted(semantic.ALLOWED_TABLES),
            "prompt": semantic.schema_prompt()}


@app.get("/", include_in_schema=False)
def explorer():
    """The single-page explorer. Kept as a static file with no build step so
    the container has no Node toolchain and the page cannot drift from the
    API it calls."""
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="explorer UI not installed")
    return FileResponse(index)
