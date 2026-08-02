"""Star schema for the Helios warehouse (SQLAlchemy Core — portable DDL).

Facts
-----
fact_headcount  grain: department x month   (employee + contractor FTE)
fact_spend      grain: cost_centre x month x account_category

Dimensions
----------
dim_date, dim_department, dim_cost_centre, dim_role_level,
dim_employee — **Type 2 slowly-changing**: every attribute change closes the
current row (valid_to, is_current=0) and opens a new one, so the org chart can
be reconstructed as of any date.

Headcount is a point-in-time (semi-additive) measure: it sums across
departments within one month but never across months. The BI layer must use
LASTDATE/CLOSINGBALANCEMONTH-style handling; documented here because the grain
decision is what makes that possible.

The same metadata produces DDL for SQLite (default, zero-setup) and
PostgreSQL (set HELIOS_WAREHOUSE_URL) — the load and view code is identical.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

dim_date = sa.Table(
    "dim_date", metadata,
    sa.Column("date_key", sa.Integer, primary_key=True, autoincrement=False),  # YYYYMM
    sa.Column("month_start", sa.Date, nullable=False, unique=True),
    sa.Column("year", sa.Integer, nullable=False),
    sa.Column("month", sa.Integer, nullable=False),
    sa.Column("quarter", sa.Integer, nullable=False),
)

dim_department = sa.Table(
    "dim_department", metadata,
    sa.Column("department_key", sa.Integer, primary_key=True),
    sa.Column("department_name", sa.String(64), nullable=False, unique=True),
)

dim_cost_centre = sa.Table(
    "dim_cost_centre", metadata,
    sa.Column("cost_centre_key", sa.Integer, primary_key=True),
    sa.Column("cost_centre_code", sa.String(16), nullable=False, unique=True),
    sa.Column("department_key", sa.Integer,
              sa.ForeignKey("dim_department.department_key"), nullable=False),
)

dim_role_level = sa.Table(
    "dim_role_level", metadata,
    sa.Column("role_level_key", sa.Integer, primary_key=True),
    sa.Column("role_level", sa.String(8), nullable=False, unique=True),
)

dim_employee = sa.Table(
    "dim_employee", metadata,
    sa.Column("employee_key", sa.Integer, primary_key=True),     # surrogate
    sa.Column("employee_id", sa.String(16), nullable=False),     # natural
    sa.Column("full_name", sa.String(128), nullable=False),
    sa.Column("department_key", sa.Integer,
              sa.ForeignKey("dim_department.department_key"), nullable=False),
    sa.Column("role_level_key", sa.Integer,
              sa.ForeignKey("dim_role_level.role_level_key"), nullable=False),
    sa.Column("fte", sa.Float, nullable=False),
    sa.Column("hire_date", sa.Date, nullable=False),
    sa.Column("termination_date", sa.Date, nullable=True),
    # SCD2 bookkeeping
    sa.Column("valid_from", sa.Date, nullable=False),
    sa.Column("valid_to", sa.Date, nullable=True),               # NULL = open
    sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.text("1")),
    sa.UniqueConstraint("employee_id", "valid_from", name="uq_employee_version"),
    sa.Index("ix_employee_current", "employee_id", "is_current"),
)

fact_headcount = sa.Table(
    "fact_headcount", metadata,
    sa.Column("date_key", sa.Integer, sa.ForeignKey("dim_date.date_key"),
              primary_key=True),
    sa.Column("department_key", sa.Integer,
              sa.ForeignKey("dim_department.department_key"), primary_key=True),
    sa.Column("employee_fte", sa.Float, nullable=False),
    sa.Column("contractor_fte", sa.Float, nullable=False),
    sa.Column("total_fte", sa.Float, nullable=False),
    sa.Column("planned_fte", sa.Float, nullable=True),  # from the workforce plan
)

fact_spend = sa.Table(
    "fact_spend", metadata,
    sa.Column("date_key", sa.Integer, sa.ForeignKey("dim_date.date_key"),
              primary_key=True),
    sa.Column("cost_centre_key", sa.Integer,
              sa.ForeignKey("dim_cost_centre.cost_centre_key"), primary_key=True),
    sa.Column("account_category", sa.String(32), primary_key=True),
    sa.Column("budget_amount", sa.Float, nullable=True),
    sa.Column("actual_amount", sa.Float, nullable=True),
)

# Analytical views the BI and forecasting layers consume. Plain SQL kept to the
# dialect-neutral subset (works on SQLite and PostgreSQL).
# Vacancy became honestly derivable once the workforce plan source was added
# (plan minus filled = unfilled positions); before that it was deliberately
# omitted rather than fabricated from budget dollars.
VIEWS: dict[str, str] = {
    "vw_budget_vs_actual": """
        SELECT d.month_start,
               dep.department_name,
               cc.cost_centre_code,
               f.account_category,
               f.budget_amount,
               f.actual_amount,
               f.actual_amount - f.budget_amount              AS variance_amount,
               CASE WHEN f.budget_amount IS NULL OR f.budget_amount = 0 THEN NULL
                    ELSE (f.actual_amount - f.budget_amount) / f.budget_amount
               END                                            AS variance_pct
        FROM fact_spend f
        JOIN dim_date d          ON d.date_key = f.date_key
        JOIN dim_cost_centre cc  ON cc.cost_centre_key = f.cost_centre_key
        JOIN dim_department dep  ON dep.department_key = cc.department_key
    """,
    "vw_headcount_trend": """
        SELECT d.month_start,
               dep.department_name,
               f.employee_fte,
               f.contractor_fte,
               f.total_fte
        FROM fact_headcount f
        JOIN dim_date d         ON d.date_key = f.date_key
        JOIN dim_department dep ON dep.department_key = f.department_key
    """,
    "vw_headcount_vs_plan": """
        SELECT d.month_start,
               dep.department_name,
               f.employee_fte,
               f.contractor_fte,
               f.total_fte,
               f.planned_fte,
               -- Vacancy = planned permanent positions not filled by employees.
               -- Contractors are excluded: they backfill work but do not fill a
               -- budgeted permanent seat. A negative value = staffed above plan.
               f.planned_fte - f.employee_fte                 AS vacancy_fte,
               CASE WHEN f.planned_fte IS NULL OR f.planned_fte = 0 THEN NULL
                    ELSE (f.planned_fte - f.employee_fte) / f.planned_fte
               END                                            AS vacancy_rate
        FROM fact_headcount f
        JOIN dim_date d         ON d.date_key = f.date_key
        JOIN dim_department dep ON dep.department_key = f.department_key
    """,
    "vw_contractor_mix": """
        SELECT d.month_start,
               dep.department_name,
               f.contractor_fte,
               f.total_fte,
               CASE WHEN f.total_fte = 0 THEN NULL
                    ELSE f.contractor_fte / f.total_fte
               END AS contractor_share
        FROM fact_headcount f
        JOIN dim_date d         ON d.date_key = f.date_key
        JOIN dim_department dep ON dep.department_key = f.department_key
    """,
}


def create_all(engine: sa.Engine) -> None:
    """Create tables and (re)create views. Idempotent."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        for name, body in VIEWS.items():
            conn.execute(sa.text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(sa.text(f"CREATE VIEW {name} AS {body}"))
