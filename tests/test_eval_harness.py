"""Tests for the eval harness itself (evals/runner.py, scoring.py, backends.py).

These run as part of the regular `pytest tests/` suite — they don't require
RUN_LIVE_EVALS and don't call any external LLM. The point is to keep the
harness honest: if we ever break the runner or a scorer, the change should
fail CI before it ships.

We use StubBackend + the existing scenarios under evals/scenarios/ as the
fixture corpus. Each scenario already carries a `stub_response` that the
StubBackend replays, so we can run the full pipeline end-to-end with zero
network and zero tokens.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from evals.backends import StubBackend, OllamaBackend, make_backend_from_env
from evals.scoring import (
    SCORERS,
    check_contains_substring,
    check_does_not_contain,
    check_matches_regex,
    check_valid_json,
    check_designer_ac_structure,
    check_coder_session_summary_blocks,
    check_reviewer_decision,
)
from evals.runner import (
    load_scenarios, run_scenario, run_all, write_report, SCENARIO_DIR,
)


# ── backends ────────────────────────────────────────────────────────────────


class TestStubBackend:
    def test_first_matching_rule_wins(self):
        b = StubBackend(rules=[("hello", "A"), ("world", "B")], default="D")
        assert b.call("say hello world") == "A"
        assert b.call("only world here") == "B"
        assert b.call("nothing matches") == "D"

    def test_empty_rules_returns_default(self):
        b = StubBackend(rules=[], default="X")
        assert b.call("anything") == "X"


class TestBackendFactory:
    def test_explicit_stub(self, monkeypatch):
        monkeypatch.setenv("EVAL_BACKEND", "stub")
        b = make_backend_from_env()
        assert b.name == "stub"

    def test_default_falls_back_to_stub_without_ollama_host(self, monkeypatch):
        monkeypatch.delenv("EVAL_BACKEND", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        b = make_backend_from_env()
        assert b.name == "stub"

    def test_default_picks_ollama_when_host_set(self, monkeypatch):
        monkeypatch.delenv("EVAL_BACKEND", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "http://x:11434")
        b = make_backend_from_env()
        assert b.name == "ollama"

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("EVAL_BACKEND", "bogus")
        with pytest.raises(ValueError, match="bogus"):
            make_backend_from_env()


# ── scoring primitives ──────────────────────────────────────────────────────


class TestScoringPrimitives:
    def test_contains_substring(self):
        assert check_contains_substring("hello world", needle="world").passed
        assert not check_contains_substring("hello", needle="world").passed

    def test_does_not_contain(self):
        assert check_does_not_contain("hello", needle="assert True").passed
        assert not check_does_not_contain("assert True", needle="assert True").passed

    def test_matches_regex(self):
        r = check_matches_regex("AC1. foo", pattern=r"AC\d+")
        assert r.passed
        assert r.data["match"] == "AC1"

    def test_valid_json(self):
        assert check_valid_json('{"a": 1}').passed
        assert not check_valid_json("not json").passed


class TestDesignerACStructure:
    def test_well_formed_doc_passes(self):
        doc = (
            "AC1. one.\n     Verify: `echo OK`\n     Expected: `OK`\n     Test: t1.\n"
            "AC2. two.\n     Verify: `echo a`\n     Expected: `a`\n     Test: t2.\n"
            "AC3. three.\n     Verify: `echo b`\n     Expected: `b`\n     Test: t3.\n"
        )
        r = check_designer_ac_structure(doc, min_acs=3, max_acs=4)
        assert r.passed
        assert r.data["ac_count"] == 3
        assert r.data["verify_count"] == 3

    def test_too_many_acs_fails(self):
        doc = "\n".join(f"AC{i}. body\n     Verify: `x`\n     Expected: `y`\n" for i in range(1, 7))
        r = check_designer_ac_structure(doc, min_acs=1, max_acs=4)
        assert not r.passed
        assert r.data["ac_count"] == 6

    def test_missing_verify_fails(self):
        doc = (
            "AC1. one.\n     Test: t1.\n"
            "AC2. two.\n     Test: t2.\n"
        )
        r = check_designer_ac_structure(doc, min_acs=1, max_acs=4)
        assert not r.passed
        assert r.data["verify_count"] == 0


class TestCoderSessionSummaryBlocks:
    def test_present_blocks_pass(self):
        text = (
            "Some narration.\n"
            "## AC1 verification:\nOK\n\n"
            "## AC2 verification:\nMATCH\n"
        )
        r = check_coder_session_summary_blocks(text, min_blocks=2)
        assert r.passed
        assert r.data["ac_numbers"] == [1, 2]

    def test_missing_blocks_fail(self):
        r = check_coder_session_summary_blocks("no blocks here", min_blocks=1)
        assert not r.passed

    def test_legacy_empirical_check_format_also_counts(self):
        text = "## AC1 empirical check:\nOK\n"
        r = check_coder_session_summary_blocks(text, min_blocks=1)
        assert r.passed


class TestReviewerDecision:
    def test_lgtm_approve(self):
        r = check_reviewer_decision(
            "✅ Commit abc: LGTM — pass. [uid]",
            expected="approved",
        )
        assert r.passed

    def test_reject_with_x(self):
        r = check_reviewer_decision(
            "❌ Functional: route mismatch on AC1.",
            expected="changes_requested",
        )
        assert r.passed

    def test_mixed_signals_fail_approve(self):
        # Has both ✅ and ❌ — ambiguous, should not count as clean approve.
        r = check_reviewer_decision(
            "✅ LGTM but also ❌ minor nit",
            expected="approved",
        )
        assert not r.passed


# ── end-to-end runner ──────────────────────────────────────────────────────


class TestRunnerEndToEnd:
    def test_load_scenarios_returns_nonempty(self):
        scenarios = load_scenarios()
        assert len(scenarios) >= 4, "expected the seed scenarios to be present"
        for s in scenarios:
            assert "id" in s
            assert "checks" in s
            assert "stub_response" in s, (
                f"scenario {s['id']!r} missing stub_response; the harness's "
                f"unit tests rely on it for token-free runs."
            )

    def test_run_scenario_with_stub_response(self):
        # Build a synthetic scenario inline so this test is independent of
        # the seed scenario set.
        scenario = {
            "id": "test_inline_scenario",
            "persona": "designer",
            "product_overrides": {"name": "TestProd"},
            "assigned_features": [
                {"id": 1, "name": "test feat", "status": "Approved"},
            ],
            "stub_response": (
                "AC1. one.\n     Verify: `echo OK`\n     Expected: `OK`\n     Test: t1.\n"
                "AC2. two.\n     Verify: `echo a`\n     Expected: `a`\n     Test: t2.\n"
            ),
            "checks": [
                {"scorer": "designer_ac_structure",
                 "args": {"min_acs": 1, "max_acs": 4}, "weight": 1.0},
                {"scorer": "contains_substring",
                 "args": {"needle": "AC1"}, "weight": 1.0},
            ],
        }
        result = run_scenario(scenario)
        assert result.error is None, f"unexpected error: {result.error}"
        assert result.passed, f"checks failed: {result.checks}"
        assert result.backend == "stub"
        assert result.total_score == result.max_score

    def test_run_scenario_failing_check_reports_clearly(self):
        scenario = {
            "id": "test_failing",
            "persona": "designer",
            "stub_response": "no AC headings at all",
            "checks": [
                {"scorer": "designer_ac_structure",
                 "args": {"min_acs": 1, "max_acs": 4}, "weight": 1.0},
            ],
        }
        result = run_scenario(scenario)
        assert not result.passed
        assert result.checks[0]["passed"] is False
        assert "0 AC(s)" in result.checks[0]["detail"]

    def test_seed_scenarios_all_pass_with_stub(self):
        """Sanity: the bundled scenarios each pass their own checks when
        replayed via StubBackend. If a seed scenario fails on its own
        stub_response, the scenario is mis-authored — the stub response
        should always satisfy the scorers it ships with."""
        for scenario in load_scenarios():
            r = run_scenario(scenario)
            assert r.passed, (
                f"seed scenario {scenario['id']} failed on its own stub_response.\n"
                f"Failing checks: "
                + "\n".join(
                    f"  - {c['scorer']}: {c['detail']}"
                    for c in r.checks if not c.get("passed")
                )
            )

    def test_write_report_produces_consumable_json(self, tmp_path):
        results = run_all()
        out = tmp_path / "report.json"
        write_report(results, out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert "summary" in payload and "scenarios" in payload
        assert payload["summary"]["total"] == len(results)
        # Schema fields the compare tool depends on.
        for s in payload["scenarios"]:
            assert {"scenario_id", "passed", "score_pct"}.issubset(s)


class TestUnknownScorerHandled:
    def test_unknown_scorer_records_failure_does_not_crash(self):
        scenario = {
            "id": "test_unknown_scorer",
            "persona": "designer",
            "stub_response": "AC1. x.\n     Verify: `echo OK`\n     Expected: `OK`\n",
            "checks": [
                {"scorer": "not_a_real_scorer", "args": {}, "weight": 1.0},
            ],
        }
        r = run_scenario(scenario)
        assert not r.passed
        assert "unknown scorer" in r.checks[0]["detail"]


class TestCompareTool:
    """compare.py is a CLI; smoke test its core function returns the right
    exit code for the regress / improve / unchanged cases."""

    def test_compare_no_regression_returns_zero(self, tmp_path):
        from evals.compare import compare
        base = {"version": 1, "summary": {"score_pct": 80.0},
                "scenarios": [
                    {"scenario_id": "a", "passed": True, "score_pct": 80.0},
                ]}
        cand = {"version": 1, "summary": {"score_pct": 90.0},
                "scenarios": [
                    {"scenario_id": "a", "passed": True, "score_pct": 90.0},
                ]}
        bp, cp = tmp_path / "b.json", tmp_path / "c.json"
        bp.write_text(json.dumps(base))
        cp.write_text(json.dumps(cand))
        assert compare(bp, cp) == 0

    def test_compare_regression_returns_one(self, tmp_path):
        from evals.compare import compare
        base = {"version": 1, "summary": {"score_pct": 80.0},
                "scenarios": [
                    {"scenario_id": "a", "passed": True, "score_pct": 80.0},
                ]}
        cand = {"version": 1, "summary": {"score_pct": 40.0},
                "scenarios": [
                    {"scenario_id": "a", "passed": False, "score_pct": 40.0},
                ]}
        bp, cp = tmp_path / "b.json", tmp_path / "c.json"
        bp.write_text(json.dumps(base))
        cp.write_text(json.dumps(cand))
        assert compare(bp, cp) == 1
