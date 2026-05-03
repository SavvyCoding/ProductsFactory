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

log = logging.getLogger("orchestrator.tools")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")
PM_USERNAME = os.environ.get("PM_USERNAME", "admin")
PM_PASSWORD = os.environ.get("PM_PASSWORD", "")
REQUEST_TIMEOUT = 15


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


def _err(msg: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": msg, **extra})


def _pm_client() -> httpx.Client:
    auth = (PM_USERNAME, PM_PASSWORD) if PM_PASSWORD else None
    return httpx.Client(base_url=PM_API_URL, timeout=REQUEST_TIMEOUT, auth=auth)


# ---------------------------------------------------------------------------
# Generic PM API passthrough
# ---------------------------------------------------------------------------

import re as _re
_FEATURE_ID_RE  = _re.compile(r"^/api/features/\d+$")
_SPRINT_ID_RE   = _re.compile(r"^/api/sprints/\d+$")


def pm_api(args: dict, **kwargs) -> str:
    method = args.get("method", "GET")
    path = args.get("path", "")
    body = args.get("body")

    # Guard: if the model tries to GET individual features/sprints, return a
    # helpful refusal to avoid blowing up the context window.
    if method.upper() == "GET":
        if _FEATURE_ID_RE.match(path):
            return json.dumps({"ok": False, "error":
                "Use get_features(product_id) instead of fetching individual features."})
        if _SPRINT_ID_RE.match(path):
            return json.dumps({"ok": False, "error":
                "Use get_active_sprint(product_id) or get_sprints(product_id) instead."})

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


_PRODUCT_KEEP = {"id", "name", "status", "working_dir", "github_repo", "tech_stack",
                 "run_now", "run_trainer_now", "quiet_hours_start", "quiet_hours_end",
                 "daily_session_cap", "last_run_at", "config", "type"}
_FEATURE_KEEP = {"id", "product_id", "sprint_id", "name", "status", "feature_type",
                 "design_doc_path", "pr_number", "pr_url", "fix_attempts"}
_SPRINT_KEEP  = {"id", "product_id", "phase_id", "name", "status", "goal",
                 "completed_at", "retro_doc_path", "dod_status"}


def _slim(obj: Any, keep: set) -> Any:
    if isinstance(obj, list):
        return [_slim(x, keep) for x in obj]
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k in keep}
    return obj


def _slim_response(raw: str, keep: set) -> str:
    try:
        parsed = json.loads(raw)
        data = parsed.get("data") if isinstance(parsed, dict) else parsed
        slimmed = _slim(data, keep)
        if isinstance(parsed, dict):
            parsed["data"] = slimmed
            return json.dumps(parsed)
        return json.dumps(slimmed)
    except Exception:
        return raw


def get_products(args: dict, **kwargs) -> str:
    raw = _slim_response(_pm("GET", "/api/products"), _PRODUCT_KEEP)
    # Augment each ready product with its active_sprint_id so the orchestrator
    # can call check-dod without a separate round-trip.
    try:
        parsed = json.loads(raw)
        products = parsed.get("data") if isinstance(parsed, dict) else parsed
        if isinstance(products, list):
            with _pm_client() as client:
                for p in products:
                    if p.get("status") in ("ready", "running"):
                        r = client.get(f"/api/products/{p['id']}/sprints/active")
                        sprint = r.json() if r.is_success else None
                        p["active_sprint_id"] = sprint["id"] if sprint else None
            if isinstance(parsed, dict):
                parsed["data"] = products
                return json.dumps(parsed)
            return json.dumps(products)
    except Exception:
        pass
    return raw


def get_active_sprint(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    return _slim_response(_pm("GET", f"/api/products/{product_id}/sprints/active"), _SPRINT_KEEP)


def get_sprints(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    return _slim_response(_pm("GET", f"/api/products/{product_id}/sprints"), _SPRINT_KEEP)


def get_features(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    raw = _slim_response(_pm("GET", f"/api/products/{product_id}/features"), _FEATURE_KEEP)
    # Only return non-terminal features — terminal ones (Pushed/Deferred/Rejected/Reverted)
    # are irrelevant to orchestration decisions and bloat the context window.
    try:
        parsed = json.loads(raw)
        data = parsed.get("data") if isinstance(parsed, dict) else parsed
        if isinstance(data, list):
            _TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
            data = [f for f in data if f.get("status") not in _TERMINAL]
            if isinstance(parsed, dict):
                parsed["data"] = data
                return json.dumps(parsed)
            return json.dumps(data)
    except Exception:
        pass
    return raw


_SYSCFG_KEEP = {"auto_merge_enabled", "max_open_prs", "stuck_feature_timeout_hours",
                "poll_interval", "session_timeout_minutes", "stale_threshold_minutes"}


def get_system_config(args: dict, **kwargs) -> str:
    return _slim_response(_pm("GET", "/api/system-config"), _SYSCFG_KEEP)


def _discover_registered_products(products: list, **kwargs) -> int:
    """
    For every product in `registered` status, run discovery (reads working
    dir, installs CLAUDE.md / AGENT_WORKFLOW.md / ARCHITECTURE.md templates,
    detects tech stack, sets status). For greenfield products discover_and_
    populate sets status='discovered' which requires a manual PM gate; we
    auto-promote that to 'ready' here so wizard-submitted products run end
    to end without an extra click — the wizard submission IS the PM intent.

    Ported from the legacy host poller (poller.py:1225-1228).
    Returns the count of products discovered.
    """
    pending = [p for p in products if p.get("status") == "registered"]
    if not pending:
        return 0
    try:
        from orchestrator.setup_product import discover_and_populate
    except Exception:
        log.exception("[discover] could not import setup_product")
        return 0

    n = 0
    for product in pending:
        try:
            log.info("[discover] starting product=%s name=%s",
                     product.get("id"), product.get("name"))
            discover_and_populate(product)
            n += 1
            # discover_and_populate has already PATCH'd the product. Re-fetch
            # to see the resulting status — if greenfield ('discovered'),
            # bump to 'ready' so the orchestrator picks it up next cycle.
            with _pm_client() as client:
                resp = client.get(f"/api/products/{product['id']}")
                if resp.is_success and resp.json().get("status") == "discovered":
                    client.patch(f"/api/products/{product['id']}",
                                 json={"status": "ready"})
                    log.info("[discover] auto-promoted greenfield product=%s discovered → ready",
                             product.get("id"))
        except Exception:
            log.exception("[discover] failed for product=%s", product.get("id"))
    return n


# Module-level cache of the last-seen PAT so we only rotate when it changes.
# Reset on orchestrator restart, which is fine — the first cycle after a
# restart will simply re-detect a "change" if the DB PAT differs from what
# the products' .git/configs already have, which is also harmless (idempotent).
_LAST_KNOWN_PAT: str | None = None


def _maybe_rotate_pat(products: list, new_pat: str) -> None:
    """Detect PAT changes between cycles and rewrite every product's
    embedded git remote URL. Cheap when PAT is unchanged (just a string
    compare); only walks workspaces when the value differs."""
    global _LAST_KNOWN_PAT
    if not new_pat:
        return
    if _LAST_KNOWN_PAT is None:
        _LAST_KNOWN_PAT = new_pat
        return
    if _LAST_KNOWN_PAT == new_pat:
        return
    log.info("[pat-rotate] system_config.github_pat changed — rewriting remote URLs")
    try:
        from orchestrator.pat_rotate import rotate_pat
        updated, skipped = rotate_pat(products, new_pat)
        log.info("[pat-rotate] updated=%d skipped=%d", updated, skipped)
    except Exception:
        log.exception("[pat-rotate] failed")
    _LAST_KNOWN_PAT = new_pat


def _scaffold_greenfield_pending(products: list, **kwargs) -> int:
    """
    For every product in `greenfield_pending`, invoke the host-side
    scaffold helper that creates the GitHub repo, generates the deploy
    key, writes initial files, and flips the product to `registered`.

    Ported from the legacy host poller (poller.py:1216) — was lost when
    the orchestrator moved into a container. Returns the number scaffolded.
    """
    pending = [p for p in products if p.get("status") == "greenfield_pending"]
    if not pending:
        return 0
    try:
        from pathlib import Path as _Path
        from orchestrator.greenfield_scaffold import scaffold_greenfield
    except Exception:
        log.exception("[scaffold] could not import greenfield_scaffold")
        return 0

    # Resolve SSH_DIR inside the orchestrator container.
    ssh_dir = _Path(os.environ.get("SSH_DIR", "/home/orchestrator/.ssh"))
    if not ssh_dir.exists():
        log.error("[scaffold] SSH_DIR=%s does not exist — cannot generate deploy keys", ssh_dir)
        return 0

    # system_config supplies github_org / github_pat / github_ssh_key_name.
    try:
        with _pm_client() as client:
            sys_cfg = client.get("/api/system-config").json()
    except Exception:
        log.exception("[scaffold] could not fetch system-config")
        return 0

    scaffolded = 0
    for product in pending:
        try:
            log.info("[scaffold] starting product=%s name=%s",
                     product.get("id"), product.get("name"))
            scaffold_greenfield(product, sys_cfg, PM_API_URL, ssh_dir)
            scaffolded += 1
        except Exception:
            log.exception("[scaffold] failed for product=%s", product.get("id"))
    return scaffolded


def _action_for_launch(launch_result: dict) -> str:
    """Translate launch_session's return into the run_cycle outer action.

    launch_session can finish in three states:
      - status="launched"          → a docker container actually started
      - status="already_active"    → guard skipped (existing session running)
      - status="already_launching" → in-memory lock held by a concurrent call
    Plus error returns ({"ok": False, ...}) when launch_session crashed.

    Pre-fix this function returned "launched" unconditionally — every cycle
    logged "action=launched" even when the launch was skipped, making real
    loops indistinguishable from "Ollama session still grinding through
    turns" in operator diagnostics. (Real incident 2026-05-02: looked like
    the orchestrator was crash-looping reviewer; it was actually one
    healthy long-running session.)

    Now we report the truth so cycle logs reflect what happened.
    """
    if not isinstance(launch_result, dict) or not launch_result.get("ok"):
        return "launch_failed"
    status = (launch_result.get("data") or {}).get("status")
    if status == "launched":
        return "launched"
    if status in ("already_active", "already_launching"):
        return "skipped_active"
    return "launched"  # conservative default — newer statuses report as launched


def run_cycle(args: dict, **kwargs) -> str:
    """
    Run a complete orchestration cycle in Python:
    1. Heartbeat (returns 409_stop if lock stolen)
    2. Kill stale containers
    3. Reset stuck features
    4. DoD checks + PR reconciliation for all ready products
    5. Find next product + determine action
    6. If action==launch_session: return {action, product_id, persona}
    7. If action==plan_sprints: call plan-sprints and return {action: "exit"}
    8. If nothing to do: return {action: "exit"}
    """
    try:
        # 1. Heartbeat
        hb = json.loads(poller_heartbeat({}, **kwargs))
        if not hb.get("ok") or hb.get("status") == 409:
            return _ok({"action": "409_stop", "reason": "Lock stolen"})

        # 2. WATCHDOG — DB-authoritative. Asks PM API for sessions past their
        # expected_deadline or without a recent heartbeat, then kills their
        # docker containers and closes the DB records. No mtime parsing, no
        # RunningFor string matching, no GitHub API calls. Single source of
        # truth: the `sessions` table.
        try:
            with _pm_client() as client:
                wd_resp = client.get("/api/sessions/watchdog/targets")
                active_resp = client.get("/api/sessions/active")
            targets = wd_resp.json() if wd_resp.is_success else []
            active_sessions = active_resp.json() if active_resp.is_success else []

            # Phase II.X.3 fix (#3): also flag any running session whose
            # container_id is no longer present in `docker ps`. The watchdog
            # endpoint runs in pm-api which has no docker access, so the
            # docker-state probe lives here. Closes the orphan-session class
            # observed today (incident #1698) where the container died but
            # the session row stayed `running` until expected_deadline 90 min
            # away. The DB-only check would not have fired in that window.
            try:
                ps = subprocess.run(
                    ["docker", "ps", "--filter", "name=pf-", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=10,
                )
                live_containers = {n for n in ps.stdout.strip().splitlines()
                                   if n and n.startswith("pf-") and n != "pf-orchestrator"}
            except Exception:
                live_containers = None  # docker probe failed — be conservative, don't synthesize targets
                log.exception("[watchdog] docker ps probe failed")

            if live_containers is not None:
                already_targeted = {t["id"] for t in targets}
                for s in active_sessions:
                    if s.get("id") in already_targeted:
                        continue
                    cid = s.get("container_id")
                    if not cid:
                        continue
                    if cid not in live_containers:
                        targets.append({
                            "id":           s["id"],
                            "product_id":   s.get("product_id"),
                            "persona":      s.get("persona"),
                            "container_id": cid,
                            "session_uid":  s.get("session_uid"),
                            "reason":       "container exited (docker ps does not list it)",
                        })

            for t in targets:
                name = t.get("container_id") or f"pf-{t['product_id']}-{t.get('session_uid','')}"
                reason = t.get("reason", "watchdog")
                log.warning("[watchdog] killing session %s container=%s reason=%s",
                            t["id"], name, reason)
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
                with _pm_client() as client:
                    client.post(f"/api/sessions/{t['id']}/kill", json={"reason": reason})
        except Exception:
            log.exception("[watchdog] failed")

        reset_stuck_features({}, **kwargs)

        # 3. Active containers are tracked per-product via the DB and the
        # per-product launch_lock. We no longer short-circuit globally on
        # "any pf-* running" — that blocks other products unnecessarily.
        # The round-robin product selection below already picks products that
        # don't have active sessions; the launch_lock prevents double-spawn
        # for a given product. No global guard needed.

        # 5. Per-product preflight
        products_raw = json.loads(get_products({}, **kwargs))
        products_data = products_raw.get("data") if isinstance(products_raw, dict) else products_raw
        products = products_data if isinstance(products_data, list) else []

        # 5α. PAT-rotation auto-detect. Cheap on the steady state (string
        # compare); only rewrites every product's .git/config when the PM
        # rotates the token in system_config.
        try:
            with _pm_client() as client:
                _sc = client.get("/api/system-config").json()
            _maybe_rotate_pat(products, _sc.get("github_pat") or "")
        except Exception:
            log.exception("[pat-rotate] auto-detect failed")

        # 5a. Scaffold greenfield_pending products (creates GitHub repo, deploy
        # key, initial files, flips status → registered).
        try:
            n = _scaffold_greenfield_pending(products, **kwargs)
            if n:
                products_raw = json.loads(get_products({}, **kwargs))
                products_data = products_raw.get("data") if isinstance(products_raw, dict) else products_raw
                products = products_data if isinstance(products_data, list) else []
        except Exception:
            log.exception("[scaffold] greenfield_pending pass failed")

        # 5b. Discover registered products (reads working_dir, installs agent
        # templates, detects stack, flips status → ready). Auto-promotes
        # greenfield 'discovered' → 'ready' so wizard-submitted products run
        # without a separate PM approval click.
        try:
            n = _discover_registered_products(products, **kwargs)
            if n:
                products_raw = json.loads(get_products({}, **kwargs))
                products_data = products_raw.get("data") if isinstance(products_raw, dict) else products_raw
                products = products_data if isinstance(products_data, list) else []
        except Exception:
            log.exception("[discover] registered pass failed")

        ready = [p for p in products if p.get("status") in ("ready", "running")]

        with _pm_client() as client:
            for p in ready:
                sid = p.get("active_sprint_id")
                if sid:
                    try:
                        client.post(f"/api/sprints/{sid}/check-dod")
                    except Exception:
                        pass

        for p in ready:
            try:
                reconcile_prs({"product_id": p["id"]}, **kwargs)
            except Exception:
                pass

        # 5. Find next work
        # Priority 1: reviewer work across all products
        reviewer_raw = json.loads(_pm("GET", "/api/features/next-for-persona?persona=reviewer"))
        reviewer_feature = (reviewer_raw.get("data") if isinstance(reviewer_raw, dict) else reviewer_raw)
        if reviewer_feature and isinstance(reviewer_feature, dict):
            pid = reviewer_feature.get("product_id")
            if any(p["id"] == pid and p.get("status") in ("ready", "running") for p in products):
                # Actually spawn the reviewer container (consistent with the
                # launch_session branch below — run_cycle is single-call).
                launch_result = json.loads(launch_session(
                    {"product_id": pid, "persona": "reviewer"}, **kwargs
                ))
                # Translate launch_session's actual outcome into the action.
                # Without this the parent log line says "action=launched" even
                # when launch_session skipped because of an active session,
                # making real loops indistinguishable from "session still
                # grinding through Ollama turns" in operator diagnostics.
                action = _action_for_launch(launch_result)
                return _ok({"action": action, "product_id": pid, "persona": "reviewer",
                            "reason": f"reviewer work for feature {reviewer_feature.get('id')}",
                            "launch": launch_result})

        # Priority 2: round-robin product
        next_raw = json.loads(_pm("GET", "/api/products/next"))
        next_product = (next_raw.get("data") if isinstance(next_raw, dict) else next_raw)
        if not next_product or not isinstance(next_product, dict):
            return _ok({"action": "exit", "reason": "No products need work"})

        product_id = next_product["id"]
        action_raw = json.loads(determine_next_action({"product_id": product_id}, **kwargs))
        action_data = action_raw.get("data") if isinstance(action_raw, dict) else action_raw
        if not isinstance(action_data, dict):
            return _ok({"action": "exit", "reason": "determine_next_action returned nothing"})

        action = action_data.get("action")
        if action == "plan_sprints":
            _pm("POST", f"/api/products/{product_id}/plan-sprints")
            return _ok({"action": "exit", "reason": f"Planned sprints for product {product_id}"})
        elif action == "launch_session":
            persona = action_data.get("persona", "planner")
            # Clear run_now before launching
            _pm("PATCH", f"/api/products/{product_id}", {"run_now": False})
            # Launch directly — we run as a deterministic orchestrator, no LLM
            # multi-turn reasoning needed.
            launch_result = json.loads(launch_session(
                {"product_id": product_id, "persona": persona}, **kwargs
            ))
            outer_action = _action_for_launch(launch_result)
            return _ok({"action": outer_action, "product_id": product_id, "persona": persona,
                        "reason": action_data.get("reason", ""),
                        "launch": launch_result})
        else:
            return _ok({"action": "exit", "reason": action_data.get("reason", "nothing to do")})

    except Exception as e:
        log.exception("run_cycle failed")
        return _err(f"run_cycle crashed: {e}")


def determine_next_action(args: dict, **kwargs) -> str:
    """
    Deterministic persona decision tree for a product.
    Returns {"action": "launch_session"|"plan_sprints"|"exit", "persona": ..., "reason": ...}.
    Call this after preflight to get the exact action to take.
    """
    product_id = args.get("product_id")
    _TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
    _IN_AGENT = {"Designing", "Implementing", "Reviewing"}

    try:
        with _pm_client() as client:
            features_resp  = client.get(f"/api/products/{product_id}/features")
            sprint_resp    = client.get(f"/api/products/{product_id}/sprints/active")
            syscfg_resp    = client.get("/api/system-config")
            # Use the dedicated /open_pr_count endpoint — /github/prs?state=open
            # doesn't exist (returns 404), which silently broke the PR cap gate.
            prs_resp       = client.get(f"/api/products/{product_id}/open_pr_count")

        features = features_resp.json() if features_resp.is_success else []
        active_sprint = sprint_resp.json() if sprint_resp.is_success else None
        sys_cfg = syscfg_resp.json() if syscfg_resp.is_success else {}
        prs_data = prs_resp.json() if prs_resp.is_success else {}
        open_pr_count = prs_data.get("count", 0) if isinstance(prs_data, dict) else 0
        max_prs = sys_cfg.get("max_open_prs") or int(os.environ.get("MAX_OPEN_PRS", "3"))

        # No active sprint
        if active_sprint is None:
            unsprinted = [f for f in features
                          if f.get("status") == "Approved" and f.get("sprint_id") is None]
            if unsprinted:
                return _ok({"action": "plan_sprints", "product_id": product_id,
                            "reason": f"{len(unsprinted)} Approved features have no sprint; call pm_api POST /api/products/{product_id}/plan-sprints"})
            # Backpressure: don't generate more features if backlog is already deep.
            # Counts ALL Approved features regardless of sprint, because planner
            # creates unsprinted Approved features — reaching cap means coder is
            # behind and making more ideas will just pile up more "Approved" work.
            all_approved = [f for f in features if f.get("status") == "Approved"]
            max_pending = (sys_cfg.get("max_pending_approved")
                           or int(os.environ.get("MAX_PENDING_APPROVED", "10")))
            if len(all_approved) >= max_pending:
                return _ok({"action": "exit",
                            "reason": f"Planner gated: {len(all_approved)} Approved features already pending (cap {max_pending})"})
            return _ok({"action": "launch_session", "persona": "planner",
                        "product_id": product_id, "reason": "No active sprint, no approved features — planner generates backlog"})

        sid = active_sprint["id"]
        sprint_features = [f for f in features if f.get("sprint_id") == sid]
        non_terminal = [f for f in sprint_features if f.get("status") not in _TERMINAL]

        if not non_terminal:
            # All features in the active sprint are merged. Run the post-sprint
            # regression chain — each persona launches as its own session so
            # the History tab shows a real audit trail of what verified the
            # sprint, and each can sign off its DoD gate independently:
            #   1. qa_tester        → runs full test suite on main, signs qa_passed
            #   2. security_auditor → audits merged code, signs security_clean
            #   3. check-dod auto-completes the sprint once both above are signed
            #   4. retrospective    → writes retro_sprint_<id>.md, signs retro_done
            #
            # Source of truth for gate state is /api/sprints/{id}/check-dod
            # (POST returns the live evaluation including the auto-recompute
            # rules from earlier today). The active_sprint payload from
            # /products/{id}/sprints/active also carries dod_status + status.
            try:
                with _pm_client() as client:
                    cd = client.post(f"/api/sprints/{sid}/check-dod").json()
                dod = cd.get("dod") if isinstance(cd, dict) else {}
            except Exception:
                cd, dod = {}, {}

            if not dod.get("qa_passed"):
                return _ok({"action": "launch_session", "persona": "qa_tester",
                            "product_id": product_id,
                            "reason": f"sprint {sid}: all features Pushed — running QA regression to sign qa_passed"})
            if not dod.get("security_clean"):
                return _ok({"action": "launch_session", "persona": "security_auditor",
                            "product_id": product_id,
                            "reason": f"sprint {sid}: all features Pushed — running security audit to sign security_clean"})

            # Both verification gates signed. check-dod above will have
            # auto-completed the sprint already if the structural gates pass
            # too (it returns action=auto_completed in that case).
            #
            # Retrospective writes retro_sprint_<id>.md, files action items,
            # and signs retro_done. Triggered on completed sprints with no
            # retro_doc_path yet. We use the active_sprint payload's status +
            # retro_doc_path here (active_sprint is fetched at the top of
            # determine_next_action and is the freshest snapshot).
            sprint_status_now = active_sprint.get("status")
            retro_done_path   = active_sprint.get("retro_doc_path")
            if cd.get("action") == "auto_completed" or sprint_status_now == "completed":
                if not retro_done_path:
                    return _ok({"action": "launch_session", "persona": "retrospective",
                                "product_id": product_id,
                                "reason": f"sprint {sid} completed — retrospective writing retro_sprint_{sid}.md"})
                return _ok({"action": "exit", "reason": f"sprint {sid} fully signed off + retro done"})

            return _ok({"action": "exit",
                        "reason": f"sprint {sid}: both gates signed but check-dod returned {cd.get('action','?')}"})

        reviewing = [f for f in non_terminal if f.get("status") == "Reviewing" and f.get("pr_number")]
        if reviewing:
            return _ok({"action": "launch_session", "persona": "reviewer",
                        "product_id": product_id, "reason": f"{len(reviewing)} features in Reviewing with PR"})

        approved_no_design = [f for f in non_terminal
                              if f.get("status") == "Approved" and not f.get("design_doc_path")]
        if approved_no_design:
            return _ok({"action": "launch_session", "persona": "product_planner",
                        "product_id": product_id, "reason": f"{len(approved_no_design)} Approved features need design docs"})

        # Coder-eligible features:
        #   - Designed (fresh from designer)
        #   - Approved with design_doc_path (skip-design products)
        #   - Implementing + review_outcome=changes_requested
        #     (reviewer rejected, coder needs another pass — without this,
        #     these sit "stuck in agent state" for 45 min until reset_stuck
        #     drops them back to Designed, even though /next-for-persona?
        #     persona=coder already returns them)
        codeable = [f for f in non_terminal
                    if f.get("status") == "Designed"
                    or (f.get("status") == "Approved" and f.get("design_doc_path"))
                    or (f.get("status") == "Implementing"
                        and f.get("review_outcome") == "changes_requested")]
        if codeable:
            new_pr_codeable = [f for f in codeable if not f.get("pr_number")]
            if new_pr_codeable and open_pr_count >= max_prs:
                return _ok({"action": "exit", "reason": f"PR gate: {open_pr_count} open PRs >= max {max_prs} (and {len(new_pr_codeable)} features need a new PR)"})
            return _ok({"action": "launch_session", "persona": "coder",
                        "product_id": product_id, "reason": f"{len(codeable)} features ready to code"})

        in_agent_stuck = [f for f in non_terminal if f.get("status") in _IN_AGENT]
        if in_agent_stuck:
            return _ok({"action": "exit", "reason": f"{len(in_agent_stuck)} features stuck in agent state; reset_stuck will handle"})

        # Sprint backfill: when about to exit idle but there are unsprinted
        # Approved features (typically filed by audit/retro/recommender), pull
        # them into the active sprint up to the max_features_per_sprint cap.
        # Without this, audit findings pile up invisibly while the dispatcher
        # idles ("No actionable work found") because it only scans the active
        # sprint when one exists. See deadlock #C in today's analysis.
        unsprinted = [f for f in features
                      if f.get("status") == "Approved" and f.get("sprint_id") is None]
        if unsprinted:
            cap = int(sys_cfg.get("max_features_per_sprint") or 5)
            in_sprint_count = len(non_terminal)  # already excludes terminal statuses
            slots = cap - in_sprint_count
            if slots > 0:
                # Prioritise bug > chore > feature, then higher priority, then id.
                _type_priority = {"bug": 0, "chore": 1, "feature": 2}
                unsprinted.sort(key=lambda f: (
                    _type_priority.get(f.get("feature_type", "feature"), 3),
                    -(f.get("priority") or 0),
                    f.get("id", 0),
                ))
                to_assign = unsprinted[:slots]
                assigned_ids: list[int] = []
                with _pm_client() as client:
                    for f in to_assign:
                        try:
                            r = client.patch(f"/api/features/{f['id']}",
                                             json={"sprint_id": sid,
                                                   "changed_by": "sprint-backfill"})
                            if r.is_success:
                                assigned_ids.append(f["id"])
                        except Exception:
                            pass
                if assigned_ids:
                    return _ok({"action": "exit",
                                "reason": f"sprint-backfill: assigned {len(assigned_ids)} unsprinted "
                                          f"Approved features {assigned_ids} → sprint {sid}; "
                                          f"next cycle will pick them up"})

        return _ok({"action": "exit", "reason": "No actionable work found"})

    except Exception as e:
        return _err(f"determine_next_action failed: {e}")


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
    host = f"orchestrator-{socket.gethostname()}"
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

_PERSONA_ALIASES = {
    "developer": "coder",
    "dev": "coder",
    "coding": "coder",
    "programmer": "coder",
    "writer": "documenter",
    "retro": "retrospective",
    "design": "designer",
    "planner": "planner",
    "qa": "qa_tester",
    "tester": "qa_tester",
    "security": "security_auditor",
    "auditor": "security_auditor",
}

# Launch-lock: prevents duplicate launches for the same product while
# docker_runner is doing setup (workspace sync, feature assign) before it
# POSTs /api/sessions. Without this, run_cycle re-launches every 60s because
# it sees neither an active container nor an active DB session yet.
import threading as _threading
_LAUNCH_LOCK = _threading.Lock()
_LAUNCHING: set[int] = set()


def launch_session(args: dict, **kwargs) -> str:
    """
    Spawn the agent Docker container for the given product+persona.
    Returns immediately — the container runs in a background daemon thread.
    A launch-lock prevents the same product from being launched concurrently
    while docker_runner is still in its setup phase.
    """
    product_id = args.get("product_id")
    persona = args.get("persona")
    if persona in _PERSONA_ALIASES:
        log.warning("launch_session: aliasing persona %r → %r", persona, _PERSONA_ALIASES[persona])
        persona = _PERSONA_ALIASES[persona]

    # DB-backed guard: survives orchestrator restarts (the in-memory
    # _LAUNCHING set doesn't). If the product already has a session in
    # pending/starting/running state, do NOT launch a second one.
    try:
        with _pm_client() as client:
            active = client.get("/api/sessions/active", params={"product_id": product_id})
        if active.is_success and active.json():
            existing = active.json()
            log.info("launch_session: product %s already has active session %s (status=%s) — skipping",
                     product_id, existing.get("id"), existing.get("status"))
            return _ok({"status": "already_active", "product_id": product_id,
                        "existing_session_id": existing.get("id")})
    except Exception:
        pass  # PM API transient — fall through to in-memory lock

    # In-memory lock catches the narrow window where two launches race
    # before either has created a DB session record.
    with _LAUNCH_LOCK:
        if product_id in _LAUNCHING:
            log.info("launch_session: product %s already launching, skipping", product_id)
            return _ok({"status": "already_launching", "product_id": product_id})
        _LAUNCHING.add(product_id)

    try:
        from orchestrator.docker_runner import run_claude_in_docker  # type: ignore
        with _pm_client() as client:
            product_resp = client.get(f"/api/products/{product_id}")
        if not product_resp.is_success:
            with _LAUNCH_LOCK:
                _LAUNCHING.discard(product_id)
            return _err(f"product {product_id} not found", status=product_resp.status_code)
        product = product_resp.json()

        # Guard: planner with unsprinted Approved features → call plan-sprints instead
        if persona == "planner":
            with _pm_client() as client:
                active_sprint_resp = client.get(f"/api/products/{product_id}/sprints/active")
                features_resp = client.get(f"/api/products/{product_id}/features")
            active_sprint = active_sprint_resp.json() if active_sprint_resp.is_success else None
            if active_sprint is None:
                features = features_resp.json() if features_resp.is_success else []
                if isinstance(features, list):
                    _TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
                    unsprinted = [f for f in features
                                  if f.get("status") == "Approved" and f.get("sprint_id") is None
                                  and f.get("status") not in _TERMINAL]
                    if unsprinted:
                        log.info("launch_session(planner) intercepted: calling plan-sprints for %d features", len(unsprinted))
                        with _LAUNCH_LOCK:
                            _LAUNCHING.discard(product_id)
                        return _pm("POST", f"/api/products/{product_id}/plan-sprints")

        # Fire and forget — run_claude_in_docker blocks for up to SESSION_TIMEOUT minutes.
        def _run():
            try:
                exit_code = run_claude_in_docker(product, persona=persona)
                log.info("launch_session background: product=%s persona=%s exit=%d",
                         product_id, persona, exit_code)
            except Exception:
                log.exception("launch_session background thread crashed")
            finally:
                with _LAUNCH_LOCK:
                    _LAUNCHING.discard(product_id)

        t = _threading.Thread(target=_run, daemon=True, name=f"session-{product_id}-{persona}")
        t.start()
        return _ok({"status": "launched", "product_id": product_id, "persona": persona,
                    "note": "container started in background — run_cycle will skip if already running"})
    except Exception as e:
        with _LAUNCH_LOCK:
            _LAUNCHING.discard(product_id)
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
