"""Canonical fixtures used by tier-1 (contract) and tier-2 (live) evals.

Keep these fixtures small and stable — every time they change, all historical
eval results become incomparable.
"""
from __future__ import annotations


# Minimal product dict shaped like what poller._normalize_product returns.
SAMPLE_PRODUCT: dict = {
    "id": 101,
    "name": "Calculator",
    "working_dir": "/workspace",
    "github_repo": "https://github.com/example/calculator",
    "type": "greenfield",
    "tech_stack": ["python", "fastapi"],
    "analysis_status": "done",
    "_assigned_features": [
        {"id": 42, "name": "Add divide endpoint", "status": "Designed"},
    ],
    "_assigned_features_md": "- #42 Add divide endpoint (Designed)",
    "_auto_merge_enabled": False,
}


SAMPLE_SESSION_UID = "eval-session-0001"


# Mapping persona → (tier-1 required substrings in the built prompt).
# These are non-negotiable invariants the prompt MUST satisfy, otherwise the
# session behaviour changes dangerously.
PROMPT_INVARIANTS: dict[str, list[str]] = {
    # Coder personas must always reference session_result.json — that's how
    # the poller learns which features were attempted.
    "greenfield": ["session_result.json"],
    "brownfield": ["session_result.json"],

    # Designer must produce a design doc in docs/ so the coder can pick it up.
    "designer": ["docs/", "design"],

    # Reviewer must not set features back to Reviewing — that's a common
    # failure mode we caught in Phase 1. Keep the invariant so the prompt
    # explicitly reminds the model.
    "reviewer": ["Reviewed"],

    # Planner (Phase 2 of futureplan_v2): must produce one Feature (sprint)
    # decomposed into Stories (features) within the size cap. Prompt must
    # reference both "Story" and the sprint POST endpoint, otherwise the
    # planner reverts to the old "create flat features" behaviour.
    "planner": ["sprint", "Story", "/api/sprints", "≤4 acceptance"],

    # Security auditor must not modify code — it only files bugs.
    "security_auditor": ["bug", "security"],

    # QA tester operates on the coder's open PR — it must push tests to that branch.
    "qa_tester": ["test"],
}
