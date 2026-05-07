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

import json
import os
import shlex
import shutil
import tempfile
import time
import uuid
import subprocess
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from orchestrator.prompts import build_prompt
from orchestrator.alerts import send_alert
from orchestrator.paths import host_path, container_path, in_container_mode
from templates.renderer import install_templates

log = logging.getLogger("poller.docker")


# Self-heartbeat shell snippet for Claude-backend containers. Runs in the
# background inside the agent container; POSTs heartbeats every 30s. This
# survives orchestrator restarts — the orchestrator's per-session heartbeat
# thread dies when its container recreates, but this one runs inside the
# agent and lives as long as the agent does. When claude exits, the parent
# `sh -c` exits and the backgrounded subshell is reaped.
#
# Resolves session_id from $SESSION_UID via the PM API on startup (up to
# ~30s of retry); silently no-ops if it can't find the session.
#
# Ollama backend already has its own self-heartbeat in ollama_agent.py
# (Python thread), so we only inject this for the claude binary path.
_AGENT_HEARTBEAT_SH = r'''
(
  set +e
  _SID=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    _SID="$(curl -s "$PM_API_URL/api/sessions/active" 2>/dev/null | python3 -c "
import sys, json
try:
    ds = json.load(sys.stdin)
    for s in ds:
        if s.get('session_uid') == '$SESSION_UID':
            print(s.get('id', ''))
            break
except Exception:
    pass
" 2>/dev/null)"
    [ -n "$_SID" ] && break
    sleep 3
  done
  if [ -n "$_SID" ]; then
    while :; do
      curl -s -X POST "$PM_API_URL/api/sessions/$_SID/heartbeat" >/dev/null 2>&1
      sleep 30
    done
  fi
) &
'''.strip()

CLAUDE_DIR  = Path(os.environ.get("CLAUDE_DIR",  ""))   # configurable via system_config.claude_credentials_dir
SSH_DIR     = Path(os.environ.get("SSH_DIR",     ""))   # configurable via system_config.ssh_keys_dir
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "productfactory-agent")
PM_API_URL  = os.environ["PM_API_URL"]
# Inside the agent container, pm-api is reachable via --add-host as http://pm-api:8080
# The host-side PM_API_URL (localhost:8080) doesn't work inside Docker.
PM_API_URL_CONTAINER = os.environ.get("PM_API_URL_CONTAINER", "http://pm-api:8080")

# Timeout default — overridden at runtime by system_config.session_timeout_minutes
_DEFAULT_SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "90")) * 60
# Alias used by tests and legacy callers
SESSION_TIMEOUT_SECONDS = _DEFAULT_SESSION_TIMEOUT_SECONDS

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
MAX_FEATURES_PER_SPRINT = int(os.environ.get("MAX_FEATURES_PER_SPRINT", "5"))


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


# Phase 1 of OrchestratorRefactor: the feature state machine, session_result.json
# I/O, and post-session reconciler moved into orchestrator/session/. Re-exported
# here so deploy/orchestrator/orchestrate.py and tests/test_docker.py continue
# to import these symbols from orchestrator.docker_runner unchanged.
from orchestrator.session.state_machine import (
    _VALID_FEATURE_STATUSES,
    _PROGRESS_RANK,
    _ALLOWED_BACKWARD,
    _apply_session_entry,
)
from orchestrator.session.result_io import (
    _read_session_result,
    _delete_session_result,
    _live_poll_session_result,
)
from orchestrator.session.reconciler import (
    _rollback_stuck_features,
    _reconcile_session_result,
)


# Phase 2b of OrchestratorRefactor: _auto_merge_approved (the reviewer-session
# auto-merge pipeline) moved into orchestrator/pipelines/auto_merge_reviewer.py.
# _parse_repo_slug + _get_gh_token (the GitHub helpers) moved into
# orchestrator/integrations/github.py. Re-exported below alongside the other
# pipelines so docker_runner.run_claude_in_docker (and tests that monkeypatch
# `docker_runner._get_gh_token`) keep working unchanged.
from orchestrator.pipelines.auto_merge_reviewer import _auto_merge_approved
from orchestrator.integrations.github import _parse_repo_slug, _get_gh_token


# Phase 2 of OrchestratorRefactor: redaction helpers and host-side Docker
# plumbing moved into orchestrator/{infra,integrations}/. Re-exported here so
# existing call sites continue to work unchanged.
from orchestrator.infra.redaction import (
    _SECRET_PATTERNS,
    _redact_secrets,
    _format_agent_event,
)
from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine


# Phase 2b of OrchestratorRefactor: planner/designer post-session pipeline
# moved into orchestrator/pipelines/post_doc.py. Re-exported below.
from orchestrator.pipelines.post_doc import (
    _run_post_doc_pipeline,
    _rollback_doc_features,
)


# Phase 2b of OrchestratorRefactor: coder post-session pipeline (the ~500 LOC
# git+GitHub fallback) moved into orchestrator/pipelines/post_coder.py.
from orchestrator.pipelines.post_coder import _run_post_coder_pipeline


# _get_gh_token moved to orchestrator/integrations/github.py and re-exported
# above (Phase 2b). Keeping _get_system_config_sync local for now — it's used
# pervasively from run_claude_in_docker and will move with the launcher in
# Phase 3.
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


def _fetch_assigned_features(product_id: int, persona: str | None, max_count: int = MAX_FEATURES_PER_SPRINT) -> tuple[list[dict], str | None, dict | None]:
    """
    Pre-fetch features the agent should work on this session.
    Sprint-aware: when an active sprint exists, picks ALL eligible features in
    that sprint (up to MAX_FEATURES_PER_SPRINT) so the entire sprint is planned
    and implemented together.
    Returns ([], None, None) for personas that manage their own work (qa_tester, recommender, etc.).

    Tuple shape: (features, active_sprint_name, active_sprint_dict).
    `active_sprint_dict` is the full sprint payload (id, name, branch_name,
    pr_number, pr_url, ...) so callers can wire sprint-PR-mode context into
    prompts and the post-coder pipeline without an extra round trip.
    """
    if persona not in ("coder", "designer", "reviewer"):
        return [], None, None
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            # Check for active sprint — if one exists, scope to its features
            active_sprint_id = None
            active_sprint_name = None
            active_sprint: dict | None = None
            active_resp = client.get(f"/api/products/{product_id}/sprints/active")
            if active_resp.status_code == 200 and active_resp.json():
                active_sprint = active_resp.json()
                active_sprint_id = active_sprint.get("id")
                active_sprint_name = active_sprint.get("name")

            resp = client.get(f"/api/products/{product_id}/features")
            resp.raise_for_status()
            all_features = resp.json()
            all_features = all_features if isinstance(all_features, list) else []

            if not active_sprint_id:
                log.info(f"[assign] No active sprint for product {product_id} — skipping feature assignment")
                features = []
            elif persona == "coder":
                # Match the API's /next-for-persona?persona=coder rules so the
                # orchestrator's persona dispatch and the agent's actually-
                # assigned features stay in sync. Without the third clause,
                # determine_next_action picks coder for `Implementing +
                # changes_requested` features but _fetch_assigned_features
                # returns 0 → agent task_done's in 3 seconds.
                candidates = [f for f in all_features
                              if f.get("status") == "Designed"
                              or (f.get("status") == "Approved" and f.get("design_doc_path"))
                              or (f.get("status") == "Implementing"
                                  and f.get("review_outcome") == "changes_requested")]
                features = [f for f in candidates if f.get("sprint_id") == active_sprint_id]
            elif persona == "reviewer":
                # Reviewer follows the PR — scope to active sprint but fall back to any sprint
                # so open PRs are not left hanging if sprint rolled over mid-review.
                candidates = [f for f in all_features
                              if f.get("status") == "Reviewing"
                              and f.get("pr_number")
                              and f.get("sprint_id") is not None]
                features = [f for f in candidates if f.get("sprint_id") == active_sprint_id]
                if not features:
                    features = candidates
            elif persona == "designer":
                # product_planner was merged into designer 2026-05-06 (Phase 1
                # of futureplan.md). They shared this same filter and wrote
                # near-identical per-feature docs.
                candidates = [f for f in all_features
                              if f.get("status") == "Approved" and not f.get("design_doc_path")]
                features = [f for f in candidates if f.get("sprint_id") == active_sprint_id]
            else:
                features = []

        selected = features[:max_count]
        log.info(f"[assign] persona={persona} assigned {len(selected)}/{len(features)} features")
        return [
            {
                "id": f["id"],
                "name": f["name"],
                "description": f.get("description", ""),
                "status": f["status"],
                "design_doc_path": f.get("design_doc_path"),
                "pr_number": f.get("pr_number"),
                "pr_url": f.get("pr_url"),
                "fix_attempts": f.get("fix_attempts", 0),
                "blocked_reason": f.get("blocked_reason"),
                # `review_outcome` lets the prompt-builder distinguish a fresh
                # rework cycle (changes_requested) from a normal first-pass
                # assignment, and so we know when to fetch the reviewer's
                # per-feature feedback below. Stripping it here used to be
                # the reason the rework coder had no idea WHY it was running.
                "review_outcome": f.get("review_outcome"),
            }
            for f in selected
        ], active_sprint_name, active_sprint
    except Exception as e:
        log.warning(f"[assign] Could not pre-fetch features for {persona}: {e} — agent will get empty list")
        return [], None, None


def _write_sprint_features_md(working_dir: str, features: list[dict], sprint_name: str | None) -> None:
    """Write a sprint-scoped features.md to the working directory (write-only, no read-back sync)."""
    md_path = Path(working_dir) / "features.md"
    if not features:
        # Remove stale features.md if no sprint features
        if md_path.exists():
            md_path.unlink()
        return
    header = sprint_name or "Current Sprint"
    lines = [
        f"# Sprint Features — {header}",
        "> Auto-generated at session start. DO NOT edit — DB is source of truth.",
        "",
    ]
    for f in features:
        ftype = f.get("feature_type", "feature") if "feature_type" in f else "feature"
        lines.append(f"## Feature #{f['id']}: {f['name']}")
        lines.append(f"- **Status:** {f['status']}")
        if ftype != "feature":
            lines.append(f"- **Type:** {ftype}")
        if f.get("description"):
            lines.append(f"- {f['description']}")
        lines.append("")
    try:
        md_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"Wrote sprint features.md ({len(features)} features) to {md_path}")
    except Exception as e:
        log.warning(f"Could not write features.md to {working_dir}: {e}")


def _fetch_recent_review_comments(feature_id: int, limit: int = 6) -> list[dict]:
    """
    Pull the last `limit` reviewer/security_auditor/qa_tester comments for a
    feature from the PM API. Returns oldest-first within the slice so the
    prompt-renderer can stack them in chronological order under the feature.

    Used by `_format_reviewer_feedback` to bridge the reviewer→coder feedback
    gap. Until 2026-05-06 the rework coder had no signal for WHY it was
    rerunning — review_outcome=changes_requested was the only hint, with the
    actual line numbers / failing test names buried in feature_comments that
    the prompt never read. Real example: reviewer 1975 left specific
    comments on feature 179 (`SRC/healthCheckService.js` lines 34/44/54/84/122,
    three skipped test cases by name) that coder 1976 never saw.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            r = client.get(f"/api/features/{feature_id}/comments")
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
            # API may return newest-first OR oldest-first depending on impl;
            # sort defensively by created_at ascending and keep the tail.
            data.sort(key=lambda c: (c.get("created_at") or ""))
            relevant = [c for c in data
                        if (c.get("author") or "").lower()
                        in ("reviewer", "security_auditor", "qa_tester")]
            return relevant[-limit:]
    except Exception as e:
        log.debug(f"[reviewer-feedback] could not fetch comments for #{feature_id}: {e}")
        return []


def _format_reviewer_feedback(features: list[dict]) -> str:
    """
    Render the most recent reviewer/auditor comments per feature as a
    Markdown block. Empty string when no feature in `features` has
    `review_outcome=changes_requested` — i.e. fresh first-pass assignments
    don't get this section, only reworks do.

    Goes into the coder prompt as `{reviewer_feedback}`.
    """
    rework_features = [f for f in features
                       if (f.get("review_outcome") == "changes_requested"
                           or (f.get("fix_attempts") or 0) > 0)]
    if not rework_features:
        return ""
    sections: list[str] = [
        "## Reviewer feedback to address",
        "",
        "These features are in a **rework cycle** — the reviewer or "
        "security auditor flagged specific issues on the prior commit. "
        "Address each item below before re-pushing. Don't reimplement "
        "from scratch — keep the working parts and patch the listed gaps.",
        "",
    ]
    for f in rework_features:
        comments = _fetch_recent_review_comments(int(f["id"]))
        if not comments:
            continue
        sections.append(f"### Feature #{f['id']} — {f.get('name','')}")
        sections.append(
            f"_review_outcome={f.get('review_outcome') or '-'}, "
            f"fix_attempts={f.get('fix_attempts', 0)}_"
        )
        sections.append("")
        for c in comments:
            ts = (c.get("created_at") or "")[:19]
            author = c.get("author") or "?"
            body = (c.get("body") or "").rstrip()
            sections.append(f"**{ts} · {author}**")
            sections.append(body)
            sections.append("")
    if len(sections) <= 4:  # only the header survived — no comments found
        return ""
    return "\n".join(sections).rstrip() + "\n"


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
        if f.get("fix_attempts", 0) > 0:
            lines.append(f"   - ⚠️ **RETRY #{f['fix_attempts']}** — this feature failed previously.")
            if f.get("blocked_reason"):
                lines.append(f"     Last failure: {f['blocked_reason']}")
            lines.append(f"     Check `Temp/qa_notes_{f['id']}.md` (if it exists) for detailed test failure analysis.")
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


# Persona → cost tier. Heavy personas make architectural decisions or write
# production code; light personas summarise, document, or generate text. The
# default mapping keeps heavy work on Sonnet and light work on Haiku (~5x
# cheaper, ~3x faster). Override per-persona via sys_cfg.claude_model_map.
_HEAVY_PERSONAS = {"coder", "reviewer", "designer"}
_LIGHT_PERSONAS = {"planner", "documenter", "retrospective",
                   "analytics", "recommender", "devops", "refactorer"}


def _model_for_persona(sys_cfg: dict, persona: str | None) -> str:
    """
    Resolution order:
      1. sys_cfg.claude_model_map[persona]  (explicit per-persona override)
      2. sys_cfg.claude_model_heavy / claude_model_light  (tier override)
      3. sys_cfg.claude_model  (single-model fallback, legacy behaviour)
      4. Hardcoded default: Sonnet for heavy, Haiku for light, Sonnet for unknown
    """
    pmap = sys_cfg.get("claude_model_map") or {}
    if isinstance(pmap, dict) and persona and pmap.get(persona):
        return pmap[persona]
    if persona in _HEAVY_PERSONAS:
        return (sys_cfg.get("claude_model_heavy")
                or sys_cfg.get("claude_model")
                or "claude-sonnet-4-6")
    if persona in _LIGHT_PERSONAS:
        return (sys_cfg.get("claude_model_light")
                or sys_cfg.get("claude_model")
                or "claude-haiku-4-5-20251001")
    # Unknown persona — safer to use the more capable model.
    return (sys_cfg.get("claude_model") or "claude-sonnet-4-6")


def _get_claude_profile(sys_cfg: dict, persona: str | None = None) -> tuple[str, str]:
    """
    Returns (credentials_dir, claude_model) from system config.
    Model is persona-aware — heavy personas (coder/reviewer/etc.) get Sonnet;
    light personas (planner/documenter/etc.) get Haiku. Configurable via
    sys_cfg.claude_model_map, claude_model_heavy, claude_model_light.
    """
    credentials_dir = sys_cfg.get("claude_credentials_dir") or str(CLAUDE_DIR)
    credentials_dir = container_path(credentials_dir) or credentials_dir
    claude_model = _model_for_persona(sys_cfg, persona)
    return credentials_dir, claude_model


# Phase 2 of OrchestratorRefactor: workspace git helpers and a unified
# safe_run subprocess wrapper now live in orchestrator/integrations/git_ops.py.
# The 4× duplicated _run helpers across these three functions are replaced by
# the single safe_run. Re-exported here so docker_runner.run_claude_in_docker
# (and any future callers) keep the same import path.
from orchestrator.integrations.git_ops import (
    _reset_workspace,
    _checkout_sprint_branch,
    _cleanup_workspace_post_session,
)


def _prepare_workspace(product: dict) -> tuple[str, str]:
    """
    Run pre-launch workspace prep, idempotently:
      1. _reset_workspace (git fetch + reset --hard origin/main + clean)
      2. delete any session_result.json the previous session committed to git
      3. re-install template files that git clean removed
      4. ensure docs/Results/Temp dirs exist
      5. chmod a+rwX so the agent UID (1001) can write everywhere

    Returns (working_dir_host, working_dir) — the first is what Docker sees
    on the host, the second is what THIS Python process sees (translated to
    /products/... when running inside Hermes, identical otherwise).

    Extracted from run_claude_in_docker during Phase 3 of OrchestratorRefactor.
    """
    working_dir_host = product["working_dir"]
    working_dir = container_path(working_dir_host)

    # Always reset workspace to clean main before starting a new session.
    # This discards any half-baked code from failed/incomplete previous sessions.
    _reset_workspace(working_dir, product.get("name", str(working_dir)))

    # Delete stale session_result.json BEFORE launch — previous agents may have
    # committed it to git, so git clean won't remove it. Robust to UID drift /
    # permission errors via alpine sidecar fallback (incident 2026-05-06: a
    # stale session_result.json from session 1942 survived 12+ subsequent
    # session starts, polluting reconcile reads with `Implemented` entries
    # that silently failed the rank guard).
    _delete_session_result(working_dir, product.get("name", "?"))

    # Re-install templates after reset — git clean may have removed untracked template files.
    # force=False ensures we never overwrite files the agent has customised and committed.
    try:
        # Pass product dict with the container-side working_dir so Path(...).exists() works
        # when docker_runner runs inside Hermes (where the DB stores the host/Windows path).
        install_templates({**product, "working_dir": working_dir}, PM_API_URL, force=False)
    except Exception as _te:
        log.warning(f"Could not re-install templates for {product.get('name')}: {_te}")

    # Ensure standard agent-writable directories exist on the host.
    for _agent_dir in ("docs", "Results", "Temp"):
        Path(working_dir, _agent_dir).mkdir(exist_ok=True)

    # Make every workspace path world-writable BEFORE the agent container starts.
    # _reset_workspace already ran one chmod, but install_templates and the
    # mkdir loop above re-create dirs at mode 755 afterwards — re-run to catch
    # SRC/, TestCases/, docs/, Results/, Temp/.
    _chmod_workspace_via_alpine(working_dir, product.get("name", "?"))

    return working_dir_host, working_dir


def _stage_gh_token_mount(gh_token: str | None) -> tuple[str | None, list[str]]:
    """
    Write gh_token to a 0600 temp file and return (path, mount_args).
    Caller is responsible for unlinking the file in its finally block.
    Returns (None, []) when no token or staging fails.
    """
    if not gh_token:
        return None, []
    gh_token_file: str | None = None
    try:
        _fd, gh_token_file = tempfile.mkstemp(prefix="pf_gh_", suffix=".token")
        os.close(_fd)
        Path(gh_token_file).write_text(gh_token)
        try:
            os.chmod(gh_token_file, 0o600)
        except Exception:
            pass  # Windows: NTFS perms don't map cleanly; 0600 is best-effort
        return gh_token_file, ["-v", f"{host_path(gh_token_file)}:/run/secrets/gh_token:ro"]
    except Exception as e:
        log.warning(f"Could not write gh token file: {e} — agent will have no gh auth")
        if gh_token_file:
            try:
                Path(gh_token_file).unlink()
            except Exception:
                pass
        return None, []


def _stage_claude_credentials(sys_cfg: dict, persona: str | None) -> tuple[list[str], str | None, str]:
    """
    Prepare the Claude OAuth credential mount. Returns (mount_args, tmp_dir, claude_model).
    Caller must shutil.rmtree(tmp_dir) in its finally block when not None.

    Selectively copies only auth-essential files (.credentials.json, settings.json,
    settings.local.json, .claude.json) to a temp dir rather than bind-mounting
    the whole ~/.claude — a full copytree races with the host's active Claude
    Code (which writes sessions/, history.jsonl, projects/, cache/ constantly)
    and causes 9P filesystem RPC hangs on Docker Desktop for Windows.

    On any copy failure, returns a read-only direct mount of the original
    creds dir as the fallback (Bash tool will be broken but auth still works).
    """
    creds_src, claude_model = _get_claude_profile(sys_cfg, persona=persona)
    log.info("Claude model for persona=%s: %s", persona, claude_model)
    # Files to copy verbatim from the host .claude dir. Everything else
    # (sessions/, history.jsonl, projects/, cache/, plugins/, etc.) is
    # skipped to avoid races with the host's live Claude Code process.
    _AUTH_FILES = [".credentials.json", "settings.json", "settings.local.json"]
    _tmp_claude_dir: str | None = None
    try:
        _tmp_claude_dir = tempfile.mkdtemp(prefix="pf_claude_creds_")
        src_path = Path(creds_src)
        if src_path.exists():
            copied: list[str] = []
            for name in _AUTH_FILES:
                src_file = src_path / name
                if src_file.exists() and src_file.is_file():
                    try:
                        shutil.copy2(str(src_file), str(Path(_tmp_claude_dir) / name))
                        copied.append(name)
                    except Exception as ce:
                        log.warning(f"Could not copy {name}: {ce}")
            log.info(f"Copied Claude auth files ({copied}) from {creds_src} to {_tmp_claude_dir}")
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
        if _tmp_claude_dir:
            shutil.rmtree(_tmp_claude_dir, ignore_errors=True)
        _tmp_claude_dir = None

    mount_dir = _tmp_claude_dir or creds_src

    # Pre-create subdirectories the Claude Code harness expects to write to,
    # and make them world-writable so the agent UID (1001) can use them.
    # mkdtemp creates 0700 dirs owned by the orchestrator user (UID 999) —
    # agent can't write there without this chmod.
    if _tmp_claude_dir:
        for _sub in ("session-env", "todos", "projects", "shell-snapshots",
                     "statsig", "sessions"):
            _sub_path = Path(_tmp_claude_dir) / _sub
            _sub_path.mkdir(exist_ok=True)
        try:
            # 0777 on the root + all children so any UID inside the container can write.
            os.chmod(_tmp_claude_dir, 0o777)
            for _child in Path(_tmp_claude_dir).rglob("*"):
                try:
                    os.chmod(_child, 0o777 if _child.is_dir() else 0o666)
                except Exception:
                    pass
        except Exception as _ce:
            log.warning(f"chmod on staged claude dir failed: {_ce}")
        # Mount read-write — safe because mount_dir is a temp copy, not the original.
        claude_mount = ["-v", f"{host_path(mount_dir)}:/home/agent/.claude"]
    else:
        # Fallback: direct mount — keep read-only to protect original credentials.
        # session-env writes will fail but that's better than exposing originals as rw.
        claude_mount = ["-v", f"{host_path(mount_dir)}:/home/agent/.claude:ro"]
        log.warning("Mounting original .claude dir read-only — Bash tool may be broken")

    # Also include .claude.json (sits alongside .claude/ in the host home dir).
    # Copy it INTO the staged dir rather than bind-mounting from host — the
    # live file is rewritten constantly by the host's Claude Code and would
    # race with the container mount.
    # Mount RW (not :ro): the claude CLI needs to update this file on init
    # (telemetry, project state). On read-only mounts it hits EROFS, stops
    # emitting debug logs, and hangs in epoll_wait — silently. The staged
    # copy is per-session disposable, so writes here never reach the host.
    creds_parent = str(Path(creds_src).parent)
    claude_json_src_host = Path(creds_parent) / ".claude.json"
    if _tmp_claude_dir and claude_json_src_host.exists():
        try:
            _staged_cj = Path(_tmp_claude_dir) / ".claude.json"
            shutil.copy2(str(claude_json_src_host), str(_staged_cj))
            claude_mount += ["-v", f"{host_path(_staged_cj)}:/home/agent/.claude.json"]
        except Exception as ce:
            log.warning(f"Could not stage .claude.json: {ce}")

    return claude_mount, _tmp_claude_dir, claude_model


def _stream_session(
    product: dict,
    persona: str | None,
    session_uid: str,
    session_id: int | None,
    working_dir: str,
    container_name: str,
    cmd: list[str],
    session_timeout_seconds: int,
    sys_cfg: dict,
) -> tuple[int, dict]:
    """
    Run the agent docker container and stream its stdout. Spawns four daemon
    helpers alongside the wait():

      _fix_session_result_perms — `docker exec touch+chmod 666` so the orchestrator
                                  (different uid than 1001) can later append
                                  Reviewing entries (incident 2026-05-03)
      _send_heartbeat            — POST /api/sessions/<id>/heartbeat every 30s
      _stream_logs               — drain process.stdout, capture the result event
                                  for cost/token totals, redact secrets, push
                                  formatted lines to PM API session log buffer
      _stall_watchdog            — kill the container if no events for
                                  stall_timeout (default 10m); independent of
                                  the session_timeout upper bound (default 90m)
      live-poll thread           — _live_poll_session_result running in parallel

    Returns (exit_code, session_meta). Exit code mapping:
      process.returncode      → as-is on clean exit
      session_timeout fired   → 1 (alerts "error: timed out")
      _stall_kill triggered   → 1 (alerts "warning: agent stalled")
      docker not on PATH      → 1 (logs error)
      PermissionError         → 1 (logs error)
      any other exception     → 1 (logs full traceback)

    Extracted from run_claude_in_docker during Phase 3 of OrchestratorRefactor.
    """
    exit_code = 1
    session_meta: dict = {}
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Workspace directory permissions are fixed BEFORE this docker run via
        # an alpine container spawned through the host docker socket — the only
        # path that works for Windows bind-mounts. Trying to chmod /workspace/*
        # from inside this agent container always fails with "Operation not
        # permitted" even as root.
        #
        # We DO still pre-touch session_result.json from inside the container
        # though: the file doesn't exist yet at session start, so `touch`
        # creates it as the agent user (uid 1001) and the chmod afterwards is
        # by the same user — that combination works on Windows mounts where
        # chmod-on-bind-mount-imported-files does not. This is load-bearing:
        # real incident 2026-05-03, session 1729 (aec9bc24) — coder declared
        # only 1 of 5 features; the post-coder fallback tried to backfill the
        # missing 4 and failed with `[Errno 13] Permission denied:
        # '/products/Calculator/session_result.json'` because the agent had
        # created the file with default 0644 perms. 4 shipped features got
        # stranded in DB until manual fix.
        def _fix_session_result_perms():
            import time as _t
            _t.sleep(3)  # Give container time to initialise
            subprocess.run(
                ["docker", "exec", "-u", "0", container_name,
                 "sh", "-c",
                 "touch /workspace/session_result.json && chmod 666 /workspace/session_result.json 2>/dev/null || true"],
                capture_output=True,
            )
        threading.Thread(target=_fix_session_result_perms, daemon=True).start()

        # Heartbeat thread — POST /api/sessions/{id}/heartbeat every 30s so the
        # watchdog knows the session is alive. When the main wait() returns
        # (container exited, timed out, killed), _hb_stop fires and the loop exits.
        _hb_stop = threading.Event()
        def _send_heartbeat():
            while not _hb_stop.wait(30):
                if session_id is None:
                    continue
                try:
                    httpx.post(f"{PM_API_URL}/api/sessions/{session_id}/heartbeat", timeout=5)
                except Exception:
                    pass  # transient failures are fine; watchdog has grace period
        if session_id is not None:
            threading.Thread(target=_send_heartbeat, daemon=True).start()

        # Stall detection (Symphony pattern): track timestamp of the last
        # event seen on the agent's stdout. A separate watchdog thread kills
        # the container if no event arrives within stall_timeout. Distinct
        # from session_timeout: that's the upper bound on a productive run;
        # stall detects stuck-but-alive containers (Ollama 500s, hung pytest,
        # claude waiting on a hung child) much sooner.
        stall_state = {"last_event_at": time.monotonic()}
        _stall_kill = threading.Event()  # signal the wait loop that we killed for stall

        def _stream_logs():
            buffer: list[str] = []
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                # Bump the stall timer on every non-empty event line.
                stall_state["last_event_at"] = time.monotonic()
                # Fast-path: capture the single result event that closes a
                # claude stream-json session (one per session). Cheap string
                # check first so we don't JSON-parse every assistant turn twice.
                if '"type":"result"' in line:
                    try:
                        _ev = json.loads(line)
                        if isinstance(_ev, dict) and _ev.get("type") == "result":
                            session_meta["cost_usd"] = _ev.get("total_cost_usd")
                            _u = _ev.get("usage") if isinstance(_ev.get("usage"), dict) else {}
                            session_meta["tokens_input"]  = _u.get("input_tokens")
                            session_meta["tokens_output"] = _u.get("output_tokens")
                    except (json.JSONDecodeError, ValueError):
                        pass
                formatted = _format_agent_event(line)
                if formatted is None:
                    continue
                # Strip credential-shaped substrings (GH PATs, Anthropic API
                # keys, Bearer tokens, etc.) before they hit docker stdout or
                # the PM API session-log buffer. Catches the case where the
                # agent inlines a token literal into a Bash command.
                formatted = _redact_secrets(formatted)
                # Surface tool calls, errors, and the final result at INFO so they
                # appear in `docker logs pf-orchestrator`. Routine assistant text
                # stays at DEBUG. PM API session log buffer gets everything.
                if any(token in formatted for token in (
                    "[tool]", "[tool_err]", "[result:", "WARNING:", "ERROR",
                    "Traceback", "non-retryable", "exit=2", "task_done",
                )):
                    log.info(f"[agent] {formatted}")
                else:
                    log.debug(f"[agent] {formatted}")
                buffer.append(formatted)
                if len(buffer) >= 10:
                    _post_log_lines(product["id"], buffer)
                    buffer = []
            if buffer:
                _post_log_lines(product["id"], buffer)

        log_thread = threading.Thread(target=_stream_logs, daemon=True)
        log_thread.start()

        # Stall watchdog (Symphony pattern). Runs alongside session_timeout:
        #   - session_timeout (90 min default) — upper bound on a productive run
        #   - stall_timeout (10 min default)   — no agent events for this long
        # The stall window is much shorter, so a hung pytest / Ollama 500-loop /
        # silent claude child gets caught at minute 10 instead of minute 90.
        # Bumped 5→10 in fix #6: at 5 min, the Ollama backoff (2/4/8/16s × 5
        # retries × N models = 30+s/turn) would trip the stall during normal
        # retry handling, contributing to the 35-39% kill rate.
        # Disabled when stall_timeout_minutes <= 0.
        stall_timeout_seconds = int(sys_cfg.get("stall_timeout_minutes")
                                    or os.environ.get("STALL_TIMEOUT_MINUTES", "10")) * 60
        _stall_stop = threading.Event()
        def _stall_watchdog():
            if stall_timeout_seconds <= 0:
                return
            while not _stall_stop.wait(30):  # check every 30s
                idle = time.monotonic() - stall_state["last_event_at"]
                if idle > stall_timeout_seconds:
                    log.warning(
                        f"[stall] {product.get('name')}: no agent events for "
                        f"{int(idle)}s (>{stall_timeout_seconds}s) — killing container"
                    )
                    _stall_kill.set()
                    try:
                        subprocess.run(
                            ["docker", "kill", f"pf-{product['id']}-{session_uid}"],
                            capture_output=True, timeout=30,
                        )
                    except Exception:
                        log.exception("[stall] docker kill failed")
                    return
        threading.Thread(target=_stall_watchdog, daemon=True).start()

        # Live-poll session_result.json while container runs — applies DB updates in real-time
        # as the agent writes phase transitions (Implementing → Reviewing, Blocked, etc.).
        _poll_stop = threading.Event()
        poll_thread = threading.Thread(
            target=_live_poll_session_result,
            args=(working_dir, _poll_stop, persona),
            daemon=True,
        )
        poll_thread.start()

        try:
            process.wait(timeout=session_timeout_seconds)
        except subprocess.TimeoutExpired:
            log.error(f"Session timed out after {session_timeout_seconds}s — killing container")
            try:
                subprocess.run(
                    ["docker", "kill", f"pf-{product['id']}-{session_uid}"],
                    capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                log.error(f"docker kill timed out — container may still be running: pf-{product['id']}-{session_uid}")
            send_alert("error", f"{product['name']}: session timed out after {session_timeout_seconds//60}m")
            exit_code = 1
        else:
            exit_code = process.returncode
            # Distinguish a stall-kill (we killed the container due to no events)
            # from a normal exit so the alert + exit_code reflect reality.
            if _stall_kill.is_set():
                send_alert("warning",
                           f"{product['name']}: agent stalled (no events for "
                           f"{stall_timeout_seconds//60}m) — killed by stall watchdog")
                exit_code = 1
        finally:
            _hb_stop.set()         # stop heartbeat thread
            _poll_stop.set()       # signal live-poll thread to stop
            _stall_stop.set()      # stop stall watchdog
            log_thread.join(timeout=10)
            if log_thread.is_alive():
                log.warning(f"[{product.get('name')}] log_thread did not exit after 10s — orphaned (daemon)")
            poll_thread.join(timeout=5)
            if poll_thread.is_alive():
                log.warning(f"[{product.get('name')}] poll_thread did not exit after 5s — orphaned (daemon)")

    except FileNotFoundError:
        log.error("'docker' not found in PATH — is Docker installed and on PATH?")
        exit_code = 1
    except PermissionError as e:
        log.error(f"Permission denied running docker: {e}")
        exit_code = 1
    except Exception as e:
        log.exception(f"docker run failed unexpectedly: {e}")
        exit_code = 1

    return exit_code, session_meta


def _finalize_session(
    product: dict,
    persona: str | None,
    session_uid: str,
    working_dir: str,
    session_id: int | None,
    exit_code: int,
    session_meta: dict,
) -> int:
    """
    Post-exit reconciliation. Runs unconditionally after the agent container
    exits, regardless of exit code. Five steps, in order:

      0. Path B post-session pipeline:
           coder            → _run_post_coder_pipeline (git+PR ceremony)
           planner/designer → _run_post_doc_pipeline   (commit docs to main)
                              + supervisor.detect_false_success
      1. Read session_result.json ONCE (shared with step 2)
      2. Auto-merge approved PRs (reviewer sessions with auto_merge_enabled)
      3. _reconcile_session_result — apply state machine, delete the file
      4. (in finally) supervisor.detect_kill_recovery on non-zero exit; PATCH
         the session record with end status + cost/token totals
      5. _rollback_stuck_features for non-zero exits or zero-progress runs
         (coder/designer only); _cleanup_workspace_post_session

    On a successful coder run, recursively launches qa_tester then
    security_auditor before returning. Returns the final exit_code (or 2
    propagated unchanged when the agent reported a non-retryable error).

    Extracted from run_claude_in_docker during Phase 3 of OrchestratorRefactor.
    """
    _session_features: list = []
    attempted = 0
    pushed = 0
    post_coder_pushed: list[int] = []
    try:
        # 0. Path B post-* ceremony — orchestrator owns ALL git for personas
        # that produce files. Agent only edits files + writes session_result.json;
        # we commit, push, and (for coder) open/update the PR. Replaces the
        # Symphony "agent owns git, fallback handles laggards" model: agents
        # were fake-claiming Reviewing in sprint-PR mode, leaving reviewers
        # to crawl over empty PRs. See commit 6b0473b for the symptom history.
        if exit_code == 0 and persona == "coder":
            try:
                post_coder_pushed = _run_post_coder_pipeline(
                    product, session_uid, working_dir,
                    product.get("_assigned_features", []),
                )
            except Exception:
                log.exception(f"Post-coder pipeline failed for {product.get('name')}")
        elif exit_code == 0 and persona == "designer":
            try:
                _run_post_doc_pipeline(product, session_uid, working_dir,
                                       product.get("_assigned_features", []),
                                       persona)
            except Exception:
                log.exception(f"Post-{persona} pipeline failed for {product.get('name')}")

            # Phase-1 supervisor: detect false-success (exit 0, no PR pushed,
            # no features advanced). Bumps fix_attempts + demotes to
            # changes_requested so the feature doesn't sit Implementing
            # forever waiting on reset_stuck. Best-effort — never raises.
            try:
                from orchestrator.supervisor import detect_false_success
                detect_false_success(
                    product_id=product["id"],
                    session_uid=session_uid,
                    exit_code=exit_code,
                    assigned_features=product.get("_assigned_features", []),
                )
            except Exception:
                log.exception(f"Supervisor detect_false_success failed for {product.get('name')}")

        # 1. Read session_result.json ONCE — shared between auto_merge and reconcile so
        #    the file isn't deleted before auto_merge can act on it.
        _session_features = _read_session_result(working_dir)

        # 2. Auto-merge BEFORE reconcile so merge/re-queue decisions override raw agent state.
        if exit_code == 0 and persona == "reviewer" and product.get("_auto_merge_enabled"):
            _session_features = _auto_merge_approved(product, _session_features)

        # 3. Apply status updates (deletes session_result.json at end).
        _reconcile_session_result(working_dir, product["id"], exit_code, features=_session_features, persona=persona)

        assigned_ids = {f["id"] for f in product.get("_assigned_features", [])}
        attempted = len(assigned_ids)
        # `pushed` = features the orchestrator (post-coder) actually moved to
        # Reviewing on this run, plus any session_result.json self-reports the
        # reconciler accepted. Counted once per feature ID so a feature
        # advanced by both paths doesn't double-count. Pre-2026-05-06 this
        # counted Designed entries as pushed — wrong, since Designed is a
        # regression — and ignored post-coder's direct PATCHes entirely.
        from_session_result = {
            f["id"] for f in _session_features
            if isinstance(f, dict)
            and f.get("id") in assigned_ids
            and f.get("status") in ("Pushed", "Reviewing", "Reviewed")
        }
        pushed = len(set(post_coder_pushed) | from_session_result)
    except Exception:
        log.exception(f"Post-exit reconciliation failed for {product.get('name')}")
    finally:
        # Phase-1 supervisor: kill-recovery. Bumps fix_attempts on every
        # assigned feature still in agent state when the session died non-
        # zero (watchdog kill, container OOM, manual kill). Without this the
        # same feature gets re-assigned next cycle and gets killed again,
        # because reset_stuck only resets status (not fix_attempts) — the
        # auto-Block route never triggers.
        if exit_code is not None and exit_code != 0:
            try:
                from orchestrator.supervisor import detect_kill_recovery
                detect_kill_recovery(
                    product_id=product["id"],
                    session_uid=session_uid,
                    persona=persona,
                    exit_code=exit_code,
                    assigned_features=product.get("_assigned_features", []),
                )
            except Exception:
                log.exception(f"Supervisor detect_kill_recovery failed for {product.get('name')}")

        # 4. Always record session end — guaranteed even if reconciliation raises.
        if session_id is not None:
            try:
                # FSM transition: exit_code=0 → ended, non-zero → killed (watchdog
                # may have already set status=killed if it was the one that fired).
                end_status = "ended" if exit_code == 0 else "killed"
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    patch_body = {
                        "status":            end_status,
                        "ended_at":          datetime.now(timezone.utc).isoformat(),
                        "exit_code":         exit_code,
                        "features_attempted": attempted,
                        "features_pushed":   pushed,
                    }
                    # Merge captured cost/token totals from the claude result
                    # event (None values dropped — they'd overwrite anything
                    # an earlier reconcile already set).
                    patch_body.update({k: v for k, v in session_meta.items() if v is not None})
                    client.patch(f"/api/sessions/{session_id}", json=patch_body)
            except Exception as e:
                log.warning(f"Could not update session record: {e}")

    # 5. Roll back any intermediate-state features with no PR evidence.
    #    Covers cases where agent claimed a feature but never finished it.
    #    Also rolls back on exit_code==0 with no progress (claimed but never written session_result).
    if exit_code != 0:
        log.warning(f"Non-zero exit ({exit_code}) for {product['name']} — rolling back incomplete features")
        _rollback_stuck_features(product["id"], persona)
    elif attempted == 0 and persona in ("coder", "designer"):
        log.info(f"Zero-progress exit for {product['name']} — rolling back claimed features")
        _rollback_stuck_features(product["id"], persona)

    # Post-session cleanup: return workspace to clean main so next session starts fresh
    _cleanup_workspace_post_session(working_dir, product.get("name", str(working_dir)))

    if exit_code == 2:
        return 2

    # Phase 2 simplification (2026-05-06): qa_tester and security_auditor
    # were merged into the reviewer persona. The reviewer's prompt now
    # covers tri-section review (functional + tests + security) on the
    # same diff in a single session. Post-coder cascade collapses to
    # just-launch-the-cycle — the next poller pass will pick up the
    # feature in Reviewing/pr_number set and dispatch the merged reviewer.
    # See futureplan.md.
    return exit_code


def run_claude_in_docker(product: dict, persona: str | None = None) -> int:
    """
    Launches the agent container. Blocks until container exits.
    Returns Docker exit code (0 = clean, non-zero = crash/auth failure).
    persona: 'designer', 'coder', 'reviewer', or None (uses legacy routing).
    """
    session_uid = str(uuid.uuid4())[:8]
    # working_dir_host  = what the Docker daemon sees (Windows path from DB)
    # working_dir       = what THIS Python process sees (translated to /products/... inside Hermes,
    #                     unchanged in host-mode). Used for every subsequent file op.
    working_dir_host, working_dir = _prepare_workspace(product)

    # ── Fetch sys_cfg FIRST — must happen before build_prompt so all substitution
    #    vars (auto_merge_enabled, assigned_features, prev_session_summary) are ready.
    sys_cfg = _get_system_config_sync()
    effective_backend = (sys_cfg.get("agent_backend") or AGENT_BACKEND)
    session_timeout_seconds = int(sys_cfg.get("session_timeout_minutes") or 0) * 60 or _DEFAULT_SESSION_TIMEOUT_SECONDS
    effective_max_features = int(sys_cfg.get("max_features_per_run") or MAX_FEATURES_PER_SPRINT)

    # Read all runtime settings from sys_cfg (DB → env var → built-in default).
    # Never rely on module-level constants after this point.
    effective_ollama_host    = sys_cfg.get("ollama_host")    or OLLAMA_HOST    or "http://host.docker.internal:11434"
    effective_ollama_api_key = sys_cfg.get("ollama_api_key") or os.environ.get("OLLAMA_API_KEY", "")

    # Per-persona Ollama model resolution (mirrors Claude routing). Order:
    #   1. sys_cfg.ollama_model_map[persona]  — explicit override
    #   2. legacy designer_model / coder_model split
    # Each lookup may yield a string OR a list of strings (primary-first
    # fallback chain, e.g. ["gpt-oss:120b", "qwen3-coder:480b"]). Always
    # normalise to a comma-separated string so the agent's MODEL parser
    # can split it back into a list.
    def _normalize_model_chain(value, fallback: str) -> str:
        if value is None or value == "":
            return fallback
        if isinstance(value, list):
            items = [str(x).strip() for x in value if str(x).strip()]
            return ",".join(items) if items else fallback
        # String (legacy single value or already-comma-joined chain)
        return str(value).strip() or fallback

    _ollama_map = sys_cfg.get("ollama_model_map") or {}
    _persona_override = (
        _ollama_map.get(persona)
        if isinstance(_ollama_map, dict) and persona else None
    )
    if persona in ("designer", "reviewer"):
        _legacy_default = sys_cfg.get("designer_model") or DESIGNER_MODEL or "qwen3-coder:30b"
    else:
        _legacy_default = sys_cfg.get("coder_model") or CODER_MODEL or "qwen3-coder:30b"
    effective_persona_model = _normalize_model_chain(_persona_override, _legacy_default)
    effective_designer_model = _normalize_model_chain(
        _ollama_map.get("designer") if isinstance(_ollama_map, dict) else None,
        sys_cfg.get("designer_model") or DESIGNER_MODEL or "gemma3:27b",
    )
    effective_coder_model = _normalize_model_chain(
        _ollama_map.get("coder") if isinstance(_ollama_map, dict) else None,
        sys_cfg.get("coder_model") or CODER_MODEL or "qwen3-coder:30b",
    )
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
    assigned_features, active_sprint_name, active_sprint = _fetch_assigned_features(product["id"], persona, effective_max_features)
    _claim_features(assigned_features, persona)
    product["_assigned_features"] = assigned_features
    product["_assigned_features_md"] = _format_assigned_features(assigned_features, persona)
    # Pull recent reviewer/auditor comments for any feature in a rework cycle
    # so the coder prompt can render them under {reviewer_feedback}. Coder
    # only — other personas don't need it (designer writes docs, reviewer
    # IS the source of comments, qa/security manage their own context).
    product["_reviewer_feedback_md"] = (
        _format_reviewer_feedback(assigned_features) if persona == "coder" else ""
    )
    product["_active_sprint"] = active_sprint or {}

    # Sprint-PR-mode context: when the product opts in via config.sprint_pr_mode
    # AND the active sprint has been provisioned with a branch + PR (see
    # orchestrator.sprint_pr.provision_sprint_pr), agents push to that branch
    # instead of cutting fresh `coder/<uid>` branches and opening parallel PRs.
    # Off by default — the per-feature branch flow remains the fallback.
    _cfg_flags = (product.get("config") or {})
    product["_sprint_pr_mode"] = bool(
        _cfg_flags.get("sprint_pr_mode")
        and active_sprint
        and active_sprint.get("branch_name")
    )
    product["_sprint_branch"] = (active_sprint or {}).get("branch_name") or ""
    product["_sprint_pr_number"] = (active_sprint or {}).get("pr_number") or ""
    product["_sprint_pr_url"] = (active_sprint or {}).get("pr_url") or ""

    # Pre-checkout the sprint branch so the agent's very first tool call —
    # regardless of whether it follows the prompt's MANDATORY-FIRST-ACTION
    # block — runs against `sprint/<id>` rather than `main`. Without this,
    # tiny models reliably skip the checkout and fall through to grepping
    # files on main; the post-coder pipeline can transfer dirty changes
    # but it's wasted turns and confusing transcripts.
    # Only fires when sprint_pr_mode is on AND the sprint already has a
    # provisioned branch (i.e. _sprint_pr_mode==True is the same gate).
    if product.get("_sprint_pr_mode"):
        _checkout_sprint_branch(
            working_dir,
            product["_sprint_branch"],
            product.get("name", str(working_dir)),
        )

    # Write sprint-scoped features.md to working dir (replaces any stale full-backlog copy)
    _write_sprint_features_md(working_dir, assigned_features, active_sprint_name)

    # Inject previous session summary for continuity.
    product["_prev_session_summary"] = _read_session_summary(working_dir)

    prompt = build_prompt(product, session_uid, persona=persona, max_features=effective_max_features, backend=effective_backend)

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
        ssh_mount = ["-v", f"{host_path(deploy_key)}:/home/agent/.ssh/id_ed25519:ro"]

    # GH_TOKEN — staged to a 0600 temp file and bind-mounted at /run/secrets/gh_token.
    # The agent_cmd wrapper (below) sources it into GH_TOKEN at runtime, so `gh` CLI
    # works but the token is never visible in `docker inspect` / process listings.
    gh_token_file, gh_mount = _stage_gh_token_mount(_get_gh_token())
    gh_env: list[str] = []  # legacy name retained — now always empty (token comes via file)

    persona_env = ["-e", f"AGENT_PERSONA={persona}"] if persona else []

    # Select the agent command based on backend
    _tmp_claude_dir: str | None = None
    if effective_backend == "ollama":
        agent_cmd = ["python", "//app/ollama_agent.py", "-p", prompt]
        ollama_env = [
            "-e", f"OLLAMA_HOST={effective_ollama_host}",
            "-e", f"OLLAMA_API_KEY={effective_ollama_api_key}",
            # OLLAMA_MODEL is the resolved per-persona model — agent uses this
            # if set; otherwise falls back to DESIGNER_MODEL/CODER_MODEL split.
            "-e", f"OLLAMA_MODEL={effective_persona_model}",
            "-e", f"DESIGNER_MODEL={effective_designer_model}",
            "-e", f"CODER_MODEL={effective_coder_model}",
            "-e", f"MAX_FEATURES_PER_SPRINT={effective_max_features}",
            "-e", f"OLLAMA_TIMEOUT={effective_ollama_timeout}",
            "-e", f"MAX_TURNS={effective_max_turns}",
            "-e", f"BASH_TIMEOUT={effective_bash_timeout}",
        ]
        # Ollama backend: no Claude OAuth mount needed
        claude_mount = []
        log.info(f"Using Ollama backend — host={effective_ollama_host} persona={persona} model={effective_persona_model}")
    else:
        # Claude backend: stage credentials to a temp dir to avoid 9P races with
        # the host's live Claude Code process. Returns the mount args + cleanup
        # tmpdir + resolved per-persona model.
        claude_mount, _tmp_claude_dir, claude_model = _stage_claude_credentials(sys_cfg, persona)

        # --dangerously-skip-permissions works now that container runs as non-root.
        # --output-format stream-json + --verbose turns claude -p into a streaming
        # NDJSON emitter (one event per line: assistant turns, tool_use, tool_result,
        # final result). Without this, claude -p only prints the FINAL message at
        # session end, so the orchestrator's _stream_logs reader sees nothing for
        # the entire run — no observability into what the agent is doing.
        agent_cmd = ["claude", "--dangerously-skip-permissions",
                     "--output-format", "stream-json", "--verbose",
                     "-p", prompt]
        ollama_env = [
            "-e", f"MAX_FEATURES_PER_SPRINT={effective_max_features}",
            "-e", f"CLAUDE_MODEL={claude_model}",
            "-e", f"MAX_TURNS={effective_max_turns}",
            "-e", f"BASH_TIMEOUT={effective_bash_timeout}",
        ]

    # If GH_TOKEN is provided via file mount, wrap agent_cmd so it's exported to the
    # agent's env at startup. Using `sh -c ... exec cmd` keeps the token out of
    # `docker inspect` while still making it available to gh CLI inside the container.
    # For the Claude backend, also start the self-heartbeat loop in the background so
    # the session survives orchestrator restarts (Ollama has its own in-Python).
    if gh_mount:
        quoted = " ".join(shlex.quote(a) for a in agent_cmd)
        prelude = '[ -r /run/secrets/gh_token ] && export GH_TOKEN="$(cat /run/secrets/gh_token)"; '
        if effective_backend == "claude":
            prelude += _AGENT_HEARTBEAT_SH + " "
        agent_cmd = [
            "sh", "-c",
            prelude + f'exec {quoted}',
        ]

    cmd = [
        "docker", "run", "--rm",
        "--name", f"pf-{product['id']}-{session_uid}",
        "--network", "productfactory-net",
        "--add-host", "pm-api:host-gateway",  # resolves to Windows host where pm-api container exposes :8080
        "--add-host", "host.docker.internal:host-gateway",  # Ollama on Windows host
        # Resource limits
        "--memory", "4g",
        "--cpus", "2",
        "--pids-limit", "512",                     # cap process count — prevents fork-bombs
        # Sandbox hardening — agent container runs untrusted LLM-generated shell commands
        "--cap-drop", "ALL",                       # drop all Linux capabilities (agent runs as UID 1001)
        "--security-opt", "no-new-privileges:true",  # block setuid privilege escalation
        "--read-only",                             # rootfs is immutable; writes go to tmpfs/volumes below
        "--tmpfs", "/tmp:rw,size=1g,mode=1777",
        "--tmpfs", "/run:rw,size=64m",
        "--tmpfs", "/home/agent/.cache:rw,size=1g,uid=1001,gid=1001",
        "--tmpfs", "/home/agent/.npm:rw,size=500m,uid=1001,gid=1001",
        "--tmpfs", "/home/agent/.config:rw,size=100m,uid=1001,gid=1001",
        "--tmpfs", "/home/agent/.local:rw,size=500m,uid=1001,gid=1001",
        # Volume mounts — unaffected by --read-only
        "-v", f"{working_dir_host}:/workspace",
        *claude_mount,                             # OAuth session (claude backend only)
        *ssh_mount,                                # deploy key :ro (not whole .ssh dir)
        *gh_mount,                                 # GH_TOKEN via file at /run/secrets/gh_token (not env)
        *gh_env,                                   # empty unless mount failed — legacy fallback
        *persona_env,                              # AGENT_PERSONA for prompt selection
        *ollama_env,                               # Ollama model config (ollama backend only)
        "-e", f"PM_API_URL={PM_API_URL_CONTAINER}",
        "-e", f"SESSION_UID={session_uid}",
        AGENT_IMAGE,
        *agent_cmd,
    ]

    log.info(f"docker run: session={session_uid} product={product['name']}")

    # Record session start with FSM status=starting. PM API sets expected_deadline
    # based on SESSION_TIMEOUT_MINUTES so watchdog can authoritatively time it out.
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
                "status":       "starting",
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

    try:
        exit_code, session_meta = _stream_session(
            product=product,
            persona=persona,
            session_uid=session_uid,
            session_id=session_id,
            working_dir=working_dir,
            container_name=container_name,
            cmd=cmd,
            session_timeout_seconds=session_timeout_seconds,
            sys_cfg=sys_cfg,
        )
    finally:
        # Clean up temp credentials copy if we created one
        if _tmp_claude_dir:
            try:
                shutil.rmtree(_tmp_claude_dir, ignore_errors=True)
            except Exception:
                pass
        # Clean up gh_token temp file if we created one
        if gh_token_file:
            try:
                Path(gh_token_file).unlink()
            except Exception:
                pass

    # ── Post-exit reconciliation (runs regardless of exit code) ─────────────────
    return _finalize_session(
        product=product,
        persona=persona,
        session_uid=session_uid,
        working_dir=working_dir,
        session_id=session_id,
        exit_code=exit_code,
        session_meta=session_meta,
    )


def _post_log_lines(product_id: int, lines: list[str]) -> None:
    """Fire-and-forget: push log lines to PM API for SSE streaming."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.post(f"/api/products/{product_id}/session/log", json={"lines": lines})
    except Exception:
        pass
