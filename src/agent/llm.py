"""LLM client abstraction: one small interface, two implementations.

`AnthropicClient` calls Claude (the real agent). `StubClient` is a deterministic,
offline SQL generator used by tests and by the no-API-key path — so the agent's
guardrails, self-correction loop, and evaluation harness are all exercisable in
CI without a key or network. The agent code depends only on the `complete`
protocol, never on a concrete client.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

DEFAULT_MODEL = "claude-opus-4-8"


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str:  # returns raw text
        ...

    @property
    def name(self) -> str:
        ...


class AnthropicClient:
    """Calls Claude via the official SDK. Credentials resolve from the
    environment (ANTHROPIC_API_KEY or an `ant auth login` profile)."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 1024):
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return f"anthropic:{self._model}"

    def complete(self, system: str, user: str) -> str:
        # Low effort + tight max_tokens: SQL generation is a short, well-scoped
        # task, so a small deterministic-ish response is preferred over
        # exploration. thinking is left adaptive (default) per the model.
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in message.content if b.type == "text")


# --- offline stub -------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_DEPTS = {"operations": "Operations", "technology": "Technology", "finance": "Finance",
          "human resources": "Human Resources", "hr": "Human Resources", "sales": "Sales"}
_CATS = {"salary": "Salaries", "salaries": "Salaries", "contractor": "Contractor Costs",
         "software": "Software", "travel": "Travel"}


class StubClient:
    """Deterministic template-based NL->SQL for offline runs and tests.

    Covers the shapes in the golden set (totals, per-department, variance,
    headcount, vacancy). It is intentionally simple — its job is to exercise the
    surrounding machinery deterministically, not to rival the real model.
    """

    @property
    def name(self) -> str:
        return "stub:offline"

    def complete(self, system: str, user: str) -> str:
        q = user.lower()
        month = self._month(q)
        dept = next((v for k, v in _DEPTS.items() if k in q), None)
        cat = next((v for k, v in _CATS.items() if k in q), None)

        if "vacancy" in q:
            sql = ("SELECT dep.department_name, v.vacancy_rate "
                   "FROM vw_headcount_vs_plan v "
                   "JOIN dim_date d ON d.month_start = v.month_start "
                   "JOIN dim_department dep ON dep.department_name = v.department_name "
                   "WHERE d.date_key = (SELECT MAX(date_key) FROM fact_headcount)")
        elif "headcount" in q or "fte" in q or "how many" in q:
            sql = ("SELECT dep.department_name, f.total_fte "
                   "FROM fact_headcount f "
                   "JOIN dim_department dep ON dep.department_key = f.department_key "
                   + self._month_clause(month, "f")
                   + " ORDER BY f.total_fte DESC")
        elif "variance" in q or ("budget" in q and "actual" in q):
            sql = ("SELECT dep.department_name, SUM(f.actual_amount - f.budget_amount) AS variance "
                   "FROM fact_spend f "
                   "JOIN dim_date d ON d.date_key = f.date_key "
                   "JOIN dim_cost_centre cc ON cc.cost_centre_key = f.cost_centre_key "
                   "JOIN dim_department dep ON dep.department_key = cc.department_key "
                   + self._filters(month, dept, cat, has_date_join=True)
                   + " GROUP BY dep.department_name ORDER BY variance DESC")
        else:  # spend total
            sql = ("SELECT SUM(f.actual_amount) AS total_actual "
                   "FROM fact_spend f JOIN dim_date d ON d.date_key = f.date_key "
                   "JOIN dim_cost_centre cc ON cc.cost_centre_key = f.cost_centre_key "
                   "JOIN dim_department dep ON dep.department_key = cc.department_key "
                   + self._filters(month, dept, cat, has_date_join=True))
        return f"```sql\n{sql}\n```"

    @staticmethod
    def _month(q: str):
        m = re.search(r"(january|february|march|april|may|june|july|august|"
                      r"september|october|november|december)\s+(\d{4})", q)
        if m:
            return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1)]:02d}-01"
        return None

    @staticmethod
    def _month_clause(month, alias):
        if month:
            return (f"JOIN dim_date d ON d.date_key = {alias}.date_key "
                    f"WHERE d.month_start = '{month}'")
        return f"WHERE {alias}.date_key = (SELECT MAX(date_key) FROM fact_headcount)"

    @staticmethod
    def _filters(month, dept, cat, has_date_join):
        clauses = []
        if month:
            clauses.append(f"d.month_start = '{month}'")
        if dept:
            clauses.append(f"dep.department_name = '{dept}'")
        if cat:
            clauses.append(f"f.account_category = '{cat}'")
        return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def default_client() -> LLMClient:
    """AnthropicClient when a credential is available, else the offline stub."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicClient()
        except Exception:
            pass
    return StubClient()
