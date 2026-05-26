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


# Optional HMAC signing for internal write endpoints (Phase #9 opt-in).
# When PF_INTERNAL_API_SECRET is set on BOTH the orchestrator and the website,
# every outbound POST/PATCH gets an X-PF-Signature header keyed by the secret.
# The website's verify_internal_signature dependency rejects unsigned writes
# when the secret is set there. Unset on either side = transparent no-op.
_INTERNAL_API_SECRET = os.environ.get("PF_INTERNAL_API_SECRET", "")


def _sign_internal_body(body: bytes) -> str:
    """Compute `sha256=<hex>` HMAC-SHA256 of body keyed by the internal secret."""
    import hashlib, hmac as _hmac
    digest = _hmac.new(_INTERNAL_API_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _SigningClient(httpx.Client):
    """httpx.Client that auto-signs every POST/PATCH/PUT/DELETE body when
    PF_INTERNAL_API_SECRET is set. Read methods (GET, HEAD) are untouched.

    The signature is computed against the JSON-serialized body matching what
    the website's verify_internal_signature reads from request.body() — so we
    must pre-serialize once and pass `content=` rather than `json=` for write
    methods. Falls back to no-op when the secret is unset.
    """
    def request(self, method: str, url, **kwargs):
        if _INTERNAL_API_SECRET and method.upper() in ("POST", "PATCH", "PUT", "DELETE"):
            json_body = kwargs.pop("json", None)
            # httpx.Client.post() passes every body kwarg (content/data/files/json)
            # in the call to self.request(); content is usually None. We only
            # take over when there's a json body AND no explicit content.
            if json_body is not None and not kwargs.get("content"):
                payload = json.dumps(json_body).encode()
                kwargs["content"] = payload
                headers = dict(kwargs.get("headers") or {})
                headers["X-PF-Signature"] = _sign_internal_body(payload)
                headers.setdefault("Content-Type", "application/json")
                kwargs["headers"] = headers
            elif json_body is not None:
                # Caller passed both json and content — restore json so the
                # ambiguity surfaces as an httpx error, don't silently drop it.
                kwargs["json"] = json_body
        return super().request(method, url, **kwargs)


def _pm_client() -> httpx.Client:
    auth = (PM_USERNAME, PM_PASSWORD) if PM_PASSWORD else None
    return _SigningClient(base_url=PM_API_URL, timeout=REQUEST_TIMEOUT, auth=auth)


def _get_github_token() -> str:
    """Return a fresh GitHub App installation token, or ``""``.

    Per CLAUDE.md "Git auth: GitHub App only": the PAT fallback was
    removed in the drift-cleanup pass. The App module caches the
    installation token and auto-refreshes when <5 min of life remains;
    on mint failure the empty string surfaces here so callers fail
    visibly rather than silently degrade.
    """
    try:
        from orchestrator.integrations.github_app import get_installation_token
        return get_installation_token() or ""
    except Exception as e:
        log.warning("App token mint failed: %s", e)
        return ""


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


def _bump_product_last_run(product_id: int) -> None:
    """Mark this product as visited by the orchestrator (advances round-robin).

    Best-effort PATCH — never raises so a transient PM API hiccup doesn't
    crash the cycle. Round-robin is `last_run_at ASC NULLS FIRST`, so
    bumping to NOW pushes this product to the back of the queue and the
    next cycle picks the next-oldest product.
    """
    try:
        from datetime import datetime as _dt, timezone as _tz
        _pm("PATCH", f"/api/products/{product_id}",
            {"last_run_at": _dt.now(_tz.utc).isoformat()})
    except Exception:
        log.debug(f"[round-robin] could not bump last_run_at for product {product_id}")


_PRODUCT_KEEP = {"id", "name", "status", "working_dir", "github_repo", "tech_stack",
                 "run_now", "run_trainer_now", "run_persona_now",
                 "quiet_hours_start", "quiet_hours_end",
                 "daily_session_cap", "last_run_at", "config", "type"}

_ONDEMAND_PERSONAS = ("documenter", "analytics", "refactorer", "devops", "recommender",
                      # Phase 8 of quality-specs (2026-05-19): architect persona
                      # for quantitative drift detection. Maintenance-shape
                      # (read-only on source); PM triggers on demand via
                      # run_persona_now="architect", or the cycle/persona gate
                      # fires it automatically every ~50 features pushed.
                      "architect")
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


_SYSCFG_KEEP = {"auto_merge_enabled", "stuck_feature_timeout_hours",
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

        # 1b. Re-normalise the bind-mounted ~/.ssh perms. Windows Docker
        # bind-mounts surface any host-side write as `root:root 777`, which
        # OpenSSH rejects ("Bad owner or permissions"). bootstrap.sh fixes
        # this at container startup but cannot react to subsequent host
        # writes. Running an alpine sidecar once per cycle catches any
        # manual ~/.ssh/config edits or scaffolding writes before the
        # next git fetch/push fires. Best-effort, non-fatal — see helper
        # docstring for the underlying incident.
        try:
            from orchestrator.integrations.docker_cli import _chmod_ssh_dir_via_alpine  # type: ignore
            _chmod_ssh_dir_via_alpine()
        except Exception:
            log.exception("ssh-perm helper failed (non-fatal)")

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

        # Phase 1 of PollerRevamp (INVARIANTS.md VII.1): per-cycle auto-merge
        # sweep. Walks every ready product and squash-merges any feature that
        # is Reviewed+approved+pr_number, regardless of sprint membership.
        # In sprint-PR mode (VII.5) it holds the sprint PR until every feature
        # in the sprint is merge-eligible, preventing premature ship of
        # incomplete work. Best-effort — never raises into run_cycle.
        try:
            from orchestrator.auto_merge import sweep_all  # type: ignore
            with _pm_client() as client:
                _sc_for_merge = client.get("/api/system-config").json()
            if isinstance(_sc_for_merge, dict):
                sweep_all(ready, _sc_for_merge)
        except Exception:
            log.exception("auto-merge sweep failed (non-fatal)")

        # Phase 3 of PollerRevamp (INVARIANTS.md VIII.2): when an active
        # sprint's security_clean gate is False and there are unsprinted bug
        # features, route them into the active sprint so the coder can ship
        # them. Side-effect only; determine_next_action's existing decision
        # tree picks them up after they're sprinted.
        for p in ready:
            try:
                _route_unsprinted_security_bugs(p)
            except Exception:
                log.exception(f"bug-routing failed for product {p.get('id')}")

        # Phase-1 supervisor detectors that operate per-product on data the
        # PM API already serves cheaply: orphan-Approved features and rapid
        # status flaps. Both run every cycle (each has its own cooldown to
        # prevent action spam). Best-effort — never raises.
        for p in ready:
            try:
                _run_supervisor_per_product_detectors(p)
            except Exception:
                log.exception(f"supervisor per-product detectors failed for product {p.get('id')}")

        # Per-cycle architect scheduler. Queues the architect persona when
        # features-pushed delta crosses N (default 3) or the 7-day fallback
        # elapses. Sets run_persona_now="architect" so the Priority-0 block
        # below picks it up on this same cycle. See _check_architect_due for
        # the trigger logic.
        for p in ready:
            try:
                _check_architect_due(p)
            except Exception:
                log.exception(f"architect-scheduler failed for product {p.get('id')}")

        # Priority 0: PM-triggered on-demand sessions. Bypasses round-robin
        # and the determine_next_action decision tree — the PM clicked a
        # button, run that persona for that product. Flag is cleared up
        # front so a crashing launch doesn't re-fire on every cycle.
        #
        # 2026-05-19: the up-front clear was over-eager. launch_session has
        # two non-error skip paths (status=already_active / already_launching)
        # that fire when the product happens to have a coder or designer
        # session in flight at the moment the PM clicks the button. Those are
        # not crashes — they're "try again next cycle." Pre-fix, the flag was
        # already cleared by then and the request was silently dropped; the
        # PM had to re-click. Now we inspect launch_session's status and
        # restore the flag on those specific deferred cases. A real crash
        # (run_claude_in_docker raising, GitHub API down, etc.) still doesn't
        # carry those status values, so the original crash-safety is preserved.
        for p in ready:
            persona = None
            patch: dict = {}
            restore_patch: dict = {}
            queued = p.get("run_persona_now")
            if queued and queued in _ONDEMAND_PERSONAS:
                persona = queued
                patch["run_persona_now"] = None
                restore_patch["run_persona_now"] = queued
            elif p.get("run_trainer_now"):
                persona = "product_trainer"
                patch["run_trainer_now"] = False
                restore_patch["run_trainer_now"] = True
            if not persona:
                continue
            pid = p["id"]
            try:
                _pm("PATCH", f"/api/products/{pid}", patch)
            except Exception:
                log.exception(f"[on-demand] could not clear flag on product {pid}")
            log.info(f"[on-demand] launching {persona} for product {pid} ({p.get('name','?')})")
            launch_result = json.loads(launch_session(
                {"product_id": pid, "persona": persona}, **kwargs
            ))
            launch_data = launch_result.get("data") if isinstance(launch_result, dict) else None
            if isinstance(launch_data, dict) and launch_data.get("status") in (
                "already_active", "already_launching",
            ):
                log.info(
                    f"[on-demand] launch deferred (status={launch_data.get('status')}, "
                    f"existing_session={launch_data.get('existing_session_id')}) — "
                    f"restoring {list(restore_patch)[0]} on product {pid}"
                )
                try:
                    _pm("PATCH", f"/api/products/{pid}", restore_patch)
                except Exception:
                    log.exception(f"[on-demand] could not restore flag on product {pid}")
                # Honest reporting (matches Priority 2 below) — don't claim
                # "launched" when launch_session short-circuited via the
                # active-session guard.
                return _ok({"action": "deferred", "product_id": pid, "persona": persona,
                            "reason": (
                                f"on-demand {persona} deferred "
                                f"(status={launch_data.get('status')}); "
                                f"flag restored for next cycle"
                            ),
                            "launch": launch_result})
            return _ok({"action": "launched", "product_id": pid, "persona": persona,
                        "reason": f"on-demand {persona}",
                        "launch": launch_result})

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
                # If launch_session deferred because a session is already
                # running on this product, fall through to Priority 2
                # (round-robin) so OTHER products' work isn't starved for
                # the duration of the active session. Pre-fix (2026-05-19)
                # this branch returned action=launched regardless of the
                # deferral, which (a) produced misleading "launched reviewer"
                # log lines every cycle for the duration of the blocking
                # session (typically up to 90 minutes), and (b) starved
                # other ready products of any dispatch slot during that
                # window. See the e60fd18 precedent for the Priority-0
                # on-demand path (which had the same shape).
                launch_data = launch_result.get("data") if isinstance(launch_result, dict) else None
                if isinstance(launch_data, dict) and launch_data.get("status") in (
                    "already_active", "already_launching",
                ):
                    log.info(
                        f"[reviewer] deferred for product {pid} "
                        f"(feature {reviewer_feature.get('id')}, status="
                        f"{launch_data.get('status')}, existing_session="
                        f"{launch_data.get('existing_session_id')}) — falling "
                        f"through to round-robin"
                    )
                    # fall through (don't return) so Priority 2 gets a turn
                else:
                    return _ok({"action": "launched", "product_id": pid, "persona": "reviewer",
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
            # Even on error, mark this product visited so round-robin advances.
            _bump_product_last_run(product_id)
            return _ok({"action": "exit", "reason": "determine_next_action returned nothing"})

        # Bump last_run_at on every cycle visit, NOT just on successful Docker
        # exits. Without this, products that resolve to action=exit (no
        # actionable work, PR-gated, etc.) keep their stale last_run_at
        # forever and dominate the round-robin — starving other products.
        # The launch_session branch's post-success bump still happens; this
        # is just defensive coverage for the no-launch paths.
        _bump_product_last_run(product_id)

        action = action_data.get("action")
        if action == "plan_sprints":
            # Capture + log the response so silent failures are visible.
            # Pre-2026-05-21 the response was discarded — a 409 from the
            # endpoint looked identical to a 201 in the cycle log, and the
            # orchestrator looped on the same plan_sprints decision every
            # cycle (e.g. MyDocusign 25+ min idle after the Blocked-sprint
            # holdpen tripped the 409 guard).
            plan_response = _pm("POST", f"/api/products/{product_id}/plan-sprints")
            try:
                parsed = json.loads(plan_response)
                # pm_api wraps successes as {"ok": True, "data": ...}; non-2xx
                # surface as {"ok": False, "error": "..."} or include status in
                # the error string.
                if isinstance(parsed, dict) and not parsed.get("ok", True):
                    log.warning(
                        f"[plan-sprints] product {product_id}: planning call "
                        f"failed: {parsed.get('error') or plan_response[:200]}"
                    )
                else:
                    log.info(
                        f"[plan-sprints] product {product_id}: planning call "
                        f"returned {plan_response[:200]}"
                    )
            except Exception:
                log.info(f"[plan-sprints] product {product_id}: {plan_response[:200]}")
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
            # Same shape as the Priority 0 (on-demand) and Priority 1 (reviewer)
            # branches: report the deferred status honestly when launch_session
            # short-circuits via the active-session guard. Priority 2 is the
            # terminal priority — there's nothing to fall through to — so this
            # is a log-honesty fix, not a behavior change. action="deferred"
            # makes the cycle-summary line accurate (operator was seeing
            # "launched session" every cycle while a coder was running, with no
            # container actually spawned).
            launch_data = launch_result.get("data") if isinstance(launch_result, dict) else None
            if isinstance(launch_data, dict) and launch_data.get("status") in (
                "already_active", "already_launching",
            ):
                return _ok({"action": "deferred", "product_id": product_id, "persona": persona,
                            "reason": (
                                f"launch deferred (status={launch_data.get('status')}, "
                                f"existing_session={launch_data.get('existing_session_id')}); "
                                f"product {product_id} already has work in flight"
                            ),
                            "launch": launch_result})
            return _ok({"action": "launched", "product_id": product_id, "persona": persona,
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

    Phase 5 of OrchestratorRefactor: the body now lives in
    ``orchestrator.cycle.persona._decide_action``. This function is a thin
    wrapper that supplies the signing _pm_client() and JSON-wraps the
    response in tools.py's standard {"ok": ..., "data": ...} envelope.

    Returns ``{"ok": True, "data": {"action": "launch_session"|"plan_sprints"|"exit", ...}}``.
    """
    product_id = args.get("product_id")
    try:
        from orchestrator.cycle.persona import _decide_action  # type: ignore
        with _pm_client() as client:
            result = _decide_action(product_id, client)
        return _ok(result)
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
        # Phase 4 of PollerRevamp: route through orchestrator.reconcile, the
        # single per-product entry point that sequences reconcile_merged_prs
        # then reconcile_in_flight_prs with isolated try/except per pass.
        from orchestrator.reconcile import reconcile_product  # type: ignore
        with _pm_client() as client:
            product_resp = client.get(f"/api/products/{product_id}")
        if not product_resp.is_success:
            return _err(f"product {product_id} not found")
        product = product_resp.json()
        reconcile_product(product)
        # Phase-1 supervisor detectors that operate on open PRs.
        # Cheap to run after the reconcile pass since we re-hit GitHub once
        # for the full open-PR list. Best-effort — never raises.
        try:
            _run_supervisor_pr_detectors(product)
        except Exception:
            log.exception("supervisor PR detectors failed")
        # Open-PR-count invariant retired with the two-tier (session-PR)
        # model: a healthy product now has 1 sprint integration PR + N
        # session PRs open in parallel, so the >1 alert would fire every
        # cycle. The reconcile sweep above already detects orphan PRs.
        return _ok({"reconciled": product_id})
    except Exception as e:
        log.exception("reconcile_prs failed")
        return _err(f"reconcile_prs failed: {e}")


def _run_supervisor_per_product_detectors(product: dict) -> None:
    """Per-cycle supervisor detectors that operate on product-level state
    fetched from the PM API: orphan-Approved + rapid status flap.
    """
    import httpx as _httpx
    from orchestrator.supervisor import detect_orphan_approved, detect_rapid_flap  # type: ignore

    pid = product.get("id")
    if not pid:
        return

    # Pull features once (full payload — orphan detector needs updated_at)
    try:
        with _pm_client() as client:
            feat_resp = client.get(f"/api/products/{pid}/features")
            features = feat_resp.json() if feat_resp.is_success else []
    except Exception:
        features = []
    if isinstance(features, list):
        try:
            detect_orphan_approved(product_id=pid, features=features)
        except Exception:
            log.exception(f"orphan_approved detector failed for product {pid}")

    # Pull flapping features (uses default thresholds from system_config
    # — endpoint accepts overrides via query string but we fall back to
    # the supervisor defaults to keep wiring simple).
    try:
        with _pm_client() as client:
            sc_resp = client.get("/api/system-config")
            sc = sc_resp.json() if sc_resp.is_success else {}
        win = sc.get("supervisor_rapid_flap_window_hours") or 1
        thr = sc.get("supervisor_rapid_flap_min_transitions") or 5
        with _pm_client() as client:
            flap_resp = client.get(
                f"/api/products/{pid}/flapping-features",
                params={"window_hours": win, "min_transitions": thr},
            )
            flapping = flap_resp.json() if flap_resp.is_success else []
        if isinstance(flapping, list) and flapping:
            detect_rapid_flap(product_id=pid, flapping_features=flapping)
    except Exception:
        log.exception(f"rapid_flap detector failed for product {pid}")


_DEFAULT_ARCHITECT_PUSHED_THRESHOLD = 3
_ARCHITECT_FALLBACK_SECONDS = 7 * 24 * 3600


def _check_architect_due(product: dict) -> None:
    """Per-cycle: launch the architect persona when ARCHITECTURE.md drift
    is likely. Two triggers, either is sufficient:

      1. **Delta-since-last-run >= N.** Count of features with status=Pushed
         minus `product.config.features_pushed_at_last_architect`. N defaults
         to 3, soft-overrideable via `system_config.architect_pending_threshold`
         (no migration required — falls back if the column doesn't exist).
         The architect persona's prompt step 7 already writes both
         `last_architect_at` and `features_pushed_at_last_architect` on
         completion, so the counters self-maintain.

      2. **7-day fallback.** Even if no features pushed (dormant product),
         run architect once a week to catch quality-gate tampering,
         DEPRECATED entries whose files were quietly deleted, etc. Without
         this, a paused product's ARCHITECTURE.md never gets reviewed.

    Side-effect: PATCHes `run_persona_now="architect"` on the product. The
    existing on-demand launch path at run_cycle line 603+ then picks it up
    on the same cycle. If `run_persona_now` is already set (PM clicked a
    button, or a prior scheduler decision wasn't consumed yet), do nothing
    -- the existing flag wins.

    Best-effort: never raises into the cycle.
    """
    import httpx as _httpx
    from datetime import datetime as _dt, timezone as _tz

    pid = product.get("id")
    if not pid:
        return
    if product.get("run_persona_now"):
        return  # something else already queued; don't overwrite

    cfg = product.get("config") or {}
    threshold = _DEFAULT_ARCHITECT_PUSHED_THRESHOLD
    last_at_iso = cfg.get("last_architect_at")
    last_pushed_count = cfg.get("features_pushed_at_last_architect") or 0

    try:
        with _pm_client() as client:
            sc_resp = client.get("/api/system-config")
            sc = sc_resp.json() if sc_resp.is_success else {}
        if isinstance(sc, dict):
            override = sc.get("architect_pending_threshold")
            if isinstance(override, int) and override > 0:
                threshold = override
    except Exception:
        pass  # use default

    # Count Pushed features. Cheap GET of just status to avoid serialising
    # full feature payloads -- but the existing /api/products/{id}/features
    # endpoint doesn't support field projection, so we fetch and filter.
    # If the per-product feature count grows huge this is worth revisiting.
    pushed_count = 0
    try:
        with _pm_client() as client:
            f_resp = client.get(f"/api/products/{pid}/features")
            features = f_resp.json() if f_resp.is_success else []
        if isinstance(features, list):
            pushed_count = sum(1 for f in features if f.get("status") == "Pushed")
    except Exception:
        return  # can't decide without features; defer to next cycle

    delta = pushed_count - int(last_pushed_count or 0)
    reasons: list[str] = []

    if delta >= threshold:
        reasons.append(
            f"{delta} features Pushed since last architect run "
            f"(threshold {threshold})"
        )

    if last_at_iso:
        try:
            last_at = _dt.fromisoformat(str(last_at_iso).replace("Z", "+00:00"))
            age = (_dt.now(_tz.utc) - last_at).total_seconds()
            if age >= _ARCHITECT_FALLBACK_SECONDS:
                reasons.append(f"7d fallback ({int(age/3600)}h since last run)")
        except Exception:
            pass
    else:
        # Never run before. Trigger on the fallback condition so first-cycle
        # ARCHITECTURE.md gets at least one architect pass without waiting
        # for the delta threshold.
        reasons.append("first architect run for this product")

    if not reasons:
        return

    # Advance the cadence counters AT QUEUE TIME, not at agent completion.
    # Originally the architect's prompt step 7 was responsible for writing
    # last_architect_at + features_pushed_at_last_architect via curl PATCH,
    # but Ollama LLMs (qwen3-coder etc.) routinely skip that step -- session
    # 2903 on MyDocusign 2026-05-20 exited 0 without writing the counter,
    # so the next cycle's scheduler re-fired the architect (since last_at
    # was still null) and session 2904 launched immediately after. With
    # cadence ownership in the scheduler, the counter advances even when
    # the agent fails / skips / crashes; the worst case is a missed drift
    # detection bounded by the 7-day fallback.
    #
    # Config is a JSONB column and the PATCH replaces it wholesale (no
    # field-level merge in api_update_product), so fetch existing config
    # and merge our two keys before sending.
    existing_cfg = dict(cfg)  # cfg captured at top of this function
    merged_cfg = {
        **existing_cfg,
        "last_architect_at": _dt.now(_tz.utc).isoformat(),
        "features_pushed_at_last_architect": pushed_count,
    }
    try:
        with _pm_client() as client:
            client.patch(
                f"/api/products/{pid}",
                json={
                    "run_persona_now": "architect",
                    "config": merged_cfg,
                },
            )
        # Mutate the in-memory product dict too so the Priority-0 on-demand
        # block in the same run_cycle picks the flag up immediately, AND so
        # subsequent _check_architect_due calls in the same cycle (e.g. if
        # multiple products are processed in a loop) see the updated cfg
        # for *this* product.
        product["run_persona_now"] = "architect"
        product["config"] = merged_cfg
        log.info(
            f"[architect-scheduler] product {pid} ({product.get('name','?')}): "
            f"queued architect -- {'; '.join(reasons)}"
        )
    except Exception:
        log.exception(
            f"[architect-scheduler] could not queue architect for product {pid}"
        )


def _route_unsprinted_security_bugs(product: dict) -> int:
    """Phase 3 of PollerRevamp (INVARIANTS.md VIII.2). When the active sprint's
    `security_clean` gate is False AND there are unsprinted bug features in
    Approved/Designed state, PATCH them into the active sprint so the coder
    predicate picks them up.

    Returns the number of bugs routed (0 if none, or if the gate is already
    clean). Standalone analog of dispatch._decide_route_unsprinted_security_bugs
    — duplicated here because the deployed orchestrator (tools.py) doesn't run
    the dispatch.py priority list. Same data-driven logic, same caveat: this
    does NOT directly clear the gate (the website's _evaluate_dod recomputes
    security_clean from sprint-bugs only); routing the bugs makes them visible
    to the coder so they can ship and clear the gate via the recompute.
    """
    pid = product.get("id")
    sid = product.get("active_sprint_id")
    if not pid or not sid:
        return 0
    try:
        with _pm_client() as client:
            dod_resp = client.get(f"/api/sprints/{sid}/dod")
            if not dod_resp.is_success:
                return 0
            dod_payload = dod_resp.json() or {}
            dod = dod_payload.get("dod") or {}
            if dod.get("security_clean") is True:
                return 0  # gate already clean

            blockers = (dod_payload.get("blockers") or {}).get("security_clean") or []
            unsprinted = [b for b in blockers if not b.get("sprint_id")]
            routable = [
                b for b in unsprinted
                if b.get("status") in ("Approved", "Designed")
            ]
            if not routable:
                return 0

            # Match website's _check_sprint_capacity: terminal features don't
            # consume sprint slots.
            sc_resp = client.get("/api/system-config")
            sys_cfg = sc_resp.json() if sc_resp.is_success else {}
            cap = int(sys_cfg.get("max_features_per_sprint") or 5)

            feats_resp = client.get(f"/api/products/{pid}/features")
            feats = feats_resp.json() if feats_resp.is_success else []
            terminal = {"Pushed", "Deferred", "Rejected", "Reverted"}
            in_sprint_active = sum(
                1 for f in (feats if isinstance(feats, list) else [])
                if f.get("sprint_id") == sid and f.get("status") not in terminal
            )
            slots = max(0, cap - in_sprint_active)
            if slots == 0:
                log.info(
                    f"[bug-routing] product={pid} sprint={sid}: security_clean=False with "
                    f"{len(routable)} unsprinted bug(s), but sprint at capacity ({cap}) — leaving in backlog"
                )
                return 0

            moved = 0
            for bug in routable[:slots]:
                try:
                    client.patch(
                        f"/api/features/{bug['id']}",
                        json={"sprint_id": sid, "changed_by": "orchestrator"},
                    )
                    log.info(
                        f"[bug-routing] product={pid} sprint={sid}: routed bug "
                        f"#{bug['id']} ({(bug.get('name') or '')[:40]}) into sprint"
                    )
                    moved += 1
                except Exception as e:
                    log.warning(f"[bug-routing] failed to route bug #{bug['id']}: {e}")
            return moved
    except Exception:
        log.exception(f"[bug-routing] crashed for product {pid}")
        return 0


def _run_supervisor_pr_detectors(product: dict) -> None:
    """Pull open PRs + features once, run dirty-PR + overlap-PR detectors."""
    import re as _re
    import httpx as _httpx
    from orchestrator.supervisor import detect_dirty_prs, detect_overlapping_prs  # type: ignore

    repo_url = product.get("github_repo") or ""
    if not repo_url:
        return
    m = _re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    if not m:
        return
    slug = m.group(1)

    # Bearer token — App installation first, PAT only as transition fallback.
    pat = _get_github_token()
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"

    # Fetch open PRs (one call covers both detectors)
    try:
        prs_resp = _httpx.get(
            f"https://api.github.com/repos/{slug}/pulls",
            headers=headers, params={"state": "open", "per_page": 30}, timeout=10,
        )
        prs = prs_resp.json() if prs_resp.status_code == 200 else []
    except Exception:
        prs = []
    if not isinstance(prs, list) or not prs:
        return

    # Augment each PR with last_commit_at (fetched per-PR; cap at 30 to bound
    # GitHub API calls per cycle).
    enriched = []
    for pr in prs[:30]:
        if not isinstance(pr, dict):
            continue
        pr_n = pr.get("number")
        try:
            commits_resp = _httpx.get(
                f"https://api.github.com/repos/{slug}/pulls/{pr_n}/commits",
                headers=headers, params={"per_page": 1, "direction": "desc"}, timeout=10,
            )
            cs = commits_resp.json() if commits_resp.status_code == 200 else []
            last_commit_at = (
                cs[-1]["commit"]["committer"]["date"]
                if cs and isinstance(cs, list) and cs[-1].get("commit") else None
            )
        except Exception:
            last_commit_at = None
        enriched.append({
            "number":          pr_n,
            "title":           pr.get("title", ""),
            "created_at":      pr.get("created_at"),
            "mergeable_state": pr.get("mergeable_state"),
            "last_commit_at":  last_commit_at,
        })

    # Pull features once for the dirty-PR feature reset
    try:
        with _pm_client() as client:
            feat_resp = client.get(f"/api/products/{product['id']}/features")
            features = feat_resp.json() if feat_resp.is_success else []
    except Exception:
        features = []
    if not isinstance(features, list):
        features = []

    # 1-PR model: every open PR is a session PR; no sprint integration
    # PR exists to exclude. Both detectors operate uniformly.
    detect_dirty_prs(
        product_id=product["id"], github_repo=repo_url,
        open_prs_with_state=enriched, features=features, github_token=pat,
    )
    detect_overlapping_prs(
        product_id=product["id"], github_repo=repo_url,
        open_prs=enriched, github_token=pat,
    )


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _github_context(product_id: int) -> tuple[str, str] | None:
    try:
        with _pm_client() as client:
            product = client.get(f"/api/products/{product_id}").json()
        repo_url = product.get("github_repo") or ""
        # App installation token (PAT fallback for transition release).
        # Minted per call by _get_github_token; the App module caches with
        # auto-refresh, so this is still effectively free.
        pat = _get_github_token()
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
