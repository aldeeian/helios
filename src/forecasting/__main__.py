"""Entry point: `python -m src.forecasting` backtests every model family on
every target, logs runs to MLflow, registers a champion per target, and writes
data/reports/backtest_report.json."""

from __future__ import annotations

import argparse

from . import runner


def main() -> None:
    p = argparse.ArgumentParser(description="Helios forecasting backtests")
    p.add_argument("--no-prophet", action="store_true",
                   help="skip Prophet (slowest family)")
    p.add_argument("--no-mlflow", action="store_true",
                   help="skip MLflow tracking (report file only)")
    args = p.parse_args()
    report = runner.run(
        include_prophet=not args.no_prophet, use_mlflow=not args.no_mlflow
    )
    print(runner.format_report(report))


if __name__ == "__main__":
    main()
