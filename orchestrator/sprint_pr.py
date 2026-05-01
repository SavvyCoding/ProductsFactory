"""Sprint PR provisioning — owner of the `sprint/<id>` branch + draft PR.

The orchestrator is the single owner of sprint PR creation. Sprint activation
(in the website) calls into this module via a thin shim; the module hits the
GitHub API to:
  1. Create a `sprint/<id>` branch off the default branch (idempotent — 422
     on the create-ref call means it already exists, which is fine on
     re-activation).
  2. Commit a `.productfactory/sprint-<id>.md` manifest so the branch has at
     least one commit ahead of base (GitHub rejects PRs with no diff).
  3. Open a draft PR (or return the existing open PR if one is already there).

Idempotent on re-entry: the manifest commit is updated in place, and an
already-open PR is returned unchanged.
"""
from __future__ import annotations

import base64
import logging
import re

import httpx

log = logging.getLogger("sprint_pr")

_TIMEOUT = 10


def _parse_repo_slug(github_repo: str) -> tuple[str, str] | None:
    """Parse owner/repo from a GitHub URL (https or ssh). None if unparseable."""
    if not github_repo:
        return None
    match = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    if not match:
        return None
    parts = match.group(1).split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def provision_sprint_pr(
    github_repo: str,
    sprint_id: int,
    sprint_name: str,
    sprint_goal: str | None,
    feature_titles: list[str],
    token: str,
) -> dict | None:
    """Create the `sprint/<id>` branch + draft PR. See module docstring.

    Returns {"branch": str, "number": int, "url": str} on success, None otherwise.
    """
    slug = _parse_repo_slug(github_repo)
    if not slug or not token:
        return None
    owner, repo = slug
    branch = f"sprint/{sprint_id}"
    h = _headers(token)
    try:
        repo_resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=h, timeout=_TIMEOUT,
        )
        if repo_resp.status_code != 200:
            log.warning(f"provision_sprint_pr: repo lookup {repo_resp.status_code} for {owner}/{repo}")
            return None
        default_branch = repo_resp.json().get("default_branch", "main")

        ref_resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{default_branch}",
            headers=h, timeout=_TIMEOUT,
        )
        if ref_resp.status_code != 200:
            log.warning(f"provision_sprint_pr: default ref lookup {ref_resp.status_code}")
            return None
        sha = ref_resp.json()["object"]["sha"]

        create_resp = httpx.post(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs",
            headers=h, timeout=_TIMEOUT,
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        if create_resp.status_code not in (201, 422):
            log.warning(f"provision_sprint_pr: create branch {create_resp.status_code}: {create_resp.text[:200]}")
            return None

        bullets = "\n".join(f"- {t}" for t in feature_titles) or "- (no features yet)"
        manifest = (
            f"# Sprint {sprint_id}: {sprint_name}\n\n"
            f"## Goal\n{sprint_goal or '(no goal set)'}\n\n"
            f"## Planned features\n{bullets}\n"
        )
        path = f".productfactory/sprint-{sprint_id}.md"
        existing = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
            params={"ref": branch}, headers=h, timeout=_TIMEOUT,
        )
        put_body: dict = {
            "message": f"chore(sprint-{sprint_id}): scaffold sprint branch",
            "content": base64.b64encode(manifest.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing.status_code == 200:
            put_body["sha"] = existing.json().get("sha")
        put_resp = httpx.put(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
            headers=h, timeout=_TIMEOUT, json=put_body,
        )
        if put_resp.status_code not in (200, 201):
            log.warning(f"provision_sprint_pr: contents PUT {put_resp.status_code}: {put_resp.text[:200]}")
            return None

        existing_pr = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}", "base": default_branch},
            headers=h, timeout=_TIMEOUT,
        )
        if existing_pr.status_code == 200 and existing_pr.json():
            pr = existing_pr.json()[0]
            return {"branch": branch, "number": pr["number"], "url": pr["html_url"]}

        body = (
            f"Sprint #{sprint_id}: {sprint_name}\n\n"
            f"{sprint_goal or ''}\n\n## Planned features\n{bullets}"
        )
        pr_resp = httpx.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=h, timeout=_TIMEOUT,
            json={
                "title": f"Sprint {sprint_id}: {sprint_name}",
                "head": branch,
                "base": default_branch,
                "body": body,
                "draft": True,
            },
        )
        if pr_resp.status_code in (200, 201):
            pr = pr_resp.json()
            return {"branch": branch, "number": pr["number"], "url": pr["html_url"]}
        log.warning(f"provision_sprint_pr: PR create {pr_resp.status_code}: {pr_resp.text[:200]}")
    except Exception as e:
        log.warning(f"provision_sprint_pr error: {e}")
    return None
