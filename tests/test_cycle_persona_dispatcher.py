"""Tests for orchestrator.cycle.persona._decide_action.

Priority ordering (2026-06-03 onward — strict): reviewer > rework <fa3 >
first-pass coder > designer. The first-pass branch runs whenever the
IU gate permits, regardless of designer-queue depth. Designer fills in
when coder has no work or is PR-gated. Replaces the prior balance-by-
queue-depth heuristic; the cycle-H bounce-loop failure mode that
motivated the balance is now bounded by IU gate + cycle DM-2 + cap-Block.
"""
from __future__ import annotations

import os

# persona module reads PM_API_URL at import time defensively; match the
# project convention.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from unittest.mock import MagicMock
from types import SimpleNamespace

from orchestrator.cycle.persona import _decide_action


def _client_for_features(features: list[dict], sys_cfg: dict | None = None):
    """Build a MagicMock httpx client that returns the given features list
    for /api/products/{id}/features and an empty sys_cfg by default."""
    client = MagicMock()
    sys_cfg = sys_cfg or {}

    def _get(url, *a, **kw):
        if "/features" in url:
            return SimpleNamespace(
                is_success=True, status_code=200, json=lambda: features,
            )
        if "/system-config" in url:
            return SimpleNamespace(
                is_success=True, status_code=200, json=lambda: sys_cfg,
            )
        return SimpleNamespace(
            is_success=False, status_code=404, json=lambda: {},
        )

    client.get.side_effect = _get
    return client


def _feature(fid, status, **kw):
    base = {
        "id": fid,
        "status": status,
        "phase_id": 1,  # avoid the auto-plan branch
        "design_doc_path": None,
        "pr_number": None,
        "review_outcome": None,
    }
    base.update(kw)
    return base


class TestDispatcherBalance:
    def test_deeper_coder_queue_runs_coder(self):
        """44 Designed features + 9 Approved-no-design: coder should win.
        This is the canonical cycle DD starvation case."""
        features = (
            # 44 first-pass-codeable
            [_feature(1000 + i, "Designed", design_doc_path=f"docs/{1000+i}.md")
             for i in range(44)]
            # 9 designer queue
            + [_feature(2000 + i, "Approved", design_doc_path=None)
               for i in range(9)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "coder", (
            f"coder should win — coder backlog (44) > designer backlog (9), "
            f"got {result}"
        )
        assert "44" in result["reason"] and "9" in result["reason"]

    def test_deeper_designer_queue_still_runs_coder(self):
        """Strict-priority change (2026-06-03): even when the designer
        queue is much deeper, the coder runs as long as first-pass work
        exists and the IU gate permits. Designer fills in only when
        coder has no work or is PR-gated.

        Under the prior balance-by-depth rule, 41 Approved vs 5 Designed
        would have run designer; the new rule drains the 5 Designed via
        coder first (each ship turns into a row in features.merge_notes
        that ARCHITECTURE.md can absorb)."""
        features = (
            [_feature(1000 + i, "Approved", design_doc_path=None)
             for i in range(41)]
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(5)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "coder", (
            f"strict priority: coder runs whenever first-pass exists, "
            f"got {result}"
        )
        assert "5" in result["reason"]

    def test_designer_runs_when_coder_pr_gated_no_rework(self):
        """When the IU gate blocks fresh first-pass (open PR exists but
        no rework-eligible candidate in first_pass_codeable), designer
        runs to drain the Approved queue.

        This is the only path where designer takes priority over fresh
        first-pass work — preserving the 1-PR-per-product invariant."""
        features = (
            # One feature already in flight (open PR, gating)
            [_feature(900, "Reviewing", pr_number=42)]
            # Designer queue
            + [_feature(1000 + i, "Approved", design_doc_path=None)
               for i in range(10)]
            # Fresh first-pass (would open a 2nd PR if claimed)
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(5)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        # Step 1 reviewer would catch this; bypass by using Implemented
        # status on the open PR feature.
        features[0] = _feature(900, "Implemented", pr_number=42)
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "designer", (
            f"designer wins when coder is PR-gated with no rework candidate, "
            f"got {result}"
        )
        assert "gated" in result["reason"]

    def test_equal_queues_tiebreak_to_coder(self):
        """Equal queue depths: tie goes to coder (PRs reach Reviewing
        and the system's ultimate throughput metric is merges)."""
        features = (
            [_feature(1000 + i, "Approved", design_doc_path=None)
             for i in range(5)]
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(5)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "coder", (
            f"equal queues should tie-break to coder, got {result}"
        )

    def test_only_designer_queue_runs_designer(self):
        """Only Approved-no-design features: designer runs (no coder work)."""
        features = [_feature(1000 + i, "Approved", design_doc_path=None)
                    for i in range(3)]
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "designer"

    def test_only_coder_queue_runs_coder(self):
        """Only Designed features: coder runs (no designer work)."""
        features = [_feature(1000 + i, "Designed",
                             design_doc_path=f"docs/{1000+i}.md")
                    for i in range(3)]
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "coder"

    def test_rework_coder_preempts_both(self):
        """An Implementing+changes_requested feature outranks both
        balancing branches — rework is most time-sensitive and runs
        before any first-pass / design work."""
        features = (
            [_feature(1, "Implementing", review_outcome="changes_requested")]
            # Big designer queue that would otherwise win
            + [_feature(1000 + i, "Approved", design_doc_path=None)
               for i in range(20)]
            # Big coder queue
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(30)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "coder"
        assert "rework" in result["reason"].lower()

    def test_rework_near_cap_loses_preemption(self):
        """Cycle DM-2: rework features with fix_attempts >= 3 don't preempt.
        They fall into the first-pass pool and compete fairly. Canonical:
        #1178 cycling at fix_attempts=3 while 30+ fresh features sit idle."""
        features = (
            # ONE stuck rework feature at fix_attempts=3 (no longer preempts)
            [_feature(1178, "Implementing",
                      review_outcome="changes_requested",
                      fix_attempts=3,
                      design_doc_path="docs/1178.md")]
            # 30 fresh codeable features
            + [_feature(2000 + i, "Designed",
                        design_doc_path=f"docs/{2000+i}.md")
               for i in range(30)]
            # 2 features needing design
            + [_feature(3000 + i, "Approved", design_doc_path=None)
               for i in range(2)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "coder", (
            f"coder should win (31 codeable vs 2 design), got {result}"
        )
        # The reason should reflect the first-pass count (30 fresh + 1
        # de-prioritized rework = 31), not the "1 rework features" line.
        assert "31" in result["reason"], (
            f"de-prioritized rework should be in first-pass pool, got: {result}"
        )

    def test_low_attempts_rework_still_preempts(self):
        """Rework at fix_attempts < 3 STILL preempts — fresh-from-reviewer
        feedback is time-sensitive and the rework workspace has the hot
        prior implementation."""
        features = (
            [_feature(1, "Implementing",
                      review_outcome="changes_requested",
                      fix_attempts=2,
                      design_doc_path="docs/1.md")]
            + [_feature(2000 + i, "Designed",
                        design_doc_path=f"docs/{2000+i}.md")
               for i in range(30)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "coder"
        assert "rework" in result["reason"].lower(), (
            f"low-attempts rework still preempts, got: {result}"
        )

    def test_reviewer_preempts_everything(self):
        """A Reviewing feature with a PR outranks every other branch.
        Open PRs MUST be reviewed before more work piles up."""
        features = (
            [_feature(1, "Reviewing", pr_number=42)]
            # Designer queue
            + [_feature(1000 + i, "Approved", design_doc_path=None)
               for i in range(20)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "reviewer"


class TestPlannerBacklogGate:
    """Step-5 planner gate counts the UN-BUILT backlog (Pending + Approved),
    not just Approved. Regression (DogTinder #31, 2026-07): the planner files
    stories as Pending, but the gate counted only Approved — so Pending piled
    up unbounded while Approved stayed ~0 and the planner re-launched every
    round-robin visit (~437 stories / 1,127 sessions over 3 days). Only Pending
    features survive to step 5 (Approved triggers designer/coder first), so
    counting Pending is what makes the gate actually fire."""

    def test_pending_backlog_gates_planner(self):
        # 15 Pending stories, nothing else. OLD code saw Approved==0 and
        # launched the planner every cycle; NEW code counts Pending →
        # backlog(15) >= cap(10) → gated, no planner launch.
        features = [_feature(1000 + i, "Pending") for i in range(15)]
        client = _client_for_features(features)  # default max_pending=10
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "exit", f"should gate, got {result}"
        assert "gated" in result["reason"].lower() and "15" in result["reason"]

    def test_empty_backlog_launches_planner(self):
        # No un-built work at all → planner generates the initial backlog.
        client = _client_for_features([])
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "planner"

    def test_pending_below_cap_does_not_spam(self):
        # 5 Pending (below cap, non-empty): planner must NOT launch (there's
        # un-built work) but is NOT at the gate either — no spam, no launch.
        features = [_feature(1000 + i, "Pending") for i in range(5)]
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "exit"
        assert result.get("persona") != "planner"

    def test_cap_respects_config_override(self):
        # 4 Pending with a max_pending_approved=3 override → gated.
        features = [_feature(1000 + i, "Pending") for i in range(4)]
        client = _client_for_features(features, sys_cfg={"max_pending_approved": 3})
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "exit"
        assert "gated" in result["reason"].lower()


class TestOrphanedPRGate:
    """Orphaned-PR exclusion (2026-08-20): a pr_number-carrying feature that
    is dependency-frozen in a coder-owned state must NOT serialize the IU
    gate — it can never be claimed, so counting it deadlocks the gate against
    the dependency gate. Canonical: MyGroceryApp #3457 / PR #171 (6-day stall:
    the open PR deferred #3456, the very dependency whose Push would have
    unfrozen #3457)."""

    def test_frozen_pr_feature_does_not_serialize_gate(self):
        # #3457 carries PR 171 but depends_on #3456 (not Pushed) → frozen.
        # #3456 is fresh first-pass. Before the fix: PR 171 counted, no
        # rework candidate (3457 is frozen out of first_pass_codeable) →
        # "PR-serialization gate" exit forever. After: coder launches.
        features = [
            _feature(3456, "Designed"),
            _feature(3457, "Designed", pr_number=171, depends_on=3456),
        ]
        client = _client_for_features(features)
        result = _decide_action(product_id=35, client=client)
        assert result["action"] == "launch_session", f"deadlocked: {result}"
        assert result["persona"] == "coder", (
            f"frozen PR-carrier must not gate the dispatcher, got {result}"
        )

    def test_reviewer_owned_frozen_pr_still_serializes(self):
        # A frozen feature in a reviewer-owned state (Reviewed) still counts:
        # the reviewer/auto-merge are not dependency-gated and WILL move its
        # PR — opening a 2nd PR here would recreate the stale-branch cascade.
        features = [
            _feature(3456, "Designed"),
            _feature(3457, "Reviewed", pr_number=171, depends_on=3456),
        ]
        client = _client_for_features(features)
        result = _decide_action(product_id=35, client=client)
        assert not (
            result.get("persona") == "coder"
            and "first-pass" in result.get("reason", "")
        ), f"Reviewed PR-carrier must still gate first-pass claims: {result}"

    def test_unfrozen_pr_feature_still_serializes_gate(self):
        # Control: a NON-frozen Designed feature with pr_number is the normal
        # rework case — gate behavior unchanged (coder may claim it as rework,
        # fresh sibling stays deferred; both route through the coder).
        features = [
            _feature(3456, "Designed"),
            _feature(3457, "Designed", pr_number=171),  # no depends_on
        ]
        client = _client_for_features(features)
        result = _decide_action(product_id=35, client=client)
        # 3457 is rework-eligible (pr_number set, in first_pass_codeable) →
        # gate permits the coder via the rework path.
        assert result["persona"] == "coder", f"got {result}"
