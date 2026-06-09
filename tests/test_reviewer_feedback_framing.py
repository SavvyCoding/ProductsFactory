"""_format_reviewer_feedback frames the workspace by whether a prior branch was
actually PUSHED — not by bare fix_attempts. A pre-push gate bounce (lint-guard/
test-check/verify-check, all before the push) leaves no branch, so the coder
starts on a clean default branch and must reimplement, not 'patch in place'."""
import os
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import orchestrator.docker_runner as dr


def _patch_comments(monkeypatch, body="fix the import"):
    monkeypatch.setattr(
        dr, "_fetch_recent_review_comments",
        lambda fid, limit=25: [{"created_at": "2026-06-08T00:00:00",
                                "author": "lint-guard", "body": body}],
    )


class TestReviewerFeedbackFraming:
    def test_fresh_feature_returns_empty(self, monkeypatch):
        _patch_comments(monkeypatch)
        feats = [{"id": 1, "name": "x", "review_outcome": None, "fix_attempts": 0}]
        assert dr._format_reviewer_feedback(feats) == ""

    def test_pushed_branch_says_patch_in_place(self, monkeypatch):
        _patch_comments(monkeypatch)
        feats = [{"id": 1, "name": "x", "review_outcome": "changes_requested",
                  "fix_attempts": 1, "branch_name": "coder/abc123"}]
        out = dr._format_reviewer_feedback(feats)
        assert "Patch the listed items in place" in out
        assert "failed a pre-push gate" not in out

    def test_pr_number_also_counts_as_pushed(self, monkeypatch):
        _patch_comments(monkeypatch)
        feats = [{"id": 1, "name": "x", "review_outcome": "changes_requested",
                  "fix_attempts": 1, "pr_number": 7}]
        assert "Patch the listed items in place" in dr._format_reviewer_feedback(feats)

    def test_no_branch_says_reimplement(self, monkeypatch):
        _patch_comments(monkeypatch)
        feats = [{"id": 1, "name": "x", "review_outcome": "changes_requested",
                  "fix_attempts": 2, "branch_name": None, "pr_number": None}]
        out = dr._format_reviewer_feedback(feats)
        assert "Reimplement the feature cleanly" in out
        assert "failed a pre-push gate" in out
        assert "Patch the listed items in place" not in out

    def test_no_comments_returns_empty(self, monkeypatch):
        monkeypatch.setattr(dr, "_fetch_recent_review_comments",
                            lambda fid, limit=25: [])
        feats = [{"id": 1, "name": "x", "review_outcome": "changes_requested",
                  "fix_attempts": 1, "branch_name": "coder/abc"}]
        assert dr._format_reviewer_feedback(feats) == ""
