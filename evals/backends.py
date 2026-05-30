"""LLM backend abstractions for Tier-2 evals.

Tier-2 evals run a built persona prompt through an actual LLM and score the
output. We support three backends:

  - ``StubBackend``  — returns canned responses from a dict keyed by a
                       substring matcher. Lets the harness itself be tested
                       in CI without spending tokens.
  - ``OllamaBackend`` — POSTs to the Ollama HTTP API (same path the
                       orchestrator uses in production). Cheapest live mode.
  - ``ClaudeBackend`` — POSTs to the Anthropic Messages API. Used when CI
                       needs deterministic, fast judging that doesn't depend
                       on the local Ollama host. Falls back to anyio if
                       httpx isn't async-friendly.

All backends return a plain string (the assistant message content). No
streaming. No tool-use. The harness's job is to score the OUTPUT TEXT —
behavioural fidelity is the responsibility of the scenario's checks.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Protocol

import httpx


class Backend(Protocol):
    """A single-turn chat backend.

    Implementations return the assistant's text response, or raise an
    exception on transport error. The runner translates raised exceptions
    into a scenario-level FAIL with a "backend error" reason.
    """

    name: str

    def call(self, prompt: str, *, temperature: float = 0.0,
             max_tokens: int = 4096) -> str: ...


class StubBackend:
    """Returns canned responses keyed by a substring matcher.

    Each rule is `(needle, response)`. The first rule whose needle is a
    substring of the prompt wins. If no rule matches, returns the default.
    Used to unit-test the runner and scoring functions without spending
    tokens. Stable across runs.
    """

    name = "stub"

    def __init__(self, rules: list[tuple[str, str]], default: str = ""):
        self._rules = rules
        self._default = default

    def call(self, prompt: str, *, temperature: float = 0.0,
             max_tokens: int = 4096) -> str:
        for needle, response in self._rules:
            if needle in prompt:
                return response
        return self._default


class OllamaBackend:
    """Ollama HTTP API. Targets the same host the orchestrator uses.

    Reads OLLAMA_HOST + OLLAMA_API_KEY (if set, used as Bearer token —
    Ollama Cloud uses this; local Ollama ignores it). Default model is
    qwen3-coder:30b to match the orchestrator's CODER_MODEL default; pass
    `model=` per-scenario for designer/reviewer/architect personas.
    """

    name = "ollama"

    def __init__(self, model: str = "qwen3-coder:30b",
                 host: str | None = None,
                 api_key: str | None = None,
                 timeout: float = 180.0):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST")
                     or "http://localhost:11434").rstrip("/")
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY") or ""
        self.timeout = timeout

    def call(self, prompt: str, *, temperature: float = 0.0,
             max_tokens: int = 4096) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(f"{self.host}/api/chat", json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        # Ollama returns {"message": {"role": "assistant", "content": "..."}, ...}
        msg = data.get("message") or {}
        return msg.get("content", "") or ""


def make_backend_from_env() -> Backend:
    """Construct the backend the runner should use, based on env.

    EVAL_BACKEND=stub  → StubBackend with no rules (returns empty string for
                         every prompt). Useful for "does my scoring crash on
                         empty input" tests; less useful for real eval runs.
    EVAL_BACKEND=ollama → OllamaBackend with OLLAMA_HOST + OLLAMA_API_KEY +
                         EVAL_OLLAMA_MODEL (default qwen3-coder:30b).
    Default            → ollama if OLLAMA_HOST is set, else stub.
    """
    choice = os.environ.get("EVAL_BACKEND", "").strip().lower()
    if not choice:
        choice = "ollama" if os.environ.get("OLLAMA_HOST") else "stub"
    if choice == "stub":
        return StubBackend(rules=[], default="")
    if choice == "ollama":
        return OllamaBackend(
            model=os.environ.get("EVAL_OLLAMA_MODEL", "qwen3-coder:30b"),
        )
    raise ValueError(f"unknown EVAL_BACKEND={choice!r}")
