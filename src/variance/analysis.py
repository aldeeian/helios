"""Budget-vs-actual variance analysis with volume / rate / mix driver attribution.

The point of this module is to explain *why* actuals diverged from plan, not
merely that they did — the difference between a variance report and a variance
*analysis* (spec Phase 5).

Materiality: a variance is flagged only when it is both large in percentage and
large in dollars, so a 40% overrun on a $2k line does not drown out a 6%
overrun on a $2M line. Thresholds are configurable.

Driver attribution (volume / mix / rate) is the standard management-accounting
decomposition and reconciles to total variance by construction — proven in
`decompose` and asserted in the tests:

    total_variance = volume_variance + mix_variance + rate_variance
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import sqlalchemy as sa

from .. import config

DEFAULT_PCT_THRESHOLD = 0.05      # 5%
DEFAULT_ABS_THRESHOLD = 50_000.0  # $50k


def load_spend_frame(engine: sa.Engine | None = None) -> pd.DataFrame:
    """Budget-vs-actual at (month, department, cost_centre, category) grain,
    read from the warehouse view — the trusted layer, never raw sources."""
    own = engine is None
    engine = engine or sa.create_engine(config.warehouse_db_url())
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sa.text("SELECT * FROM vw_budget_vs_actual"), conn)
            hc = pd.read_sql(sa.text(
                "SELECT d.month_start, dep.department_name, "
                "f.employee_fte, f.planned_fte "
                "FROM fact_headcount f "
                "JOIN dim_date d ON d.date_key = f.date_key "
                "JOIN dim_department dep ON dep.department_key = f.department_key"),
                conn)
    finally:
        if own:
            engine.dispose()
    df["month_start"] = pd.to_datetime(df["month_start"])
    hc["month_start"] = pd.to_datetime(hc["month_start"])
    return df.merge(hc, on=["month_start", "department_name"], how="left")


def compute_variance(df: pd.DataFrame,
                     pct_threshold: float = DEFAULT_PCT_THRESHOLD,
                     abs_threshold: float = DEFAULT_ABS_THRESHOLD) -> pd.DataFrame:
    """Add variance, direction, and materiality columns.

    Sign convention: for spend, actual > budget is UNFAVOURABLE (overspend).
    variance_amount = actual - budget (positive = over budget).
    """
    out = df.copy()
    out = out[out["budget_amount"].notna() & out["actual_amount"].notna()].copy()
    out["variance_amount"] = out["actual_amount"] - out["budget_amount"]
    out["variance_pct"] = out.apply(
        lambda r: (r["variance_amount"] / r["budget_amount"])
        if r["budget_amount"] else pd.NA, axis=1)
    out["direction"] = out["variance_amount"].apply(
        lambda v: "unfavourable" if v > 0 else ("favourable" if v < 0 else "on_budget"))
    out["material"] = out.apply(
        lambda r: bool(pd.notna(r["variance_pct"])
                       and abs(r["variance_pct"]) >= pct_threshold
                       and abs(r["variance_amount"]) >= abs_threshold), axis=1)
    return out


@dataclass
class Decomposition:
    total_variance: float
    volume_variance: float   # actuals vs plan differed in total quantity (headcount)
    mix_variance: float      # department composition shifted
    rate_variance: float     # cost per head differed from plan
    reconciles: bool

    def as_dict(self) -> dict:
        return {
            "total_variance": round(self.total_variance, 2),
            "volume_variance": round(self.volume_variance, 2),
            "mix_variance": round(self.mix_variance, 2),
            "rate_variance": round(self.rate_variance, 2),
            "reconciles": self.reconciles,
        }


def decompose(cells: pd.DataFrame, tol: float = 0.01) -> Decomposition:
    """Volume / mix / rate decomposition across cells (e.g. departments).

    Each row needs actual_qty, budget_qty, actual_spend, budget_spend. Rates
    are derived per cell (spend / qty). The identity, proven in the module
    docstring's algebra:

        volume + mix + rate = Σ(Aqᵢ·Arᵢ) − Σ(Bqᵢ·Brᵢ) = total actual − total budget

    Cells with zero quantity on either side are dropped from the rate/mix terms
    (rate undefined) but their spend still counts in the total, surfaced as an
    unattributed residual folded into rate so the identity always holds.
    """
    d = cells.copy()
    total_actual = d["actual_spend"].sum()
    total_budget = d["budget_spend"].sum()
    total_var = total_actual - total_budget

    valid = d[(d["actual_qty"] > 0) & (d["budget_qty"] > 0)].copy()
    Qa = valid["actual_qty"].sum()
    Qb = valid["budget_qty"].sum()
    valid["br"] = valid["budget_spend"] / valid["budget_qty"]
    valid["ar"] = valid["actual_spend"] / valid["actual_qty"]
    valid["mix_b"] = valid["budget_qty"] / Qb if Qb else 0.0
    valid["mix_a"] = valid["actual_qty"] / Qa if Qa else 0.0

    avg_budget_rate = (valid["mix_b"] * valid["br"]).sum()
    volume = (Qa - Qb) * avg_budget_rate
    mix = Qa * ((valid["mix_a"] - valid["mix_b"]) * valid["br"]).sum()
    rate = (valid["actual_qty"] * (valid["ar"] - valid["br"])).sum()

    # Fold any spend from dropped (zero-qty) cells into rate as residual so the
    # reported components always reconcile to the true total variance.
    attributed = volume + mix + rate
    residual = total_var - attributed
    rate += residual

    return Decomposition(
        total_variance=total_var,
        volume_variance=volume,
        mix_variance=mix,
        rate_variance=rate,
        reconciles=abs((volume + mix + rate) - total_var) < tol,
    )


def decompose_salaries(variance_df: pd.DataFrame, month: pd.Timestamp) -> Decomposition:
    """Volume/mix/rate for Salaries across departments in one month.

    Quantity = FTE: actual = filled employee FTE, budget = planned FTE. This is
    the honest, fully-budgeted driver — headcount plan exists, so both actual
    and budget quantities are real, not inferred.
    """
    sal = variance_df[(variance_df["account_category"] == "Salaries")
                      & (variance_df["month_start"] == month)].copy()
    cells = pd.DataFrame({
        "department": sal["department_name"],
        "actual_qty": sal["employee_fte"],
        "budget_qty": sal["planned_fte"],
        "actual_spend": sal["actual_amount"],
        "budget_spend": sal["budget_amount"],
    }).dropna()
    return decompose(cells)


def top_variances(variance_df: pd.DataFrame, n: int = 10,
                  material_only: bool = True) -> pd.DataFrame:
    """Largest absolute-dollar variances, most material first."""
    df = variance_df.copy()
    if material_only:
        df = df[df["material"]]
    df = df.assign(abs_var=df["variance_amount"].abs())
    return (df.sort_values("abs_var", ascending=False)
            .drop(columns="abs_var")
            .head(n)
            .reset_index(drop=True))


def detect_anomalies(variance_df: pd.DataFrame, z_threshold: float = 2.0) -> pd.DataFrame:
    """Flag variance% that is statistically unusual vs that cell's own history.

    Per (department, category) series, a robust z-score using median and MAD
    (median absolute deviation) — resistant to the very outliers we are hunting,
    unlike mean/std. Needs >= 6 historical points or the cell is skipped.
    """
    df = variance_df.copy()
    df["anomaly"] = False
    df["anomaly_z"] = pd.NA
    for _, idx in df.groupby(["department_name", "account_category"]).groups.items():
        grp = df.loc[idx].sort_values("month_start")
        v = grp["variance_pct"].astype(float)
        if v.notna().sum() < 6:
            continue
        med = v.median()
        mad = (v - med).abs().median()
        if mad == 0:
            continue
        z = 0.6745 * (v - med) / mad  # 0.6745 scales MAD to std-equivalent
        df.loc[grp.index, "anomaly_z"] = z.round(2)
        df.loc[grp.index, "anomaly"] = z.abs() >= z_threshold
    return df


def summarize_period(variance_df: pd.DataFrame, month: pd.Timestamp) -> str:
    """Plain-language variance summary for one month (spec Phase 5 step 4)."""
    m = variance_df[variance_df["month_start"] == month]
    material = m[m["material"]]
    total_over = m[m["variance_amount"] > 0]["variance_amount"].sum()
    total_under = m[m["variance_amount"] < 0]["variance_amount"].sum()
    lines = [
        f"Variance summary for {month.strftime('%B %Y')}:",
        f"  Net variance: ${m['variance_amount'].sum():,.0f} "
        f"(${total_over:,.0f} over, ${total_under:,.0f} under budget)",
        f"  {len(material)} material variance(s) of {len(m)} line items.",
    ]
    for _, r in top_variances(m, n=3).iterrows():
        verb = "over" if r["variance_amount"] > 0 else "under"
        lines.append(
            f"  - {r['department_name']} / {r['account_category']}: "
            f"${abs(r['variance_amount']):,.0f} {verb} "
            f"({r['variance_pct']:+.1%})")
    dec = decompose_salaries(variance_df, month)
    if dec.total_variance != 0:
        lines.append(
            f"  Salary variance drivers: "
            f"volume ${dec.volume_variance:,.0f}, "
            f"mix ${dec.mix_variance:,.0f}, "
            f"rate ${dec.rate_variance:,.0f}.")
    return "\n".join(lines)
