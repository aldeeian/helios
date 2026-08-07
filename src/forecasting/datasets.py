"""Build forecast target series from the warehouse analytical views.

Forecasting reads ONLY the warehouse (the trusted layer) — never the raw
sources and never data/truth/. Ground truth is reserved for evaluation
reporting, keeping the model-building path honest.
"""

from __future__ import annotations

import pandas as pd
import sqlalchemy as sa

from .. import config


def load_targets(engine: sa.Engine | None = None) -> dict[str, pd.Series]:
    """Return monthly series keyed 'headcount::<dept>' / 'spend::<dept>' plus
    '::TOTAL' aggregates. Spend excludes Sales (CC-500) — its rows are
    quarantined by the unmapped-lookup gap upstream, and inventing them here
    would hide a data-quality finding.
    """
    own = engine is None
    engine = engine or sa.create_engine(config.warehouse_db_url())
    try:
        with engine.connect() as conn:
            hc = pd.read_sql(sa.text(
                "SELECT month_start, department_name, total_fte "
                "FROM vw_headcount_trend"), conn)
            sp = pd.read_sql(sa.text(
                "SELECT month_start, department_name, actual_amount "
                "FROM vw_budget_vs_actual WHERE actual_amount IS NOT NULL"), conn)
    finally:
        if own:
            engine.dispose()

    targets: dict[str, pd.Series] = {}
    hc["month_start"] = pd.to_datetime(hc["month_start"])
    for dept, g in hc.groupby("department_name"):
        targets[f"headcount::{dept}"] = (
            g.set_index("month_start")["total_fte"].sort_index().asfreq("MS")
        )
    targets["headcount::TOTAL"] = (
        hc.groupby("month_start")["total_fte"].sum().sort_index().asfreq("MS")
    )

    sp["month_start"] = pd.to_datetime(sp["month_start"])
    monthly_spend = (
        sp.groupby(["month_start", "department_name"])["actual_amount"].sum().reset_index()
    )
    for dept, g in monthly_spend.groupby("department_name"):
        s = g.set_index("month_start")["actual_amount"].sort_index().asfreq("MS")
        # Interior gaps (e.g. CC-300's missing periods) are linearly
        # interpolated for modeling and the gap is documented upstream on the
        # QC scorecard; models cannot fit NaNs.
        targets[f"spend::{dept}"] = s.interpolate(limit_area="inside")
    targets["spend::TOTAL"] = (
        monthly_spend.groupby("month_start")["actual_amount"].sum()
        .sort_index().asfreq("MS").interpolate(limit_area="inside")
    )
    return targets
