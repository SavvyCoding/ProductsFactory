"""Tests for AgentLoop context-window management (_window) — bounds the
re-sent history so per-turn input (and Ollama-Cloud token cost) stays flat
regardless of session length, without ever splitting a tool_call from its
result. See orchestrator/agent_loop.py.
"""
from orchestrator.agent_loop import AgentLoop, _ELISION_MARK


def _loop(budget=5000, keep=2):
    return AgentLoop(
        backend=lambda m, t: {"content": "", "tool_calls": [], "finish_reason": "stop"},
        tool_specs=[],
        dispatcher=lambda n, a: ("", False),
        window_token_budget=budget,
        keep_recent_exchanges=keep,
    )


def _exchange(i, result_chars=8000):
    """One assistant tool-call turn + its tool result."""
    asst = {"content": f"Step {i}: editing file_{i}.py",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}}],
            "finish_reason": "tool_calls"}
    tool = {"role": "tool", "tool_call_id": f"c{i}", "content": "X" * result_chars}
    return [asst, tool]


def _no_orphans(messages):
    """Every tool result must have a preceding assistant tool_call with its id."""
    seen_ids = set()
    for m in messages:
        if m.get("role") not in ("system", "user", "tool"):  # assistant
            for tc in (m.get("tool_calls") or []):
                seen_ids.add(tc.get("id"))
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in seen_ids:
                return False
    return True


SYS = {"role": "system", "content": "system prompt + tool schema"}
TASK = {"role": "user", "content": "TASK: implement feature 42"}


class TestWindow:
    def test_short_session_passes_through_unchanged(self):
        msgs = [SYS, TASK] + _exchange(1, 100) + _exchange(2, 100)
        assert _loop(budget=60000)._window(msgs) is msgs  # identity — untouched

    def test_disabled_when_budget_nonpositive(self):
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        assert _loop(budget=0)._window(msgs) is msgs

    def test_long_session_is_bounded(self):
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        loop = _loop(budget=5000, keep=2)
        out = loop._window(msgs)
        assert loop._est_total(out) < loop._est_total(msgs)        # actually shrank
        assert loop._est_total(out) <= loop.window_token_budget + 1500  # ~budget (+ marker/pinned)

    def test_pins_system_and_task(self):
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        out = _loop()._window(msgs)
        assert out[0] is SYS                       # system always first
        assert TASK in out                          # task anchor preserved

    def test_inserts_elision_marker(self):
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        out = _loop()._window(msgs)
        markers = [m for m in out if _ELISION_MARK in str(m.get("content", ""))]
        assert len(markers) == 1
        assert "file_" in markers[0]["content"]     # breadcrumb of evicted intents

    def test_never_orphans_tool_results(self):
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        out = _loop()._window(msgs)
        assert _no_orphans(out)

    def test_keeps_most_recent_exchanges_and_last_message(self):
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        out = _loop(keep=2)._window(msgs)
        assert out[-1] is msgs[-1]                  # newest tool result preserved
        assert any("Step 19" in str(m.get("content", "")) for m in out)  # last exchange kept

    def test_repeated_windowing_does_not_compound_markers(self):
        loop = _loop(budget=5000, keep=2)
        msgs = [SYS, TASK] + sum((_exchange(i) for i in range(20)), [])
        once = loop._window(msgs)
        twice = loop._window(once + _exchange(99))   # simulate next turn appended
        markers = [m for m in twice if _ELISION_MARK in str(m.get("content", ""))]
        assert len(markers) == 1                    # one marker, not two stacked
        assert _no_orphans(twice)


class TestWindowInRunLoop:
    def test_run_keeps_context_bounded_and_completes(self):
        sent_sizes = []
        sys_always_first = []

        def backend(messages, tools):
            sent_sizes.append(sum(len(str(m.get("content") or "")) for m in messages))
            sys_always_first.append(messages[0].get("role") == "system")
            t = backend.turn
            backend.turn += 1
            name = "read_file" if t < 12 else "task_done"
            return {"content": f"turn {t}", "finish_reason": "tool_calls",
                    "tool_calls": [{"id": f"c{t}", "function": {"name": name, "arguments": "{}"}}]}
        backend.turn = 0

        def dispatcher(name, args):
            return ("Y" * 8000, name == "task_done")  # big result each turn; done on task_done

        loop = AgentLoop(backend=backend, tool_specs=[], dispatcher=dispatcher,
                         max_turns=30, system_prompt="S" * 400,
                         window_token_budget=6000, keep_recent_exchanges=3)
        rc = loop.run("INITIAL TASK PROMPT")
        assert rc == 0                              # completed via task_done
        assert all(sys_always_first)                # system pinned on every call
        # Without windowing this grows ~8k/turn unbounded; with it, bounded.
        assert max(sent_sizes) <= 6000 * 4 + 6000   # ~budget in chars (+ tolerance)
