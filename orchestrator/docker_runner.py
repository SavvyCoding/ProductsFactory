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


def _rollback_stuck_features(product_id: int, persona: str | None) -> None:
    """
    Roll back features that were claimed by a session that never completed.
    Only resets features with NO evidence of completion (no PR, not in session_result.json).
    Features that have pr_number set are left alone — they're already in Reviewing.

    Each feature is patched individually so a single API failure does not block the rest.
    """
    stuck_statuses = {
        "designer":         ["Designing"],
        "product_planner":  ["Designing"],
        "coder":            ["Implementing"],
        "reviewer":         [],
    }
    rollback_from = stuck_statuses.get(persona or "", ["Designing", "Implementing"])
    if not rollback_from:
        return
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get(f"/api/products/{product_id}/features")
            resp.raise_for_status()
            feats = resp.json()
            if not isinstance(feats, list):
                log.warning(f"Unexpected response fetching features for product {product_id}")
                return
            rolled_back = 0
            for f in feats:
                if f["status"] in rollback_from and not f.get("pr_number"):
                    try:
                        # Preserve Designed state if design doc exists
                        reset_to = "Designed" if f.get("design_doc_path") else "Approved"
                        client.patch(f"/api/features/{f['id']}", json={"status": reset_to})
                        log.info(f"Rolled back feature #{f['id']} '{f['name']}' {f['status']} -> {reset_to}")
                        rolled_back += 1
                    except Exception as fe:
                        log.warning(f"Could not roll back feature #{f['id']}: {fe}")
            if rolled_back:
                log.info(f"Rolled back {rolled_back} feature(s) for product {product_id}")
    except Exception as e:
        log.warning(f"Could not rollback stuck features for product {product_id}: {e}")


def _read_session_result(working_dir: str) -> list[dict]:
    """
    Read and parse session_result.json (newline-delimited JSON — one entry per line).
    Returns list of feature dicts. Skips blank or malformed lines.

    Also handles the wrapped format where an agent writes a single JSON object with a
    top-level "features" array instead of one object per line, e.g.:
        {"features": [{"id": 42, "status": "Reviewed", ...}, ...]}
    Such entries are unpacked into individual feature dicts.
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
                obj = _json.loads(line)
                # Bare JSON array written by some agent versions: [{"id":42,...}, ...]
                if isinstance(obj, list):
                    log.warning(f"[session_result] Unwrapping bare JSON array ({len(obj)} entries)")
                    entries.extend(e for e in obj if isinstance(e, dict))
                # Wrapped {"features": [...]} format
                elif isinstance(obj, dict) and "features" in obj and isinstance(obj["features"], list) and "id" not in obj:
                    log.warning(f"[session_result] Unwrapping nested 'features' array ({len(obj['features'])} entries)")
                    entries.extend(e for e in obj["features"] if isinstance(e, dict))
                elif isinstance(obj, dict):
                    entries.append(obj)
                # else: skip non-dict, non-list top-level values
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


_VALID_FEATURE_STATUSES = frozenset({
    "Pending", "Approved",
    "Designing", "Designed",
    "Implementing", "Implemented",
    "Reviewing", "Reviewed",
    "Testing", "Committed", "Pushed",
    "Blocked", "Rejected", "Reverted", "Deferred",
})


def _apply_session_entry(client: httpx.Client, entry: dict) -> bool:
    """PATCH a single session_result entry to the PM API. Returns True on success."""
    fid = entry.get("id")
    if not fid:
        log.warning(f"[progress] Skipping session_result entry with no feature id: {entry}")
        return False
    # Guard: reject unknown status values before they hit the DB constraint
    status = entry.get("status")
    if status is not None and status not in _VALID_FEATURE_STATUSES:
        log.warning(f"[progress] Skipping feature #{fid} entry with unknown status '{status}' — agent bug?")
        return False
    # Contract: Reviewing entries MUST carry pr_number (from the field or
    # embedded in pr_url). Without it the feature gets stuck (auto-merge has
    # nothing to merge). Previously we warned and let it through; now we
    # REJECT and bump fix_attempts so repeated violations auto-Block.
    if entry.get("status") == "Reviewing" and not entry.get("pr_number"):
        pr_url = entry.get("pr_url", "")
        import re as _re
        m = _re.search(r"/pull/(\d+)", pr_url)
        if m:
            entry = dict(entry, pr_number=int(m.group(1)))
            log.info(f"[progress] Feature #{fid}: extracted pr_number={entry['pr_number']} from pr_url")
        else:
            log.warning(f"[progress] REJECTING feature #{fid} — Reviewing without pr_number (agent contract violation)")
            try:
                cur = client.get(f"/api/features/{fid}").json()
                attempts = int(cur.get("fix_attempts") or 0) + 1
                patch = {"fix_attempts": attempts}
                if attempts >= 5:
                    patch.update({"status": "Blocked",
                                  "blocked_reason": "Agent kept marking Reviewing without a real PR number"})
                client.patch(f"/api/features/{fid}", json=patch)
            except Exception:
                pass
            return False

    # Contract: Reviewed entries MUST carry review_outcome. Without it the
    # auto-merge path can't decide whether to merge.
    if entry.get("status") == "Reviewed" and not entry.get("review_outcome"):
        log.warning(f"[progress] REJECTING feature #{fid} — Reviewed without review_outcome")
        return False
    # Guard: never downgrade a feature's status — except for review-driven
    # backward transitions, which are part of the normal pipeline:
    #   - Reviewing → Implementing  (reviewer requested changes; coder reworks)
    #   - Reviewed  → Implementing  (post-approval issue caught; coder reworks)
    # Without this allowlist the reviewer's "changes_requested" PATCH gets
    # silently dropped because Implementing(4) < Reviewing(5), and the feature
    # sits in Reviewing forever — `next-for-persona` keeps handing it back to
    # the reviewer, producing an infinite review loop on the same PR.
    _PROGRESS_RANK = {
        "Pending": 0, "Approved": 1, "Designing": 2, "Designed": 3,
        "Implementing": 4, "Reviewing": 5, "Reviewed": 6, "Pushed": 7,
        "Blocked": 2, "Deferred": 7, "Rejected": 7, "Reverted": 0,
    }
    _ALLOWED_BACKWARD = {("Reviewing", "Implementing"), ("Reviewed", "Implementing")}
    if status:
        try:
            current_resp = client.get(f"/api/features/{fid}")
            if current_resp.status_code == 200:
                current_status = current_resp.json().get("status", "")
                if (
                    _PROGRESS_RANK.get(current_status, 0) > _PROGRESS_RANK.get(status, 0)
                    and (current_status, status) not in _ALLOWED_BACKWARD
                ):
                    log.debug(f"[progress] Feature #{fid}: skipping downgrade {current_status} → {status}")
                    return False
        except Exception:
            pass  # proceed with update if check fails

    patch_body = {k: v for k, v in entry.items() if k not in ("id", "confidence")}
    try:
        resp = client.patch(f"/api/features/{fid}", json=patch_body)
        resp.raise_for_status()
        log.info(f"[progress] Feature #{fid} -> {patch_body.get('status', '?')}")
        return True
    except httpx.HTTPStatusError as e:
        log.warning(
            f"[progress] PM API rejected feature #{fid} update "
            f"({e.response.status_code}): {e.response.text[:200]}"
        )
        return False
    except Exception as e:
        log.warning(f"[progress] Could not update feature #{fid}: {e}")
        return False


def _live_poll_session_result(working_dir: str, stop_event: threading.Event, persona: str = "") -> None:
    """
    Background thread: polls session_result.json every 30 s while the container runs.
    Applies new NDJSON lines to the DB in real-time as the agent writes phase transitions.
    Tracks applied lines by index so each entry is applied exactly once.

    On shutdown (stop_event set) performs a final drain pass so any entries written
    between the last tick and container exit are not silently lost.
    """
    import json as _json
    result_file = Path(working_dir) / "session_result.json"
    applied_up_to = 0  # number of lines already applied this session

    def _drain_new(label: str) -> None:
        nonlocal applied_up_to
        if not result_file.exists():
            return
        try:
            lines = result_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        new_lines = lines[applied_up_to:]
        if not new_lines:
            return
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        applied_up_to += 1
                        continue
                    try:
                        entry = _json.loads(line)
                        # Reviewer must not set Reviewing (pre-claim artifact).
                        if persona == "reviewer" and entry.get("status") == "Reviewing":
                            log.debug(f"[{label}] Skipping reviewer Reviewing entry for feature #{entry.get('id')}")
                        # Coder/reviewer must not write Pushed — PRs must go through GitHub merge.
                        elif persona in ("coder", "reviewer") and entry.get("status") == "Pushed":
                            log.warning(f"[{label}] Blocked agent-written Pushed for feature #{entry.get('id')} (persona={persona}) — PRs must merge via GitHub")
                        else:
                            _apply_session_entry(client, entry)
                    except Exception:
                        pass  # malformed line — skip, don't block the rest
                    applied_up_to += 1
        except Exception as e:
            log.debug(f"[{label}] PM API error: {e}")

    while not stop_event.wait(30):  # poll every 30 s; exits when stop_event is set
        _drain_new("live-poll")

    # Final drain — catches entries written between the last tick and stop_event.
    # Reconcile will re-apply these idempotently but running it here ensures the
    # DB reaches a consistent state even if reconcile is short-circuited by an
    # exception, and gives the user faster feedback in the UI.
    _drain_new("live-poll-final")


def _reconcile_session_result(working_dir: str, product_id: int, exit_code: int,
                               features: list[dict] | None = None, persona: str = "") -> None:
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

    # Reviewer must not set Reviewing; coder/reviewer must not write Pushed directly.
    if persona in ("coder", "reviewer"):
        before = len(features)
        def _is_blocked(e: dict) -> bool:
            s = e.get("status")
            if persona == "reviewer" and s == "Reviewing":
                return True
            if s == "Pushed":
                return True
            return False
        features = [e for e in features if not _is_blocked(e)]
        skipped = before - len(features)
        if skipped:
            log.info(f"[reconcile] Filtered out {skipped} disallowed entries for persona={persona} (Pushed or reviewer-Reviewing)")

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
    For reviewer sessions with auto_merge_enabled: merge every approved feature.
    - approved: attempt GitHub merge → update entry to Pushed
    - PR closed/conflicted: close PR, update entry to Implementing (clears pr_number)

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
        if not isinstance(entry, dict) or entry.get("review_outcome") != "approved":
            continue

        fid = entry.get("id")
        pr_number = entry.get("pr_number")

        if not pr_number:
            # Fetch pr_number from DB if not in session entry
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    resp = client.get(f"/api/features/{fid}")
                    resp.raise_for_status()
                    feat = resp.json()
                    pr_number = feat.get("pr_number")
                    # Fallback: parse from pr_url if pr_number column is null
                    if not pr_number and feat.get("pr_url"):
                        import re as _re
                        m = _re.search(r"/pull/(\d+)", feat["pr_url"])
                        if m:
                            pr_number = int(m.group(1))
                            log.info(f"[auto-merge] Feature #{fid}: resolved pr_number={pr_number} from pr_url")
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

        # PR is open — attempt merge regardless of reviewer-reported confidence.
        # First, try to update the PR branch with main (GitHub's "Update branch"
        # button, REST endpoint /update-branch). If the PR branch is behind main
        # or has conflicts, this rebases/merges main into the branch so the
        # subsequent merge PUT succeeds. 422 = already up-to-date (ok to ignore).
        try:
            upd = httpx.put(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}/update-branch",
                headers=gh_headers, timeout=15,
            )
            if upd.status_code in (202, 200):
                log.info(f"[auto-merge] PR #{pr_number} branch updated — waiting 5s for GitHub to recompute mergeability")
                import time as _t
                _t.sleep(5)
            elif upd.status_code == 422:
                log.info(f"[auto-merge] PR #{pr_number} already up-to-date with base")
            else:
                log.warning(f"[auto-merge] update-branch returned {upd.status_code} for PR #{pr_number}: {upd.text[:120]}")
        except Exception as _ue:
            log.warning(f"[auto-merge] update-branch failed for PR #{pr_number}: {_ue} — proceeding to merge anyway")

        log.info(f"[auto-merge] Merging PR #{pr_number} for feature #{fid}")
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
                if "application/json" in resp.headers.get("content-type", ""):
                    body = resp.json()
                    gh_msg = body.get("message", resp.text[:120]) if isinstance(body, dict) else resp.text[:120]
                else:
                    gh_msg = resp.text[:120]
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


import re as _re_secrets

# Patterns for credentials that must never appear in logs. Anything matching
# is replaced with ***REDACTED*** before lines are sent to docker stdout or
# POSTed to the PM API session log buffer. List grows as new auth schemes are
# discovered in the wild — over-redaction is fine; under-redaction is not.
_SECRET_PATTERNS = [
    _re_secrets.compile(r'gh[psoua]_[A-Za-z0-9]{20,}'),                                    # GitHub classic + variants
    _re_secrets.compile(r'github_pat_[A-Za-z0-9_]{20,}'),                                  # GitHub fine-grained
    _re_secrets.compile(r'sk-ant-(?:oat|ort|api|admin)[A-Za-z0-9_\-]{20,}'),               # Anthropic
    _re_secrets.compile(r'sk-[A-Za-z0-9]{20,}'),                                           # Generic OpenAI-shape
    _re_secrets.compile(r'AKIA[A-Z0-9]{16}'),                                              # AWS access key id
    _re_secrets.compile(r'xox[bpasr]-[A-Za-z0-9-]+'),                                      # Slack tokens
    _re_secrets.compile(r'(Bearer\s+)[A-Za-z0-9_.\-=]{12,}', _re_secrets.IGNORECASE),       # HTTP Bearer
]


def _redact_secrets(s: str) -> str:
    """Strip credential-shaped substrings before logging."""
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


def _run_post_coder_pipeline(product: dict, session_uid: str, working_dir: str,
                              assigned_features: list[dict]) -> None:
    """
    Deterministic git+gh pipeline run after the coder LLM exits cleanly.
    The coder ONLY writes code; this function does the ceremony:
      1. Detect if there are any changes in the workspace
      2. Create branch coder/{session_uid}
      3. git add + commit + push
      4. gh pr create
      5. Append one Reviewing entry per assigned feature to session_result.json
         (the existing reconcile loop will then PATCH each feature → Reviewing
         with the same pr_number, and the auto-merge logic later picks it up)

    Single PR for all assigned features in this session — simpler than per-feature
    branches and matches how human devs typically batch related changes.
    """
    import subprocess as _sp
    import json as _json
    import re as _re

    pname = product.get("name", "?")
    if not assigned_features:
        log.info(f"[post-coder] {pname}: no assigned features — skipping commit/PR")
        return

    # Symphony-style: this pipeline is a *fallback* now. Strong models open
    # their own PRs and write Reviewing entries to session_result.json. If
    # every assigned feature already has a Reviewing entry with pr_number,
    # the agent did the work — skip the deterministic ceremony.
    try:
        sr_path = Path(working_dir) / "session_result.json"
        already_handled: set[int] = set()
        if sr_path.exists():
            for ln in sr_path.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    e = _json.loads(ln)
                except Exception:
                    continue
                if (isinstance(e, dict) and e.get("status") == "Reviewing"
                        and isinstance(e.get("pr_number"), int)
                        and isinstance(e.get("id"), int)):
                    already_handled.add(e["id"])
        assigned_ids = {f["id"] for f in assigned_features}
        if assigned_ids and assigned_ids.issubset(already_handled):
            log.info(
                f"[post-coder] {pname}: agent opened PRs for all "
                f"{len(assigned_ids)} assigned features — skipping fallback pipeline"
            )
            return
        if already_handled & assigned_ids:
            # Partial coverage. Narrow `assigned_features` to the unhandled
            # subset so the downstream loops in this function (the no-diff
            # Blocked-flip at lines ~755 and the session_result.json
            # writeback near the end) don't touch features the agent already
            # finished. Without this filter, two corruption paths fire:
            #   (a) empty git diff → every assigned feature gets PATCHed to
            #       Blocked, demoting already-Reviewing features.
            #   (b) pipeline opens its own fresh PR → writeback overwrites
            #       the agent's correct pr_number with the new one.
            # Both bypass the rank guard in _apply_session_entry.
            log.info(
                f"[post-coder] {pname}: agent handled {sorted(already_handled & assigned_ids)}; "
                f"running fallback for {sorted(assigned_ids - already_handled)}"
            )
            assigned_features = [f for f in assigned_features if f["id"] not in already_handled]
            if not assigned_features:
                # Filter consumed everything — handled features already
                # taken care of by _apply_session_entry, no fallback work
                # to do. (Defensive: the issubset check above should have
                # caught this, but rely on it here too in case the set
                # math races with a concurrent live-poll application.)
                log.info(f"[post-coder] {pname}: all features handled by agent — skipping fallback pipeline")
                return
    except Exception:
        log.exception(f"[post-coder] {pname}: agent-handled detection failed — running pipeline")

    def _run(cmd: list[str], **kw) -> _sp.CompletedProcess:
        # Pop timeout from kw so the caller's override doesn't collide with the
        # default we pass into _sp.run. Without this, e.g. _run(..., timeout=180)
        # raises TypeError("got multiple values for keyword argument 'timeout'")
        # — which crashes the whole pipeline before our diagnostic checks run.
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout, **kw)

    # 1. Detect changes — anything uncommitted in the tree, OR committed-but-
    # not-pushed (the agent may have committed itself; we still need to push).
    status = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()
               and not ln.endswith("session_result.json")
               and not ln.endswith("session_summary.md")
               and "/Temp/" not in ln and "/Results/" not in ln]
    has_unpushed_commits = False
    for ref in ("@{u}", "origin/main", "origin/master"):
        ahead = _run(["git", "rev-list", "--count", f"{ref}..HEAD"])
        if ahead.returncode == 0:
            try:
                has_unpushed_commits = int((ahead.stdout or "0").strip()) > 0
            except ValueError:
                pass
            break
    if not changed and not has_unpushed_commits:
        log.warning(f"[post-coder] {pname}: agent exited 0 but no code changes or unpushed commits — skipping PR")
        # Mark features Blocked so they don't loop in Implementing forever
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for f in assigned_features:
                    client.patch(f"/api/features/{f['id']}", json={
                        "status": "Blocked",
                        "blocked_reason": f"Coder session {session_uid} exited 0 with no code changes",
                    })
        except Exception:
            pass
        return
    log.info(f"[post-coder] {pname}: {len(changed)} changed file(s) detected — sample: {changed[:3]}")

    def _fmt_err(r) -> str:
        """Render a CompletedProcess for diagnostic logging — git often prints
        useful info on stdout, not stderr (e.g. 'nothing to commit')."""
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return f"rc={r.returncode} stdout={out[:300]!r} stderr={err[:300]!r}"

    # 2. Resolve target branch.
    # Sprint-PR mode: every coder run pushes to the same sprint/<id> branch so
    # there's exactly one PR per sprint (no fan-out, no orphan PRs). The branch
    # and PR were provisioned by the website's _maybe_provision_sprint_pr at
    # sprint activation; we just check it out and push commits.
    # Per-feature mode (default): cut a fresh `coder/<session_uid>` branch and
    # later open a new PR for it.
    feat_ids = [f["id"] for f in assigned_features]
    sprint_pr_mode = bool(product.get("_sprint_pr_mode"))
    sprint_branch  = product.get("_sprint_branch") or ""
    sprint_pr_num  = product.get("_sprint_pr_number") or None
    sprint_pr_url  = product.get("_sprint_pr_url") or ""
    if sprint_pr_mode:
        if not sprint_branch or not sprint_pr_num:
            log.warning(
                f"[post-coder] {pname}: sprint_pr_mode set but sprint metadata missing "
                f"(branch={sprint_branch!r} pr={sprint_pr_num!r}) — falling back to per-feature mode"
            )
            sprint_pr_mode = False
    if sprint_pr_mode:
        branch = sprint_branch
        # Fetch first so we have the remote state, then check out (branch exists
        # remotely from sprint provisioning). Pull --ff-only catches the case
        # where another coder run already pushed and we'd otherwise diverge.
        _run(["git", "fetch", "origin"])
        co = _run(["git", "checkout", branch])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: git checkout {branch} failed — {_fmt_err(co)}")
            return
        pull_r = _run(["git", "pull", "--ff-only", "origin", branch])
        if pull_r.returncode != 0:
            log.warning(f"[post-coder] {pname}: git pull --ff-only on {branch} failed — {_fmt_err(pull_r)}")
            # Don't return: a non-fast-forward state is rare and we still want
            # to attempt the push so the operator sees the conflict.
    else:
        branch = f"coder/{session_uid}"
        co = _run(["git", "checkout", "-b", branch])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: git checkout -b {branch} failed — {_fmt_err(co)}")
            return

    # 3. Add + commit + push.
    # Three states the working tree can be in at this point:
    #   (a) Uncommitted changes present  → add, sanity-check stage, commit
    #   (b) Clean tree, unpushed commits → agent already committed; skip to push
    #   (c) Clean tree, no unpushed cmts → caught by the early-return above
    feat_summary = ", ".join(f"#{i}" for i in feat_ids)
    if changed:
        add_r = _run(["git", "add", "-A"])
        if add_r.returncode != 0:
            log.warning(f"[post-coder] {pname}: git add -A failed — {_fmt_err(add_r)}")
            return
        # Sanity: was anything actually staged? `diff --cached --quiet` exits 1
        # if there are staged changes, 0 if none. Catches the "porcelain showed
        # lines but add staged nothing" scenario (e.g. all changes inside a
        # submodule or excluded path) so we surface a clear error instead of
        # an empty stderr.
        cached = _run(["git", "diff", "--cached", "--quiet"])
        if cached.returncode == 0:
            ls = _run(["git", "status", "--porcelain"])
            log.warning(
                f"[post-coder] {pname}: nothing staged after `git add -A` "
                f"despite {len(changed)} porcelain entries. status={ls.stdout.strip()[:400]!r}"
            )
            return
        commit_msg = f"feat: implement features {feat_summary} [coder-{session_uid}]"
        commit_result = _run(["git", "commit", "-m", commit_msg])
        if commit_result.returncode != 0:
            log.warning(f"[post-coder] {pname}: git commit failed — {_fmt_err(commit_result)}")
            return
    else:
        log.info(
            f"[post-coder] {pname}: tree clean but {has_unpushed_commits and 'unpushed commits exist'} "
            f"— skipping add/commit, going straight to push"
        )

    if sprint_pr_mode:
        push_args = ["git", "push", "origin", branch]
    else:
        push_args = ["git", "push", "-u", "origin", branch]
    push_result = _run(push_args, timeout=180)
    if push_result.returncode != 0:
        log.warning(f"[post-coder] {pname}: git push failed: {push_result.stderr.strip()[:300]}")
        return
    log.info(f"[post-coder] {pname}: pushed branch {branch}")

    # 4. PR resolution. In sprint mode the PR already exists — just reuse it.
    # In per-feature mode, open a fresh PR via gh CLI.
    if sprint_pr_mode:
        pr_number = int(sprint_pr_num)
        pr_url = sprint_pr_url
        log.info(f"[post-coder] {pname}: reusing sprint PR #{pr_number} — {pr_url}")
    else:
        gh_token = _get_gh_token()
        if not gh_token:
            log.warning(f"[post-coder] {pname}: no GH_TOKEN — cannot open PR. Branch pushed; PM must open manually.")
            return

        # Build PR body — include feature names so PM can review at a glance
        feat_lines = []
        for f in assigned_features:
            name = f.get("name", f"Feature {f['id']}")
            feat_lines.append(f"- Closes #{f['id']}: {name}")
        pr_body = (
            f"Automated PR from coder session `{session_uid}`.\n\n"
            f"## Features\n" + "\n".join(feat_lines) + "\n\n"
            f"_This PR was generated by the ProductFactory coder agent. The agent "
            f"writes code; the orchestrator handles git + PR ceremony deterministically._"
        )
        pr_title = (f"feat: {assigned_features[0].get('name', 'changes')}"
                    if len(assigned_features) == 1
                    else f"feat: implement {feat_summary} [{session_uid}]")

        pr_env = dict(os.environ)
        pr_env["GH_TOKEN"] = gh_token
        pr_result = _sp.run(
            ["gh", "pr", "create", "--base", "main", "--head", branch,
             "--title", pr_title, "--body", pr_body],
            cwd=working_dir, capture_output=True, text=True, env=pr_env, timeout=60,
        )
        if pr_result.returncode != 0:
            log.warning(f"[post-coder] {pname}: gh pr create failed: {pr_result.stderr.strip()[:300]}")
            return

        # gh prints the PR URL on stdout
        pr_url = pr_result.stdout.strip().splitlines()[-1]
        m = _re.search(r"/pull/(\d+)", pr_url)
        if not m:
            log.warning(f"[post-coder] {pname}: could not parse PR number from gh output: {pr_url[:200]}")
            return
        pr_number = int(m.group(1))
        log.info(f"[post-coder] {pname}: opened PR #{pr_number} — {pr_url}")

    # 5. Append session_result.json entries — one Reviewing per assigned feature.
    sr_path = Path(working_dir) / "session_result.json"
    try:
        with sr_path.open("a", encoding="utf-8") as f:
            for feat in assigned_features:
                f.write(_json.dumps({
                    "id":         feat["id"],
                    "status":     "Reviewing",
                    "pr_number":  pr_number,
                    "pr_url":     pr_url,
                }) + "\n")
        log.info(f"[post-coder] {pname}: wrote {len(assigned_features)} entries to session_result.json")
    except Exception as e:
        log.warning(f"[post-coder] {pname}: failed to append session_result.json: {e}")


def _get_gh_token() -> str | None:
    """Fetch GitHub PAT from system config for GH_TOKEN injection into agent containers."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            resp.raise_for_status()
            if "application/json" not in resp.headers.get("content-type", ""):
                return None
            data = resp.json()
            return data.get("github_pat") or None if isinstance(data, dict) else None
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
    if persona not in ("coder", "designer", "product_planner", "reviewer"):
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
            elif persona in ("designer", "product_planner"):
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
_HEAVY_PERSONAS = {"coder", "reviewer", "designer", "security_auditor", "qa_tester"}
_LIGHT_PERSONAS = {"planner", "product_planner", "documenter", "retrospective",
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


def _reset_workspace(working_dir: str, product_name: str) -> None:
    """
    Sync the product workspace to the latest state on origin/main before each session.
    Uses git fetch + reset --hard (not pull --ff-only) so it succeeds even if local
    has diverged from remote (e.g. partial commits from a crashed previous session).
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return  # Not a git repo yet — skip

    # Fix workspace + .git/log permissions so Hermes (uid 999) can reset files that
    # agent containers (uid 1001) created.  p.chmod() from uid 999 fails on alien-owned
    # files, so we run a throwaway alpine container as root via the Docker socket.
    try:
        host_base = os.environ.get("PRODUCTS_BASE_DIR", "").rstrip("/\\")
        rel = str(wd.relative_to("/products"))
        host_wd_path = f"{host_base}/{rel}"
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_wd_path}:/ws",
             "alpine", "sh", "-c", "chmod -R a+w /ws 2>/dev/null || true"],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass

    def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a git command with a hard timeout so network hangs don't freeze the poller."""
        try:
            return subprocess.run(
                cmd, cwd=str(wd), capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as te:
            log.warning(f"[{product_name}] git {' '.join(cmd[1:])} timed out after {timeout}s")
            # Synthesize a failed CompletedProcess so callers uniformly check returncode
            return subprocess.CompletedProcess(
                cmd, returncode=124,
                stdout=(te.stdout.decode() if isinstance(te.stdout, bytes) else (te.stdout or "")),
                stderr=f"timed out after {timeout}s",
            )

    # 1. Fetch latest from origin (updates remote-tracking refs, prunes deleted branches)
    # Longer timeout — fetch can legitimately take a while on slow networks.
    r = _run(["git", "fetch", "origin", "--prune"], timeout=120)
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


def _checkout_sprint_branch(working_dir: str, sprint_branch: str, product_name: str) -> bool:
    """
    Pre-checkout the sprint branch before launching the agent so the agent's
    very first tool call lands on the right branch regardless of whether it
    follows the prompt's MANDATORY-FIRST-ACTION instruction.

    Runs after `_reset_workspace` (which leaves us on main) and assumes the
    sprint branch already exists on origin (provisioned by website's
    `_maybe_provision_sprint_pr` at sprint activation time).

    Returns True on success. On failure logs a warning and returns False —
    caller should leave the agent on `main` and rely on the post-coder
    pipeline's own checkout to recover, but flag this loudly so the operator
    knows the sprint branch wasn't pre-set.
    """
    if not sprint_branch:
        return False
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        log.warning(f"[{product_name}] sprint pre-checkout: not a git repo, skipping")
        return False

    def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, cwd=str(wd), capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as te:
            return subprocess.CompletedProcess(
                cmd, returncode=124,
                stdout=(te.stdout.decode() if isinstance(te.stdout, bytes) else (te.stdout or "")),
                stderr=f"timed out after {timeout}s",
            )

    # Fetch first so the remote ref is current. _reset_workspace already
    # fetched, but it was for origin/main with --prune; the sprint ref may
    # not have existed at fetch time if just provisioned.
    r = _run(["git", "fetch", "origin", sprint_branch], timeout=60)
    if r.returncode != 0:
        log.warning(
            f"[{product_name}] sprint pre-checkout: fetch origin {sprint_branch} failed: "
            f"{r.stderr.strip()[:200]}"
        )
        return False

    # Check it out as a tracking branch. -B forces creation/reset so we always
    # end up on a clean local branch tracking origin/<sprint_branch>.
    r = _run(["git", "checkout", "-B", sprint_branch, f"origin/{sprint_branch}"])
    if r.returncode != 0:
        log.warning(
            f"[{product_name}] sprint pre-checkout: checkout {sprint_branch} failed: "
            f"{r.stderr.strip()[:200]}"
        )
        return False

    log.info(f"[{product_name}] sprint pre-checkout: now on {sprint_branch}")
    return True


def _cleanup_workspace_post_session(working_dir: str, product_name: str) -> None:
    """
    Post-exit cleanup: return to main branch and remove uncommitted session artifacts.
    Runs after container exits (success or failure) so the next session starts clean.
    Does NOT delete the working directory — git history and pushed branches are preserved.
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return

    def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, cwd=str(wd), capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.warning(f"[{product_name}] git {' '.join(cmd[1:])} timed out after {timeout}s")
            return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timed out")

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
    # working_dir_host  = what the Docker daemon sees (Windows path from DB)
    # working_dir       = what THIS Python process sees (translated to /products/... inside Hermes,
    #                     unchanged in host-mode). Used for every subsequent file op.
    working_dir_host = product["working_dir"]
    working_dir = container_path(working_dir_host)

    # Always reset workspace to clean main before starting a new session.
    # This discards any half-baked code from failed/incomplete previous sessions.
    _reset_workspace(working_dir, product.get("name", str(working_dir)))

    # Delete stale session_result.json BEFORE launch — previous agents may have
    # committed it to git, so git clean won't remove it.
    _delete_session_result(working_dir)

    # Re-install templates after reset — git clean may have removed untracked template files.
    # force=False ensures we never overwrite files the agent has customised and committed.
    try:
        # Pass product dict with the container-side working_dir so Path(...).exists() works
        # when docker_runner runs inside Hermes (where the DB stores the host/Windows path).
        install_templates({**product, "working_dir": working_dir}, PM_API_URL, force=False)
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
    product["_active_sprint"] = active_sprint or {}

    # Sprint-PR-mode context: when the product opts in via config.sprint_pr_mode
    # AND the active sprint has been provisioned with a branch + PR (see
    # website._maybe_provision_sprint_pr), agents push to that branch instead of
    # cutting fresh `coder/<uid>` branches and opening parallel PRs. Off by
    # default — the per-feature branch flow remains the fallback.
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

    # GH_TOKEN — write to a 0600 temp file and bind-mount at /run/secrets/gh_token.
    # The agent_cmd wrapper (below) sources it into GH_TOKEN at runtime, so `gh` CLI
    # works but the token is never visible in `docker inspect` / process listings.
    gh_token = _get_gh_token()
    gh_token_file: str | None = None
    gh_mount: list[str] = []
    if gh_token:
        try:
            _fd, gh_token_file = tempfile.mkstemp(prefix="pf_gh_", suffix=".token")
            os.close(_fd)
            Path(gh_token_file).write_text(gh_token)
            try:
                os.chmod(gh_token_file, 0o600)
            except Exception:
                pass  # Windows: NTFS perms don't map cleanly; 0600 is best-effort
            gh_mount = ["-v", f"{host_path(gh_token_file)}:/run/secrets/gh_token:ro"]
        except Exception as e:
            log.warning(f"Could not write gh token file: {e} — agent will have no gh auth")
            if gh_token_file:
                try:
                    Path(gh_token_file).unlink()
                except Exception:
                    pass
            gh_token_file = None
            gh_mount = []
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
        # Claude backend: SELECTIVELY copy only auth-essential files to a temp dir.
        # A full copytree of ~/.claude races with the host's active Claude Code
        # (which writes sessions/, history.jsonl, projects/, cache/ constantly)
        # and causes 9P filesystem RPC hangs on Docker Desktop for Windows.
        # We only need credentials + settings; per-session state is generated
        # fresh inside the agent container.
        creds_src, claude_model = _get_claude_profile(sys_cfg, persona=persona)
        log.info("Claude model for persona=%s: %s", persona, claude_model)
        # Files to copy verbatim from the host .claude dir. Everything else
        # (sessions/, history.jsonl, projects/, cache/, plugins/, etc.) is
        # skipped to avoid races with the host's live Claude Code process.
        _AUTH_FILES = [".credentials.json", "settings.json", "settings.local.json"]
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

        # Captured from the final claude -p `result` event so we can persist
        # cost / turn count / token totals onto the session record at end.
        # Claude only — Ollama agent doesn't emit a result event in this shape.
        session_meta: dict = {}

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
        #   - stall_timeout (5 min default)    — no agent events for this long
        # The stall window is much shorter, so a hung pytest / Ollama 500-loop /
        # silent claude child gets caught at minute 5 instead of minute 90.
        # Disabled when stall_timeout_minutes <= 0.
        stall_timeout_seconds = int(sys_cfg.get("stall_timeout_minutes")
                                    or os.environ.get("STALL_TIMEOUT_MINUTES", "5")) * 60
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
    _session_features: list = []
    attempted = 0
    pushed = 0
    try:
        # 0. Coder ceremony — if a coder session exited cleanly, run the
        # deterministic commit/push/PR pipeline. Coders only write code; Python
        # handles git + gh, which makes the LLM's job 10× simpler and more
        # reliable. The pipeline appends Reviewing entries to session_result.json
        # so the existing reconcile picks them up below.
        if exit_code == 0 and persona == "coder":
            try:
                _run_post_coder_pipeline(product, session_uid, working_dir,
                                         product.get("_assigned_features", []))
            except Exception:
                log.exception(f"Post-coder pipeline failed for {product.get('name')}")

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
        pushed = sum(1 for f in _session_features
                     if isinstance(f, dict)
                     and f.get("id") in assigned_ids
                     and f.get("status") in ("Pushed", "Reviewing", "Reviewed", "Designed"))
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

    # After a successful coder session: QA → Security (feature-level, run per PR).
    # Recommender runs post-sprint via the poller's _post_sprint_persona_due gate.
    if exit_code == 0 and persona == "coder":
        log.info(f"Coder succeeded — launching QA Tester for {product['name']}")
        run_claude_in_docker(product, persona="qa_tester")

        log.info(f"Launching Security Auditor for {product['name']}")
        run_claude_in_docker(product, persona="security_auditor")

    return exit_code


def _post_log_lines(product_id: int, lines: list[str]) -> None:
    """Fire-and-forget: push log lines to PM API for SSE streaming."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.post(f"/api/products/{product_id}/session/log", json={"lines": lines})
    except Exception:
        pass
