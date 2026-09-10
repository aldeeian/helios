"""Entry point: `python -m src.alerting` evaluates the latest month and sends
the digest to whatever channels are configured.

    python -m src.alerting                 # evaluate + deliver
    python -m src.alerting --dry-run       # print the digest, deliver nothing
    python -m src.alerting --month 2025-12 # pin the period
    python -m src.alerting --test          # send a synthetic alert to prove wiring

`--test` exists because the interesting failure — a webhook or API key that is
wrong — cannot be discovered on a month where nothing breached, and you do not
want to find out during a real incident.
"""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd
import sqlalchemy as sa

from .. import config
from ..variance import analysis as va
from . import digest, rules
from .channels import Alert, send

DRIFT_REPORT = config.DATA_DIR / "reports" / "drift_report.json"


def _context(variance_df: pd.DataFrame, month: pd.Timestamp,
             engine: sa.Engine) -> digest.Context:
    m = variance_df[variance_df["month_start"] == month]
    headcount = None
    try:
        with engine.connect() as c:
            headcount = c.execute(sa.text(
                "SELECT SUM(employee_fte) + COALESCE(SUM(contractor_fte), 0) "
                "FROM fact_headcount WHERE month_start = :m"),
                {"m": month.strftime("%Y-%m-%d")}).scalar()
    except Exception:
        pass  # context is a nicety; never let it break the alert
    return digest.Context(
        month=month.strftime("%B %Y"),
        actual=float(m["actual_amount"].sum()),
        budget=float(m["budget_amount"].sum()),
        variance=float(m["variance_amount"].sum()),
        headcount=float(headcount) if headcount is not None else None,
    )


def collect(month: str | None = None) -> tuple[list[rules.Finding], digest.Context]:
    """Run every rule against the current warehouse + drift report."""
    engine = sa.create_engine(config.warehouse_db_url())
    try:
        var = va.compute_variance(va.load_spend_frame(engine))
        var = va.detect_anomalies(var)
        target = (pd.Timestamp(month + "-01") if month
                  else var["month_start"].max())
        ctx = _context(var, target, engine)
    finally:
        engine.dispose()

    findings = rules.evaluate_variance(var, target)

    if DRIFT_REPORT.exists():
        try:
            findings += rules.evaluate_drift(
                json.loads(DRIFT_REPORT.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            findings.append(rules.Finding(
                "DRIFT_REPORT_UNREADABLE", "warning",
                "Drift report could not be read",
                f"{DRIFT_REPORT} exists but failed to parse: {e}. "
                "Model-health rules did not run this cycle."))
    return findings, ctx


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.alerting",
                                description="Evaluate Helios thresholds and send a digest.")
    p.add_argument("--month", help="YYYY-MM to evaluate (default: latest in warehouse)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the digest without delivering it")
    p.add_argument("--test", action="store_true",
                   help="deliver a synthetic alert to verify channel configuration")
    p.add_argument("--quiet-if-clear", action="store_true",
                   help="send nothing when no thresholds were breached")
    args = p.parse_args(argv)

    if args.test:
        alert = Alert(
            subject="Helios: channel test",
            body_text=("This is a test alert from Helios. If you are reading it, "
                       "this channel is wired correctly. No thresholds were evaluated."),
            level="info")
    else:
        try:
            findings, ctx = collect(args.month)
        except Exception as e:
            print(f"Could not evaluate alerts: {type(e).__name__}: {e}", file=sys.stderr)
            print("Has the warehouse been loaded? Try: python -m src.warehouse",
                  file=sys.stderr)
            return 1
        if not findings and args.quiet_if_clear:
            print("No thresholds breached; nothing sent (--quiet-if-clear).")
            return 0
        alert = digest.build(findings, ctx)

    print(f"Subject: {alert.subject}\n")
    print(alert.body_text)
    print()

    if args.dry_run:
        print("[dry run] nothing delivered.")
        return 0

    results = send(alert)
    for r in results:
        print(f"  [{'ok ' if r.ok else 'FAIL'}] {r.channel}: {r.detail}")

    remote = [r for r in results if r.channel != "file"]
    if not remote:
        print("\nOnly the file channel is configured. Set HELIOS_SLACK_WEBHOOK_URL "
              "or RESEND_API_KEY + HELIOS_ALERT_FROM/TO to deliver externally "
              "(see .env.example).")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
