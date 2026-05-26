"""LLM-infra exhaustion (exit code 43) path — quota/auth/network failures
must not push features to Blocked.

Real incident: MyTracking 2026-05-22 lost 47 features to Blocked when 106
Ollama-Cloud 429s ground every designer/coder session through 5 fix_attempts
each. The fix is symmetric to the existing exit-42 (pf-verify-env) path:
LLMInfraExhausted → loop returns 43 → _finalize_session rolls back without
charging fix_attempts → detect_kill_recovery short-circuits to 0.

Tests are pure-function (no DB, no Docker, no real LLM). Matches the style
of test_agent_loop_hardening.py.
"""

import os

import pytest


os.environ.setdefault("PM_API_URL", "http://pm-api:8080")


# ──────────────────────────────────────────────────────────────────────────────
# LLMInfraExhausted exception
# ──────────────────────────────────────────────────────────────────────────────

class TestLLMInfraExhausted:
    def test_carries_category(self):
        from orchestrator.ollama_agent import LLMInfraExhausted
        e = LLMInfraExhausted("quota hit", category="quota")
        assert e.category == "quota"
        assert "quota hit" in str(e)

    def test_default_category_is_unknown(self):
        from orchestrator.ollama_agent import LLMInfraExhausted
        e = LLMInfraExhausted("no info")
        assert e.category == "unknown"

    def test_subclasses_runtime_error(self):
        """So existing `except RuntimeError` catches in callers still work,
        but new `except LLMInfraExhausted` catches can distinguish."""
        from orchestrator.ollama_agent import LLMInfraExhausted
        assert issubclass(LLMInfraExhausted, RuntimeError)


# ──────────────────────────────────────────────────────────────────────────────
# AgentLoop returns 43 on LLMInfraExhausted, 1 on everything else
# ──────────────────────────────────────────────────────────────────────────────

class TestAgentLoopExit43:
    def test_returns_43_on_llm_infra_exhausted(self):
        from orchestrator.agent_loop import AgentLoop
        from orchestrator.ollama_agent import LLMInfraExhausted

        def backend(messages, tools):
            raise LLMInfraExhausted("Ollama failed across all 2 models", category="quota")

        loop = AgentLoop(
            backend=backend,
            tool_specs=[],
            dispatcher=lambda name, args: ("", False),
            max_turns=3,
        )
        assert loop.run("any prompt") == 43

    def test_returns_1_on_generic_runtime_error(self):
        """Generic backend errors keep exit-1 semantics so we don't accidentally
        release fix_attempts for non-infra bugs."""
        from orchestrator.agent_loop import AgentLoop

        def backend(messages, tools):
            raise RuntimeError("model returned malformed JSON")

        loop = AgentLoop(
            backend=backend,
            tool_specs=[],
            dispatcher=lambda name, args: ("", False),
            max_turns=3,
        )
        assert loop.run("any prompt") == 1


# ──────────────────────────────────────────────────────────────────────────────
# detect_kill_recovery short-circuits on exit 43
# ──────────────────────────────────────────────────────────────────────────────

class TestSupervisorGateExit43:
    def test_exit_43_returns_zero_without_db_call(self):
        """Must early-return BEFORE the httpx client opens. If it didn't,
        this test would crash trying to hit the PM API."""
        from orchestrator.supervisor import detect_kill_recovery
        touched = detect_kill_recovery(
            product_id=999,
            session_uid="test-uid",
            persona="designer",
            exit_code=43,
            assigned_features=[{"id": 1}, {"id": 2}, {"id": 3}],
        )
        assert touched == 0

    def test_exit_42_still_returns_zero(self):
        """Regression guard: the existing env-not-ready path must keep working."""
        from orchestrator.supervisor import detect_kill_recovery
        assert detect_kill_recovery(
            product_id=999,
            session_uid="test-uid",
            persona="coder",
            exit_code=42,
            assigned_features=[{"id": 1}],
        ) == 0

    def test_exit_zero_returns_zero(self):
        """Successes are owned by detect_false_success, not kill_recovery."""
        from orchestrator.supervisor import detect_kill_recovery
        assert detect_kill_recovery(
            product_id=999,
            session_uid="test-uid",
            persona="coder",
            exit_code=0,
            assigned_features=[{"id": 1}],
        ) == 0
