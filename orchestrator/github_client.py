"""
GitHub API client — used by the poller for PR management.

Handles:
  - count_open_prs()            PR count gate (pause if ≥3 open)
  - reconcile_merged_prs()      sync merged/closed PRs → DB feature status
  - reconcile_in_flight_prs()   check every in-flight feature's PR against GitHub
"""

import os
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("poller.github")


def _gh_get(url: str, headers: dict, params: dict | None = None, timeout: int = 15) -> httpx.Response | None:
    """
    GET from the GitHub API with automatic retry on 429/403 rate-limit responses.
    Returns the response on success (2xx), None on permanent failure.
    Retries up to 3 times with exponential back-off honouring Retry-After when present.
    """
    for attempt in range(3):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as e:
            log.warning(f"GitHub GET {url} attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

        if resp.status_code in (429, 403):
            retry_after = int(resp.headers.get("Retry-After", 0))
            wait = retry_after if retry_after > 0 else (2 ** attempt * 5)
            log.warning(f"GitHub rate-limited ({resp.status_code}) — waiting {wait}s before retry {attempt + 1}/3")
            time.sleep(wait)
            continue

        return resp

    log.warning(f"GitHub GET {url} failed after 3 attempts (rate-limited)")
    return None

PM_API_URL = os.environ["PM_API_URL"]

def _get_pat() -> str:
    """Fetch GitHub PAT fresh from system_config each call — no caching so DB changes apply immediately."""
    try:
        resp = httpx.get(f"{PM_API_URL}/api/system-config", timeout=5)
        resp.raise_for_status()
        if "application/json" not in resp.headers.get("content-type", ""):
            return ""
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
        resp = _gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 10},
            headers=_github_headers(),
        )
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            return len(data) if isinstance(data, list) else 0
    except Exception as e:
        log.warning(f"count_open_prs failed: {e}")
    return 0


def reconcile_merged_prs(product: dict):
    """
    Fetch recently closed PRs from GitHub and sync feature status in DB.
    - Merged PRs   → feature status Pushed
    - Closed (not merged) PRs → feature reset to Approved/Implementing so coder retries

    Also handles features where pr_number is null but pr_url contains the PR link.
    """
    slug = _parse_repo_slug(product)
    if not slug:
        return
    owner, repo = slug

    try:
        resp = _gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "closed", "per_page": 20},
            headers=_github_headers(),
        )
        if resp is None or resp.status_code != 200:
            return

        prs_data = resp.json()
        if not isinstance(prs_data, list):
            log.warning("reconcile_merged_prs: unexpected response shape from GitHub")
            return

        merged_numbers  = {pr["number"] for pr in prs_data if pr.get("merged_at")}
        closed_numbers  = {pr["number"] for pr in prs_data if not pr.get("merged_at")}

        if not merged_numbers and not closed_numbers:
            return

        if merged_numbers:
            log.info(f"Reconciling merged PRs: {sorted(merged_numbers)}")
        if closed_numbers:
            log.info(f"Reconciling closed (unmerged) PRs: {sorted(closed_numbers)}")

        import re as _re

        def _pr_number_for(feature: dict) -> int | None:
            """Return the PR number from pr_number column or parsed from pr_url."""
            n = feature.get("pr_number")
            if n:
                return int(n)
            url = feature.get("pr_url") or ""
            m = _re.search(r"/pull/(\d+)", url)
            return int(m.group(1)) if m else None

        with httpx.Client(base_url=PM_API_URL) as client:
            all_features = client.get(
                f"/api/products/{product['id']}/features",
            )
            if all_features.status_code != 200:
                fallback = client.get(
                    "/api/features/approved",
                    params={"product_id": product["id"]},
                )
                fallback.raise_for_status()
                features_data = fallback.json()
            else:
                features_data = all_features.json()

            if not isinstance(features_data, list):
                log.warning("reconcile_merged_prs: unexpected features response shape")
                return

            terminal = {"Pushed", "Rejected", "Reverted", "Deferred"}

            for feature in features_data:
                if feature.get("status") in terminal:
                    continue
                # Never reset a feature that was already Pushed (race condition guard)
                # Re-fetch current status to avoid stale data
                try:
                    fresh = client.get(f"/api/features/{feature['id']}")
                    if fresh.status_code == 200 and fresh.json().get("status") in terminal:
                        continue
                except Exception:
                    pass
                pr_n = _pr_number_for(feature)
                if pr_n is None:
                    continue

                if pr_n in merged_numbers:
                    client.patch(
                        f"/api/features/{feature['id']}",
                        json={"status": "Pushed", "pr_number": None},
                    )
                    log.info(f"Reconciled feature {feature['id']} → Pushed (PR #{pr_n} merged)")

                elif pr_n in closed_numbers:
                    # PR closed without merging — reset so coder can retry
                    reset_status = "Approved"
                    if feature.get("status") == "Reviewing":
                        reset_status = "Implementing"  # keep coder loop, not restart from scratch
                    client.patch(
                        f"/api/features/{feature['id']}",
                        json={"status": reset_status, "pr_number": None, "pr_url": None, "branch_name": None},
                    )
                    log.info(f"Reconciled feature {feature['id']} → {reset_status} (PR #{pr_n} closed without merge)")

    except Exception as e:
        log.warning(f"reconcile_merged_prs failed: {e}")


def reconcile_in_flight_prs(product: dict):
    """
    For every in-flight feature (Implementing/Reviewing/Reviewed) that has a
    pr_url or pr_number, check the actual PR state on GitHub and fix any
    DB/GitHub mismatch immediately — without waiting for reset_stuck timeout.

    Called every poller cycle (coder path) so stuck features self-heal within
    one poll interval (~60s) instead of waiting up to 2 hours.
    """
    slug = _parse_repo_slug(product)
    if not slug:
        return
    owner, repo = slug

    import re as _re

    IN_FLIGHT = {"Implementing", "Reviewing", "Reviewed"}
    TERMINAL  = {"Pushed", "Rejected", "Reverted", "Pending", "Approved",
                 "Designed", "Designing", "Deferred", "Blocked"}

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            resp = client.get(f"/api/products/{product['id']}/features")
            if resp.status_code != 200:
                return
            features_data = resp.json()
            if not isinstance(features_data, list):
                return

            # Collect features that have a PR reference
            candidates = []
            for f in features_data:
                if f.get("status") not in IN_FLIGHT:
                    continue
                pr_n = f.get("pr_number")
                if not pr_n:
                    url = f.get("pr_url") or ""
                    m = _re.search(r"/pull/(\d+)", url)
                    if m:
                        pr_n = int(m.group(1))
                if pr_n:
                    candidates.append((f, int(pr_n)))

            if not candidates:
                return

            gh_headers = _github_headers()
            for feature, pr_n in candidates:
                fid = feature["id"]
                try:
                    pr_resp = _gh_get(
                        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_n}",
                        headers=gh_headers,
                    )
                    if pr_resp is None or pr_resp.status_code != 200:
                        continue
                    pr = pr_resp.json()
                    if not isinstance(pr, dict):
                        continue
                except Exception as e:
                    log.debug(f"[in-flight] Could not fetch PR #{pr_n}: {e}")
                    continue

                state     = pr.get("state")      # "open" | "closed"
                merged_at = pr.get("merged_at")  # None if not merged

                if merged_at and feature.get("status") not in ("Pushed",):
                    client.patch(f"/api/features/{fid}",
                                 json={"status": "Pushed", "pr_number": None})
                    log.info(f"[in-flight] Feature #{fid} → Pushed (PR #{pr_n} already merged)")

                elif state == "closed" and not merged_at:
                    reset = "Approved"
                    if feature.get("status") == "Reviewing":
                        reset = "Implementing"
                    client.patch(f"/api/features/{fid}",
                                 json={"status": reset, "pr_number": None,
                                       "pr_url": None, "branch_name": None})
                    log.info(f"[in-flight] Feature #{fid} → {reset} (PR #{pr_n} closed without merge)")

                # PR is open — nothing to do, agent is still working

    except Exception as e:
        log.warning(f"reconcile_in_flight_prs failed: {e}")
