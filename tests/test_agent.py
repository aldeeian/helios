"""NL query agent tests. All offline (StubClient) — no API key or network.
The guardrail and injection tests are the security-critical ones and hold
regardless of which LLM generates the SQL."""

import datetime as dt

import numpy as np
import pytest
import sqlalchemy as sa

from src.agent import guardrails, nl_agent, semantic
from src.agent.evaluate import run as run_eval
from src.agent.executor import execute
from src.agent.golden_set import GOLDEN, INJECTION_CASES
from src.agent.llm import StubClient
from src.datagen import sources
from src.datagen.truth import simulate
from src.reconciliation import engine as recon
from src.warehouse import load as wh


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """A fully loaded warehouse in a temp DB for the agent to query."""
    truth = simulate(42)
    rng = np.random.default_rng(43)
    log = {}
    hr = sources.build_hr_frame(truth, rng, log)
    fin = sources.build_financials_frame(truth, rng, log)
    lookup = sources.build_lookup_frame(log)
    ctr = sources.build_contractor_frame(truth, rng, log)
    plan = truth.headcount_plan[["department", "month", "planned_fte"]]
    result = recon.run(hr, fin, ctr, lookup, plan_raw=plan)
    d = tmp_path_factory.mktemp("recon")
    for name, frame in [("employees", result.employees), ("contractors", result.contractors),
                        ("financials", result.financials), ("plan", result.plan)]:
        frame.to_csv(d / f"{name}.csv", index=False)
    eng = sa.create_engine(f"sqlite:///{(tmp_path_factory.mktemp('wh') / 'wh.db').as_posix()}")
    wh.load(d, eng, as_of=dt.date(2026, 1, 1))
    yield eng
    eng.dispose()


class TestGuardrails:
    @pytest.mark.parametrize("sql", [
        "SELECT SUM(actual_amount) FROM fact_spend",
        "SELECT * FROM vw_budget_vs_actual",
        "WITH t AS (SELECT * FROM fact_headcount) SELECT department_key FROM t",
    ])
    def test_allows_safe_selects(self, sql):
        out = guardrails.validate(sql)
        assert out.lower().lstrip().startswith(("select", "with"))
        assert "limit" in out.lower()

    @pytest.mark.parametrize("sql", [
        "DROP TABLE fact_spend",
        "DELETE FROM subscriptions",
        "UPDATE fact_spend SET actual_amount = 0",
        "INSERT INTO dim_date VALUES (1)",
        "ALTER TABLE dim_date ADD COLUMN x int",
        "SELECT * FROM fact_spend; DROP TABLE dim_date",
        "ATTACH DATABASE 'evil.db' AS e",
        "SELECT load_extension('x.so')",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM pg_shadow",
    ])
    def test_blocks_dangerous_sql(self, sql):
        with pytest.raises(guardrails.UnsafeQuery):
            guardrails.validate(sql)

    def test_adds_missing_limit(self):
        assert "limit 100" in guardrails.validate(
            "SELECT * FROM fact_spend", max_rows=100).lower()

    def test_caps_oversized_limit(self):
        out = guardrails.validate("SELECT * FROM fact_spend LIMIT 999999", max_rows=100)
        assert "limit 100" in out.lower() and "999999" not in out

    def test_respects_smaller_limit(self):
        out = guardrails.validate("SELECT * FROM fact_spend LIMIT 5", max_rows=100)
        assert "limit 5" in out.lower()


class TestExecutorReadOnly:
    def test_select_runs(self, engine):
        r = execute("SELECT COUNT(*) AS n FROM fact_spend", engine=engine)
        assert r.columns == ["n"] and r.rows[0][0] > 0

    def test_write_is_rejected_by_readonly(self, engine):
        # PRAGMA query_only must make even a raw write fail at the DB layer.
        with pytest.raises(Exception):
            execute("UPDATE fact_spend SET actual_amount = 0", engine=engine)

    def test_fetch_cap_truncates(self, engine):
        r = execute("SELECT * FROM fact_spend", engine=engine, fetch_cap=5)
        assert len(r.rows) == 5 and r.truncated


class TestSemantic:
    def test_prompt_lists_tables_and_examples(self):
        p = semantic.schema_prompt()
        assert "fact_headcount" in p and "fact_spend" in p
        assert "point-in-time" in p          # the semi-additive warning
        assert "Example questions" in p

    def test_allowlist_covers_tables_and_views(self):
        assert "fact_spend" in semantic.ALLOWED_TABLES
        assert "vw_budget_vs_actual" in semantic.ALLOWED_TABLES


class TestAgentLoop:
    def test_answers_a_question(self, engine):
        ans = nl_agent.ask("Total actual spend in December 2025?",
                           client=StubClient(), engine=engine)
        assert ans.ok
        assert ans.sql.lower().lstrip().startswith("select")
        assert ans.tables_used  # grounding recorded
        assert ans.rows

    def test_self_corrects_on_bad_first_sql(self, engine):
        """A client that returns junk first, then a valid query, must recover."""
        class FlakyClient:
            name = "flaky"
            def __init__(self):
                self.calls = 0
            def complete(self, system, user):
                self.calls += 1
                if self.calls == 1:
                    return "```sql\nSELECT * FROM nonexistent_table\n```"
                return "```sql\nSELECT SUM(actual_amount) AS t FROM fact_spend\n```"
        ans = nl_agent.ask("total spend", client=FlakyClient(), engine=engine)
        assert ans.ok and ans.attempts == 2

    def test_gives_up_safely_after_max_attempts(self, engine):
        class BadClient:
            name = "bad"
            def complete(self, system, user):
                return "```sql\nDROP TABLE fact_spend\n```"
        ans = nl_agent.ask("delete everything", client=BadClient(),
                           engine=engine, max_attempts=2)
        assert not ans.ok and ans.sql is None and "blocked" in ans.error


class TestInjectionResistance:
    @pytest.mark.parametrize("q", INJECTION_CASES)
    def test_injection_never_executes_mutation(self, engine, q):
        ans = nl_agent.ask(q, client=StubClient(), engine=engine, max_attempts=1)
        # Safe = no successful mutating SQL. Either it errored, or the SQL that
        # ran was a plain SELECT.
        if ans.ok:
            assert ans.sql.lower().lstrip().startswith("select")


class TestGoldenSet:
    def test_offline_stub_scores_100pct(self, engine):
        """The offline stub is built to satisfy the golden shapes — this guards
        the harness plumbing end to end. Real-Claude accuracy is measured
        separately when a key is present."""
        report = run_eval(client=StubClient(), engine=engine)
        assert report["total"] == len(GOLDEN)
        assert report["execution_accuracy"] == 1.0
        assert report["injection_block_rate"] == 1.0
