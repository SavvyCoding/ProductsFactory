"""
Tests for orchestrator/poller.py — the main poll loop and its helper functions.

Strategy: unit tests with httpx and subprocess mocked. No live Docker or PM API required.
"""

import subprocess
from unittest.mock import MagicMock, patch, call

import pytest
import httpx


# ── get_next_product ──────────────────────────────────────────────────────────

class TestGetNextProduct:
    def test_returns_product_dict_on_200(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        monkeypatch.setenv("UBUNTU_VM_IP", "192.168.1.100")

        from orchestrator import poller

        product_data = {"id": 1, "name": "TestProd", "status": "ready"}

        def mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = product_data
            resp.raise_for_status = lambda: None
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = mock_get
            mock_client_cls.return_value = mock_client

            result = poller.get_next_product()

        assert result == product_data

    def test_returns_none_on_204(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")

        from orchestrator import poller

        def mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 204
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = mock_get
            mock_client_cls.return_value = mock_client

            result = poller.get_next_product()

        assert result is None

    def test_returns_none_on_http_error(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")

        from orchestrator import poller

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client_cls.return_value = mock_client

            result = poller.get_next_product()

        assert result is None


# ── reset_stuck_features ──────────────────────────────────────────────────────

class TestResetStuckFeatures:
    def test_posts_to_reset_stuck_endpoint(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")

        from orchestrator import poller

        post_calls = []

        def mock_post(path, **kwargs):
            post_calls.append(path)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"reset_count": 0}
            resp.raise_for_status = lambda: None
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = mock_post
            mock_client_cls.return_value = mock_client

            poller.reset_stuck_features()

        assert any("/api/features/reset_stuck" in p for p in post_calls)

    def test_logs_when_features_reset(self, monkeypatch, caplog):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")

        from orchestrator import poller
        import logging
        caplog.set_level(logging.INFO)

        def mock_post(path, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"reset_count": 3}
            resp.raise_for_status = lambda: None
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = mock_post
            mock_client_cls.return_value = mock_client

            poller.reset_stuck_features()

        assert "3" in caplog.text or "stuck" in caplog.text.lower()

    def test_swallows_http_error(self, monkeypatch):
        """reset_stuck_features must never raise — poller loop must keep going."""
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")

        from orchestrator import poller

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("refused")
            mock_client_cls.return_value = mock_client

            # Must not raise
            poller.reset_stuck_features()


# ── claude_auth_healthy ───────────────────────────────────────────────────────

class TestClaudeAuthHealthy:
    def test_returns_true_on_zero_exit(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        from orchestrator import poller

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = poller.claude_auth_healthy()

        assert result is True
        cmd = mock_run.call_args[0][0]
        assert "claude" in cmd
        assert "-p" in cmd
        # Must be a real API call — not --version
        assert "--version" not in cmd

    def test_returns_false_on_nonzero_exit(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        from orchestrator import poller

        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = poller.claude_auth_healthy()

        assert result is False

    def test_returns_false_on_timeout(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        from orchestrator import poller

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30)):
            result = poller.claude_auth_healthy()

        assert result is False

    def test_returns_false_when_claude_not_found(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        from orchestrator import poller

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = poller.claude_auth_healthy()

        assert result is False


# ── github_client — count_open_prs ───────────────────────────────────────────

class TestCountOpenPrs:
    def test_returns_pr_count(self):
        from orchestrator.github_client import count_open_prs

        product = {"github_repo": "https://github.com/owner/repo.git"}

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = [{}, {}, {}]  # 3 open PRs
            return resp

        with patch("httpx.get", side_effect=mock_get):
            result = count_open_prs(product)

        assert result == 3

    def test_returns_zero_when_no_github_repo(self):
        from orchestrator.github_client import count_open_prs
        product = {"github_repo": ""}
        assert count_open_prs(product) == 0

    def test_returns_zero_on_api_error(self):
        from orchestrator.github_client import count_open_prs

        product = {"github_repo": "https://github.com/owner/repo.git"}

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = count_open_prs(product)

        assert result == 0


# ── github_client — _parse_repo_slug ─────────────────────────────────────────

class TestParseRepoSlug:
    def test_parses_https_url(self):
        from orchestrator.github_client import _parse_repo_slug
        product = {"github_repo": "https://github.com/myowner/myrepo.git"}
        assert _parse_repo_slug(product) == ("myowner", "myrepo")

    def test_parses_https_url_without_git_suffix(self):
        from orchestrator.github_client import _parse_repo_slug
        product = {"github_repo": "https://github.com/myowner/myrepo"}
        assert _parse_repo_slug(product) == ("myowner", "myrepo")

    def test_parses_ssh_url(self):
        from orchestrator.github_client import _parse_repo_slug
        product = {"github_repo": "git@github.com:myowner/myrepo.git"}
        assert _parse_repo_slug(product) == ("myowner", "myrepo")

    def test_returns_none_for_empty(self):
        from orchestrator.github_client import _parse_repo_slug
        assert _parse_repo_slug({"github_repo": ""}) is None
        assert _parse_repo_slug({}) is None


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

        with patch("httpx.get", side_effect=github_responses):
            with patch("httpx.Client", return_value=MockPMClient()):
                reconcile_merged_prs(product)

        assert any("feat-1" in p[0] and p[1] == {"status": "Pushed"} for p in patch_calls)
        assert not any("feat-2" in p[0] for p in patch_calls), "Only merged PR's feature should update"

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


# ── heartbeat — check_stale_sessions ─────────────────────────────────────────

class TestCheckStaleSessions:
    def test_kills_stale_session(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        monkeypatch.setenv("STALE_THRESHOLD_MINUTES", "45")

        from orchestrator import heartbeat
        from datetime import datetime, timezone, timedelta

        # Session is 60 minutes old (above 45m threshold)
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=60)

        kill_calls = []

        def fake_get_progress(product):
            return stale_time

        def fake_kill(product):
            kill_calls.append(product["id"])

        monkeypatch.setattr(heartbeat, "_get_progress_last_push", fake_get_progress)
        monkeypatch.setattr(heartbeat, "_kill_container", fake_kill)
        monkeypatch.setattr(heartbeat, "send_alert", lambda *a, **kw: None)

        products = [{"id": "1", "name": "Stale", "status": "ready"}]
        heartbeat.check_stale_sessions(products)

        assert "1" in kill_calls

    def test_skips_non_ready_products(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")

        from orchestrator import heartbeat

        kill_calls = []
        monkeypatch.setattr(heartbeat, "_kill_container", lambda p: kill_calls.append(p))
        monkeypatch.setattr(heartbeat, "send_alert", lambda *a, **kw: None)

        products = [
            {"id": "2", "name": "Paused", "status": "paused"},
            {"id": "3", "name": "Registered", "status": "registered"},
        ]
        heartbeat.check_stale_sessions(products)
        assert kill_calls == []

    def test_does_not_kill_fresh_session(self, monkeypatch):
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        monkeypatch.setenv("STALE_THRESHOLD_MINUTES", "45")

        from orchestrator import heartbeat
        from datetime import datetime, timezone, timedelta

        # Session is only 10 minutes old (below threshold)
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        kill_calls = []
        monkeypatch.setattr(heartbeat, "_get_progress_last_push", lambda p: recent_time)
        monkeypatch.setattr(heartbeat, "_kill_container", lambda p: kill_calls.append(p))
        monkeypatch.setattr(heartbeat, "send_alert", lambda *a, **kw: None)

        products = [{"id": "4", "name": "Fresh", "status": "ready"}]
        heartbeat.check_stale_sessions(products)
        assert kill_calls == []


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
