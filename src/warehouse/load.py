"""Warehouse load: reconciled CSVs -> star schema.

Idempotency contract (spec Phase 3): loading the same reconciled data twice
produces byte-identical warehouse state — dims upsert on natural keys, the
Type 2 employee merge is a no-op when nothing changed, and facts are
delete-then-insert per (month, key) inside one transaction.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

from .. import config
from . import schema


def _read_reconciled(
    reconciled_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    emp = pd.read_csv(reconciled_dir / "employees.csv",
                      parse_dates=["hire_date", "termination_date"])
    ctr = pd.read_csv(reconciled_dir / "contractors.csv",
                      parse_dates=["start_date", "end_date"])
    fin = pd.read_csv(reconciled_dir / "financials.csv")
    plan_path = reconciled_dir / "plan.csv"
    plan = pd.read_csv(plan_path) if plan_path.exists() else None
    return emp, ctr, fin, plan


def _date_key(month_start: dt.date) -> int:
    return month_start.year * 100 + month_start.month


def _month_range(fin: pd.DataFrame) -> list[dt.date]:
    periods = pd.period_range(fin["period"].min(), fin["period"].max(), freq="M")
    return [p.to_timestamp().date() for p in periods]


def _upsert_dims(conn: sa.Connection, emp: pd.DataFrame, fin: pd.DataFrame,
                 months: list[dt.date]) -> tuple[dict, dict, dict]:
    """Insert missing dimension members; return natural-key -> surrogate maps."""
    for m in months:
        exists = conn.execute(
            sa.select(schema.dim_date.c.date_key)
            .where(schema.dim_date.c.date_key == _date_key(m))
        ).first()
        if not exists:
            conn.execute(schema.dim_date.insert().values(
                date_key=_date_key(m), month_start=m, year=m.year,
                month=m.month, quarter=(m.month - 1) // 3 + 1,
            ))

    def upsert_simple(table: sa.Table, name_col: str, values: list[str],
                      key_col: str) -> dict[str, int]:
        existing = {
            r[0]: r[1] for r in conn.execute(
                sa.select(table.c[name_col], table.c[key_col])
            )
        }
        for v in sorted(set(values) - set(existing)):
            res = conn.execute(table.insert().values(**{name_col: v}))
            existing[v] = res.inserted_primary_key[0]
        return existing

    dept_map = upsert_simple(schema.dim_department, "department_name",
                             list(emp["department"]) + list(fin["department"]),
                             "department_key")
    role_map = upsert_simple(schema.dim_role_level, "role_level",
                             list(emp["role_level"]), "role_level_key")

    cc_existing = {
        r[0]: r[1] for r in conn.execute(
            sa.select(schema.dim_cost_centre.c.cost_centre_code,
                      schema.dim_cost_centre.c.cost_centre_key)
        )
    }
    cc_pairs = fin[["cost_centre_code", "department"]].drop_duplicates()
    for _, row in cc_pairs.iterrows():
        if row["cost_centre_code"] not in cc_existing:
            res = conn.execute(schema.dim_cost_centre.insert().values(
                cost_centre_code=row["cost_centre_code"],
                department_key=dept_map[row["department"]],
            ))
            cc_existing[row["cost_centre_code"]] = res.inserted_primary_key[0]
    return dept_map, role_map, cc_existing


def _merge_employees_scd2(conn: sa.Connection, emp: pd.DataFrame,
                          dept_map: dict, role_map: dict,
                          as_of: dt.date) -> dict[str, int]:
    """Type 2 merge. Attribute change -> close current row, open a new version.

    Returns counts for the load report: {"inserted": n, "versioned": n, "unchanged": n}.
    """
    current = {
        r.employee_id: r for r in conn.execute(
            sa.select(schema.dim_employee).where(schema.dim_employee.c.is_current == True)  # noqa: E712
        )
    }
    stats = {"inserted": 0, "versioned": 0, "unchanged": 0}
    for _, s in emp.iterrows():
        term = s["termination_date"].date() if pd.notna(s["termination_date"]) else None
        attrs = dict(
            full_name=s["full_name"],
            department_key=dept_map[s["department"]],
            role_level_key=role_map[s["role_level"]],
            fte=float(s["fte"]),
            hire_date=s["hire_date"].date(),
            termination_date=term,
        )
        cur = current.get(s["employee_id"])
        if cur is None:
            conn.execute(schema.dim_employee.insert().values(
                employee_id=s["employee_id"], valid_from=as_of,
                valid_to=None, is_current=True, **attrs,
            ))
            stats["inserted"] += 1
            continue
        unchanged = all(
            getattr(cur, k) == v for k, v in attrs.items()
        )
        if unchanged:
            stats["unchanged"] += 1
            continue
        # A same-day re-load with different attrs must correct in place rather
        # than violate uq(employee_id, valid_from) with a second version.
        if cur.valid_from == as_of:
            conn.execute(
                schema.dim_employee.update()
                .where(schema.dim_employee.c.employee_key == cur.employee_key)
                .values(**attrs)
            )
        else:
            conn.execute(
                schema.dim_employee.update()
                .where(schema.dim_employee.c.employee_key == cur.employee_key)
                .values(valid_to=as_of, is_current=False)
            )
            conn.execute(schema.dim_employee.insert().values(
                employee_id=s["employee_id"], valid_from=as_of,
                valid_to=None, is_current=True, **attrs,
            ))
        stats["versioned"] += 1
    return stats


def _active_fte(df: pd.DataFrame, start_col: str, end_col: str,
                month_start: dt.date, fte_col: str | None) -> pd.Series:
    """FTE active during a month, grouped by department.

    Active = started on/before month end AND (no end date or ended on/after
    month start). Contractors count as 1.0 FTE each (day-rate engagements are
    full-time by convention; documented in the data dictionary).
    """
    month_end = (pd.Timestamp(month_start) + pd.offsets.MonthEnd(0)).date()
    start = pd.to_datetime(df[start_col]).dt.date
    end = pd.to_datetime(df[end_col]).dt.date
    active = (start <= month_end) & (end.isna() | (end >= month_start))
    sub = df[active]
    weights = sub[fte_col] if fte_col else pd.Series(1.0, index=sub.index)
    return weights.groupby(sub["department"]).sum()


def _load_facts(conn: sa.Connection, emp: pd.DataFrame, ctr: pd.DataFrame,
                fin: pd.DataFrame, plan: pd.DataFrame | None,
                months: list[dt.date], dept_map: dict, cc_map: dict) -> None:
    keys = [_date_key(m) for m in months]
    conn.execute(schema.fact_headcount.delete()
                 .where(schema.fact_headcount.c.date_key.in_(keys)))
    conn.execute(schema.fact_spend.delete()
                 .where(schema.fact_spend.c.date_key.in_(keys)))

    plan_lookup: dict[tuple[str, str], float] = {}
    if plan is not None:
        plan_lookup = {
            (r["department"], str(r["month"])[:7]): float(r["planned_fte"])
            for _, r in plan.iterrows()
        }
    hc_rows = []
    for m in months:
        e = _active_fte(emp, "hire_date", "termination_date", m, "fte")
        c = _active_fte(ctr, "start_date", "end_date", m, None)
        for dept in sorted(set(e.index) | set(c.index)):
            e_fte = round(float(e.get(dept, 0.0)), 2)
            c_fte = round(float(c.get(dept, 0.0)), 2)
            hc_rows.append(dict(
                date_key=_date_key(m), department_key=dept_map[dept],
                employee_fte=e_fte, contractor_fte=c_fte,
                total_fte=round(e_fte + c_fte, 2),
                planned_fte=plan_lookup.get((dept, m.isoformat()[:7])),
            ))
    if hc_rows:
        conn.execute(schema.fact_headcount.insert(), hc_rows)

    spend_rows = [
        dict(
            date_key=int(str(r["period"]).replace("-", "")),
            cost_centre_key=cc_map[r["cost_centre_code"]],
            account_category=r["account_category"],
            budget_amount=None if pd.isna(r["budget_amount"]) else float(r["budget_amount"]),
            actual_amount=None if pd.isna(r["actual_amount"]) else float(r["actual_amount"]),
        )
        for _, r in fin.iterrows()
    ]
    if spend_rows:
        conn.execute(schema.fact_spend.insert(), spend_rows)


def load(reconciled_dir: Path | None = None, engine: sa.Engine | None = None,
         as_of: dt.date | None = None) -> dict:
    """Full load. Returns a report dict (row counts + SCD2 stats)."""
    reconciled_dir = reconciled_dir or config.RECONCILED_DIR
    own_engine = engine is None
    engine = engine or sa.create_engine(config.warehouse_db_url())
    as_of = as_of or dt.date.today()
    try:
        schema.create_all(engine)
        emp, ctr, fin, plan = _read_reconciled(reconciled_dir)
        months = _month_range(fin)
        with engine.begin() as conn:
            dept_map, role_map, cc_map = _upsert_dims(conn, emp, fin, months)
            scd = _merge_employees_scd2(conn, emp, dept_map, role_map, as_of)
            _load_facts(conn, emp, ctr, fin, plan, months, dept_map, cc_map)
        with engine.connect() as conn:
            counts = {
                t.name: conn.execute(
                    sa.select(sa.func.count()).select_from(t)
                ).scalar_one()
                for t in schema.metadata.tables.values()
            }
        return {"as_of": as_of.isoformat(), "scd2": scd, "row_counts": counts}
    finally:
        if own_engine:
            engine.dispose()
