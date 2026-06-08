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
import os
from typing import Callable, Protocol

# Sentinel prefix marking a synthetic context-elision message so repeated
# windowing passes don't re-summarize their own markers.
_ELISION_MARK = "[context-elided]"


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
        window_token_budget: int | None = None,
        keep_recent_exchanges: int | None = None,
    ) -> None:
        self.backend = backend
        self.tool_specs = tool_specs
        self.dispatcher = dispatcher
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        self.log = log
        # Context-window management. The loop re-sends the full message history
        # every turn, so without bounding it the per-turn input — and, on
        # token-billed backends like Ollama Cloud, the cost — grows without
        # limit (observed: 1.9M cumulative input tokens in a single 67-turn
        # session). _window() keeps the pinned anchors (system + initial task)
        # plus the most recent exchanges, eliding the middle. 0/negative budget
        # disables windowing entirely (full history, legacy behavior).
        self.window_token_budget = (
            window_token_budget if window_token_budget is not None
            else int(os.environ.get("AGENT_CONTEXT_WINDOW_BUDGET", "60000"))
        )
        self.keep_recent_exchanges = (
            keep_recent_exchanges if keep_recent_exchanges is not None
            else int(os.environ.get("AGENT_KEEP_RECENT_EXCHANGES", "8"))
        )

    # ── Context-window management ────────────────────────────────────────────

    @staticmethod
    def _is_assistant(msg: dict) -> bool:
        """A backend (assistant) turn. Backend messages carry no explicit role
        (see Backend protocol) — anything that isn't system/user/tool is one."""
        return msg.get("role") not in ("system", "user", "tool")

    @staticmethod
    def _est_tokens(msg: dict) -> int:
        """Cheap token estimate (~4 chars/token) over content + any tool_calls.
        Good enough to budget against; avoids a tokenizer dependency."""
        n = len(str(msg.get("content") or ""))
        tc = msg.get("tool_calls")
        if tc:
            n += len(json.dumps(tc, default=str))
        return n // 4 + 4  # +4 per-message structural overhead

    def _est_total(self, messages: list[dict]) -> int:
        return sum(self._est_tokens(m) for m in messages)

    def _elision_marker(self, evicted: list[dict]) -> dict:
        """A single synthetic user message summarizing evicted turns — keeps the
        action breadcrumb (one line per evicted assistant turn) while dropping
        the bulky tool results. Skips prior markers so it doesn't compound."""
        intents: list[str] = []
        for m in evicted:
            if not self._is_assistant(m):
                continue
            first = (str(m.get("content") or "").strip().splitlines() or [""])[0]
            if first and not first.startswith(_ELISION_MARK):
                intents.append(f"- {first[:160]}")
        intents = intents[-15:]  # most recent breadcrumbs only
        body = (
            f"{_ELISION_MARK} {len(evicted)} earlier message(s) were trimmed to "
            f"stay within the context window."
        )
        if intents:
            body += " Recent actions before this point:\n" + "\n".join(intents)
        body += "\nRe-read any file whose current contents you need — older tool output was dropped."
        return {"role": "user", "content": body}

    def _window(self, messages: list[dict]) -> list[dict]:
        """Return a length-bounded view of `messages`. Pins the leading system
        prompt + the initial task (first user message), keeps whole recent
        (assistant + its tool/nudge) exchanges within the token budget, and
        replaces the evicted middle with one elision marker. Tool_call/result
        pairs are never split (eviction is by whole exchange). Best-effort:
        any failure returns the original list unchanged."""
        try:
            if self.window_token_budget <= 0:
                return messages
            if self._est_total(messages) <= self.window_token_budget:
                return messages

            # Pinned prefix = leading system (if present) + the first user
            # message (the task). Everything before/incl. the first user role.
            pin_end = 0
            for i, m in enumerate(messages):
                if m.get("role") == "user":
                    pin_end = i + 1
                    break
            else:
                return messages  # no task anchor found — don't risk it
            pinned = messages[:pin_end]
            tail = messages[pin_end:]
            if not tail:
                return messages

            # Group the tail into assistant-led exchanges. Leading non-assistant
            # messages (e.g. a prior elision marker) attach to the first group.
            groups: list[list[dict]] = []
            for m in tail:
                if self._is_assistant(m) or not groups:
                    groups.append([m])
                else:
                    groups[-1].append(m)

            budget = self.window_token_budget - self._est_total(pinned) - 256  # marker reserve
            kept_rev: list[list[dict]] = []
            running = 0
            for g in reversed(groups):
                gsize = sum(self._est_tokens(m) for m in g)
                if kept_rev and running + gsize > budget and len(kept_rev) >= self.keep_recent_exchanges:
                    break
                kept_rev.append(g)
                running += gsize
            kept = list(reversed(kept_rev))
            evicted_count = len(groups) - len(kept)
            if evicted_count <= 0:
                return messages  # nothing to gain

            evicted_msgs = [m for g in groups[:evicted_count] for m in g]
            result = list(pinned)
            result.append(self._elision_marker(evicted_msgs))
            for g in kept:
                result.extend(g)
            self.log(
                f"[window] trimmed {evicted_count} exchange(s) / "
                f"{len(evicted_msgs)} msg(s); ~{self._est_total(result)} tok kept "
                f"(budget {self.window_token_budget})"
            )
            return result
        except Exception as e:  # never break the loop over windowing
            self.log(f"[window] skipped (error: {e})")
            return messages

    def run(self, initial_prompt: str) -> int:
        """Run the loop until done or max_turns.

        Return codes:
          0  — dispatcher signalled done (task_done)
          1  — backend error (exception) or max_turns exceeded
          2  — model stopped without calling any tool (incomplete)
          43 — LLM-infra exhaustion (quota / auth / network / all models 5xx).
               Distinguished from 1 so docker_runner._finalize_session and
               supervisor.detect_kill_recovery can skip charging fix_attempts —
               the failure isn't the agent's fault, it's the backend. Imported
               lazily to keep this module backend-agnostic.
        """
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": initial_prompt})

        # Backend-agnostic import: only Ollama defines LLMInfraExhausted today,
        # but the convention (and the exit code) is generic. Any future backend
        # signalling infra exhaustion can raise the same class.
        try:
            from orchestrator.ollama_agent import LLMInfraExhausted
        except Exception:
            LLMInfraExhausted = None  # type: ignore[assignment]

        for turn in range(1, self.max_turns + 1):
            self.log(f"Turn {turn}/{self.max_turns}")

            # Bound the history before each call. Reassigned (not copied) so the
            # elision persists across turns — new messages append to the trimmed
            # list rather than the unbounded original.
            messages = self._window(messages)

            try:
                message = self.backend(messages, self.tool_specs)
            except Exception as e:
                if LLMInfraExhausted is not None and isinstance(e, LLMInfraExhausted):
                    self.log(
                        f"ERROR: LLM infrastructure exhausted "
                        f"(category={getattr(e, 'category', 'unknown')}): {e}"
                    )
                    return 43
                self.log(f"ERROR: backend call failed: {e}")
                return 1

            # Hallucination guard — catches the pattern where the assistant
            # emits a fake tool result (e.g. {"stdout": "...", "returncode": 0})
            # inside its own content as if it had already run a tool. Without
            # this, appending the message to history lets the model "continue"
            # the conversation against imagined results, burning many pseudo-
            # turns. Only fires when there's no real tool call to dispatch —
            # if the model called a real tool and just *also* monologued
            # weirdly, the dispatch path handles it.
            from orchestrator.ollama_agent import _detect_hallucinated_tool_results
            content = message.get("content") or ""
            native_tool_calls = message.get("tool_calls") or []
            if not native_tool_calls:
                halluc_reason = _detect_hallucinated_tool_results(content)
                if halluc_reason:
                    self.log(
                        f"Hallucinated tool result detected — exit=2 "
                        f"(incomplete). Reason: {halluc_reason}"
                    )
                    return 2

            messages.append(message)

            # Surface assistant reasoning for log readers.
            for line in (message.get("content") or "").split("\n"):
                if line.strip():
                    self.log(f"  > {line}")

            tool_calls = message.get("tool_calls") or []
            finish_reason = message.get("finish_reason", "")

            if not tool_calls:
                # Local models frequently produce a text-only "plan" response
                # then stop. Rather than bailing out, nudge them to continue.
                # Only give up after 3 consecutive no-tool turns OR if the
                # model explicitly said it's done without calling task_done.
                nudge_count = getattr(self, "_nudge_count", 0) + 1
                self._nudge_count = nudge_count
                if nudge_count >= 3:
                    self.log(f"Agent produced {nudge_count} text-only turns without a tool call — exit=2 (incomplete)")
                    return 2
                self.log(f"No tool call in response (nudge {nudge_count}/3) — reminding agent to use tools")
                messages.append({
                    "role": "user",
                    "content": (
                        "You just produced text without calling a tool. "
                        "You MUST use a tool call to make progress. "
                        "Call `bash`, `read_file`, `write_file`, or `http_request` to do work, "
                        "or call `task_done(status=\"success\"|\"blocked\"|\"incomplete\", summary=\"...\")` "
                        "to finish the session."
                    ),
                })
                continue
            # Reset the nudge counter whenever the agent successfully calls tools.
            self._nudge_count = 0

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
