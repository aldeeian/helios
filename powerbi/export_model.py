"""Export the warehouse as Power BI-ready CSVs.

Assembling the .pbix is a GUI step, but the *tedious* part of it — standing up a
database connection from a laptop, getting drivers working, picking the right
tables — is not modelling work and should not be done by hand. This dumps the
star schema to `powerbi/model/`, so Get Data -> Folder is the only connection
step, and the person doing the GUI work spends their time on relationships and
measures instead of ODBC.

Dates are written as ISO `YYYY-MM-DD` strings: Power BI's locale-sensitive date
parsing will silently read `03/04/2025` as March or April depending on the
machine's regional settings, and a dashboard that is wrong by a month on someone
else's laptop is worse than one that fails loudly.

    python -m powerbi.export_model            # -> powerbi/model/*.csv
    python -m powerbi.export_model --out DIR  # elsewhere
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

from src import config

MODEL_DIR = Path(__file__).resolve().parent / "model"

# Dimensions and facts for the star schema, plus the views that carry business
# logic (vacancy, contractor mix) we do not want re-implemented in DAX.
TABLES = [
    "dim_date", "dim_department", "dim_cost_centre", "dim_role_level",
    "dim_employee", "fact_headcount", "fact_spend",
    "vw_budget_vs_actual", "vw_headcount_vs_plan", "vw_contractor_mix",
]

DATE_COLUMNS = {"month_start", "hire_date", "termination_date",
                "valid_from", "valid_to"}


def _iso_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise real date columns to ISO strings, and nothing else.

    Numeric columns are skipped unconditionally: `date_key` is an integer
    surrogate, and pd.to_datetime happily reads 1, 2, 3 as nanoseconds since the
    epoch — collapsing every row to 1970-01-01 and destroying the joins. The
    dtype check, not the column name, is what makes this safe.
    """
    for col in df.columns:
        if col not in DATE_COLUMNS:
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            continue
        parsed = pd.to_datetime(df[col], errors="coerce")
        if parsed.notna().any():
            df[col] = parsed.dt.strftime("%Y-%m-%d")
    return df


def export(out_dir: Path | None = None, engine: sa.Engine | None = None) -> dict[str, int]:
    out = Path(out_dir or MODEL_DIR)
    out.mkdir(parents=True, exist_ok=True)

    eng = engine or sa.create_engine(config.warehouse_db_url())
    written: dict[str, int] = {}
    try:
        available = set(sa.inspect(eng).get_table_names()) | set(sa.inspect(eng).get_view_names())
        for table in TABLES:
            if table not in available:
                continue  # optional views may not exist in a partial build
            df = pd.read_sql_table(table, eng) if table in available else None
            df = _iso_dates(df)
            df.to_csv(out / f"{table}.csv", index=False, encoding="utf-8")
            written[table] = len(df)
    finally:
        if engine is None:
            eng.dispose()

    if not written:
        raise RuntimeError(
            "Warehouse has no tables to export. Run `python -m src.warehouse` first.")
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m powerbi.export_model",
                                description="Export the warehouse as Power BI-ready CSVs.")
    p.add_argument("--out", type=Path, default=MODEL_DIR)
    args = p.parse_args(argv)

    try:
        written = export(args.out)
    except Exception as e:
        print(f"Export failed: {e}")
        return 1

    width = max(len(t) for t in written)
    for table, rows in written.items():
        print(f"  {table.ljust(width)}  {rows:>7,} rows")
    print(f"\n{len(written)} tables -> {args.out.resolve()}")
    print("In Power BI Desktop: Get Data -> Folder -> select that directory -> "
          "Combine & Load.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
