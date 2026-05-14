"""Pure-function tests for the agent-loop hardening fixes (#3, #6, #7).

Three pure functions, each unit-tested without spinning up a docker
process or a backend call:

  #3  orchestrator.docker_runner._select_end_status
      → 3-way FSM classifier for session finalization.
  #6  orchestrator.ollama_agent._try_parse_tool_calls_from_content
      → tool-call shape repair including the two new patterns added
        for gpt-oss:120b's inner-args output (session 2438).
  #7  orchestrator.ollama_agent._detect_hallucinated_tool_results
      → orphan-{stdout,returncode} guard (session 2449).

The PATCH /api/sessions/{id} status-downgrade guard (also part of #3)
is covered by an integration test in tests/test_website.py.
"""

import json
import os

import pytest


# ── Module-level env shim: docker_runner reads PM_API_URL at import time ──
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")


# ──────────────────────────────────────────────────────────────────────────────
# #3 — _select_end_status
# ──────────────────────────────────────────────────────────────────────────────

class TestSelectEndStatus:
    """3-way FSM classifier — ended / lost / killed."""

    def test_clean_exit_with_terminal_marker_is_ended(self):
        from orchestrator.docker_runner import _select_end_status
        assert _select_end_status(exit_code=0, terminal_marker_seen=True) == "ended"

    def test_clean_exit_without_terminal_marker_is_lost(self):
        """Container exited 0 but the agent never declared done — the
        2406/2418 case. Must be `lost`, not `ended`, so success-rate
        metrics aren't silently inflated and the reconciler can retry."""
        from orchestrator.docker_runner import _select_end_status
        assert _select_end_status(exit_code=0, terminal_marker_seen=False) == "lost"

    def test_nonzero_exit_is_killed_regardless_of_marker(self):
        from orchestrator.docker_runner import _select_end_status
        assert _select_end_status(exit_code=1,   terminal_marker_seen=True)  == "killed"
        assert _select_end_status(exit_code=1,   terminal_marker_seen=False) == "killed"
        assert _select_end_status(exit_code=137, terminal_marker_seen=False) == "killed"  # SIGKILL
        assert _select_end_status(exit_code=-1,  terminal_marker_seen=False) == "killed"  # watchdog


# ──────────────────────────────────────────────────────────────────────────────
# #7 — _detect_hallucinated_tool_results
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectHallucinatedToolResults:
    """Orphan tool-result-shaped JSON in assistant content (session 2449)."""

    def test_detects_stdout_returncode_in_natural_order(self):
        from orchestrator.ollama_agent import _detect_hallucinated_tool_results
        content = (
            "Running the test:\n"
            '{"stdout": "hello\\nworld", "returncode": 0}\n'
            "Looks good."
        )
        assert _detect_hallucinated_tool_results(content) is not None

    def test_detects_returncode_before_stdout(self):
        """Key order shouldn't matter — both arrangements are hallucination."""
        from orchestrator.ollama_agent import _detect_hallucinated_tool_results
        content = 'Sure — {"returncode": 0, "stdout": "output"}'
        assert _detect_hallucinated_tool_results(content) is not None

    def test_ignores_well_formed_tool_call_wrapper(self):
        """Real <tool_call> blocks are repaired separately — don't
        false-positive on them. After stripping <tool_call>...</tool_call>,
        the orphan keys must still be present for hallucination to fire."""
        from orchestrator.ollama_agent import _detect_hallucinated_tool_results
        content = (
            '<tool_call>{"name":"bash","arguments":'
            '{"command":"echo hi","stdout_capture":true}}</tool_call>'
            # ↑ "stdout_capture" mentioning stdout is irrelevant; both keys
            # must coexist outside any wrapper.
        )
        assert _detect_hallucinated_tool_results(content) is None

    def test_ignores_natural_prose_mentioning_keys(self):
        """Reasoning that *mentions* stdout / returncode as identifiers in
        a sentence (not as JSON) should not trip the detector."""
        from orchestrator.ollama_agent import _detect_hallucinated_tool_results
        content = (
            "subprocess.run() returns a CompletedProcess with stdout and "
            "returncode attributes. We'll inspect both after the command runs."
        )
        assert _detect_hallucinated_tool_results(content) is None

    def test_ignores_only_one_of_the_keys(self):
        from orchestrator.ollama_agent import _detect_hallucinated_tool_results
        assert _detect_hallucinated_tool_results('{"stdout": "x"}') is None
        assert _detect_hallucinated_tool_results('{"returncode": 0}') is None

    def test_ignores_plain_text(self):
        from orchestrator.ollama_agent import _detect_hallucinated_tool_results
        assert _detect_hallucinated_tool_results("Plain text, no tool reference.") is None


# ──────────────────────────────────────────────────────────────────────────────
# #6 — _try_parse_tool_calls_from_content (Patterns 3 + 4)
# ──────────────────────────────────────────────────────────────────────────────

class TestToolCallShapeRepair:
    """The two new patterns added for gpt-oss:120b inner-args (#6).

    Existing Patterns 1 (<tool_call> tags) and 2 ({"name", "arguments"})
    are out of scope here — they were not changed.
    """

    def test_pattern_4_bare_bash_inner_args(self):
        """The session 2438 shape: {"command": "...", "cwd": "..."}."""
        from orchestrator.ollama_agent import _try_parse_tool_calls_from_content
        content = 'Let me run: {"command": "ls -la /workspace", "cwd": "/workspace"}'
        calls = _try_parse_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "bash"
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"command": "ls -la /workspace", "cwd": "/workspace"}

    def test_pattern_4_strict_keys_rejects_foreign_fields(self):
        """If the object has any key outside {command, cwd, timeout},
        Pattern 4 must reject it — otherwise we'd mis-wrap unrelated
        JSON that happens to mention "command" as a bash call."""
        from orchestrator.ollama_agent import _try_parse_tool_calls_from_content
        content = '{"command": "run", "unrelated_key": "value"}'
        calls = _try_parse_tool_calls_from_content(content)
        assert calls == []

    def test_pattern_4_requires_command_key(self):
        from orchestrator.ollama_agent import _try_parse_tool_calls_from_content
        content = '{"cwd": "/tmp", "timeout": 30}'   # no "command"
        calls = _try_parse_tool_calls_from_content(content)
        assert calls == []

    def test_pattern_3_tool_tool_input(self):
        from orchestrator.ollama_agent import _try_parse_tool_calls_from_content
        content = 'Try: {"tool": "read_file", "tool_input": {"path": "/x"}}'
        calls = _try_parse_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "read_file"
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"path": "/x"}

    def test_plain_text_returns_empty(self):
        from orchestrator.ollama_agent import _try_parse_tool_calls_from_content
        assert _try_parse_tool_calls_from_content("Just thoughts, no tool.") == []
