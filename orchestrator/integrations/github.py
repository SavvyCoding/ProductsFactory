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
    """Return a fresh GitHub App installation token, or None.

    Per CLAUDE.md "Git auth: GitHub App only": the PAT fallback was
    removed in the drift-cleanup pass. The App module (github_app.py)
    owns its own short-lived cache; on mint failure the None surfaces
    here so callers can fail visibly rather than silently degrade.
    """
    return github_app.get_installation_token() or None
