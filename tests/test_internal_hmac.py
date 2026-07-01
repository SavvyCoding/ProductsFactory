"""Tests for the opt-in HMAC signing/verification (Phase #9)."""

import os
import json
from unittest.mock import AsyncMock, patch

import pytest


class TestSignatureComputation:
    def test_compute_signature_deterministic(self):
        from website.auth import _compute_internal_signature
        body = b'{"feature_ids": [1, 2]}'
        sig1 = _compute_internal_signature(body, "secret")
        sig2 = _compute_internal_signature(body, "secret")
        assert sig1 == sig2
        assert sig1.startswith("sha256=")

    def test_different_body_different_sig(self):
        from website.auth import _compute_internal_signature
        sig_a = _compute_internal_signature(b'{"x": 1}', "secret")
        sig_b = _compute_internal_signature(b'{"x": 2}', "secret")
        assert sig_a != sig_b

    def test_different_secret_different_sig(self):
        from website.auth import _compute_internal_signature
        body = b'{"x": 1}'
        sig_a = _compute_internal_signature(body, "secret-A")
        sig_b = _compute_internal_signature(body, "secret-B")
        assert sig_a != sig_b


class TestRoundTrip:
    """Sign on the orchestrator side, verify on the website side — same secret."""
    def test_orchestrator_sig_matches_website_sig(self):
        from website.auth import _compute_internal_signature
        # Simulate orchestrator-side signing (deploy/orchestrator/tools.py:_sign_internal_body)
        # — same algorithm, hand-computed here to avoid importing tools.py
        # which requires `import orchestrator_runtime as tools` (only exists in container).
        import hashlib, hmac
        secret = "shared-secret-xyz"
        body = json.dumps({"feature_ids": [42], "reason": "test"}).encode()
        orch_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        web_sig = _compute_internal_signature(body, secret)
        assert orch_sig == web_sig


class TestVerifyDependency:
    """The FastAPI dependency: opt-in based on env var, raises 401 on mismatch."""

    @pytest.fixture(autouse=True)
    def _restore_auth_module(self):
        # Each test below sets PF_INTERNAL_API_SECRET then importlib.reload()s
        # website.auth to re-capture the module-level _INTERNAL_API_SECRET.
        # monkeypatch restores the ENV VAR on teardown but NOT the reloaded
        # module var, so the test secret leaked and made every later file's
        # internal-signature endpoint 401 (caught test_website's terminal-state
        # guard in the full-suite run). Reload once more with the env cleared so
        # _INTERNAL_API_SECRET returns to its "" default.
        yield
        import os, importlib, website.auth
        os.environ.pop("PF_INTERNAL_API_SECRET", None)
        importlib.reload(website.auth)

    @pytest.mark.asyncio
    async def test_noop_when_secret_unset(self, monkeypatch):
        monkeypatch.delenv("PF_INTERNAL_API_SECRET", raising=False)
        # Re-import to pick up env change
        import importlib, website.auth
        importlib.reload(website.auth)
        from website.auth import verify_internal_signature
        request = AsyncMock()
        # No header, no body — should silently no-op
        result = await verify_internal_signature(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_401_when_signature_missing(self, monkeypatch):
        monkeypatch.setenv("PF_INTERNAL_API_SECRET", "test-secret")
        import importlib, website.auth
        importlib.reload(website.auth)
        from website.auth import verify_internal_signature
        from fastapi import HTTPException
        request = AsyncMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc:
            await verify_internal_signature(request)
        assert exc.value.status_code == 401
        assert "missing" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_accepts_valid_signature(self, monkeypatch):
        monkeypatch.setenv("PF_INTERNAL_API_SECRET", "test-secret")
        import importlib, website.auth
        importlib.reload(website.auth)
        from website.auth import verify_internal_signature, _compute_internal_signature
        body = b'{"gate":"qa_passed","value":true}'
        sig = _compute_internal_signature(body, "test-secret")
        request = AsyncMock()
        request.headers = {"X-PF-Signature": sig}
        request.body = AsyncMock(return_value=body)
        result = await verify_internal_signature(request)
        assert result is None  # success

    @pytest.mark.asyncio
    async def test_raises_401_on_bad_signature(self, monkeypatch):
        monkeypatch.setenv("PF_INTERNAL_API_SECRET", "test-secret")
        import importlib, website.auth
        importlib.reload(website.auth)
        from website.auth import verify_internal_signature
        from fastapi import HTTPException
        request = AsyncMock()
        request.headers = {"X-PF-Signature": "sha256=wrong"}
        request.body = AsyncMock(return_value=b"{}")
        with pytest.raises(HTTPException) as exc:
            await verify_internal_signature(request)
        assert exc.value.status_code == 401
        assert "invalid" in exc.value.detail.lower()
