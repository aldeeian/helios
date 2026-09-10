"""Alerting: thresholds fire on the right conditions, and delivery is resilient.

The behaviour worth protecting is not "an email got sent" — it is that a
channel outage cannot suppress the durable record, and that a quiet month stays
quiet. Both are easy to regress and expensive to discover in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.alerting import channels, digest, rules


# --- fixtures -------------------------------------------------------------

def _variance_frame(variance_amount: float = 200_000.0,
                    budget: float = 1_000_000.0,
                    material: bool = True) -> pd.DataFrame:
    return pd.DataFrame({
        "month_start": [pd.Timestamp("2025-12-01")],
        "department_name": ["Technology"],
        "account_category": ["Contractor Costs"],
        "budget_amount": [budget],
        "actual_amount": [budget + variance_amount],
        "variance_amount": [variance_amount],
        "variance_pct": [variance_amount / budget],
        "direction": ["unfavourable"],
        "material": [material],
    })


# --- rules ----------------------------------------------------------------

def test_net_variance_needs_both_pct_and_dollars():
    """A big percentage on a small base must not fire — that is the noise the
    dual threshold exists to suppress."""
    t = rules.Thresholds(net_variance_pct=0.05, net_variance_abs=100_000.0)

    loud_but_tiny = _variance_frame(variance_amount=900.0, budget=1_000.0)
    assert rules.evaluate_variance(loud_but_tiny, thresholds=t) == []

    big_and_material = _variance_frame(variance_amount=200_000.0, budget=1_000_000.0)
    codes = [f.code for f in rules.evaluate_variance(big_and_material, thresholds=t)]
    assert "NET_VARIANCE" in codes


def test_net_variance_escalates_to_critical():
    t = rules.Thresholds(critical_variance_pct=0.15)
    df = _variance_frame(variance_amount=300_000.0, budget=1_000_000.0)  # +30%
    finding = next(f for f in rules.evaluate_variance(df, thresholds=t)
                   if f.code == "NET_VARIANCE")
    assert finding.severity == "critical"


def test_clean_month_produces_no_findings():
    df = _variance_frame(variance_amount=1_000.0, budget=1_000_000.0, material=False)
    assert rules.evaluate_variance(df) == []


def test_drift_without_promotion_is_critical():
    """A model that drifted and could not be replaced is the worst state: the
    degraded forecast is still serving. It must outrank a successful retrain."""
    report = {"cycles": {
        "headcount::Technology": {
            "detail": {"ref_mae": 2.0, "cur_mae": 35.0},
            "retrain": {"triggered": True, "reasons": ["concept_drift"]},
            "promotion": {"promoted": False},
        },
        "headcount::Operations": {
            "detail": {"ref_mae": 6.0, "cur_mae": 25.0},
            "retrain": {"triggered": True, "reasons": ["performance_decay"]},
            "promotion": {"promoted": True},
        },
    }}
    findings = rules.evaluate_drift(report)
    by_code = {f.code: f for f in findings}
    assert by_code["DRIFT_UNRESOLVED"].severity == "critical"
    assert by_code["MODEL_RETRAINED"].severity == "warning"
    assert rules.overall_severity(findings) == "critical"


def test_untriggered_targets_are_silent():
    report = {"cycles": {"headcount::TOTAL": {
        "retrain": {"triggered": False, "reasons": []}, "promotion": None}}}
    assert rules.evaluate_drift(report) == []


# --- digest ---------------------------------------------------------------

def test_html_and_text_carry_the_same_findings():
    """A plain-text reader must never be worse informed than an HTML one."""
    findings = [rules.Finding("X", "warning", "Spend is over budget", "detail here")]
    ctx = digest.Context(month="December 2025", actual=1_100.0, budget=1_000.0)
    alert = digest.build(findings, ctx)
    for body in (alert.body_text, alert.body_html):
        assert "Spend is over budget" in body
        assert "December 2025" in body
    assert alert.level == "warning"


def test_all_clear_digest_is_info_level():
    alert = digest.build([], digest.Context(month="December 2025"))
    assert alert.level == "info"
    assert "Nothing needs attention" in alert.body_text


def test_alert_rejects_unknown_level():
    with pytest.raises(ValueError):
        channels.Alert(subject="s", body_text="b", level="catastrophe")


# --- channels -------------------------------------------------------------

def test_file_channel_writes_a_readable_record(tmp_path):
    sender = channels.FileSender(tmp_path)
    result = sender.send(channels.Alert("subj", "body text", level="critical"))
    assert result.ok
    written = json.loads(Path(result.detail).read_text(encoding="utf-8"))
    assert written["subject"] == "subj"
    assert written["level"] == "critical"


def test_broken_channel_cannot_suppress_the_file_record(tmp_path):
    """The whole point of fanning out: a webhook outage still leaves evidence."""

    class Exploding:
        name = "exploding"

        def send(self, alert):
            raise RuntimeError("webhook is down")

    results = channels.send(channels.Alert("subj", "body"),
                            [channels.FileSender(tmp_path), Exploding()])
    by_channel = {r.channel: r for r in results}
    assert by_channel["file"].ok
    assert not by_channel["exploding"].ok
    assert "webhook is down" in by_channel["exploding"].detail
    assert len(list(tmp_path.iterdir())) == 1


def test_build_senders_needs_complete_config(monkeypatch):
    """Half-configured email is dropped rather than silently doing nothing —
    an absent channel is easier to debug than a broken one."""
    for var in ("HELIOS_SLACK_WEBHOOK_URL", "RESEND_API_KEY", "HELIOS_SMTP_HOST",
                "HELIOS_ALERT_FROM", "HELIOS_ALERT_TO"):
        monkeypatch.delenv(var, raising=False)

    assert [s.name for s in channels.build_senders()] == ["file"]

    monkeypatch.setenv("RESEND_API_KEY", "re_test")  # recipients still missing
    assert [s.name for s in channels.build_senders()] == ["file"]

    monkeypatch.setenv("HELIOS_ALERT_FROM", "alerts@example.com")
    monkeypatch.setenv("HELIOS_ALERT_TO", "a@example.com, b@example.com")
    senders = channels.build_senders()
    assert [s.name for s in senders] == ["file", "resend"]
    assert senders[1].recipients == ["a@example.com", "b@example.com"]
