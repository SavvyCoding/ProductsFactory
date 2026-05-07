"""Tier-1 evals — structural invariants on persona prompts.

These run as part of `pytest evals/` and catch the most common prompt-drift
regression: a refactor that silently removes a security instruction or output
format requirement.

They never call an LLM, so they're fast and deterministic.
"""
from __future__ import annotations

import os

# docker_runner.py is imported transitively by prompts.build_prompt in some
# paths — make sure PM_API_URL is set before import.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import pytest

from orchestrator.prompts import build_prompt
from evals.fixtures import SAMPLE_PRODUCT, SAMPLE_SESSION_UID, PROMPT_INVARIANTS


def _build(persona: str, product: dict | None = None) -> str:
    return build_prompt(product or SAMPLE_PRODUCT, SAMPLE_SESSION_UID, persona=persona)


@pytest.mark.parametrize("persona", list(PROMPT_INVARIANTS.keys()))
def test_persona_prompt_contains_required_invariants(persona: str) -> None:
    """Every persona's built prompt must contain all configured invariants.

    Failure here means a prompt refactor dropped a non-negotiable instruction
    and the agent will likely regress. Add the missing phrase back to the
    template in orchestrator/prompts/<persona>.md.
    """
    if persona in ("greenfield", "brownfield"):
        # Coder uses type-based routing, not persona=coder.
        product = dict(SAMPLE_PRODUCT, type=persona)
        prompt = _build(persona=None, product=product)
    else:
        prompt = _build(persona=persona)

    missing = [s for s in PROMPT_INVARIANTS[persona] if s.lower() not in prompt.lower()]
    assert not missing, (
        f"persona={persona} is missing required invariants in its prompt: {missing}. "
        f"Edit orchestrator/prompts/{persona}.md to re-introduce them."
    )


def test_all_personas_produce_nonempty_prompt() -> None:
    """Sanity check — every known persona should produce a non-trivial prompt."""
    # 2026-05-06 persona simplification (futureplan.md):
    #   Phase 1: product_planner merged into designer
    #   Phase 2: qa_tester + security_auditor merged into reviewer
    #   Phase 3: retrospective replaced by inline templated generator
    #            (orchestrator/pipelines/retro_generator.py) — no LLM
    #            session, no prompt file. Dispatch returns
    #            action=run_inline instead of launch_session.
    # The standalone .md files for those merged/replaced personas are
    # deleted; persona dispatch routes any lingering callers to the
    # survivor (or to planner.md as a defensive fallback for retrospective).
    all_personas = [
        "designer", "reviewer",
        "recommender", "planner",
        "documenter", "refactorer", "devops", "analytics", "product_trainer",
    ]
    for p in all_personas:
        prompt = _build(persona=p)
        assert prompt and len(prompt) > 200, \
            f"persona={p} produced a suspiciously short prompt ({len(prompt)} chars)"


def test_sprint_aware_personas_reference_assigned_features() -> None:
    """Personas that work inside a sprint must show the model which features
    it was assigned — otherwise the model re-discovers them (slow, unreliable)."""
    # designer / reviewer / coder are pre-assigned features.
    for p in ("designer", "reviewer"):
        prompt = _build(persona=p)
        # The fixture assigned feature #42.
        assert "42" in prompt or "Add divide endpoint" in prompt, \
            f"persona={p} prompt should include the pre-assigned features"
