"""Coder model-ladder resolution — pure, side-effect-free tier selection.

The coder persona can be configured with an ordered ladder of model tiers
(``system_config.coder_tiers``): a First Attempt plus up to three escalation
tiers, each naming a ``(backend, model, max_attempts)``. A feature climbs the
ladder one tier at a time — but only for features the diagnose-first
diagnostician has judged ``fixable`` (the gating lives in ``docker_runner``;
this module is just the arithmetic).

Design invariants (see docs/design/coder_escalation_tiers.md):

- The active tier is derived from ``features.escalation_step`` — a DEDICATED
  *cumulative* counter (# of failed coder attempts on the ladder), never
  ``fix_attempts`` (which the blocked re-processor resets). Walking the
  cumulative ``max_attempts`` of each live tier maps one counter onto per-tier
  attempt budgets — e.g. tiers [minimax×2, glm×3, opus×2] give step ranges
  0–1 → minimax, 2–4 → glm, 5–6 → opus, 7+ → EXHAUSTED.
- Disabled escalation tiers are removed from the effective ladder entirely. The
  First Attempt (index 0) is always live regardless of its ``enabled`` flag.
- Exhausting the last live tier yields ``EXHAUSTED`` — the caller routes the
  feature to ``Blocked`` (the familiar PM holdpen), NOT a separate terminal
  state, so operators triage ladder-exhausted features in the normal queue.

This module has no DB/Docker imports so it unit-tests token-free.
"""
from __future__ import annotations

from typing import Optional, TypedDict

# Backends whose sessions cost real money (summed against the daily-USD cap).
# Ollama is free and never touches the cap.
PAID_BACKENDS = frozenset({"claude-api", "openai"})
VALID_BACKENDS = frozenset({"ollama", "claude-api", "openai"})

# Sentinel returned when a feature has climbed past the last live tier.
EXHAUSTED = "__EXHAUSTED__"


class Tier(TypedDict):
    backend: str          # 'ollama' | 'claude-api' | 'openai'
    model: str
    max_attempts: int     # failures at this tier before the next climb (>=1)
    enabled: bool


def is_paid_backend(backend: Optional[str]) -> bool:
    """True when a tier's backend spends money (drives the daily-USD cap sum)."""
    return (backend or "").strip().lower() in PAID_BACKENDS


def effective_tiers(coder_tiers: Optional[list]) -> list:
    """The live ladder: First Attempt (always) + enabled escalation tiers.

    Returns [] when the ladder is unconfigured/empty — the caller then uses the
    legacy single-model coder path. Filtering disabled escalation tiers here
    (rather than at the step boundary) keeps ``escalation_step`` mapping onto a
    real tier even as the operator toggles individual tiers on and off.
    """
    if not coder_tiers or not isinstance(coder_tiers, list):
        return []
    out: list = []
    for i, t in enumerate(coder_tiers):
        if not isinstance(t, dict):
            continue
        # First Attempt (index 0) is always live; escalation tiers must opt in.
        if i == 0 or t.get("enabled", True):
            out.append(t)
    return out


def _tier_max_attempts(tier: dict) -> int:
    try:
        return max(1, int(tier.get("max_attempts")))
    except (TypeError, ValueError):
        return 1


def resolve_coder_tier(coder_tiers: Optional[list], escalation_step: int):
    """Pick the active tier for a feature at cumulative ``escalation_step``.

    Walks the live ladder accumulating each tier's ``max_attempts``; the tier
    whose cumulative window contains ``escalation_step`` is active. Returns the
    tier dict, ``EXHAUSTED`` when the step is past the last tier's budget, or
    ``None`` when the ladder is unconfigured (caller uses the legacy coder model).
    """
    eff = effective_tiers(coder_tiers)
    if not eff:
        return None
    step = max(0, int(escalation_step or 0))
    ceiling = 0
    for tier in eff:
        ceiling += _tier_max_attempts(tier)
        if step < ceiling:
            return tier
    return EXHAUSTED


def total_attempts(coder_tiers: Optional[list]) -> int:
    """Sum of every live tier's max_attempts — the step at which the ladder is exhausted."""
    return sum(_tier_max_attempts(t) for t in effective_tiers(coder_tiers))


def is_last_tier(coder_tiers: Optional[list], escalation_step: int) -> bool:
    """True when ``escalation_step`` falls in the FINAL live tier's window
    (a further failure past this tier's budget → EXHAUSTED → Blocked)."""
    eff = effective_tiers(coder_tiers)
    if not eff:
        return False
    step = max(0, int(escalation_step or 0))
    last_tier_start = sum(_tier_max_attempts(t) for t in eff[:-1])
    total = last_tier_start + _tier_max_attempts(eff[-1])
    return last_tier_start <= step < total


def validate_coder_tiers(coder_tiers) -> list[str]:
    """Return a list of human-readable validation errors ([] == valid).

    Used by the save-settings API (Slice B) so a malformed ladder is rejected at
    the edge rather than surfacing as a routing surprise mid-session.
    """
    errs: list[str] = []
    if coder_tiers is None:
        return errs  # unconfigured is valid (legacy path)
    if not isinstance(coder_tiers, list):
        return ["coder_tiers must be a list"]
    if not (1 <= len(coder_tiers) <= 4):
        errs.append("coder_tiers must have 1–4 entries (First Attempt + up to 3 escalations)")
    for i, t in enumerate(coder_tiers):
        where = f"tier {i}"
        if not isinstance(t, dict):
            errs.append(f"{where}: must be an object")
            continue
        backend = (t.get("backend") or "").strip().lower()
        if backend not in VALID_BACKENDS:
            errs.append(f"{where}: backend must be one of {sorted(VALID_BACKENDS)}")
        if not (t.get("model") or "").strip():
            errs.append(f"{where}: model is required")
        try:
            if int(t.get("max_attempts")) < 1:
                errs.append(f"{where}: max_attempts must be >= 1")
        except (TypeError, ValueError):
            errs.append(f"{where}: max_attempts must be an integer >= 1")
    return errs
