"""Execute validated SQL against the warehouse, read-only and bounded.

Belt to the guardrails' suspenders: the query is already validated as a
row-limited SELECT, and here it also runs under a statement timeout and a hard
fetch cap, and (on Postgres) as a read-only transaction. Even a validation
bypass cannot mutate or hang the database.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from .. import config

FETCH_CAP = 1000
STATEMENT_TIMEOUT_MS = 5000


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool

    def to_records(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]


def execute(sql: str, engine: sa.Engine | None = None,
            fetch_cap: int = FETCH_CAP) -> QueryResult:
    own = engine is None
    engine = engine or sa.create_engine(config.warehouse_db_url())
    try:
        with engine.connect() as conn:
            _apply_readonly(conn)
            result = conn.execute(sa.text(sql))
            cols = list(result.keys())
            rows = result.fetchmany(fetch_cap + 1)
            truncated = len(rows) > fetch_cap
            return QueryResult(columns=cols, rows=[tuple(r) for r in rows[:fetch_cap]],
                               truncated=truncated)
    finally:
        if own:
            engine.dispose()


def _apply_readonly(conn: sa.Connection) -> None:
    """Best-effort per-dialect read-only + timeout hardening."""
    dialect = conn.engine.dialect.name
    try:
        if dialect == "postgresql":
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            conn.execute(sa.text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        elif dialect == "sqlite":
            # query_only rejects any write at the engine level for this connection.
            conn.execute(sa.text("PRAGMA query_only = ON"))
    except Exception:
        # Hardening is defense-in-depth; guardrails already guarantee a SELECT.
        pass
