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

    def test_marks_ready_then_merges_squash(self):
        from orchestrator import sprint_pr

        ready = MagicMock(status_code=200, text="")
        merge = MagicMock(status_code=200, text="merged")
        with patch.object(sprint_pr.httpx, "patch", return_value=ready) as mock_patch, \
             patch.object(sprint_pr.httpx, "put", return_value=merge) as mock_put:
            code, body = sprint_pr.merge_sprint_pr("https://github.com/o/r.git", 42, "tok")
        assert code == 200
        assert body == "merged"
        # Mark ready first
        assert "/pulls/42" in mock_patch.call_args.args[0]
        assert mock_patch.call_args.kwargs["json"] == {"draft": False}
        # Then squash-merge
        assert "/pulls/42/merge" in mock_put.call_args.args[0]
        assert mock_put.call_args.kwargs["json"] == {"merge_method": "squash"}

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
