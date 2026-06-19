"""Premium API backends for the agent loop — Claude API and OpenAI.

These conform to ``orchestrator.agent_loop.Backend``::

    __call__(messages, tools) -> {"content", "tool_calls", "finish_reason"}

and let a feature's **escalation pass** (docs/blocked_escalation_plan.md) run on a
frontier model, reusing the *exact same* tool loop + dispatcher the Ollama backend
uses. Only the "prompt → assistant message + tool calls" step differs per provider.

The agent loop speaks an OpenAI-style message format:
  - messages : ``{role: system|user|assistant|tool, content, tool_calls?, tool_call_id?}``
  - tool_calls: ``[{id, function: {name, arguments(JSON-str)}}]``
  - tools     : ``[{type:"function", function:{name, description, parameters}}]``

``OpenAIBackend`` is ~passthrough. ``ClaudeAPIBackend`` converts to/from Anthropic's
content-block format. SDKs are imported lazily so this module imports anywhere; the
agent image carries ``anthropic`` / ``openai``.

Each backend accumulates ``total_input_tokens`` / ``total_output_tokens`` /
``total_cost_usd`` so the session-metrics patch can record real cost — which the
daily-USD escalation cap then sums.
"""
from __future__ import annotations

import json
import os
import time

# Approximate USD per 1M tokens (input, output). Keyed by a model-name prefix;
# the cap is a soft guardrail, so published list prices are good enough. Update
# as pricing changes. Fallback is intentionally on the high side so an unknown
# model can't silently under-count against the cap.
_PRICING = {
    # claude-opus-4-8 calibrated to 0.5x list (2026-06-18): the $15/$75 list
    # constant over-counted real billing ~2.7x against the Anthropic console
    # ($12.71 estimated vs $4.72 actual), tripping the daily escalation cap
    # early. 0.5x lands closer to observed spend while staying on the safe
    # (slightly-conservative) side of the ~0.37x measured ratio.
    "claude-opus":    (7.5, 37.5),
    "claude-sonnet":  (3.0, 15.0),
    "claude-haiku":   (0.80, 4.0),
    "claude-fable":   (15.0, 75.0),
    "gpt-4o-mini":    (0.15, 0.60),
    "gpt-4o":         (2.5, 10.0),
    "gpt-4.1":        (2.0, 8.0),
    "o1":             (15.0, 60.0),
    "gpt-5":          (5.0, 15.0),
}
_PRICING_FALLBACK = (15.0, 75.0)


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for prefix, price in _PRICING.items():
        if m.startswith(prefix):
            return price
    return _PRICING_FALLBACK


class _CostTracker:
    """Mixin: accumulate token + USD totals across __call__ turns."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.call_count = 0

    def _record(self, in_tok: int, out_tok: int,
                cache_read: int = 0, cache_write: int = 0) -> None:
        # Prompt-caching aware (Anthropic): a cache WRITE bills at 1.25x base
        # input price, a cache READ at 0.10x. usage.input_tokens is only the
        # uncached remainder, so without these terms a cached multi-turn session
        # would be wildly UNDER-counted (and an uncached one is the $3.72/224K
        # case that tripped the daily cap on its own — 2026-06-18).
        in_price, out_price = _price_for(self.model)
        self.total_input_tokens += (in_tok or 0) + (cache_read or 0) + (cache_write or 0)
        self.total_output_tokens += out_tok or 0
        self.total_cost_usd += (
            (in_tok or 0) * in_price
            + (cache_write or 0) * in_price * 1.25
            + (cache_read or 0) * in_price * 0.10
            + (out_tok or 0) * out_price
        ) / 1_000_000.0
        self.call_count += 1


# ── tool / message format conversion (OpenAI-style ⇄ Anthropic) ──────────────


def _tools_to_anthropic(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({
            "name": fn.get("name"),
            "description": fn.get("description", "") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _messages_to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert OpenAI-style messages → (system_prompt, anthropic_messages).

    Anthropic requires strict user/assistant alternation and tool_results inside
    USER content blocks. The agent loop appends one ``role:"tool"`` message per
    tool call; consecutive ones are merged into a single user turn here.
    """
    system_parts: list[str] = []
    amsgs: list[dict] = []

    def _append_user_block(block: dict) -> None:
        # Merge into the previous user turn if it's already user (keeps
        # alternation valid when several tool_results follow one assistant turn).
        if amsgs and amsgs[-1]["role"] == "user":
            amsgs[-1]["content"].append(block)
        else:
            amsgs.append({"role": "user", "content": [block]})

    for m in messages or []:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            if content:
                system_parts.append(content)
        elif role == "user":
            _append_user_block({"type": "text", "text": content or "(no content)"})
        elif role == "tool":
            _append_user_block({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "unknown",
                "content": content or "(empty)",
            })
        else:
            # Assistant turn. CRITICAL: the agent loop appends the raw backend
            # response dict (agent_loop.py ~L261), which carries NO explicit
            # "role" key — agent_loop._is_assistant treats "anything not
            # system/user/tool" as the assistant. So we MUST catch role==None
            # (and "assistant") here, not just role=="assistant". Missing this
            # silently DROPPED the tool_use turn, and the following tool_result
            # then merged into the user turn → Anthropic 400 "unexpected
            # tool_use_id in tool_result … no corresponding tool_use".
            # (2026-06-18 premium-escalation incident: every Opus session died
            # on turn 2.)
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                args = fn.get("arguments")
                try:
                    inp = json.loads(args) if isinstance(args, str) else (args or {})
                except (ValueError, TypeError):
                    inp = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"call_{len(amsgs)}",
                    "name": fn.get("name"),
                    "input": inp,
                })
            if not blocks:
                blocks.append({"type": "text", "text": "(no content)"})
            amsgs.append({"role": "assistant", "content": blocks})
    return "\n\n".join(system_parts), amsgs


def _with_cache_control(system: str, amsgs: list[dict]) -> tuple[list[dict] | None, list[dict]]:
    """Add ephemeral prompt-cache breakpoints so a multi-turn tool loop re-reads
    the stable prefix (system + prior turns) at 0.10x input price instead of
    re-paying full price for the whole re-sent context every turn.

    Anthropic automatically bills the longest already-cached matching prefix as a
    cache READ regardless of where THIS request's breakpoints sit — breakpoints
    only control what gets WRITTEN. So one breakpoint on the system block + one on
    the current conversation tail is enough for incremental caching: each turn
    writes the new tail, the next turn reads everything before it.

    Returns (system_param, amsgs): system becomes a one-element list of cached
    text blocks (or None when empty), and the last content block of the last
    message is tagged. amsgs is rebuilt fresh each call by _messages_to_anthropic,
    so tagging it here never leaks cache_control back into the loop's history.
    """
    system_param = None
    if system:
        system_param = [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]
    if amsgs:
        content = amsgs[-1].get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    return system_param, amsgs


def _anthropic_response_to_openai(resp) -> dict:
    """Anthropic Message → {content, tool_calls(OpenAI-style), finish_reason}."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in (resp.content or []):
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": getattr(block, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(block, "name", ""),
                    "arguments": json.dumps(getattr(block, "input", {}) or {}),
                },
            })
    return {
        "content": "".join(text_parts),
        "tool_calls": tool_calls,
        "finish_reason": getattr(resp, "stop_reason", "") or "",
    }


# ── backends ─────────────────────────────────────────────────────────────────


class ClaudeAPIBackend(_CostTracker):
    """Anthropic Messages API backend (tool use)."""

    def __init__(self, model: str, api_key: str, *, max_tokens: int = 8192,
                 timeout: int = 600, log=lambda _m: None) -> None:
        super().__init__(model)
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._log = log
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic  # lazy — only the agent image needs the SDK
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        system, amsgs = _messages_to_anthropic(messages)
        system_param, amsgs = _with_cache_control(system, amsgs)
        atools = _tools_to_anthropic(tools)
        client = self._client_lazy()
        last_err = None
        for attempt in range(5):  # internal retry on transient errors (per protocol)
            try:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_param,
                    messages=amsgs,
                    tools=atools or None,
                )
                usage = getattr(resp, "usage", None)
                self._record(
                    getattr(usage, "input_tokens", 0) if usage else 0,
                    getattr(usage, "output_tokens", 0) if usage else 0,
                    getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
                    getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
                )
                return _anthropic_response_to_openai(resp)
            except Exception as e:  # noqa: BLE001 — backend owns its retries
                last_err = e
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (401, 403):
                    raise  # auth — retrying won't help
                self._log(f"[claude-api] attempt {attempt+1}/5 failed: {e}")
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"ClaudeAPIBackend exhausted retries: {last_err}")


class OpenAIBackend(_CostTracker):
    """OpenAI Chat Completions backend (function calling). ~passthrough format."""

    def __init__(self, model: str, api_key: str, *, base_url: str | None = None,
                 timeout: int = 600, log=lambda _m: None) -> None:
        super().__init__(model)
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._log = log
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import openai  # lazy
            self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url,
                                         timeout=self.timeout)
        return self._client

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        client = self._client_lazy()
        last_err = None
        for attempt in range(5):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools or None,
                )
                choice = resp.choices[0]
                usage = getattr(resp, "usage", None)
                self._record(getattr(usage, "prompt_tokens", 0) if usage else 0,
                             getattr(usage, "completion_tokens", 0) if usage else 0)
                tcs = []
                for tc in (choice.message.tool_calls or []):
                    tcs.append({
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    })
                return {
                    "content": choice.message.content or "",
                    "tool_calls": tcs,
                    "finish_reason": choice.finish_reason or "",
                }
            except Exception as e:  # noqa: BLE001
                last_err = e
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (401, 403):
                    raise
                self._log(f"[openai] attempt {attempt+1}/5 failed: {e}")
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"OpenAIBackend exhausted retries: {last_err}")


def build_api_backend(kind: str, model: str, api_key: str, *, log=lambda _m: None):
    """Factory: 'claude-api' → ClaudeAPIBackend, 'openai' → OpenAIBackend."""
    if kind == "claude-api":
        return ClaudeAPIBackend(model, api_key, log=log)
    if kind == "openai":
        return OpenAIBackend(model, api_key, log=log)
    raise ValueError(f"unknown API backend kind: {kind!r}")
