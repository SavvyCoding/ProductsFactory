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
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx


# ── LLM-infrastructure failures ───────────────────────────────────────────────
# Raised when the backend cannot reach a usable model through any retry/fallback
# — i.e. the failure is in the LLM infrastructure (rate-limit / quota / auth /
# all models 5xx-ing / network partition), not in the work the agent was doing.
# docker_runner translates a session ending with this exception into exit code
# 43 (`EXIT_LLM_INFRA`); supervisor.detect_kill_recovery and _finalize_session
# treat 43 the same as the existing exit-42 env-not-ready path — release the
# feature claim without charging fix_attempts. See orchestrator/docker_runner.py
# and orchestrator/supervisor.py.
class LLMInfraExhausted(RuntimeError):
    """All models in the backend chain failed for infrastructure reasons.

    `category` is one of: 'quota', 'auth', 'network', 'unknown'. Used by the
    operator alert so the message names the actual root cause (quota reset
    is hours; auth rotation is minutes; network is a deploy problem).
    """

    def __init__(self, message: str, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


# ── Secret redaction for tool results ─────────────────────────────────────────
# Tool output (bash stdout/stderr, file contents, HTTP responses) flows back
# into the agent's conversation history and reaches Ollama Cloud on every
# subsequent turn. Strip credential-shaped substrings BEFORE the agent sees
# them so they never leave this container.
#
# Patterns live in orchestrator.infra.redaction (the canonical list shared
# with docker_runner's log redaction). This module previously kept a
# near-duplicate local copy that had already drifted from the canonical;
# the drift-cleanup pass dropped it and imported from there instead. New
# patterns belong in infra/redaction.py.
from orchestrator.infra.redaction import _SECRET_PATTERNS, _redact_secrets  # noqa: F401


# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_HOST    = os.environ.get("OLLAMA_HOST",    "http://host.docker.internal:11434")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")  # required for Ollama Cloud, empty locally
DESIGNER_MODEL = os.environ.get("DESIGNER_MODEL", "gemma3:27b")
CODER_MODEL    = os.environ.get("CODER_MODEL",    "qwen3-coder:30b")
AGENT_PERSONA  = os.environ.get("AGENT_PERSONA",  "coder")
PM_API_URL     = os.environ.get("PM_API_URL",     "http://pm-api:8080")
SESSION_UID    = os.environ.get("SESSION_UID",    "local")
MAX_TURNS        = int(os.environ.get("MAX_TURNS",         "80"))
OLLAMA_TIMEOUT   = int(os.environ.get("OLLAMA_TIMEOUT",    "300"))  # seconds for model inference
BASH_TIMEOUT     = int(os.environ.get("BASH_TIMEOUT",      "180"))  # seconds for shell commands
RETRY_SLEEP      = int(os.environ.get("OLLAMA_RETRY_SLEEP", "2"))   # seconds between retries
# Context window allocated per Ollama request. Was hardcoded at 32768, which
# silently truncated the context for the large-window cloud models actually in
# use (qwen3-coder:480b = 256K, deepseek-v4-pro = 1M, etc.) — once a session's
# accumulated context crossed 32K, Ollama dropped the system prompt + tool
# schema from the front and the model degenerated into text-only turns. 131072
# (128K) is safe for every model in the current ollama_model_map (smallest is
# gemma3:27b at exactly 128K). Env-overridable so it can be tuned per
# deployment without a rebuild. Pair with AgentLoop history windowing to keep
# the working set — and per-turn token spend — bounded well under this ceiling.
OLLAMA_NUM_CTX   = int(os.environ.get("OLLAMA_NUM_CTX", "131072"))

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

# Model selection. The orchestrator pre-resolves the per-persona model chain
# and passes it via OLLAMA_MODEL — a comma-separated list, primary first
# (e.g. "gpt-oss:120b,qwen3-coder:480b,deepseek-v4-flash"). Fall back to the
# legacy designer/coder split only if OLLAMA_MODEL is unset (test_run.py).
def _parse_model_list(raw: str, fallback_single: str) -> list[str]:
    items = [m.strip() for m in (raw or "").split(",")]
    items = [m for m in items if m]
    return items or [fallback_single]

_EXPLICIT_MODEL = os.environ.get("OLLAMA_MODEL", "").strip()
if _EXPLICIT_MODEL:
    MODELS = _parse_model_list(_EXPLICIT_MODEL, DESIGNER_MODEL)
else:
    _legacy = DESIGNER_MODEL if AGENT_PERSONA in ("designer", "reviewer") else CODER_MODEL
    MODELS = [_legacy]
# Keep a string alias for log lines and the reachability probe.
MODEL = MODELS[0]

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
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines]) + f"\n[...{len(lines) - max_lines} more lines truncated]"
        else:
            text = "\n".join(lines)
        # Char cap defends against minified/single-line files that slip past the
        # line cap (e.g. a 1-line bundle.js = 200KB → 50k tokens into history,
        # re-sent every subsequent turn since agent_loop has no sliding window).
        # Mirrors the bash output cap pattern: head + tail with a marker.
        if len(text) > 10_000:
            text = text[:5_000] + "\n[...read_file truncated, file too large...]\n" + text[-4_000:]
        return text
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
    """Returns (result_text, is_done).

    All tool results that originate outside the agent (bash output, file
    contents, HTTP responses) pass through `_redact_secrets` before returning
    so credential-shaped substrings (PATs, Bearer tokens, x-access-token
    URLs) never enter the LLM's conversation context. write_file's return is
    just a status string we generated ourselves, so it doesn't need redaction.
    """
    # Read-only persona guard: reviewer / qa_tester / security_auditor must NOT
    # write code or commit to git. Their job is to look at existing artifacts
    # (diffs, tests, code) and PATCH the PM API with verdicts. The Ollama
    # qwen3-coder backend has heavy bias toward "fix the problem by writing
    # code" regardless of persona prompt — model would happily restore
    # missing functions, rewrite main.py to satisfy failing tests, etc.,
    # turning reviewer/qa/security sessions into amateur coder runs that
    # waste turns and produce unreviewed code on the PR branch.
    #
    # Hard guard: refuse write_file unconditionally for these personas, and
    # filter mutating bash commands (git commit, git add, sed -i, awk -i,
    # tee >>, redirect to file). Read-only bash (ls, cat, grep, pytest,
    # gh pr view, gh pr review) still works.
    _READONLY_PERSONAS = {"reviewer", "security_auditor"}
    _MUTATING_BASH_PATTERNS = (
        "git commit", "git add", "git push", "git rebase", "git merge",
        "git reset", "git checkout -b", "git tag",
        "sed -i", "awk -i",
    )

    if name == "bash":
        cmd = args.get("command", "")
        if AGENT_PERSONA in _READONLY_PERSONAS:
            cmd_lc = cmd.lower()
            for pat in _MUTATING_BASH_PATTERNS:
                if pat in cmd_lc:
                    _log(f"REFUSING mutating bash for {AGENT_PERSONA} persona: {pat!r} in command")
                    return (
                        f"REJECTED: persona={AGENT_PERSONA} is read-only — bash commands "
                        f"that mutate the working tree or git history are not allowed (saw "
                        f"{pat!r} in your command). Your job is to READ artifacts and decide. "
                        f"If the code is broken, request changes via the PM API — do NOT "
                        f"fix it yourself. Use task_done with summary starting 'blocked:' if "
                        f"you genuinely cannot proceed."
                    ), False
            # Crude redirect-to-file check ("foo > bar" or ">> bar"). Allow
            # heredoc-style "<<" since those are read patterns. Allow stderr
            # redirects "2>" which are diagnostic. Block plain ">" or ">>" to
            # any path under /workspace/ — EXCEPT session_result.json, which
            # is the reviewer's contract: the prompt instructs `echo '{...}' >>
            # /workspace/session_result.json`. Without this carve-out, the
            # filter blocks the very write the reviewer needs to perform.
            if (" > " in cmd or " >> " in cmd) and "/workspace/" in cmd:
                if "session_result.json" not in cmd:
                    _log(f"REFUSING file-redirect bash for {AGENT_PERSONA} persona: {cmd[:120]!r}")
                    return (
                        f"REJECTED: persona={AGENT_PERSONA} is read-only — can't redirect "
                        f"output into files under /workspace/ (except session_result.json). "
                        f"Read and decide."
                    ), False
        return _redact_secrets(tool_bash(cmd, args.get("cwd", "/workspace"))), False
    elif name == "read_file":
        return _redact_secrets(tool_read_file(args.get("path", ""), args.get("max_lines", 500))), False
    elif name == "write_file":
        if AGENT_PERSONA in _READONLY_PERSONAS:
            _log(f"REFUSING write_file for {AGENT_PERSONA} persona")
            # Earlier guidance pointed at `gh pr review --request-changes` and
            # vague "PATCH the feature" — but `gh` isn't on PATH and the
            # PATCH command wasn't spelled out. Reviewers (sessions 2158/2161,
            # 2026-05-10) hit this rejection, gave up with task_done(blocked),
            # and left features stuck in Reviewing. Spell out both working
            # paths so the model has somewhere to go from here.
            return (
                f"REJECTED: persona={AGENT_PERSONA} is read-only — write_file is blocked. "
                f"To record your decision use ONE of these (NOT write_file, NOT gh):\n"
                f"  (a) bash: echo '{{\"id\":<feat_id>,\"status\":\"Reviewed\"|\"Implementing\","
                f"\"review_outcome\":\"approved\"|\"changes_requested\",\"pr_number\":<n>}}'"
                f" >> /workspace/session_result.json\n"
                f"  (b) bash: curl -sS -X PATCH $PM_API_URL/api/features/<feat_id> "
                f"-H 'Content-Type: application/json' -d '{{\"status\":\"...\","
                f"\"review_outcome\":\"...\",\"changed_by\":\"reviewer\"}}'\n"
                f"Both work. Bash redirects to /workspace/session_result.json are "
                f"the ONE allowed write for this persona. Use task_done with summary "
                f"starting 'blocked:' only if you genuinely cannot proceed."
            ), False
        return tool_write_file(args.get("path", ""), args.get("content", "")), False
    elif name == "http_request":
        return _redact_secrets(tool_http_request(
            args.get("method", "GET"), args.get("url", ""),
            args.get("body"), args.get("headers"),
        )), False
    elif name == "task_done":
        summary = args.get("summary", "")
        # Coder gate: if the post-coder pipeline would Block these features for
        # an empty diff (`git status --porcelain` empty), refuse `task_done` here
        # and force the agent to either actually edit something or self-report
        # the failure with a clear status keyword. Catches the hallucinated-
        # completion failure mode seen with quantised local models.
        if AGENT_PERSONA == "coder" and not _agent_made_edits():
            sl = summary.lower()
            self_reports_failure = any(
                kw in sl for kw in ("blocked:", "incomplete:", "cannot ", "unable to", "cannot proceed")
            )
            if not self_reports_failure:
                _log("REFUSING task_done — no file edits detected and summary doesn't acknowledge incompleteness")
                return (
                    "REJECTED: You called task_done(success) but `git status --porcelain` is empty — "
                    "no files have been modified. The post-coder pipeline will Block these features "
                    "if the session ends with no diff. You MUST do one of the following:\n"
                    "  (a) Actually edit at least one file using `write_file` (preferred) or via "
                    "`bash` (heredoc, sed, etc.). Then call task_done again.\n"
                    "  (b) If you genuinely cannot make progress, call task_done with summary "
                    "starting with 'blocked: <one-line reason>' or 'incomplete: <what is partially done>'. "
                    "This will be allowed.\n"
                    "Continue working — do NOT call task_done(success) again until files are changed."
                ), False
        # Product-trainer gate: the trainer's deliverable is the showcase
        # narration + video at /workspace/output/. Session 2306 (2026-05-12)
        # declared task_done(success) after a `write_file: /workspace/output/
        # narration.md (2576 chars)` log line — but the host filesystem had
        # no `output/` directory at all. Verified via empirical reproduction
        # that an agent-shaped container CAN write to /workspace/output/, so
        # the most likely failure mode is that an Ollama timeout (or other
        # error) interrupted the write and the model ignored the tool's
        # ERROR response, declaring success on hallucinated work. Require
        # the deliverable file to actually exist before accepting success.
        if AGENT_PERSONA == "product_trainer":
            narration_path = Path(WORKSPACE_DIR) / "output" / "narration.md"
            if not narration_path.exists():
                sl = summary.lower()
                self_reports_failure = any(
                    kw in sl for kw in ("blocked:", "incomplete:", "cannot ", "unable to", "cannot proceed", "skipping", "no work", "fewer than")
                )
                if not self_reports_failure:
                    _log(f"REFUSING task_done — product_trainer deliverable {narration_path} missing")
                    return (
                        f"REJECTED: You called task_done(success) but the expected deliverable "
                        f"{narration_path} does not exist on disk. The product_trainer's contract "
                        f"is to produce that narration.md and (optionally) a "
                        f"`product_video_<ts>.mp4` in /workspace/output/. You MUST do one of:\n"
                        f"  (a) Actually write /workspace/output/narration.md via write_file or "
                        f"`bash` heredoc, then verify with `ls /workspace/output/` before calling "
                        f"task_done again.\n"
                        f"  (b) If you cannot produce it (e.g. fewer than 2 shipped features), "
                        f"call task_done with summary starting 'skipping:', 'blocked:', or "
                        f"'incomplete:' — that is allowed."
                    ), False
        # Reviewer gate: a reviewer that ends without writing review decisions
        # to session_result.json leaves the assigned features stuck in Reviewing
        # forever — auto-merge has no entries to act on. Force the model to
        # commit to a verdict (or self-report blocked) before letting it exit.
        if AGENT_PERSONA == "reviewer" and _reviewer_made_decisions() == 0:
            sl = summary.lower()
            self_reports_failure = any(
                kw in sl for kw in ("blocked:", "incomplete:", "cannot ", "unable to", "cannot proceed")
            )
            if not self_reports_failure:
                _log("REFUSING task_done — no review decisions in session_result.json")
                return (
                    "REJECTED: You called task_done(success) but session_result.json has zero "
                    "review decisions. Per the reviewer contract you MUST append one NDJSON line "
                    "for each feature you reviewed BEFORE calling task_done:\n"
                    '  Approve:         {"id": <feature_id>, "status": "Reviewed", "review_outcome": "approved"}\n'
                    '  Request changes: {"id": <feature_id>, "status": "Implementing", "review_outcome": "changes_requested"}\n'
                    "Append via bash: `echo '{\"id\":N,\"status\":\"Reviewed\",\"review_outcome\":\"approved\"}' "
                    ">> /workspace/session_result.json` (one line per feature). Do NOT PATCH the API "
                    "directly — the orchestrator picks decisions up from this file and triggers auto-merge.\n"
                    "If you genuinely cannot reach a decision, call task_done with summary starting "
                    "'blocked: <reason>' or 'incomplete: <what is partially done>' — that will be allowed."
                ), False
        _log(f"Task done: {summary}")
        return "Session complete.", True
    return f"ERROR: Unknown tool '{name}'", False


_TRANSIENT_GIT_PATTERNS = (
    "index.lock", "lock file", "could not lock", "unable to create temporary",
    "resource temporarily unavailable", "device or resource busy",
)

def _git_status_with_retry(max_retries: int = 3, sleep_s: float = 0.5):
    """Run `git status --porcelain --untracked-files=no` with retries on transient errors.

    **Why --untracked-files=no:** the default `git status` walks the entire
    working tree to enumerate untracked files. On a Windows bind-mounted
    workspace (Docker Desktop) with a Next.js / Python venv / etc.
    project, the underlying `stat()` calls go through Docker's filesystem
    layer for every file — including `node_modules/` even when gitignored
    (git still has to traverse to check). 30k+ files × Windows-bind-mount
    stat latency consistently overruns the 10s timeout, blocking the
    `_agent_made_edits` gate even on workspaces with real edits. Real
    incident 2026-05-18 02:46: session `c5a9cf52` exhausted the 3-retry
    budget on a 35K-file Next.js workspace; agent refused task_done; all
    work discarded. Skipping the untracked walk drops the typical scan
    from 5-15s to <1s. Untracked files are still checked via the
    separate `_has_untracked_files()` helper which uses `git ls-files
    --others --exclude-standard` — that respects .gitignore so
    node_modules is excluded at git's discovery level, not just at the
    "report it" stage.

    Two transient failure modes still get retried:

    1. **stderr-fail-fast** — git returns non-zero quickly with a lock-message
       (e.g. `fatal: Unable to create '.../index.lock': File exists`).
       Matched by `_TRANSIENT_GIT_PATTERNS`.

    2. **hang-then-timeout** — git makes no progress for `timeout=10`s and
       `subprocess.run` raises `TimeoutExpired`. Observed in the smoke test
       on 2026-05-05 when a parallel docker_runner heartbeat held the
       index lock for >10s; previously this propagated out and fail-closed
       the gate immediately, looping the agent on retries it could never
       satisfy.

    Real failures (corrupted .git, write-protected tree, ENOSPC) hit
    neither path and surface to the caller with the original return value
    (or a `TimeoutExpired` from the final attempt) so the gate still
    fail-closes when there's no recovery path.
    """
    last = None
    for attempt in range(max_retries):
        try:
            last = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=WORKSPACE_DIR, capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            if attempt < max_retries - 1:
                _log(f"_agent_made_edits: git status timed out, retry {attempt+1}/{max_retries}")
                time.sleep(sleep_s)
                continue
            raise  # final attempt — let caller fail-closed
        if last.returncode == 0:
            return last
        stderr_lc = (last.stderr or "").lower()
        if any(pat in stderr_lc for pat in _TRANSIENT_GIT_PATTERNS):
            _log(f"_agent_made_edits: transient git error (rc={last.returncode}), retry {attempt+1}/{max_retries}")
            time.sleep(sleep_s)
            continue
        return last  # non-transient — surface immediately
    return last


def _has_untracked_files() -> bool:
    """Fast check for any untracked, non-ignored file in the workspace.

    Companion to `_git_status_with_retry` (which excludes untracked
    enumeration for perf). Uses `git ls-files --others --exclude-standard`
    which:
      - Respects .gitignore at the enumeration level (node_modules,
        .next, dist, etc. are SKIPPED entirely, not just filtered after
        stat — this is the key perf win).
      - Returns one path per line, no diff computation, no rename detection.
      - Empty output ⇒ no untracked files.

    Filters out the same housekeeping paths the tracked-file check
    ignores (session_result.json, session_summary.md, Temp/, Results/).
    Returns False on timeout — we'd rather miss a corner-case "agent
    wrote only into a Temp/-shaped path" than block the gate. The
    tracked-file check + commits-ahead check cover the typical real-edit
    cases.
    """
    try:
        r = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=WORKSPACE_DIR, capture_output=True, text=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        _log("_has_untracked_files: timed out — falling back to tracked-only check")
        return False
    if r.returncode != 0:
        return False
    for path in (r.stdout or "").split("\x00"):
        path = path.strip()
        if not path:
            continue
        if path.endswith("session_result.json") or path.endswith("session_summary.md"):
            continue
        if "/Temp/" in path or "/Results/" in path:
            continue
        return True
    return False


def _reviewer_made_decisions() -> int:
    result_file = Path(WORKSPACE_DIR) / "session_result.json"
    if not result_file.exists():
        return 0
    try:
        lines = result_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in ("Reviewed", "Implementing") and entry.get("review_outcome"):
            count += 1
    return count


def _agent_made_edits() -> bool:
    """Detect whether the agent actually wrote code.

    Three signals count as proof of edits:
      1. Uncommitted changes in the working tree (`git status --porcelain`)
      2. Commits ahead of the upstream branch (committed but not pushed)
      3. Commits ahead of `origin/main` when no upstream is set yet

    Fail-CLOSED on git errors that aren't "this isn't a git repo" — the
    agent can't have written real code if git itself can't operate (lock
    files, permission failures, OOM, etc.). Earlier this fell open on
    any subprocess error and let the agent game the no-edit gate by
    triggering write-permission failures and then claiming success.

    The only fall-open case is when WORKSPACE_DIR isn't a git repo at
    all (test_run.py edge cases) — there the gate is meaningless.
    """
    try:
        # 1a. Tracked-file changes (fast: skips the untracked walk; see
        #     _git_status_with_retry docstring).
        status = _git_status_with_retry()
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.endswith("session_result.json") or line.endswith("session_summary.md"):
                    continue
                if "/Temp/" in line or "/Results/" in line:
                    continue
                return True
        else:
            # Distinguish "not a git repo" (fall open — gate is irrelevant)
            # from any other git failure (fail closed — agent likely can't
            # actually edit, e.g. permission errors locking .git/index).
            stderr_lc = (status.stderr or "").lower()
            if "not a git repository" in stderr_lc:
                _log(f"WARNING: workspace not a git repo — gate falling open (stderr: {stderr_lc[:120]})")
                return True
            _log(f"WARNING: git status failed (rc={status.returncode}); gate FAIL-CLOSED. stderr: {stderr_lc[:200]}")
            return False

        # 1b. Untracked files (fast via `git ls-files --others
        #     --exclude-standard`; respects .gitignore at enumeration
        #     time so node_modules etc. are skipped, not just filtered).
        if _has_untracked_files():
            return True

        # 2/3. Local commits ahead of remote tracking branch (or main).
        for ref in ("@{u}", "origin/main", "origin/master"):
            ahead = subprocess.run(
                ["git", "rev-list", "--count", f"{ref}..HEAD"],
                cwd=WORKSPACE_DIR, capture_output=True, text=True, timeout=10,
            )
            if ahead.returncode == 0:
                try:
                    if int((ahead.stdout or "0").strip()) > 0:
                        return True
                except ValueError:
                    pass
                break  # ref resolved (count was 0) — don't try fallbacks
        return False
    except Exception as e:
        # Fail-CLOSED on unexpected errors — silently falling open is what
        # let the permission-error attack succeed earlier.
        _log(f"WARNING: _agent_made_edits raised {type(e).__name__}: {e!r}; gate FAIL-CLOSED")
        return False


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    line = f"[ollama-agent/{AGENT_PERSONA}/{SESSION_UID}] {msg}"
    print(line, flush=True)


# ── Main agentic loop ─────────────────────────────────────────────────────────

class _OllamaBackend:
    """Single-turn chat backend against Ollama's OpenAI-compatible API.

    Handles Ollama-specific concerns: retrying on HTTP 500 (common with
    quantized models), timeout distinction, and falling back to embedded-JSON
    tool-call parsing for models that don't use the structured tool_calls field.

    Multi-model fallback: `models` is a primary-first list. For each turn we
    try models in order — when one exhausts its 5-retry budget on transient
    errors we move to the next. Auth/lookup errors (401/403/404) are treated
    as fatal (don't try sibling models with the same broken state).

    Sticky preference: once a model succeeds on a given turn, future turns
    start with that model first. Callers don't need to know which model
    answered.
    """

    def __init__(self, models: list[str], chat_url: str, timeout: int, retry_sleep: int,
                 api_key: str = "") -> None:
        if not models:
            raise ValueError("_OllamaBackend requires at least one model")
        self.models = list(models)
        self.chat_url = chat_url
        self.timeout = timeout
        self.retry_sleep = retry_sleep
        # Ollama Cloud requires Bearer auth; local Ollama ignores it.
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        # Token accumulators for end-of-session metrics PATCH.
        self.total_input_tokens  = 0
        self.total_output_tokens = 0
        self.call_count          = 0
        # Ordered set of model fallbacks fired this session, for diagnostics.
        self.fallback_log: list[str] = []

    @property
    def model(self) -> str:
        """Currently-preferred model — used in log lines + reachability probe."""
        return self.models[0]

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        import time as _time
        # Retry up to 5× per model with exponential backoff on transient errors.
        # When one model exhausts its budget, fall through to the next in the
        # list. Auth (401/403) and missing-model (404) are fatal across the
        # whole chain — same key/account on Ollama Cloud, no point retrying.
        _RETRYABLE_STATUS = {429, 500, 502, 503, 504}
        _CHAIN_FATAL_STATUS = {401, 403}  # 404 handled per-model (treat as not-installed → fall through)

        data = None
        used_model: str | None = None
        last_err: str | None = None
        for model_idx, model in enumerate(self.models):
            payload = {
                "model":       model,
                "messages":    messages,
                "tools":       tools,
                "tool_choice": "auto",
                "stream":      False,
                "options": {
                    "temperature": 0.2,    # low temp for deterministic code generation
                    "num_ctx":     OLLAMA_NUM_CTX,
                },
            }
            tail = "" if model_idx == 0 else f" (fallback {model_idx}/{len(self.models)-1})"
            for attempt in range(5):
                try:
                    resp = httpx.post(self.chat_url, json=payload,
                                      headers=self.headers, timeout=self.timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    used_model = model
                    if model_idx > 0:
                        # Sticky: promote the survivor so the next turn doesn't
                        # waste budget on the failing primary.
                        self.models = self.models[model_idx:] + self.models[:model_idx]
                        msg = f"recovered on fallback model {model!r}"
                        _log(f"INFO: {msg}")
                        self.fallback_log.append(f"{model_idx}→{model}: {msg}")
                    break
                except httpx.TimeoutException:
                    last_err = f"{model}: timeout after {self.timeout}s"
                    _log(f"WARNING: Ollama timeout on {model!r} (attempt {attempt+1}/5){tail}")
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    body = e.response.text[:300]
                    last_err = f"{model}: HTTP {code}: {body}"
                    if code in _CHAIN_FATAL_STATUS:
                        # Auth-class failure — same chain key, won't help to swap models.
                        # This is an infra problem (rotate the API key), not an
                        # agent-side failure. Raise LLMInfraExhausted so the
                        # session exits with the dedicated exit code and the
                        # supervisor skips bumping fix_attempts.
                        raise LLMInfraExhausted(
                            f"Ollama auth failure {code} on {model!r}: {body[:200]}",
                            category="auth",
                        )
                    if code == 404:
                        # Model not found / not pulled — break out of retry loop
                        # and fall through to the next model in the chain.
                        _log(f"WARNING: model {model!r} returned 404 — falling through to next")
                        break
                    if code in _RETRYABLE_STATUS:
                        _log(f"WARNING: Ollama {code} on {model!r} (attempt {attempt+1}/5){tail}: {body[:120]}")
                    else:
                        raise RuntimeError(f"Ollama non-retryable {code}: {body}")
                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                    last_err = f"{model}: network: {type(e).__name__}: {e}"
                    _log(f"WARNING: Ollama network error on {model!r} (attempt {attempt+1}/5){tail}: {last_err}")
                except Exception as e:
                    last_err = f"{model}: {type(e).__name__}: {e}"
                    _log(f"WARNING: Ollama unexpected error on {model!r} (attempt {attempt+1}/5){tail}: {last_err}")
                # Exponential backoff: 2s, 4s, 8s, 16s, then give up on this model
                if attempt < 4:
                    _time.sleep(self.retry_sleep * (2 ** attempt))
            if data is not None:
                break
            # Exhausted this model — log clearly before trying the next.
            if model_idx + 1 < len(self.models):
                next_model = self.models[model_idx + 1]
                _log(f"WARNING: {model!r} exhausted 5 retries — falling back to {next_model!r}")
                self.fallback_log.append(f"{model_idx}→{model}: exhausted, switching to {next_model}")
        if data is None:
            # Whole-chain failure — by definition an infra problem (every
            # model fell over, swapping models won't help). Categorize from
            # last_err so the operator alert names the actual root cause.
            err_text = (last_err or "").lower()
            if "429" in err_text or "usage limit" in err_text or "rate limit" in err_text:
                category = "quota"
            elif "network" in err_text or "connect" in err_text or "timeout" in err_text:
                category = "network"
            elif "401" in err_text or "403" in err_text or "auth" in err_text:
                category = "auth"
            else:
                category = "unknown"
            raise LLMInfraExhausted(
                f"Ollama failed across all {len(self.models)} models in chain: "
                f"{', '.join(self.models)} — last error: {last_err}",
                category=category,
            )

        # Track token usage for session-end metrics PATCH. Ollama's OpenAI-
        # compatible endpoint returns prompt_tokens / completion_tokens per
        # call; we accumulate so the History tab cost column shows real
        # totals (cost stays None — Ollama Cloud doesn't return $ per call).
        _u = data.get("usage") or {}
        self.total_input_tokens  += int(_u.get("prompt_tokens") or 0)
        self.total_output_tokens += int(_u.get("completion_tokens") or 0)
        self.call_count          += 1

        choice = data["choices"][0]
        message: dict = choice["message"]
        tool_calls = message.get("tool_calls") or []

        # Fallback parser for models that emit tool calls inside `content`.
        if not tool_calls and message.get("content"):
            tool_calls = _try_parse_tool_calls_from_content(message["content"])

        # Normalize to the shape AgentLoop expects.
        return {
            "role":          "assistant",
            "content":       message.get("content") or "",
            "tool_calls":    tool_calls,
            "finish_reason": choice.get("finish_reason", ""),
        }


def _start_self_heartbeat() -> None:
    """
    Start a background daemon thread that POSTs heartbeats from inside the agent
    container. Survives orchestrator restarts (the orchestrator's heartbeat
    thread dies when its container restarts; ours doesn't).
    Requires PM_API_URL + SESSION_UID + product_id resolution via /api/sessions/active.
    """
    import threading, time
    if not PM_API_URL or SESSION_UID == "local":
        return  # No PM API or running standalone — nothing to heartbeat
    def _hb_loop():
        # First, resolve session_id from session_uid by querying PM API.
        session_id = None
        for _ in range(10):
            try:
                resp = httpx.get(f"{PM_API_URL}/api/sessions/active", timeout=5)
                if resp.is_success:
                    sessions = resp.json()
                    for s in sessions if isinstance(sessions, list) else []:
                        if s.get("session_uid") == SESSION_UID:
                            session_id = s.get("id")
                            break
                if session_id:
                    break
            except Exception:
                pass
            time.sleep(3)
        if not session_id:
            _log(f"WARNING: self-heartbeat could not find session for uid={SESSION_UID}")
            return
        _log(f"Self-heartbeat thread started for session_id={session_id}")
        while True:
            try:
                httpx.post(f"{PM_API_URL}/api/sessions/{session_id}/heartbeat", timeout=5)
            except Exception:
                pass  # transient — watchdog has 15min grace
            time.sleep(30)
    t = threading.Thread(target=_hb_loop, daemon=True, name="agent-heartbeat")
    t.start()


def run_agent(initial_prompt: str) -> int:
    """Returns 0 on clean exit, 1 on error, 2 on incomplete.

    Thin wrapper: the loop itself lives in orchestrator.agent_loop.AgentLoop so
    future Claude-API / OpenAI-gateway backends don't have to reimplement it.
    """
    from orchestrator.agent_loop import AgentLoop

    chain_str = ",".join(MODELS) if len(MODELS) > 1 else MODEL
    _log(f"Starting — model={chain_str} persona={AGENT_PERSONA} max_turns={MAX_TURNS}")
    _start_self_heartbeat()

    # Best-effort reachability probe — don't abort on failure since Ollama may
    # still be starting up. Send Bearer auth for Cloud, ignored locally.
    _probe_headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    try:
        resp = httpx.get(f"{OLLAMA_HOST}/api/tags", headers=_probe_headers, timeout=10)
        available = [m["name"] for m in resp.json().get("models", [])]
        if MODEL not in available and not any(MODEL in m for m in available):
            _log(f"WARNING: model '{MODEL}' not found at {OLLAMA_HOST}. Available: {available[:10]}")
    except Exception as e:
        _log(f"WARNING: Could not reach Ollama at {OLLAMA_HOST}: {e}")
        _log("Proceeding anyway — Ollama may still be starting up...")

    system_prompt = (
        "You are an autonomous software agent. Tools available: bash, read_file, "
        "write_file, http_request, task_done.\n\n"
        "RULES (follow strictly):\n"
        "1. Call tools with structured `tool_calls`, not embedded JSON in text.\n"
        "2. Work ONE step at a time. After each tool result, decide the next step.\n"
        "3. You MUST call `task_done` before you stop. Acceptable statuses:\n"
        "   - success: all assigned work done\n"
        "   - blocked: cannot proceed (include a one-line reason in `summary`)\n"
        "   - incomplete: partial progress (include what's done in `summary`)\n"
        "4. If a command fails, read the error and fix ONE thing. Do not re-run the same "
        "failing command twice.\n"
        "5. **Tool selection — critical for efficiency:**\n"
        "   - Use `write_file` for ANY code edit. Read the file with `read_file`, "
        "modify the content in your response, write the new version with `write_file`. "
        "ONE write replaces 10+ sed/awk turns and avoids indentation breakage.\n"
        "   - Use `bash` for git, curl, gh, pytest, ls, mkdir, mv, rm.\n"
        "   - Do NOT use `sed -i` or `awk -i` to edit code — they corrupt indentation "
        "in Python and waste turns.\n"
        "6. Keep outputs short. Use `head -N` or `grep` to limit output from large files.\n\n"
        f"Your working directory is {WORKSPACE_DIR}. If unsure about a path, "
        f"run `bash('ls {WORKSPACE_DIR}')` first."
    )

    backend = _OllamaBackend(
        models=MODELS, chat_url=CHAT_URL,
        timeout=OLLAMA_TIMEOUT, retry_sleep=RETRY_SLEEP,
        api_key=OLLAMA_API_KEY,
    )
    loop = AgentLoop(
        backend=backend,
        tool_specs=TOOLS,
        dispatcher=dispatch_tool,
        max_turns=MAX_TURNS,
        system_prompt=system_prompt,
        log=_log,
    )
    rc = loop.run(initial_prompt)

    # Persist token totals onto the session record so the PM History tab
    # shows real numbers for Ollama runs (cost_usd stays None — Ollama
    # Cloud doesn't return $ per call). Best-effort: skip if PM API
    # unreachable or session_id not found, since the agent has already
    # done its real work.
    _patch_session_metrics(
        input_tokens=backend.total_input_tokens,
        output_tokens=backend.total_output_tokens,
        call_count=backend.call_count,
    )
    return rc


def _patch_session_metrics(input_tokens: int, output_tokens: int, call_count: int) -> None:
    """End-of-run PATCH /api/sessions/{id} with accumulated Ollama token totals."""
    if not PM_API_URL or SESSION_UID == "local":
        _log(f"[metrics] skipping PATCH: PM_API_URL or SESSION_UID unset "
             f"(turns={call_count}, in={input_tokens}, out={output_tokens})")
        return
    if input_tokens == 0 and output_tokens == 0:
        return
    try:
        resp = httpx.get(f"{PM_API_URL}/api/sessions/active", timeout=5)
        sessions = resp.json() if resp.is_success else []
        session_id = next(
            (s.get("id") for s in (sessions if isinstance(sessions, list) else [])
             if s.get("session_uid") == SESSION_UID),
            None,
        )
        if session_id is None:
            _log(f"[metrics] could not resolve session_id for uid={SESSION_UID}; "
                 f"in={input_tokens} out={output_tokens} turns={call_count}")
            return
        httpx.patch(
            f"{PM_API_URL}/api/sessions/{session_id}",
            json={"tokens_input": input_tokens, "tokens_output": output_tokens},
            timeout=5,
        )
        _log(f"[metrics] session_id={session_id} tokens_in={input_tokens} "
             f"tokens_out={output_tokens} turns={call_count}")
    except Exception as e:
        _log(f"[metrics] PATCH failed: {e}")


def _detect_hallucinated_tool_results(content: str) -> str | None:
    """Detect orphan tool-result-shaped JSON in assistant content.

    Real tool results arrive at the model as ``role:"tool"`` messages
    injected by the harness after a real dispatch — they should NEVER
    appear in the assistant's own content. When they do, the model is
    pretending to have run a tool and "continues" the conversation
    against imaginary results. Session 2449 burned dozens of pseudo-
    turns on this pattern before being killed for unrelated reasons;
    bounding it early stops the token cascade.

    Returns a short reason string when hallucination is detected, or
    ``None`` otherwise.

    Conservative criteria:
      1. Content contains both ``"stdout"`` and ``"returncode"`` keys
         in close proximity (≤500 chars apart), AND
      2. After stripping legitimate ``<tool_call>`` wrappers (which the
         shape-repair pass in ``_try_parse_tool_calls_from_content``
         handles separately), the orphan keys are still present.

    Reasoning prose that *mentions* stdout/returncode, fenced code
    blocks that contain them as Python identifiers, and well-formed
    ``<tool_call>`` blocks all pass through cleanly.
    """
    import re
    if "stdout" not in content or "returncode" not in content:
        return None
    # Strip well-formed <tool_call>...</tool_call> blocks first; if the
    # only stdout/returncode mentions live there, the shape-repair pass
    # will handle them and this is NOT a hallucination.
    stripped = re.sub(r'<tool_call>[\s\S]*?</tool_call>', '', content)
    if "stdout" not in stripped or "returncode" not in stripped:
        return None
    # Look for the two keys in close proximity, either order.
    if re.search(
        r'"stdout"\s*:[\s\S]{0,500}"returncode"\s*:'
        r'|"returncode"\s*:[\s\S]{0,200}"stdout"\s*:',
        stripped,
    ):
        return 'orphan {"stdout":..., "returncode":...} in assistant content'
    return None


def _try_parse_tool_calls_from_content(content: str) -> list[dict]:
    """
    Fallback: try to extract JSON tool calls from model content text.
    Some quantized / non-OpenAI-fine-tuned models output tool calls as
    embedded JSON in content instead of via the structured tool_calls field.

    Recognized shapes (tried in order):
        Pattern 1: <tool_call>{"name": "bash", "arguments": {...}}</tool_call>
        Pattern 2: {"name": "bash", "arguments": {...}}
        Pattern 3: {"tool": "bash", "tool_input": {...}}             — Anthropic style
        Pattern 4: {"command": "...", "cwd": "..."}                   — bash inner-args
                   (gpt-oss:120b emits this in place of a real tool call;
                    session 2438 was killed because the nudge counter didn't
                    accept it. Strict shape match: keys ⊆ {command, cwd,
                    timeout}, and "command" required.)

    When a non-trivial shape is repaired, logs a "repaired N call(s) from
    {shape}" line so new variants are easy to spot in production.
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

    # Pattern 3: {"tool": "X", "tool_input": {...}} — one level of nesting
    # allowed for the inner tool_input value.
    results: list[dict] = []
    for m in re.finditer(
        r'(\{(?:[^{}]|\{[^{}]*\})*?"tool"\s*:\s*"(\w+)"(?:[^{}]|\{[^{}]*\})*?\})',
        content,
    ):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict) and "tool" in obj and "tool_input" in obj:
                results.append({
                    "id": f"fallback_{len(results)}",
                    "type": "function",
                    "function": {
                        "name": obj["tool"],
                        "arguments": json.dumps(obj["tool_input"]),
                    },
                })
        except json.JSONDecodeError:
            pass
    if results:
        _log(f"_try_parse_tool_calls_from_content: repaired {len(results)} call(s) "
             f"from {{tool, tool_input}} shape")
        return results

    # Pattern 4: bare {"command": "...", "cwd": "..."} bash inner-args.
    # Strict subset check on keys to avoid false-positives — accept only
    # if every top-level key is in {command, cwd, timeout} and "command"
    # is present. Wraps as a bash tool call.
    _bash_args_allowed = {"command", "cwd", "timeout"}
    for m in re.finditer(r'\{[^{}]*\}', content):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "command" not in obj:
            continue
        if not set(obj.keys()).issubset(_bash_args_allowed):
            continue
        results.append({
            "id": f"fallback_{len(results)}",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps(obj),
            },
        })
    if results:
        _log(f"_try_parse_tool_calls_from_content: repaired {len(results)} call(s) "
             f"from {{command, cwd}} shape (gpt-oss:120b inner-args)")
        return results

    return []


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ollama agent runner for ProductFactory")
    parser.add_argument("-p", "--prompt", required=True, help="Initial prompt")
    args = parser.parse_args()

    exit_code = run_agent(args.prompt)
    sys.exit(exit_code)
