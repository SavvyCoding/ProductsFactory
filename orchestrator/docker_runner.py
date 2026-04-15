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
from datetime import datetime, timezone
from pathlib import Path

import httpx

from orchestrator.prompts import build_prompt
from orchestrator.alerts import send_alert
from templates.renderer import install_templates

log = logging.getLogger("poller.docker")

CLAUDE_DIR  = Path(os.environ.get("CLAUDE_DIR",  ""))   # configurable via system_config.claude_credentials_dir
SSH_DIR     = Path(os.environ.get("SSH_DIR",     ""))   # configurable via system_config.ssh_keys_dir
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "productfactory-agent")
PM_API_URL  = os.environ["PM_API_URL"]
# Inside the agent container, pm-api is reachable via --add-host as http://pm-api:8080
# The host-side PM_API_URL (localhost:8080) doesn't work inside Docker.
PM_API_URL_CONTAINER = os.environ.get("PM_API_URL_CONTAINER", "http://pm-api:8080")

# Timeout default — overridden at runtime by system_config.session_timeout_minutes
_DEFAULT_SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "90")) * 60

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


def _get_deploy_key_path(product: dict, ssh_dir: Path | None = None) -> Path | None:
    """
    Returns the deploy key path for this product, falling back to the default key.
    Mounting only the key file (not the whole .ssh dir) preserves the
    known_hosts baked into the image.
    ssh_dir: resolved from sys_cfg.ssh_keys_dir → SSH_DIR env var — passed at call site.
    """
    resolved_ssh_dir = ssh_dir or SSH_DIR
    if not resolved_ssh_dir or not resolved_ssh_dir.exists():
        log.warning(f"SSH keys directory not configured or missing: {resolved_ssh_dir}")
        return None
    name_slug = (product.get("name") or "").lower().replace(" ", "_").replace("-", "_")
    per_product = resolved_ssh_dir / f"id_ed25519_{name_slug}"
    if per_product.exists():
        return per_product
    default_key = resolved_ssh_dir / DEPLOY_KEY_FILENAME
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


def _read_session_result(working_dir: str) -> list[dict]:
    """
    Read and parse session_result.json (newline-delimited JSON — one entry per line).
    Returns list of feature dicts. Skips blank or malformed lines.
    """
    import json as _json
    result_file = Path(working_dir) / "session_result.json"
    if not result_file.exists():
        return []
    entries = []
    try:
        for line in result_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(_json.loads(line))
            except Exception:
                pass  # skip malformed lines (e.g. partial write mid-line)
    except Exception as e:
        log.warning(f"Could not read session_result.json: {e}")
    return entries


def _delete_session_result(working_dir: str) -> None:
    try:
        (Path(working_dir) / "session_result.json").unlink(missing_ok=True)
    except Exception:
        pass


def _apply_session_entry(client: httpx.Client, entry: dict) -> bool:
    """PATCH a single session_result entry to the PM API. Returns True on success."""
    fid = entry.get("id")
    if not fid:
        return False
    patch_body = {k: v for k, v in entry.items() if k not in ("id", "confidence")}
    try:
        client.patch(f"/api/features/{fid}", json=patch_body)
        log.info(f"[progress] Feature #{fid} -> {patch_body.get('status', '?')}")
        return True
    except Exception as e:
        log.warning(f"[progress] Could not update feature #{fid}: {e}")
        return False


def _live_poll_session_result(working_dir: str, stop_event: threading.Event) -> None:
    """
    Background thread: polls session_result.json every 30 s while the container runs.
    Applies new NDJSON lines to the DB in real-time as the agent writes phase transitions.
    Tracks applied lines by index so each entry is applied exactly once.
    """
    import json as _json
    result_file = Path(working_dir) / "session_result.json"
    applied_up_to = 0  # number of lines already applied this session

    while not stop_event.wait(30):  # poll every 30 s; exits when stop_event is set
        if not result_file.exists():
            continue
        try:
            lines = result_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        new_lines = lines[applied_up_to:]
        if not new_lines:
            continue

        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        applied_up_to += 1
                        continue
                    try:
                        entry = _json.loads(line)
                        _apply_session_entry(client, entry)
                    except Exception:
                        pass  # malformed line — skip, don't block the rest
                    applied_up_to += 1
        except Exception as e:
            log.debug(f"[live-poll] PM API error: {e}")


def _reconcile_session_result(working_dir: str, product_id: int, exit_code: int,
                               features: list[dict] | None = None) -> None:
    """
    Final reconcile after container exits: re-applies all entries in session_result.json.
    Idempotent — safe to re-apply entries the live-poll thread already sent.
    Handles auto-merge decisions (passed in via features) and then deletes the file.
    """
    if features is None:
        features = _read_session_result(working_dir)

    if not features:
        _delete_session_result(working_dir)
        return

    applied = 0
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for entry in features:
                if _apply_session_entry(client, entry):
                    applied += 1
    except Exception as e:
        log.warning(f"[reconcile] PM API error: {e}")

    log.info(f"[reconcile] Final reconcile: {applied}/{len(features)} feature updates applied")
    _delete_session_result(working_dir)


def _auto_merge_approved(product: dict, features: list[dict]) -> list[dict]:
    """
    For reviewer sessions with auto_merge_enabled: process approved features.
    - High confidence: attempt GitHub merge → update entry to Pushed
    - PR closed/conflicted: close PR, update entry to Implementing (clears pr_number)
    - Low confidence: leave entry unchanged (stays Reviewed for human sign-off)

    Takes the session features list, modifies entries in-place, and returns it
    so reconcile applies the final authoritative state.
    """
    gh_token = _get_gh_token()
    if not gh_token:
        log.warning("[auto-merge] No GitHub PAT configured — skipping auto-merge")
        return features

    github_repo = product.get("github_repo", "")
    if not github_repo:
        log.warning("[auto-merge] Product has no github_repo — skipping auto-merge")
        return features

    repo_slug = _parse_repo_slug(github_repo)
    gh_headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}

    for entry in features:
        if entry.get("review_outcome") != "approved":
            continue

        fid = entry.get("id")
        pr_number = entry.get("pr_number")

        if not pr_number:
            # Fetch pr_number from DB if not in session entry
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    feat = client.get(f"/api/features/{fid}").json()
                    pr_number = feat.get("pr_number")
                    entry["pr_number"] = pr_number
            except Exception as e:
                log.warning(f"[auto-merge] Could not fetch feature #{fid}: {e}")
                continue

        if not pr_number:
            log.warning(f"[auto-merge] Feature #{fid} has no PR number — skipping")
            continue

        # Check current PR state on GitHub before doing anything
        try:
            pr_resp = httpx.get(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}",
                headers=gh_headers, timeout=10,
            )
            if pr_resp.status_code == 200:
                pr_data = pr_resp.json()
                pr_state = pr_data.get("state")   # "open" | "closed"
                merged_at = pr_data.get("merged_at")  # None if not merged
            else:
                pr_state, merged_at = None, None
        except Exception as e:
            log.warning(f"[auto-merge] Could not check PR #{pr_number} state: {e}")
            pr_state, merged_at = None, None

        # Already merged on GitHub — just mark Pushed
        if merged_at:
            log.info(f"[auto-merge] PR #{pr_number} already merged — marking feature #{fid} Pushed")
            entry.update({"status": "Pushed", "pr_number": None})
            continue

        # PR closed but not merged — re-queue to Implementing
        if pr_state == "closed":
            log.info(f"[auto-merge] PR #{pr_number} closed (not merged) — re-queuing feature #{fid} to Implementing")
            entry.update({
                "status": "Implementing",
                "pr_number": None,
                "review_outcome": None,
                "review_notes": f"PR #{pr_number} was closed without merging — coder will rebase and reopen.",
            })
            continue

        # Low confidence — leave as Reviewed for human sign-off
        if entry.get("confidence", "low") != "high":
            log.info(f"[auto-merge] Feature #{fid} approved but low-confidence — leaving for human review")
            continue

        # PR is open + high confidence — attempt merge
        log.info(f"[auto-merge] Merging PR #{pr_number} for feature #{fid} (high-confidence)")
        try:
            resp = httpx.put(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}/merge",
                json={"merge_method": "squash", "commit_title": f"feat: auto-merge PR #{pr_number} [ProductFactory]"},
                headers=gh_headers, timeout=15,
            )
            if resp.status_code in (200, 201):
                log.info(f"[auto-merge] PR #{pr_number} merged successfully")
                entry.update({"status": "Pushed", "pr_number": None})
            else:
                body = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
                gh_msg = body.get("message", resp.text[:120])
                log.warning(f"[auto-merge] GitHub {resp.status_code} for PR #{pr_number}: {gh_msg}")
                if resp.status_code == 405 or "not mergeable" in gh_msg.lower():
                    # Conflicts — close PR and re-queue
                    httpx.patch(
                        f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}",
                        json={"state": "closed"}, headers=gh_headers, timeout=10,
                    )
                    httpx.post(
                        f"https://api.github.com/repos/{repo_slug}/issues/{pr_number}/comments",
                        json={"body": "Closing due to merge conflicts — ProductFactory will rebase and reopen."},
                        headers=gh_headers, timeout=10,
                    )
                    entry.update({
                        "status": "Implementing",
                        "pr_number": None,
                        "review_outcome": None,
                        "review_notes": f"Merge failed: conflicts (GitHub {resp.status_code}). Coder must rebase.",
                    })
        except Exception as e:
            log.warning(f"[auto-merge] Error merging PR #{pr_number}: {e}")

    return features


def _parse_repo_slug(github_repo: str) -> str:
    """Extract 'owner/repo' from a GitHub URL for API calls."""
    import re
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    return m.group(1) if m else github_repo


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


def _read_session_summary(working_dir: str) -> str:
    """
    Read context from the previous agent session.
    Primary: session_summary.md (incremental log written throughout the session).
    Fallback: last 50 lines of progress.md if summary is absent (first session, or crash
    before any summary lines were written).
    Returns empty string if neither file exists.
    """
    summary_file = Path(working_dir) / "session_summary.md"
    progress_file = Path(working_dir) / "progress.md"

    def _read_truncated(path: Path, max_chars: int = 2000) -> str:
        try:
            content = path.read_text(encoding="utf-8").strip()
            if len(content) > max_chars:
                content = content[:max_chars - 50] + "\n...[truncated]"
            return content
        except Exception as e:
            log.warning(f"Could not read {path.name}: {e}")
            return ""

    if summary_file.exists():
        content = _read_truncated(summary_file)
        if content:
            return content

    # Fallback: last 50 lines of progress.md (already written incrementally by coder)
    if progress_file.exists():
        try:
            lines = progress_file.read_text(encoding="utf-8").splitlines()
            tail = "\n".join(lines[-50:])
            if tail.strip():
                log.info("[context] session_summary.md absent — falling back to progress.md tail")
                return f"[From progress.md — last session]\n\n{tail}"
        except Exception as e:
            log.warning(f"Could not read progress.md fallback: {e}")

    return ""


def _fetch_assigned_features(product_id: int, persona: str | None, max_count: int) -> list[dict]:
    """
    Pre-fetch features the agent should work on this session.
    Poller selects features — agent no longer self-discovers via API.
    Returns [] for personas that manage their own work (qa_tester, recommender, etc.).
    """
    if persona not in ("coder", "designer", "reviewer"):
        return []
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            if persona == "coder":
                resp = client.get("/api/features/approved", params={"product_id": product_id})
            else:
                # designer + reviewer: get all features for product and filter client-side
                resp = client.get(f"/api/products/{product_id}/features")
            resp.raise_for_status()
            all_features = resp.json()

        if persona == "designer":
            features = [f for f in all_features
                        if f.get("status") == "Approved" and not f.get("skip_design")]
        elif persona == "reviewer":
            features = [f for f in all_features
                        if f.get("status") == "Reviewing" and f.get("pr_number")]
        else:
            features = all_features  # coder: already filtered by /approved endpoint

        selected = features[:max_count]
        log.info(f"[assign] persona={persona} assigned {len(selected)}/{len(features)} features")
        return [
            {
                "id": f["id"],
                "name": f["name"],
                "description": f.get("description", ""),
                "status": f["status"],
                "skip_design": f.get("skip_design", False),
                "design_doc_path": f.get("design_doc_path"),
                "pr_number": f.get("pr_number"),
                "pr_url": f.get("pr_url"),
            }
            for f in selected
        ]
    except Exception as e:
        log.warning(f"[assign] Could not pre-fetch features for {persona}: {e} — agent will get empty list")
        return []


def _format_assigned_features(features: list[dict], persona: str | None) -> str:
    """Render the assigned feature list as Markdown for injection into the prompt."""
    if not features:
        return ""
    lines = [f"### Assigned features for this session ({len(features)} total)\n"]
    for i, f in enumerate(features, 1):
        lines.append(f"{i}. **[#{f['id']}] {f['name']}**")
        if f.get("description"):
            lines.append(f"   - {f['description']}")
        if persona == "coder" and f.get("design_doc_path"):
            lines.append(f"   - Design doc: /workspace/{f['design_doc_path']}")
        if persona == "reviewer" and f.get("pr_number"):
            lines.append(f"   - PR: #{f['pr_number']}" + (f" ({f['pr_url']})" if f.get("pr_url") else ""))
        lines.append("")
    lines.append("Work through these features IN ORDER. Do not query the features API for additional work.")
    return "\n".join(lines)


def _claim_features(features: list[dict], persona: str | None) -> None:
    """
    Mark assigned features as in-progress before launching Docker.
    Prevents double-claiming if the poller runs again before the container finishes.
    Reviewer features stay in 'Reviewing' — no claim needed.
    """
    status_map = {"designer": "Designing", "coder": "Implementing"}
    target_status = status_map.get(persona or "")
    if not target_status or not features:
        return
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in features:
                try:
                    client.patch(f"/api/features/{f['id']}", json={"status": target_status})
                    log.info(f"[claim] Feature #{f['id']} '{f['name']}' → {target_status}")
                except Exception as fe:
                    log.warning(f"[claim] Could not claim feature #{f['id']}: {fe}")
    except Exception as e:
        log.warning(f"[claim] Could not connect to PM API: {e}")


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
    Sync the product workspace to the latest state on origin/main before each session.
    Uses git fetch + reset --hard (not pull --ff-only) so it succeeds even if local
    has diverged from remote (e.g. partial commits from a crashed previous session).
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return  # Not a git repo yet — skip

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True)

    # 1. Fetch latest from origin (updates remote-tracking refs, prunes deleted branches)
    r = _run(["git", "fetch", "origin", "--prune"])
    if r.returncode != 0:
        log.warning(f"[{product_name}] git fetch failed: {r.stderr.strip()[:200]}")

    # 2. Switch to main (or master) — abandon any half-baked feature branch
    main_branch: str | None = None
    for branch in ("main", "master"):
        r = _run(["git", "checkout", branch])
        if r.returncode == 0:
            main_branch = branch
            break
    if not main_branch:
        log.warning(f"[{product_name}] Could not checkout main/master — workspace reset skipped")
        return

    # 3. Hard-reset to origin — discards any local commits or staged changes
    r = _run(["git", "reset", "--hard", f"origin/{main_branch}"])
    if r.returncode != 0:
        log.warning(f"[{product_name}] git reset --hard failed: {r.stderr.strip()[:200]}")

    # 4. Remove untracked and ignored files (session artifacts, .pyc, etc.)
    #    Preserve output/ and Results/ which may contain artefacts the PM cares about.
    _run(["git", "clean", "-fdx", "--exclude=output/", "--exclude=Results/", "--exclude=Temp/"])

    # 5. Delete stale local feature branches (not main/master)
    r = _run(["git", "branch"])
    for line in r.stdout.splitlines():
        branch = line.strip().lstrip("* ")
        if branch and branch not in ("main", "master"):
            _run(["git", "branch", "-D", branch])
            log.info(f"[{product_name}] Deleted stale local branch: {branch}")

    log.info(f"[{product_name}] Workspace synced to origin/{main_branch} (hard reset)")


def _cleanup_workspace_post_session(working_dir: str, product_name: str) -> None:
    """
    Post-exit cleanup: return to main branch and remove uncommitted session artifacts.
    Runs after container exits (success or failure) so the next session starts clean.
    Does NOT delete the working directory — git history and pushed branches are preserved.
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True)

    # Return to main branch (agent may have left us on a feature branch)
    for branch in ("main", "master"):
        if _run(["git", "checkout", branch]).returncode == 0:
            break

    # Remove any files the agent created but didn't commit/push
    # Keep docs/, Results/, Temp/ — those may contain artefacts the PM cares about
    _run(["git", "clean", "-fd", "--exclude=docs/", "--exclude=Results/", "--exclude=Temp/"])
    log.info(f"[{product_name}] Post-session workspace cleanup complete")


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

    # Re-install templates after reset — git clean may have removed untracked template files.
    # force=False ensures we never overwrite files the agent has customised and committed.
    try:
        install_templates(product, PM_API_URL, force=False)
    except Exception as _te:
        log.warning(f"Could not re-install templates for {product.get('name')}: {_te}")

    # Ensure standard agent-writable directories exist on the host.
    # Permissions are fixed inside the container via docker exec -u 0 after startup
    # (Windows NTFS bind-mounts appear as root-owned inside Docker; host chmod is a no-op).
    for _agent_dir in ("docs", "Results", "Temp"):
        Path(working_dir, _agent_dir).mkdir(exist_ok=True)

    # ── Fetch sys_cfg FIRST — must happen before build_prompt so all substitution
    #    vars (auto_merge_enabled, assigned_features, prev_session_summary) are ready.
    sys_cfg = _get_system_config_sync()
    effective_backend = (sys_cfg.get("agent_backend") or AGENT_BACKEND)
    session_timeout_seconds = int(sys_cfg.get("session_timeout_minutes") or 0) * 60 or _DEFAULT_SESSION_TIMEOUT_SECONDS
    effective_max_features = product.get("max_features_per_run") or int(sys_cfg.get("max_features_per_run") or MAX_FEATURES_PER_RUN)

    # Read all runtime settings from sys_cfg (DB → env var → built-in default).
    # Never rely on module-level constants after this point.
    effective_ollama_host    = sys_cfg.get("ollama_host")    or OLLAMA_HOST    or "http://host.docker.internal:11434"
    effective_designer_model = sys_cfg.get("designer_model") or DESIGNER_MODEL or "gemma3:27b"
    effective_coder_model    = sys_cfg.get("coder_model")    or CODER_MODEL    or "qwen3-coder:30b"
    effective_ollama_timeout = int(sys_cfg.get("ollama_timeout") or os.environ.get("OLLAMA_TIMEOUT", "600"))
    effective_max_turns      = int(sys_cfg.get("max_turns")      or os.environ.get("MAX_TURNS",      "80"))
    effective_bash_timeout   = int(sys_cfg.get("bash_timeout")   or os.environ.get("BASH_TIMEOUT",   "180"))
    _raw_ssh_dir = sys_cfg.get("ssh_keys_dir") or str(SSH_DIR)
    effective_ssh_dir = Path(_raw_ssh_dir) if _raw_ssh_dir else None

    # Enrich product dict with all computed values before building the prompt.
    product = dict(product)
    product["_auto_merge_enabled"] = bool(sys_cfg.get("auto_merge_enabled", False))

    # Poller-driven feature assignment: pre-fetch and claim features before launch.
    # Agent receives an explicit task list — no self-discovery inside the container.
    assigned_features = _fetch_assigned_features(product["id"], persona, effective_max_features)
    _claim_features(assigned_features, persona)
    product["_assigned_features"] = assigned_features
    product["_assigned_features_md"] = _format_assigned_features(assigned_features, persona)

    # Inject previous session summary for continuity.
    product["_prev_session_summary"] = _read_session_summary(working_dir)

    prompt = build_prompt(product, session_uid, persona=persona)

    # Guard: check DB for an already-running session for this product.
    # Cross-check with docker ps — if the container is gone, auto-close the stale DB record.
    product_id = product["id"]
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/sessions/active", params={"product_id": product_id})
            active = resp.json()
            if active:
                container_id = active.get("container_id", "")
                # Verify container is actually running — stale DB records block forever otherwise
                docker_check = subprocess.run(
                    ["docker", "ps", "--filter", f"name={container_id}", "--format", "{{.Names}}"],
                    capture_output=True, text=True
                )
                if container_id and docker_check.stdout.strip():
                    log.info(
                        f"Active session for {product['name']} already running "
                        f"(container={container_id} persona={active.get('persona')}) — waiting"
                    )
                    return 99  # Sentinel: already running, not an error
                else:
                    # Container is gone but DB record is open — close it
                    session_id = active.get("id")
                    log.warning(
                        f"Orphaned session {session_id} in DB for {product['name']} "
                        f"(container {container_id} not running) — closing and proceeding"
                    )
                    try:
                        client.patch(f"/api/sessions/{session_id}", json={
                            "ended_at": datetime.now(timezone.utc).isoformat(),
                            "exit_code": 1,
                            "notes": "orphaned - container exited without cleanup",
                        })
                    except Exception as ce:
                        log.warning(f"Could not close orphaned session {session_id}: {ce}")
    except Exception as e:
        log.warning(f"Could not check active session in DB: {e} — falling back to docker ps")
        # Fallback to docker ps if API is unreachable
        running = subprocess.run(
            ["docker", "ps", "--filter", f"name=pf-{product_id}-", "--format", "{{.Names}}"],
            capture_output=True, text=True
        ).stdout.strip()
        if running:
            log.info(f"Container already running for product {product_id} ({running}) — waiting")
            return 99  # Sentinel: already running, not an error

    # Mount only the deploy key, not the whole .ssh directory.
    # This preserves the known_hosts baked into the image.
    deploy_key = _get_deploy_key_path(product, effective_ssh_dir)
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
            "-e", f"OLLAMA_HOST={effective_ollama_host}",
            "-e", f"DESIGNER_MODEL={effective_designer_model}",
            "-e", f"CODER_MODEL={effective_coder_model}",
            "-e", f"MAX_FEATURES_PER_RUN={effective_max_features}",
            "-e", f"OLLAMA_TIMEOUT={effective_ollama_timeout}",
            "-e", f"MAX_TURNS={effective_max_turns}",
            "-e", f"BASH_TIMEOUT={effective_bash_timeout}",
        ]
        # Ollama backend: no Claude OAuth mount needed
        claude_mount = []
        log.info(f"Using Ollama backend — host={effective_ollama_host} persona={persona}")
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

        # Pre-create session-env/ so the Claude Code harness can write session state.
        # Without this directory the harness fails to initialise and the Bash tool is broken.
        if _tmp_claude_dir:
            (Path(_tmp_claude_dir) / "session-env").mkdir(exist_ok=True)
            # Mount read-write — safe because mount_dir is a temp copy, not the original.
            # The harness must be able to write to session-env/ at runtime.
            claude_mount = ["-v", f"{mount_dir}:/home/agent/.claude"]
        else:
            # Fallback: direct mount — keep read-only to protect original credentials.
            # session-env writes will fail but that's better than exposing originals as rw.
            claude_mount = ["-v", f"{mount_dir}:/home/agent/.claude:ro"]
            log.warning("Mounting original .claude dir read-only — Bash tool may be broken")

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
            "-e", f"MAX_TURNS={effective_max_turns}",
            "-e", f"BASH_TIMEOUT={effective_bash_timeout}",
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

        # Fix workspace dir permissions as root immediately after container starts.
        # Windows bind-mounts appear as root-owned inside Docker; the agent user (UID 1001)
        # is "other" and can't write without this chmod.
        def _fix_workspace_perms():
            import time as _t
            _t.sleep(3)  # Give container time to initialise
            # Use sh -c to avoid Git Bash converting /workspace/* to Windows paths
            subprocess.run(
                ["docker", "exec", "-u", "0", container_name,
                 "sh", "-c",
                 "chmod 777 /workspace/docs /workspace/Results /workspace/Temp 2>/dev/null || true"],
                capture_output=True,
            )
        threading.Thread(target=_fix_workspace_perms, daemon=True).start()

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

        # Live-poll session_result.json while container runs — applies DB updates in real-time
        # as the agent writes phase transitions (Implementing → Reviewing, Blocked, etc.).
        _poll_stop = threading.Event()
        poll_thread = threading.Thread(
            target=_live_poll_session_result,
            args=(working_dir, _poll_stop),
            daemon=True,
        )
        poll_thread.start()

        try:
            process.wait(timeout=session_timeout_seconds)
        except subprocess.TimeoutExpired:
            log.error(f"Session timed out after {session_timeout_seconds}s — killing container")
            subprocess.run(["docker", "kill", f"pf-{product['id']}-{session_uid}"], capture_output=True)
            send_alert("error", f"{product['name']}: session timed out after {session_timeout_seconds//60}m")
            exit_code = 1
        else:
            exit_code = process.returncode
        finally:
            _poll_stop.set()       # signal live-poll thread to stop
            log_thread.join(timeout=10)
            poll_thread.join(timeout=5)

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
    # 1. Read session_result.json ONCE — shared between auto_merge and reconcile so
    #    the file isn't deleted before auto_merge can act on it.
    _session_features = _read_session_result(working_dir)

    # 2. Auto-merge BEFORE reconcile so merge/re-queue decisions override raw agent state.
    if exit_code == 0 and persona == "reviewer" and product.get("_auto_merge_enabled"):
        _session_features = _auto_merge_approved(product, _session_features)

    # 3. Apply status updates (deletes session_result.json at end).
    _reconcile_session_result(working_dir, product["id"], exit_code, features=_session_features)

    # 4. Record session end in DB.
    if session_id is not None:
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                client.patch(f"/api/sessions/{session_id}", json={
                    "ended_at":  datetime.now(timezone.utc).isoformat(),
                    "exit_code": exit_code,
                })
        except Exception as e:
            log.warning(f"Could not update session record: {e}")

    # 5. Roll back any intermediate-state features with no PR evidence.
    #    Covers cases where agent claimed a feature but never finished it.
    #    (Features with pr_number are left alone — they're already Reviewing.)
    if exit_code != 0:
        log.warning(f"Non-zero exit ({exit_code}) for {product['name']} — rolling back incomplete features")
        _rollback_stuck_features(product["id"], persona)

    # Post-session cleanup: return workspace to clean main so next session starts fresh
    _cleanup_workspace_post_session(working_dir, product.get("name", str(working_dir)))

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
            commit_and_push_output(working_dir, deploy_key=_get_deploy_key_path(product, effective_ssh_dir))
        else:
            log.warning("Product video generation skipped or failed — continuing to recommender")
        # Skip recommender if the Pending backlog is at or above the configured threshold.
        # Threshold is read from system_config (recommender_pending_threshold, default 15).
        # Set to 0 to always run the recommender.
        _sys_cfg = _get_system_config_sync()
        _rec_threshold = int(_sys_cfg.get("recommender_pending_threshold") or 15)
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as _client:
                _resp = _client.get(
                    "/api/features/count",
                    params={"product_id": product["id"], "status": "Pending"},
                )
                _pending_count = _resp.json().get("count", 0)
        except Exception as _e:
            log.warning(f"Could not count pending features: {_e} — skipping recommender (fail-safe)")
            _pending_count = max(_rec_threshold, 1)  # Fail closed: skip rather than runaway

        if _rec_threshold > 0 and _pending_count >= _rec_threshold:
            log.info(
                f"Skipping recommender for {product['name']} — "
                f"{_pending_count} Pending features in backlog (threshold: {_rec_threshold})"
            )
        else:
            log.info(f"Launching recommender for {product['name']} ({_pending_count} Pending, threshold: {_rec_threshold})")
            run_claude_in_docker(product, persona="recommender")

    return exit_code


def _post_log_lines(product_id: int, lines: list[str]) -> None:
    """Fire-and-forget: push log lines to PM API for SSE streaming."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.post(f"/api/products/{product_id}/session/log", json={"lines": lines})
    except Exception:
        pass
