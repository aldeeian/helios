"""The semantic layer the NL agent reads before writing SQL.

The agent must never guess at schema. This module hands it the exact tables,
columns, business definitions, and a few worked examples — grounding every
query in the real warehouse contract (spec Phase 9 step 1). It is generated
from the SQLAlchemy schema plus curated descriptions, so it can never drift
from the actual tables.
"""

from __future__ import annotations

from ..warehouse import schema

# Business definitions per table/column — the part an LLM cannot infer from
# names alone. Kept beside the schema so a column rename surfaces here too.
TABLE_DOCS: dict[str, str] = {
    "dim_date": "One row per month. date_key is YYYYMM (e.g. 202501). month_start is the first day of the month.",
    "dim_department": "The five canonical departments: Operations, Technology, Finance, Human Resources, Sales.",
    "dim_cost_centre": "Cost centres (CC-100..CC-500) mapped to their department.",
    "dim_role_level": "Employee role levels: L1-L4 (individual contributor), M1-M2 (management).",
    "dim_employee": "Type 2 slowly-changing employee dimension. Filter is_current=1 for the present roster; "
                    "use valid_from/valid_to to reconstruct history. fte is 0-1.",
    "fact_headcount": "Grain: one row per department per month. employee_fte + contractor_fte = total_fte. "
                      "planned_fte is the workforce plan. HEADCOUNT IS POINT-IN-TIME: never SUM total_fte across "
                      "months — filter to a single month, or take the latest month, instead.",
    "fact_spend": "Grain: cost_centre x month x account_category. account_category is one of "
                  "Salaries, Contractor Costs, Software, Travel. Spend IS additive across months.",
}

COLUMN_DOCS: dict[str, str] = {
    "fact_headcount.total_fte": "point-in-time FTE; do not sum across months",
    "fact_spend.actual_amount": "actual spend in CAD",
    "fact_spend.budget_amount": "budgeted spend in CAD; variance = actual - budget",
}

# Worked NL -> SQL examples. Grounding the model on a few correct queries is the
# single biggest lever on generation accuracy for a fixed schema.
EXAMPLES: list[dict[str, str]] = [
    {
        "question": "What was total actual spend in December 2025?",
        "sql": "SELECT SUM(f.actual_amount) AS total_actual "
               "FROM fact_spend f JOIN dim_date d ON d.date_key = f.date_key "
               "WHERE d.month_start = '2025-12-01'",
    },
    {
        "question": "Which department had the highest headcount in the latest month?",
        "sql": "SELECT dep.department_name, f.total_fte "
               "FROM fact_headcount f "
               "JOIN dim_department dep ON dep.department_key = f.department_key "
               "WHERE f.date_key = (SELECT MAX(date_key) FROM fact_headcount) "
               "ORDER BY f.total_fte DESC LIMIT 1",
    },
    {
        "question": "Show budget vs actual variance for Technology salaries by month in 2025.",
        "sql": "SELECT d.month_start, f.budget_amount, f.actual_amount, "
               "f.actual_amount - f.budget_amount AS variance "
               "FROM fact_spend f "
               "JOIN dim_date d ON d.date_key = f.date_key "
               "JOIN dim_cost_centre cc ON cc.cost_centre_key = f.cost_centre_key "
               "JOIN dim_department dep ON dep.department_key = cc.department_key "
               "WHERE dep.department_name = 'Technology' AND f.account_category = 'Salaries' "
               "AND d.year = 2025 ORDER BY d.month_start",
    },
]


def _columns_for(table) -> list[str]:
    out = []
    for col in table.columns:
        key = f"{table.name}.{col.name}"
        note = COLUMN_DOCS.get(key)
        line = f"    {col.name} {col.type}"
        if note:
            line += f"  -- {note}"
        out.append(line)
    return out


def schema_prompt() -> str:
    """Full schema + business definitions + examples, formatted for the system
    prompt. Read-only views are included since answers may prefer them."""
    lines = ["# Helios warehouse schema (SQLite/PostgreSQL, read-only)\n"]
    for table in schema.metadata.tables.values():
        lines.append(f"TABLE {table.name}")
        if table.name in TABLE_DOCS:
            lines.append(f"  # {TABLE_DOCS[table.name]}")
        lines.extend(_columns_for(table))
        lines.append("")
    lines.append("# Convenience views (already joined):")
    for name in schema.VIEWS:
        lines.append(f"  {name}")
    lines.append("\n# Example questions and correct SQL:")
    for ex in EXAMPLES:
        lines.append(f"Q: {ex['question']}\nSQL: {ex['sql']}\n")
    return "\n".join(lines)


ALLOWED_TABLES: set[str] = set(schema.metadata.tables) | set(schema.VIEWS)
