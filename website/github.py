"""
GitHub helpers used by the PM website.
Fetches repo file content (session_summary.md, ARCHITECTURE.md) for the viewers.
"""

import re
import logging
import httpx

log = logging.getLogger("website.github")

_TIMEOUT = 10


def _headers(token: str | None = None) -> dict:
    h = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
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


def fetch_repo_file(github_repo: str, path: str, token: str | None = None) -> str | None:
    """
    Fetch the raw content of a file at `path` from the repo's default branch.
    Returns None if the repo has no github_repo set or the file doesn't exist.
    """
    slug = parse_repo_slug(github_repo)
    if not slug:
        return None
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 404:
            return None  # file doesn't exist yet
        log.warning(f"GitHub {path} fetch: {resp.status_code} for {owner}/{repo}")
    except httpx.TimeoutException:
        log.warning(f"GitHub fetch timed out for {owner}/{repo} ({path})")
    except Exception as e:
        log.warning(f"GitHub fetch error ({path}): {e}")
    return None


def fetch_session_summary_md(github_repo: str, token: str | None = None) -> str | None:
    """Fetch session_summary.md — the live session-continuity doc that
    replaced progress.md. Written incrementally by the coder/designer."""
    return fetch_repo_file(github_repo, "session_summary.md", token)


def fetch_architecture_md(github_repo: str, token: str | None = None) -> str | None:
    """Fetch ARCHITECTURE.md from the default branch."""
    return fetch_repo_file(github_repo, "ARCHITECTURE.md", token)


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


def close_pr(github_repo: str, pr_number: int, token: str, reason: str = "") -> bool:
    """Close a PR without merging. Returns True on success."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return False
    owner, repo = slug
    h = {**_headers(token), "Accept": "application/vnd.github+json"}
    try:
        # Post a comment explaining why
        if reason:
            httpx.post(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": reason}, headers=h, timeout=_TIMEOUT,
            )
        resp = httpx.patch(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            json={"state": "closed"}, headers=h, timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            log.info(f"close_pr: closed PR #{pr_number} in {owner}/{repo}")
            return True
        log.warning(f"close_pr: GitHub {resp.status_code} for {owner}/{repo} PR#{pr_number}")
    except Exception as e:
        log.warning(f"close_pr error: {e}")
    return False


