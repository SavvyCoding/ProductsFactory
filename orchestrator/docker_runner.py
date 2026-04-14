"""
Launches a Claude Code session inside an isolated Docker container.

Security model:
  - Isolated bridge network (productfactory-net) — NOT --network host
  - ~/.claude mounted read-only (OAuth session, never writable by container)
  - ~/.ssh mounted read-only (per-repo deploy key injected separately)
  - /workspace bound to product working_dir only
  - No --privileged
  - pm-api resolves to Windows host via host-gateway (PM website in Docker)
"""

import os
import uuid
import subprocess
import logging
import threading
from pathlib import Path

import httpx

from orchestrator.prompts import build_prompt
from orchestrator.alerts import send_alert

log = logging.getLogger("poller.docker")

CLAUDE_DIR  = Path(os.environ.get("CLAUDE_DIR",  "C:/Users/digvi/.claude"))
SSH_DIR     = Path(os.environ.get("SSH_DIR",     "C:/Users/digvi/.ssh"))
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "productfactory-agent")
PM_API_URL  = os.environ["PM_API_URL"]
# Inside the agent container, pm-api is reachable via --add-host as http://pm-api:8080
# The host-side PM_API_URL (localhost:8080) doesn't work inside Docker.
PM_API_URL_CONTAINER = os.environ.get("PM_API_URL_CONTAINER", "http://pm-api:8080")

# Timeout: kill container if it runs longer than this (minutes → seconds)
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "90")) * 60

# Deploy key filename inside SSH_DIR.
# Each product repo has its own key: id_ed25519_{product_name}
# The PM generates this key and adds it as a GitHub deploy key.
DEPLOY_KEY_FILENAME = os.environ.get("DEPLOY_KEY_FILENAME", "id_ed25519_productfactory")

# ── Ollama backend config ─────────────────────────────────────────────────────
# Set AGENT_BACKEND=ollama to use local Ollama instead of the Claude CLI.
# Ollama must be running on the Windows host (accessible as host.docker.internal:11434).
AGENT_BACKEND  = os.environ.get("AGENT_BACKEND", "claude")   # "claude" | "ollama"
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST",   "http://host.docker.internal:11434")
DESIGNER_MODEL       = os.environ.get("DESIGNER_MODEL",       "gemma3:27b")
CODER_MODEL          = os.environ.get("CODER_MODEL",          "qwen3-coder:30b")
MAX_FEATURES_PER_RUN = int(os.environ.get("MAX_FEATURES_PER_RUN", "1"))


def _get_deploy_key_path(product: dict) -> Path | None:
    """
    Returns the deploy key path for this product, falling back to the default key.
    Mounting only the key file (not the whole .ssh dir) preserves the
    known_hosts baked into the image.
    """
    # Per-product key: id_ed25519_{product_name_snake}
    name_slug = (product.get("name") or "").lower().replace(" ", "_").replace("-", "_")
    per_product = SSH_DIR / f"id_ed25519_{name_slug}"
    if per_product.exists():
        return per_product
    # Fall back to the default ProductFactory deploy key
    default_key = SSH_DIR / DEPLOY_KEY_FILENAME
    if default_key.exists():
        return default_key
    log.warning(f"No deploy key found for product '{product.get('name')}' — git push may fail")
    return None


def _rollback_stuck_features(product_id: int, persona: str | None) -> None:
    """Roll back features that were claimed by a session that never completed."""
    stuck_statuses = {
        "designer": ["Designing"],
        "coder":    ["Implementing"],
        "reviewer": [],  # reviewer doesn't change status at start
    }
    rollback_from = stuck_statuses.get(persona or "", ["Designing", "Implementing"])
    if not rollback_from:
        return
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            feats = client.get(f"/api/products/{product_id}/features").json()
            for f in feats:
                if f["status"] in rollback_from:
                    target = "Approved"
                    client.patch(f"/api/features/{f['id']}", json={"status": target})
                    log.info(f"Rolled back feature #{f['id']} '{f['name']}' {f['status']} -> {target}")
    except Exception as e:
        log.warning(f"Could not rollback stuck features: {e}")


def _get_gh_token() -> str | None:
    """Fetch GitHub PAT from system config for GH_TOKEN injection into agent containers."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            return resp.json().get("github_pat") or None
    except Exception:
        return None


def _reset_workspace(working_dir: str, product_name: str) -> None:
    """
    Reset the product workspace to a clean state before each session:
    1. Checkout main (abandon any half-baked feature branch)
    2. Pull latest from origin/main
    3. Delete stale local feature branches
    4. Remove untracked files left by previous sessions
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return  # Not a git repo yet — skip

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True)

    # 1. Switch to main (or master)
    for branch in ("main", "master"):
        r = _run(["git", "checkout", branch])
        if r.returncode == 0:
            break
    else:
        log.warning(f"[{product_name}] Could not checkout main/master — workspace reset skipped")
        return

    # 2. Pull latest (soft fail — repo may not have a remote yet)
    r = _run(["git", "pull", "--ff-only", "origin", "main"])
    if r.returncode != 0:
        _run(["git", "pull", "--ff-only", "origin", "master"])

    # 3. Delete stale local feature branches (not main/master)
    r = _run(["git", "branch"])
    for line in r.stdout.splitlines():
        branch = line.strip().lstrip("* ")
        if branch and branch not in ("main", "master"):
            _run(["git", "branch", "-D", branch])
            log.info(f"[{product_name}] Deleted stale branch: {branch}")

    # 4. Remove untracked files/dirs (junk left by previous sessions)
    _run(["git", "clean", "-fd", "--exclude=output/", "--exclude=Results/"])

    log.info(f"[{product_name}] Workspace reset to clean main")


def run_claude_in_docker(product: dict, persona: str | None = None) -> int:
    """
    Launches the agent container. Blocks until container exits.
    Returns Docker exit code (0 = clean, non-zero = crash/auth failure).
    persona: 'designer', 'coder', 'reviewer', or None (uses legacy routing).
    """
    session_uid = str(uuid.uuid4())[:8]
    working_dir = product["working_dir"]

    # Always reset workspace to clean main before starting a new session.
    # This discards any half-baked code from failed/incomplete previous sessions.
    _reset_workspace(working_dir, product.get("name", str(working_dir)))
    # Per-product override takes precedence over global env default
    effective_max_features = product.get("max_features_per_run") or MAX_FEATURES_PER_RUN
    prompt = build_prompt(product, session_uid, persona=persona)

    lock_path = Path(working_dir) / "session.lock"
    if lock_path.exists():
        log.warning(f"session.lock exists for {product['name']} — skipping (duplicate launch guard)")
        return 1

    # Mount only the deploy key, not the whole .ssh directory.
    # This preserves the known_hosts baked into the image.
    deploy_key = _get_deploy_key_path(product)
    ssh_mount = []
    if deploy_key:
        ssh_mount = ["-v", f"{deploy_key}:/root/.ssh/id_ed25519:ro"]

    # Inject GH_TOKEN so `gh` CLI works inside the container without a separate login
    gh_token = _get_gh_token()
    gh_env = ["-e", f"GH_TOKEN={gh_token}"] if gh_token else []

    persona_env = ["-e", f"AGENT_PERSONA={persona}"] if persona else []

    # Select the agent command based on backend
    if AGENT_BACKEND == "ollama":
        agent_cmd = ["python", "//app/ollama_agent.py", "-p", prompt]
        ollama_env = [
            "-e", f"OLLAMA_HOST={OLLAMA_HOST}",
            "-e", f"DESIGNER_MODEL={DESIGNER_MODEL}",
            "-e", f"CODER_MODEL={CODER_MODEL}",
            "-e", f"MAX_FEATURES_PER_RUN={effective_max_features}",
            "-e", f"OLLAMA_TIMEOUT={os.environ.get('OLLAMA_TIMEOUT', '600')}",
        ]
        # Ollama backend: no Claude OAuth mount needed
        claude_mount = []
        log.info(f"Using Ollama backend — host={OLLAMA_HOST} persona={persona}")
    else:
        agent_cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        ollama_env = ["-e", f"MAX_FEATURES_PER_RUN={effective_max_features}"]
        claude_mount = ["-v", f"{CLAUDE_DIR}:/root/.claude:ro"]

    cmd = [
        "docker", "run", "--rm",
        "--name", f"pf-{product['id']}-{session_uid}",
        "--network", "productfactory-net",
        "--add-host", "pm-api:host-gateway",  # resolves to Windows host where pm-api container exposes :8080
        "--add-host", "host.docker.internal:host-gateway",  # Ollama on Windows host
        "--memory", "4g",
        "--cpus", "2",
        "-v", f"{working_dir}:/workspace",
        *claude_mount,                             # OAuth session (claude backend only)
        *ssh_mount,                                # deploy key :ro (not whole .ssh dir)
        *gh_env,                                   # GH_TOKEN for gh CLI auth
        *persona_env,                              # AGENT_PERSONA for prompt selection
        *ollama_env,                               # Ollama model config (ollama backend only)
        "-e", f"PM_API_URL={PM_API_URL_CONTAINER}",
        "-e", f"SESSION_UID={session_uid}",
        AGENT_IMAGE,
        *agent_cmd,
    ]

    log.info(f"docker run: session={session_uid} product={product['name']}")

    # Record session start
    session_id: int | None = None
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/sessions", json={
                "product_id":  product["id"],
                "session_uid": session_uid,
            })
            resp.raise_for_status()
            session_id = resp.json()["id"]
    except Exception as e:
        log.warning(f"Could not create session record: {e}")

    # Clear old log buffer before starting
    try:
        httpx.delete(f"{PM_API_URL}/api/products/{product['id']}/session/log", timeout=5)
    except Exception:
        pass

    exit_code = 1
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _stream_logs():
            buffer: list[str] = []
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                log.debug(f"[agent] {line}")
                buffer.append(line)
                if len(buffer) >= 10:
                    _post_log_lines(product["id"], buffer)
                    buffer = []
            if buffer:
                _post_log_lines(product["id"], buffer)

        log_thread = threading.Thread(target=_stream_logs, daemon=True)
        log_thread.start()

        try:
            process.wait(timeout=SESSION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log.error(f"Session timed out after {SESSION_TIMEOUT_SECONDS}s — killing container")
            subprocess.run(["docker", "kill", f"pf-{product['id']}-{session_uid}"], capture_output=True)
            send_alert("error", f"{product['name']}: session timed out after {SESSION_TIMEOUT_SECONDS//60}m")
            exit_code = 1
        else:
            exit_code = process.returncode
        finally:
            log_thread.join(timeout=10)

    except Exception as e:
        log.exception(f"docker run failed: {e}")
        exit_code = 1

    # Record session end
    if session_id is not None:
        from datetime import datetime, timezone
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                client.patch(f"/api/sessions/{session_id}", json={
                    "ended_at":    datetime.now(timezone.utc).isoformat(),
                    "exit_code":   exit_code,
                    "container_id": f"pf-{product['id']}-{session_uid}",
                    "persona":     persona,
                })
        except Exception as e:
            log.warning(f"Could not update session record: {e}")

    # Exit code 2 = agent exited cleanly but never called task_done (incomplete session).
    # Roll back any features the agent may have claimed (Designing/Implementing → Approved).
    if exit_code == 2:
        log.warning(f"Incomplete session for {product['name']} (no task_done) — rolling back stuck features")
        _rollback_stuck_features(product["id"], persona)
        return 2

    # After a successful coder session: QA → Security → video → recommender
    if exit_code == 0 and persona == "coder":
        log.info(f"Coder succeeded — launching QA Tester for {product['name']}")
        run_claude_in_docker(product, persona="qa_tester")

        log.info(f"Launching Security Auditor for {product['name']}")
        run_claude_in_docker(product, persona="security_auditor")

        from orchestrator.video_builder import build_product_video, commit_and_push_output
        working_dir = Path(product["working_dir"])
        video_path  = build_product_video(product, working_dir)
        if video_path:
            log.info(f"Product video built: {video_path}")
            commit_and_push_output(working_dir, deploy_key=_get_deploy_key_path(product))
        else:
            log.warning("Product video generation skipped or failed — continuing to recommender")
        log.info(f"Launching recommender for {product['name']}")
        run_claude_in_docker(product, persona="recommender")

    return exit_code


def _post_log_lines(product_id: int, lines: list[str]) -> None:
    """Fire-and-forget: push log lines to PM API for SSE streaming."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.post(f"/api/products/{product_id}/session/log", json={"lines": lines})
    except Exception:
        pass
