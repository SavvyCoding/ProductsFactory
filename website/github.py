"""
GitHub helpers used by the PM website.
Fetches progress.md content for the progress viewer.
"""

import os
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


def fetch_progress_md(github_repo: str, token: str | None = None) -> str | None:
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
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 404:
            return None  # file doesn't exist yet — session hasn't started
        log.warning(f"GitHub progress.md fetch: {resp.status_code} for {owner}/{repo}")
    except httpx.TimeoutException:
        log.warning(f"GitHub fetch timed out for {owner}/{repo}")
    except Exception as e:
        log.warning(f"GitHub fetch error: {e}")
    return None


def fetch_architecture_md(github_repo: str, token: str | None = None) -> str | None:
    """Fetch ARCHITECTURE.md from the default branch, same pattern as progress.md."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return None
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/ARCHITECTURE.md",
            headers=_headers(token),
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


def provision_sprint_pr(
    github_repo: str,
    sprint_id: int,
    sprint_name: str,
    sprint_goal: str | None,
    feature_titles: list[str],
    token: str,
) -> dict | None:
    """
    Create a `sprint/<id>` branch off the default branch and open a draft PR for it.

    Idempotent on re-entry: if the branch already exists, the manifest commit is
    updated in place; if a PR is already open against that branch, returns it.
    Returns {"branch": str, "number": int, "url": str} on success, None otherwise.

    A `.productfactory/sprint-<id>.md` manifest is committed so the branch has at
    least one commit ahead of base — GitHub rejects PRs with no diff.
    """
    import base64
    slug = parse_repo_slug(github_repo)
    if not slug or not token:
        return None
    owner, repo = slug
    branch = f"sprint/{sprint_id}"
    h = {**_headers(token), "Accept": "application/vnd.github+json"}
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

        # Create branch (422 = already exists, that's fine for re-activation)
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


def count_open_prs(github_repo: str, token: str | None = None) -> int:
    """Returns the number of open PRs. Returns 0 on any error."""
    slug = parse_repo_slug(github_repo)
    if not slug:
        return 0
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 10},
            headers={**_headers(token), "Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return len(resp.json())
    except Exception as e:
        log.warning(f"count_open_prs error: {e}")
    return 0
