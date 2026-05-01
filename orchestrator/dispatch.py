"""Priority-list persona dispatcher — Phase 2 of PollerRevamp.

Replaces the nested if/elif cascade in poller.determine_persona() with
an ordered list of decisions. Each decision returns one of:

  - a persona name (str): launch this persona
  - DONE (sentinel): a side-effect was performed; no persona this cycle
  - None: this decision doesn't apply, try the next

The dispatcher walks the list in order and returns the first non-None
result. Behavior is identical to the legacy determine_persona at HEAD
of master, except the inline auto-merge path (legacy lines 655-722) is
removed because Phase 1's auto_merge.sweep_all already merges every
Reviewed+approved+pr_number feature per cycle (see INVARIANTS.md VII.1).

Each decision function is small enough to test in isolation. Adding a
new orchestration rule is one entry in ACTIVE_SPRINT_DECISIONS or
NO_SPRINT_DECISIONS — no surgery on a 220-line cascade required.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

import httpx

log = logging.getLogger("dispatch")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")

TERMINAL = frozenset({"Pushed", "Deferred", "Rejected", "Reverted"})
AGENT_STATES = frozenset({"Designing", "Implementing", "Reviewing"})


# Sentinel for "side-effect performed; caller should return None (no persona)".
class _Done:
    __slots__ = ()
    def __repr__(self) -> str: return "DONE"
DONE = _Done()

DecisionResult = Union[str, _Done, None]


# ── Context ──────────────────────────────────────────────────────────────────

@dataclass
class Context:
    """One-shot snapshot of every datum a decision might need.
    Built at the top of determine_persona; passed by reference to each decision.
    """
    product: dict
    client: httpx.Client
    active_sprint: Optional[dict]
    all_sprints: list[dict]
    completed_sprints: list[dict]
    last_completed_sprint: Optional[dict]
    completed_no_retro: list[dict]
    features: list[dict]

    @property
    def sprint_features(self) -> list[dict]:
        """Features assigned to the active sprint (empty list if no active sprint)."""
        if not self.active_sprint:
            return []
        sid = self.active_sprint.get("id")
        return [f for f in self.features if f.get("sprint_id") == sid]


def _build_context(product: dict, client: httpx.Client) -> Optional[Context]:
    """Fetch sprints + features + active sprint in three calls. Returns None on
    PM API failure (caller should treat as 'no actionable work this cycle')."""
    pid = product["id"]
    try:
        active_resp = client.get(f"/api/products/{pid}/sprints/active")
        active_sprint = active_resp.json() if active_resp.status_code == 200 and active_resp.json() else None

        sprints_resp = client.get(f"/api/products/{pid}/sprints")
        all_sprints = sprints_resp.json() if sprints_resp.status_code == 200 else []
        if not isinstance(all_sprints, list):
            all_sprints = []

        feat_resp = client.get(f"/api/products/{pid}/features")
        features = feat_resp.json() if feat_resp.status_code == 200 else []
        if not isinstance(features, list):
            features = []
    except httpx.HTTPError as e:
        log.error(f"_build_context HTTP error: {e}")
        return None

    completed = [s for s in all_sprints if s.get("status") == "completed"]
    last_completed = max(completed, key=lambda s: s["id"]) if completed else None
    completed_no_retro = [s for s in completed if not s.get("retro_doc_path")]

    return Context(
        product=product,
        client=client,
        active_sprint=active_sprint,
        all_sprints=all_sprints,
        completed_sprints=completed,
        last_completed_sprint=last_completed,
        completed_no_retro=completed_no_retro,
        features=features,
    )


# ── Decisions: active-sprint path ────────────────────────────────────────────

def _decide_complete_sprint(ctx: Context) -> DecisionResult:
    """All sprint features terminal: run retro if needed, else force-complete.
    Mirrors legacy determine_persona poller.py:603-623."""
    sf = ctx.sprint_features
    non_terminal = [f for f in sf if f.get("status") not in TERMINAL]
    if not sf or non_terminal:
        return None  # Sprint not ready to complete

    sid = ctx.active_sprint["id"]
    if not ctx.active_sprint.get("retro_doc_path"):
        log.info(f"Active sprint {sid}: all features terminal — running retrospective first")
        return "retrospective"

    log.info(f"Active sprint {sid}: retro complete, completing sprint")
    try:
        complete_resp = ctx.client.post(f"/api/sprints/{sid}/force-complete")
        if complete_resp.status_code == 200:
            result = complete_resp.json()
            log.info(f"Sprint {sid} completed via API: {result.get('action')}")
            if result.get("release_notes"):
                log.info(f"Release notes generated ({len(result['release_notes'])} chars)")
    except Exception as e:
        log.warning(f"Force-complete sprint {sid} failed: {e}")

    working_dir = ctx.product.get("working_dir")
    if working_dir:
        # Lazy import to avoid circular dep with poller.py
        from orchestrator.poller import _clear_sprint_context
        _clear_sprint_context(working_dir)
    return DONE


def _decide_product_planner(ctx: Context) -> DecisionResult:
    """Approved features in the active sprint with no design_doc_path → product_planner."""
    needs_design = [
        f for f in ctx.sprint_features
        if f.get("status") == "Approved" and not f.get("design_doc_path")
    ]
    return "product_planner" if needs_design else None


def _decide_coder(ctx: Context) -> DecisionResult:
    """Designed OR (Approved + design_doc_path) features in active sprint → coder."""
    codable = [
        f for f in ctx.sprint_features
        if f.get("status") == "Designed"
        or (f.get("status") == "Approved" and f.get("design_doc_path"))
    ]
    return "coder" if codable else None


def _decide_reviewer(ctx: Context) -> DecisionResult:
    """Reviewing features with a pr_number → reviewer."""
    reviewable = [
        f for f in ctx.sprint_features
        if f.get("status") == "Reviewing" and f.get("pr_number")
    ]
    return "reviewer" if reviewable else None


def _decide_flip_reviewed_no_pr(ctx: Context) -> DecisionResult:
    """Reviewed features with no pr_number → Pushed (PR was already merged/closed externally).
    Pure side-effect; returns None so the next decision in the list runs."""
    flipped = 0
    for f in ctx.sprint_features:
        if f.get("status") == "Reviewed" and not f.get("pr_number"):
            try:
                ctx.client.patch(f"/api/features/{f['id']}", json={"status": "Pushed"})
                log.info(f"Feature #{f['id']}: Reviewed with no PR -> Pushed")
                flipped += 1
            except Exception:
                pass
    # Return None regardless: this is opportunistic cleanup, not a stopping condition.
    return None


def _decide_reset_orphan_agents(ctx: Context) -> DecisionResult:
    """Features in agent states (Designing/Implementing/Reviewing) but no live
    container session → reset to prior ready state. Mirrors legacy poller.py:731-742."""
    in_agent = [f for f in ctx.sprint_features if f.get("status") in AGENT_STATES]
    if not in_agent:
        return None

    try:
        active_session = ctx.client.get(
            "/api/sessions/active",
            params={"product_id": ctx.product["id"]},
        )
        has_active = active_session.status_code == 200 and active_session.json()
    except Exception:
        has_active = True  # Fail closed: if we can't check, assume agent is running.

    if has_active:
        sid = ctx.active_sprint["id"]
        log.info(f"Active sprint {sid}: {len(in_agent)} features being processed by agents - waiting")
        return DONE

    sid = ctx.active_sprint["id"]
    log.info(f"Active sprint {sid}: {len(in_agent)} features in agent states but no active session - resetting")
    for f in in_agent:
        reset_to = "Designed" if f.get("design_doc_path") else "Approved"
        try:
            ctx.client.patch(f"/api/features/{f['id']}", json={"status": reset_to})
            log.info(f"  Feature #{f['id']} {f['status']} -> {reset_to} (no active session)")
        except Exception:
            pass
    return DONE


def _decide_log_pending_or_waiting(ctx: Context) -> DecisionResult:
    """Final fallthrough for the active-sprint path. Logs and returns DONE.
    Distinguishes 'PM-approval pending' from 'in-flight, just waiting'."""
    sf = ctx.sprint_features
    sid = ctx.active_sprint["id"]
    pending = [f for f in sf if f.get("status") == "Pending"]
    in_agent = [f for f in sf if f.get("status") in AGENT_STATES]

    if pending and not in_agent:
        log.info(f"Active sprint {sid}: {len(pending)} Pending features awaiting PM approval - nothing for agents to do")
    else:
        non_terminal = [f for f in sf if f.get("status") not in TERMINAL]
        log.info(f"Active sprint {sid}: {len(non_terminal)} features in other states - waiting")
    return DONE


# ── Decisions: no-active-sprint path ─────────────────────────────────────────

def _decide_auto_create_sprint(ctx: Context) -> DecisionResult:
    """If unsprinted Approved features exist, auto-create a sprint.
    Delegates to poller._auto_create_sprint_for_unsprinted (kept in poller.py
    to avoid circular imports and because it's a pure persistence helper)."""
    from orchestrator.poller import _auto_create_sprint_for_unsprinted
    if _auto_create_sprint_for_unsprinted(ctx.product, ctx.client):
        return DONE  # Next cycle picks up the new active sprint
    return None


def _decide_retrospective_for_completed(ctx: Context) -> DecisionResult:
    """If a completed sprint has no retro_doc_path, run retrospective."""
    if not ctx.completed_no_retro:
        return None
    most_recent = max(ctx.completed_no_retro, key=lambda s: s["id"])
    log.info(f"Sprint {most_recent['id']} completed without retrospective — running retro")
    return "retrospective"


def _decide_post_sprint_persona(ctx: Context) -> DecisionResult:
    """Documenter / analytics / refactorer / devops / recommender — schedule-gated."""
    from orchestrator.poller import _post_sprint_persona_due
    persona = _post_sprint_persona_due(ctx.product, ctx.last_completed_sprint)
    if persona:
        last_sprint_id = ctx.last_completed_sprint.get("id") if ctx.last_completed_sprint else None
        log.info(f"Post-sprint persona due: {persona} (last sprint: {last_sprint_id})")
        return persona
    return None


def _decide_planner_fallback(ctx: Context) -> DecisionResult:
    """Last resort: nothing else to do, generate new feature ideas."""
    log.info(f"No active sprint and no approved unsprinted features for product {ctx.product['id']}")
    return "planner"


# ── Decision lists ───────────────────────────────────────────────────────────
# Order matters. First decision returning a non-None result wins.

ACTIVE_SPRINT_DECISIONS: list[Callable[[Context], DecisionResult]] = [
    _decide_complete_sprint,
    _decide_product_planner,
    _decide_coder,
    _decide_reviewer,
    _decide_flip_reviewed_no_pr,    # opportunistic side-effect, always returns None
    _decide_reset_orphan_agents,
    _decide_log_pending_or_waiting,  # always returns DONE
]

NO_SPRINT_DECISIONS: list[Callable[[Context], DecisionResult]] = [
    _decide_auto_create_sprint,
    _decide_retrospective_for_completed,
    _decide_post_sprint_persona,
    _decide_planner_fallback,
]


# ── Public entry point ───────────────────────────────────────────────────────

def determine_persona(product: dict) -> Optional[str]:
    """Decide which persona should run for this product, or None if nothing.

    Drop-in replacement for poller.determine_persona. Same return contract:
    a persona name string to launch, or None to skip this product this cycle.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            ctx = _build_context(product, client)
            if ctx is None:
                return None

            decisions = ACTIVE_SPRINT_DECISIONS if ctx.active_sprint else NO_SPRINT_DECISIONS
            for decision in decisions:
                try:
                    result = decision(ctx)
                except Exception:
                    log.exception(f"decision {decision.__name__} crashed")
                    continue
                if result is DONE:
                    return None
                if isinstance(result, str):
                    return result
                # None → try next decision
    except httpx.HTTPError as e:
        log.error(f"determine_persona failed: {e}")
        return None

    return None
