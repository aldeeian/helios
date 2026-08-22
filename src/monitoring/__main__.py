"""Entry point: `python -m src.monitoring` demonstrates the full MLOps loop on
real Helios targets — the month-24 reorganization is detected as drift,
triggers retraining, and the challenger is gated against the champion.

Writes data/reports/drift_report.json.
"""

from __future__ import annotations

import json

from .. import config
from ..datagen.org_model import STRUCTURAL_BREAK_MONTH
from ..forecasting import datasets
from ..forecasting.models import sarima
from . import pipeline

REPORTS_DIR = config.DATA_DIR / "reports"

# The departments the reorg hit hardest — where drift should be unambiguous.
DEMO_TARGETS = ["headcount::Technology", "headcount::Operations", "headcount::TOTAL"]


def main() -> None:
    targets = datasets.load_targets()
    report: dict = {"structural_break_month": STRUCTURAL_BREAK_MONTH, "cycles": {}}

    for name in DEMO_TARGETS:
        series = targets[name]
        result = pipeline.run_cycle(
            series=series, target=name, model_fn=sarima, model_name="sarima",
            break_index=STRUCTURAL_BREAK_MONTH,
        )
        report["cycles"][name] = {
            "detail": result.detail,
            "signals": [{"monitor": s.monitor, "drift": s.drift, **s.detail}
                        for s in result.signals],
            "retrain": {"triggered": result.retrain.triggered,
                        "reasons": result.retrain.reasons},
            "promotion": result.promotion.__dict__ if result.promotion else None,
        }

        print(f"\n=== {name} ===")
        for s in result.signals:
            mark = "DRIFT" if s.drift else "  ok "
            print(f"  [{mark}] {s.monitor}")
        print(f"  MAE: reference {result.detail['ref_mae']} -> "
              f"current {result.detail['cur_mae']}")
        if result.retrain.triggered:
            print(f"  RETRAIN TRIGGERED by: {', '.join(result.retrain.reasons)}")
            p = result.promotion
            print(f"  champion MAE {p.champion_mae} vs challenger MAE {p.challenger_mae}"
                  f"  -> {p.note}")
        else:
            print("  no retraining needed")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "drift_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    n_triggered = sum(c["retrain"]["triggered"] for c in report["cycles"].values())
    print(f"\nRetraining triggered on {n_triggered}/{len(DEMO_TARGETS)} targets.")
    print(f"Report -> {REPORTS_DIR / 'drift_report.json'}")


if __name__ == "__main__":
    main()
