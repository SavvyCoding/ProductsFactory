#!/usr/bin/env python3
"""
Local feature test runner.

Runs the agent against a real product working directory without Docker and
without touching the remote git repository.

What runs:   git add / git commit  (local commits — you can inspect with git log)
What's faked: git push / git push --force / gh pr create / gh pr edit
              (intercepted by a shim — logged but not executed)

Usage:
    # Run against an existing product (picks next approved feature from PM API)
    python scripts/test_run.py --product-id 3 --working-dir /path/to/product

    # Create a one-off test feature and run it
    python scripts/test_run.py \\
        --product-id 3 \\
        --working-dir /path/to/product \\
        --feature "Add hello world endpoint" \\
        --desc "GET /hello returns {message: 'hello'}" \\
        --persona coder

    # Designer persona
    python scripts/test_run.py \\
        --product-id 3 \\
        --working-dir /path/to/product \\
        --feature "User auth system" \\
        --persona designer

Environment variables (all optional — fall back to .env defaults):
    PM_API_URL       default: http://localhost:8080
    OLLAMA_HOST      default: http://localhost:11434
    DESIGNER_MODEL   default: gemma3:27b
    CODER_MODEL      default: qwen3-coder:30b
    MAX_TURNS        default: 80
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Allow running from repo root without installing ───────────────────────────
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.prompts import build_prompt


# ── Defaults ──────────────────────────────────────────────────────────────────

PM_API_URL     = os.environ.get("PM_API_URL",     "http://localhost:8080")
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST",    "http://localhost:11434")
DESIGNER_MODEL = os.environ.get("DESIGNER_MODEL", "gemma3:27b")
CODER_MODEL    = os.environ.get("CODER_MODEL",    "qwen3-coder:30b")
MAX_TURNS      = os.environ.get("MAX_TURNS",      "80")

# The agent script to invoke (no Docker — runs on host Python directly)
AGENT_SCRIPT   = REPO_ROOT / "orchestrator" / "ollama_agent.py"


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--product-id",  required=True, type=int, help="Product ID in PM database")
    p.add_argument("--working-dir", required=True, help="Absolute path to the product working directory")
    p.add_argument("--feature",     help="Feature name to create (optional — omit to use existing Approved/Designed features)")
    p.add_argument("--desc",        default="", help="Feature description (used when --feature is given)")
    p.add_argument("--persona",     default="coder", choices=["coder", "designer", "reviewer"],
                   help="Agent persona to run (default: coder)")
    p.add_argument("--allow-push",  action="store_true",
                   help="Allow real git push and gh pr commands (default: intercepted/faked)")
    p.add_argument("--tech-stack",  default="", help="Comma-separated tech stack override, e.g. python,fastapi")
    return p.parse_args()


# ── PM API helpers ────────────────────────────────────────────────────────────

def pm_get(path: str) -> dict | list | None:
    import httpx
    try:
        r = httpx.get(f"{PM_API_URL}{path}", timeout=10)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[test_run] PM API GET {path} failed: {e}", file=sys.stderr)
        return None


def pm_post(path: str, body: dict) -> dict | None:
    import httpx
    try:
        r = httpx.post(f"{PM_API_URL}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[test_run] PM API POST {path} failed: {e}", file=sys.stderr)
        return None


def pm_patch(path: str, body: dict) -> dict | None:
    import httpx
    try:
        r = httpx.patch(f"{PM_API_URL}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[test_run] PM API PATCH {path} failed: {e}", file=sys.stderr)
        return None


def fetch_product(product_id: int) -> dict:
    data = pm_get("/api/products")
    if not data:
        sys.exit(f"[test_run] ERROR: could not fetch products from PM API ({PM_API_URL})")
    match = next((p for p in data if p["id"] == product_id), None)
    if not match:
        sys.exit(f"[test_run] ERROR: product {product_id} not found. Available IDs: {[p['id'] for p in data]}")
    return match


def create_and_approve_feature(product_id: int, name: str, desc: str) -> dict:
    feat = pm_post("/api/features", {
        "product_id": product_id,
        "name": name,
        "description": desc,
        "source": "pm",
        "skip_design": True,   # always skip design in test runs — coder goes straight to code
    })
    if not feat:
        sys.exit("[test_run] ERROR: could not create feature")

    feat_id = feat["id"]
    print(f"[test_run] Created feature #{feat_id}: {name}")

    # Approve it so the coder/designer can pick it up
    import httpx
    try:
        httpx.patch(f"{PM_API_URL}/api/features/{feat_id}",
                    json={"status": "Approved"}, timeout=10)
        print(f"[test_run] Feature #{feat_id} set to Approved")
    except Exception as e:
        print(f"[test_run] WARNING: could not approve feature: {e}", file=sys.stderr)

    return feat


def fetch_batch_features(product_id: int, persona: str) -> list[dict]:
    """Return the features the agent will work on (mirrors poller logic, max 3)."""
    if persona == "coder":
        data = pm_get(f"/api/features/approved?product_id={product_id}")
    elif persona == "designer":
        # Designer picks Approved features that still need a design doc
        data = pm_get(f"/api/features/approved?product_id={product_id}")
        if data:
            data = [f for f in data
                    if f["status"] == "Approved" and not f.get("skip_design")]
    elif persona == "reviewer":
        data = pm_get(f"/api/products/{product_id}/features")
        if data:
            data = [f for f in data
                    if f["status"] == "Reviewing" and f.get("pr_number")]
    else:
        data = []
    return (data or [])[:3]


_STATUS_START = {
    "coder":    "Implementing",
    "designer": "Designing",
    "reviewer": "Reviewing",   # already in Reviewing; no change needed
}
_STATUS_END = {
    "coder":    "Pushed",      # push is faked — mark done directly
    "designer": "Designed",
    "reviewer": "Reviewed",
}


def advance_features(features: list[dict], to_status: str) -> None:
    for feat in features:
        result = pm_patch(f"/api/features/{feat['id']}", {"status": to_status})
        if result:
            print(f"[test_run] Feature #{feat['id']} '{feat['name']}' -> {to_status}")
        else:
            print(f"[test_run] WARNING: could not update feature #{feat['id']} to {to_status}",
                  file=sys.stderr)


# ── Git shim ─────────────────────────────────────────────────────────────────

GIT_SHIM = """\
#!/bin/sh
# Fake git shim — intercepts network operations for local test runs.
# git push / git push --force → no-op (logged only)
# Everything else → real git

case "$1" in
  push)
    echo "[NO-PUSH] git $@  (intercepted by test_run.py shim)" >&2
    exit 0
    ;;
  *)
    exec "$(which git 2>/dev/null || echo /usr/bin/git)" "$@"
    ;;
esac
"""

GH_SHIM = """\
#!/bin/sh
# Fake gh shim — intercepts PR creation for local test runs.
echo "[NO-GH]   gh $@  (intercepted by test_run.py shim)" >&2
exit 0
"""


def make_shim_dir(allow_push: bool) -> str | None:
    """Create a temp dir with fake git/gh shims. Returns the dir path, or None."""
    if allow_push:
        return None

    shim_dir = tempfile.mkdtemp(prefix="pf_test_shims_")
    for name, content in [("git", GIT_SHIM), ("gh", GH_SHIM)]:
        p = Path(shim_dir) / name
        p.write_text(content, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return shim_dir


# ── Results logging ───────────────────────────────────────────────────────────

def setup_log(working_dir: Path, persona: str) -> Path:
    results_dir = working_dir / "Results"
    results_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = results_dir / f"test_run_{persona}_{ts}.log"
    return log_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    working_dir = Path(args.working_dir).resolve()

    if not working_dir.exists():
        sys.exit(f"[test_run] ERROR: working dir not found: {working_dir}")

    print(f"[test_run] Product ID : {args.product_id}")
    print(f"[test_run] Working dir: {working_dir}")
    print(f"[test_run] Persona    : {args.persona}")
    print(f"[test_run] PM API     : {PM_API_URL}")
    print(f"[test_run] Ollama     : {OLLAMA_HOST}")

    # Fetch product record (for tech_stack etc.)
    product = fetch_product(args.product_id)
    if args.tech_stack:
        product["tech_stack"] = [s.strip() for s in args.tech_stack.split(",")]
    product["working_dir"] = str(working_dir)

    # Optionally create a test feature
    if args.feature:
        create_and_approve_feature(args.product_id, args.feature, args.desc)

    # Snapshot which features the agent will work on BEFORE it starts
    batch = fetch_batch_features(args.product_id, args.persona)
    if batch:
        names = ", ".join(f"#{f['id']} {f['name']}" for f in batch)
        print(f"[test_run] Batch      : {names}")
    else:
        print("[test_run] WARNING: no ready features found for this persona", file=sys.stderr)

    # Build the agent prompt (same logic as the real poller)
    session_uid = f"test-{uuid.uuid4().hex[:8]}"
    prompt = build_prompt(product, session_uid, persona=args.persona)

    # Show a preview of the prompt
    preview = prompt[:400].replace("\n", " | ")
    print(f"\n[test_run] Prompt preview:\n  {preview}...\n")

    # Set up git shims
    shim_dir = make_shim_dir(args.allow_push)
    if shim_dir:
        print(f"[test_run] Git shims  : {shim_dir}  (push + gh pr are no-ops)")
    else:
        print("[test_run] WARNING: --allow-push set — real git push will run!")

    # Log file
    log_path = setup_log(working_dir, args.persona)
    print(f"[test_run] Log file   : {log_path}\n")

    # Environment for the agent process
    env = os.environ.copy()
    env.update({
        "PM_API_URL":           PM_API_URL,
        "PM_API_URL_CONTAINER": PM_API_URL,  # local run: container URL must also resolve on host
        "OLLAMA_HOST":          OLLAMA_HOST,
        "DESIGNER_MODEL":       DESIGNER_MODEL,
        "CODER_MODEL":          CODER_MODEL,
        "AGENT_PERSONA":        args.persona,
        "SESSION_UID":          session_uid,
        "MAX_TURNS":            MAX_TURNS,
        # Tell the agent its working dir (ollama_agent.py runs on host, not in /workspace)
        "WORKSPACE_DIR":        str(working_dir),
        "PYTHONIOENCODING":     "utf-8",
        "PYTHONUTF8":           "1",
    })

    # Find the Python executable (use venv if available)
    venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable

    # Add venv Scripts/bin to PATH so bash subshells can find python/pytest
    venv_bin = Path(python_exe).parent
    path_parts = [str(venv_bin)]
    if shim_dir:
        path_parts.append(shim_dir)   # shim dir first so fake git wins
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(path_parts)

    cmd = [python_exe, str(AGENT_SCRIPT), "-p", prompt]

    # Claim features — advance to in-progress status so PM dashboard reflects reality
    start_status = _STATUS_START.get(args.persona)
    if batch and start_status and args.persona != "reviewer":
        advance_features(batch, start_status)

    # Record session start
    session_id: int | None = None
    try:
        resp = pm_post("/api/sessions", {"product_id": args.product_id, "session_uid": session_uid})
        if resp:
            session_id = resp["id"]
            print(f"[test_run] Session ID : {session_id}")
    except Exception as e:
        print(f"[test_run] WARNING: could not create session record: {e}", file=sys.stderr)

    print(f"[test_run] Starting agent... (Ctrl+C to interrupt)\n{'-'*60}")

    # Run agent, tee output to log file
    with open(log_path, "w", encoding="utf-8") as log_fh:
        log_fh.write(f"# test_run session: {session_uid}\n")
        log_fh.write(f"# started: {datetime.now(timezone.utc).isoformat()}\n")
        log_fh.write(f"# persona: {args.persona}\n")
        log_fh.write(f"# product_id: {args.product_id}\n")
        log_fh.write(f"# working_dir: {working_dir}\n\n")

        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(working_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log_fh.write(line)

            proc.wait()
            exit_code = proc.returncode

        except KeyboardInterrupt:
            print("\n[test_run] Interrupted by user")
            proc.terminate()
            exit_code = 130

    print(f"\n{'-'*60}")
    print(f"[test_run] Agent exited with code {exit_code}")
    print(f"[test_run] Log saved to: {log_path}")

    # Record session end
    if session_id is not None:
        import httpx
        try:
            httpx.patch(f"{PM_API_URL}/api/sessions/{session_id}", json={
                "ended_at":  datetime.now(timezone.utc).isoformat(),
                "exit_code": exit_code,
                "persona":   args.persona,
            }, timeout=10)
        except Exception as e:
            print(f"[test_run] WARNING: could not update session record: {e}", file=sys.stderr)

    # Show what changed in git
    try:
        diff = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=str(working_dir),
            capture_output=True, text=True,
        )
        if diff.stdout.strip():
            print(f"\n[test_run] Recent commits in {working_dir.name}:")
            print(diff.stdout.rstrip())

        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(working_dir),
            capture_output=True, text=True,
        )
        if status.stdout.strip():
            print(f"\n[test_run] Uncommitted changes:")
            print(status.stdout.rstrip())
    except Exception:
        pass  # git not available or not a repo — fine

    # ── Run tests and show clear PASS/FAIL ────────────────────────────────────
    test_exit = _run_tests(product, working_dir, python_exe, log_path)

    # Clean up shim dir
    if shim_dir:
        import shutil
        shutil.rmtree(shim_dir, ignore_errors=True)

    # Advance feature statuses based on outcome
    end_status = _STATUS_END.get(args.persona)
    if batch and end_status:
        if exit_code == 0 and test_exit == 0:
            advance_features(batch, end_status)
        elif start_status and args.persona != "reviewer":
            # Roll back to Approved/Designed so the feature can be retried
            rollback = "Approved" if args.persona == "coder" else "Approved"
            advance_features(batch, rollback)
            print(f"[test_run] Features rolled back to {rollback} (agent failed)")

    overall = exit_code == 0 and test_exit == 0
    if overall:
        print("\n" + "=" * 60)
        print("  RESULT: PASS  — agent succeeded and all tests passed")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        if exit_code != 0:
            print(f"  RESULT: FAIL  — agent exited with code {exit_code}")
        else:
            print(f"  RESULT: FAIL  — agent succeeded but tests failed (exit {test_exit})")
        print("=" * 60)

    # After a successful coder run: build product video → push → recommend
    if overall and args.persona == "coder":
        _build_and_push_video(product, working_dir, allow_push=args.allow_push)
        _run_recommender(args.product_id, product, python_exe, env)

    sys.exit(0 if overall else 1)


def _build_and_push_video(product: dict, working_dir: Path, allow_push: bool = False) -> None:
    """Build product showcase video, commit output/, and push to origin."""
    from orchestrator.video_builder import build_product_video, commit_and_push_output

    print(f"\n[test_run] {'-'*60}")
    print("[test_run] Building product video...")

    video_path = build_product_video(product, working_dir)
    if not video_path:
        print("[test_run] WARNING: video generation skipped or failed — install Pillow gtts moviepy",
              file=sys.stderr)
        return

    print(f"[test_run] Video saved : {video_path}")

    if allow_push:
        ok = commit_and_push_output(working_dir)
        if ok:
            print("[test_run] output/ committed and pushed to GitHub")
        else:
            print("[test_run] WARNING: git push of output/ failed", file=sys.stderr)
    else:
        # Commit locally; skip real push (shims intercept it anyway)
        from orchestrator.video_builder import commit_and_push_output as _c
        import subprocess as _sp
        try:
            _sp.run(["git", "add", "output/"], cwd=str(working_dir), check=True, capture_output=True)
            diff = _sp.run(["git", "diff", "--cached", "--quiet"], cwd=str(working_dir), capture_output=True)
            if diff.returncode != 0:
                _sp.run(
                    ["git", "commit", "-m", "chore: add product video to output/"],
                    cwd=str(working_dir), check=True, capture_output=True,
                )
                print("[test_run] output/ committed locally (push skipped — use --allow-push to push)")
            else:
                print("[test_run] output/ — nothing new to commit")
        except Exception as e:
            print(f"[test_run] WARNING: could not commit output/: {e}", file=sys.stderr)


def _run_recommender(product_id: int, product: dict, python_exe: str, env: dict) -> None:
    """Spawn the recommender agent to suggest new features after a successful coder session."""
    from orchestrator.prompts import build_prompt
    rec_uid = f"rec-{uuid.uuid4().hex[:8]}"
    prompt  = build_prompt(product, rec_uid, persona="recommender")

    print(f"\n[test_run] {'-'*60}")
    print(f"[test_run] Launching recommender (session {rec_uid})...")
    print(f"[test_run] {'-'*60}")

    rec_env = {**env, "AGENT_PERSONA": "recommender", "SESSION_UID": rec_uid}

    # Record recommender session start
    rec_session_id: int | None = None
    try:
        resp = pm_post("/api/sessions", {"product_id": product_id, "session_uid": rec_uid})
        if resp:
            rec_session_id = resp["id"]
    except Exception:
        pass

    try:
        proc = subprocess.Popen(
            [python_exe, str(AGENT_SCRIPT), "-p", prompt],
            env=rec_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait()
        rec_exit = proc.returncode
    except KeyboardInterrupt:
        proc.terminate()
        rec_exit = 130

    print(f"[test_run] Recommender exited with code {rec_exit}")

    if rec_session_id is not None:
        try:
            import httpx
            httpx.patch(f"{env['PM_API_URL']}/api/sessions/{rec_session_id}", json={
                "ended_at":  datetime.now(timezone.utc).isoformat(),
                "exit_code": rec_exit,
                "persona":   "recommender",
            }, timeout=10)
        except Exception:
            pass


def _run_tests(product: dict, working_dir: Path, python_exe: str, log_path: Path) -> int:
    """Run the product's test command and print a clear summary. Returns exit code."""
    # Determine test command: product config > stack default > skip
    config      = product.get("config") or {}
    tech_stack  = (product.get("tech_stack") or [])
    stack       = (tech_stack[0].lower() if tech_stack else "")

    if config.get("test_command"):
        test_cmd_str = config["test_command"]
    elif stack == "python":
        test_cmd_str = f"{python_exe} -m pytest TestCases/ -v --tb=short"
    elif stack == "node":
        test_cmd_str = "npm test"
    elif stack == "go":
        test_cmd_str = "go test ./..."
    else:
        # Try to find any pytest-able tests
        if list(working_dir.rglob("test_*.py")) or list(working_dir.rglob("*_test.py")):
            test_cmd_str = f"{python_exe} -m pytest . -v --tb=short"
        else:
            print("\n[test_run] No test command configured and no test files found — skipping")
            return 0

    print(f"\n[test_run] Running tests: {test_cmd_str}")
    print("-" * 60)

    result = subprocess.run(
        test_cmd_str,
        shell=True,
        cwd=str(working_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    # Append test output to the session log
    with open(log_path, "a", encoding="utf-8") as lf:
        lf.write(f"\n\n# ── Test run ──\n# command: {test_cmd_str}\n# exit: {result.returncode}\n")

    return result.returncode


if __name__ == "__main__":
    main()
