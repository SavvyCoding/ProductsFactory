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
