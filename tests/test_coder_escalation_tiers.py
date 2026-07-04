"""Unit tests for the coder model-ladder resolver (orchestrator/coder_tiers.py).

Pure logic — no DB, no Docker, no tokens. Covers tier boundaries, disabled-tier
filtering, the EXHAUSTED→Blocked terminal, paid-backend detection (cap driver),
and ladder validation.
"""
import pytest

from orchestrator.coder_tiers import (
    EXHAUSTED,
    effective_tiers,
    is_last_tier,
    is_paid_backend,
    resolve_coder_tier,
    tier_index_for_step,
    total_attempts,
    validate_coder_tiers,
)


def _t(backend, model, max_attempts=2, enabled=True):
    return {"backend": backend, "model": model, "max_attempts": max_attempts, "enabled": enabled}


# The canonical operator config: minimax (free, ×2) → glm (free, ×3) → opus (paid, ×2).
# Cumulative step windows: 0–1 minimax, 2–4 glm, 5–6 opus, 7+ EXHAUSTED.
LADDER = [
    _t("ollama", "minimax-m2", 2),
    _t("ollama", "glm-4.6", 3),
    _t("claude-api", "claude-opus-4-8", 2),
]


# ── resolve_coder_tier: cumulative step windows ───────────────────────────────

def test_step_windows_map_to_tiers():
    assert resolve_coder_tier(LADDER, 0)["model"] == "minimax-m2"   # tier 0, attempt 1
    assert resolve_coder_tier(LADDER, 1)["model"] == "minimax-m2"   # tier 0, attempt 2
    assert resolve_coder_tier(LADDER, 2)["model"] == "glm-4.6"      # tier 1, attempt 1
    assert resolve_coder_tier(LADDER, 4)["model"] == "glm-4.6"      # tier 1, attempt 3
    assert resolve_coder_tier(LADDER, 5)["model"] == "claude-opus-4-8"  # tier 2, attempt 1
    assert resolve_coder_tier(LADDER, 6)["model"] == "claude-opus-4-8"  # tier 2, attempt 2


def test_past_total_budget_is_exhausted():
    # total budget = 2+3+2 = 7 → step 7 and beyond → EXHAUSTED (caller → Blocked)
    assert total_attempts(LADDER) == 7
    assert resolve_coder_tier(LADDER, 7) == EXHAUSTED
    assert resolve_coder_tier(LADDER, 99) == EXHAUSTED


def test_unconfigured_ladder_returns_none_for_legacy_path():
    assert resolve_coder_tier(None, 0) is None
    assert resolve_coder_tier([], 0) is None


def test_negative_or_garbage_step_clamps_to_zero():
    assert resolve_coder_tier(LADDER, -5)["model"] == "minimax-m2"
    assert resolve_coder_tier(LADDER, None)["model"] == "minimax-m2"


def test_single_tier_ladder_exhausts_at_its_budget():
    solo = [_t("ollama", "minimax-m2", 3)]
    assert resolve_coder_tier(solo, 2)["model"] == "minimax-m2"
    assert resolve_coder_tier(solo, 3) == EXHAUSTED


# ── disabled-tier filtering ───────────────────────────────────────────────────

def test_disabled_escalation_tier_is_removed_from_ladder():
    ladder = [
        _t("ollama", "minimax-m2", 2),
        _t("ollama", "glm-4.6", 3, enabled=False),   # disabled → removed entirely
        _t("claude-api", "claude-opus-4-8", 2),
    ]
    # windows collapse to: 0–1 minimax, 2–3 opus, 4+ exhausted
    assert resolve_coder_tier(ladder, 1)["model"] == "minimax-m2"
    assert resolve_coder_tier(ladder, 2)["model"] == "claude-opus-4-8"
    assert resolve_coder_tier(ladder, 4) == EXHAUSTED


def test_first_attempt_is_live_even_if_enabled_false():
    ladder = [_t("ollama", "minimax-m2", 2, enabled=False)]
    assert resolve_coder_tier(ladder, 0)["model"] == "minimax-m2"


def test_effective_tiers_drops_non_dict_entries():
    assert effective_tiers([_t("ollama", "x"), "garbage", None]) == [_t("ollama", "x")]


# ── is_last_tier (a further failure → EXHAUSTED → Blocked) ─────────────────────

def test_is_last_tier():
    assert not is_last_tier(LADDER, 1)      # minimax window
    assert not is_last_tier(LADDER, 4)      # glm window
    assert is_last_tier(LADDER, 5)          # opus (final) window start
    assert is_last_tier(LADDER, 6)          # opus (final) window end
    assert not is_last_tier(LADDER, 7)      # already exhausted, not "in" the last tier


def test_is_last_tier_unconfigured_is_false():
    assert is_last_tier(None, 0) is False


# ── tier_index_for_step (telemetry, migration 050) ────────────────────────────

def test_tier_index_for_step():
    # LADDER = minimax(2)→glm(3)→opus(2): 0-1=tier0, 2-4=tier1, 5-6=tier2, 7+=exhausted
    assert tier_index_for_step(LADDER, 0) == 0
    assert tier_index_for_step(LADDER, 1) == 0
    assert tier_index_for_step(LADDER, 2) == 1
    assert tier_index_for_step(LADDER, 4) == 1
    assert tier_index_for_step(LADDER, 5) == 2   # opus tier
    assert tier_index_for_step(LADDER, 6) == 2
    assert tier_index_for_step(LADDER, 7) == -1  # exhausted
    assert tier_index_for_step(None, 0) == -1    # unconfigured


# ── paid-backend detection (daily-USD cap driver) ─────────────────────────────

@pytest.mark.parametrize("backend,paid", [
    ("ollama", False),
    ("claude-api", True),
    ("openai", True),
    ("OpenAI", True),        # case-insensitive
    (" claude-api ", True),  # trimmed
    (None, False),
    ("", False),
])
def test_is_paid_backend(backend, paid):
    assert is_paid_backend(backend) is paid


def test_all_ollama_ladder_is_free():
    free = [_t("ollama", "minimax-m2"), _t("ollama", "glm-4.6")]
    assert not any(is_paid_backend(t["backend"]) for t in free)


# ── validation (Slice B API edge) ─────────────────────────────────────────────

def test_valid_ladder_has_no_errors():
    assert validate_coder_tiers(LADDER) == []


def test_none_is_valid_legacy():
    assert validate_coder_tiers(None) == []


def test_too_many_tiers_rejected():
    errs = validate_coder_tiers([_t("ollama", "m")] * 5)
    assert any("1–4 entries" in e for e in errs)


def test_empty_list_rejected():
    errs = validate_coder_tiers([])
    assert any("1–4 entries" in e for e in errs)


def test_unknown_backend_rejected():
    errs = validate_coder_tiers([_t("mistral", "x")])
    assert any("backend must be one of" in e for e in errs)


def test_missing_model_rejected():
    errs = validate_coder_tiers([_t("ollama", "")])
    assert any("model is required" in e for e in errs)


def test_bad_max_attempts_rejected():
    assert any("max_attempts" in e for e in validate_coder_tiers([_t("ollama", "m", max_attempts=0)]))
    assert any("max_attempts" in e for e in validate_coder_tiers([{"backend": "ollama", "model": "m", "max_attempts": "x"}]))


# ── _resolve_coder_ladder (docker_runner wrapper: default tier + at_cap) ───────
# Lazy import so a docker_runner import hiccup only skips these, not the pure tests.

def _ladder(sys_cfg, assigned, persona="coder", default="qwen:base"):
    import os
    os.environ.setdefault("PM_API_URL", "http://localhost:8080")
    from orchestrator.docker_runner import _resolve_coder_ladder
    return _resolve_coder_ladder(sys_cfg, persona, assigned, default)


def _feat(step=0, fa=0, fid=1):
    return {"id": fid, "escalation_step": step, "fix_attempts": fa}


def test_ladder_default_tier_when_unconfigured():
    # No coder_tiers → single default Ollama tier built from the coder model.
    r = _ladder({}, [_feat(step=0)])
    assert r["active"] is True
    assert r["tier"]["backend"] == "ollama"
    assert r["tier"]["model"] == "qwen:base"
    assert r["at_cap"] is False
    assert r["tier0_budget"] == 4          # ESCALATION_FIX_ATTEMPTS_THRESHOLD default


def test_ladder_default_tier_at_cap_and_exhausted():
    # Single default tier total=4: step 4 is both at_cap (→ diagnostician) and exhausted.
    r = _ladder({}, [_feat(step=4)])
    assert r["at_cap"] is True
    assert r["exhausted"] is True
    assert r["tier"] is None


def test_ladder_configured_tiers_resolve_and_flag_at_cap():
    cfg = {"coder_tiers": [
        _t("ollama", "minimax", 2),
        _t("claude-api", "opus", 2),
    ]}
    r = _ladder(cfg, [_feat(step=2)])       # step 2 → tier 1 (opus), past tier-0 budget
    assert r["tier"]["backend"] == "claude-api"
    assert r["at_cap"] is True
    assert r["tier0"]["model"] == "minimax"


def test_ladder_uses_max_step_across_batch():
    cfg = {"coder_tiers": [_t("ollama", "minimax", 2), _t("claude-api", "opus", 2)]}
    r = _ladder(cfg, [_feat(step=0, fid=1), _feat(step=3, fid=2)])
    assert r["step"] == 3                    # max across the batch drives the tier
    assert r["tier"]["model"] == "opus"      # step 3 → last tier (windows: 0-1 minimax, 2-3 opus)
    assert r["exhausted"] is False           # total=4; exhaustion only at step>=4


def test_ladder_step_uses_fix_attempts_floor_when_escalation_step_lags():
    # R1 fix: a feature that bounced (fix_attempts=4) but whose escalation_step
    # lags (0, e.g. counted only post-deploy) must still route to the glm tier —
    # otherwise the fix_attempts>=5 cap-Block pre-empts the ladder (NewtorkPnL #2271).
    cfg = {"coder_tiers": [_t("ollama", "minimax", 2), _t("ollama", "glm-5.2", 3)]}
    r = _ladder(cfg, [{"id": 2271, "escalation_step": 0, "fix_attempts": 4}])
    assert r["step"] == 4                 # max(escalation_step=0, fix_attempts=4)
    assert r["tier"]["model"] == "glm-5.2"  # step 4 → tier 1 (glm), not minimax


def test_ladder_step_honors_escalation_step_when_fix_attempts_reset():
    # Reprocessor reset case: fix_attempts=0 (reset) but escalation_step=3
    # (already climbed) → stay on the escalation tier, don't fall back to tier 0.
    cfg = {"coder_tiers": [_t("ollama", "minimax", 2), _t("ollama", "glm-5.2", 3)]}
    r = _ladder(cfg, [{"id": 1, "escalation_step": 3, "fix_attempts": 0}])
    assert r["step"] == 3
    assert r["tier"]["model"] == "glm-5.2"


def test_ladder_inactive_for_non_coder():
    assert _ladder({}, [_feat()], persona="designer")["active"] is False


def test_ladder_inactive_without_features():
    assert _ladder({}, [])["active"] is False


# ── daily-cap enforcement helpers (docker_runner) ─────────────────────────────

def _dr():
    import os
    os.environ.setdefault("PM_API_URL", "http://localhost:8080")
    import orchestrator.docker_runner as d
    return d


def test_daily_paid_cap_disabled_when_zero_or_blank():
    d = _dr()
    # cap <= 0 short-circuits to False with no network call → paid tiers off.
    assert d._daily_paid_cap_ok({}) is False
    assert d._daily_paid_cap_ok({"blocked_escalation_daily_usd_cap": 0}) is False
    assert d._daily_paid_cap_ok({"blocked_escalation_daily_usd_cap": "0"}) is False


def test_cheapest_ollama_model_picks_first_ollama_tier():
    d = _dr()
    tiers = [_t("claude-api", "opus"), _t("ollama", "glm"), _t("ollama", "minimax")]
    assert d._cheapest_ollama_model(tiers, "fallback") == "glm"


def test_cheapest_ollama_model_falls_back_when_all_paid():
    d = _dr()
    assert d._cheapest_ollama_model([_t("openai", "gpt"), _t("claude-api", "opus")], "fb") == "fb"
