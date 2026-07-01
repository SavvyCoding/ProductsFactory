"""
Reviewer text-vs-structured validation: the negative-VERDICT detector must not
substring-match the verb "reject(s)" used to DESCRIBE correct code behavior.

Canonical false positive (HCS #1867, 34 features in one week): a genuine LGTM
approval whose prose said "Query rejects empty input" was matched by the old
bare `"REJECT" in body.upper()` check and force-rejected. Both the apply-time
guard (session/result_io.py) and the supervisor detector share the regex.
"""

import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.session.result_io import _NEG_VERDICT_RE as RIO_RE
from orchestrator.supervisor import _NEG_VERDICT_RE as SUP_RE

import pytest

# The actual #1867 review opening — an unambiguous approval that the old check
# false-flagged on the word "rejects".
REVIEW_1867 = (
    "✅ Commits 17a66d1: LGTM.\nFunctional: all 4 ACs implemented.\n"
    "- AC2: Query(min_length=1, max_length=100) rejects empty; in-handler "
    "category.isdigit() raises RequestValidationError for pure-digit input.\n"
    "NIT: test name says empty_or_invalid. Not blocking — the verify recipe covers it."
)

NOT_NEG = [
    REVIEW_1867,
    "Query rejects empty input",
    "the validator rejected the malformed request",
    "rejects invalid payloads with a 422",
    "rejection handling is correct",
    "Not blocking; non-blocking nit only",
    "✅ LGTM — all ACs implemented, no issues",
    "no error.message exposure in HTTP responses",
]

IS_NEG = [
    "❌ Functional: AC3 is broken",
    "review_outcome=changes_requested",
    "changes requested: please add a test",
    "I am requesting changes here",
    "request changes before merge",
    "I reject this PR",
    "we reject this approach",
    "rejecting this commit until the auth check is added",
    "this must be reworked",
    "must be rejected — the migration is missing",
]


@pytest.mark.parametrize("text", NOT_NEG)
def test_descriptive_reject_is_not_a_negative_verdict(text):
    assert RIO_RE.search(text) is None, f"false positive: {text!r}"
    assert SUP_RE.search(text) is None, f"false positive (supervisor): {text!r}"


@pytest.mark.parametrize("text", IS_NEG)
def test_genuine_negative_verdict_is_detected(text):
    assert RIO_RE.search(text) is not None, f"missed verdict: {text!r}"
    assert SUP_RE.search(text) is not None, f"missed verdict (supervisor): {text!r}"


def test_two_copies_stay_in_sync():
    assert RIO_RE.pattern == SUP_RE.pattern
