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
