"""Tests for orchestrator.supervisor.detect_orphaned_prs (2026-08-20).

An open session PR whose every non-terminal tracking feature is dependency-
frozen in a coder-owned state is orphaned: no persona can ever process it,
and it deadlocks the PR-serialization gate against the dependency gate.
Canonical: MyGroceryApp #3457 / PR #171 (6-day stall).

The detector closes the PR and clears pr_number/pr_url/branch_name on the
tracking features WITHOUT touching their status (they are legitimately
waiting on a dependency).
"""
from __future__ import annotations

import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import pytest

from orchestrator import supervisor


OLD = "2026-08-01T00:00:00Z"  # comfortably past any age/idle threshold


def _cfg(**over):
    cfg = dict(supervisor._DEFAULTS)
    cfg.update(over)
    return cfg


def _pr(number=171, created_at=OLD, last_commit_at=OLD, mergeable_state="clean"):
    return {"number": number, "title": f"session PR {number}",
            "created_at": created_at, "last_commit_at": last_commit_at,
            "mergeable_state": mergeable_state}


def _feat(fid, status, pr_number=None, depends_on=None):
    return {"id": fid, "status": status, "pr_number": pr_number,
            "depends_on": depends_on}


class _FakePMClient:
    """Captures PATCH calls issued to the PM API."""
    patches: list = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def patch(self, url, json=None):
        _FakePMClient.patches.append((url, json))
        class _R:  # noqa: N801
            is_success = True
        return _R()


@pytest.fixture
def harness(monkeypatch):
    """Patch config, action-audit plumbing, and the PM client; return the
    captured record/patch lists."""
    records: list = []
    _FakePMClient.patches = []
    monkeypatch.setattr(supervisor, "_get_supervisor_config", lambda: _cfg())
    monkeypatch.setattr(supervisor, "_recent_action", lambda **kw: False)
    monkeypatch.setattr(supervisor, "_record_action",
                        lambda **kw: records.append(kw))
    monkeypatch.setattr(supervisor.httpx, "Client", _FakePMClient)
    return records


def _run(features, prs=None, **kw):
    return supervisor.detect_orphaned_prs(
        product_id=35, github_repo="https://github.com/org/repo",
        open_prs_with_state=prs if prs is not None else [_pr()],
        features=features, github_token=None, **kw,
    )


class TestOrphanDetection:
    def test_closes_orphaned_pr_and_clears_pointers(self, harness):
        # 3457 carries PR 171, frozen on 3456 (Designed ≠ Pushed) → orphan.
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
        ]
        assert _run(features) == 1
        assert harness and harness[0]["detector"] == "orphan_pr_close"
        assert harness[0]["target_id"] == 171
        # pointer cleared, status untouched
        assert _FakePMClient.patches == [
            ("/api/features/3457",
             {"pr_number": None, "pr_url": None, "branch_name": None,
              "changed_by": "supervisor"}),
        ]

    def test_unfrozen_tracker_is_not_orphan(self, harness):
        # No depends_on → rework path can claim it; PR is NOT orphaned.
        features = [_feat(3457, "Designed", pr_number=171)]
        assert _run(features) == 0
        assert not harness

    def test_reviewer_owned_tracker_is_not_orphan(self, harness):
        # Frozen but in Reviewing → reviewer will move the PR; not orphaned.
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Reviewing", pr_number=171, depends_on=3456),
        ]
        assert _run(features) == 0

    def test_mixed_trackers_one_claimable_not_orphan(self, harness):
        # Two features on the PR; one frozen, one claimable → not orphaned.
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
            _feat(3458, "Designed", pr_number=171),
        ]
        assert _run(features) == 0

    def test_untracked_pr_skipped(self, harness):
        # No feature points at the PR → overlap/reconciler territory, skip.
        # (dep-blocked set must be non-empty or the detector bails early —
        # 3438's frozen dep proves the gate is live without tracking PR 171.)
        features = [
            _feat(3435, "Blocked"),
            _feat(3438, "Designed", depends_on=3435),
        ]
        assert _run(features) == 0

    def test_pushed_tracker_ignored(self, harness):
        # A Pushed row still holding pr_number must not veto orphan-hood.
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
            _feat(3400, "Pushed", pr_number=171),
        ]
        assert _run(features) == 1

    def test_young_pr_not_closed(self, harness):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
        ]
        assert _run(features, prs=[_pr(created_at=now_iso,
                                       last_commit_at=now_iso)]) == 0

    def test_dry_run_records_but_does_not_mutate(self, harness, monkeypatch):
        monkeypatch.setattr(supervisor, "_get_supervisor_config",
                            lambda: _cfg(supervisor_dry_run_only=True))
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
        ]
        assert _run(features) == 1
        assert harness[0]["dry_run"] is True
        assert _FakePMClient.patches == []

    def test_disabled_flag_short_circuits(self, harness, monkeypatch):
        monkeypatch.setattr(supervisor, "_get_supervisor_config",
                            lambda: _cfg(supervisor_orphan_pr_enabled=False))
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
        ]
        assert _run(features) == 0
        assert not harness

    def test_recent_action_dedupes(self, harness, monkeypatch):
        monkeypatch.setattr(supervisor, "_recent_action", lambda **kw: True)
        features = [
            _feat(3456, "Designed"),
            _feat(3457, "Designed", pr_number=171, depends_on=3456),
        ]
        assert _run(features) == 0
