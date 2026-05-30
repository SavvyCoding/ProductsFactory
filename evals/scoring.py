"""Scoring primitives for Tier-2 evals.

Each scorer is a pure function: `(output: str, **kwargs) -> ScoreResult`.
The runner applies them per scenario and aggregates into a pass/fail
report. Scorers are deterministic — given the same output + args they
return the same score, so two runs of the same prompt can be compared
byte-for-byte.

Add a new scorer when:
  - You observed a failure mode that the existing scorers miss.
  - You want to measure a property no current scorer covers.

Naming: each scorer is `check_<noun>` so the scenario JSON's `checks`
list reads naturally.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoreResult:
    """Outcome of one scorer call.

    Fields:
      passed: bool — whether the scorer's predicate held.
      score:  float — 0.0..1.0 fine-grained score (most scorers are binary
                      0 or 1; AC-coverage style scorers are graded).
      detail: str  — one-line human-readable explanation. Lands in the
                     report and is what a human reads first when
                     debugging a regression.
      data:   dict — structured side-info for the runner / compare tool
                     (e.g. observed counts, parsed sub-objects). Used to
                     compute deltas in compare.py.
    """
    passed: bool
    score: float
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


# ── Structural checks (output shape) ─────────────────────────────────────────


def check_contains_substring(output: str, *, needle: str) -> ScoreResult:
    """Pass if `needle` appears anywhere in output (substring match)."""
    ok = needle in output
    return ScoreResult(
        passed=ok, score=1.0 if ok else 0.0,
        detail=(f"substring {needle!r} {'present' if ok else 'MISSING'} "
                f"in {len(output)}-char output"),
        data={"needle": needle},
    )


def check_does_not_contain(output: str, *, needle: str) -> ScoreResult:
    """Pass if `needle` does NOT appear (negative check). Used for banned
    patterns: e.g. coder output must not contain `assert True`."""
    bad = needle in output
    return ScoreResult(
        passed=not bad, score=0.0 if bad else 1.0,
        detail=(f"banned substring {needle!r} "
                f"{'PRESENT' if bad else 'absent'}"),
        data={"needle": needle},
    )


def check_matches_regex(output: str, *, pattern: str,
                        flags: int = 0) -> ScoreResult:
    """Pass if `pattern` matches anywhere in output."""
    m = re.search(pattern, output, flags)
    ok = m is not None
    return ScoreResult(
        passed=ok, score=1.0 if ok else 0.0,
        detail=f"regex {pattern!r} {'matched' if ok else 'did NOT match'}",
        data={"pattern": pattern, "match": m.group(0) if m else None},
    )


def check_min_length(output: str, *, min_chars: int) -> ScoreResult:
    """Pass if output is at least `min_chars` long. Catches empty / truncated
    responses and 500-style backend failures the runner translated to ""."""
    ok = len(output) >= min_chars
    return ScoreResult(
        passed=ok, score=1.0 if ok else 0.0,
        detail=f"output {len(output)} chars (min={min_chars})",
        data={"length": len(output)},
    )


def check_valid_json(output: str) -> ScoreResult:
    """Pass if `output` parses as JSON. Used when the persona is expected to
    emit a JSON payload (e.g. session_result.json line)."""
    try:
        parsed = json.loads(output.strip())
    except Exception as e:
        return ScoreResult(
            passed=False, score=0.0,
            detail=f"JSON parse failed: {e}", data={},
        )
    return ScoreResult(
        passed=True, score=1.0,
        detail=f"JSON parsed: {type(parsed).__name__} with "
               f"{len(parsed) if hasattr(parsed, '__len__') else '?'} entries",
        data={"parsed": parsed},
    )


# ── Persona-specific composite checks ────────────────────────────────────────


# Matches the post-2026-05-30 designer's AC heading shape (`AC<N>.`).
_AC_HEADING_RE = re.compile(r"^AC(\d+)\.\s", re.MULTILINE)
# Matches the Verify recipe + Expected pair the designer must produce per AC.
_VERIFY_LINE_RE = re.compile(r"^\s*Verify:\s*`", re.MULTILINE)
_EXPECTED_LINE_RE = re.compile(r"^\s*Expected:\s*", re.MULTILINE)


def check_designer_ac_structure(output: str, *,
                                min_acs: int = 1,
                                max_acs: int = 4,
                                require_verify: bool = True) -> ScoreResult:
    """Composite: designer's story doc must have N..M acceptance criteria,
    each with a Verify recipe and Expected output (per the post-2026-05-30
    prompt contract).

    Score is graded: ac_count / max_acs for the count part, and
    verify_pairs / ac_count for the recipe part, averaged.
    """
    ac_count = len(_AC_HEADING_RE.findall(output))
    verify_count = len(_VERIFY_LINE_RE.findall(output))
    expected_count = len(_EXPECTED_LINE_RE.findall(output))
    count_ok = min_acs <= ac_count <= max_acs
    if require_verify and ac_count > 0:
        # At least one Verify per AC, at least one Expected per Verify.
        recipe_ok = verify_count >= ac_count and expected_count >= verify_count
    else:
        recipe_ok = True
    passed = count_ok and recipe_ok
    grade = 0.5 * (1.0 if count_ok else 0.0) + 0.5 * (
        min(1.0, verify_count / max(1, ac_count)) if ac_count else 0.0
    )
    return ScoreResult(
        passed=passed, score=grade,
        detail=(f"{ac_count} AC(s) (target {min_acs}..{max_acs}), "
                f"{verify_count} Verify line(s), "
                f"{expected_count} Expected line(s) — "
                f"{'OK' if passed else 'FAIL'}"),
        data={"ac_count": ac_count, "verify_count": verify_count,
              "expected_count": expected_count},
    )


def check_coder_session_summary_blocks(output: str, *,
                                       min_blocks: int = 1) -> ScoreResult:
    """Composite: coder's session_summary.md must contain
    `## AC<N> verification:` blocks (or the legacy `## AC<N> empirical
    check:` variant). The verify-driven prompt requires one per behavior
    AC; this check counts them.
    """
    verification_re = re.compile(
        r"^##\s+AC(\d+)\s+(?:verification|empirical check):",
        re.MULTILINE,
    )
    matches = verification_re.findall(output)
    n = len(matches)
    ok = n >= min_blocks
    return ScoreResult(
        passed=ok, score=min(1.0, n / max(1, min_blocks)),
        detail=(f"{n} AC verification block(s) (min={min_blocks}) — "
                f"{'OK' if ok else 'FAIL'}"),
        data={"block_count": n, "ac_numbers": sorted(set(int(x) for x in matches))},
    )


def check_reviewer_decision(output: str, *,
                            expected: str) -> ScoreResult:
    """Composite: reviewer must produce a decision (approve / changes_requested).

    `expected` is one of:
      "approved"           — output must contain '✅' AND 'LGTM' or 'approved'
      "changes_requested"  — output must contain '❌' AND a reason
    """
    expected = expected.lower()
    has_approve = "✅" in output and (
        "lgtm" in output.lower() or "approved" in output.lower()
    )
    has_reject = "❌" in output
    if expected == "approved":
        ok = has_approve and not has_reject
        detail = f"expected APPROVE — got {'APPROVE' if has_approve else 'no approve marker'}{', also got REJECT (mixed)' if has_reject else ''}"
    elif expected == "changes_requested":
        ok = has_reject
        detail = f"expected REJECT — got {'REJECT' if has_reject else 'no reject marker'}"
    else:
        ok = False
        detail = f"unknown expected value {expected!r} — should be approved|changes_requested"
    return ScoreResult(
        passed=ok, score=1.0 if ok else 0.0,
        detail=detail,
        data={"has_approve": has_approve, "has_reject": has_reject,
              "expected": expected},
    )


# Registry of available scorers — the runner resolves a scenario's
# `check.scorer` name to the function via this map. Add new scorers
# above and register them here.
SCORERS: dict[str, Any] = {
    "contains_substring":           check_contains_substring,
    "does_not_contain":             check_does_not_contain,
    "matches_regex":                check_matches_regex,
    "min_length":                   check_min_length,
    "valid_json":                   check_valid_json,
    "designer_ac_structure":        check_designer_ac_structure,
    "coder_session_summary_blocks": check_coder_session_summary_blocks,
    "reviewer_decision":            check_reviewer_decision,
}
