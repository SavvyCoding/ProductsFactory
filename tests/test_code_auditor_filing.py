"""
Increment 1 of the code_auditor filing promotion (2026-06-25).

Asserts the deterministic output-mode switch in the prompt builder:
  - comment-only (default) → the audit raises dashboard alerts, files nothing.
  - filing  (CODE_AUDITOR_FILING_ENABLED) → the audit files each finding as a
    `bug` feature, severity-routed, with NO stop-the-line (findings land
    Pending for the PM).

The switch is builder-side (the builder injects exactly one block) rather than
agent-branched, so a local model can't do the wrong half on a bad turn — these
tests pin that contract.
"""

import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.prompts import (
    build_prompt,
    _code_auditor_filing_on,
    _CODE_AUDITOR_FILING_STEPS,
    _CODE_AUDITOR_ALERT_STEPS,
)


def _product():
    return {
        "id": 99,
        "name": "TestProd",
        "working_dir": "/nonexistent-test-dir",
        "tech_stack": ["python"],
        "config": {},
    }


# --------------------------------------------------------------------------- #
# Flag resolution precedence
# --------------------------------------------------------------------------- #
def test_filing_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("CODE_AUDITOR_FILING_ENABLED", raising=False)
    assert _code_auditor_filing_on(_product()) is False


def test_filing_flag_env_on(monkeypatch):
    monkeypatch.setenv("CODE_AUDITOR_FILING_ENABLED", "1")
    assert _code_auditor_filing_on(_product()) is True
    for v in ("true", "YES", "on"):
        monkeypatch.setenv("CODE_AUDITOR_FILING_ENABLED", v)
        assert _code_auditor_filing_on(_product()) is True


def test_filing_flag_per_product_config_overrides_env(monkeypatch):
    # explicit per-product False wins over env-on
    monkeypatch.setenv("CODE_AUDITOR_FILING_ENABLED", "1")
    p = _product()
    p["config"]["code_auditor_filing"] = False
    assert _code_auditor_filing_on(p) is False
    # explicit per-product True wins over env-off
    monkeypatch.delenv("CODE_AUDITOR_FILING_ENABLED", raising=False)
    p["config"]["code_auditor_filing"] = True
    assert _code_auditor_filing_on(p) is True


# --------------------------------------------------------------------------- #
# Rendered prompt carries exactly one sink, fully resolved
# --------------------------------------------------------------------------- #
def test_filing_mode_renders_feature_filing(monkeypatch):
    monkeypatch.setenv("CODE_AUDITOR_FILING_ENABLED", "1")
    prompt = build_prompt(_product(), "sess-uid", persona="code_auditor")

    # filing-specific markers present
    assert "/api/features" in prompt
    assert '"feature_type": "bug"' in prompt
    assert "Code-review:" in prompt
    assert "Critical → 1" in prompt and "Low → 40" in prompt
    # severity-routed auto-approve (2026-07-16): Critical/High skip triage,
    # Medium/Low stay Pending.
    assert '"status": "<STATUS>"' in prompt
    assert "Critical and High → `Approved`" in prompt
    assert "Medium and Low →" in prompt and "`Pending`" in prompt
    # nested placeholders inside the injected block were resolved
    assert "{code_auditor_output_steps}" not in prompt
    assert "{pm_api_url}" not in prompt and "{product_id}" not in prompt
    # the resolved product id reached the filing body
    assert '"product_id": 99' in prompt


def test_comment_only_mode_renders_alerts_only(monkeypatch):
    monkeypatch.delenv("CODE_AUDITOR_FILING_ENABLED", raising=False)
    prompt = build_prompt(_product(), "sess-uid", persona="code_auditor")

    # comment-only must NOT instruct feature filing
    assert "/api/features" not in prompt
    assert '"feature_type": "bug"' not in prompt
    # it still uses alerts (both modes post a summary alert; this mode files
    # findings as alerts too)
    assert "/api/alerts" in prompt
    assert "alerts only" in prompt
    assert "{code_auditor_output_steps}" not in prompt


def test_blocks_are_mutually_exclusive():
    # The two canned blocks are genuinely different sinks (guards against a
    # copy-paste that makes filing == comment-only).
    assert "/api/features" in _CODE_AUDITOR_FILING_STEPS
    assert "/api/features" not in _CODE_AUDITOR_ALERT_STEPS
