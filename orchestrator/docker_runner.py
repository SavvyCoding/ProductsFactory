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
import shutil
import tempfile
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
    """
    Roll back features that were claimed by a session that never completed.
    Only resets features with NO evidence of completion (no PR, not in session_result.json).
    Features that have pr_number set are left alone — they're already in Reviewing.
    """
    stuck_statuses = {
        "designer": ["Designing"],
        "coder":    ["Implementing"],
        "reviewer": [],
    }
    rollback_from = stuck_statuses.get(persona or "", ["Designing", "Implementing"])
    if not rollback_from:
        return
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            feats = client.get(f"/api/products/{product_id}/features").json()
            for f in feats:
                if f["status"] in rollback_from and not f.get("pr_number"):
                    client.patch(f"/api/features/{f['id']}", json={"status": "Approved"})
                    log.info(f"Rolled back feature #{f['id']} '{f['name']}' {f['status']} -> Approved")
    except Exception as e:
        log.warning(f"Could not rollback stuck features: {e}")


def _reconcile_session_result(working_dir: str, product_id: int, exit_code: int) -> None:
    """
    Read session_result.json written by the agent and apply all status updates
    via the PM API. This runs AFTER the container exits — status authority lives
    here, not inside the container, so kills/crashes can't leave status wrong.

    File format (agent writes this incrementally after each feature):
      {
        "features": [
          {"id": 21, "status": "Reviewing", "pr_number": 5, "pr_url": "https://..."},
          {"id": 22, "status": "Designed",  "design_doc_path": "docs/feature_022_design.md"},
          {"id": 25, "status": "Blocked",   "blocked_reason": "tests failing"}
        ]
      }

    After applying, the file is deleted so it doesn't affect the next session.
    """
    result_file = Path(working_dir) / "session_result.json"
    if not result_file.exists():
        return

    import json as _json
    try:
        data = _json.loads(result_file.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read session_result.json: {e}")
        return

    features = data.get("features", [])
    if not features:
        result_file.unlink(missing_ok=True)
        return

    applied = 0
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for entry in features:
                fid = entry.get("id")
                if not fid:
                    continue
                patch_body = {k: v for k, v in entry.items() if k != "id"}
                try:
                    client.patch(f"/api/features/{fid}", json=patch_body)
                    log.info(f"[reconcile] Feature #{fid} → {patch_body.get('status', '?')}")
                    applied += 1
                except Exception as fe:
                    log.warning(f"[reconcile] Could not update feature #{fid}: {fe}")
    except Exception as e:
        log.warning(f"[reconcile] PM API error: {e}")

    log.info(f"[reconcile] Applied {applied}/{len(features)} feature updates from session_result.json")
    try:
        result_file.unlink(missing_ok=True)
    except Exception:
        pass


def _get_gh_token() -> str | None:
    """Fetch GitHub PAT from system config for GH_TOKEN injection into agent containers."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            return resp.json().get("github_pat") or None
    except Exception:
        return None


def _get_system_config_sync() -> dict:
    """Fetch current system config from PM API. Returns empty dict on failure."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.warning(f"Could not fetch system config: {e}")
        return {}


def _get_claude_profile(sys_cfg: dict) -> tuple[str, str]:
    """
    Returns (credentials_dir, claude_model) from system config with sensible defaults.
    credentials_dir: falls back to CLAUDE_DIR env var.
    claude_model: falls back to 'claude-sonnet-4-6'.
    """
    credentials_dir = sys_cfg.get("claude_credentials_dir") or str(CLAUDE_DIR)
    claude_model = sys_cfg.get("claude_model") or "claude-sonnet-4-6"
    return credentials_dir, claude_model


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

    # Read backend from DB config (overrides env var)
    sys_cfg = _get_system_config_sync()
    effective_backend = (sys_cfg.get("agent_backend") or AGENT_BACKEND)

    # Guard: check DB for an already-running session for this product.
    # Using the DB (not docker ps) means the check survives poller restarts.
    product_id = product["id"]
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/sessions/active", params={"product_id": product_id})
            active = resp.json()
            if active:
                log.warning(
                    f"Active session in DB for {product['name']} "
                    f"(container={active.get('container_id')} persona={active.get('persona')}) — skipping"
                )
                return 1
    except Exception as e:
        log.warning(f"Could not check active session in DB: {e} — falling back to docker ps")
        # Fallback to docker ps if API is unreachable
        running = subprocess.run(
            ["docker", "ps", "--filter", f"name=pf-{product_id}-", "--format", "{{.Names}}"],
            capture_output=True, text=True
        ).stdout.strip()
        if running:
            log.warning(f"Container already running for product {product_id} ({running}) — skipping")
            return 1

    # Mount only the deploy key, not the whole .ssh directory.
    # This preserves the known_hosts baked into the image.
    deploy_key = _get_deploy_key_path(product)
    ssh_mount = []
    if deploy_key:
        ssh_mount = ["-v", f"{deploy_key}:/home/agent/.ssh/id_ed25519:ro"]

    # Inject GH_TOKEN so `gh` CLI works inside the container without a separate login
    gh_token = _get_gh_token()
    gh_env = ["-e", f"GH_TOKEN={gh_token}"] if gh_token else []

    persona_env = ["-e", f"AGENT_PERSONA={persona}"] if persona else []

    # Select the agent command based on backend
    _tmp_claude_dir: str | None = None
    if effective_backend == "ollama":
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
        # Claude backend: copy credentials to a temp dir and mount the copy
        creds_src, claude_model = _get_claude_profile(sys_cfg)
        try:
            _tmp_claude_dir = tempfile.mkdtemp(prefix="pf_claude_creds_")
            src_path = Path(creds_src)
            if src_path.exists():
                # Copy contents into the temp dir
                shutil.copytree(str(src_path), _tmp_claude_dir, dirs_exist_ok=True)
                log.info(f"Copied Claude credentials from {creds_src} to {_tmp_claude_dir}")
                # Ensure settings.json has the permissions + model we want
                import json as _json
                settings_path = Path(_tmp_claude_dir) / "settings.json"
                try:
                    settings = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
                    settings["skipDangerousModePermissionPrompt"] = True
                    settings["model"] = claude_model
                    settings_path.write_text(_json.dumps(settings, indent=2))
                except Exception as se:
                    log.warning(f"Could not patch settings.json: {se}")
            else:
                log.warning(f"Claude credentials dir not found: {creds_src} — container may fail auth")
        except Exception as e:
            log.warning(f"Could not copy Claude credentials: {e} — falling back to direct mount")
            _tmp_claude_dir = None

        mount_dir = _tmp_claude_dir or creds_src
        # Container now runs as non-root 'agent' user — home is /home/agent
        claude_mount = ["-v", f"{mount_dir}:/home/agent/.claude:ro"]

        # Also mount .claude.json (sits alongside .claude/ in the host home dir)
        creds_parent = str(Path(creds_src).parent)
        claude_json_src = str(Path(creds_parent) / ".claude.json")
        if Path(claude_json_src).exists():
            claude_mount += ["-v", f"{claude_json_src}:/home/agent/.claude.json:ro"]
        else:
            # Restore from backup inside the .claude dir
            backup_dir = Path(mount_dir) / "backups"
            if backup_dir.exists():
                backups = sorted(backup_dir.glob(".claude.json.backup.*"))
                if backups and _tmp_claude_dir:
                    shutil.copy2(str(backups[-1]), str(Path(_tmp_claude_dir) / ".claude.json"))
                    claude_mount += ["-v", f"{_tmp_claude_dir}/.claude.json:/home/agent/.claude.json:ro"]
                    log.info(f"Restored .claude.json from backup: {backups[-1].name}")

        # --dangerously-skip-permissions works now that container runs as non-root
        agent_cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        ollama_env = [
            "-e", f"MAX_FEATURES_PER_RUN={effective_max_features}",
            "-e", f"CLAUDE_MODEL={claude_model}",
        ]

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

    # Record session start — include container_id, persona, backend upfront so
    # the DB is queryable immediately (used by active-session guard on next poll).
    session_id: int | None = None
    container_name = f"pf-{product['id']}-{session_uid}"
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/sessions", json={
                "product_id":   product["id"],
                "session_uid":  session_uid,
                "container_id": container_name,
                "persona":      persona,
                "backend":      effective_backend,
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

    except FileNotFoundError:
        log.error("'docker' not found in PATH — is Docker installed and on PATH?")
        exit_code = 1
    except PermissionError as e:
        log.error(f"Permission denied running docker: {e}")
        exit_code = 1
    except Exception as e:
        log.exception(f"docker run failed unexpectedly: {e}")
        exit_code = 1
    finally:
        # Clean up temp credentials copy if we created one
        if _tmp_claude_dir:
            try:
                shutil.rmtree(_tmp_claude_dir, ignore_errors=True)
            except Exception:
                pass

    # ── Post-exit reconciliation (runs regardless of exit code) ─────────────────
    # 1. Apply status updates from session_result.json (written by agent incrementally).
    #    This is the authoritative status update — runs outside the container so it
    #    can never be skipped by a kill/crash.
    _reconcile_session_result(working_dir, product["id"], exit_code)

    # 2. Record session end in DB.
    if session_id is not None:
        from datetime import datetime, timezone
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                client.patch(f"/api/sessions/{session_id}", json={
                    "ended_at":  datetime.now(timezone.utc).isoformat(),
                    "exit_code": exit_code,
                })
        except Exception as e:
            log.warning(f"Could not update session record: {e}")

    # 3. Roll back any intermediate-state features with no PR evidence.
    #    Covers cases where agent claimed a feature but never finished it.
    #    (Features with pr_number are left alone — they're already Reviewing.)
    if exit_code != 0:
        log.warning(f"Non-zero exit ({exit_code}) for {product['name']} — rolling back incomplete features")
        _rollback_stuck_features(product["id"], persona)

    if exit_code == 2:
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
