"""
GitHub helpers used by the post-session pipelines.

This module is intentionally narrow — it only houses the two helpers the
pipelines need to call out to GitHub. Phase 5 will absorb the full
``orchestrator.github_client`` module here under one consistent slug-parsing
contract; today there are three different ``_parse_repo_slug`` definitions
across the orchestrator (str / str|None / tuple[str,str]|None) which we'll
unify when we tackle the per-feature reconciler.

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os
import re

import httpx

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


def _parse_repo_slug(github_repo: str) -> str:
    """Extract 'owner/repo' from a GitHub URL for API calls."""
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    return m.group(1) if m else github_repo


def _get_gh_token() -> str | None:
    """Fetch GitHub PAT from system config for GH_TOKEN injection into agent containers."""
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
