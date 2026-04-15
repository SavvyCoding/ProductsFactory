"""
GitHub API client — used by the poller for PR management.

Handles:
  - count_open_prs()       PR count gate (pause if ≥3 open)
  - reconcile_merged_prs() sync merged PRs → DB feature status = Pushed
"""

import os
import logging
from pathlib import Path

import httpx

log = logging.getLogger("poller.github")

PM_API_URL = os.environ["PM_API_URL"]

def _get_pat() -> str:
    """Fetch GitHub PAT fresh from system_config each call — no caching so DB changes apply immediately."""
    try:
        resp = httpx.get(f"{PM_API_URL}/api/system-config", timeout=5)
        return resp.json().get("github_pat") or ""
    except Exception:
        return ""


def _parse_repo_slug(product: dict) -> tuple[str, str] | None:
    """Parse 'owner/repo' from github_repo URL."""
    github_repo = product.get("github_repo", "")
    if not github_repo:
        return None
    import re
    match = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    if match:
        owner, repo = match.group(1).split("/", 1)
        return owner, repo
    return None


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    pat = _get_pat()
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    return headers


def count_open_prs(product: dict) -> int:
    """Returns number of open PRs for this product's repo."""
    slug = _parse_repo_slug(product)
    if not slug:
        return 0
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 10},
            headers=_github_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            return len(resp.json())
    except Exception as e:
        log.warning(f"count_open_prs failed: {e}")
    return 0


def reconcile_merged_prs(product: dict):
    """
    Fetch recently merged PRs from GitHub and sync feature status → Pushed in DB.
    Prevents stale DB state where feature is still Implementing but PR is merged.
    """
    slug = _parse_repo_slug(product)
    if not slug:
        return
    owner, repo = slug

    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "closed", "per_page": 20},
            headers=_github_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            return

        merged_prs = [pr for pr in resp.json() if pr.get("merged_at")]

        if not merged_prs:
            return

        merged_numbers = [pr["number"] for pr in merged_prs]
        log.info(f"Reconciling merged PRs: {merged_numbers}")

        # Fetch all features for this product and find any with merged PR numbers
        # (features can be in Reviewing, Reviewed, or Implementing when PR merges)
        with httpx.Client(base_url=PM_API_URL) as client:
            all_features = client.get(
                "/api/products/{product_id}/features".format(product_id=product["id"]),
            )
            if all_features.status_code != 200:
                # Fallback: use the approved endpoint if the all-features endpoint isn't available
                features_data = client.get(
                    "/api/features/approved",
                    params={"product_id": product["id"]},
                ).json()
            else:
                features_data = all_features.json()

            for feature in features_data:
                if feature.get("pr_number") in merged_numbers and feature.get("status") not in ("Pushed", "Rejected", "Reverted"):
                    client.patch(
                        f"/api/features/{feature['id']}",
                        json={"status": "Pushed"},
                    )
                    log.info(f"Reconciled feature {feature['id']} → Pushed (PR #{feature['pr_number']} merged)")

    except Exception as e:
        log.warning(f"reconcile_merged_prs failed: {e}")
