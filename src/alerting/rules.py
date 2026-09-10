"""Which findings are worth waking a human for.

Alert fatigue is the failure mode here: a monitor that fires every month gets
muted, and then it is worth nothing on the month that matters. So thresholds are
deliberately conservative and every rule states the condition it fired on, in
the alert body, so the reader can judge it without opening the warehouse.

Thresholds are configurable via the environment, because "material" is a
business decision, not an engineering one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Finding:
    """One thing that tripped a rule."""

    code: str
    severity: str
    title: str
    detail: str


@dataclass
class Thresholds:
    """Alerting thresholds, distinct from the variance engine's own materiality.

    Materiality decides what shows up in a *report* a human is already reading;
    these decide what is worth an unprompted interruption, so the bars are
    higher.
    """

    net_variance_pct: float = 0.05       # net over/under budget for the month
    net_variance_abs: float = 100_000.0  # ...and at least this many dollars
    material_count: int = 5              # this many material line items
    critical_variance_pct: float = 0.15  # escalate to critical past this

    @classmethod
    def from_env(cls) -> "Thresholds":
        def _f(key: str, default: float) -> float:
            raw = os.environ.get(key, "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            net_variance_pct=_f("HELIOS_ALERT_NET_VARIANCE_PCT", 0.05),
            net_variance_abs=_f("HELIOS_ALERT_NET_VARIANCE_ABS", 100_000.0),
            material_count=int(_f("HELIOS_ALERT_MATERIAL_COUNT", 5)),
            critical_variance_pct=_f("HELIOS_ALERT_CRITICAL_PCT", 0.15),
        )


def evaluate_variance(variance_df: pd.DataFrame,
                      month: pd.Timestamp | None = None,
                      thresholds: Thresholds | None = None) -> list[Finding]:
    """Rules over one month of the variance report.

    Both a percentage *and* an absolute-dollar bar must be cleared for the net
    rule to fire — a 40% variance on a $3k line item is noise, and a $120k
    variance on a $40M budget is rounding.
    """
    t = thresholds or Thresholds.from_env()
    if variance_df.empty:
        return []
    month = month if month is not None else variance_df["month_start"].max()
    m = variance_df[variance_df["month_start"] == month]
    if m.empty:
        return []

    findings: list[Finding] = []
    label = pd.Timestamp(month).strftime("%B %Y")

    budget = float(m["budget_amount"].sum())
    net = float(m["variance_amount"].sum())
    pct = net / budget if budget else 0.0

    if abs(pct) >= t.net_variance_pct and abs(net) >= t.net_variance_abs:
        severity = "critical" if abs(pct) >= t.critical_variance_pct else "warning"
        direction = "over" if net > 0 else "under"
        findings.append(Finding(
            code="NET_VARIANCE",
            severity=severity,
            title=f"{label} spend is {abs(pct):.1%} {direction} budget",
            detail=(f"Net variance ${net:,.0f} against a ${budget:,.0f} budget "
                    f"({pct:+.1%}). Fired because |{pct:.1%}| >= "
                    f"{t.net_variance_pct:.0%} and |${net:,.0f}| >= "
                    f"${t.net_variance_abs:,.0f}."),
        ))

    material = m[m["material"]]
    if len(material) >= t.material_count:
        findings.append(Finding(
            code="MATERIAL_COUNT",
            severity="warning",
            title=f"{len(material)} material variances in {label}",
            detail=(f"{len(material)} of {len(m)} line items breached materiality "
                    f"(threshold: {t.material_count}). Broad misses usually mean a "
                    f"stale plan rather than a single bad line."),
        ))

    if "anomaly" in m.columns:
        anomalies = m[m["anomaly"].fillna(False).astype(bool)]
        for _, r in anomalies.iterrows():
            findings.append(Finding(
                code="ANOMALY",
                severity="warning",
                title=(f"{r['department_name']} / {r['account_category']} is "
                       f"unusual vs its own history"),
                detail=(f"Variance {r['variance_pct']:+.1%} "
                        f"(${r['variance_amount']:,.0f}), robust z-score "
                        f"{r.get('anomaly_z', 'n/a')} against this cell's median "
                        f"and MAD - not just large, but out of character."),
            ))

    return findings


def evaluate_drift(drift_report: dict) -> list[Finding]:
    """Rules over data/reports/drift_report.json.

    A promoted challenger is *good* news operationally but still worth telling
    people about: the forecast they were quoting last week has been replaced.
    """
    findings: list[Finding] = []
    for target, cycle in (drift_report.get("cycles") or {}).items():
        retrain = cycle.get("retrain") or {}
        if not retrain.get("triggered"):
            continue
        reasons = ", ".join(retrain.get("reasons") or []) or "unspecified"
        detail_block = cycle.get("detail") or {}
        ref, cur = detail_block.get("ref_mae"), detail_block.get("cur_mae")
        moved = ""
        if ref and cur:
            try:
                moved = f" Forecast error moved from MAE {float(ref):.2f} to {float(cur):.2f}."
            except (TypeError, ValueError):
                moved = ""

        promotion = cycle.get("promotion") or {}
        if promotion.get("promoted"):
            findings.append(Finding(
                code="MODEL_RETRAINED",
                severity="warning",
                title=f"{target}: forecast retrained and replaced",
                detail=(f"Drift detected ({reasons}).{moved} A challenger model beat "
                        f"the incumbent on a held-out window and was promoted - "
                        f"forecasts for this target have changed."),
            ))
        else:
            findings.append(Finding(
                code="DRIFT_UNRESOLVED",
                severity="critical",
                title=f"{target}: drift detected but no better model found",
                detail=(f"Drift detected ({reasons}).{moved} The challenger did not "
                        f"beat the incumbent, so the degraded model is still live. "
                        f"Treat this target's forecast as unreliable until reviewed."),
            ))
    return findings


def overall_severity(findings: list[Finding]) -> str:
    """The alert's level is its worst finding — nothing gets downgraded by
    being bundled with routine news."""
    if not findings:
        return "info"
    return max((f.severity for f in findings), key=lambda s: SEVERITY_ORDER[s])
