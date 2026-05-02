"""Tests for orchestrator/sprint_pr.py — sprint branch + draft PR provisioning."""

from unittest.mock import MagicMock, patch

import pytest


class TestParseRepoSlug:
    def test_parses_https_url(self):
        from orchestrator.sprint_pr import _parse_repo_slug
        assert _parse_repo_slug("https://github.com/myowner/myrepo.git") == ("myowner", "myrepo")

    def test_parses_https_url_without_git_suffix(self):
        from orchestrator.sprint_pr import _parse_repo_slug
        assert _parse_repo_slug("https://github.com/myowner/myrepo") == ("myowner", "myrepo")

    def test_parses_ssh_url(self):
        from orchestrator.sprint_pr import _parse_repo_slug
        assert _parse_repo_slug("git@github.com:myowner/myrepo.git") == ("myowner", "myrepo")

    def test_returns_none_for_empty(self):
        from orchestrator.sprint_pr import _parse_repo_slug
        assert _parse_repo_slug("") is None
        assert _parse_repo_slug(None) is None  # type: ignore[arg-type]


class TestProvisionSprintPr:
    def test_returns_none_when_repo_unparseable(self):
        from orchestrator.sprint_pr import provision_sprint_pr
        assert provision_sprint_pr("", 1, "S1", None, [], "tok") is None

    def test_returns_none_when_token_missing(self):
        from orchestrator.sprint_pr import provision_sprint_pr
        assert provision_sprint_pr("https://github.com/o/r.git", 1, "S1", None, [], "") is None

    def test_creates_branch_and_pr_on_happy_path(self):
        from orchestrator import sprint_pr

        repo_resp   = MagicMock(status_code=200, json=MagicMock(return_value={"default_branch": "main"}))
        ref_resp    = MagicMock(status_code=200, json=MagicMock(return_value={"object": {"sha": "abc123"}}))
        create_resp = MagicMock(status_code=201, text="")
        existing    = MagicMock(status_code=404)
        put_resp    = MagicMock(status_code=201, text="")
        existing_pr = MagicMock(status_code=200, json=MagicMock(return_value=[]))
        pr_resp     = MagicMock(
            status_code=201,
            json=MagicMock(return_value={"number": 42, "html_url": "https://github.com/o/r/pull/42"}),
        )

        with patch.object(sprint_pr.httpx, "get", side_effect=[repo_resp, ref_resp, existing, existing_pr]), \
             patch.object(sprint_pr.httpx, "post", side_effect=[create_resp, pr_resp]), \
             patch.object(sprint_pr.httpx, "put", return_value=put_resp):
            result = sprint_pr.provision_sprint_pr(
                "https://github.com/o/r.git", 7, "Sprint 7", "Goal text", ["feat one", "feat two"], "tok",
            )
        assert result == {"branch": "sprint/7", "number": 42, "url": "https://github.com/o/r/pull/42"}

    def test_returns_existing_open_pr_on_reactivation(self):
        from orchestrator import sprint_pr

        repo_resp    = MagicMock(status_code=200, json=MagicMock(return_value={"default_branch": "main"}))
        ref_resp     = MagicMock(status_code=200, json=MagicMock(return_value={"object": {"sha": "abc"}}))
        create_resp  = MagicMock(status_code=422, text="branch exists")  # idempotent re-entry
        existing     = MagicMock(status_code=200, json=MagicMock(return_value={"sha": "manifest_sha"}))
        put_resp     = MagicMock(status_code=200, text="")
        existing_pr  = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"number": 13, "html_url": "https://github.com/o/r/pull/13"}]),
        )

        with patch.object(sprint_pr.httpx, "get", side_effect=[repo_resp, ref_resp, existing, existing_pr]), \
             patch.object(sprint_pr.httpx, "post", side_effect=[create_resp]), \
             patch.object(sprint_pr.httpx, "put", return_value=put_resp):
            result = sprint_pr.provision_sprint_pr(
                "https://github.com/o/r.git", 7, "Sprint 7", "G", ["a"], "tok",
            )
        assert result == {"branch": "sprint/7", "number": 13, "url": "https://github.com/o/r/pull/13"}


class TestMergeSprintPr:
    def test_returns_zero_when_inputs_missing(self):
        from orchestrator.sprint_pr import merge_sprint_pr
        code, _ = merge_sprint_pr("", 1, "tok")
        assert code == 0
        code, _ = merge_sprint_pr("https://github.com/o/r.git", 0, "tok")
        assert code == 0
        code, _ = merge_sprint_pr("https://github.com/o/r.git", 1, "")
        assert code == 0

    def test_marks_ready_updates_branch_then_merges_squash(self):
        """Happy path: un-draft, update-branch up-to-date (422), squash-merge."""
        from orchestrator import sprint_pr

        ready = MagicMock(status_code=200, text="")
        # update-branch returns 422 = already up-to-date so no sleep
        upd_branch = MagicMock(status_code=422, text='{"message": "branch is up to date"}')
        merge = MagicMock(status_code=200, text="merged")
        with patch.object(sprint_pr.httpx, "patch", return_value=ready) as mock_patch, \
             patch.object(sprint_pr.httpx, "put", side_effect=[upd_branch, merge]) as mock_put:
            code, body = sprint_pr.merge_sprint_pr("https://github.com/o/r.git", 42, "tok")
        assert code == 200
        assert body == "merged"
        # Mark ready first
        assert "/pulls/42" in mock_patch.call_args.args[0]
        assert mock_patch.call_args.kwargs["json"] == {"draft": False}
        # update-branch happens before merge
        assert "/pulls/42/update-branch" in mock_put.call_args_list[0].args[0]
        # Then squash-merge
        assert "/pulls/42/merge" in mock_put.call_args_list[1].args[0]
        assert mock_put.call_args_list[1].kwargs["json"] == {"merge_method": "squash"}

    def test_update_branch_202_sleeps_then_merges(self):
        """update-branch 202 = job queued; sleeps 5s for GitHub to recompute."""
        from orchestrator import sprint_pr
        ready = MagicMock(status_code=200, text="")
        upd_branch = MagicMock(status_code=202, text="queued")
        merge = MagicMock(status_code=200, text="merged")
        # Patch time.sleep so test doesn't actually wait 5s
        with patch.object(sprint_pr.httpx, "patch", return_value=ready), \
             patch.object(sprint_pr.httpx, "put", side_effect=[upd_branch, merge]), \
             patch("time.sleep") as mock_sleep:
            code, _ = sprint_pr.merge_sprint_pr("https://github.com/o/r.git", 42, "tok")
        assert code == 200
        mock_sleep.assert_called_once_with(5)

    def test_returns_405_unchanged_on_conflict(self):
        from orchestrator import sprint_pr
        ready = MagicMock(status_code=200, text="")
        merge = MagicMock(status_code=405, text='{"message": "PR is not mergeable"}')
        with patch.object(sprint_pr.httpx, "patch", return_value=ready), \
             patch.object(sprint_pr.httpx, "put", return_value=merge):
            code, body = sprint_pr.merge_sprint_pr("https://github.com/o/r.git", 42, "tok")
        assert code == 405
        assert "not mergeable" in body

    def test_returns_422_when_already_merged(self):
        from orchestrator import sprint_pr
        ready = MagicMock(status_code=422, text="already merged")
        merge = MagicMock(status_code=422, text="already merged")
        with patch.object(sprint_pr.httpx, "patch", return_value=ready), \
             patch.object(sprint_pr.httpx, "put", return_value=merge):
            code, _ = sprint_pr.merge_sprint_pr("https://github.com/o/r.git", 42, "tok")
        assert code == 422

    def test_transport_exception_returns_zero(self):
        from orchestrator import sprint_pr
        with patch.object(sprint_pr.httpx, "patch", side_effect=Exception("network down")):
            code, body = sprint_pr.merge_sprint_pr("https://github.com/o/r.git", 42, "tok")
        assert code == 0
        assert "exception" in body


class TestCheckOpenPrInvariant:
    def setup_method(self):
        from orchestrator.sprint_pr import _alerted_excess_prs
        _alerted_excess_prs.clear()

    def test_no_alert_when_count_one(self):
        from orchestrator.sprint_pr import check_open_pr_invariant
        calls = []
        check_open_pr_invariant({"id": 1, "name": "P"}, 1, alerter=lambda lvl, msg: calls.append((lvl, msg)))
        assert calls == []

    def test_no_alert_when_count_zero(self):
        from orchestrator.sprint_pr import check_open_pr_invariant
        calls = []
        check_open_pr_invariant({"id": 1, "name": "P"}, 0, alerter=lambda lvl, msg: calls.append((lvl, msg)))
        assert calls == []

    def test_alerts_on_count_above_one(self):
        from orchestrator.sprint_pr import check_open_pr_invariant
        calls = []
        check_open_pr_invariant({"id": 1, "name": "MyProd"}, 3, alerter=lambda lvl, msg: calls.append((lvl, msg)))
        assert len(calls) == 1
        level, msg = calls[0]
        assert level == "warning"
        assert "MyProd" in msg
        assert "3" in msg

    def test_idempotent_per_run(self):
        from orchestrator.sprint_pr import check_open_pr_invariant
        calls = []
        alerter = lambda lvl, msg: calls.append((lvl, msg))
        for _ in range(5):
            check_open_pr_invariant({"id": 1, "name": "P"}, 2, alerter=alerter)
        assert len(calls) == 1

    def test_rearms_after_recovery(self):
        from orchestrator.sprint_pr import check_open_pr_invariant
        calls = []
        alerter = lambda lvl, msg: calls.append((lvl, msg))
        check_open_pr_invariant({"id": 1, "name": "P"}, 2, alerter=alerter)
        check_open_pr_invariant({"id": 1, "name": "P"}, 1, alerter=alerter)  # recovered
        check_open_pr_invariant({"id": 1, "name": "P"}, 2, alerter=alerter)  # spike again
        assert len(calls) == 2


class TestAutoMergeDraftHandling:
    """Regression tests for the 405-draft conflation bug.

    Sprint PRs (Phase 6.1) are provisioned as drafts. Pre-fix, the per-cycle
    auto-merge sweep AND the post-coder _auto_merge_approved both treated 405
    "draft" the same as 405 "conflict" → closed the PR. After the fix:
    405-draft → un-draft + retry once; only persistent 405 closes.
    """
    def test_sweep_un_drafts_then_merges(self):
        from orchestrator import auto_merge

        first  = MagicMock(status_code=405, text='{"message": "Pull Request is still a draft"}')
        second = MagicMock(status_code=200, text='{"merged": true}')
        un_draft = MagicMock(status_code=200, text="")

        with patch.object(auto_merge.httpx, "put", side_effect=[first, second]) as mock_put, \
             patch.object(auto_merge.httpx, "patch", return_value=un_draft) as mock_patch:
            code, _ = auto_merge._try_merge_pr("o/r", 42, "tok")
        assert code == 200
        # Two merges (initial + retry)
        assert mock_put.call_count == 2
        # One un-draft PATCH between them
        assert mock_patch.call_count == 1
        assert mock_patch.call_args.kwargs["json"] == {"draft": False}

    def test_sweep_does_not_retry_on_real_405_conflict(self):
        from orchestrator import auto_merge

        # 405 without "draft" in body = real merge conflict / failing CI / branch protection
        conflict = MagicMock(status_code=405, text='{"message": "Pull Request is not mergeable"}')

        with patch.object(auto_merge.httpx, "put", return_value=conflict) as mock_put, \
             patch.object(auto_merge.httpx, "patch") as mock_patch:
            code, body = auto_merge._try_merge_pr("o/r", 42, "tok")
        assert code == 405
        assert "not mergeable" in body
        # Single attempt, no un-draft (the caller decides what to do with 405)
        assert mock_put.call_count == 1
        assert mock_patch.call_count == 0

    def test_sweep_passes_through_200(self):
        from orchestrator import auto_merge

        ok = MagicMock(status_code=200, text="merged")
        with patch.object(auto_merge.httpx, "put", return_value=ok) as mock_put, \
             patch.object(auto_merge.httpx, "patch") as mock_patch:
            code, _ = auto_merge._try_merge_pr("o/r", 42, "tok")
        assert code == 200
        assert mock_put.call_count == 1
        assert mock_patch.call_count == 0
