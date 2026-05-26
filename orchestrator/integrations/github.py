"""
GitHub helpers used by the post-session pipelines.

This module is the canonical home for ``_parse_repo_slug`` and ``_get_gh_token``.
``orchestrator.github_client`` and ``orchestrator.auto_merge`` previously had
their own copies with three different return shapes (``str`` / ``str|None`` /
``tuple[str,str]|None``); the drift-cleanup pass collapsed them here.

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os
import re

import httpx

from orchestrator.integrations import github_app

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


def _parse_repo_slug(github_repo: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, or None if unparseable.

    Handles both HTTPS (`https://github.com/owner/repo.git`) and SSH
    (`git@github.com:owner/repo.git`) forms via the `[:/]` prefix in the
    regex. Returns None on an empty input or anything the regex can't
    match — callers must guard, never blindly format the result into a
    URL.
    """
    if not github_repo:
        return None
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    return m.group(1) if m else None


def _get_gh_token() -> str | None:
    """Return a token usable as GH_TOKEN for git pushes and GitHub API calls.

    Preference order:
      1. Fresh GitHub App installation token (the new path).
      2. system_config.github_pat (legacy fallback during transition).

    Returns None only if both paths are unconfigured. Per-call rather than
    cached at this layer — the App module owns its own caching with proper
    expiry handling; the PAT branch is rarely hit and cheap to re-read.
    """
    app_token = github_app.get_installation_token()
    if app_token:
        return app_token

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            resp.raise_for_status()
            if "application/json" not in resp.headers.get("content-type", ""):
                return None
            data = resp.json()
            return data.get("github_pat") or None if isinstance(data, dict) else None
    except Exception:
        return None
