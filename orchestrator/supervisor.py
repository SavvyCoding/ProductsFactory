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

Detectors implemented:
  - false_success: coder session ended exit=0 with features_pushed=0 and no
    fix_attempts increment — bumps the counter and demotes status so the
    feature doesn't sit Implementing forever.

Detectors planned (see CLAUDE.md / Phase-1 plan):
  - dirty_pr_close
  - auto_plan
  - merge_stall_alert
  - overlap_pr
"""
from __future__ import annotations

import logging
import os
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
