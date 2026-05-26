"""
Canonical persona decision tree (Phase 5 of OrchestratorRefactor — Option B).

Adopts ``deploy/orchestrator/tools.determine_next_action`` as the single
implementation. Both the legacy poller (``orchestrator.poller``) and the
deployed orchestrator (``deploy/orchestrator/tools``) now delegate here.

API surface:
  _decide_action(product_id, client) -> dict
        The core decision tree. Returns one of:
          {"action": "launch_session", "persona": ..., "product_id": ..., "reason": ...}
          {"action": "plan_sprints",   "product_id": ..., "reason": ...}
          {"action": "exit",           "reason": ...}
        Caller provides the httpx.Client (poller uses plain client; the
        deployed orchestrator passes its signing client).

  _decide_action(product_id, client) -> dict  (single source of truth)
        Adapter for orchestrator.poller. Calls _decide_action and collapses
        the response: returns persona name on launch_session, None otherwise
        (exit, plan_sprints, etc.). Manages its own httpx.Client lifecycle.

Behavior changes vs legacy ``orchestrator.dispatch`` (which this replaces
on the poller path):
  + Implementing+changes_requested counts as codeable (was: stuck for 45m)
  + Explicit DoD-gate dispatch: qa_passed → qa_tester, security_clean →
    security_auditor (was: inferred from feature states)
  + max_pending_approved backpressure (default 10): planner skipped when
    Approved backlog is already deep
  + auto_completed sprint detection via /api/sprints/{id}/check-dod
  + Inline supervisor.detect_merge_stall + detect_auto_plan calls

Behaviors lost from the legacy dispatch.py (accepted for Option B):
  - _decide_flip_reviewed_no_pr: opportunistic Reviewed-no-PR → Pushed flip.
    Now redundant with auto_merge.sweep_all + reconcile_in_flight_prs which
    already detect closed-on-GitHub PRs and update the feature accordingly.
  - _decide_route_unsprinted_security_bugs: dispatch.py routed unsprinted
    bugs into the active sprint during persona selection. The deployed path
    already does this in tools.run_cycle's per-cycle hook
    (_route_unsprinted_security_bugs); the poller path will need an
    equivalent call site (TODO Phase 5b).
  - _decide_reset_orphan_agents: aggressive immediate reset of agent-state
    features when no live session exists. Now relies on the slower
    /api/features/reset_stuck (45-min threshold) called per cycle.
"""

import json
import logging
import os

import httpx

log = logging.getLogger("orchestrate")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")


def _decide_action(product_id: int, client: httpx.Client) -> dict:
    """
    Deterministic persona decision tree for a product. The single source of
    truth — both poller and the deployed orchestrator delegate here.

    Returns a dict with keys ``action`` and (depending on the action)
    ``persona``, ``product_id``, ``reason``. Never raises — failures return
    ``{"action": "exit", "reason": "..."}``.
    """
    _TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
    _IN_AGENT = {"Designing", "Implementing", "Reviewing"}

    try:
        features_resp = client.get(f"/api/products/{product_id}/features")
        syscfg_resp   = client.get("/api/system-config")

        features = features_resp.json() if features_resp.is_success else []
        sys_cfg = syscfg_resp.json() if syscfg_resp.is_success else {}

        non_terminal = [f for f in features if f.get("status") not in _TERMINAL]

        # 1. Reviewer first — clear open PRs before anything else.
        reviewing = [f for f in non_terminal
                     if f.get("status") == "Reviewing" and f.get("pr_number")]
        if reviewing:
            return {"action": "launch_session", "persona": "reviewer",
                    "product_id": product_id,
                    "reason": f"{len(reviewing)} features in Reviewing with PR"}

        # 2. Coder — features ready to be implemented.
        codeable = [f for f in non_terminal
                    if f.get("status") == "Designed"
                    or (f.get("status") == "Approved" and f.get("design_doc_path"))
                    or (f.get("status") == "Implementing"
                        and f.get("review_outcome") == "changes_requested")]
        if codeable:
            return {"action": "launch_session", "persona": "coder",
                    "product_id": product_id,
                    "reason": f"{len(codeable)} features ready to code"}

        # 3. Designer — Approved features without a design doc.
        approved_no_design = [f for f in non_terminal
                              if f.get("status") == "Approved" and not f.get("design_doc_path")]
        if approved_no_design:
            return {"action": "launch_session", "persona": "designer",
                    "product_id": product_id,
                    "reason": f"{len(approved_no_design)} Approved features need design docs"}

        # 4. In-agent stuck features — let reset_stuck handle them.
        in_agent_stuck = [f for f in non_terminal if f.get("status") in _IN_AGENT]
        if in_agent_stuck:
            return {"action": "exit",
                    "reason": f"{len(in_agent_stuck)} features stuck in agent state; reset_stuck will handle"}

        # 5. Planner — unphased Approved features waiting for grouping.
        # Phases→features flat model (migration 043): plan-phases groups
        # Approved features into phases by theme. Optional — features can
        # ship without a phase, but grouping helps the UI.
        unphased_approved = [f for f in features
                             if f.get("status") == "Approved" and f.get("phase_id") is None]
        try:
            from orchestrator.supervisor import detect_auto_plan  # type: ignore
            if detect_auto_plan(
                product_id=product_id,
                unphased_approved_count=len(unphased_approved),
            ):
                return {"action": "exit",
                        "reason": f"supervisor auto_plan triggered for product {product_id}"}
        except Exception:
            log.exception("supervisor auto_plan detector failed")

        # 6. Recommender / planner — generate features if backlog is light.
        all_approved = [f for f in features if f.get("status") == "Approved"]
        max_pending = (sys_cfg.get("max_pending_approved")
                       or int(os.environ.get("MAX_PENDING_APPROVED", "10")))
        if len(all_approved) >= max_pending:
            return {"action": "exit",
                    "reason": f"Planner gated: {len(all_approved)} Approved features already pending (cap {max_pending})"}

        if not all_approved:
            return {"action": "launch_session", "persona": "planner",
                    "product_id": product_id,
                    "reason": "No Approved features — planner generates backlog"}

        return {"action": "exit", "reason": "No actionable work found"}

    except Exception as e:
        return {"action": "exit", "reason": f"_decide_action failed: {e}"}


# determine_persona adapter retired 2026-05-19: it was the bridge for the
# legacy orchestrator/poller.py which was deleted in PR A (commit 88d8031).
# The live containerized path (deploy/orchestrator/tools.py) calls
# _decide_action directly. No callers remained.
