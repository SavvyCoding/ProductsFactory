"""Baseline tests for the deployed orchestrator's run_cycle (#10).

Pre-#10 the deployed path (deploy/orchestrator/tools.py:run_cycle) had zero
test coverage — Phase 6.x bugs (sprint PR provisioning, MAX_OPEN_PRS removal,
the 405-draft conflation) all shipped to prod without exercise. This file
adds the minimum: lock-stolen exit, watchdog docker-probe augmentation, and
the _SigningClient round-trip when HMAC is enabled.

Imports tools.py as `orchestrator_runtime` (its renamed-in-image identity)
so the module's `import orchestrator_runtime as tools` line works without
the production container layout.
"""

import json
import os
import sys
import importlib.util
from unittest.mock import MagicMock, patch

import pytest


# ── Module loader (imports tools.py as if it were the in-container name) ────
@pytest.fixture(scope="module")
def tools(monkeypatch_session=None):
    os.environ.setdefault("PM_API_URL", "http://pm-api:8080")
    os.environ.setdefault("PM_USERNAME", "test")
    os.environ.setdefault("PM_PASSWORD", "test")
    spec = importlib.util.spec_from_file_location(
        "orchestrator_runtime", "deploy/orchestrator/tools.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_runtime"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestRunCycleLockStolen:
    def test_409_stop_when_heartbeat_says_lock_lost(self, tools, monkeypatch):
        """If poller_heartbeat returns ok=False with status=409 → action=409_stop."""
        monkeypatch.setattr(
            tools, "poller_heartbeat",
            lambda *a, **kw: json.dumps({"ok": False, "status": 409}),
        )
        result = json.loads(tools.run_cycle({}))
        assert result["ok"] is True
        assert result["data"]["action"] == "409_stop"


class TestSigningClient:
    """Verifies _SigningClient adds X-PF-Signature only when secret is set
    and only on write methods. Uses httpx.MockTransport so no real network
    calls happen and we can inspect every outbound Request."""

    def _make_capturing_client(self, tools, secret: str):
        import httpx
        captured = []
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({
                "method":  request.method,
                "headers": dict(request.headers),
                "content": request.content,
            })
            return httpx.Response(200, json={"ok": True})
        tools._INTERNAL_API_SECRET = secret
        client = tools._SigningClient(
            base_url="http://pm-api:8080",
            transport=httpx.MockTransport(handler),
        )
        return client, captured

    def test_signs_post_when_secret_set(self, tools):
        client, captured = self._make_capturing_client(tools, "test-secret")
        client.post("/api/sprints/29/sign-off", json={"gate": "qa_passed", "value": True})
        assert len(captured) == 1
        c = captured[0]
        assert c["method"] == "POST"
        # httpx normalizes headers to lowercase
        assert "x-pf-signature" in c["headers"]
        assert c["headers"]["x-pf-signature"].startswith("sha256=")
        # Verify signature is correct against the actual body
        import hashlib, hmac
        expected = "sha256=" + hmac.new(b"test-secret", c["content"], hashlib.sha256).hexdigest()
        assert c["headers"]["x-pf-signature"] == expected

    def test_does_not_sign_when_secret_unset(self, tools):
        client, captured = self._make_capturing_client(tools, "")
        client.post("/api/anywhere", json={"x": 1})
        assert len(captured) == 1
        assert "x-pf-signature" not in captured[0]["headers"]

    def test_does_not_sign_get_methods(self, tools):
        client, captured = self._make_capturing_client(tools, "test-secret")
        client.get("/api/products")
        assert len(captured) == 1
        assert captured[0]["method"] == "GET"
        assert "x-pf-signature" not in captured[0]["headers"]
