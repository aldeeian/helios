"""Orchestration-step tests. Target the plain step functions (Prefect-free) so
CI validates the pipeline logic without a workflow engine. A single smoke test
runs the real Prefect daily flow if Prefect is importable."""

import json

import pytest

from flows import steps


@pytest.fixture(scope="module", autouse=True)
def _pipeline_ready():
    """Ensure sources + reconciled data exist for the step tests."""
    steps.ensure_sources()
    steps.run_reconciliation()


class TestFreshness:
    def test_reports_existing_sources(self):
        r = steps.check_source_freshness(max_age_days=3650)
        assert r.ok
        assert not r.detail["missing"]
        assert r.detail["status"]["hr"]["exists"]

    def test_flags_missing(self, tmp_path, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "HR_XLSX", tmp_path / "nope.xlsx")
        r = steps.check_source_freshness()
        assert not r.ok
        assert "hr" in r.detail["missing"]


class TestQCGate:
    def test_passes_on_known_design_quarantines(self):
        r = steps.run_reconciliation()
        gate = steps.qc_gate(r.detail["scorecard"])
        assert gate.ok  # CC-500 (~20% of financials) sits under the 50% bar
        assert not gate.detail["breaches"]

    def test_blocks_catastrophic_rate(self):
        fake = {"rows_in": {"hr": 100}, "rows_quarantined": {"hr": 80}}
        gate = steps.qc_gate(fake, max_quarantine_rate=0.5)
        assert not gate.ok
        assert "hr" in gate.detail["breaches"]


class TestSteps:
    def test_reconciliation_writes_scorecard(self):
        from src import config
        r = steps.run_reconciliation()
        assert (config.RECONCILED_DIR / "scorecard.json").exists()
        assert r.detail["scorecard"]["identity_holds"]

    def test_load_warehouse_idempotent(self):
        r1 = steps.load_warehouse()
        r2 = steps.load_warehouse()
        assert r1.detail["row_counts"] == r2.detail["row_counts"]

    def test_alert_persists(self):
        path = steps.emit_alert("test", "body", level="info")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["subject"] == "test" and data["level"] == "info"
        path.unlink()


class TestPrefectFlow:
    def test_daily_flow_runs_end_to_end(self):
        pytest.importorskip("prefect")
        from flows.pipelines import daily_data_pipeline
        result = daily_data_pipeline()
        assert result["qc"]["breaches"] == []
        assert result["load"]["row_counts"]["fact_headcount"] > 0
