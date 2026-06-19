"""Unit tests for the premium API backends — format conversion + cost tracking.

The SDK calls themselves aren't exercised (no network); the risky part is the
OpenAI-style ⇄ Anthropic format conversion and the cost accounting that feeds the
daily escalation cap.
"""
import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.backends import (  # noqa: E402
    _messages_to_anthropic, _tools_to_anthropic, _price_for, _CostTracker,
    _with_cache_control,
    _anthropic_response_to_openai, build_api_backend, ClaudeAPIBackend, OpenAIBackend,
)


class TestToolConversion:
    def test_openai_tool_to_anthropic(self):
        tools = [{"type": "function", "function": {
            "name": "bash", "description": "run a shell command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}}]
        out = _tools_to_anthropic(tools)
        assert out == [{"name": "bash", "description": "run a shell command",
                        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]

    def test_missing_parameters_defaults_to_object(self):
        out = _tools_to_anthropic([{"function": {"name": "x"}}])
        assert out[0]["input_schema"] == {"type": "object", "properties": {}}


class TestMessageConversion:
    def test_system_pulled_out_user_wrapped(self):
        system, msgs = _messages_to_anthropic([
            {"role": "system", "content": "you are X"},
            {"role": "user", "content": "do the thing"},
        ])
        assert system == "you are X"
        assert msgs == [{"role": "user", "content": [{"type": "text", "text": "do the thing"}]}]

    def test_assistant_tool_call_becomes_tool_use_block(self):
        _, msgs = _messages_to_anthropic([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "running it",
             "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}]},
        ])
        a = msgs[1]
        assert a["role"] == "assistant"
        assert {"type": "text", "text": "running it"} in a["content"]
        tu = [b for b in a["content"] if b["type"] == "tool_use"][0]
        assert tu == {"type": "tool_use", "id": "c1", "name": "bash", "input": {"cmd": "ls"}}

    def test_consecutive_tool_results_merge_into_one_user_turn(self):
        # The agent loop appends one role:tool message per call; Anthropic needs
        # them merged into a single user turn to preserve alternation.
        _, msgs = _messages_to_anthropic([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out1"},
            {"role": "tool", "tool_call_id": "c2", "content": "out2"},
        ])
        # one assistant turn, then ONE merged user turn with two tool_result blocks
        assert msgs[0]["role"] == "assistant"
        assert msgs[1]["role"] == "user"
        results = [b for b in msgs[1]["content"] if b["type"] == "tool_result"]
        assert {r["tool_use_id"] for r in results} == {"c1", "c2"}

    def test_roleless_assistant_turn_is_not_dropped(self):
        # REGRESSION (2026-06-18 premium-escalation incident): the agent loop
        # appends the raw backend response, which has NO "role" key. The
        # converter must treat it as an assistant turn — otherwise the tool_use
        # is dropped and the following tool_result orphans into the user turn,
        # producing Anthropic 400 "unexpected tool_use_id in tool_result".
        _, msgs = _messages_to_anthropic([
            {"role": "user", "content": "go"},
            # no "role" — exactly what backend() returns + agent_loop appends
            {"content": "running it",
             "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
             "finish_reason": "tool_calls"},
            {"role": "tool", "tool_call_id": "c1", "content": "out1"},
        ])
        # Must be: user(text) → assistant(tool_use) → user(tool_result).
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        # The tool_use block survives in the assistant turn …
        assert any(b["type"] == "tool_use" and b["id"] == "c1"
                   for b in msgs[1]["content"])
        # … and the tool_result lands in its OWN user turn, correctly paired —
        # NOT merged into msgs[0] (the bug signature: msgs[0].content[1]).
        assert len(msgs[0]["content"]) == 1
        assert msgs[2]["content"][0]["type"] == "tool_result"
        assert msgs[2]["content"][0]["tool_use_id"] == "c1"

    def test_bad_json_arguments_degrade_to_empty_input(self):
        _, msgs = _messages_to_anthropic([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{not json"}}]},
        ])
        tu = [b for b in msgs[0]["content"] if b["type"] == "tool_use"][0]
        assert tu["input"] == {}


class TestPromptCaching:
    def test_cache_control_on_system_and_conversation_tail(self):
        system, amsgs = _messages_to_anthropic([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            {"content": "ok", "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
        ])
        sp, am = _with_cache_control(system, amsgs)
        # system → a single cached text block (not a bare string)
        assert sp == [{"type": "text", "text": "sys",
                       "cache_control": {"type": "ephemeral"}}]
        # exactly one message-side breakpoint, on the last block of the last turn
        assert am[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        # earlier blocks stay unmarked — a single tail breakpoint is enough
        assert "cache_control" not in am[0]["content"][0]

    def test_no_system_and_empty_messages(self):
        sp, am = _with_cache_control("", [])
        assert sp is None
        assert am == []


class TestCostTracking:
    def test_price_lookup_by_prefix(self):
        assert _price_for("claude-opus-4-8") == (7.5, 37.5)  # 0.5x list (calibrated)
        assert _price_for("gpt-4o") == (2.5, 10.0)
        assert _price_for("totally-unknown-model")  # falls back, non-zero

    def test_record_accumulates_tokens_and_cost(self):
        t = _CostTracker("claude-opus-4-8")
        t._record(1_000_000, 1_000_000)   # 1M in, 1M out → $7.5 + $37.5
        assert t.total_input_tokens == 1_000_000
        assert t.total_output_tokens == 1_000_000
        assert abs(t.total_cost_usd - 45.0) < 1e-6
        assert t.call_count == 1

    def test_record_cache_pricing(self):
        # Cache WRITE bills 1.25x base input, cache READ 0.10x. Opus = (7.5, 37.5)/M.
        t = _CostTracker("claude-opus-4-8")
        t._record(1_000_000, 1_000_000, cache_read=1_000_000, cache_write=1_000_000)
        # 7.5 (uncached in) + 9.375 (write) + 0.75 (read) + 37.5 (out) = 55.125
        assert abs(t.total_cost_usd - 55.125) < 1e-6
        # all input-side tokens (uncached + read + write) roll into total_input_tokens
        assert t.total_input_tokens == 3_000_000
        assert t.total_output_tokens == 1_000_000

    def test_cache_read_is_cheaper_than_uncached(self):
        # A cached re-send costs 0.10x what the same tokens cost uncached — the
        # whole point of caching the multi-turn prefix.
        uncached = _CostTracker("claude-opus-4-8"); uncached._record(500_000, 0)
        cached = _CostTracker("claude-opus-4-8"); cached._record(0, 0, cache_read=500_000)
        assert abs(cached.total_cost_usd - uncached.total_cost_usd * 0.10) < 1e-6


class TestAnthropicResponseParsing:
    def test_text_and_tool_use_blocks(self):
        class _Block:
            def __init__(self, **kw): self.__dict__.update(kw)
        class _Resp:
            content = [_Block(type="text", text="hi "),
                       _Block(type="tool_use", id="c9", name="bash", input={"cmd": "ls"})]
            stop_reason = "tool_use"
        out = _anthropic_response_to_openai(_Resp())
        assert out["content"] == "hi "
        assert out["finish_reason"] == "tool_use"
        assert out["tool_calls"][0]["function"]["name"] == "bash"
        assert out["tool_calls"][0]["function"]["arguments"] == '{"cmd": "ls"}'


class TestFactory:
    def test_build_known_backends(self):
        assert isinstance(build_api_backend("claude-api", "claude-opus-4-8", "k"), ClaudeAPIBackend)
        assert isinstance(build_api_backend("openai", "gpt-4o", "k"), OpenAIBackend)

    def test_unknown_kind_raises(self):
        import pytest
        with pytest.raises(ValueError):
            build_api_backend("nope", "m", "k")
