"""
Heartbeat monitor — detects stale Claude sessions.

Poller calls check_stale_sessions() each cycle.
A session is stale if progress.md has not been pushed to GitHub in >45 minutes.
Stale session → kill container → relaunch on next cycle.
"""

import os
import logging
from datetime import datetime, timezone, timedelta

import httpx

from orchestrator.github_client import _parse_repo_slug, _github_headers
from orchestrator.alerts import send_alert

log = logging.getLogger("poller.heartbeat")

PM_API_URL           = os.environ["PM_API_URL"]
STALE_THRESHOLD_MIN  = int(os.environ.get("STALE_THRESHOLD_MINUTES", "45"))


def check_stale_sessions(products: list[dict]):
    """
    For each product with status=ready, check when progress.md was last pushed.
    If >STALE_THRESHOLD_MIN minutes ago, kill the container and alert PM.
    """
    for product in products:
        if product["status"] != "ready":
            continue
        last_push = _get_progress_last_push(product)
        if last_push is None:
            continue
        age_minutes = (datetime.now(timezone.utc) - last_push).seconds // 60
        if age_minutes > STALE_THRESHOLD_MIN:
            log.warning(f"Stale session detected: {product['name']} — progress.md not pushed in {age_minutes}m")
            send_alert("error", f"{product['name']}: session stale ({age_minutes}m since last heartbeat) — killing and relaunching")
            _kill_container(product)


def _get_progress_last_push(product: dict) -> datetime | None:
    """Fetch the last commit date of progress.md from GitHub."""
    slug = _parse_repo_slug(product)
    if not slug:
        return None
    owner, repo = slug
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            params={"path": "progress.md", "per_page": 1},
            headers=_github_headers(),
            timeout=15,
        )
        if resp.status_code == 200 and resp.json():
            date_str = resp.json()[0]["commit"]["author"]["date"]
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception as e:
        log.warning(f"_get_progress_last_push failed: {e}")
    return None


def _kill_container(product: dict):
    """Kill the Docker container for this product (by name pattern)."""
    import subprocess
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=pf-{product['id']}-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        for container_name in result.stdout.strip().splitlines():
            subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10)
            log.info(f"Killed container: {container_name}")
    except Exception as e:
        log.warning(f"_kill_container failed: {e}")
