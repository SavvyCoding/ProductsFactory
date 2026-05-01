"""
Phase-1 supervisor — rule-based detectors that catch stuck-state patterns
the deterministic orchestrator misses.

No LLM. Each detector is a small Python function called from existing hook
points (post-coder pipeline, reconcile sweep, determine_next_action). Every
firing writes one audit row to `supervisor_actions` so PMs can see what the
system has been doing automatically.

This module is the audit + dispatcher layer. The detector functions live
here too. To disable any detector, flip its flag in `system_config` — the
orchestrator's regular config-fetch picks it up next cycle, no restart.

Detectors:
  - false_success     — coder session lied about completing
  - dirty_pr_close    — PR with merge conflicts, idle, ≥1h old → close + reset
  - auto_plan         — active sprint dead, ≥N unsprinted Approved → call plan
  - merge_stall_alert — sprint all-Reviewed but PR not merging for ≥1h → alert
  - overlap_pr        — multiple open PRs cover the same feature IDs → close older

All detectors honor `supervisor_dry_run_only` (global kill switch) and
their per-detector enabled flag. Audit rows are written even in dry-run.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Iterable

import httpx

log = logging.getLogger("supervisor")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")


# ── Audit log writer ─────────────────────────────────────────────────────────

def _record_action(
    *,
    detector: str,
    target_type: str,
    target_id: str | int,
    action: str,
    reason: str,
    product_id: int | None = None,
    dry_run: bool = False,
) -> None:
    """Append a row to supervisor_actions. Best-effort — never raises so a
    transient PM API hiccup doesn't drop the detector mid-cycle.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.post(
                "/api/supervisor/actions",
                json={
                    "detector":    detector,
                    "product_id":  product_id,
                    "target_type": target_type,
                    "target_id":   str(target_id),
                    "action":      action,
                    "reason":      reason,
                    "dry_run":     dry_run,
                },
            )
    except Exception:
        log.debug("supervisor audit write failed", exc_info=True)


def _get_supervisor_config() -> dict:
    """Pull supervisor-related fields from system_config, applying defaults."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            if resp.status_code != 200:
                return _DEFAULTS.copy()
            cfg = resp.json() or {}
    except Exception:
        return _DEFAULTS.copy()
    out = _DEFAULTS.copy()
    for key in _DEFAULTS:
        v = cfg.get(key)
        if v is not None and v != "":
            out[key] = v
    return out


_DEFAULTS = {
    "supervisor_dry_run_only":             False,
    "supervisor_false_success_enabled":    True,
    "supervisor_dirty_pr_enabled":         True,
    "supervisor_dirty_pr_min_age_min":     60,
    "supervisor_dirty_pr_idle_min":        30,
    "supervisor_auto_plan_enabled":        True,
    "supervisor_auto_plan_min_unsprinted": 3,
    "supervisor_merge_stall_enabled":      True,
    "supervisor_merge_stall_min_min":      60,
    "supervisor_overlap_pr_enabled":       True,
}


# ── Detector B: coder false-success ──────────────────────────────────────────
# Coder session ended exit_code=0 but features_pushed=0 AND no fix_attempts
# bump happened on its assigned features. The agent gamed the no-edit gate
# (e.g. wrote summary markdown files, then called task_done). Without this
# detector the assigned features sit Implementing for 45 min until reset_stuck
# rolls them back, then the next coder claims them and likely repeats. This
# bumps fix_attempts now so the Blocked-sprint route triggers faster, and
# demotes the feature so the next coder treats it as a rework.

def detect_false_success(
    *,
    product_id: int,
    session_uid: str,
    exit_code: int | None,
    assigned_features: Iterable[dict],
) -> int:
    """Run after a coder session exits. Returns number of features touched.

    Inputs:
      product_id, session_uid: identifies the session for audit trail
      exit_code: docker exit code (None = still running, skip)
      assigned_features: list of feature dicts from
        _fetch_assigned_features (must include id + status)
    """
    if exit_code != 0:
        return 0  # only fires on clean-exit lies; non-zero exits handle themselves

    cfg = _get_supervisor_config()
    if not cfg["supervisor_false_success_enabled"]:
        return 0

    dry_run = cfg["supervisor_dry_run_only"]
    touched = 0

    # Re-fetch each assigned feature's CURRENT state from the PM API.
    # The agent may have advanced some via session_result.json; we only
    # care about the ones that didn't move.
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f_in in assigned_features:
                fid = f_in.get("id")
                if not fid:
                    continue
                fresh = client.get(f"/api/features/{fid}")
                if fresh.status_code != 200:
                    continue
                feat = fresh.json()
                # Only act on features still stuck Implementing with no PR.
                # If the agent did push (Reviewing/Reviewed) we leave alone.
                if feat.get("status") != "Implementing" or feat.get("pr_number"):
                    continue
                cur_attempts = feat.get("fix_attempts") or 0
                new_attempts = cur_attempts + 1
                reason = (
                    f"Coder session {session_uid} exited 0 with features_pushed=0 "
                    f"and no PR for feature #{fid}. Bumping fix_attempts "
                    f"{cur_attempts}→{new_attempts} and demoting to "
                    f"Implementing+changes_requested so next coder treats it "
                    f"as a rework cycle."
                )
                _record_action(
                    detector="false_success",
                    product_id=product_id,
                    target_type="feature",
                    target_id=fid,
                    action="bump_attempts_and_demote",
                    reason=reason,
                    dry_run=dry_run,
                )
                touched += 1
                if dry_run:
                    continue
                # Apply: increment fix_attempts + ensure review_outcome marker
                # so determine_next_action's "codeable" filter picks it up next
                # cycle. (Implementing+changes_requested IS in the codeable set.)
                client.patch(
                    f"/api/features/{fid}",
                    json={
                        "fix_attempts":   new_attempts,
                        "review_outcome": "changes_requested",
                        "changed_by":     "supervisor",
                    },
                )
    except Exception:
        log.exception(f"detect_false_success crashed for session {session_uid}")
    return touched


# ── Cooldown helper ──────────────────────────────────────────────────────────

def _recent_action(
    *,
    product_id: int,
    detector: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    within_hours: float = 24,
) -> bool:
    """Return True if a non-dry-run action of this detector fired against the
    given target within the cooldown window. Used to prevent detectors from
    re-firing on the same target every cycle.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get(f"/api/products/{product_id}/supervisor-actions",
                              params={"limit": 200})
            if resp.status_code != 200:
                return False
            rows = resp.json() or []
    except Exception:
        return False
    cutoff = datetime.now(timezone.utc).timestamp() - (within_hours * 3600)
    for r in rows:
        if r.get("detector") != detector:
            continue
        if r.get("dry_run"):
            continue
        if target_type is not None and r.get("target_type") != target_type:
            continue
        if target_id is not None and str(r.get("target_id")) != str(target_id):
            continue
        ts = r.get("created_at") or ""
        try:
            row_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        if row_ts >= cutoff:
            return True
    return False


# ── Detector A: dirty-PR auto-close ──────────────────────────────────────────
# Open PR with mergeable_state="dirty", opened ≥dirty_pr_min_age_min ago, and
# no commits in the last dirty_pr_idle_min minutes → close + reset features
# tracking that PR back to Implementing+changes_requested. Grace period
# protects against transient dirty states (CI restart, main moving). Rate
# limit caps closes per cycle so we never thunder-close on a misconfig.

_DIRTY_PR_MAX_PER_CYCLE = 2

def detect_dirty_prs(
    *,
    product_id: int,
    github_repo: str,
    open_prs_with_state: list[dict],
    features: list[dict],
    github_token: str | None,
) -> int:
    """Caller passes pre-fetched PR + feature data so we don't re-hit GitHub.

    Each entry in `open_prs_with_state` must include: number, mergeable_state,
    created_at, last_commit_at (ISO strings).
    Returns number of PRs closed (or that would be closed in dry-run).
    """
    cfg = _get_supervisor_config()
    if not cfg["supervisor_dirty_pr_enabled"]:
        return 0
    dry_run = cfg["supervisor_dry_run_only"]
    min_age = cfg["supervisor_dirty_pr_min_age_min"] * 60
    idle    = cfg["supervisor_dirty_pr_idle_min"] * 60
    now = datetime.now(timezone.utc).timestamp()
    closed = 0

    def _ts(s: str | None) -> float:
        if not s:
            return 0
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0

    for pr in open_prs_with_state:
        if closed >= _DIRTY_PR_MAX_PER_CYCLE:
            break
        if pr.get("mergeable_state") != "dirty":
            continue
        age = now - _ts(pr.get("created_at"))
        idle_for = now - _ts(pr.get("last_commit_at") or pr.get("created_at"))
        if age < min_age or idle_for < idle:
            continue
        pr_n = pr.get("number")
        if not pr_n or _recent_action(product_id=product_id, detector="dirty_pr_close",
                                       target_type="pr", target_id=pr_n, within_hours=24):
            continue
        # Find features tracking this PR
        affected = [f for f in features if f.get("pr_number") == pr_n
                    and f.get("status") not in ("Pushed", "Rejected", "Reverted", "Deferred")]
        reason = (
            f"PR #{pr_n} mergeable_state=dirty for {int(age//60)}min, "
            f"idle for {int(idle_for//60)}min. Closing and resetting "
            f"{len(affected)} affected feature(s) for rework."
        )
        _record_action(detector="dirty_pr_close", product_id=product_id,
                       target_type="pr", target_id=pr_n,
                       action="close_and_reset", reason=reason, dry_run=dry_run)
        closed += 1
        if dry_run:
            continue
        # Mutate: close the PR, reset features
        if github_token and github_repo:
            try:
                # Parse owner/repo from URL
                m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
                if m:
                    slug = m.group(1)
                    headers = {
                        "Authorization": f"Bearer {github_token}",
                        "Accept":        "application/vnd.github+json",
                    }
                    httpx.post(
                        f"https://api.github.com/repos/{slug}/issues/{pr_n}/comments",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"body": f"[supervisor] Auto-closing — mergeable_state=dirty for "
                                      f"{int(age//60)}min with no recent commits. "
                                      f"Affected features reset to changes_requested."},
                        timeout=10,
                    )
                    httpx.patch(
                        f"https://api.github.com/repos/{slug}/pulls/{pr_n}",
                        headers=headers, json={"state": "closed"}, timeout=10,
                    )
            except Exception:
                log.exception(f"dirty_pr_close: failed to close PR #{pr_n}")
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for f in affected:
                    client.patch(f"/api/features/{f['id']}", json={
                        "status":         "Implementing",
                        "review_outcome": "changes_requested",
                        "pr_number":      None,
                        "pr_url":         None,
                        "branch_name":    None,
                        "changed_by":     "supervisor",
                    })
        except Exception:
            log.exception(f"dirty_pr_close: failed to reset features for PR #{pr_n}")
    return closed


# ── Detector C: auto-plan trigger ────────────────────────────────────────────
# When the active sprint has nothing actionable AND there are unsprinted
# Approved features sitting in the backlog, call /plan-sprints so the
# planner picks them up. Cooldown prevents a re-plan loop.

def detect_auto_plan(
    *,
    product_id: int,
    active_sprint_has_codeable: bool,
    unsprinted_approved_count: int,
) -> bool:
    """Caller has already determined whether the active sprint has codeable
    work and how many unsprinted Approved features exist. We just decide
    whether to call /plan-sprints. Returns True if we did (or would have
    in dry-run).
    """
    cfg = _get_supervisor_config()
    if not cfg["supervisor_auto_plan_enabled"]:
        return False
    if active_sprint_has_codeable:
        return False
    threshold = cfg["supervisor_auto_plan_min_unsprinted"]
    if unsprinted_approved_count < threshold:
        return False
    if _recent_action(product_id=product_id, detector="auto_plan",
                      target_type="product", target_id=product_id, within_hours=4):
        return False
    dry_run = cfg["supervisor_dry_run_only"]
    reason = (
        f"Active sprint has no codeable features but {unsprinted_approved_count} "
        f"Approved features are unsprinted (threshold {threshold}). Calling "
        f"/plan-sprints to bring them into a new sprint."
    )
    _record_action(detector="auto_plan", product_id=product_id,
                   target_type="product", target_id=product_id,
                   action="plan_sprints", reason=reason, dry_run=dry_run)
    if dry_run:
        return True
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=120) as client:
            client.post(f"/api/products/{product_id}/plan-sprints")
    except Exception:
        log.exception(f"auto_plan: plan-sprints call failed for product {product_id}")
    return True


# ── Detector D: merge-stall alert ────────────────────────────────────────────
# Active sprint, all non-terminal features are Reviewed, and no PR merge
# happened for ≥merge_stall_min_min. Doesn't auto-mutate (multiple causes
# possible: CI flake, human reviewer needed, auto-merge disabled, conflicts).
# Just writes an audit row + Alert so PMs see it on the dashboard.

def detect_merge_stall(
    *,
    product_id: int,
    sprint_id: int,
    sprint_features: list[dict],
    last_merge_ts: float | None,
) -> bool:
    cfg = _get_supervisor_config()
    if not cfg["supervisor_merge_stall_enabled"]:
        return False
    threshold = cfg["supervisor_merge_stall_min_min"] * 60
    non_terminal = [f for f in sprint_features
                    if f.get("status") not in ("Pushed", "Rejected", "Reverted", "Deferred")]
    if not non_terminal:
        return False
    if not all(f.get("status") == "Reviewed" for f in non_terminal):
        return False
    now = datetime.now(timezone.utc).timestamp()
    if last_merge_ts is not None and (now - last_merge_ts) < threshold:
        return False
    if _recent_action(product_id=product_id, detector="merge_stall",
                      target_type="sprint", target_id=sprint_id, within_hours=24):
        return False
    reason = (
        f"Sprint {sprint_id} has {len(non_terminal)} feature(s) at Reviewed/approved "
        f"but no PR merge has happened for ≥{cfg['supervisor_merge_stall_min_min']}min. "
        f"Possible causes: dirty PR, CI flake, human reviewer needed, auto-merge disabled."
    )
    _record_action(detector="merge_stall", product_id=product_id,
                   target_type="sprint", target_id=sprint_id,
                   action="alert", reason=reason,
                   dry_run=cfg["supervisor_dry_run_only"])
    # Always-write Alert row (alerts are non-mutating informational by design).
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.post("/api/alerts", json={
                "product_id": product_id,
                "category":   "supervisor",
                "severity":   "warning",
                "message":    reason[:500],
            })
    except Exception:
        log.debug("merge_stall: alert post failed", exc_info=True)
    return True


# ── Detector E: overlap-PR detector ──────────────────────────────────────────
# Multiple open PRs whose titles list overlapping `#NN` feature IDs. Keep
# the freshest PR; close the older ones. Strict subset matching only — we
# never close on uncertainty.

_PR_FEAT_RE = re.compile(r"#(\d+)")
_OVERLAP_PR_MAX_PER_CYCLE = 2

def detect_overlapping_prs(
    *,
    product_id: int,
    github_repo: str,
    open_prs: list[dict],
    github_token: str | None,
) -> int:
    """Each entry in `open_prs` should have number, title, created_at.
    Returns number of PRs closed (or that would close in dry-run).
    """
    cfg = _get_supervisor_config()
    if not cfg["supervisor_overlap_pr_enabled"]:
        return 0
    dry_run = cfg["supervisor_dry_run_only"]
    closed = 0

    # Parse feature-ID set from each PR title; sort newest-first
    parsed = []
    for pr in open_prs:
        ids = set(int(m) for m in _PR_FEAT_RE.findall(pr.get("title", "")))
        if not ids:
            continue
        parsed.append((pr, ids))
    if len(parsed) < 2:
        return 0
    parsed.sort(key=lambda x: x[0].get("created_at", ""), reverse=True)

    # Only close PRs whose feature set is a strict subset of (or equal to) a
    # newer PR's set — that's the safe definition of "superseded". Disjoint
    # sets are NOT superseded; partial overlap is not enough either.
    keepers: list[set] = []
    for pr, ids in parsed:
        if closed >= _OVERLAP_PR_MAX_PER_CYCLE:
            break
        superseded_by = next((kept for kept in keepers if ids.issubset(kept)), None)
        if not superseded_by:
            keepers.append(ids)
            continue
        pr_n = pr.get("number")
        if not pr_n or _recent_action(product_id=product_id, detector="overlap_pr",
                                       target_type="pr", target_id=pr_n, within_hours=24):
            continue
        reason = (
            f"PR #{pr_n} (features {sorted(ids)}) is fully covered by a newer open PR "
            f"(features {sorted(superseded_by)}). Closing as superseded."
        )
        _record_action(detector="overlap_pr", product_id=product_id,
                       target_type="pr", target_id=pr_n,
                       action="close_superseded", reason=reason, dry_run=dry_run)
        closed += 1
        if dry_run:
            continue
        if github_token and github_repo:
            try:
                m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
                if m:
                    slug = m.group(1)
                    headers = {
                        "Authorization": f"Bearer {github_token}",
                        "Accept":        "application/vnd.github+json",
                    }
                    httpx.post(
                        f"https://api.github.com/repos/{slug}/issues/{pr_n}/comments",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"body": f"[supervisor] Auto-closing — superseded by a newer "
                                      f"open PR covering the same feature set."},
                        timeout=10,
                    )
                    httpx.patch(
                        f"https://api.github.com/repos/{slug}/pulls/{pr_n}",
                        headers=headers, json={"state": "closed"}, timeout=10,
                    )
            except Exception:
                log.exception(f"overlap_pr: failed to close PR #{pr_n}")
    return closed
