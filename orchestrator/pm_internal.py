"""Shared internal-API auth header for orchestrator → PM-website calls.

The PM website's ``GET /api/system-config`` masks secret fields (API keys, the
GitHub App PEM, webhook secret) unless the caller proves it is the orchestrator
by presenting the shared ``PF_INTERNAL_API_SECRET`` in the ``X-PF-Internal-Token``
header. Every orchestrator code path that needs the *real* secret values must
attach these headers to its system-config fetch.

Backward-compatible: when ``PF_INTERNAL_API_SECRET`` is unset (single-host dev /
existing deployments) the website reveals secrets regardless, and this header is
an inert empty value — exactly mirroring the HMAC-signing convention in
``deploy/orchestrator/tools.py`` (unset on either side = transparent no-op).

This is a leaf module (only stdlib) so it is safe to import from anywhere in the
orchestrator without circular-import risk.
"""
from __future__ import annotations

import os


def internal_headers() -> dict[str, str]:
    """Headers that unlock real secret values from ``GET /api/system-config``."""
    return {"X-PF-Internal-Token": os.environ.get("PF_INTERNAL_API_SECRET", "")}


def sign_body(body: bytes) -> str:
    """HMAC-sign a request body for internal write endpoints guarded by the
    website's ``verify_internal_signature``. Returns ``"sha256=<hex>"`` or ``""``
    when the secret is unset (signing disabled). Format MUST match the website's
    ``_compute_internal_signature`` exactly: HMAC-SHA256 over the raw body bytes.
    """
    import hashlib
    import hmac as _hmac
    secret = os.environ.get("PF_INTERNAL_API_SECRET", "")
    if not secret:
        return ""
    return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
