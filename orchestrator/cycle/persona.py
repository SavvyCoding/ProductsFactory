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

  determine_persona(product) -> str | None
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
from typing import Optional

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
        features_resp  = client.get(f"/api/products/{product_id}/features")
        sprint_resp    = client.get(f"/api/products/{product_id}/sprints/active")
        syscfg_resp    = client.get("/api/system-config")

        features = features_resp.json() if features_resp.is_success else []
        active_sprint = sprint_resp.json() if sprint_resp.is_success else None
        sys_cfg = syscfg_resp.json() if syscfg_resp.is_success else {}

        # No active sprint
        if active_sprint is None:
            unsprinted = [f for f in features
                          if f.get("status") == "Approved" and f.get("sprint_id") is None]
            if unsprinted:
                return {"action": "plan_sprints", "product_id": product_id,
                        "reason": f"{len(unsprinted)} Approved features have no sprint; call pm_api POST /api/products/{product_id}/plan-sprints"}
            # Backpressure: don't generate more features if backlog is already deep.
            # Counts ALL Approved features regardless of sprint, because planner
            # creates unsprinted Approved features — reaching cap means coder is
            # behind and making more ideas will just pile up more "Approved" work.
            all_approved = [f for f in features if f.get("status") == "Approved"]
            max_pending = (sys_cfg.get("max_pending_approved")
                           or int(os.environ.get("MAX_PENDING_APPROVED", "10")))
            if len(all_approved) >= max_pending:
                return {"action": "exit",
                        "reason": f"Planner gated: {len(all_approved)} Approved features already pending (cap {max_pending})"}
            return {"action": "launch_session", "persona": "planner",
                    "product_id": product_id, "reason": "No active sprint, no approved features — planner generates backlog"}

        sid = active_sprint["id"]
        sprint_features = [f for f in features if f.get("sprint_id") == sid]
        non_terminal = [f for f in sprint_features if f.get("status") not in _TERMINAL]

        if not non_terminal:
            # All features in the active sprint are merged. Run the post-sprint
            # regression chain — each persona launches as its own session so
            # the History tab shows a real audit trail of what verified the
            # sprint, and each can sign off its DoD gate independently:
            #   1. qa_tester        → runs full test suite on main, signs qa_passed
            #   2. security_auditor → audits merged code, signs security_clean
            #   3. check-dod auto-completes the sprint once both above are signed
            #   4. retrospective    → writes retro_sprint_<id>.md, signs retro_done
            #
            # Source of truth for gate state is /api/sprints/{id}/check-dod
            # (POST returns the live evaluation including the auto-recompute
            # rules from earlier today). The active_sprint payload from
            # /products/{id}/sprints/active also carries dod_status + status.
            try:
                cd = client.post(f"/api/sprints/{sid}/check-dod").json()
                # Skip-action paths ({"action":"skipped"}) return no `dod`
                # key — `cd.get("dod")` returns None then, not {}. `or {}`
                # collapses both Nones and missing keys into a safe empty
                # dict so subsequent dod.get(...) calls don't AttributeError.
                # Triggered when the post-sprint regression chain auto-
                # completes the sprint between this cycle's active_sprint
                # fetch and the check-dod POST.
                dod = (cd.get("dod") if isinstance(cd, dict) else None) or {}
            except Exception:
                cd, dod = {}, {}

            if not dod.get("qa_passed"):
                return {"action": "launch_session", "persona": "qa_tester",
                        "product_id": product_id,
                        "reason": f"sprint {sid}: all features Pushed — running QA regression to sign qa_passed"}
            if not dod.get("security_clean"):
                return {"action": "launch_session", "persona": "security_auditor",
                        "product_id": product_id,
                        "reason": f"sprint {sid}: all features Pushed — running security audit to sign security_clean"}

            # Both verification gates signed. check-dod above will have
            # auto-completed the sprint already if the structural gates pass
            # too (it returns action=auto_completed in that case).
            #
            # Retrospective writes retro_sprint_<id>.md, files action items,
            # and signs retro_done. Triggered on completed sprints with no
            # retro_doc_path yet. We use the active_sprint payload's status +
            # retro_doc_path here (active_sprint is fetched at the top of
            # _decide_action and is the freshest snapshot).
            sprint_status_now = active_sprint.get("status")
            retro_done_path   = active_sprint.get("retro_doc_path")
            if cd.get("action") == "auto_completed" or sprint_status_now == "completed":
                if not retro_done_path:
                    return {"action": "launch_session", "persona": "retrospective",
                            "product_id": product_id,
                            "reason": f"sprint {sid} completed — retrospective writing retro_sprint_{sid}.md"}
                return {"action": "exit", "reason": f"sprint {sid} fully signed off + retro done"}

            return {"action": "exit",
                    "reason": f"sprint {sid}: both gates signed but check-dod returned {cd.get('action','?')}"}

        reviewing = [f for f in non_terminal if f.get("status") == "Reviewing" and f.get("pr_number")]
        if reviewing:
            return {"action": "launch_session", "persona": "reviewer",
                    "product_id": product_id, "reason": f"{len(reviewing)} features in Reviewing with PR"}

        approved_no_design = [f for f in non_terminal
                              if f.get("status") == "Approved" and not f.get("design_doc_path")]
        if approved_no_design:
            # Phase-1 simplification (2026-05-06): product_planner merged into
            # designer. Both used to write near-identical per-feature docs to
            # /workspace/docs/ against the same candidate filter — duplicating
            # work and adding a handoff seam for no benefit. designer is now
            # the single canonical doc writer for Approved + no-doc features.
            # See futureplan.md.
            return {"action": "launch_session", "persona": "designer",
                    "product_id": product_id, "reason": f"{len(approved_no_design)} Approved features need design docs"}

        # Coder-eligible features:
        #   - Designed (fresh from designer)
        #   - Approved with design_doc_path (skip-design products)
        #   - Implementing + review_outcome=changes_requested
        #     (reviewer rejected, coder needs another pass — without this,
        #     these sit "stuck in agent state" for 45 min until reset_stuck
        #     drops them back to Designed, even though /next-for-persona?
        #     persona=coder already returns them)
        codeable = [f for f in non_terminal
                    if f.get("status") == "Designed"
                    or (f.get("status") == "Approved" and f.get("design_doc_path"))
                    or (f.get("status") == "Implementing"
                        and f.get("review_outcome") == "changes_requested")]
        if codeable:
            return {"action": "launch_session", "persona": "coder",
                    "product_id": product_id, "reason": f"{len(codeable)} features ready to code"}

        in_agent_stuck = [f for f in non_terminal if f.get("status") in _IN_AGENT]
        if in_agent_stuck:
            return {"action": "exit", "reason": f"{len(in_agent_stuck)} features stuck in agent state; reset_stuck will handle"}

        # Phase-1 supervisor: detector D — sprint all-Reviewed but no merge.
        # Compute last-activity ts from non_terminal updated_at; skip the
        # detector entirely if we can't (avoids a perpetual false-fire when
        # updated_at isn't serialized).
        try:
            from datetime import datetime as _dt
            from orchestrator.supervisor import detect_merge_stall  # type: ignore
            ts_strs = [f.get("updated_at") for f in non_terminal if f.get("updated_at")]
            last_activity_ts = None
            if ts_strs:
                parsed_ts = []
                for s in ts_strs:
                    try:
                        parsed_ts.append(_dt.fromisoformat(s.replace("Z", "+00:00")).timestamp())
                    except (ValueError, AttributeError):
                        pass
                if parsed_ts:
                    last_activity_ts = max(parsed_ts)
            if last_activity_ts is not None:
                detect_merge_stall(
                    product_id=product_id, sprint_id=sid,
                    sprint_features=sprint_features, last_merge_ts=last_activity_ts,
                )
        except Exception:
            log.exception("supervisor merge_stall detector failed")

        # Phase-1 supervisor: detector C — auto-plan when active sprint has
        # nothing actionable but unsprinted Approved features are piling up.
        # Detector POSTs /plan-sprints itself; we just exit this cycle.
        try:
            from orchestrator.supervisor import detect_auto_plan  # type: ignore
            unsprinted_approved = sum(
                1 for f in features
                if f.get("status") == "Approved" and f.get("sprint_id") is None
            )
            if detect_auto_plan(
                product_id=product_id,
                active_sprint_has_codeable=False,
                unsprinted_approved_count=unsprinted_approved,
            ):
                return {"action": "exit",
                        "reason": f"supervisor auto_plan triggered for product {product_id}"}
        except Exception:
            log.exception("supervisor auto_plan detector failed")

        return {"action": "exit", "reason": "No actionable work found"}

    except Exception as e:
        return {"action": "exit", "reason": f"_decide_action failed: {e}"}


def determine_persona(product: dict) -> Optional[str]:
    """
    Adapter for orchestrator.poller.

    Returns the persona name on ``action == "launch_session"`` and ``None``
    for ``exit`` / ``plan_sprints`` / unknown actions. Manages its own
    httpx.Client (plain — no PF-internal signing because the legacy poller
    runs on the host with the website assumed unsigned).

    Replaces the dispatch.py cascade that the poller path used pre-Phase 5.
    """
    pid = product.get("id")
    if pid is None:
        return None
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            result = _decide_action(pid, client)
    except Exception as e:
        log.error(f"determine_persona failed for product {pid}: {e}")
        return None
    if result.get("action") == "launch_session":
        return result.get("persona")
    return None
