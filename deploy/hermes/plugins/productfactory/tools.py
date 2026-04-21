"""
Tool handlers for the ProductFactory Hermes plugin.

Every handler returns a JSON string (per Hermes convention). Errors are caught
and returned as {"error": "message"} — handlers never raise.

Handlers wrap existing ProductFactory modules:
- docker_runner.run_claude_in_docker  (launch_session)
- heartbeat.check_stale_sessions      (check_stale_sessions, kill_stale_container)
- reconcile logic in docker_runner/poller (reconcile_prs)
- PM REST API                         (pm_api, get_*, set_feature_status, alerts)
- GitHub API                          (github_list_prs, github_merge_pr)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

import httpx

log = logging.getLogger("hermes.productfactory")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")
REQUEST_TIMEOUT = 15


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


def _err(msg: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": msg, **extra})


def _pm_client() -> httpx.Client:
    return httpx.Client(base_url=PM_API_URL, timeout=REQUEST_TIMEOUT)


# ---------------------------------------------------------------------------
# Generic PM API passthrough
# ---------------------------------------------------------------------------

def pm_api(method: str, path: str, body: dict | None = None) -> str:
    try:
        with _pm_client() as client:
            resp = client.request(method.upper(), path, json=body)
        data: Any
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return json.dumps({"ok": resp.is_success, "status": resp.status_code, "data": data})
    except httpx.HTTPError as e:
        return _err(f"pm_api {method} {path} failed: {e}")


def get_products() -> str:
    return pm_api("GET", "/api/products")


def get_active_sprint(product_id: int) -> str:
    return pm_api("GET", f"/api/products/{product_id}/sprints/active")


def get_sprints(product_id: int) -> str:
    return pm_api("GET", f"/api/products/{product_id}/sprints")


def get_features(product_id: int) -> str:
    return pm_api("GET", f"/api/products/{product_id}/features")


def get_system_config() -> str:
    return pm_api("GET", "/api/system-config")


def set_feature_status(feature_id: int, status: str, pr_number: int | None = None) -> str:
    body: dict[str, Any] = {"status": status}
    if pr_number is not None:
        body["pr_number"] = pr_number
    return pm_api("PATCH", f"/api/features/{feature_id}", body)


def reset_stuck_features() -> str:
    return pm_api("POST", "/api/features/reset_stuck")


def poller_heartbeat() -> str:
    return pm_api("POST", "/api/poller/heartbeat")


def alert(severity: str, message: str, product_name: str | None = None) -> str:
    try:
        from orchestrator.alerts import send_alert  # type: ignore
        send_alert(severity, message, product_name)
        return _ok({"sent": True})
    except Exception as e:
        return _err(f"alert failed: {e}")


# ---------------------------------------------------------------------------
# Docker / session management
# ---------------------------------------------------------------------------

def launch_session(product_id: int, persona: str) -> str:
    """
    Spawn the agent Docker container for the given product+persona. Blocks until
    it exits. Returns exit_code and features_pushed count.

    Wraps docker_runner.run_claude_in_docker — all the heavy lifting (feature
    claiming, git reset, creds mount, live-poll, post-coder chain) lives there.
    """
    try:
        from orchestrator.docker_runner import run_claude_in_docker  # type: ignore
        with _pm_client() as client:
            product_resp = client.get(f"/api/products/{product_id}")
        if not product_resp.is_success:
            return _err(f"product {product_id} not found", status=product_resp.status_code)
        product = product_resp.json()
        exit_code = run_claude_in_docker(product, persona=persona)
        # features_pushed/attempted counts are recorded in DB by docker_runner;
        # surface the exit code only — Hermes can look up session details if needed.
        return _ok({"exit_code": exit_code, "product_id": product_id, "persona": persona})
    except Exception as e:
        log.exception("launch_session failed")
        return _err(f"launch_session crashed: {e}")


def kill_stale_container(product_id: int) -> str:
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=pf-{product_id}-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        killed: list[str] = []
        for name in result.stdout.strip().splitlines():
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
            killed.append(name)
        return _ok({"killed": killed})
    except Exception as e:
        return _err(f"kill_stale_container failed: {e}")


def check_stale_sessions() -> str:
    """
    For every ready product, kill the agent container if progress.md hasn't been
    pushed to GitHub in >STALE_THRESHOLD_MINUTES. Only fires when a container is
    actually running (see heartbeat._container_running).
    """
    try:
        from orchestrator.heartbeat import check_stale_sessions as _check  # type: ignore
        with _pm_client() as client:
            products = client.get("/api/products").json()
        _check(products)
        return _ok({"checked": len(products)})
    except Exception as e:
        return _err(f"check_stale_sessions failed: {e}")


# ---------------------------------------------------------------------------
# PR reconciliation (per-product)
# ---------------------------------------------------------------------------

def reconcile_prs(product_id: int | None) -> str:
    """
    Reconcile merged + in-flight PRs for one product. Mirrors the poller's
    reconcile_merged_prs + reconcile_in_flight_prs called together. Pass null
    to run reset_stuck_features only.
    """
    try:
        if product_id is None:
            return reset_stuck_features()
        # Import here to avoid hard dependency at plugin load time.
        from orchestrator.github_client import reconcile_merged_prs, reconcile_in_flight_prs  # type: ignore
        with _pm_client() as client:
            product_resp = client.get(f"/api/products/{product_id}")
        if not product_resp.is_success:
            return _err(f"product {product_id} not found")
        product = product_resp.json()
        reconcile_merged_prs(product)
        reconcile_in_flight_prs(product)
        return _ok({"reconciled": product_id})
    except Exception as e:
        log.exception("reconcile_prs failed")
        return _err(f"reconcile_prs failed: {e}")


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _github_context(product_id: int) -> tuple[str, str] | None:
    """Return (repo_slug, github_pat) or None if unavailable."""
    try:
        with _pm_client() as client:
            product = client.get(f"/api/products/{product_id}").json()
            sys_cfg = client.get("/api/system-config").json()
        repo_url = product.get("github_repo") or ""
        pat = sys_cfg.get("github_pat") or ""
        if not repo_url or not pat:
            return None
        slug = repo_url.rstrip("/").split("github.com/")[-1].replace(".git", "")
        return slug, pat
    except Exception:
        return None


def github_list_prs(product_id: int, state: str = "open") -> str:
    ctx = _github_context(product_id)
    if ctx is None:
        return _err("github context unavailable (missing repo or PAT)")
    slug, pat = ctx
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{slug}/pulls",
            params={"state": state, "per_page": 30},
            headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
            timeout=20,
        )
        data = resp.json() if resp.is_success else []
        return _ok([{"number": p["number"], "title": p["title"], "head": p["head"]["ref"]} for p in data])
    except Exception as e:
        return _err(f"github_list_prs failed: {e}")


def github_merge_pr(product_id: int, pr_number: int) -> str:
    ctx = _github_context(product_id)
    if ctx is None:
        return _err("github context unavailable")
    slug, pat = ctx
    try:
        resp = httpx.put(
            f"https://api.github.com/repos/{slug}/pulls/{pr_number}/merge",
            headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
            json={"merge_method": "squash"},
            timeout=30,
        )
        return json.dumps({"ok": resp.is_success, "status": resp.status_code, "body": resp.text[:200]})
    except Exception as e:
        return _err(f"github_merge_pr failed: {e}")
