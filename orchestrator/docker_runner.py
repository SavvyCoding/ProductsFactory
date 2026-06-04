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
import re
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
from orchestrator.paths import host_path, container_path
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

# pf-verify-env.sh in the agent image exits 42 when a mechanical preflight
# check fails (chrome-headless-shell missing/no-exec-bit, pytest unimportable,
# etc.). _finalize_session and detect_kill_recovery special-case this so the
# affected features are NOT charged a fix_attempt — the failure is
# environmental, not the agent's fault. Operator gets an alert with the
# failing-check string. See deploy/docker/pf-verify-env.sh.
EXIT_ENV_NOT_READY = 42

# orchestrator/agent_loop.py returns 43 when the LLM backend exhausts every
# model in the chain for an infrastructure reason (quota/rate-limit/auth/
# whole-chain 5xx). Like exit 42 the agent's feature work isn't to blame —
# _finalize_session and detect_kill_recovery release the claim without
# bumping fix_attempts so a quota window doesn't push every assigned
# feature to Blocked at fa=5. Real incident: MyTracking 2026-05-22, where
# 106 Ollama-Cloud 429s drove 47 features to Blocked over a single day.
EXIT_LLM_INFRA = 43

# ── Ollama backend config ─────────────────────────────────────────────────────
# Set AGENT_BACKEND=ollama to use local Ollama instead of the Claude CLI.
# Ollama must be running on the Windows host (accessible as host.docker.internal:11434).
AGENT_BACKEND  = os.environ.get("AGENT_BACKEND", "claude")   # "claude" | "ollama"
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST",   "http://host.docker.internal:11434")
DESIGNER_MODEL       = os.environ.get("DESIGNER_MODEL",       "gemma3:27b")
CODER_MODEL          = os.environ.get("CODER_MODEL",          "qwen3-coder:30b")
MAX_FEATURES_PER_SPRINT = int(os.environ.get("MAX_FEATURES_PER_SPRINT", "5"))


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
from orchestrator.integrations.docker_cli import (
    _chmod_workspace_via_alpine,
    _ensure_session_result_writable,
)


# Phase 2b of OrchestratorRefactor: planner/designer post-session pipeline
# moved into orchestrator/pipelines/post_doc.py. Re-exported below.
from orchestrator.pipelines.post_doc import (
    _run_post_doc_pipeline,
    _rollback_doc_features,
)


# Phase 2b of OrchestratorRefactor: coder post-session pipeline (the ~500 LOC
# git+GitHub fallback) moved into orchestrator/pipelines/post_coder.py.
from orchestrator.pipelines.post_coder import _run_post_coder_pipeline
# On-demand maintenance personas (documenter, analytics, recommender, devops,
# refactorer, product_trainer): orchestrator owns git add/commit/push for any
# file edits the agent left in the working tree. Without this, the agent's
# uncommitted work is discarded by the next _cleanup_workspace_post_session
# reset.
#
# product_trainer added 2026-05-18: previously the trainer prompt had its own
# Step 5 "git add output/ && git commit && git push" block, but it had no
# error check on push — a failed push (most commonly product_video_*.mp4
# exceeding GitHub's 100 MB single-file limit, with no Git LFS configured)
# silently left the commit local, and the next _reset_workspace --hard wiped
# it. Routing the trainer through the same maintenance pipeline as documenter
# means narration.md + slides + video are committed and pushed loudly: push
# failures land in the log via post_maintenance.py's stderr capture instead of
# being swallowed.
from orchestrator.pipelines.post_maintenance import _run_post_maintenance_pipeline

_MAINTENANCE_PERSONAS = frozenset({
    "documenter", "analytics", "recommender", "devops", "refactorer",
    "product_trainer", "architect",
})


# PM-curated files that always get mounted read-only into agent containers.
# These are stack-template artifacts (CLAUDE.md, AGENT_WORKFLOW.md, etc.) or
# greenfield-PM-set config (product_config.json, quality_gates.json) or
# operational invariants (.gitignore). No agent persona is authorized to
# modify these; until now the only enforcement was post-commit denylisting
# (post-coder) and allowlisting (post-doc / post-maintenance), which let
# the agent silently write and then silently strip — wasting LLM tokens
# and providing no feedback to the agent.
#
# With kernel-level RO bind mounts: `echo > /workspace/CLAUDE.md` from inside
# the agent container returns EROFS immediately. The lint guards become
# belt-and-suspenders rather than primary defense.
#
# Real incidents this hard-enforces against:
#   - MyDocusign PR #36 (2026-05-21): coder added 58-line `## AWS Shield`
#     section to ARCHITECTURE.md
#   - designer #608 (2026-05-20): deleted Phase-1 sections from ARCH.md
#   - designer #608: also rewrote large parts of CLAUDE.md
#   - architect ce3601f7 (2026-05-21): added rogue AWS Shield section
#
# ARCHITECTURE.md is handled per-persona — see _build_pm_curated_ro_mounts.
_PM_CURATED_RO_FILES = (
    "CLAUDE.md",
    "AGENT_WORKFLOW.md",
    "CONTRIBUTING.md",
    "quality_gates.json",
    "product_config.json",
    ".gitignore",
    # Pre-commit self-review helper installed by templates/renderer.py.
    # RO so the coder can't tamper with the check to return clean — see
    # AGENT_WORKFLOW.md Step 5b and post_coder.py Guard 17.
    "check_deletion_safety.py",
    # Pre-shipped pytest config (python stack only — other stacks have
    # no host file at this path, so the conditional mount is a no-op).
    # RO so the coder can't write a broken version (mismatched pythonpath
    # vs `from src.X` import style → ModuleNotFoundError on collection).
    # Canonical 2026-05-26 SmokeTest #950 incident — see renderer.py.
    "pytest.ini",
)


def _build_pm_curated_ro_mounts(
    working_dir: str,
    working_dir_host: str,
    persona: str | None,
) -> list[str]:
    """Return `-v <host>:<container>:ro` docker args for PM-curated files.

    Docker bind-mount overlay semantics: a more-specific bind mount
    overrides a less-specific one at the same path. So after the parent
    `-v {working_dir_host}:/workspace` (RW), these per-file `:ro` mounts
    make each specific file read-only while the rest of /workspace stays
    writable. The agent's `sed`/`echo`/`cat >` against these paths returns
    EROFS at the kernel level — no syscall succeeds, no post-commit cleanup
    needed.

    Per-persona logic: architect is the ONLY persona authorized to write
    ARCHITECTURE.md (enforced by orchestrator/pipelines/post_maintenance.py's
    `_MAINTENANCE_ALLOWLISTS["architect"]`). For architect, ARCHITECTURE.md
    stays RW (no overlay). For every other persona, it gets the RO overlay.

    Defensive: skips files that don't exist on the host. Docker file-level
    bind mounts behave badly when the source is missing — Docker silently
    creates a DIRECTORY at the source path. That would (a) corrupt the
    working tree by replacing a missing file with an empty directory and
    (b) make the container's view of that path a directory instead of a
    file, breaking any code that tries to read it. Greenfield first
    sessions where the renderer hasn't installed templates yet would hit
    this. Skip-if-missing is the only safe default.
    """
    args: list[str] = []
    files_to_mount = list(_PM_CURATED_RO_FILES)
    # Architect: skip ARCHITECTURE.md (needs RW to add MODULES rows).
    # Everyone else: prepend it so it gets the RO overlay.
    if persona != "architect":
        files_to_mount.insert(0, "ARCHITECTURE.md")
    # OSS borrowing #4 (Agentless-style immutable reproducer / SWE-agent
    # "the design doc is the contract"): the coder and reviewer must not
    # be able to "fix" a failing AC by editing the design doc instead of
    # the code. Designer writes docs/story_*.md; everyone else reads.
    # Architect (the maintenance reviewer of doc state) is also RO here —
    # ARCHITECTURE.md is its write target, not story docs.
    if persona in ("coder", "reviewer", "architect"):
        try:
            import glob as _glob
            docs_dir = os.path.join(working_dir, "docs")
            if os.path.isdir(docs_dir):
                for path in _glob.glob(os.path.join(docs_dir, "story_*.md")):
                    rel = os.path.relpath(path, working_dir).replace("\\", "/")
                    files_to_mount.append(rel)
        except Exception:
            pass  # best-effort; skip-if-missing logic below handles it
    for filename in files_to_mount:
        # Use the in-process working_dir for the file-exists check (the
        # orchestrator can stat its own bind-mounted /products path).
        # Use working_dir_host for the docker bind spec (what the Docker
        # daemon sees on the host filesystem).
        if not os.path.isfile(os.path.join(working_dir, filename)):
            continue
        # Forward slashes inside the host_spec join are accepted by Docker
        # on both Linux and Windows. The existing parent mount
        # (`-v {working_dir_host}:/workspace`) uses the same path format.
        host_spec = f"{working_dir_host}/{filename}"
        container_spec = f"/workspace/{filename}"
        args.extend(["-v", f"{host_spec}:{container_spec}:ro"])
    return args


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
    Read context from the previous agent session via session_summary.md
    (the incremental log written throughout the session). Returns empty
    string if the file doesn't exist (first session).

    The legacy progress.md fallback was removed 2026-05-28 — agents stopped
    writing progress.md, so the fallback never fired; session_summary.md is
    the sole continuity channel now.
    """
    summary_file = Path(working_dir) / "session_summary.md"
    if not summary_file.exists():
        return ""
    try:
        content = summary_file.read_text(encoding="utf-8").strip()
        max_chars = 2000
        if len(content) > max_chars:
            # Tail-keep: agents append the most recent / closing notes (per-feature
            # verification notes, the end-of-batch "Session State Summary") to the
            # END of the file. Keep the tail so that the freshest context survives;
            # front-truncation used to drop exactly the part the next session needs.
            content = "...[truncated]\n" + content[-(max_chars - 50):]
        return content
    except Exception as e:
        log.warning(f"Could not read session_summary.md: {e}")
        return ""


def _fetch_assigned_features(product_id: int, persona: str | None, max_count: int = MAX_FEATURES_PER_SPRINT) -> tuple[list[dict], str | None, dict | None]:
    """
    Pre-fetch features the agent should work on this session.

    Phases→features flat model (migration 043): no sprint scoping. We pick
    eligible features by status + persona-specific filters, ordered by
    priority. Each feature ships as its own session PR.

    Returns ([], None, None) for personas that manage their own work
    (recommender, architect, etc.).

    Tuple shape: (features, sprint_name, sprint_dict) — sprint_name and
    sprint_dict are kept as None for callsite + test-stub compatibility;
    the flat model has no sprint context.
    """
    if persona not in ("coder", "designer", "reviewer"):
        return [], None, None
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get(f"/api/products/{product_id}/features")
            resp.raise_for_status()
            all_features = resp.json()
            all_features = all_features if isinstance(all_features, list) else []

            if persona == "coder":
                # Match the API's /next-for-persona?persona=coder rules so the
                # orchestrator's persona dispatch and the agent's actually-
                # assigned features stay in sync.
                features = [f for f in all_features
                            if f.get("status") == "Designed"
                            or (f.get("status") == "Approved" and f.get("design_doc_path"))
                            or (f.get("status") == "Implementing"
                                and f.get("review_outcome") == "changes_requested")]
            elif persona == "reviewer":
                # Reviewer follows the SESSION PR: every reviewer session
                # reviews exactly one open session PR's worth of features.
                # Group by pr_number, pick the oldest (GitHub assigns
                # monotonically per repo) so older work doesn't starve
                # waiting on newer session PRs.
                candidates = [f for f in all_features
                              if f.get("status") == "Reviewing"
                              and f.get("pr_number")]
                if candidates:
                    from collections import defaultdict as _dd
                    by_pr: dict = _dd(list)
                    for _f in candidates:
                        by_pr[_f["pr_number"]].append(_f)
                    oldest_pr = min(by_pr.keys())
                    features = by_pr[oldest_pr]
                    log.info(
                        f"[assign] reviewer scoped to session PR #{oldest_pr} "
                        f"({len(features)} feature(s)); {len(by_pr)} open "
                        f"session PR group(s) total"
                    )
                else:
                    features = []
            elif persona == "designer":
                features = [f for f in all_features
                            if f.get("status") == "Approved" and not f.get("design_doc_path")]
            else:
                features = []

        # Sort: stuck-rework features (Implementing+changes_requested AND
        # fix_attempts >= REWORK_CAP_PROXIMITY) go LAST so the coder picks
        # a healthy fresh feature when both exist. Within each bucket,
        # priority ASC (LOWER number = higher rank) + id asc.
        #
        # Cycle DQ (2026-06-01) follow-up to cycle DM-2: the dispatcher
        # fix de-prioritized near-cap reworks at the persona level
        # (cycle/persona.py 2a), but here in _fetch_assigned_features
        # the coder was still picking stuck reworks because the priority
        # sort put them at the top. Canonical: DocumentSign #1178 has
        # higher priority than 1101/1104/etc; with fix_attempts=4 it
        # kept winning even though it's doomed.
        #
        # Cycle GM (2026-06-04): the priority direction was wrong. The
        # PM API (`/api/features/next-for-persona` and
        # `website/main.py:739`) sorts ASC — LOWER priority number means
        # HIGHER priority. This dispatcher used `-priority` DESC, which
        # inverted the convention. Canonical incident: chore #1358
        # (Fix setup_test_env fixture, filed at priority=5) sat
        # unpicked by designer for 3+ hours while designer kept picking
        # priority=50–60 features that ranked HIGHER under the DESC
        # convention. Flipped to ASC to match the PM API. Note that
        # drift_detectors had been working around this bug by filing
        # chores at priority=99; that workaround is now retired (see
        # drift_detectors.py docstring) and new chores file at
        # priority=1.
        #
        # This sort must mirror cycle/persona.py's REWORK_CAP_PROXIMITY
        # threshold (3). Move stuck reworks to the end of the bucketed
        # list — they still get picked when no fresh codeable exists,
        # so they can hit cap-Block naturally.
        _REWORK_CAP_PROXIMITY = 3

        def _is_stuck_rework(f: dict) -> bool:
            return (
                f.get("status") == "Implementing"
                and f.get("review_outcome") == "changes_requested"
                and (f.get("fix_attempts") or 0) >= _REWORK_CAP_PROXIMITY
            )

        features.sort(key=lambda f: (
            1 if _is_stuck_rework(f) else 0,
            f.get("priority") if f.get("priority") is not None else 50,
            f.get("id") or 0,
        ))
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
                "branch_name": f.get("branch_name"),
                "fix_attempts": f.get("fix_attempts", 0),
                "blocked_reason": f.get("blocked_reason"),
                "review_outcome": f.get("review_outcome"),
                "phase_id": f.get("phase_id"),
                "parent_id": f.get("parent_id"),
            }
            for f in selected
        ], None, None
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


def _fetch_recent_review_comments(feature_id: int, limit: int = 25) -> list[dict]:
    """
    Pull the comments relevant to the CURRENT rework cycle. Returns
    oldest-first within the slice so the prompt-renderer can stack them
    in chronological order under the feature.

    Filter (2026-06-01): only the LATEST bounce's feedback drives the
    rework — not the accumulated history. The bounce can come from the
    reviewer OR from any post-coder gate (lint-guard, post-coder:
    test-check, post-coder:verify-check); whichever fired most recently
    is what the coder needs to address. Paired with the coder-rework-
    pre-checkout in `_prepare_workspace` — the coder now lands on the
    previous coder branch with the prior implementation intact, so
    they're patching specific issues against existing code, not
    re-deriving the whole feature from a long accumulated checklist.
    Implementation:

      - Find the timestamp of the most recent comment (any allowed
        author: reviewer, lint-guard, post-coder:test-check,
        post-coder:verify-check). That's the "last bounce."
      - Define a 10-minute window backward from that timestamp.
        Reviewer sessions post multiple comments (one per failing
        section: functional / tests / security) within tens of seconds.
        Post-coder gates can also stack (e.g. lint-guard violation
        then test-check failure on the same commit). The window
        captures the last bounce's coherent feedback set without
        bleeding into prior bounces.
      - Anything older than the window is from a PRIOR bounce that
        the coder either already addressed (post-checkout, the prior
        code in the workspace reflects those fixes) or that the
        latest reviewer / gate has decided to re-raise. Either way,
        the latest signal is the truth source.

    Earlier behavior (2026-05-06 → 2026-06-01) was the opposite —
    accumulate up to 25 prior comments so unresolved items from older
    sessions couldn't be forgotten. That made sense when the workspace
    was reset to clean main each cycle (the coder needed the full
    history because the *code* was gone). With the rework branch now
    persisted, the prior code carries the prior decisions; the
    accumulated text was making the coder address already-fixed flags
    and ignore the actual latest rejection.
    """
    import datetime as _dt
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
            # Include lint-guard + post-coder:test-check comments too —
            # under the phases→features flat model, the deterministic
            # post-coder pipeline catches lint / test failures before the
            # reviewer ever runs. Without these in the rework feedback the
            # coder retries blind ("changes_requested but why?") and the
            # lint-rework loop never converges. Real example surfaced by
            # the 2026-05-26 SmokeTest smoke test: feature #950 cycled
            # 1 → 2 → 1 violations across three coder runs because the
            # specific "pytest.ini missing" and "skipped tests in
            # tests/test_database.py" feedback was authored by lint-guard,
            # not reviewer, and therefore excluded from the prompt.
            _ALLOWED_AUTHORS = {
                "reviewer", "lint-guard",
                "post-coder:test-check", "post-coder:verify-check",
                # post-coder:test-env: pip-install / runner-missing failures.
                # Without this, the env_broken handler's comment (containing
                # the literal pip error excerpt) is invisible to the next
                # coder cycle and the agent ships the same bad pin forever.
                # Canonical 2026-06-01 DocumentSign #1102 cascade: pin
                # `opentelemetry-instrumentation-fastapi<1,>=0.40` doesn't
                # resolve (all available versions are pre-release beta
                # tags); env_broken fired twice in a row at 19:16:22 and
                # 19:27:41 with identical state.
                "post-coder:test-env",
                # system:reviewer-validation: reviewer text-vs-structured
                # mismatch rejections (shipped 2026-06-03 OSS borrowing #1
                # in orchestrator/session/result_io.py). Surfacing the
                # rejection reason ensures the next session — which may be
                # a coder if the reviewer outcome lands on changes_requested
                # via the trust_json fallback — sees the prior validation
                # complaint and doesn't repeat it.
                "system:reviewer-validation",
            }
            relevant = [c for c in data
                        if (c.get("author") or "").lower() in _ALLOWED_AUTHORS]
            if not relevant:
                return []
            # Last-bounce filter (2026-06-01): keep only comments within
            # a 10-minute window backward from the most recent comment's
            # timestamp. See docstring for the rationale.
            last_ts_str = relevant[-1].get("created_at") or ""
            try:
                last_ts = _dt.datetime.fromisoformat(
                    last_ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                # Unparseable timestamp — bail back to the tail cap so we
                # don't silently drop everything. Better to over-include
                # than to leave the coder with no feedback at all.
                return relevant[-limit:]
            window_start = last_ts - _dt.timedelta(minutes=10)
            filtered: list[dict] = []
            for c in relevant:
                cts = c.get("created_at") or ""
                try:
                    cts_dt = _dt.datetime.fromisoformat(
                        cts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if cts_dt >= window_start:
                    filtered.append(c)
            return filtered[-limit:]
    except Exception as e:
        log.debug(f"[reviewer-feedback] could not fetch comments for #{feature_id}: {e}")
        return []


def _find_rework_branch(assigned_features: list[dict], product: dict,
                        product_name: str = "?") -> str:
    """Find the branch that the prior coder run pushed for this rework
    cycle, so the next coder lands on that branch and inherits the prior
    implementation. Returns "" when no rework branch is identifiable —
    caller leaves the session on main (fresh first-pass behavior).

    A "rework" feature has either `review_outcome=changes_requested` or
    `fix_attempts > 0`. We expect the 1-PR model to assign one branch to
    a coherent group of rework features (the session PR's head), so the
    set of branches/PRs across rework features should be singleton —
    we bail when it isn't.

    Strategy (in priority order):
      1. `feature.branch_name` from DB — set by post_coder on push,
         usually retained on reviewer bounce. Cheap and reliable.
      2. `feature.pr_number` from DB → GitHub `/pulls/{n}` → `head.ref`.
         Slower (network) but works when branch_name was cleared (some
         legacy flows clear it on status transition).

    Returns "" on any failure path — the coder falls back to the
    pre-existing fresh-main behavior, which is no worse than before
    this feature shipped.
    """
    rework_features = [
        f for f in assigned_features
        if f.get("review_outcome") == "changes_requested"
        or (f.get("fix_attempts") or 0) > 0
    ]
    if not rework_features:
        return ""
    # Strategy 1: branch_name on feature row
    branch_names = {f.get("branch_name") for f in rework_features
                    if f.get("branch_name")}
    if len(branch_names) == 1:
        return next(iter(branch_names))
    if len(branch_names) > 1:
        log.warning(
            f"[{product_name}] rework pre-checkout: assigned features have "
            f"divergent branch_names {branch_names}; skipping checkout "
            f"(coder starts on main)"
        )
        return ""
    # Strategy 2: pr_number → GitHub lookup
    pr_numbers = {f.get("pr_number") for f in rework_features
                  if isinstance(f.get("pr_number"), int)}
    if len(pr_numbers) != 1:
        return ""
    pr_n = next(iter(pr_numbers))
    try:
        gh_token = _get_gh_token()
        github_repo = product.get("github_repo", "")
        if not (gh_token and github_repo):
            return ""
        repo_slug = _parse_repo_slug(github_repo)
        r = httpx.get(
            f"https://api.github.com/repos/{repo_slug}/pulls/{pr_n}",
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        pr_data = r.json()
        if not isinstance(pr_data, dict) or pr_data.get("state") != "open":
            return ""
        return (pr_data.get("head") or {}).get("ref") or ""
    except Exception as e:
        log.debug(
            f"[{product_name}] rework pre-checkout: PR #{pr_n} lookup "
            f"failed: {e}"
        )
        return ""


def _format_reviewer_feedback(features: list[dict]) -> str:
    """
    Render the most recent bounce's feedback per feature as a Markdown
    block. Empty string when no feature in `features` has
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
        "## Latest feedback to address",
        "",
        "These features are in a **rework cycle** — the most recent "
        "reviewer or post-coder gate (lint-guard / test-check / "
        "verify-check) flagged specific issues on the prior commit. "
        "Your workspace IS the previous coder branch — the prior "
        "implementation is already in your tree (open `git log` to see). "
        "**Patch the listed items in place — do not reimplement from "
        "scratch.** Keep every working part of the prior code; only "
        "change what the listed feedback names.",
        "",
        "Only the LATEST bounce's feedback is included below (the "
        "10-minute window around the most recent comment). Earlier "
        "bounces' feedback was either already addressed in the code "
        "you now see, or the latest bounce decided to re-raise it — "
        "either way, the items below are what's still outstanding.",
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
_LIGHT_PERSONAS = {"planner", "documenter",
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
    _checkout_branch,
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

    # Pre-create session_result.json mode 666 so the agent (uid 1001) can
    # always append to it, regardless of who owned it before. The
    # `_fix_session_result_perms` thread inside `run_claude_in_docker` runs
    # 3s POST-launch — too late if the agent races ahead. See
    # docker_cli._ensure_session_result_writable docstring for the full
    # uid-999-vs-1001 incident analysis (reviewer 1983, 2026-05-07 01:32).
    _ensure_session_result_writable(working_dir, product.get("name", "?"))

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


def _select_end_status(*, exit_code: int, terminal_marker_seen: bool) -> str:
    """Pick the FSM status to write at session finalization.

    Three-way classifier — pulled out of ``_stream_session`` so it can be
    unit-tested without spinning up a docker process.

      exit_code != 0           → "killed"   (process aborted / timeout fired
                                              / watchdog killed it / hit
                                              non-zero internal exit)
      exit_code == 0 + marker  → "ended"    (agent declared completion via
                                              ``task_done`` or printed the
                                              final ``[metrics]`` line)
      exit_code == 0, no mark  → "lost"     (container exited cleanly but the
                                              agent never declared done —
                                              caught the SIGTERM/OOM/daemon-
                                              kill case that previously
                                              produced misleading status=ended
                                              rows alongside a populated
                                              kill_reason, e.g. sessions
                                              2406 / 2418)
    """
    if exit_code != 0:
        return "killed"
    if terminal_marker_seen:
        return "ended"
    return "lost"


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
    # `terminal_marker_seen` is flipped by _stream_logs when the agent emits
    # its canonical end-of-session log line ("Session completed via task_done"
    # from agent_loop.py, or "[metrics] session_id=..." from ollama_agent.py).
    # Used at finalize time to distinguish a real clean exit from a container
    # that vanished mid-turn but happened to record exit_code=0 — see the
    # status-selection block lower in this function.
    session_meta: dict = {"terminal_marker_seen": False}
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
                # Capture the canonical "agent finished cleanly" signal at
                # streaming time so finalize can distinguish ended vs lost
                # without re-scanning the log (race-prone — the buffer may
                # not have flushed yet). Two markers, either is sufficient:
                #   - "Session completed via task_done"  (agent_loop.py:152)
                #   - "[metrics] session_id="             (ollama_agent.py:967)
                if not session_meta["terminal_marker_seen"] and (
                    "Session completed via task_done" in formatted
                    or "[metrics] session_id=" in formatted
                ):
                    session_meta["terminal_marker_seen"] = True
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

    Returns the final exit_code (or 2 propagated unchanged when the agent
    reported a non-retryable error).
    """
    _session_features: list = []
    attempted = 0
    pushed = 0
    post_coder_pushed: list[int] = []

    # pf-verify-env.sh failure → env-not-ready. The agent never started; no
    # commits, no session_result.json, nothing to reconcile. Skip the whole
    # post-pipeline (post-coder, kill_recovery, rollback) and just record
    # the session as ended with notes + alert the operator. fix_attempts on
    # the assigned features stays untouched because the failure is
    # environmental, not the agent's fault.
    if exit_code == EXIT_ENV_NOT_READY:
        try:
            log_tail = ""
            try:
                tail = subprocess.run(
                    ["docker", "logs", "--tail", "5", f"pf-{product['id']}-{session_uid}"],
                    capture_output=True, text=True, timeout=10,
                )
                log_tail = (tail.stdout + tail.stderr).strip()
            except Exception:
                pass
            send_alert(
                "error",
                f"{product.get('name', '?')}: pf-verify-env failed (exit 42) — "
                f"agent never started; assigned features released without "
                f"fix_attempt charge. Last lines:\n{log_tail[-500:]}",
            )
        except Exception:
            log.exception("env-not-ready alert failed")
        if session_id is not None:
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    client.patch(f"/api/sessions/{session_id}", json={
                        "status":     "ended",
                        "ended_at":   datetime.now(timezone.utc).isoformat(),
                        "exit_code":  EXIT_ENV_NOT_READY,
                        "notes":      "env-not-ready (pf-verify-env exit 42)",
                    })
            except Exception as e:
                log.warning(f"Could not update env-not-ready session record: {e}")
        # Release the claim so the features can be re-picked after env fix,
        # without bumping fix_attempts (rollback resets agent-state features
        # to their prior status; it does not touch fix_attempts).
        _rollback_stuck_features(product["id"], persona)
        _cleanup_workspace_post_session(working_dir, product.get("name", str(working_dir)))
        return EXIT_ENV_NOT_READY

    # exit 43 = LLM-infra exhaustion (quota / auth / whole-chain 5xx). Parallel
    # to the env-not-ready path above: alert the operator with the failure
    # category, release the feature claim without charging fix_attempts, skip
    # the entire post-pipeline.
    if exit_code == EXIT_LLM_INFRA:
        category = "unknown"
        log_tail = ""
        try:
            tail = subprocess.run(
                ["docker", "logs", "--tail", "30", f"pf-{product['id']}-{session_uid}"],
                capture_output=True, text=True, timeout=10,
            )
            log_tail = (tail.stdout + tail.stderr).strip()
            # agent_loop.py logs `LLM infrastructure exhausted (category=X)`.
            m = re.search(r"category=([a-z_]+)", log_tail)
            if m:
                category = m.group(1)
        except Exception:
            pass
        try:
            send_alert(
                "error",
                f"{product.get('name', '?')}: LLM backend exhausted "
                f"(category={category}, exit 43) — agent could not reach a "
                f"usable model. Features released without fix_attempt charge. "
                f"Last lines:\n{log_tail[-500:]}",
            )
        except Exception:
            log.exception("llm-infra alert failed")
        if session_id is not None:
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    client.patch(f"/api/sessions/{session_id}", json={
                        "status":      "ended",
                        "ended_at":    datetime.now(timezone.utc).isoformat(),
                        "exit_code":   EXIT_LLM_INFRA,
                        "kill_reason": f"llm_infra_{category}",
                        "notes":       f"llm-infra exhausted (category={category})",
                    })
            except Exception as e:
                log.warning(f"Could not update llm-infra session record: {e}")
        _rollback_stuck_features(product["id"], persona)
        _cleanup_workspace_post_session(working_dir, product.get("name", str(working_dir)))
        return EXIT_LLM_INFRA

    # FSM transition: running → wrapping. The agent container has exited
    # (cleanly or otherwise); the orchestrator-side post-* pipeline is
    # about to run for 30s-2min. The watchdog reads `/api/sessions/active`
    # which filters on status in (pending, starting, running) — moving to
    # `wrapping` removes us from that set, so the watchdog stops trying to
    # kill the session on "container exited (docker ps does not list it)"
    # mid-finalize. The final PATCH in the finally block below transitions
    # wrapping → ended/lost/killed with the real end status + token totals.
    #
    # Best-effort: if the PATCH fails (PM API hiccup), proceed to post-*
    # anyway — the watchdog race only fires on rare timing, never blocks
    # progress.
    if session_id is not None:
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                client.patch(f"/api/sessions/{session_id}", json={
                    "status": "wrapping",
                })
        except Exception as e:
            log.warning(
                f"[{product.get('name', '?')}] session {session_id} "
                f"wrapping-state PATCH failed: {e} — proceeding to post-* "
                f"(watchdog may mis-fire during this window)"
            )

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
            # Auto-heal: coder exited 0 but post-coder pushed nothing.
            # Pauses the product, runs the diagnostic checklist, applies known
            # fixes, then either resumes (status=ready) or escalates with a
            # banner on the product list page. See supervisor docstring +
            # `auto_heal_unproductive_coder` for the recognised patterns.
            try:
                from orchestrator.supervisor import auto_heal_unproductive_coder
                auto_heal_unproductive_coder(
                    product=product,
                    session_uid=session_uid,
                    exit_code=exit_code,
                    post_coder_pushed=post_coder_pushed,
                    assigned_features=product.get("_assigned_features", []),
                )
            except Exception:
                log.exception(f"auto_heal_unproductive_coder failed for {product.get('name')}")
        elif exit_code == 0 and persona == "designer":
            try:
                _run_post_doc_pipeline(product, session_uid, working_dir,
                                       product.get("_assigned_features", []),
                                       persona)
            except Exception:
                log.exception(f"Post-{persona} pipeline failed for {product.get('name')}")
        elif exit_code == 0 and persona in _MAINTENANCE_PERSONAS:
            try:
                _run_post_maintenance_pipeline(product, session_uid, working_dir, persona)
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

        # 2b. Detect repeated reviewer feedback (auto-block dead-end loops).
        # Runs ONLY for reviewer sessions, before reconcile flips the
        # feature to Implementing. The detector hashes the reviewer's
        # `❌ <section>:` comments and blocks the feature when the same
        # fingerprint repeats N consecutive cycles — catching loops earlier
        # than the fix_attempts=5 cap (~3-4 hours saved on Ollama Cloud).
        # Best-effort — never raises.
        if exit_code == 0 and persona == "reviewer":
            try:
                from orchestrator.supervisor import (
                    detect_repeated_review_feedback,
                    detect_divergent_review_feedback,
                    detect_reviewer_outcome_text_mismatch,
                )
                for _entry in _session_features:
                    if (isinstance(_entry, dict)
                            and _entry.get("review_outcome") == "changes_requested"
                            and _entry.get("id")):
                        # Cycle JV (2026-06-03): defensive alert when the
                        # reviewer's comment text disagrees with the
                        # structured review_outcome (LGTM body + structured
                        # changes_requested). Alert-only, no override —
                        # see detect_reviewer_outcome_text_mismatch
                        # docstring. Canonical case: cycle JR #1131
                        # cap-Blocked despite ✅ LGTM commit. Best-effort.
                        try:
                            detect_reviewer_outcome_text_mismatch(
                                feature_id=_entry["id"],
                                product_id=product["id"],
                                review_outcome=_entry.get("review_outcome"),
                            )
                        except Exception:
                            log.exception(
                                f"detect_reviewer_outcome_text_mismatch "
                                f"failed for {product.get('name')} feature "
                                f"#{_entry['id']}"
                            )
                        # Convergent cascade: same feedback N× in a row.
                        _result = detect_repeated_review_feedback(
                            feature_id=_entry["id"],
                            product_id=product["id"],
                            review_notes=_entry.get("review_notes"),
                        )
                        if _result.get("action") == "blocked":
                            _entry["status"] = "Blocked"
                            _entry["pr_number"] = None
                            _entry["review_outcome"] = "changes_requested"
                            continue   # already blocked; skip the divergent check
                        # Divergent cascade: different feedback each round.
                        # Phase 6 of quality-specs (2026-05-19). Same end-state
                        # (route to Blocked sprint) but a different signal:
                        # max pairwise Jaccard similarity across the last N
                        # reviewer comments is below threshold (default 0.25).
                        # StockAnalysis feature 594's cascade was the canonical
                        # case — 4 rounds, 4 different findings, the convergent
                        # detector never fired.
                        try:
                            _divergent = detect_divergent_review_feedback(
                                feature_id=_entry["id"],
                                product_id=product["id"],
                            )
                            if _divergent.get("action") == "blocked":
                                _entry["status"] = "Blocked"
                                _entry["pr_number"] = None
                                _entry["review_outcome"] = "changes_requested"
                        except Exception:
                            log.exception(
                                f"Supervisor detect_divergent_review_feedback "
                                f"failed for {product.get('name')} feature #{_entry['id']}"
                            )
            except Exception:
                log.exception(
                    f"Supervisor detect_repeated_review_feedback failed "
                    f"for {product.get('name')}"
                )

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
                # FSM transition — see _select_end_status for the 3-way logic.
                # The PATCH endpoint (website/main.py:api_end_session) refuses
                # status downgrades from terminal states (killed/orphaned), so
                # a racing watchdog that already set killed wins over this call.
                end_status = _select_end_status(
                    exit_code=exit_code,
                    terminal_marker_seen=bool(session_meta.get("terminal_marker_seen")),
                )
                if end_status == "lost":
                    log.warning(
                        f"[{product.get('name')}] session {session_id} exit=0 "
                        f"but agent never emitted task_done / [metrics] — "
                        f"recording as `lost` (container ended without the "
                        f"agent finalizing)."
                    )
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
                    # an earlier reconcile already set). `terminal_marker_seen`
                    # is an internal flag and must not be PATCHed.
                    patch_body.update({
                        k: v for k, v in session_meta.items()
                        if v is not None and k != "terminal_marker_seen"
                    })
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
    # Reviewer is read-only and bounded: review N feature commits, post comments,
    # exit. Healthy reviewer sessions complete in <30 turns; the global 80-200
    # turn cap is for the coder/designer who actually iterate. Without this cap
    # reviewers occasionally wander for 60+ minutes doing repeat find/grep
    # variants on the codebase (session 2154 / d3f200e2, 2026-05-10: 63 min,
    # turn 75/200, repeated `find /workspace/src -name "*.css"` searches). The
    # reverted-once theory that this cap caused the 10s JSON-in-content fail in
    # session 2145 was disproved — sessions 2150/2152 worked fine at the default
    # 200 with the cap removed; the 2145 failure was just model variance.
    if persona == "reviewer":
        effective_max_turns = min(effective_max_turns, 200)
    # Designer cap parity with reviewer at 200 (raised 2026-05-14 from 40).
    # The 40 cap defended against Ollama 400 "prompt too long" context overflow
    # observed on session 2277 (28 turns, 245k input tokens). That defence is
    # now downstream — agent_loop's hallucination guard + the nudge counter
    # bound the most pathological burn paths, and per-feature design sessions
    # genuinely need more than 40 turns when iterating on rework feedback.
    # Re-validate if Ollama context-overflow 400s reappear on designers.
    if persona == "designer":
        effective_max_turns = min(effective_max_turns, 200)
    effective_bash_timeout   = int(sys_cfg.get("bash_timeout")   or os.environ.get("BASH_TIMEOUT",   "180"))

    # Enrich product dict with all computed values before building the prompt.
    product = dict(product)
    product["_auto_merge_enabled"] = bool(sys_cfg.get("auto_merge_enabled", False))

    # Poller-driven feature assignment: pre-fetch and claim features before launch.
    # Agent receives an explicit task list — no self-discovery inside the container.
    assigned_features, active_sprint_name, _ = _fetch_assigned_features(product["id"], persona, effective_max_features)
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
    # Phase 7 of quality-specs (2026-05-19): pre-coder context augmentation.
    # Read the product's ARCHITECTURE.md + scan the area's source dir for
    # existing modules so the coder prompt can render a "use these, don't
    # parallel them" block before any code is written. Coder only — other
    # personas don't need this scaffolding. Best-effort: any failure → empty
    # block, never blocks session launch.
    product["_related_existing_code_md"] = ""
    if persona == "coder" and assigned_features:
        try:
            from orchestrator.session.context_builder import build_related_code_context
            _arch_md = ""
            try:
                from pathlib import Path as _PArch
                _arch_path = _PArch(product.get("working_dir", "")) / "ARCHITECTURE.md"
                if _arch_path.exists():
                    _arch_md = _arch_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
            product["_related_existing_code_md"] = build_related_code_context(
                product.get("working_dir", ""),
                assigned_features[0],
                architecture_md=_arch_md,
            )
        except Exception:
            log.debug("Phase 7 pre-coder context build failed (non-fatal)", exc_info=True)
    # Session-PR context (1-PR model). Reviewer assignments are already
    # grouped by `pr_number` in _fetch_assigned_features, so the set of
    # session pr_numbers across assigned_features should be singleton. The
    # session branch comes from the feature row's `branch_name` (set by
    # post_coder when the session PR was opened). Coder/designer don't
    # need session context here — coder cuts its own fresh session branch
    # in post_coder; designer commits docs straight to main.
    _session_pr_set = {
        f.get("pr_number") for f in assigned_features
        if isinstance(f.get("pr_number"), int)
    }
    _session_branch_set = {
        f.get("branch_name") for f in assigned_features
        if f.get("branch_name")
    }
    if persona == "reviewer" and len(_session_pr_set) == 1:
        product["_session_pr_number"] = next(iter(_session_pr_set))
        product["_session_branch"] = (
            next(iter(_session_branch_set)) if len(_session_branch_set) == 1 else ""
        )
        product["_session_pr_url"] = next(
            (f.get("pr_url", "") for f in assigned_features if f.get("pr_url")), ""
        )
    else:
        product["_session_pr_number"] = None
        product["_session_branch"] = ""
        product["_session_pr_url"] = ""

    # Reviewer pre-checkout: land the agent on the session branch under
    # review so its first `git log`/`git show` runs against the right
    # tree. Designer always stays on whatever branch `_reset_workspace`
    # left them on (main/master) — designer commits docs straight to main.
    # Coder: fresh first-pass stays on main and post_coder cuts the
    # session branch itself; rework lands on the previous coder branch
    # so the prior implementation persists — paired with the last-bounce
    # feedback filter in _fetch_recent_review_comments, the coder
    # patches in place against existing code instead of reimplementing.
    if persona == "reviewer" and product.get("_session_branch"):
        _checkout_branch(
            working_dir,
            product["_session_branch"],
            product.get("name", str(working_dir)),
        )
        # _prepare_workspace already ran chmod a+rwX, but the checkout
        # above re-creates files at the orchestrator's umask (typically
        # 022 → mode 644). Owned by uid 999 inside the agent container,
        # mode 644 means the agent (uid 1001 = "other") gets read-only
        # on tracked files like .eslintrc.cjs, package.json, etc. Re-run
        # chmod AFTER the checkout. (Reviewer is read-only on workspace,
        # so this is defensive — kept symmetric with the prior code path.)
        _chmod_workspace_via_alpine(working_dir, product.get("name", "?"))
    elif persona == "coder" and assigned_features:
        rework_branch = _find_rework_branch(
            assigned_features, product, product.get("name", "?"))
        if rework_branch:
            log.info(
                f"[{product.get('name','?')}] coder rework pre-checkout: "
                f"landing on {rework_branch!r} to preserve prior "
                f"implementation"
            )
            ok = _checkout_branch(
                working_dir, rework_branch,
                product.get("name", str(working_dir)),
            )
            if ok:
                # Same chmod-after-checkout rationale as the reviewer
                # block above. The coder will write files (uid 1001),
                # so it MUST have write access — without this re-chmod
                # the first `write_file` would EACCES.
                _chmod_workspace_via_alpine(
                    working_dir, product.get("name", "?"))

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

    # SSH deploy-key mount removed: git auth is now a GitHub App installation
    # token delivered via the GITHUB_TOKEN env var + HTTPS origin (see
    # orchestrator/integrations/git_ops.py:_enforce_https_origin). The agent
    # container needs nothing in ~/.ssh besides the known_hosts already baked
    # into the image. Variable kept for downstream concatenation only.
    ssh_mount: list[str] = []

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
        # Per-file RO overlays on PM-curated files. Kernel-level enforcement
        # against agent writes — `echo > /workspace/CLAUDE.md` returns EROFS
        # instead of succeeding-then-getting-stripped at commit time.
        # ARCHITECTURE.md stays RW for architect, RO for everyone else.
        *_build_pm_curated_ro_mounts(working_dir, working_dir_host, persona),
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
