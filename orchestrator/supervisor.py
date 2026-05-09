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
    "supervisor_dry_run_only":                  False,
    "supervisor_false_success_enabled":         True,
    "supervisor_kill_recovery_enabled":         True,
    "supervisor_dirty_pr_enabled":              True,
    "supervisor_dirty_pr_min_age_min":          60,
    "supervisor_dirty_pr_idle_min":             30,
    "supervisor_auto_plan_enabled":             True,
    "supervisor_auto_plan_min_unsprinted":      3,
    "supervisor_merge_stall_enabled":           True,
    "supervisor_merge_stall_min_min":           60,
    "supervisor_overlap_pr_enabled":            True,
    "supervisor_orphan_approved_enabled":       True,
    "supervisor_orphan_approved_min_age_hours": 24,
    "supervisor_orphan_approved_threshold":     1,
    "supervisor_rapid_flap_enabled":            True,
    "supervisor_rapid_flap_window_hours":       1,
    # 10 = ~one full designer→coder→reviewer cycle (7 transitions) plus one
    # rework iteration. Was 5 — too aggressive; auto-blocked features after
    # a single changes_requested round-trip. Must match the website default
    # in website/main.py to keep cross-process behaviour consistent.
    "supervisor_rapid_flap_min_transitions":    10,
    # detect_repeated_review_feedback: block when the reviewer's structured
    # feedback fingerprint repeats N consecutive cycles. Threshold=2 means
    # the third matching cycle blocks (signature seen, then seen again,
    # then seen-and-block). Threshold=1 would block on the very first repeat.
    "supervisor_repeated_feedback_enabled":     True,
    "supervisor_repeated_feedback_threshold":   2,
    # detect_invalid_status_combos: per-cycle sweep that auto-corrects
    # features stuck in the hybrid Reviewed/Reviewing + changes_requested
    # state (a reviewer-prompt-violation pattern). Demotes them to
    # Implementing+changes_requested so the codeable filter picks them up.
    # Belt-and-braces alongside the in-line normalizer in state_machine.py.
    "supervisor_invalid_combos_enabled":        True,
}


def _resolve_max_fix_attempts() -> int:
    """Read system_config.max_fix_attempts; fall back to env (5)."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            if resp.status_code == 200:
                val = (resp.json() or {}).get("max_fix_attempts")
                if val and int(val) > 0:
                    return int(val)
    except Exception:
        pass
    return int(os.environ.get("MAX_FIX_ATTEMPTS", "5"))


def _route_to_blocked_if_at_cap(
    *,
    client: httpx.Client,
    detector: str,
    product_id: int,
    feature_id: int,
    new_attempts: int,
    max_attempts: int,
    extra_reason: str,
    dry_run: bool,
) -> bool:
    """When fix_attempts has just crossed `max_attempts`, route the feature
    to the per-product Blocked sprint regardless of whether it has a PR.

    Bridges the gap that the existing Blocked-sprint route in
    `github_client.reconcile_in_flight_prs` only fires for features with
    a closed-unmerged PR on GitHub. Never-pushed features that exhaust
    their budget via repeated kills/false-success would otherwise sit in
    the active sprint forever — bug 126 in webcalculator was the canonical
    example: fix_attempts=5, status=Implementing, no pr_number, no escape.

    Idempotent: the website endpoint short-circuits if the feature is
    already in the Blocked sprint. Best-effort — never raises.

    Returns True if the route was attempted (regardless of HTTP outcome).
    """
    if new_attempts < max_attempts:
        return False

    full_reason = (
        f"Auto-blocked by supervisor.{detector}: fix_attempts={new_attempts} "
        f">= max_fix_attempts={max_attempts}. {extra_reason}"
    )
    _record_action(
        detector=detector,
        product_id=product_id,
        target_type="feature",
        target_id=feature_id,
        action="route_to_blocked_sprint",
        reason=full_reason,
        dry_run=dry_run,
    )
    if dry_run:
        return True
    try:
        resp = client.post(
            f"/api/products/{product_id}/sprints/blocked/route",
            json={"feature_ids": [feature_id], "reason": full_reason},
        )
        if resp.is_success:
            log.warning(
                f"[{detector}] Feature #{feature_id} -> Blocked sprint "
                f"(fix_attempts={new_attempts} >= {max_attempts})"
            )
        else:
            log.warning(
                f"[{detector}] route to Blocked sprint failed for "
                f"#{feature_id}: HTTP {resp.status_code} {resp.text[:120]}"
            )
    except Exception as e:
        log.warning(
            f"[{detector}] route to Blocked sprint failed for #{feature_id}: {e}"
        )
    return True


# ── Repeated-review-feedback signature ───────────────────────────────────────
# A canonical fingerprint of a reviewer's changes_requested feedback. Two
# reviewers complaining about the same thing in slightly different prose
# should produce the same signature; if the coder addresses anything, the
# signature should change.
#
# Strategy: pull the section heads (functional / tests / security) and the
# first-noun key phrases out of each ❌ bullet, normalize, hash. Falls back
# to a hash of `review_notes` when no per-section comments are available.

import hashlib as _hashlib

_SECTION_RE = re.compile(r"^[^a-z]*?(functional|tests?|security)\b", re.IGNORECASE)
# The reviewer prompt mandates a trailing `[{session_uid}]` tag on every
# comment for audit attribution. The session UID rotates per-session, so
# we strip the tag before fingerprinting — otherwise two reviewers with
# the SAME complaint would produce different signatures.
_SESSION_TAG_RE = re.compile(r"\s*\[[^\]]+\]\s*$")


def _signature_from_comments(comments: list[dict]) -> str | None:
    """Build a stable signature from the reviewer's `❌ <section>: <body>`
    comments for one feature. Returns None if no usable comments are found.

    Implementation notes:
    - Only consider comments authored by `reviewer` (case-insensitive).
    - Only consider comments whose body starts with `❌` (the prompt-mandated
      changes-requested marker). `✅` comments don't carry feedback to repeat.
    - For each match, extract `(section, payload)` where:
        section = functional|tests|security (lower-cased)
        payload = the first ~60 chars of the body after the section colon,
                  lowercased, alphanumerics+spaces only, single-spaced.
    - Sort the resulting set so order doesn't affect the hash.
    - Return sha1("|".join(sorted_set)) or None if the set is empty.
    """
    if not comments:
        return None
    keys: set[str] = set()
    for c in comments:
        if (c.get("author") or "").lower() != "reviewer":
            continue
        body = (c.get("body") or "").strip()
        if not body or not body.startswith("❌"):
            continue
        m = _SECTION_RE.match(body)
        if not m:
            continue
        section = m.group(1).lower().rstrip("s")  # tests -> test
        # Strip the section prefix and the leading "❌" / colon / spaces.
        tail = body[m.end():].lstrip(" :—-")
        # Drop the trailing `[session_uid]` audit tag — it rotates per
        # reviewer session and would otherwise pollute the signature.
        tail = _SESSION_TAG_RE.sub("", tail).lower()
        # Reduce to alnum+space and clamp length so trivial wording shifts
        # ("at file:42" vs "in file:42") don't produce different hashes.
        norm = re.sub(r"[^a-z0-9 ]+", " ", tail)
        norm = re.sub(r"\s+", " ", norm).strip()[:60]
        if norm:
            keys.add(f"{section}:{norm}")
    if not keys:
        return None
    return _hashlib.sha1("|".join(sorted(keys)).encode("utf-8")).hexdigest()


def _signature_from_review_notes(review_notes: str | None) -> str | None:
    """Fallback signature when no per-section comments are present —
    happens when the reviewer wrote `review_notes` directly on the feature
    instead of (or in addition to) posting comments. Coarser than the
    comment-based signature but still catches identical-text repeats."""
    if not review_notes:
        return None
    norm = re.sub(r"[^a-z0-9 ]+", " ", review_notes.lower())
    norm = re.sub(r"\s+", " ", norm).strip()[:200]
    if not norm:
        return None
    return _hashlib.sha1(norm.encode("utf-8")).hexdigest()


# ── Detector: repeated review feedback (auto-block dead-end loops) ───────────
# When a reviewer's changes_requested feedback fingerprint matches the
# previous changes_requested cycle, the coder is going in circles. The
# fix_attempts=5 cap would eventually catch this in ~3-4 hours (4 cycles
# of ~30-60 min each on Ollama Cloud). This detector catches it earlier:
# at threshold=2 (default), the third consecutive matching signature
# blocks. The blocked feature gets pr_number=None so the sprint PR's
# auto-merge sweep stops waiting on it.
#
# Hook point: post-reviewer pipeline (auto_merge_reviewer.py), called once
# per feature whose session_result.json entry was changes_requested.

def detect_repeated_review_feedback(
    *,
    feature_id: int,
    product_id: int | None = None,
    review_notes: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Compute the latest reviewer-feedback signature for `feature_id` and
    compare it to the previously-stored one. Bumps `repeated_changes_count`
    if they match; blocks the feature when the count crosses the threshold.

    Returns a dict describing what happened:
      {"action": "no-op|stored|incremented|blocked",
       "signature": <sha1 hex or None>,
       "repeated": <int>,
       "reason": "<explanation>"}

    Best-effort — never raises. Honors dry-run + the per-detector enabled
    flag in system_config. Idempotent on re-invocation: if the signature
    is unchanged but already counted, this call is a no-op (count is only
    incremented when storing a NEW comparison).
    """
    cfg = _get_supervisor_config()
    if cfg.get("supervisor_dry_run_only"):
        dry_run = True
    if not cfg.get("supervisor_repeated_feedback_enabled"):
        return {"action": "no-op", "signature": None, "repeated": 0,
                "reason": "detector disabled in system_config"}

    threshold = int(cfg.get("supervisor_repeated_feedback_threshold", 2))

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            feat_resp = client.get(f"/api/features/{feature_id}")
            if feat_resp.status_code != 200:
                return {"action": "no-op", "signature": None, "repeated": 0,
                        "reason": f"feature fetch returned HTTP {feat_resp.status_code}"}
            feat = feat_resp.json()
            comments_resp = client.get(f"/api/features/{feature_id}/comments")
            comments = comments_resp.json() if comments_resp.status_code == 200 else []

            # Build the latest signature. Prefer per-section comments;
            # fall back to review_notes hash if comments are absent.
            sig = _signature_from_comments(comments)
            if sig is None:
                sig = _signature_from_review_notes(
                    review_notes if review_notes is not None else feat.get("review_notes")
                )
            if sig is None:
                return {"action": "no-op", "signature": None, "repeated": 0,
                        "reason": "no usable feedback to fingerprint"}

            prior_sig = feat.get("last_changes_signature")
            prior_count = int(feat.get("repeated_changes_count") or 0)

            if prior_sig is None:
                # First changes_requested cycle — record the baseline only.
                _record_action(
                    detector="repeated_review_feedback",
                    product_id=product_id or feat.get("product_id"),
                    target_type="feature",
                    target_id=feature_id,
                    action="store_baseline_signature",
                    reason=f"First changes_requested fingerprint stored: {sig[:12]}…",
                    dry_run=dry_run,
                )
                if not dry_run:
                    client.patch(
                        f"/api/features/{feature_id}",
                        json={
                            "last_changes_signature": sig,
                            "repeated_changes_count": 0,
                            "changed_by": "supervisor.repeated_review_feedback",
                        },
                    )
                return {"action": "stored", "signature": sig, "repeated": 0,
                        "reason": "baseline stored"}

            if sig != prior_sig:
                # Coder addressed something — reset the counter and roll the signature.
                _record_action(
                    detector="repeated_review_feedback",
                    product_id=product_id or feat.get("product_id"),
                    target_type="feature",
                    target_id=feature_id,
                    action="reset_signature",
                    reason=f"Signature changed ({prior_sig[:12]}… -> {sig[:12]}…); resetting counter",
                    dry_run=dry_run,
                )
                if not dry_run:
                    client.patch(
                        f"/api/features/{feature_id}",
                        json={
                            "last_changes_signature": sig,
                            "repeated_changes_count": 0,
                            "changed_by": "supervisor.repeated_review_feedback",
                        },
                    )
                return {"action": "stored", "signature": sig, "repeated": 0,
                        "reason": "signature changed; counter reset"}

            # Same signature as last time — increment.
            new_count = prior_count + 1
            if new_count < threshold:
                _record_action(
                    detector="repeated_review_feedback",
                    product_id=product_id or feat.get("product_id"),
                    target_type="feature",
                    target_id=feature_id,
                    action="increment_repeated_count",
                    reason=f"Same fingerprint {new_count}× consecutively (threshold={threshold})",
                    dry_run=dry_run,
                )
                if not dry_run:
                    client.patch(
                        f"/api/features/{feature_id}",
                        json={
                            "repeated_changes_count": new_count,
                            "changed_by": "supervisor.repeated_review_feedback",
                        },
                    )
                return {"action": "incremented", "signature": sig, "repeated": new_count,
                        "reason": "below threshold"}

            # Threshold met — block the feature and detach from the sprint PR.
            note_excerpt = (review_notes or feat.get("review_notes") or "")[:200]
            block_reason = (
                f"Auto-blocked by supervisor.repeated_review_feedback: "
                f"reviewer requested the same change {new_count + 1}× consecutively "
                f"(threshold={threshold}). Last note: {note_excerpt}"
            )
            _record_action(
                detector="repeated_review_feedback",
                product_id=product_id or feat.get("product_id"),
                target_type="feature",
                target_id=feature_id,
                action="block_repeated_feedback",
                reason=block_reason,
                dry_run=dry_run,
            )
            if dry_run:
                return {"action": "blocked", "signature": sig, "repeated": new_count,
                        "reason": "dry-run: would block + clear pr_number + route to Blocked sprint"}

            # Block: status=Blocked, pr_number cleared, blocked_reason set.
            client.patch(
                f"/api/features/{feature_id}",
                json={
                    "status": "Blocked",
                    "pr_number": None,
                    "blocked_reason": block_reason,
                    "repeated_changes_count": new_count,
                    "changed_by": "supervisor.repeated_review_feedback",
                },
            )
            # Route to per-product Blocked sprint via the same endpoint
            # _route_to_blocked_if_at_cap uses. Idempotent — the website
            # endpoint short-circuits if already there.
            try:
                pid = product_id or feat.get("product_id")
                if pid:
                    client.post(
                        f"/api/products/{pid}/sprints/blocked/route",
                        json={"feature_ids": [feature_id], "reason": block_reason},
                    )
            except Exception as e:
                log.warning(
                    f"[repeated_review_feedback] route-to-Blocked-sprint failed "
                    f"for #{feature_id}: {e}"
                )
            log.warning(
                f"[repeated_review_feedback] Feature #{feature_id} -> Blocked "
                f"(same fingerprint {new_count + 1}× in a row)"
            )
            return {"action": "blocked", "signature": sig, "repeated": new_count,
                    "reason": "threshold met; feature blocked"}
    except Exception:
        log.exception(
            f"detect_repeated_review_feedback crashed for feature #{feature_id}"
        )
        return {"action": "no-op", "signature": None, "repeated": 0,
                "reason": "exception (logged)"}


# ── Detector: invalid status/review_outcome combos (per-cycle sweep) ─────────
# Per-cycle reactive layer for the same reviewer-prompt-violation that the
# state_machine normalizer guards against in real time. Catches:
#   - features that landed in `Reviewed + changes_requested` or
#     `Reviewing + changes_requested` BEFORE the in-line normalizer was
#     deployed (historical drift)
#   - features that bypass _apply_session_entry (PATCHes via the website
#     UI, manual scripts, future code paths) and end up in the hybrid state
#
# The action: demote `status` to `Implementing` while preserving
# `review_outcome=changes_requested`. The next dispatcher cycle then
# matches the codeable filter and a coder picks it up. Idempotent — a
# feature in valid state is a no-op.

def detect_invalid_status_combos(
    products: list[dict] | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """Sweep all `ready` products for features in invalid hybrid status
    combos. Demotes them to `Implementing+changes_requested`.

    Honors `supervisor_invalid_combos_enabled` (default True) and the
    global `supervisor_dry_run_only` kill switch. Returns a summary dict:
        {"checked": <int>, "fixed": <int>, "errors": <int>}
    Best-effort — never raises. Safe to call from any per-cycle hook.
    """
    cfg = _get_supervisor_config()
    if cfg.get("supervisor_dry_run_only"):
        dry_run = True
    if not cfg.get("supervisor_invalid_combos_enabled", True):
        return {"checked": 0, "fixed": 0, "errors": 0,
                "skipped": "disabled in system_config"}

    summary = {"checked": 0, "fixed": 0, "errors": 0}
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            # If caller didn't pass products, fetch the active set.
            if products is None:
                resp = client.get("/api/products")
                if resp.status_code != 200:
                    return summary
                products = [p for p in resp.json()
                            if isinstance(p, dict) and p.get("status") == "ready"]

            for product in products:
                pid = product.get("id")
                if not pid:
                    continue
                try:
                    feats_resp = client.get(f"/api/products/{pid}/features")
                    if feats_resp.status_code != 200:
                        continue
                    features = feats_resp.json()
                except Exception:
                    summary["errors"] += 1
                    continue

                for feat in features:
                    if not isinstance(feat, dict):
                        continue
                    summary["checked"] += 1
                    status = feat.get("status")
                    outcome = feat.get("review_outcome")
                    fid = feat.get("id")
                    # The two invalid combos we know about:
                    #   - Reviewed + changes_requested (#374 incident)
                    #   - Reviewing + changes_requested (#377 incident)
                    if (status in ("Reviewed", "Reviewing")
                            and outcome == "changes_requested"
                            and fid):
                        reason = (
                            f"Auto-correcting hybrid combo: status={status} + "
                            f"review_outcome=changes_requested → status=Implementing. "
                            f"Belongs in the codeable queue, not the reviewer queue."
                        )
                        _record_action(
                            detector="invalid_status_combos",
                            product_id=pid,
                            target_type="feature",
                            target_id=fid,
                            action="normalize_to_implementing",
                            reason=reason,
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            try:
                                client.patch(
                                    f"/api/features/{fid}",
                                    json={
                                        "status": "Implementing",
                                        "review_outcome": "changes_requested",
                                        "changed_by": "supervisor.invalid_status_combos",
                                    },
                                )
                                summary["fixed"] += 1
                            except Exception:
                                summary["errors"] += 1
                                log.exception(
                                    f"[invalid_status_combos] PATCH failed for "
                                    f"feature #{fid}"
                                )
                        else:
                            summary["fixed"] += 1   # would-have-fixed in dry-run
            if summary["fixed"]:
                log.info(
                    f"[invalid_status_combos] checked={summary['checked']} "
                    f"fixed={summary['fixed']} errors={summary['errors']} "
                    f"dry_run={dry_run}"
                )
    except Exception:
        log.exception("detect_invalid_status_combos sweep crashed")
        summary["errors"] += 1
    return summary


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

    dry_run      = cfg["supervisor_dry_run_only"]
    max_attempts = _resolve_max_fix_attempts()
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
                # If this bump just crossed max_fix_attempts, route to the
                # per-product Blocked sprint. Without this, never-pushed
                # features (no PR ever opened) bypass the existing route in
                # github_client.reconcile_in_flight_prs and sit in the active
                # sprint forever. See INVARIANTS.md VI.5.
                _route_to_blocked_if_at_cap(
                    client=client,
                    detector="false_success",
                    product_id=product_id,
                    feature_id=fid,
                    new_attempts=new_attempts,
                    max_attempts=max_attempts,
                    extra_reason=(
                        f"Coder session {session_uid} declared success but "
                        f"feature #{fid} never reached Reviewing."
                    ),
                    dry_run=dry_run,
                )
    except Exception:
        log.exception(f"detect_false_success crashed for session {session_uid}")
    return touched


# ── Detector F: kill recovery ────────────────────────────────────────────────
# Session was killed (watchdog timeout, stall, container OOM, manual docker
# kill, etc.) — exit_code != 0 and no clean session_result.json was written.
# Without this, assigned features stay in their pre-session state until
# reset_stuck rolls them back after 45 min, then the next agent picks the
# same poisoned features and gets killed again. Each kill bumps fix_attempts
# so the Blocked-sprint route triggers after `max_fix_attempts` kills, then
# the loop terminates with a PM triage signal.

def detect_kill_recovery(
    *,
    product_id: int,
    session_uid: str,
    persona: str,
    exit_code: int | None,
    assigned_features: Iterable[dict],
) -> int:
    """Run after a non-zero / killed session exits. Returns features touched.

    Symmetric to detect_false_success but fires on the failure side. The
    feature filter is broader (any agent state, not just Implementing) so
    designer/reviewer kills also bump.
    """
    if exit_code == 0 or exit_code is None:
        return 0  # successes go through detect_false_success

    # exit 42 = pf-verify-env preflight failure. Agent never started. Charging
    # fix_attempts here would bin features for an environmental issue that no
    # in-container code change can repair. _finalize_session already alerted
    # the operator and released the claims via _rollback_stuck_features.
    if exit_code == 42:
        return 0

    cfg = _get_supervisor_config()
    if not cfg["supervisor_kill_recovery_enabled"]:
        return 0
    dry_run      = cfg["supervisor_dry_run_only"]
    max_attempts = _resolve_max_fix_attempts()
    touched = 0

    _AGENT_STATES = {"Designing", "Implementing", "Reviewing"}
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
                if feat.get("status") not in _AGENT_STATES:
                    continue  # feature already moved on (Pushed/Reviewed/etc.)
                cur_attempts = feat.get("fix_attempts") or 0
                new_attempts = cur_attempts + 1
                reason = (
                    f"{persona} session {session_uid} killed (exit {exit_code}). "
                    f"Feature #{fid} still in {feat.get('status')}. Bumping "
                    f"fix_attempts {cur_attempts}→{new_attempts} so the kill "
                    f"loop hits the Blocked-sprint route at max_fix_attempts."
                )
                _record_action(
                    detector="kill_recovery",
                    product_id=product_id,
                    target_type="feature",
                    target_id=fid,
                    action="bump_attempts_after_kill",
                    reason=reason,
                    dry_run=dry_run,
                )
                touched += 1
                if dry_run:
                    continue
                # Demote to Implementing+changes_requested so the coder
                # filter picks it up next cycle (unless already Designing,
                # in which case _rollback_stuck_features will reset it).
                patch: dict = {"fix_attempts": new_attempts, "changed_by": "kill_recovery"}
                if feat.get("status") in ("Implementing", "Reviewing"):
                    patch["review_outcome"] = "changes_requested"
                client.patch(f"/api/features/{fid}", json=patch)
                # If this bump just crossed max_fix_attempts, route to the
                # per-product Blocked sprint. Bridges the gap that
                # github_client.reconcile_in_flight_prs only routes features
                # with a closed-unmerged PR. See INVARIANTS.md VI.5.
                _route_to_blocked_if_at_cap(
                    client=client,
                    detector="kill_recovery",
                    product_id=product_id,
                    feature_id=fid,
                    new_attempts=new_attempts,
                    max_attempts=max_attempts,
                    extra_reason=(
                        f"{persona} session {session_uid} killed without "
                        f"pushing a PR; feature #{fid} exhausted retries."
                    ),
                    dry_run=dry_run,
                )
    except Exception:
        log.exception(f"detect_kill_recovery crashed for session {session_uid}")
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


# ── Detector G: orphan-Approved features ─────────────────────────────────────
# Approved features sitting unsprinted for >24h are dead weight — the
# planner already ran and didn't pick them up. Auto_plan only triggers when
# the active sprint has zero codeable; if a sprint is busy these orphans
# never get adopted. This detector watches for them age-out and calls
# /plan-sprints regardless of active-sprint state.

def detect_orphan_approved(
    *,
    product_id: int,
    features: list[dict],
) -> bool:
    """`features` is the product's full feature list (already fetched by
    the caller for other purposes — passing it in avoids an extra round-trip).
    """
    cfg = _get_supervisor_config()
    if not cfg["supervisor_orphan_approved_enabled"]:
        return False
    min_age = cfg["supervisor_orphan_approved_min_age_hours"] * 3600
    threshold = cfg["supervisor_orphan_approved_threshold"]
    now = datetime.now(timezone.utc).timestamp()

    def _ts(s: str | None) -> float:
        if not s:
            return 0
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0

    orphans = [
        f for f in features
        if f.get("status") == "Approved"
        and not f.get("sprint_id")
        and (now - _ts(f.get("updated_at"))) >= min_age
    ]
    if len(orphans) < threshold:
        return False
    if _recent_action(product_id=product_id, detector="orphan_approved",
                      target_type="product", target_id=product_id, within_hours=4):
        return False
    dry_run = cfg["supervisor_dry_run_only"]
    reason = (
        f"{len(orphans)} Approved feature(s) unsprinted for "
        f">={cfg['supervisor_orphan_approved_min_age_hours']}h "
        f"(threshold {threshold}). Calling /plan-sprints — orphan IDs: "
        f"{[f.get('id') for f in orphans[:10]]}."
    )
    _record_action(detector="orphan_approved", product_id=product_id,
                   target_type="product", target_id=product_id,
                   action="plan_sprints", reason=reason, dry_run=dry_run)
    if dry_run:
        return True
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=120) as client:
            client.post(f"/api/products/{product_id}/plan-sprints")
    except Exception:
        log.exception(f"orphan_approved: plan-sprints call failed for product {product_id}")
    return True


# ── Detector H: rapid status flap ────────────────────────────────────────────
# Feature status pinging Reviewing↔Implementing (or any state ↔ another)
# more than `min_transitions` times in `window_hours` indicates the agent
# pipeline is stuck in a tight loop with no progress. Route the feature to
# the per-product Blocked sprint for PM triage. 24h cooldown per feature.

def detect_rapid_flap(
    *,
    product_id: int,
    flapping_features: list[dict],
) -> int:
    """`flapping_features` is the API response from
    GET /api/products/{id}/flapping-features (one row per feature with
    {feature_id, transitions, window_hours}). Caller pre-fetches.
    Returns number of features routed (or that would route in dry-run).
    """
    cfg = _get_supervisor_config()
    if not cfg["supervisor_rapid_flap_enabled"]:
        return 0
    if not flapping_features:
        return 0
    dry_run = cfg["supervisor_dry_run_only"]
    routed = 0
    feature_ids: list[int] = []
    reasons: list[str] = []
    for row in flapping_features:
        fid = row.get("feature_id") or row.get("id")
        if not fid:
            continue
        if _recent_action(product_id=product_id, detector="rapid_flap",
                          target_type="feature", target_id=fid, within_hours=24):
            continue
        n = row.get("transitions", 0)
        win = row.get("window_hours", cfg["supervisor_rapid_flap_window_hours"])
        reason = (
            f"Feature #{fid} cycled status {n} times in {win}h "
            f"(threshold {cfg['supervisor_rapid_flap_min_transitions']}). "
            f"Routing to Blocked sprint for PM triage — agent pipeline is "
            f"stuck in a flap loop with no progress."
        )
        _record_action(detector="rapid_flap", product_id=product_id,
                       target_type="feature", target_id=fid,
                       action="route_to_blocked", reason=reason, dry_run=dry_run)
        routed += 1
        if not dry_run:
            feature_ids.append(int(fid))
            reasons.append(reason)
    if feature_ids and not dry_run:
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
                client.post(
                    f"/api/products/{product_id}/sprints/blocked/route",
                    json={"feature_ids": feature_ids,
                          "reason": "Auto-routed: rapid status flap loop detected by supervisor"},
                )
        except Exception:
            log.exception(f"rapid_flap: blocked-route call failed for product {product_id}")
    return routed
