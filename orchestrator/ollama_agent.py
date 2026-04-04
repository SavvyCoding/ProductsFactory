"""
Ollama agent runner — drop-in replacement for 'claude -p {prompt}' for local testing.

Implements an agentic tool-use loop against Ollama's OpenAI-compatible API.

Usage (same interface as claude CLI):
    python ollama_agent.py -p "Your prompt here"

Environment variables:
    OLLAMA_HOST         Base URL for Ollama  (default: http://host.docker.internal:11434)
    AGENT_PERSONA       designer | reviewer | coder  — selects which model to use
    DESIGNER_MODEL      Model for designer + reviewer  (default: gemma3:27b)
    CODER_MODEL         Model for coder               (default: qwen3-coder:30b)
    PM_API_URL          ProductFactory PM API base URL (for logging + API calls)
    MAX_TURNS           Hard cap on agentic turns      (default: 80)
    SESSION_UID         Injected by docker_runner; included in log lines

Networking:
    Ollama runs on the Windows host. Inside the Docker container the host is
    reachable as host.docker.internal — so set OLLAMA_HOST accordingly.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx


# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_HOST    = os.environ.get("OLLAMA_HOST",    "http://host.docker.internal:11434")
DESIGNER_MODEL = os.environ.get("DESIGNER_MODEL", "gemma3:27b")
CODER_MODEL    = os.environ.get("CODER_MODEL",    "qwen3-coder:30b")
AGENT_PERSONA  = os.environ.get("AGENT_PERSONA",  "coder")
PM_API_URL     = os.environ.get("PM_API_URL",     "http://pm-api:8080")
SESSION_UID    = os.environ.get("SESSION_UID",    "local")
MAX_TURNS      = int(os.environ.get("MAX_TURNS",  "80"))

# Map persona → model
MODEL = DESIGNER_MODEL if AGENT_PERSONA in ("designer", "reviewer") else CODER_MODEL

CHAT_URL = f"{OLLAMA_HOST}/v1/chat/completions"


# ── Tool definitions (OpenAI function-calling format) ─────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a bash command. Use this for git, pytest, gh CLI, file operations, "
                "and anything else that needs a shell. cwd defaults to /workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute (bash -c). Can be multi-line.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (default: /workspace)",
                        "default": "/workspace",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file. Paths relative to /workspace are resolved automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (absolute or workspace-relative)"},
                    "max_lines": {"type": "integer", "description": "Maximum lines to return (default: 500)", "default": 500},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (or overwrite) a file. Parent directories are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make an HTTP request. Use this to call the PM API (e.g. PATCH feature status, "
                "POST session, GET features). Also works for GitHub API calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PATCH", "DELETE", "PUT"],
                    },
                    "url": {"type": "string", "description": "Full URL including query params"},
                    "body": {
                        "type": "object",
                        "description": "JSON request body (for POST/PATCH/PUT)",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Additional request headers",
                    },
                },
                "required": ["method", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_done",
            "description": (
                "Call this when the agent has completed all work for this session and is ready "
                "to exit cleanly (equivalent to exit 0)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Brief summary of what was accomplished"},
                },
                "required": ["summary"],
            },
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path("/workspace") / p
    return p


def tool_bash(command: str, cwd: str = "/workspace") -> str:
    _log(f"bash: {command[:120]}")
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        # Cap output to avoid flooding context
        if len(output) > 10_000:
            output = output[:5_000] + "\n[...truncated...]\n" + output[-4_000:]
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: Command timed out after 180s"
    except Exception as e:
        return f"ERROR: {e}"


def tool_read_file(path: str, max_lines: int = 500) -> str:
    p = _resolve_path(path)
    _log(f"read: {p}")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n[...{len(lines) - max_lines} more lines truncated]"
        return "\n".join(lines)
    except FileNotFoundError:
        return f"ERROR: File not found: {p}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_write_file(path: str, content: str) -> str:
    p = _resolve_path(path)
    _log(f"write: {p} ({len(content)} chars)")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_http_request(method: str, url: str, body: dict = None, headers: dict = None) -> str:
    _log(f"http {method} {url}")
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.request(method, url, json=body, headers=headers or {})
            try:
                data = resp.json()
                text = json.dumps(data, indent=2)
            except Exception:
                text = resp.text
            if len(text) > 8_000:
                text = text[:8_000] + "\n[...truncated]"
            return f"HTTP {resp.status_code}\n{text}"
    except Exception as e:
        return f"ERROR: {e}"


def dispatch_tool(name: str, args: dict) -> tuple[str, bool]:
    """Returns (result_text, is_done)."""
    if name == "bash":
        return tool_bash(args.get("command", ""), args.get("cwd", "/workspace")), False
    elif name == "read_file":
        return tool_read_file(args.get("path", ""), args.get("max_lines", 500)), False
    elif name == "write_file":
        return tool_write_file(args.get("path", ""), args.get("content", "")), False
    elif name == "http_request":
        return tool_http_request(
            args.get("method", "GET"), args.get("url", ""),
            args.get("body"), args.get("headers"),
        ), False
    elif name == "task_done":
        _log(f"Task done: {args.get('summary', '')}")
        return "Session complete.", True
    return f"ERROR: Unknown tool '{name}'", False


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    line = f"[ollama-agent/{AGENT_PERSONA}/{SESSION_UID}] {msg}"
    print(line, flush=True)


# ── Main agentic loop ─────────────────────────────────────────────────────────

def run_agent(initial_prompt: str) -> int:
    """Returns 0 on clean exit, 1 on error."""
    _log(f"Starting — model={MODEL} persona={AGENT_PERSONA} max_turns={MAX_TURNS}")

    # Verify Ollama is reachable
    try:
        resp = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        available = [m["name"] for m in resp.json().get("models", [])]
        if MODEL not in available and not any(MODEL in m for m in available):
            _log(f"WARNING: model '{MODEL}' not found in Ollama. Available: {available}")
            _log(f"Pull it with: ollama pull {MODEL}")
    except Exception as e:
        _log(f"WARNING: Could not reach Ollama at {OLLAMA_HOST}: {e}")
        _log("Proceeding anyway — Ollama may still be starting up...")

    messages = [
        {
            "role": "system",
            "content": (
                "You are an autonomous software agent. You have access to tools: bash, read_file, "
                "write_file, http_request, and task_done. Work step-by-step, using one or more tools "
                "per response. Always call task_done when you have completed all work. "
                "When in doubt about a path, check with bash('ls /workspace') first."
            ),
        },
        {"role": "user", "content": initial_prompt},
    ]

    for turn in range(1, MAX_TURNS + 1):
        _log(f"Turn {turn}/{MAX_TURNS}")

        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "stream": False,
            "options": {
                "temperature": 0.2,     # low temperature for deterministic code generation
                "num_ctx": 32768,       # large context window
            },
        }

        try:
            resp = httpx.post(CHAT_URL, json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            _log("ERROR: Ollama request timed out (300s) — model may be too slow or stuck")
            return 1
        except httpx.HTTPStatusError as e:
            _log(f"ERROR: Ollama API returned {e.response.status_code}: {e.response.text[:500]}")
            return 1
        except Exception as e:
            _log(f"ERROR: Unexpected error calling Ollama: {e}")
            return 1

        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "")
        messages.append(message)

        # Print assistant reasoning/text
        if message.get("content"):
            for line in message["content"].split("\n"):
                _log(f"  > {line}")

        # Extract tool calls
        tool_calls = message.get("tool_calls") or []

        # Fallback: some models embed tool calls as JSON in content
        if not tool_calls and message.get("content"):
            tool_calls = _try_parse_tool_calls_from_content(message["content"])

        if not tool_calls:
            if finish_reason in ("stop", "end_turn", ""):
                _log("Agent finished without calling task_done — treating as clean exit")
                return 0
            _log(f"No tool calls and finish_reason={finish_reason!r} — done")
            return 0

        # Execute each tool call
        tool_results = []
        session_done = False
        for tc in tool_calls:
            fn = tc.get("function", tc)  # handle both formats
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")

            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            result_text, is_done = dispatch_tool(name, args)
            if is_done:
                session_done = True

            # Build tool result in the format Ollama expects
            tc_id = tc.get("id", f"call_{turn}_{name}")
            tool_results.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result_text,
            })

        messages.extend(tool_results)

        if session_done:
            _log("Session completed via task_done")
            return 0

    _log(f"Reached max turns ({MAX_TURNS}) without completing — exiting with error")
    return 1


def _try_parse_tool_calls_from_content(content: str) -> list[dict]:
    """
    Fallback: try to extract JSON tool calls from model content text.
    Some older or quantized models output tool calls as embedded JSON rather
    than using the structured tool_calls field.

    Looks for patterns like:
        {"name": "bash", "arguments": {"command": "..."}}
    or:
        <tool_call>{"name": "bash", "arguments": {"command": "..."}}</tool_call>
    """
    import re

    # Pattern 1: <tool_call>...</tool_call> tags
    tag_pattern = re.findall(r'<tool_call>(.*?)</tool_call>', content, re.DOTALL)
    if tag_pattern:
        results = []
        for raw in tag_pattern:
            try:
                obj = json.loads(raw.strip())
                results.append({
                    "id": f"fallback_{len(results)}",
                    "type": "function",
                    "function": {
                        "name": obj.get("name", ""),
                        "arguments": json.dumps(obj.get("arguments", obj.get("parameters", {}))),
                    },
                })
            except json.JSONDecodeError:
                pass
        if results:
            return results

    # Pattern 2: raw JSON block with "name" + "arguments" at top level
    json_blocks = re.findall(r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*\}', content)
    if json_blocks:
        # Try to parse each block
        results = []
        for match in re.finditer(r'(\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*\})', content):
            try:
                obj = json.loads(match.group(1))
                results.append({
                    "id": f"fallback_{len(results)}",
                    "type": "function",
                    "function": {
                        "name": obj.get("name", ""),
                        "arguments": json.dumps(obj.get("arguments", {})),
                    },
                })
            except json.JSONDecodeError:
                pass
        if results:
            return results

    return []


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ollama agent runner for ProductFactory")
    parser.add_argument("-p", "--prompt", required=True, help="Initial prompt")
    args = parser.parse_args()

    exit_code = run_agent(args.prompt)
    sys.exit(exit_code)
