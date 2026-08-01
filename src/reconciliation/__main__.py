"""Entry point: `python -m src.reconciliation` — one command produces the
reconciled dataset + quarantine table + QC scorecard (spec Phase 2 DoD)."""

from __future__ import annotations

import json

from .. import config
from ..ingestion import readers
from . import engine


def main() -> None:
    hr = readers.read_hr()
    fin = readers.read_financials()
    ctr = readers.read_contractors()
    cc_map = readers.read_cost_centre_map()
    plan = readers.read_headcount_plan()

    result = engine.run(hr, fin, ctr, cc_map, plan_raw=plan)

    out = config.RECONCILED_DIR
    out.mkdir(parents=True, exist_ok=True)
    result.employees.to_csv(out / "employees.csv", index=False)
    result.contractors.to_csv(out / "contractors.csv", index=False)
    result.financials.to_csv(out / "financials.csv", index=False)
    if result.plan is not None:
        result.plan.to_csv(out / "plan.csv", index=False)
    result.quarantine.to_csv(out / "quarantine.csv", index=False)
    (out / "scorecard.json").write_text(
        json.dumps(result.scorecard, indent=2), encoding="utf-8"
    )

    sc = result.scorecard
    print("Reconciliation complete — data quality scorecard")
    print("-" * 48)
    for source in sc["rows_in"]:
        print(f"  {source:11s}: {sc['rows_in'][source]:5d} in -> "
              f"{sc['rows_kept'][source]:5d} kept, "
              f"{sc['rows_quarantined'].get(source, 0):3d} quarantined")
    print(f"  quarantine reasons: {sc['quarantine_reasons']}")
    print(f"  period gaps: {sc['period_gaps']}")
    print(f"  outputs -> {out}")


if __name__ == "__main__":
    main()
