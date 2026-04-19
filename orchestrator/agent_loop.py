"""
Backend-agnostic tool-use loop for agentic sessions.

Extracted from ollama_agent.py so future backends (Claude API, OpenAI-compatible
gateways, etc.) can reuse the same loop without duplicating the message /
tool-dispatch plumbing.

Design:
  - A `Backend` is a callable that takes the current message list + the tool
    spec and returns one AssistantMessage. It is responsible for HTTP, retries,
    model selection, and parsing the model's response into a dict with keys
    {role, content, tool_calls, finish_reason}.
  - A `ToolDispatcher` is a callable `(name, args) -> (result_text, is_done)`.
  - `AgentLoop.run(initial_prompt)` runs turns until the dispatcher signals done
    (via a tool named "task_done" returning is_done=True), or max_turns elapses.

The loop is deliberately small (<200 LOC). It exists to make it obvious where
state lives — which in turn makes migrating to Claude Agent SDK or LangGraph a
surgical change rather than a rewrite.
"""
from __future__ import annotations

import json
from typing import Callable, Protocol


class Backend(Protocol):
    """A single-turn chat backend.

    Called once per loop turn. Must return a dict with these keys:
      - content       : str (model's visible output)
      - tool_calls    : list of {id, function: {name, arguments(JSON-str)}}
      - finish_reason : str (e.g. "stop", "tool_calls", "length")

    Exceptions raised here abort the loop — the backend is responsible for
    internal retries (e.g. retry on HTTP 500).
    """

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict: ...


ToolDispatcher = Callable[[str, dict], tuple[str, bool]]
"""(tool_name, tool_args) -> (result_text_for_model, is_session_done)."""


class AgentLoop:
    """Turn-based tool-use loop. Stateless between `run()` calls."""

    def __init__(
        self,
        backend: Backend,
        tool_specs: list[dict],
        dispatcher: ToolDispatcher,
        *,
        max_turns: int = 80,
        system_prompt: str = "",
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.backend = backend
        self.tool_specs = tool_specs
        self.dispatcher = dispatcher
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        self.log = log

    def run(self, initial_prompt: str) -> int:
        """Run the loop until done or max_turns.

        Return codes:
          0 — dispatcher signalled done (task_done)
          1 — backend error (exception) or max_turns exceeded
          2 — model stopped without calling any tool (incomplete)
        """
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": initial_prompt})

        for turn in range(1, self.max_turns + 1):
            self.log(f"Turn {turn}/{self.max_turns}")

            try:
                message = self.backend(messages, self.tool_specs)
            except Exception as e:
                self.log(f"ERROR: backend call failed: {e}")
                return 1

            messages.append(message)

            # Surface assistant reasoning for log readers.
            for line in (message.get("content") or "").split("\n"):
                if line.strip():
                    self.log(f"  > {line}")

            tool_calls = message.get("tool_calls") or []
            finish_reason = message.get("finish_reason", "")

            if not tool_calls:
                if finish_reason in ("stop", "end_turn", ""):
                    self.log("Agent finished without calling task_done — exit=2 (incomplete)")
                    return 2
                self.log(f"No tool calls and finish_reason={finish_reason!r} — exit=2 (incomplete)")
                return 2

            # Dispatch each tool call and collect results.
            session_done = False
            for tc in tool_calls:
                fn = tc.get("function", tc)
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}

                try:
                    result_text, is_done = self.dispatcher(name, args)
                except Exception as e:
                    # Don't abort the loop — surface the error back to the model
                    # as a tool result so it can decide whether to retry.
                    result_text, is_done = (f"TOOL ERROR: {e}", False)
                    self.log(f"  tool {name!r} raised: {e}")

                if is_done:
                    session_done = True

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{turn}_{name}"),
                    "content": result_text,
                })

            if session_done:
                self.log("Session completed via task_done")
                return 0

        self.log(f"Reached max turns ({self.max_turns}) without completing — exit=1")
        return 1
