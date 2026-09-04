"""The natural-language query agent: question in, grounded answer + SQL out.

Loop (spec Phase 9 step 2):
    interpret -> generate SQL -> VALIDATE (guardrails) -> execute
    -> on error, feed the error back and retry (capped) -> return

Every answer carries the SQL that produced it and the tables it touched, so it
is auditable — the agent never returns a number the user can't trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlalchemy as sa
import sqlglot
from sqlglot import exp

from . import executor, guardrails, semantic
from .llm import LLMClient, default_client

MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """You are a careful analytics assistant for the Helios data warehouse.
Translate the user's question into a single read-only SQL SELECT for SQLite.

Rules:
- Output ONLY the SQL in a ```sql code block. No prose.
- SELECT only. Never INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA.
- Use only the tables, columns, and views in the schema below.
- Headcount (fact_headcount.total_fte) is point-in-time: never SUM it across
  months; filter to one month or use the latest month.
- Prefer the convenience views when they answer the question directly.
- Always include a LIMIT unless using an aggregate that returns one row.

{schema}
"""


@dataclass
class AgentAnswer:
    question: str
    sql: str | None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    attempts: int = 0
    error: str | None = None
    llm: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and self.sql is not None

    def summary(self) -> str:
        if not self.ok:
            return f"Could not answer: {self.error}"
        if not self.rows:
            return "Query ran but returned no rows."
        if len(self.rows) == 1 and len(self.columns) == 1:
            return f"{self.columns[0]} = {self.rows[0][0]}"
        head = ", ".join(self.columns)
        preview = "; ".join(str(tuple(r)) for r in self.rows[:5])
        return f"[{head}] {preview}" + (" …" if len(self.rows) > 5 else "")


def _extract_sql(text: str) -> str:
    """Pull SQL out of a ```sql block, or fall back to the raw text."""
    m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else text).strip().rstrip(";").strip()


def _tables_in(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return []
    cte = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    return sorted({t.name for t in tree.find_all(exp.Table) if t.name not in cte})


def ask(question: str, client: LLMClient | None = None,
        engine: sa.Engine | None = None,
        max_attempts: int = MAX_ATTEMPTS) -> AgentAnswer:
    client = client or default_client()
    system = SYSTEM_PROMPT.format(schema=semantic.schema_prompt())
    answer = AgentAnswer(question=question, sql=None, llm=client.name)
    user = question
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        answer.attempts = attempt
        raw = client.complete(system, user)
        candidate = _extract_sql(raw)
        try:
            safe_sql = guardrails.validate(candidate)
        except guardrails.UnsafeQuery as e:
            last_error = f"blocked by safety check: {e}"
            user = (f"{question}\n\nYour previous SQL was rejected: {e}\n"
                    f"Rejected SQL:\n{candidate}\nWrite a corrected read-only SELECT.")
            continue
        try:
            result = executor.execute(safe_sql, engine=engine)
        except Exception as e:  # execution error -> feed back and retry
            last_error = f"execution error: {type(e).__name__}: {e}"
            user = (f"{question}\n\nYour previous SQL failed to execute: {e}\n"
                    f"SQL:\n{safe_sql}\nFix it and return a corrected SELECT.")
            continue
        # success
        answer.sql = safe_sql
        answer.columns = result.columns
        answer.rows = result.rows
        answer.tables_used = _tables_in(safe_sql)
        answer.error = None
        return answer

    answer.error = last_error or "no valid SQL produced"
    return answer
