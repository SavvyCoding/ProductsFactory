"""
Tool handlers for the ProductFactory Hermes plugin.

Hermes dispatches tools as: handler(args: dict, **kwargs) where args is the
JSON argument dict and kwargs may include task_id, session_id, etc.

Every handler returns a JSON string. Errors are caught and returned as
{"error": "message"} — handlers never raise.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from typing import Any

import httpx

# The orchestrator package lives at /app/orchestrator inside the Hermes container.
# Insert /app so lazy imports (launch_session, check_stale_sessions, etc.) resolve.
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

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

def pm_api(args: dict, **kwargs) -> str:
    method = args.get("method", "GET")
    path = args.get("path", "")
    body = args.get("body")
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


def _pm(method: str, path: str, body: dict | None = None) -> str:
    """Internal helper for simple PM calls."""
    return pm_api({"method": method, "path": path, "body": body})


def get_products(args: dict, **kwargs) -> str:
    return _pm("GET", "/api/products")


def get_active_sprint(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    return _pm("GET", f"/api/products/{product_id}/sprints/active")


def get_sprints(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    return _pm("GET", f"/api/products/{product_id}/sprints")


def get_features(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    return _pm("GET", f"/api/products/{product_id}/features")


def get_system_config(args: dict, **kwargs) -> str:
    return _pm("GET", "/api/system-config")


def set_feature_status(args: dict, **kwargs) -> str:
    feature_id = args.get("feature_id")
    status = args.get("status")
    pr_number = args.get("pr_number")
    body: dict[str, Any] = {"status": status}
    if pr_number is not None:
        body["pr_number"] = pr_number
    return _pm("PATCH", f"/api/features/{feature_id}", body)


def reset_stuck_features(args: dict, **kwargs) -> str:
    return _pm("POST", "/api/features/reset_stuck")


def poller_heartbeat(args: dict, **kwargs) -> str:
    pid = os.getpid()
    host = f"hermes-{socket.gethostname()}"
    return _pm("POST", "/api/poller/heartbeat", {"pid": pid, "host": host})


def alert(args: dict, **kwargs) -> str:
    severity = args.get("severity", "info")
    message = args.get("message", "")
    product_name = args.get("product_name")
    try:
        from orchestrator.alerts import send_alert  # type: ignore
        send_alert(severity, message, product_name)
        return _ok({"sent": True})
    except Exception as e:
        return _err(f"alert failed: {e}")


# ---------------------------------------------------------------------------
# Docker / session management
# ---------------------------------------------------------------------------

def launch_session(args: dict, **kwargs) -> str:
    """
    Spawn the agent Docker container for the given product+persona. Blocks until
    it exits. Returns exit_code and features_pushed count.
    """
    product_id = args.get("product_id")
    persona = args.get("persona")
    try:
        from orchestrator.docker_runner import run_claude_in_docker  # type: ignore
        with _pm_client() as client:
            product_resp = client.get(f"/api/products/{product_id}")
        if not product_resp.is_success:
            return _err(f"product {product_id} not found", status=product_resp.status_code)
        product = product_resp.json()
        exit_code = run_claude_in_docker(product, persona=persona)
        return _ok({"exit_code": exit_code, "product_id": product_id, "persona": persona})
    except Exception as e:
        log.exception("launch_session failed")
        return _err(f"launch_session crashed: {e}")


def kill_stale_container(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
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


def check_stale_sessions(args: dict, **kwargs) -> str:
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

def reconcile_prs(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    try:
        if product_id is None:
            return reset_stuck_features({})
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


def github_list_prs(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    state = args.get("state", "open")
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


def github_merge_pr(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    pr_number = args.get("pr_number")
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
