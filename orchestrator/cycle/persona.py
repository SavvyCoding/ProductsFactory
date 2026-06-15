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

        # Keystone dependency gate (wave-10): resolve depends_on against the
        # RAW list — including infra stories — BEFORE they're stripped below,
        # so a story that depends_on an infra/predecessor feature still finds
        # it. dependency_blocked_feature_ids returns ids whose depends_on
        # hasn't shipped (status != Pushed); those are frozen from the
        # coder/designer pools so vertical slices sequence instead of racing.
        dep_blocked_ids: set = set()
        try:
            from orchestrator.cycle.dependencies import dependency_blocked_feature_ids
            dep_blocked_ids = dependency_blocked_feature_ids(features)
        except Exception:
            log.exception("dependency-gate read failed; proceeding ungated")

        # Infra stories (feature_type='infra') are system-executed by the
        # cycle loop (tools._execute_infra_stories) — never coder/designer/
        # planner work. Strip them from the decision pool entirely. The
        # sibling filter lives in docker_runner._fetch_assigned_features
        # (same two-enforcement-points discipline as the phase gate).
        features = [f for f in features if f.get("feature_type") != "infra"]

        non_terminal = [f for f in features if f.get("status") not in _TERMINAL]

        # Human-in-loop phase gate (migration 045). Opt-in per product via
        # config.human_gate_phases; fully no-op (empty set) otherwise, so the
        # decision tree below is byte-for-byte the autonomous behavior when the
        # flag is off. When on: features in phases ordered AFTER the current
        # gating phase (the lowest-order phase not yet 'approved') are FROZEN —
        # excluded from the coder/designer/rework dispatch pools — until a human
        # approves the gating phase. Reviewer (step 1) and the planner (step 0)
        # are intentionally left unfiltered: the planner must keep phasing the
        # backlog, and any in-flight PR should still be reviewable.
        gated_out_ids: set = set()
        try:
            prod_resp = client.get(f"/api/products/{product_id}")
            prod_cfg = (prod_resp.json().get("config") or {}) if prod_resp.is_success else {}
            if prod_cfg.get("human_gate_phases", True) is not False:  # ON by default
                from orchestrator.cycle.phase_gate import gated_out_feature_ids
                ph_resp = client.get(f"/api/products/{product_id}/phases")
                phases = ph_resp.json() if ph_resp.is_success else []
                gated_out_ids = gated_out_feature_ids(features, phases, prod_cfg)
        except Exception:
            log.exception("phase-gate read failed; proceeding ungated")

        # Frozen = phase-gated OR dependency-blocked. Both freeze a feature out
        # of the coder/designer pools; the union is what the pool filters below
        # consult so the two reasons stay in one place.
        frozen_ids: set = set(gated_out_ids) | set(dep_blocked_ids)
        if dep_blocked_ids:
            log.info(f"[dependency-gate] product {product_id}: "
                     f"{len(dep_blocked_ids)} feature(s) held — depends_on not yet Pushed")

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
                           and (f.get("fix_attempts") or 0) < _REWORK_CAP_PROXIMITY
                           and f.get("id") not in frozen_ids]
        if rework_codeable:
            return {"action": "launch_session", "persona": "coder",
                    "product_id": product_id,
                    "reason": f"{len(rework_codeable)} rework features (changes_requested)"}

        # 2b/2c. Strict priority: first-pass coder > designer.
        #
        # The system's throughput metric is Pushed-features-per-hour,
        # which is downstream of the CODER. Designer just primes the
        # pump — Approved-without-design is a near-zero-cost backlog
        # row, but Designed-without-being-coded is wasted work (the
        # design doc goes stale, sibling features ship in between,
        # conflicts emerge at merge time). So bias all dispatch toward
        # draining toward Pushed; refill the Designed queue only when
        # it would otherwise empty.
        #
        # History — two prior orderings failed:
        #   - 2026-06-01 cycle H: coder-first starved designer
        #     because a rework loop monopolized the coder (same
        #     feature bounced lint-guard repeatedly, step 2a kept
        #     re-claiming it).
        #   - 2026-06-01 cycle DD: designer-first starved coder
        #     when Approved backlog stayed large.
        # Both were patched at the dispatcher layer with balance-by-
        # queue-depth. That was a workaround for the cycle-H bounce-
        # loop bug — which is now bounded by independent guards:
        #   * IU gate (cycle JV fix E + cycle KI fix F): ONE PR per
        #     product at a time. The coder can only rework the open
        #     PR's feature; it cannot cycle across multiple features.
        #   * Cycle DM-2 deprioritization: fa ≥ 3 rework falls out
        #     of step 2a and joins first_pass_codeable.
        #   * Cap-Block circuit (fa = 5): bounded rework attempts.
        # Together these prevent a rework loop from running more than
        # ~5 sessions on any single feature — the cycle-H failure mode
        # is no longer reachable.
        #
        # So strict priority is safe to restore here. Coder runs whenever
        # there's first-pass work the IU gate permits; designer fills
        # in when the coder is gated or has no work.
        approved_no_design = [f for f in non_terminal
                              if f.get("status") == "Approved" and not f.get("design_doc_path")
                              and f.get("id") not in frozen_ids]
        first_pass_codeable = [f for f in non_terminal
                               if (f.get("status") == "Designed"
                                   or (f.get("status") == "Approved"
                                       and f.get("design_doc_path"))
                                   # Cycle DM-2 (2026-06-01): rework features
                                   # at/near the cap also land here so they
                                   # compete fairly with fresh work via the
                                   # queue-depth balancer instead of preempting.
                                   or (f.get("status") == "Implementing"
                                       and f.get("review_outcome") == "changes_requested"
                                       and (f.get("fix_attempts") or 0) >= _REWORK_CAP_PROXIMITY))
                               and f.get("id") not in frozen_ids]

        # Cycle IU (2026-06-02): open-PR serialization gate. Only one
        # coder session PR open per product at a time. If any feature for
        # this product already has pr_number set and is not yet terminal
        # (Pushed/Rejected/Deferred/Reverted/Blocked), the system already
        # has an in-flight session PR — claiming a fresh first-pass
        # feature here would open a 2nd PR.
        #
        # This is the actual root cause of the merge-conflict cap-Block
        # cascade (canonical 2026-06-02 DocumentSign #1074 cycle GW and
        # #1223 cycles II/IP): coder kept claiming fresh Designed
        # features while 1223's PR #326 was in rework, which let #324/
        # #325/#327 merge to main and move the base branch forward.
        # By the time the reviewer approved 1223, its branch was three
        # merges behind main → auto-merge 405 conflicts → status revert
        # Reviewed→Implementing → fix_attempts++ → cap-Block.
        #
        # With this gate, only ONE in-flight PR exists per product at any
        # moment. Sibling merges can't move main forward while a PR is in
        # rework. The PR ships or gets Blocked before the next one opens.
        #
        # Rework (step 2a above) still runs because it reuses the
        # existing PR — no 2nd PR risk. Reviewer (step 1) still runs.
        # Designer still runs because designs don't carry PRs. The gate
        # is narrow: only first-pass coder claims that would open a NEW
        # PR get blocked.
        #
        # The trade-off is lower parallel throughput per product, but
        # it removes the entire class of stale-branch cap-Blocks. Net
        # throughput should improve because no features get killed by
        # infrastructure-side branch staleness.
        #
        # Cycle JV (2026-06-03): the gate used to filter on a fixed
        # _PR_OPEN_STATUSES = {Implementing, Implemented, Reviewing,
        # Reviewed}. That set missed Designed / Approved / Designing —
        # which is the state a feature lands in when drift-scanner
        # auto-heal clears its design_doc_path mid-cycle, or when the
        # reconciler resets a Reviewed-but-conflict-blocked feature
        # back. Canonical incidents (all ended cap-Blocked despite
        # having open PRs): DocumentSign #1130 (cycle JH), #1131
        # (cycle JQ), MyTracking #1261 (cycle JU). In each case the
        # feature had pr_number set + a live PR on GitHub, but its
        # transient status was Designed at the moment the dispatcher
        # ran — so the gate count saw 0, a fresh coder claim
        # succeeded, sibling PRs merged ahead, the original PR went
        # stale, auto-merge 405'd, fix_attempts hit 5, cap-Block.
        #
        # Fix: trust pr_number, not status. Any non-terminal feature
        # with a pr_number IS an in-flight session PR regardless of
        # its momentary status. Exclude Blocked defensively (the
        # terminal-PR-closer should have cleared pr_number on Block,
        # but a stale row would otherwise gate the dispatcher forever).
        open_session_pr_count = sum(
            1 for f in non_terminal
            if f.get("pr_number") and f.get("status") != "Blocked"
        )

        # Cycle KD-KH (2026-06-03): Fix E correctly closed the stale-
        # branch cap-Block escape (1130/1131/1261), but accidentally
        # blocks the rework-recovery path. When reset_stuck moves a
        # near-cap rework feature from Implementing → Designed while
        # preserving pr_number, fix E now counts that pr_number against
        # the gate. The feature cannot be claimed for rework — even
        # though claiming would force-push to the EXISTING PR (no 2nd
        # PR opened). Result: feature self-deadlocks. Canonical
        # incidents: DocumentSign #1136 + MyTracking #1262 (both
        # 2026-06-03 cycle KD), idle for hours waiting on coder.
        #
        # Refinement: distinguish first_pass candidates that would
        # REUSE an existing PR (have pr_number) from those that would
        # OPEN a new one (no pr_number). Only the latter would violate
        # the 1-PR-per-product invariant. If at least one rework-
        # eligible candidate exists, the gate doesn't block — downstream
        # _fetch_assigned_features picks the rework path safely.
        first_pass_rework = [f for f in first_pass_codeable if f.get("pr_number")]

        # Strict priority: coder runs whenever first_pass_codeable has
        # work AND the IU gate permits. The gate permits when either:
        #   (a) no open session PR exists for this product, OR
        #   (b) at least one first_pass candidate has its own pr_number
        #       set (rework path — reuses the existing PR, no 2nd PR
        #       opened). See cycle KI fix F for the rework-aware refinement.
        coder_gated_by_open_pr = (
            open_session_pr_count >= 1 and not first_pass_rework
        )
        if first_pass_codeable and not coder_gated_by_open_pr:
            return {"action": "launch_session", "persona": "coder",
                    "product_id": product_id,
                    "reason": (
                        f"{len(first_pass_codeable)} features ready to code "
                        f"(first-pass; strict priority over designer's "
                        f"{len(approved_no_design)} Approved"
                        + (f"; {len(first_pass_rework)} rework-eligible"
                           if first_pass_rework and open_session_pr_count >= 1
                           else "")
                        + ")"
                    )}
        # Designer — coder is either out of work or gated by an open PR
        # whose rework candidate isn't in first_pass_codeable.
        if approved_no_design:
            return {"action": "launch_session", "persona": "designer",
                    "product_id": product_id,
                    "reason": (
                        f"{len(approved_no_design)} Approved features need design docs"
                        + (
                            f" (coder gated by {open_session_pr_count} open session PR(s))"
                            if coder_gated_by_open_pr and first_pass_codeable
                            else (
                                ""
                                if not first_pass_codeable
                                else f" (no first-pass work; designer drains backlog)"
                            )
                        )
                    )}
        if first_pass_codeable:
            # No designer work, the coder is gated, AND no rework-
            # eligible candidate exists — let the in-flight PR
            # finish before claiming another first-pass.
            if open_session_pr_count >= 1 and not first_pass_rework:
                return {"action": "exit",
                        "reason": (
                            f"PR-serialization gate: {open_session_pr_count} open session "
                            f"PR(s) in flight; deferring {len(first_pass_codeable)} first-pass "
                            f"feature(s) until the open PR ships or Blocks"
                        )}
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
