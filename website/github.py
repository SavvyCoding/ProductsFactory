"""
GitHub helpers used by the PM website.
Fetches progress.md content for the progress viewer.
"""

import os
import re
import logging
import httpx

log = logging.getLogger("website.github")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_TIMEOUT = 10


def _headers(token: str | None = None) -> dict:
    h = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
    effective_token = token or GITHUB_TOKEN
    if effective_token:
        h["Authorization"] = f"Bearer {effective_token}"
    return h


def parse_repo_slug(github_repo: str) -> tuple[str, str] | None:
    """Parse owner/repo from a GitHub URL (https or ssh)."""
    if not github_repo:
        return None
    match = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    if match:
        parts = match.group(1).split("/", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
    return None


def fetch_progress_md(github_repo: str) -> str | None:
    """
    Fetch the raw content of progress.md from the default branch.
    Returns None if the repo has no github_repo set or the file doesn't exist yet.
    """
    slug = parse_repo_slug(github_repo)
    if not slug:
        return None
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/progress.md",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            # Raw content returned when Accept: application/vnd.github.raw+json
            return resp.text
        if resp.status_code == 404:
            return None  # file doesn't exist yet — session hasn't started
        log.warning(f"GitHub progress.md fetch: {resp.status_code} for {owner}/{repo}")
    except httpx.TimeoutException:
        log.warning(f"GitHub fetch timed out for {owner}/{repo}")
    except Exception as e:
        log.warning(f"GitHub fetch error: {e}")
    return None


def fetch_architecture_md(github_repo: str) -> str | None:
    """Fetch ARCHITECTURE.md from the default branch, same pattern as progress.md."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return None
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/ARCHITECTURE.md",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        return resp.text if resp.status_code == 200 else None
    except Exception as e:
        log.warning(f"fetch_architecture_md error: {e}")
    return None


def list_open_prs(github_repo: str, token: str | None = None) -> list[dict]:
    """Return list of open PRs with number, title, url, branch."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return []
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 20},
            headers={**_headers(token), "Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return [
                {"number": pr["number"], "title": pr["title"],
                 "url": pr["html_url"], "branch": pr["head"]["ref"]}
                for pr in resp.json()
            ]
        log.warning(f"list_open_prs: GitHub returned {resp.status_code} for {owner}/{repo}")
    except Exception as e:
        log.warning(f"list_open_prs error: {e}")
    return []


def merge_pr(github_repo: str, pr_number: int, token: str) -> tuple[bool, str]:
    """Merge a PR via GitHub API. Returns (success, error_message)."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return False, "Could not parse repo slug from github_repo"
    owner, repo = slug
    try:
        h = {**_headers(token), "Accept": "application/vnd.github+json"}
        resp = httpx.put(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            json={"merge_method": "squash"},
            headers=h,
            timeout=_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return True, ""
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg = body.get("message", resp.text[:200])
        log.warning(f"merge_pr: GitHub returned {resp.status_code} for {owner}/{repo} PR#{pr_number}: {msg}")
        return False, f"GitHub {resp.status_code}: {msg}"
    except Exception as e:
        log.warning(f"merge_pr error: {e}")
        return False, str(e)


def count_open_prs(github_repo: str) -> int:
    """Returns the number of open PRs. Returns 0 on any error."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return 0
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 10},
            headers={**_headers(), "Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return len(resp.json())
    except Exception as e:
        log.warning(f"count_open_prs error: {e}")
    return 0
