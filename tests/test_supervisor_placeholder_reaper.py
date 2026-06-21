"""Unit tests for the placeholder/probe reaper detection logic
(orchestrator.supervisor._is_placeholder_block).

Pure-logic, no DB or PM API needed — guards the precision that matters: reject
agent probe/placeholder JUNK, but never sweep a genuinely-underspecced REAL
feature (those need a human, not an auto-reject).
"""
from orchestrator.supervisor import _is_placeholder_block


JUNK = [
    {"status": "Blocked", "name": "probe",
     "blocked_reason": "Placeholder story — name='probe', description='probe to check response'. Cannot design."},
    {"status": "Blocked", "name": "query", "description": "querying features",
     "blocked_reason": "Placeholder story — no ACs."},
    {"status": "Blocked", "name": "probe feature 5",
     "description": "probe feature to test API query 5", "blocked_reason": ""},
    {"status": "Blocked", "name": "test-probe-2",
     "description": "probe to test API connectivity for designer session", "blocked_reason": ""},
]

REAL_BLOCKS = [
    # Underspecced but real — needs a PM decision, NOT a reject.
    {"status": "Blocked", "name": "Worker background check integration",
     "blocked_reason": "Insufficient spec — 7 NEEDS CLARIFICATION markers. Cannot design without a vendor."},
    # Oversized real feature blocked via flap.
    {"status": "Blocked", "name": "In-app chat and notifications",
     "blocked_reason": "Auto-blocked: fix_attempts=5 via Reviewing->Implementing flap."},
    # Real feature, rapid-flap block.
    {"status": "Blocked", "name": "Admin user listing with search and role filter",
     "blocked_reason": "Auto-blocked by supervisor.rapid_flap: cycled 10x."},
    # A real feature that merely mentions a query in its name — must NOT match.
    {"status": "Blocked", "name": "Listing search and filter",
     "blocked_reason": "rapid_flap"},
]


def test_junk_placeholder_blocks_match():
    for f in JUNK:
        assert _is_placeholder_block(f) is True, f"should match: {f['name']!r}"


def test_real_blocked_features_are_spared():
    for f in REAL_BLOCKS:
        assert _is_placeholder_block(f) is False, f"should NOT match: {f['name']!r}"


def test_non_blocked_features_ignored():
    # Even an obvious probe is ignored unless it's actually Blocked.
    assert _is_placeholder_block(
        {"status": "Approved", "name": "probe", "blocked_reason": ""}
    ) is False
    assert _is_placeholder_block(
        {"status": "Rejected", "name": "probe", "blocked_reason": "Placeholder story"}
    ) is False


def test_missing_fields_safe():
    assert _is_placeholder_block({"status": "Blocked"}) is False
    assert _is_placeholder_block({}) is False
