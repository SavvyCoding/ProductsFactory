"""
Secret redaction and Claude stream-json event formatting.

Two responsibilities, both pure (no side effects):
  - _redact_secrets: strip credential-shaped substrings from log lines before
    they are sent to docker stdout or POSTed to the PM API session log buffer.
  - _format_agent_event: parse one event from `claude -p --output-format
    stream-json --verbose` into a single readable log line.

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import json
import re

# Patterns for credentials that must never appear in logs. Anything matching
# is replaced with ***REDACTED*** before lines are sent to docker stdout or
# POSTed to the PM API session log buffer. List grows as new auth schemes are
# discovered in the wild — over-redaction is fine; under-redaction is not.
#
# This is the canonical list. ``orchestrator/ollama_agent.py`` previously kept
# a near-duplicate copy that had already drifted (added the
# ``x-access-token:`` pattern this list was missing); the drift-cleanup pass
# merged that pattern in and made ollama_agent import from here.
_SECRET_PATTERNS = [
    re.compile(r'gh[psoua]_[A-Za-z0-9]{20,}'),                                    # GitHub classic + variants
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),                                  # GitHub fine-grained
    re.compile(r'sk-ant-(?:oat|ort|api|admin)[A-Za-z0-9_\-]{20,}'),               # Anthropic
    re.compile(r'sk-[A-Za-z0-9]{20,}'),                                           # Generic OpenAI-shape
    re.compile(r'AKIA[A-Z0-9]{16}'),                                              # AWS access key id
    re.compile(r'xox[bpasr]-[A-Za-z0-9-]+'),                                      # Slack tokens
    re.compile(r'(Bearer\s+)[A-Za-z0-9_.\-=]{12,}', re.IGNORECASE),               # HTTP Bearer
    re.compile(r'(x-access-token:)[^@\s\'"]{8,}'),                                # Embedded PAT in git remote URL
]


def _redact_secrets(s: str) -> str:
    """Strip credential-shaped substrings before logging.

    Returns the input unchanged when falsy (None / "") so callers don't
    have to special-case empty tool-result strings.
    """
    if not s:
        return s
    for pat in _SECRET_PATTERNS:
        s = pat.sub('***REDACTED***', s)
    return s


def _format_agent_event(line: str) -> str | None:
    """
    Parse one stream-json event from `claude -p --output-format stream-json --verbose`
    into a single readable log line. Falls back to the raw line if it isn't JSON
    (so Ollama agent's plain-text output still flows through unchanged).

    Returns None for events worth dropping (init noise) so we don't spam the log.
    """
    try:
        ev = json.loads(line)
        if not isinstance(ev, dict):
            return line
    except (json.JSONDecodeError, ValueError):
        return line

    t = ev.get("type")

    if t == "system":
        sub = ev.get("subtype", "")
        sid = (ev.get("session_id") or "?")[:8]
        model = ev.get("model", "?")
        return f"[system:{sub}] sid={sid} model={model}"

    if t == "assistant":
        msg = ev.get("message") or {}
        out: list[str] = []
        for block in (msg.get("content") or []):
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                txt = (block.get("text") or "").strip().replace("\n", " ⏎ ")
                if txt:
                    out.append(f"[text] {txt[:300]}")
            elif bt == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input") or {}
                # Surface the most distinguishing input field per tool
                if name == "Bash":
                    summary = (inp.get("command") or "")[:200]
                elif name in ("Read", "Edit", "Write", "NotebookEdit"):
                    summary = inp.get("file_path") or inp.get("path") or ""
                elif name == "Grep":
                    summary = f"pattern={(inp.get('pattern') or '')[:80]} path={inp.get('path') or ''}"
                elif name in ("Glob",):
                    summary = inp.get("pattern") or ""
                else:
                    summary = json.dumps(inp, default=str)[:200]
                out.append(f"[tool] {name}({summary})")
        return " | ".join(out) if out else None

    if t == "user":
        # tool_result feedback — we only surface a one-line summary; full content
        # is too large to log per-event.
        msg = ev.get("message") or {}
        for block in (msg.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            content = str(content).strip()
            tag = "tool_err" if block.get("is_error") else "tool_ok"
            first = content.split("\n", 1)[0][:200]
            return f"[{tag}] {first}"
        return None

    if t == "result":
        sub = ev.get("subtype", "")
        cost = ev.get("total_cost_usd")
        turns = ev.get("num_turns")
        dur = ev.get("duration_ms")
        return f"[result:{sub}] turns={turns} cost=${cost} duration={dur}ms"

    # Unknown event type — log a short summary so we don't lose anything.
    return f"[{t}] {json.dumps(ev, default=str)[:300]}"
