"""
Heartbeat monitor — detects stale Claude sessions.

Poller calls check_stale_sessions() each cycle.
A session is stale if progress.md has not been pushed to GitHub in >45 minutes.
Stale session → kill container → relaunch on next cycle.
"""

import os
import logging
from datetime import datetime, timezone

import httpx

from orchestrator.github_client import _github_headers
from orchestrator.integrations.github import _parse_repo_slug
from orchestrator.alerts import send_alert

log = logging.getLogger("poller.heartbeat")

PM_API_URL           = os.environ["PM_API_URL"]
STALE_THRESHOLD_MIN  = int(os.environ.get("STALE_THRESHOLD_MINUTES", "45"))


def check_stale_sessions(products: list[dict]):
    """
    For each product with status=ready, check when progress.md was last pushed.
    If >STALE_THRESHOLD_MIN minutes ago AND a container is actually running, kill it and alert PM.

    Grace period: skip the kill if the container is younger than STALE_THRESHOLD_MIN.
    A fresh container cannot possibly have pushed progress.md yet — killing it
    immediately creates an infinite launch-kill loop.
    """
    for product in products:
        if product["status"] != "ready":
            continue
        if not _container_running(product):
            continue
        container_age = _container_age_minutes(product)
        # Grace period: skip kill if container is fresh OR if we couldn't
        # determine age (default to safe — don't kill unknowns).
        if container_age is None or container_age < STALE_THRESHOLD_MIN:
            continue
        last_push = _get_progress_last_push(product)
        if last_push is None:
            continue
        age_minutes = int((datetime.now(timezone.utc) - last_push).total_seconds() / 60)
        if age_minutes > STALE_THRESHOLD_MIN:
            log.warning(f"Stale session detected: {product['name']} — progress.md not pushed in {age_minutes}m")
            send_alert("error", f"{product['name']}: session stale ({age_minutes}m since last heartbeat) — killing and relaunching")
            _kill_container(product)


def _container_age_minutes(product: dict) -> int | None:
    """Return the running container's age in minutes, or None if no container."""
    import subprocess as _sp
    try:
        result = _sp.run(
            ["docker", "ps", "--filter", f"name=pf-{product['id']}-",
             "--format", "{{.RunningFor}}"],
            capture_output=True, text=True, timeout=5,
        )
        running_for = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        # Parse Docker's human-readable "Less than a second", "About a minute",
        # "N minutes", "About an hour", "N hours", "About a day", "N days".
        s = running_for.lower()
        if "second" in s or s == "about a minute":
            return 0
        if "hour" in s or "day" in s:
            return 999  # definitely old enough
        # "N minutes" — extract N
        import re as _re
        m = _re.search(r"(\d+)\s+minutes?", s)
        if m:
            return int(m.group(1))
        return None
    except Exception:
        return None


def _get_progress_last_push(product: dict) -> datetime | None:
    """Fetch the last commit date of progress.md from GitHub."""
    slug = _parse_repo_slug(product.get("github_repo", ""))
    if not slug:
        return None
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{slug}/commits",
            params={"path": "progress.md", "per_page": 1},
            headers=_github_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            commits = resp.json()
            if isinstance(commits, list) and commits:
                date_str = commits[0]["commit"]["author"]["date"]
                return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception as e:
        log.warning(f"_get_progress_last_push failed: {e}")
    return None


def _container_running(product: dict) -> bool:
    """Return True if a Docker container is currently running for this product."""
    import subprocess
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=pf-{product['id']}-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


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
