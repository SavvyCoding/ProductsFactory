"""
Tests for orchestrator helper modules — github_client, heartbeat, alerts.

Previously this file was tests/test_poller.py; the legacy host-mode poller
was retired 2026-05-18 (deprecation guard in commit 648ab33, full deletion
in this PR). The three test classes that targeted poller.get_next_product,
claude_auth_healthy, and reset_stuck_features went with it — those code
paths now live in deploy/orchestrator/tools.py + deploy/orchestrator/
orchestrate.py and are covered by integration testing of the live
pf-orchestrator container, not by these unit tests.

The five surviving test classes here exercise the shared helper modules
that both the legacy and live paths use: github_client, heartbeat, alerts.

Strategy: unit tests with httpx and subprocess mocked. No live Docker or PM API required.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
import httpx


# Module-level: orchestrator.integrations.github reads PM_API_URL at import
# time, and several tests in this file trigger that import via
# `from orchestrator.github_client import ...` outside a monkeypatch context.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")


# ── integrations.github — _parse_repo_slug (canonical) ───────────────────────
# Previously defined 3x with incompatible return shapes (tuple|None, str,
# str|None) across github_client, integrations/github, and auto_merge.
# Drift-cleanup pass collapsed them onto integrations.github with one
# canonical contract: input is the URL string, output is "owner/repo" or None.
# Re-exported from github_client / auto_merge / docker_runner for callers
# that already imported it from those modules.

class TestParseRepoSlug:
    def test_parses_https_url(self):
        from orchestrator.integrations.github import _parse_repo_slug
        assert _parse_repo_slug("https://github.com/myowner/myrepo.git") == "myowner/myrepo"

    def test_parses_https_url_without_git_suffix(self):
        from orchestrator.integrations.github import _parse_repo_slug
        assert _parse_repo_slug("https://github.com/myowner/myrepo") == "myowner/myrepo"

    def test_parses_ssh_url(self):
        from orchestrator.integrations.github import _parse_repo_slug
        assert _parse_repo_slug("git@github.com:myowner/myrepo.git") == "myowner/myrepo"

    def test_returns_none_for_empty(self):
        from orchestrator.integrations.github import _parse_repo_slug
        assert _parse_repo_slug("") is None

    def test_returns_none_for_unparseable(self):
        from orchestrator.integrations.github import _parse_repo_slug
        # Regression: previous integrations/github version returned the
        # input verbatim on no-match, which silently corrupted callers'
        # URL formatting. Now: None forces an explicit caller guard.
        assert _parse_repo_slug("not-a-url") is None

    def test_reexported_from_legacy_modules(self):
        """Importers of the old locations still resolve to the canonical."""
        from orchestrator.github_client import _parse_repo_slug as gc_slug
        from orchestrator.auto_merge import _parse_repo_slug as am_slug
        from orchestrator.integrations.github import _parse_repo_slug as canon
        assert gc_slug is canon
        assert am_slug is canon


# ── github_client — reconcile_merged_prs ─────────────────────────────────────

class TestReconcileMergedPrs:
    def test_updates_merged_features_to_pushed(self):
        from orchestrator.github_client import reconcile_merged_prs

        product = {
            "id": "1",
            "github_repo": "https://github.com/owner/repo.git",
        }

        # GitHub returns 1 merged PR with number 42
        github_responses = [
            MagicMock(
                status_code=200,
                json=lambda: [{"number": 42, "merged_at": "2024-01-01T00:00:00Z"}],
            )
        ]

        patch_calls = []

        class MockPMClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, path, **kwargs):
                resp = MagicMock()
                resp.json.return_value = [
                    {"id": "feat-1", "pr_number": 42, "status": "Implementing"},
                    {"id": "feat-2", "pr_number": 99, "status": "Implementing"},
                ]
                return resp
            def patch(self, path, json=None, **kwargs):
                patch_calls.append((path, json))
                return MagicMock()

        with patch("orchestrator.github_client._gh_get", side_effect=github_responses):
            with patch("httpx.Client", return_value=MockPMClient()):
                reconcile_merged_prs(product)

        assert any("feat-1" in p[0] and p[1].get("status") == "Pushed" for p in patch_calls)
        assert not any("feat-2" in p[0] for p in patch_calls), "Only merged PR's feature should update"

    def test_closed_pr_at_cap_blocks_feature(self, monkeypatch):
        """A closed-without-merge PR on a feature at the fix-attempts cap
        must PATCH the feature to Blocked (and no sprint routing — the
        Blocked-sprint holdpen was retired with migration 043)."""
        monkeypatch.setenv("MAX_FIX_ATTEMPTS", "5")
        from orchestrator.github_client import reconcile_in_flight_prs

        product = {"id": 42, "github_repo": "https://github.com/owner/repo.git"}

        # One in-flight feature one bump away from cap, with a closed PR.
        feature = {"id": 99, "status": "Implementing", "pr_number": 7, "fix_attempts": 4}
        pr_resp = MagicMock(status_code=200, json=lambda: {"state": "closed", "merged_at": None})

        recorded = []

        class MockPMClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, path, **kwargs):
                resp = MagicMock(status_code=200)
                resp.json.return_value = [feature]
                return resp
            def patch(self, path, json=None, **kwargs):
                recorded.append(("PATCH", path, json))
                return MagicMock(status_code=200)
            def post(self, path, json=None, **kwargs):
                recorded.append(("POST", path, json))
                return MagicMock(status_code=200, json=lambda: {})

        with patch("orchestrator.github_client._gh_get", return_value=pr_resp), \
             patch("httpx.Client", return_value=MockPMClient()):
            reconcile_in_flight_prs(product)

        # PATCH must have flipped feature to Blocked
        patch_calls = [c for c in recorded if c[0] == "PATCH"]
        assert any(c[2].get("status") == "Blocked" for c in patch_calls), \
            f"Expected Blocked PATCH, got {patch_calls}"

        # Retired sprint routing must NOT fire (endpoint no longer exists)
        route_calls = [c for c in recorded if c[0] == "POST" and "blocked/route" in c[1]]
        assert not route_calls, f"Retired sprint routing fired: {route_calls}"

    def test_skips_when_no_merged_prs(self):
        from orchestrator.github_client import reconcile_merged_prs

        product = {
            "id": "1",
            "github_repo": "https://github.com/owner/repo.git",
        }

        patch_calls = []

        with patch("httpx.get", return_value=MagicMock(
            status_code=200,
            json=lambda: [{"number": 1, "merged_at": None}],  # open, not merged
        )):
            with patch("httpx.Client") as mock_client_cls:
                mock_client_cls.return_value.__enter__ = lambda s: MagicMock(
                    get=MagicMock(return_value=MagicMock(json=lambda: [])),
                    patch=lambda *a, **kw: patch_calls.append(a),
                )
                mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
                reconcile_merged_prs(product)

        assert patch_calls == []


# The progress.md-push-timestamp heartbeat (orchestrator/heartbeat.py,
# check_stale_sessions) was removed 2026-05-28 — never wired into the
# cycle loop, superseded by the watchdog. Its tests went with it.


# ── alerts ────────────────────────────────────────────────────────────────────

class TestAlerts:
    def test_posts_to_webhook(self, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.com/alert")

        from orchestrator import alerts
        monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "https://hooks.example.com/alert")
        monkeypatch.setattr(alerts, "WEBHOOK_FAIL_COUNTER", 0)

        post_calls = []

        def fake_post(url, json=None, timeout=None):
            post_calls.append((url, json))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=fake_post):
            alerts.send_alert("error", "test message")

        assert len(post_calls) == 1
        assert "test message" in str(post_calls[0][1])

    def test_no_webhook_call_when_url_not_set(self, monkeypatch):
        from orchestrator import alerts
        monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "")
        monkeypatch.setattr(alerts, "PM_API_URL", "")
        monkeypatch.setattr(alerts, "_cached_webhook_url", None)

        post_calls = []
        with patch("httpx.post", side_effect=lambda *a, **kw: post_calls.append(a)):
            alerts.send_alert("info", "no webhook configured")

        assert post_calls == []

    def test_increments_fail_counter_on_bad_status(self, monkeypatch):
        from orchestrator import alerts
        monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "https://hooks.example.com/alert")
        monkeypatch.setattr(alerts, "WEBHOOK_FAIL_COUNTER", 0)

        def fake_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        with patch("httpx.post", side_effect=fake_post):
            alerts.send_alert("error", "failing webhook")

        assert alerts.WEBHOOK_FAIL_COUNTER == 1

    def test_resets_fail_counter_on_success(self, monkeypatch):
        from orchestrator import alerts
        monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "https://hooks.example.com/alert")
        monkeypatch.setattr(alerts, "WEBHOOK_FAIL_COUNTER", 2)  # was partially failing

        def fake_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=fake_post):
            alerts.send_alert("info", "success now")

        assert alerts.WEBHOOK_FAIL_COUNTER == 0

    def test_level_prefix_in_message(self, monkeypatch):
        from orchestrator import alerts
        monkeypatch.setattr(alerts, "ALERT_WEBHOOK_URL", "")

        # Just verify it doesn't raise for all levels
        for level in ("info", "warning", "error", "critical"):
            alerts.send_alert(level, "test")
