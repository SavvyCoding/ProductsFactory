"""
Final reconcile pass after a session container exits.

Two responsibilities:
  - _reconcile_session_result: re-apply every entry in session_result.json
    (idempotent with the live-poll thread), filter out entries that violate
    the reviewer/coder contract, then delete the file.
  - _rollback_stuck_features: reset features that were claimed but never
    completed (no PR, no session_result entry). Enforces INVARIANTS V.2/V.5.

Extracted from docker_runner.py during Phase 1 of OrchestratorRefactor.
"""

import logging
import os

import httpx

from orchestrator.session.state_machine import _apply_session_entry
from orchestrator.session.result_io import _read_session_result, _delete_session_result

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


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
                        # Pass changed_by="rollback" alongside the website's
                        # /api/features/{id} bypass list. NOTE: the website's
                        # FeatureUpdate Pydantic schema currently doesn't
                        # declare changed_by, so it's stripped before the
                        # rank-guard handler sees it (website bug). Until that
                        # schema is fixed, status downgrades (Implementing
                        # → Designed) will return 422 here. We pass it anyway
                        # so the moment the schema is fixed, this works without
                        # another orchestrator change.
                        #
                        # Until then: the supervisor.detect_kill_recovery path
                        # bumps fix_attempts (which doesn't trigger the rank
                        # guard, since fix_attempts isn't a status change), and
                        # _route_to_blocked_if_at_cap routes the feature to the
                        # Blocked sprint when fix_attempts hits the cap. So the
                        # loop terminates via the Blocked path, just slower
                        # than via this rollback.
                        #
                        # Real incident 2026-05-06: 5 features stuck after
                        # Ollama exit=2 storm. Rolled-back log appeared but
                        # every PATCH was actually a 422 — the explicit log
                        # below is what made it visible.
                        r = client.patch(
                            f"/api/features/{f['id']}",
                            json={"status": reset_to, "changed_by": "rollback"},
                        )
                        if r.status_code >= 300:
                            log.warning(
                                f"Could not roll back feature #{f['id']} "
                                f"'{f['name']}' {f['status']} -> {reset_to}: "
                                f"HTTP {r.status_code} {r.text[:200]} "
                                f"(supervisor.detect_kill_recovery will bump "
                                f"fix_attempts → Blocked sprint at cap)"
                            )
                            continue
                        log.info(f"Rolled back feature #{f['id']} '{f['name']}' {f['status']} -> {reset_to}")
                        rolled_back += 1
                    except Exception as fe:
                        log.warning(f"Could not roll back feature #{f['id']}: {fe}")
            if rolled_back:
                log.info(f"Rolled back {rolled_back} feature(s) for product {product_id}")
    except Exception as e:
        log.warning(f"Could not rollback stuck features for product {product_id}: {e}")


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
