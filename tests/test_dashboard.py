"""Smoke test for the Power BI dashboard data extraction. A dashboard is
verified visually (see powerbi/screenshots), but this guards the data contract:
collect() returns every key the template consumes, and the HTML builds and
embeds valid JSON."""

import json

import pandas as pd
import pytest

from flows import steps


@pytest.fixture(scope="module", autouse=True)
def _warehouse_ready():
    steps.ensure_sources()
    steps.run_reconciliation()
    steps.load_warehouse()


def test_collect_has_all_sections():
    from powerbi import build_dashboard
    d = build_dashboard.collect()
    for key in ("kpis", "months", "headcount_total", "forecast", "top_variances",
                "dept_budget_actual", "decomposition", "spend_trend", "vacancy",
                "contractor_mix"):
        assert key in d, f"missing {key}"
    assert set(d["kpis"]) >= {"actual", "budget", "variance", "headcount", "month"}
    # decomposition reconciles: volume + mix + rate == total (within rounding)
    dec = d["decomposition"]
    assert abs((dec["volume_variance"] + dec["mix_variance"] + dec["rate_variance"])
               - dec["total_variance"]) < 1.0
    # forecast has history + forward window with a band
    assert len(d["forecast"]["fc_mean"]) == 6
    assert len(d["forecast"]["fc_lo"]) == len(d["forecast"]["fc_hi"]) == 6


def test_html_builds_with_valid_embedded_json(tmp_path, monkeypatch):
    from powerbi import build_dashboard
    data = build_dashboard.collect()
    payload = json.dumps(data, default=build_dashboard._json_default)
    # round-trips cleanly (no numpy types leak past the encoder)
    assert json.loads(payload)["kpis"]["month"]
    template = (build_dashboard.Path(build_dashboard.__file__).resolve().parent
                / "_dashboard_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/", payload)
    assert "__DATA__" not in html            # placeholder was substituted
    assert "<svg" not in html                # SVGs are built client-side, not in template
    assert html.strip().startswith("<!doctype html>")


class TestModelExport:
    """The CSV handoff into Power BI Desktop."""

    def test_integer_surrogate_keys_survive_export(self):
        """Regression: date_key is an int surrogate (202301), and a name-based
        date coercion read it as nanoseconds-since-epoch, collapsing every row
        to 1970-01-01 and silently breaking every join in the model."""
        from powerbi.export_model import _iso_dates

        df = pd.DataFrame({"date_key": [202301, 202302],
                           "month_start": pd.to_datetime(["2023-01-01", "2023-02-01"])})
        out = _iso_dates(df.copy())
        assert out["date_key"].tolist() == [202301, 202302]
        assert out["month_start"].tolist() == ["2023-01-01", "2023-02-01"]

    def test_export_writes_the_star_schema(self, tmp_path):
        from powerbi.export_model import export

        written = export(tmp_path)
        for table in ("dim_date", "dim_department", "fact_headcount", "fact_spend"):
            assert written[table] > 0
            assert (tmp_path / f"{table}.csv").exists()

        # Facts must still join to dimensions after the round-trip through CSV.
        facts = pd.read_csv(tmp_path / "fact_headcount.csv")
        dates = pd.read_csv(tmp_path / "dim_date.csv")
        assert set(facts["date_key"]) <= set(dates["date_key"])
