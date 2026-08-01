"""Entry point: `python -m src.datagen --seed 42` regenerates all three source
systems deterministically (spec Phase 1 definition of done)."""

from __future__ import annotations

import argparse
import json

import numpy as np

from .. import config
from . import sources, writers
from .truth import simulate


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Helios synthetic source systems")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    truth = simulate(args.seed)
    # Separate, seed-derived stream for messiness so truth and mess are
    # independently reproducible.
    mess_rng = np.random.default_rng(args.seed + 1)
    log: dict = {"seed": args.seed, "params": truth.params}

    hr = sources.build_hr_frame(truth, mess_rng, log)
    fin = sources.build_financials_frame(truth, mess_rng, log)
    lookup = sources.build_lookup_frame(log)
    ctr = sources.build_contractor_frame(truth, mess_rng, log)

    writers.write_hr_xlsx(hr)
    writers.write_financials_csv(fin)
    writers.write_headcount_plan_csv(truth)
    writers.write_lookup_csv(lookup)
    writers.write_contractor_table(ctr)
    writers.write_truth(truth)
    log["headcount_plan"] = {"rows": len(truth.headcount_plan), "clean_by_design": True}

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = config.DATA_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(log, indent=2), encoding="utf-8")

    print(f"Generated sources (seed={args.seed}):")
    print(f"  HR workbook     : {config.HR_XLSX}  ({log['hr']['rows']} rows)")
    print(f"  Financials CSV  : {config.FINANCIALS_CSV}  ({log['financials']['rows']} rows)")
    print(f"  Lookup CSV      : {config.COST_CENTRE_MAP_CSV}  (CC-500 missing by design)")
    print(f"  Contractor table: contractor_roster  ({log['contractors']['rows']} rows)")
    print(f"  Manifest        : {manifest_path}")


if __name__ == "__main__":
    main()
