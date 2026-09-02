"""SQL safety validation — the security boundary for the NL query agent.

Every model-generated query passes through `validate` before it touches the
database. The checks are defense-in-depth (spec Phase 9 step 3): even with a
read-only DB user, the agent must not be a vector for exfiltration or denial of
service. Parsing (not regex) is the primary mechanism — we inspect the parsed
AST via sqlglot, so obfuscation like comments or casing cannot slip a mutation
past a string match.

Rejections raise `UnsafeQuery`; the agent surfaces the reason and may retry.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .semantic import ALLOWED_TABLES

MAX_ROW_LIMIT = 1000

# Statement types that mutate or leak — anything not a plain SELECT is rejected.
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Merge, exp.Command,  # Command covers PRAGMA, ATTACH, VACUUM, etc.
)
# Functions a read-only analytical query never needs, but an attacker would.
_FORBIDDEN_FUNCS = {"load_extension", "readfile", "writefile", "edit", "pg_read_file",
                    "pg_sleep", "dblink", "lo_import", "lo_export"}


class UnsafeQuery(ValueError):
    """Raised when generated SQL fails a safety check."""


def _single_statement(sql: str):
    statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    if len(statements) != 1:
        raise UnsafeQuery(f"expected exactly one statement, got {len(statements)} "
                          "(stacked queries are blocked)")
    return statements[0]


def validate(sql: str, max_rows: int = MAX_ROW_LIMIT) -> str:
    """Parse and vet `sql`; return a safe, row-limited SELECT or raise UnsafeQuery.

    Enforced invariants:
      1. Parses as exactly one statement (no `;`-stacked injection).
      2. The root is a SELECT (or a parenthesized/CTE SELECT). Nothing else.
      3. No INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH anywhere in the tree.
      4. No blocklisted functions (file/network/sleep primitives).
      5. Every referenced table is in the known schema allowlist.
      6. A LIMIT <= max_rows is present (added if missing) — bounds result size.
    """
    if not sql or not sql.strip():
        raise UnsafeQuery("empty query")
    try:
        tree = _single_statement(sql)
    except sqlglot.errors.ParseError as e:
        raise UnsafeQuery(f"could not parse SQL: {e}") from e

    # 2. Root must be a SELECT (unwrap CTEs / subquery wrappers).
    root = tree
    if isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, exp.Union)):
        raise UnsafeQuery(f"only SELECT queries are allowed, got {type(tree).__name__}")

    # 3. No mutating / command nodes anywhere.
    for node_type in _FORBIDDEN_NODES:
        bad = list(tree.find_all(node_type))
        if bad:
            raise UnsafeQuery(f"forbidden statement type: {node_type.__name__}")

    # 4. No dangerous functions (match by name, case-insensitive).
    for fn in tree.find_all(exp.Anonymous):
        name = (fn.name or "").lower()
        if name in _FORBIDDEN_FUNCS:
            raise UnsafeQuery(f"forbidden function: {name}")
    for fn in tree.find_all(exp.Func):
        name = (fn.sql_name() or "").lower()
        if name in _FORBIDDEN_FUNCS:
            raise UnsafeQuery(f"forbidden function: {name}")

    # 5. Every real table reference must be a known schema object.
    referenced = {t.name for t in tree.find_all(exp.Table)}
    # CTE names are self-defined, not schema tables — exclude them.
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    unknown = {t for t in referenced if t not in ALLOWED_TABLES and t not in cte_names}
    if unknown:
        raise UnsafeQuery(f"unknown table(s): {sorted(unknown)}")

    # 6. Enforce a row limit. Respect a tighter existing LIMIT; cap a larger one.
    # Mutate in place (copy=False) so the change is reflected in `tree`, which
    # `root` is part of (root is tree, tree.this, or wrapped below).
    if isinstance(root, exp.Select):
        limit = root.args.get("limit")
        if limit is None:
            root.limit(max_rows, copy=False)
        else:
            try:
                current = int(limit.expression.name)
            except (ValueError, AttributeError):
                current = max_rows + 1  # unparseable limit -> force the cap
            if current > max_rows:
                root.limit(max_rows, copy=False)
    else:  # UNION and similar: wrap to bound total rows
        tree = exp.select("*").from_(tree.subquery("_capped")).limit(max_rows)

    return tree.sql(dialect="sqlite")
