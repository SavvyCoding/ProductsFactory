"""
Per-product persona decision tree — the single source of truth for "what
should this product do next."

`deploy/orchestrator/tools.determine_next_action` wraps `_decide_action`
and supplies a signing httpx.Client; tools.run_cycle calls into here
after its Priority 0/1 trainer+reviewer-preempt branches resolve the
product to look at.

Returns one of:
    {"action": "launch_session", "persona": ..., "product_id": ..., "reason": ...}
    {"action": "exit",           "reason": ...}

Behavior notes:
  - Phase planner runs at step 0 (before reviewer/coder/designer) whenever
    any Approved feature is unphased. Restores pre-fe54264 semantics: phases
    are planned eagerly, not after the designer drains the Approved backlog.
    The 4h per-product cooldown inside detect_auto_plan prevents thrash.
  - Implementing+changes_requested counts as codeable immediately (no
    45-minute stuck timer needed — the reviewer explicitly bounced it).
  - max_pending_approved backpressure (default 10): planner skipped when
    the Approved backlog is already deep, so the system completes
    pending work before adding more.
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

        # 0. Phase planner — plan whenever any Approved feature is unphased.
        # Restores pre-fe54264 semantics: phase planning runs eagerly, not
        # after the designer drains the backlog. detect_auto_plan still
        # honors its 4h per-product cooldown so a transient plan-phases
        # failure doesn't get retried every cycle.
        unphased_approved = [f for f in features
                             if f.get("status") == "Approved" and f.get("phase_id") is None]
        if unphased_approved:
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

        # 1. Reviewer first — clear open PRs before anything else.
        reviewing = [f for f in non_terminal
                     if f.get("status") == "Reviewing" and f.get("pr_number")]
        if reviewing:
            return {"action": "launch_session", "persona": "reviewer",
                    "product_id": product_id,
                    "reason": f"{len(reviewing)} features in Reviewing with PR"}

        # 2a. Rework coder — Implementing+changes_requested is the most
        # time-sensitive coder work: a reviewer or post-coder gate just
        # bounced the feature back, and the rework workspace already
        # carries the prior implementation (since 80f42e3). Run these
        # before anything that competes for the coder.
        #
        # Cycle DM-2 (2026-06-01): rework features near the cap (fix_
        # attempts >= REWORK_CAP_PROXIMITY) LOSE preemption priority.
        # Canonical incident: DocumentSign #1178 cycled at fix_attempts
        # 3→4→5 because the design doc required a runtime tool absent
        # from the agent image. Each rework cycle monopolized the coder
        # (always 2a preempted) while 30+ healthy fresh-codeable features
        # sat idle. Doomed reworks should compete with fresh work, not
        # block it — the rework still gets attention via the first-pass
        # codeable pool below (which already accepts Implementing+
        # changes_requested in its filter), it just no longer preempts.
        # 2 more attempts either complete or cap-Block; meanwhile fresh
        # features get throughput.
        _REWORK_CAP_PROXIMITY = 3
        rework_codeable = [f for f in non_terminal
                           if f.get("status") == "Implementing"
                           and f.get("review_outcome") == "changes_requested"
                           and (f.get("fix_attempts") or 0) < _REWORK_CAP_PROXIMITY]
        if rework_codeable:
            return {"action": "launch_session", "persona": "coder",
                    "product_id": product_id,
                    "reason": f"{len(rework_codeable)} rework features (changes_requested)"}

        # 2b/2c. Designer vs fresh-coder — balance by queue depth.
        # Whichever backlog is larger goes first; if only one has work,
        # it goes alone. This is the stable balancing rule that avoids
        # BOTH starvation modes the system has hit:
        #
        #   - 2026-06-01 cycle H: coder-first starved the designer.
        #     41 Approved features waited for design while the coder
        #     cycled 10 sessions on a handful of pre-designed features.
        #     0 features pushed for ~90 minutes.
        #   - 2026-06-01 cycle DD: designer-first starved the coder.
        #     44 Designed features ready to code, 9 Approved features
        #     needing design — designer monopolized every cycle for
        #     ~1.5 hours, ZERO coder runs, nothing reaching Reviewing,
        #     nothing merging. User-reported as "nothing has been
        #     shipped in last many hours."
        #
        # Naive priority orderings (designer-first OR coder-first) are
        # unstable: whichever persona is faster monopolizes; the slower
        # persona's queue grows unbounded. Balancing by depth keeps both
        # queues bounded — when one queue is larger, it runs; equilibrium
        # is when both are roughly equal.
        approved_no_design = [f for f in non_terminal
                              if f.get("status") == "Approved" and not f.get("design_doc_path")]
        first_pass_codeable = [f for f in non_terminal
                               if f.get("status") == "Designed"
                               or (f.get("status") == "Approved"
                                   and f.get("design_doc_path"))
                               # Cycle DM-2 (2026-06-01): rework features
                               # at/near the cap also land here so they
                               # compete fairly with fresh work via the
                               # queue-depth balancer instead of preempting.
                               or (f.get("status") == "Implementing"
                                   and f.get("review_outcome") == "changes_requested"
                                   and (f.get("fix_attempts") or 0) >= _REWORK_CAP_PROXIMITY)]

        if approved_no_design and first_pass_codeable:
            # Both queues have work: run whichever is deeper. Ties go
            # to coder so PRs reach Reviewing and merging — the system's
            # ultimate throughput metric.
            if len(first_pass_codeable) >= len(approved_no_design):
                return {"action": "launch_session", "persona": "coder",
                        "product_id": product_id,
                        "reason": (
                            f"{len(first_pass_codeable)} features ready to code "
                            f"(first-pass; deeper queue than designer's "
                            f"{len(approved_no_design)} Approved)"
                        )}
            return {"action": "launch_session", "persona": "designer",
                    "product_id": product_id,
                    "reason": (
                        f"{len(approved_no_design)} Approved features need design docs "
                        f"(deeper queue than coder's "
                        f"{len(first_pass_codeable)} first-pass)"
                    )}
        if approved_no_design:
            return {"action": "launch_session", "persona": "designer",
                    "product_id": product_id,
                    "reason": f"{len(approved_no_design)} Approved features need design docs"}
        if first_pass_codeable:
            return {"action": "launch_session", "persona": "coder",
                    "product_id": product_id,
                    "reason": f"{len(first_pass_codeable)} features ready to code (first-pass)"}

        # 4. In-agent stuck features — let reset_stuck handle them.
        in_agent_stuck = [f for f in non_terminal if f.get("status") in _IN_AGENT]
        if in_agent_stuck:
            return {"action": "exit",
                    "reason": f"{len(in_agent_stuck)} features stuck in agent state; reset_stuck will handle"}

        # 5. Recommender / planner — generate features if backlog is light.
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
