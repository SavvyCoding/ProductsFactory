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
MAX_TURNS        = int(os.environ.get("MAX_TURNS",         "80"))
OLLAMA_TIMEOUT   = int(os.environ.get("OLLAMA_TIMEOUT",    "300"))  # seconds for model inference
BASH_TIMEOUT     = int(os.environ.get("BASH_TIMEOUT",      "180"))  # seconds for shell commands
RETRY_SLEEP      = int(os.environ.get("OLLAMA_RETRY_SLEEP", "2"))   # seconds between retries

# WORKSPACE_DIR: inside Docker this is /workspace; for local test_run.py it's the real product path
WORKSPACE_DIR  = os.environ.get("WORKSPACE_DIR",  "/workspace")

# On Windows, `bash` may resolve to WSL which fails. Find Git Bash explicitly.
_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

def _find_bash() -> list[str]:
    """Return the bash executable to use for tool_bash."""
    if os.name != "nt":
        return ["bash"]
    for candidate in _GIT_BASH_CANDIDATES:
        if os.path.isfile(candidate):
            return [candidate, "--login", "-c"]
    return ["bash"]  # fallback — might be WSL

BASH_CMD = _find_bash()

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
                        "description": f"Working directory (default: {WORKSPACE_DIR})",
                        "default": WORKSPACE_DIR,
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
    """Resolve a file path, mapping /workspace → WORKSPACE_DIR for local test runs."""
    import re
    workspace = Path(WORKSPACE_DIR)
    # Always replace /workspace prefix with the actual workspace dir.
    # On Linux in Docker, WORKSPACE_DIR=/workspace so this is a no-op.
    # On Windows with test_run.py, WORKSPACE_DIR=C:\...\product and /workspace
    # would otherwise map to C:\workspace (drive-root), which is wrong.
    str_path = path.replace("\\", "/")
    if str_path == "/workspace" or str_path.startswith("/workspace/"):
        relative = str_path[len("/workspace/"):]
        return workspace / relative if relative else workspace
    # Convert Git Bash POSIX drive paths: /c/Users/... → C:/Users/...
    # Without this, write_file('/c/Users/...') on Windows creates C:\c\Users\...
    if os.name == "nt":
        m = re.match(r"^/([a-zA-Z])(/.*)$", str_path)
        if m:
            return Path(f"{m.group(1).upper()}:{m.group(2)}")
    p = Path(path)
    if not p.is_absolute():
        return workspace / p
    return p


def _resolve_cwd(cwd: str) -> str:
    """Translate /workspace cwd to the real workspace path."""
    if cwd == "/workspace" or cwd.startswith("/workspace/"):
        return str(_resolve_path(cwd))
    return cwd


def _to_posix_cwd(path: str) -> str:
    """Convert a Windows path to POSIX form for Git Bash (e.g. C:\foo -> /c/foo)."""
    p = Path(path)
    if os.name == "nt" and p.drive:
        drive_letter = p.drive[0].lower()          # 'C' → 'c'
        rest = str(p)[len(p.drive):].replace("\\", "/")
        return f"/{drive_letter}{rest}"
    return path


def tool_bash(command: str, cwd: str = WORKSPACE_DIR) -> str:
    # Replace /workspace references in the command with the real path (local test runs)
    if WORKSPACE_DIR != "/workspace":
        posix_ws = WORKSPACE_DIR.replace("\\", "/")
        command = command.replace("/workspace", posix_ws)
    _log(f"bash: {command[:120]}")
    real_cwd = _resolve_cwd(cwd)
    posix_cwd = _to_posix_cwd(real_cwd)
    try:
        # BASH_CMD is ["bash"] on Linux/Docker, or [git-bash-path, "--login", "-c"] on Windows
        # Splice the command in: if BASH_CMD ends with "-c", append command; else add "-c" + command
        if BASH_CMD[-1] == "-c":
            full_cmd = BASH_CMD + [command]
        else:
            full_cmd = BASH_CMD + ["-c", command]

        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
            cwd=real_cwd,
            env={**os.environ, "PWD": posix_cwd},
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
            if len(text) > 3_000:
                text = text[:3_000] + "\n[...truncated]"
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
                f"When in doubt about a path, check with bash('ls {WORKSPACE_DIR}') first."
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

        # Retry up to 3 times on 500 errors (model-side XML/serialization glitches)
        data = None
        for _attempt in range(3):
            try:
                resp = httpx.post(CHAT_URL, json=payload, timeout=OLLAMA_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.TimeoutException:
                _log(f"ERROR: Ollama request timed out ({OLLAMA_TIMEOUT}s) — model may be too slow or stuck")
                return 1
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 500 and _attempt < 2:
                    _log(f"WARNING: Ollama 500 on attempt {_attempt+1}, retrying… ({e.response.text[:200]})")
                    import time as _time; _time.sleep(RETRY_SLEEP)
                    continue
                _log(f"ERROR: Ollama API returned {e.response.status_code}: {e.response.text[:500]}")
                return 1
            except Exception as e:
                _log(f"ERROR: Unexpected error calling Ollama: {e}")
                return 1
        if data is None:
            _log("ERROR: Ollama 500 persisted after 3 attempts — giving up")
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
                _log("Agent finished without calling task_done — exiting with code 2 (incomplete)")
                return 2
            _log(f"No tool calls and finish_reason={finish_reason!r} — exiting with code 2 (incomplete)")
            return 2

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
